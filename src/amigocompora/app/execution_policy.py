"""Qué se ejecuta sin preguntar, cuánto, y qué queda anotado.

Tres piezas que van juntas porque una sin las otras no significa nada:

- **`ExecutionLedger`** — el registro append-only de lo ejecutado. Es lo que hace
  que el tope diario sea un tope: si sólo se contara lo de esta sesión, cerrar y
  abrir la aplicación lo borraría.
- **`AutonomyPolicy`** — si la ejecución desatendida está armada, y con qué
  condiciones. Armar es lo único que puede saltarse la pregunta al usuario.
- **`PolicyBypass`** — el gancho que `ConfirmationGateway` consulta antes de
  preguntar.

### El bypass no puede saltarse el modo, y no por disciplina

`PolicyBypass` se consulta **después** de que `ConfirmationGateway` haya
comprobado el modo, y por eso no hay ninguna rama en la que pueda evitar la
barrera: la comprobación del modo ya lanzó antes de llegar aquí. No es una regla
que este fichero recuerde cumplir, es el orden de las líneas del otro. Un bypass
que pudiera saltarse el modo convertiría la decisión del usuario sobre en qué
modo está la aplicación en un adorno.

### La asimetría de armar y desarmar

Armar exige la frase de autonomía; desarmar no exige nada. Es deliberado y es la
misma razón por la que un freno de emergencia no lleva llave: si el operador está
delante y quiere parar, bloquearle el freno es exactamente el fallo que no se
puede permitir. El coste de desarmar por error es perder una oportunidad; el de
no poder desarmar es no poder parar una máquina que está gastando dinero.

### El registro no lleva secretos, y hay una prueba que lo comprueba

Un `executions.jsonl` con la clave privada dentro sería el peor sitio posible
para ella: un fichero de texto plano que crece, que se copia al hacer una copia
de seguridad y que se adjunta a un informe de un fallo. Lo que se escribe son
hechos económicos: cuándo, en qué red, qué par, cuánto y con qué hash.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from io import SEEK_END
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import structlog

from amigocompora.app.confirmation import PendingAction
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.execution import ExecutionLimits, TriggerKind
from amigocompora.domain.models import (
    BroadcastReceipt,
    BroadcastStatus,
    PredictionPosition,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount

_log = structlog.get_logger(__name__)

#: Ventana del tope acumulado.
WINDOW: Final = timedelta(hours=24)

#: Estados que **consumen** tope. `PENDING` cuenta aunque todavía no se sepa el
#: desenlace: el dinero ya salió de la cartera, y contarlo es el lado por el que
#: interesa equivocarse. `REVERTED` no cuenta porque el swap no ocurrió — el gas
#: se gastó, pero el importe de referencia no se intercambió—, y `UNKNOWN`
#: tampoco porque no hay prueba de que ocurriera.
_COUNTED_STATUSES: Final = frozenset({BroadcastStatus.SUCCESS, BroadcastStatus.PENDING})

#: Tipo de asiento: una transacción que se emitió a una cadena, o una orden que
#: se publicó en un recinto que liquida fuera de ella. Se distingue porque lo
#: que se guarda en la columna del identificador **no es lo mismo** en los dos
#: casos —un hash de transacción se busca en un explorador de bloques y un
#: identificador de orden se busca en el recinto—, y un asiento que los
#: confundiera mandaría a quien lo lea a un sitio donde no hay nada.
TRANSACTION_KIND: Final = "transaction"
ORDER_KIND: Final = "order"

#: Tipo de asiento del **cobro** de una posición de predicción ya resuelta. Es el
#: único que entra dinero en vez de sacarlo, y por eso tiene nombre propio: sin
#: él, un cobro sería un `transaction` más y `counts_towards_limits` lo daría por
#: bueno en cuanto el recibo llegara en `SUCCESS` — con lo que cobrar una
#: posición gastaría presupuesto del día y podría dejar al usuario sin poder
#: operar por haber recuperado su propio dinero.
REDEEM_KIND: Final = "redeem"

#: Motivo con el que se anota una ejecución que no pasó por el diálogo. Aparece
#: tal cual en la traza de auditoría y en el registro.
POLICY_REASON: Final = "ejecutado sin confirmación por política de autonomía"


class PrivateKeySource(Protocol):
    """De dónde sale la clave privada. Se pide en el momento de firmar, no antes."""

    def require(self) -> str: ...

    def available(self) -> bool: ...


class AddressSource(Protocol):
    """De dónde sale la **dirección** pública, sin la clave privada.

    Es lo que necesita una lectura que sólo mira: enseñar el saldo de una
    cartera no debe leer la clave privada ni de refilón, y tipar el caso de uso
    contra `PrivateKeySource` obligaría a que el doble de prueba —y cualquier
    implementación futura— pudiera entregarla. Aquí no se puede: lo único que
    este protocolo entrega es una dirección, que es pública.

    `available()` no lanza —pinta botones—; `address()` devuelve `None` cuando
    no hay cartera, igual que el proveedor del llavero.
    """

    def address(self) -> str | None: ...

    def available(self) -> bool: ...


class PassphraseSource(Protocol):
    """De dónde sale la frase de autonomía."""

    def get(self) -> str | None: ...


class ExecutableIntent(Protocol):
    """Lo que la política necesita saber de una operación, sea cual sea su forma.

    Existe porque la comprobación de límites **no** depende de que la operación
    sea un swap: mira la red, los símbolos que toca, el motor que la construye y
    un importe ya expresado en la unidad del tope. Eso es cierto para un swap y
    para una orden de un mercado de predicción, y las dos cosas no se parecen en
    nada más.

    Se declara aquí, en la capa que lo consume, y no en el dominio: es la
    interfaz que **esta** política necesita, no una propiedad de las entidades.
    Un `ExecutionIntent` y un `PredictionOrderIntent` lo satisfacen sin heredar
    de nada ni conocerse entre sí.

    `label` es aparte de `tokens` a propósito: lo que se escribe en el registro
    es lo que un humano lee para reconstruir qué pasó —«ETH/USDC», «¿Invasión de
    Irán? — Sí»— y eso no siempre coincide con los símbolos que se comprueban
    contra la lista blanca.
    """

    @property
    def chain(self) -> str: ...

    @property
    def label(self) -> str: ...

    @property
    def tokens(self) -> tuple[str, ...]: ...

    @property
    def engine_id(self) -> str: ...

    @property
    def recipient(self) -> str: ...

    @property
    def notional(self) -> TokenAmount: ...

    @property
    def reference_value(self) -> TokenAmount | None: ...

    @property
    def measured(self) -> TokenAmount: ...

    @property
    def counts_towards_limits(self) -> bool:
        """Si esta operación **compromete** dinero, que es lo que el tope acota.

        Se declara en vez de deducirse del tipo porque el tipo no lo dice: un
        swap y una orden de predicción entregan dinero, y un cobro lo recibe, y
        los tres exponen los mismos campos. Sin este dato, `check_intent` tendría
        que aplicar `check_amount` a los tres, y aplicárselo a un cobro es
        absurdo en los dos sentidos: impediría recuperar una posición grande por
        serlo, y gastaría presupuesto del día por dinero que **entra**.

        Es obligatorio y sin valor por omisión a propósito. Un intento nuevo
        tiene que **decidir** esta respuesta y escribirla, y el día que se añada
        uno que no comprometa nada, el compilador obligará a mirarlo en vez de
        heredar en silencio un «sí» que estrecharía un tope sin que nadie lo
        eligiera.
        """
        ...


@runtime_checkable
class MultiChainIntent(Protocol):
    """Un intento que compromete dinero en **más de una red** a la vez.

    Hoy sólo lo satisface un puente, y existe por una razón de fondo: la lista
    blanca de redes se comprueba sobre `intent.chain`, que es una sola, y un
    puente toca dos. Sin esto, un puente desde una red permitida hacia una que no
    lo está pasaría la comprobación entera, porque la única red que se mira es la
    de origen — y la de destino es precisamente donde el dinero va a acabar.

    ### Por qué no se extiende `ExecutableIntent`

    Sería lo natural y está descartado a conciencia: `ExecutableIntent` es
    `runtime_checkable`, y esa comprobación mira **qué miembros existen**. Un
    miembro nuevo obligatorio ahí invalida de golpe a `ExecutionIntent`,
    `PredictionOrderIntent` y `PredictionRedeemIntent` —los tres tendrían que
    declarar una lista de redes de un elemento para no dejar de ser lo que ya
    eran—. Se midió con `SwapPlanner.expected_destination`: añadirlo rompió seis
    pruebas de dobles que no lo tenían.

    Así que esto es un protocolo **aparte**, y `check_intent` pregunta por él con
    `isinstance`. Un intento que no lo satisface se sigue comprobando igual que
    siempre, con su única red.

    `chains` devuelve **todas** las redes que la operación toca, y no sólo la de
    destino: quien la lea no debería tener que saber que `chain` ya viene
    incluida. El orden no importa —se comprueban todas— pero se declara
    determinista para que un mensaje de error sea reproducible.
    """

    @property
    def chains(self) -> tuple[str, ...]: ...


# --------------------------------------------------------------------------- #
# Registro de ejecuciones
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Un hecho económico ya ocurrido. Sin secretos, por construcción.

    `tx_hash` es **el identificador que dio el recinto**, y no siempre es el hash
    de una transacción: para una orden de predicción es el identificador con el
    que el CLOB la nombra. `kind` es lo que permite distinguirlos, y por eso
    existe en vez de dejar que lo adivine quien lee el fichero: los dos campos
    son cadenas hexadecimales de la misma forma, así que **no se distinguen
    mirándolos**.
    """

    occurred_at: datetime
    chain: str
    pair: str
    engine_id: str
    notional: str
    notional_symbol: str
    tx_hash: str
    status: str
    recipient: str
    description: str
    #: `transaction`, `order` o `redeem`. Por omisión `transaction`, que es lo
    #: que eran todos los renglones escritos antes de que existiera este campo:
    #: los ficheros de registro que ya hay en disco se siguen leyendo igual, y su
    #: significado no cambia.
    kind: str = TRANSACTION_KIND

    @property
    def notional_value(self) -> Decimal:
        return Decimal(self.notional)

    @property
    def counts_towards_limits(self) -> bool:
        """Si este asiento consume tope.

        Una orden cuenta **siempre**, y no por una lista de estados: sólo se
        anota cuando el recinto ya la aceptó, así que el hecho de que exista el
        renglón es la prueba. Enumerar sus estados sería la forma de que el día
        que el recinto estrene uno, ese gasto dejara de contar en silencio —que
        es justo el lado por el que no interesa equivocarse—.

        Un cobro no cuenta **nunca**, y tampoco por una lista de estados: el
        dinero va en la dirección contraria a la que el tope acota. Contarlo
        tendría dos consecuencias, las dos malas: gastaría presupuesto del día
        por recuperar lo propio, y una posición grande —la que más merece
        cobrarse— sería la que más acercara al usuario a quedarse sin poder
        operar. Se dice por tipo y no por estado porque incluso un cobro fallido
        no ha movido nada hacia fuera: lo único que costó fue el gas.
        """
        if self.kind == REDEEM_KIND:
            return False
        if self.kind == ORDER_KIND:
            return True
        return self.status in _COUNTED_STATUSES

    def to_json(self) -> str:
        return json.dumps(
            {
                "occurred_at": self.occurred_at.isoformat(),
                "chain": self.chain,
                "pair": self.pair,
                "engine_id": self.engine_id,
                "notional": self.notional,
                "notional_symbol": self.notional_symbol,
                "tx_hash": self.tx_hash,
                "status": self.status,
                "recipient": self.recipient,
                "description": self.description,
                "kind": self.kind,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_record(cls, record: dict[str, object]) -> LedgerEntry | None:
        """Reconstruye una entrada, o `None` si el renglón está incompleto.

        Devolver `None` en vez de lanzar es deliberado: el registro lo lee la
        aplicación al arrancar para calcular el gasto del día, y un renglón a
        medias no puede impedir que la aplicación abra. Un fichero que impide
        arrancar por su propio contenido es peor que el renglón perdido.
        """
        try:
            return cls(
                occurred_at=datetime.fromisoformat(str(record["occurred_at"])),
                chain=str(record["chain"]),
                pair=str(record["pair"]),
                engine_id=str(record["engine_id"]),
                notional=str(record["notional"]),
                notional_symbol=str(record["notional_symbol"]),
                tx_hash=str(record["tx_hash"]),
                status=str(record["status"]),
                recipient=str(record["recipient"]),
                description=str(record.get("description", "")),
                # Los renglones escritos antes de que existiera este campo son
                # todos transacciones, así que el valor por omisión no es una
                # suposición: es lo que eran.
                kind=str(record.get("kind", TRANSACTION_KIND)),
            )
        except (KeyError, ValueError, TypeError):
            return None


class ExecutionLedger:
    """`executions.jsonl`: append-only, legible y sin secretos.

    Se guarda **una línea JSON por ejecución** en vez de un documento que se
    reescribe entero. Un fichero que se reescribe se puede corromper a medias si
    el proceso muere mientras escribe, y entonces se pierde **todo** el histórico
    —justo lo que el tope diario necesita—; con líneas, lo que se pierde es la
    última, y sólo si el proceso murió en el peor instante posible.
    """

    __slots__ = ("_clock", "_path")

    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        self._path = path
        self._clock = clock or SystemClock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry: LedgerEntry) -> None:
        """Anota una ejecución. Nunca lanza por un fallo de disco.

        Que el registro no se pueda escribir **no** puede tumbar una operación
        que la red ya aceptó: el dinero ya se movió y negarlo no lo devuelve. Se
        avisa a gritos y se sigue, porque lo único peor que un registro
        incompleto es una aplicación que dice que la operación falló cuando sí
        ocurrió.

        Antes de añadir se cierra la línea si quedó a medias. Se midió con una
        prueba: un renglón partido por un corte de luz —sin salto final— hace que
        la entrada **siguiente** se pegue a ese fragmento, con lo que la línea
        rota se lleva por delante a una ejecución de verdad. Una ejecución que
        desaparece del registro no es sólo un hueco en la traza: es gasto que el
        tope del día deja de contar. Escribiendo el salto que falta, lo único que
        puede perderse es el propio fragmento.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Binario y en modo añadir: los saltos de línea son un byte y no
            # dependen de la plataforma, así que el «¿termina en salto?» de
            # abajo no tiene que adivinar convenciones. En modo añadir, escribir
            # va siempre al final aunque se haya leído antes.
            with self._path.open("a+b") as handle:
                handle.seek(0, SEEK_END)
                size = handle.tell()
                if size:
                    handle.seek(size - 1)
                    if handle.read(1) != b"\n":
                        handle.write(b"\n")
                handle.write((entry.to_json() + "\n").encode("utf-8"))
        except OSError as error:
            _log.error(
                "execution.ledger_write_failed",
                path=str(self._path),
                tx_hash=entry.tx_hash,
                reason=str(error),
            )

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._read())

    def spent_since(self, moment: datetime) -> Decimal:
        """Cuánto se ejecutó desde `moment`, en unidades del token de referencia.

        Se suman símbolos distintos, y eso es correcto **porque no deberían
        serlo**: el importe de referencia se mide siempre en la stablecoin con la
        que se cotiza el par, así que dos entradas de la misma red están en la
        misma unidad. Si alguna vez no lo estuvieran sería un fallo de
        `ExecutionIntent`, que ya lo comprueba al construirse.
        """
        total = Decimal(0)
        for entry in self._read():
            if entry.counts_towards_limits and entry.occurred_at >= moment:
                total += entry.notional_value
        return total

    def spent_today(self) -> Decimal:
        return self.spent_since(self._clock.now() - WINDOW)

    def _read(self) -> Iterator[LedgerEntry]:
        if not self._path.is_file():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as error:
            _log.error(
                "execution.ledger_read_failed",
                path=str(self._path),
                reason=str(error),
            )
            return

        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                # La última línea a medias es el rastro normal de un corte de
                # luz; una línea rota en medio es otra cosa, pero en los dos
                # casos lo correcto es saltarla y decirlo. Parar aquí dejaría a
                # la aplicación sin poder calcular el gasto del día por un
                # renglón ilegible.
                _log.warning(
                    "execution.ledger_line_skipped", path=str(self._path), line=number
                )
                continue
            if not isinstance(record, dict):
                _log.warning(
                    "execution.ledger_line_skipped", path=str(self._path), line=number
                )
                continue
            entry = LedgerEntry.from_record(record)
            if entry is None:
                _log.warning(
                    "execution.ledger_line_skipped", path=str(self._path), line=number
                )
                continue
            yield entry

    def record_receipt(
        self,
        receipt: BroadcastReceipt,
        intent: ExecutableIntent,
    ) -> LedgerEntry:
        """Anota el desenlace de una ejecución y devuelve lo anotado.

        Se anota también lo que **no** es prueba —`UNKNOWN`—, y la entrada sabe
        que no cuenta para los topes (`counts_towards_limits`). Un registro que
        sólo guardara los éxitos no podría explicar por qué una operación que el
        usuario vio lanzarse no aparece por ningún lado.

        La columna del importe lleva la **valoración** en la moneda del tope, no
        lo entregado, y por eso lo entregado se escribe además en la descripción:
        un asiento que dijera «0,014» sin decir de qué token no serviría para
        reconstruir nada, y el número que decide si se puede seguir operando tiene
        que estar en una sola unidad para que la suma del día signifique algo.
        """
        measured = intent.measured
        valorado = (
            ""
            if intent.reference_value is None
            else (
                f" — entregado {intent.notional}, valorado en {measured.symbol} "
                f"para el tope"
            )
        )
        entry = LedgerEntry(
            occurred_at=receipt.observed_at,
            chain=receipt.chain,
            pair=intent.label,
            engine_id=intent.engine_id,
            notional=f"{measured.as_decimal():f}",
            notional_symbol=measured.symbol,
            tx_hash=receipt.tx_hash,
            status=receipt.status.value,
            recipient=intent.recipient,
            description=f"{receipt.reason}{valorado}",
        )
        self.append(entry)
        return entry

    def record_order(
        self,
        *,
        intent: ExecutableIntent,
        order_id: str,
        status: str,
        observed_at: datetime,
        chain_key: str,
        reason: str = "",
    ) -> LedgerEntry:
        """Anota una orden publicada en un recinto. **No es una transacción.**

        Tiene su propio método en vez de reutilizar `record_receipt` porque un
        recibo describe algo que una orden no tiene: un bloque, un gas y un
        estado que sólo puede ser uno de cuatro. Una orden no toca la cadena al
        publicarse, y meterla en un `BroadcastReceipt` obligaría a inventarle un
        `tx_hash` y un estado de una lista en la que no encaja.

        `chain_key` entra aparte y no se lee de `intent` porque el intento no la
        tiene: `ExecutableIntent` expone `chain` para los topes, y aquí hace
        falta la red que se escribe en el asiento. Se pide explícita en vez de
        dar por hecho que son lo mismo.

        El importe que se anota es el **medido**, el mismo que se comprobó contra
        los topes, para que lo que se suma al gasto del día sea exactamente lo
        que se autorizó.
        """
        measured = intent.measured
        entry = LedgerEntry(
            occurred_at=observed_at,
            chain=chain_key,
            pair=intent.label,
            engine_id=intent.engine_id,
            notional=f"{measured.as_decimal():f}",
            notional_symbol=measured.symbol,
            tx_hash=order_id,
            status=status,
            recipient=intent.recipient,
            # Se dice en la descripción que el identificador es del recinto y no
            # de una cadena: es el único sitio donde quien lea el registro se
            # entera, porque las dos cadenas tienen la misma forma.
            description=(
                f"orden publicada, identificador del recinto (no es una "
                f"transacción): {order_id} — {reason}"
            ),
            kind=ORDER_KIND,
        )
        self.append(entry)
        return entry

    def record_redeem(
        self,
        receipt: BroadcastReceipt,
        *,
        intent: ExecutableIntent,
        chain_key: str,
        position: PredictionPosition,
    ) -> LedgerEntry:
        """Anota el cobro de una posición ya resuelta. **Es dinero que entra.**

        Tiene su propio método, y su propio `kind`, porque un cobro es el único
        asiento que va en la dirección contraria a la de todos los demás. Metido
        como `transaction` contaría para el tope del día —un `SUCCESS` es un
        `SUCCESS`—, y entonces recuperar lo propio gastaría presupuesto de
        operación: cobrar una posición grande acercaría al usuario a quedarse sin
        poder operar. Ver `LedgerEntry.counts_towards_limits`.

        El importe se anota igualmente, y en la misma columna que los gastos, para
        que el fichero se lea entero y se pueda reconstruir cuánto entró. Que no
        se sume al tope lo decide el tipo del asiento, no que la cifra falte: un
        registro que escondiera el importe de un cobro no podría explicar por qué
        el saldo de la cartera subió.

        `chain_key` se pide explícita por la misma razón que en `record_order`:
        el intento expone la red para los topes, y aquí se escribe la red que va
        en el asiento. Son cosas distintas aunque hoy coincidan.
        """
        measured = intent.measured
        entry = LedgerEntry(
            occurred_at=receipt.observed_at,
            chain=chain_key,
            pair=intent.label,
            engine_id=intent.engine_id,
            notional=f"{measured.as_decimal():f}",
            notional_symbol=measured.symbol,
            tx_hash=receipt.tx_hash,
            status=receipt.status.value,
            recipient=intent.recipient,
            # Se dice que entra, porque el signo no está en ninguna columna: quien
            # sume la columna del importe sin leer esto contaría un cobro como un
            # gasto. Y se nombra el mercado por su condición, que es lo que hace
            # falta para volver a mirarlo en el explorador.
            description=(
                f"cobro de {position.shares:f} participaciones al resultado "
                f"«{position.outcome_label}» — entra colateral, no sale; "
                f"mercado {position.condition_id}"
            ),
            kind=REDEEM_KIND,
        )
        self.append(entry)
        return entry

    def record_approval(
        self,
        receipt: BroadcastReceipt,
        *,
        pair: str,
        engine_id: str,
        token_symbol: str,
        spender: str,
        granted_raw: int,
        via: str | None = None,
    ) -> LedgerEntry:
        """Anota la aprobación de un ERC-20. Es un hecho, no un importe gastado.

        Se anota con **importe de referencia cero**, y eso es correcto: una
        aprobación no mueve valor, sólo concede permiso al router para moverlo
        después. Sumar el importe autorizado al gasto del día contaría dos veces
        el mismo dinero —una al aprobarlo y otra al cambiarlo—, y un tope que
        cuenta doble es un tope que se agota a la mitad de lo que dice.

        Lo que sí queda es el hecho: sin él, un `executions.jsonl` no podría
        explicar por qué un router tuvo autorización sobre el token de alguien.
        El importe concedido va en la descripción, que es donde se lee, no en la
        columna que decide si se puede seguir operando.

        `via` es la dirección de Permit2 cuando la aprobación es la **segunda**
        de las dos —la que le concede al router el permiso que Permit2 ya tenía—,
        y va en la descripción porque sin ella el asiento se lee al revés: dos
        líneas con el mismo token y el mismo importe, una contra Permit2 y otra
        contra el router, se confunden con una aprobación directa al router, que
        es justo lo que este permiso **no** es.
        """
        via_text = "" if via is None else f"vía Permit2 {via} "
        entry = LedgerEntry(
            occurred_at=receipt.observed_at,
            chain=receipt.chain,
            pair=pair,
            engine_id=engine_id,
            notional="0",
            notional_symbol=token_symbol,
            tx_hash=receipt.tx_hash,
            status=receipt.status.value,
            # Aquí el destinatario de la traza es a quien se le concede el
            # permiso, que es el router. Es el dato que hace útil el asiento.
            recipient=spender,
            description=(
                # El importe va en unidad mínima y **diciéndolo**: aquí no se
                # conoce la escala del token, y dividir por una escala supuesta
                # escribiría un número equivocado con toda la apariencia de ser
                # el bueno. El entero exacto no engaña a nadie.
                f"aprobación de {token_symbol} por {granted_raw} en unidad mínima "
                f"{via_text}— {receipt.reason}"
            ),
        )
        self.append(entry)
        return entry


# --------------------------------------------------------------------------- #
# Autonomía
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AutonomyEvent:
    """Una vez que se armó o desarmó la autonomía. Es parte de la auditoría."""

    occurred_at: datetime
    armed: bool
    reason: str


#: Firma de los observadores de cambio de autonomía. Se les pasa el estado
#: **efectivo** —el de `armed`, no el de la bandera interna—, que es el que la
#: interfaz pinta. La UI se suscribe para repintar el indicador sin que la capa
#: de aplicación conozca Qt, igual que con `ModeGuard`.
AutonomyListener = Callable[[bool], None]


class AutonomyPolicy:
    """Si la ejecución desatendida está armada, y qué la habilita.

    Se **desarma sola** mientras falte cualquiera de las dos cosas que la hacen
    posible: la clave con la que firmar y la frase que declara que el operador
    quiso esto. No es una comprobación que se hace al armar y luego se olvida —
    `armed` la vuelve a evaluar cada vez que se pregunta, así que retirar la
    clave del llavero desarma la ejecución automática sin que nadie tenga que
    acordarse de desarmarla.
    """

    __slots__ = (
        "_armed",
        "_clock",
        "_events",
        "_keys",
        "_ledger",
        "_limits",
        "_listeners",
        "_passphrase",
        "_trigger",
    )

    def __init__(
        self,
        *,
        keys: PrivateKeySource,
        passphrase: PassphraseSource,
        limits: ExecutionLimits,
        ledger: ExecutionLedger,
        clock: Clock | None = None,
        trigger: TriggerKind = TriggerKind.MANUAL,
    ) -> None:
        self._keys = keys
        self._passphrase = passphrase
        self._limits = limits
        self._ledger = ledger
        self._clock = clock or SystemClock()
        self._trigger = trigger
        self._armed = False
        self._events: list[AutonomyEvent] = []
        self._listeners: list[AutonomyListener] = []

    @property
    def ledger(self) -> ExecutionLedger:
        """El registro de lo ejecutado. Se expone para **leer**, no para escribir.

        Lo escriben esta clase y el camino de ejecución; la interfaz sólo lo lee
        —lo gastado en 24 h, la traza de lo que se hizo sin preguntar—, y para eso
        necesita llegar a él. Es el mismo objeto y no una copia: una copia daría
        una cifra que dejaría de coincidir con el fichero en cuanto llegara la
        siguiente ejecución, que es justo lo que un tope no se puede permitir.
        """
        return self._ledger

    @property
    def limits(self) -> ExecutionLimits:
        return self._limits

    @property
    def trigger(self) -> TriggerKind:
        return self._trigger

    @property
    def events(self) -> Sequence[AutonomyEvent]:
        return tuple(self._events)

    def subscribe(self, listener: AutonomyListener) -> Callable[[], None]:
        """Registra un observador y devuelve la función para darse de baja.

        Existe porque el estado que se observa aquí es el que autoriza a gastar
        **sin preguntar**. Un indicador que sólo se pintara al arrancar dejaría a
        la vista un «desarmada» que puede haber dejado de ser cierto, y en esa
        dirección el error es el caro: alguien daría por hecho que la aplicación
        va a consultarle y no lo haría.
        """
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    @property
    def can_arm(self) -> bool:
        """Si están dadas las condiciones para poder armar."""
        return self._keys.available() and bool(self._passphrase.get())

    @property
    def armed(self) -> bool:
        """Si la ejecución desatendida está operativa **ahora mismo**.

        Se reevalúa en cada consulta y no se limita a devolver la bandera: una
        bandera que quedó puesta cuando había clave seguiría diciendo que sí
        después de que la clave desaparezca, y eso es exactamente lo que no debe
        pasar con algo que gasta dinero sin preguntar.
        """
        return self._armed and self.can_arm

    def arm(self, passphrase: str) -> None:
        """Arma la autonomía. Exige la frase correcta.

        Compara con `hmac.compare_digest` en vez de `==` porque comparar
        cadenas con `==` sale antes en el primer carácter distinto, y el tiempo
        que tarda en salir filtra cuántos caracteres acertó quien lo intenta.
        """
        stored = self._passphrase.get()
        if not stored:
            raise ExecutionError(
                "no hay frase de autonomía configurada, así que no se puede armar la "
                "ejecución desatendida. Es lo que declara que esto se hace a "
                "propósito."
            )
        if not hmac.compare_digest(stored, passphrase):
            # No se registra la frase recibida, sólo que falló.
            self._note(armed=False, reason="frase de autonomía incorrecta")
            raise ExecutionError("la frase de autonomía no es correcta")
        if not self._keys.available():
            raise ExecutionError(
                "no hay clave privada configurada: sin ella no hay nada que firmar y "
                "la autonomía no serviría de nada."
            )
        self._armed = True
        self._note(armed=True, reason="armada por el operador")

    def disarm(self, reason: str = "desarmada por el operador") -> None:
        """Desarma. Nunca falla, y es lo que la hace útil como freno."""
        self._armed = False
        self._note(armed=False, reason=reason)

    def arm_from_config(self) -> bool:
        """Arma al arrancar si la configuración lo pide y hay con qué.

        Devuelve si quedó armada. Es la vía de la ejecución desatendida de
        verdad: por definición no hay nadie delante para escribir la frase, así
        que lo que habilita es **que la frase esté configurada** — un acto
        deliberado del operador, y no un valor por omisión que nadie eligió.

        Con `trigger = manual` no arma nada: si el usuario no ha pedido
        ejecución automática, armar sería decidir por él.
        """
        if self._trigger is TriggerKind.MANUAL:
            return False
        if not self.can_arm:
            _log.info(
                "autonomy.not_armed",
                trigger=str(self._trigger),
                reason="falta la clave privada o la frase de autonomía",
            )
            return False
        self._armed = True
        self._note(armed=True, reason=f"armada al arrancar (disparador: {self._trigger})")
        return True

    def check_intent(self, intent: ExecutableIntent) -> None:
        """Aplica los límites al intento. Un solo sitio, un solo orden.

        Se comprueban aquí y no repartidos por la UI porque un límite que sólo
        comprueba la vista no es un límite: es una advertencia que cualquiera
        puede saltarse llamando al caso de uso.

        El orden va de lo más barato a lo más caro de explicar: primero lo que
        el intento **es** —red, tokens, motor—, y después el importe, que
        necesita el gasto acumulado y por tanto leer el registro.

        El importe se salta entero cuando la operación no compromete nada, y ahí
        está el único caso que tiene: el cobro de una posición ya resuelta. Los
        topes acotan lo que **sale**, y un cobro mete; medirlo contra ellos
        dejaría el dinero del usuario encerrado en el contrato justo cuando por
        fin se puede sacar, y encima gastaría presupuesto del día por recuperar
        lo propio. Lo que sí se le comprueba es todo lo anterior —modo, red,
        colateral y motor—, que es lo que decide si la operación puede existir.
        La decisión la toma el intento y no una lista de tipos escrita aquí: ver
        `ExecutableIntent.counts_towards_limits`.

        Y por delante de todo, el interruptor maestro: con la ejecución apagada no
        hay nada que comprobar, y decir «te pasaste del tope» cuando el problema
        es que no se ejecuta sería mandar al usuario a ajustar la cifra que no es.
        """
        self._limits.check_enabled()
        # Todas las redes que la operación toca. Para un swap, una; para un
        # puente, las dos, porque la blanca tiene que cubrir también aquella en
        # la que el dinero acaba. Se hace aquí y no repartido por los casos de
        # uso para que siga habiendo **un** sitio donde se comprueban los
        # límites, y en un orden que se puede leer de arriba abajo.
        chains = intent.chains if isinstance(intent, MultiChainIntent) else (intent.chain,)
        for chain_key in chains:
            self._limits.check_chain(chain_key)
        self._limits.check_token(intent.tokens)
        self._limits.check_engine(intent.engine_id)
        if not intent.counts_towards_limits:
            return
        measured = intent.measured
        self._limits.check_amount(
            measured.as_decimal(),
            spent_today=self.spent_in_window(measured.symbol),
        )

    def spent_in_window(self, symbol: str) -> Decimal:
        """El gasto de las últimas 24 h **en esa unidad**.

        Se suman sólo las entradas del mismo símbolo, y no todas: el importe de
        referencia se mide siempre en la stablecoin con la que se cotiza el par,
        así que lo normal es que todas las entradas de una red compartan unidad
        — pero si alguna vez no fuera así, sumarlas sería sumar peras y manzanas
        y el tope dejaría de significar lo que dice. Se cuenta sólo la que
        corresponde y se avisa de las otras, para que la discrepancia se vea en
        vez de esconderse dentro de una cifra.
        """
        since = self._clock.now() - WINDOW
        total = Decimal(0)
        otras: set[str] = set()
        for entry in self._ledger.entries():
            if not entry.counts_towards_limits or entry.occurred_at < since:
                continue
            if entry.notional_symbol != symbol:
                otras.add(entry.notional_symbol)
                continue
            total += entry.notional_value
        if otras:
            _log.warning(
                "autonomy.mixed_notional_units",
                window_hours=WINDOW.total_seconds() / 3600,
                counted=symbol,
                ignored=sorted(otras),
                hint=(
                    "hay ejecuciones en otra unidad dentro de la ventana; no se suman "
                    "porque no son comparables, así que el tope cuenta de menos"
                ),
            )
        return total

    def _note(self, *, armed: bool, reason: str) -> None:
        event = AutonomyEvent(occurred_at=self._clock.now(), armed=armed, reason=reason)
        self._events.append(event)
        _log.info("autonomy.changed", armed=armed, reason=reason)
        # La notificación se **deriva** en vez de reenviar el `armed` que se
        # acaba de anotar. Hoy los dos coinciden —`arm` y `arm_from_config`
        # comprueban `can_arm` antes de tocar la bandera—, pero son cosas
        # distintas: la bandera dice qué se pidió y `armed` dice qué está
        # operativo. Lo que la interfaz pinta es lo segundo, y una autonomía que
        # se pinta operativa sin serlo es el error caro de este indicador.
        self._notify()

    def _notify(self) -> None:
        """Avisa a los observadores del estado efectivo, sin dejar que rompan nada.

        Un observador que lanza —una interfaz que se está cerrando, un widget ya
        destruido— no puede tumbar el armar o el desarmar: lo que se está
        anotando aquí es una decisión sobre dinero, y la decisión ya está tomada
        cuando se llega a este punto. Se registra el fallo y se sigue.
        """
        estado = self.armed
        for listener in tuple(self._listeners):
            try:
                listener(estado)
            except Exception:  # un observador no decide nada
                _log.warning("autonomy.listener_failed", armed=estado, exc_info=True)


class PolicyBypass:
    """El gancho que `ConfirmationGateway` consulta antes de preguntar.

    Sólo puede saltarse la pregunta, y sólo para emitir: construir o cotizar no
    tiene efecto y preguntar por ello entrena al usuario a decir que sí sin leer.
    """

    __slots__ = ("_policy",)

    def __init__(self, policy: AutonomyPolicy) -> None:
        self._policy = policy

    def reason_to_skip(self, action: PendingAction) -> str | None:
        """El motivo para no preguntar, o `None` para que se pregunte.

        Sólo mira dos cosas: que se trate de **emitir** y que la autonomía esté
        armada. Los límites de importe no se comprueban aquí sino en
        `AutonomyPolicy.check_intent`, que corre antes de llegar a este punto y
        tiene delante el intento completo; repetirlos aquí obligaría a pasar por
        este gancho un importe que no tiene, y una comprobación que no puede
        hacerse no es una salvaguarda, es una promesa.
        """
        if action.capability is not Capability.BROADCAST_TX:
            return None
        if not self._policy.armed:
            return None
        return POLICY_REASON


# --------------------------------------------------------------------------- #
# Configuración → política
# --------------------------------------------------------------------------- #
def limits_from_config(
    *,
    enabled: bool,
    max_quote_per_trade: str | None,
    max_quote_per_day: str | None,
    allowed_tokens: Sequence[str],
    allowed_chains: Sequence[str],
    allowed_engines: Sequence[str],
    max_executions_per_cycle: int,
    slippage_bps: int,
) -> ExecutionLimits:
    """Traduce el `[execution]` del TOML a los límites del dominio.

    Las cifras llegan como texto y se convierten con `parse_amount` para que no
    pase por `float` en ningún momento: un tope de gasto redondeado en algún
    punto es un tope que no se cumple.
    """
    from amigocompora.domain.execution import parse_amount

    def _clean(
        values: Sequence[str], transform: Callable[[str], str] | None = None
    ) -> frozenset[str]:
        normalizar = transform or (lambda value: value)
        return frozenset(normalizar(value.strip()) for value in values if value.strip())

    return ExecutionLimits(
        enabled=enabled,
        max_quote_per_trade=parse_amount(max_quote_per_trade, field_name="max_quote_per_trade"),
        max_quote_per_day=parse_amount(max_quote_per_day, field_name="max_quote_per_day"),
        # Cada lista se normaliza a la forma en que la escribe el resto del
        # sistema —símbolos en mayúsculas, claves de red en minúsculas— porque
        # quien la escribe es una persona y el `config.toml` no tiene por qué
        # respetar la caja: `usdc` no casando con `USDC` cortaría la operación
        # por un límite que el usuario cree haber configurado bien.
        allowed_tokens=_clean(allowed_tokens, str.upper),
        allowed_chains=_clean(allowed_chains, str.lower),
        allowed_engines=_clean(allowed_engines),
        max_executions_per_cycle=max_executions_per_cycle,
        slippage_bps=slippage_bps,
    )
