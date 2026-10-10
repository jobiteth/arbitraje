"""AMM V2 por contrato: la vía sin clave para la familia de producto constante.

Lo que se fija aquí no es que «cotice». Es lo que puede perder dinero o publicar
una cifra falsa:

1. **La codificación.** El `path[]` de V2 es una **lista de direcciones real** —
   desplazamiento, longitud y una palabra por token—, al contrario que el `bytes
   path` empaquetado de V3; confundirlos no revierte al codificar, revierte en la
   cadena después de firmar. Y `swapExactETHForTokens` **no lleva `amountIn`**: el
   importe es el `value` de la transacción, así que leer su cabeza con el molde de
   `swapExactTokensForTokens` desplaza todos los campos una palabra.

2. **La comisión se mide, no se supone.** PancakeSwap cobra `9975/10000` —0,25 %—
   y su repositorio decía otra cosa; la prueba reproduce el número medido con
   aritmética entera y comprueba que **ninguna otra** constante lo reproduce. La
   tabla de direcciones se contrasta contra esa medición al construir cada
   entrada, y aquí se comprueba que ese contraste existe.

3. **El pool directo manda.** Si el par directo cotiza, ése es el resultado
   aunque una ruta de dos saltos diera más: elegir ruta es más gas y más
   superficie, y no lo decide el motor a espaldas del usuario.

4. **Al construir se ejecuta el camino publicado.** El plan vuelve a cotizar
   **el mismo camino** —no una búsqueda nueva que pudiera dar otro—, calcula el
   mínimo sobre la cotización fresca y aborta si el precio se movió más de lo
   tolerado. Y el `path[]` sale de la ruta que la cotización lleva dentro.

5. **Qué se hace con cada fallo.** Un revert del router al cotizar —par sin
   reservas, salida cero— es una respuesta del mercado y se lee como «no hay
   cotización»; un revert de `getPair` no se silencia, porque esa llamada no
   tiene motivo para revertir con entradas válidas.

Las cifras están **medidas** contra los contratos desplegados el 2026-10-09:
Base (Uniswap V2, 0,3 %), BNB Chain (PancakeSwap, 0,25 %) y Polygon (QuickSwap,
0,3 %), con las reservas y las salidas reales de sus pares contra el envuelto
nativo. El lector se sustituye por un doble, que es la frontera de red del
motor; todo lo demás que se ejercita es el código de verdad.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

import pytest

from amigocompora.domain.chains import chain
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    EngineConfigError,
    InvalidAmountError,
    NoQuotesError,
    QuoteMovedError,
    SourceResponseError,
    UnsupportedOperationError,
)
from amigocompora.domain.models import (
    Measurement,
    Quote,
    RouteHop,
    SwapRoute,
    Token,
    TokenApproval,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.money import EXACT, BasisPoints, TokenAmount
from amigocompora.engines.amm_protocols import CONSTANT_FEES, parse_dex_id
from amigocompora.engines.catalog import token_by_symbol, wrapped_native
from amigocompora.engines.uniswap_math import combine_impact_bps, impact_bps_from_marginal
from amigocompora.engines.uniswap_v2 import calldata as abi
from amigocompora.engines.uniswap_v2 import engine as v2
from amigocompora.engines.uniswap_v2.addresses import DEPLOYMENTS, SWAP_CHAINS, Deployment

AHORA = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
DEADLINE = int(AHORA.timestamp()) + 1800

#: Medido contra Base el 2026-10-09: el pool WETH/USDC de Uniswap V2, sus
#: reservas y lo que dio `getAmountsOut` para 0,1787… WETH.
POOL_BASE = "0x88a43bbdf9d098eec7bceda4e2494615dfd9bb9c"
RESERVA_WETH_BASE = 178_770_929_501_812_672_143
RESERVA_USDC_BASE = 444_496_854_693
ENTRADA_BASE = 178_770_929_501_812_672
SALIDA_BASE = 442_721_970

#: Medido contra BNB Chain el 2026-10-09: el pool USDT/WBNB de PancakeSwap,
#: sus reservas y lo que dio `getAmountsOut` para 29.024,69… USDT. La salida
#: reproduce la fórmula con `9975/10000` **y con ninguna otra**.
POOL_BSC = "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"
RESERVA_USDT_BSC = 29_026_494_035_356_300_437_190_611
RESERVA_WBNB_BSC = 39_147_291_996_640_605_763_103
ENTRADA_BSC = 29_024_692_343_227_066_646_886
SALIDA_BSC = 39_008_091_788_822_147_237

#: Medido contra Polygon el 2026-10-09: el pool WPOL/USDC de QuickSwap.
POOL_POLYGON = "0x6d9e8dbb2779853db00418d4dcf96f3987cfc9d2"
RESERVA_WPOL_POLYGON = 6_967_980_519_070_209_505_845
RESERVA_USDC_POLYGON = 693_814_750
ENTRADA_POLYGON = 6_967_980_519_070_209_505
SALIDA_POLYGON = 691_044

DESTINATARIO = "0x" + "b" * 40

#: Un token inventado para los escenarios de ruta: no está en el catálogo, así
#: que ningún hub se confunde con él y su par directo no existe por definición.
ZORRO = "0x" + "f0" * 20


# --------------------------------------------------------------------------- #
# Piezas
# --------------------------------------------------------------------------- #
def _palabra_uint(value: int) -> str:
    return f"{value:064x}"


def _palabra_addr(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _cero() -> str:
    return "0x" + _palabra_uint(0)


def _reservas(reserve0: int, reserve1: int) -> str:
    """`getReserves()` real: dos `uint112` y el `blockTimestampLast` detrás.

    La palabra de más se deja puesta a propósito: el motor tiene que leer las dos
    primeras y ignorarla, y una prueba que devolviera sólo dos palabras mediría
    una respuesta que ningún par contesta.
    """
    return "0x" + _palabra_uint(reserve0) + _palabra_uint(reserve1) + _palabra_uint(0)


def _salida_router(amounts: tuple[int, ...]) -> str:
    """La respuesta de `getAmountsOut`: un array dinámico de `uint256`."""
    return (
        "0x"
        + _palabra_uint(32)
        + _palabra_uint(len(amounts))
        + "".join(_palabra_uint(value) for value in amounts)
    )


def _formula_producto_constante(
    amount_in: int, reserve_in: int, reserve_out: int, fee_numerator: int, fee_denominator: int
) -> int:
    """La aritmética entera de `UniswapV2Library.getAmountOut`, de referencia.

    Es la cuenta que usó la medición para decidir la comisión de cada DEX: el
    contrato desplegado reproduce **exactamente** esta fórmula con una única
    comisión candidata, y con ninguna otra. Escribirla aquí, en la prueba, es lo
    que permite contrastar el número medido contra el número leído.
    """
    fee_amount = amount_in * fee_numerator
    return (fee_amount * reserve_out) // (reserve_in * fee_denominator + fee_amount)


def _dir(token: Token) -> str:
    """La dirección con la que el motor pregunta: en minúsculas."""
    return (token.address or "").lower()


def _weth(chain_key: str = "base") -> Token:
    envoltorio = wrapped_native(chain_key)
    assert envoltorio is not None
    return envoltorio


def _stable(symbol: str, chain_key: str) -> Token:
    token = token_by_symbol(symbol, chain_key)
    assert token is not None
    return token


def _nativo(chain_key: str = "base") -> Token:
    spec = chain(chain_key)
    return Token(chain=chain_key, address=None, symbol=spec.native_symbol, decimals=18)


def _zorro(chain_key: str = "base") -> Token:
    return Token(chain=chain_key, address=ZORRO, symbol="ZZZ", decimals=18)


def _par(base: Token, quote: Token) -> TradingPair:
    return TradingPair(base=base, quote=quote)


class _Lector:
    """Doble de `ChainReader`, que despacha por **calldata** y no por dirección.

    Se sustituye el lector entero y no el transporte HTTP porque lo que se
    ejercita es la **decisión** del motor —qué pregunta y qué hace con cada
    respuesta—, y el failover tiene sus propias pruebas. Despachar por calldata
    y no por dirección es lo que permite que `getReserves()` y `token0()` vayan
    al pool correcto: con un doble por dirección, las lecturas de dos pools
    recibirían el mismo hexadecimal y la prueba pasaría por el motivo equivocado.
    """

    def __init__(self, handlers: dict[str, object] | None = None) -> None:
        self.handlers = handlers or {}
        self.calls: list[tuple[str, str]] = []

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def eth_call(self, chain_key: str, to: str, data: str, *, block: str = "latest") -> str:
        del chain_key, block
        self.calls.append((to, data))
        handler = self.handlers.get(data)
        if handler is None:
            handler = self._por_defecto(to, data)
        if handler is None:
            raise AssertionError(f"lectura no prevista a {to} con {data[:10]}")
        if isinstance(handler, Exception):
            raise handler
        assert isinstance(handler, str)
        return handler

    def _por_defecto(self, to: str, data: str) -> object:
        return None

    def calls_to(self, address: str) -> list[str]:
        return [data for target, data in self.calls if target.lower() == address.lower()]


@dataclass(frozen=True, slots=True)
class _Pool:
    """Un pool del doble: dónde vive, quién es su `token0` y sus dos reservas."""

    address: str
    token0: str
    reserve0: int
    reserve1: int


class _Escenario(_Lector):
    """Pools, pares y salidas del router declarados por calldata exacto.

    Todo lo que el motor pregunte tiene que estar declarado aquí: un `getPair`
    que no se esperaba es una prueba que está midiendo otro camino del que cree,
    y el doble lo dice en vez de contestar cero.
    """

    def __init__(
        self,
        *,
        pools: dict[str, _Pool] | None = None,
        pares: dict[str, str | None] | None = None,
        salidas: dict[str, tuple[int, ...]] | None = None,
    ) -> None:
        super().__init__()
        self.pools = {address.lower(): pool for address, pool in (pools or {}).items()}
        self.pares = pares or {}
        self.salidas = salidas or {}

    def _por_defecto(self, to: str, data: str) -> object:
        if data in self.pares:
            direccion = self.pares[data]
            # La dirección cero es «no hay pool en ese par»: la respuesta normal
            # de la fábrica y no un error.
            return _cero() if direccion is None else "0x" + _palabra_addr(direccion)
        if data in self.salidas:
            return _salida_router(self.salidas[data])
        pool = self.pools.get(to.lower())
        if pool is not None:
            if data == abi.SELECTOR_GET_RESERVES:
                return _reservas(pool.reserve0, pool.reserve1)
            if data == abi.SELECTOR_TOKEN0:
                return "0x" + _palabra_addr(pool.token0)
        return None


def _motor(lector: _Lector) -> v2.UniswapV2Engine:
    # El doble cumple la superficie que el motor usa del lector; el motor lo
    # acepta por inyección precisamente para que esto sea posible.
    return v2.UniswapV2Engine(clock=FrozenClock(AHORA), reader=lector)  # type: ignore[arg-type]


def _entrada(amount_in_raw: int, base: Token) -> TokenAmount:
    return TokenAmount(amount_in_raw, base.decimals, base.symbol)


def _cotizacion(
    base: Token,
    quote: Token,
    *,
    amount_in_raw: int,
    salida: int,
    ruta: SwapRoute | None = None,
    venue: Venue | None = None,
) -> Quote:
    """Una cotización armada a mano, para ejercitar `plan_swap` sin la búsqueda."""
    chain_key = base.chain
    deployment = DEPLOYMENTS[chain_key]
    if venue is None:
        venue = (
            v2._route_venue(chain_key, deployment)
            if ruta is not None
            else v2._venue(chain_key, deployment)
        )
    return Quote(
        venue=venue,
        engine_id="uniswap_v2",
        pair=_par(base, quote),
        amount_in=_entrada(amount_in_raw, base),
        amount_out=TokenAmount(salida, quote.decimals, quote.symbol),
        fee_bps=BasisPoints(deployment.fee_bps),
        fee_basis=Measurement.DERIVED,
        price_impact_bps=BasisPoints(5),
        impact_basis=Measurement.DERIVED,
        observed_at=AHORA,
        route=ruta,
    )


def _escenario_base(*, salida: int = SALIDA_BASE) -> _Escenario:
    """El pool WETH/USDC de Uniswap V2 en Base, con las cifras medidas."""
    weth = _dir(_weth())
    usdc = _dir(_stable("USDC", "base"))
    return _Escenario(
        pools={
            POOL_BASE: _Pool(
                address=POOL_BASE,
                token0=weth,
                reserve0=RESERVA_WETH_BASE,
                reserve1=RESERVA_USDC_BASE,
            )
        },
        pares={abi.get_pair(weth, usdc): POOL_BASE},
        salidas={abi.get_amounts_out(ENTRADA_BASE, (weth, usdc)): (ENTRADA_BASE, salida)},
    )


# --------------------------------------------------------------------------- #
# 1. La codificación
# --------------------------------------------------------------------------- #
def test_cada_firma_produce_el_selector_que_se_buscó_en_la_cadena() -> None:
    """Los selectores salieron de escanear el bytecode desplegado de los routers.

    Si alguien cambia una firma, el selector deja de coincidir con el que el
    contrato tiene, y la llamada entra por otra función del mismo contrato.
    """
    assert abi.SELECTOR_GET_PAIR == "0xe6a43905"
    assert abi.SELECTOR_GET_RESERVES == "0x0902f1ac"
    assert abi.SELECTOR_TOKEN0 == "0x0dfe1681"
    assert abi.SELECTOR_GET_AMOUNTS_OUT == "0xd06ca61f"
    assert abi.SELECTOR_SWAP_EXACT_TOKENS_FOR_TOKENS == "0x38ed1739"
    assert abi.SELECTOR_SWAP_EXACT_ETH_FOR_TOKENS == "0x7ff36ab5"
    assert abi.SELECTOR_SWAP_EXACT_TOKENS_FOR_ETH == "0x18cbafe5"


def test_la_comprobacion_de_los_selectores_no_es_decorativa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si una firma se desvía de lo medido, el módulo **no** se importa.

    Sin esta prueba, `_require_measured_selectors` sería una función que se llama
    y no comprueba nada, que es peor que no tenerla: da la sensación de que hay
    una verificación.

    La tabla se sustituye entera en vez de mutarla: es un `Mapping` inmutable a
    propósito —que nadie la edite en caliente es parte de lo que la hace una
    medición—, así que se cambia el atributo del módulo y `monkeypatch` lo deja
    como estaba al acabar, pase lo que pase.
    """
    monkeypatch.setattr(
        abi, "_MEASURED", {**abi._MEASURED, abi.GET_PAIR: "0x00000000"}
    )
    with pytest.raises(EngineConfigError, match="no coincide con la que se midió"):
        abi._require_measured_selectors()


