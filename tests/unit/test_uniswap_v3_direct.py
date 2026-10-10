"""Uniswap V3 por contrato: la vía sin clave, y las formas de equivocarse.

Lo que se fija aquí no es que «cotice». Es lo que puede perder dinero o publicar
una cifra falsa:

1. **La codificación.** Un `exactInputSingle` con las palabras en el orden
   equivocado no revierte: cotiza o ejecuta **otro** pool. Los dos routers tienen
   structs distintos —el V1 lleva `deadline` y el SwapRouter02 no—, así que hay
   dos codificadores y usar el de uno en el otro produce un calldata que el
   contrato lee como una función distinta. Y el `bytes[]` del `multicall` lleva
   desplazamientos **relativos a la tabla de desplazamientos**, no al principio
   del calldata: es el error clásico y con un solo elemento no se ve.

2. **El impacto de precio.** Se deriva, y derivarlo mal publica un número que
   parece medido. La comisión se descuenta del marginal **una sola vez**: si se
   contara también como impacto, un pool del 1 % aparecería con un 1 % de impacto
   en cada operación por pequeña que fuera.

3. **Qué se hace con cada fallo.** Que un nodo conteste «revierte» al cotizar un
   pool y que no conteste son cosas distintas, y confundirlas hace que una fuente
   caída se lea como un pool sin liquidez. Aquí se comprueba que el revert del
   quoter **omite ese tramo** y el revert de `getPool` **aborta**: el primero es
   una respuesta del mercado, el segundo sólo puede ser que la fábrica o el
   selector no son los que la tabla dice.

4. **El permiso que viaja en el payload.** En Base no existe el SwapRouter V1,
   así que el camino de un solo `approve` no existe ahí: el payload tiene que
   declarar los dos permisos encadenados, y lo tiene que decidir el motor, que es
   quien acaba de leer el `factory()` de ese router.

5. **El camino empaquetado del multi-salto.** El `bytes path` de `exactInput` no
   es una lista de direcciones como el `path[]` de V2: son los bytes pegados
   —token de 20 bytes, comisión de 3, token— sin longitud por medio. Una
   dirección de 19 bytes corre todos los bytes siguientes y el contrato lee una
   comisión donde hay media dirección: no revierte al codificar, revierte en la
   cadena después de firmar.

6. **El segundo salto.** Cuando el par no tiene pool directo en ningún tramo, el
   motor arma una ruta de dos saltos por un token del catálogo y la ejecuta en
   **una sola transacción**. Lo que se fija aquí es que la ruta publicada sea la
   que se ejecuta —mismo camino, no una búsqueda nueva que diera más—, que la
   segunda pata se cotice con lo que de verdad daría la primera y que la comisión
   de la ruta viaje en unidades de V3 al construir.

Las cifras de las cotizaciones están **medidas**: contra Base el 2026-10-06 —el
pool bankr/WETH del tramo del 1 %, su `sqrtPriceX96` y la salida del QuoterV2— y
contra Polygon el 2026-10-09 —1 POL por WPOL→USDC→pUSD, con las salidas de sus
dos patas— para que las cuentas hablen del mismo orden de magnitud que la
cadena. El lector se sustituye por un doble, que es la frontera de red del
motor; todo lo demás que se ejercita es el código de verdad.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from amigocompora.domain.chains import chain
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    EngineConfigError,
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
    TradingPair,
)
from amigocompora.domain.money import EXACT, BasisPoints, TokenAmount
from amigocompora.engines.catalog import token_by_symbol, tokens_for, wrapped_native
from amigocompora.engines.uniswap_math import combine_impact_bps, impact_bps
from amigocompora.engines.uniswap_v3 import calldata as abi
from amigocompora.engines.uniswap_v3 import engine as v3
from amigocompora.engines.uniswap_v3.addresses import (
    DEPLOYMENTS,
    SWAP_CHAINS,
    Approval,
    Deployment,
)

AHORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

#: Medido contra Base el 2026-10-06, sólo con lecturas.
BANKR = "0x26f79444595cf1c6753baed09dbc0c6f73144443"
POOL_BANKR = "0x1220f66a1a58275403b683c670ba10b9c7f03178"
SQRT_PRICE_X96 = 1060855681176090901021375

#: Lo que dio el QuoterV2 para 0,0005 WETH por el tramo del 1 %.
ENTRADA_RAW = 500_000_000_000_000
SALIDA_RAW = 2_760_804_136_986_585_319_610_519

DESTINATARIO = "0x" + "b" * 40
BASE = DEPLOYMENTS["base"]
ETHEREUM = DEPLOYMENTS["ethereum"]


# --------------------------------------------------------------------------- #
# Piezas
# --------------------------------------------------------------------------- #
def _palabra_uint(value: int) -> str:
    return f"{value:064x}"


def _palabra_addr(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _cero() -> str:
    return "0x" + _palabra_uint(0)


def _slot0(sqrt_price_x96: int) -> str:
    """`slot0()` real: `sqrtPriceX96` y cuatro campos más detrás."""
    return "0x" + _palabra_uint(sqrt_price_x96) + _palabra_uint(0) * 4


def _dir(token: Token) -> str:
    """La dirección con la que el motor pregunta: en minúsculas.

    `_on_chain_address` normaliza a minúsculas —las fuentes discrepan en el
    *checksum* y comparar dos formas de la misma dirección daría dos tokens
    distintos—, así que las claves del doble de lectura tienen que ser el
    calldata que de verdad se manda, no una versión parecida con otra caja.
    """
    return (token.address or "").lower()


def _weth(chain_key: str = "base") -> Token:
    envoltorio = wrapped_native(chain_key)
    assert envoltorio is not None
    return envoltorio


def _bankr(chain_key: str = "base") -> Token:
    return Token(chain=chain_key, address=BANKR, symbol="bankr", decimals=18)


def _nativo(chain_key: str = "base") -> Token:
    spec = chain(chain_key)
    return Token(chain=chain_key, address=None, symbol=spec.native_symbol, decimals=18)


def _par() -> TradingPair:
    return TradingPair(base=_weth(), quote=_bankr())


class _Lector:
    """Doble de `ChainReader`, que despacha por **calldata** y no por dirección.

    Se sustituye el lector entero y no el transporte HTTP porque lo que se
    ejercita es la **decisión** del motor —qué pregunta y qué hace con cada
    respuesta—, y el failover tiene sus propias pruebas. Despachar por calldata
    y no por dirección es lo que permite que `slot0()` y `token0()` vayan al
    mismo pool sin confundirse: con un doble por dirección, las dos lecturas
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


class _Escenario(_Lector):
    """Un pool que existe en un solo tramo, y el resto de tramos en cero."""

    def __init__(
        self,
        *,
        solo_fee: int = 10_000,
        pool: str = POOL_BANKR,
        salida_raw: int = SALIDA_RAW,
        sqrt_price_x96: int = SQRT_PRICE_X96,
        token0: str = BANKR,
        fee_cerrado: dict[int, int] | None = None,
    ) -> None:
        super().__init__()
        self.solo_fee = solo_fee
        self.pool = pool
        self.salida_raw = salida_raw
        self.sqrt_price_x96 = sqrt_price_x96
        self.token0 = token0
        self.fee_cerrado = fee_cerrado or {}

    def _por_defecto(self, to: str, data: str) -> object:
        if data.startswith(abi.SELECTOR_GET_POOL):
            tramo = int(data[-64:], 16)
            if tramo != self.solo_fee:
                return _cero()
            return "0x" + _palabra_addr(self.pool)
        if data == abi.SELECTOR_TOKEN0:
            return "0x" + _palabra_addr(self.token0)
        if data == abi.SELECTOR_SLOT0:
            return _slot0(self.sqrt_price_x96)
        if data.startswith(abi.SELECTOR_QUOTE_EXACT_INPUT_SINGLE):
            # El tramo va en la cuarta palabra de este calldata, que es lo que
            # permite dar una salida distinta por pool en el mismo quoter. El
            # desplazamiento se cuenta desde el final del selector y no desde el
            # principio de la cadena: el `0x` y los cuatro bytes del selector
            # ocupan sitio, y saltárselos lee una ventana desplazada que no es
            # ninguna palabra —con lo que todos los tramos devolverían lo mismo y
            # la prueba pasaría por el motivo equivocado—.
            inicio = len(abi.SELECTOR_QUOTE_EXACT_INPUT_SINGLE) + 3 * 64
            tramo = int(data[inicio : inicio + 64], 16)
            return "0x" + _palabra_uint(self.fee_cerrado.get(tramo, self.salida_raw))
        return None


def _motor(lector: _Lector) -> v3.UniswapV3Engine:
    # El doble cumple la superficie que el motor usa del lector; el motor lo
    # acepta por inyección precisamente para que esto sea posible.
    return v3.UniswapV3Engine(clock=FrozenClock(AHORA), reader=lector)  # type: ignore[arg-type]


