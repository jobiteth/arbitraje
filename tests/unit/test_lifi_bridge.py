"""Motor de puentes LI.FI: medido contra la API real.

Todo lo que se fija aquí se midió en vivo el 2026-10-07 cotizando 1 USDC de Base
a Polygon, y la respuesta entera —tal cual llegó, sin retocar— es el fichero
`tests/data/lifi_usdc_base_polygon.json`. Las pruebas parten de ese documento y
lo **modifican** para cada escenario: así lo medido y lo simulado se distinguen
de un vistazo, y una prueba que se rompe se puede comparar contra la respuesta de
verdad.

Lo que cubre, y por qué cada cosa:

- **El rechazo del checksum.** Medido: una `fromAddress` con el *checksum* EIP-55
  mal puesto responde `code 1011` y no cotiza. El motor la pasa a minúsculas en
  su frontera, y esa prueba es la que impide que alguien «limpie» el `.lower()`
  por parecer redundante.
- **El contraste del destino.** Es la comprobación que separa un puente de un
  swap: aquí un `to` equivocado no pierde dinero en una operación de mercado, lo
  manda a otra parte y no vuelve.
- **La comisión en el token de origen, y sólo entonces.** Medido: las tres
  partidas vienen en el USDC de Base. Si alguna viniera en otro token, el total
  no se suma — se dice que no se sabe.
- **La deriva.** Un puente tarda minutos: lo que se enseñó y lo que se firma
  pueden estar lejos, y eso hay que pararlo.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    NoQuotesError,
    QuoteMovedError,
    SourceResponseError,
)
from amigocompora.domain.models import BridgeQuote, BridgeRequest, Measurement, Token
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.engines.lifi import engine as modulo
from amigocompora.engines.lifi.engine import (
    BRIDGE_CHAINS,
    DIAMONDS,
    MANIFEST,
    NATIVE_TOKEN_ADDRESS,
    PROVIDER,
    QUOTE_RECIPIENT,
    QUOTE_URL,
    LifIEngine,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

DATOS: Path = Path(__file__).resolve().parents[1] / "data" / "lifi_usdc_base_polygon.json"

#: Los dos USDC, en minúsculas, tal como se mandaron en la petición medida.
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_POLYGON = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"

#: Medidos en la respuesta: 989.888 unidades mínimas de USDC salen de 1 USDC, y
#: las tres partidas de comisión suman 10.112 (2.500 + 99 + 7.513), o sea 101 bps
#: sobre lo entregado.
OUT_RAW = 989_888
FEE_RAW = 10_112

#: El diamante medido en Base, y el mismo texto con el checksum como lo publica
#: la API —el motor lo normaliza, así que las dos formas valen—.
DIAMANTE = "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae"
DIAMANTE_CHECKSUM = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"

_DESTINATARIO = "0x2c887da24c6e6c939b0fe3adae50814b938b44f9"
_OTRO = "0x00000000000000000000000000000000deadbeef"


@pytest.fixture(autouse=True)
def _sin_red() -> Iterator[None]:
    """Ninguna prueba de este módulo sale a internet, y ninguna puede olvidarse."""
    with respx.mock:
        yield


def respuesta(**cambios: Any) -> dict[str, Any]:
    """La respuesta medida, con los campos que se le pidan cambiados.

    Se parte **siempre** del documento real y no de un diccionario escrito a
    mano: así una prueba no puede pasar por describir una respuesta que LI.FI no
    produce, que es exactamente el error que este fichero de datos evita.
    """
    cuerpo: dict[str, Any] = json.loads(DATOS.read_text(encoding="utf-8"))
    for ruta, valor in cambios.items():
        actual: Any = cuerpo
        partes = ruta.split(".")
        for parte in partes[:-1]:
            actual = actual[parte]
        actual[partes[-1]] = valor
    return cuerpo


def solicitud(*, origen: str = USDC_BASE, destino: str = USDC_POLYGON) -> BridgeRequest:
    return BridgeRequest(
        origin=Token("USDC", 6, "base", origen),
        destination=Token("USDC", 6, "polygon", destino),
        amount_in=TokenAmount(1_000_000, 6, "USDC"),
    )


def ruta(
    *, cuerpo: dict[str, Any] | None = None, status: int = 200
) -> respx.Route:
    return respx.get(QUOTE_URL).mock(
        return_value=httpx.Response(status, json=cuerpo if cuerpo is not None else respuesta())
    )


@asynccontextmanager
async def motor(**kwargs: Any) -> AsyncIterator[LifIEngine]:
    """Motor abierto y cerrado aunque la prueba falle, con el reloj parado."""
    engine = LifIEngine(clock=FrozenClock(NOW), min_interval_seconds=0.0, **kwargs)
    await engine.aopen()
    try:
        yield engine
    finally:
        await engine.aclose()


# --------------------------------------------------------------------------- #
# El manifiesto y la ranura
# --------------------------------------------------------------------------- #
def test_el_manifiesto_ocupa_la_ranura_de_puentes() -> None:
    """No es un motor DEX: es el que cruza, y por eso tiene ranura propia."""
    from amigocompora.domain.protocols import EngineKind

    assert MANIFEST.kind is EngineKind.CROSS_CHAIN
    assert Capability.PREPARE_TX in MANIFEST.capabilities


def test_solo_declara_las_redes_que_tiene_medidas() -> None:
    """La tabla de destinos y las redes que se ofrecen son la misma pregunta.

    Si se escribieran dos veces podrían discrepar, y una red anunciada sin
    dirección medida se cotizaría sin poder contrastar el destino — que es justo
    la comprobación que impide firmar contra otro contrato.
    """
    assert frozenset(DIAMONDS) == BRIDGE_CHAINS
    assert "base" in BRIDGE_CHAINS


def test_la_clave_de_api_es_opcional() -> None:
    """Medido: cotiza sin clave. Exigirla apagaría el motor para no ganar nada."""
    assert MANIFEST.required_config == ()
    assert "api_key" in MANIFEST.optional_config


def test_sin_clave_se_construye_igual() -> None:
    """`create` sin clave da un motor usable, no uno a medias."""
    assert isinstance(PROVIDER.create({}), LifIEngine)


# --------------------------------------------------------------------------- #
# Cotizar
# --------------------------------------------------------------------------- #
async def test_una_red_sin_destino_medido_no_se_cotiza() -> None:
    """Se niega **sin gastar la petición**: no hay nada que contrastar después.

    Es la diferencia entre no ofrecer una ruta y ofrecer una cuyo destino no se
    puede comprobar. Lo segundo sería exactamente lo que la tabla existe para
    impedir, así que la comprobación va antes de la llamada, no después.

    La red de esta prueba es `bsc`, que LI.FI no cubre para cruzar y que por eso
    no está en la tabla medida. Antes era `arbitrum`, y dejó de servir el día en
    que se midió: una prueba de «red sin medir» tiene que usar una red que de
    verdad no esté medida.
    """
    route = ruta()
    otra = BridgeRequest(
        origin=Token("USDC", 6, "bsc", "0x" + "a" * 40),
        destination=Token("USDC", 6, "polygon", USDC_POLYGON),
        amount_in=TokenAmount(1_000_000, 6, "USDC"),
    )
    async with motor() as engine:
        assert await engine.quote_bridge(otra) == ()
        assert engine.expected_destination("bsc") is None
    assert route.call_count == 0


async def test_lee_lo_que_la_respuesta_medida_dice() -> None:
    ruta()
    async with motor() as engine:
        (quote,) = await engine.quote_bridge(solicitud())

    assert quote.engine_id == MANIFEST.engine_id
    assert quote.provider == "AcrossV4"
    assert quote.amount_out == TokenAmount(OUT_RAW, 6, "USDC")
    assert quote.amount_out_min == TokenAmount(OUT_RAW, 6, "USDC")
    assert quote.duration_seconds == 1
    assert quote.observed_at == NOW
    assert quote.is_cross_chain


async def test_suma_las_tres_partidas_de_comision_medidas() -> None:
    """2.500 + 99 + 7.513, las tres en el USDC de origen: 101 bps."""
    ruta()
    async with motor() as engine:
        (quote,) = await engine.quote_bridge(solicitud())

    assert quote.fee == TokenAmount(FEE_RAW, 6, "USDC")
    assert quote.fee_basis is Measurement.REPORTED
    assert quote.fee_bps == BasisPoints(101)


async def test_una_comision_en_otro_token_no_se_suma() -> None:
    """Sumar USDC con ETH no da una cifra: da una lista mal sumada.

    Cuando pasa, la comisión se declara **desconocida** —`fee=None`— y la nota lo
    dice. Es la diferencia entre «no se sabe» y un número falso, y aquí importa
    porque la comisión es lo que se compara entre rutas.
    """
    cuerpo = respuesta()
    cuerpo["estimate"]["feeCosts"][1]["token"]["address"] = "0x" + "e" * 40
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        (quote,) = await engine.quote_bridge(solicitud())

    assert quote.fee is None
    assert quote.fee_basis is None
    assert quote.fee_bps is None
    assert "otro token" in quote.source_note


async def test_una_partida_no_incluida_no_cuenta() -> None:
    """`included: false` es un pago aparte: no sale del importe recibido."""
    cuerpo = respuesta()
    cuerpo["estimate"]["feeCosts"][0]["included"] = False
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        (quote,) = await engine.quote_bridge(solicitud())

    assert quote.fee == TokenAmount(FEE_RAW - 2500, 6, "USDC")


async def test_la_ruta_dice_por_donde_pasa() -> None:
    """«paso previo (Integrator Fee) → cruce por AcrossV4», leído de los pasos."""
    ruta()
    async with motor() as engine:
        (quote,) = await engine.quote_bridge(solicitud())

    assert "AcrossV4" in quote.route
    assert "cruce" in quote.route


async def test_un_cuerpo_sin_estimate_no_se_lee_como_una_ruta() -> None:
    cuerpo = respuesta()
    del cuerpo["estimate"]
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        assert await engine.quote_bridge(solicitud()) == ()


async def test_sin_ruta_la_fuente_contesta_404_y_no_es_un_fallo() -> None:
    """Medido como código ausente: «ese par no se puede cruzar», no formato roto."""
    ruta(cuerpo={"message": "No route found"}, status=404)
    async with motor() as engine:
        assert await engine.quote_bridge(solicitud()) == ()


async def test_un_nativo_se_cotiza_con_el_centinela_medido() -> None:
    """La API nombra los tokens por contrato y el nativo no tiene ninguno.

    Antes esta prueba fijaba el rechazo, porque el centinela no se había medido.
    Ya se midió el 2026-10-07: los dos candidatos que publica la documentación se
    aceptan, la API **normaliza los dos a la dirección cero**, y una dirección
    inventada se rechaza con `code 1011`. Así que lo que hay que fijar ahora es lo
    contrario — que la petición sale, y que sale con el centinela medido.
    """
    route = ruta(cuerpo=respuesta(**{"estimate.fromAmount": str(10**15)}))
    # `noqa: S105`: la dirección cero, que es el centinela medido del nativo, no
    # una credencial. La palabra «TOKEN» del nombre es lo que dispara el aviso.
    assert NATIVE_TOKEN_ADDRESS == "0x0000000000000000000000000000000000000000"  # noqa: S105
    nativo = BridgeRequest(
        origin=Token("ETH", 18, "base", None),
        destination=Token("USDC", 6, "polygon", USDC_POLYGON),
        amount_in=TokenAmount(10**15, 18, "ETH"),
    )
    async with motor() as engine:
        assert len(await engine.quote_bridge(nativo)) == 1

    enviado = route.calls[-1].request.url
    assert enviado.params["fromToken"] == NATIVE_TOKEN_ADDRESS
    assert "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" not in str(enviado)


# --------------------------------------------------------------------------- #
# La petición que se envía
# --------------------------------------------------------------------------- #
async def test_la_direccion_va_en_minusculas() -> None:
    """Medido: con el checksum EIP-55 puesto la API responde `code 1011`.

    El destinatario llega de la interfaz tal como lo escriba el usuario, así que
    la minúscula se aplica en la frontera del motor. Sin esto, el caso normal
    —pegar una dirección desde un explorador, que la da con checksum— dejaría de
    poder cruzar.
    """
    route = ruta()
    async with motor() as engine:
        await engine.plan_bridge(_cotizar(), recipient=DIAMANTE_CHECKSUM.upper())

    params = dict(route.calls[-1].request.url.params)
    assert params["fromAddress"] == DIAMANTE_CHECKSUM.upper().lower()
    assert params["fromAddress"] == params["fromAddress"].lower()


async def test_la_peticion_lleva_las_dos_redes_y_los_dos_contratos() -> None:
    route = ruta()
    async with motor() as engine:
        await engine.quote_bridge(solicitud())

    params = dict(route.calls[-1].request.url.params)
    assert params["fromChain"] == "8453"
    assert params["toChain"] == "137"
    assert params["fromToken"] == USDC_BASE
    assert params["toToken"] == USDC_POLYGON
    assert params["fromAmount"] == "1000000"


async def test_cotizar_usa_un_centinela_y_construir_el_destinatario_real() -> None:
    """El destinatario entra en el calldata, así que las dos peticiones no se pisan."""
    route = ruta()
    async with motor() as engine:
        await engine.quote_bridge(solicitud())
        await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)

    assert route.call_count == 2
    assert dict(route.calls[0].request.url.params)["fromAddress"] == QUOTE_RECIPIENT
    assert dict(route.calls[1].request.url.params)["fromAddress"] == _DESTINATARIO


async def test_la_clave_va_en_la_cabecera_cuando_la_hay() -> None:
    route = ruta()
    async with motor(api_key="clave-de-prueba") as engine:
        await engine.quote_bridge(solicitud())

    assert route.calls[-1].request.headers["x-lifi-api-key"] == "clave-de-prueba"


async def test_sin_clave_no_se_manda_la_cabecera() -> None:
    """Una cabecera vacía es una credencial que la fuente ve y no entiende."""
    route = ruta()
    async with motor() as engine:
        await engine.quote_bridge(solicitud())

    assert "x-lifi-api-key" not in route.calls[-1].request.headers


# --------------------------------------------------------------------------- #
# Construir
# --------------------------------------------------------------------------- #
async def test_construye_el_payload_del_origen() -> None:
    ruta()
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)

    assert tx.chain_id == 8453
    assert tx.to_address == DIAMANTE
    assert tx.calldata.startswith("0x1794958f")
    # Medido: aquí el `value` viene en hexadecimal.
    assert tx.value == TokenAmount(0, 18, "ETH")
    assert tx.gas_limit == int("0xa3b8f", 16)


async def test_la_aprobacion_va_al_gastador_que_declara_la_fuente() -> None:
    """No se supone que sea el destino: se lee de `estimate.approvalAddress`."""
    ruta()
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)

    assert tx.approval is not None
    assert tx.approval.spender == DIAMANTE
    assert tx.approval.target == DIAMANTE
    assert not tx.approval.is_chained


async def test_un_destino_distinto_al_medido_no_se_firma() -> None:
    """Es la comprobación que separa un puente de un swap.

    Aquí un `to` equivocado no pierde dinero en una operación de mercado: lo
    manda a otra parte y no vuelve. Por eso se contrasta **antes** de leer nada
    más del payload.
    """
    ruta(cuerpo=respuesta(**{"transactionRequest.to": _OTRO, "transactionRequest.data": "no-hex"}))
    async with motor() as engine:
        with pytest.raises(SourceResponseError, match="contrato medido para esa red"):
            await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)


async def test_el_contraste_admite_las_mayusculas_de_la_fuente() -> None:
    """El checksum es presentación, no identidad: la dirección es la misma."""
    ruta(cuerpo=respuesta(**{"transactionRequest.to": DIAMANTE.upper()}))
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)

    assert tx.to_address == DIAMANTE


async def test_el_contraste_no_depende_de_como_este_escrita_la_tabla(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La otra mitad del mismo asunto, y la que estaba rota.

    La comprobación pasaba a minúsculas la dirección que devolvía la fuente pero
    **no** la de la tabla, así que funcionaba sólo mientras la tabla estuviera
    escrita en minúsculas — una condición que no estaba escrita en ninguna parte
    y que se rompe el día que alguien guarde la dirección con checksum, que es la
    forma legible y la que usa el resto del proyecto. Relay comparaba las dos
    desde el principio; esto fija que LI.FI también.
    """
    monkeypatch.setattr(modulo, "DIAMONDS", {"base": DIAMANTE_CHECKSUM})
    ruta()
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)

    assert tx.to_address.lower() == DIAMANTE.lower()


