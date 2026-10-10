"""La pestaña de motores: un interruptor por fila, y lo que ese gesto deja dicho.

Esta prueba monta la página **entera** —contenedor de verdad, registro de verdad,
widgets de Qt de verdad— por la misma razón que las demás pruebas de integración
de la carpeta: lo que se mide no vive en la vista sino en lo que la vista deja
detrás. Un clic en el interruptor tiene que abrir o retirar un motor en el
registro **y** dejar la ranura escrita en `config.toml`, y eso son dos capas que
una prueba doblada no puede medir a la vez.

Las tres reglas que se fijan aquí, cada una con su porqué:

- Encender **suma**, no sustituye: los motores de una ranura se comparan entre
  sí, y apagar los demás al encender uno sería lo contrario de para lo que están.
- Apagar el último deja la ranura declarada **vacía** (`[]`), no ausente: sin la
  clave volvería el motor por omisión al reiniciar; con la clave en vacío no
  vuelve nadie.
- Un motor que no arranca deja su interruptor apagado —el registro es la única
  verdad y la tabla se repinta desde él— sin arrastrar a los que ya estaban.

El `config.toml` se escribe de verdad, contra un archivo de `tmp_path`: lo que se
comprueba es que el gesto **persiste**, y doblar el escritor mediría la llamada,
no el resultado. Ninguna prueba de este fichero firma, emite ni sale a la red.

Qt necesita un `QApplication` vivo, y en una máquina sin pantalla —CI, un
servidor— eso se resuelve con la plataforma `offscreen`, que se fija aquí antes
de que se importe nada de Qt.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import tomlkit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from amigocompora.app.container import Container, build_container
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import InMemorySecretStore
from amigocompora.ui.pages import engines as engines_module
from amigocompora.ui.pages.engines import EnginesPage
from amigocompora.ui.widgets import Switch

#: Los dos motores que pueblan la ranura en estas pruebas. Son de la casa y
#: ninguno pide clave para abrirse: encenderlos aquí no toca la red.
V3 = "uniswap_v3"
V2 = "uniswap_v2"

#: El motor del doble que no arranca, y su nombre —el que sale en el aviso.
ROTO = "roto"
NOMBRE_ROTO = "Motor roto"

#: El archivo que la pestaña encontrará al guardar. Con un comentario y **sin**
#: tabla `[active_engines]`: la escritura tiene que crear la tabla sin llevarse
#: por delante lo que no era suyo, y eso se mide.
_CONFIG = """\
# Comentario que debe sobrevivir.
mode = "observation"
"""


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@asynccontextmanager
async def _pagina(
    motores: str | Sequence[str] = V3,
    nombres: Mapping[str, str] | None = None,
) -> AsyncIterator[tuple[Container, EnginesPage]]:
    """La pestaña montada sobre un contenedor de verdad, con esa ranura encendida.

    La ranura se declara explícita —y no se hereda del valor por omisión— porque
    lo que se mide en la mitad de las pruebas de aquí es justo la diferencia
    entre «encendido por configuración» y «encendido desde la pantalla».
    """
    ajustes: dict[str, object] = {
        "mode": "observation",
        "active_engines": {"dex_quotes": motores},
    }
    if nombres is not None:
        ajustes["engine_names"] = dict(nombres)
    container = await build_container(
        Settings.model_validate(ajustes),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        yield container, EnginesPage(container)
    finally:
        await container.aclose()


# --------------------------------------------------------------------------- #
# Los localizadores: la fila y los widgets de dentro, por id y no por posición
# --------------------------------------------------------------------------- #
def _fila(page: EnginesPage, engine_id: str) -> int:
    """La fila de ese motor, por id.

    Por id y no por posición porque las posiciones dependen de los motores
    instalados en la máquina que corre la prueba: una prueba que eligiera «la
    primera» estaría afirmando sobre el catálogo en vez de sobre lo que se mide.
    """
    for fila in range(page._table.rowCount()):
        item = page._table.item(fila, engines_module._COL_ID)
        if item is not None and item.text() == engine_id:
            return fila
    raise AssertionError(f"el motor «{engine_id}» no está en la tabla")


def _celda(page: EnginesPage, engine_id: str, columna: int) -> QWidget:
    celda = page._table.cellWidget(_fila(page, engine_id), columna)
    assert celda is not None
    return celda


def _interruptor(page: EnginesPage, engine_id: str) -> Switch:
    """El interruptor de ese motor, buscado en su celda.

    Se busca el widget de verdad en vez de guardarlo en la página: lo que hay
    que pulsar es el que se ve, y una referencia interna podría quedarse
    apuntando al de una fila que ya no existe tras un repintado.
    """
    boton = _celda(page, engine_id, engines_module._COL_ACTIVE).findChild(Switch)
    assert boton is not None
    return boton


def _nombre_mostrado(page: EnginesPage, engine_id: str) -> str:
    etiqueta = _celda(page, engine_id, engines_module._COL_NAME).findChild(QLabel)
    assert etiqueta is not None
    return etiqueta.text()


def _lapiz(page: EnginesPage, engine_id: str) -> QPushButton:
    boton = _celda(page, engine_id, engines_module._COL_NAME).findChild(QPushButton)
    assert boton is not None
    return boton


def _falso_nombre(
    monkeypatch: pytest.MonkeyPatch, texto: str, *, aceptar: bool = True
) -> list[tuple[str, str, str]]:
    """Sustituye el diálogo del alias por una respuesta fija, y anota con qué se abrió.

    Es un doble y no un modal de verdad por la misma razón que la propia función
    de la página es sustituible: un `QInputDialog` bloquearía la prueba que lo
    mide. Lo que interesa no es el diálogo, sino lo que viene después
    —normalizar, guardar, decidir qué se cuenta—, y eso se mide entero.
    """
    abiertos: list[tuple[str, str, str]] = []

    def _responder(
        parent: QWidget, *, original: str, engine_id: str, current: str
    ) -> tuple[str, bool]:
        abiertos.append((original, engine_id, current))
        return (texto, aceptar)

    monkeypatch.setattr(engines_module, "ask_engine_name", _responder)
    return abiertos


def _leer(archivo: Path) -> dict[str, Any]:
    """El `config.toml` escrito, como diccionario plano: lo que la carga leería."""
    return tomlkit.parse(archivo.read_text(encoding="utf-8")).unwrap()


# --------------------------------------------------------------------------- #
# Lo que la tabla enseña
# --------------------------------------------------------------------------- #
async def test_cada_motor_tiene_su_interruptor_y_dice_el_estado_de_la_ranura() -> None:
    """El interruptor dice lo que dice el registro, no lo que decía el archivo.

    Y va uno por fila, también en los motores apagados: un panel que sólo
    enseñara los encendidos no tendría dónde encender los demás.
    """
    async with _pagina() as (container, page):
        assert page._table.rowCount() == len(container.registry.available())
        assert _interruptor(page, V3).isChecked() is True
        assert _interruptor(page, V2).isChecked() is False
        assert container.registry.is_active(V2) is False
        # El reparto permanente: quién está encendido en cada ranura.
        assert f"Cotizaciones DEX: {V3}" in page._roster.text()


# --------------------------------------------------------------------------- #
# Encender y apagar
# --------------------------------------------------------------------------- #
async def test_encender_suma_a_la_ranura_y_lo_deja_escrito(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El clic abre el motor, lo suma, y la ranura queda escrita tal como quedó.

    Las tres cosas a la vez porque son la misma promesa: lo que se enciende se
    consulta **y** arranca así la próxima vez. Si sólo cambiara la tabla, el
    encendido moriría al cerrar la aplicación; si sólo cambiara el archivo, el
    interruptor estaría mintiendo hasta el próximo reinicio.
    """
    archivo = tmp_path / "config.toml"
    archivo.write_text(_CONFIG, encoding="utf-8")

    async with _pagina() as (container, page):
        monkeypatch.setattr(engines_module, "config_file", lambda: archivo)

        _interruptor(page, V2).click()
        await asyncio.sleep(0.05)

        ids = [
            engine.manifest.engine_id
            for engine in container.registry.active_stack(EngineKind.DEX_QUOTES)
        ]
        assert ids == [V3, V2], "se suma sin apagar al que ya estaba"
        assert _interruptor(page, V2).isChecked() is True
        assert "2 motores" in page._status.text()
        assert "uniswap_v3, uniswap_v2" in page._status.text()
        assert _leer(archivo)["active_engines"]["dex_quotes"] == [V3, V2]
        assert "# Comentario que debe sobrevivir." in archivo.read_text(encoding="utf-8")
        assert page._table.isEnabled() is True, "la tabla vuelve a aceptar gestos"


