"""El alias de pantalla de un motor: el nombre que elige el usuario.

Lo que se fija aquí son las tres reglas que hacen que un alias no pueda
convertirse en un problema: gana al nombre de fábrica pero sólo para el suyo,
vacío **borra** en vez de dejar una fila sin nombre, y lo que llega de un
`config.toml` escrito a mano —espacios de más, un salto de línea, un id en
blanco— se normaliza al entrar y no en cada pantalla que lo pinta.
"""

from __future__ import annotations

from amigocompora.app.engine_names import MAX_NAME_CHARS, EngineNames


def test_sin_alias_se_resuelve_el_nombre_de_fabrica() -> None:
    assert EngineNames().resolve("uniswap_v3", "Uniswap V3") == "Uniswap V3"


def test_el_alias_gana_al_de_fabrica_solo_para_el_suyo() -> None:
    nombres = EngineNames({"uniswap_v3": "El bueno"})
    assert nombres.resolve("uniswap_v3", "Uniswap V3") == "El bueno"
    assert nombres.resolve("uniswap_v2", "Uniswap V2") == "Uniswap V2"


def test_vacio_devuelve_el_nombre_de_fabrica() -> None:
    """Borrar el texto del diálogo quita el alias; no deja un nombre en blanco."""
    nombres = EngineNames({"uniswap_v3": "El bueno"})
    nombres.set("uniswap_v3", "   ")
    assert nombres.resolve("uniswap_v3", "Uniswap V3") == "Uniswap V3"
    assert nombres.as_dict() == {}


def test_los_espacios_se_normalizan_y_el_largo_se_acota() -> None:
    nombres = EngineNames()
    nombres.set("v3", "  Uniswap\n   V3  ")
    assert nombres.resolve("v3", "x") == "Uniswap V3"
    nombres.set("v3", "a" * (MAX_NAME_CHARS + 20))
    assert len(nombres.resolve("v3", "x")) == MAX_NAME_CHARS


def test_un_id_en_blanco_no_es_un_motor() -> None:
    nombres = EngineNames()
    nombres.set("   ", "Nada")
    assert nombres.as_dict() == {}


def test_la_instantanea_es_una_copia() -> None:
    """Lo que se persiste no puede ser el estado vivo, o un cambio posterior lo movería."""
    nombres = EngineNames({"a": "A"})
    copia = nombres.as_dict()
    copia["b"] = "B"
    assert nombres.as_dict() == {"a": "A"}


def test_lo_que_llega_de_la_configuracion_se_normaliza() -> None:
    """Un config.toml se escribe a mano: el alias entra ya saneado, no al pintarse."""
    nombres = EngineNames({"  uniswap_v3  ": "  El  bueno ", "otro": "   "})
    assert nombres.as_dict() == {"uniswap_v3": "El bueno"}
