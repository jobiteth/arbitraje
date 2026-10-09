"""Traducción del estado de un puente de LI.FI a lo que enseña la pantalla."""

from __future__ import annotations

from amigocompora.domain.models import BridgeTrackState
from amigocompora.engines.lifi.engine import progress_from_status


def test_destino_recibido_es_hecho_y_lleva_el_importe_y_el_hash() -> None:
    progreso = progress_from_status(
        {
            "status": "DONE",
            "substatus": "COMPLETED",
            "substatusMessage": "Transferencia completada",
            "receiving": {"txHash": "0xabc", "amount": "1000000", "chainId": 137},
        }
    )
    assert progreso.state is BridgeTrackState.DONE
    assert progreso.state.is_final
    assert progreso.receiving_tx_hash == "0xabc"
    assert progreso.received_raw == 1_000_000
    assert progreso.received_chain == "137"


def test_reembolso_en_curso_no_es_final_ni_hecho() -> None:
    progreso = progress_from_status(
        {
            "status": "PENDING",
            "substatus": "REFUND_IN_PROGRESS",
            "substatusMessage": "The tokens are being refunded to the user.",
        }
    )
    assert progreso.state is BridgeTrackState.REFUNDING
    assert not progreso.state.is_final
    assert progreso.receiving_tx_hash is None
    assert progreso.received_raw is None


def test_reembolso_completado_es_final_y_distinto_de_fallo() -> None:
    reembolsado = progress_from_status({"status": "FAILED", "substatus": "REFUNDED"})
    fallido = progress_from_status({"status": "FAILED", "substatus": "BRIDGE_NOT_AVAILABLE"})
    assert reembolsado.state is BridgeTrackState.REFUNDED
    assert fallido.state is BridgeTrackState.FAILED
    assert reembolsado.state.is_final
    assert fallido.state.is_final


def test_pendiente_sin_subestado_de_reembolso_sigue_en_curso() -> None:
    progreso = progress_from_status(
        {"status": "PENDING", "substatus": "WAIT_DESTINATION_TRANSACTION"}
    )
    assert progreso.state is BridgeTrackState.PENDING
    assert not progreso.state.is_final


def test_respuesta_desconocida_no_se_presenta_como_hecho() -> None:
    progreso = progress_from_status({"status": "NOT_FOUND"})
    assert progreso.state is BridgeTrackState.UNKNOWN
    assert progreso.provider_status == "NOT_FOUND"


def test_importe_en_hexadecimal_se_lee_igual_que_en_decimal() -> None:
    progreso = progress_from_status(
        {"status": "DONE", "receiving": {"amount": "0xf4240", "chainId": 137}}
    )
    assert progreso.received_raw == 1_000_000
