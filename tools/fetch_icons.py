"""Descarga de web3icons los iconos de los nombres que la aplicación muestra.

Uso:

    .venv/Scripts/python.exe tools/fetch_icons.py            # a assets/
    .venv/Scripts/python.exe tools/fetch_icons.py --lista    # sólo enseña qué pediría

De dónde salen los nombres —y esto es lo importante del script—: **no de una
lista escrita aquí**, sino de las colecciones de la propia aplicación. Las redes
son las del registro (`domain.chains`), los tokens los del catálogo
(`engines.catalog`, con los envoltorios nativos y la stablecoin de cada red) y
las marcas los identificadores de motor del proyecto. El slug de cada nombre lo
resuelve `ui.icons.slug_for`, que es la misma tabla que usa la aplicación al
buscar el icono en tiempo de ejecución: así lo que se descarga y lo que se busca
no pueden divergir —si divergieran, el síntoma sería un icono que está en disco
y no aparece nunca—.

Lo que web3icons no publica se imprime en el informe final y no se reintenta.
Hoy eso incluye marcas que la aplicación usa de verdad —LI.FI, Relay, Circle,
Polymarket—: su logo, si se quiere, hay que tomarlo de su propio kit de marca,
con su propia licencia. Eso es una decisión aparte, no un fallo de este script.

Los ficheros van a `<destino>/networks|tokens|brands`, que son los nombres del
catálogo de web3icons; el proyecto además lee las carpetas que mantiene a mano
(`assets/chains/`, `Coin/`, `plataform/`), así que una descarga a mano y una del
script conviven. Lo que ya está en el destino no se pisa, y lo que la aplicación
**ya resuelve** —por la carpeta que sea y con el nombre que sea— no se baja: un
segundo fichero en `networks/` le ganaría al que se ve hoy y cambiaría el listado
sin que nadie lo haya pedido.

El TLS es el de la aplicación (`infra.http.build_ssl_context`), no el de
`urllib` por omisión: `create_default_context` lee `SSLKEYLOGFILE` —variable que
algunos antivirus dejan puesta en el entorno— para volcar las claves de sesión, y
además se fía sólo de `certifi`, que no conoce la CA del antivirus que intercepta
TLS. Con el contexto de la aplicación las descargas van por donde va el resto de
la aplicación: almacén del sistema operativo, sin volcar claves.

Esto sólo escribe ficheros: no toca `config.toml`, no lee claves, no firma ni
emite nada.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

#: La raíz del repositorio, para encontrar `src/` y la carpeta de destino por
#: omisión sin depender del directorio desde el que se invoque.
_RAIZ: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_RAIZ / "src"))

from amigocompora.domain.chains import CHAINS  # noqa: E402
from amigocompora.engines.catalog import tokens_for  # noqa: E402
from amigocompora.infra.http import build_ssl_context  # noqa: E402
from amigocompora.ui import icons  # noqa: E402

#: El mismo contexto TLS que usa la aplicación para hablar con las APIs.
_SSL: Final = build_ssl_context()

#: Los SVG «background»: el logo sobre un cuadrado del color de la marca, que es
#: el estilo de los iconos que ya tiene el proyecto (`assets/chains/`). Las otras
#: dos variantes no encajan aquí —`branded` es el logo suelto y transparente, que
#: junto a los cuadrados se ve como si faltara algo, y `mono` viene con el trazo
#: en blanco fijo, que desaparece sobre un fondo claro—. Con `--variante` se
#: puede pedir otra.
_VARIANTE: Final = "background"

_URL: Final = (
    "https://raw.githubusercontent.com/0xa3k5/web3icons/main/raw-svgs/"
    "{tipo}/{variante}/{slug}.svg"
)

#: Una petición que no contesta en este tiempo es una petición caída, no una
#: lenta: son ficheros de unos pocos kilobytes en un CDN.
_TIMEOUT: Final = 30

#: Los identificadores de motor que la aplicación enseña. Se escriben aquí y no
#: se leen del registro porque descubrir los motores exige arrancar el
#: contenedor entero —configuración, llavero, adapters— para acabar usando sólo
#: una tupla de cadenas; y un motor nuevo sin icono no rompe nada, sólo se queda
#: con su texto, que es lo que hay hoy. Si alguno no existe en web3icons, el
#: informe final lo dice.
_MOTORES: Final[tuple[str, ...]] = (
    "geckoterminal",
    "dexscreener",
    "jupiter",
    "uniswap",
    "uniswap_v3",
    "uniswap_v4",
    "zeroex",
    "lifi",
    "relay",
    "circle",
    "polymarket",
    "wallet",
)


def _nombres_redes() -> tuple[str, ...]:
    """Las claves de las redes del registro."""
    return tuple(CHAINS)


def _nombres_tokens() -> tuple[str, ...]:
    """Todos los nombres de token que el catálogo puede enseñar.

    Se recogen los nombres, no los slugs: la moneda nativa de cada red —que el
    catálogo sólo ofrece envuelta (`WSOL`, `WETH`), pero la cartera la enseña
    suelta y necesita su icono—, el envoltorio, la stablecoin y los extras. La
    traducción a slug la hace `icons.slug_for` al armar los pedidos, y con ella
    el envoltorio y el USDC puenteado caen en el mismo slug que su moneda: lo
    que se baja es el conjunto de logos distintos, no el de símbolos distintos.
    """
    nombres: list[str] = []
    for clave, spec in CHAINS.items():
        nombres.append(spec.native_symbol)
        for token in tokens_for(clave):
            nombres.append(token.display_symbol)
            nombres.append(token.symbol)
    return tuple(nombres)


def _nombres_marcas() -> tuple[str, ...]:
    """Los identificadores de motor, por la misma tabla de alias del runtime."""
    return _MOTORES


def _pedidos(tipo: str, nombres: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Las parejas `(slug, nombre)` que hay que pedir, sin repetir slug.

    El nombre que acompaña al slug es el primero que lo reclamó, y sirve para
    preguntar si el icono **ya está**: la aplicación resuelve por nombre —con
    sus alias, sus carpetas y sus sinónimos— y el script tiene que hacer la
    misma pregunta antes de bajar nada, o dejaría en disco un segundo fichero
    que tapa el que ya se veía en pantalla.
    """
    vistos: dict[str, str] = {}
    for nombre in nombres:
        vistos.setdefault(icons.slug_for(tipo, nombre), nombre)
    return tuple(sorted(vistos.items()))


