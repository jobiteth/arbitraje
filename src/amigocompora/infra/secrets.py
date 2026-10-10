"""Secretos en el keyring del sistema, nunca en disco.

En Windows esto es el Administrador de credenciales; en macOS el Llavero; en
Linux Secret Service. La aplicación nunca escribe una API key en un fichero
propio, y `infra.config` impide por diseño que una acabe en `config.toml`.

`build_config_resolver` es lo que el `EngineRegistry` recibe para resolver la
`required_config` de un motor antes de instanciarlo. El registro no importa
`keyring`: recibe una función, lo que además permite que los tests inyecten un
almacén en memoria.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

import structlog

from amigocompora.domain.errors import (
    AmigocomporaError,
    KeyCustodyError,
    NoWalletError,
)
from amigocompora.domain.protocols import (
    ConfigResolver,
    EngineManifest,
    WalletChannelCredentials,
)
from amigocompora.infra.evm.signer import address_from_key

_log = structlog.get_logger(__name__)

#: Nombre del servicio bajo el que se agrupan las credenciales en el keyring.
SERVICE_NAME: Final = "Amigocompora"

#: Prefijo de las credenciales que **no** son de un motor: la cartera que firma
#: y lo que la habilita. Van separadas de las de motor porque no siguen su
#: ciclo de vida —no se activan ni se desactivan con un motor— y porque mezclar
#: una clave privada con una API key en el mismo espacio de nombres invita a
#: tratarlas igual, que es justo lo que no hay que hacer.
APP_PREFIX: Final = "app:"

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")

#: Credencial de la clave privada que firma. La aplicación no la escribe nunca.
#:
#: `noqa: S105` porque el analizador ve «KEY» en el nombre y avisa de un secreto
#: incrustado: lo que hay aquí es el **nombre de la entrada** en el almacén, no
#: la clave. La clave no aparece en este fichero ni puede aparecer — escribirla
#: aquí sería justo el fallo que estas dos constantes existen para evitar.
PRIVATE_KEY_SECRET: Final = "execution_private_key"  # noqa: S105
#: Credencial de la frase que habilita la ejecución desatendida. Mismo caso.
AUTONOMY_PASSPHRASE_SECRET: Final = "execution_autonomy_passphrase"  # noqa: S105
#: Clave API del relayer de Polymarket y la dirección a la que pertenece.
POLYMARKET_RELAYER_KEY_SECRET: Final = "polymarket_relayer_key"  # noqa: S105
POLYMARKET_RELAYER_ADDRESS_SECRET: Final = "polymarket_relayer_address"  # noqa: S105
#: Builder key de Polymarket: la única que puede crear la deposit wallet.
POLYMARKET_BUILDER_KEY_SECRET: Final = "polymarket_builder_key"  # noqa: S105
POLYMARKET_BUILDER_SECRET_SECRET: Final = "polymarket_builder_secret"  # noqa: S105
POLYMARKET_BUILDER_PASSPHRASE_SECRET: Final = "polymarket_builder_passphrase"  # noqa: S105


class SecretStoreError(AmigocomporaError):
    """El almacén de credenciales del sistema no está disponible."""


class MissingSecretError(AmigocomporaError):
    """Se pidió un secreto por su nombre y no está en ningún sitio.

    Distinto de `SecretStoreError`: ahí el almacén no responde y puede que el
    secreto exista; aquí el almacén respondió y dijo que no lo tiene. El mensaje
    tiene que decir **qué variable** definir, porque quien lo lee sólo ve que un
    endpoint no funciona.
    """

    def __init__(self, name: str, place: str) -> None:
        self.name = name
        self.place = place
        super().__init__(
            f"falta el secreto «{name}», que se usa en {place}. Guárdalo en el "
            f"administrador de credenciales como «{app_secret_key(name)}» o define "
            f"la variable {app_env_var_name(name)}."
        )


def secret_key(engine_id: str, option: str) -> str:
    """Nombre de la credencial: `<engine_id>:<option>`."""
    return f"{engine_id}:{option}"


def app_secret_key(name: str) -> str:
    """Credencial de la aplicación, no de un motor: `app:<name>`."""
    return f"{APP_PREFIX}{name}"


def env_var_name(engine_id: str, option: str) -> str:
    """Variable de entorno equivalente, para CI y contenedores."""
    slug = _NON_ALNUM.sub("_", f"{engine_id}_{option}".upper()).strip("_")
    return f"AMIGOCOMPORA_ENGINE_{slug}"


def app_env_var_name(name: str) -> str:
    """Variable de entorno equivalente para una credencial de la aplicación.

    `app_env_var_name("execution_private_key")` da
    `AMIGOCOMPORA_EXECUTION_PRIVATE_KEY`, sin el `ENGINE_` que llevan las de
    motor.
    """
    slug = _NON_ALNUM.sub("_", name.upper()).strip("_")
    return f"AMIGOCOMPORA_{slug}"


def _first_missing(values: Sequence[tuple[str, str | None]]) -> str:
    """El primer nombre cuyo valor falta, para poder decir **cuál** es."""
    return next(name for name, value in values if value is None)


@runtime_checkable
class SecretStore(Protocol):
    """Almacén de credenciales. Ninguna implementación debe loguear valores."""

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class KeyringSecretStore:
    """Implementación real sobre el keyring del sistema operativo."""

    __slots__ = ("_service",)

    def __init__(self, service: str = SERVICE_NAME) -> None:
        self._service = service

    def get(self, key: str) -> str | None:
        import keyring
        import keyring.errors

        try:
            return keyring.get_password(self._service, key)
        except keyring.errors.KeyringError as error:
            raise SecretStoreError(
                f"no se pudo leer «{key}» del almacén de credenciales: {error}"
            ) from error

    def set(self, key: str, value: str) -> None:
        import keyring
        import keyring.errors

        try:
            keyring.set_password(self._service, key, value)
        except keyring.errors.KeyringError as error:
            raise SecretStoreError(
                f"no se pudo guardar «{key}» en el almacén de credenciales: {error}"
            ) from error

    def delete(self, key: str) -> None:
        import keyring
        import keyring.errors

        try:
            keyring.delete_password(self._service, key)
        except keyring.errors.PasswordDeleteError:
            # Borrar algo que no existe es el estado deseado: no es un error.
            return
        except keyring.errors.KeyringError as error:
            raise SecretStoreError(
                f"no se pudo borrar «{key}» del almacén de credenciales: {error}"
            ) from error


@dataclass(slots=True)
class InMemorySecretStore:
    """Almacén volátil, para tests. No persiste nada."""

    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class SpendingKeyProvider:
    """Entrega la clave privada que firma, y sólo cuando hay que firmar.

    Existe separada del `SecretStore` porque la política de custodia de una
    clave privada **no** es la de una API key, y mezclarlas habría hecho que la
    primera heredara las comodidades de la segunda:

    - Una API key filtrada se rota en un minuto. Una clave privada filtrada
      vacía la cartera para siempre, y no hay rotación que lo arregle: la
      dirección cambia, y con ella los fondos.
    - Por eso el fallback a variable de entorno —razonable para una API key, y
      la única vía que funciona en una máquina sin keyring— aquí viene
      **apagado por omisión**. Se enciende con `allow_env_fallback`, que es una
      decisión explícita de quien opera y no un descuido de configuración.

    Nada de esto guarda la clave: se devuelve, se usa y se deja de referenciar.
    La aplicación no la escribe en disco, no la cachea y no la registra.
    """

    __slots__ = ("_allow_env_fallback", "_store")

    def __init__(
        self,
        store: SecretStore,
        *,
        allow_env_fallback: bool = False,
    ) -> None:
        self._store = store
        self._allow_env_fallback = allow_env_fallback

    @property
    def env_var(self) -> str:
        """La variable donde se buscaría, para poder decírselo al usuario."""
        return app_env_var_name(PRIVATE_KEY_SECRET)

    def available(self) -> bool:
        """Si hay con qué firmar. No lanza: la UI lo llama para pintar botones."""
        try:
            return self.get() is not None
        except (NoWalletError, KeyCustodyError):
            return False

    def address(self) -> str | None:
        """La **dirección pública** que firmaría, o `None` si no hay cartera.

        Deriva la dirección de la clave —una operación pública y barata— y
        devuelve la dirección, nunca la clave. Es lo que la interfaz necesita para
        enseñar de qué cartera saldría el dinero y para proponerla como destino por
        omisión: teclear una dirección a mano es la forma más común de perder
        fondos, y ofrecer la propia la elimina.

        No lanza, igual que `available`: es una consulta para pintar la pantalla, y
        una pantalla que no se puede pintar porque el llavero no responde es peor
        que una que dice «sin cartera». Quien vaya a **firmar** usa `require()`, que
        sí distingue «no hay clave» de «el almacén falló».
        """
        try:
            return address_from_key(self.require())
        except (NoWalletError, KeyCustodyError):
            return None

    def get(self) -> str | None:
        """La clave privada, o `None` si no hay ninguna configurada.

        Lanza `KeyCustodyError` cuando **sí** podría haberla pero el almacén no
        responde y el fallback está apagado: distinguirlo de «no hay clave»
        importa, porque el mensaje que hay que darle al usuario es distinto —
        «configúrala» frente a «tu almacén no funciona y no voy a leerla de un
        sitio inseguro».
        """
        store_error: SecretStoreError | None = None
        try:
            value = self._store.get(app_secret_key(PRIVATE_KEY_SECRET))
        except SecretStoreError as error:
            store_error = error
            value = None

        if value:
            return value

        if self._allow_env_fallback:
            from_env = os.environ.get(self.env_var)
            if from_env:
                _log.warning(
                    "secrets.private_key_from_env",
                    hint=(
                        "la clave privada se leyó de una variable de entorno. "
                        "Cualquiera con acceso al entorno del proceso la tiene."
                    ),
                )
                return from_env

        if store_error is not None and not self._allow_env_fallback:
            raise KeyCustodyError(
                f"el almacén de credenciales no está disponible ({store_error}) y el "
                f"fallback a la variable de entorno está apagado. Una clave privada "
                f"en el entorno del proceso la ve cualquiera que pueda leer el "
                f"entorno, y a diferencia de una API key no se puede rotar sin "
                f"cambiar de cartera. Instala un almacén cifrado (por ejemplo "
                f"`keyrings.cryptfile`) o activa explícitamente "
                f"`execution.allow_env_key` si asumes el riesgo."
            )

        if store_error is not None:
            _log.warning("secrets.store_unavailable", reason=str(store_error))
        return None

    def require(self) -> str:
        """Como `get`, pero lanza `NoWalletError` si no hay clave.

        Es lo que llama el caso de uso: si no hay con qué firmar, la operación
        no se intenta siquiera, y el mensaje dice dónde se configura.
        """
        key = self.get()
        if key is None:
            raise NoWalletError(
                f"no hay ninguna cartera configurada, así que no hay con qué firmar. "
                f"Guarda la clave privada en el administrador de credenciales como "
                f"«{app_secret_key(PRIVATE_KEY_SECRET)}»"
                + (f" o en {self.env_var}" if self._allow_env_fallback else "")
                + "."
            )
        return key

    def env_lookup(self) -> str | None:
        """La variable de la que saldría la clave, si es que sale de una.

        Contesta «¿el valor que falta llegaría por el entorno?», que es una
        pregunta distinta de «¿hay valor?»: con el respaldo apagado la respuesta
        es no aunque la variable esté puesta, y es justo lo que hay que poder
        decirle a quien mira una credencial cuando el llavero no responde.
        Pensada para preguntarse **cuando el almacén ya ha fallado**, que es
        donde se llama.
        """
        try:
            return self.env_var if self.get() else None
        except KeyCustodyError:
            # El respaldo está apagado: nombrar una variable que el proveedor no
            # va a leer sería prometer una clave que no va a aparecer.
            return None


class PolymarketWalletChannelProvider:
    """Las credenciales del canal de la deposit wallet de Polymarket.

    Son las del relayer: la **Builder key** —la única que puede desplegar la
    wallet y firmar los lotes por cuenta de cualquiera— y, opcional, la
    **Relayer key** con su dirección, que sólo sirve para operaciones de esa
    misma dirección (medido: el relayer la rechaza para otro dueño). Sin la
    Builder key no hay canal, y `available()` lo dice; con ella y sin Relayer,
    los lotes van autenticados con la Builder key, que es lo que se midió que
    funciona.

    Se leen en el momento y no se cachean: `require()` devuelve un valor que
    quien llama usa y suelta, igual que la clave privada. El fallback a variable
    de entorno viene **encendido**, como en el resto de las claves de API de la
    aplicación y a diferencia de la clave privada: una API key filtrada se rota
    en un minuto, y estas dos no pueden mover un céntimo sin una firma de la
    cartera, que no viaja por aquí.
    """

    __slots__ = ("_allow_env_fallback", "_store")

    def __init__(self, store: SecretStore, *, allow_env_fallback: bool = True) -> None:
        self._store = store
        self._allow_env_fallback = allow_env_fallback

    def _read(self, name: str) -> str | None:
        try:
            value = self._store.get(app_secret_key(name))
        except SecretStoreError as error:
            # Un almacén caído se trata como «esta clave no está»: la decisión
            # —operar por la EOA— recae del lado seguro, y el aviso dice la causa.
            _log.warning("secrets.wallet_channel_store_unavailable", reason=str(error))
            value = None
        if value:
            return value
        if self._allow_env_fallback:
            from_env = os.environ.get(app_env_var_name(name))
            if from_env:
                _log.warning(
                    "secrets.wallet_channel_from_env",
                    name=name,
                    hint=(
                        "una credencial del canal de la deposit wallet se leyó de "
                        "una variable de entorno; cualquiera con acceso al entorno "
                        "del proceso la ve."
                    ),
                )
                return from_env
        return None

    def available(self) -> bool:
        """Si hay con qué operar la wallet. No lanza: se pregunta para decidir."""
        return all(
            self._read(name)
            for name in (
                POLYMARKET_BUILDER_KEY_SECRET,
                POLYMARKET_BUILDER_SECRET_SECRET,
                POLYMARKET_BUILDER_PASSPHRASE_SECRET,
            )
        )

    def require(self) -> WalletChannelCredentials:
        """Las credenciales del canal, o un error que dice **cuál** falta."""
        builder = self._read(POLYMARKET_BUILDER_KEY_SECRET)
        secret = self._read(POLYMARKET_BUILDER_SECRET_SECRET)
        passphrase = self._read(POLYMARKET_BUILDER_PASSPHRASE_SECRET)
        if builder is None or secret is None or passphrase is None:
            raise MissingSecretError(
                _first_missing(
                    (
                        (POLYMARKET_BUILDER_KEY_SECRET, builder),
                        (POLYMARKET_BUILDER_SECRET_SECRET, secret),
                        (POLYMARKET_BUILDER_PASSPHRASE_SECRET, passphrase),
                    )
                ),
                "el canal de la deposit wallet de Polymarket",
            )
        relayer_key = self._read(POLYMARKET_RELAYER_KEY_SECRET)
        relayer_address = self._read(POLYMARKET_RELAYER_ADDRESS_SECRET)
        if relayer_key is None or relayer_address is None:
            # A medias no vale —la clave sin su dirección no autentica nada, y la
            # dirección sin la clave tampoco—, así que se opera sin Relayer key.
            relayer_key = None
            relayer_address = None
        return WalletChannelCredentials(
            builder_api_key=builder,
            builder_secret=secret,
            builder_passphrase=passphrase,
            relayer_api_key=relayer_key,
            relayer_address=relayer_address,
        )


class AutonomyPassphraseProvider:
    """La frase que habilita la ejecución desatendida.

    Es prima hermana de `SpendingKeyProvider` y **no** la misma cosa: esta frase
    no firma nada ni mueve un céntimo por sí sola. Lo que hace es convertir «hay
    una clave cargada» en «se emite sin preguntar», que es la decisión que un
    operador tiene que tomar a propósito y no descubrir después.

    Por eso se lee de los mismos dos sitios y con el mismo recato, y por eso
    `get` **no lanza nunca**: un llavero roto, una frase ausente y una frase vacía
    significan lo mismo —no hay autonomía— y el fallo cae del lado de preguntar.
    Es la diferencia deliberada con `SpendingKeyProvider.require()`, donde un
    error de custodia **sí** tiene que detener la operación: quedarse sin clave
    impide firmar, y quedarse sin frase sólo impide firmar *sin preguntar*.
    """

    __slots__ = ("_allow_env_fallback", "_store")

    def __init__(
        self,
        store: SecretStore,
        *,
        allow_env_fallback: bool = False,
    ) -> None:
        self._store = store
        self._allow_env_fallback = allow_env_fallback

    @property
    def env_var(self) -> str:
        """La variable donde se buscaría, para poder decírselo al usuario."""
        return app_env_var_name(AUTONOMY_PASSPHRASE_SECRET)

    def get(self) -> str | None:
        """La frase, o `None` si no hay ninguna configurada.

        El llavero manda sobre el entorno: si hay frase guardada, la del entorno
        ni se mira. Es la misma precedencia que usa la clave privada, y por la
        misma razón — el sitio bueno gana cuando existe, y el respaldo sólo
        rellena su ausencia.
        """
        try:
            value = self._store.get(app_secret_key(AUTONOMY_PASSPHRASE_SECRET))
        except SecretStoreError as error:
            # Un llavero que no responde no es motivo para no arrancar: sin frase
            # no hay autonomía, y sin autonomía la aplicación sigue sirviendo para
            # todo lo demás. Se registra para que el silencio no oculte la causa
            # cuando alguien pregunte por qué no se arma.
            _log.warning("secrets.passphrase_store_unavailable", reason=str(error))
            value = None

        if value:
            return value

        if self._allow_env_fallback:
            from_env = os.environ.get(self.env_var)
            if from_env:
                _log.warning(
                    "secrets.passphrase_from_env",
                    hint=(
                        "la frase de autonomía se leyó de una variable de entorno. "
                        "Quien pueda leer el entorno del proceso puede habilitar la "
                        "ejecución sin confirmación."
                    ),
                )
                return from_env
        return None

    def env_lookup(self) -> str | None:
        """La variable de la que saldría la frase, si es que sale de una.

        Mismo caso que en `SpendingKeyProvider`: se pregunta cuando el llavero no
        responde, para poder decir que la credencial **está** aunque no sea de ahí.
        """
        return self.env_var if self.get() else None


#: Marcador de secreto dentro de un texto de configuración: `${NOMBRE}`.
#:
#: El nombre va deliberadamente restringido a identificadores simples. Un
#: marcador que admitiera cualquier cosa sería una plantilla, y una plantilla en
#: un fichero de configuración es una superficie de ataque: `${a}${b}` o rutas
#: con `..` dejarían de ser una referencia a una credencial para convertirse en
#: una forma de leer otra cosa.
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z][A-Za-z0-9_]*)\}")


def has_placeholders(text: str) -> bool:
    """Si el texto referencia algún secreto. No revela cuáles."""
    return bool(_PLACEHOLDER.search(text))


def placeholder_names(text: str) -> tuple[str, ...]:
    """Los nombres que el texto referencia, en orden y sin repetir.

    Existe para que la interfaz pueda **ofrecer** la casilla de una clave de RPC
    sin tener escrita en ninguna parte la lista de proveedores. Quien escribe
    `${INFURA_API_KEY}` en un endpoint de `config.toml` está declarando que
    necesita esa credencial; leer el nombre de ahí es lo que hace que añadir un
    nodo de Alchemy, de QuickNode o de un proveedor que no existía cuando se
    escribió esto no obligue a tocar la interfaz.

    Devolver los nombres **no** revela ningún valor: un nombre es lo que ya está
    escrito en el fichero de configuración, que es público por diseño.
    """
    found: list[str] = []
    for match in _PLACEHOLDER.finditer(text):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return tuple(found)


def resolve_placeholders(
    text: str,
    store: SecretStore,
    *,
    allow_env_fallback: bool = True,
    place: str = "la configuración",
) -> str:
    """Sustituye cada `${NOMBRE}` por la credencial `app:NOMBRE`.

    Existe para que una clave de RPC —que viaja **dentro** de una URL— no tenga
    que escribirse en `config.toml`. El fichero de configuración se copia, se
    pega en un issue y se sube a un repositorio; una URL con la clave dentro
    convierte cada una de esas cosas en una filtración. Con el marcador, el
    fichero dice dónde está la clave y la clave está en otro sitio.

    Se resuelve **una sola vez**, al construir el contenedor, y no en cada
    petición: el valor sustituido no se guarda en ningún objeto de larga vida más
    allá del cliente HTTP, que ya tenía que llevar la URL de todos modos.

    Un marcador que no se puede resolver es un error, no una cadena vacía: dejar
    la URL con `${...}` dentro produciría un endpoint que falla con un error de
    DNS incomprensible, y sustituirlo por vacío produciría uno que responde 401
    sin decir por qué.
    """
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            value = store.get(app_secret_key(name))
        except SecretStoreError:
            # Un almacén caído no es un secreto ausente, pero tampoco se puede
            # inventar el valor: se trata como ausente y el mensaje dirá las dos
            # vías de configurarlo, una de las cuales no depende del almacén.
            value = None
        value = value or (os.environ.get(app_env_var_name(name)) if allow_env_fallback else None)
        if not value:
            missing.append(name)
            return match.group(0)
        return value

    resolved = _PLACEHOLDER.sub(replace, text)
    if missing:
        raise MissingSecretError(missing[0], place)
    return resolved


def build_config_resolver(
    store: SecretStore,
    *,
    allow_env_fallback: bool = True,
) -> ConfigResolver:
    """Devuelve el `ConfigResolver` que consume `EngineRegistry`.

    Orden de resolución por clave: keyring, y si no está, variable de entorno.
    El fallback por entorno existe para CI y despliegues automatizados, donde no
    hay keyring; se puede desactivar.

    Resuelve también las claves **opcionales** del manifiesto, pero no avisa de
    las que falten: si faltan, el motor funciona igual, y un aviso ahí entrenaría
    al usuario a ignorar los avisos que sí importan.

    ### Un almacén caído no es una clave ausente

    Si el keyring no está disponible —una máquina sin Secret Service, un
    contenedor, un servidor sin sesión gráfica—, `store.get` lanza
    `SecretStoreError`. Eso **no** puede impedir arrancar un motor cuyas claves
    sean todas opcionales: el manifiesto declara `optional_config` justo para
    que el motor funcione sin ellas, y `geckoterminal` es el caso real (sin su
    API key cotiza igual, sólo más despacio).

    Por eso un fallo del almacén se trata como «esta clave no está» y se sigue
    por el fallback de entorno. La diferencia sólo se hace notar cuando el
    problema cambia de naturaleza: si además falta una clave **obligatoria**, no
    se puede afirmar que el usuario no la haya configurado —puede tenerla
    guardada en un almacén que no responde—, así que ahí se lanza
    `SecretStoreError` en vez de un «te falta configuración» que culparía al
    usuario de un fallo del sistema.

    Sólo se registra **qué** claves se encontraron, nunca su valor.
    """

    def resolve(manifest: EngineManifest) -> Mapping[str, str]:
        resolved: dict[str, str] = {}
        missing: list[str] = []
        store_error: SecretStoreError | None = None
        required = frozenset(manifest.required_config)
        for option in manifest.config_options:
            try:
                value = store.get(secret_key(manifest.engine_id, option))
            except SecretStoreError as error:
                # Se guarda el primero: los siguientes serán el mismo fallo.
                store_error = store_error or error
                value = None
            if value is None and allow_env_fallback:
                value = os.environ.get(env_var_name(manifest.engine_id, option))
            if value:
                resolved[option] = value
            elif option in required:
                missing.append(option)

        if store_error is not None:
            _log.warning(
                "secrets.store_unavailable",
                engine_id=manifest.engine_id,
                reason=str(store_error),
                hint=(
                    "el motor arranca igual si sus claves son opcionales; si no, "
                    "define las variables de entorno equivalentes"
                ),
            )
        if missing:
            if store_error is not None:
                raise SecretStoreError(
                    f"no se pudo comprobar la configuración de «{manifest.engine_id}»: "
                    f"el almacén de credenciales no está disponible ({store_error}). "
                    f"Claves obligatorias sin resolver: {', '.join(missing)}"
                )
            _log.warning(
                "secrets.missing",
                engine_id=manifest.engine_id,
                keys=missing,
                hint="guárdalas en el administrador de credenciales del sistema",
            )
        return resolved

    return resolve
