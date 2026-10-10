"""Pestaña: Copiloto. Un chat con el modelo, que consulta y que propone.

### Qué cambia respecto a la pestaña anterior

Antes era un formulario: una pregunta y una caja de contexto que había que
rellenar **a mano** copiando las cifras de las otras pantallas. Con eso, «¿tiene
saldo esta dirección?» no se podía preguntar de ninguna manera. Ahora la pregunta
se escribe y las cifras las trae la aplicación, por los mismos casos de uso que
usan las demás pestañas.

### Quién hace qué

La página no piensa: junta las piezas y las conecta. El historial lo escribe
`ChatStore`, el turno lo resuelve `Copilot`, el chat lo pinta `ChatView` y la
decisión de si se puede firmar la toman las **mismas** funciones que consulta la
pantalla de conversión (`execution_blockers`, `bridge_blockers`). Aquí sólo se
decide qué se enseña y cuándo.

### La propuesta no firma: abre el camino de siempre

Cuando el copiloto propone una operación, la tarjeta la enseña con su cifra
observada y su botón. Ese botón no firma nada por su cuenta: entra por
`execute_swap`/`execute_bridge`, los mismos casos de uso que las otras pantallas,
con sus diálogos de confirmación y sus topes. Si la autonomía está armada y la
operación cae dentro de los límites, esos diálogos se cierran sin preguntar —pero
eso lo decide la política, no el chat—.

### Lo que se guarda de un turno

El texto del usuario, y la respuesta con su letra pequeña (hallazgos y lo
consultado). La propuesta **no** se guarda como propuesta: al reabrir el chat
quedaría una cifra vieja con un botón de firmar al lado. Para ejecutarla se vuelve
a preguntar, y la cotización será la de entonces.
"""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from amigocompora.app.chat_store import ChatMessage, ChatRole
from amigocompora.app.container import Container
from amigocompora.app.copilot import CopilotProposal, CopilotReply
from amigocompora.domain.errors import ConfirmationDeniedError
from amigocompora.domain.models import BridgeQuote, BroadcastReceipt, Quote
from amigocompora.ui.copilot_chat import (
    ChatView,
    Composer,
    MessageBubble,
    ProposalCard,
    detail_of,
)
from amigocompora.ui.copilot_controls import AutonomyStrip, ModelPicker
from amigocompora.ui.copilot_history import HistoryPanel
from amigocompora.ui.execution_gate import bridge_blockers, execution_blockers
from amigocompora.ui.receipt_dialog import SwapReceiptDialog, explorer_url
from amigocompora.ui.widgets import Card, ScrollArea, divider, spawn

#: El ancho de la columna del historial. Cabe un título de 48 caracteres en la
#: fila —el tope que guarda `ChatStore`— sin comerse el chat, que es lo que se
#: viene a leer: más ancho para la lista sería menos alto para la conversación.
HISTORY_WIDTH = 250


