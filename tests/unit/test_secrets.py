"""Tests del resolutor de credenciales.

El caso que se fija aquí es el que se midió contra el entorno real: una máquina
sin backend de keyring hacía fallar el arranque de `geckoterminal` por una clave
que su manifiesto declara **opcional**. Un almacén caído y una clave ausente no
son lo mismo, y el resolutor tiene que distinguirlo.
"""

from __future__ import annotations

import pytest

from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.infra.secrets import (
    InMemorySecretStore,
    SecretStoreError,
    build_config_resolver,
    env_var_name,
)

OPTIONAL_ENGINE = EngineManifest(
    engine_id="geckoterminal",
    name="Fuente con clave opcional",
    version="1.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary="Cotiza con o sin API key; con ella sube el límite de peticiones.",
    optional_config=("coingecko_api_key",),
)

REQUIRED_ENGINE = EngineManifest(
    engine_id="claude",
    name="Asistente con clave obligatoria",
    version="1.0.0",
    kind=EngineKind.AI_ADVISOR,
    summary="Sin clave no hay nada que consultar.",
    required_config=("api_key",),
)


class BrokenStore:
    """Almacén que simula un keyring sin backend disponible."""

    __slots__ = ()

    def get(self, key: str) -> str | None:
        raise SecretStoreError(f"no se pudo leer «{key}»: no hay backend de keyring")

    def set(self, key: str, value: str) -> None:
        raise SecretStoreError("no hay backend de keyring")

    def delete(self, key: str) -> None:
        raise SecretStoreError("no hay backend de keyring")


def test_broken_store_does_not_block_engine_with_optional_keys() -> None:
    """El motor arranca: ninguna de sus claves es obligatoria."""
    resolve = build_config_resolver(BrokenStore())
    assert resolve(OPTIONAL_ENGINE) == {}


def test_broken_store_still_allows_env_fallback() -> None:
    """El fallback por entorno se sigue consultando tras fallar el almacén."""
    var = env_var_name(OPTIONAL_ENGINE.engine_id, "coingecko_api_key")
    resolve = build_config_resolver(BrokenStore())
    assert resolve(OPTIONAL_ENGINE) == {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(var, "clave-de-ci")
        assert resolve(OPTIONAL_ENGINE) == {"coingecko_api_key": "clave-de-ci"}


def test_missing_required_key_with_working_store_is_not_a_store_error() -> None:
    """Sin clave obligatoria se devuelve vacío: `validate_config` da el mensaje.

    No se lanza desde aquí porque con un almacén sano la causa es exactamente
    «el usuario no la ha configurado», que es lo que `validate_config` sabe
    explicar mejor.
    """
    resolve = build_config_resolver(InMemorySecretStore())
    assert resolve(REQUIRED_ENGINE) == {}


def test_missing_required_key_with_broken_store_blames_the_store() -> None:
    """Un fallo del sistema no se reporta como un fallo del usuario.

    Si el almacén no responde no se puede afirmar que falte la clave: puede
    estar guardada y ser el keyring el que no contesta. El error tiene que decir
    eso, y nombrar la clave que quedó sin resolver.
    """
    resolve = build_config_resolver(BrokenStore())
    with pytest.raises(SecretStoreError) as caught:
        resolve(REQUIRED_ENGINE)
    message = str(caught.value)
    assert "claude" in message
    assert "api_key" in message
    assert "no está disponible" in message


def test_required_key_resolved_from_env_survives_broken_store() -> None:
    """Con la clave obligatoria en el entorno, el almacén caído es irrelevante."""
    var = env_var_name(REQUIRED_ENGINE.engine_id, "api_key")
    resolve = build_config_resolver(BrokenStore())
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(var, "clave-de-ci")
        assert resolve(REQUIRED_ENGINE) == {"api_key": "clave-de-ci"}


def test_env_fallback_can_be_disabled() -> None:
    var = env_var_name(REQUIRED_ENGINE.engine_id, "api_key")
    resolve = build_config_resolver(BrokenStore(), allow_env_fallback=False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(var, "clave-de-ci")
        with pytest.raises(SecretStoreError):
            resolve(REQUIRED_ENGINE)
