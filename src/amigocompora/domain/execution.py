"""Hechos puros de la ejecución: qué se dispara, cuánto se paga, cuánto se deja.

Aquí sólo hay datos y comprobaciones. Las **decisiones** —armar la autonomía,
anotar en el registro, preguntar al usuario— viven en `app.execution_policy`,
porque son comportamiento y necesitan el reloj y el disco. Esta separación es la
que permite que `infra.evm` aplique una política de gas sin importar nada de la
capa de aplicación: la dependencia va `infra → domain`, nunca al revés.

El límite de gasto se expresa en **unidades del token de referencia de la red**
(la stablecoin con la que se cotiza el par), no en el token que se entrega. Es
la única unidad en la que un tope puede significar algo: «5 000 por operación»
tiene que querer decir lo mismo para un par de WETH que para uno de un token de
18 decimales que nadie conoce, y la stablecoin es lo que ambos lados comparten.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from amigocompora.domain.errors import ExecutionLimitExceededError, InvalidAmountError

#: El importe de referencia tiene que ser positivo: un tope de cero no es un tope
#: laxo, es «no operes nunca» disfrazado de configuración.
_UNSET: Final = "sin definir"

#: Comodín para `allowed_tokens`: permite operar con cualquier token. Se escribe en
#: la configuración como `allowed_tokens = ["*"]`. Es deliberadamente un valor
#: explícito y no una lista vacía, porque vacía significa «nada permitido».
ANY_TOKEN: Final = "*"  # noqa: S105  # el nombre contiene «TOKEN», pero es un símbolo de la lista, no un secreto

#: El mismo comodín en la lista de **redes**: `allowed_chains = ["*"]` permite
#: firmar en cualquier red. Mismo valor y misma semántica que `ANY_TOKEN` —y por
#: eso se define a partir de él, para que no puedan separarse—: es una decisión
#: escrita a propósito, no un vacío que se confunda con «nada permitido».
ANY_CHAIN: Final = ANY_TOKEN


class TriggerKind(StrEnum):
    """Qué dispara una operación. Los tres son combinables."""

    #: Sólo el botón. Nada se ejecuta sin que alguien lo pulse.
    MANUAL = "manual"
    #: El barrido de pares vigilados, cuando aparece una oportunidad que cabe en
    #: los límites. Requiere que la autonomía esté armada.
    AUTO = "auto"
    #: Operaciones fijas: par, importe y cadencia declarados en configuración.
    #: No depende de que aparezca un diferencial, así que no necesita umbral.
    PINNED = "pinned"


class GasStrategy(StrEnum):
    """Cómo se decide cuánto pagar por gas."""

    #: Se lee el estado de la red —base fee y propina observada— y se calcula el
    #: techo. Es lo correcto por omisión: un precio fijo en una red congestionada
    #: deja la transacción esperando sin que nada lo diga.
    AUTO = "auto"
    #: Un precio declarado, sin consultar la red. Para cadenas donde el precio es
    #: estable y se quiere previsibilidad de coste.
    FIXED = "fixed"


@dataclass(frozen=True, slots=True)
class GasPolicy:
    """Cuánto está dispuesto a pagar el operador por una transacción."""

    strategy: GasStrategy = GasStrategy.AUTO
    #: Factor sobre el techo calculado. Existe para subirlo cuando la red está
    #: congestionada y una transacción que no entra es una oportunidad perdida.
    #: Se guarda como `Decimal` y no como `float` por la misma razón que no hay
    #: floats en el dinero: multiplicar y truncar a entero con un `float` de por
    #: medio da cifras que no se pueden reproducir.
    multiplier: Decimal = Decimal("1.2")
    #: Sólo con `FIXED`, y obligatorios en ese caso.
    fixed_max_fee_per_gas: int | None = None
    fixed_max_priority_fee_per_gas: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.multiplier, Decimal):
            raise InvalidAmountError(
                f"el factor de gas debe ser Decimal, llegó {type(self.multiplier).__name__}"
            )
        if self.multiplier <= 0:
            raise InvalidAmountError(
                f"el factor de gas debe ser positivo, llegó {self.multiplier}"
            )
        if self.strategy is GasStrategy.FIXED:
            if self.fixed_max_fee_per_gas is None or self.fixed_max_priority_fee_per_gas is None:
                raise InvalidAmountError(
                    "con estrategia de gas «fixed» hay que declarar el precio máximo por "
                    "gas y la propina: sin ellos no hay nada que usar."
                )
            if self.fixed_max_fee_per_gas <= 0:
                raise InvalidAmountError(
                    f"el precio máximo por gas debe ser positivo, llegó "
                    f"{self.fixed_max_fee_per_gas}"
                )
            if self.fixed_max_fee_per_gas < self.fixed_max_priority_fee_per_gas:
                raise InvalidAmountError(
                    f"el precio máximo por gas ({self.fixed_max_fee_per_gas}) es menor que "
                    f"la propina ({self.fixed_max_priority_fee_per_gas}): ninguna "
                    f"transacción se aceptaría con esas cifras."
                )
        elif self.fixed_max_fee_per_gas is not None:
            raise InvalidAmountError(
                "se declaró un precio de gas fijo con estrategia «auto»: uno de los dos "
                "está de más y no se puede saber cuál quería el operador."
            )

    def cap(self, base_fee_per_gas: int, observed_tip: int) -> tuple[int, int]:
        """`(max_fee_per_gas, max_priority_fee_per_gas)` a partir de la red.

        La fórmula de `AUTO` es la que usa todo el ecosistema: `2 · base + tip`.
        El factor dos no es un margen arbitrario — cubre las seis rondas de
        bloques consecutivos que la base fee puede subir al máximo—, y sin él una
        transacción firmada con el techo justo del bloque actual se queda fuera
        en cuanto el bloque siguiente sube.
        """
        if self.strategy is GasStrategy.FIXED:
            max_fee = self.fixed_max_fee_per_gas
            tip = self.fixed_max_priority_fee_per_gas
            # `__post_init__` ya garantizó que están. Se comprueba otra vez en
            # vez de con un `assert` porque un `assert` desaparece bajo `-O` y
            # esto decide cuánto se paga: si algún día esta rama se alcanzara sin
            # valores, lo correcto es un error, no seguir con `None`.
            if max_fee is None or tip is None:
                raise InvalidAmountError(
                    "la estrategia de gas es «fixed» pero faltan el precio máximo o la "
                    "propina: no hay cifras con las que firmar."
                )
            return max_fee, tip

        tip = max(observed_tip, 0)
        ceiling = (2 * base_fee_per_gas) + tip
        scaled = int(Decimal(ceiling) * self.multiplier)
        # Nunca por debajo de la propina: `maxFee < tip` es una transacción que
        # ningún nodo acepta, y el truncado a entero podría llegar ahí con un
        # factor muy pequeño.
        return max(scaled, tip), tip


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Los topes duros. Se comprueban en el dominio, antes de firmar.

    **Vacío significa «nada permitido», no «sin restricción».** Es la elección
    deliberada y va en contra de la intuición: una lista blanca vacía que
    permitiera todo convertiría un descuido al escribir el `config.toml` —dejar
    `allowed_tokens` sin rellenar— en una autorización para operar con cualquier
    token. Como esto mueve dinero, el fallo tiene que caer del lado de no operar.
    """

    #: Interruptor maestro. **Apagado por omisión**, y apagado significa que no se
    #: firma nada, pase lo que pase con los demás topes.
    #:
    #: Vive aquí y no en la vista por la misma razón que el resto de los topes: un
    #: interruptor que sólo comprueba la interfaz no es un interruptor, es un botón
    #: gris que cualquiera esquiva llamando al caso de uso. Y vive en el **dominio**
    #: y no en la política porque `AutonomyPolicy.check_intent` ya recorre estos
    #: límites en un orden fijo; añadirlo como un caso aparte sería tener dos
    #: sitios donde se decide si se puede operar.
    #:
    #: Que sea `False` por omisión es lo que convierte activar la ejecución en un
    #: acto deliberado: sin esta línea puesta a `true` en el `config.toml`, el modo
    #: `EJECUCIÓN` por sí solo no basta para mover dinero.
    enabled: bool = False
    #: Tope por operación, en unidades del token de referencia de la red.
    max_quote_per_trade: Decimal | None = None
    #: Tope acumulado en 24 h, en la misma unidad.
    max_quote_per_day: Decimal | None = None
    #: Símbolos con los que se puede operar, en cualquiera de las dos patas.
    allowed_tokens: frozenset[str] = frozenset()
    #: Redes en las que se puede operar.
    allowed_chains: frozenset[str] = frozenset()
    #: Motores cuyo payload se puede firmar. Un motor que sólo cotiza no debería
    #: poder construir lo que se firma, y esto lo hace explícito.
    allowed_engines: frozenset[str] = frozenset()
    #: Operaciones desatendidas por ciclo de barrido. Evita que un bucle de
    #: ejecución desatendida vacíe la cartera en una tarde de volatilidad.
    max_executions_per_cycle: int = 1
    slippage_bps: int = 50

    def __post_init__(self) -> None:
        for name, value in (
            ("max_quote_per_trade", self.max_quote_per_trade),
            ("max_quote_per_day", self.max_quote_per_day),
        ):
            if value is not None and value <= 0:
                raise InvalidAmountError(
                    f"{name} debe ser positivo si se declara, llegó {value}. "
                    f"Un tope de cero no es un tope laxo: es no operar nunca."
                )
        if self.max_executions_per_cycle < 0:
            raise InvalidAmountError(
                f"max_executions_per_cycle no puede ser negativo, llegó "
                f"{self.max_executions_per_cycle}"
            )
        if not 0 <= self.slippage_bps <= 10_000:
            raise InvalidAmountError(
                f"slippage_bps fuera de rango: {self.slippage_bps}. Se espera 0..10000."
            )
        if (
            self.max_quote_per_trade is not None
            and self.max_quote_per_day is not None
            and self.max_quote_per_trade > self.max_quote_per_day
        ):
            raise InvalidAmountError(
                f"el tope por operación ({self.max_quote_per_trade}) es mayor que el tope "
                f"diario ({self.max_quote_per_day}): una sola operación agotaría el día, "
                f"lo que casi siempre indica que una de las dos cifras está mal."
            )

    # ----------------------------------------------------------- comprobar  #
    def check_enabled(self) -> None:
        """Corta si la ejecución no está habilitada. **Va antes que todo lo demás.**

        Es el único de estos métodos que no compara cifras, y aun así tiene que ir
        el primero: con el interruptor apagado, los demás límites describen una
        operación que, además de pasarse de un tope, no debería existir. El
        mensaje que recibe el usuario tiene que ser «la ejecución está apagada»,
        no «te pasaste del tope diario» — lo segundo le haría ajustar una cifra
        para arreglar algo que no es el problema.

        Un `enabled` apagado con los topes vacíos es además el estado por omisión,
        así que este es el mensaje que más veces se va a leer: dice qué escribir y
        dónde.
        """
        if not self.enabled:
            raise ExecutionLimitExceededError(
                "ejecución deshabilitada",
                "la ejecución está apagada, así que no se firma ni se emite nada. "
                "Pon `enabled = true` bajo `[execution]` en `config.toml` —junto con "
                "las listas de redes, tokens y motores— y el modo en EJECUCIÓN para "
                "poder operar.",
            )

    def allows_token(self, symbol: str) -> bool:
        """Si la lista blanca deja operar con `symbol`.

        Es la comprobación que usan `check_token` **y los avisos de la interfaz**
        (la tarjeta de la orden de predicción, el par del swap, el selector de
        tokens): una sola respuesta para todos, para que un token no esté gris en
        una pantalla y firmable en la siguiente.

        La comparación es **por mayúsculas en los dos lados**, y no por capricho:
        el catálogo tiene símbolos en caja mixta —`pUSD`, el colateral del recinto
        de predicción, o `BTC.b`— y la lista llega normalizada a mayúsculas desde
        `limits_from_config`. Comparar en crudo bloquearía esos tokens contra una
        lista que el usuario escribió bien, y el mensaje diría «pUSD no está»
        mientras enseña `PUSD` declarado, que es de los errores que más caro
        cuestan de creer. Se normalizan los dos lados y no sólo el símbolo porque
        unos límites construidos a mano —pruebas, cables sueltos— pueden traer la
        lista sin normalizar, y una comprobación que dependiera de que alguien la
        normalizara antes sería una trampa esperando a su dueño.
        """
        # El comodín permite cualquier token a propósito: es una decisión del usuario,
        # escrita como tal en la configuración, y no un vacío que se confunda con
        # «nada permitido». Sin él, la lista vacía sigue significando que no se opera.
        if ANY_TOKEN in self.allowed_tokens:
            return True
        permitidos = {entrada.upper() for entrada in self.allowed_tokens}
        return symbol.upper() in permitidos

    def check_token(self, symbols: tuple[str, ...]) -> None:
        """Corta si lo que se mueve toca un token fuera de la lista blanca.

        Admite cualquier número de símbolos: un swap mueve un par y una orden de
        predicción mueve sólo el colateral. La comprobación es la misma —ninguno
        fuera de la lista—, así que la lista blanca se escribe una vez y vale
        para los dos caminos. Quién deja pasar cada símbolo lo decide
        `allows_token`.
        """
        outside = sorted(symbol for symbol in symbols if not self.allows_token(symbol))
        if outside:
            raise ExecutionLimitExceededError(
                "tokens permitidos",
                f"la operación toca {', '.join(outside)}, que no está en la lista "
                f"blanca ({_list(self.allowed_tokens)}). Añádelo si quieres operar "
                f"con él.",
            )

    def allows_chain(self, chain_key: str) -> bool:
        """Si la lista blanca deja firmar en `chain_key`.

        Espejo de `allows_token`, y con la misma forma: el comodín `["*"]`
        —cualquier red— es una decisión escrita a propósito en la configuración,
        no un vacío que se lea como «nada permitido». La lista de redes se
        normaliza a minúsculas al cargarla, pero se comparan **los dos lados**
        por la misma razón que en los tokens: unos límites construidos a mano
        —pruebas, cables sueltos— pueden traer la lista sin normalizar, y una
        comprobación que dependiera de que alguien la normalizara antes sería
        una trampa esperando a su dueño.

        La usan `check_chain` **y los avisos de la interfaz** (la tarjeta de
        predicción al publicar, al retirar y al cobrar): una sola respuesta para
        todos, como en los tokens.
        """
        if ANY_CHAIN in self.allowed_chains:
            return True
        return chain_key.lower() in {item.lower() for item in self.allowed_chains}

    def check_chain(self, chain_key: str) -> None:
        if not self.allows_chain(chain_key):
            raise ExecutionLimitExceededError(
                "redes permitidas",
                f"«{chain_key}» no está entre las redes habilitadas "
                f"({_list(self.allowed_chains)}).",
            )

    def check_engine(self, engine_id: str) -> None:
        if engine_id not in self.allowed_engines:
            raise ExecutionLimitExceededError(
                "motores permitidos",
                f"el motor «{engine_id}» no está habilitado para firmar "
                f"({_list(self.allowed_engines)}). Un motor que sólo cotiza no debería "
                f"poder construir lo que se firma.",
            )

    def check_amount(self, notional: Decimal, *, spent_today: Decimal) -> None:
        """Corta si el importe de referencia supera el tope por operación o el diario.

        `spent_today` lo aporta el registro de ejecuciones, y por eso el tope
        diario significa algo: si sólo se contara lo de esta sesión, reiniciar la
        aplicación lo borraría y no sería un tope.
        """
        if self.max_quote_per_trade is not None and notional > self.max_quote_per_trade:
            raise ExecutionLimitExceededError(
                "importe por operación",
                f"la operación vale {notional:f} y el máximo por operación es "
                f"{self.max_quote_per_trade:f}",
            )
        if self.max_quote_per_day is not None:
            accumulated = spent_today + notional
            if accumulated > self.max_quote_per_day:
                raise ExecutionLimitExceededError(
                    "importe acumulado en 24 h",
                    f"ya se ejecutaron {spent_today:f} en las últimas 24 h y esta "
                    f"operación sumaría {notional:f}, hasta {accumulated:f} sobre un "
                    f"máximo de {self.max_quote_per_day:f}",
                )

    def describe(self) -> tuple[tuple[str, str], ...]:
        """Los límites en vigor, para que el usuario los tenga a la vista."""
        return (
            ("Ejecución", "habilitada" if self.enabled else "apagada"),
            ("Por operación", _decimal(self.max_quote_per_trade)),
            ("Cada 24 h", _decimal(self.max_quote_per_day)),
            ("Tokens", _list(self.allowed_tokens)),
            ("Redes", _list(self.allowed_chains)),
            ("Motores que firman", _list(self.allowed_engines)),
            ("Operaciones por ciclo", str(self.max_executions_per_cycle)),
            ("Slippage tolerado", f"{self.slippage_bps} bps"),
        )


def _list(values: frozenset[str]) -> str:
    return ", ".join(sorted(values)) if values else "ninguno"


def _decimal(value: Decimal | None) -> str:
    return f"{value:f}" if value is not None else _UNSET


def parse_amount(value: str | int | Decimal | None, *, field_name: str) -> Decimal | None:
    """Convierte una cifra de configuración a `Decimal` exacto, o `None`.

    La configuración llega como texto del TOML. Se pasa por `str` antes de
    `Decimal` para que un `float` no pueda colarse: un tope de gasto que se
    hubiera redondeado en algún punto es un tope que no se cumple.
    """
    if value is None:
        return None
    if isinstance(value, float):  # pragma: no cover - pydantic ya lo impide
        raise InvalidAmountError(
            f"{field_name} llegó como float ({value}): los importes son Decimal o texto"
        )
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as error:
        raise InvalidAmountError(f"{field_name} no es una cifra válida: «{value}»") from error
