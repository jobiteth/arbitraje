"""El chat del copiloto en pantalla: burbujas, lo consultado y la propuesta.

### Qué enseña un turno, y por qué lo enseña mientras ocurre

Un turno del copiloto puede tardar segundos: pide una herramienta, espera a la
red, lee el resultado y vuelve a preguntarle al modelo. Durante esos segundos lo
único honesto es decir qué se está haciendo, y al terminar poder enseñar **qué se
consultó**, que es la prueba de dónde salió cada cifra. Por eso la burbuja del
copiloto tiene dos partes: el texto —la respuesta— y, plegado debajo, el registro
de las herramientas, con la salida en crudo y con la fecha del turno.

### La propuesta se firma donde siempre

La tarjeta de propuesta **no** ejecuta nada: dice lo que se propone, avisa de que
todavía no se ha firmado y, si el camino no está abierto, enseña qué falta. El
botón sólo pide —emite `execute_requested`— y quien escucha es la página, que
entra por el mismo caso de uso que la pantalla de conversión, con sus diálogos de
confirmación y sus topes. Es la regla de la casa puesta en un widget: la IA
propone, el usuario decide.

### Recargar un chat no vuelve a ofrecer su propuesta

De un turno se guarda el texto y lo que se consultó, no la cotización. Una
propuesta recargada sería una cifra vieja con un botón de firmar al lado, así que
no se recarga: en el historial queda dicho qué se propuso, y para ejecutarlo se
vuelve a pedir —con su cotización de ahora—. Es la misma razón por la que el
precio se mide otra vez antes de firmar.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.chat_store import ChatMessage, ChatRole
from amigocompora.app.copilot import (
    CopilotEvent,
    CopilotEventKind,
    CopilotProposal,
    CopilotReply,
)
from amigocompora.app.usecases.execute_swap import StepUpdate
from amigocompora.ui.swap_steps import SwapStepsPanel
from amigocompora.ui.theme import COLOR_DANGER, COLOR_MUTED
from amigocompora.ui.widgets import Card

#: Cómo se llama cada voz en pantalla. El usuario es «Tú» porque en una
#: conversación uno no se llama por su propio nombre.
_VOICES: dict[ChatRole, str] = {
    ChatRole.USER: "Tú",
    ChatRole.COPILOT: "Copiloto",
    ChatRole.TOOL: "Herramienta",
}

#: Cuánto detalle de una herramienta cabe en su fila mientras se narra el turno.
#: El texto entero está en el tooltip y en el cajón «lo que se consultó»: una fila
#: de cien líneas dentro del chat no se lee, se salta.
MAX_ACTIVITY_CHARS = 160

#: Lo que se ve en un chat recién abierto. Dice qué se puede preguntar —y con qué
#: ejemplos— porque un chat vacío sin ejemplos se queda vacío: no se sabe si
#: espera una pregunta sobre precios, una dirección o una operación.
EMPTY_HINT = (
    "Pregúntale al copiloto lo que quieras saber del mercado, y él consulta "
    "los datos por su cuenta.\n\nPor ejemplo: «¿qué saldo tiene "
    "0x2c88…44f9?», «¿cuánto me dan por 50 USDC en Polygon?», «¿cuánto cuesta "
    "pasar 20 USDC de Base a Polygon?» o «cámbiame 50 USDC por WPOL».\n\n"
    "El copiloto sólo lee y propone: nada se firma hasta que tú lo confirmes."
)


def chain_key_of(proposal: CopilotProposal) -> str:
    """La red de la operación propuesta, para saber dónde mirar su explorador.

    Se saca de la cotización —y no se pregunta aparte— porque es la misma red que
    la del swap o la de origen del puente: preguntarla dos veces sería dos
    respuestas que pueden no coincidir.
    """
    if proposal.quote is not None:
        return proposal.quote.pair.chain
    if proposal.bridge is not None:
        return proposal.bridge.request.origin.chain
    return ""


def detail_of(reply: CopilotReply) -> str:
    """Lo que se guarda de un turno, además de su texto: hallazgos y consultas.

    Se guarda ya montado —y no como pasos sueltos— porque es lo que se va a
    enseñar y nada más: al reabrir un chat no se reconstruye la conversación, se
    lee lo que se anotó. Los hallazgos van aquí y no en el texto porque son la
    letra pequeña de la respuesta: se leen cuando se quiere comprobar de dónde
    sale, no cada vez.
    """
    partes: list[str] = []
    if reply.findings:
        partes.append("Hallazgos:\n" + "\n".join(f"• {linea}" for linea in reply.findings))
    if reply.tool_events:
        lineas: list[str] = []
        for evento in reply.tool_events:
            flecha = "→" if evento.kind is CopilotEventKind.TOOL else "←"
            suffix = f": {evento.detail}" if evento.detail else ""
            lineas.append(f"{flecha} {evento.title}{suffix}")
        partes.append("Lo que se consultó:\n" + "\n".join(lineas))
    return "\n\n".join(partes)


# --------------------------------------------------------------------------- #
# Piezas
# --------------------------------------------------------------------------- #
class Foldable(QWidget):
    """Un mando que abre y cierra lo que lleva debajo, y el cajón con el contenido.

    Nace plegado porque lo que guarda es la letra pequeña —lo que el copiloto
    consultó, en crudo—: se abre cuando se quiere comprobar una cifra y mientras
    tanto no compite con la respuesta, que es lo que se viene a leer. El contenido
    se añade después, así que el mismo cajón sirve para lo que está pasando ahora.
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._base = title
        self._open = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        self._btn = QPushButton()
        self._btn.setObjectName("link")
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self._toggle)
        lay.addWidget(self._btn, 0, Qt.AlignmentFlag.AlignLeft)

        self._caja = QFrame()
        self._caja.setObjectName("toolDetail")
        self._rows = QVBoxLayout(self._caja)
        self._rows.setContentsMargins(10, 8, 10, 8)
        self._rows.setSpacing(4)
        self._caja.setVisible(False)
        lay.addWidget(self._caja)
        self._refresh()

    def add_row(self, widget: QWidget) -> None:
        self._rows.addWidget(widget)
        self._refresh()

    def add_text(self, texto: str) -> QLabel:
        """Añade un párrafo de texto en crudo y devuelve su rótulo."""
        etiqueta = QLabel(texto)
        etiqueta.setWordWrap(True)
        etiqueta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.add_row(etiqueta)
        return etiqueta

    def is_open(self) -> bool:
        return self._open

    def set_open(self, abierto: bool) -> None:
        self._open = abierto
        self._caja.setVisible(abierto)
        self._refresh()

    def has_content(self) -> bool:
        """Si hay algo que enseñar. Con el cajón vacío no se pinta ni el mando."""
        return self._rows.count() > 0

    def button(self) -> QPushButton:
        """El mando que pliega, para las pruebas."""
        return self._btn

    def _toggle(self) -> None:
        self.set_open(not self._open)

    def _refresh(self) -> None:
        flecha = "▾" if self._open else "▸"
        self._btn.setText(f"{flecha} {self._base}")