async def test_sin_calldata_no_se_construye() -> None:
    ruta(cuerpo=respuesta(**{"transactionRequest.data": "no-hex"}))
    async with motor() as engine:
        with pytest.raises(SourceResponseError, match="hexadecimal"):
            await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)


async def test_sin_transaction_request_no_se_construye() -> None:
    """Cotizó, y aun así no hay nada que firmar: hay que decirlo, no adivinar."""
    cuerpo = respuesta()
    del cuerpo["transactionRequest"]
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        with pytest.raises(SourceResponseError, match="transactionRequest"):
            await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)


async def test_sin_ruta_no_se_sigue_con_la_cotizacion_vieja() -> None:
    ruta(cuerpo={"message": "No route found"}, status=404)
    async with motor() as engine:
        with pytest.raises(NoQuotesError, match="ya no tiene ruta"):
            await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)


# --------------------------------------------------------------------------- #
# La deriva: lo que se enseñó contra lo que se firma
# --------------------------------------------------------------------------- #
async def test_aborta_si_lo_que_se_recibe_se_movio() -> None:
    """Un 10 % menos: se para, y se para **antes** de validar el destino.

    La cotización simulada trae además un destino falso, así que si la
    comprobación corriera después el error sería el del destino. Que salga
    `QuoteMovedError` demuestra el orden — y el orden importa: no tiene sentido
    validar un payload que ya se sabe que no se va a entregar.
    """
    ruta(
        cuerpo=respuesta(
            **{
                "estimate.toAmount": str(OUT_RAW * 9 // 10),
                "transactionRequest.to": _OTRO,
            }
        )
    )
    async with motor() as engine:
        with pytest.raises(QuoteMovedError) as caught:
            await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)
    assert "1000 bps" in str(caught.value)