def _cotizacion(
    *,
    chain_key: str = "base",
    fee: int = 10_000,
    salida: int = SALIDA_RAW,
    base: Token | None = None,
    quote: Token | None = None,
) -> Quote:
    par = TradingPair(
        base=base or _weth(chain_key), quote=quote or _bankr(chain_key)
    )
    return Quote(
        venue=v3._venue(chain_key, fee),
        engine_id="uniswap_v3",
        pair=par,
        amount_in=par.base.amount("0.0005"),
        amount_out=TokenAmount(salida, par.quote.decimals, par.quote.symbol),
        fee_bps=BasisPoints(fee // 100),
        fee_basis=Measurement.DERIVED,
        price_impact_bps=BasisPoints(5),
        impact_basis=Measurement.DERIVED,
        observed_at=AHORA,
    )


# --------------------------------------------------------------------------- #
# 1. La codificación
# --------------------------------------------------------------------------- #
def test_cada_firma_produce_el_selector_que_se_buscó_en_la_cadena() -> None:
    """Los selectores salieron de escanear los `PUSH4` del bytecode desplegado.

    Si alguien cambia una firma, el selector deja de coincidir con el que el
    contrato tiene, y la llamada entra por otra función del mismo contrato.
    """
    assert abi.SELECTOR_EXACT_INPUT_SINGLE_V1 == "0x414bf389"
    assert abi.SELECTOR_EXACT_INPUT_SINGLE_SR02 == "0x04e45aaf"
    assert abi.SELECTOR_EXACT_INPUT_V1 == "0xc04b8d59"
    assert abi.SELECTOR_EXACT_INPUT_SR02 == "0xb858183f"
    assert abi.SELECTOR_QUOTE_EXACT_INPUT == "0xcdca1753"
    assert abi.SELECTOR_MULTICALL == "0xac9650d8"
    assert abi.SELECTOR_UNWRAP_WETH9 == "0x49404b7c"
    assert abi.SELECTOR_REFUND_ETH == "0x12210e8a"
    assert abi.SELECTOR_GET_POOL == "0x1698ee82"
    assert abi.SELECTOR_SLOT0 == "0x3850c7bd"
    assert abi.SELECTOR_TOKEN0 == "0x0dfe1681"
    assert abi.SELECTOR_QUOTE_EXACT_INPUT_SINGLE == "0xc6a5026a"


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
        abi, "_MEASURED", {**abi._MEASURED, abi.GET_POOL: "0x00000000"}
    )
    with pytest.raises(EngineConfigError, match="no coincide con la que se midió"):
        abi._require_measured_selectors()


def test_el_v1_lleva_deadline_y_el_swaprouter02_no() -> None:
    """Ocho palabras tras el selector en el V1, siete en el 02.

    Pasarle a uno el struct del otro no revierte al construirse: el contrato lee
    el campo de más como parte del siguiente y ejecuta contra otro destinatario.
    """
    # El mismo juego de campos para los dos: lo único que cambia es el
    # `deadline`, que es lo que se está midiendo.
    comun: dict[str, Any] = {
        "token_in": BANKR,
        "token_out": _weth().address or "",
        "fee": 10_000,
        "recipient": DESTINATARIO,
        "amount_in_raw": 1_000,
        "amount_out_min_raw": 900,
    }
    v1 = abi.exact_input_single(**comun, deadline=1_800_000_000)
    sr02 = abi.exact_input_single(**comun)

    assert v1.startswith(abi.SELECTOR_EXACT_INPUT_SINGLE_V1)
    assert sr02.startswith(abi.SELECTOR_EXACT_INPUT_SINGLE_SR02)
    assert len(v1) == len(abi.SELECTOR_EXACT_INPUT_SINGLE_V1) + 8 * 64
    assert len(sr02) == len(abi.SELECTOR_EXACT_INPUT_SINGLE_SR02) + 7 * 64

    # El cuerpo se recorta por la longitud del selector en vez de por un 2
    # escrito a mano: el selector incluye el `0x`, así que son 10 caracteres, y
    # un desplazamiento de 2 lee ocho caracteres dentro del propio selector.
    cuerpo = v1[len(abi.SELECTOR_EXACT_INPUT_SINGLE_V1) :]
    delgado = sr02[len(abi.SELECTOR_EXACT_INPUT_SINGLE_SR02) :]
    # La `deadline` va **entre** el destinatario y el importe, y sólo en el V1:
    # el campo que el 02 tiene ahí es el importe de entrada.
    assert cuerpo[4 * 64 : 5 * 64] == _palabra_uint(1_800_000_000)
    assert delgado[4 * 64 : 5 * 64] == _palabra_uint(1_000)
    assert cuerpo[5 * 64 : 6 * 64] == _palabra_uint(1_000)
    # Y el resto del struct coincide palabra a palabra entre los dos.
    for palabra in (0, 1, 2, 3):
        desde = palabra * 64
        assert cuerpo[desde : desde + 64] == delgado[desde : desde + 64]


def test_el_multicall_lleva_los_desplazamientos_relativos_a_la_tabla() -> None:
    """El error clásico de `bytes[]`: los desplazamientos no son absolutos.

    Van contados desde el principio de la tabla de desplazamientos, no desde el
    principio del calldata, y avanzan **el tamaño rellenado** del elemento, no el
    de su contenido. Las dos cosas se comprueban aquí porque con un solo elemento
    ninguna de las dos se nota, y estos `multicall` llevan dos o tres.
    """
    primera = abi.refund_eth()
    segunda = abi.unwrap_weth9(7, DESTINATARIO)
    crudo = abi.multicall([primera, segunda])

    assert crudo.startswith(abi.SELECTOR_MULTICALL)
    cuerpo = crudo[len(abi.SELECTOR_MULTICALL) :]
    assert cuerpo[0:64] == _palabra_uint(32)  # puntero al array
    assert cuerpo[64:128] == _palabra_uint(2)  # longitud del array

    # La tabla empieza en la palabra 2 del array, así que el primer elemento
    # apunta a 64 —los dos desplazamientos— y no a 32.
    tabla = cuerpo[128:]
    assert int(tabla[0:64], 16) == 64
    # El segundo salta el primero **ya rellenado**: 32 de longitud más 32 de
    # `refundETH()`, que son 4 bytes de contenido y 28 de relleno. Contar sólo
    # los 4 deja el segundo desplazamiento 28 bytes por detrás de donde el
    # decodificador busca el elemento.
    assert abi._padded_bytes(len(primera) - 2) == 32
    assert int(tabla[64:128], 16) == 64 + 32 + 32

    # Y el contenido de cada elemento está donde dice su desplazamiento, medido
    # desde el principio de la tabla.
    inicio_tabla = len(abi.SELECTOR_MULTICALL) + 128
    for indice, payload in enumerate((primera, segunda)):
        desplazamiento = int(
            crudo[inicio_tabla + indice * 64 : inicio_tabla + (indice + 1) * 64], 16
        )
        comienzo = inicio_tabla + desplazamiento * 2
        assert int(crudo[comienzo : comienzo + 64], 16) == len(payload[2:]) // 2
        assert crudo[comienzo + 64 : comienzo + 64 + len(payload[2:])] == payload[2:]


def test_un_multicall_sin_llamadas_es_un_error_y_no_un_calldata_vacío() -> None:
    """`multicall([])` a un router es una transacción que no hace nada.

    Se firma, se emite, se paga el gas y no se compra nada. Mejor que no se pueda
    construir.
    """
    with pytest.raises(SourceResponseError, match="sin llamadas"):
        abi.multicall([])


def test_el_orden_de_los_campos_de_la_cotización_es_el_de_la_firma() -> None:
    """En `quoteExactInputSingle` la comisión va **después** del importe.

    En la struct del swap va tercera. Intercambiar esas dos palabras no falla:
    cotiza otro pool, y el usuario ve un precio que no es el que va a ejecutar.
    """
    crudo = abi.quote_exact_input_single(BANKR, _weth().address or "", 1_000, 10_000)
    assert crudo.startswith(abi.SELECTOR_QUOTE_EXACT_INPUT_SINGLE)
    cuerpo = crudo[len(abi.SELECTOR_QUOTE_EXACT_INPUT_SINGLE) :]
    assert cuerpo[0:64] == _palabra_addr(BANKR)
    assert cuerpo[64:128] == _palabra_addr(_weth().address or "")
    assert cuerpo[128:192] == _palabra_uint(1_000)
    assert cuerpo[192:256] == _palabra_uint(10_000)
    assert cuerpo[256:320] == _palabra_uint(0)


def test_una_respuesta_truncada_es_sin_dato_y_no_un_cero() -> None:
    """`0x` no es «la dirección cero»: es «no hay respuesta».

    Un nodo que contesta `0x` a un contrato que no existe daría, leído como
    número, un cero — y un cero es el valor que significa «no hay pool» y «precio
    cero». Confundirlos hace que una respuesta rota se lea como un pool vacío.
    """
    assert abi.decode_address("0x") is None
    assert abi.decode_uint("0x") is None
    assert abi.decode_uint("0x" + "ab" * 10) is None  # palabra incompleta
    # Un cero legítimo sigue siendo cero, y una palabra entera sí se lee.
    assert abi.decode_uint("0x" + _palabra_uint(0)) == 0
    assert abi.decode_address("0x" + _palabra_uint(0)) == "0x" + "00" * 20


# --------------------------------------------------------------------------- #
# 2. El impacto
# --------------------------------------------------------------------------- #
def _salida_con_impacto(impacto: Decimal, fee: BasisPoints) -> int:
    """La salida que produciría ese impacto, para poder afirmar el número exacto."""
    marginal = EXACT.divide(Decimal(SQRT_PRICE_X96 * SQRT_PRICE_X96), Decimal(1 << 192))
    efectivo = EXACT.multiply(marginal, EXACT.subtract(Decimal(1), fee.as_ratio()))
    return int(
        EXACT.multiply(
            Decimal(ENTRADA_RAW), EXACT.multiply(efectivo, EXACT.subtract(Decimal(1), impacto))
        )
    )


def test_el_impacto_se_mide_contra_el_marginal() -> None:
    """Se afirma el número, no «que sea mayor que cero».

    Una prueba que sólo mira el signo pasa también con la fórmula equivocada.
    """
    fee = BasisPoints(100)
    impacto = impact_bps(
        amount_in_raw=ENTRADA_RAW,
        amount_out_raw=_salida_con_impacto(Decimal("0.01"), fee),
        sqrt_price_x96=SQRT_PRICE_X96,
        token_in_is_token0=True,
        fee=fee,
    )
    assert impacto is not None
    assert abs(impacto.value - 100) <= 1


