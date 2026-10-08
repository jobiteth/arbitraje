"""El viaje de ida y vuelta: qué se gana de verdad, y cuándo gana.

### Por qué no basta con `Opportunity`

`Opportunity.net_spread_bps` (ver `domain.models`) compara dos cotizaciones **del
mismo sentido** y resta las comisiones de los dos lados. Es un buen indicio de
dónde mirar, pero no es lo que se cobra, por dos razones que no son matices:

1. **La vuelta no está cotizada.** El diferencial supone que se sale al mismo
   precio al que se entró. En un AMM eso es falso: la ida mueve el pool de la ida
   y la vuelta mueve el de la vuelta, y el impacto se paga **dos veces**. Cuanto
   mayor el importe, más falso.
2. **No incluye gas**, y lo dice su propio docstring. En un viaje de diez
   unidades sobre un diferencial de 50 bps —el caso real de esta aplicación con
   los topes que trae— el gas de dos swaps es del orden del margen entero, así
   que omitirlo no desplaza el resultado: le cambia el signo.

Por eso aquí un viaje se **mide**: la ida se cotiza con el importe que se
entrega, y la vuelta se cotiza con **el importe que la ida devuelve**. Lo que
queda, menos el gas de las dos patas, es el beneficio. Lo demás es una
estimación con aspecto de dato.

### Qué no promete este módulo

Que el viaje sea atómico. Son dos transacciones, y entre una y otra el precio se
mueve —precisamente por eso había diferencial—. `RoundTrip` describe una medida
tomada en un instante, y `wins()` responde sobre esa medida: quien ejecuta tiene
que volver a medir antes de cada pata, porque un viaje que ganaba hace treinta
segundos no es un viaje que gane. Ver `app.usecases.execute_arbitrage`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from amigocompora.domain.errors import CurrencyMismatchError, InvalidAmountError
from amigocompora.domain.models import Measurement, Quote, Token
from amigocompora.domain.money import EXACT, BasisPoints, TokenAmount

#: Techo de gas por motor, en unidades de gas, para **dos** patas de un swap de
#: un solo salto.
#:
#: **No son mediciones de esta aplicación**, y decirlo importa: son cotas por
#: encima de lo que consume un swap sencillo en los routers de cada protocolo,
#: elegidas para que el error caiga siempre del mismo lado. Sobreestimar el gas
#: sólo puede hacer que se descarte un viaje que habría ganado; subestimarlo hace
#: que se firme uno que pierde, y el segundo error cuesta dinero mientras el
#: primero sólo cuesta una oportunidad. Con «siempre debe ganar» como requisito,
#: la elección no es dudosa.
#:
#: Se calibran leyendo el diario de viajes (`app.arbitrage_journal`), que guarda
#: el hash de cada pata: el gas realmente consumido se lee del recibo en la red y
#: estas cifras se bajan a lo que se haya medido. Hasta entonces son un techo
#: declarado, marcado como `ESTIMATED` en cada viaje que las use.
GAS_CEILING_BY_ENGINE: Final[dict[str, int]] = {
    # Universal Router de Uniswap, un salto, con el permiso ya concedido.
    "uniswap_v3": 220_000,
    # V4 pasa por el PoolManager y liquida en `unlock`: más caro que V3.
    "uniswap_v4": 260_000,
    # Un agregador reparte la orden entre varios venues: el techo tiene que
    # cubrir la ruta multisalto, que es el caso normal y no el excepcional.
    "zeroex": 400_000,
}

#: Para un motor que no esté en la tabla. Deliberadamente por encima de todos los
#: que sí están: un motor desconocido es justo aquel sobre cuyo consumo no hay
#: nada que afirmar.
DEFAULT_GAS_CEILING: Final = 400_000


def gas_ceiling_for(engine_id: str) -> int:
    """Unidades de gas que se presuponen para **una** pata de este motor."""
    return GAS_CEILING_BY_ENGINE.get(engine_id, DEFAULT_GAS_CEILING)


@dataclass(frozen=True, slots=True)
class GasCost:
    """Lo que cuestan las dos patas, en el token nativo y en la moneda del viaje.

    Los dos campos existen porque sirven para cosas distintas. `native` es lo que
    de verdad se paga y lo que se ve en el explorador; `in_cash` es lo único que
    se puede **restar** del margen, porque el margen está en la moneda del viaje.

    `in_cash` puede ser `None`, y entonces el viaje no se declara ganador. Es la
    misma regla que ya aplica `ValueInReference` con los topes y que
    `scan_opportunities` aplica con `fee_bps is None`: una cifra que no se puede
    medir no se trata como un cero. Un gas de cero haría que cualquier
    diferencial pareciese beneficio.
    """

    #: Precio real del gas (de `read_fees`) por el techo de unidades. Siempre hay.
    native: TokenAmount
    #: `native` valorado en la moneda del viaje, o `None` si nadie sabe valorarlo.
    in_cash: TokenAmount | None
    #: Procedencia de la cifra. `ESTIMATED` mientras el techo venga de la tabla:
    #: el precio del gas es real, pero las unidades son un supuesto, y una cifra
    #: es tan exacta como su parte más floja.
    basis: Measurement = Measurement.ESTIMATED
    #: Cómo se obtuvo, para enseñarlo junto al número.
    detail: str = ""

    def __post_init__(self) -> None:
        if self.native.raw < 0:
            raise InvalidAmountError("el gas no puede ser negativo")
        if self.in_cash is not None and self.in_cash.raw < 0:
            raise InvalidAmountError("el gas valorado no puede ser negativo")

    @property
    def is_known(self) -> bool:
        """Si se puede restar del margen."""
        return self.in_cash is not None

    def describe(self) -> str:
        if self.in_cash is None:
            return f"{self.native} (sin valorar en la moneda del viaje)"
        return f"{self.native} ≈ {self.in_cash}"


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """Un viaje medido: se entrega `cash`, se vuelve a `cash`, y sobra o falta.

    El sentido del viaje lo decide **quién es `cash`**, y por eso no hay un campo
    «dirección»: con el par POL/USDC, un viaje con `cash=USDC` compra POL y lo
    vende, y uno con `cash=POL` vende POL y lo recompra. Son los dos sentidos
    pedidos —comprar y vender, o al revés— y son dos `RoundTrip` distintos, cada
    uno con su medida y su resultado. Añadir una bandera además de los tokens
    permitiría construir el estado contradictorio «dirección que no coincide con
    las patas».
    """

    #: La moneda del viaje. **No tiene que ser una stablecoin**: el margen se mide
    #: en lo que se entrega, así que un viaje en ETH se mide en ETH.
    cash: Token
    #: Lo que se compra para volver a venderlo (o se vende para recomprarlo).
    asset: Token
    cash_in: TokenAmount
    #: `cash` → `asset`, cotizada con `cash_in`.
    out_leg: Quote
    #: `asset` → `cash`, cotizada con **lo que la ida devuelve**, no con un
    #: importe redondo: ahí está la diferencia con el diferencial de una pata.
    back_leg: Quote
    gas: GasCost
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.cash.chain != self.asset.chain:
            raise CurrencyMismatchError(
                f"{self.cash.symbol} está en {self.cash.chain} y {self.asset.symbol} "
                f"en {self.asset.chain}: un viaje de ida y vuelta no cruza de red"
            )
        if self.cash.is_same_asset(self.asset):
            raise CurrencyMismatchError(
                f"un viaje no puede ser {self.cash.symbol} contra sí mismo"
            )
        if not self.out_leg.pair.base.is_same_asset(self.cash):
            raise CurrencyMismatchError(
                f"la ida sale de {self.out_leg.pair.base.symbol} y el viaje entrega "
                f"{self.cash.symbol}"
            )
        if not self.out_leg.pair.quote.is_same_asset(self.asset):
            raise CurrencyMismatchError(
                f"la ida compra {self.out_leg.pair.quote.symbol} y el activo del viaje "
                f"es {self.asset.symbol}"
            )
        if not self.back_leg.pair.base.is_same_asset(self.asset):
            raise CurrencyMismatchError(
                f"la vuelta vende {self.back_leg.pair.base.symbol} y el activo del "
                f"viaje es {self.asset.symbol}"
            )
        if not self.back_leg.pair.quote.is_same_asset(self.cash):
            raise CurrencyMismatchError(
                f"la vuelta acaba en {self.back_leg.pair.quote.symbol} y el viaje "
                f"vuelve a {self.cash.symbol}"
            )
        if self.out_leg.amount_in != self.cash_in:
            raise InvalidAmountError(
                f"la ida se cotizó con {self.out_leg.amount_in} y el viaje entrega "
                f"{self.cash_in}"
            )
        # El invariante que define «viaje medido»: la vuelta se cotiza con lo que
        # la ida devuelve. Cotizarla con otro importe daría un beneficio que nadie
        # puede cobrar, porque entre las dos patas no hay nada que añada ni quite
        # tokens.
        if self.back_leg.amount_in != self.out_leg.amount_out:
            raise InvalidAmountError(
                f"la vuelta se cotizó con {self.back_leg.amount_in} pero la ida "
                f"devuelve {self.out_leg.amount_out}: el viaje no está medido"
            )
        if self.gas.in_cash is not None and self.gas.in_cash.symbol != self.cash.symbol:
            raise CurrencyMismatchError(
                f"el gas está valorado en {self.gas.in_cash.symbol} y el viaje se mide "
                f"en {self.cash.symbol}"
            )
        if self.observed_at.tzinfo is None or self.observed_at.tzinfo.utcoffset(
            self.observed_at
        ) is None:
            raise InvalidAmountError("observed_at debe llevar zona horaria")

    # ------------------------------------------------------------------ leer #
    @property
    def chain(self) -> str:
        return self.cash.chain

    @property
    def pair_symbol(self) -> str:
        """Cómo se nombra el viaje en la tabla y en el diario."""
        return f"{self.asset.symbol}/{self.cash.symbol}"

    @property
    def route(self) -> str:
        """Los dos motores, en el orden en que se usan."""
        return f"{self.out_leg.engine_id} → {self.back_leg.engine_id}"

    @property
    def asset_held(self) -> TokenAmount:
        """Lo que queda en la cartera entre las dos patas."""
        return self.out_leg.amount_out

    @property
    def cash_out(self) -> TokenAmount:
        return self.back_leg.amount_out

    @property
    def gross_profit(self) -> TokenAmount:
        """Lo que sobra antes del gas. Puede ser negativo."""
        return self.cash_out - self.cash_in

    @property
    def net_profit(self) -> TokenAmount | None:
        """Lo que sobra después del gas, o `None` si el gas no se pudo valorar.

        `None` no es cero: es «no se sabe». Quien decida sobre esto no ejecuta.
        """
        if self.gas.in_cash is None:
            return None
        return self.gross_profit - self.gas.in_cash

    @property
    def gross_bps(self) -> BasisPoints:
        return _ratio_bps(self.gross_profit, self.cash_in)

    @property
    def net_bps(self) -> BasisPoints | None:
        net = self.net_profit
        if net is None:
            return None
        return _ratio_bps(net, self.cash_in)

    # ----------------------------------------------------------- decidir     #
    def wins(self, floor: TokenAmount) -> bool:
        """Si este viaje gana lo suficiente para hacerlo. **El único criterio.**

        Lo consultan la tabla, el bucle automático y el ejecutor. Tres sitios
        decidiendo por su cuenta qué significa «gana» es exactamente cómo se
        acaba firmando algo que la pantalla pintaba en verde.

        Pide las tres cosas a la vez: que el gas se haya podido medir, que el
        neto sea **positivo** y que llegue al suelo declarado. Lo segundo no lo
        implica lo tercero: un suelo de cero con `>=` dejaría pasar un viaje que
        no gana nada, y hacer dos transacciones para quedarse igual es perder.
        """
        if floor.symbol != self.cash_in.symbol:
            raise CurrencyMismatchError(
                f"el suelo está en {floor.symbol} y el viaje se mide en "
                f"{self.cash_in.symbol}"
            )
        if floor.raw < 0:
            raise InvalidAmountError(
                "el suelo de ganancia no puede ser negativo: sería autorizar pérdidas"
            )
        net = self.net_profit
        if net is None:
            return False
        return net.is_positive and net >= floor

    def why_not(self, floor: TokenAmount) -> str:
        """Por qué no gana, en una frase. Vacía si gana.

        Vive junto a `wins` para que el motivo no pueda desfasarse del criterio.
        """
        if self.gas.in_cash is None:
            return (
                f"no se pudo valorar el gas en {self.cash.symbol}, así que el neto no "
                f"se puede calcular: sin eso no se ejecuta"
            )
        net = self.net_profit
        if net is None:  # pragma: no cover - lo cubre la rama anterior
            return "el neto no se puede calcular"
        if not net.is_positive:
            return f"el viaje deja {net} después del gas ({self.gas.describe()})"
        if net < floor:
            return f"deja {net} y el suelo declarado es {floor}"
        return ""

    def describe(self) -> str:
        net = self.net_profit
        tail = f"neto {net}" if net is not None else "neto sin calcular (gas sin valorar)"
        return (
            f"{self.cash_in} → {self.asset_held} → {self.cash_out} "
            f"[{self.route}] {tail}"
        )


def _ratio_bps(part: TokenAmount, whole: TokenAmount) -> BasisPoints:
    """`part/whole` en puntos básicos, con el signo de `part`.

    `BasisPoints.from_ratio` redondea a entero, así que un margen por debajo de
    medio punto básico sale como 0 bps. Es correcto para mostrar y para ordenar,
    y por eso **no** es lo que decide si se opera: eso lo decide `net_profit`,
    que está en unidades del token y no pierde nada.
    """
    if whole.is_zero:
        raise InvalidAmountError("no se puede medir un margen sobre un importe cero")
    return BasisPoints.from_ratio(
        EXACT.divide(Decimal(part.raw), Decimal(whole.raw)),
    )
