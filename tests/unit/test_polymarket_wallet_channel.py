"""El canal de la deposit wallet por dentro del motor: lotes firmados y esperas.

Lo que se fija aquí es lo que el motor pone en la red antes de que exista una
orden —el despliegue de la wallet y los permisos por el relayer—, con el relayer
simulado. Son las dos operaciones que se pagan una vez y sobre las que después se
apoya cada orden:

1. **El despliegue se espera hasta la confirmación**, y lo que el relayer
   confirma se contrasta con la dirección **derivada**: si no coinciden, se
   corta en vez de operar contra una wallet que no es la que se calculó.
2. **El lote se firma con el nonce del relayer y con la wallet derivada**, y
   también se espera a que confirme: devolver el identificador antes dejaría la
   orden siguiente operando contra un permiso que todavía no existe.

Ninguna prueba envía nada al relayer real: el relayer es simulado y la clave que
firma es una de desarrollo, publicada y sin fondos.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.models import Token
from amigocompora.domain.protocols import WalletChannelCredentials
from amigocompora.engines.polymarket.engine import PolymarketEngine
from amigocompora.engines.polymarket.relayer import ROOT, Call, sign_batch
from amigocompora.infra.evm.broadcast import build_approve_calldata

#: Cuenta 0 de Hardhat: publicada y sin fondos en ninguna red real.
CLAVE = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

#: Dueño del canal y su wallet derivada, del vector fijado en `test_deposit_wallet`.
OWNER = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
WALLET = "0x8154deDBDAAA46c07453E47b5b6C0Ff60B8542b9"
OTRA = "0x2222222222222222222222222222222222222222"

USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"

MAXIMO_UINT256 = (1 << 256) - 1

AHORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

#: Builder key de prueba: el secreto va en base64, como lo entrega Polymarket,
#: y la frase de paso en una constante —un literal con forma de credencial en la
#: llamada al constructor lo señala el linter, con razón.
SECRETO = "c2VjcmV0by1kZS1wdWViYS0wMTIzNDU2Nzg5"
FRASE = "frase-de-prueba"
CREDENCIALES = WalletChannelCredentials(
    builder_api_key="builder-publica",
    builder_secret=SECRETO,
    builder_passphrase=FRASE,
    relayer_api_key="relayer-clave",
    relayer_address=OWNER,
)


def _motor(*, intentos: int = 2) -> PolymarketEngine:
    """El motor de producción, con reloj congelado y sin espera entre sondeos."""
    return PolymarketEngine(
        clock=FrozenClock(AHORA),
        relayer_poll_seconds=0.0,
        relayer_poll_attempts=intentos,
    )


def _ficha(*, id_: str, estado: str, proxy: str | None = None) -> dict[str, object]:
    ficha: dict[str, object] = {"transactionID": id_, "state": estado}
    if proxy is not None:
        ficha["proxyAddress"] = proxy
    return ficha


# --------------------------------------------------------------------------- #
# El despliegue
# --------------------------------------------------------------------------- #
@respx.mock
async def test_el_despliegue_se_espera_hasta_la_confirmacion() -> None:
    """A `STATE_NEW` no se devuelve nada: la operación siguiente usa esa wallet.

    Lo que se comprueba aquí es la **espera**: el primer estado llega en curso y
    el camino vuelve a preguntar en vez de dar por desplegada una dirección que
    todavía no lo está. Y el cuerpo que viaja es el del despliegue, autenticado
    con la Builder key —la única que ese endpoint admite.
    """
    envio = respx.post(f"{ROOT}/submit").mock(
        return_value=httpx.Response(200, json={"transactionID": "tx-wallet", "state": "STATE_NEW"})
    )
    estado = respx.route(method="GET", path="/transaction").mock(
        side_effect=[
            httpx.Response(200, json=[_ficha(id_="tx-wallet", estado="STATE_NEW")]),
            httpx.Response(
                200, json=[_ficha(id_="tx-wallet", estado="STATE_CONFIRMED", proxy=WALLET)]
            ),
        ]
    )

    wallet = await _motor().deploy_settlement_wallet(owner=OWNER, credentials=CREDENCIALES)

    assert wallet == WALLET
    assert estado.call_count == 2, "se volvió a preguntar en vez de darla por hecha"
    peticion = envio.calls[-1].request
    assert peticion.headers["POLY_BUILDER_API_KEY"] == "builder-publica"
    assert b'"type":"WALLET-CREATE"' in peticion.content


@respx.mock
async def test_una_direccion_confirmada_distinta_de_la_derivada_corta() -> None:
    """El relayer es la fuente de verdad, y una discrepancia es un destino errado.

    Operar con la dirección calculada mientras el relayer confirmó otra sería
    mandar los fondos a un sitio que nadie eligió: se prefiere no operar.
    """
    respx.post(f"{ROOT}/submit").mock(
        return_value=httpx.Response(200, json={"transactionID": "tx-wallet", "state": "STATE_NEW"})
    )
    respx.route(method="GET", path="/transaction").mock(
        return_value=httpx.Response(
            200, json=[_ficha(id_="tx-wallet", estado="STATE_CONFIRMED", proxy=OTRA)]
        )
    )

    with pytest.raises(ExecutionError, match="no coincide"):
        await _motor().deploy_settlement_wallet(owner=OWNER, credentials=CREDENCIALES)


# --------------------------------------------------------------------------- #
# El lote
# --------------------------------------------------------------------------- #
@respx.mock
async def test_el_lote_lleva_el_nonce_del_relayer_la_wallet_y_la_llamada_exacta() -> None:
    """El permiso del colateral, con lo que el relayer exige: el máximo.

    La firma se recalcula aquí con los mismos datos del cuerpo —nonce, wallet,
    plazo y llamada—: si el motor firmara otros, la del cuerpo y la calculada no
    coincidirían, y un lote firmado por otros datos lo rechaza el relayer o,
    peor, ejecuta otra cosa.
    """
    respx.route(method="GET", path="/v1/account/transactions/params").mock(
        return_value=httpx.Response(200, json={"nonce": "7"})
    )
    envio = respx.post(f"{ROOT}/submit").mock(
        return_value=httpx.Response(200, json={"transactionID": "tx-lote", "state": "STATE_NEW"})
    )
    estado = respx.route(method="GET", path="/v1/account/transactions/tx-lote").mock(
        side_effect=[
            httpx.Response(200, json={"state": "STATE_NEW"}),
            httpx.Response(200, json={"state": "STATE_CONFIRMED"}),
        ]
    )
    colateral = Token(symbol="USDC", decimals=6, chain="polygon", address=USDC_E)

    enviado = await _motor().approve_wallet_collateral(
        owner=OWNER,
        collateral=colateral,
        spender=EXCHANGE_V2,
        private_key=CLAVE,
        credentials=CREDENCIALES,
    )

    assert enviado == "tx-lote"
    assert estado.call_count == 2
    cuerpo = json.loads(envio.calls[-1].request.content)
    assert cuerpo["type"] == "WALLET"
    assert cuerpo["from"] == OWNER
    assert cuerpo["nonce"] == "7"
    datos = cuerpo["depositWalletParams"]
    assert datos["depositWallet"] == WALLET
    plazo = int(AHORA.timestamp()) + 1800
    assert datos["deadline"] == str(plazo)
    # La llamada es el permiso del colateral al recinto, por el máximo: es lo
    # que el relayer exige, y lo que este canal concede.
    (llamada,) = datos["calls"]
    assert llamada["target"] == USDC_E
    assert llamada["value"] == "0"
    assert llamada["data"] == build_approve_calldata(EXCHANGE_V2, MAXIMO_UINT256)
    # Y la firma del cuerpo es la del lote, con esos mismos datos.
    assert cuerpo["signature"] == sign_batch(
        CLAVE,
        chain_id=137,
        wallet=WALLET,
        nonce=7,
        deadline=plazo,
        calls=[Call(target=USDC_E, value=0, data=llamada["data"])],
    )
    # Con la Relayer key configurada y siendo su dirección la del dueño, la
    # autenticación es la suya; la Builder key viaja igual para el despliegue.
    assert envio.calls[-1].request.headers["RELAYER_API_KEY"] == "relayer-clave"


@respx.mock
async def test_un_lote_que_no_confirma_dentro_del_límite_no_se_da_por_bueno() -> None:
    """Agotar los sondeos sin confirmación se dice, y no se sigue.

    Seguir con un permiso que el relayer no llegó a confirmar produciría una
    orden que el recinto rechaza —o que no puede cobrar—, y el rechazo llegaría
    hablando de saldos en vez de decir que el permiso no estaba.
    """
    respx.route(method="GET", path="/v1/account/transactions/params").mock(
        return_value=httpx.Response(200, json={"nonce": "7"})
    )
    respx.post(f"{ROOT}/submit").mock(
        return_value=httpx.Response(200, json={"transactionID": "tx-lote", "state": "STATE_NEW"})
    )
    estado = respx.route(method="GET", path="/v1/account/transactions/tx-lote").mock(
        return_value=httpx.Response(200, json={"state": "STATE_NEW"})
    )
    colateral = Token(symbol="USDC", decimals=6, chain="polygon", address=USDC_E)

    with pytest.raises(ExecutionError, match="no llegó a confirmar"):
        await _motor(intentos=2).approve_wallet_collateral(
            owner=OWNER,
            collateral=colateral,
            spender=EXCHANGE_V2,
            private_key=CLAVE,
            credentials=CREDENCIALES,
        )

    assert estado.call_count == 2, "se agotaron los sondeos y se dijo"