async def test_apagar_el_ultimo_deja_la_ranura_vacia_y_escrita(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[]` es «apagada a propósito», y la carga tiene que leer lo mismo.

    Es la diferencia que hace que apagar sobreviva al reinicio, así que la prueba
    no comprueba que el motor salió —eso es del registro— sino que el archivo lo
    dice **de la forma que la carga entiende**: con la clave puesta y la lista
    vacía. Borrar la clave en vez de vaciarla haría volver el motor por omisión.
    """
    archivo = tmp_path / "config.toml"
    archivo.write_text(_CONFIG, encoding="utf-8")

    async with _pagina() as (container, page):
        monkeypatch.setattr(engines_module, "config_file", lambda: archivo)

        _interruptor(page, V3).click()
        await asyncio.sleep(0.05)

        assert container.registry.active_stack(EngineKind.DEX_QUOTES) == ()
        assert _interruptor(page, V3).isChecked() is False
        assert "sin motores" in page._status.text()
        documento = _leer(archivo)
        assert documento["active_engines"]["dex_quotes"] == []
        ajustes = Settings.model_validate(documento)
        assert "dex_quotes" in ajustes.active_engines
        assert ajustes.active_engines["dex_quotes"] == ()


async def test_un_motor_que_no_arranca_deja_su_interruptor_apagado() -> None:
    """El registro es la única verdad: el repintado deja el interruptor como estaba.

    El caso real: un motor instalado que exige una clave que falta. Pulsar su
    interruptor no puede llevarse por delante a los que ya estaban, y el fallo
    tiene que decirse con su motivo: si el interruptor volviera encendido, quien
    lo mirara creería que ese motor se está consultando.
    """
    async with _pagina() as (container, _):
        # La página se monta **después** de registrar el roto: así la fila existe
        # desde el primer repintado, que es el orden real de un paquete instalado.
        container.registry.register(_Provider(engine=_Roto()))
        page = EnginesPage(container)

        _interruptor(page, ROTO).click()
        await asyncio.sleep(0.05)

        assert container.registry.is_active(ROTO) is False
        assert _interruptor(page, ROTO).isChecked() is False
        assert page._status.text().startswith(f"No se pudo encender «{NOMBRE_ROTO}»")
        assert "no pudo arrancar" in page._status.text()
        assert _interruptor(page, V3).isChecked() is True, "los que ya estaban siguen"


# --------------------------------------------------------------------------- #
# El alias de pantalla
# --------------------------------------------------------------------------- #
async def test_el_lapiz_renombra_y_lo_deja_escrito(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El alias llega a la tabla, a la copia viva y al archivo — los tres a la vez.

    El alias es de pantalla y nada más, así que lo que hay que medir es que todo
    lo que enseña un nombre pase por el mismo resolutor: si sólo cambiara la
    tabla, el mismo motor tendría dos nombres según dónde se mire. El diálogo
    llega con el alias vigente —aquí vacío— para poder borrarlo, no sólo
    cambiarlo por otro.
    """
    archivo = tmp_path / "config.toml"
    archivo.write_text(_CONFIG, encoding="utf-8")

    async with _pagina() as (container, page):
        original = container.registry.lookup(V3).manifest.name
        abiertos = _falso_nombre(monkeypatch, "El bueno")
        monkeypatch.setattr(engines_module, "config_file", lambda: archivo)

        _lapiz(page, V3).click()

        assert abiertos == [(original, V3, "")]
        assert container.engine_names.resolve(V3, original) == "El bueno"
        assert _nombre_mostrado(page, V3) == "El bueno"
        assert "ahora se muestra como «El bueno»" in page._status.text()
        assert _leer(archivo)["engine_names"] == {V3: "El bueno"}
        assert "# Comentario que debe sobrevivir." in archivo.read_text(encoding="utf-8")


async def test_borrar_el_alias_devuelve_el_nombre_de_fabrica(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vaciar el diálogo quita el alias, y con él la tabla `[engine_names]`.

    Sin esta salida un alias sólo se podría cambiar por otro, y dejar la tabla
    vacía en el archivo sería escribir una clave que ya no dice nada.
    """
    archivo = tmp_path / "config.toml"
    archivo.write_text(
        _CONFIG + f'\n[engine_names]\n{V3} = "El bueno"\n', encoding="utf-8"
    )

    async with _pagina(nombres={V3: "El bueno"}) as (container, page):
        original = container.registry.lookup(V3).manifest.name
        assert _nombre_mostrado(page, V3) == "El bueno", "el alias de la configuración manda"

        abiertos = _falso_nombre(monkeypatch, "")
        monkeypatch.setattr(engines_module, "config_file", lambda: archivo)

        _lapiz(page, V3).click()

        assert abiertos == [(original, V3, "El bueno")], "llega el alias vigente"
        assert _nombre_mostrado(page, V3) == original
        assert "recupera su nombre original" in page._status.text()
        texto = archivo.read_text(encoding="utf-8")
        assert "engine_names" not in texto
        assert "# Comentario que debe sobrevivir." in texto


async def test_cancelar_el_renombrado_no_toca_nada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelar es la salida honesta del diálogo: no puede quedar ni rastro.

    Ni la copia viva, ni el archivo, ni una línea de estado prometiendo algo que
    no pasó. Es el contrapunto de la prueba anterior, y el que evita que un
    «aceptar» mal cableado convierta cancelar en guardar un vacío.
    """
    archivo = tmp_path / "config.toml"
    archivo.write_text(_CONFIG, encoding="utf-8")

    async with _pagina() as (container, page):
        original = container.registry.lookup(V3).manifest.name
        _falso_nombre(monkeypatch, "", aceptar=False)

        _lapiz(page, V3).click()

        assert container.engine_names.as_dict() == {}
        assert _nombre_mostrado(page, V3) == original
        assert archivo.read_text(encoding="utf-8") == _CONFIG
        assert not archivo.with_name(archivo.name + ".bak").exists(), "ni copia de seguridad"


# --------------------------------------------------------------------------- #
# El doble que no arranca
# --------------------------------------------------------------------------- #
class _Roto:
    """Un motor que falla **al abrirse**: el paquete de terceros mal configurado.

    Su `aopen` lanza a propósito, que es donde falla el de verdad —una clave que
    falta, un nodo que no responde—: construir el motor no debe hacer E/S, así
    que un doble que fallara en el constructor mediría otro caso.
    """

    __slots__ = ("_manifest",)

    def __init__(self) -> None:
        self._manifest = EngineManifest(
            engine_id=ROTO,
            name=NOMBRE_ROTO,
            version="0.0.1",
            kind=EngineKind.DEX_QUOTES,
            summary="Doble de prueba: no arranca.",
        )

    @property
    def manifest(self) -> EngineManifest:
        return self._manifest

    async def aopen(self) -> None:
        raise RuntimeError("sin conexión")

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Provider:
    """La fábrica del doble, como la publicaría un entry point de verdad."""

    engine: _Roto

    @property
    def manifest(self) -> EngineManifest:
        return self.engine.manifest

    def create(self, config: Mapping[str, str]) -> _Roto:
        return self.engine
