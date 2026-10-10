"""Motor de Uniswap: impacto, destino del build, deriva de precio y el nativo.

Cada bloque existe porque cubre algo que puede perder dinero o pintar un dato
falso, y ninguno se detecta solo:

- **Las unidades de `priceImpact`.** El campo se llama igual que el de Jupiter
  salvo por `Pct`, y mide cosas distintas: allí es una fracción, aquí un
  porcentaje. Confundirlos multiplica por cien cada cifra de la tabla. El test
  que lo fija compara contra el número crudo, no contra la fórmula.
- **El destino del build.** La API devuelve el contrato contra el que se firma,
  y siete direcciones distintas para ocho redes. Si la tabla se relajara a «una
  dirección cualquiera con forma de dirección», el usuario firmaría contra el
  contrato que le pusieran.
- **La deriva de precio.** Medido: el endpoint de swap construye sobre la
  cotización que se le entregue sin recomprobar nada. Si la comprobación se
  rompe, nada más la echa de menos.
- **La dirección cero del nativo.** El nativo no tiene contrato, y la API lo
  nombra con la dirección cero —con el centinela de 0x responde
  `NoRouteFoundError`—. Se afirma la dirección que viaja en la petición porque
  equivocarla es dejar sin ruta justo a vender la moneda de la red.
- **El permiso del token.** El router de este motor cobra vía Permit2 —medido
  en su bytecode—: el payload tiene que declararlo o el ejecutor aprueba a
  quien nunca cobra, y el swap revierte al estimar el gas.
- **El deslizamiento efectivo.** El configurado manda, en el cuerpo de la
  petición y en el texto del payload; sin él se conserva la constante del
  motor, y la tolerancia entra en la clave de caché para que dos tolerancias
  no compartan construcción.

`plan_swap` va contra el `JsonSource` real con `respx` por debajo, así que se
ejercitan de verdad la lista blanca de hosts, la caché por `cache_key`, el mapeo
de errores por código y el `post_json` sin caché.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from amigocompora.domain.chains import chain
from amigocompora.domain.errors import (
    NoQuotesError,
    QuoteMovedError,
    SourceResponseError,
    UnsupportedOperationError,
)
from amigocompora.domain.models import (
    Measurement,
    Quote,
    Token,
    TokenApproval,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.engines.catalog import native_token, quote_token, wrapped_native
from amigocompora.engines.uniswap.engine import (
    CONFIG_API_KEY,
    MANIFEST,
    PERMIT2,
    PROVIDER,
    QUOTE_SWAPPER,
    QUOTE_URL,
    ROUTERS,
    SLIPPAGE_PCT,
    SWAP_URL,
    UniswapEngine,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

#: Precio medido el 2026-10-06: 1 USDC salía a 0,00037068 WETH. Se usa la cifra
#: de verdad para que los tests hablen del mismo orden de magnitud que la fuente.
_OUT_RAW = "370676399815965"
_IN_RAW = "1000000"

_RECIPIENT = "0x" + "a" * 40


@pytest.fixture(autouse=True)
def _sin_red() -> Iterator[None]:
    """Ninguna prueba de este módulo sale a internet, y ninguna puede olvidarse.

    Hace falta porque `respx.post(...).mock(...)` **registra** una ruta pero no
    activa nada: sin el router en marcha la petición sigue su curso hasta el
    host de verdad. Pasó, y se vio en la primera ejecución —veinticuatro fallos
    con un 401 real de Uniswap delante—, así que en lugar de repetir el
    decorador en cada test se pone aquí, donde vale para todos los que se
    escriban después. Con `assert_all_mocked` por omisión, además, una petición
    sin ruta simulada falla en vez de salir.
    """
    with respx.mock:
        yield


# --------------------------------------------------------------------------- #
# El nativo: la dirección cero
# --------------------------------------------------------------------------- #
async def test_cotiza_el_nativo_con_la_direccion_cero() -> None:
    """Medido el 2026-10-09: sin el centinela la API no busca ruta al nativo.

    Es el caso que dejaba «1 POL → pUSD» sin ninguna cotización —el motor se
    rendía antes de preguntar—, así que se afirma la dirección concreta que
    viaja en la petición, que es el contrato entero de este arreglo, y no una
    constante del módulo: el valor tiene que ser «todo ceros» aunque alguien
    cambie la constante.
    """
    stable = _pair("polygon").base
    native = native_token("polygon")
    pair = TradingPair(base=native, quote=stable)
    amount = TokenAmount(10**18, native.decimals, native.symbol)
    route = _mock_quote(in_raw=str(10**18))

    async with _engine() as engine:
        quotes = await engine.quote(pair, amount)

    assert len(quotes) == 1
    assert quotes[0].amount_out.raw == int(_OUT_RAW)
    body = _body_of(route.calls[-1].request)
    assert body["tokenIn"] == "0x0000000000000000000000000000000000000000"


async def test_recibir_el_nativo_tambien_usa_la_direccion_cero() -> None:
    """La otra dirección del par: comprar nativo se nombra igual."""
    stable = _pair("polygon").base
    native = native_token("polygon")
    pair = TradingPair(base=stable, quote=native)
    amount = TokenAmount(int(_IN_RAW), stable.decimals, stable.symbol)
    route = _mock_quote()

    async with _engine() as engine:
        quotes = await engine.quote(pair, amount)

    assert len(quotes) == 1
    body = _body_of(route.calls[-1].request)
    assert body["tokenOut"] == "0x0000000000000000000000000000000000000000"


async def test_el_build_del_nativo_lleva_el_value() -> None:
    """Al vender nativo el importe viaja como `value` y el router lo envuelve.

    Medido: la transacción que la API devuelve para 1 POL trae
    `value = 0x0de0b6b3a7640000` (10¹⁸) contra el router medido de la red. El
    lector del `value` ya existía; lo que faltaba era llegar hasta aquí.
    """
    native = native_token("polygon")
    _mock_quote(in_raw=str(10**18))
    _mock_swap(to=ROUTERS["polygon"], value="0x0de0b6b3a7640000")

    async with _engine() as engine:
        transaction = await engine.plan_swap(_quote_nativo(), recipient=_RECIPIENT)

    assert transaction.value.raw == 10**18
    assert transaction.value.symbol == native.symbol
    assert transaction.to_address == ROUTERS["polygon"]


async def test_al_vender_nativo_no_hay_permiso_de_token_que_declarar() -> None:
    """El nativo se paga como `value`, así que no hay ERC-20 que aprobar.

    Declarar aquí un permiso pediría un `approve` de un token que la
    transacción no mueve —el primer diálogo hablaría de un permiso que no
    existe—, y la descripción lo dice para que quien lea el payload no lo eche
    de menos.
    """
    _mock_quote(in_raw=str(10**18))
    _mock_swap(to=ROUTERS["polygon"], value="0x0de0b6b3a7640000")

    async with _engine() as engine:
        transaction = await engine.plan_swap(_quote_nativo(), recipient=_RECIPIENT)

    assert transaction.approval is None
    assert "no hay permiso de token que conceder" in transaction.description


# --------------------------------------------------------------------------- #
# Unidades del impacto: el error de cien veces
# --------------------------------------------------------------------------- #
def test_el_impacto_se_lee_como_porcentaje_no_como_fraccion() -> None:
    """`0.01` son **1 bp**, no 100.

    Es el valor que la API publica de verdad para una orden de 1 WETH. Leído
    como fracción —que es lo que hace Jupiter con su `priceImpactPct`— daría
    100 bps, un 1 % de impacto en una orden pequeña. Se afirma el número
    concreto para que el fallo no pueda colarse cambiando la fórmula.

    Se pasa `Decimal` y no `float` porque es lo que el motor recibe de verdad:
    `optional_decimal` reconstruye desde la repr más corta, así que al `0.01`
    del JSON le corresponde este mismo valor.
    """
    assert BasisPoints.from_percent(Decimal("0.01")) == BasisPoints(1)
    # Y lo que saldría de leerlo mal, para que la diferencia quede escrita:
    assert BasisPoints.from_ratio(Decimal("0.01")) == BasisPoints(100)


async def test_una_cotizacion_sin_impacto_no_se_emite() -> None:
    """Medido: la API omite `priceImpact` a partir de unos 10 WETH.

    Justo donde importa. `Quote` exige la cifra, así que la elección es
    descartar o inventar; inventar un cero diría «esta orden no mueve el
    precio», que es lo contrario de lo que está pasando.
    """
    _mock_quote(impact=None)
    async with _engine() as engine:
        assert await engine.quote(_pair(), _amount()) == ()


async def test_un_impacto_desmedido_descarta_la_cotizacion() -> None:
    _mock_quote(impact=25.0)  # 2 500 bps, por encima del techo de 1 000
    async with _engine() as engine:
        assert await engine.quote(_pair(), _amount()) == ()


# --------------------------------------------------------------------------- #
# Traducción de la cotización
# --------------------------------------------------------------------------- #
async def test_traduce_los_importes_que_la_api_manda_como_cadena() -> None:
    """`output.amount` llega como `"370676399815965"`, no como número.

    Y la comisión va en `None`: la API no la desglosa, sólo publica el gas.
    Publicar 0 afirmaría que el swap es gratis.
    """
    _mock_quote()
    async with _engine() as engine:
        (quote,) = await engine.quote(_pair(), _amount())

    assert quote.amount_out == TokenAmount(int(_OUT_RAW), 18, "WETH")
    assert quote.fee_bps is None
    assert quote.fee_basis is None
    assert quote.price_impact_bps == BasisPoints(1)
    assert quote.engine_id == MANIFEST.engine_id
    assert quote.impact_basis is Measurement.REPORTED
    assert quote.venue.chain == "ethereum"
    assert "V3" in quote.source_note


async def test_una_cotizacion_de_otro_tamano_no_se_usa() -> None:
    """En `EXACT_INPUT` el importe de entrada tiene que ser el que se pidió.

    Si no coincide, la cifra es de otra orden: publicarla daría un precio que no
    es el de nadie, y encima pasaría la comprobación de deriva al construir
    contra un tamaño distinto.
    """
    _mock_quote(in_raw="2000000")
    async with _engine() as engine:
        assert await engine.quote(_pair(), _amount()) == ()


async def test_un_importe_ilegible_no_se_convierte_en_cotizacion() -> None:
    """`"1.5"` no son unidades mínimas: media unidad indivisible no existe."""
    _mock_quote(out_raw="1.5")
    async with _engine() as engine:
        assert await engine.quote(_pair(), _amount()) == ()


async def test_una_respuesta_sin_objeto_quote_es_no_haber_ruta() -> None:
    """Sin `quote` y con HTTP 200: la fuente dice que ahí no hay ruta."""
    respx.post(QUOTE_URL).mock(return_value=httpx.Response(200, json={"routing": "CLASSIC"}))
    async with _engine() as engine:
        assert await engine.quote(_pair(), _amount()) == ()


async def test_enumera_un_venue_por_red_cubierta() -> None:
    async with _engine() as engine:
        (venue,) = await engine.venues("base")
        assert venue.chain == "base"
        assert venue.venue_id == "uniswap@base"
        assert await engine.venues("solana") == ()
        assert await engine.venues("arc") == ()


async def test_no_cotiza_fuera_de_su_lista_de_redes() -> None:
    """Arc cobra el gas en USDC y no tiene envoltorio nativo que cotizar."""
    async with _engine() as engine:
        assert await engine.quote(_pair(chain_key="arc"), _amount()) == ()


# --------------------------------------------------------------------------- #
# El destino del build
# --------------------------------------------------------------------------- #
async def test_la_tabla_de_destinos_cubre_todas_las_redes_que_declara() -> None:
    """Una red en `swap_chains` sin destino medido sería un motor que ofrece
    construir y luego se niega: se comprueba la coherencia de las dos tablas."""
    assert set(MANIFEST.swap_chains) == set(ROUTERS)


@pytest.mark.parametrize("chain_key", sorted(ROUTERS))
async def test_construye_para_cada_red_medida(chain_key: str) -> None:
    _mock_quote()
    swap = _mock_swap(to=ROUTERS[chain_key])
    async with _engine() as engine:
        tx = await engine.plan_swap(_quote(chain_key=chain_key), recipient=_RECIPIENT)

    assert tx.to_address == ROUTERS[chain_key]
    assert tx.chain_id == chain(chain_key).require_eip155_id()
    assert tx.calldata == "0xdeadbeef"
    assert swap.called


async def test_rechaza_un_destino_distinto_del_medido() -> None:
    """Lo que impide que la API —o quien esté en medio— desvíe los fondos.

    No es una validación de forma: se comprueba la **identidad** del contrato.
    El destino que se usa aquí está bien formado y aun así tiene que parar la
    construcción. El payload sí se llega a pedir —es una lectura, y hasta
    entonces no hay nada que validar—, pero se descarta sin devolverlo.

    El error nombra las dos direcciones: quien lo lea tiene que poder ver que la
    fuente se desvió, no sólo que algo falló.
    """
    impostor = "0x" + "b" * 40
    _mock_quote()
    _mock_swap(to=impostor)
    async with _engine() as engine:
        with pytest.raises(SourceResponseError) as caught:
            await engine.plan_swap(_quote(), recipient=_RECIPIENT)

    assert impostor in str(caught.value)
    assert ROUTERS["ethereum"] in str(caught.value)


async def test_no_construye_en_una_red_que_no_cubre() -> None:
    """Solana no es de este motor, y lo dice en vez de devolver nada."""
    async with _engine() as engine:
        with pytest.raises(UnsupportedOperationError, match="sólo construye swaps"):
            await engine.plan_swap(_quote(chain_key="solana"), recipient=_RECIPIENT)


async def test_rechaza_un_calldata_que_no_es_hexadecimal() -> None:
    _mock_quote()
    _mock_swap(data="no soy hex")
    async with _engine() as engine:
        with pytest.raises(SourceResponseError, match="hexadecimal"):
            await engine.plan_swap(_quote(), recipient=_RECIPIENT)


async def test_rechaza_un_valor_ilegible() -> None:
    """Sin saber cuánto nativo mueve, la transacción no es revisable."""
    _mock_quote()
    _mock_swap(value=None)
    async with _engine() as engine:
        with pytest.raises(SourceResponseError, match="`value` legible"):
            await engine.plan_swap(_quote(), recipient=_RECIPIENT)


async def test_lee_el_valor_en_hexadecimal() -> None:
    """La API manda el valor como `"0x00"`, no como número."""
    _mock_quote()
    _mock_swap(value="0x0de0b6b3a7640000")  # 1 ETH
    async with _engine() as engine:
        tx = await engine.plan_swap(_quote(), recipient=_RECIPIENT)
    assert tx.value == TokenAmount(10**18, 18, "ETH")


# --------------------------------------------------------------------------- #
# Deriva de precio
# --------------------------------------------------------------------------- #
async def test_aborta_sin_construir_si_el_precio_se_movio() -> None:
    """10 % de deriva sobre lo que el usuario vio: se para y no se construye.

    Lo que se afirma no es sólo que se lance, sino que el endpoint de swap **no
    se llegó a llamar**: construir y luego avisar dejaría en manos de quien
    confirma detectar la discrepancia comparando dos cifras de un diálogo.
    """
    _mock_quote(out_raw=str(int(_OUT_RAW) * 9 // 10))
    swap = _mock_swap()
    async with _engine() as engine:
        with pytest.raises(QuoteMovedError) as caught:
            await engine.plan_swap(_quote(), recipient=_RECIPIENT)

    assert not swap.called, "construyó un payload sobre un precio ya inválido"
    assert "1000 bps" in str(caught.value)


async def test_tolera_la_deriva_dentro_del_margen() -> None:
    """Exigir identidad convertiría el botón en uno que casi nunca funciona."""
    fresh = int(_OUT_RAW) * 997 // 1000  # -30 bps
    _mock_quote(out_raw=str(fresh))
    swap = _mock_swap()
    async with _engine() as engine:
        tx = await engine.plan_swap(_quote(), recipient=_RECIPIENT)

    assert swap.called
    # La descripción habla del precio fresco, no del que se mostró.
    assert str(TokenAmount(fresh, 18, "WETH")) in tx.description


async def test_sin_ruta_no_sigue_con_la_cotizacion_vieja() -> None:
    respx.post(QUOTE_URL).mock(return_value=httpx.Response(200, json={"routing": "CLASSIC"}))
    swap = _mock_swap()
    async with _engine() as engine:
        with pytest.raises(NoQuotesError, match="ya no tiene ruta"):
            await engine.plan_swap(_quote(), recipient=_RECIPIENT)
    assert not swap.called


# --------------------------------------------------------------------------- #
# La petición que se envía
# --------------------------------------------------------------------------- #
async def test_la_peticion_lleva_la_clave_y_el_destinatario_real() -> None:
    """La clave va en `x-api-key`, que es la cabecera que la API acepta.

    Y el `swapper` del build es el usuario, no el centinela con el que se cotiza:
    construir con el centinela daría un payload a nombre de otra dirección.
    """
    quote_route = _mock_quote()
    _mock_swap()
    async with _engine() as engine:
        await engine.plan_swap(_quote(), recipient=_RECIPIENT)

    request = quote_route.calls[-1].request
    assert request.headers["x-api-key"] == "clave-de-prueba"
    body = _body_of(request)
    assert body["swapper"] == _RECIPIENT
    assert body["tokenIn"] == _pair().base.address
    assert body["tokenOut"] == _pair().quote.address
    assert body["amount"] == _IN_RAW
    assert body["type"] == "EXACT_INPUT"
    assert body["routingPreference"] == "BEST_PRICE"


async def test_cotizar_usa_un_centinela_y_no_altera_la_cache_del_build() -> None:
    """El `swapper` entra en la clave de caché, así que las dos no se pisan.

    Si no entrara, el payload de un usuario podría salir con los datos de
    permiso de otro: la cotización los lleva dentro.
    """
    quote_route = _mock_quote()
    _mock_swap()
    async with _engine() as engine:
        await engine.quote(_pair(), _amount())  # caché del centinela
        await engine.plan_swap(_quote(), recipient=_RECIPIENT)  # caché del usuario

    assert quote_route.call_count == 2, "el build se sirvió de la cotización del centinela"
    assert _body_of(quote_route.calls[0].request)["swapper"] == QUOTE_SWAPPER
    assert _body_of(quote_route.calls[1].request)["swapper"] == _RECIPIENT


async def test_cotizar_dos_veces_no_gasta_cuota() -> None:
    """Con el mismo destinatario la segunda va a caché, que es para lo que está."""
    quote_route = _mock_quote()
    async with _engine() as engine:
        await engine.quote(_pair(), _amount())
        await engine.quote(_pair(), _amount())
    assert quote_route.call_count == 1


# --------------------------------------------------------------------------- #
# El permiso del token: este router cobra vía Permit2
# --------------------------------------------------------------------------- #
async def test_el_payload_declara_el_permiso_encadenado_por_permit2() -> None:
    """Medido el 2026-10-09: los ocho routers llevan Permit2 dentro.

    El router no mueve el token con un `transferFrom` propio: se lo pide a
    Permit2. Sin declarar el permiso en el payload, el ejecutor aprueba directo
    al router —que nunca cobra así— y el swap revierte al estimar el gas. Es la
    declaración lo que desencadena los dos permisos encadenados: el ERC-20 a
    Permit2 y Permit2 al router.
    """
    _mock_quote()
    _mock_swap()
    async with _engine() as engine:
        tx = await engine.plan_swap(_quote(), recipient=_RECIPIENT)

    assert tx.approval is not None
    assert tx.approval == TokenApproval(spender=ROUTERS["ethereum"], via=PERMIT2)
    # El `approve` directo es a Permit2, no al router: es el primer eslabón.
    assert tx.approval.target == PERMIT2
    assert "encadenado por Permit2" in tx.description
    assert PERMIT2 in tx.description


# --------------------------------------------------------------------------- #
# El deslizamiento: manda el configurado; sin él, la constante del motor
# --------------------------------------------------------------------------- #
async def test_el_deslizamiento_configurado_manda_en_la_peticion_y_en_el_texto() -> None:
    """La API construye con el `slippageTolerance` que se le entregue.

    La cifra del texto sale del valor **efectivo** —el configurado—, no de la
    constante: un payload que dijera 0,5 % mientras el cuerpo pide 2,5 % haría
    firmar una tolerancia que no es la que se aplica.
    """
    quote_route = _mock_quote()
    _mock_swap()
    async with _engine() as engine:
        tx = await engine.plan_swap(_quote(), recipient=_RECIPIENT, slippage_bps=250)

    assert _body_of(quote_route.calls[-1].request)["slippageTolerance"] == 2.5
    assert "2.5 % de deslizamiento tolerado" in tx.description


async def test_sin_deslizamiento_configurado_sigue_siendo_el_del_motor() -> None:
    """El parámetro es opcional: sin él, todo queda exactamente como estaba."""
    quote_route = _mock_quote()
    _mock_swap()
    async with _engine() as engine:
        tx = await engine.plan_swap(_quote(), recipient=_RECIPIENT)

    assert _body_of(quote_route.calls[-1].request)["slippageTolerance"] == SLIPPAGE_PCT
    assert f"{SLIPPAGE_PCT:g} % de deslizamiento tolerado" in tx.description


async def test_dos_deslizamientos_no_comparten_la_cotizacion_en_cache() -> None:
    """La tolerancia entra en la clave de caché: son construcciones distintas.

    Compartir la cotización entre dos tolerancias serviría el cuerpo de la
    segunda con la tolerancia de la primera, sin que nadie lo notara.
    """
    quote_route = _mock_quote()
    _mock_swap()
    async with _engine() as engine:
        await engine.plan_swap(_quote(), recipient=_RECIPIENT, slippage_bps=10)
        await engine.plan_swap(_quote(), recipient=_RECIPIENT, slippage_bps=250)

    assert quote_route.call_count == 2
    tolerancias = [_body_of(call.request)["slippageTolerance"] for call in quote_route.calls]
    assert tolerancias == [0.1, 2.5]


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
def test_el_proveedor_declara_la_clave_como_obligatoria() -> None:
    """Sin ella la API responde 401: pedirla es lo que evita un motor mudo."""
    assert MANIFEST.required_config == (CONFIG_API_KEY,)
    assert MANIFEST.allowed_hosts == ("trade-api.gateway.uniswap.org",)


def test_no_se_construye_sin_clave() -> None:
    with pytest.raises(SourceResponseError, match="clave de API"):
        UniswapEngine(api_key="")


def test_el_proveedor_crea_el_motor_con_la_clave_de_la_configuracion() -> None:
    engine = PROVIDER.create({CONFIG_API_KEY: "clave-de-prueba"})
    assert engine.manifest is MANIFEST


# --------------------------------------------------------------------------- #
# Utilería
# --------------------------------------------------------------------------- #
def _body_of(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body


def _pair(chain_key: str = "ethereum") -> TradingPair:
    """Par stablecoin contra envoltorio nativo, que es como cotiza la aplicación.

    Se toman del catálogo real y no de constantes del test: si el catálogo cambia
    una dirección, estas pruebas siguen hablando de la dirección que el motor
    manda de verdad, en vez de pasar contra una copia congelada.
    """
    native = wrapped_native(chain_key)
    stable = quote_token(chain_key)
    if native is None:
        # Arc cobra el gas en USDC y no tiene envoltorio. Se fabrica uno para
        # poder comprobar que el motor lo rechaza **por red** —que es lo que se
        # está midiendo— y no por no haber podido armar el par.
        native = Token("WETH", 18, chain_key, "0x" + "1" * 40)
    if stable is None:
        stable = Token("USDC", 6, chain_key, "0x" + "2" * 40)
    return TradingPair(base=stable, quote=native)


def _amount() -> TokenAmount:
    base = _pair().base
    return TokenAmount(int(_IN_RAW), base.decimals, base.symbol)


def _quote(*, chain_key: str = "ethereum", out_raw: int = int(_OUT_RAW)) -> Quote:
    pair = _pair(chain_key)
    return Quote(
        venue=Venue(
            venue_id=f"uniswap@{chain_key}",
            name=f"Uniswap (agregado, {chain_key})",
            kind=VenueKind.DEX,
            chain=chain_key,
        ),
        engine_id=MANIFEST.engine_id,
        pair=pair,
        amount_in=TokenAmount(int(_IN_RAW), pair.base.decimals, pair.base.symbol),
        amount_out=TokenAmount(out_raw, pair.quote.decimals, pair.quote.symbol),
        fee_bps=None,
        fee_basis=None,
        price_impact_bps=BasisPoints(1),
        observed_at=NOW,
        impact_basis=Measurement.REPORTED,
    )


def _quote_nativo(chain_key: str = "polygon") -> Quote:
    """La cotización de vender el nativo: el caso sin permiso de token.

    Se construye a mano —y no con `_quote`— porque el par tiene el nativo de
    base, que es lo que hace que el build lo pague como `value`.
    """
    native = native_token(chain_key)
    stable = _pair(chain_key).base
    pair = TradingPair(base=native, quote=stable)
    return Quote(
        venue=Venue(
            venue_id=f"uniswap@{chain_key}",
            name=f"Uniswap (agregado, {chain_key})",
            kind=VenueKind.DEX,
            chain=chain_key,
        ),
        engine_id=MANIFEST.engine_id,
        pair=pair,
        amount_in=TokenAmount(10**18, native.decimals, native.symbol),
        amount_out=TokenAmount(int(_OUT_RAW), stable.decimals, stable.symbol),
        fee_bps=None,
        fee_basis=None,
        price_impact_bps=BasisPoints(1),
        observed_at=NOW,
        impact_basis=Measurement.REPORTED,
    )


def _mock_quote(
    *,
    out_raw: str = _OUT_RAW,
    in_raw: str = _IN_RAW,
    impact: Any = 0.01,
) -> respx.Route:
    """Simula `POST /quote` con la forma medida de la respuesta."""
    pair = _pair()
    envelope: dict[str, Any] = {
        "requestId": "req-1",
        "routing": "CLASSIC",
        "quote": {
            "chainId": 1,
            "input": {"amount": in_raw, "token": pair.base.address},
            "output": {"amount": out_raw, "token": pair.quote.address},
            "priceImpact": impact,
            "routeString": "[V3] 100.00% = USDC -- 0.01% --> WETH",
            "gasFee": "1000000000000000",
            "gasFeeUSD": "3.20",
        },
    }
    return respx.post(QUOTE_URL).mock(return_value=httpx.Response(200, json=envelope))


def _mock_swap(**overrides: Any) -> respx.Route:
    """Simula `POST /swap`. Por defecto devuelve el destino medido de Ethereum."""
    payload: dict[str, Any] = {
        "to": ROUTERS["ethereum"],
        "data": "0xdeadbeef",
        "value": "0x00",
        "gasLimit": 200_000,
        "chainId": 1,
    }
    payload.update(overrides)
    return respx.post(SWAP_URL).mock(
        return_value=httpx.Response(200, json={"swap": payload, "requestId": "req-2"})
    )


@asynccontextmanager
async def _engine() -> AsyncIterator[UniswapEngine]:
    """Motor abierto y cerrado aunque el test falle.

    Sin intervalo entre peticiones: el ritmo real defiende de la cuota, y en un
    test sólo añadiría medio segundo de espera por petición.
    """
    engine = UniswapEngine(api_key="clave-de-prueba", min_interval_seconds=0.0)
    await engine.aopen()
    try:
        yield engine
    finally:
        await engine.aclose()
