"""El historial del copiloto en pantalla: los chats de ahora y los archivados.

### Dos listas y no una con un filtro

Un historial de conversaciones que crece acaba mezclando lo que se está usando
con lo que se consultó una vez. Archivado no es borrado —el chat viejo sigue
explicando por qué se hizo lo que se hizo—, así que la lista de archivados es
una sección aparte, plegada, y no una etiqueta dentro de una lista única.

### Quién decide, y quién sólo pinta

Este panel **no** escribe en el almacén: pide —«archiva este», «borra aquel»— y
lo dice por señales. Quien guarda es `ChatStore`, y quien llama al almacén es la
página. Así el panel se puede probar sin ficheros y el historial tiene un solo
escritor.

Archivar no pregunta nada —se deshace de un clic— y borrar sí, con un aviso que
dice que no hay vuelta atrás: es el único camino de esta pantalla que destruye
algo.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.chat_store import Chat
from amigocompora.ui.widgets import divider

#: Cuántos chats caben en cada lista antes de que la sección se coma la pantalla.
#: El historial completo sigue en el disco; esto acota lo que se pinta.
MAX_VISIBLES = 30


def _cuando(chat: Chat) -> str:
    """La fecha del último mensaje, en corto: es lo que ordena la lista."""
    return chat.updated_at.astimezone().strftime("%d/%m %H:%M")


class ChatRow(QFrame):
    """Una fila del historial: título, adelanto y sus dos mandos.

    El título es el botón que elige el chat —y no la fila entera— porque los
    mandos de archivar y borrar viven dentro, y una fila que se elige al pulsar
    cualquier sitio haría que borrar un chat lo abriese de paso.
    """

    selected = Signal(str)
    archive_requested = Signal(str, bool)
    delete_requested = Signal(str)

    def __init__(self, chat: Chat, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatRow")
        self._chat_id = chat.chat_id

        caja = QVBoxLayout(self)
        caja.setContentsMargins(8, 6, 6, 6)
        caja.setSpacing(2)

        arriba = QHBoxLayout()
        arriba.setContentsMargins(0, 0, 0, 0)
        arriba.setSpacing(4)
        self._titulo = QPushButton(chat.title or "(sin título)")
        self._titulo.setObjectName("chatTitle")
        self._titulo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._titulo.setToolTip(chat.title or "Este chat todavía no tiene título.")
        self._titulo.clicked.connect(lambda: self.selected.emit(self._chat_id))
        arriba.addWidget(self._titulo, 1)

        archivar = QPushButton("⇩" if not chat.archived else "⇧")
        archivar.setObjectName("rowAction")
        archivar.setFixedWidth(24)
        archivar.setCursor(Qt.CursorShape.PointingHandCursor)
        archivar.setToolTip(
            "Devolver este chat a la lista de los de ahora."
            if chat.archived
            else "Archivar este chat: sale de la lista de los de ahora y se queda "
            "en la de archivados, sin borrarse."
        )
        archivar.clicked.connect(
            lambda: self.archive_requested.emit(self._chat_id, not chat.archived)
        )
        arriba.addWidget(archivar)

        borrar = QPushButton("✕")
        borrar.setObjectName("rowAction")
        borrar.setFixedWidth(24)
        borrar.setCursor(Qt.CursorShape.PointingHandCursor)
        borrar.setToolTip("Borrar este chat y su historial. No se puede deshacer.")
        borrar.clicked.connect(lambda: self.delete_requested.emit(self._chat_id))
        arriba.addWidget(borrar)

        caja.addLayout(arriba)

        abajo = QLabel(f"{_cuando(chat)} · {chat.preview or 'vacío'}")
        abajo.setObjectName("chatPreview")
        # El adelanto es una línea: el texto entero, con sus saltos, está dentro
        # del chat y ahí es donde se lee.
        abajo.setText(abajo.text().replace("\n", " "))
        caja.addWidget(abajo)

    def set_selected(self, seleccionado: bool) -> None:
        """Pinta la fila como la elegida. Va por propiedad para que la pinte el QSS."""
        self.setProperty("selected", "true" if seleccionado else "false")
        # Qt no repinta solo cuando cambia una propiedad: hay que pedírselo.
        estilo = self.style()
        estilo.unpolish(self)
        estilo.polish(self)

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @property
    def title_button(self) -> QPushButton:
        """El botón del título, para las pruebas."""
        return self._titulo


class HistoryPanel(QWidget):
    """Las dos listas del historial, con sus mandos.

    No guarda nada: `refresh(chats)` recibe lo que hay y lo pinta. El chat
    elegido se recuerda aquí sólo para poder repintar la selección.
    """

    chat_selected = Signal(str)
    archive_requested = Signal(str, bool)
    delete_requested = Signal(str)
    new_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current: str | None = None
        self._rows: list[ChatRow] = []
        self._archived_count = 0

        caja = QVBoxLayout(self)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(6)

        nuevo = QPushButton("+  Chat nuevo")
        nuevo.setObjectName("secondary")
        nuevo.setToolTip(
            "Empezar una conversación nueva. Se guarda sola en cuanto escribas el "
            "primer mensaje."
        )
        nuevo.clicked.connect(self.new_requested.emit)
        caja.addWidget(nuevo)

        self._lista = QVBoxLayout()
        self._lista.setContentsMargins(0, 0, 0, 0)
        self._lista.setSpacing(2)
        caja.addLayout(self._lista)

        self._vacio = QLabel("Todavía no hay conversaciones.")
        self._vacio.setObjectName("hint")
        self._vacio.setWordWrap(True)
        caja.addWidget(self._vacio)

        self._separador = divider()
        caja.addWidget(self._separador)

        self._toggle = QPushButton("Archivados")
        self._toggle.setObjectName("link")
        self._toggle.setCheckable(True)
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.setToolTip("Mostrar u ocultar los chats archivados.")
        self._toggle.toggled.connect(self._on_toggle_archived)
        caja.addWidget(self._toggle, 0, Qt.AlignLeft)

        self._caja_archivados = QVBoxLayout()
        self._caja_archivados.setContentsMargins(0, 0, 0, 0)
        self._caja_archivados.setSpacing(2)
        caja.addLayout(self._caja_archivados)

        self._vacio_archivados = QLabel("No hay chats archivados.")
        self._vacio_archivados.setObjectName("hint")
        self._vacio_archivados.setVisible(False)
        caja.addWidget(self._vacio_archivados)

        caja.addStretch(1)
        self._on_toggle_archived(False)

    # ------------------------------------------------------------------ #
    def refresh(self, chats: Sequence[Chat]) -> None:
        """Repinta las dos listas con lo que hay, conservando la selección."""
        self._clear(self._lista)
        self._clear(self._caja_archivados)
        self._rows = []

        activos = [chat for chat in chats if not chat.archived][:MAX_VISIBLES]
        archivados = [chat for chat in chats if chat.archived][:MAX_VISIBLES]
        self._archived_count = len(archivados)
        for chat in activos:
            self._lista.addWidget(self._row(chat))
        for chat in archivados:
            self._caja_archivados.addWidget(self._row(chat))

        self._vacio.setVisible(not activos)
        self._separador.setVisible(bool(archivados))
        self._toggle.setVisible(bool(archivados))
        # Las filas recién creadas nacen visibles y la sección puede estar
        # plegada: se vuelve a aplicar el plegado, que es también lo que deja el
        # mando con su cuenta. Sin esto, repintar la lista con la sección cerrada
        # enseñaría los archivados —justo lo que archivar venía a evitar—.
        self._on_toggle_archived(self._toggle.isChecked())
        self._sync_selection()

    def set_current(self, chat_id: str | None) -> None:
        """Marca el chat elegido. Con `None` no hay ninguno, y se nota."""
        self._current = chat_id
        self._sync_selection()

    def current(self) -> str | None:
        """El chat elegido, o `None` si todavía no se ha abierto ninguno."""
        return self._current

    def rows(self) -> tuple[ChatRow, ...]:
        """Las filas pintadas, en orden. Sólo las pruebas las miran."""
        return tuple(self._rows)

    # ------------------------------------------------------------------ #
    def _row(self, chat: Chat) -> ChatRow:
        fila = ChatRow(chat, self)
        fila.selected.connect(self._on_selected)
        fila.archive_requested.connect(self.archive_requested.emit)
        fila.delete_requested.connect(self._on_delete)
        self._rows.append(fila)
        return fila

    def _on_selected(self, chat_id: str) -> None:
        self._current = chat_id
        self._sync_selection()
        self.chat_selected.emit(chat_id)

    def _on_delete(self, chat_id: str) -> None:
        """Borra, después de preguntar. Es lo único que destruye historial aquí.

        El aviso se abre con el chat delante y dice que no hay vuelta atrás: un
        borrado sin confirmación en una lista de un clic sería la forma más fácil
        de perder una conversación entera sin querer.
        """
        respuesta = QMessageBox.question(
            self,
            "Borrar el chat",
            "Se borra esta conversación entera, con sus mensajes. No se puede "
            "deshacer.\n\nSi sólo quieres apartarla, usa «archivar»: el chat se "
            "queda en la lista de archivados.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if respuesta == QMessageBox.StandardButton.Yes:
            if self._current == chat_id:
                self._current = None
            self.delete_requested.emit(chat_id)

    def _on_toggle_archived(self, abierto: bool) -> None:
        """Pliega o despliega la sección, reetiquetando el mando.

        La sección se pliega **de verdad** —los widgets se ocultan, no se
        vacían—: plegar no puede perder nada, o volver a abrirla mostraría menos
        de lo que había.
        """
        for indice in range(self._caja_archivados.count()):
            item = self._caja_archivados.itemAt(indice)
            widget = None if item is None else item.widget()
            if widget is not None:
                widget.setVisible(abierto)
        self._vacio_archivados.setVisible(abierto and self._archived_count == 0)
        self._refresh_toggle()

    def _refresh_toggle(self) -> None:
        flecha = "▾" if self._toggle.isChecked() else "▸"
        cuenta = f" ({self._archived_count})" if self._archived_count else ""
        self._toggle.setText(f"{flecha}  Archivados{cuenta}")

    def _sync_selection(self) -> None:
        for fila in self._rows:
            fila.set_selected(fila.chat_id == self._current)

    def _clear(self, caja: QVBoxLayout) -> None:
        while caja.count():
            item = caja.takeAt(0)
            widget = None if item is None else item.widget()
            if widget is not None:
                widget.deleteLater()