def test_get_amounts_out_codifica_una_lista_de_direcciones_de_verdad() -> None:
    """`path[]` es un `address[]`: desplazamiento, longitud y una palabra por token.

    Al contrario que el `bytes path` empaquetado de V3 —que no lleva longitud por
    medio—, aquí la lista se codifica como array dinámico. Codificarla empaquetada
    no revierte al construirse: el router lee la longitud donde hay una dirección
    y busca pools que no existen.
    """
    weth = _dir(_weth())
    usdc = _dir(_stable("USDC", "base"))
    esperado = (
        "0xd06ca61f"
        + _palabra_uint(1_000)
        + _palabra_uint(64)
        + _palabra_uint(2)
        + _palabra_addr(weth)
        + _palabra_addr(usdc)
    )
    assert abi.get_amounts_out(1_000, (weth, usdc)) == esperado


def test_swap_exact_eth_for_tokens_no_lleva_amount_in() -> None:
    """El importe de entrada va en el `value`, no en la llamada.

    La cabeza de `swapExactETHForTokens` es `(amountOutMin, path, to, deadline)`:
    cuatro palabras, con el array en la palabra 0x80. Leerla con el molde de
    `swapExactTokensForTokens` —que sí lleva `amountIn`— desplaza todos los
    campos una palabra y la transacción entregaría otra cosa.
    """
    weth = _dir(_weth())
    usdc = _dir(_stable("USDC", "base"))
    compra = abi.swap_exact_eth_for_tokens(
        amount_out_min_raw=900, path=(weth, usdc), recipient=DESTINATARIO, deadline=DEADLINE
    )
    assert compra == (
        "0x7ff36ab5"
        + _palabra_uint(900)
        + _palabra_uint(0x80)
        + _palabra_addr(DESTINATARIO)
        + _palabra_uint(DEADLINE)
        + _palabra_uint(2)
        + _palabra_addr(weth)
        + _palabra_addr(usdc)
    )
    # La misma operación con tokens sí lleva el importe, y su array va en 0xA0
    # porque hay una palabra de cabeza más. Los dos offsets no son intercambiables.
    venta = abi.swap_exact_tokens_for_tokens(
        amount_in_raw=1_000,
        amount_out_min_raw=900,
        path=(weth, usdc),
        recipient=DESTINATARIO,
        deadline=DEADLINE,
    )
    assert venta == (
        "0x38ed1739"
        + _palabra_uint(1_000)
        + _palabra_uint(900)
        + _palabra_uint(0xA0)
        + _palabra_addr(DESTINATARIO)
        + _palabra_uint(DEADLINE)
        + _palabra_uint(2)
        + _palabra_addr(weth)
        + _palabra_addr(usdc)
    )
    retirada = abi.swap_exact_tokens_for_eth(
        amount_in_raw=1_000,
        amount_out_min_raw=900,
        path=(usdc, weth),
        recipient=DESTINATARIO,
        deadline=DEADLINE,
    )
    assert retirada == (
        "0x18cbafe5"
        + _palabra_uint(1_000)
        + _palabra_uint(900)
        + _palabra_uint(0xA0)
        + _palabra_addr(DESTINATARIO)
        + _palabra_uint(DEADLINE)
        + _palabra_uint(2)
        + _palabra_addr(usdc)
        + _palabra_addr(weth)
    )


