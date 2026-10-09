"""Cliente del relayer de Polymarket: despliegue de la deposit wallet y lotes.

El relayer ejecuta por cuenta de la cuenta las operaciones que la deposit wallet
necesita on-chain: desplegarla (`WALLET-CREATE`) y ejecutar lotes de llamadas
firmados (`WALLET`). Este módulo sólo las **prepara y envía**; cada envío lo
decide quien llama, y ningún caso de uso de la aplicación lo ejecuta todavía sin
confirmación del usuario.

La autenticación depende de la operación. `WALLET-CREATE` exige la **Builder key**
(cabeceras `POLY_BUILDER_*`: HMAC-SHA256 sobre marca de tiempo, método, ruta y
cuerpo exacto, con el secreto decodificado de base64). Los lotes, su nonce y su
estado admiten también la **Relayer key** (cabeceras `RELAYER_API_KEY*`), pero
esa clave está **atada a la dirección de su cuenta**: enviar un lote de otro
dueño con ella se rechaza con «from 0x… does not match auth 0x…» (medido contra
el relayer). Por eso la elección de credencial mira al **dueño** de la
operación: la Relayer key sólo cuando su dirección es la del dueño, y la Builder
key en cualquier otro caso, que firma por cuenta de cualquiera.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_module
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
import structlog
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.address import to_checksum_address

from amigocompora.domain.errors import ExecutionError, SourceResponseError, SourceUnavailableError
from amigocompora.engines.polymarket.deposit_wallet import FACTORY
from amigocompora.infra.http import build_client

_log = structlog.get_logger(__name__)

HOST: Final = "relayer-v2.polymarket.com"
ROOT: Final = f"https://{HOST}"

SUBMIT: Final = "/submit"
WALLET_CREATE_STATUS: Final = "/transaction"
WALLET_NONCE: Final = "/v1/account/transactions/params"
WALLET_BATCH_STATUS: Final = "/v1/account/transactions/"

TYPE_WALLET_CREATE: Final = "WALLET-CREATE"
TYPE_WALLET: Final = "WALLET"
CREATE_METADATA: Final = "Deploy Deposit Wallet"

#: Estados terminales del relayer. Cualquier otro es «sigue en curso».
TERMINAL_STATES: Final = frozenset({"STATE_CONFIRMED", "STATE_FAILED", "STATE_INVALID"})

#: El único estado terminal que significa «salió bien». Los otros dos existen
#: para poder decir que no, y por eso quien espera distingue entre este y el
#: resto en vez de comprobar sólo «es terminal».
CONFIRMED_STATE: Final = "STATE_CONFIRMED"

DOMAIN_NAME: Final = "DepositWallet"
DOMAIN_VERSION: Final = "1"

BATCH_TYPES: Final[dict[str, list[dict[str, str]]]] = {
    "Call": [
        {"name": "target", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"},
    ],
    "Batch": [
        {"name": "wallet", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "calls", "type": "Call[]"},
    ],
}


@dataclass(frozen=True, slots=True)
class BuilderCredentials:
    """La Builder key: clave pública, secreto y frase de paso.

    Como las credenciales del CLOB, no se registran ni se imprimen.
    """

    api_key: str
    secret: str
    passphrase: str

    def __repr__(self) -> str:
        return f"BuilderCredentials(api_key={self.api_key[:6]}…, secreto oculto)"


@dataclass(frozen=True, slots=True)
class RelayerKey:
    """La Relayer key y la dirección a la que pertenece. Sirve para lotes y consultas."""

    api_key: str
    address: str

    def __repr__(self) -> str:
        return f"RelayerKey(address={self.address}, clave oculta)"


@dataclass(frozen=True, slots=True)
class Call:
    """Una llamada del lote. `data` va en hexadecimal con prefijo `0x`."""

    target: str
    value: int
    data: str


@dataclass(frozen=True, slots=True)
class RelayerSubmission:
    transaction_id: str
    state: str


@dataclass(frozen=True, slots=True)
class RelayerTransaction:
    transaction_id: str
    state: str
    proxy_address: str | None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def builder_signature(
    secret: str, *, method: str, path: str, body: str, timestamp: int
) -> str:
    """La firma de una petición con la Builder key. Pura y sin red."""
    try:
        clave = base64.b64decode(secret.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise ExecutionError(
            "el secreto de la Builder key no es base64 válido: no se puede firmar."
        ) from error
    mensaje = f"{timestamp}{method.upper()}{path}{body}"
    digest = hmac_module.new(clave, mensaje.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def sign_batch(
    private_key: str,
    *,
    chain_id: int,
    wallet: str,
    nonce: int,
    deadline: int,
    calls: Sequence[Call],
) -> str:
    """La firma EIP-712 de un lote de la deposit wallet. Pura y sin red.

    Dominio `DepositWallet` v1 con la wallet como `verifyingContract`, y tipo
    primario `Batch`. Coincide con `signBatch` de `builder-relayer-client` 0.0.10
    y con viem; la prueba fija un vector producido con viem.
    """
    message = {
        "wallet": to_checksum_address(wallet),
        "nonce": nonce,
        "deadline": deadline,
        "calls": [
            {
                "target": to_checksum_address(call.target),
                "value": call.value,
                "data": call.data,
            }
            for call in calls
        ],
    }
    typed = {
        "types": BATCH_TYPES,
        "primaryType": "Batch",
        "domain": {
            "name": DOMAIN_NAME,
            "version": DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(wallet),
        },
        "message": message,
    }
    firmada = Account.sign_message(encode_typed_data(full_message=typed), private_key)
    return "0x" + bytes(firmada.signature).hex()


class RelayerClient:
    """Habla con el relayer con la credencial que cada operación admite."""

    __slots__ = ("_builder", "_client", "_relayer")

    def __init__(
        self,
        *,
        builder: BuilderCredentials | None = None,
        relayer: RelayerKey | None = None,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if builder is None and relayer is None:
            raise ExecutionError("el relayer necesita la Builder key o la Relayer key.")
        self._builder = builder
        self._relayer = relayer
        self._client = client or build_client(
            allowed_hosts=(HOST,), timeout_seconds=timeout_seconds
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def auth_headers(
        self,
        method: str,
        path: str,
        *,
        body: str = "",
        builder_only: bool = False,
        owner: str | None = None,
        timestamp: int | None = None,
    ) -> dict[str, str]:
        """Las cabeceras de la credencial que admite esta operación.

        `WALLET-CREATE` sólo acepta la Builder key. Los lotes y sus consultas
        aceptan las dos, pero la Relayer key está atada a su dirección: para una
        operación con `owner` se usa sólo si coincide con ella; si no —o si no
        hay dueño conocido— vale la Builder key, que firma por cuenta de
        cualquiera. Sin ninguna que pueda autenticar la operación, se dice por
        qué en vez de mandar cabeceras que el relayer va a rechazar.
        """
        if builder_only:
            if self._builder is None:
                raise ExecutionError(
                    "esta operación exige la Builder key, y no hay ninguna configurada."
                )
            return self._builder_headers(method, path, body=body, timestamp=timestamp)
        if self._relayer is not None and (
            owner is None or self._relayer.address.lower() == owner.lower()
        ):
            return {
                "RELAYER_API_KEY": self._relayer.api_key,
                "RELAYER_API_KEY_ADDRESS": self._relayer.address,
            }
        if self._builder is not None:
            return self._builder_headers(method, path, body=body, timestamp=timestamp)
        raise ExecutionError(
            "la Relayer key configurada es de otra dirección y no hay Builder key: "
            "no se puede autenticar una operación de este dueño."
        )

    def _builder_headers(
        self, method: str, path: str, *, body: str, timestamp: int | None
    ) -> dict[str, str]:
        builder = self._builder
        if builder is None:
            raise ExecutionError("no hay Builder key configurada para firmar la petición.")
        marca = int(timestamp if timestamp is not None else time.time())
        firma = builder_signature(
            builder.secret,
            method=method,
            path=path,
            body=body,
            timestamp=marca,
        )
        return {
            "POLY_BUILDER_API_KEY": builder.api_key,
            "POLY_BUILDER_PASSPHRASE": builder.passphrase,
            "POLY_BUILDER_SIGNATURE": firma,
            "POLY_BUILDER_TIMESTAMP": str(marca),
        }

    async def submit_wallet_create(self, owner: str) -> RelayerSubmission:
        """Pide desplegar la deposit wallet de la EOA `owner`."""
        cuerpo = _json_body(
            {
                "type": TYPE_WALLET_CREATE,
                "from": to_checksum_address(owner),
                "to": FACTORY,
                "metadata": CREATE_METADATA,
            }
        )
        return await self._submit(cuerpo, builder_only=True)

    async def submit_wallet_batch(
        self,
        *,
        owner: str,
        wallet: str,
        nonce: int,
        deadline: int,
        calls: Sequence[Call],
        signature: str,
    ) -> RelayerSubmission:
        """Envía un lote ya firmado a la deposit wallet."""
        cuerpo = _json_body(
            {
                "type": TYPE_WALLET,
                "from": to_checksum_address(owner),
                "to": FACTORY,
                "nonce": str(nonce),
                "signature": signature,
                "depositWalletParams": {
                    "depositWallet": to_checksum_address(wallet),
                    "deadline": str(deadline),
                    "calls": [
                        {
                            "target": to_checksum_address(c.target),
                            "value": str(c.value),
                            "data": c.data,
                        }
                        for c in calls
                    ],
                },
            }
        )
        return await self._submit(cuerpo, owner=owner)

    async def wallet_nonce(self, owner: str) -> int:
        """El nonce que el lote de `owner` debe llevar."""
        params = f"?address={to_checksum_address(owner)}&type={TYPE_WALLET}"
        payload = await self._get(WALLET_NONCE, params=params, owner=owner)
        if not isinstance(payload, Mapping) or "nonce" not in payload:
            raise SourceResponseError(
                "el relayer no devolvió el nonce de la wallet; no se puede firmar el lote."
            )
        return int(payload["nonce"])

    async def wallet_create_status(
        self, transaction_id: str, *, owner: str | None = None
    ) -> RelayerTransaction:
        payload = await self._get(WALLET_CREATE_STATUS, params=f"?id={transaction_id}", owner=owner)
        return _parse_transaction(payload, transaction_id)

    async def batch_status(
        self, transaction_id: str, *, owner: str | None = None
    ) -> RelayerTransaction:
        payload = await self._get(f"{WALLET_BATCH_STATUS}{transaction_id}", owner=owner)
        return _parse_transaction(payload, transaction_id)

    async def _submit(
        self, cuerpo: str, *, builder_only: bool = False, owner: str | None = None
    ) -> RelayerSubmission:
        headers = self.auth_headers(
            "POST", SUBMIT, body=cuerpo, builder_only=builder_only, owner=owner
        )
        headers["Content-Type"] = "application/json"
        payload = await self._request("POST", SUBMIT, headers=headers, body=cuerpo)
        if not isinstance(payload, Mapping):
            raise SourceResponseError("el relayer devolvió una respuesta inesperada al enviar.")
        transaction_id = str(payload.get("transactionID") or "")
        if not transaction_id:
            raise SourceResponseError(f"el relayer no devolvió el identificador: {payload}")
        state = str(payload.get("state") or "sin estado declarado")
        _log.info("relayer.submitted", transaction_id=transaction_id, state=state)
        return RelayerSubmission(transaction_id=transaction_id, state=state)

    async def _get(self, path: str, *, params: str = "", owner: str | None = None) -> Any:
        headers = self.auth_headers("GET", path, owner=owner)
        return await self._request("GET", path, headers=headers, params=params)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: str | None = None,
        params: str = "",
    ) -> Any:
        url = f"{ROOT}{path}{params}"
        try:
            respuesta = await self._client.request(
                method,
                url,
                headers=dict(headers),
                content=body.encode("utf-8") if body is not None else None,
            )
        except httpx.HTTPError as error:
            raise SourceUnavailableError(
                f"no se pudo hablar con el relayer de Polymarket: {type(error).__name__}."
            ) from error
        if respuesta.status_code >= 500:
            raise SourceUnavailableError(
                f"el relayer respondió {respuesta.status_code}: está caído o saturado."
            )
        if respuesta.status_code >= 400:
            raise SourceResponseError(
                f"el relayer respondió {respuesta.status_code} a {method} {path}: "
                f"{' '.join(respuesta.text.split())[:300]}"
            )
        try:
            return respuesta.json()
        except ValueError as error:
            raise SourceResponseError(f"el relayer no devolvió JSON válido: {error}") from error


def _json_body(payload: Mapping[str, Any]) -> str:
    """El cuerpo exacto que se firma y que se envía: el mismo texto byte a byte."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _parse_transaction(payload: Any, transaction_id: str) -> RelayerTransaction:
    """Lee la ficha de una transacción del relayer.

    `GET /transaction?id=…` devuelve una **lista** de fichas (en la práctica, de
    un solo elemento) mientras que el estado de un lote
    (`/v1/account/transactions/{id}`) devuelve el objeto suelto (medido). Se
    aceptan las dos formas: si viene en lista se elige la ficha cuyo
    `transactionID` coincide antes de leerla, en vez de reventar con una
    respuesta que en realidad sí traía el estado.

    Y una ficha con **otro** identificador no se acepta aunque sea la única: es
    la ficha de otra operación, y tomar su estado por el nuestro haría seguir
    —o parar— por lo que le pasó a otra cosa. Sólo se acepta la solitaria que no
    declara identificador, que no contradice a ninguna.
    """
    if isinstance(payload, list):
        elegida: Mapping[str, Any] | None = None
        for ficha in payload:
            if not isinstance(ficha, Mapping):
                continue
            if str(ficha.get("transactionID") or "") == transaction_id:
                elegida = ficha
                break
        if (
            elegida is None
            and len(payload) == 1
            and isinstance(payload[0], Mapping)
            and not str(payload[0].get("transactionID") or "")
        ):
            elegida = payload[0]
        payload = elegida
    if not isinstance(payload, Mapping):
        raise SourceResponseError("el relayer devolvió una respuesta inesperada al consultar.")
    state = str(payload.get("state") or "sin estado declarado")
    proxy = payload.get("proxyAddress")
    return RelayerTransaction(
        transaction_id=transaction_id,
        state=state,
        proxy_address=to_checksum_address(str(proxy)) if proxy else None,
    )
