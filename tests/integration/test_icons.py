"""Los iconos de marca: dónde se buscan, qué se traduce y qué pasa si no están.

Lo que se comprueba aquí es la propiedad que sostiene todo lo demás: **sin
fichero no se dibuja nada** y el listado se queda exactamente como estaba. Un
icono de relleno en una fila de dinero es una marca que el usuario no eligió,
así que la ausencia es un estado probado y no un accidente.

Qt necesita un `QApplication` vivo incluso para construir un `QIcon`, y en una
máquina sin pantalla —CI, un servidor— eso se resuelve con la plataforma
`offscreen`, que se fija aquí antes de que se importe nada de Qt.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from amigocompora.ui import icons

#: Un SVG mínimo pero válido. Lo que se comprueba es que el fichero se encuentra
#: y se carga —el cargador de SVG tiene que estar entre los plugins de Qt—, no
#: la forma del dibujo.
_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    b'<circle cx="12" cy="12" r="10" fill="#4c8dff"/></svg>'
)


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@pytest.fixture
def raiz(tmp_path: Path) -> Iterator[Path]:
    """Una carpeta de iconos vacía, y la búsqueda normal al terminar.

    El estado de `icons` es global —la raíz forzada y la caché—, así que una
    prueba que no lo devuelva deja a las siguientes mirando su carpeta temporal.
    """
    icons.use_asset_root(tmp_path)
    yield tmp_path
    icons.use_asset_root(None)


def _escribir(raiz: Path, tipo: str, slug: str) -> None:
    """Deja un icono en la carpeta, como lo dejaría el script de descarga."""
    carpeta = raiz / tipo
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / f"{slug}.svg").write_bytes(_SVG)


def test_sin_fichero_no_hay_icono(raiz: Path) -> None:
    assert icons.icon_path(icons.NETWORKS, "base") is None
    assert icons.network_icon("base").isNull()


def test_el_fichero_se_encuentra_por_su_slug(raiz: Path) -> None:
    _escribir(raiz, icons.NETWORKS, "base")
    assert icons.icon_path(icons.NETWORKS, "base") is not None
    assert not icons.network_icon("base").isNull()


def test_la_clave_se_traduce_antes_de_buscar(raiz: Path) -> None:
    """La traducción y la búsqueda, juntas: es la costura que puede romperse.

    Una tabla de slugs que traduzca a un nombre que no es el del fichero deja el
    icono en disco y sin usar, y el síntoma es indistinguible de «no se descargó».
    """
    _escribir(raiz, icons.NETWORKS, "binance-smart-chain")
    _escribir(raiz, icons.TOKENS, "USDC")
    assert not icons.network_icon("bsc").isNull()
    assert not icons.token_icon("USDC.e").isNull()


def test_la_carpeta_de_casa_tambien_vale(raiz: Path) -> None:
    """`chains/` es la carpeta del proyecto, no la de web3icons.

    Los iconos de red que hay hoy están ahí y con sus nombres de descarga
    (`binance-smart-chain.svg`), así que el resolvedor tiene que mirar las dos
    carpetas o los ficheros estarían en disco sin salir nunca en pantalla.
    """
    _escribir(raiz, "chains", "binance-smart-chain")
    assert not icons.network_icon("bsc").isNull()


def test_la_carpeta_del_proyecto_gana_a_la_descargada(raiz: Path) -> None:
    """Con las dos en disco manda la del proyecto: la eligió una persona.

    La descarga masiva no puede tapar un fichero puesto a mano — y el script,
    que pregunta antes de bajar, tampoco lo deja compitiendo en disco.
    """
    _escribir(raiz, "networks", "base")
    _escribir(raiz, "chains", "base")
    ruta = icons.icon_path(icons.NETWORKS, "base")
    assert ruta is not None
    assert ruta.parent.name == "chains"


def test_el_nombre_crudo_vale_cuando_el_slug_no_esta(raiz: Path) -> None:
    """`arbitrum.svg` a secas: web3icons publica `arbitrum-one`, la carpeta no."""
    _escribir(raiz, icons.NETWORKS, "arbitrum")
    assert not icons.network_icon("arbitrum").isNull()


def test_el_sinonimo_de_casa_llega_al_fichero(raiz: Path) -> None:
    """La red se llama `avalanche` y su logo se descargó como `avax.svg`."""
    _escribir(raiz, "chains", "avax")
    assert not icons.network_icon("avalanche").isNull()


def test_el_slug_del_catalogo_gana_al_nombre_crudo(raiz: Path) -> None:
    """Con los dos ficheros en disco se elige el del catálogo, que es el estable."""
    _escribir(raiz, icons.NETWORKS, "arbitrum-one")
    _escribir(raiz, icons.NETWORKS, "arbitrum")
    ruta = icons.icon_path(icons.NETWORKS, "arbitrum")
    assert ruta is not None
    assert ruta.name == "arbitrum-one.svg"


def test_un_token_sin_fichero_no_inventa_logo(raiz: Path) -> None:
    """Sin fichero ni genérico no hay nada: la fila se queda con su texto."""
    _escribir(raiz, icons.TOKENS, "USDC")
    assert icons.token_icon("ORCA").isNull()


def test_un_token_sin_logo_lleva_el_generico(raiz: Path) -> None:
    """`token.svg` es el que dice «esto es un token» sin fingir ser una marca.

    Y es sólo de los tokens: para una red no hay genérico, porque un dibujo de
    cadena no diría nada que el nombre no diga ya.
    """
    _escribir(raiz, icons.TOKENS, "token")
    assert not icons.token_icon("ARB").isNull()
    assert icons.network_icon("base").isNull()


def test_un_motor_sin_logo_lleva_su_generico(raiz: Path) -> None:
    """`plataform.svg`, con la ortografía de la carpeta, es el genérico de marcas."""
    _escribir(raiz, "plataform", "plataform")
    _escribir(raiz, "plataform", "lifi")
    assert not icons.brand_icon("relay").isNull()
    ruta = icons.icon_path(icons.BRANDS, "lifi")
    assert ruta is not None
    assert ruta.stem == "lifi"


def test_el_simbolo_exacto_gana_al_alias(raiz: Path) -> None:
    """Con `WETH.svg` y `ETH.svg` en disco, `WETH` enseña el suyo.

    El alias al logo de la moneda base es un último recurso, no la primera
    respuesta: quien guarda los dos ficheros quiere ver la diferencia.
    """
    _escribir(raiz, "coin", "WETH")
    _escribir(raiz, "coin", "ETH")
    ruta = icons.icon_path(icons.TOKENS, "WETH")
    assert ruta is not None
    assert ruta.stem == "WETH"
    ruta_eth = icons.icon_path(icons.TOKENS, "ETH")
    assert ruta_eth is not None
    assert ruta_eth.stem == "ETH"


def test_el_simbolo_tambien_se_busca_sin_puntos(raiz: Path) -> None:
    """`USDC.e` encuentra `usdce.svg`, y antes que el logo de USDC a secas."""
    _escribir(raiz, "coin", "usdce")
    _escribir(raiz, "coin", "USDC")
    ruta = icons.icon_path(icons.TOKENS, "USDC.e")
    assert ruta is not None
    assert ruta.stem == "usdce"


def test_un_png_con_nombre_svg_tambien_carga(raiz: Path) -> None:
    """Manda el contenido, no la extensión.

    Qt elige el cargador por la extensión y hay kits de iconos que publican mapa
    de bits con nombre `.svg`. Sin el segundo intento, el fichero estaría en
    disco, el resolvedor lo daría por bueno y en pantalla no habría nada: el
    peor estado posible, porque no se distingue de «no hay icono».
    """
    carpeta = raiz / icons.TOKENS
    carpeta.mkdir(parents=True, exist_ok=True)
    mapa = QPixmap(8, 8)
    mapa.fill(QColor("#4c8dff"))
    assert mapa.save(str(carpeta / "PNG.svg"), "PNG")
    assert not icons.token_icon("PNG").isNull()


def test_un_fichero_roto_deja_el_listado_como_sin_icono(raiz: Path) -> None:
    """Un SVG truncado no puede reventar una tabla de saldos: se queda vacío."""
    (raiz / icons.TOKENS).mkdir(parents=True, exist_ok=True)
    (raiz / icons.TOKENS / "ROTO.svg").write_bytes(b"<svg xmlns=")
    assert icons.token_icon("ROTO").isNull()


def test_available_lista_lo_que_hay(raiz: Path) -> None:
    _escribir(raiz, icons.NETWORKS, "base")
    _escribir(raiz, icons.TOKENS, "USDC")
    assert icons.available() == {
        icons.NETWORKS: ("base",),
        icons.TOKENS: ("USDC",),
        icons.BRANDS: (),
    }


def test_volver_a_fijar_la_raiz_olvida_el_hueco(raiz: Path) -> None:
    """El hueco también se cachea, y eso está bien; lo que no puede es quedarse.

    Un fichero que no estaba al abrir el listado tampoco va a estar en el
    repintado siguiente, y buscarlo por fila serían varios `stat` por fila. Pero
    volver a fijar la carpeta —lo que hacen las pruebas y el propio desarrollo al
    descargar iconos con la app abierta— tiene que vaciar esa memoria, o el
    primer «no está» se quedaría para siempre.
    """
    assert icons.network_icon("base").isNull()
    _escribir(raiz, icons.NETWORKS, "base")
    assert icons.network_icon("base").isNull()
    icons.use_asset_root(raiz)
    assert not icons.network_icon("base").isNull()