def test_un_camino_de_un_solo_token_no_une_nada() -> None:
    """Un swap necesita entrada y salida: un camino de uno no tiene segundo par."""
    weth = _dir(_weth())
    with pytest.raises(SourceResponseError, match="no une nada"):
        abi.get_amounts_out(1_000, (weth,))
    with pytest.raises(SourceResponseError, match="no une nada"):
        abi.swap_exact_eth_for_tokens(
            amount_out_min_raw=1, path=(weth,), recipient=DESTINATARIO, deadline=DEADLINE
        )


def test_una_direccion_mal_formada_no_construye_el_camino() -> None:
    """`word_address` exige los 40 dígitos: una dirección corta es otro contrato.

    Una dirección de 39 dígitos no corre los bytes siguientes —cada token ocupa
    su palabra—, pero construiría el camino contra un contrato que no es el que
    se cree, y el swap se firmaría igual.
    """
    with pytest.raises(InvalidAmountError, match="40 dígitos"):
        abi.get_amounts_out(1_000, ("0x" + "ab" * 19, "0x" + "cd" * 20))


# --------------------------------------------------------------------------- #
# 2. La decodificación
# --------------------------------------------------------------------------- #
def test_decode_amounts_out_comprueba_la_forma_y_no_supone() -> None:
    """La respuesta tiene que ser un array dinámico con un importe por token.

    Una longitud distinta significa que esa respuesta no es la de esta función
    —otra llamada al mismo contrato, por ejemplo— y se dice en vez de recortarla.
    """
    bien = _salida_router((1_000, 900, 800))
    assert abi.decode_amounts_out(bien, hops=2) == (1_000, 900, 800)
    with pytest.raises(SourceResponseError, match="no es la de esta función"):
        abi.decode_amounts_out(bien, hops=1)
    desviada = "0x" + _palabra_uint(64) + _palabra_uint(2) + _palabra_uint(1) + _palabra_uint(2)
    with pytest.raises(SourceResponseError, match="no es la de esta función"):
        abi.decode_amounts_out(desviada, hops=1)


