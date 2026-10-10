from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit
from pydantic import ValidationError

from amigocompora.domain.errors import InvalidAmountError
from amigocompora.infra.config import RpcEndpointSettings, Settings
from amigocompora.infra.config_writer import (
    set_active_engines,
    set_chain_endpoints,
    set_engine_names,
    set_execution_slippage_bps,
)

ARCHIVO_ORIGINAL = """\
# Comentario que debe sobrevivir.
mode = "execution"

[[chains]]
chain = "polygon"
include_public_fallbacks = true
endpoints = [
    { url = "https://polygon-mainnet.infura.io/v3/${INFURA_API_KEY}", priority = 10 },
]
"""

ARCHIVO_CON_EJECUCION = """\
# Comentario de cabecera.
[execution]
# El interruptor maestro: sin esto no se firma nada.
enabled = false
max_value_usd = 250
"""


def _endpoint(url: str, label: str = "propio", priority: int = 10) -> RpcEndpointSettings:
    return RpcEndpointSettings(url=url, label=label, priority=priority)


def test_conserva_comentarios_y_bloques_ajenos(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_ORIGINAL, encoding="utf-8")

    set_chain_endpoints(path, "polygon", [_endpoint("https://nodo.example/polygon")])

    texto = path.read_text(encoding="utf-8")
    assert "# Comentario que debe sobrevivir." in texto
    assert 'mode = "execution"' in texto
    assert "https://nodo.example/polygon" in texto
    assert "polygon-mainnet.infura.io" not in texto


def test_crea_la_red_si_no_estaba_declarada(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_ORIGINAL, encoding="utf-8")

    set_chain_endpoints(path, "base", [_endpoint("https://nodo.example/base")])

    texto = path.read_text(encoding="utf-8")
    assert texto.count("[[chains]]") == 2
    assert 'chain = "base"' in texto
    assert "https://nodo.example/base" in texto


def test_nodo_invalido_no_toca_el_archivo(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_ORIGINAL, encoding="utf-8")

    with pytest.raises(ValidationError):
        _endpoint("http://nodo-remoto.example/base")

    assert path.read_text(encoding="utf-8") == ARCHIVO_ORIGINAL
    assert not path.with_name(path.name + ".bak").exists()


def test_red_desconocida_se_rechaza_sin_escribir(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_ORIGINAL, encoding="utf-8")

    with pytest.raises(ValidationError):
        set_chain_endpoints(path, "red-inventada", [_endpoint("https://nodo.example/x")])

    assert path.read_text(encoding="utf-8") == ARCHIVO_ORIGINAL


def test_deja_copia_de_seguridad_antes_de_reemplazar(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_ORIGINAL, encoding="utf-8")

    set_chain_endpoints(path, "polygon", [])

    assert path.with_name("config.toml.bak").read_text(encoding="utf-8") == ARCHIVO_ORIGINAL
    assert not path.with_name("config.toml.tmp").exists()


# --------------------------------------------------------------------------- #
# El deslizamiento por defecto
# --------------------------------------------------------------------------- #
def test_crea_la_tabla_execution_si_no_estaba(tmp_path: Path) -> None:
    """El engranaje tiene que poder guardar en un archivo que no la tenía."""
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_ORIGINAL, encoding="utf-8")

    set_execution_slippage_bps(path, 250)

    texto = path.read_text(encoding="utf-8")
    assert "# Comentario que debe sobrevivir." in texto
    assert 'mode = "execution"' in texto
    assert tomlkit.parse(texto)["execution"]["slippage_bps"] == 250


def test_actualiza_la_tabla_sin_perder_lo_demas(tmp_path: Path) -> None:
    """La tabla se edita, no se recrea: el interruptor y los topes se conservan.

    Recrearla desde el modelo dejaría el archivo sin las claves que no están en
    el modelo —y sin los comentarios que explican por qué están—, que es justo
    lo que `tomlkit` evita.
    """
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_CON_EJECUCION, encoding="utf-8")

    set_execution_slippage_bps(path, 100)

    texto = path.read_text(encoding="utf-8")
    assert "# El interruptor maestro: sin esto no se firma nada." in texto
    assert "enabled = false" in texto
    assert "max_value_usd = 250" in texto
    assert tomlkit.parse(texto)["execution"]["slippage_bps"] == 100


@pytest.mark.parametrize("bps", [-1, 10_001])
def test_deslizamiento_fuera_de_rango_no_toca_el_archivo(tmp_path: Path, bps: int) -> None:
    """La misma regla que la carga: un archivo que no se leería no se escribe."""
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_ORIGINAL, encoding="utf-8")

    with pytest.raises(InvalidAmountError):
        set_execution_slippage_bps(path, bps)

    assert path.read_text(encoding="utf-8") == ARCHIVO_ORIGINAL
    assert not path.with_name(path.name + ".bak").exists()


