from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from amigocompora.infra.config import RpcEndpointSettings
from amigocompora.infra.config_writer import set_chain_endpoints

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