def test_una_respuesta_truncada_no_se_lee_como_un_pool_vacio() -> None:
    """`None` y `(0, 0)` son cosas distintas: una es «no hay dato», la otra un pool.

    Un nodo que contesta `0x` a un contrato que no conoce devuelve una respuesta
    truncada; leerla como ceros haría cotizar un pool vacío como si existiera.
    """
    assert abi.decode_reserves("0x") is None
    assert abi.decode_amounts_out("0x", hops=1) is None
    assert abi.decode_address("0x") is None
    assert abi.decode_reserves(_reservas(0, 0)) == (0, 0)


# --------------------------------------------------------------------------- #
# 3. La cotización directa
# --------------------------------------------------------------------------- #
async def test_cotiza_el_pool_directo_con_las_cifras_medidas() -> None:
    """El importe publicado es el que el router da, y la comisión es la medida.

    La prueba reproduce además la medición: la salida del router es exactamente
    la fórmula de producto constante con `997/1000`. Esa igualdad es lo que
    demostró que el despliegue de Base cobra 0,3 % y no otra cosa.
    """
    lector = _escenario_base()
    base = _weth()
    par = _par(base, _stable("USDC", "base"))
    cotizaciones = await _motor(lector).quote(par, _entrada(ENTRADA_BASE, base))
    assert len(cotizaciones) == 1
    cotizacion = cotizaciones[0]

    assert _formula_producto_constante(
        ENTRADA_BASE, RESERVA_WETH_BASE, RESERVA_USDC_BASE, 997, 1000
    ) == SALIDA_BASE
    assert cotizacion.amount_out.raw == SALIDA_BASE
    assert cotizacion.fee_bps == BasisPoints(30)
    assert cotizacion.fee_basis is Measurement.DERIVED
    assert cotizacion.venue.venue_id == "uniswap-v2@30"
    assert cotizacion.engine_id == "uniswap_v2"
    assert cotizacion.route is None

    # La liquidez publicada es la reserva del lado quote del pool, medida.
    assert cotizacion.liquidity is not None
    assert cotizacion.liquidity.raw == RESERVA_USDC_BASE
    assert cotizacion.liquidity.symbol == "USDC"

    # El impacto sale del marginal de las reservas, con la comisión descontada
    # una sola vez: recomponerlo aquí con los mismos datos medidos lo contrasta.
    marginal = EXACT.divide(Decimal(RESERVA_USDC_BASE), Decimal(RESERVA_WETH_BASE))
    esperado = impact_bps_from_marginal(
        amount_in_raw=ENTRADA_BASE,
        amount_out_raw=SALIDA_BASE,
        marginal_out_per_in=marginal,
        fee=BasisPoints(30),
    )
    assert cotizacion.price_impact_bps == esperado
    assert cotizacion.price_impact_bps is not None
    assert 0 < cotizacion.price_impact_bps.value < 100


async def test_pancakeswap_cotiza_con_su_comision_medida() -> None:
    """La comisión de PancakeSwap es la que se midió: 25 bps, no 30.

    Su repositorio y su documentación no coincidían; el contrato desplegado sí
    lo dice: su `getAmountsOut` sobre este par reproduce la fórmula con
    `9975/10000` y **no** con `997/1000`. La segunda igualdad de la prueba es lo
    que descarta la comisión de la familia.
    """
    usdt = _stable("USDT", "bsc")
    wbnb = _weth("bsc")
    lector = _Escenario(
        pools={
            POOL_BSC: _Pool(
                address=POOL_BSC,
                token0=_dir(usdt),
                reserve0=RESERVA_USDT_BSC,
                reserve1=RESERVA_WBNB_BSC,
            )
        },
        pares={abi.get_pair(_dir(usdt), _dir(wbnb)): POOL_BSC},
        salidas={
            abi.get_amounts_out(ENTRADA_BSC, (_dir(usdt), _dir(wbnb))): (ENTRADA_BSC, SALIDA_BSC)
        },
    )
    par = _par(usdt, wbnb)
    cotizaciones = await _motor(lector).quote(par, _entrada(ENTRADA_BSC, usdt))
    assert len(cotizaciones) == 1

    assert _formula_producto_constante(
        ENTRADA_BSC, RESERVA_USDT_BSC, RESERVA_WBNB_BSC, 9975, 10000
    ) == SALIDA_BSC
    assert _formula_producto_constante(
        ENTRADA_BSC, RESERVA_USDT_BSC, RESERVA_WBNB_BSC, 997, 1000
    ) != SALIDA_BSC
    assert cotizaciones[0].fee_bps == BasisPoints(25)
    assert cotizaciones[0].venue.venue_id == "pancakeswap-v2@25"


