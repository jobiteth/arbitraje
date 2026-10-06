"""Guardia de la propia suite: que no lea los ficheros de quien la ejecuta.

Este fichero no comprueba el producto, comprueba las **pruebas**. Existe porque
el fallo ya ocurrió dos veces y las dos veces costó un rato entenderlo: la suite
pasaba en CI y fallaba en la máquina de desarrollo por un `config.toml` que no
tenía nada que ver con lo que se estaba comprobando.

La primera vez fue `Settings()` leyendo el fichero del usuario. La segunda, al
añadir la primera prueba **unitaria** que construía un `Settings()` completo: el
aislamiento vivía en `tests/integration/`, y el fallo no distingue carpetas.

Lo que se afirma aquí es que el aislamiento está puesto de verdad, no que exista
un fixture con el nombre adecuado. Si alguien mueve o desmonta `isolate_config`,
esto lo dice en una línea en vez de dejar que aparezca como tres fallos raros en
otra parte.
"""

from __future__ import annotations

#: Se importa el **módulo** y no sus funciones. `from ... import config_dir`
#: copia la referencia al objeto función, así que parchear el atributo del
#: módulo —que es lo que hace el fixture— no rebindea la copia y esta prueba
#: leería la del disco mientras el resto de la suite lee la temporal. Se
#: descubrió al escribirla: `config_file()` veía el parche y `config_dir()`, no.
from amigocompora.app.container import build_container
from amigocompora.infra import config as config_module
from amigocompora.infra.secrets import InMemorySecretStore


def test_the_config_file_does_not_exist_during_the_suite() -> None:
    """`config_dir` apunta a un directorio temporal y vacío."""
    assert not config_module.config_file().exists()
    assert "pytest" in str(config_module.config_dir())


def test_the_default_settings_do_not_come_from_a_file_on_disk() -> None:
    """Los valores son los del modelo, no los que alguien tenga configurados.

    `chains` viene vacío en el modelo y la configuración de una máquina de
    desarrollo tiene nodos RPC declarados, así que es el campo que mejor
    distingue «leí el modelo» de «leí el fichero de alguien».
    """
    settings = config_module.Settings()
    assert settings.chains == ()
    assert settings.watch_pairs == ()
    assert settings.execution.enabled is False


async def test_the_execution_ledger_does_not_point_at_the_real_config_dir() -> None:
    """El registro de ejecuciones se abre en el directorio temporal, no en el real.

    Es la **tercera** vez que aparece el mismo fallo, y por eso esta prueba
    existe: el aislamiento parchea `amigocompora.infra.config.config_dir`, así que
    cualquier módulo que se haya quedado con una copia de la función —vía
    `from ... import config_dir`— sigue leyendo el directorio del usuario. Pasó
    con `container.py`, que abría `executions.jsonl` en el directorio real: la
    suite leía el registro de quien la lanzaba, y bastaba con que esa persona
    hubiera ejecutado un swap de verdad una vez para que una prueba del
    contenedor empezara a fallar por un fichero ajeno.

    En la otra dirección es peor todavía: una prueba que escriba a través del
    contenedor anotaría en el registro **real**, y el registro es lo que el tope
    diario usa para contar el gasto. Se afirma sobre la ruta y no sobre el
    contenido porque lo que se comprueba es dónde apunta, no qué hay dentro.

    El contenedor se importa arriba, en el **nivel de módulo**, y no dentro de la
    prueba: el fallo que esto vigila sólo existe si la copia se hizo **antes** de
    que el aislamiento parchee, y un import dentro de la prueba ocurre después y
    copiaría ya la función parcheada. Se comprobó por mutación: con el import
    dentro, esta prueba pasaba con el fallo reintroducido.
    """
    container = await build_container(
        config_module.Settings(),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        ruta = container.policy.ledger.path
        assert ruta.parent == config_module.config_dir()
        assert "pytest" in str(ruta)
    finally:
        await container.aclose()

