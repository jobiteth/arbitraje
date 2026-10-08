"""Cobrar posiciones ya resueltas: cambiar participaciones por el colateral.

Capacidad requerida: `BROADCAST_TX`, que **sólo** concede el modo `EJECUCIÓN`.
Es una transacción real en Polygon, y todo lo que hay aquí está ordenado por esa
consecuencia.

### Es el único camino que **entra** dinero

Los otros dos que emiten —el swap y la orden de predicción— lo sacan. Éste
recibe: se entregan unas participaciones que ya se pagaron y el contrato devuelve
el colateral que valen. De ahí salen las dos diferencias que tiene respecto a
ellos, y las dos están escritas donde se notan:

- **No le aplica el tope de gasto.** Los topes acotan lo que sale; medir contra
  ellos un cobro dejaría el dinero del usuario encerrado en el contrato justo
  cuando por fin se puede sacar, y encima gastaría presupuesto del día por
  recuperar lo propio. Quien lo decide es el intento, no este fichero: ver
  `ExecutableIntent.counts_towards_limits`. El resto de la comprobación —modo,
  red, colateral y motor— sí se le hace entera.
- **No pide ningún permiso previo.** Los otros dos conceden uno porque mueven un
  token **ajeno** —el router cobra con `transferFrom`—. Aquí no: el contrato
  condicional quema las participaciones de quien llama, así que no hay a quién
  autorizar nada. Añadir un `approve` «por si acaso» sería conceder un permiso
  que nadie necesita, que es la forma más barata de ampliar la superficie.

### Un mercado por transacción, y por qué se dice

El contrato cobra un mercado por llamada. Una cartera con varias posiciones
resueltas necesita varias transacciones, y este caso de uso las hace **una
detrás de otra**, parando en la primera que falle y diciendo cuántas se
cobraron. Lo que no hace es agrupar por dentro para devolver una sola: eso
dejaría las demás sin cobrar sin que nadie se entere, que es la forma de perder
dinero sin que se note.

Pero antes de emitir la primera se construyen y se comprueban **todas**. Esa
parte es pura —no gasta red ni pide la clave—, así que hacerla entera primero no
cuesta nada y compra dos cosas: un mercado que no cabe deja la tanda **sin
empezar** en vez de a medias, y una operación que la política prohíbe no llega a
pedir la clave ni para derivar una dirección. Lo que queda dentro del bucle es
sólo lo que de verdad no se puede saber antes: lo que contesta la red.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.execution_policy import AutonomyPolicy, PrivateKeySource
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.addresses import shorten
from amigocompora.domain.chains import chain
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.models import (
    BroadcastReceipt,
    BroadcastStatus,
    PredictionPosition,
    PredictionRedeemIntent,
    Token,
    UnsignedTransaction,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import PredictionRedeemer
from amigocompora.infra.evm.broadcast import EvmBroadcaster, require_evm
from amigocompora.infra.evm.dispatch import sign_and_send
from amigocompora.infra.evm.signer import address_from_key

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Preparado:
    """Un mercado ya construido y ya permitido, listo para emitirse.

    Existe para que la frontera entre «lo que se puede comprobar sin red» y «lo
    que sólo contesta la red» sea un dato y no un comentario: lo que hay aquí ya
    pasó todas las comprobaciones puras, y lo que se hace con ello es emitirlo.
    """

    mercado: str
    positions: tuple[PredictionPosition, ...]
    transaction: UnsignedTransaction
    intent: PredictionRedeemIntent


@dataclass(frozen=True, slots=True)
class RedeemPrediction:
    """Cobra posiciones ya resueltas. Devuelve el recibo de cada transacción."""

    registry: EngineRegistry
    gateway: ConfirmationGateway
    keys: PrivateKeySource
    policy: AutonomyPolicy
    broadcasters: Mapping[str, EvmBroadcaster] = field(default_factory=dict)
    clock: Clock | None = None

    async def __call__(
        self,
        positions: Sequence[PredictionPosition],
        *,
        wallet: str,
        recipient: str,
    ) -> tuple[BroadcastReceipt, ...]:
        # 1. El modo primero: antes de gastar red, y antes de pedir la clave.
        self.gateway.precheck(Capability.PREPARE_TX)
        self.gateway.precheck(Capability.BROADCAST_TX)

        if not positions:
            raise ExecutionError(
                "no hay ninguna posición que cobrar. Una lista vacía no es una "
                "operación: no se firma nada."
            )

        redeemer = self.registry.prediction_redeemer()
        chain_key = positions[0].venue.chain
        # 2. Esta entrega firma en EVM y sólo en EVM: el cobro es una transacción.
        require_evm(chain(chain_key))

        collateral = redeemer.collateral_on(chain_key)
        broadcaster = self._broadcaster(chain_key)

        # 3. Construir y comprobar **todos** los mercados antes de emitir ninguno.
        #    Lo de dentro es puro, así que adelantarlo no cuesta nada y evita que
        #    un mercado que no cabe deje los anteriores ya cobrados: una tanda a
        #    medias por un motivo que se sabía desde el principio es dinero movido
        #    sin necesidad.
        preparados = self._preparar(
            positions, redeemer, collateral, recipient=recipient, wallet=wallet
        )

        # 4. Que la cartera que firma sea la dueña de lo que se cobra. Va después
        #    de los límites a propósito: una operación que la política ya prohíbe
        #    no llega a pedir la clave ni para derivar una dirección.
        self._require_signer(wallet)

        # 5. Emitir, uno detrás de otro. Aquí ya sólo puede fallar la red.
        recibos: list[BroadcastReceipt] = []
        for preparado in preparados:
            try:
                recibo = await self._emitir(
                    preparado, chain_key=chain_key, broadcaster=broadcaster
                )
            except Exception as error:
                # Se para y se dice cuántas sí se cobraron. Seguir con las demás
                # después de un fallo firmaría transacciones que el usuario no ha
                # visto en el mismo estado que él creía, y callar las que sí
                # pasaron dejaría el recibo incompleto.
                if recibos:
                    raise ExecutionError(
                        f"se cobraron {len(recibos)} mercado(s) y el siguiente "
                        f"falló: {error}. Lo cobrado está anotado en el registro."
                    ) from error
                raise
            recibos.append(recibo)
        return tuple(recibos)

    # --------------------------------------------------------------- pasos  #
    def _preparar(
        self,
        positions: Sequence[PredictionPosition],
        redeemer: PredictionRedeemer,
        collateral: Token,
        *,
        recipient: str,
        wallet: str,
    ) -> tuple[_Preparado, ...]:
        """Construye y comprueba cada mercado. **Puro**: sin red y sin claves.

        `build_redeem` lanza si el mercado no está resuelto, si comparte colateral
        con otros o si las posiciones no son todas del mismo: son negativas que se
        leen antes de gastar red, y leerlas **todas** antes de emitir la primera es
        lo que impide que una tanda quede medio cobrada por algo que ya se sabía.
        """
        preparados: list[_Preparado] = []
        for mercado, suyas in _por_mercado(positions):
            transaction = redeemer.build_redeem(suyas, wallet=wallet)
            intent = self._intent(suyas, redeemer, collateral, recipient)
            self.policy.check_intent(intent)
            preparados.append(_Preparado(mercado, suyas, transaction, intent))
        return tuple(preparados)

    async def _emitir(
        self,
        preparado: _Preparado,
        *,
        chain_key: str,
        broadcaster: EvmBroadcaster,
    ) -> BroadcastReceipt:
        """Las tres cosas que le quedan a un mercado ya construido y ya permitido."""
        # 6. El «sí» del usuario, con el cobro entero delante.
        await self._authorize(preparado)

        # 7. Firmar, emitir, anotar. La clave se pide aquí y se suelta: no se
        #    guarda en el objeto ni se pasa a nadie más.
        transaction = preparado.transaction
        receipt = await sign_and_send(
            transaction,
            broadcaster,
            private_key=self.keys.require(),
            expected_to=transaction.to_address,
            event="prediction.redeem_sending",
        )
        self.policy.ledger.record_redeem(
            receipt,
            intent=preparado.intent,
            chain_key=chain_key,
            position=preparado.positions[0],
        )
        _log.info(
            "prediction.redeem_recorded",
            market=preparado.mercado,
            positions=len(preparado.positions),
            tx_hash=receipt.tx_hash,
            status=receipt.status.value,
        )
        _require_success(receipt, "el cobro")
        return receipt

    def _require_signer(self, wallet: str) -> None:
        """Corta si la clave configurada no es la dueña de las posiciones.

        `redeemPositions` quema las participaciones de **quien llama**, así que
        firmar con otra cartera quemaría las suyas —ninguna— y la transacción
        revertiría habiendo pagado el gas. Y leer las posiciones de una cartera y
        firmar con otra es exactamente el fallo que no se detecta mirando la
        transacción.
        """
        firmante = self.address()
        if firmante is None:
            raise ExecutionError(
                "no hay ninguna clave configurada, así que no hay cartera que "
                "pueda cobrar estas posiciones."
            )
        if firmante.lower() != wallet.lower():
            raise ExecutionError(
                f"las posiciones son de {shorten(wallet)} y la clave configurada "
                f"es de {shorten(firmante)}. No se cobra: el contrato quema las "
                f"participaciones de quien firma, así que con esta clave la "
                f"transacción revertiría habiendo pagado el gas."
            )

    def _intent(
        self,
        positions: Sequence[PredictionPosition],
        redeemer: PredictionRedeemer,
        collateral: Token,
        recipient: str,
    ) -> PredictionRedeemIntent:
        """El intento que la política evalúa, con lo que se cobra en su unidad.

        El importe es la **suma de lo que pagan las posiciones**, y no hay nada
        que valorar: cada participación ganadora paga una unidad de colateral, así
        que el total ya está en la unidad de los topes. Es el mismo número que
        verá el usuario en la descripción de la transacción, y tiene que serlo:
        dos cifras distintas para el mismo cobro serían dos verdades.
        """
        primera = positions[0]
        total = sum((posicion.payout for posicion in positions), Decimal(0))
        return PredictionRedeemIntent(
            position=primera,
            recipient=recipient,
            notional=TokenAmount.from_decimal(total, collateral.decimals, collateral.symbol),
            engine_id=redeemer.manifest.engine_id,
        )

    async def _authorize(self, preparado: _Preparado) -> None:
        """El permiso para emitir, con el cobro delante y sin adjetivos.

        Se dice explícitamente que esto **sí** es una transacción, porque es lo
        contrario de lo que hace la orden de predicción del mismo recinto y quien
        venga de ahí puede esperar un mensaje firmado que se puede cancelar. Un
        cobro emitido no se puede deshacer.
        """
        positions = preparado.positions
        primera = positions[0]
        await self.gateway.authorize(
            Capability.BROADCAST_TX,
            f"Cobrar posición en {primera.venue.name}",
            details=(
                f"Mercado: {primera.question}",
                f"Resultado: {primera.outcome_label}",
                f"Posiciones: {len(positions)}",
                f"Participaciones: {sum((p.shares for p in positions), Decimal(0)):f}",
                f"Cobras: {preparado.intent.notional}",
                f"Contrato que cobra: {shorten(preparado.transaction.to_address)}",
                f"Firma la cartera: {self.address() or 'sin clave configurada'}",
                f"Recibe: {shorten(preparado.intent.recipient)}",
                f"Identificador del mercado: {preparado.mercado}",
                "Es una transacción real en la cadena: cuesta gas y no se puede "
                "deshacer. Lo que se cobra entra en la cartera que firma.",
            ),
            transaction=preparado.transaction,
        )

    # --------------------------------------------------------------- apoyo  #
    def address(self) -> str | None:
        """La cartera que firma, o `None` si no hay clave. **Nunca la clave.**

        Se deriva pidiendo la clave y soltándola aquí mismo: lo único que sale de
        esta función es la dirección, que es pública.
        """
        if not self.keys.available():
            return None
        return address_from_key(self.keys.require())

    def _now(self) -> datetime:
        """El momento del asiento, según el reloj inyectado."""
        return (self.clock or SystemClock()).now()

    def _broadcaster(self, chain_key: str) -> EvmBroadcaster:
        broadcaster = self.broadcasters.get(chain_key)
        if broadcaster is None:
            raise ExecutionError(
                f"no hay ningún nodo configurado para «{chain_key}»: sin él no se "
                f"puede emitir el cobro ni leer la comisión de la red."
            )
        return broadcaster


def _por_mercado(
    positions: Sequence[PredictionPosition],
) -> tuple[tuple[str, tuple[PredictionPosition, ...]], ...]:
    """Agrupa las posiciones por mercado, conservando el orden en que llegaron.

    El orden importa y por eso no se usa un `dict` construido a lo loco: el
    usuario ve los mercados en el orden en que la fuente los dio, y firmarlos en
    otro orden haría que el segundo diálogo apareciera antes de lo que él cree.
    """
    orden: list[str] = []
    grupos: dict[str, list[PredictionPosition]] = {}
    for posicion in positions:
        if posicion.condition_id not in grupos:
            orden.append(posicion.condition_id)
            grupos[posicion.condition_id] = []
        grupos[posicion.condition_id].append(posicion)
    return tuple((mercado, tuple(grupos[mercado])) for mercado in orden)


def _require_success(receipt: BroadcastReceipt, what: str) -> None:
    """Corta si la transacción no llegó a buen término."""
    if receipt.status is not BroadcastStatus.SUCCESS:
        raise ExecutionError(
            f"{what} no llegó a buen término ({receipt.status.value}, "
            f"{receipt.tx_hash}). No se cobró nada."
        )
