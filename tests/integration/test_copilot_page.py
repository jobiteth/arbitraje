"""La pestaña del copiloto: el chat, el historial y lo que se puede firmar.

Se construye la página **de verdad** —contenedor de verdad, almacén de chats de
verdad en el directorio temporal de la prueba, widgets de Qt de verdad—, y el
modelo que contesta es el asistente offline, que es determinista y no sale a la
red. Ninguna prueba de este fichero firma, emite ni abre una conexión.

Lo que se comprueba aquí es el reparto de responsabilidades de la pantalla: que
un mensaje se guarde antes de preguntar, que el historial se lea del almacén y no
de la vista, que la tarjeta de propuesta **no** habilite firmar cuando el camino
no está abierto, y que elegir modelo deje la ranura con uno solo. Lo que decide
el copiloto por dentro se prueba en `tests/unit/test_copilot.py`, y lo que decide
si se puede firmar, en `tests/unit/test_execution_gate.py`.

Qt necesita un `QApplication` vivo, y en una máquina sin pantalla —CI, un
servidor— eso se resuelve con la plataforma `offscreen`, que se fija aquí antes
de que se importe nada de Qt.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from amigocompora.app.chat_store import Chat, ChatMessage, ChatRole
from amigocompora.app.container import Container, build_container
from amigocompora.app.copilot import (
    CopilotEvent,
    CopilotEventKind,
    CopilotProposal,
    CopilotReply,
    ProposalKind,
)
from amigocompora.app.execution_policy import LedgerEntry
from amigocompora.domain.models import (
    BroadcastStatus,
    Measurement,
    Quote,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import OperationMode
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.domain.protocols import EngineKind
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    AUTONOMY_PASSPHRASE_SECRET,
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    app_secret_key,
    secret_key,
)
from amigocompora.ui.copilot_chat import ChatView, Composer, MessageBubble, ProposalCard
from amigocompora.ui.copilot_history import HistoryPanel
from amigocompora.ui.pages.ai import AiPage

#: Clave de desarrollo **publicada** (la primera de Hardhat). No es un secreto:
#: que sea conocida es justo lo que la hace útil aquí.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

#: La frase que habilita la ejecución desatendida. En la prueba es una constante
#: porque lo que se comprueba es que se pida, no de dónde salga.
FRASE_DE_AUTONOMIA = "frase-de-la-prueba"

AHORA = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

_USDC = Token("USDC", 6, "polygon", "0x" + "3" * 40)
_WPOL = Token("WPOL", 18, "polygon", "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270")
_VENUE = Venue(
    venue_id="uniswap_v3@polygon", name="Uniswap v3", kind=VenueKind.DEX, chain="polygon"
)

#: Con esto la ejecución queda lista salvo por lo que cada prueba quite.
_TERMINOS: dict[str, object] = {
    "enabled": True,
    "allowed_tokens": ["USDC", "WPOL"],
    "allowed_chains": ["polygon"],
    "allowed_engines": ["uniswap"],
    "max_quote_per_trade": "1000",
    "max_quote_per_day": "1000",
}


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _store(
    *, con_cartera: bool = True, con_frase: bool = False, con_clave_claude: bool = False
) -> InMemorySecretStore:
    store = InMemorySecretStore()
    if con_cartera:
        store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    if con_frase:
        store.set(app_secret_key(AUTONOMY_PASSPHRASE_SECRET), FRASE_DE_AUTONOMIA)
    if con_clave_claude:
        # Una clave falsa para poder encender «claude» sin llamar a su API: sólo
        # la enciende, y en la prueba en que se enciende no se le pregunta nada.
        store.set(secret_key("claude", "api_key"), "clave-de-prueba")
    # El motor necesita su clave para activarse, y activarse es lo que lo
    # convierte en planificador. La clave es falsa y no se usa: aquí no hay red.
    store.set(secret_key("uniswap", "api_key"), "clave-de-prueba")
    return store


@asynccontextmanager
async def _pagina(
    *,
    modo: OperationMode = OperationMode.OBSERVATION,
    con_cartera: bool = True,
    con_frase: bool = False,
    con_planificador: bool = True,
    asesor: str | None = "stub_advisor",
    con_clave_claude: bool = False,
) -> AsyncIterator[tuple[Container, AiPage]]:
    """La pestaña montada sobre un contenedor de verdad.

    El asistente offline es el que contesta por omisión, y no necesita clave, así
    que el copiloto responde de verdad —con prosa, que es el caso que su bucle
    trata como respuesta final— sin salir a ninguna red. Sin ranura de asesor el
    contenedor enciende igualmente el asistente offline: es el único que puede
    arrancar sin credenciales. Por eso, para probar un cambio de modelo, el que
    hay que poner de partida es otro.
    """
    activos: dict[str, object] = {}
    if asesor is not None:
        activos["ai_advisor"] = asesor
    if con_planificador:
        activos["dex_quotes"] = "uniswap"
    config: dict[str, object] = {
        "mode": modo.value,
        "execution": dict(_TERMINOS),
        "active_engines": activos,
    }
    container = await build_container(
        Settings.model_validate(config),
        secret_store=_store(
            con_cartera=con_cartera, con_frase=con_frase, con_clave_claude=con_clave_claude
        ),
        configure_logs=False,
    )
    try:
        yield container, AiPage(container)
    finally:
        await container.aclose()


def _cotizacion() -> Quote:
    return Quote(
        venue=_VENUE,
        pair=TradingPair(base=_USDC, quote=_WPOL),
        amount_in=TokenAmount.from_decimal(50, 6, "USDC"),
        amount_out=TokenAmount.from_decimal(100, 18, "WPOL"),
        engine_id="uniswap",
        fee_bps=BasisPoints(5),
        fee_basis=Measurement.REPORTED,
        price_impact_bps=BasisPoints(2),
        impact_basis=Measurement.DERIVED,
        observed_at=AHORA,
    )


def _propuesta() -> CopilotProposal:
    return CopilotProposal(
        kind=ProposalKind.SWAP,
        summary="Vender 50 USDC por 100 WPOL en polygon",
        quote=_cotizacion(),
    )


def _chat(identificador: str, titulo: str, *, archived: bool = False) -> Chat:
    momento = datetime(2026, 10, 10, 12, int(identificador[-1]), tzinfo=UTC)
    return Chat(
        chat_id=identificador,
        title=titulo,
        created_at=momento,
        updated_at=momento,
        archived=archived,
        messages=(ChatMessage(role=ChatRole.USER, text=titulo, at=momento),),
    )


async def _ensenar_propuesta(pagina: AiPage) -> ProposalCard:
    """Pinta una respuesta con propuesta, como la pintaría un turno del copiloto."""
    chat_id = pagina._create_chat()
    burbuja = pagina.view().begin_turn()
    pagina._show_reply(
        chat_id, CopilotReply(text="Te lo preparo.", proposal=_propuesta()), burbuja
    )
    tarjeta = pagina.view().proposal()
    assert tarjeta is not None
    return tarjeta


def _activos(container: Container) -> list[str]:
    return [
        engine.manifest.engine_id
        for engine in container.registry.active_stack(EngineKind.AI_ADVISOR)
    ]


# --------------------------------------------------------------------------- #
# Las piezas del chat
# --------------------------------------------------------------------------- #
def test_el_historial_separa_los_activos_de_los_archivados() -> None:
    """Los archivados salen en su sección, y **no** se ven hasta que se piden.

    Es lo único que significa archivar: si el archivado siguiera a la vista, la
    lista de los de ahora volvería a mezclar lo que se está usando con lo que se
    consultó una vez, que es el problema que la sección viene a resolver.
    """
    panel = HistoryPanel()
    panel.refresh((_chat("c1", "Uno"), _chat("c3", "Viejo", archived=True)))

    assert [fila.chat_id for fila in panel.rows()] == ["c1", "c3"], "activos y luego archivados"
    por_id = {fila.chat_id: fila for fila in panel.rows()}
    assert por_id["c1"].isVisibleTo(panel)
    assert not por_id["c3"].isVisibleTo(panel), "el archivado está plegado, no borrado"
    assert panel.current() is None, "nada elegido hasta que se elige"


def test_repintar_con_la_seccion_plegada_no_vuelve_a_ensenar_los_archivados() -> None:
    """Una fila recién creada nace visible: hay que volver a plegarla al repintar.

    Sin esto, archivar un chat lo sacaba de la lista hasta la siguiente recarga, y
    entonces reaparecía —por la puerta de atrás, en la sección que dice estar
    cerrada—.
    """
    panel = HistoryPanel()
    panel.refresh((_chat("c3", "Viejo", archived=True),))
    panel.refresh((_chat("c4", "Otro viejo", archived=True),))

    (fila,) = panel.rows()
    assert fila.chat_id == "c4"
    assert not fila.isVisibleTo(panel)


def test_borrar_pregunta_antes_y_archivar_no() -> None:
    """Borrar destruye historial y archivar no: sólo uno puede preguntar.

    Se contesta «No» primero a propósito: lo que hay que probar es que un «no» en
    el aviso **no** borra. Un aviso que se enseña y no se obedece es peor que no
    tenerlo, porque el usuario cree haber salvado el chat.
    """
    panel = HistoryPanel()
    panel.refresh((_chat("c1", "Uno"),))
    borrados: list[str] = []
    archivados: list[tuple[str, bool]] = []
    panel.delete_requested.connect(borrados.append)
    panel.archive_requested.connect(lambda chat_id, si: archivados.append((chat_id, si)))

    panel.archive_requested.emit("c1", True)  # archivar no pregunta: va directo
    assert archivados == [("c1", True)]

    respuestas = iter((QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes))
    original = QMessageBox.question

    def preguntar(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
        return next(respuestas)

    QMessageBox.question = preguntar  # type: ignore[method-assign]
    try:
        panel._on_delete("c1")
        assert borrados == [], "dijo que no, así que no se borra"
        panel._on_delete("c1")
    finally:
        QMessageBox.question = original  # type: ignore[method-assign]

    assert borrados == ["c1"], "y con un sí, se borra una vez"


def test_la_burbuja_guarda_lo_consultado_plegado() -> None:
    """La letra pequeña existe desde el principio y no se despliega sola."""
    burbuja = MessageBubble(ChatRole.COPILOT, "Cien WPOL.", detail="Lo que se consultó:\n← ...")

    assert burbuja.text() == "Cien WPOL."
    assert burbuja.detail_text().startswith("Lo que se consultó")
    mando = burbuja.detail_button()
    assert "▸" in mando.text(), "plegado: el mando lo dice con la flecha"
    mando.click()
    assert "▾" in mando.text(), "y abierto, también"


def test_el_turno_cuelga_el_resultado_de_su_peticion() -> None:
    """Una petición y su resultado son **una** fila, no dos.

    Es lo que evita que el chat parezca estar pidiendo lo mismo dos veces: el
    resultado no es un paso más del turno, es lo que devolvió el paso anterior.
    """
    burbuja = MessageBubble(ChatRole.COPILOT, "")
    actividad = burbuja.activity()

    actividad.show_event(
        CopilotEvent(kind=CopilotEventKind.TOOL, title="Consultando «saldos»", detail="red: base")
    )
    actividad.show_event(
        CopilotEvent(
            kind=CopilotEventKind.TOOL_RESULT, title="Resultado de «saldos»", detail="12 USDC"
        )
    )

    lineas = actividad.lines()
    assert len(lineas) == 1
    assert "saldos" in lineas[0]
    assert "12 USDC" in lineas[0]


def test_una_propuesta_ya_ejecutada_no_vuelve_a_encenderse() -> None:
    """La cifra que ya se gastó no se puede volver a firmar.

    Pasa por el camino real: ejecutar, y después un repintado del estado —el modo
    cambia, la cartera se desbloquea— que vuelve a decir que no falta nada. El
    botón tiene que seguir apagado: es la misma cotización ya consumida.
    """
    tarjeta = ProposalCard(_propuesta())
    tarjeta.set_blockers(())
    assert tarjeta.button().isEnabled()

    tarjeta.mark_spent()
    tarjeta.set_blockers(())
    assert not tarjeta.button().isEnabled()
    assert tarjeta.is_spent()


def test_el_compositor_no_manda_en_blanco() -> None:
    """Un Enter en un campo vacío no es una pregunta."""
    compositor = Composer()
    enviados: list[int] = []
    compositor.submitted.connect(lambda: enviados.append(1))

    compositor.send_button().click()
    compositor.edit().setPlainText("   \n  ")
    compositor.send_button().click()
    assert enviados == []

    compositor.edit().setPlainText("¿qué saldo tiene?")
    compositor.send_button().click()
    assert enviados == [1]


def test_el_chat_pinta_lo_guardado_y_la_propuesta_va_al_final() -> None:
    """Al abrir un chat se lee lo suyo, y una propuesta nueva sustituye a la anterior.

    Sólo una viva a la vez: dos propuestas serían dos botones de firmar y ninguna
    forma de saber cuál manda.
    """
    vista = ChatView()
    vista.add(_chat("c1", "Uno").messages[0])
    assert [burbuja.text() for burbuja in vista.bubbles()] == ["Uno"]

    vista.show_proposal(_propuesta())
    segunda = vista.show_proposal(_propuesta())
    assert vista.proposal() is segunda

    vista.clear()
    assert vista.bubbles() == ()
    assert vista.proposal() is None


# --------------------------------------------------------------------------- #
# La pestaña, con un contenedor de verdad
# --------------------------------------------------------------------------- #
async def test_el_primer_mensaje_crea_el_chat_y_guarda_la_respuesta() -> None:
    """Pregunta, respuesta y las dos guardadas, con el título del primer mensaje.

    Se comprueba sobre el almacén y no sobre la vista: la vista es lo que se ve
    ahora y el almacén es lo que sigue ahí mañana, y el fallo que importa es el
    segundo.
    """
    async with _pagina() as (container, page):
        assert container.chats.chats() == (), "todavía no hay conversaciones"

        await page._do_send("¿qué tal va todo?")

        guardados = container.chats.chats()
        assert len(guardados) == 1
        chat = guardados[0]
        assert chat.title == "¿qué tal va todo?", "el título sale de la pregunta"
        assert [mensaje.role for mensaje in chat.messages] == [ChatRole.USER, ChatRole.COPILOT]
        assert chat.messages[1].text.startswith("Análisis offline de:")

        burbujas = page.view().bubbles()
        assert [burbuja.role() for burbuja in burbujas] == [ChatRole.USER, ChatRole.COPILOT]
        assert burbujas[1].text() == chat.messages[1].text
        assert len(page.history_panel().rows()) == 1, "y aparece en el historial"


async def test_abrir_un_chat_guardado_lee_lo_suyo_y_no_lo_del_otro() -> None:
    async with _pagina() as (container, page):
        await page._do_send("primera conversación")
        primero = container.chats.chats()[0].chat_id
        page._on_new_chat()
        await page._do_send("segunda conversación")

        page._open(primero)

        textos = [burbuja.text() for burbuja in page.view().bubbles()]
        assert textos[0] == "primera conversación"
        assert not any("segunda" in texto for texto in textos)


async def test_borrar_el_chat_abierto_deja_la_vista_limpia() -> None:
    """Al borrar lo que se está leyendo, la conversación se va con él.

    Dejar los mensajes en pantalla después de borrarlos sería lo peor de los dos
    mundos: parece que siguen guardados y ya no están.
    """
    async with _pagina() as (container, page):
        await page._do_send("una pregunta")
        chat_id = container.chats.chats()[0].chat_id

        page._on_delete(chat_id)

        assert container.chats.chats() == ()
        assert page.view().bubbles() == ()
        assert page.history_panel().rows() == ()


async def test_archivar_saca_el_chat_de_la_lista_sin_borrarlo() -> None:
    async with _pagina() as (container, page):
        await page._do_send("una pregunta")
        chat_id = container.chats.chats()[0].chat_id

        page._on_archive(chat_id, True)

        assert len(container.chats.chats()) == 1, "sigue guardado"
        (fila,) = page.history_panel().rows()
        assert not fila.isVisibleTo(page.history_panel()), "pero plegado fuera de la vista"


async def test_la_propuesta_sin_modo_de_ejecucion_no_se_puede_firmar() -> None:
    """El botón apagado **dice por qué**, y no firma nada.

    Sin esto, la tarjeta ofrecería una operación que el caso de uso rechazaría al
    pulsarla: es justo el botón que menos puede permitirse prometer.
    """
    async with _pagina(modo=OperationMode.OBSERVATION) as (container, page):
        tarjeta = await _ensenar_propuesta(page)

        assert not tarjeta.button().isEnabled()
        assert "EJECUCIÓN" in tarjeta.blockers_text()
        # Y el turno se guardó entero: texto y letra pequeña.
        chat = container.chats.chats()[0]
        assert chat.messages[-1].text == "Te lo preparo."
        assert chat.messages[-1].role is ChatRole.COPILOT


async def test_la_propuesta_lista_deja_el_boton_encendido() -> None:
    """Con todo en su sitio, la lista de lo que falta está vacía.

    Es la otra mitad de la prueba anterior: si esta no pasara, la pantalla estaría
    diciendo que falta algo cuando no falta —y un botón permanentemente apagado
    enseña a buscar la forma de saltárselo—.
    """
    async with _pagina(modo=OperationMode.EXECUTION) as (_container, page):
        tarjeta = await _ensenar_propuesta(page)

        assert tarjeta.blockers_text() == "", tarjeta.blockers_text()
        assert tarjeta.button().isEnabled()


async def test_sin_cartera_la_tarjeta_dice_que_falta_la_cartera() -> None:
    async with _pagina(modo=OperationMode.EXECUTION, con_cartera=False) as (_c, page):
        tarjeta = await _ensenar_propuesta(page)

        assert "cartera" in tarjeta.blockers_text()
        assert not tarjeta.button().isEnabled()


async def test_sin_motor_que_construya_la_tarjeta_lo_dice() -> None:
    """Cotizar y construir son cosas distintas, y la tarjeta nombra lo que falta.

    El planificador es lo que convierte una ruta en una transacción: sin él, la
    propuesta no se puede firmar aunque haya modo, topes y cartera.
    """
    async with _pagina(modo=OperationMode.EXECUTION, con_planificador=False) as (_c, page):
        tarjeta = await _ensenar_propuesta(page)

        assert "motor" in tarjeta.blockers_text()
        assert not tarjeta.button().isEnabled()


# --------------------------------------------------------------------------- #
# Los mandos
# --------------------------------------------------------------------------- #
async def test_elegir_modelo_deja_la_ranura_con_uno_solo() -> None:
    """Elegir un modelo enciende ése y apaga el anterior, y lo dice.

    Con dos encendidos, el desplegable diría «Asistente offline» mientras contesta
    otro, que es una pantalla mintiendo sobre quién está hablando. El que se apagó
    se nombra: volver a encenderlo es cosa de la pestaña de Motores, y hay que
    saber qué fue lo que se apagó para ir a buscarlo.
    """
    async with _pagina(asesor="claude", con_clave_claude=True) as (container, page):
        assert _activos(container) == ["claude"], "de partida contesta el otro modelo"

        await page.model_picker()._apply("stub_advisor")

        assert _activos(container) == ["stub_advisor"]
        mensaje = page.model_picker().message()
        assert mensaje.startswith("El copiloto usa")
        assert "Se apagó" in mensaje


async def test_elegir_un_modelo_sin_clave_lo_dice_en_vez_de_callarse() -> None:
    """Un motor que no arranca tiene que salir en la pantalla, con su motivo.

    Elegir «Claude» sin su clave es el caso normal de alguien que aún no la ha
    puesto: lo que no puede pasar es que el desplegable se quede enseñando el
    modelo nuevo y el chat siga contestando con el viejo.
    """
    async with _pagina(asesor="stub_advisor") as (container, page):
        await page.model_picker()._apply("claude")

        assert "No se pudo cambiar el modelo" in page.model_picker().message()
        assert _activos(container) == ["stub_advisor"], (
            "el que estaba sigue siendo el que contesta"
        )


async def test_la_tira_de_autonomia_dice_que_falta_para_armarla() -> None:
    """Sin frase de autonomía no se puede armar, y la tira dice dónde se pone.

    Un mando deshabilitado sin motivo manda a buscar el problema donde no está.
    """
    async with _pagina() as (_container, page):
        tira = page.autonomy()

        assert "desarmada" in tira.state_text().lower()
        assert "frase de autonomía" in tira.note_text()
        assert "Configuración" in tira.note_text()


async def test_la_tira_ensena_el_gasto_por_unidad_y_no_un_numero_suelto() -> None:
    """El gasto se desglosa por unidad: es la cifra que decide si armar esto.

    Un total sin unidad —«gastado: 120»— no dice si son ciento veinte dólares o
    ciento veinte mil, y es justo el número que se mira para dejar que la
    aplicación firme sola.
    """
    async with _pagina() as (container, page):
        container.policy.ledger.append(
            LedgerEntry(
                occurred_at=datetime.now(UTC),
                chain="polygon",
                pair="USDC/WPOL",
                engine_id="uniswap",
                notional="12.5",
                notional_symbol="USDC",
                tx_hash="0xabc",
                status=BroadcastStatus.SUCCESS.value,
                recipient="0x" + "9" * 40,
                description="Swap de prueba",
                tokens=("USDC", "WPOL"),
            )
        )

        page.autonomy().refresh()

        detalle = page.autonomy().detail_text()
        assert "12.5 USDC" in detalle
        assert "nada" not in detalle


async def test_la_autonomia_armada_pinta_el_aviso_y_deja_desarmar() -> None:
    """Armada, se dice en alto, y el freno está a mano.

    Se arranca la página con la frase puesta y se arma después: lo que se
    comprueba es la cadena entera —política, observador, tira—, porque lo que
    puede romperse es que la notificación no llegue.
    """
    async with _pagina(con_frase=True) as (container, page):
        container.policy.arm(FRASE_DE_AUTONOMIA)

        tira = page.autonomy()
        assert "ARMADA" in tira.state_text()
        assert "sin preguntar" in tira.state_text()
        assert tira.disarm_button().isVisibleTo(tira)
        assert not tira.arm_button().isVisibleTo(tira)

        tira.disarm_button().click()
        assert "desarmada" in tira.state_text().lower()
        assert container.policy.armed is False
