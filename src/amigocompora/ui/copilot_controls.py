"""Los mandos del copiloto: con qué modelo habla, y si puede ejecutar solo.

### Por qué son dos piezas y no campos de la página

Son los dos únicos mandos del copiloto que cambian algo **fuera** de la
conversación: el modelo se cambia en el registro de motores —y se deja escrito en
`config.toml`— y la autonomía arma la ejecución desatendida. Tenerlos aparte deja
la página con una sola responsabilidad, la conversación, y —lo que importa más—
hace que el mando que autoriza a emitir sin preguntar se pueda leer y probar por
separado del chat.

### El modelo: uno a la vez

El copiloto le pregunta a **un** motor: el registro devuelve el preferido de la
ranura. Por eso elegir aquí un modelo deja la ranura con ése y apaga los demás.
Con dos encendidos el que contesta no sería el elegido, y un desplegable que dice
«DeepSeek» mientras contesta otro es una pantalla que miente. Lo que se apagó se
dice al lado, y volver a encenderlo es cosa de la pestaña de Motores: allí están
las ranuras, y un mando que además tocara otras sería un mando que hace dos cosas.

### Armar pide la frase, y desarmar no pide nada

Armar la autonomía es declarar que la aplicación puede firmar y emitir sin
consultar. Lo que lo convierte en un acto deliberado es la frase: se pide al
armar —con el texto oculto— y se compara contra la guardada, y sin frase
configurada ni se ofrece. Desarmar, en cambio, es el freno: no pregunta, no pide
frase y no falla nunca, porque un freno que puede negarse a frenar no sirve.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.protocols import EngineKind
from amigocompora.infra.config import config_file
from amigocompora.infra.config_writer import set_active_engines
from amigocompora.ui.theme import COLOR_DANGER, COLOR_MUTED
from amigocompora.ui.widgets import format_amount, spawn


class ModelPicker(QWidget):
    """Elegir con qué modelo conversa el copiloto, y dejarlo escrito.

    El desplegable se rellena con **todos** los motores de IA instalados, no sólo
    con los encendidos: elegir un modelo apagado es el caso normal —es la forma de
    encenderlo sin ir a otra pestaña—, y una lista con lo ya elegido no dejaría
    elegir nada. Al elegir: se enciende el nuevo, se apaga el anterior si lo
    había, y la ranura queda escrita en `config.toml`.
    """

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._loading = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        etiqueta = QLabel("Modelo")
        etiqueta.setObjectName("hint")
        lay.addWidget(etiqueta)

        self._combo = QComboBox()
        anchura = 220
        self._combo.setMinimumWidth(anchura)
        self._combo.setToolTip(
            "Con qué modelo conversa el copiloto. El elegido queda encendido en su "
            "ranura y los demás se apagan: el copiloto pregunta a uno, y este es."
        )
        self._combo.currentIndexChanged.connect(self._on_changed)
        lay.addWidget(self._combo)

        self._message = QLabel("")
        self._message.setObjectName("hint")
        self._message.setWordWrap(True)
        lay.addWidget(self._message, 1)

        self.refresh()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """Repinta la lista con lo que hay instalado y enciende lo que esté activo."""
        reg = self._container.registry
        activos = [engine.manifest.engine_id for engine in reg.active_stack(EngineKind.AI_ADVISOR)]
        preferido = activos[0] if activos else ""

        self._loading = True
        try:
            self._combo.clear()
            for entry in reg.available(EngineKind.AI_ADVISOR):
                nombre = self._container.engine_names.resolve(entry.engine_id, entry.manifest.name)
                self._combo.addItem(nombre, entry.engine_id)
            indice = self._combo.findData(preferido)
            self._combo.setCurrentIndex(indice)
        finally:
            self._loading = False

        self._combo.setEnabled(self._combo.count() > 0)
        if self._combo.count() == 0:
            self._set_message("No hay ningún modelo de IA instalado.")
        elif not preferido:
            self._set_message("Sin modelo encendido: elige uno para poder preguntar.")

    def combo(self) -> QComboBox:
        """El desplegable, para las pruebas."""
        return self._combo

    def message(self) -> str:
        """Lo que dice la línea de estado, para las pruebas."""
        return self._message.text()

    # ------------------------------------------------------------------ #
    def _on_changed(self, _indice: int) -> None:
        if self._loading:
            return
        engine_id = self._combo.currentData()
        if not isinstance(engine_id, str) or not engine_id:
            return
        spawn(self._apply(engine_id))

    async def _apply(self, engine_id: str) -> None:
        """Enciende el elegido, apaga el resto de la ranura y lo guarda.

        El orden importa: primero se enciende el nuevo y sólo después se apaga el
        viejo. Al revés —apagar y luego encender—, un motor que no arranca dejaría
        al copiloto sin ninguno y el usuario con un chat que no contesta; así, en
        el peor caso quedan dos encendidos, que es lo que ya había.
        """
        reg = self._container.registry
        nombre = self._container.engine_names.resolve(engine_id, engine_id)
        apagados: list[str] = []
        self._combo.setEnabled(False)
        try:
            await reg.add(engine_id)
            for engine in reg.active_stack(EngineKind.AI_ADVISOR):
                otro = engine.manifest.engine_id
                if otro != engine_id:
                    await reg.remove(otro)
                    apagados.append(otro)
        except Exception as error:
            self._set_message(f"No se pudo cambiar el modelo a «{nombre}»: {error}")
        else:
            aviso = self._save()
            dicho = f"El copiloto usa «{nombre}»."
            if apagados:
                dicho += f" Se apagó {', '.join(f'«{otro}»' for otro in apagados)}."
            self._set_message(dicho + aviso)
        finally:
            self.refresh()

    def _save(self) -> str:
        """Deja la ranura escrita. Devuelve el aviso si no se pudo, o vacío."""
        ids = [
            engine.manifest.engine_id
            for engine in self._container.registry.active_stack(EngineKind.AI_ADVISOR)
        ]
        try:
            set_active_engines(config_file(), EngineKind.AI_ADVISOR.value, ids)
        except Exception as error:
            return f" No se pudo guardar en config.toml ({error}): vale para esta sesión."
        return ""

    def _set_message(self, texto: str) -> None:
        self._message.setText(texto)


class AutonomyStrip(QWidget):
    """El estado de la ejecución desatendida, con su mando para armarla.

    Enseña las tres cosas que hacen falta para decidir con conocimiento: si está
    armada, cuánto se ha gastado en las últimas 24 h —en la unidad de cada asiento,
    porque un número sin unidad no dice si son cien dólares o cien mil— y hasta
    dónde llegan los topes.

    Con los límites apagados lo dice en vez de ofrecer el mando: armar la
    autonomía con `enabled = false` no armaría nada, y un botón que no puede
    cumplir lo que promete es peor que no tenerlo.
    """

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container

        caja = QVBoxLayout(self)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(4)

        fila = QHBoxLayout()
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(8)

        self._state = QLabel("")
        fila.addWidget(self._state)

        self._arm_btn = QPushButton("Armar…")
        self._arm_btn.setObjectName("secondary")
        self._arm_btn.setToolTip(
            "Permite que la aplicación firme y emita **sin preguntarte**, dentro de "
            "los topes declarados. Pide la frase de autonomía."
        )
        self._arm_btn.clicked.connect(self._on_arm)
        fila.addWidget(self._arm_btn)

        self._disarm_btn = QPushButton("Desarmar")
        self._disarm_btn.setObjectName("danger")
        self._disarm_btn.setToolTip(
            "Detiene la ejecución desatendida. No pide nada y surte efecto al "
            "instante: es el freno."
        )
        self._disarm_btn.clicked.connect(self._on_disarm)
        fila.addWidget(self._disarm_btn)

        fila.addStretch()
        caja.addLayout(fila)

        self._detail = QLabel("")
        self._detail.setObjectName("hint")
        self._detail.setWordWrap(True)
        caja.addWidget(self._detail)

        self._note = QLabel("")
        self._note.setWordWrap(True)
        caja.addWidget(self._note)

        # El estado puede cambiar sin pasar por aquí —al arrancar con la
        # autonomía configurada, o al desaparecer la clave del llavero, que la
        # desarma sola—: un indicador que sólo se pintara al construirse diría
        # «desarmada» sobre una aplicación que ya está emitiendo.
        container.policy.subscribe(lambda _armed: self.refresh())
        self.refresh()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """Repinta el estado, el gasto y los topes."""
        policy = self._container.policy
        armed = policy.armed
        self._arm_btn.setVisible(not armed)
        self._disarm_btn.setVisible(armed)

        if armed:
            self._state.setText("● Ejecución desatendida ARMADA · emite sin preguntar")
            self._state.setStyleSheet(f"color: {COLOR_DANGER}; font-weight: 600;")
        else:
            self._state.setText("Autonomía desarmada · cada operación te pide confirmación")
            self._state.setStyleSheet(f"color: {COLOR_MUTED};")

        self._detail.setText(self._spending_text())

        if armed:
            self._set_note("", "")
        elif not policy.limits.enabled:
            self._set_note(
                f"La ejecución está apagada (`enabled = true` bajo `[execution]` en "
                f"config.toml): armar no haría nada. {self._why_cannot_arm()}",
                COLOR_MUTED,
            )
        else:
            self._set_note(self._why_cannot_arm(), COLOR_MUTED)

    def state_text(self) -> str:
        """Lo que dice el rótulo del estado, para las pruebas."""
        return self._state.text()

    def detail_text(self) -> str:
        """Lo que dice la línea de gasto y topes, para las pruebas."""
        return self._detail.text()

    def note_text(self) -> str:
        """Lo que dice la letra pequeña, para las pruebas."""
        return self._note.text()

    def arm_button(self) -> QPushButton:
        """El mando de armar, para las pruebas."""
        return self._arm_btn

    def disarm_button(self) -> QPushButton:
        """El mando de desarmar, para las pruebas."""
        return self._disarm_btn

    # ------------------------------------------------------------------ #
    def _spending_text(self) -> str:
        """Lo gastado en 24 h y los topes, tal como los declara la política.

        Lo gastado se desglosa por unidad: la suma sola sería un número sin
        unidad, y esa es justo la cifra que se mira para saber si se puede armar
        esto con tranquilidad.
        """
        limites = self._container.policy.limits
        por_operacion = (
            f"por operación {format_amount(limites.max_quote_per_trade)}"
            if limites.max_quote_per_trade is not None
            else "sin tope por operación"
        )
        cada_dia = (
            f"cada 24 h {format_amount(limites.max_quote_per_day)}"
            if limites.max_quote_per_day is not None
            else "sin tope diario"
        )
        gastado = self._container.policy.ledger.spent_today_by_symbol()
        if gastado:
            dicho = ", ".join(
                f"{format_amount(total)} {symbol}" for symbol, total in gastado
            )
        else:
            dicho = "nada"
        return f"Gastado en las últimas 24 h: {dicho}. Topes: {por_operacion} · {cada_dia}."

    def _why_cannot_arm(self) -> str:
        """Qué falta para poder armar, nombrando **dónde** se arregla."""
        policy = self._container.policy
        if policy.can_arm:
            return ""
        if not self._container.keys.available():
            return "Para armarla falta la cartera con la que firmar (pestaña Cartera)."
        if not self._container.passphrase.get():
            return (
                "Para armarla falta la frase de autonomía "
                "(Configuración → Credenciales)."
            )
        return ""

    def _set_note(self, texto: str, color: str) -> None:
        self._note.setText(texto)
        self._note.setStyleSheet(f"color: {color};")
        self._note.setVisible(bool(texto))

    def _on_arm(self) -> None:
        """Pide la frase y arma. La frase es lo que convierte esto en deliberado."""
        frase, aceptado = QInputDialog.getText(
            self,
            "Armar la ejecución desatendida",
            "Frase de autonomía:",
            QLineEdit.EchoMode.Password,
        )
        if not aceptado:
            return
        try:
            self._container.policy.arm(frase)
        except ExecutionError as error:
            self._set_note(f"No se pudo armar: {error}", COLOR_DANGER)
            return
        self._set_note(
            "Armada: dentro de los topes, la aplicación firmará y emitirá sin "
            "preguntarte. «Desarmar» lo detiene al instante.",
            COLOR_DANGER,
        )
        self.refresh()

    def _on_disarm(self) -> None:
        """Desarma sin preguntar. Es el freno, y un freno no se bloquea."""
        self._container.policy.disarm()
        self._set_note("Desarmada por ti. Las operaciones vuelven a pedirte «sí».", COLOR_MUTED)
        self.refresh()
