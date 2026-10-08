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
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.ui.pages.ai import AiPage
from amigocompora.ui.pages.alerts import AlertsPage
from amigocompora.ui.pages.engines import EnginesPage
from amigocompora.ui.pages.prediction import PredictionPage
from amigocompora.ui.pages.prices import PricesPage
from amigocompora.ui.pages.wallet import WalletPage
from amigocompora.ui.theme import (
    APP_SUBTITLE,
    APP_TITLE,
    COLOR_DANGER,
    STYLESHEET,
)
from amigocompora.ui.widgets import AlertBanner, QtConfirmationPrompt, ScrollArea, spawn


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

        # Barra superior: título, modo y estado.
        #
        # El modo va aquí arriba y no dentro de una pestaña porque no pertenece a
        # ninguna: decide lo que pueden hacer **todas** a la vez. Y lleva debajo
        # su descripción y sus capacidades porque un desplegable que dice
        # «ASISTIDO» no explica qué se puede y qué no, y esa es exactamente la
        # pregunta que alguien se hace antes de intentar operar.
        top = QHBoxLayout()
        title = QLabel(f"<b style='font-size:16px'>{APP_TITLE}</b>")
        title.setTextFormat(Qt.RichText)
        top.addWidget(title)
        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setObjectName("hint")
        top.addWidget(subtitle)
        top.addStretch()
        top.addWidget(QLabel("Modo:"))
        self._mode_combo = QComboBox()
        for mode in OperationMode:
            self._mode_combo.addItem(mode.label, mode)
            self._mode_combo.setItemData(
                self._mode_combo.count() - 1, mode.description, Qt.ItemDataRole.ToolTipRole
            )
        # Seleccionar el modo activo del guard.
        self._mode_combo.setCurrentIndex(self._mode_index(container.guard.mode))
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        top.addWidget(self._mode_combo)
        lay.addLayout(top)

        self._mode_hint = QLabel("")
        self._mode_hint.setObjectName("hint")
        self._mode_hint.setWordWrap(True)
        lay.addWidget(self._mode_hint)

        # Banner de alertas
        self._banner = AlertBanner()
        lay.addWidget(self._banner)
        alert_center.subscribe(lambda a: self._on_alert(a))

        # Pestañas
        self._prices = PricesPage(container)
        self._prediction = PredictionPage(container)
        self._engines = EnginesPage(container)
        self._wallet = WalletPage(container)
        self._tabs = QTabWidget()

        # La cartera **no** es una pestaña: va dentro de la de swap, justo debajo
        # de la tarjeta de conversión. Es el orden de una cartera de verdad —lo
        # que vas a intercambiar arriba, tu cuenta y tus tokens debajo— y es donde
        # se contesta la pregunta que se hace justo antes de convertir: cuánto
        # tengo de esto y en qué red. En una pestaña aparte obligaba a ir y volver
        # para leer un saldo que se está a punto de firmar.
        self._prices.insert_below_converter(self._wallet)

        # Los puentes sí tienen pestaña propia: cruzar de red es una operación con
        # sus dos redes, su importe y su ruta, y compartía pantalla con el swap
        # sin compartir ninguno de sus controles. La sección la sigue construyendo
        # `PricesPage` —su estado de ejecución es el mismo y su
        # `refresh_execution_state` la repinta—, y aquí sólo se coloca, dentro de
        # su propio armazón para que también se desplace cuando no quepa.
        self._bridges_tab = QWidget()
        marco = ScrollArea.fill(self._bridges_tab, spacing=12)
        marco.body().addWidget(self._prices.take_bridges())
        marco.body().addStretch(1)

        self._alerts_page = AlertsPage(alert_center)
        self._tabs.addTab(self._prices, "Swap")
        self._tabs.addTab(self._bridges_tab, "Entre redes")
        self._tabs.addTab(self._prediction, "Predicción")
        self._tabs.addTab(AiPage(container), "Copiloto IA")
        self._tabs.addTab(self._engines, "Motores")
        self._tabs.addTab(self._alerts_page, "Alertas")
        lay.addWidget(self._tabs, stretch=1)

        # «Intercambiar este token» en la cartera: se prepara el par y la vista
        # vuelve al importe. Ya no hay a dónde cambiarse —la cartera está en esta
        # misma pestaña—, así que lo que falta no es irse a otro sitio sino subir
        # al sitio donde se escribe, y con el cursor ya dentro.
        self._wallet.swap_requested.connect(self._on_swap_requested)
        # Y al revés: un token añadido por su dirección en la pestaña de swap
        # entra en la lista de la cartera sin esperar a que se relea la red.
        self._prices.token_added.connect(self._wallet.set_token_in_store)

        # Configurar la cartera en la pestaña de motores cambia si se puede
        # firmar, y los botones que firman viven en otras dos pestañas. Sin esto,
        # el usuario guardaría su clave y encontraría los botones todavía
        # apagados, que es exactamente lo que enseña a desconfiar de un botón.
        self._engines.credentials_changed.connect(self._prices.refresh_execution_state)
        self._engines.credentials_changed.connect(self._prediction.refresh_execution_state)
        # Y la cartera: la dirección que se lee **es** la que abre esa clave, así
        # que guardarla o borrarla cambia lo que hay que enseñar. Sin esto, la
        # pestaña seguiría mostrando la cartera anterior —o ninguna— con la
        # misma seguridad que si fuera la correcta.
        self._engines.credentials_changed.connect(self._wallet.refresh)

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
        # El modo decide si se puede firmar y emitir, así que la pestaña de swap
        # tiene que enterarse de que ha cambiado. Sin esto, «Ejecutar» se queda
        # como estaba y el usuario descubre el cambio al pulsarlo —que es justo
        # lo que enseña a desconfiar de los botones—.
        #
        # Y el desplegable también tiene que enterarse, porque el modo lo puede
        # cambiar la propia pestaña —el atajo «Cambiar a EJECUCIÓN»— y un
        # desplegable que sigue diciendo «OBSERVACIÓN» mientras la aplicación ya
        # firma es una pantalla que miente sobre lo que está pasando.
        container.guard.subscribe(lambda mode: self._sync_mode_combo(mode))
        container.guard.subscribe(lambda _m: self._prices.refresh_execution_state())
        # La de predicción tiene **dos** botones que firman —publicar una orden y
        # cobrar una posición ya resuelta— y los dos dependen del modo por la
        # misma razón que «Ejecutar». Se apunta aquí y no dentro de la pestaña
        # porque `subscribe` es de quien construye, no de quien se apunta.
        container.guard.subscribe(lambda _m: self._prediction.refresh_execution_state())

        # Cerrar: detener scheduler y motores.
        self._closing = False

    def _on_swap_requested(self, token) -> None:  # type: ignore[no-untyped-def]
        """Prepara el par con el token de la cartera y sube la vista al importe.

        Se prepara **antes** de desplazar: si el scroll se moviese primero, el
        usuario vería la tarjeta saltar para arriba y cambiar de contenido un
        instante después. Cuando llega, ya está donde tiene que estar.
        """
        self._prices.prepare_with_token(token)
        self._tabs.setCurrentWidget(self._prices)
        self._prices.scroll_to_converter()

    def _mode_index(self, mode: OperationMode) -> int:
        """Índice del modo en el desplegable, o 0 si no estuviera.

        La comparación es por valor y no por identidad **a propósito**, aunque
        `OperationMode` sea un `StrEnum`: Qt guarda el dato del ítem como texto,
        así que `itemData` devuelve la cadena y una comparación con `is` no
        acertaría nunca —el desplegable se quedaría siempre en el primer modo—.
        Como cada modo aparece una sola vez, el valor identifica al ítem.
        """
        for index in range(self._mode_combo.count()):
            if self._mode_combo.itemData(index) == mode:
                return index
        return 0

    def _mode_value(self) -> OperationMode | None:
        """El modo elegido, **reconstruido** desde el desplegable.

        `currentData()` no devuelve el `OperationMode` que se le dio: Qt guarda
        el dato del ítem como texto y lo devuelve como texto, y `OperationMode`
        es un `StrEnum` (el mismo tropiezo que documenta `PredictionPage`). El
        guard ya se defiende de eso, pero reconstruirlo aquí deja el dato bueno
        en el sitio donde entra, en vez de depender de que el de dentro lo
        arregle.
        """
        dato = self._mode_combo.currentData()
        return None if dato is None else OperationMode(dato)

    def _sync_mode_combo(self, mode: OperationMode) -> None:
        """Pone el desplegable donde dice el guard. Sin volver a avisar al guard.

        `setCurrentIndex` emite `currentIndexChanged` y `_on_mode_changed` sale
        sin hacer nada cuando el modo elegido ya es el del guard, así que no hay
        bucle; lo que sí hay es un desplegable que deja de mentir.
        """
        self._mode_combo.setCurrentIndex(self._mode_index(mode))

    def _on_mode_changed(self) -> None:
        mode = self._mode_value()
        if mode is None or mode == self._container.guard.mode:
            return
        self._container.guard.set_mode(mode)
        self.statusBar().showMessage(f"Modo cambiado a {mode.label}: {mode.description}", 4000)
        self._refresh_status()

    def _on_alert(self, alert) -> None:  # type: ignore[no-untyped-def]
        self._banner.show_alert(alert.title, alert.detail, alert.severity.value)
        self._refresh_status()
        # Avisar en la barra de estado de las alertas altas si no se está ya
        # en la pestaña de alertas. Se pregunta por el widget y no por un número
        # de pestaña: el orden de las pestañas ya cambió una vez, y un índice
        # escrito a mano hace que este aviso empiece a salir —o a no salir— en la
        # pestaña equivocada sin que nada falle.
        if alert.severity == Severity.HIGH and self._tabs.currentWidget() is not self._alerts_page:
            self.statusBar().showMessage(f"⚠ Alerta alta: {alert.title}", 6000)

    def _refresh_status(self) -> None:
        pending = self._alerts.unacknowledged_count
        self._alert_label.setText(f"● {pending} alerta(s)" if pending else "Sin alertas")
        mode = self._container.guard.mode
        caps = ", ".join(sorted(c.label for c in self._container.guard))
        self._status_label.setText(f"Modo {mode.label} · capacidades: {caps or 'ninguna'}")
        # La línea de debajo de la barra: qué permite el modo, en las palabras
        # del propio dominio. Se lee del guard y no se escribe aquí, para que la
        # pantalla no pueda describir un modo distinto del que el guard aplica.
        #
        # Una línea, y sólo una. La versión anterior encadenaba la descripción
        # entera del modo más el aviso, y en rojo: dos renglones de texto de
        # alarma permanente encima de todo, que es la forma más rápida de que
        # nadie lea ninguno de los dos. La descripción completa sigue estando,
        # en el tooltip y en el del desplegable; aquí va lo que decide si se
        # puede operar o no.
        permite_firmar = self._container.guard.allows(Capability.BROADCAST_TX)
        if permite_firmar:
            texto = (
                f"<b>{mode.label}</b> — puede firmar y emitir; los topes y el "
                "interruptor siguen mandando."
            )
        else:
            texto = (
                f"<b>{mode.label}</b> — no firma ni emite: lee, calcula y prepara."
            )
        self._mode_hint.setText(texto)
        self._mode_hint.setTextFormat(Qt.RichText)
        self._mode_hint.setToolTip(f"{mode.label}: {mode.description}")
        self._mode_hint.setStyleSheet(
            f"color: {COLOR_DANGER};" if permite_firmar else ""
        )
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
