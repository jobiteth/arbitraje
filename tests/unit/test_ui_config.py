"""El ajuste que decide si la tabla de rutas enseña las de sólo cotización.

La tabla mezcla motores que construyen el swap con fuentes de sólo lectura que
publican precios —GeckoTerminal, DexScreener—. Esas filas llevan su marca ámbar
«· sólo cotiza» y no se pueden firmar: la cifra sirve para comparar contra las
rutas firmables. El ajuste existe porque hay quien las tiene por ruido y
prefiere una tabla con sólo lo firmable, y es del usuario decidirlo.

Dos cosas se afirman aquí, y son las que sostienen el diseño: **por omisión se
enseñan** —la comparación es el producto, y la marca ya evita firmar una ruta
que no lleva a ninguna parte—, y una clave mal escrita **no se ignora**:
`extra="forbid"` la convierte en un error de arranque, como en el resto del
modelo.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from amigocompora.infra.config import Settings


def test_showing_them_is_the_default() -> None:
    """Apagado por omisión: la cifra es un precio real y compara.

    El valor por omisión tiene que reproducir lo que ya hacía la tabla, y la
    tabla las enseñaba con su marca. Encender el ajuste es lo que cambia la
    vista; sin encenderlo, nadie pierde una cifra que venía usando.
    """
    assert Settings().ui.hide_quote_only_routes is False


def test_the_setting_can_be_declared_in_its_own_section() -> None:
    """`[ui] hide_quote_only_routes = true` llega al modelo tal cual.

    Se afirma sobre la configuración completa, y no sobre el bloque suelto,
    porque el camino que de verdad recorre el valor es el del fichero: la
    sección `[ui]` dentro de `Settings`.
    """
    settings = Settings.model_validate({"ui": {"hide_quote_only_routes": True}})
    assert settings.ui.hide_quote_only_routes is True


def test_an_unknown_key_in_the_ui_section_aborts_the_startup() -> None:
    """Una clave mal escrita se dice al arrancar en vez de perderse en silencio.

    Sin `extra="forbid"`, un `hide_quote_only = true` escrito de memoria no
    haría nada y la tabla seguiría enseñando lo que el usuario creía haber
    apagado, sin relacionar nunca las dos cosas.
    """
    with pytest.raises(ValidationError) as excinfo:
        Settings.model_validate({"ui": {"hide_quote_only": True}})

    assert "Extra inputs are not permitted" in str(excinfo.value)
    assert "hide_quote_only" in str(excinfo.value)