def test_la_comisión_no_se_cuenta_dos_veces() -> None:
    """Un pool del 1 % al marginal exacto da impacto **cero**, no 1 %.

    Es el error que publica una cifra falsa en cada operación: el importe del
    quoter ya viene neto de comisión, así que si además se le resta la comisión
    al comparar, el tramo aparece como impacto aunque la orden sea diminuta.
    """
    fee = BasisPoints(100)
    impacto = impact_bps(
        amount_in_raw=ENTRADA_RAW,
        amount_out_raw=_salida_con_impacto(Decimal(0), fee),
        sqrt_price_x96=SQRT_PRICE_X96,
        token_in_is_token0=True,
        fee=fee,
    )
    assert impacto is not None
    assert impacto.value <= 1


def test_un_impacto_negativo_se_publica_como_cero() -> None:
    """Ejecutar nunca es mejor que el marginal: un negativo es el redondeo.

    La linealización de `sqrtPriceX96` tiene error y con órdenes pequeñas la resta
    sale negativa por unos puntos básicos. Publicar un impacto negativo diría que
    el swap mejora el precio, que no es cierto.
    """
    impacto = impact_bps(
        amount_in_raw=ENTRADA_RAW,
        amount_out_raw=ENTRADA_RAW * 10,
        sqrt_price_x96=SQRT_PRICE_X96,
        token_in_is_token0=True,
        fee=BasisPoints(100),
    )
    assert impacto is not None
    assert impacto.value == 0


def test_sin_base_de_comparacion_no_hay_impacto() -> None:
    """Se devuelve `None`, no un cero tranquilizador."""
    base: dict[str, object] = {
        "amount_in_raw": 1,
        "amount_out_raw": 1,
        "sqrt_price_x96": 1,
        "token_in_is_token0": True,
        "fee": BasisPoints(100),
    }
    for campo in ("amount_in_raw", "amount_out_raw", "sqrt_price_x96"):
        alterado = {**base, campo: 0}
        assert impact_bps(**alterado) is None  # type: ignore[arg-type]


def test_el_sentido_del_precio_depende_de_cual_sea_el_token0() -> None:
    """Vender el token1 es el inverso de vender el token0, y el impacto lo nota.

    Si el motor diera por hecho que el token vendido es el 0, el signo del precio
    marginal se invertiría sin que nada lo delatara.
    """
    fee = BasisPoints(100)
    salida = _salida_con_impacto(Decimal("0.01"), fee)
    como_token0 = impact_bps(
        amount_in_raw=ENTRADA_RAW,
        amount_out_raw=salida,
        sqrt_price_x96=SQRT_PRICE_X96,
        token_in_is_token0=True,
        fee=fee,
    )
    como_token1 = impact_bps(
        amount_in_raw=ENTRADA_RAW,
        amount_out_raw=salida,
        sqrt_price_x96=SQRT_PRICE_X96,
        token_in_is_token0=False,
        fee=fee,
    )
    assert como_token0 is not None
    assert como_token1 is not None
    assert como_token0.value != como_token1.value


# --------------------------------------------------------------------------- #
# 3. El venue y su camino de vuelta
# --------------------------------------------------------------------------- #
def test_el_venue_lleva_el_tramo_dentro_y_se_puede_deshacer() -> None:
    """La cotización identifica el pool por su tramo y al construir hay que recuperarlo.

    Y el camino de vuelta tiene que devolver el tramo **en unidades de V3**, no
    en las del `venue_id`. `venue_for` escribe la comisión en puntos básicos
    —`uniswap-v3@100` es el 1 %— porque es la unidad del dominio y la comparten
    todos los motores, pero el router de V3 quiere centésimas de punto básico,
    donde ese mismo pool es `10000`. Devolver el número del `venue_id` tal cual
    construiría contra el pool del 0,01 %: otro contrato, otro precio, y sin que
    nada chirriara hasta que la transacción revirtiera.
    """
    for tramo in (100, 500, 3_000, 10_000):
        venue = v3._venue("base", tramo)
        assert venue.venue_id == f"uniswap-v3@{tramo // 100}"
        assert v3._fee_from_venue(venue.venue_id) == tramo
    assert v3._venue("base", 10_000).name == "Uniswap V3 1 %"


def test_un_venue_de_otro_motor_no_se_acepta() -> None:
    """Construir contra el venue de otro motor ejecutaría en otro sitio."""
    for ajeno in ("uniswap-v2@3000", "uniswap-v3@", "uniswap-v3@abc", "uniswap-v3@0"):
        with pytest.raises(UnsupportedOperationError, match="no es un venue"):
            v3._fee_from_venue(ajeno)


def test_el_tramo_en_centesimas_de_punto_basico_se_publica_en_puntos_basicos() -> None:
    """V3 cuenta la comisión en centésimas de punto básico; el dominio, en puntos.

    Un tramo `3000` de V3 es el 0,30 %, o sea 30 puntos básicos. Publicarlo como
    `3000` multiplicaría por cien la comisión en la tabla y falsearía la
    comparación contra los demás motores.
    """
    assert v3._percent(3_000) == "0.3"
    assert v3._percent(10_000) == "1"
    assert v3._percent(100) == "0.01"
    assert v3._venue("base", 3_000).name == "Uniswap V3 0.3 %"
    assert v3._fee_from_venue("uniswap-v3@30") == 3_000
    assert v3._fee_from_venue("uniswap-v3@1") == 100


# --------------------------------------------------------------------------- #
# 4. Cotizar
# --------------------------------------------------------------------------- #
async def test_los_venues_son_los_cuatro_tramos_que_se_sondean() -> None:
    """Se listan los tramos que se preguntan, no los pools que existen.

    Saber cuáles existen exige preguntar a la fábrica, y hacerlo al pintar la
    lista gastaría cuatro lecturas por red y por par. Los `venue_id` de la lista
    tienen que ser **exactamente** los que llevan las cotizaciones: si no, la
    tabla mostraría un venue que después no casa con ninguna fila.
    """
    motor = _motor(_Escenario())
    assert [venue.venue_id for venue in await motor.venues("base")] == [
        "uniswap-v3@1",
        "uniswap-v3@5",
        "uniswap-v3@30",
        "uniswap-v3@100",
    ]
    assert [venue.name for venue in await motor.venues("base")] == [
        "Uniswap V3 0.01 %",
        "Uniswap V3 0.05 %",
        "Uniswap V3 0.3 %",
        "Uniswap V3 1 %",
    ]
    # Y en una red sin despliegue medido no se promete nada.
    assert await motor.venues("solana") == ()


async def test_cotiza_contra_el_pool_que_existe() -> None:
    """El tramo sin pool devuelve el cero de la fábrica y se descarta."""
    motor = _motor(_Escenario())
    cotizaciones = await motor.quote(_par(), _par().base.amount("0.0005"))

    assert len(cotizaciones) == 1
    cotizacion = cotizaciones[0]
    assert cotizacion.venue.venue_id == "uniswap-v3@100"
    assert cotizacion.amount_out.raw == SALIDA_RAW
    assert cotizacion.engine_id == "uniswap_v3"

    # La comisión es el tramo, y se declara **derivada**, no publicada: la
    # deducimos de con qué tramo la fábrica construyó ese pool; no la reporta
    # nadie. `REPORTED` diría que alguien la publica.
    assert cotizacion.fee_bps == BasisPoints(100)
    assert cotizacion.fee_basis is Measurement.DERIVED
    assert cotizacion.impact_basis is Measurement.DERIVED

    # La liquidez va a `None` a propósito: el `L` de un pool V3 no es una
    # cantidad de ningún token, y ponerlo al lado de las reservas de un pool V2
    # compararía dos cosas distintas.
    assert cotizacion.liquidity is None


async def test_se_pregunta_a_la_fábrica_por_los_cuatro_tramos() -> None:
    """Quedarse con el primero es quedarse con un precio peor sin decirlo.

    Medido: WETH/USDC en Ethereum tiene pool en tres tramos distintos.
    """
    lector = _Escenario()
    motor = _motor(lector)
    await motor.quote(_par(), _par().base.amount("0.0005"))
    assert len(lector.calls_to(BASE.factory)) == 4


async def test_se_queda_con_el_pool_que_mas_da() -> None:
    """Con dos pools que cotizan, gana la salida mayor.

    El motor normaliza las direcciones a minúsculas antes de preguntar, así que
    las claves del doble son el calldata que de verdad se manda y no una versión
    parecida: si no coincidieran, la lectura caería al comportamiento por
    omisión y la prueba pasaría por el motivo equivocado.

    Las dos salidas son del orden de la medida —no cifras de juguete— porque una
    salida ridícula se descarta por impacto: el motor compara lo que se obtiene
    con el precio marginal, y obtener mil unidades donde caben 2,7 cuatrillones
    es un impacto del 100 % que no se publica. Con salidas falsas pero del tamaño
    real, lo único que decide es cuál de los dos tramos da más.
    """
    lector = _Escenario(solo_fee=500)
    lector.handlers[abi.get_pool(_dir(_weth()), BANKR, 3_000)] = "0x" + _palabra_addr(
        "0x" + "9" * 40
    )
    lector.fee_cerrado = {500: int(SALIDA_RAW * 0.97), 3_000: SALIDA_RAW}

    motor = _motor(lector)
    cotizaciones = await motor.quote(_par(), _par().base.amount("0.0005"))
    assert len(cotizaciones) == 1
    assert cotizaciones[0].venue.venue_id == "uniswap-v3@30"
    assert cotizaciones[0].amount_out.raw == SALIDA_RAW


