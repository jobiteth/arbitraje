"""El historial del copiloto: un chat por fichero, y ninguno bloquea el arranque.

El chat dejó de ser «una pregunta y una respuesta»: ahora es una conversación
con el modelo, con sus herramientas y sus propuestas, y una conversación que se
pierde al cerrar la ventana no es un historial — es un borrador. Aquí se
guarda, y se guarda **archivada**: lo que ya no interesa se aparta sin borrarse,
porque un chat viejo todavía explica por qué se hizo lo que se hizo.

### Un fichero por chat, y no un fichero con todos

Es la misma decisión que el resto de la casa toma con lo que crece: escribir un
chat no toca a los demás, y un JSON a medio escribir se lleva por delante **ese**
chat en vez de todo el historial. El coste —leer el directorio al arrancar— es
una decena de ficheros pequeños.

### Lo que no se puede leer no impide charlar

Un fichero roto se salta con un aviso y se sigue: el historial es memoria, no un
estado del que dependa nada crítico, y negarse a abrir la aplicación por un JSON
truncado costaría mucho más que perder un chat. La escritura tampoco lanza: un
fallo al guardar deja el chat vivo en esta sesión y se registra el motivo.

### Por qué vive en `app` y no en `infra`

Por la misma razón que `ExecutionLedger`: recibe el `Path` ya resuelto y no
calcula ninguno. `infra.config.config_dir()` es de la capa de infraestructura, y
una clase de `app` que la llamara ataría el caso de uso al sitio donde el usuario
tenga su configuración. Quien construye el contenedor es quien sabe dónde va.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: Tope del título automático. Es lo que se lee en la lista del historial, que es
#: una columna estrecha: un título de tres renglones ahí no se lee, se recorta.
MAX_TITLE_LENGTH = 48

#: Identificador de chat aceptable como nombre de fichero. Se valida al **leer**
#: —un fichero con un nombre raro se salta— además de al generar: el historial
#: acepta ficheros que no escribió esta clase, y un nombre con `..` o `/` sería
#: una ruta, no un identificador.
_ID_VALIDO = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ChatRole(StrEnum):
    """Quién dijo cada cosa. Se guarda el rol y no un booleano «es mío».

    Un `bool` obligaría a adivinar qué significa el otro valor cada vez que se
    lee el fichero, y aquí hay tres voces —el usuario, el copiloto y lo que
    contestaron las herramientas—, no dos.
    """

    USER = "usuario"
    COPILOT = "copiloto"
    TOOL = "herramienta"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Una línea del chat, con su momento y —si es una herramienta— su detalle.

    `detail` guarda lo que la herramienta devolvió, en crudo. Se enseña plegado
    —es la prueba de lo que el copiloto consultó— y no se manda al modelo: lo que
    se le manda es el resumen que ya viajó en `text`.
    """

    role: ChatRole
    text: str
    at: datetime
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "role": str(self.role),
            "text": self.text,
            "at": self.at.isoformat(),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Chat:
    """Una conversación con el copiloto, con lo mínimo para listarla y abrirla."""

    chat_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    archived: bool = False
    messages: tuple[ChatMessage, ...] = ()

    @property
    def preview(self) -> str:
        """La última línea, para la lista: dice de qué iba sin abrirlo."""
        if not self.messages:
            return ""
        ultimo = self.messages[-1]
        return ultimo.text.replace("\n", " ").strip()[:80]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.chat_id,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "archived": self.archived,
            "messages": [message.to_json() for message in self.messages],
        }