async def test_quickswap_cotiza_con_su_comision_medida() -> None:
    """QuickSwap entró con la misma prueba: 30 bps medidos en su pool de Polygon."""
    wpol = _weth("polygon")
    usdc = _stable("USDC", "polygon")
    lector = _Escenario(
        pools={
            POOL_POLYGON: _Pool(
                address=POOL_POLYGON,
                token0=_dir(wpol),
                reserve0=RESERVA_WPOL_POLYGON,
                reserve1=RESERVA_USDC_POLYGON,
            )
        },
        pares={abi.get_pair(_dir(wpol), _dir(usdc)): POOL_POLYGON},
        salidas={
            abi.get_amounts_out(ENTRADA_POLYGON, (_dir(wpol), _dir(usdc))): (
                ENTRADA_POLYGON,
                SALIDA_POLYGON,
            )
        },
    )
    par = _par(wpol, usdc)
    cotizaciones = await _motor(lector).quote(par, _entrada(ENTRADA_POLYGON, wpol))
    assert len(cotizaciones) == 1
    assert _formula_producto_constante(
        ENTRADA_POLYGON, RESERVA_WPOL_POLYGON, RESERVA_USDC_POLYGON, 997, 1000
    ) == SALIDA_POLYGON
    assert cotizaciones[0].venue.venue_id == "quickswap-v2@30"


async def test_un_revert_del_router_es_una_respuesta_y_no_una_caida() -> None:
    """`getAmountsOut` revierte cuando no puede dar salida: eso es «no hay cotización».

    El router revierte con `INSUFFICIENT_LIQUIDITY` —medido— cuando las reservas
    no cubren la orden; leerlo como una caída haría que un par sin profundidad
    tumbara la cotización entera en vez de quedarse sin fila.
    """
    lector = _escenario_base()
    weth = _dir(_weth())
    usdc = _dir(_stable("USDC", "base"))
    clave = abi.get_amounts_out(ENTRADA_BASE, (weth, usdc))
    lector.handlers[clave] = SourceResponseError("execution reverted: INSUFFICIENT_LIQUIDITY")
    # Sin pool directo que cotice, la búsqueda de hubs tampoco encuentra: los pares
    # del par con cada hub candidato no existen. WETH y USDC quedan fuera de la
    # búsqueda —un hub no da la vuelta al par contra sí mismo—, así que los
    # candidatos de este par son los otros dos.
    for symbol in ("cbBTC", "USDT"):
        token = token_by_symbol(symbol, "base")
        assert token is not None
        lector.pares[abi.get_pair(weth, _dir(token))] = None
        lector.pares[abi.get_pair(_dir(token), usdc)] = None
    par = _par(_weth(), _stable("USDC", "base"))
    cotizaciones = await _motor(lector).quote(par, _entrada(ENTRADA_BASE, par.base))
    assert cotizaciones == ()


async def test_un_get_pair_que_revierte_aborta() -> None:
    """La fábrica no tiene motivo para revertir: si lo hace, algo no es lo que dice."""
    lector = _escenario_base()
    weth = _dir(_weth())
    usdc = _dir(_stable("USDC", "base"))
    lector.handlers[abi.get_pair(weth, usdc)] = SourceResponseError("execution reverted")
    par = _par(_weth(), _stable("USDC", "base"))
    with pytest.raises(SourceResponseError):
        await _motor(lector).quote(par, _entrada(ENTRADA_BASE, par.base))


async def test_el_pool_directo_no_se_cambia_por_una_ruta_que_daria_mas() -> None:
    """Si el par directo cotiza, ése es el resultado, aunque el hub diera más.

    Elegir ruta es una decisión con más gas y más superficie de contrato, y no la
    toma el motor a espaldas del usuario. El segundo salto es para los pares que
    hoy no tendrían nada.
    """
    z = _zorro()
    usdc = _stable("USDC", "base")
    weth = _weth()
    pool_directo = "0x" + "aa" * 20
    pool_z_w = "0x" + "bb" * 20
    # La salida del pool directo sale de la fórmula sobre sus propias reservas:
    # un doble que devolviera otra cosa mediría un impacto que no existe.
    salida_directa = _formula_producto_constante(10**20, 10**24, 10**12, 997, 1000)
    lector = _Escenario(
        pools={
            pool_directo: _Pool(
                address=pool_directo, token0=_dir(z), reserve0=10**24, reserve1=10**12
            ),
            pool_z_w: _Pool(address=pool_z_w, token0=_dir(z), reserve0=10**24, reserve1=10**21),
            POOL_BASE: _Pool(
                address=POOL_BASE,
                token0=_dir(weth),
                reserve0=RESERVA_WETH_BASE,
                reserve1=RESERVA_USDC_BASE,
            ),
        },
        pares={
            abi.get_pair(_dir(z), _dir(usdc)): pool_directo,
            abi.get_pair(_dir(z), _dir(weth)): pool_z_w,
            abi.get_pair(_dir(weth), _dir(usdc)): POOL_BASE,
        },
        salidas={
            abi.get_amounts_out(10**20, (_dir(z), _dir(usdc))): (10**20, salida_directa),
            # La ruta que el motor no debe preferir: da bastante más.
            abi.get_amounts_out(10**20, (_dir(z), _dir(weth), _dir(usdc))): (
                10**20,
                10**18,
                500_000_000,
            ),
        },
    )
    cotizaciones = await _motor(lector).quote(_par(z, usdc), _entrada(10**20, z))
    assert len(cotizaciones) == 1
    assert cotizaciones[0].route is None
    assert cotizaciones[0].amount_out.raw == salida_directa
    assert salida_directa < 500_000_000


# --------------------------------------------------------------------------- #
# 4. El segundo salto
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Ruta:
    """El escenario de una ruta ZZZ → WETH → USDC y las cifras que da el router."""

    lector: _Escenario
    entrada: int
    mitad: int
    salida: int
    reserva_z: int
    reserva_w: int
    reserva_w_pool: int
    reserva_usdc_pool: int


