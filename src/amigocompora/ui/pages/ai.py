"""Pestaña: Copiloto IA."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.protocols import AnalysisRequest
from amigocompora.ui.theme import COLOR_MUTED
from amigocompora.ui.widgets import spawn


class AiPage(QWidget):
    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        lay.addWidget(QLabel("<b>Pregunta al copiloto</b> — el modelo sólo ve el contexto que escribas abajo (cotizaciones, overround, etc.). No sale a buscar datos por su cuenta."))

        self._question = QPlainTextEdit()
        self._question.setPlaceholderText("Ej.: Resume las cotizaciones y señala si el diferencial neto compensa comisiones e impacto…")
        self._question.setFixedHeight(70)
        lay.addWidget(self._question)

        lay.addWidget(QLabel("Contexto (pega aquí las cifras que ves en las otras pestañas):"))
        self._context = QPlainTextEdit()
        self._context.setPlaceholderText("Ej.:\nWETH/USDC en Ethereum: 3 venues, spread 42 bps\nOverround Polymarket: 12 bps\n…")
        self._context.setMinimumHeight(110)
        lay.addWidget(self._context)

        row = QHBoxLayout()
        self._btn = QPushButton("Consultar")
        self._btn.clicked.connect(self._on_ask)
        row.addWidget(self._btn)
        row.addStretch()
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        row.addWidget(self._status)
        lay.addLayout(row)

        self._answer = QTextEdit()
        self._answer.setReadOnly(True)
        self._answer.setPlaceholderText("La respuesta del copiloto aparecerá aquí…")
        lay.addWidget(self._answer, stretch=1)

        self._disclaimer = QLabel("")
        self._disclaimer.setWordWrap(True)
        self._disclaimer.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 11px;")
        lay.addWidget(self._disclaimer)

    def _on_ask(self) -> None:
        question = self._question.toPlainText().strip()
        if not question:
            self._status.setText("Escribe una pregunta.")
            return
        # Contexto libre: cada línea es una entrada; si está vacío, una nota.
        raw = self._context.toPlainText().strip()
        context = {"nota": raw} if raw else {"nota": "(sin contexto adicional; responde de forma general y recuerda verificar cifras)"}
        # Si hay varias líneas con ":", partirlas como claves.
        if raw:
            parsed: dict[str, str] = {}
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    parsed[k.strip() or "dato"] = v.strip()
                else:
                    parsed.setdefault("observaciones", "")
                    parsed["observaciones"] += (", " if parsed["observaciones"] else "") + line
            if parsed:
                context = parsed

        self._btn.setEnabled(False)
        self._status.setText("Consultando…")
        spawn(self._do_ask(question, context))

    async def _do_ask(self, question: str, context: dict[str, str]) -> None:
        try:
            result = await self._container.analyze_with_ai(AnalysisRequest(question=question, context=context))
            self._answer.setPlainText(result.summary)
            if result.findings:
                self._answer.append("\n— Hallazgos:\n" + "\n".join(f"• {f}" for f in result.findings))
            self._disclaimer.setText(result.disclaimer)
            self._status.setText("Completado.")
        except Exception as error:
            self._status.setText(f"Error: {error}")
            self._answer.setPlainText(f"No se pudo consultar al copiloto: {error}")
        finally:
            self._btn.setEnabled(True)