async def test_un_quoter_que_revierte_omite_ese_tramo_y_no_rompe_la_cotizacion() -> None:
    """«Ese pool no puede dar salida a este tamaño» es una respuesta, no una caída.

    Medido: en Base los tramos 100, 500 y 3000 de bankr/WETH **no tienen pool** y
    el quoter revierte al preguntarles. Si el revert abortara, el par dejaría de
    cotizar por culpa de tres tramos vacíos.
    """
    lector = _Escenario()
    lector.handlers[
        abi.quote_exact_input_single(_dir(_weth()), BANKR, ENTRADA_RAW, 10_000)
    ] = SourceResponseError("execution reverted")
    motor = _motor(lector)
    assert await motor.quote(_par(), _par().base.amount("0.0005")) == ()


async def test_un_getpool_que_revierte_aborta_la_cotizacion() -> None:
    """`getPool` no tiene motivo para revertir con entradas válidas.

    Devuelve la dirección cero cuando no hay pool. Así que un revert sólo puede
    significar que la fábrica o el selector no son los que la tabla dice, y eso
    hay que verlo antes de firmar nada en vez de tragárselo como «no hay pool».
    """
    lector = _Escenario()
    lector.handlers[abi.get_pool(_dir(_weth()), BANKR, 100)] = SourceResponseError("revert")
    lector.handlers[abi.get_pool(_dir(_weth()), BANKR, 500)] = SourceResponseError("revert")
    lector.handlers[abi.get_pool(_dir(_weth()), BANKR, 3_000)] = SourceResponseError("revert")
    lector.handlers[abi.get_pool(_dir(_weth()), BANKR, 10_000)] = SourceResponseError("revert")
    motor = _motor(lector)
    with pytest.raises(SourceResponseError):
        await motor.quote(_par(), _par().base.amount("0.0005"))


async def test_si_el_mejor_pool_no_se_puede_leer_el_estado_se_usa_el_siguiente() -> None:
    """Un `slot0` ilegible no puede dejar al usuario sin cotización habiendo otra.

    Los candidatos se recorren de mayor a menor salida, así que se usa el
    siguiente mejor. Devolver vacío sería tirar una cotización que sí se lee.

    El siguiente mejor tiene que dar una salida **creíble**: si diera una cifra
    muy por debajo de la medida, se descartaría por impacto y la prueba no
    distinguiría «no se pudo leer el mejor» de «ninguno sirve».
    """
    sobrante = "0x" + "9" * 40
    lector = _Escenario(solo_fee=3_000, pool=POOL_BANKR)
    lector.handlers[abi.get_pool(_dir(_weth()), BANKR, 10_000)] = "0x" + _palabra_addr(
        sobrante
    )
    lector.fee_cerrado = {3_000: int(SALIDA_RAW * 0.99), 10_000: SALIDA_RAW}
    motor = _motor(lector)

    # El estado del pool que más da no se puede leer.
    original = lector._por_defecto

    def con_fallo(to: str, data: str) -> object:
        if data == abi.SELECTOR_SLOT0 and to.lower() == sobrante.lower():
            return SourceResponseError("no se pudo leer el estado")
        return original(to, data)

    lector._por_defecto = con_fallo  # type: ignore[method-assign]
    cotizaciones = await motor.quote(_par(), _par().base.amount("0.0005"))
    assert len(cotizaciones) == 1
    assert cotizaciones[0].venue.venue_id == "uniswap-v3@30"


