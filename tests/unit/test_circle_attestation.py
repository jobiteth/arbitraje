from __future__ import annotations

from typing import Any

from eth_abi.abi import decode

from amigocompora.engines.circle.attestation import (
    MESSAGES_URL,
    SELECTOR_RECEIVE,
    Attestation,
    encode_receive_message,
    fetch_attestation,
)

MENSAJE = "0x" + "11" * 40
FIRMA = "0x" + "22" * 65
HASH_TX = "0x" + "ab" * 32


class _FuenteFalsa:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.llamadas: list[tuple[str, dict[str, str] | None]] = []

    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None, **_: Any
    ) -> Any:
        self.llamadas.append((url, params))
        return self.payload


def _respuesta(*items: dict[str, Any]) -> dict[str, Any]:
    return {"messages": list(items), "sourceTxHash": HASH_TX}


def _item(status: str, attestation: str = FIRMA, message: str = MENSAJE) -> dict[str, Any]:
    return {"status": status, "attestation": attestation, "message": message}


async def test_la_atestacion_completa_se_devuelve_con_su_mensaje() -> None:
    fuente = _FuenteFalsa(_respuesta(_item("complete")))

    atestado = await fetch_attestation(fuente, source_domain=6, tx_hash=HASH_TX)  # type: ignore[arg-type]

    assert atestado == Attestation(message=MENSAJE, attestation=FIRMA)


async def test_la_consulta_va_al_dominio_de_origen_con_el_hash() -> None:
    fuente = _FuenteFalsa(_respuesta(_item("complete")))

    await fetch_attestation(fuente, source_domain=6, tx_hash=HASH_TX)  # type: ignore[arg-type]

    assert fuente.llamadas == [(f"{MESSAGES_URL}/6", {"transactionHash": HASH_TX})]


async def test_una_atestacion_pendiente_no_se_da_por_lista() -> None:
    fuente = _FuenteFalsa(
        _respuesta(_item("pending_confirmations", attestation="PENDING", message="0x"))
    )

    assert await fetch_attestation(fuente, source_domain=6, tx_hash=HASH_TX) is None  # type: ignore[arg-type]


async def test_un_404_de_iris_es_pendiente_no_fallo() -> None:
    fuente = _FuenteFalsa(None)

    assert await fetch_attestation(fuente, source_domain=6, tx_hash=HASH_TX) is None  # type: ignore[arg-type]


async def test_una_atestacion_que_no_es_hexadecimal_se_descarta() -> None:
    fuente = _FuenteFalsa(_respuesta(_item("complete", attestation="PENDING")))

    assert await fetch_attestation(fuente, source_domain=6, tx_hash=HASH_TX) is None  # type: ignore[arg-type]


async def test_entre_varios_mensajes_se_toma_el_primero_completo() -> None:
    fuente = _FuenteFalsa(
        _respuesta(
            _item("pending_confirmations", attestation="PENDING", message="0x"),
            _item("complete", attestation="0x" + "33" * 65, message="0x" + "44" * 8),
        )
    )

    atestado = await fetch_attestation(fuente, source_domain=6, tx_hash=HASH_TX)  # type: ignore[arg-type]

    assert atestado == Attestation(message="0x" + "44" * 8, attestation="0x" + "33" * 65)


def test_receive_message_se_decodifica_con_mensaje_y_atestacion() -> None:
    calldata = encode_receive_message(Attestation(message=MENSAJE, attestation=FIRMA))

    assert calldata.startswith(SELECTOR_RECEIVE)
    mensaje, atestacion = decode(
        ["bytes", "bytes"], bytes.fromhex(calldata[2 + 8 :])
    )
    assert mensaje == bytes.fromhex(MENSAJE[2:])
    assert atestacion == bytes.fromhex(FIRMA[2:])
