"""Seguimiento de los puentes emitidos: qué se vigila y dónde queda constancia.

Tras emitir el origen, el puente no termina: el proveedor tarda minutos en entregar
en destino y a veces devuelve los fondos. Aquí se guarda cada cruce en disco y se
sondea al motor hasta que llega a un estado final, para que cerrar la ventana o
reiniciar la aplicación no haga desaparecer un dinero que sigue en camino.

No firma ni emite nada: sólo lee.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.models import (
    BridgeProgress,
    BridgeQuote,
    BridgeTrackState,
    BroadcastReceipt,
    BroadcastStatus,
)
from amigocompora.domain.protocols import BridgeTracker, EngineKind

_log = structlog.get_logger(__name__)

POLL_SECONDS: float = 15.0

#: Subestado con el que el motor dice que el origen ya está atestado y falta recibir.
ATTESTED_SUBSTATUS: str = "atestado"

Claimer = Callable[["TrackedBridge"], Awaitable[BroadcastReceipt]]


class ClaimState(StrEnum):
    """En qué va la recepción en destino de un puente que necesita un segundo paso."""

    NONE = ""
    RUNNING = "reclamando"
    DONE = "reclamado"
    #: Un intento que no llegó a buen término. No se reintenta solo: lo pide el usuario.
    FAILED = "fallido"


@dataclass(frozen=True, slots=True)
class TrackedBridge:
    """Un cruce emitido y lo último que dijo el proveedor sobre él."""

    tx_hash: str
    origin_chain: str
    destination_chain: str
    amount_text: str
    destination_symbol: str
    destination_decimals: int
    #: `BroadcastStatus` del origen, tal cual lo devolvió el emisor.
    origin_status: str
    #: `BridgeTrackState` del proveedor.
    state: str
    provider_status: str = ""
    substatus: str = ""
    message: str = ""
    receiving_tx_hash: str | None = None
    received_text: str | None = None
    updated_at: str = ""
    #: Motor que emitió el origen. Vacío en los registros anteriores a este campo.
    engine_id: str = ""
    #: `ClaimState` de la recepción en destino, tal cual se guarda.
    claim_state: str = ""
    claim_tx_hash: str | None = None
    claim_message: str = ""
    #: Instantes ISO de cada hito; vacíos en los registros anteriores a estos campos.
    created_at: str = ""
    provider_at: str = ""
    attested_at: str = ""
    finished_at: str = ""

    @property
    def track_state(self) -> BridgeTrackState:
        return BridgeTrackState(self.state)

    @property
    def claim(self) -> ClaimState:
        return ClaimState(self.claim_state)

    @property
    def claimable(self) -> bool:
        """Si el usuario puede pedir la recepción ahora: tras un fallo, o atestado sin intentar."""
        if self.claim is ClaimState.FAILED:
            return True
        return self.claim is ClaimState.NONE and self.substatus == ATTESTED_SUBSTATUS


def new_tracked_bridge(quote: BridgeQuote, receipt: BroadcastReceipt) -> TrackedBridge:
    """El registro inicial de un cruce recién emitido."""
    request = quote.request
    reverted = receipt.status is BroadcastStatus.REVERTED
    now = _now()
    return TrackedBridge(
        tx_hash=receipt.tx_hash,
        origin_chain=request.origin.chain,
        destination_chain=request.destination.chain,
        amount_text=str(request.amount_in),
        destination_symbol=request.destination.symbol,
        destination_decimals=request.destination.decimals,
        origin_status=receipt.status.value,
        state=(BridgeTrackState.FAILED if reverted else BridgeTrackState.PENDING).value,
        message=(
            "La transacción de origen revirtió: el puente no llegó a salir."
            if reverted
            else "Origen emitido. Esperando al proveedor."
        ),
        updated_at=now,
        created_at=now,
        engine_id=quote.engine_id,
    )


class BridgeTracking:
    """Registro de cruces y sondeo de cada uno hasta un estado final."""

    def __init__(
        self,
        path: Path,
        registry: EngineRegistry,
        *,
        poll_seconds: float = POLL_SECONDS,
        claimer: Claimer | None = None,
    ) -> None:
        self._path = path
        self._registry = registry
        self._poll_seconds = poll_seconds
        self._claimer = claimer
        self._records: dict[str, TrackedBridge] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._claiming: set[str] = set()
        self._listeners: list[Callable[[TrackedBridge], None]] = []
        self._loaded = False

    def records(self) -> tuple[TrackedBridge, ...]:
        """Todos los cruces, del más antiguo al más reciente."""
        self._ensure_loaded()
        return tuple(self._records.values())

    def record(self, tx_hash: str) -> TrackedBridge | None:
        self._ensure_loaded()
        return self._records.get(tx_hash)

    def subscribe(self, listener: Callable[[TrackedBridge], None]) -> None:
        self._listeners.append(listener)

    def add(self, record: TrackedBridge) -> None:
        self._ensure_loaded()
        self._records[record.tx_hash] = record
        self._save()
        self._notify(record)
        self.resume()

    def resume(self) -> None:
        """Vuelve a sondear los cruces que aún no tienen estado final."""
        self._ensure_loaded()
        for record in self._records.values():
            if not record.track_state.is_final and record.tx_hash not in self._watchers:
                self._watchers[record.tx_hash] = asyncio.get_running_loop().create_task(
                    self._watch(record.tx_hash), name=f"bridge:{record.tx_hash}"
                )

    async def claim(self, tx_hash: str) -> None:
        """Emite la recepción en destino del cruce. Lo pide el botón «Reclamar»."""
        self._ensure_loaded()
        if tx_hash in self._records:
            await self._claim(tx_hash)

    async def aclose(self) -> None:
        for task in self._watchers.values():
            task.cancel()
        await asyncio.gather(*self._watchers.values(), return_exceptions=True)
        self._watchers.clear()

    # ------------------------------------------------------------ interno #
    async def _watch(self, tx_hash: str) -> None:
        try:
            while not self._records[tx_hash].track_state.is_final:
                await self._poll_once(tx_hash)
                if self._records[tx_hash].track_state.is_final:
                    break
                # Un motor activo que no sabe seguir cruces no va a aprender a
                # hacerlo mientras la app siga abierta: insistir cada 15 s sólo
                # reescribiría la misma frase. El registro queda sin estado final
                # y `resume()` volverá a sondearlo en el próximo arranque (p. ej.,
                # tras actualizar el motor).
                if not self._worth_polling(tx_hash):
                    break
                await asyncio.sleep(self._poll_seconds)
        finally:
            self._watchers.pop(tx_hash, None)

    async def _poll_once(self, tx_hash: str) -> None:
        record = self._records[tx_hash]
        tracker = self._tracker(record.engine_id)
        if tracker is None:
            self._store(tx_hash, message=self._no_tracker_message(record.engine_id))
            return
        try:
            progress = await tracker.track_bridge(
                tx_hash,
                origin_chain=record.origin_chain,
                destination_chain=record.destination_chain,
            )
        except Exception as error:
            _log.warning("bridge_tracking.poll_failed", tx_hash=tx_hash, reason=str(error))
            self._store(
                tx_hash,
                message=f"No se pudo consultar al proveedor ({error}). Se reintentará.",
            )
            return
        self._store_progress(record, progress)
        if (
            progress.substatus == ATTESTED_SUBSTATUS
            and self._records[tx_hash].claim is ClaimState.NONE
        ):
            await self._claim(tx_hash)

    async def _claim(self, tx_hash: str) -> None:
        """Emite la recepción una vez. Un fallo queda en `fallido` y no se reintenta solo."""
        if self._claimer is None or tx_hash in self._claiming:
            return
        if self._records[tx_hash].claim in {ClaimState.RUNNING, ClaimState.DONE}:
            return
        self._claiming.add(tx_hash)
        try:
            self._store(
                tx_hash,
                claim_state=ClaimState.RUNNING.value,
                claim_message="Reclamando la recepción en destino…",
            )
            try:
                receipt = await self._claimer(self._records[tx_hash])
            except Exception as error:
                _log.warning("bridge_tracking.claim_failed", tx_hash=tx_hash, reason=str(error))
                self._store(
                    tx_hash, claim_state=ClaimState.FAILED.value, claim_message=str(error)
                )
                return
            self._store_claim_receipt(tx_hash, receipt)
        finally:
            self._claiming.discard(tx_hash)

    def _store_claim_receipt(self, tx_hash: str, receipt: BroadcastReceipt) -> None:
        if receipt.status is BroadcastStatus.SUCCESS:
            self._store(
                tx_hash,
                state=BridgeTrackState.DONE.value,
                claim_state=ClaimState.DONE.value,
                claim_tx_hash=receipt.tx_hash,
                receiving_tx_hash=receipt.tx_hash,
                message="Recibido en destino: la recepción se confirmó en la red.",
                claim_message="Recepción confirmada en la red de destino.",
            )
            return
        self._store(
            tx_hash,
            claim_state=ClaimState.FAILED.value,
            claim_tx_hash=receipt.tx_hash,
            claim_message=(
                f"La recepción no se confirmó ({receipt.status.value}, {receipt.tx_hash}). "
                "Comprueba el hash en el explorador antes de volver a reclamar."
            ),
        )

    def _store_progress(self, record: TrackedBridge, progress: BridgeProgress) -> None:
        received = record.received_text
        if progress.received_raw is not None:
            received = _format_received(
                progress.received_raw, record.destination_decimals, record.destination_symbol
            )
        self._store(
            record.tx_hash,
            state=progress.state.value,
            provider_status=progress.provider_status,
            substatus=progress.substatus,
            message=progress.message,
            receiving_tx_hash=progress.receiving_tx_hash or record.receiving_tx_hash,
            received_text=received,
            provider_at=record.provider_at or (_now() if progress.provider_status else ""),
            attested_at=record.attested_at
            or (_now() if progress.substatus == ATTESTED_SUBSTATUS else ""),
        )

    def _store(self, tx_hash: str, **changes: Any) -> None:
        now = _now()
        updated = replace(self._records[tx_hash], updated_at=now, **changes)
        if updated.track_state.is_final and not updated.finished_at:
            updated = replace(updated, finished_at=now)
        self._records[tx_hash] = updated
        self._save()
        self._notify(updated)

    def _notify(self, record: TrackedBridge) -> None:
        for listener in self._listeners:
            listener(record)

    def _tracker(self, engine_id: str) -> BridgeTracker | None:
        """El motor activo que emitió el cruce. Sin `engine_id` (registro antiguo),
        el primero que sepa seguir puentes, como antes de guardarlo."""
        for engine in self._registry.active_stack(EngineKind.CROSS_CHAIN):
            if isinstance(engine, BridgeTracker) and (
                not engine_id or engine.manifest.engine_id == engine_id
            ):
                return engine
        return None

    def _is_engine_active(self, engine_id: str) -> bool:
        """Si el motor que emitió el cruce está en la pila activa, sepa seguir o no."""
        if not engine_id:
            return False
        return any(
            engine.manifest.engine_id == engine_id
            for engine in self._registry.active_stack(EngineKind.CROSS_CHAIN)
        )

    def _worth_polling(self, tx_hash: str) -> bool:
        """Si seguir sondeando puede cambiar algo con lo que hay ahora mismo.

        Sin seguidor hay dos casos: si el motor ni está activo, se sigue
        sondeando —puede activarse desde la página de motores—; si está activo
        pero no sabe seguir cruces, no hay nada que esperar y se deja de
        sondear.
        """
        engine_id = self._records[tx_hash].engine_id
        if self._tracker(engine_id) is not None:
            return True
        return not self._is_engine_active(engine_id)

    def _no_tracker_message(self, engine_id: str) -> str:
        """Lo que se dice cuando no hay quien siga el cruce, sin mentir sobre el porqué."""
        if self._is_engine_active(engine_id):
            return (
                f"El motor «{engine_id}» está activo, pero no sabe seguir cruces "
                "todavía: no hay seguimiento automático. "
                "Mira el hash en el explorador."
            )
        return f"El motor «{engine_id}» no está activo: no se puede seguir este cruce."

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            _log.warning("bridge_tracking.read_failed", path=str(self._path), reason=str(error))
            return
        if not isinstance(raw, list):
            return
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                record = TrackedBridge(**entry)
            except TypeError:
                _log.warning("bridge_tracking.entry_ignored", path=str(self._path))
                continue
            if record.claim is ClaimState.RUNNING:
                record = replace(
                    record,
                    claim_state=ClaimState.FAILED.value,
                    claim_message=(
                        "La aplicación se cerró mientras se recibía el puente: comprueba "
                        "el hash en el explorador antes de reclamar."
                    ),
                )
            self._records[record.tx_hash] = record

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Temporal y reemplazo: un corte a mitad de escritura no debe dejar
            # el registro truncado y perder los cruces que aún siguen en camino.
            scratch = self._path.with_suffix(self._path.suffix + ".tmp")
            scratch.write_text(
                json.dumps(
                    [asdict(record) for record in self._records.values()],
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
            scratch.replace(self._path)
        except OSError as error:
            _log.warning("bridge_tracking.write_failed", path=str(self._path), reason=str(error))


def _format_received(raw: int, decimals: int, symbol: str) -> str:
    amount = Decimal(raw).scaleb(-decimals).normalize()
    return f"{amount:f} {symbol}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
