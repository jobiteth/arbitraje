"""La pestaña Configuración: sus tres subpestañas y la tabla de nodos RPC.

La pestaña dejó de ser una tarjeta con un formulario fijo al final. Ahora son
tres subpestañas —Nodos RPC, APIs de motores, Credenciales— y cada credencial
vive en un solo sitio; lo que se comprueba aquí es eso: que un nodo se agrega o
se edita **en su ventana** (no en un formulario dentro de la tabla), que cada
fila propia tiene sus acciones, y que la tabla reparte el ancho como la de
motores —cada columna mide su contenido y la URL, que es lo largo, se lleva lo
que sobra—.

La ventana de nodo no se abre de verdad: en una prueba no hay nadie que pulse
sus botones, y tiene sus propias pruebas en `test_node_dialog.py`. Aquí se
sustituye por un doble que se acepta solo, para medir el cableado —qué le pasa
el panel y qué hace con lo que devuelve—, y la escritura que sí es real —la de
`config.toml` al eliminar— cae en un directorio temporal.

Ninguna prueba de este fichero sale a la red.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tomlkit
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHeaderView,
    QPushButton,
    QTabWidget,
    QWidget,
)

from amigocompora.app.container import Container, build_container
from amigocompora.domain.chains import CHAINS
from amigocompora.infra.config import RpcEndpointSettings, Settings
from amigocompora.infra.secrets import InMemorySecretStore, app_env_var_name, app_secret_key
from amigocompora.ui.pages import settings as settings_module
from amigocompora.ui.pages.settings import (
    _COL_ACTIONS,
    _COL_CHAIN,
    _COL_KEY,
    _COL_LABEL,
    _COL_ORIGIN,
    _COL_PRIORITY,
    _COL_URL,
    _ORIGEN_CONFIG,
    _ORIGEN_PUBLICO,
    ConfiguracionPage,
    NodesPanel,
)

#: Sin respaldo por entorno: el caso por omisión, dicho en voz alta.
_APAGADO: dict[str, object] = {"execution": {"allow_env_key": False}}

#: Un nodo propio con su clave dentro de la URL —el caso de Infura, de Alchemy
#: y de cualquier proveedor— y un segundo nodo sin clave.
_URL_CON_CLAVE = "https://mainnet.infura.io/v3/${INFURA_API_KEY}"

_CON_DOS_NODOS: dict[str, object] = {
    "execution": {"allow_env_key": False},
    "chains": [
        {
            "chain": "ethereum",
            "endpoints": [
                {"url": _URL_CON_CLAVE, "label": "infura", "priority": 10},
                {"url": "https://eth.llamanodo.example/", "label": "llama", "priority": 20},
            ],
        }
    ],
}

#: La URL que el doble devuelve como nodo recién agregado.
_URL_NUEVA = "https://rpc.nuevo.example/eth"

#: Los dobles de ventana que se abrieron en el test en curso, en orden.
_ABIERTAS: list[NodeDialogFalso] = []


class NodeDialogFalso:
    """La ventana de un nodo, doblada: se acepta sola y deja dicho qué recibió.

    Devuelve la lista que le llegó más un nodo nuevo —lo que haría la ventana de
    verdad tras pulsar Guardar—, para que el panel tenga algo distinto que
    enseñar y se pueda comprobar que lo pinta.
    """

    DialogCode = QDialog.DialogCode

    def __init__(
        self,
        container: Container,
        parent: QWidget | None = None,
        *,
        chain_key: str,
        endpoints: list[RpcEndpointSettings],
        index: int | None = None,
    ) -> None:
        self.container = container
        self.chain_key = chain_key
        self.previos = list(endpoints)
        self.index = index
        self.endpoints: tuple[RpcEndpointSettings, ...] | None = (
            *endpoints,
            RpcEndpointSettings(url=_URL_NUEVA, label="nuevo"),
        )
        self.key_saved = False
        # `exec` se asigna como atributo y no se define como método: un método
        # llamado `exec` sombrea un builtin dentro de la clase (ruff lo señala).
        self.exec = lambda: QDialog.DialogCode.Accepted
        _ABIERTAS.append(self)


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@asynccontextmanager
async def _pagina(
    ajustes: dict[str, object] | None = None,
    *,
    store: object | None = None,
) -> AsyncIterator[tuple[Container, ConfiguracionPage]]:
    """La pestaña montada sobre un contenedor de verdad, con su llavero de prueba."""
    container = await build_container(
        Settings.model_validate(ajustes if ajustes is not None else _APAGADO),
        secret_store=store if store is not None else InMemorySecretStore(),  # type: ignore[arg-type]
        configure_logs=False,
    )
    try:
        yield container, ConfiguracionPage(container)
    finally:
        await container.aclose()


def _texto_de(panel: NodesPanel, fila: int, columna: int) -> str:
    item = panel._table.item(fila, columna)
    assert item is not None, f"la fila {fila} no tiene texto en la columna {columna}"
    return item.text()


def _hay_fila(panel: NodesPanel, marca: str) -> bool:
    """Si alguna fila menciona `marca` en cualquiera de sus celdas."""
    for fila in range(panel._table.rowCount()):
        for columna in range(panel._table.columnCount()):
            item = panel._table.item(fila, columna)
            if item is not None and marca in item.text():
                return True
    return False


def _fila_del_nodo(panel: NodesPanel, marca: str) -> int:
    """La fila cuya etiqueta o URL menciona `marca`. Falla si no hay ninguna."""
    for fila in range(panel._table.rowCount()):
        for columna in (_COL_LABEL, _COL_URL):
            item = panel._table.item(fila, columna)
            if item is not None and marca in item.text():
                return fila
    pytest.fail(f"ninguna fila del panel menciona «{marca}»")


def _boton_de(padre: QWidget, texto: str) -> QPushButton:
    for boton in padre.findChildren(QPushButton):
        if boton.text() == texto:
            return boton
    pytest.fail(f"no hay ningún botón «{texto}» que pulsar")


def _acciones_de(panel: NodesPanel, fila: int) -> QWidget:
    celda = panel._table.cellWidget(fila, _COL_ACTIONS)
    assert celda is not None, f"la fila {fila} no tiene botones de acción"
    return celda


# --------------------------------------------------------------------------- #
# Las tres subpestañas
# --------------------------------------------------------------------------- #
async def test_la_pestana_son_tres_subpestanias() -> None:
    """Nodos, APIs y Credenciales: el reparto que deja cada credencial en un sitio."""
    async with _pagina() as (_, pagina):
        tabs = pagina.findChild(QTabWidget)
        assert tabs is not None
        assert [tabs.tabText(indice) for indice in range(tabs.count())] == [
            "Nodos RPC",
            "APIs de motores",
            "Credenciales",
        ]


async def test_las_columnas_cortas_miden_su_contenido_y_la_url_se_estira() -> None:
    """El ancho se reparte donde hay texto que crece, no a partes iguales.

    Repartido a partes iguales la URL se recortaba y «Origen» se quedaba con un
    palmo de hueco: una columna de una palabra no necesita crecer.
    """
    async with _pagina() as (_, pagina):
        header = pagina._nodes._table.horizontalHeader()

        assert not header.stretchLastSection(), "la última columna es de botones"
        assert header.sectionResizeMode(_COL_URL) == QHeaderView.ResizeMode.Stretch
        for columna in (
            _COL_CHAIN,
            _COL_LABEL,
            _COL_PRIORITY,
            _COL_ORIGIN,
            _COL_KEY,
            _COL_ACTIONS,
        ):
            assert header.sectionResizeMode(columna) == QHeaderView.ResizeMode.ResizeToContents


# --------------------------------------------------------------------------- #
# La tabla de nodos
# --------------------------------------------------------------------------- #
async def test_la_tabla_trae_los_nodos_declarados_y_sus_respaldos() -> None:
    """Una fila por nodo efectivo: los propios primero, los públicos detrás."""
    async with _pagina(_CON_DOS_NODOS) as (_, pagina):
        panel = pagina._nodes
        declarados = sum(len(lista) for lista in panel._declared.values())
        publicos = sum(len(lista) for lista in panel._public.values())

        assert declarados == 2, "los dos endpoints de config.toml"
        assert panel._table.rowCount() == declarados + publicos
        fila = _fila_del_nodo(panel, "infura")
        assert _texto_de(panel, fila, _COL_PRIORITY) == "10"
        assert _texto_de(panel, fila, _COL_ORIGIN) == _ORIGEN_CONFIG
        assert _hay_fila(panel, "llama")
        if publicos:
            assert _hay_fila(panel, _ORIGEN_PUBLICO), "los respaldos se distinguen de lo propio"


async def test_solo_los_nodos_propios_tienen_acciones_de_fila() -> None:
    """Un respaldo público no se edita desde aquí: no está en config.toml."""
    async with _pagina(_CON_DOS_NODOS) as (_, pagina):
        panel = pagina._nodes

        assert _acciones_de(panel, _fila_del_nodo(panel, "infura")) is not None
        for fila in range(panel._table.rowCount()):
            if _texto_de(panel, fila, _COL_ORIGIN) == _ORIGEN_PUBLICO:
                assert panel._table.cellWidget(fila, _COL_ACTIONS) is None


async def test_la_clave_del_nodo_se_lee_en_su_columna() -> None:
    """La columna dice si la clave que pide la URL está, sin enseñarla nunca."""
    store = InMemorySecretStore()
    async with _pagina(_CON_DOS_NODOS, store=store) as (container, pagina):
        panel = pagina._nodes
        fila = _fila_del_nodo(panel, "infura")
        assert _texto_de(panel, fila, _COL_KEY) == "INFURA_API_KEY: sin configurar"

        # La ayuda dice dónde vive —el llavero y la variable— sin la condición de
        # la firma: a los marcadores de config.toml los resuelve el contenedor al
        # arrancar, y `allow_env_key` gobierna a los proveedores que firman.
        celda = panel._table.item(fila, _COL_KEY)
        assert celda is not None
        ayuda = celda.toolTip()
        assert app_env_var_name("INFURA_API_KEY") in ayuda
        assert "allow_env_key" not in ayuda
        assert container.settings.execution.allow_env_key is False
        assert "✎" in ayuda, "y dice cómo escribirla: la ventana de este nodo"

        store.set(app_secret_key("INFURA_API_KEY"), "clave-x")
        panel.refresh_keys()
        assert _texto_de(panel, fila, _COL_KEY) == "INFURA_API_KEY: configurada"


async def test_un_nodo_sin_clave_no_pide_ninguna() -> None:
    """Un nodo público no lleva marcador: su celda de clave no inventa una."""
    async with _pagina(_CON_DOS_NODOS) as (_, pagina):
        fila = _fila_del_nodo(pagina._nodes, "llama")
        assert _texto_de(pagina._nodes, fila, _COL_KEY) == "—"


# --------------------------------------------------------------------------- #
# Agregar, editar y eliminar
# --------------------------------------------------------------------------- #
async def test_agregar_nodo_abre_la_ventana_y_ensena_la_fila(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El botón abre la ventana de alta —no un formulario fijo— y pinta lo guardado."""
    monkeypatch.setattr(settings_module, "NodeDialog", NodeDialogFalso)
    _ABIERTAS.clear()
    async with _pagina(_CON_DOS_NODOS) as (_, pagina):
        panel = pagina._nodes
        _boton_de(panel, "Agregar nodo…").click()

        assert len(_ABIERTAS) == 1
        abierta = _ABIERTAS[0]
        assert abierta.index is None, "el alta no edita ningún nodo existente"
        assert abierta.previos == [], "la ventana del alta no recibe nodos previos"
        assert abierta.chain_key in {spec.key for spec in CHAINS.values() if spec.is_evm}

        # La celda de URL sólo conserva el host —`safe_url` descarta ruta y query
        # porque ahí viajan las claves—, así que la fila se busca por el host.
        fila = _fila_del_nodo(panel, "rpc.nuevo.example")
        assert _texto_de(panel, fila, _COL_ORIGIN) == _ORIGEN_CONFIG
        assert "guardado en config.toml" in panel._status.text()
        assert "Reinicia la aplicación" in panel._status.text()