@dataclass(slots=True)
class ChatStore:
    """Los chats guardados, un JSON por chat dentro del directorio que se le dé.

    La copia en memoria se lee una vez y se mantiene al día con cada escritura,
    igual que `UserTokenStore`: el único que escribe estos ficheros es esta
    clase, así que la única forma de que la copia quede vieja es editar el JSON a
    mano mientras la aplicación corre, y para eso está `reload()`.
    """

    directory: Path
    _cache: dict[str, Chat] | None = field(default=None, init=False, repr=False)
    _warned: bool = field(default=False, init=False, repr=False)

    # --------------------------------------------------------------- lectura #
    def chats(self) -> tuple[Chat, ...]:
        """Todos los chats, el más reciente primero, archivados incluidos.

        Se devuelven todos —y no sólo los activos— porque quien filtra por
        archivado es la pantalla: la vista tiene dos listas, pero el almacén no
        tiene por qué saber cuántas pestañas lo van a leer.
        """
        return tuple(
            sorted(
                self._all().values(),
                key=lambda chat: (chat.updated_at, chat.chat_id),
                reverse=True,
            )
        )

    def get(self, chat_id: str) -> Chat | None:
        return self._all().get(chat_id)

    def reload(self) -> tuple[Chat, ...]:
        """Vuelve a leer del disco, descartando la copia en memoria."""
        self._cache = None
        return self.chats()

    def _all(self) -> dict[str, Chat]:
        if self._cache is None:
            self._cache = self._read()
        return self._cache

    def _read(self) -> dict[str, Chat]:
        if not self.directory.is_dir():
            return {}
        found: dict[str, Chat] = {}
        for path in sorted(self.directory.glob("*.json")):
            chat = _parse(path)
            if chat is not None:
                found[chat.chat_id] = chat
        return found

    def _warn_once(self, event: str, **fields: Any) -> None:
        if self._warned:
            return
        self._warned = True
        _log.warning(event, **fields)

    # -------------------------------------------------------------- escritura #
    def create(self, *, title: str = "", chat_id: str | None = None) -> Chat:
        """Un chat vacío, ya guardado: la lista lo enseña sin escribir nada.

        Nace guardado y no en memoria a propósito: un chat recién creado tiene que
        sobrevivir a un cierre inesperado aunque nadie haya escrito todavía, o la
        lista de hoy aparecería vacía mañana y el hueco no se explicaría.
        """
        momento = datetime.now(UTC)
        identificador = chat_id or uuid.uuid4().hex[:12]
        if not _ID_VALIDO.match(identificador):
            raise ValueError(f"«{identificador}» no sirve como identificador de chat")
        chat = Chat(
            chat_id=identificador,
            title=title.strip()[:MAX_TITLE_LENGTH],
            created_at=momento,
            updated_at=momento,
        )
        self._store(chat)
        return chat

    def append(self, chat_id: str, message: ChatMessage) -> Chat | None:
        """Añade una línea. Devuelve el chat actualizado, o `None` si no existe.

        El título se pone aquí, con el primer mensaje del usuario, y no lo pone la
        pantalla: quien escribe en el historial es quien sabe qué decir de él, y
        un título que sólo existe mientras la ventana está abierta desaparece al
        reabrir la lista.
        """
        chat = self._all().get(chat_id)
        if chat is None:
            return None
        titulo = chat.title
        if not titulo and message.role is ChatRole.USER:
            titulo = _titulo_de(message.text)
        actualizado = Chat(
            chat_id=chat.chat_id,
            title=titulo,
            created_at=chat.created_at,
            updated_at=message.at,
            archived=chat.archived,
            messages=(*chat.messages, message),
        )
        self._store(actualizado)
        return actualizado

    def archive(self, chat_id: str, *, archived: bool = True) -> Chat | None:
        """Aparta el chat (o lo devuelve a la lista), sin borrar nada."""
        chat = self._all().get(chat_id)
        if chat is None:
            return None
        actualizado = Chat(
            chat_id=chat.chat_id,
            title=chat.title,
            created_at=chat.created_at,
            updated_at=chat.updated_at,
            archived=archived,
            messages=chat.messages,
        )
        self._store(actualizado)
        return actualizado

    def delete(self, chat_id: str) -> bool:
        """Borra el chat entero. Devuelve si estaba.

        Es el único camino que destruye historial, y por eso el botón que lo
        llama pregunta antes: aquí no hay papelera que deshaga.
        """
        chat = self._all().pop(chat_id, None)
        if chat is None:
            return False
        try:
            self._path(chat_id).unlink(missing_ok=True)
        except OSError as error:
            _log.warning("chat_store.delete_failed", chat_id=chat_id, reason=str(error))
        _log.info("chat_store.deleted", chat_id=chat_id)
        return True

    def _path(self, chat_id: str) -> Path:
        return self.directory / f"{chat_id}.json"

    def _store(self, chat: Chat) -> None:
        """Escribe el fichero del chat y actualiza la copia en memoria.

        Un fallo de escritura **no** se propaga, por la misma razón que en
        `UserTokenStore`: el chat sigue vivo en esta sesión y la conversación no
        se interrumpe por no poder recordarla. Se registra el motivo, que es lo
        único que permitirá explicarlo después.
        """
        self._all()[chat.chat_id] = chat
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            scratch = self._path(chat.chat_id).with_suffix(".json.tmp")
            scratch.write_text(
                json.dumps(chat.to_json(), ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            scratch.replace(self._path(chat.chat_id))
        except OSError as error:
            _log.warning(
                "chat_store.write_failed",
                chat_id=chat.chat_id,
                reason=str(error),
                hint="el chat funciona en esta sesión pero no se recordará",
            )

    def __iter__(self) -> Iterator[Chat]:
        return iter(self.chats())

    def __len__(self) -> int:
        return len(self._all())


# --------------------------------------------------------------------------- #
# Lectura tolerante
# --------------------------------------------------------------------------- #
def _titulo_de(texto: str) -> str:
    """El título que se deduce del primer mensaje del usuario.

    Se corta por la primera línea y por el tope de la columna: la lista del
    historial enseña una línea, y guardar tres renglones para recortarlos al
    pintar sería guardar lo que no se lee.
    """
    primera = texto.strip().splitlines()[0] if texto.strip() else ""
    limpio = " ".join(primera.split())
    if len(limpio) > MAX_TITLE_LENGTH:
        return limpio[: MAX_TITLE_LENGTH - 1].rstrip() + "…"
    return limpio


def _parse(path: Path) -> Chat | None:
    """Lee un fichero de chat, o `None` si no describe ninguno.

    Se valida campo a campo en vez de dejar que reviente el constructor: un
    fichero roto no puede llevarse por delante a los demás chats. Que falte el
    identificador o el título no es un chat — es un fichero que casualmente está
    ahí, y esa distinción es la que evita adoptar cualquier JSON de la carpeta.
    """
    identificador = path.stem
    if not _ID_VALIDO.match(identificador):
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _log.warning("chat_store.unreadable", chat=str(path), reason=str(error))
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("id") != identificador:
        # El nombre del fichero **es** el identificador. Si el contenido dice
        # otro, el par no identifica nada de forma estable y se salta: adoptarlo
        # daría dos chats distintos leídos del mismo sitio según por dónde se
        # llegue.
        return None
    creado = _momento(raw.get("created_at"))
    if creado is None:
        return None
    # `updated_at` puede faltar —un fichero viejo— y entonces vale el de creación;
    # pero si está y no se entiende, el fichero se salta en vez de sustituirlo:
    # el orden del historial es su razón de ser, y corregirlo en silencio por
    # detrás dejaría la lista desordenada sin que nada lo dijera.
    crudo_actualizado = raw.get("updated_at")
    actualizado = creado if crudo_actualizado is None else _momento(crudo_actualizado)
    if actualizado is None:
        return None
    mensajes = raw.get("messages")
    if not isinstance(mensajes, list):
        return None
    return Chat(
        chat_id=identificador,
        title=str(raw.get("title") or "")[:MAX_TITLE_LENGTH],
        created_at=creado,
        updated_at=actualizado,
        archived=bool(raw.get("archived")),
        messages=tuple(
            mensaje for entry in mensajes if (mensaje := _parse_message(entry)) is not None
        ),
    )


def _parse_message(entry: Any) -> ChatMessage | None:
    if not isinstance(entry, dict):
        return None
    texto = entry.get("text")
    momento = _momento(entry.get("at"))
    try:
        rol = ChatRole(str(entry.get("role")))
    except ValueError:
        return None
    if not isinstance(texto, str) or momento is None:
        return None
    detail = entry.get("detail")
    return ChatMessage(
        role=rol,
        text=texto,
        at=momento,
        detail=detail if isinstance(detail, str) else "",
    )


def _momento(raw: Any) -> datetime | None:
    """Una marca de tiempo del fichero, en UTC, o `None` si no lo es.

    Se exige que traiga zona —`fromisoformat` acepta una fecha sin ella— y se
    pasa a UTC: sin eso, dos chats escritos en husos distintos se ordenarían por
    una hora que no es la suya, y el historial aparecería desordenado sin que
    nada estuviera roto.
    """
    if not isinstance(raw, str):
        return None
    try:
        momento = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if momento.tzinfo is None:
        return None
    return momento.astimezone(UTC)
