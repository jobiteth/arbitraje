"""Motor de 0x: el interruptor del build, el fee, el impacto que no publica y el nativo.

Cada bloque cubre algo que se midió contra la API real y que si se rompe no lo
detecta nadie más:

- **El build apagado.** 0x cobra un 0,15 % de lo que se recibe, y el motor nace
  sin construir para que nadie pague esa comisión sin saberlo. Lo que se prueba
  no es que `plan_swap` se niegue —eso es lo fácil— sino que **el manifiesto
  deje de declarar las redes**, que es lo que impide que la interfaz pinte un
  botón que falle.
- **El impacto ausente.** La v2 de 0x no publica impacto de precio, así que la
  cotización sale con `None`. Estos tests fijan que `None` no se convierta en un
  cero por el camino, que es la forma en que este dato se estropea.
- **El `value` en decimal.** Medido: 0x manda `"0"` donde Uniswap manda `"0x00"`.
  Un lector que sólo entendiera una de las dos formas habría abortado cada swap
  o, peor, habría leído un valor distinto de cero como cero.
- **El centinela del nativo.** El nativo no tiene dirección, y la API lo nombra
  con `0xEeee…` —no con la dirección cero, que es la de Uniswap—. Se afirma la
  dirección que viaja en la petición porque confundirla es no tener ruta.

Va contra el `JsonSource` real con `respx` por debajo, así que se ejercitan la
lista blanca de hosts, la caché por URL y parámetros, y el mapeo de errores.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from amigocompora.domain.chains import chain
from amigocompora.domain.errors import (
    InvalidAmountError,
    NoQuotesError,
    QuoteMovedError,
    SourceResponseError,
    UnsupportedOperationError,
)
from amigocompora.domain.models import (
    Measurement,
    Quote,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.engines.catalog import native_token, quote_token, wrapped_native
from amigocompora.engines.zeroex.engine import (
    CONFIG_API_KEY,
    CONFIG_ENABLE_BUILD,
    MANIFEST,
    PROVIDER,
    QUOTE_TAKER,
    QUOTE_URL,
    ROUTERS,
    SLIPPAGE_BPS,
    SWAP_CHAINS,
    ZeroExEngine,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

#: Medidos el 2026-10-06 en Ethereum: 1 WETH salía a 2 692,605025 USDC y la
#: comisión de 0x eran 4,044987 USDC, que sobre lo recibido son 15,02 bps.
_SELL_RAW = "1000000000000000000"
_BUY_RAW = "2692605025"
_FEE_RAW = "4044987"

_TAKER = "0x" + "a" * 40

#: El impacto que publicaría cualquier otro motor. Es el valor por omisión al
#: fabricar una cotización ya observada, y sirve para lo contrario de lo que
#: parece: comprobar que la cifra que se mostró es la que se compara al
#: construir, venga de quien venga — porque 0x no la publica y aun así el
#: payload tiene que salir contra el precio que el usuario vio.
_SAMPLE_IMPACT = BasisPoints(1)


@pytest.fixture(autouse=True)
def _sin_red() -> Iterator[None]:
    """Ninguna prueba de este módulo sale a internet, y ninguna puede olvidarse.

    `respx.get(...).mock(...)` registra una ruta pero no activa nada: sin el
    router en marcha la petición sigue hasta el host de verdad. Con
    `assert_all_mocked` por omisión, además, una petición sin ruta simulada
    falla en vez de salir.
    """
    with respx.mock:
        yield


# --------------------------------------------------------------------------- #
# El dominio: un impacto que no se sabe
# --------------------------------------------------------------------------- #
def test_una_cotizacion_puede_no_traer_impacto() -> None:
    """`None` es «la fuente no lo dice», y tiene que poder decirse.

    Es lo que permite integrar la v2 de 0x, que eliminó el campo. Antes de esto
    la única salida habría sido inventar un número.
    """
    quote = _quote(impact=None, impact_basis=None)
    assert quote.price_impact_bps is None
    assert quote.impact_basis is None
    assert not quote.impact_is_known


def test_el_impacto_y_su_procedencia_van_juntos() -> None:
    """Uno sin el otro describe un estado que no quiere decir nada."""
    with pytest.raises(InvalidAmountError, match="no son coherentes"):
        _quote(impact=None, impact_basis=Measurement.REPORTED)
    with pytest.raises(InvalidAmountError, match="no son coherentes"):
        _quote(impact=BasisPoints(5), impact_basis=None)


def test_un_impacto_desconocido_no_hace_inexacta_la_cotizacion() -> None:
    """`amount_out` sigue medido: falta un desglose, no la cifra que se compara.

    Marcarla como aproximada haría que la interfaz dudara de un número que está
    al dígito, por el mismo motivo que con la comisión desconocida.
    """
    assert _quote(impact=None, impact_basis=None).is_exact


def test_un_impacto_estimado_si_hace_inexacta_la_cotizacion() -> None:
    """La regla contraria, para que la anterior no sea una puerta abierta."""
    assert not _quote(impact=BasisPoints(5), impact_basis=Measurement.ESTIMATED).is_exact


# --------------------------------------------------------------------------- #
# El build apagado
# --------------------------------------------------------------------------- #
def test_el_manifiesto_estatico_no_declara_ninguna_red() -> None:
    """Apagado tiene que ser un hecho del manifiesto, no una condición escondida.

    Es lo que hace que el registro no lo ofrezca como constructor y que la
    interfaz no pinte el botón: sin `swap_chains`, `planners_for` no lo devuelve
    para ninguna red, aunque el motor sepa construir.
    """
    assert MANIFEST.swap_chains == frozenset()
    assert Capability.PREPARE_TX not in MANIFEST.capabilities


async def test_apagado_no_construye_y_dice_por_que() -> None:
    """El mensaje tiene que hablar del precio, no de una avería."""
    async with _engine() as engine:
        with pytest.raises(UnsupportedOperationError, match="comisión de volumen"):
            await engine.plan_swap(_quote(), recipient=_TAKER)


async def test_encendido_publica_las_redes_y_la_capacidad() -> None:
    """Encendido, el manifiesto cambia: es la otra mitad del interruptor."""
    async with _engine(build=True) as engine:
        assert engine.manifest.swap_chains == SWAP_CHAINS
        assert Capability.PREPARE_TX in engine.manifest.capabilities
        assert engine.build_enabled


async def test_apagado_sigue_cotizando() -> None:
    """Cotizar es gratis y es para lo que se enciende el motor.

    El manifiesto recorta `swap_chains` al apagar el build, así que los venues
    **no** pueden mirar ahí: si miraran, apagar el build dejaría la tabla de
    precios vacía, que es justo lo contrario de lo que se quiere.
    """
    _mock_quote()
    async with _engine() as engine:
        (venue,) = await engine.venues("ethereum")
        assert venue.venue_id == "zeroex@ethereum"
        (quote,) = await engine.quote(_pair(), _amount())
        assert quote.amount_out == TokenAmount(int(_BUY_RAW), 6, "USDC")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("sí", True),
        ("true ", True),
        ("", False),
        ("false", False),
        # Una errata no puede encender algo que cobra: se lee como «no».
        ("quizá", False),
        ("verdadero", False),
    ],
)
def test_el_interruptor_solo_se_enciende_con_algo_reconocible(raw: str, expected: bool) -> None:
    engine = PROVIDER.create({CONFIG_API_KEY: "k", CONFIG_ENABLE_BUILD: raw})
    assert engine.build_enabled is expected


def test_sin_interruptor_el_build_esta_apagado() -> None:
    """Ausente es apagado: no puede depender de que alguien lo escriba."""
    assert PROVIDER.create({CONFIG_API_KEY: "k"}).build_enabled is False


def test_no_se_construye_sin_clave() -> None:
    with pytest.raises(SourceResponseError, match="clave de API"):
        ZeroExEngine(api_key="")


# --------------------------------------------------------------------------- #
# Cobertura
# --------------------------------------------------------------------------- #
def test_cubre_todas_las_redes_de_su_tabla_de_destinos() -> None:
    assert frozenset(ROUTERS) == SWAP_CHAINS
    assert MANIFEST.required_config == (CONFIG_API_KEY,)
    assert MANIFEST.allowed_hosts == ("api.0x.org",)


def test_cubre_robinhood_que_uniswap_no_tiene() -> None:
    """El motivo por el que este motor aporta algo aunque cobre.

    Uniswap no llega a Robinhood Chain, así que ahí 0x es la única vía de EVM
    con payload. Se afirma sobre las dos tablas para que quitar la red sea una
    decisión consciente y no un descuido al reordenar.
    """
    assert "robinhood" in ROUTERS
    assert "robinhood" not in _uniswap_chains()
    assert wrapped_native("robinhood") is not None


def test_arc_no_tiene_envoltorio_nativo_y_queda_fuera() -> None:
    """Arc cobra el gas en USDC: no hay par nativo que cotizar, y no se inventa."""
    assert "arc" not in ROUTERS
    assert wrapped_native("arc") is None


async def test_una_red_sin_tabla_no_da_ni_venue_ni_cotizacion() -> None:
    async with _engine() as engine:
        assert await engine.quote(_pair(chain_key="arc"), _amount("arc")) == ()
        assert await engine.venues("arc") == ()
        assert await engine.venues("solana") == ()


# --------------------------------------------------------------------------- #
# La cotización
# --------------------------------------------------------------------------- #
async def test_lee_la_comision_que_cobra_0x() -> None:
    """El fee es un dato real y se publica: 4 044 987 sobre 2 692 605 025.

    Se marca como publicado porque lo está: la API lo desglosa en `fees`. Es la
    cifra que justifica que el build venga apagado, así que tiene que llegar.
    """
    _mock_quote()
    async with _engine() as engine:
        (quote,) = await engine.quote(_pair(), _amount())

    assert quote.fee_bps == BasisPoints(15)
    assert quote.fee_basis is Measurement.REPORTED
    assert quote.fee_is_known


async def test_suma_la_comision_del_integrador() -> None:
    """Las dos comisiones las paga el usuario, así que se cuentan las dos."""
    _mock_quote(integrator={"amount": "1000000", "token": str(_pair().quote.address)})
    async with _engine() as engine:
        (quote,) = await engine.quote(_pair(), _amount())

    assert quote.fee_bps is not None
    assert quote.fee_bps.value > 15


async def test_una_comision_en_otro_token_no_se_suma() -> None:
    """La proporción se mide contra lo recibido; mezclar unidades no significa nada."""
    _mock_quote(fee_token=str(_pair().base.address))
    async with _engine() as engine:
        (quote,) = await engine.quote(_pair(), _amount())

    assert quote.fee_bps is None
    assert quote.fee_basis is None


async def test_la_cotizacion_no_trae_impacto_y_no_se_inventa() -> None:
    """El punto entero de la integración: la v2 no lo publica.

    Si alguien «arreglara» esto rellenando un cero, la interfaz diría que la
    orden no mueve el precio — que es lo contrario de lo que pasa en las
    órdenes grandes, y justo donde el dato importa.
    """
    _mock_quote()
    async with _engine() as engine:
        (quote,) = await engine.quote(_pair(), _amount())

    assert quote.price_impact_bps is None
    assert quote.impact_basis is None
    assert "no publica el impacto" in quote.source_note


async def test_sin_liquidez_no_hay_cotizacion() -> None:
    """Medido: la API contesta 200 con `liquidityAvailable: false`."""
    _mock_quote(available=False)
    async with _engine() as engine:
        assert await engine.quote(_pair(), _amount()) == ()


async def test_una_orden_que_no_es_de_entrada_exacta_no_se_usa() -> None:
    _mock_quote(mode="exact-out")
    async with _engine() as engine:
        assert await engine.quote(_pair(), _amount()) == ()


async def test_una_cotizacion_de_otro_tamano_no_se_usa() -> None:
    _mock_quote(sell="2000000000000000000")
    async with _engine() as engine:
        assert await engine.quote(_pair(), _amount()) == ()


async def test_un_importe_ilegible_no_se_convierte_en_cotizacion() -> None:
    _mock_quote(buy="no es un número")
    async with _engine() as engine:
        assert await engine.quote(_pair(), _amount()) == ()


# --------------------------------------------------------------------------- #
# Construir
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("chain_key", sorted(ROUTERS))
async def test_construye_para_cada_red_medida(chain_key: str) -> None:
    """Una red, una dirección de destino: se prueba la tabla entera, no una."""
    _mock_quote(chain_key=chain_key)
    async with _engine(build=True) as engine:
        tx = await engine.plan_swap(_quote(chain_key=chain_key), recipient=_TAKER)

    assert tx.to_address == ROUTERS[chain_key]
    assert tx.chain_id == chain(chain_key).require_eip155_id()
    assert tx.calldata == "0xdeadbeef"


@pytest.mark.parametrize("value", ["0", "0x0", 0, "0x00"])
async def test_lee_el_valor_en_decimal_y_en_hexadecimal(value: Any) -> None:
    """Medido: 0x manda `"0"` donde Uniswap manda `"0x00"`.

    Las dos formas tienen que leerse igual. Un lector de sólo hexadecimal
    habría tratado ese `"0"` como ilegible y abortado **cada** swap de esta
    fuente, así que la prueba va sobre la forma exacta que se midió.
    """
    _mock_quote(value=value)
    async with _engine(build=True) as engine:
        tx = await engine.plan_swap(_quote(), recipient=_TAKER)
    assert tx.value == TokenAmount(0, 18, "ETH")


async def test_lee_un_valor_nativo_distinto_de_cero() -> None:
    """Un ERC-20 contra ERC-20 lo trae a cero; el nativo no."""
    _mock_quote(value="1000000000000000000")
    async with _engine(build=True) as engine:
        tx = await engine.plan_swap(_quote(), recipient=_TAKER)
    assert tx.value == TokenAmount(10**18, 18, "ETH")


@pytest.mark.parametrize("value", [None, "", "1.5", "-1", "no", True])
async def test_un_valor_ilegible_aborta(value: Any) -> None:
    """`"1.5"` son unidades mínimas de token, y media unidad no existe."""
    _mock_quote(value=value)
    async with _engine(build=True) as engine:
        with pytest.raises(SourceResponseError, match="`value` legible"):
            await engine.plan_swap(_quote(), recipient=_TAKER)


async def test_rechaza_un_destino_distinto_del_medido() -> None:
    """Comprueba identidad, no forma: el impostor está bien formado y aun así para."""
    impostor = "0x" + "b" * 40
    _mock_quote(to=impostor)
    async with _engine(build=True) as engine:
        with pytest.raises(SourceResponseError) as caught:
            await engine.plan_swap(_quote(), recipient=_TAKER)

    assert impostor in str(caught.value)
    assert ROUTERS["ethereum"] in str(caught.value)


async def test_rechaza_un_calldata_que_no_es_hexadecimal() -> None:
    _mock_quote(data="no soy hex")
    async with _engine(build=True) as engine:
        with pytest.raises(SourceResponseError, match="hexadecimal"):
            await engine.plan_swap(_quote(), recipient=_TAKER)


async def test_sin_transaction_no_se_construye() -> None:
    _mock_quote(with_transaction=False)
    async with _engine(build=True) as engine:
        with pytest.raises(SourceResponseError, match="no devolvió `transaction`"):
            await engine.plan_swap(_quote(), recipient=_TAKER)


async def test_aborta_si_el_precio_se_movio_antes_de_mirar_nada_mas() -> None:
    """10 % de deriva: se para. Y se para **antes** de validar el destino.

    La cotización simulada trae además un destino falso, así que si la
    comprobación de precio corriera después, el error sería el del destino. Que
    salga `QuoteMovedError` demuestra el orden, que es lo que importa: no tiene
    sentido validar un payload que ya se sabe que no se va a entregar.
    """
    drifts = str(int(_BUY_RAW) * 9 // 10)
    _mock_quote(buy=drifts, to="0x" + "b" * 40)
    async with _engine(build=True) as engine:
        with pytest.raises(QuoteMovedError) as caught:
            await engine.plan_swap(_quote(), recipient=_TAKER)
    assert "1000 bps" in str(caught.value)


async def test_tolera_la_deriva_dentro_del_margen() -> None:
    """Exigir identidad convertiría el botón en uno que casi nunca funciona."""
    fresh = int(_BUY_RAW) * 997 // 1000
    _mock_quote(buy=str(fresh))
    async with _engine(build=True) as engine:
        tx = await engine.plan_swap(_quote(), recipient=_TAKER)
    assert str(TokenAmount(fresh, 6, "USDC")) in tx.description


async def test_sin_ruta_no_sigue_con_la_cotizacion_vieja() -> None:
    _mock_quote(available=False)
    async with _engine(build=True) as engine:
        with pytest.raises(NoQuotesError, match="ya no tiene ruta"):
            await engine.plan_swap(_quote(), recipient=_TAKER)


async def test_el_deslizamiento_fijado_viaja_en_la_peticion_y_en_el_texto() -> None:
    """La tolerancia se le pide a la API en la re-cotización del build.

    Va en los parámetros —y por tanto en la clave de caché—, así que dos
    tolerancias no comparten cotización; el texto la dice con la cifra efectiva.
    """
    route = _mock_quote()
    async with _engine(build=True) as engine:
        tx = await engine.plan_swap(_quote(), recipient=_TAKER, slippage_bps=250)

    assert dict(route.calls[-1].request.url.params)["slippageBps"] == "250"
    assert "2.5 %" in tx.description


async def test_sin_deslizamiento_fijado_se_pide_el_del_motor() -> None:
    """El parámetro es opcional: sin él, la petición queda exactamente igual."""
    route = _mock_quote()
    async with _engine(build=True) as engine:
        await engine.plan_swap(_quote(), recipient=_TAKER)

    assert dict(route.calls[-1].request.url.params)["slippageBps"] == str(SLIPPAGE_BPS)


# --------------------------------------------------------------------------- #
# La petición que se envía
# --------------------------------------------------------------------------- #
async def test_la_peticion_lleva_la_clave_la_version_y_el_taker_real() -> None:
    """`0x-version: v2` es obligatoria: sin ella la API usa el contrato de la v1."""
    route = _mock_quote()
    async with _engine(build=True) as engine:
        await engine.plan_swap(_quote(), recipient=_TAKER)

    request = route.calls[-1].request
    assert request.headers["0x-api-key"] == "clave-de-prueba"
    assert request.headers["0x-version"] == "v2"
    params = dict(request.url.params)
    assert params["taker"] == _TAKER
    assert params["sellToken"] == _pair().base.address
    assert params["buyToken"] == _pair().quote.address
    assert params["sellAmount"] == _SELL_RAW
    assert params["chainId"] == "1"


async def test_cotizar_usa_un_centinela_distinto_del_taker_del_build() -> None:
    """El `taker` entra en el calldata, así que las dos cotizaciones no se pisan."""
    route = _mock_quote()
    async with _engine(build=True) as engine:
        await engine.quote(_pair(), _amount())
        await engine.plan_swap(_quote(), recipient=_TAKER)

    assert route.call_count == 2
    assert dict(route.calls[0].request.url.params)["taker"] == QUOTE_TAKER
    assert dict(route.calls[1].request.url.params)["taker"] == _TAKER


async def test_cotizar_dos_veces_no_gasta_cuota() -> None:
    """La caché de `get_json` incluye los parámetros, así que la clave va sola."""
    route = _mock_quote()
    async with _engine() as engine:
        await engine.quote(_pair(), _amount())
        await engine.quote(_pair(), _amount())
    assert route.call_count == 1


# --------------------------------------------------------------------------- #
# El nativo: su centinela
# --------------------------------------------------------------------------- #
async def test_cotiza_el_nativo_con_su_centinela() -> None:
    """Medido el 2026-10-09: sin centinela la petición ni se hacía.

    La API identifica los tokens por contrato y el nativo no tiene, así que el
    motor se rendía antes de preguntar. Se afirma la dirección concreta que
    viaja en los parámetros —el centinela de 0x, no la dirección cero de
    Uniswap— porque cada API tiene la suya y confundirlas es no tener ruta.
    """
    native = native_token("polygon")
    pair = TradingPair(base=native, quote=_stable("polygon"))
    amount = TokenAmount(10**18, native.decimals, native.symbol)
    route = _mock_quote(sell=str(10**18))

    async with _engine() as engine:
        quotes = await engine.quote(pair, amount)

    assert len(quotes) == 1
    params = dict(route.calls[-1].request.url.params)
    assert params["sellToken"] == "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


async def test_el_build_del_nativo_lee_el_value_decimal() -> None:
    """Vender nativo devuelve el importe en `value`, en decimal como todo aquí."""
    native = native_token("polygon")
    stable = _stable("polygon")
    pair = TradingPair(base=native, quote=stable)
    amount = TokenAmount(10**18, native.decimals, native.symbol)
    quote = Quote(
        venue=Venue(
            venue_id="zeroex@polygon",
            name="0x (agregado, polygon)",
            kind=VenueKind.DEX,
            chain="polygon",
        ),
        engine_id=MANIFEST.engine_id,
        pair=pair,
        amount_in=amount,
        amount_out=TokenAmount(int(_BUY_RAW), stable.decimals, stable.symbol),
        fee_bps=None,
        fee_basis=None,
        price_impact_bps=None,
        impact_basis=None,
        observed_at=NOW,
    )
    _mock_quote(chain_key="polygon", sell=str(10**18), value=str(10**18))

    async with _engine(build=True) as engine:
        transaction = await engine.plan_swap(quote, recipient=_TAKER)

    assert transaction.value.raw == 10**18
    assert transaction.value.symbol == native.symbol
    assert transaction.to_address == ROUTERS["polygon"]


# --------------------------------------------------------------------------- #
# Utilería
# --------------------------------------------------------------------------- #
def _native(chain_key: str) -> Token:
    """El envoltorio nativo de la red, o uno de relleno si no tiene.

    El relleno existe para poder pedir una cotización de una red **sin** tabla
    de destinos —Arc— y comprobar que se rechaza por la red y no por el par.
    """
    native = wrapped_native(chain_key)
    if native is not None:
        return native
    spec = chain(chain_key)
    return Token(f"W{spec.native_symbol}", spec.native_decimals, chain_key, "0x" + "1" * 40)


def _stable(chain_key: str) -> Token:
    stable = quote_token(chain_key)
    if stable is not None:
        return stable
    return Token("USDC", 6, chain_key, "0x" + "2" * 40)


def _pair(chain_key: str = "ethereum") -> TradingPair:
    """El par medido: el envoltorio nativo contra la stablecoin de la red."""
    return TradingPair(base=_native(chain_key), quote=_stable(chain_key))


def _amount(chain_key: str = "ethereum") -> TokenAmount:
    base = _pair(chain_key).base
    return TokenAmount(int(_SELL_RAW), base.decimals, base.symbol)


def _uniswap_chains() -> frozenset[str]:
    """Las redes del motor de Uniswap, leídas de su tabla de destinos.

    Se lee de la tabla y no de un `frozenset` escrito a mano para que la
    afirmación sea sobre lo que el otro motor hace de verdad, no sobre lo que
    aquí se recuerde de él.
    """
    from amigocompora.engines.uniswap.engine import ROUTERS as UNISWAP_ROUTERS

    return frozenset(UNISWAP_ROUTERS)


def _quote(
    *,
    chain_key: str = "ethereum",
    out_raw: int = int(_BUY_RAW),
    impact: BasisPoints | None = _SAMPLE_IMPACT,
    impact_basis: Measurement | None = Measurement.REPORTED,
) -> Quote:
    """Una cotización ya observada, como la que la interfaz tendría en la tabla.

    El impacto por omisión es el de otro motor: aquí sirve para comprobar que la
    cifra que se mostró es la que se compara al construir, sea quien sea el que
    la publicó.
    """
    pair = _pair(chain_key)
    return Quote(
        venue=Venue(
            venue_id=f"zeroex@{chain_key}",
            name=f"0x (agregado, {chain_key})",
            kind=VenueKind.DEX,
            chain=chain_key,
        ),
        engine_id=MANIFEST.engine_id,
        pair=pair,
        amount_in=_amount(chain_key),
        amount_out=TokenAmount(out_raw, pair.quote.decimals, pair.quote.symbol),
        fee_bps=None,
        fee_basis=None,
        price_impact_bps=impact,
        impact_basis=impact_basis,
        observed_at=NOW,
    )


def _mock_quote(
    *,
    chain_key: str = "ethereum",
    buy: str = _BUY_RAW,
    sell: str = _SELL_RAW,
    fee: str = _FEE_RAW,
    fee_token: str | None = None,
    integrator: dict[str, str] | None = None,
    available: bool = True,
    mode: str = "exact-in",
    to: str | None = None,
    data: str = "0xdeadbeef",
    value: Any = "0",
    with_transaction: bool = True,
) -> respx.Route:
    """Simula `GET /swap/permit2/quote` con la forma medida de la respuesta.

    Aquí **no hay endpoint aparte de build**: medido, 0x devuelve el
    `transaction` dentro de la propia cotización. Por eso construir y cotizar
    van contra la misma ruta simulada, y por eso lo único que distingue una
    petición de build de una de cotización es el `taker` que lleva.
    """
    pair = _pair(chain_key)
    body: dict[str, Any] = {
        "buyAmount": buy,
        "sellAmount": sell,
        "buyToken": pair.quote.address,
        "sellToken": pair.base.address,
        "minBuyAmount": buy,
        "mode": mode,
        "liquidityAvailable": available,
        "issues": {"balance": None, "allowance": None, "invalidSourcesPassed": []},
        "fees": {
            "zeroExFee": {
                "amount": fee,
                "token": fee_token or pair.quote.address,
                "type": "volume",
            },
            "integratorFee": integrator,
            "gasFee": None,
        },
        "totalNetworkFee": "216013770161890",
        "route": {
            "fills": [{"source": "Uniswap_V3", "proportionBps": "10000"}],
            "tokens": [{"symbol": pair.base.symbol}, {"symbol": pair.quote.symbol}],
        },
        "zid": "0x2185",
    }
    if with_transaction:
        body["transaction"] = {
            "to": to if to is not None else ROUTERS[chain_key],
            "data": data,
            "value": value,
            "gas": "264203",
            "gasPrice": "1000000000",
        }
    return respx.get(QUOTE_URL).mock(return_value=httpx.Response(200, json=body))


@asynccontextmanager
async def _engine(*, build: bool = False) -> AsyncIterator[ZeroExEngine]:
    """Motor abierto y cerrado aunque el test falle, con el build como se pida."""
    engine = ZeroExEngine(
        api_key="clave-de-prueba",
        enable_swap_build=build,
        min_interval_seconds=0.0,
    )
    await engine.aopen()
    try:
        yield engine
    finally:
        await engine.aclose()
