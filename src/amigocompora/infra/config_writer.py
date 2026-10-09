"""Escritura de los nodos RPC en `config.toml` sin tocar el resto del archivo.

El archivo lo escribe una persona a mano, con comentarios que explican cada
decisión. Por eso no se reserializa desde el modelo: `tomlkit` edita sólo el array
`endpoints` de la red afectada y deja intacto todo lo demás, incluidos los
comentarios.

Antes de reemplazar nada se validan los endpoints con los mismos modelos que usa
la carga, así que un archivo que no se podría leer nunca llega a escribirse.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import AoT, Table

from amigocompora.infra.config import ChainSettings, RpcEndpointSettings

_CHAINS_KEY = "chains"


def set_chain_endpoints(
    path: Path, chain: str, endpoints: Sequence[RpcEndpointSettings]
) -> None:
    """Sustituye los endpoints declarados de una red y guarda el archivo."""
    ChainSettings(chain=chain, endpoints=tuple(endpoints))
    document = _read(path)
    table = _chain_table(document, chain)
    if table is None:
        table = tomlkit.table()
        table["chain"] = chain
        table["include_public_fallbacks"] = True
        table["endpoints"] = _endpoints_array(endpoints)
        _chains(document).append(table)
    else:
        table["endpoints"] = _endpoints_array(endpoints)
    _write(path, document)


def _read(path: Path) -> TOMLDocument:
    if not path.exists():
        return tomlkit.document()
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def _chain_table(document: TOMLDocument, chain: str) -> Table | None:
    chains = document.get(_CHAINS_KEY)
    if not isinstance(chains, AoT):
        return None
    for table in chains:
        if isinstance(table, Table) and str(table.get("chain", "")).strip().lower() == chain:
            return table
    return None


def _chains(document: TOMLDocument) -> AoT:
    chains = document.get(_CHAINS_KEY)
    if isinstance(chains, AoT):
        return chains
    created = tomlkit.aot()
    document.append(_CHAINS_KEY, created)
    return created


def _endpoints_array(endpoints: Sequence[RpcEndpointSettings]) -> tomlkit.items.Array:
    array = tomlkit.array()
    array.multiline(True)
    for endpoint in endpoints:
        inline = tomlkit.inline_table()
        inline["url"] = endpoint.url
        inline["label"] = endpoint.label
        inline["priority"] = endpoint.priority
        array.append(inline)
    return array


def _write(path: Path, document: TOMLDocument) -> None:
    """Copia de seguridad y reemplazo atómico: un corte a mitad no deja el archivo a medias."""
    text = tomlkit.dumps(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