class _ActivityRow(QFrame):
    """Una línea de lo que está pasando: glifo, qué y —si lo hay— el detalle."""

    def __init__(
        self,
        glyph: str,
        titulo: str,
        detalle: str = "",
        *,
        danger: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("toolEvent")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._glyph = QLabel(glyph)
        self._glyph.setFixedWidth(14)
        self._glyph.setStyleSheet(
            f"color: {COLOR_DANGER if danger else COLOR_MUTED}; font-weight: 700;"
        )
        lay.addWidget(self._glyph, 0, Qt.AlignmentFlag.AlignTop)

        self._texto = QLabel()
        self._texto.setWordWrap(True)
        self._texto.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._texto, 1)
        self.apply(titulo, detalle)

    def apply(self, titulo: str, detalle: str) -> None:
        recortado = " ".join(detalle.split())
        if len(recortado) > MAX_ACTIVITY_CHARS:
            recortado = recortado[: MAX_ACTIVITY_CHARS - 1].rstrip() + "…"
        # El detalle en gris detrás del título: el título dice qué se pidió y el
        # detalle, con qué.
        texto = f"<b>{titulo}</b>"
        if recortado:
            texto += f' <span style="color:{COLOR_MUTED};">{_escapar(recortado)}</span>'
        self._texto.setText(texto)
        self.setToolTip(detalle)

    def text(self) -> str:
        """Lo que dice la fila, para las pruebas."""
        return self._texto.text()


