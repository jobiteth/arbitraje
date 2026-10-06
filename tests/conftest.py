"""Aislamiento de la suite respecto a la máquina que la ejecuta.

Existe por un fallo medido, y no era teórico: `Settings()` lee el `config.toml`
del directorio de configuración del **usuario**, así que la suite dependía de si
quien la ejecuta tiene o no uno propio. En CI no hay ninguno y todo pasaba; en la
máquina de desarrollo, en cuanto apareció un `config.toml` con nodos RPC
declarados, tres pruebas del contenedor empezaron a fallar por un fichero que no
tiene nada que ver con lo que estaban comprobando.

Una prueba que cambia de resultado según los ficheros de quien la lanza no está
comprobando el código: está comprobando el escritorio. Lo que se fija aquí es que
la configuración se lea de un directorio temporal y vacío, y que el entorno no
aporte credenciales que la prueba no haya puesto ella misma.

Vive en la raíz de `tests/` y no en `tests/integration/` porque el fallo no
distingue carpetas: en cuanto una prueba unitaria construye un `Settings()` —y
la de la configuración de la ejecución lo hace— vuelve a depender del escritorio
de quien la lanza. Estuvo en integración y la misma dependencia reapareció al
añadir la primera prueba unitaria que tocaba la configuración completa.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Prefijo de todas las variables de la aplicación. Las que definan el `.env` del
#: repositorio o el entorno real del proceso se retiran durante la prueba: si una
#: prueba necesita una credencial, la pone ella.
APP_PREFIX = "AMIGOCOMPORA_"


@pytest.fixture(autouse=True)
def isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ni el `config.toml` del usuario ni sus variables de entorno entran aquí."""
    monkeypatch.setattr(
        "amigocompora.infra.config.config_dir",
        lambda: tmp_path / "config",
    )
    # `config_dir()` no crea el directorio y `config_file()` sólo compone la ruta,
    # así que basta con que no exista: es el caso «esta máquina no tiene
    # configuración», que es el que las pruebas deben ejercitar por omisión.
    for name in [key for key in os.environ if key.startswith(APP_PREFIX)]:
        monkeypatch.delenv(name)
    # No hay desmontaje propio: `monkeypatch` deshace los dos cambios al terminar.
