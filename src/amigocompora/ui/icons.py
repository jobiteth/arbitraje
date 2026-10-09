"""Iconos de marca para redes, tokens y motores.

En los listados —desplegables de token, tablas de saldos, rejilla de rutas— el
nombre va acompañado de su icono. El nombre **no se sustituye** por el icono: un
logo sin texto obliga a reconocer una marca de memoria, y la mitad de estos
símbolos se parecen entre sí. El icono adelanta; el texto decide.

### Qué hace este módulo y qué no

Resuelve «qué fichero le toca a este nombre» y lo cachea. No dibuja, no compone,
no descarga: los SVG están en disco antes de arrancar la aplicación. Si el
fichero no está, devuelve un `QIcon` vacío y el listado se queda con su texto, que
es exactamente lo que hay hoy. **Ningún logo de relleno**: un logo ajeno en una
fila de dinero es una marca que el usuario no eligió. La excepción son los
genéricos por tipo —`token.svg`, `plataform.svg`—: no son marcas de nadie, no
dicen «esto es USDC» sino «esto es un token», y sólo aparecen cuando el icono
propio falta.

### Dónde viven los ficheros

Dos sitios, en este orden:

1. `src/amigocompora/ui/assets/` — dentro del paquete, que es lo que viaja en el
   wheel y en el `.exe`.
2. `assets/` en la raíz del repositorio — el buzón de desarrollo, donde se dejan
   los SVG recién descargados.

Dentro de cada raíz, una carpeta por tipo y un fichero por nombre. Cada tipo
admite dos nombres de carpeta: el que mantiene el proyecto a mano —`chains`,
`coin`, `plataform`, con los SVG elegidos uno a uno— y el de web3icons
—`networks`, `tokens`, `brands`, lo que escribe el script de descarga—. Se mira
primero la del proyecto, así que una descarga masiva nunca tapa un fichero puesto
a mano; y el script, para no dejar en disco nada que no se vea, ni siquiera baja
lo que ya se resuelve. Y para el fichero, el nombre tampoco tiene que ser exacto:
se prueban el slug de web3icons, nuestra propia clave y unos pocos sinónimos de
casa (`avalanche` → `avax.svg`) — ver `_candidatos`. Mayúsculas tampoco importan
en el nombre del fichero: en Windows —el objetivo— `coin/eth.svg` sirve al
símbolo `ETH`.

Los nombres de los slugs son los de web3icons —minúsculas con guiones para redes
y marcas, ticker en mayúsculas para tokens— porque es de ahí de donde salen los
ficheros y renombrarlos al descargar es una traducción más que mantener. Lo que
**sí** vive aquí es la traducción de *nuestras* claves a esos slugs
(`bsc` → `binance-smart-chain`), que es información nuestra.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QPixmap

from amigocompora.ui.theme import ICON_PX

#: Las tres carpetas por tipo. Son los nombres de web3icons menos `exchanges`,
#: que aquí se junta con el resto de marcas: para quien mira la pantalla, LI.FI y
#: Uniswap son lo mismo —de quién es la ruta—, y separarlos obliga a saber de
#: antemano si un nombre es un exchange o un puente para encontrar su fichero.
NETWORKS: Final = "networks"
TOKENS: Final = "tokens"
BRANDS: Final = "brands"

#: Tamaño de icono para quien tenga que pedirlo a mano —una ventana, un diálogo—:
#: en los listados lo pone la hoja de estilos (`theme.py`), que es donde vive el
#: número. Este es el mismo, para que una llamada suelta no mida distinto.
ICON_SIZE: Final = QSize(ICON_PX, ICON_PX)

#: Extensiones que se prueban, en orden. El SVG primero porque escala sin pelusa
#: en pantallas HiDPI; después el mapa de bits, que es lo que publican algunos
#: kits —PNG y WebP—, y hay carpetas que lo traen con la extensión cambiada: a
#: ésos los rescata `_cargar`, que mira el contenido.
_EXTENSIONES: Final = (".svg", ".png", ".webp")

#: Bajo qué carpeta puede estar cada tipo, en orden. La primera de cada pareja es
#: la que mantiene el proyecto a mano —la que se llena con los SVG elegidos
#: (`assets/chains/`, `assets/coin/`, `assets/plataform/`)— y la segunda la de
#: web3icons, que es la que escribe `tools/fetch_icons.py`. Manda la del proyecto:
#: un fichero elegido a mano no puede quedar tapado por una descarga masiva (y el
#: script, que pregunta antes de bajar, ni siquiera la deja en disco). Se prueban
#: todas para que un fichero descargado a mano no acabe en una carpeta que nadie
#: lee: el síntoma sería un icono en disco que no aparece nunca.
_CARPETAS: Final[dict[str, tuple[str, ...]]] = {
    NETWORKS: ("chains", "networks"),
    TOKENS: ("coin", "tokens"),
    BRANDS: ("plataform", "brands"),
}

#: Nuestras claves de red → slug de web3icons. Sólo las que no coinciden: lo que
#: no está en la tabla se busca por su propia clave.
_RED_A_SLUG: Final[dict[str, str]] = {
    "bsc": "binance-smart-chain",
    "arbitrum": "arbitrum-one",
}

#: Nombres alternativos de red que también valen como nombre de fichero. No son
#: de web3icons sino de nuestras carpetas: la red se llama `avalanche` en el
#: código y su logo se descargó como `avax.svg`. Se prueban **después** del slug
#: de web3icons, que es el que escribe el script de descarga.
_RED_SINONIMOS: Final[dict[str, str]] = {
    "avalanche": "avax",
}

#: Símbolo de token → slug. Son alias de marca, no equivalencias de activo: el
#: USDC puenteado de Polygon lleva el logo de USDC porque *es* USDC al otro lado
#: del puente, y el ether envuelto lleva el de ether. Lo que no tiene logo propio
#: ni alias honesto se queda sin icono, que es más barato que un logo prestado.
_TOKEN_A_SLUG: Final[dict[str, str]] = {
    "USDC.e": "USDC",
    "WETH": "ETH",
    "WETH.e": "ETH",
    "WSOL": "SOL",
    "WPOL": "POL",
    "WBNB": "BNB",
    "WAVAX": "AVAX",
    "BTC.b": "BTC",
    "cbBTC": "BTC",
    "cirBTC": "BTC",
}

#: Identificador de motor → slug de marca. Los tres Uniswap son la misma marca;
#: el resto se busca por su propio identificador.
_MOTOR_A_SLUG: Final[dict[str, str]] = {
    "uniswap_v3": "uniswap",
    "uniswap_v4": "uniswap",
}

#: El fichero genérico de cada tipo, para cuando un nombre no tiene logo propio.
#: Que sea genérico no lo convierte en el relleno que este módulo evita: no es la
#: marca de nadie, dice sólo «esto es un token» o «esto es un proveedor». Las
#: redes no tienen —enseñar una cadena cualquiera no diría nada— y `plataform`
#: va con la ortografía de la carpeta del proyecto: así se llama su fichero, y
#: renombrarlo sería moverle el sitio a quien lo puso.
_GENERICOS: Final[dict[str, str]] = {
    TOKENS: "token",
    BRANDS: "plataform",
}

#: Raíz forzada por `use_asset_root`, para pruebas y para apuntar a otra carpeta
#: sin tocar el árbol.
_raiz_forzada: Path | None = None

#: `QIcon` ya construidos, por (tipo, slug). Un `QIcon` se puede compartir entre
#: widgets —no es un widget— y construirlo toca disco, así que se guarda. Los
#: vacíos también: un fichero que no está hoy tampoco está dentro de un segundo,
#: y buscarlo en cada repintado de tabla son cuatro `stat` por fila.
_cache: dict[tuple[str, str], QIcon] = {}


def use_asset_root(ruta: Path | None) -> None:
    """Fija la carpeta de iconos; con `None` vuelve a la búsqueda normal.

    Para pruebas y para desarrollo. Vacía la caché, que si no guardaría los
    iconos —y los huecos— de la carpeta anterior.
    """
    global _raiz_forzada
    _raiz_forzada = ruta
    _cache.clear()


def asset_roots() -> tuple[Path, ...]:
    """Las carpetas donde se buscan iconos, en orden de preferencia."""
    if _raiz_forzada is not None:
        return (_raiz_forzada,)
    aqui = Path(__file__).resolve()
    return (aqui.parent / "assets", aqui.parents[3] / "assets")


def _candidatos(tipo: str, nombre: str) -> tuple[str, ...]:
    """Los nombres de fichero que pueden llevar el icono de `nombre`, en orden.

    En una **red** manda el slug de web3icons, que es el que escribe el script de
    descarga, y la clave cruda queda después (`arbitrum.svg` donde el catálogo
    publica `arbitrum-one`). En un **token** manda el símbolo tal y como se
    enseña —y sin puntos y en minúsculas, porque una carpeta mantenida a mano
    trae `usdce.svg` y `btcb.svg` donde el símbolo es `USDC.e` y `BTC.b`—, y el
    alias al catálogo (`WETH` → el logo de `ETH`) queda al final: es una
    aproximación honesta, pero el logo exacto del envoltorio es mejor cuando
    está. El sinónimo de casa va el último de todos. Se quita lo repetido
    conservando el orden.
    """
    sinonimo = _RED_SINONIMOS.get(nombre, "") if tipo == NETWORKS else ""
    sin_puntos = nombre.replace(".", "").lower() if tipo == TOKENS else ""
    orden = (
        (nombre, sin_puntos, slug_for(tipo, nombre), sinonimo)
        if tipo == TOKENS
        else (slug_for(tipo, nombre), nombre, sinonimo)
    )
    candidatos: list[str] = []
    for candidato in orden:
        if candidato and candidato not in candidatos:
            candidatos.append(candidato)
    return tuple(candidatos)


def icon_path(tipo: str, nombre: str) -> Path | None:
    """El fichero de este icono, o `None` si no está en ninguna raíz."""
    for raiz in asset_roots():
        for carpeta in _CARPETAS.get(tipo, (tipo,)):
            for candidato in _candidatos(tipo, nombre):
                for extension in _EXTENSIONES:
                    fichero = raiz / carpeta / f"{candidato}{extension}"
                    if fichero.is_file():
                        return fichero
    return None


def _cargar(ruta: Path | None) -> QIcon:
    """Carga el fichero de un icono; vacío si no se puede enseñar.

    Qt elige el cargador por la **extensión**, y hay kits de iconos que publican
    mapa de bits con nombre `.svg`: como SVG no cargan, y el resolvedor —que sí
    los encuentra— diría que el icono está mientras en pantalla no hay nada, que
    es el peor de los estados. El segundo intento va por el contenido
    (`loadFromData` mira los primeros bytes, no el nombre) y los carga igual. El
    orden no es casual: el SVG se intenta primero porque escala sin pelusa en
    pantallas HiDPI.

    Un fichero ilegible tampoco puede romper nada —esto se llama al pintar una
    tabla de saldos—, así que se queda en un icono vacío, que es exactamente lo
    que se ve cuando el fichero no está.
    """
    if ruta is None:
        return QIcon()
    icono = QIcon(str(ruta))
    if not icono.isNull():
        return icono
    mapa = QPixmap()
    try:
        carga = mapa.loadFromData(ruta.read_bytes())
    except OSError:
        return QIcon()
    if not carga:
        return QIcon()
    return QIcon(mapa)


def icon(tipo: str, nombre: str) -> QIcon:
    """El icono de este nombre, o un `QIcon` vacío si no hay fichero.

    Un `QIcon` vacío es seguro en todas partes: `addItem`, `setIcon` y
    `setWindowIcon` lo aceptan y no reservan hueco, así que el listado se ve como
    antes de que existieran los iconos.
    """
    clave = (tipo, nombre)
    guardado = _cache.get(clave)
    if guardado is not None:
        return guardado
    construido = _cargar(icon_path(tipo, nombre))
    _cache[clave] = construido
    return construido


def slug_for(tipo: str, nombre: str) -> str:
    """El slug del catálogo de iconos que le toca a uno de nuestros nombres.

    Es la **única** traducción entre cómo llama la aplicación a las cosas y
    cómo las llama web3icons, y por eso es pública: la usan igual la búsqueda en
    tiempo de ejecución y el script de descarga (`tools/fetch_icons.py`). Si
    viviera duplicada, lo que se descarga y lo que se busca podrían divergir, y
    el síntoma sería un icono que está en disco y no aparece nunca.
    """
    if tipo == NETWORKS:
        return _RED_A_SLUG.get(nombre, nombre)
    if tipo == TOKENS:
        return _TOKEN_A_SLUG.get(nombre, nombre)
    return _MOTOR_A_SLUG.get(nombre, nombre)


def network_icon(chain_key: str) -> QIcon:
    """El icono de una red, por nuestra clave (`base`, `bsc`, `polygon`…).

    Se pasa la clave sin traducir: la traducción a slug la hace `_candidatos`, y
    es ahí donde el nombre crudo queda como último recurso. Traducir aquí antes
    de llamar dejaría fuera ese recurso —candidatos de `arbitrum-one` ya no
    incluyen `arbitrum`, así que un `arbitrum.svg` en la carpeta no saldría
    nunca.
    """
    return icon(NETWORKS, chain_key)


def _generico(tipo: str) -> QIcon:
    """El icono genérico de un tipo, vacío si ese tipo no tiene."""
    nombre = _GENERICOS.get(tipo)
    return icon(tipo, nombre) if nombre else QIcon()


def token_icon(symbol: str) -> QIcon:
    """El icono de un token, por su símbolo tal y como se muestra.

    Sin logo propio se enseña el genérico (`token.svg`), que no dice «esto es
    USDC» sino «esto es un token»; y si el genérico tampoco está, la fila se
    queda como estaba, con su texto.
    """
    propio = icon(TOKENS, symbol)
    return propio if not propio.isNull() else _generico(TOKENS)


def brand_icon(engine_id: str) -> QIcon:
    """El icono de un motor o proveedor (`lifi`, `relay`, `uniswap`…).

    Igual que los tokens: sin logo propio, el genérico (`plataform.svg`).
    """
    propio = icon(BRANDS, engine_id)
    return propio if not propio.isNull() else _generico(BRANDS)


def available() -> dict[str, tuple[str, ...]]:
    """Qué iconos hay en disco, por tipo. Para diagnóstico y para las pruebas."""
    encontrados: dict[str, set[str]] = {NETWORKS: set(), TOKENS: set(), BRANDS: set()}
    for raiz in asset_roots():
        for tipo, slugs in encontrados.items():
            for carpeta in _CARPETAS[tipo]:
                ruta = raiz / carpeta
                if not ruta.is_dir():
                    continue
                slugs.update(
                    fichero.stem
                    for fichero in ruta.iterdir()
                    if fichero.suffix.lower() in _EXTENSIONES
                )
    return {tipo: tuple(sorted(slugs)) for tipo, slugs in encontrados.items()}