def _escenario_ruta() -> _Ruta:
    """Un par sin pool directo, con ruta viva por WETH y el resto de hubs secos.

    Las cifras intermedias salen de la fórmula de producto constante sobre las
    reservas declaradas: el doble devuelve el número que el router devolvería,
    que es lo que hace que el impacto compuesto sea el de verdad.
    """
    z = _dir(_zorro())
    usdc = _dir(_stable("USDC", "base"))
    weth = _dir(_weth())
    reserva_z = 1_000_000 * 10**18
    reserva_w = 500 * 10**18
    entrada = 1_000 * 10**18
    mitad = _formula_producto_constante(entrada, reserva_z, reserva_w, 997, 1000)
    salida = _formula_producto_constante(mitad, RESERVA_WETH_BASE, RESERVA_USDC_BASE, 997, 1000)
    pool_z_w = "0x" + "cc" * 20
    pares: dict[str, str | None] = {
        abi.get_pair(z, usdc): None,
        abi.get_pair(z, weth): pool_z_w,
        abi.get_pair(weth, usdc): POOL_BASE,
    }
    # El resto de hubs del catálogo no tienen par con el zorro: se declaran secos
    # para que la prueba no confunda «no hay ruta» con «no se preguntó».
    for symbol in ("cbBTC", "USDT"):
        token = token_by_symbol(symbol, "base")
        assert token is not None
        pares[abi.get_pair(z, _dir(token))] = None
        pares[abi.get_pair(_dir(token), usdc)] = None
    lector = _Escenario(
        pools={
            pool_z_w: _Pool(
                address=pool_z_w, token0=z, reserve0=reserva_z, reserve1=reserva_w
            ),
            POOL_BASE: _Pool(
                address=POOL_BASE,
                token0=weth,
                reserve0=RESERVA_WETH_BASE,
                reserve1=RESERVA_USDC_BASE,
            ),
        },
        pares=pares,
        salidas={abi.get_amounts_out(entrada, (z, weth, usdc)): (entrada, mitad, salida)},
    )
    return _Ruta(
        lector=lector,
        entrada=entrada,
        mitad=mitad,
        salida=salida,
        reserva_z=reserva_z,
        reserva_w=reserva_w,
        reserva_w_pool=RESERVA_WETH_BASE,
        reserva_usdc_pool=RESERVA_USDC_BASE,
    )


async def test_sin_pool_directo_busca_dos_saltos_por_los_hubs() -> None:
    """Sin pool directo, la ruta sale de los hubs del catálogo, en una sola lectura.

    El camino entero se cotiza con **una** llamada —el `getAmountsOut` con el
    `path[]` completo—, que es exactamente la llamada que el swap repetirá: los
    dos importes no son la composición de dos lecturas por separado.
    """
    ruta = _escenario_ruta()
    z = _zorro()
    usdc = _stable("USDC", "base")
    cotizaciones = await _motor(ruta.lector).quote(_par(z, usdc), _entrada(ruta.entrada, z))
    assert len(cotizaciones) == 1
    cotizacion = cotizaciones[0]

    assert cotizacion.route is not None
    assert [token.symbol for token in cotizacion.route.tokens] == ["ZZZ", "WETH", "USDC"]
    assert cotizacion.amount_out.raw == ruta.salida
    assert cotizacion.venue.venue_id == "uniswap-v2@30+30"
    assert cotizacion.fee_bps == BasisPoints(60)

    # El impacto es la composición de los dos tramos, medida contra el marginal
    # de cada pool: se recompone aquí con las mismas reservas declaradas.
    primera = impact_bps_from_marginal(
        amount_in_raw=ruta.entrada,
        amount_out_raw=ruta.mitad,
        marginal_out_per_in=EXACT.divide(Decimal(ruta.reserva_w), Decimal(ruta.reserva_z)),
        fee=BasisPoints(30),
    )
    segunda = impact_bps_from_marginal(
        amount_in_raw=ruta.mitad,
        amount_out_raw=ruta.salida,
        marginal_out_per_in=EXACT.divide(
            Decimal(ruta.reserva_usdc_pool), Decimal(ruta.reserva_w_pool)
        ),
        fee=BasisPoints(30),
    )
    assert primera is not None
    assert segunda is not None
    assert cotizacion.price_impact_bps == combine_impact_bps([primera, segunda])
    assert "dos saltos" in cotizacion.source_note


async def test_la_fabrica_se_pregunta_primero_por_el_par_directo() -> None:
    """El orden de la lectura es la regla: primero el pool directo, y sólo después hubs.

    Se comprueba sobre las llamadas de verdad: la primera pregunta a la fábrica
    es por el par que se está cotizando, no por una ruta «por si acaso».
    """
    ruta = _escenario_ruta()
    z = _zorro()
    usdc = _stable("USDC", "base")
    lector = ruta.lector
    await _motor(lector).quote(_par(z, usdc), _entrada(ruta.entrada, z))
    fabrica = DEPLOYMENTS["base"].factory
    preguntas = lector.calls_to(fabrica)
    assert preguntas[0] == abi.get_pair(_dir(z), _dir(usdc))


async def test_un_plan_de_ruta_ejecuta_el_camino_publicado() -> None:
    """El payload de una ruta lleva **su** camino, no una búsqueda nueva.

    El `path[]` sale de la ruta que la cotización lleva dentro —ZZZ → WETH →
    USDC—, y la cotización fresca se pide con ese mismo camino: si el motor
    volviera a buscar «el mejor de ahora», podría ejecutar un swap distinto del
    que el usuario leyó.
    """
    ruta = _escenario_ruta()
    z = _zorro()
    usdc = _stable("USDC", "base")
    motor = _motor(ruta.lector)
    cotizacion = (await motor.quote(_par(z, usdc), _entrada(ruta.entrada, z)))[0]
    plan = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    esperado_fresco = TokenAmount(ruta.salida, 6, "USDC")
    minimo = esperado_fresco.scaled_by(
        EXACT.subtract(Decimal(1), BasisPoints.from_percent(Decimal("0.5")).as_ratio()),
        rounding=ROUND_DOWN,
    )
    assert plan.calldata == abi.swap_exact_tokens_for_tokens(
        amount_in_raw=ruta.entrada,
        amount_out_min_raw=minimo.raw,
        path=(_dir(z), _dir(_weth()), _dir(usdc)),
        recipient=DESTINATARIO,
        deadline=DEADLINE,
    )
    assert plan.to_address == DEPLOYMENTS["base"].router
    assert plan.approval == TokenApproval(spender=DEPLOYMENTS["base"].router)
    assert "dos tramos" in plan.description