async def test_un_pool_de_liquidez_residual_no_produce_cotizacion() -> None:
    """Un impacto enorme no se publica: sólo añade ruido a la tabla."""
    # Precio marginal 1 y salida la mitad de lo entregado: un 50 % de impacto,
    # muy por encima del 10 % tolerado.
    motor = _motor(_Escenario(salida_raw=ENTRADA_RAW // 2, sqrt_price_x96=1 << 96))
    assert await motor.quote(_par(), _par().base.amount("0.0005")) == ()


async def test_sin_direccion_de_contrato_no_hay_pool_que_preguntar() -> None:
    """Un token sin dirección no existe en la cadena, y ahí no hay nada que leer."""
    motor = _motor(_Escenario())
    suelto = Token(chain="base", address=None, symbol="RARO", decimals=18)
    par = TradingPair(base=_weth(), quote=suelto)
    assert await motor.quote(par, par.base.amount("1")) == ()


async def test_el_nativo_se_cotiza_por_su_envoltorio() -> None:
    """Los AMM no operan con el nativo: se opera con el ERC-20 que lo envuelve."""
    assert v3._on_chain_address(_nativo()) == (_weth().address or "").lower()
    # Un par que se resuelve al mismo contrato por los dos lados no es un par:
    # sería el motor preguntándose por WETH contra WETH.
    motor = _motor(_Escenario())
    par = TradingPair(base=_nativo(), quote=_weth())
    assert await motor.quote(par, par.base.amount("1")) == ()


async def test_en_una_red_sin_despliegue_medido_no_se_cotiza() -> None:
    """No hay tabla de contratos para esa red, así que no se inventa una."""
    motor = _motor(_Escenario())
    otro = Token(
        chain="solana",
        address="So11111111111111111111111111111111111111112",
        symbol="SOL",
        decimals=9,
    )
    otro_mas = Token(
        chain="solana",
        address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        symbol="USDC",
        decimals=6,
    )
    par = TradingPair(base=otro, quote=otro_mas)
    assert await motor.quote(par, par.base.amount("1")) == ()


# --------------------------------------------------------------------------- #
# 5. Construir
# --------------------------------------------------------------------------- #
async def test_un_token_contra_token_es_una_sola_llamada_sin_multicall() -> None:
    """Sin envoltorio que desenvolver ni sobrante que devolver no hay nada que envolver.

    Un `multicall` de una sola llamada gasta gas de más y añade una capa donde
    algo se puede codificar mal.
    """
    motor = _motor(_Escenario())
    payload = await motor.plan_swap(_cotizacion(), recipient=DESTINATARIO)

    assert payload.calldata.startswith(abi.SELECTOR_EXACT_INPUT_SINGLE_SR02)
    assert payload.to_address == BASE.router
    assert payload.chain_id == chain("base").require_eip155_id()


async def test_comprar_con_el_nativo_lleva_el_importe_y_un_reembolso() -> None:
    """El nativo viaja con la transacción, y el sobrante tiene que volver.

    El router cobra lo que consume y deja el resto en el contrato si nadie lo
    reclama: por eso el patrón es `multicall([swap, refundETH])`.
    """
    motor = _motor(_Escenario())
    payload = await motor.plan_swap(
        _cotizacion(base=_nativo(), quote=_bankr()), recipient=DESTINATARIO
    )

    assert payload.calldata.startswith(abi.SELECTOR_MULTICALL)
    assert abi.SELECTOR_REFUND_ETH[2:] in payload.calldata
    # El importe que se manda es exactamente el que se cotizó.
    assert payload.value.raw == ENTRADA_RAW
    assert payload.value.decimals == chain("base").native_decimals
    # Comprar con el nativo no necesita autorizar ningún token.
    assert payload.approval is None


async def test_vender_por_el_nativo_desenvuelve_antes_de_entregar() -> None:
    """V3 no entrega nativo: entrega el envoltorio, y hay que retirarlo.

    Sin `unwrapWETH9` el usuario recibiría WETH creyendo que recibió ETH. Y el
    destinatario del swap tiene que ser el propio router —el envoltorio se le
    queda a él y él lo retira— mientras que el del `unwrapWETH9` es el usuario:
    poner el usuario en los dos haría que el nativo no saliera nunca del router.
    """
    motor = _motor(_Escenario())
    payload = await motor.plan_swap(
        _cotizacion(base=_bankr(), quote=_nativo()), recipient=DESTINATARIO
    )

    assert payload.calldata.startswith(abi.SELECTOR_MULTICALL)
    assert abi.SELECTOR_UNWRAP_WETH9[2:] in payload.calldata
    cuerpo = payload.calldata
    assert _palabra_addr(BASE.router)[2:] in cuerpo
    assert _palabra_addr(DESTINATARIO)[2:] in cuerpo
    assert payload.value.raw == 0


async def test_el_payload_declara_el_permiso_que_ese_router_necesita() -> None:
    """En Base el SwapRouter V1 no existe: el permiso son dos, no uno.

    Es el dato que el ejecutor necesita antes de firmar, y lo declara el motor,
    que es quien acaba de leer el `factory()` de ese router. Si viajara una tabla
    de casos por red en el camino que firma, al añadir una red habría dos sitios
    que actualizar y uno se olvidaría.
    """
    motor = _motor(_Escenario())
    en_base = await motor.plan_swap(_cotizacion(), recipient=DESTINATARIO)

    assert en_base.approval is not None
    assert en_base.approval.spender == BASE.router
    assert en_base.approval.via == BASE.permit2
    assert en_base.approval.is_chained


async def test_el_minimo_es_el_de_ahora_y_nunca_por_encima_de_lo_que_el_pool_da() -> None:
    """`amountOutMinimum` sale de la cotización fresca y se redondea hacia abajo.

    Hacia arriba quedaría por encima de lo que el pool entrega de verdad, y la
    transacción revertiría pagando el gas. Sobre la fresca y no sobre la mostrada
    porque es la de ahora la que se va a ejecutar.
    """
    motor = _motor(_Escenario())
    payload = await motor.plan_swap(_cotizacion(), recipient=DESTINATARIO)

    # El mínimo es la palabra 6 del struct (índice 5 tras el destinatario): se
    # lee del calldata en vez de rehacer la cuenta a mano.
    cuerpo = payload.calldata[2 + 8 :]
    minimo = int(cuerpo[5 * 64 : 6 * 64], 16)
    assert minimo == int(Decimal(SALIDA_RAW) * Decimal("0.995"))
    assert minimo < SALIDA_RAW


async def test_el_minimo_no_puede_quedar_por_encima_de_lo_que_el_pool_entrega() -> None:
    """Es el caso que revierte: el mínimo tiene que ser menor que la salida real.

    Se comprueba sobre una salida que no es divisible de forma exacta por el
    deslizamiento, que es donde un redondeo hacia arriba se notaría.
    """
    motor = _motor(_Escenario(salida_raw=1_000_003))
    payload = await motor.plan_swap(_cotizacion(salida=1_000_003), recipient=DESTINATARIO)
    cuerpo = payload.calldata[2 + 8 :]
    minimo = int(cuerpo[5 * 64 : 6 * 64], 16)
    assert minimo < 1_000_003


async def test_el_payload_caduca_donde_el_router_admite_caducidad() -> None:
    """El SwapRouter V1 lleva `deadline` en su struct; el 02 no lo acepta.

    Ponerle la caducidad al 02 sería llamar a otra función: ese campo no existe
    en su struct, y el contrato leería el importe mínimo donde cree que está otra
    cosa.
    """
    assert ETHEREUM.has_deadline
    assert not BASE.has_deadline

    motor = _motor(_Escenario())
    cotizacion = _cotizacion(chain_key="ethereum", base=_weth("ethereum"), quote=_bankr("ethereum"))
    payload = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    assert payload.calldata.startswith(abi.SELECTOR_EXACT_INPUT_SINGLE_V1)
    esperada = int(AHORA.timestamp()) + v3.DEADLINE_SECONDS
    assert _palabra_uint(esperada) in payload.calldata


async def test_si_el_precio_se_movio_mas_de_lo_tolerado_no_se_construye() -> None:
    """El payload tiene que corresponder al precio que el usuario leyó.

    Se tolera un 1 %: por debajo el mercado se mueve solo; por encima, el usuario
    estaría firmando una operación distinta de la que vio.
    """
    motor = _motor(_Escenario(salida_raw=int(SALIDA_RAW * 1.05)))
    with pytest.raises(QuoteMovedError):
        await motor.plan_swap(_cotizacion(), recipient=DESTINATARIO)


async def test_un_movimiento_pequeno_del_precio_no_impide_construir() -> None:
    """Un movimiento dentro de la tolerancia es el mercado funcionando."""
    motor = _motor(_Escenario(salida_raw=int(SALIDA_RAW * 1.005)))
    payload = await motor.plan_swap(_cotizacion(), recipient=DESTINATARIO)
    assert payload.calldata


async def test_si_el_pool_ya_no_cotiza_se_dice_que_vuelva_a_cotizar() -> None:
    """La liquidez se agota entre que se pinta la tabla y se pulsa el botón."""
    lector = _Escenario()
    lector.handlers[
        abi.quote_exact_input_single(_dir(_weth()), BANKR, ENTRADA_RAW, 10_000)
    ] = SourceResponseError("execution reverted")
    motor = _motor(lector)
    with pytest.raises(NoQuotesError, match="ya no cotiza"):
        await motor.plan_swap(_cotizacion(), recipient=DESTINATARIO)


async def test_construir_en_una_red_sin_contratos_medidos_es_un_error_explicito() -> None:
    """Un motor no debe construir donde no ha medido los contratos.

    Y el error tiene que decir qué redes sí cubre, porque el usuario no puede
    adivinarlo desde «no soportado».
    """
    motor = _motor(_Escenario())
    with pytest.raises(UnsupportedOperationError, match="no construye swaps"):
        await motor.plan_swap(_cotizacion(chain_key="solana"), recipient=DESTINATARIO)


async def test_el_destino_declarado_es_el_router_de_la_tabla() -> None:
    """Es el contraste contra el que el ejecutor comprueba el `to` antes de firmar.

    Se lee de la tabla de despliegues y no se escribe aparte: son la misma
    pregunta —«¿a qué contrato manda este motor los swaps?»— y dos listas
    acabarían discrepando.
    """
    motor = _motor(_Escenario())
    for red, despliegue in DEPLOYMENTS.items():
        assert motor.expected_destination(red) == despliegue.router
    assert motor.expected_destination("solana") is None


async def test_la_descripcion_dice_lo_que_el_usuario_tiene_que_saber_antes_de_firmar() -> None:
    """Lo que se entrega, lo que se recibe, el mínimo y cuántos permisos hacen falta."""
    motor = _motor(_Escenario())
    payload = await motor.plan_swap(_cotizacion(), recipient=DESTINATARIO)
    texto = payload.description
    assert "entregas" in texto
    assert "recibes" in texto
    assert "mínimo" in texto
    # En Base se cobra por Permit2, así que hay que decir que son **dos**
    # permisos: «autoriza el token al router» sería mentira y el swap revertiría
    # después de firmar.
    assert "dos" in texto
    assert "Permit2" in texto


async def test_el_payload_no_lleva_limite_de_gas_y_eso_es_deliberado() -> None:
    """El motor no conoce la dirección que paga, así que no puede estimarlo.

    Lo estima quien firma, contra el estado del momento, que es lo único que
    vale. Un límite inventado aquí sería un número que nadie comprobó.
    """
    motor = _motor(_Escenario())
    payload = await motor.plan_swap(_cotizacion(), recipient=DESTINATARIO)
    assert payload.gas_limit is None


# --------------------------------------------------------------------------- #
# 6. La tabla y la publicación
# --------------------------------------------------------------------------- #
def test_los_contratos_declarados_son_los_que_se_midieron() -> None:
    """La tabla es medida, y estas son las direcciones que se midieron.

    Está aquí para que un cambio en la tabla tenga que ser deliberado y no se
    cuele por un refactor.
    """
    assert set(DEPLOYMENTS) == {"ethereum", "optimism", "arbitrum", "polygon", "base", "bsc"}
    assert frozenset(DEPLOYMENTS) == SWAP_CHAINS
    # En Base **no** hay SwapRouter V1, y por eso allí el permiso es encadenado.
    assert BASE.permit2 == "0x000000000022D473030F116dDEE9F6B43aC78BA3"
    assert not BASE.has_deadline
    assert BASE.approval is Approval.PERMIT2
    # Y en Ethereum sí está, con permiso directo y caducidad.
    assert ETHEREUM.permit2 is None
    assert ETHEREUM.has_deadline
    assert ETHEREUM.approval is Approval.DIRECT


def test_un_despliegue_se_niega_a_declararse_de_forma_incoherente() -> None:
    """Encadenado sin Permit2 y directo con Permit2 serían permisos al contrato equivocado.

    El ejecutor leería `via` y resolvería al camino que no es: autorizaría al
    router cuando quien cobra es Permit2 —o al revés— y el swap revertiría
    después de pagar el gas.
    """
    with pytest.raises(ValueError, match="Permit2"):
        Deployment(
            factory="0x" + "1" * 40,
            quoter="0x" + "2" * 40,
            router="0x" + "3" * 40,
            approval=Approval.PERMIT2,
            has_deadline=False,
        )
    with pytest.raises(ValueError, match="permiso directo"):
        Deployment(
            factory="0x" + "1" * 40,
            quoter="0x" + "2" * 40,
            router="0x" + "3" * 40,
            approval=Approval.DIRECT,
            has_deadline=True,
            permit2="0x" + "4" * 40,
        )


def test_el_proveedor_no_necesita_configuracion() -> None:
    """Ni clave, ni host, ni límite: es la propiedad que hace que no se detenga.

    Un motor que declarara configuración obligatoria se quedaría sin funcionar en
    una instalación recién hecha, que es justo lo que esta vía existe para
    evitar.
    """
    assert v3.MANIFEST.required_config == ()
    motor = v3.PROVIDER.create({})
    assert motor.manifest.engine_id == "uniswap_v3"
    # Y los hosts que declara el manifiesto salen de la tabla de nodos medidos,
    # no de una lista escrita a mano que podría separarse de la que se usa.
    from amigocompora.engines.evm_rpc import rpc_hosts

    assert v3.MANIFEST.allowed_hosts == rpc_hosts(SWAP_CHAINS)
    assert v3.MANIFEST.allowed_hosts


def test_el_manifiesto_declara_que_prepara_transacciones() -> None:
    """Un motor con redes de swap tiene que declarar que construye, o no se le pide.

    `EngineManifest` lo exige al construirse: un motor que diga que cotiza pero no
    que construye nunca recibiría la petición del payload.
    """
    from amigocompora.domain.modes import Capability

    assert Capability.PREPARE_TX in v3.MANIFEST.capabilities
    assert v3.MANIFEST.swap_chains == SWAP_CHAINS


# --------------------------------------------------------------------------- #
# 7. El camino empaquetado del multi-salto
# --------------------------------------------------------------------------- #
#: Medido contra Polygon el 2026-10-09: 1 POL por WPOL(500)USDC(100)pUSD.
WPOL = "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270"
USDC_POLYGON = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
USDCE_POLYGON = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
POLYGON = DEPLOYMENTS["polygon"]

ENTRADA_POL = 10**18
PATA_1_MEDIDA = 99_603
PATA_2_MEDIDA = 99_594


def test_el_camino_empaquetado_es_token_comision_token() -> None:
    """La forma medida: 20 bytes de token, 3 de comisión, 20 del siguiente.

    El valor exacto se afirma y no se describe: es el camino que el QuoterV2 de
    Polygon aceptó el 2026-10-09 devolviendo 99594 —lo mismo que componen sus
    dos patas sueltas—, así que cualquier desalineación de un byte aquí produce
    un camino que el contrato lee como otra cosa.
    """
    camino = abi.packed_path([WPOL, USDC_POLYGON, PUSD], [500, 100])
    assert camino == "0x" + WPOL[2:] + "0001f4" + USDC_POLYGON[2:] + "000064" + PUSD[2:]
    assert len(camino) == 2 + 2 * 66  # los 66 bytes medidos


def test_un_camino_que_no_encadena_no_se_codifica() -> None:
    """De `n` comisiones salen `n + 1` tokens; cualquier otra cuenta no es un camino."""
    with pytest.raises(SourceResponseError, match="no encadena"):
        abi.packed_path([WPOL, USDC_POLYGON, PUSD], [500])


def test_un_tramo_que_no_cabe_en_tres_bytes_no_es_un_camino() -> None:
    """La comisión ocupa 3 bytes exactos: ni un tramo mayor ni el cero."""
    with pytest.raises(SourceResponseError, match="3 bytes"):
        abi.packed_path([WPOL, USDC_POLYGON], [1 << 24])
    with pytest.raises(SourceResponseError, match="3 bytes"):
        abi.packed_path([WPOL, USDC_POLYGON], [0])


def test_una_direccion_corta_corre_el_camino_entero() -> None:
    """19 bytes de dirección leen media dirección como comisión, sin revertir al codificar."""
    with pytest.raises(SourceResponseError, match="20 bytes"):
        abi.packed_path([WPOL[:-1], USDC_POLYGON], [500])
    with pytest.raises(SourceResponseError, match="hexadecimal"):
        abi.packed_path(["0x" + "z" * 40, USDC_POLYGON], [500])


def test_el_exactinput_v1_lleva_deadline_y_el_del_02_no() -> None:
    """Diez palabras tras el selector en el V1, nueve en el 02 — y en otro orden.

    El struct del multi-salto tiene un campo más en el V1 (`deadline`), así que
    usar el ABI de uno en el router del otro es llamar a otra función. Y la
    longitud del camino va en **bytes** (66), no en caracteres: el contrato lee
    esa palabra como contador de bytes.
    """
    camino = abi.packed_path([WPOL, USDC_POLYGON, PUSD], [500, 100])
    comun: dict[str, Any] = {
        "path": camino,
        "recipient": DESTINATARIO,
        "amount_in_raw": 1_000,
        "amount_out_min_raw": 900,
    }
    v1 = abi.exact_input(**comun, deadline=1_800_000_000)
    sr02 = abi.exact_input(**comun)

    assert v1.startswith(abi.SELECTOR_EXACT_INPUT_V1)
    assert sr02.startswith(abi.SELECTOR_EXACT_INPUT_SR02)
    assert len(v1) == len(abi.SELECTOR_EXACT_INPUT_V1) + 10 * 64
    assert len(sr02) == len(abi.SELECTOR_EXACT_INPUT_SR02) + 9 * 64

    cuerpo = v1[len(abi.SELECTOR_EXACT_INPUT_V1) :]
    delgado = sr02[len(abi.SELECTOR_EXACT_INPUT_SR02) :]
    assert cuerpo[0:64] == _palabra_uint(32)  # desplazamiento al struct
    assert cuerpo[64:128] == _palabra_uint(0xA0)  # y al `bytes` dentro del struct
    assert delgado[64:128] == _palabra_uint(0x80)
    assert cuerpo[128:192] == _palabra_addr(DESTINATARIO)
    # La caducidad va entre el destinatario y el importe, y sólo en el V1: lo
    # que el 02 tiene en esa palabra es ya el importe de entrada.
    assert cuerpo[192:256] == _palabra_uint(1_800_000_000)
    assert delgado[192:256] == _palabra_uint(1_000)
    assert cuerpo[256:320] == _palabra_uint(1_000)
    assert cuerpo[320:384] == _palabra_uint(900)
    assert cuerpo[384:448] == _palabra_uint(66)
    assert cuerpo[448 : 448 + 132] == camino[2:]
    # Los 60 caracteres que sobran son el relleno del bloque de 32 bytes.
    assert cuerpo[448 + 132 :] == "0" * 60


def test_quote_exact_input_lleva_el_camino_y_el_importe() -> None:
    """El quoter del multi-salto cotiza el camino entero, no un pool."""
    camino = abi.packed_path([WPOL, USDC_POLYGON, PUSD], [500, 100])
    crudo = abi.quote_exact_input(camino, 1_000)
    assert crudo.startswith(abi.SELECTOR_QUOTE_EXACT_INPUT)
    cuerpo = crudo[len(abi.SELECTOR_QUOTE_EXACT_INPUT) :]
    assert cuerpo[0:64] == _palabra_uint(64)
    assert cuerpo[64:128] == _palabra_uint(1_000)
    assert cuerpo[128:192] == _palabra_uint(66)
    assert cuerpo[192 : 192 + 132] == camino[2:]


def test_un_camino_mal_formado_no_llega_al_quoter() -> None:
    """La forma se valida en los dos sitios que consumen el camino.

    Un camino puede llegar construido de fuera —una ruta publicada, una celda de
    caché—, así que no basta con validarlo dentro de `packed_path`.
    """
    for malo in ("0x1234", "0x" + "ab" * 44, "0x" + "ab" * 45):
        with pytest.raises(SourceResponseError, match=r"20 \+ 23"):
            abi.quote_exact_input(malo, 1)
        with pytest.raises(SourceResponseError, match=r"20 \+ 23"):
            abi.exact_input(
                path=malo,
                recipient=DESTINATARIO,
                amount_in_raw=1,
                amount_out_min_raw=1,
                deadline=1,
            )


# --------------------------------------------------------------------------- #
# 8. Cotizar la ruta de dos saltos
# --------------------------------------------------------------------------- #
def _palabra_de(data: str, selector: str, index: int) -> str:
    """La palabra `index` del cuerpo de ese calldata."""
    inicio = len(selector) + index * 64
    return data[inicio : inicio + 64]


def _direccion_de(data: str, selector: str, index: int) -> str:
    return "0x" + _palabra_de(data, selector, index)[24:]


def _clave_pool(a: str, b: str, fee: int) -> tuple[str, str, int]:
    """La clave de un pool: la pareja **ordenada** y el tramo.

    La fábrica ordena por dirección antes de calcular la dirección del pool, así
    que `(A, B)` y `(B, A)` dan el mismo contrato y el doble no puede distinguir
    el orden con el que se preguntó.
    """
    first, second = sorted((a.lower(), b.lower()))
    return first, second, fee


class _EscenarioRuta(_Lector):
    """Un par sin pool directo y una ruta de dos saltos por un hub.

    Despacha por el camino pedido: los pools por pareja de direcciones en
    cualquier orden, y las cotizaciones por la terna **direccional**
    `(entra, sale, tramo, importe)`. El importe va dentro de la clave a
    propósito: la segunda pata se cotiza con lo que de verdad dio la primera, y
    una tabla que lo ignorara devolvería la misma salida a cualquier entrada,
    que es justo el error que la prueba no debe dejar pasar.
    """

    def __init__(
        self,
        *,
        pools: dict[tuple[str, str, int], str],
        salidas: dict[tuple[str, str, int, int], int],
        caminos: dict[tuple[str, int], int],
        sqrt: dict[str, int],
        token0: dict[str, str],
        camino_revierte: bool = False,
    ) -> None:
        super().__init__()
        self.pools = pools
        self.salidas = salidas
        self.caminos = caminos
        self.sqrt = sqrt
        self.token0 = token0
        self.camino_revierte = camino_revierte

    def _por_defecto(self, to: str, data: str) -> object:
        if data.startswith(abi.SELECTOR_GET_POOL):
            pool = self.pools.get(
                _clave_pool(
                    _direccion_de(data, abi.SELECTOR_GET_POOL, 0),
                    _direccion_de(data, abi.SELECTOR_GET_POOL, 1),
                    int(_palabra_de(data, abi.SELECTOR_GET_POOL, 2), 16),
                )
            )
            return _cero() if pool is None else "0x" + _palabra_addr(pool)
        if data.startswith(abi.SELECTOR_QUOTE_EXACT_INPUT_SINGLE):
            selector = abi.SELECTOR_QUOTE_EXACT_INPUT_SINGLE
            salida = self.salidas.get(
                (
                    _direccion_de(data, selector, 0),
                    _direccion_de(data, selector, 1),
                    int(_palabra_de(data, selector, 3), 16),
                    int(_palabra_de(data, selector, 2), 16),
                )
            )
            if salida is None:
                # Ese tramo no puede dar salida a este tamaño: una respuesta del
                # mercado, no una caída.
                raise SourceResponseError("execution reverted")
            return "0x" + _palabra_uint(salida)
        if data.startswith(abi.SELECTOR_QUOTE_EXACT_INPUT):
            if self.camino_revierte:
                raise SourceResponseError("execution reverted")
            inicio = len(abi.SELECTOR_QUOTE_EXACT_INPUT)
            cantidad = int(data[inicio + 64 : inicio + 128], 16)
            largo = int(data[inicio + 128 : inicio + 192], 16)
            camino = "0x" + data[inicio + 192 : inicio + 192 + largo * 2]
            salida = self.caminos.get((camino, cantidad))
            if salida is None:
                # Construir con otro camino que el publicado es el fallo que
                # esta prueba existe para ver: que se vea con el camino entero.
                raise AssertionError(f"se cotizó un camino que no es el publicado: {camino}")
            return "0x" + _palabra_uint(salida)
        if data == abi.SELECTOR_TOKEN0:
            return "0x" + _palabra_addr(self.token0[to.lower()])
        if data == abi.SELECTOR_SLOT0:
            return _slot0(self.sqrt[to.lower()])
        return None


def _sin_datos() -> _EscenarioRuta:
    """Un lector que no contesta nada: para construir sin volver a cotizar."""
    return _EscenarioRuta(pools={}, salidas={}, caminos={}, sqrt={}, token0={})


def _sqrt_de(precio: Decimal) -> int:
    """`sqrtPriceX96` de un precio marginal, para no escribir el entero a mano."""
    return int(EXACT.multiply(precio.sqrt(), Decimal(1 << 96)))


def _pusd() -> Token:
    token = token_by_symbol("pUSD", "polygon")
    assert token is not None
    return token


def _par_pol_a_pusd() -> TradingPair:
    return TradingPair(base=_nativo("polygon"), quote=_pusd())


#: Los pools del escenario medido: WPOL/USDC en dos tramos, WPOL/USDC.e y USDC/pUSD.
P1 = "0x" + "1" * 40
P2 = "0x" + "2" * 40
P3 = "0x" + "3" * 40
P4 = "0x" + "4" * 40

#: La vuelta: pUSD → USDC → WPOL, con las cifras coherentes con la ida.
REVERSA_ENTRADA = 99_594
REVERSA_PATA_1 = 99_600
REVERSA_PATA_2 = 995_000_000_000_000_000

CAMINO_IDA = abi.packed_path([WPOL, USDC_POLYGON, PUSD], [500, 100])
CAMINO_VUELTA = abi.packed_path([PUSD, USDC_POLYGON, WPOL], [100, 500])


def _escenario_pol_a_pusd() -> _EscenarioRuta:
    """El caso medido: 1 POL no tiene pool directo con pUSD en ningún tramo.

    El hub USDC.e cotiza la primera pata pero no tiene segunda —es lo que se
    midió: `quoteExactInputSingle` de USDC.e→pUSD revierte—, así que su ruta no
    llega entera y compite sólo la del USDC. Las direcciones de la vuelta van en
    los mismos diccionarios para poder construir también pUSD → POL.
    """
    return _EscenarioRuta(
        pools={
            _clave_pool(WPOL, USDC_POLYGON, 500): P1,
            _clave_pool(WPOL, USDC_POLYGON, 3_000): P4,
            _clave_pool(WPOL, USDCE_POLYGON, 500): P3,
            _clave_pool(USDC_POLYGON, PUSD, 100): P2,
        },
        salidas={
            (WPOL, USDC_POLYGON, 500, ENTRADA_POL): PATA_1_MEDIDA,
            (WPOL, USDC_POLYGON, 3_000, ENTRADA_POL): 99_500,  # peor: no se elige
            (WPOL, USDCE_POLYGON, 500, ENTRADA_POL): 99_608,  # sin segunda pata
            (USDC_POLYGON, PUSD, 100, PATA_1_MEDIDA): PATA_2_MEDIDA,
            (PUSD, USDC_POLYGON, 100, REVERSA_ENTRADA): REVERSA_PATA_1,
            (USDC_POLYGON, WPOL, 500, REVERSA_PATA_1): REVERSA_PATA_2,
        },
        caminos={
            (CAMINO_IDA, ENTRADA_POL): PATA_2_MEDIDA,
            (CAMINO_VUELTA, REVERSA_ENTRADA): REVERSA_PATA_2,
        },
        # El marginal de WPOL/USDC sale del precio medido y el de USDC/pUSD, de
        # la paridad con la pegged: los dos lados de la ruta existen de verdad.
        sqrt={P1: _sqrt_de(Decimal("9.97e-14")), P2: _sqrt_de(Decimal("1.001"))},
        token0={P1: WPOL, P2: USDC_POLYGON},
    )


def test_las_direcciones_del_escenario_son_las_del_catalogo() -> None:
    """Si el catálogo cambia una dirección, el escenario tiene que cambiar con él."""
    assert _dir(_weth("polygon")) == WPOL
    usdc = token_by_symbol("USDC", "polygon")
    assert usdc is not None
    assert (usdc.address or "").lower() == USDC_POLYGON
    assert (_pusd().address or "").lower() == PUSD
    assert USDCE_POLYGON in {
        (token.address or "").lower() for token in tokens_for("polygon")
    }


async def test_si_el_par_directo_cotiza_no_se_buscan_rutas() -> None:
    """La ruta no compite con el pool directo: sólo entra cuando no hay ninguno."""
    lector = _Escenario()
    motor = _motor(lector)
    cotizaciones = await motor.quote(_par(), _par().base.amount("0.0005"))

    assert len(cotizaciones) == 1
    assert cotizaciones[0].route is None
    # Cuatro `getPool`, los del par directo: ninguna pareja de hub se preguntó.
    assert len(lector.calls_to(BASE.factory)) == 4


async def test_una_ruta_de_dos_saltos_cotiza_el_camino_entero() -> None:
    """El par sin pool directo cae a la ruta por hub, y la publica dentro."""
    lector = _escenario_pol_a_pusd()
    motor = _motor(lector)
    par = _par_pol_a_pusd()
    cotizaciones = await motor.quote(par, par.base.amount("1"))

    assert len(cotizaciones) == 1
    cotizacion = cotizaciones[0]
    assert cotizacion.venue.venue_id == "uniswap-v3@5+1"
    assert cotizacion.venue.name == "Uniswap V3 0.05 % + 0.01 %"
    assert cotizacion.fee_bps == BasisPoints(6)
    assert cotizacion.fee_basis is Measurement.DERIVED
    assert cotizacion.impact_basis is Measurement.DERIVED
    assert cotizacion.liquidity is None
    assert cotizacion.amount_out.raw == PATA_2_MEDIDA
    assert cotizacion.amount_out.symbol == "pUSD"

    # El camino publicado nombra los tokens **del pool**: el lado nativo del par
    # es su envoltorio, que es el que de verdad cruza el pool.
    assert cotizacion.route is not None
    assert [token.symbol for token in cotizacion.route.tokens] == ["WPOL", "USDC", "pUSD"]
    assert [hop.fee_bps for hop in cotizacion.route.hops] == [
        BasisPoints(5),
        BasisPoints(1),
    ]
    assert cotizacion.route.total_fee_bps == BasisPoints(6)

    # El impacto es la composición de los dos tramos, calculada con los datos
    # que el doble dio a cada pata —incluida la segunda, cotizada con lo que
    # dio la primera— y no una suma ni el impacto de un solo pool.
    primera = impact_bps(
        amount_in_raw=ENTRADA_POL,
        amount_out_raw=PATA_1_MEDIDA,
        sqrt_price_x96=lector.sqrt[P1],
        token_in_is_token0=True,
        fee=BasisPoints(5),
    )
    segunda = impact_bps(
        amount_in_raw=PATA_1_MEDIDA,
        amount_out_raw=PATA_2_MEDIDA,
        sqrt_price_x96=lector.sqrt[P2],
        token_in_is_token0=True,
        fee=BasisPoints(1),
    )
    assert primera is not None
    assert segunda is not None
    assert cotizacion.price_impact_bps == combine_impact_bps([primera, segunda])
    assert 0 < cotizacion.price_impact_bps.value < 100

    assert "dos saltos" in cotizacion.source_note
    assert "USDC" in cotizacion.source_note


async def test_la_ruta_se_ejecuta_en_un_solo_swap_con_el_camino_publicado() -> None:
    """Construir vuelve a cotizar **ese** camino, no busca otro que diera más.

    El doble sólo contesta a la ruta exacta que el motor publicó: si al
    construir se buscara de nuevo, o se cambiara un tramo, la lectura no
    casaría y la prueba lo vería.
    """
    lector = _escenario_pol_a_pusd()
    motor = _motor(lector)
    par = _par_pol_a_pusd()
    cotizacion = (await motor.quote(par, par.base.amount("1")))[0]
    payload = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    assert payload.calldata.startswith(abi.SELECTOR_MULTICALL)
    assert abi.SELECTOR_REFUND_ETH[2:] in payload.calldata
    indice = payload.calldata.index(abi.SELECTOR_EXACT_INPUT_V1[2:])
    cuerpo = payload.calldata[indice + 8 :]
    assert cuerpo[64:128] == _palabra_uint(0xA0)  # el struct del V1, con caducidad
    assert cuerpo[128:192] == _palabra_addr(DESTINATARIO)
    assert cuerpo[192:256] == _palabra_uint(int(AHORA.timestamp()) + v3.DEADLINE_SECONDS)
    assert cuerpo[256:320] == _palabra_uint(ENTRADA_POL)
    # El mínimo sale de la cotización **fresca** del camino y se redondea abajo.
    assert cuerpo[320:384] == _palabra_uint(int(Decimal(PATA_2_MEDIDA) * Decimal("0.995")))
    assert cuerpo[384:448] == _palabra_uint(66)
    assert cuerpo[448 : 448 + 132] == CAMINO_IDA[2:]

    assert payload.to_address == POLYGON.router
    assert payload.value.raw == ENTRADA_POL
    assert payload.approval is None  # se paga con el nativo: no hay token que autorizar


async def test_vender_por_el_nativo_en_una_ruta_desenvuelve_en_la_misma_transaccion() -> None:
    """La vuelta: pUSD → USDC → WPOL, con el envoltorio retirado al final.

    El destinatario del swap es el router —el envoltorio se le queda a él— y el
    del `unwrapWETH9` es el usuario: con el usuario en los dos, el nativo no
    saldría nunca del router.
    """
    lector = _escenario_pol_a_pusd()
    motor = _motor(lector)
    pusd = _pusd()
    par = TradingPair(base=pusd, quote=_nativo("polygon"))
    cotizacion = (await motor.quote(par, pusd.amount("0.099594")))[0]

    assert cotizacion.venue.venue_id == "uniswap-v3@1+5"
    assert cotizacion.amount_out.raw == REVERSA_PATA_2
    payload = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    assert payload.calldata.startswith(abi.SELECTOR_MULTICALL)
    assert abi.SELECTOR_UNWRAP_WETH9[2:] in payload.calldata
    indice = payload.calldata.index(abi.SELECTOR_EXACT_INPUT_V1[2:])
    cuerpo = payload.calldata[indice + 8 :]
    assert cuerpo[128:192] == _palabra_addr(POLYGON.router)
    assert cuerpo[448 : 448 + 132] == CAMINO_VUELTA[2:]
    assert payload.value.raw == 0
    assert payload.approval is not None
    assert payload.approval.spender == POLYGON.router
    assert payload.approval.via is None


async def test_la_descripcion_de_una_ruta_dice_por_donde_pasa() -> None:
    """Lo que el usuario tiene que leer antes de firmar: el camino y los tramos."""
    motor = _motor(_escenario_pol_a_pusd())
    par = _par_pol_a_pusd()
    cotizacion = (await motor.quote(par, par.base.amount("1")))[0]
    payload = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    texto = payload.description
    assert "un solo swap" in texto
    assert "dos tramos" in texto
    assert "0.05 % y 0.01 %" in texto
    assert "WPOL → USDC → pUSD" in texto


async def test_si_el_camino_se_movio_mas_de_lo_tolerado_no_se_construye() -> None:
    """La deriva se mide contra la cotización fresca del mismo camino."""
    lector = _escenario_pol_a_pusd()
    motor = _motor(lector)
    par = _par_pol_a_pusd()
    cotizacion = (await motor.quote(par, par.base.amount("1")))[0]
    lector.caminos[(CAMINO_IDA, ENTRADA_POL)] = int(PATA_2_MEDIDA * 1.05)

    with pytest.raises(QuoteMovedError):
        await motor.plan_swap(cotizacion, recipient=DESTINATARIO)


async def test_si_el_camino_ya_no_cotiza_se_dice_por_donde_iba() -> None:
    """La liquidez se agota entre la tabla y el botón, también en una ruta."""
    lector = _escenario_pol_a_pusd()
    lector.camino_revierte = True
    motor = _motor(lector)
    par = _par_pol_a_pusd()
    cotizacion = (await motor.quote(par, par.base.amount("1")))[0]

    with pytest.raises(NoQuotesError, match="WPOL → USDC → pUSD"):
        await motor.plan_swap(cotizacion, recipient=DESTINATARIO)


async def test_una_ruta_que_no_corresponde_al_par_no_se_construye() -> None:
    """Un camino que no empiece o no termine donde el par ejecutaría otro swap."""
    motor = _motor(_sin_datos())
    par = _par_pol_a_pusd()
    wpol = Token(chain="polygon", address=WPOL, symbol="WPOL", decimals=18)
    usdc = Token(chain="polygon", address=USDC_POLYGON, symbol="USDC", decimals=6)
    usdce = Token(chain="polygon", address=USDCE_POLYGON, symbol="USDC.e", decimals=6)
    ajena = SwapRoute(
        hops=(
            RouteHop(base=wpol, quote=usdc, fee_bps=BasisPoints(5)),
            RouteHop(base=usdc, quote=usdce, fee_bps=BasisPoints(1)),
        )
    )
    cotizacion = Quote(
        venue=v3._route_venue("polygon", (500, 100)),
        engine_id="uniswap_v3",
        pair=par,
        amount_in=par.base.amount("1"),
        amount_out=TokenAmount(PATA_2_MEDIDA, 6, "pUSD"),
        fee_bps=BasisPoints(6),
        fee_basis=Measurement.DERIVED,
        price_impact_bps=BasisPoints(5),
        impact_basis=Measurement.DERIVED,
        observed_at=AHORA,
        route=ajena,
    )
    with pytest.raises(UnsupportedOperationError, match="no corresponde al par"):
        await motor.plan_swap(cotizacion, recipient=DESTINATARIO)


# --------------------------------------------------------------------------- #
# 9. El camino hacia el router, y la ruta en otra red
# --------------------------------------------------------------------------- #
def test_el_camino_de_la_ruta_va_en_unidades_de_v3() -> None:
    """De la ruta publicada al camino se multiplica por cien, como en el venue.

    `fee_bps` son puntos básicos —5 es el 0,05 %— pero el camino empaquetado
    quiere centésimas de punto básico, donde ese pool es 500. Sin la conversión
    el camino apuntaría al pool del 0,0005 %, que en la mayoría de los pares ni
    existe, y no chirriaría hasta ejecutar.
    """
    wpol = Token(chain="polygon", address=WPOL, symbol="WPOL", decimals=18)
    usdc = Token(chain="polygon", address=USDC_POLYGON, symbol="USDC", decimals=6)
    ruta = SwapRoute(
        hops=(
            RouteHop(base=wpol, quote=usdc, fee_bps=BasisPoints(5)),
            RouteHop(base=usdc, quote=_pusd(), fee_bps=BasisPoints(1)),
        )
    )
    motor = _motor(_sin_datos())
    fees, camino = motor._route_path(ruta, token_in=WPOL, token_out=PUSD)

    assert fees == (500, 100)
    assert camino == CAMINO_IDA


def test_una_ruta_sin_comisiones_no_se_puede_construir() -> None:
    """Sin la comisión de cada tramo no hay camino: se dice, no se inventa un 0."""
    wpol = Token(chain="polygon", address=WPOL, symbol="WPOL", decimals=18)
    usdc = Token(chain="polygon", address=USDC_POLYGON, symbol="USDC", decimals=6)
    ruta = SwapRoute(
        hops=(
            RouteHop(base=wpol, quote=usdc),
            RouteHop(base=usdc, quote=_pusd()),
        )
    )
    motor = _motor(_sin_datos())
    with pytest.raises(UnsupportedOperationError, match="comisión"):
        motor._route_path(ruta, token_in=WPOL, token_out=PUSD)


def test_el_venue_de_una_ruta_lleva_los_dos_tramos_dentro() -> None:
    """Dos rutas del mismo par por hubs distintos son sitios distintos, no una fila."""
    venue = v3._route_venue("polygon", (500, 100))
    assert venue.venue_id == "uniswap-v3@5+1"
    assert venue.name == "Uniswap V3 0.05 % + 0.01 %"


#: La vuelta medida: bankr → WETH → USDC, con el 1 % del pool bankr/WETH.
ENTRADA_BANKR = SALIDA_RAW
PATA_B_WETH = 500_000_000_000_000
PATA_B_USDC = 1_900_000
Q1 = "0x" + "5" * 40
Q2 = "0x" + "6" * 40
WETH_BASE = "0x4200000000000000000000000000000000000006"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
CAMINO_BASE = abi.packed_path([BANKR, WETH_BASE, USDC_BASE], [10_000, 500])


async def test_en_una_red_sin_swaprouter_v1_la_ruta_usa_el_abi_del_02() -> None:
    """En Base el permiso es encadenado y el struct no lleva caducidad, también en ruta.

    Es la prueba de que el multi-salto no tiene su propia tabla de routers: el
    ABI sale de la misma tabla medida que usa el swap de un solo pool.
    """
    lector = _EscenarioRuta(
        pools={
            _clave_pool(BANKR, WETH_BASE, 10_000): Q1,
            _clave_pool(WETH_BASE, USDC_BASE, 500): Q2,
        },
        salidas={
            (BANKR, WETH_BASE, 10_000, ENTRADA_BANKR): PATA_B_WETH,
            (WETH_BASE, USDC_BASE, 500, PATA_B_WETH): PATA_B_USDC,
        },
        caminos={(CAMINO_BASE, ENTRADA_BANKR): PATA_B_USDC},
        sqrt={
            Q1: SQRT_PRICE_X96,
            Q2: _sqrt_de(Decimal(PATA_B_USDC) / Decimal(PATA_B_WETH)),
        },
        token0={Q1: BANKR, Q2: WETH_BASE},
    )
    motor = _motor(lector)
    usdc = token_by_symbol("USDC", "base")
    assert usdc is not None
    par = TradingPair(base=_bankr(), quote=usdc)
    cotizacion = (await motor.quote(par, TokenAmount(ENTRADA_BANKR, 18, "bankr")))[0]

    assert cotizacion.venue.venue_id == "uniswap-v3@100+5"
    assert cotizacion.fee_bps == BasisPoints(105)
    assert cotizacion.amount_out.raw == PATA_B_USDC
    assert cotizacion.route is not None
    assert [token.symbol for token in cotizacion.route.tokens] == ["bankr", "WETH", "USDC"]

    payload = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    # Token contra token: la llamada sola, sin `multicall` de envoltorio.
    assert payload.calldata.startswith(abi.SELECTOR_EXACT_INPUT_SR02)
    cuerpo = payload.calldata[len(abi.SELECTOR_EXACT_INPUT_SR02) :]
    assert cuerpo[64:128] == _palabra_uint(0x80)
    assert cuerpo[128:192] == _palabra_addr(DESTINATARIO)
    # Sin caducidad en el struct: la palabra que sigue al destinatario es ya el
    # importe de entrada —con el ABI del V1 aquí habría una deadline—.
    assert cuerpo[192:256] == _palabra_uint(ENTRADA_BANKR)
    assert cuerpo[320:384] == _palabra_uint(len(CAMINO_BASE[2:]) // 2)
    assert cuerpo[384 : 384 + len(CAMINO_BASE[2:])] == CAMINO_BASE[2:]

    assert payload.approval is not None
    assert payload.approval.spender == BASE.router
    assert payload.approval.via == BASE.permit2
    assert payload.approval.is_chained
