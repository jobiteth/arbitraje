"""El registro de cruces: se guarda en disco, se sondea y no inventa estados."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.track_bridge import BridgeTracking, ClaimState, TrackedBridge
from amigocompora.domain.models import (
    BridgeProgress,
    BridgeTrackState,
    BroadcastReceipt,
    BroadcastStatus,
)
from amigocompora.domain.protocols import EngineKind

HASH = "0xea9e69ca592ef264acb9ac96721cfb6db6023fa82992fc09ba5b671dc01eaa88"


def _registro(
    origin_status: str = "success",
    state: str = "pending",
    *,
    tx_hash: str = HASH,
    engine_id: str = "",
) -> TrackedBridge:
    return TrackedBridge(
        tx_hash=tx_hash,
        origin_chain="base",
        destination_chain="polygon",
        amount_text="1.2 USDC",
        destination_symbol="USDC.e",
        destination_decimals=6,
        origin_status=origin_status,
        state=state,
        engine_id=engine_id,
    )


class _Motor:
    def __init__(self, progresos: list[BridgeProgress]) -> None:
        self._progresos = progresos

    async def track_bridge(
        self, tx_hash: str, *, origin_chain: str, destination_chain: str
    ) -> BridgeProgress:
        return self._progresos.pop(0) if len(self._progresos) > 1 else self._progresos[0]


@dataclass(frozen=True)
class _Manifiesto:
    engine_id: str


class _MotorNombrado(_Motor):
    def __init__(self, engine_id: str, progresos: list[BridgeProgress]) -> None:
        super().__init__(progresos)
        self.manifest = _Manifiesto(engine_id)


class _MotorSinSeguimiento:
    """Un motor activo que no implementa `BridgeTracker`: relay antes del arreglo."""

    def __init__(self, engine_id: str) -> None:
        self.manifest = _Manifiesto(engine_id)


def _progreso(mensaje: str) -> BridgeProgress:
    return BridgeProgress(
        state=BridgeTrackState.PENDING, provider_status="", substatus="", message=mensaje
    )


class _Registro:
    def __init__(self, *motores: object) -> None:
        self._motores = motores

    def active_stack(self, kind: EngineKind) -> tuple[object, ...]:
        return self._motores


def _registro_de(*motores: object) -> EngineRegistry:
    return cast(EngineRegistry, _Registro(*motores))


def test_origen_revertido_queda_fallido_sin_sondear(tmp_path: Path) -> None:
    async def escenario() -> None:
        tracking = BridgeTracking(tmp_path / "b.json", _registro_de(), poll_seconds=0)
        tracking.add(_registro(origin_status="reverted", state="failed"))
        assert tracking.record(HASH) is not None
        assert tracking.record(HASH).track_state is BridgeTrackState.FAILED  # type: ignore[union-attr]
        await tracking.aclose()

    asyncio.run(escenario())


def test_sondeo_hasta_entregado_y_el_registro_sobrevive_al_reinicio(tmp_path: Path) -> None:
    ruta = tmp_path / "bridge_tracking.json"
    entregado = BridgeProgress(
        state=BridgeTrackState.DONE,
        provider_status="DONE",
        substatus="COMPLETED",
        message="Transferencia completada",
        receiving_tx_hash="0xdef",
        received_raw=1_197_000,
        received_chain="137",
    )

    async def escenario() -> None:
        tracking = BridgeTracking(ruta, _registro_de(_Motor([entregado])), poll_seconds=0)
        tracking.add(_registro())
        for _ in range(200):
            actual = tracking.record(HASH)
            if actual is not None and actual.track_state.is_final:
                break
            await asyncio.sleep(0.01)
        await tracking.aclose()

    asyncio.run(escenario())

    reiniciado = BridgeTracking(ruta, _registro_de(), poll_seconds=0)
    registro = reiniciado.record(HASH)
    assert registro is not None
    assert registro.track_state is BridgeTrackState.DONE
    assert registro.received_text == "1.197 USDC.e"
    assert registro.receiving_tx_hash == "0xdef"


def test_cada_cruce_lo_sigue_el_motor_que_lo_emitio(tmp_path: Path) -> None:
    otro_hash = "0x" + "cd" * 32
    circle = _MotorNombrado("circle", [_progreso("circle-msg")])
    lifi = _MotorNombrado("lifi", [_progreso("lifi-msg")])

    async def escenario() -> None:
        tracking = BridgeTracking(tmp_path / "b.json", _registro_de(circle, lifi), poll_seconds=0)
        tracking.add(_registro(engine_id="lifi"))
        tracking.add(_registro(tx_hash=otro_hash, engine_id="circle"))
        await asyncio.sleep(0.05)
        await tracking.aclose()
        lifi_registro = tracking.record(HASH)
        circle_registro = tracking.record(otro_hash)
        assert lifi_registro is not None
        assert circle_registro is not None
        assert lifi_registro.message == "lifi-msg"
        assert circle_registro.message == "circle-msg"

    asyncio.run(escenario())


def test_registro_antiguo_sin_motor_lo_sigue_el_primero(tmp_path: Path) -> None:
    motor = _MotorNombrado("lifi", [_progreso("lifi-msg")])

    async def escenario() -> None:
        tracking = BridgeTracking(tmp_path / "b.json", _registro_de(motor), poll_seconds=0)
        tracking.add(_registro())
        await asyncio.sleep(0.05)
        await tracking.aclose()
        actual = tracking.record(HASH)
        assert actual is not None
        assert actual.message == "lifi-msg"

    asyncio.run(escenario())


def test_sin_motor_de_seguimiento_no_se_inventa_un_estado(tmp_path: Path) -> None:
    async def escenario() -> None:
        tracking = BridgeTracking(tmp_path / "b.json", _registro_de(), poll_seconds=0)
        tracking.add(_registro())
        await asyncio.sleep(0.02)
        await tracking.aclose()
        actual = tracking.record(HASH)
        assert actual is not None
        assert actual.track_state is BridgeTrackState.PENDING
        assert "no está activo" in actual.message

    asyncio.run(escenario())


def test_un_motor_activo_sin_seguimiento_lo_dice_sin_mentir_y_deja_de_sondear(
    tmp_path: Path,
) -> None:
    """El caso medido el 2026-10-10: relay activo, cruce parado en el paso 1.

    El registro decía «el motor no está activo» cada 15 s, y era falso. Ahora
    dice lo que pasa —activo, pero sin seguimiento— y deja de sondear: un motor
    que no sabe seguir no va a aprender mientras la app siga abierta, así que
    insistir sólo reescribiría la misma frase. El registro queda sin estado
    final y se vuelve a sondear al arrancar.
    """
    motor = _MotorSinSeguimiento("relay")

    async def escenario() -> None:
        tracking = BridgeTracking(tmp_path / "b.json", _registro_de(motor), poll_seconds=0)
        vistos: list[TrackedBridge] = []
        tracking.subscribe(vistos.append)
        tracking.add(_registro(engine_id="relay"))
        await asyncio.sleep(0.05)
        cuantos = len(vistos)
        await asyncio.sleep(0.05)
        await tracking.aclose()
        actual = tracking.record(HASH)
        assert actual is not None
        assert actual.track_state is BridgeTrackState.PENDING
        assert "está activo" in actual.message
        assert "no sabe seguir" in actual.message
        # Con `poll_seconds=0`, un vigilante vivo avisaría cientos de veces en el
        # segundo sueño; parado, ni una: el contador no puede haber crecido.
        assert len(vistos) == cuantos

    asyncio.run(escenario())


def test_un_motor_inactivo_se_sigue_sondeando_por_si_se_activa(tmp_path: Path) -> None:
    """El motor inactivo es el caso contrario: puede activarse en caliente.

    Desde la página de motores se puede activar el motor que emitió el cruce, y
    entonces el sondeo tiene que estar ahí para encontrarlo. Sólo se deja de
    sondear cuando no hay nada que esperar.
    """

    async def escenario() -> None:
        tracking = BridgeTracking(tmp_path / "b.json", _registro_de(), poll_seconds=0)
        vistos: list[TrackedBridge] = []
        tracking.subscribe(vistos.append)
        tracking.add(_registro(engine_id="relay"))
        await asyncio.sleep(0.05)
        cuantos = len(vistos)
        await asyncio.sleep(0.05)
        await tracking.aclose()
        actual = tracking.record(HASH)
        assert actual is not None
        assert "no está activo" in actual.message
        assert len(vistos) > cuantos

    asyncio.run(escenario())


HASH_RECEPCION = "0x" + "ef" * 32


class _Reclamador:
    def __init__(self, resultado: BroadcastReceipt | Exception) -> None:
        self._resultado = resultado
        self.llamadas = 0

    async def __call__(self, record: TrackedBridge) -> BroadcastReceipt:
        self.llamadas += 1
        if isinstance(self._resultado, Exception):
            raise self._resultado
        return self._resultado


def _recibo(status: BroadcastStatus, reason: str = "") -> BroadcastReceipt:
    return BroadcastReceipt(
        tx_hash=HASH_RECEPCION,
        chain="polygon",
        status=status,
        observed_at=datetime.now(UTC),
        reason=reason,
    )


def _progreso_atestado() -> BridgeProgress:
    return BridgeProgress(
        state=BridgeTrackState.PENDING,
        provider_status="complete",
        substatus="atestado",
        message="Circle ha atestado la quema.",
    )


def _guardar(ruta: Path, *registros: TrackedBridge) -> None:
    ruta.write_text(json.dumps([asdict(r) for r in registros]), encoding="utf-8")


async def _esperar(
    tracking: BridgeTracking, condicion: Callable[[TrackedBridge], bool]
) -> TrackedBridge:
    for _ in range(200):
        actual = tracking.record(HASH)
        if actual is not None and condicion(actual):
            return actual
        await asyncio.sleep(0.01)
    raise AssertionError("el registro no llegó al estado esperado")


def test_atestado_se_reclama_solo_una_vez_y_queda_entregado(tmp_path: Path) -> None:
    reclamador = _Reclamador(_recibo(BroadcastStatus.SUCCESS))

    async def escenario() -> None:
        motor = _Motor([_progreso_atestado()])
        tracking = BridgeTracking(
            tmp_path / "b.json", _registro_de(motor), poll_seconds=0, claimer=reclamador
        )
        tracking.add(_registro())
        actual = await _esperar(tracking, lambda r: r.claim is ClaimState.DONE)
        await tracking.aclose()
        assert actual.track_state is BridgeTrackState.DONE
        assert actual.receiving_tx_hash == HASH_RECEPCION
        assert actual.claim_tx_hash == HASH_RECEPCION
        assert actual.provider_at
        assert actual.attested_at
        assert actual.finished_at

    asyncio.run(escenario())
    assert reclamador.llamadas == 1


def test_un_fallo_al_reclamar_queda_fallido_y_no_se_reintenta_solo(tmp_path: Path) -> None:
    reclamador = _Reclamador(RuntimeError("sin nodo"))

    async def escenario() -> None:
        motor = _Motor([_progreso_atestado()])
        tracking = BridgeTracking(
            tmp_path / "b.json", _registro_de(motor), poll_seconds=0, claimer=reclamador
        )
        tracking.add(_registro())
        actual = await _esperar(tracking, lambda r: r.claim is ClaimState.FAILED)
        await asyncio.sleep(0.05)
        await tracking.aclose()
        assert actual.track_state is BridgeTrackState.PENDING
        assert "sin nodo" in actual.claim_message

    asyncio.run(escenario())
    assert reclamador.llamadas == 1


def test_una_recepcion_revertida_queda_fallida_con_su_hash(tmp_path: Path) -> None:
    reclamador = _Reclamador(_recibo(BroadcastStatus.REVERTED, reason="out of gas"))

    async def escenario() -> None:
        motor = _Motor([_progreso_atestado()])
        tracking = BridgeTracking(
            tmp_path / "b.json", _registro_de(motor), poll_seconds=0, claimer=reclamador
        )
        tracking.add(_registro())
        actual = await _esperar(tracking, lambda r: r.claim is ClaimState.FAILED)
        await tracking.aclose()
        assert actual.claim_tx_hash == HASH_RECEPCION
        assert actual.track_state is BridgeTrackState.PENDING

    asyncio.run(escenario())


def test_reclamar_a_mano_tras_un_fallo_vuelve_a_emitir(tmp_path: Path) -> None:
    ruta = tmp_path / "b.json"
    _guardar(
        ruta,
        replace(
            _registro(),
            substatus="atestado",
            claim_state=ClaimState.FAILED.value,
            claim_message="sin nodo",
        ),
    )
    reclamador = _Reclamador(_recibo(BroadcastStatus.SUCCESS))

    async def escenario() -> None:
        tracking = BridgeTracking(ruta, _registro_de(), poll_seconds=0, claimer=reclamador)
        await tracking.claim(HASH)
        actual = tracking.record(HASH)
        assert actual is not None
        assert actual.claim is ClaimState.DONE
        assert actual.track_state is BridgeTrackState.DONE

    asyncio.run(escenario())
    assert reclamador.llamadas == 1


def test_una_reclamacion_interrumpida_al_cerrar_queda_fallida(tmp_path: Path) -> None:
    ruta = tmp_path / "b.json"
    _guardar(
        ruta,
        replace(_registro(), substatus="atestado", claim_state=ClaimState.RUNNING.value),
    )

    tracking = BridgeTracking(ruta, _registro_de(), poll_seconds=0)

    actual = tracking.record(HASH)
    assert actual is not None
    assert actual.claim is ClaimState.FAILED


def test_solo_se_puede_reclamar_tras_atestado_o_fallo() -> None:
    assert replace(_registro(), substatus="atestado").claimable
    assert replace(
        _registro(), substatus="atestado", claim_state=ClaimState.FAILED.value
    ).claimable
    assert not replace(
        _registro(), substatus="atestado", claim_state=ClaimState.DONE.value
    ).claimable
    assert not _registro().claimable
