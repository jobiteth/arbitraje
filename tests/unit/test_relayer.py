"""El cliente del relayer: firmas puras y peticiones simuladas, sin red real.

Lo que se fija aquí es lo que el relayer rechazaría o, peor, aceptaría por error:
la firma EIP-712 de un lote (contra un vector producido con viem), la firma HMAC
de la Builder key (recalculada aquí por separado) y el cuerpo exacto de cada
petición. Ninguna prueba envía nada al relayer real.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest
import respx
from eth_utils.address import to_checksum_address

from amigocompora.domain.errors import ExecutionError, SourceResponseError, SourceUnavailableError
from amigocompora.engines.polymarket.deposit_wallet import FACTORY
from amigocompora.engines.polymarket.relayer import (
    CREATE_METADATA,
    ROOT,
    TERMINAL_STATES,
    BuilderCredentials,
    Call,
    RelayerClient,
    RelayerKey,
    builder_signature,
    sign_batch,
)

CLAVE_EOA = "0x" + "11" * 32
OWNER = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
WALLET = "0x8154deDBDAAA46c07453E47b5b6C0Ff60B8542b9"
PROXY = "0x7d7eff9a4AA45A1B798C3f1EEF2957Da9146a16E"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

#: Firma de lote producida con viem para la misma clave, wallet, nonce y llamada.
FIRMA_LOTE_VIEM = (
    "0xe093d4b36f34361e85ab1cacc26ee5713f73fde57649d0e6ddb2e4dbeeea0968"
    "629a016c5129027babf20deb618b9bd341e2c913e6dccb159f16d395e10044901c"
)

PUBLICA = "clave-publica-builder"
SECRETO_BYTES = b"secreto-de-prueba-0123456789"
SECRETO = base64.urlsafe_b64encode(SECRETO_BYTES).decode("utf-8")
FRASE = "frase-de-prueba"
CLAVE_RELAYER = "clave-relayer-de-prueba"

CREDENCIALES = BuilderCredentials(api_key=PUBLICA, secret=SECRETO, passphrase=FRASE)


def _cliente() -> RelayerClient:
    return RelayerClient(builder=CREDENCIALES, timeout_seconds=5)


def _cliente_con_relayer() -> RelayerClient:
    return RelayerClient(
        builder=CREDENCIALES,
        relayer=RelayerKey(api_key=CLAVE_RELAYER, address=OWNER),
        timeout_seconds=5,
    )


# --------------------------------------------------------------------------- #
# Firma del lote
# --------------------------------------------------------------------------- #
def test_la_firma_del_lote_coincide_con_el_vector_de_viem() -> None:
    firma = sign_batch(
        CLAVE_EOA,
        chain_id=137,
        wallet=WALLET,
        nonce=7,
        deadline=1800000000,
        calls=[Call(target=USDC_E, value=0, data="0x095ea7b3")],
    )
    assert firma == FIRMA_LOTE_VIEM


def test_la_firma_del_lote_cambia_si_cambia_el_nonce() -> None:
    """Un lote firmado con un nonce no sirve para otro: la firma lo ata."""
    llamadas = [Call(target=USDC_E, value=0, data="0x095ea7b3")]
    con_nonce_7 = sign_batch(
        CLAVE_EOA, chain_id=137, wallet=WALLET, nonce=7, deadline=1800000000, calls=llamadas
    )
    con_nonce_8 = sign_batch(
        CLAVE_EOA, chain_id=137, wallet=WALLET, nonce=8, deadline=1800000000, calls=llamadas
    )
    assert con_nonce_7 != con_nonce_8


# --------------------------------------------------------------------------- #
# Firma HMAC de la Builder key
# --------------------------------------------------------------------------- #
def test_la_firma_hmac_coincide_con_un_calculo_independiente() -> None:
    mensaje = b'1700000000POST/submit{"a":1}'
    esperada = base64.urlsafe_b64encode(
        hmac.new(SECRETO_BYTES, mensaje, hashlib.sha256).digest()
    ).decode("utf-8")
    obtenida = builder_signature(
        SECRETO, method="post", path="/submit", body='{"a":1}', timestamp=1700000000
    )
    assert obtenida == esperada


def test_un_secreto_que_no_es_base64_no_firma() -> None:
    with pytest.raises(ExecutionError, match="base64"):
        builder_signature("no base64 !!!", method="GET", path="/x", body="", timestamp=1)


def test_las_cabeceras_llevan_la_builder_key_y_la_marca() -> None:
    cabeceras = _cliente().auth_headers("GET", "/x", builder_only=True, timestamp=1700000000)
    assert cabeceras["POLY_BUILDER_API_KEY"] == PUBLICA
    assert cabeceras["POLY_BUILDER_PASSPHRASE"] == FRASE
    assert cabeceras["POLY_BUILDER_TIMESTAMP"] == "1700000000"
    assert cabeceras["POLY_BUILDER_SIGNATURE"] == builder_signature(
        SECRETO, method="GET", path="/x", body="", timestamp=1700000000
    )


# --------------------------------------------------------------------------- #
# Peticiones
# --------------------------------------------------------------------------- #
@respx.mock
async def test_crear_la_wallet_envia_el_cuerpo_exacto_y_se_firma_con_la_builder_key() -> None:
    ruta = respx.post(f"{ROOT}/submit").mock(
        return_value=httpx.Response(200, json={"transactionID": "tx-1", "state": "STATE_NEW"})
    )
    cliente = _cliente()
    try:
        resultado = await cliente.submit_wallet_create(OWNER)
    finally:
        await cliente.aclose()

    peticion = ruta.calls.last.request
    cuerpo = peticion.content.decode("utf-8")
    assert json.loads(cuerpo) == {
        "type": "WALLET-CREATE",
        "from": to_checksum_address(OWNER),
        "to": FACTORY,
        "metadata": CREATE_METADATA,
    }
    assert peticion.headers["POLY_BUILDER_API_KEY"] == PUBLICA
    marca = int(peticion.headers["POLY_BUILDER_TIMESTAMP"])
    assert peticion.headers["POLY_BUILDER_SIGNATURE"] == builder_signature(
        SECRETO, method="POST", path="/submit", body=cuerpo, timestamp=marca
    )
    assert resultado.transaction_id == "tx-1"
    assert resultado.state == "STATE_NEW"


@respx.mock
async def test_el_lote_envia_nonce_firma_y_llamadas() -> None:
    ruta = respx.post(f"{ROOT}/submit").mock(
        return_value=httpx.Response(200, json={"transactionID": "tx-2", "state": "STATE_NEW"})
    )
    cliente = _cliente()
    try:
        await cliente.submit_wallet_batch(
            owner=OWNER,
            wallet=WALLET,
            nonce=7,
            deadline=1800000000,
            calls=[Call(target=USDC_E, value=0, data="0x095ea7b3")],
            signature=FIRMA_LOTE_VIEM,
        )
    finally:
        await cliente.aclose()

    cuerpo = json.loads(ruta.calls.last.request.content)
    assert cuerpo["type"] == "WALLET"
    assert cuerpo["nonce"] == "7"
    assert cuerpo["signature"] == FIRMA_LOTE_VIEM
    assert cuerpo["depositWalletParams"]["depositWallet"] == to_checksum_address(WALLET)
    assert cuerpo["depositWalletParams"]["calls"] == [
        {"target": to_checksum_address(USDC_E), "value": "0", "data": "0x095ea7b3"}
    ]


@respx.mock
async def test_el_nonce_se_lee_del_relayer() -> None:
    respx.route(method="GET", path="/v1/account/transactions/params").mock(
        return_value=httpx.Response(200, json={"nonce": "7"})
    )
    cliente = _cliente()
    try:
        assert await cliente.wallet_nonce(OWNER) == 7
    finally:
        await cliente.aclose()


@respx.mock
async def test_una_respuesta_sin_nonce_no_se_firma() -> None:
    respx.route(method="GET", path="/v1/account/transactions/params").mock(
        return_value=httpx.Response(200, json={})
    )
    cliente = _cliente()
    try:
        with pytest.raises(SourceResponseError, match="nonce"):
            await cliente.wallet_nonce(OWNER)
    finally:
        await cliente.aclose()


@respx.mock
async def test_una_wallet_confirmada_devuelve_su_direccion() -> None:
    respx.route(method="GET", path="/transaction").mock(
        return_value=httpx.Response(
            200, json={"state": "STATE_CONFIRMED", "proxyAddress": PROXY}
        )
    )
    cliente = _cliente()
    try:
        estado = await cliente.wallet_create_status("tx-1")
    finally:
        await cliente.aclose()
    assert estado.state == "STATE_CONFIRMED"
    assert estado.proxy_address == to_checksum_address(PROXY)
    assert estado.is_terminal


@respx.mock
async def test_la_consulta_de_estado_acepta_la_lista_que_devuelve_el_relayer() -> None:
    """`/transaction?id=…` contesta una **lista** de fichas, no el objeto suelto.

    Es una forma medida contra el relayer: tratarla como si fuera un mapa hacía
    que una wallet ya confirmada se leyera como «respuesta inesperada».
    """
    respx.route(method="GET", path="/transaction").mock(
        return_value=httpx.Response(
            200,
            json=[{"state": "STATE_CONFIRMED", "proxyAddress": PROXY, "transactionID": "tx-1"}],
        )
    )
    cliente = _cliente()
    try:
        estado = await cliente.wallet_create_status("tx-1")
    finally:
        await cliente.aclose()
    assert estado.state == "STATE_CONFIRMED"
    assert estado.proxy_address == to_checksum_address(PROXY)


@respx.mock
async def test_en_una_lista_de_varias_fichas_se_elige_la_del_identificador() -> None:
    """Con varias fichas, la buena es la que lleva el identificador pedido."""
    respx.route(method="GET", path="/transaction").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"state": "STATE_FAILED", "transactionID": "otra"},
                {"state": "STATE_CONFIRMED", "transactionID": "tx-1"},
            ],
        )
    )
    cliente = _cliente()
    try:
        estado = await cliente.wallet_create_status("tx-1")
    finally:
        await cliente.aclose()
    assert estado.state == "STATE_CONFIRMED"


@respx.mock
async def test_una_lista_sin_la_ficha_pedida_no_se_lee_como_estado() -> None:
    respx.route(method="GET", path="/transaction").mock(
        return_value=httpx.Response(
            200, json=[{"state": "STATE_CONFIRMED", "transactionID": "otra"}]
        )
    )
    cliente = _cliente()
    try:
        with pytest.raises(SourceResponseError, match="inesperada"):
            await cliente.wallet_create_status("tx-1")
    finally:
        await cliente.aclose()


@respx.mock
async def test_un_estado_en_curso_no_es_terminal() -> None:
    respx.route(method="GET", path="/v1/account/transactions/tx-3").mock(
        return_value=httpx.Response(200, json={"state": "STATE_EXECUTED"})
    )
    cliente = _cliente()
    try:
        estado = await cliente.batch_status("tx-3")
    finally:
        await cliente.aclose()
    assert not estado.is_terminal
    assert estado.proxy_address is None


def test_los_estados_terminales_son_los_documentados() -> None:
    assert frozenset({"STATE_CONFIRMED", "STATE_FAILED", "STATE_INVALID"}) == TERMINAL_STATES


@respx.mock
async def test_un_400_es_respuesta_inesperada_y_no_se_reintenta() -> None:
    respx.post(f"{ROOT}/submit").mock(
        return_value=httpx.Response(400, json={"error": "maker address not allowed"})
    )
    cliente = _cliente()
    try:
        with pytest.raises(SourceResponseError, match="400"):
            await cliente.submit_wallet_create(OWNER)
    finally:
        await cliente.aclose()


@respx.mock
async def test_un_500_es_fuente_no_disponible() -> None:
    respx.post(f"{ROOT}/submit").mock(return_value=httpx.Response(500, text="caído"))
    cliente = _cliente()
    try:
        with pytest.raises(SourceUnavailableError):
            await cliente.submit_wallet_create(OWNER)
    finally:
        await cliente.aclose()


def test_un_lote_con_relayer_key_usa_sus_cabeceras_y_no_las_de_builder() -> None:
    cabeceras = _cliente_con_relayer().auth_headers("POST", "/submit", body="{}")
    assert cabeceras == {
        "RELAYER_API_KEY": CLAVE_RELAYER,
        "RELAYER_API_KEY_ADDRESS": OWNER,
    }


#: Un dueño que no es el de la Relayer key: es lo que se midió contra el
#: relayer real —«from 0x… does not match auth 0x…»— y lo que aquí se evita.
OTRO = "0xc2bdD32C9b5b1b6e2E6e0E6B7B8a1f0E1d2C3b4A"


def test_con_el_dueno_de_la_relayer_key_sus_cabeceras_son_las_que_firman() -> None:
    cabeceras = _cliente_con_relayer().auth_headers("POST", "/submit", body="{}", owner=OWNER)
    assert cabeceras["RELAYER_API_KEY"] == CLAVE_RELAYER


def test_con_otro_dueno_la_relayer_key_no_se_usa_sino_la_builder() -> None:
    """La Relayer key está atada a su dirección: usarla por otro dueño se rechaza.

    Se usa la comparación sin distinguir mayúsculas porque la dirección viaja
    con suma de comprobación y la comparación no puede depender de eso.
    """
    cabeceras = _cliente_con_relayer().auth_headers(
        "POST", "/submit", body="{}", owner=OTRO.lower()
    )
    assert "RELAYER_API_KEY" not in cabeceras
    assert cabeceras["POLY_BUILDER_API_KEY"] == PUBLICA


def test_sin_builder_y_con_otro_dueno_no_hay_credencial_que_valga() -> None:
    """Decirlo con un error propio en vez de mandar algo que se va a rechazar."""
    cliente = RelayerClient(relayer=RelayerKey(api_key=CLAVE_RELAYER, address=OWNER))
    with pytest.raises(ExecutionError, match="otra dirección"):
        cliente.auth_headers("POST", "/submit", body="{}", owner=OTRO)


@respx.mock
async def test_el_lote_de_otro_dueno_se_firma_con_la_builder_key_de_punta_a_punta() -> None:
    """La regla no se queda en `auth_headers`: el envío la aplica."""
    ruta = respx.post(f"{ROOT}/submit").mock(
        return_value=httpx.Response(200, json={"transactionID": "tx-9", "state": "STATE_NEW"})
    )
    cliente = _cliente_con_relayer()
    try:
        await cliente.submit_wallet_batch(
            owner=OTRO,
            wallet=WALLET,
            nonce=1,
            deadline=1800000000,
            calls=[Call(target=USDC_E, value=0, data="0x095ea7b3")],
            signature=FIRMA_LOTE_VIEM,
        )
    finally:
        await cliente.aclose()

    peticion = ruta.calls.last.request
    assert peticion.headers["POLY_BUILDER_API_KEY"] == PUBLICA
    assert "RELAYER_API_KEY" not in peticion.headers


def test_crear_la_wallet_no_se_firma_con_la_relayer_key() -> None:
    cliente = RelayerClient(relayer=RelayerKey(api_key=CLAVE_RELAYER, address=OWNER))
    with pytest.raises(ExecutionError, match="Builder key"):
        cliente.auth_headers("POST", "/submit", body="{}", builder_only=True)


def test_sin_ninguna_credencial_el_relayer_no_arranca() -> None:
    with pytest.raises(ExecutionError, match="Builder key o la Relayer key"):
        RelayerClient()


def test_la_representacion_no_muestra_el_secreto_ni_la_frase() -> None:
    texto = repr(CREDENCIALES)
    assert SECRETO not in texto
    assert FRASE not in texto


def test_la_representacion_de_la_relayer_key_no_muestra_la_clave() -> None:
    texto = repr(RelayerKey(api_key=CLAVE_RELAYER, address=OWNER))
    assert CLAVE_RELAYER not in texto
    assert OWNER in texto