# --------------------------------------------------------------------------- #
# 5. Construir: la cotización fresca, el mínimo y el nativo
# --------------------------------------------------------------------------- #
async def test_el_plan_vuelve_a_cotizar_y_usa_lo_fresco_para_el_minimo() -> None:
    """El `amountOutMinimum` sale de la cotización de **ahora**, no de la pantalla.

    Con una salida fresca algo peor que la mostrada —dentro de la tolerancia—, el
    mínimo se calcula sobre la fresca: es la cifra que de verdad se va a recibir.
    """
    lector = _escenario_base()
    base = _weth()
    par = _par(base, _stable("USDC", "base"))
    motor = _motor(lector)
    cotizacion = (await motor.quote(par, _entrada(ENTRADA_BASE, base)))[0]

    fresca = 444_000_000
    lector.salidas[abi.get_amounts_out(ENTRADA_BASE, (_dir(base), _dir(par.quote)))] = (
        ENTRADA_BASE,
        fresca,
    )
    plan = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    esperado = TokenAmount(fresca, 6, "USDC").scaled_by(
        EXACT.subtract(Decimal(1), BasisPoints.from_percent(Decimal("0.5")).as_ratio()),
        rounding=ROUND_DOWN,
    )
    # El segundo argumento del calldata es el mínimo; se lee como palabra y no se
    # reconstruye con el mismo codificador que se está probando.
    cuerpo = plan.calldata[10:]
    assert cuerpo[64:128] == _palabra_uint(esperado.raw)
    # Y el preview del dominio —la cifra que se enseña— es la fresca, no la vieja.
    assert str(esperado) in plan.description


async def test_la_deriva_mas_alla_de_lo_tolerado_no_construye() -> None:
    """Entre pintar la tabla y pulsar el botón el precio se mueve: se corta."""
    lector = _escenario_base()
    base = _weth()
    par = _par(base, _stable("USDC", "base"))
    motor = _motor(lector)
    cotizacion = (await motor.quote(par, _entrada(ENTRADA_BASE, base)))[0]

    # 442.721.970 → 430.000.000 es una caída del 2,87 %, muy por encima del 1 %.
    lector.salidas[abi.get_amounts_out(ENTRADA_BASE, (_dir(base), _dir(par.quote)))] = (
        ENTRADA_BASE,
        430_000_000,
    )
    with pytest.raises(QuoteMovedError) as excinfo:
        await motor.plan_swap(cotizacion, recipient=DESTINATARIO)
    assert excinfo.value.tolerance_bps == v2.MAX_QUOTE_DRIFT_BPS.value


async def test_sin_cotizacion_fresca_no_se_construye() -> None:
    """Si el pool ya no da salida, no hay payload viejo que firmar."""
    lector = _escenario_base()
    base = _weth()
    par = _par(base, _stable("USDC", "base"))
    motor = _motor(lector)
    cotizacion = (await motor.quote(par, _entrada(ENTRADA_BASE, base)))[0]

    clave = abi.get_amounts_out(ENTRADA_BASE, (_dir(base), _dir(par.quote)))
    del lector.salidas[clave]
    lector.handlers[clave] = SourceResponseError("execution reverted: INSUFFICIENT_LIQUIDITY")
    with pytest.raises(NoQuotesError, match="ya no cotiza"):
        await motor.plan_swap(cotizacion, recipient=DESTINATARIO)


async def test_comprar_el_nativo_manda_el_value_y_no_autoriza_nada() -> None:
    """Pagar con el nativo usa `swapExactETHForTokens`: sin `amountIn` y sin permiso.

    El importe va en el `value` de la transacción y el router envuelve él mismo,
    así que no hay nada que autorizar. El camino empieza por el envuelto —el
    contrato lo exige— y la dirección cero no aparece en ningún sitio.
    """
    lector = _escenario_base()
    nativo = _nativo()
    par = _par(nativo, _stable("USDC", "base"))
    motor = _motor(lector)
    cotizacion = (await motor.quote(par, _entrada(ENTRADA_BASE, nativo)))[0]
    plan = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    assert plan.calldata.startswith(abi.SELECTOR_SWAP_EXACT_ETH_FOR_TOKENS)
    assert plan.approval is None
    assert plan.value == TokenAmount(ENTRADA_BASE, 18, "ETH")
    # La cabeza es (outMin, 0x80, to, deadline): no hay palabra de `amountIn`.
    cuerpo = plan.calldata[10:]
    assert cuerpo[64:128] == _palabra_uint(0x80)
    assert cuerpo[128:192] == _palabra_addr(DESTINATARIO)
    assert cuerpo[192:256] == _palabra_uint(DEADLINE)
    assert "envuelve" in plan.description


async def test_vender_por_el_nativo_usa_swap_exact_tokens_for_eth() -> None:
    """Cobrar en el nativo: el router retira el envuelto dentro de la misma función.

    No hay `unwrapWETH9` ni `multicall` que valga —eso es V3—: aquí el camino
    termina en el envuelto y `swapExactTokensForETH` manda el nativo al
    destinatario. Y el token de entrada sí se autoriza, directo contra el router.
    """
    usdc = _stable("USDC", "base")
    weth = _weth()
    entrada = 500_000_000
    salida = 200_000_000_000_000_000
    lector = _Escenario(
        pools={
            POOL_BASE: _Pool(
                address=POOL_BASE,
                token0=_dir(weth),
                reserve0=RESERVA_WETH_BASE,
                reserve1=RESERVA_USDC_BASE,
            )
        },
        pares={abi.get_pair(_dir(usdc), _dir(weth)): POOL_BASE},
        salidas={abi.get_amounts_out(entrada, (_dir(usdc), _dir(weth))): (entrada, salida)},
    )
    par = _par(usdc, _nativo())
    motor = _motor(lector)
    cotizacion = (await motor.quote(par, _entrada(entrada, usdc)))[0]
    plan = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    assert plan.calldata.startswith(abi.SELECTOR_SWAP_EXACT_TOKENS_FOR_ETH)
    assert plan.value.raw == 0
    assert plan.approval == TokenApproval(spender=DEPLOYMENTS["base"].router)
    assert "retira" in plan.description