def _plan() -> dict[str, tuple[tuple[str, str], ...]]:
    """Qué se va a pedir, por tipo, en orden estable para que el informe no baile."""
    return {
        icons.NETWORKS: _pedidos(icons.NETWORKS, _nombres_redes()),
        icons.TOKENS: _pedidos(icons.TOKENS, _nombres_tokens()),
        icons.BRANDS: _pedidos(icons.BRANDS, _nombres_marcas()),
    }


def _descargar(tipo: str, slug: str, destino_raiz: Path, variante: str) -> str:
    """Trae un SVG y lo escribe. Devuelve `""` si bajó, o por qué no.

    Un 404 se informa como tal y no como error: la mitad de los candidatos no
    existen en web3icons y eso ya se sabe; lo que no se sabe hasta probar es
    *cuáles*. Cualquier otro fallo —sin red, DNS, TLS— se distingue en el texto
    porque ése sí es un problema del entorno y no del catálogo.

    Lo que ya está en el destino no se vuelve a pedir ni se pisa: un fichero
    presente puede estar ahí porque alguien lo eligió a mano, y reescribirlo
    cambiaría lo que se ve en pantalla sin que nadie lo haya pedido.
    """
    destino = destino_raiz / tipo / f"{slug}.svg"
    if destino.exists():
        return "ya estaba"
    url = _URL.format(tipo=tipo, variante=variante, slug=slug)
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT, context=_SSL) as respuesta:  # noqa: S310
            cuerpo = respuesta.read()
    except urllib.error.HTTPError as error:
        return f"no está en web3icons (HTTP {error.code})"
    except OSError as error:
        return f"fallo de red: {error}"
    if not cuerpo.lstrip().startswith(b"<"):
        return "la respuesta no es un SVG"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(cuerpo)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        default=_RAIZ / "assets",
        help="carpeta de destino (por omisión, assets/ en la raíz del repositorio)",
    )
    parser.add_argument(
        "--lista",
        action="store_true",
        help="enseñar lo que se pediría sin descargar nada",
    )
    parser.add_argument(
        "--variante",
        default=_VARIANTE,
        choices=("background", "branded", "mono"),
        help=f"variante de web3icons (por omisión, {_VARIANTE})",
    )
    args = parser.parse_args()

    plan = _plan()
    total = sum(len(pedidos) for pedidos in plan.values())
    if args.lista:
        for tipo, pedidos in plan.items():
            print(f"{tipo} ({len(pedidos)}):")
            for slug, nombre in pedidos:
                visible = icons.icon_path(tipo, nombre)
                marca = f"  ->  ya visible: {visible}" if visible is not None else ""
                print(f"  {slug} ({nombre}){marca}")
        print(f"Total: {total} ficheros hacia {args.dest}")
        return 0

    descargados = 0
    ya: list[tuple[str, str, str]] = []
    faltan: list[tuple[str, str, str]] = []
    for tipo, pedidos in plan.items():
        for slug, nombre in pedidos:
            # La aplicación pregunta por nombre, no por slug: si ya resuelve este
            # icono —por otra carpeta o por su nombre crudo—, bajarlo dejaría en
            # `networks/` un segundo fichero que le gana al que se ve hoy (es la
            # primera carpeta que se mira) y cambiaría el listado sin que nadie lo
            # haya pedido. Un icono que ya se ve no se baja.
            visible = icons.icon_path(tipo, nombre)
            if visible is not None:
                ya.append((tipo, slug, str(visible)))
                continue
            motivo = _descargar(tipo, slug, args.dest, args.variante)
            if not motivo:
                descargados += 1
                print(f"  ok    {tipo}/{slug}.svg")
            elif motivo == "ya estaba":
                ya.append((tipo, slug, str(args.dest / tipo / f"{slug}.svg")))
            else:
                faltan.append((tipo, slug, motivo))

    print()
    print(f"Descargados {descargados} de {total} en {args.dest}.")
    if ya:
        print(f"Ya estaban ({len(ya)}), sin tocar:")
        for tipo, slug, donde in ya:
            print(f"  {tipo}/{slug}: {donde}")
    if faltan:
        print(f"Sin descargar ({len(faltan)}):")
        for tipo, slug, motivo in faltan:
            print(f"  {tipo}/{slug}: {motivo}")
        print(
            "Los que no existen en web3icons se quedan con su texto y sin icono; "
            "su logo, si se quiere, viene de su propio kit de marca y con su propia "
            "licencia."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
