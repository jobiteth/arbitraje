"""Ventana principal de Amigocompora."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.alerts import AlertCenter, Severity
from amigocompora.app.container import Container
from amigocompora.domain.modes import OperationMode
from amigocompora.ui.pages.ai import AiPage
from amigocompora.ui.pages.alerts import AlertsPage
from amigocompora.ui.pages.engines import EnginesPage
from amigocompora.ui.pages.prediction import PredictionPage
from amigocompora.ui.pages.prices import PricesPage
from amigocompora.ui.theme import (
    APP_SUBTITLE,
    APP_TITLE,
    COLOR_DANGER,
    STYLESHEET,
)
from amigocompora.ui.widgets import AlertBanner, QtConfirmationPrompt, spawn


class MainWindow(QMainWindow):
    def __init__(self, container: Container, alert_center: AlertCenter) -> None:
        super().__init__()
        self._container = container
        self._alerts = alert_center
        self.setWindowTitle(f"{APP_TITLE} — {APP_SUBTITLE}")
        self.setMinimumSize(1100, 720)
        self.setStyleSheet(STYLESHEET)

        # Prompt de confirmación: la UI real que pide el «sí» explícito.
        self._container.gateway.set_prompt(QtConfirmationPrompt(self))

        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        # Barra superior: modo + estado
        top = QHBoxLayout()
        title = QLabel(f"<b style='font-size:15px'>{APP_TITLE}</b>  <span style='color:#8b95a5'>{APP_SUBTITLE}</span>")
        title.setTextFormat(Qt.RichText)
        top.addWidget(title)
        top.addStretch()
        top.addWidget(QLabel("Modo:"))
        self._mode_combo = QComboBox()
        for mode in OperationMode:
            self._mode_combo.addItem(mode.label, mode)
        # Seleccionar el modo activo del guard.
        idx = next((i for i in range(self._mode_combo.count()) if self._mode_combo.itemData(i) == container.guard.mode), 0)
        self._mode_combo.setCurrentIndex(idx)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        top.addWidget(self._mode_combo)
        lay.addLayout(top)

        # Banner de alertas
        self._banner = AlertBanner()
        lay.addWidget(self._banner)
        alert_center.subscribe(lambda a: self._on_alert(a))

        # Pestañas
        self._prices = PricesPage(container)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._prices, "Cotizaciones")
        self._tabs.addTab(PredictionPage(container), "Predicción")
        self._tabs.addTab(AiPage(container), "Copiloto IA")
        self._tabs.addTab(EnginesPage(container), "Motores")
        self._tabs.addTab(AlertsPage(alert_center), "Alertas")
        lay.addWidget(self._tabs, stretch=1)

        # Status bar
        status = QStatusBar()
        self.setStatusBar(status)
        self._status_label = QLabel("Listo")
        status.addWidget(self._status_label, stretch=1)
        # El indicador de autonomía va **antes** de las alertas, o sea más a la
        # izquierda y más cerca de donde se lee primero. No es una cifra más: es
        # la única señal de que la aplicación puede gastar sin consultar, y por
        # eso se pinta con el color de peligro y no con el de aviso.
        self._autonomy_label = QLabel("")
        status.addPermanentWidget(self._autonomy_label)
        self._alert_label = QLabel("")
        status.addPermanentWidget(self._alert_label)
        self._refresh_status()
        alert_center.subscribe(lambda _a: self._refresh_status())
        container.guard.subscribe(lambda _m: self._refresh_status())
        container.policy.subscribe(lambda _armed: self._refresh_autonomy())
        # El modo decide si se puede firmar y emitir, así que la pestaña de
        # cotizaciones tiene que enterarse de que ha cambiado. Sin esto, «Ejecutar»
        # se queda como estaba y el usuario descubre el cambio al pulsarlo —que es
        # justo lo que enseña a desconfiar de los botones—.
        container.guard.subscribe(lambda _m: self._prices.refresh_execution_state())

        # Cerrar: detener scheduler y motores.
        self._closing = False

    def _on_mode_changed(self) -> None:
        mode: OperationMode = self._mode_combo.currentData()
        if mode is None or mode == self._container.guard.mode:
            return
        self._container.guard.set_mode(mode)
        self.statusBar().showMessage(f"Modo cambiado a {mode.label}: {mode.description}", 4000)
        self._refresh_status()

    def _on_alert(self, alert) -> None:  # type: ignore[no-untyped-def]
        self._banner.show_alert(alert.title, alert.detail, alert.severity.value)
        self._refresh_status()
        # Avisar en la barra de estado de las alertas altas si no se está ya
        # en la pestaña de alertas.
        if alert.severity == Severity.HIGH and self._tabs.currentIndex() != 4:
            self.statusBar().showMessage(f"⚠ Alerta alta: {alert.title}", 6000)

    def _refresh_status(self) -> None:
        pending = self._alerts.unacknowledged_count
        self._alert_label.setText(f"● {pending} alerta(s)" if pending else "Sin alertas")
        caps = ", ".join(sorted(c.value for c in self._container.guard))
        self._status_label.setText(f"Modo {self._container.guard.mode.label} · capacidades: {caps or 'ninguna'}")
        self._refresh_autonomy()

    def _refresh_autonomy(self) -> None:
        """Pinta el estado de la ejecución desatendida.

        Sólo se enciende cuando está **armada**, que es el caso que hay que ver.
        El estado normal —desarmada— deja la etiqueta vacía a propósito: un
        indicador permanente diciendo que todo va bien ocupa el mismo sitio que
        la alarma y acaba leyéndose igual, que es nada. La ausencia de la alarma
        es el mensaje.

        Se pinta con el color de peligro y no con el de aviso porque no avisa de
        un riesgo: describe un hecho que ya está en marcha. Armada, la aplicación
        emite transacciones reales sin preguntar, dentro de los límites, y lo
        único que la detiene es desarmarla.
        """
        if not self._container.policy.armed:
            self._autonomy_label.setText("")
            self._autonomy_label.setToolTip("")
            return
        self._autonomy_label.setText("● Ejecución desatendida ARMADA · emite sin preguntar")
        self._autonomy_label.setStyleSheet(f"color: {COLOR_DANGER}; font-weight: 600;")
        self._autonomy_label.setToolTip(
            "La ejecución desatendida está armada: dentro de las listas blancas y "
            "los topes declarados, la aplicación firma y emite sin pedirte "
            "confirmación. Para detenerla, desarma la autonomía con "
            "`AutonomyPolicy.disarm()`."
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._closing:
            event.accept()
            return
        event.ignore()
        # Cierre asíncrono: dar tiempo a aclose() sin congelar la UI.
        self._closing = True
        spawn(self._do_close())

    async def _do_close(self) -> None:
        try:
            await self._container.aclose()
        finally:
            self.close()
