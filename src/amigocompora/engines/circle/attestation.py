"""Atestación de Circle y la llamada de recepción en destino. Sin firma.

Tras quemar en origen, Circle atesta el mensaje en su API Iris. Cuando el estado es
`complete`, el mensaje y su atestación se entregan a `receiveMessage` del
`MessageTransmitterV2` de destino, que mintea el USDC nativo al destinatario.

Este módulo sólo **lee** la atestación y **codifica** la llamada. Quién la emite y
cuándo lo decide el caso de uso, detrás de la misma confirmación que cualquier otra
transacción.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from eth_abi.abi import encode
from eth_utils.crypto import keccak

from amigocompora.engines.http_source import JsonSource

#: Ruta documentada: `GET /v2/messages/{sourceDomainId}?transactionHash=…`.
MESSAGES_URL: Final = "https://iris-api.circle.com/v2/messages"

#: `MessageTransmitterV2`, la misma dirección en todas las redes de la tabla de Circle.
MESSAGE_TRANSMITTER: Final = "0x81D40F21F12A8F0E3252Bccb954D722d4c464B64"  # dirección pública

COMPLETE: Final = "complete"

RECEIVE_SIGNATURE: Final = "receiveMessage(bytes,bytes)"
SELECTOR_RECEIVE: Final = "0x" + keccak(text=RECEIVE_SIGNATURE)[:4].hex()


@dataclass(frozen=True, slots=True)
class Attestation:
    """Un mensaje de CCTP con su atestación, ya listo para `receiveMessage`."""

    message: str
    attestation: str


def encode_receive_message(attested: Attestation) -> str:
    """El calldata de `receiveMessage(message, attestation)`, en hexadecimal."""
    args = encode(
        ["bytes", "bytes"],
        [bytes.fromhex(attested.message[2:]), bytes.fromhex(attested.attestation[2:])],
    )
    return "0x" + (bytes.fromhex(SELECTOR_RECEIVE[2:]) + args).hex()


async def fetch_attestation(
    source: JsonSource, *, source_domain: int, tx_hash: str
) -> Attestation | None:
    """La atestación del mensaje de esa transacción, o `None` si aún no está lista.

    Un 404 significa que Iris todavía no ha indexado la quema: no es un fallo y se
    vuelve a preguntar. Sólo se devuelve con estado `complete` y con la atestación
    presente; cualquier otra cosa se trata como pendiente.
    """
    payload = await _messages(source, source_domain=source_domain, tx_hash=tx_hash)
    return _first_complete(payload)


async def fetch_message_status(
    source: JsonSource, *, source_domain: int, tx_hash: str
) -> str | None:
    """El estado que Iris da a la quema, o `None` si aún no la tiene indexada."""
    payload = await _messages(source, source_domain=source_domain, tx_hash=tx_hash)
    return _first_status(payload)


async def _messages(source: JsonSource, *, source_domain: int, tx_hash: str) -> Any:
    return await source.get_json(
        f"{MESSAGES_URL}/{source_domain}",
        params={"transactionHash": tx_hash},
        absent_statuses=frozenset({404}),
    )


def _first_status(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    mensajes = payload.get("messages")
    if not isinstance(mensajes, list):
        return None
    for item in mensajes:
        status = item.get("status") if isinstance(item, dict) else None
        if isinstance(status, str):
            return status
    return None


def _first_complete(payload: Any) -> Attestation | None:
    if not isinstance(payload, dict):
        return None
    mensajes = payload.get("messages")
    if not isinstance(mensajes, list):
        return None
    for item in mensajes:
        if not isinstance(item, dict) or item.get("status") != COMPLETE:
            continue
        message = item.get("message")
        attestation = item.get("attestation")
        if not isinstance(message, str) or not isinstance(attestation, str):
            continue
        if not _is_hex(message) or not _is_hex(attestation):
            continue
        return Attestation(message=message, attestation=attestation)
    return None


def _is_hex(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) <= 2:
        return False
    try:
        bytes.fromhex(value[2:])
    except ValueError:
        return False
    return True