class TurnActivity(QFrame):
    """Una línea por paso del turno, mientras el turno ocurre.

    Sólo registra lo que se consultó —que se pidió una herramienta y lo que
    devolvió—: la respuesta, la propuesta y el error se leen en el texto de la
    burbuja, y repetirlos aquí contaría dos veces lo mismo.

    El resultado se cuelga de la fila de su petición, en orden de llegada y no
    buscando el nombre dentro del texto: la pantalla no tiene por qué leer los
    rótulos que le manda el caso de uso.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pending: _ActivityRow | None = None
        self._filas: list[_ActivityRow] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self._lay = lay

    def show_event(self, evento: CopilotEvent) -> None:
        """Pinta un paso del turno. Lo que no sea un paso se ignora."""
        if evento.kind is CopilotEventKind.TOOL:
            self._pending = self._add("⟳", evento.title, evento.detail)
        elif evento.kind is CopilotEventKind.TOOL_RESULT:
            fila = self._pending
            self._pending = None
            if fila is None:
                self._add("✓", evento.title, evento.detail)
            else:
                fila.apply(evento.title, evento.detail)
        elif evento.kind is CopilotEventKind.ERROR:
            self._add("✕", evento.title, evento.detail, danger=True)

    def lines(self) -> tuple[str, ...]:
        """Lo que dicen las filas, en orden. Sólo las pruebas lo miran."""
        return tuple(fila.text() for fila in self._filas)

    def _add(
        self, glyph: str, titulo: str, detalle: str = "", *, danger: bool = False
    ) -> _ActivityRow:
        fila = _ActivityRow(glyph, titulo, detalle, danger=danger, parent=self)
        self._filas.append(fila)
        self._lay.addWidget(fila)
        return fila


class MessageBubble(QFrame):
    """Una línea de la conversación, con la voz que la dijo.

    La del usuario va con el acento y las demás con el fondo elevado: de un
    vistazo se distingue quién habló sin leer el rótulo.
    """

    def __init__(
        self,
        role: ChatRole,
        text: str = "",
        *,
        detail: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._role = role
        self.setObjectName(_object_name_for(role))

        caja = QVBoxLayout(self)
        caja.setContentsMargins(12, 9, 12, 9)
        caja.setSpacing(4)
        self._caja = caja

        voz = QLabel(_VOICES[role])
        voz.setObjectName("hint")
        caja.addWidget(voz)

        self._activity: TurnActivity | None = None

        self._text = QLabel(text)
        self._text.setWordWrap(True)
        self._text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        caja.addWidget(self._text)

        self._findings = QLabel("")
        self._findings.setWordWrap(True)
        self._findings.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._findings.setVisible(False)
        caja.addWidget(self._findings)

        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        caja.addWidget(self._status)

        self._detail = Foldable("Lo que se consultó", self)
        self._detail.setVisible(False)
        caja.addWidget(self._detail)
        if detail:
            self.set_detail(detail)

    # ------------------------------------------------------------------ #
    def activity(self) -> TurnActivity:
        """El registro en vivo del turno, creado la primera vez que se pide.

        Está antes del texto a propósito: mientras el turno corre, lo que importa
        es qué se está consultando; la respuesta ocupa su sitio cuando llegue.
        """
        if self._activity is None:
            self._activity = TurnActivity(self)
            # Justo debajo de la voz y encima del texto: mientras el turno corre,
            # lo que está pasando es lo primero que se lee.
            self._caja.insertWidget(1, self._activity)
        return self._activity

    def set_text(self, texto: str) -> None:
        self._text.setText(texto)

    def text(self) -> str:
        """Lo que dice la burbuja, para las pruebas."""
        return self._text.text()

    def set_findings(self, findings: Sequence[str]) -> None:
        """Los hallazgos, como viñetas debajo de la respuesta."""
        self._findings.setText("\n".join(f"• {linea}" for linea in findings))
        self._findings.setVisible(bool(findings))

    def set_status(self, texto: str) -> None:
        """La línea de estado: «Pensando…» mientras corre, o el aviso del final."""
        self._status.setText(texto)
        self._status.setVisible(bool(texto))

    def set_detail(self, texto: str) -> None:
        """Lo que se consultó, plegado. Sin texto, el cajón no aparece."""
        if not texto:
            self._detail.setVisible(False)
            return
        cajon = self._detail
        if not cajon.has_content():
            cajon.add_text(texto)
        cajon.setVisible(True)

    def detail_text(self) -> str:
        """El texto del cajón, plegado o no. Sólo las pruebas lo miran."""
        for fila in self._detail.findChildren(QLabel):
            if fila.text():
                return fila.text()
        return ""

    def detail_button(self) -> QPushButton:
        """El mando que abre lo consultado. Sólo las pruebas lo miran."""
        return self._detail.button()

    def role(self) -> ChatRole:
        return self._role


def _object_name_for(role: ChatRole) -> str:
    if role is ChatRole.USER:
        return "bubbleUser"
    if role is ChatRole.COPILOT:
        return "bubbleCopilot"
    return "toolEvent"


def _escapar(texto: str) -> str:
    """El texto que va dentro de un rótulo con formato, sin poder romperlo.

    La salida de una herramienta es texto de fuera —un símbolo de token, un
    mensaje de error de una API— y dentro de un `<span>` un `<` suelto se come el
    resto de la línea.
    """
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
# La propuesta
# --------------------------------------------------------------------------- #
class ProposalCard(Card):
    """Lo que el copiloto propone, y el botón que entra por el camino de siempre.

    No ejecuta ni comprueba nada por su cuenta: la lista de lo que falta se la
    pasa la página —con las mismas funciones que consultan las otras pantallas—,
    y el botón sólo emite. Lo que sí hace es decir, siempre, que todavía no se ha
    firmado nada: es la frase que separa una sugerencia de una operación.
    """

    execute_requested = Signal()

    def __init__(self, proposal: CopilotProposal, parent: QWidget | None = None) -> None:
        super().__init__("Operación propuesta", parent)
        self._proposal = proposal

        body = self.body()
        self._summary = QLabel(proposal.summary or "Sin resumen.")
        self._summary.setWordWrap(True)
        self._summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self._summary)

        self._error = QLabel(proposal.error)
        self._error.setObjectName("danger")
        self._error.setWordWrap(True)
        self._error.setVisible(bool(proposal.error))
        body.addWidget(self._error)

        self._note = QLabel(
            "Todavía no se ha firmado nada. El botón abre la ejecución de "
            "siempre: con su confirmación —o con la autonomía ya armada, dentro "
            "de sus límites—."
        )
        self._note.setObjectName("hint")
        self._note.setWordWrap(True)
        self._note.setVisible(proposal.ready)
        body.addWidget(self._note)

        self._blockers = QLabel("")
        self._blockers.setObjectName("danger")
        self._blockers.setWordWrap(True)
        self._blockers.setVisible(False)
        body.addWidget(self._blockers)

        fila = QHBoxLayout()
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(8)
        self._btn = QPushButton("Revisar y ejecutar")
        self._btn.setToolTip(
            "Ejecutar esta operación por el camino de siempre: permisos, coste de "
            "red y confirmación antes de firmar."
        )
        self._btn.setVisible(proposal.ready)
        self._btn.clicked.connect(self.execute_requested.emit)
        fila.addWidget(self._btn)

        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.RichText)
        fila.addWidget(self._status)
        fila.addStretch()
        body.addLayout(fila)

        self._status_text = ""
        self._spent = False
        self._steps: SwapStepsPanel | None = None

    # ------------------------------------------------------------------ #
    def proposal(self) -> CopilotProposal:
        """La propuesta que se está enseñando, con su cifra observada."""
        return self._proposal

    def button(self) -> QPushButton:
        """El botón de ejecutar, para las pruebas."""
        return self._btn

    def set_blockers(self, motivos: tuple[str, ...]) -> None:
        """Lo que falta para poder firmar, tal como lo dirá la ejecución.

        Una propuesta ya ejecutada no se vuelve a habilitar aunque después no
        falte nada: la cifra que llevaba ya se gastó, y un botón que reaparece
        encendido sobre una cotización consumida es una invitación a firmar dos
        veces lo mismo.
        """
        self._blockers.setText(
            "Para ejecutarlo falta: " + "; ".join(motivos) if motivos else ""
        )
        self._blockers.setVisible(bool(motivos))
        self._btn.setEnabled(not motivos and not self._spent)

    def blockers_text(self) -> str:
        """Lo que dice la lista de lo que falta, para las pruebas."""
        return self._blockers.text()

    def set_status(self, texto: str, *, link: str | None = None) -> None:
        """La línea del final: «Ejecutando…», «✓ Emitida …» o el motivo.

        Con `link` se añade el enlace al explorador de la red. El texto se escapa
        antes de meterlo en el rótulo: lo que llega aquí puede ser el mensaje de
        error de una API, y un `<` suelto se comería la mitad de la línea.
        """
        self._status_text = texto
        if link is None:
            self._status.setText(_escapar(texto))
        else:
            self._status.setText(
                f'{_escapar(texto)} <a href="{link}">ver en el explorador</a>'
            )

    def status_text(self) -> str:
        """Lo que dice la línea de estado, para las pruebas."""
        return self._status_text

    def mark_spent(self) -> None:
        """Marca la propuesta como ya ejecutada: su botón no vuelve a encenderse."""
        self._spent = True
        self._btn.setEnabled(False)

    def is_spent(self) -> bool:
        """Si la propuesta ya se ejecutó."""
        return self._spent

    def start_steps(self, chain_key: str) -> SwapStepsPanel:
        """Prepara la lista de transacciones y la enseña. Se crea al primer uso.

        La lista se construye aquí y no al enseñar la propuesta porque la mayoría
        de las propuestas no se llegan a ejecutar: montarla siempre sería pintar
        un panel vacío bajo cada sugerencia.
        """
        if self._steps is None:
            self._steps = SwapStepsPanel(self)
            self.body().addWidget(self._steps)
        self._steps.start(chain_key)
        return self._steps

    def record(self, update: StepUpdate) -> None:
        """Añade un aviso de progreso a la lista de transacciones."""
        if self._steps is not None:
            self._steps.record(update)

    def steps(self) -> SwapStepsPanel | None:
        """La lista de transacciones, si llegó a crearse."""
        return self._steps


# --------------------------------------------------------------------------- #
# La conversación
# --------------------------------------------------------------------------- #
class ChatView(QWidget):
    """La conversación: las burbujas en orden y, debajo, la propuesta del turno.

    No sabe nada del almacén ni del copiloto: recibe mensajes y eventos ya hechos
    y los pinta. `clear()` la deja como recién construida, que es lo que hace
    falta al cambiar de chat.
    """

    execute_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        outer.addWidget(self._scroll)

        contenido = QWidget()
        contenido.setObjectName("scrollContent")
        self._body = QVBoxLayout(contenido)
        self._body.setContentsMargins(2, 2, 8, 2)
        self._body.setSpacing(8)

        self._empty = QLabel(EMPTY_HINT)
        self._empty.setObjectName("hint")
        self._empty.setWordWrap(True)
        self._body.addWidget(self._empty)

        # El estirado del final: las burbujas se apilan arriba y el hueco queda
        # abajo, que es donde se escribe. Todo se inserta **antes** de él.
        self._body.addStretch(1)
        self._scroll.setWidget(contenido)

        self._bubbles: list[MessageBubble] = []
        self._notes: list[QLabel] = []
        self._proposal: ProposalCard | None = None

    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        """Deja la vista como recién construida: sin burbujas y sin propuesta."""
        self.clear_proposal()
        for widget in (*self._bubbles, *self._notes):
            self._body.removeWidget(widget)
            widget.deleteLater()
        self._bubbles = []
        self._notes = []
        self._empty.setVisible(True)

    def add(self, message: ChatMessage) -> MessageBubble:
        """Pinta un mensaje guardado: el texto y, si lo trae, su letra pequeña."""
        burbuja = MessageBubble(message.role, message.text, detail=message.detail, parent=self)
        self._insert(burbuja)
        self._bubbles.append(burbuja)
        self._empty.setVisible(False)
        self._to_bottom()
        return burbuja

    def begin_turn(self) -> MessageBubble:
        """Abre la burbuja del copiloto para el turno que empieza ahora.

        Nace con el estado «Pensando…» y sin texto: lo que se enseñe después
        —los pasos, y luego la respuesta— se le añade a esta misma burbuja, para
        que el turno se lea como una sola intervención y no como varias.
        """
        burbuja = MessageBubble(ChatRole.COPILOT, "", parent=self)
        burbuja.set_status("Pensando…")
        self._insert(burbuja)
        self._bubbles.append(burbuja)
        self._empty.setVisible(False)
        self._to_bottom()
        return burbuja

    def add_note(self, texto: str, *, danger: bool = False) -> None:
        """Un aviso del turno que no es la voz de nadie: un fallo de la aplicación.

        Va fuera de las burbujas a propósito: «no se pudo consultar al copiloto»
        no es algo que dijera el copiloto, y ponerlo en su boca con su rótulo
        sería atribuirle un error que no es suyo.
        """
        nota = QLabel(texto)
        nota.setObjectName("danger" if danger else "hint")
        nota.setWordWrap(True)
        nota.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._insert(nota)
        self._notes.append(nota)
        self._to_bottom()

    def show_proposal(self, proposal: CopilotProposal) -> ProposalCard:
        """Enseña la propuesta del turno, sustituyendo a la anterior si la hubiera.

        Se sustituye en vez de acumular: dos propuestas vivas a la vez serían dos
        botones de firmar y ninguna forma de saber cuál manda.
        """
        self.clear_proposal()
        tarjeta = ProposalCard(proposal, self)
        tarjeta.execute_requested.connect(self.execute_requested.emit)
        self._insert(tarjeta)
        self._proposal = tarjeta
        self._to_bottom()
        return tarjeta

    def clear_proposal(self) -> None:
        """Retira la propuesta viva, si la hay."""
        if self._proposal is None:
            return
        self._body.removeWidget(self._proposal)
        self._proposal.deleteLater()
        self._proposal = None

    def proposal(self) -> ProposalCard | None:
        """La propuesta viva, o `None` si no hay ninguna."""
        return self._proposal

    def scroll_area(self) -> QScrollArea:
        """El área que se desplaza, para poder llevarla al final desde fuera."""
        return self._scroll

    def bubbles(self) -> tuple[MessageBubble, ...]:
        """Las burbujas pintadas, en orden. Sólo las pruebas las miran."""
        return tuple(self._bubbles)

    def notes(self) -> tuple[QLabel, ...]:
        """Los avisos pintados, en orden. Sólo las pruebas los miran."""
        return tuple(self._notes)

    def scroll_to_bottom(self) -> None:
        """Baja hasta lo último escrito."""
        self._to_bottom()

    # ------------------------------------------------------------------ #
    def _insert(self, widget: QWidget) -> None:
        """Añade al final de la conversación, antes del hueco del final."""
        self._body.insertWidget(self._body.count() - 1, widget)

    def _to_bottom(self) -> None:
        # El desplazamiento se pide en la vuelta siguiente del bucle de sucesos:
        # el widget acaba de añadirse y todavía no tiene su altura definitiva, así
        # que un `maximum()` leído ahora apuntaría a donde estaba antes de él.
        QTimer.singleShot(0, self._scroll_to_end)

    def _scroll_to_end(self) -> None:
        barra = self._scroll.verticalScrollBar()
        barra.setValue(barra.maximum())


class _ComposerEdit(QPlainTextEdit):
    """El campo de escritura, con Enter enviando y Mayús+Enter para una línea nueva.

    Es la convención de cualquier chat, y aquí además evita el accidente que
    tendría un campo de varias líneas sin ella: una pregunta escrita en tres
    renglones no puede enviarse a medias porque el segundo Enter se fue solo.
    """

    submitted = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        nueva_linea = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        con_mayus = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if nueva_linea and not con_mayus:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class Composer(QWidget):
    """Escribir y enviar. Se apaga entero mientras el copiloto responde.

    Se apaga el campo y **no** sólo el botón: si el turno está en marcha, dejarlo
    escribir invita a mandar una segunda pregunta que llegaría al mismo hilo a
    medias. La línea de estado dice por qué está apagado.
    """

    submitted = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._edit = _ComposerEdit()
        self._edit.setPlaceholderText(
            "Pregúntale algo… por ejemplo «¿qué saldo tiene 0x2c88…44f9?» "
            "(Enter envía, Mayús+Enter hace una línea nueva)"
        )
        self._edit.setFixedHeight(64)
        self._edit.submitted.connect(self._on_submit)
        lay.addWidget(self._edit, 1)

        self._send = QPushButton("Enviar")
        self._send.clicked.connect(self._on_submit)
        lay.addWidget(self._send, 0, Qt.AlignmentFlag.AlignBottom)

    def text(self) -> str:
        """Lo escrito, sin espacios de sobra."""
        return self._edit.toPlainText().strip()

    def clear(self) -> None:
        self._edit.clear()

    def set_busy(self, ocupado: bool) -> None:
        """Apaga o enciende el envío. Se llama al empezar y al acabar un turno."""
        self._edit.setEnabled(not ocupado)
        self._send.setEnabled(not ocupado)
        self._send.setText("Consultando…" if ocupado else "Enviar")

    def focus(self) -> None:
        """Deja el cursor dentro, listo para escribir."""
        self._edit.setFocus()

    def edit(self) -> _ComposerEdit:
        """El campo de escritura, para las pruebas."""
        return self._edit

    def send_button(self) -> QPushButton:
        """El botón de enviar, para las pruebas."""
        return self._send

    def _on_submit(self) -> None:
        # El vacío no se emite: un Enter en un campo en blanco no es una pregunta,
        # y hacerle llegar al copiloto una cadena vacía gastaría un turno entero
        # del modelo para que contestara que no entendió nada.
        if self.text():
            self.submitted.emit()