class AiPage(QWidget):
    """El chat del copiloto, con su historial a la izquierda y sus mandos arriba."""

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._chat_id: str | None = None
        self._busy = False
        #: La ventana de la confirmación de una operación emitida. Se guarda aquí
        #: porque una ventana sin dueño la recoge el recolector en cuanto sale de
        #: la función que la abrió, y el usuario no llega a verla.
        self._recibo: SwapReceiptDialog | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)
        lay.addWidget(self._build_history())

        derecha = QVBoxLayout()
        derecha.setContentsMargins(0, 0, 0, 0)
        derecha.setSpacing(10)
        derecha.addWidget(self._build_controls())

        self._view = ChatView(self)
        self._view.execute_requested.connect(self._on_execute)
        derecha.addWidget(self._view, 1)

        self._composer = Composer(self)
        self._composer.submitted.connect(self._on_send)
        derecha.addWidget(self._composer)

        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        derecha.addWidget(self._status)
        lay.addLayout(derecha, 1)

        # El estado de la ejecución puede cambiar sin que el chat haga nada —el
        # modo se cambia en la cabecera, la cartera se bloquea, la autonomía se
        # arma—, y la tarjeta de propuesta dice qué falta para firmar. Si eso no
        # se repintara, la lista de lo que falta se quedaría contando lo de antes.
        container.guard.subscribe(lambda _mode: self._refresh_gate())
        container.policy.subscribe(lambda _armed: self._refresh_gate())

        self._refresh_history()
        self._open_latest()

    # ------------------------------------------------------------------ #
    # Construcción
    # ------------------------------------------------------------------ #
    def _build_history(self) -> ScrollArea:
        """La columna del historial, con su propio desplazamiento."""
        self._history = HistoryPanel(self)
        self._history.chat_selected.connect(self._open)
        self._history.archive_requested.connect(self._on_archive)
        self._history.delete_requested.connect(self._on_delete)
        self._history.new_requested.connect(self._on_new_chat)

        # El historial se desplaza solo, dentro de su columna: con treinta chats
        # la lista llegaría más abajo que la ventana, y lo que tiene que
        # desplazarse es la lista y no la página entera —el chat no se mueve de
        # su sitio porque haya muchos chats guardados—.
        area = ScrollArea(self, spacing=6)
        area.setFixedWidth(HISTORY_WIDTH)
        area.body().addWidget(self._history)
        return area

    def _build_controls(self) -> Card:
        """El modelo con el que se habla, la autonomía, y lo que son las dos."""
        card = Card(
            "El copiloto",
            subtitle="consulta y propone; no firma nada mientras tú no lo confirmes",
        )
        cuerpo = card.body()
        self._picker = ModelPicker(self._container, self)
        cuerpo.addWidget(self._picker)
        cuerpo.addWidget(divider())
        self._autonomy = AutonomyStrip(self._container, self)
        cuerpo.addWidget(self._autonomy)

        nota = QLabel(
            "Las respuestas las redacta un modelo de IA. Las cifras las trae la "
            "aplicación —por los mismos caminos que las otras pantallas—, así que "
            "son las que hay; pero el texto que las rodea es del modelo: "
            "comprueba antes de operar."
        )
        nota.setObjectName("hint")
        nota.setWordWrap(True)
        cuerpo.addWidget(nota)
        return card

    # ------------------------------------------------------------------ #
    # El historial
    # ------------------------------------------------------------------ #
    def _refresh_history(self) -> None:
        self._history.refresh(self._container.chats.chats())
        self._history.set_current(self._chat_id)

    def _open_latest(self) -> None:
        """Abre el chat más reciente, como cualquier conversación.

        Abrir siempre en blanco obligaría a buscar a mano lo que se estaba
        hablando la última vez, y aquí el historial **es** el estado: no hay nada
        que restaurar más allá de la conversación.
        """
        activos = [chat for chat in self._container.chats.chats() if not chat.archived]
        if activos:
            self._open(activos[0].chat_id)

    def _open(self, chat_id: str) -> None:
        """Enseña un chat guardado. De sus propuestas no vuelve ninguna."""
        chat = self._container.chats.get(chat_id)
        if chat is None:
            # El chat desapareció entre el clic y la lectura —borrado a mano, o
            # por otra ventana—: se repinta la lista y se dice, en vez de dejar
            # el hueco sin explicación.
            self._refresh_history()
            self._status.setText("Ese chat ya no está.")
            return
        self._chat_id = chat_id
        self._history.set_current(chat_id)
        self._view.clear()
        for mensaje in chat.messages:
            self._view.add(mensaje)
        self._status.setText("")
        self._composer.focus()

    def _on_new_chat(self) -> None:
        """Un chat en blanco. No se guarda hasta que se escriba el primer mensaje."""
        self._chat_id = None
        self._history.set_current(None)
        self._view.clear()
        self._status.setText("")
        self._composer.focus()

    def _on_archive(self, chat_id: str, archived: bool) -> None:
        self._container.chats.archive(chat_id, archived=archived)
        self._refresh_history()

    def _on_delete(self, chat_id: str) -> None:
        self._container.chats.delete(chat_id)
        if self._chat_id == chat_id:
            self._chat_id = None
            self._view.clear()
        self._refresh_history()

    # ------------------------------------------------------------------ #
    # El turno
    # ------------------------------------------------------------------ #
    def _on_send(self) -> None:
        texto = self._composer.text()
        if not texto or self._busy:
            return
        self._composer.clear()
        spawn(self._do_send(texto))

    async def _do_send(self, texto: str) -> None:
        """Un turno completo: se guarda lo preguntado, se pide, y se guarda la respuesta.

        La conversación anterior se lee **antes** de añadir nada: lo que el modelo
        tiene que saber es de qué se venía hablando, y el mensaje de ahora le llega
        aparte, como la pregunta.
        """
        chat_id = self._chat_id or self._create_chat()
        chat = self._container.chats.get(chat_id)
        historial = chat.messages if chat is not None else ()

        ahora = datetime.now(UTC)
        self._view.add(ChatMessage(role=ChatRole.USER, text=texto, at=ahora))
        # El mensaje del usuario se guarda antes de preguntar: si el motor se cae,
        # lo que se escribió no se pierde —queda en el hilo— y al reabrir el chat
        # se ve qué se preguntó y que no hubo respuesta.
        self._container.chats.append(
            chat_id, ChatMessage(role=ChatRole.USER, text=texto, at=ahora)
        )
        self._refresh_history()

        burbuja = self._view.begin_turn()
        self._set_busy(True)
        try:
            respuesta = await self._container.copilot.ask(
                texto, history=historial, on_event=burbuja.activity().show_event
            )
        except Exception as error:
            burbuja.set_status("")
            burbuja.set_detail("")
            self._view.add_note(
                f"No se pudo consultar al copiloto: {error}", danger=True
            )
            self._set_busy(False)
            return
        self._set_busy(False)
        self._show_reply(chat_id, respuesta, burbuja)

    def _show_reply(self, chat_id: str, respuesta: CopilotReply, burbuja: MessageBubble) -> None:
        """Pinta la respuesta, la guarda y —si la hay— enseña su propuesta."""
        letra_pequena = detail_of(respuesta)
        burbuja.set_text(respuesta.text)
        burbuja.set_findings(respuesta.findings)
        burbuja.set_detail(letra_pequena)
        burbuja.set_status(respuesta.disclaimer)

        self._container.chats.append(
            chat_id,
            ChatMessage(
                role=ChatRole.COPILOT,
                text=respuesta.text,
                at=datetime.now(UTC),
                detail=letra_pequena,
            ),
        )
        self._refresh_history()

        propuesta = respuesta.proposal
        if propuesta is None:
            return
        tarjeta = self._view.show_proposal(propuesta)
        if not propuesta.ready:
            tarjeta.set_status("No hay nada que ejecutar: no se pudo resolver.")
            return
        tarjeta.set_blockers(self._blockers_for(propuesta))

    # ------------------------------------------------------------------ #
    # Ejecutar lo propuesto
    # ------------------------------------------------------------------ #
    def _blockers_for(self, propuesta: CopilotProposal) -> tuple[str, ...]:
        """Qué falta para firmar esto, con las **mismas** funciones que las otras pantallas.

        Se pregunta a `execution_gate` y no se escribe aquí una lista propia: dos
        listas que dicen cosas distintas son una de ellas mintiendo, y la que
        miente es siempre la que no se ejecuta.
        """
        if propuesta.quote is not None:
            return execution_blockers(
                self._container, pair=propuesta.quote.pair, quote=propuesta.quote
            )
        if propuesta.bridge is not None:
            return bridge_blockers(
                self._container, request=propuesta.bridge.request, quote=propuesta.bridge
            )
        return ()

    def _refresh_gate(self) -> None:
        """Vuelve a mirar si la propuesta que está a la vista se puede firmar."""
        tarjeta = self._view.proposal()
        if tarjeta is None or not tarjeta.proposal().ready or tarjeta.is_spent():
            return
        tarjeta.set_blockers(self._blockers_for(tarjeta.proposal()))

    def _on_execute(self) -> None:
        """El botón de la tarjeta: se vuelve a comprobar y se entra por el camino de siempre."""
        tarjeta = self._view.proposal()
        if tarjeta is None or self._busy:
            return
        propuesta = tarjeta.proposal()
        motivos = self._blockers_for(propuesta)
        if motivos:
            # La lista que se enseñó puede haberse quedado vieja —el modo, la
            # cartera, los topes se mueven desde otras pantallas— y el botón no
            # es el sitio donde enterarse: se dice aquí y no se ejecuta nada.
            tarjeta.set_blockers(motivos)
            tarjeta.set_status("No se ejecutó: falta lo que dice arriba.")
            return
        spawn(self._do_execute(tarjeta, propuesta))

    async def _do_execute(self, tarjeta: ProposalCard, propuesta: CopilotProposal) -> None:
        recipiente = self._container.keys.address()
        if recipiente is None:
            tarjeta.set_status("No hay ninguna cartera de la que salga el dinero.")
            return
        self._set_busy(True)
        tarjeta.button().setEnabled(False)
        try:
            if propuesta.quote is not None:
                await self._execute_swap(tarjeta, propuesta.quote, recipiente)
            elif propuesta.bridge is not None:
                await self._execute_bridge(tarjeta, propuesta.bridge, recipiente)
        finally:
            self._set_busy(False)

    async def _execute_swap(
        self, tarjeta: ProposalCard, quote: Quote, recipiente: str
    ) -> None:
        """Firma y emite el swap propuesto, narrando cada transacción en su lista."""
        panel = tarjeta.start_steps(quote.pair.base.chain)
        tarjeta.set_status("Ejecutando…")
        try:
            recibo = await self._container.execute_swap(
                quote, recipient=recipiente, on_step=panel.record
            )
        except ConfirmationDeniedError:
            # Decir «error» aquí sería mentir: el usuario hizo lo correcto.
            tarjeta.set_status("Cancelaste la operación. No se firmó ni se emitió nada.")
            tarjeta.button().setEnabled(True)
        except Exception as error:
            tarjeta.set_status(f"No se ejecutó: {error}")
            tarjeta.button().setEnabled(True)
        else:
            tarjeta.mark_spent()
            tarjeta.set_status(f"✓ Emitida {recibo.tx_hash} · estado {recibo.status.value}.")
            self._show_receipt(quote, recibo, recipiente)

    async def _execute_bridge(
        self, tarjeta: ProposalCard, quote: BridgeQuote, recipiente: str
    ) -> None:
        """Emite el cruce propuesto. Un puente es **una** transacción, no una lista.

        Por eso aquí no hay panel de pasos, a diferencia del swap: no hay dos
        permisos ni un orden que narrar, y montar una lista de un solo renglón
        fingiría una operación por etapas que no existe.
        """
        tarjeta.set_status("Ejecutando…")
        try:
            recibo = await self._container.execute_bridge(quote, recipient=recipiente)
        except ConfirmationDeniedError:
            tarjeta.set_status("Cancelaste la operación. No se firmó ni se emitió nada.")
            tarjeta.button().setEnabled(True)
        except Exception as error:
            tarjeta.set_status(f"No se ejecutó: {error}")
            tarjeta.button().setEnabled(True)
        else:
            tarjeta.mark_spent()
            tarjeta.set_status(
                f"✓ Emitida {recibo.tx_hash} · estado {recibo.status.value}. El dinero "
                f"salió de {quote.request.origin.chain}: el tiempo del cruce lo pone "
                f"el proveedor, no la aplicación.",
                link=explorer_url(quote.request.origin.chain, recibo.tx_hash),
            )

    def _show_receipt(self, quote: Quote, recibo: BroadcastReceipt, recipiente: str) -> None:
        """Abre la confirmación de la operación emitida, sin bloquear la pantalla.

        Se abre sin `exec()` porque un diálogo modal aquí anidaría el bucle de
        sucesos dentro de la tarea de la operación, y se guarda la referencia en la
        página para que no la recoja el recolector antes de que se vea.
        """
        ventana = SwapReceiptDialog(
            quote=quote, receipt=recibo, recipient=recipiente, parent=self
        )
        self._recibo = ventana
        ventana.show()
        ventana.raise_()
        ventana.activateWindow()

    # ------------------------------------------------------------------ #
    def _create_chat(self) -> str:
        """Crea el chat del primer mensaje y lo deja elegido en la lista."""
        chat = self._container.chats.create()
        self._chat_id = chat.chat_id
        self._refresh_history()
        return chat.chat_id

    def _set_busy(self, ocupado: bool) -> None:
        """Mientras hay un turno o una ejecución en marcha no se cambia de chat.

        El historial se apaga con el compositor: la respuesta se pinta en la
        burbuja que se abrió, y cambiar de conversación a mitad de turno dejaría
        la respuesta del chat viejo dentro del nuevo.
        """
        self._busy = ocupado
        self._composer.set_busy(ocupado)
        self._history.setEnabled(not ocupado)
        self._status.setText("Trabajando…" if ocupado else "")

    # ------------------------------------------------------------------ #
    # Para las pruebas
    # ------------------------------------------------------------------ #
    def view(self) -> ChatView:
        """La conversación."""
        return self._view

    def history_panel(self) -> HistoryPanel:
        """La columna del historial."""
        return self._history

    def composer(self) -> Composer:
        """El campo de escritura."""
        return self._composer

    def model_picker(self) -> ModelPicker:
        """El desplegable del modelo."""
        return self._picker

    def autonomy(self) -> AutonomyStrip:
        """La tira de la autonomía."""
        return self._autonomy

    def status_text(self) -> str:
        """Lo que dice la línea de estado de abajo."""
        return self._status.text()