async def test_el_lapiz_de_la_fila_edita_ese_nodo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editar abre la misma ventana, con el nodo de esa fila y su índice."""
    monkeypatch.setattr(settings_module, "NodeDialog", NodeDialogFalso)
    _ABIERTAS.clear()
    async with _pagina(_CON_DOS_NODOS) as (_, pagina):
        panel = pagina._nodes
        _boton_de(_acciones_de(panel, _fila_del_nodo(panel, "infura")), "✎").click()

        assert len(_ABIERTAS) == 1
        abierta = _ABIERTAS[0]
        assert abierta.index == 0, "edita el nodo de esa fila, no otro"
        assert [endpoint.label for endpoint in abierta.previos] == ["infura", "llama"]
        assert "actualizado en config.toml" in panel._status.text()


async def test_eliminar_de_la_fila_reescribe_config_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Eliminar quita el nodo del fichero y de la tabla, y no toca los demás."""
    destino = tmp_path / "config.toml"
    monkeypatch.setattr(settings_module, "config_file", lambda: destino)
    async with _pagina(_CON_DOS_NODOS) as (_, pagina):
        panel = pagina._nodes
        _boton_de(_acciones_de(panel, _fila_del_nodo(panel, "infura")), "✕").click()

        documento = tomlkit.parse(destino.read_text(encoding="utf-8"))
        urls = [endpoint["url"] for endpoint in documento["chains"][0]["endpoints"]]
        assert urls == ["https://eth.llamanodo.example/"], "sólo se fue el borrado"
        assert panel._declared["ethereum"] == [
            RpcEndpointSettings(url="https://eth.llamanodo.example/", label="llama", priority=20)
        ]
        assert not _hay_fila(panel, "infura")
        assert "eliminado de config.toml" in panel._status.text()


# --------------------------------------------------------------------------- #
# El cableado entre subpestañas
# --------------------------------------------------------------------------- #
async def test_guardar_una_credencial_repinta_claves_y_avisa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guardar en Credenciales repinta lo que enseña estados y lo reenvía fuera.

    La credencial que se acaba de guardar puede ser la clave que un nodo está
    esperando: su columna tiene que cambiar sin salir de la pestaña. Y quien
    firma —otras pantallas— escucha en `ConfiguracionPage`, no tiene por qué
    saber que las credenciales viven en una subpestaña.
    """
    async with _pagina() as (_, pagina):
        llamadas: list[str] = []
        monkeypatch.setattr(pagina._apis, "refresh", lambda: llamadas.append("apis"))
        monkeypatch.setattr(pagina._nodes, "refresh_keys", lambda: llamadas.append("nodos"))
        avisos: list[int] = []
        pagina.credentials_changed.connect(lambda: avisos.append(1))

        pagina._credentials.credentials_changed.emit()

        assert llamadas == ["apis", "nodos"]
        assert avisos == [1]
