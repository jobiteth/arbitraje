"""El historial del copiloto: un fichero por chat, tolerante al leer y al escribir.

Lo que estas pruebas fijan es lo que hace que un historial se pueda dejar crecer
sin miedo: que el orden sea el de la última conversación, que el título salga del
primer mensaje sin que nadie lo escriba, que archivar no borre, y que un fichero
roto —o escrito por otra versión— se salte sin llevarse por delante a los demás.
Un historial que no se puede leer entero no vale para nada, y el caso que lo
rompe es siempre el mismo: un JSON a medio escribir.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amigocompora.app.chat_store import (
    MAX_TITLE_LENGTH,
    ChatMessage,
    ChatRole,
    ChatStore,
)

_T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _mensaje(texto: str, *, rol: ChatRole = ChatRole.USER, desfase: int = 0) -> ChatMessage:
    return ChatMessage(role=rol, text=texto, at=_T0 + timedelta(minutes=desfase))


def _store(tmp_path: Path) -> ChatStore:
    return ChatStore(tmp_path / "chats")


# --------------------------------------------------------------------------- #
# Guardar y volver a leer
# --------------------------------------------------------------------------- #
def test_un_chat_recien_creado_ya_esta_en_el_disco(tmp_path: Path) -> None:
    """Nace guardado: un chat vacío tiene que sobrevivir a un cierre inesperado."""
    store = _store(tmp_path)
    chat = store.create()

    assert (tmp_path / "chats" / f"{chat.chat_id}.json").is_file()
    releido = _store(tmp_path)
    assert [guardado.chat_id for guardado in releido.chats()] == [chat.chat_id]


def test_el_titulo_sale_del_primer_mensaje_y_no_se_pisa(tmp_path: Path) -> None:
    """El título lo pone el primer mensaje del usuario, y sólo ese."""
    store = _store(tmp_path)
    chat = store.create()
    store.append(chat.chat_id, _mensaje("¿Tiene saldo esta dirección?"))
    store.append(chat.chat_id, _mensaje("y otra cosa", desfase=1))

    actual = store.get(chat.chat_id)
    assert actual is not None
    assert actual.title == "¿Tiene saldo esta dirección?"
    assert len(actual.messages) == 2


def test_el_titulo_es_una_sola_linea(tmp_path: Path) -> None:
    """La lista enseña una línea: el título se queda con la primera."""
    store = _store(tmp_path)
    chat = store.create()
    store.append(chat.chat_id, _mensaje("¿Tiene saldo\nesta dirección?"))

    actual = store.get(chat.chat_id)
    assert actual is not None
    assert actual.title == "¿Tiene saldo"


def test_un_titulo_larguisimo_se_recorta_para_la_lista(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create()
    store.append(chat.chat_id, _mensaje("palabra " * 30))

    actual = store.get(chat.chat_id)
    assert actual is not None
    assert len(actual.title) == MAX_TITLE_LENGTH
    assert actual.title.endswith("…")


def test_el_orden_es_por_la_ultima_conversacion(tmp_path: Path) -> None:
    """El chat que acaba de hablar va primero, aunque se creara antes."""
    store = _store(tmp_path)
    viejo = store.create()
    nuevo = store.create()

    store.append(viejo.chat_id, _mensaje("sigo aquí", desfase=30))

    assert [chat.chat_id for chat in store.chats()] == [viejo.chat_id, nuevo.chat_id]


def test_archivar_aparta_sin_borrar(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create()
    store.append(chat.chat_id, _mensaje("hola"))

    archivado = store.archive(chat.chat_id)

    assert archivado is not None
    assert archivado.archived
    assert store.get(chat.chat_id) is not None, "sigue estando, sólo que apartado"
    assert len(_store(tmp_path).chats()) == 1


def test_borrar_quita_el_chat_y_su_fichero(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create()

    assert store.delete(chat.chat_id) is True

    assert store.get(chat.chat_id) is None
    assert not (tmp_path / "chats" / f"{chat.chat_id}.json").exists()
    assert store.delete(chat.chat_id) is False, "borrar dos veces no es un error, es un no"


def test_anadir_a_un_chat_que_no_existe_no_hace_nada(tmp_path: Path) -> None:
    """Un `None` explícito y no un chat fantasma: la pantalla tiene que poder decirlo."""
    store = _store(tmp_path)
    assert store.append("no-existe", _mensaje("hola")) is None


# --------------------------------------------------------------------------- #
# Lo que está roto no tumba al resto
# --------------------------------------------------------------------------- #
def test_un_fichero_ilegible_se_salta_y_los_demas_siguen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    bueno = store.create()
    store.append(bueno.chat_id, _mensaje("bueno"))
    (tmp_path / "chats" / "roto.json").write_text("{ no es json", encoding="utf-8")

    otro = _store(tmp_path)

    assert [chat.chat_id for chat in otro.chats()] == [bueno.chat_id]


def test_un_fichero_cuyo_id_no_coincide_con_su_nombre_se_salta(tmp_path: Path) -> None:
    """El nombre **es** el identificador: si el contenido dice otro, no identifica nada."""
    store = _store(tmp_path)
    store.create()
    (tmp_path / "chats" / "otro.json").write_text(
        json.dumps(
            {
                "id": "distinto",
                "title": "x",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
                "messages": [],
            }
        ),
        encoding="utf-8",
    )

    otro = _store(tmp_path)

    assert len(otro.chats()) == 1, "sólo el que escribió la aplicación"


def test_un_mensaje_de_rol_desconocido_se_salta_y_el_resto_se_lee(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create()
    store.append(chat.chat_id, _mensaje("vale"))
    ruta = tmp_path / "chats" / f"{chat.chat_id}.json"
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    datos["messages"].insert(
        0, {"role": "sistema-nuevo", "text": "?", "at": _T0.isoformat(), "detail": ""}
    )
    ruta.write_text(json.dumps(datos), encoding="utf-8")

    releido = ChatStore(tmp_path / "chats").get(chat.chat_id)

    assert releido is not None
    assert [mensaje.text for mensaje in releido.messages] == ["vale"]


def test_una_fecha_sin_zona_se_rechaza(tmp_path: Path) -> None:
    """Sin zona, ordenar es comparar horas de husos distintos."""
    store = _store(tmp_path)
    chat = store.create()
    ruta = tmp_path / "chats" / f"{chat.chat_id}.json"
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    datos["updated_at"] = "2026-10-10T12:00:00"
    ruta.write_text(json.dumps(datos), encoding="utf-8")

    assert ChatStore(tmp_path / "chats").get(chat.chat_id) is None


def test_un_nombre_de_fichero_que_seria_una_ruta_se_ignora(tmp_path: Path) -> None:
    chats = tmp_path / "chats"
    chats.mkdir(parents=True)
    (chats / "..json").write_text("{}", encoding="utf-8")

    assert ChatStore(chats).chats() == ()


# --------------------------------------------------------------------------- #
# Escritura
# --------------------------------------------------------------------------- #
def test_la_escritura_es_atomica_y_no_deja_temporales(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create()
    store.append(chat.chat_id, _mensaje("hola"))

    sobran = [ruta.name for ruta in (tmp_path / "chats").iterdir() if ruta.suffix != ".json"]
    assert sobran == []


def test_un_directorio_imposible_no_lanza_al_guardar(tmp_path: Path) -> None:
    """Sin poder guardar, el chat sigue vivo en esta sesión; se registra y se sigue."""
    store = ChatStore(tmp_path / "chats")
    # Un fichero donde debería ir el directorio: `mkdir` fallará con OSError.
    (tmp_path / "chats").write_text("estorbando", encoding="utf-8")

    chat = store.create()

    assert chat.chat_id
    assert store.get(chat.chat_id) is not None, "en memoria sí está"
