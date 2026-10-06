"""El `.env` se lee de verdad, y una credencial dentro no rompe el arranque.

Estas pruebas fijan un fallo real, medido: `.env.example` documentaba desde el
principio «copia este fichero a `.env`», pero `settings_customise_sources`
descartaba `dotenv_settings`, así que rellenar una clave **no tenía ningún
efecto**. El síntoma de una variable ignorada en silencio es el peor posible,
porque quien la escribió cree haberla configurado.

Y al arreglarlo apareció el segundo problema, que es el que da forma a la
solución: con `env_file` en `SettingsConfigDict`, las claves del `.env` se
comparan contra los campos declarados, y una que no corresponda a ninguno choca
con `extra="forbid"` y **aborta el arranque**. Un `.env` con la clave de un
proveedor de RPC dejaba la aplicación sin poder abrirse. La causa de fondo es que
`extra="forbid"` es una defensa sobre `config.toml` —impedir que un secreto
acabe en un fichero que se copia, se pega y se sube a un repositorio— y no tiene
nada que decir sobre el `.env`, que existe precisamente para nombrar secretos.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from amigocompora.infra.config import env_files, load_env_files, load_settings

#: Una clave con la misma forma que la que provocó el fallo: con prefijo de la
#: aplicación y sin corresponder a ningún campo declarado.
UNKNOWN_APP_VAR = "AMIGOCOMPORA_INFURA_API_KEY"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Aísla la prueba del entorno y del `.env` reales de la máquina.

    Snapshot y restauración completos de `os.environ`, y no sólo un `delenv` de
    la variable que a esta prueba le interesa. Hace falta porque `load_env_files`
    escribe en el entorno del proceso y el `.env` del repositorio existe de
    verdad mientras se desarrolla esto: sin restaurar, la primera prueba que
    cargara un `.env` dejaría sus valores puestos para todas las siguientes, y a
    partir de ahí la suite pasaría o fallaría según lo que hubiera en disco.
    """
    saved = dict(os.environ)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "amigocompora.infra.config.config_dir",
        lambda: tmp_path / "config",
    )
    yield tmp_path
    os.environ.clear()
    os.environ.update(saved)


def test_dotenv_is_loaded_into_the_process_environment(clean_env: Path) -> None:
    """El camino documentado en `.env.example` funciona."""
    (clean_env / ".env").write_text(f"{UNKNOWN_APP_VAR}=clave-de-prueba\n")
    load_env_files()
    assert os.environ[UNKNOWN_APP_VAR] == "clave-de-prueba"


def test_an_unknown_credential_in_dotenv_does_not_break_startup(clean_env: Path) -> None:
    """El fallo que motivó esta solución, fijado como prueba.

    Si alguien volviera a poner `env_file` en `SettingsConfigDict`, esto falla con
    `extra_forbidden` y el mensaje apunta al sitio exacto.
    """
    (clean_env / ".env").write_text(f"{UNKNOWN_APP_VAR}=clave-de-prueba\n")
    settings = load_settings()
    assert settings.mode is not None


def test_settings_only_reads_declared_fields_from_dotenv(clean_env: Path) -> None:
    """Una clave desconocida no se convierte en un atributo de la configuración."""
    (clean_env / ".env").write_text(
        f"{UNKNOWN_APP_VAR}=clave-de-prueba\nAMIGOCOMPORA_LOG_LEVEL=DEBUG\n"
    )
    settings = load_settings()
    assert settings.log_level == "DEBUG"
    assert not hasattr(settings, "infura_api_key")


def test_the_real_environment_wins_over_dotenv(clean_env: Path) -> None:
    """Un contenedor puede imponerse sin editar el fichero de nadie."""
    (clean_env / ".env").write_text("AMIGOCOMPORA_LOG_LEVEL=DEBUG\n")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("AMIGOCOMPORA_LOG_LEVEL", "ERROR")
        load_env_files()
        # La comprobación va dentro del bloque: `patch` deshace el `setenv` al
        # salir, así que leerlo después comprobaría el entorno restaurado.
        assert os.environ["AMIGOCOMPORA_LOG_LEVEL"] == "ERROR"


def test_the_most_specific_dotenv_wins(clean_env: Path) -> None:
    """El `.env` del usuario sobrescribe al del repositorio, no al revés.

    El del repositorio es una plantilla compartida; el del directorio de
    configuración es lo que ese usuario decidió. Que gane el general sería una
    sorpresa desagradable y silenciosa.
    """
    config_dir = clean_env / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text("AMIGOCOMPORA_LOG_LEVEL=WARNING\n")
    (clean_env / ".env").write_text("AMIGOCOMPORA_LOG_LEVEL=DEBUG\n")
    load_env_files()
    assert os.environ["AMIGOCOMPORA_LOG_LEVEL"] == "WARNING"


def test_env_files_are_ordered_from_most_to_least_specific(clean_env: Path) -> None:
    """El orden del tuple es el de precedencia, y el primero que define gana."""
    paths = env_files()
    assert paths[0].name == ".env"
    assert paths[0].parent != paths[1].parent


def test_missing_dotenv_is_not_an_error(clean_env: Path) -> None:
    """La mayoría de las instalaciones no tienen `.env` y eso está bien."""
    assert load_env_files() == ()
    assert load_settings().mode is not None
