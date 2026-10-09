"""Pestaña Errores: avisos y fallos recientes de la sesión, en orden cronológico."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.infra.error_journal import DIARIO, EntradaDiario

REFRESCO_MS = 2000

_TODOS = "Todos"
_NIVELES = ("Todos", "Aviso", "Error")
_NIVEL_INTERNO = {"Aviso": {"warning", "warn"}, "Error": {"error", "critical", "exception"}}


class ErrorsPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._visibles: tuple[EntradaDiario, ...] = ()

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        barra = QHBoxLayout()
        barra.addWidget(QLabel("Nivel:"))
        self._filtro = QComboBox()
        self._filtro.addItems(_NIVELES)
        self._filtro.currentIndexChanged.connect(lambda _i: self.refrescar())
        barra.addWidget(self._filtro)
        barra.addStretch(1)
        self._contador = QLabel("")
        self._contador.setObjectName("hint")
        barra.addWidget(self._contador)
        self._actualizar = QPushButton("Actualizar")
        self._actualizar.clicked.connect(self.refrescar)
        barra.addWidget(self._actualizar)
        self._vaciar = QPushButton("Limpiar")
        self._vaciar.clicked.connect(self._on_vaciar)
        barra.addWidget(self._vaciar)
        lay.addLayout(barra)

        ayuda = QLabel(
            "Avisos y errores de esta sesión, sin secretos: las URLs de nodo se "
            "muestran sin ruta y las claves no aparecen nunca."
        )
        ayuda.setObjectName("hint")
        ayuda.setWordWrap(True)
        lay.addWidget(ayuda)

        self._tabla = QTableWidget(0, 4)
        self._tabla.setHorizontalHeaderLabels(["Hora", "Nivel", "Evento", "Detalle"])
        self._tabla.verticalHeader().setVisible(False)
        self._tabla.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tabla.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        cabecera = self._tabla.horizontalHeader()
        cabecera.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        cabecera.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        cabecera.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        cabecera.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self._tabla, stretch=1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refrescar)
        self._timer.start(REFRESCO_MS)
        self.refrescar()

    def refrescar(self) -> None:
        nivel = self._filtro.currentText()
        todas = DIARIO.entradas()
        if nivel != _TODOS:
            todas = tuple(e for e in todas if e.nivel in _NIVEL_INTERNO[nivel])
        self._visibles = tuple(reversed(todas))
        self._tabla.setRowCount(len(self._visibles))
        for fila, entrada in enumerate(self._visibles):
            self._tabla.setItem(fila, 0, _celda(entrada.momento.strftime("%H:%M:%S")))
            self._tabla.setItem(fila, 1, _celda(_etiqueta_nivel(entrada.nivel)))
            self._tabla.setItem(fila, 2, _celda(entrada.evento))
            self._tabla.setItem(fila, 3, _celda(_detalle(entrada)))
        total = len(DIARIO.entradas())
        self._contador.setText(f"{len(self._visibles)} de {total}")
        self._vaciar.setEnabled(total > 0)

    def _on_vaciar(self) -> None:
        DIARIO.vaciar()
        self.refrescar()


def _celda(texto: str) -> QTableWidgetItem:
    item = QTableWidgetItem(texto)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _etiqueta_nivel(nivel: str) -> str:
    return "Error" if nivel in _NIVEL_INTERNO["Error"] else "Aviso"


def _detalle(entrada: EntradaDiario) -> str:
    return "  ".join(f"{clave}={valor}" for clave, valor in entrada.detalles)