def test_el_deslizamiento_tambien_deja_copia_de_seguridad(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_ORIGINAL, encoding="utf-8")

    set_execution_slippage_bps(path, 50)

    assert path.with_name(path.name + ".bak").read_text(encoding="utf-8") == ARCHIVO_ORIGINAL
    assert not path.with_name(path.name + ".tmp").exists()


# --------------------------------------------------------------------------- #
# Los interruptores de la pestaña de motores
# --------------------------------------------------------------------------- #
ARCHIVO_CON_MOTORES = """\
# Comentario de cabecera.
[active_engines]
dex_quotes = ["uniswap_v3", "geckoterminal"]
# Relay necesita su clave para arrancar.
cross_chain = "lifi"
"""


def test_apagar_el_ultimo_motor_escribe_la_ranura_vacia(tmp_path: Path) -> None:
    """`[]` es «apagada a propósito», y la carga tiene que leer lo mismo.

    Es la diferencia que hace que apagar la ranura de puentes sobreviva al
    reinicio: sin la clave volvería el motor por omisión, con la clave en una
    lista vacía no vuelve nadie.
    """
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_CON_MOTORES, encoding="utf-8")

    set_active_engines(path, "cross_chain", [])

    texto = path.read_text(encoding="utf-8")
    assert "# Relay necesita su clave para arrancar." in texto
    assert "dex_quotes" in texto
    documento = tomlkit.parse(texto).unwrap()
    assert documento["active_engines"]["cross_chain"] == []
    settings = Settings.model_validate(documento)
    assert "cross_chain" in settings.active_engines
    assert settings.active_engines["cross_chain"] == ()


def test_encender_un_motor_crea_la_tabla_si_no_estaba(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '# Comentario que debe sobrevivir.\nmode = "observation"\n', encoding="utf-8"
    )

    set_active_engines(path, "dex_quotes", ["uniswap_v3", "uniswap_v4"])

    texto = path.read_text(encoding="utf-8")
    assert "# Comentario que debe sobrevivir." in texto
    documento = tomlkit.parse(texto).unwrap()
    assert documento["active_engines"]["dex_quotes"] == ["uniswap_v3", "uniswap_v4"]
    settings = Settings.model_validate(documento)
    assert settings.active_engines["dex_quotes"] == ("uniswap_v3", "uniswap_v4")


def test_ranura_desconocida_se_rechaza_sin_escribir(tmp_path: Path) -> None:
    """La misma regla que la carga: un archivo que no se leería no se escribe."""
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_ORIGINAL, encoding="utf-8")

    with pytest.raises(ValueError, match="ranura desconocida"):
        set_active_engines(path, "ranura-inventada", ["uniswap_v3"])

    assert path.read_text(encoding="utf-8") == ARCHIVO_ORIGINAL
    assert not path.with_name(path.name + ".bak").exists()


# --------------------------------------------------------------------------- #
# Los alias de pantalla
# --------------------------------------------------------------------------- #
def test_los_alias_se_escriben_ordenados_y_sin_tocar_lo_demas(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_CON_MOTORES, encoding="utf-8")

    set_engine_names(path, {"zeta": "Última", "alfa": "Primera"})

    texto = path.read_text(encoding="utf-8")
    assert "# Comentario de cabecera." in texto
    assert 'alfa = "Primera"' in texto
    assert texto.index("alfa") < texto.index("zeta")
    documento = tomlkit.parse(texto).unwrap()
    assert documento["engine_names"] == {"alfa": "Primera", "zeta": "Última"}
    settings = Settings.model_validate(documento)
    assert settings.engine_names["zeta"] == "Última"


def test_sin_alias_la_tabla_se_retira(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        ARCHIVO_CON_MOTORES + '\n[engine_names]\nuniswap_v3 = "El bueno"\n',
        encoding="utf-8",
    )

    set_engine_names(path, {})

    texto = path.read_text(encoding="utf-8")
    assert "engine_names" not in texto
    assert "# Comentario de cabecera." in texto


def test_un_alias_en_blanco_no_llega_a_escribir(tmp_path: Path) -> None:
    """Sin tabla que retirar y sin nada que poner, el archivo no se toca — ni el `.bak`."""
    path = tmp_path / "config.toml"
    path.write_text(ARCHIVO_ORIGINAL, encoding="utf-8")

    set_engine_names(path, {"uniswap_v3": "   "})

    assert path.read_text(encoding="utf-8") == ARCHIVO_ORIGINAL
    assert not path.with_name(path.name + ".bak").exists()
