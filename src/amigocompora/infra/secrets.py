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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

import structlog

from amigocompora.domain.errors import AmigocomporaError
from amigocompora.domain.protocols import ConfigResolver, EngineManifest

_log = structlog.get_logger(__name__)

#: Nombre del servicio bajo el que se agrupan las credenciales en el keyring.
SERVICE_NAME: Final = "Amigocompora"

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


class SecretStoreError(AmigocomporaError):
    """El almacén de credenciales del sistema no está disponible."""


def secret_key(engine_id: str, option: str) -> str:
    """Nombre de la credencial: `<engine_id>:<option>`."""
    return f"{engine_id}:{option}"


def env_var_name(engine_id: str, option: str) -> str:
    """Variable de entorno equivalente, para CI y contenedores."""
    slug = _NON_ALNUM.sub("_", f"{engine_id}_{option}".upper()).strip("_")
    return f"AMIGOCOMPORA_ENGINE_{slug}"


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