async def test_un_nativo_declarado_con_otros_decimales_no_se_firma() -> None:
    """Un `value` con la escala equivocada manda otra cantidad, sin fallar al construir."""
    lector = _escenario_base()
    mal = Token(chain="base", address=None, symbol="ETH", decimals=6)
    cotizacion = _cotizacion(
        mal, _stable("USDC", "base"), amount_in_raw=ENTRADA_BASE, salida=SALIDA_BASE
    )
    with pytest.raises(InvalidAmountError, match="decimales"):
        await _motor(lector).plan_swap(cotizacion, recipient=DESTINATARIO)


async def test_una_cotizacion_de_otro_venue_no_se_construye() -> None:
    """Construir un venue ajeno contra este router ejecutaría otro swap.

    Cada red tiene aquí su DEX medido y el `venue_id` lo dice; una cotización de
    otro sitio —otro DEX de la misma red, u otro motor— no puede producir un
    payload de éste.
    """
    ajeno = Venue(
        venue_id="sushiswap-v2@30", name="Sushiswap V2 0.3 %", kind=VenueKind.DEX, chain="base"
    )
    cotizacion = _cotizacion(
        _weth(),
        _stable("USDC", "base"),
        amount_in_raw=ENTRADA_BASE,
        salida=SALIDA_BASE,
        venue=ajeno,
    )
    with pytest.raises(UnsupportedOperationError, match="no es el venue"):
        await _motor(_escenario_base()).plan_swap(cotizacion, recipient=DESTINATARIO)


async def test_una_ruta_que_no_corresponde_al_par_no_se_construye() -> None:
    """Una ruta cuyos extremos no son los del par ejecutaría un swap que nadie pidió."""
    usdc = _stable("USDC", "base")
    weth = _weth()
    usdt = _stable("USDT", "base")
    ruta = SwapRoute(
        hops=(
            RouteHop(base=weth, quote=usdc, fee_bps=BasisPoints(30)),
            RouteHop(base=usdc, quote=usdt, fee_bps=BasisPoints(30)),
        )
    )
    cotizacion = _cotizacion(
        _zorro(), usdc, amount_in_raw=10**20, salida=100_000_000, ruta=ruta
    )
    with pytest.raises(UnsupportedOperationError, match="no corresponde al par"):
        await _motor(_escenario_ruta().lector).plan_swap(cotizacion, recipient=DESTINATARIO)


async def test_una_red_sin_dex_medido_no_construye() -> None:
    """Las redes que no se midieron no están: una dirección de memoria no se usa."""
    token = Token(chain="unichain", address="0x" + "ab" * 20, symbol="X", decimals=18)
    quote = Token(chain="unichain", address="0x" + "cd" * 20, symbol="Y", decimals=18)
    cotizacion = Quote(
        venue=Venue(venue_id="x@30", name="X 0.3 %", kind=VenueKind.DEX, chain="unichain"),
        engine_id="uniswap_v2",
        pair=_par(token, quote),
        amount_in=_entrada(10**18, token),
        amount_out=TokenAmount(10**6, 18, "Y"),
        fee_bps=BasisPoints(30),
        fee_basis=Measurement.DERIVED,
        price_impact_bps=BasisPoints(5),
        impact_basis=Measurement.DERIVED,
        observed_at=AHORA,
    )
    with pytest.raises(UnsupportedOperationError, match="no construye swaps"):
        await _motor(_Lector()).plan_swap(cotizacion, recipient=DESTINATARIO)


# --------------------------------------------------------------------------- #
# 6. La tabla medida y los venues
# --------------------------------------------------------------------------- #
def test_la_tabla_solo_acepta_comisiones_medidas() -> None:
    """Una entrada con una comisión sin medir no se construye, y lo dice.

    La coherencia entre `addresses` y `amm_protocols` se comprueba al crear cada
    entrada: publicar una comisión que el router no cobra envenena el impacto y
    la tabla de precios sin que nada chirríe.
    """
    valida = ("0x" + "a1" * 20, "0x" + "b2" * 20)
    with pytest.raises(ValueError, match="las dos no pueden ser"):
        Deployment(protocol="uniswap_v2", factory=valida[0], router=valida[1], fee_bps=25)
    with pytest.raises(ValueError, match="no tiene una comisión constante"):
        Deployment(protocol="traderjoe_v2", factory=valida[0], router=valida[1], fee_bps=30)
    with pytest.raises(ValueError, match="40 dígitos"):
        Deployment(protocol="uniswap_v2", factory="0x123", router=valida[1], fee_bps=30)


def test_cada_despliegue_medido_es_coherente() -> None:
    """Toda la tabla cuadra: comisión medida, direcciones hexadecimales, redes."""
    for chain_key, deployment in DEPLOYMENTS.items():
        ref = parse_dex_id(deployment.protocol)
        constante = CONSTANT_FEES[(ref.family, ref.version)]
        assert constante.value == deployment.fee_bps, chain_key
        for address in (deployment.factory, deployment.router):
            assert len(address) == 42, chain_key
            bytes.fromhex(address[2:])
    assert frozenset(DEPLOYMENTS) == SWAP_CHAINS
    assert set(DEPLOYMENTS) == {"ethereum", "optimism", "arbitrum", "polygon", "base", "bsc"}


async def test_los_venues_son_los_que_las_cotizaciones_llevan() -> None:
    """La lista de venues y las cotizaciones tienen que hablar del mismo sitio."""
    motor = _motor(_Lector())
    venues = await motor.venues("base")
    assert len(venues) == 1
    assert venues[0].venue_id == "uniswap-v2@30"
    assert await motor.venues("unichain") == ()
    protocolo = parse_dex_id(DEPLOYMENTS["bsc"].protocol)
    assert protocolo.label == "Pancakeswap V2"


def test_el_destino_declarado_es_el_router_medido() -> None:
    """`expected_destination` sale de la misma tabla que los payloads, no de otra."""
    motor = _motor(_Lector())
    for chain_key, deployment in DEPLOYMENTS.items():
        assert motor.expected_destination(chain_key) == deployment.router
    assert motor.expected_destination("unichain") is None


async def test_un_par_contra_su_propio_envoltorio_no_cotiza() -> None:
    """Nativo contra su envoltorio se resuelve al mismo contrato: no es un par."""
    motor = _motor(_Lector())
    par = _par(_nativo(), _weth())
    cotizaciones = await motor.quote(par, _entrada(10**18, par.base))
    assert cotizaciones == ()
