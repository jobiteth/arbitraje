"""El fichero de ejemplo que se entrega tiene que funcionar tal cual.

Existe por un fallo que se midió, no por una precaución teórica. En TOML, toda
clave suelta que venga después de una cabecera `[tabla]` pertenece a esa tabla, y
en el ejemplo `watch_addresses = []` estaba escrito **debajo** de
`[active_engines]`. El resultado no era que la clave se ignorara —eso sería
inofensivo y visible—: se colaba dentro de la tabla anterior y llegaba al modelo
como `active_engines.watch_addresses`, donde se espera una cadena y llegaba una
lista. Copiar el ejemplo y arrancar fallaba con un error que hablaba de los
motores, no de las direcciones, y que no mencionaba el fichero de ejemplo por
ningún lado.

Lo grave no es el error, es cuándo aparece: el fichero de ejemplo no lo usa nadie
del equipo —cada cual tiene el suyo—, así que un ejemplo roto se queda roto hasta
que lo sufre un usuario nuevo, que es justo quien menos puede diagnosticarlo.

Lo que se afirma aquí es que el documento se valida y que **ninguna clave de
nivel superior se ha colado dentro de una tabla**. Lo segundo no lo cubre la
validación: si el valor colado hubiera tenido el tipo esperado, habría pasado sin
decir nada y el ajuste se habría perdido en silencio.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Final

from amigocompora.infra.config import Settings

#: `tests/unit/<fichero>` → la raíz del repositorio.
EJEMPLO: Final = Path(__file__).resolve().parents[2] / "config.example.toml"

#: Los nombres que la aplicación reconoce en el primer nivel. Una clave con uno
#: de estos nombres dentro de una tabla no significa lo mismo que fuera, y es
#: exactamente el error que se está vigilando.
DE_PRIMER_NIVEL: Final = frozenset(Settings.model_fields)


def _documento() -> dict[str, Any]:
    with EJEMPLO.open("rb") as handle:
        return tomllib.load(handle)


def test_the_example_exists_where_it_is_shipped() -> None:
    """Si alguien mueve el fichero, la prueba tiene que decir eso y no otra cosa."""
    assert EJEMPLO.is_file(), f"no está el ejemplo en {EJEMPLO}"


def test_the_example_validates_against_the_real_settings_model() -> None:
    """Copia, arranca, funciona. Sin editar nada."""
    settings = Settings.model_validate(_documento())
    # Y se comprueba que de verdad se leyó el fichero y no los valores por
    # omisión del modelo: si la carga se rompiera en silencio, esto lo notaría.
    assert settings.execution.gas.multiplier == "1.2"
    assert settings.active_engines["dex_quotes"] == (
        "uniswap_v3",
        "uniswap_v4",
        "geckoterminal",
    )


def test_no_top_level_setting_is_nested_inside_a_table() -> None:
    """La comprobación que el fallo original habría detenido.

    Se recorre lo que el fichero declara de verdad —el documento ya parseado, no
    su texto— y se busca un nombre de primer nivel dentro de cualquier tabla. El
    orden importa: mirar el texto no serviría, porque el error consiste
    precisamente en que la línea está escrita donde no le toca y en que TOML la
    coloca en otro sitio.
    """
    documento = _documento()
    coladas: list[str] = []
    for tabla, contenido in documento.items():
        if isinstance(contenido, dict):
            coladas.extend(f"{tabla}.{clave}" for clave in contenido if clave in DE_PRIMER_NIVEL)
    assert not coladas, (
        "estas claves están dentro de una tabla pero son ajustes de primer nivel, "
        "así que TOML las lee como otra cosa: " + ", ".join(sorted(coladas))
    )


def test_the_example_declares_the_settings_it_documents() -> None:
    """Las claves que el ejemplo enseña sin comentar llegan al modelo.

    Es el ancla que hace útil a la prueba anterior: sin esto, un ejemplo al que
    alguien le hubiera comentado todo seguiría pasando las otras dos.
    """
    documento = _documento()
    assert documento["watch_addresses"] == []
    assert documento["mode"] == "observation"
    assert documento["execution"]["enabled"] is False