async def test_tolera_la_deriva_dentro_del_margen() -> None:
    """Exigir identidad convertiría el botón en uno que casi nunca funciona."""
    fresco = OUT_RAW * 997 // 1000
    ruta(cuerpo=respuesta(**{"estimate.toAmount": str(fresco)}))
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)

    assert str(TokenAmount(fresco, 6, "USDC")) in tx.description


async def test_la_cotizacion_fresca_sin_importe_legible_se_para() -> None:
    ruta(cuerpo=respuesta(**{"estimate.toAmount": "no-es-un-numero"}))
    async with motor() as engine:
        with pytest.raises(SourceResponseError, match="sin importe de salida"):
            await engine.plan_bridge(_cotizar(), recipient=_DESTINATARIO)


# --------------------------------------------------------------------------- #
# Utilería
# --------------------------------------------------------------------------- #
def _cotizar(request: BridgeRequest | None = None) -> BridgeQuote:
    """Una cotización ya observada, con las cifras medidas.

    Se construye a mano y no cotizando, porque lo que se prueba al construir es
    que la cifra **mostrada** es la que se compara — y para eso tiene que ser
    distinta de la que devuelva la respuesta simulada.
    """
    peticion = request or solicitud()
    return BridgeQuote(
        engine_id=MANIFEST.engine_id,
        provider="AcrossV4",
        request=peticion,
        amount_out=TokenAmount(OUT_RAW, 6, "USDC"),
        amount_out_min=TokenAmount(OUT_RAW, 6, "USDC"),
        fee=TokenAmount(FEE_RAW, 6, "USDC"),
        fee_basis=Measurement.REPORTED,
        duration_seconds=1,
        observed_at=NOW,
    )
