"""La ventana de un nodo RPC: probar antes de guardar, y no borrar lo que no se toca.

Antes de esta ventana el alta era un formulario fijo al final de la tarjeta, y
editar un nodo no existía: corregir una URL era eliminar el nodo y volver a
escribirlo. Aquí se comprueba lo que la ventana promete —que un nodo nuevo sólo
se guarda si la prueba (`eth_chainId`) respondió, que al editar no se exige
probar lo que no cambió, que la clave va al llavero y nunca al fichero, y que un
campo de clave en blanco **no** borra la que ya estaba—.

La prueba de red se sustituye por un doble: esta suite no sale a internet. La
escritura del fichero sí es la de verdad, sobre un directorio temporal.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from amigocompora.app.container import Container, build_container
from amigocompora.domain.chains import CHAINS
from amigocompora.infra.config import RpcEndpointSettings, Settings
from amigocompora.infra.rpc.probe import ProbeResult
from amigocompora.infra.secrets import InMemorySecretStore, app_secret_key
from amigocompora.ui import node_dialog as node_dialog_module
from amigocompora.ui.node_dialog import NodeDialog

_URL_CON_CLAVE = "https://mainnet.infura.io/v3/${INFURA_API_KEY}"
_CLAVE_NOMBRE = "INFURA_API_KEY"


class ProbeFalsa:
    """La prueba de red, doblada: contesta lo que el test le diga, y anota qué se le pidió."""

    def __init__(self, *, ok: bool = True, detail: str = "chainId 1 (0x1)") -> None:
        self.ok = ok
        self.detail = detail
        self.llamadas: list[tuple[str, int | None]] = []

    async def __call__(self, url: str, expected_chain_id: int | None) -> ProbeResult:
        self.llamadas.append((url, expected_chain_id))
        return ProbeResult(ok=self.ok, detail=self.detail, latency_ms=5)


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ProbeFalsa:
    doble = ProbeFalsa()
    monkeypatch.setattr(node_dialog_module, "probe_node", doble)
    return doble


@pytest.fixture
def pendientes(monkeypatch: pytest.MonkeyPatch) -> list[Coroutine[Any, Any, None]]:
    """Las corrutinas que la ventana lanzó con `spawn`, retenidas sin ejecutar.

    La ventana lanza la prueba con `spawn` —como toda la UI—, que crea una tarea
    en el bucle; aquí se guarda la corrutina para poder esperarla de forma
    determinista y comprobar el resultado sin dormir a ciegas.
    """
    lanzadas: list[Coroutine[Any, Any, None]] = []
    monkeypatch.setattr(node_dialog_module, "spawn", lanzadas.append)
    return lanzadas


@pytest.fixture
def destino(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """El `config.toml` de la ventana: uno de verdad, en un directorio temporal."""
    path = tmp_path / "config.toml"
    monkeypatch.setattr(node_dialog_module, "config_file", lambda: path)
    return path


@asynccontextmanager
async def _montar(*, store: object | None = None) -> AsyncIterator[Container]:
    container = await build_container(
        Settings.model_validate({"execution": {"allow_env_key": False}}),
        secret_store=store if store is not None else InMemorySecretStore(),  # type: ignore[arg-type]
        configure_logs=False,
    )
    try:
        yield container
    finally:
        await container.aclose()


def _endpoint_infura() -> RpcEndpointSettings:
    return RpcEndpointSettings(url=_URL_CON_CLAVE, label="infura", priority=10)


def _ventana(
    container: Container,
    *,
    endpoints: list[RpcEndpointSettings] | None = None,
    index: int | None = None,
) -> NodeDialog:
    return NodeDialog(
        container, None, chain_key="ethereum", endpoints=endpoints or [], index=index
    )


async def _probar(dialog: NodeDialog, pendientes: list[Coroutine[Any, Any, None]]) -> None:
    dialog._on_probe()
    assert pendientes, "la prueba no se llegó a lanzar"
    await pendientes.pop()


# `read_text` y `exists` dentro de una corrutina bloquean el bucle, y el linter lo
# señala (ASYNC240) —con razón en la aplicación, donde el fichero puede estar en
# un disco lento—. El `config.toml` de una prueba son unos renglones en un
# directorio temporal y comprobarlo no tiene nada de asíncrono, así que se hace
# desde funciones síncronas y el aviso no se silencia.
def _leer(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _existe(path: Path) -> bool:
    return path.exists()


# --------------------------------------------------------------------------- #
# Un nodo nuevo
# --------------------------------------------------------------------------- #
async def test_un_nodo_nuevo_solo_se_guarda_tras_la_prueba(
    probe: ProbeFalsa, pendientes: list[Coroutine[Any, Any, None]], destino: Path
) -> None:
    async with _montar() as container:
        ventana = _ventana(container)
        ventana._url.setText("https://rpc.prueba.example/eth")

        assert not ventana._save_btn.isEnabled(), "sin probar no hay nada que guardar"
        await _probar(ventana, pendientes)

        assert ventana._save_btn.isEnabled()
        assert probe.llamadas == [
            ("https://rpc.prueba.example/eth", CHAINS["ethereum"].eip155_id)
        ], "se prueba la URL escrita contra la red elegida"
        ventana._on_save()

        assert ventana.result() == QDialog.DialogCode.Accepted
        assert "https://rpc.prueba.example/eth" in _leer(destino)
        assert ventana.endpoints is not None
        assert ventana.endpoints[-1].url == "https://rpc.prueba.example/eth"


async def test_una_prueba_fallida_no_habilita_guardar(
    probe: ProbeFalsa, pendientes: list[Coroutine[Any, Any, None]], destino: Path
) -> None:
    probe.ok = False
    probe.detail = "no responde"
    async with _montar() as container:
        ventana = _ventana(container)
        ventana._url.setText("https://rpc.caido.example/eth")
        await _probar(ventana, pendientes)

        assert not ventana._save_btn.isEnabled()
        assert "✗" in ventana._probe_label.text()
        ventana._on_save()
        assert ventana.endpoints is None, "no se guardó nada"
        assert not _existe(destino), "ni se tocó el fichero"


async def test_una_url_repetida_se_rechaza(
    probe: ProbeFalsa, pendientes: list[Coroutine[Any, Any, None]], destino: Path
) -> None:
    """El mismo nodo dos veces en la misma red no aporta y duplica la espera."""
    async with _montar() as container:
        ventana = _ventana(container, endpoints=[_endpoint_infura()])
        ventana._url.setText(_URL_CON_CLAVE)
        ventana._key_name.setText(_CLAVE_NOMBRE)
        ventana._key_value.setText("clave-x")
        await _probar(ventana, pendientes)
        ventana._on_save()

        assert ventana.endpoints is None
        assert ventana.result() != QDialog.DialogCode.Accepted
        assert "Ya hay un nodo con esa URL" in ventana._probe_label.text()
        assert not _existe(destino)


# --------------------------------------------------------------------------- #
# Editar
# --------------------------------------------------------------------------- #
async def test_editar_la_etiqueta_no_exige_volver_a_probar(
    probe: ProbeFalsa, pendientes: list[Coroutine[Any, Any, None]], destino: Path
) -> None:
    """La URL ya se probó el día que se guardó; exigirlo otra vez no comprueba nada."""
    async with _montar() as container:
        ventana = _ventana(container, endpoints=[_endpoint_infura()], index=0)
        assert ventana._save_btn.isEnabled(), "nada nuevo que probar"

        ventana._label.setText("casa")
        ventana._on_save()

        assert ventana.result() == QDialog.DialogCode.Accepted
        assert not probe.llamadas, "no se prueba lo que no cambió"
        assert not pendientes
        assert ventana.endpoints is not None
        assert ventana.endpoints[0].label == "casa"
        assert _URL_CON_CLAVE in _leer(destino)


async def test_cambiar_la_url_obliga_a_probar_de_nuevo(
    probe: ProbeFalsa, pendientes: list[Coroutine[Any, Any, None]]
) -> None:
    async with _montar() as container:
        ventana = _ventana(container, endpoints=[_endpoint_infura()], index=0)
        ventana._url.setText("https://rpc.otro.example/eth")

        assert not ventana._save_btn.isEnabled(), "una URL nueva no está probada"
        await _probar(ventana, pendientes)

        assert ventana._save_btn.isEnabled()
        assert probe.llamadas[0][0] == "https://rpc.otro.example/eth"


async def test_una_clave_nueva_obliga_a_probar_y_acaba_en_el_llavero(
    probe: ProbeFalsa, pendientes: list[Coroutine[Any, Any, None]], destino: Path
) -> None:
    """La clave viaja dentro de la URL al probar, y al fichero sólo va el marcador."""
    async with _montar() as container:
        ventana = _ventana(container, endpoints=[_endpoint_infura()], index=0)
        ventana._key_name.setText(_CLAVE_NOMBRE)
        ventana._key_value.setText("clave-nueva")

        assert not ventana._save_btn.isEnabled(), "una clave nueva tampoco está probada"
        await _probar(ventana, pendientes)

        assert probe.llamadas[0][0] == "https://mainnet.infura.io/v3/clave-nueva", (
            "la prueba usa la clave de verdad, no el marcador"
        )
        ventana._on_save()

        assert container.secrets.get(app_secret_key(_CLAVE_NOMBRE)) == "clave-nueva"
        assert ventana.key_saved
        escrito = _leer(destino)
        assert "${INFURA_API_KEY}" in escrito, "el fichero guarda el marcador"
        assert "clave-nueva" not in escrito, "y nunca la clave"


async def test_editar_sin_escribir_la_clave_no_borra_la_guardada() -> None:
    """Dejar el campo en blanco es «no tocarla»; para reemplazarla se escribe la nueva."""
    store = InMemorySecretStore()
    store.set(app_secret_key(_CLAVE_NOMBRE), "clave-vieja")
    async with _montar(store=store) as container:
        ventana = _ventana(container, endpoints=[_endpoint_infura()], index=0)
        ventana._label.setText("casa")
        ventana._on_save()

        assert store.get(app_secret_key(_CLAVE_NOMBRE)) == "clave-vieja"
        assert not ventana.key_saved


async def test_editar_no_deja_cambiar_la_red() -> None:
    """Cambiar la red de un nodo guardado sería borrarlo y crearlo en otra."""
    async with _montar() as container:
        assert _ventana(container)._chain.isEnabled()
        assert not _ventana(container, endpoints=[_endpoint_infura()], index=0)._chain.isEnabled()


# --------------------------------------------------------------------------- #
# Lo que se rechaza antes de probar
# --------------------------------------------------------------------------- #
async def test_una_clave_que_no_esta_en_la_url_se_dice_antes_de_probar(
    pendientes: list[Coroutine[Any, Any, None]],
) -> None:
    """Si la URL no lleva `${NOMBRE}`, la clave nunca llegaría al nodo."""
    async with _montar() as container:
        ventana = _ventana(container)
        ventana._url.setText("https://rpc.prueba.example/eth")
        ventana._key_name.setText(_CLAVE_NOMBRE)
        ventana._key_value.setText("clave-x")
        ventana._on_probe()

        assert not pendientes, "no se prueba una URL que no llevaría la clave"
        assert "${INFURA_API_KEY}" in ventana._probe_label.text()


async def test_el_nombre_de_la_clave_se_rellena_desde_la_url() -> None:
    """Escribir `${NOMBRE}` en la URL ya declara qué credencial hace falta."""
    async with _montar() as container:
        ventana = _ventana(container)
        ventana._url.setText(_URL_CON_CLAVE)
        assert ventana._key_name.text() == _CLAVE_NOMBRE

        # Y no pisa lo que ya se estaba escribiendo: adivinar encima de alguien
        # que teclea es la forma rápida de guardar un nombre que no era el suyo.
        ventana._key_name.setText("OTRA_CLAVE")
        ventana._url.setText(_URL_CON_CLAVE + "?v=2")
        assert ventana._key_name.text() == "OTRA_CLAVE"
