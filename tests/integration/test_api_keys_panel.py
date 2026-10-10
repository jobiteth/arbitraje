"""La subpestaña de APIs de motores: una fila por motor, con sus claves y su modelo.

Antes este dato estaba en dos sitios —una tabla de sólo lectura en Configuración
y una casilla por opción en la tarjeta de credenciales—, así que las dos podían
enseñar cosas distintas. Aquí se comprueba la única vista que queda: que cada
motor registrado tiene su fila, que una clave se guarda como `<motor>:<opción>`
en el llavero y no se enseña nunca, que el modelo —que no es un secreto— sí se
lee, y que un campo en blanco **no** borra lo guardado.

El motor de prueba es «claude» porque su manifiesto declara las dos opciones que
importan aquí: una clave obligatoria (`api_key`) y un modelo opcional (`model`).
Se comprueba contra el registro de verdad del contenedor, no contra una lista
escrita a mano.

Ninguna prueba de este fichero sale a la red.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

from amigocompora.app.container import Container, build_container
from amigocompora.app.registry import RegisteredEngine
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    InMemorySecretStore,
    SecretStoreError,
    env_var_name,
    secret_key,
)
from amigocompora.ui.pages.apis import (
    _COL_ACTIONS,
    _COL_ENGINE,
    ApiKeyDialog,
    ApiKeysPanel,
)

_MOTOR = "claude"

#: Sin respaldo por entorno: el caso por omisión, dicho en voz alta.
_APAGADO: dict[str, object] = {"execution": {"allow_env_key": False}}


class SinLlavero:
    """Un almacén que se comporta como un servidor sin Secret Service."""

    __slots__ = ()

    def get(self, key: str) -> str | None:
        raise SecretStoreError(f"no se pudo leer «{key}»: no hay backend de keyring")

    def set(self, key: str, value: str) -> None:
        raise SecretStoreError("no hay backend de keyring")

    def delete(self, key: str) -> None:
        raise SecretStoreError("no hay backend de keyring")


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@asynccontextmanager
async def _pagina(
    *, store: object | None = None
) -> AsyncIterator[tuple[Container, ApiKeysPanel]]:
    """El panel montado sobre un contenedor de verdad, con su llavero de prueba."""
    container = await build_container(
        Settings.model_validate(_APAGADO),
        secret_store=store if store is not None else InMemorySecretStore(),  # type: ignore[arg-type]
        configure_logs=False,
    )
    try:
        yield container, ApiKeysPanel(container)
    finally:
        await container.aclose()


def _motor(container: Container, engine_id: str = _MOTOR) -> RegisteredEngine:
    for entry in container.registry.available():
        if entry.manifest.engine_id == engine_id:
            return entry
    pytest.fail(f"el contenedor de pruebas no trae el motor «{engine_id}»")


def _fila_del_motor(container: Container, panel: ApiKeysPanel, engine_id: str) -> int:
    entry = _motor(container, engine_id)
    nombre = container.engine_names.resolve(entry.manifest.engine_id, entry.manifest.name)
    for fila in range(panel._table.rowCount()):
        item = panel._table.item(fila, _COL_ENGINE)
        if item is not None and item.text() == nombre:
            return fila
    pytest.fail(f"el panel no tiene fila para «{nombre}»")


# --------------------------------------------------------------------------- #
# La tabla
# --------------------------------------------------------------------------- #
async def test_una_fila_por_motor_registrado() -> None:
    async with _pagina() as (container, panel):
        assert panel._table.rowCount() == len(container.registry.available())
        assert _fila_del_motor(container, panel, _MOTOR) >= 0


async def test_sin_clave_la_columna_lo_dice_y_ofrece_el_modelo_de_fabrica() -> None:
    """El estado de cada opción se lee sin abrir nada, y el modelo no se finge."""
    async with _pagina() as (container, panel):
        motor = _motor(container)
        assert (
            panel._config_text(motor)
            == "api_key: sin configurar; model: por omisión del motor"
        )


async def test_guardar_una_clave_la_ensena_como_configurada() -> None:
    store = InMemorySecretStore()
    store.set(secret_key(_MOTOR, "api_key"), "clave-x")
    store.set(secret_key(_MOTOR, "model"), "modelo-y")
    async with _pagina(store=store) as (container, panel):
        motor = _motor(container)
        panel.refresh()
        assert panel._config_text(motor) == "api_key: configurada; model: modelo-y"


async def test_la_variable_de_un_motor_se_nombra_sin_condiciones() -> None:
    """Las de motor se leen del entorno siempre: no llevan la condición de la firma.

    `allow_env_key` gobierna a los proveedores que firman; el registro de motores
    resuelve sus claves del entorno por omisión porque es la única vía en un
    despliegue sin llavero y no firma nada. Contar aquí ese requisito mandaría a
    encender una opción que no tiene nada que ver con esta credencial.
    """
    async with _pagina() as (container, panel):
        ayuda = panel._config_tooltip(_motor(container))

        assert secret_key(_MOTOR, "api_key") in ayuda
        assert env_var_name(_MOTOR, "api_key") in ayuda
        assert "allow_env_key" not in ayuda
        assert container.settings.execution.allow_env_key is False


async def test_un_motor_sin_opciones_no_tiene_lapiz_ni_configuracion() -> None:
    """El asistente offline funciona sin claves: su fila no ofrece editarlas."""
    async with _pagina() as (container, panel):
        motor = _motor(container, "stub_advisor")
        assert motor.manifest.config_options == ()
        assert panel._config_text(motor) == "—"
        fila = _fila_del_motor(container, panel, "stub_advisor")
        assert panel._table.cellWidget(fila, _COL_ACTIONS) is None


async def test_sin_llavero_el_estado_lo_dice_sin_romper_la_fila() -> None:
    async with _pagina(store=SinLlavero()) as (container, panel):
        assert panel._config_text(_motor(container)).startswith("api_key: llavero no responde")


# --------------------------------------------------------------------------- #
# La ventana de edición
# --------------------------------------------------------------------------- #
async def test_guardar_escribe_cada_opcion_como_motor_dos_puntos_opcion() -> None:
    store = InMemorySecretStore()
    async with _pagina(store=store) as (container, _):
        dialogo = ApiKeyDialog(container, _motor(container))
        dialogo._fields["api_key"].setText("clave-de-prueba")
        dialogo._fields["model"].setText("modelo-de-prueba")
        dialogo._on_save()

        assert store.get(secret_key(_MOTOR, "api_key")) == "clave-de-prueba"
        assert store.get(secret_key(_MOTOR, "model")) == "modelo-de-prueba"
        assert dialogo.saved == ("api_key", "model")
        assert dialogo.result() == QDialog.DialogCode.Accepted
        assert dialogo._fields["api_key"].text() == "", "la clave no se queda en pantalla"


async def test_la_clave_se_escribe_a_ciegas_y_el_modelo_a_la_vista() -> None:
    """El modelo no es un secreto —viaja en cada petición—; la clave sí."""
    async with _pagina() as (container, _):
        dialogo = ApiKeyDialog(container, _motor(container))
        assert dialogo._fields["api_key"].echoMode() == QLineEdit.EchoMode.Password
        assert dialogo._fields["model"].echoMode() == QLineEdit.EchoMode.Normal
        assert dialogo._states["api_key"].text() == "sin configurar"


async def test_el_modelo_guardado_se_lee_en_el_estado() -> None:
    store = InMemorySecretStore()
    store.set(secret_key(_MOTOR, "api_key"), "clave-x")
    store.set(secret_key(_MOTOR, "model"), "modelo-y")
    async with _pagina(store=store) as (container, _):
        dialogo = ApiKeyDialog(container, _motor(container))
        assert dialogo._states["api_key"].text() == "configurada"
        assert dialogo._states["model"].text() == "modelo-y", "el que está en uso"
        assert dialogo._fields["model"].text() == "", "pero no precargado en el campo"


async def test_un_campo_en_blanco_no_borra_lo_guardado() -> None:
    """Dejar el campo en blanco es «no tocar esta opción», no «bórrala»."""
    store = InMemorySecretStore()
    store.set(secret_key(_MOTOR, "api_key"), "clave-x")
    async with _pagina(store=store) as (container, _):
        dialogo = ApiKeyDialog(container, _motor(container))
        dialogo._on_save()

        assert store.get(secret_key(_MOTOR, "api_key")) == "clave-x"
        assert dialogo.saved == ()
        assert dialogo.result() != QDialog.DialogCode.Accepted
        assert "No hay nada que guardar" in dialogo._status.text()


async def test_borrar_quita_las_claves_guardadas_y_apaga_su_boton() -> None:
    store = InMemorySecretStore()
    store.set(secret_key(_MOTOR, "api_key"), "clave-x")
    store.set(secret_key(_MOTOR, "model"), "modelo-y")
    async with _pagina(store=store) as (container, _):
        dialogo = ApiKeyDialog(container, _motor(container))
        assert dialogo._delete_btn.isEnabled()

        dialogo._on_delete()

        assert store.get(secret_key(_MOTOR, "api_key")) is None
        assert store.get(secret_key(_MOTOR, "model")) is None
        assert dialogo.deleted == ("api_key", "model")
        assert dialogo.result() == QDialog.DialogCode.Accepted
        assert not dialogo._delete_btn.isEnabled(), "ya no hay nada que borrar"


async def test_borrar_sin_nada_guardado_no_hace_nada() -> None:
    """El botón está apagado sin claves; forzarlo no borra lo que no existe."""
    async with _pagina() as (container, _):
        dialogo = ApiKeyDialog(container, _motor(container))
        assert not dialogo._delete_btn.isEnabled()

        dialogo._on_delete()

        assert dialogo.deleted == ()
        assert dialogo.result() == QDialog.DialogCode.Accepted
