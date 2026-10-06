"""El indicador de ejecución desatendida de la barra de estado.

Existe por un hueco que no era de funcionalidad sino de **visibilidad**. Armada
la autonomía, la aplicación firma y emite transacciones reales sin preguntar,
dentro de las listas blancas y los topes — y hasta esta prueba eso ocurría sin
que nada en pantalla lo dijera. El modo sí se veía, y el interruptor maestro
también, pero la autonomía no: era el único de los tres estados que autorizan a
gastar que había que deducir.

El error caro tiene dirección. Alguien que dé por armada una aplicación desarmada
pierde una operación; alguien que dé por desarmada una que sí lo está deja de
mirar justo cuando la aplicación está decidiendo sola. Por eso el indicador se
pinta con el color de peligro, dice qué implica, y —esto es lo que se comprueba
aquí— **se apaga** en cuanto se desarma: un aviso que se queda encendido de más
es el que enseña a no mirarlo.

Se monta la ventana de verdad, con un `Container` de verdad, porque lo que se
prueba es el cableado —política → observador → etiqueta— y ese cableado no existe
en ninguna clase por separado.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from amigocompora.app.container import Container, build_container
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    AUTONOMY_PASSPHRASE_SECRET,
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    app_secret_key,
)
from amigocompora.ui.main_window import MainWindow

#: Clave de desarrollo **publicada** (la primera de Hardhat). No es un secreto:
#: que sea conocida es justo lo que la hace útil, porque se puede afirmar sobre
#: la dirección derivada sin que la calcule el código que se está probando.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
FRASE_DE_AUTONOMIA = "frase-de-la-prueba"


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _store() -> InMemorySecretStore:
    """Los dos secretos que hacen falta para poder armar, y no más."""
    store = InMemorySecretStore()
    store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    store.set(app_secret_key(AUTONOMY_PASSPHRASE_SECRET), FRASE_DE_AUTONOMIA)
    return store


@asynccontextmanager
async def _ventana() -> AsyncIterator[tuple[Container, MainWindow]]:
    container = await build_container(
        Settings(), secret_store=_store(), configure_logs=False
    )
    try:
        yield container, MainWindow(container, container.alert_center)
    finally:
        await container.aclose()


async def test_the_alarm_is_off_while_the_autonomy_is_disarmed() -> None:
    """Desarmada, la etiqueta está vacía y no dice «todo bien».

    La ausencia del aviso **es** el mensaje. Una etiqueta permanente que anunciara
    el estado seguro ocuparía el mismo sitio que la alarma y acabaría leyéndose
    igual, que es nada.
    """
    async with _ventana() as (container, window):
        assert container.policy.armed is False
        assert window._autonomy_label.text() == ""


async def test_arming_turns_the_alarm_on_without_anyone_refreshing_it() -> None:
    """Armar enciende el indicador por sí solo, y dice qué implica.

    Se comprueba la cadena entera —política, observador, etiqueta— y no sólo que
    el texto sea el esperado: lo que puede romperse es que la notificación no
    llegue, y entonces la etiqueta se quedaría correcta y muda.
    """
    async with _ventana() as (container, window):
        container.policy.arm(FRASE_DE_AUTONOMIA)

        texto = window._autonomy_label.text()
        assert "ARMADA" in texto
        # No basta con estar encendido: tiene que decir qué significa estar
        # armado, que es que emite sin preguntar.
        assert "sin preguntar" in texto
        assert "desarm" in window._autonomy_label.toolTip().lower()


async def test_disarming_turns_the_alarm_off_again() -> None:
    """Y desarmar la apaga, que es el error que no se puede permitir.

    Un indicador que sólo se enterara de armar se quedaría encendido después de
    desarmar, y entonces diría que la aplicación gasta sin preguntar cuando ya no
    lo hace: el usuario desconfiaría de una alarma que no se apaga nunca.
    """
    async with _ventana() as (container, window):
        container.policy.arm(FRASE_DE_AUTONOMIA)
        assert window._autonomy_label.text() != ""

        container.policy.disarm()
        assert window._autonomy_label.text() == ""


async def test_an_autonomy_that_cannot_arm_shows_no_alarm() -> None:
    """Sin clave que firmar no hay autonomía, y el indicador no la anuncia.

    Es la otra mitad de la propiedad: `armed` es `bandera and can_arm`, así que
    una bandera puesta sin clave no enciende nada. Se comprueba sobre la ventana
    y no sobre la política porque lo que se está probando es lo que se ve.
    """
    container = await build_container(
        Settings(), secret_store=InMemorySecretStore(), configure_logs=False
    )
    try:
        window = MainWindow(container, container.alert_center)
        # Sin los secretos, `can_arm` es falso y armar se rechaza.
        assert container.policy.can_arm is False
        assert window._autonomy_label.text() == ""
    finally:
        await container.aclose()
