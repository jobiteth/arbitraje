"""La modal de un puente en curso: cada paso con su marca, y el cierre en verde.

### Por qué los pasos se derivan y no se inventan

Cada paso sale de lo que el emisor o el proveedor han dicho. Si el proveedor aún no
conoce el cruce, el paso del proveedor queda en espera; no se marca como hecho por
haber pasado un tiempo. El check verde sólo aparece cuando el proveedor confirma la
entrega en destino (`DONE`): un reembolso o un fallo nunca se pintan como éxito.

### Qué no hace

No consulta nada: recibe el registro y lo enseña. Quien sondea es `BridgeTracking`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.usecases.track_bridge import ATTESTED_SUBSTATUS, ClaimState, TrackedBridge
from amigocompora.domain.models import BridgeTrackState, BroadcastStatus
from amigocompora.ui.receipt_dialog import explorer_url
from amigocompora.ui.theme import COLOR_DANGER, COLOR_MUTED, COLOR_SUCCESS, COLOR_WARNING

LIFI_SCAN_URL = "https://scan.li.fi/tx/{tx}"

EMOJI_EN_ESPERA = "🔵"
EMOJI_HECHO = "✅"
EMOJI_REEMBOLSO = "🟡"
EMOJI_ERROR = "❌"


@dataclass(frozen=True, slots=True)
class Paso:
    emoji: str
    titulo: str
    detalle: str


def estado_visual(record: TrackedBridge) -> tuple[str, str, str]:
    """Emoji, texto y color de un cruce, tanto en la tabla como en la modal."""
    if record.origin_status == BroadcastStatus.REVERTED.value:
        return EMOJI_ERROR, "Fallido", COLOR_DANGER
    state = record.track_state
    if state is BridgeTrackState.DONE:
        return EMOJI_HECHO, "Completado", COLOR_SUCCESS
    if record.claim is ClaimState.FAILED:
        return EMOJI_ERROR, "Reclamo fallido", COLOR_DANGER
    if record.claim is ClaimState.RUNNING:
        return EMOJI_EN_ESPERA, "Reclamando", COLOR_WARNING
    if state is BridgeTrackState.REFUNDED:
        return EMOJI_REEMBOLSO, "Reembolsado", COLOR_WARNING
    if state is BridgeTrackState.REFUNDING:
        return EMOJI_REEMBOLSO, "Reembolso en curso", COLOR_WARNING
    if state is BridgeTrackState.FAILED:
        return EMOJI_ERROR, "Fallido", COLOR_DANGER
    return EMOJI_EN_ESPERA, "En espera", COLOR_MUTED


def pasos(record: TrackedBridge) -> tuple[Paso, ...]:
    state = record.track_state
    origen = _paso_origen(record)
    proveedor = Paso(
        EMOJI_HECHO if record.provider_status else EMOJI_EN_ESPERA,
        "El proveedor registra el cruce",
        f"Estado del proveedor: {record.provider_status}"
        if record.provider_status
        else "Esperando a que el proveedor lo registre.",
    )
    if state is BridgeTrackState.DONE:
        en_curso = Paso(EMOJI_HECHO, "Puente en curso", record.message or "Completado.")
        destino = Paso(
            EMOJI_HECHO,
            "Entregado en destino",
            f"Recibido: {record.received_text or 'importe no informado'}",
        )
    elif state in {BridgeTrackState.REFUNDING, BridgeTrackState.REFUNDED}:
        en_curso = Paso(EMOJI_REEMBOLSO, "Puente interrumpido", record.message)
        destino = Paso(
            EMOJI_REEMBOLSO,
            "No llegó al destino",
            "El proveedor devuelve el importe a la dirección de origen.",
        )
    elif state is BridgeTrackState.FAILED:
        en_curso = Paso(EMOJI_ERROR, "Puente fallido", record.message)
        destino = Paso(EMOJI_ERROR, "No llegó al destino", record.message)
    else:
        en_curso = Paso(
            EMOJI_EN_ESPERA,
            "Puente en curso",
            record.substatus or record.message or "Esperando al proveedor.",
        )
        destino = _paso_destino_pendiente(record)
    return origen, proveedor, en_curso, destino


def _paso_destino_pendiente(record: TrackedBridge) -> Paso:
    if record.claim is ClaimState.RUNNING:
        return Paso(EMOJI_EN_ESPERA, "Recibiendo en destino", record.claim_message)
    if record.claim is ClaimState.FAILED:
        return Paso(
            EMOJI_ERROR,
            "Recepción en destino fallida",
            f"{record.claim_message} Puedes reclamarla de nuevo desde Transacciones.",
        )
    if record.substatus == ATTESTED_SUBSTATUS:
        return Paso(
            EMOJI_EN_ESPERA,
            "Entrega en destino",
            "Circle atestó la quema: la recepción en destino se emite sola.",
        )
    return Paso(EMOJI_EN_ESPERA, "Entrega en destino", "Pendiente.")


def cabecera(record: TrackedBridge) -> tuple[str, str]:
    """El mensaje final o intermedio de la modal y su color."""
    state = record.track_state
    if record.origin_status == BroadcastStatus.REVERTED.value:
        return f"{EMOJI_ERROR} La operación de origen revirtió", COLOR_DANGER
    if state is BridgeTrackState.DONE:
        return f"{EMOJI_HECHO} Puente completado", COLOR_SUCCESS
    if record.claim is ClaimState.FAILED:
        return (
            f"{EMOJI_ERROR} La recepción en destino falló: reclámala desde Transacciones",
            COLOR_DANGER,
        )
    if record.claim is ClaimState.RUNNING:
        return f"{EMOJI_EN_ESPERA} Reclamando la recepción en destino", COLOR_WARNING
    if state is BridgeTrackState.REFUNDED:
        return f"{EMOJI_REEMBOLSO} Fondos devueltos a origen", COLOR_WARNING
    if state is BridgeTrackState.REFUNDING:
        return f"{EMOJI_REEMBOLSO} Reembolso en curso", COLOR_WARNING
    if state is BridgeTrackState.FAILED:
        return f"{EMOJI_ERROR} Puente fallido", COLOR_DANGER
    return (
        f"{EMOJI_EN_ESPERA} Puente en curso: puedes cerrar esta ventana, "
        "el seguimiento sigue en Transacciones",
        COLOR_MUTED,
    )


def acortar(tx_hash: str) -> str:
    """El hash abreviado para la tabla; el completo va en el enlace y en el portapapeles."""
    if len(tx_hash) <= 16:
        return tx_hash
    return f"{tx_hash[:7]}...{tx_hash[-5:]}"


def enlace(chain: str, tx_hash: str, *, texto: str | None = None) -> str:
    """El hash como enlace al explorador de su red, o como texto si no hay uno."""
    url = explorer_url(chain, tx_hash)
    visible = escape(texto if texto is not None else tx_hash)
    return f'<a href="{escape(url)}">{visible}</a>' if url else visible


def enlace_lifi(tx_hash: str, *, texto: str | None = None) -> str:
    url = LIFI_SCAN_URL.format(tx=tx_hash)
    visible = escape(texto if texto is not None else tx_hash)
    return f'<a href="{escape(url)}">{visible}</a>'


def _paso_origen(record: TrackedBridge) -> Paso:
    status = record.origin_status
    if status == BroadcastStatus.SUCCESS.value:
        emoji, detalle = EMOJI_HECHO, f"Confirmado en {record.origin_chain}."
    elif status == BroadcastStatus.REVERTED.value:
        emoji, detalle = EMOJI_ERROR, "La red la procesó y falló: el importe no salió."
    else:
        emoji, detalle = EMOJI_EN_ESPERA, "Enviado, pendiente de confirmar en la red."
    return Paso(emoji, f"Origen emitido ({record.amount_text})", detalle)


def titulo_ventana(record: TrackedBridge) -> str:
    return (
        f"Info del puente: {record.amount_text}, {record.origin_chain} → {record.destination_chain}"
    )


def formatear_duracion(segundos: float) -> str:
    total = max(0, round(segundos))
    horas, resto = divmod(total, 3600)
    minutos, seg = divmod(resto, 60)
    if horas:
        return f"{horas} h {minutos:02d} min"
    if minutos:
        return f"{minutos} min {seg:02d} s"
    return f"{seg} s"


def intervalos(record: TrackedBridge) -> tuple[tuple[datetime | None, datetime | None], ...]:
    """Inicio y fin de cada paso, en el orden de `pasos`. Un fin vacío significa en curso."""
    creado = _instante(record.created_at)
    proveedor = _instante(record.provider_at)
    atestado = _instante(record.attested_at)
    return (
        (None, None),
        (creado, proveedor),
        (proveedor, atestado),
        (atestado, None),
    )


def tiempos(record: TrackedBridge, ahora: datetime) -> tuple[str, ...]:
    """Lo que tarda cada paso. Al terminar el cruce el reloj deja de contar."""
    tope = _instante(record.finished_at) or ahora
    textos: list[str] = []
    for inicio, fin in intervalos(record):
        if inicio is None:
            textos.append("—")
        else:
            textos.append(formatear_duracion(((fin or tope) - inicio).total_seconds()))
    return tuple(textos)


def tiempo_total(record: TrackedBridge) -> str:
    inicio, fin = _instante(record.created_at), _instante(record.finished_at)
    if inicio is None or fin is None:
        return ""
    return f"Tardó {formatear_duracion((fin - inicio).total_seconds())} en total."


def _instante(texto: str) -> datetime | None:
    return datetime.fromisoformat(texto) if texto else None


class BridgeProgressDialog(QDialog):
    """La modal de un cruce. Se puede cerrar y reabrir desde la tabla de transacciones."""

    def __init__(self, record: TrackedBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModal(False)
        self.resize(600, 420)
        self._record = record
        self._tiempos: list[QLabel] = []

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet("font-size: 16px; font-weight: 700;")

        self._steps = QVBoxLayout()
        self._steps.setSpacing(8)

        self._total = QLabel()
        self._total.setWordWrap(True)
        self._total.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {COLOR_SUCCESS};")

        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._summary.setOpenExternalLinks(True)
        self._summary.setObjectName("hint")

        botones = QDialogButtonBox(QDialogButtonBox.Close)
        botones.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self._banner)
        lay.addLayout(self._steps)
        lay.addWidget(self._total)
        lay.addWidget(self._summary)
        lay.addStretch()
        lay.addWidget(botones)

        self._reloj = QTimer(self)
        self._reloj.timeout.connect(self._refrescar_tiempos)
        self._reloj.start(1000)

        self.update_record(record)

    def update_record(self, record: TrackedBridge) -> None:
        self._record = record
        self.setWindowTitle(titulo_ventana(record))
        texto, color = cabecera(record)
        self._banner.setText(texto)
        self._banner.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {color};")
        self._clear_steps()
        for paso in pasos(record):
            fila_widget = QWidget()
            fila = QHBoxLayout(fila_widget)
            fila.setContentsMargins(0, 0, 0, 0)
            texto_paso = QLabel(
                f"<span style='font-size: 15px'>{paso.emoji}</span>&nbsp; "
                f"<b>{escape(paso.titulo)}</b><br>"
                f"<span style='color: {COLOR_MUTED}'>{escape(paso.detalle)}</span>"
            )
            texto_paso.setWordWrap(True)
            tiempo = QLabel()
            tiempo.setStyleSheet(f"color: {COLOR_MUTED};")
            tiempo.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            fila.addWidget(texto_paso, 1)
            fila.addWidget(tiempo)
            self._steps.addWidget(fila_widget)
            self._tiempos.append(tiempo)
        self._total.setText(tiempo_total(record))
        self._summary.setText(_resumen(record))
        self._refrescar_tiempos()

    def _refrescar_tiempos(self) -> None:
        textos = tiempos(self._record, datetime.now(UTC))
        for etiqueta, texto in zip(self._tiempos, textos, strict=True):
            etiqueta.setText(texto)
        if self._record.finished_at:
            self._reloj.stop()

    def _clear_steps(self) -> None:
        self._tiempos.clear()
        while (item := self._steps.takeAt(0)) is not None:
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


def _resumen(record: TrackedBridge) -> str:
    lineas = [f"Origen ({record.origin_chain}): {enlace(record.origin_chain, record.tx_hash)}"]
    if record.engine_id in ("", "lifi"):
        lineas.append(f"Seguimiento LI.FI: {enlace_lifi(record.tx_hash)}")
    if record.receiving_tx_hash:
        lineas.append(
            f"Destino ({record.destination_chain}): "
            f"{enlace(record.destination_chain, record.receiving_tx_hash)}"
        )
    if record.claim_tx_hash and record.claim_tx_hash != record.receiving_tx_hash:
        lineas.append(
            f"Intento de recepción ({record.destination_chain}): "
            f"{enlace(record.destination_chain, record.claim_tx_hash)}"
        )
    if record.substatus:
        lineas.append(f"Subestado del proveedor: {escape(record.substatus)}")
    if record.updated_at:
        lineas.append(f"Última consulta: {escape(record.updated_at)}")
    return "<br>".join(lineas)
