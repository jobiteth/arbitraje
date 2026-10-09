from __future__ import annotations

from typing import Any

from amigocompora.domain.models import BridgeTrackState
from amigocompora.engines.circle.attestation import (
    MESSAGE_TRANSMITTER,
    MESSAGES_URL,
    SELECTOR_RECEIVE,
)
from amigocompora.engines.circle.engine import CircleEngine

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


def _motor(payload: Any) -> tuple[CircleEngine, _FuenteFalsa]:
    motor = CircleEngine()
    fuente = _FuenteFalsa(payload)
    motor._source = fuente  # type: ignore[assignment]
    return motor, fuente


def _mensaje(status: str) -> dict[str, Any]:
    return {"status": status, "attestation": "PENDING", "message": "0x"}


async def test_una_quema_no_indexada_es_desconocida() -> None:
    motor, _ = _motor(None)

    progreso = await motor.track_bridge(HASH_TX, origin_chain="base", destination_chain="polygon")

    assert progreso.state is BridgeTrackState.UNKNOWN


async def test_la_consulta_va_al_dominio_de_origen() -> None:
    motor, fuente = _motor({"messages": []})

    await motor.track_bridge(HASH_TX, origin_chain="base", destination_chain="polygon")

    assert fuente.llamadas == [(f"{MESSAGES_URL}/6", {"transactionHash": HASH_TX})]


async def test_las_confirmaciones_pendientes_siguen_en_curso() -> None:
    motor, _ = _motor({"messages": [_mensaje("pending_confirmations")]})

    progreso = await motor.track_bridge(HASH_TX, origin_chain="base", destination_chain="polygon")

    assert progreso.state is BridgeTrackState.PENDING
    assert progreso.provider_status == "pending_confirmations"
    assert progreso.substatus == "esperando confirmaciones"


async def test_atestada_sigue_pendiente_y_nunca_se_da_por_entregada() -> None:
    motor, _ = _motor({"messages": [_mensaje("complete")]})

    progreso = await motor.track_bridge(HASH_TX, origin_chain="base", destination_chain="polygon")

    assert progreso.state is BridgeTrackState.PENDING
    assert progreso.substatus == "atestado"
    assert "polygon" in progreso.message


def _atestado() -> dict[str, Any]:
    return {"status": "complete", "attestation": "0xabcd", "message": "0x1234"}


async def test_sin_atestacion_no_hay_recepcion_que_construir() -> None:
    motor, _ = _motor({"messages": [_mensaje("pending_confirmations")]})

    tx = await motor.prepare_receive(HASH_TX, origin_chain="base", destination_chain="polygon")

    assert tx is None


async def test_la_recepcion_atestada_llama_a_receive_message_en_destino() -> None:
    motor, _ = _motor({"messages": [_atestado()]})

    tx = await motor.prepare_receive(HASH_TX, origin_chain="base", destination_chain="polygon")

    assert tx is not None
    assert tx.to_address == MESSAGE_TRANSMITTER
    assert tx.chain_id == 137
    assert tx.calldata.startswith(SELECTOR_RECEIVE)
