"""Motor de puentes Relay: lo que se fija aquí y de dónde sale cada cosa.

**Este fichero no parte de una respuesta capturada, y se nota a propósito.** Los
otros motores de puente parten de un documento medido en vivo y lo modifican; aquí
el punto de partida es la forma que publica la documentación —`POST /quote/v2` con
`requestId`, `steps[]`, `fees` y `details`—, y está marcada como tal en el nombre:
`RESPUESTA_DOCUMENTADA`. Escribir como medición una forma que sólo se ha leído
sería el error que este repositorio evita en todas partes; y la clave de Relay no
se escribe en un fichero del repositorio ni aunque se tenga.

Lo que sí se midió es la tabla que decide a dónde va el dinero: **`DEPOSITORIES`
se rellenó el 2026-10-07 midiendo la API en vivo** —tres usuarios, tres importes y
dos destinos sobre cinco redes de origen, y siempre el mismo contrato—, y hay una
prueba que lo fija para que nadie edite la dirección a ojo. Esa tabla es el
contrato al que Relay manda los fondos desde cada red, y es lo que se contrasta
antes de firmar: aquí un destino equivocado no pierde dinero en una operación de
mercado — lo manda a otra parte y no vuelve.

Lo que se prueba entero es la **maquinaria** —con la tabla medida para las rutas
normales y con una tabla inyectada para las variantes—, y sobre todo los
**rechazos**, que es donde está el riesgo:

- **Un paso `signature`.** La documentación dice que los pasos son de dos
  clases, `transaction` y `signature`, y la segunda se firma fuera de la cadena.
  Este camino firma una transacción EVM: emitir la pata que sí sabe y parar
  dejaría fondos a medio cruzar, así que se rechaza la cotización entera.
- **Más de un paso que emitir.** `UnsignedTransaction` es **una** transacción. Se
  admite `approve` + depósito porque la aprobación sí se expresa —el ejecutor la
  construye a partir del gastador— y se rechaza cualquier otro reparto.
- **Una aprobación que no sea `approve(address,uint256)`.** Se lee el selector en
  vez de suponerlo: autorizar al contrato equivocado se firma, se emite y se paga
  el gas de descubrirlo.
- **Un destino distinto al medido.** Es la comprobación que separa un puente de
  un swap.
- **Una comisión en otro token.** Sumar lo que queda daría una parte presentada
  como el total, y la comisión es justo el número con el que se comparan rutas.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    NoQuotesError,
    QuoteMovedError,
    SourceResponseError,
    UnsupportedOperationError,
)
from amigocompora.domain.models import BridgeQuote, BridgeRequest, Measurement, Token
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import EngineKind
from amigocompora.engines.relay import engine as modulo
from amigocompora.engines.relay.engine import (
    BRIDGE_CHAINS,
    DEPOSITORIES,
    MANIFEST,
    NATIVE_CURRENCY,
    PROVIDER,
    QUOTE_URL,
    RelayEngine,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_POLYGON = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"

#: El depósito de Base **del ejemplo de la documentación**, no de una medición.
#: Vive aquí, y no en el motor, porque el motor ya tiene el suyo medido: éste se
#: inyecta para probar que la maquinaria funciona con cualquier tabla, y de paso
#: deja escrito que el ejemplo de la documentación no era la dirección de verdad.
DEPOSITO_BASE = "0xf70da97812cb96acdf810712aa562db8dfa3dbef"

#: El mismo contrato escrito en mayúsculas, para probar que el contraste no
#: confunde presentación con identidad.
DEPOSITO_BASE_EN_MAYUSCULAS = "0x" + DEPOSITO_BASE[2:].upper()

#: El depósito **de verdad**, medido el 2026-10-07 contra la API en vivo.
#:
#: No coincide con el del ejemplo de la documentación de arriba, y eso da la
#: razón al recelo que tuvo el motor mientras no había medición: el ejemplo
#: `0xf70d…dbef` no es el contrato al que Relay manda los fondos. Escribirlo
#: aquí, y no sólo en el motor, es lo que hace que editarlo a ojo rompa la suite.
DEPOSITO_MEDIDO = "0x4cD00E387622C35bDDB9b4c962C136462338BC31"

#: Las cifras del documento de prueba: sale menos de lo que entra porque el
#: puente cobra, y el mínimo garantizado es el suelo por debajo del estimado.
OUT_RAW = 994_000
MIN_RAW = 990_000
FEE_RAW = 7_000  # relayer 2.500 + relayerGas 1.500 + relayerService 3.000
DURACION = 20

_RECEPTOR = "0x2c887da24c6e6c939b0fe3adae50814b938b44f9"
_OTRO = "0x00000000000000000000000000000000deadbeef"


def _importe(cantidad: int, direccion: str, chain_id: int = 137) -> dict[str, Any]:
    """Un objeto de importe de Relay.

    `chain_id` importa: el motor exige que `currencyIn` y `currencyOut` declaren
    **las redes que se pidieron**, y una respuesta que dijera 137 en la entrada
    de una ruta que sale de Base no describe un puente. El valor por defecto es
    el de Polygon porque es el destino de las rutas de estas pruebas.
    """
    return {
        "currency": {
            "chainId": chain_id,
            "address": direccion,
            "symbol": "USDC",
            "decimals": 6,
        },
        "amount": str(cantidad),
        "amountFormatted": f"{cantidad / 1_000_000:.6f}",
        "amountUsd": f"{cantidad / 1_000_000:.6f}",
        "minimumAmount": str(MIN_RAW),
    }


def _comision(cantidad: int, direccion: str) -> dict[str, Any]:
    return {
        "currency": {"chainId": 8453, "address": direccion, "symbol": "USDC"},
        "amount": str(cantidad),
        "amountUsd": f"{cantidad / 1_000_000:.6f}",
    }


def _paso_deposito(*, destino: str = DEPOSITO_BASE) -> dict[str, Any]:
    return {
        "id": "deposit",
        "action": "Send funds to the Relay depository",
        "description": "Deposit funds to the Relay order",
        "kind": "transaction",
        "requestId": "0x" + "ab" * 32,
        "items": [
            {
                "status": "incomplete",
                "data": {
                    "from": _RECEPTOR,
                    "to": destino,
                    "data": "0x" + "cd" * 100,
                    "value": "0",
                    "chainId": 8453,
                    "gas": "250000",
                    "maxFeePerGas": "1000000",
                    "maxPriorityFeePerGas": "100000",
                },
                "check": {
                    "endpoint": "/intents/status/v3?requestId=0x" + "ab" * 32,
                    "method": "GET",
                },
            }
        ],
    }


def _paso_aprobacion(
    *, gastador: str = DEPOSITO_BASE, selector: str = "0x095ea7b3"
) -> dict[str, Any]:
    """El `approve(address,uint256)` que Relay pide antes de tomar el token."""
    calldata = selector + "0" * 24 + gastador.lower()[2:] + "0" * 64
    return {
        "id": "approve",
        "action": "Approve",
        "description": "Approve the token for the Relay depository",
        "kind": "transaction",
        "items": [
            {
                "status": "incomplete",
                "data": {
                    "from": _RECEPTOR,
                    "to": USDC_BASE,
                    "data": calldata,
                    "value": "0",
                    "chainId": 8453,
                    "gas": "60000",
                },
            }
        ],
    }


def respuesta_documentada() -> dict[str, Any]:
    """La forma que publica la documentación de Relay para `POST /quote/v2`.

    **No es una respuesta capturada.** Se llama «documentada» y no «medida» para
    que nadie la lea como lo otro: aquí no hubo ninguna petición que contestara
    esto. Lo que se prueba con ella es que el motor lee esa forma y que **se
    niega** ante las variantes peligrosas.
    """
    return {
        "requestId": "0x" + "ab" * 32,
        "steps": [_paso_deposito()],
        "fees": {
            "gas": _comision(8_000, USDC_BASE),
            "relayer": _comision(2_500, USDC_BASE),
            "relayerGas": _comision(1_500, USDC_BASE),
            "relayerService": _comision(3_000, USDC_BASE),
            "app": _comision(0, USDC_BASE),
        },
        "details": {
            "operation": "bridge",
            "sender": _RECEPTOR,
            "recipient": _RECEPTOR,
            # La entrada sale de Base y la salida llega a Polygon: es lo que el
            # motor comprueba ahora para saber que la ruta cruza de verdad.
            "currencyIn": _importe(1_000_000, USDC_BASE, chain_id=8453),
            "currencyOut": _importe(OUT_RAW, USDC_POLYGON, chain_id=137),
            "rate": "0.994",
            "timeEstimate": DURACION,
            "totalImpact": {"usd": "0.007", "percent": "0.70"},
            "swapImpact": {"usd": "0.000", "percent": "0.00"},
            "slippageTolerance": "50",
            "route": {
                "origin": {
                    "inputCurrency": {"address": USDC_BASE, "chainId": 8453},
                    "router": "0x" + "11" * 20,
                    "includedSwapSources": [],
                },
                "destination": {
                    "outputCurrency": {"address": USDC_POLYGON, "chainId": 137}
                },
            },
        },
        "protocol": {"v2": {"orderId": "0x" + "ef" * 32, "hubType": "relay"}},
    }


def respuesta(**cambios: Any) -> dict[str, Any]:
    """El documento de prueba con los campos que se le pidan cambiados.

    Se parte siempre de la misma forma y se toca por ruta con puntos, igual que
    en las pruebas de LI.FI: así cada escenario se lee como «lo mismo, con esto
    distinto». Un tramo numérico es un índice de lista.
    """
    cuerpo = respuesta_documentada()
    for ruta_campo, valor in cambios.items():
        actual: Any = cuerpo
        partes = ruta_campo.split(".")
        for parte in partes[:-1]:
            actual = actual[int(parte)] if isinstance(actual, list) else actual[parte]
        ultima = partes[-1]
        if isinstance(actual, list):
            actual[int(ultima)] = valor
        else:
            actual[ultima] = valor
    return cuerpo


@pytest.fixture(autouse=True)
def _sin_red() -> Iterator[None]:
    """Ninguna prueba de este módulo sale a internet, y ninguna puede olvidarse."""
    with respx.mock:
        yield


@pytest.fixture
def deposito_medido(monkeypatch: pytest.MonkeyPatch) -> str:
    """Sustituye la tabla medida por una de una sola red, para probar la maquinaria.

    Se parchean los dos globales y no el manifiesto: el manifiesto declara lo que
    se ha medido —cinco redes—, y eso no cambia porque una prueba necesite una
    tabla distinta. `quote_bridge` y `plan_bridge` leen el global en cada llamada,
    así que basta con esto.
    """
    monkeypatch.setattr(modulo, "DEPOSITORIES", {"base": DEPOSITO_BASE})
    monkeypatch.setattr(modulo, "BRIDGE_CHAINS", frozenset({"base"}))
    return DEPOSITO_BASE


def solicitud(*, origen: str = USDC_BASE, destino: str = USDC_POLYGON) -> BridgeRequest:
    return BridgeRequest(
        origin=Token("USDC", 6, "base", origen),
        destination=Token("USDC", 6, "polygon", destino),
        amount_in=TokenAmount(1_000_000, 6, "USDC"),
    )


def ruta(*, cuerpo: dict[str, Any] | None = None, status: int = 200) -> respx.Route:
    return respx.post(QUOTE_URL).mock(
        return_value=httpx.Response(
            status, json=cuerpo if cuerpo is not None else respuesta_documentada()
        )
    )


@asynccontextmanager
async def motor(**kwargs: Any) -> AsyncIterator[RelayEngine]:
    """Motor abierto y cerrado aunque la prueba falle, con el reloj parado."""
    engine = RelayEngine(
        api_key=kwargs.pop("api_key", "clave-de-prueba"),
        clock=FrozenClock(NOW),
        min_interval_seconds=0.0,
        **kwargs,
    )
    await engine.aopen()
    try:
        yield engine
    finally:
        await engine.aclose()


def _cotizar(request: BridgeRequest | None = None) -> BridgeQuote:
    """Una cotización ya observada con las cifras del documento de prueba.

    Se construye a mano porque lo que se prueba al construir es que la cifra
    **mostrada** es la que se compara: para eso tiene que ser un dato propio y no
    el que devuelva la respuesta simulada.
    """
    peticion = request or solicitud()
    return BridgeQuote(
        engine_id=MANIFEST.engine_id,
        provider="red de solvers",
        request=peticion,
        amount_out=TokenAmount(OUT_RAW, 6, "USDC"),
        amount_out_min=TokenAmount(MIN_RAW, 6, "USDC"),
        fee=TokenAmount(FEE_RAW, 6, "USDC"),
        fee_basis=Measurement.REPORTED,
        duration_seconds=DURACION,
        observed_at=NOW,
    )


# --------------------------------------------------------------------------- #
# El manifiesto, la clave y la tabla de destinos
# --------------------------------------------------------------------------- #
def test_el_manifiesto_ocupa_la_ranura_de_puentes() -> None:
    assert MANIFEST.kind is EngineKind.CROSS_CHAIN
    assert Capability.PREPARE_TX in MANIFEST.capabilities
    assert MANIFEST.bridge_priority > 0


def test_la_clave_es_obligatoria_al_contrario_que_en_lifi() -> None:
    """La documentación la exige en cada petición desde el 2026-10-02.

    Un motor sin clave no debe arrancar y fallar en cada consulta, sino no
    arrancar: si se pudiera crear sin ella, cada ruta pedida sería un error de
    red disfrazado de «no hay ruta».
    """
    assert MANIFEST.required_config == ("api_key",)
    with pytest.raises(SourceResponseError, match="clave de API"):
        RelayEngine(api_key="")


def test_el_proveedor_construye_con_clave() -> None:
    assert isinstance(PROVIDER.create({"api_key": "de-prueba"}), RelayEngine)


def test_la_tabla_de_destinos_lleva_lo_medido_y_no_lo_supuesto() -> None:
    """El valor medido está escrito dos veces a propósito.

    Antes esta prueba afirmaba `DEPOSITORIES == {}`, y era lo correcto mientras
    la tabla no se había medido: el único valor del que se tenía noticia venía de
    un **ejemplo** de la documentación, y un ejemplo no es una medición.

    Ya se midió —el 2026-10-07, contra la API en vivo: tres usuarios, tres
    importes y dos destinos por red, y siempre el mismo contrato—, así que lo que
    hay que proteger es lo contrario: que nadie edite la tabla a ojo. Cambiar la
    dirección en el motor sin cambiarla aquí rompe esta prueba, que es
    exactamente para lo que está.
    """
    assert DEPOSITORIES, "la tabla ya medida no puede volver a estar vacía"
    assert set(DEPOSITORIES.values()) == {DEPOSITO_MEDIDO}
    assert frozenset(DEPOSITORIES) == BRIDGE_CHAINS
    assert MANIFEST.bridge_chains == BRIDGE_CHAINS


# --------------------------------------------------------------------------- #
# Cotizar
# --------------------------------------------------------------------------- #
async def test_sin_tabla_no_se_cotiza_y_no_se_gasta_la_peticion() -> None:
    """Una red de origen sin depósito medido no se ofrece: se niega antes de salir.

    Enseñar una ruta que después no se puede firmar es peor que no enseñarla,
    porque el usuario la elige y se queda mirándola. El motor construye desde las
    cinco redes que se midieron; `bsc` no está entre ellas — Relay no la cubre —
    y ahí sigue valiendo el mismo razonamiento, ahora contra una red de verdad en
    vez de contra todas.
    """
    peticion = BridgeRequest(
        origin=Token("USDC", 6, "bsc", "0x" + "ee" * 20),
        destination=Token("USDC", 6, "polygon", USDC_POLYGON),
        amount_in=TokenAmount(1_000_000, 6, "USDC"),
    )
    route = ruta()
    async with motor() as engine:
        assert await engine.quote_bridge(peticion) == ()
        assert engine.expected_destination("bsc") is None
        with pytest.raises(UnsupportedOperationError, match="sólo cruza desde"):
            await engine.plan_bridge(_cotizar(peticion), recipient=_RECEPTOR)
    assert route.call_count == 0


async def test_lee_lo_que_la_respuesta_documentada_dice(deposito_medido: str) -> None:
    ruta()
    async with motor() as engine:
        (quote,) = await engine.quote_bridge(solicitud())

    assert quote.engine_id == MANIFEST.engine_id
    assert quote.amount_out == TokenAmount(OUT_RAW, 6, "USDC")
    assert quote.amount_out_min == TokenAmount(MIN_RAW, 6, "USDC")
    assert quote.duration_seconds == DURACION
    assert quote.observed_at == NOW
    assert quote.is_cross_chain


async def test_la_comision_suma_solo_lo_que_cobra_el_servicio(deposito_medido: str) -> None:
    """El `gas` queda fuera: es lo que cuesta emitir, no lo que cobra el puente.

    Contarlo inflaría la cifra con un concepto que no es de Relay, y la comisión
    es justo el número con el que se comparan rutas entre motores.
    """
    ruta()
    async with motor() as engine:
        (quote,) = await engine.quote_bridge(solicitud())

    assert quote.fee == TokenAmount(FEE_RAW, 6, "USDC")
    assert quote.fee_basis is Measurement.REPORTED


async def test_una_comision_en_otro_token_declara_lo_desconocido(deposito_medido: str) -> None:
    """Sumar USDC con otra cosa no da una cifra: da una lista mal sumada.

    Se para en la primera partida que venga en otro token y se dice que no se
    sabe. Una parte presentada como el total hace que la ruta parezca más barata
    de lo que es — y una ruta que parece barata se elige.
    """
    cuerpo = respuesta_documentada()
    cuerpo["fees"]["relayerGas"]["currency"]["address"] = "0x" + "e" * 40
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        (quote,) = await engine.quote_bridge(solicitud())

    assert quote.fee is None
    assert quote.fee_basis is None


async def test_sin_minimo_garantizado_no_se_ofrece_la_ruta(deposito_medido: str) -> None:
    """En un puente el suelo es la mitad del dato: no se rellena con el estimado.

    Poner el estimado en su lugar sería prometer lo que nadie promete, en la
    cifra que el usuario mira para decidir.
    """
    cuerpo = respuesta_documentada()
    del cuerpo["details"]["currencyOut"]["minimumAmount"]
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        assert await engine.quote_bridge(solicitud()) == ()


async def test_swap_si_se_cotiza_porque_es_lo_que_relay_contesta(deposito_medido: str) -> None:
    """La prueba que habría cazado el motor muerto.

    Antes había aquí una prueba que exigía lo contrario —que `swap` se
    rechazara—, escrita sobre la suposición de que `operation` distingue un
    puente de un intercambio. Medido el 2026-10-07 contra la API en vivo, Relay
    contesta `"swap"` en **todas** las rutas entre redes, incluida nativo→nativo,
    así que el motor rechazaba todas y no cotizaba nada por mucho que se
    activara. El OpenAPI de Relay enumera los valores y no define ninguno.

    Esta prueba fija el hecho medido: con `operation: "swap"` y las redes que se
    pidieron, la ruta **se cotiza**.
    """
    ruta(cuerpo=respuesta(**{"details.operation": "swap"}))
    async with motor() as engine:
        cotizaciones = await engine.quote_bridge(solicitud())

    assert len(cotizaciones) == 1
    assert cotizaciones[0].amount_out == TokenAmount(OUT_RAW, 6, "USDC")


@pytest.mark.parametrize(
    "cambio",
    [
        # La entrada dice salir de Polygon, y la ruta se pidió desde Base.
        {"details.currencyIn.currency.chainId": 137},
        # La salida dice llegar a Base, y se pidió que llegara a Polygon.
        {"details.currencyOut.currency.chainId": 8453},
        # Las dos puntas en la misma red: eso no cruza nada.
        {"details.currencyOut.currency.chainId": 8453, "details.currencyIn.currency.chainId": 8453},
    ],
)
async def test_una_ruta_que_no_cruza_de_verdad_no_se_compara_como_puente(
    deposito_medido: str, cambio: dict[str, Any]
) -> None:
    """El sustituto de la comprobación por `operation`, y más estricto.

    `operation` no dice si la ruta cruza; los `chainId` de las dos puntas sí. Se
    exige que sean los que se pidieron y que sean distintos entre sí, así que
    cualquier respuesta que describa otra cosa se descarta por lo que **dice**,
    no por cómo se llame la operación.
    """
    ruta(cuerpo=respuesta(**cambio))
    async with motor() as engine:
        assert await engine.quote_bridge(solicitud()) == ()


async def test_sin_ruta_la_fuente_contesta_400_y_no_es_un_fallo(deposito_medido: str) -> None:
    """La documentación publica un 400 con `errorCode`: «ese par no se puede
    cruzar», no un formato roto. Por eso se declara ausente en vez de abortar."""
    ruta(cuerpo={"errorCode": "NO_ROUTE", "message": "No route found"}, status=400)
    async with motor() as engine:
        assert await engine.quote_bridge(solicitud()) == ()


async def test_el_nativo_se_nombra_con_la_direccion_cero(deposito_medido: str) -> None:
    """A diferencia de LI.FI, aquí el centinela del nativo **sí** está publicado.

    Lo dice el ejemplo de la documentación —para cruzar ETH se usa la dirección
    cero—, y por eso aquí un nativo se puede cotizar sin esperar a medir nada.
    """
    route = ruta()
    nativo = BridgeRequest(
        origin=Token("ETH", 18, "base", None),
        destination=Token("USDC", 6, "polygon", USDC_POLYGON),
        amount_in=TokenAmount(10**15, 18, "ETH"),
    )
    async with motor() as engine:
        quote = await engine.quote_bridge(nativo)

    assert len(quote) == 1
    enviado = route.calls[-1].request.content.decode()
    assert NATIVE_CURRENCY in enviado
    assert "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" not in enviado


# --------------------------------------------------------------------------- #
# La petición que se envía
# --------------------------------------------------------------------------- #
async def test_la_peticion_lleva_las_dos_redes_los_dos_tokens_y_el_tipo(
    deposito_medido: str,
) -> None:
    route = ruta()
    async with motor() as engine:
        await engine.quote_bridge(solicitud())

    enviado = json.loads(route.calls[-1].request.content)
    assert enviado["originChainId"] == 8453
    assert enviado["destinationChainId"] == 137
    assert enviado["originCurrency"] == USDC_BASE
    assert enviado["destinationCurrency"] == USDC_POLYGON
    assert enviado["amount"] == "1000000"
    assert enviado["tradeType"] == "EXACT_INPUT"


async def test_la_clave_va_en_la_cabecera_y_no_en_el_cuerpo(deposito_medido: str) -> None:
    """La clave vive sólo en el backend: cabecera, nunca cuerpo.

    Un cuerpo viaja a los registros de la fuente y de ahí a donde sea; una
    cabecera es donde la API la espera. Y no aparece en el cuerpo que se enseña.
    """
    route = ruta()
    async with motor(api_key="clave-secreta-de-prueba") as engine:
        await engine.quote_bridge(solicitud())

    peticion = route.calls[-1].request
    assert peticion.headers["x-api-key"] == "clave-secreta-de-prueba"
    assert b"clave-secreta-de-prueba" not in peticion.content


async def test_cotizar_usa_un_centinela_y_construir_el_receptor_real(
    deposito_medido: str,
) -> None:
    """El receptor entra en el calldata, así que las dos peticiones no se pisan."""
    route = ruta()
    async with motor() as engine:
        await engine.quote_bridge(solicitud())
        await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)

    assert route.call_count == 2
    assert json.loads(route.calls[0].request.content)["user"] == modulo.QUOTE_USER
    assert json.loads(route.calls[1].request.content)["user"] == _RECEPTOR

# --------------------------------------------------------------------------- #
# Los rechazos: es donde está el riesgo
# --------------------------------------------------------------------------- #
async def test_un_paso_de_firma_fuera_de_la_cadena_se_rechaza_entero(
    deposito_medido: str,
) -> None:
    """La documentación admite pasos `signature`, y éste no los sabe emitir.

    Emitir la parte que sí sabemos y parar dejaría fondos a medio cruzar, que es
    peor que no empezar. Se rechaza la cotización y se nombra el paso.
    """
    cuerpo = respuesta_documentada()
    cuerpo["steps"].append(
        {
            "id": "authorize",
            "kind": "signature",
            "items": [{"data": {"chainId": 8453}}],
        }
    )
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        with pytest.raises(UnsupportedOperationError, match="authorize"):
            await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)


async def test_una_ruta_que_solo_trae_un_paso_de_firma_se_rechaza(
    deposito_medido: str,
) -> None:
    """La firma **sola**, que es la que se cuela por el hueco de contar pasos.

    La prueba de arriba mete la firma al lado de un depósito, y ahí la caza el
    guardián de «más de un paso que emitir» aunque el rechazo del `signature`
    desaparezca: se ve que falla por el tipo de error, no por el motivo. Ésta es
    la otra mitad, y es la que de verdad importa: con una firma y nada más queda
    **un solo** paso, así que aquel guardián no tiene nada que objetar y el paso
    llegaría entero hasta la firma.

    El paso va con un `data` completo a propósito. Si estuviera a medias, el
    motor se negaría por el calldata y la prueba pasaría por el motivo
    equivocado: lo que se quiere demostrar es que se niega **por ser una firma**,
    no porque el payload estuviera mal formado.
    """
    paso = _paso_deposito(destino=DEPOSITO_BASE)
    paso["id"] = "authorize"
    paso["kind"] = "signature"
    cuerpo = respuesta_documentada()
    cuerpo["steps"] = [paso]
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        with pytest.raises(UnsupportedOperationError, match="authorize"):
            await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)


async def test_dos_pasos_que_emitir_y_ninguno_es_una_aprobacion_se_rechazan(
    deposito_medido: str,
) -> None:
    """`UnsignedTransaction` es **una** transacción, y aquí llegarían dos.

    Se nombran los pasos que llegaron para que quien lo lea sepa qué contestó la
    fuente, en vez de un «no se pudo» que no dice nada.
    """
    cuerpo = respuesta_documentada()
    otro = _paso_deposito(destino=DEPOSITO_BASE)
    otro["id"] = "swap"
    cuerpo["steps"].append(otro)
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        with pytest.raises(SourceResponseError, match="swap"):
            await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)


async def test_una_aprobacion_que_no_es_approve_se_rechaza_nombrando_el_selector(
    deposito_medido: str,
) -> None:
    """Permit2 y EIP-3009 también son permisos, y no son éste.

    Se lee el selector en vez de suponerlo porque autorizar al contrato
    equivocado se firma, se emite, y se paga el gas de descubrirlo.
    """
    cuerpo = respuesta_documentada()
    cuerpo["steps"].insert(0, _paso_aprobacion(selector="0x87517c45"))
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        with pytest.raises(UnsupportedOperationError, match="0x87517c45"):
            await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)


async def test_una_aprobacion_para_un_token_nativo_se_rechaza(deposito_medido: str) -> None:
    """El nativo no se autoriza: no hay contrato al que autorizar.

    Que la fuente lo pida significa que la respuesta se contradice, y una
    respuesta que se contradice no se firma.
    """
    cuerpo = respuesta_documentada()
    cuerpo["steps"].insert(0, _paso_aprobacion())
    ruta(cuerpo=cuerpo)
    nativo = BridgeRequest(
        origin=Token("ETH", 18, "base", None),
        destination=Token("USDC", 6, "polygon", USDC_POLYGON),
        amount_in=TokenAmount(10**15, 18, "ETH"),
    )
    async with motor() as engine:
        with pytest.raises(SourceResponseError, match="nativo"):
            await engine.plan_bridge(_cotizar(nativo), recipient=_RECEPTOR)


async def test_una_aprobacion_dirigida_a_otro_contrato_se_rechaza(deposito_medido: str) -> None:
    """El permiso se concede al token, así que va al contrato del token de origen.

    Un `approve` con el mismo selector sobre otro contrato autoriza lo que ese
    otro contrato entienda por autorizar, y el usuario no puede verlo: el
    selector es idéntico.
    """
    cuerpo = respuesta_documentada()
    aprobacion = _paso_aprobacion()
    aprobacion["items"][0]["data"]["to"] = _OTRO
    cuerpo["steps"].insert(0, aprobacion)
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        with pytest.raises(SourceResponseError, match="token de origen"):
            await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)


async def test_un_destino_distinto_al_medido_no_se_firma(deposito_medido: str) -> None:
    """Es la comprobación que separa un puente de un swap.

    Un `to` equivocado no pierde dinero en una operación de mercado: lo manda a
    otra parte y no vuelve. Por eso se contrasta **antes** de leer nada más — la
    prueba manda además un calldata inválido, y si el orden fuera el otro el
    error sería el del calldata.
    """
    ruta(
        cuerpo=respuesta(
            **{
                "steps.0.items.0.data.to": _OTRO,
                "steps.0.items.0.data.data": "no-hex",
            }
        )
    )
    async with motor() as engine:
        with pytest.raises(SourceResponseError, match="contrato medido para esa red"):
            await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)


async def test_el_contraste_admite_las_mayusculas_de_la_fuente(deposito_medido: str) -> None:
    """El checksum es presentación, no identidad: la dirección es la misma."""
    ruta(cuerpo=respuesta(**{"steps.0.items.0.data.to": DEPOSITO_BASE_EN_MAYUSCULAS}))
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)

    assert tx.to_address == DEPOSITO_BASE


async def test_aborta_si_lo_que_se_recibe_se_movio(deposito_medido: str) -> None:
    """Un puente tarda minutos: lo que se enseñó y lo que se firma pueden estar
    lejos, y eso hay que pararlo antes de firmar."""
    ruta(cuerpo=respuesta(**{"details.currencyOut.amount": str(OUT_RAW * 9 // 10)}))
    async with motor() as engine:
        with pytest.raises(QuoteMovedError) as capturado:
            await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)

    assert "1000 bps" in str(capturado.value)


async def test_tolera_la_deriva_dentro_del_margen(deposito_medido: str) -> None:
    """Exigir identidad convertiría el botón en uno que casi nunca funciona."""
    fresco = OUT_RAW * 997 // 1000
    ruta(cuerpo=respuesta(**{"details.currencyOut.amount": str(fresco)}))
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)

    assert str(TokenAmount(fresco, 6, "USDC")) in tx.description


async def test_sin_ruta_no_se_sigue_con_la_cotizacion_vieja(deposito_medido: str) -> None:
    ruta(cuerpo={"errorCode": "NO_ROUTE"}, status=400)
    async with motor() as engine:
        with pytest.raises(NoQuotesError, match="ya no tiene ruta"):
            await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)


async def test_un_calldata_que_no_es_hexadecimal_no_se_firma(deposito_medido: str) -> None:
    ruta(cuerpo=respuesta(**{"steps.0.items.0.data.data": "no-hex"}))
    async with motor() as engine:
        with pytest.raises(SourceResponseError, match="hexadecimal"):
            await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)


async def test_un_valor_ilegible_no_se_sustituye_por_cero(deposito_medido: str) -> None:
    """Poner cero sería decir «no mueve nativo» cuando la fuente no lo dijo.

    En un puente desde un token nativo ese cero significaría firmar una
    transacción que no lleva el dinero.
    """
    ruta(cuerpo=respuesta(**{"steps.0.items.0.data.value": "no-es-un-numero"}))
    async with motor() as engine:
        with pytest.raises(SourceResponseError, match="value"):
            await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)


# --------------------------------------------------------------------------- #
# Construir
# --------------------------------------------------------------------------- #
async def test_construye_el_payload_del_origen(deposito_medido: str) -> None:
    ruta()
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)

    assert tx.chain_id == 8453
    assert tx.to_address == DEPOSITO_BASE
    assert tx.calldata == "0x" + "cd" * 100
    assert tx.value == TokenAmount(0, 18, "ETH")
    assert tx.gas_limit == 250_000


async def test_la_aprobacion_va_al_gastador_del_propio_calldata(deposito_medido: str) -> None:
    """No se supone que sea el depósito: se lee de la dirección que autoriza.

    Es el mismo contrato en el ejemplo de la documentación, y aun así se lee: el
    día que no coincidan, la diferencia tiene que salir de los datos y no de una
    suposición sobre cómo los pone la fuente.
    """
    cuerpo = respuesta_documentada()
    cuerpo["steps"].insert(0, _paso_aprobacion(gastador=DEPOSITO_BASE))
    ruta(cuerpo=cuerpo)
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)

    assert tx.approval is not None
    assert tx.approval.spender == DEPOSITO_BASE
    assert not tx.approval.is_chained


async def test_el_valor_se_lee_tambien_en_hexadecimal(deposito_medido: str) -> None:
    """La fuente publica los enteros como cadenas, y no siempre en base diez."""
    ruta(cuerpo=respuesta(**{"steps.0.items.0.data.value": "0x2386f26fc10000"}))
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)

    assert tx.value.raw == 10**16


async def test_la_descripcion_nombra_el_destino_medido(deposito_medido: str) -> None:
    ruta()
    async with motor() as engine:
        tx = await engine.plan_bridge(_cotizar(), recipient=_RECEPTOR)

    assert DEPOSITO_BASE in tx.description
    assert _RECEPTOR in tx.description
    assert str(TokenAmount(MIN_RAW, 6, "USDC")) in tx.description


def test_el_destino_esperado_sale_de_la_tabla(deposito_medido: str) -> None:
    """Lo que contesta por red de **origen**: una red sin medir no tiene destino."""
    engine = RelayEngine(api_key="de-prueba")
    assert engine.expected_destination("base") == DEPOSITO_BASE
    assert engine.expected_destination("polygon") is None
