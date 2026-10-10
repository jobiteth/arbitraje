"""Escritura de ajustes en `config.toml` sin tocar el resto del archivo.

El archivo lo escribe una persona a mano, con comentarios que explican cada
decisión. Por eso no se reserializa desde el modelo: `tomlkit` edita sólo la
clave que cambia y deja intacto todo lo demás, incluidos los comentarios.

Antes de reemplazar nada se validan los valores con las mismas reglas que usa
la carga, así que un archivo que no se podría leer nunca llega a escribirse.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import AoT, InlineTable, Table

from amigocompora.domain.errors import InvalidAmountError
from amigocompora.domain.protocols import EngineKind
from amigocompora.infra.config import ChainSettings, RpcEndpointSettings

_CHAINS_KEY = "chains"
_EXECUTION_KEY = "execution"
_ACTIVE_ENGINES_KEY = "active_engines"
_ENGINE_NAMES_KEY = "engine_names"


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


def set_execution_slippage_bps(path: Path, bps: int) -> None:
    """Fija el deslizamiento por defecto en `[execution]` y guarda el archivo.

    Se valida **antes** de tocar el documento, con la misma regla que la carga
    (`ExecutionSettings`, 0..10 000 bps) y que el valor vivo (`DefaultSlippage`):
    un archivo que no se podría leer nunca llega a escribirse, y el error cae
    aquí —con el valor delante— en vez de en el siguiente arranque.

    La tabla se edita, no se recrea: si ya existe con otras claves —`enabled`,
    topes, listas— se conservan tal cual, con sus comentarios. `InlineTable` entra
    en la comprobación porque un `execution = { slippage_bps = 50 }` escrito de
    una línea también es una tabla y recrearla se llevaría por delante lo demás.
    """
    if not 0 <= bps <= 10_000:
        raise InvalidAmountError(f"slippage_bps fuera de rango: {bps}. Se espera 0..10000.")
    document = _read(path)
    existing = document.get(_EXECUTION_KEY)
    if isinstance(existing, (Table, InlineTable)):
        table: Table | InlineTable = existing
    else:
        table = tomlkit.table()
        document[_EXECUTION_KEY] = table
    table["slippage_bps"] = bps
    _write(path, document)


def set_active_engines(path: Path, slot: str, engine_ids: Sequence[str]) -> None:
    """Deja escrito qué motores arrancan en una ranura, y guarda el archivo.

    Lo escribe la pestaña de motores al encender o apagar uno, para que la
    elección siga valiendo en el siguiente arranque. Se toca **sólo** la ranura
    que cambió: las demás quedan tal como estén, escritas o no, porque
    reescribirlas todas convertiría un encendido en una foto completa que
    incluiría a motores que no llegaron a arrancar.

    Una lista **vacía** se escribe como lista vacía, y significa «esta ranura
    apagada a propósito» —distinto de no tener la clave—, que es exactamente lo
    que apagar el último motor de una ranura quiere decir. Borrar la clave en
    vez de escribir `[]` haría lo contrario: el motor por omisión volvería solo
    en el siguiente arranque.

    La ranura se valida **antes** de tocar el documento, con el mismo enum que
    usa la carga: una que no exista es un error de quien llama y cae aquí, con
    el nombre delante, en vez de quedarse en el archivo esperando al arranque.
    """
    try:
        EngineKind(slot)
    except ValueError:
        raise ValueError(f"ranura desconocida: {slot!r}") from None
    document = _read(path)
    existing = document.get(_ACTIVE_ENGINES_KEY)
    if isinstance(existing, (Table, InlineTable)):
        table: Table | InlineTable = existing
    else:
        table = tomlkit.table()
        document[_ACTIVE_ENGINES_KEY] = table
    array = tomlkit.array()
    for engine_id in engine_ids:
        array.append(engine_id)
    table[slot] = array
    _write(path, document)


def set_engine_names(path: Path, names: Mapping[str, str]) -> None:
    """Deja escritos los alias de pantalla de los motores, y guarda el archivo.

    El mapa que llega es el estado **completo** de los alias —los que hay y los
    que no— y por eso la tabla se reescribe entera: dejar fuera un alias
    borrado lo resucitaría en el siguiente arranque. Sin ningún alias, la tabla
    se retira en vez de quedarse vacía: una tabla sin entradas es ruido en un
    fichero que se lee a mano. Las entradas en blanco se descartan aquí porque
    un alias vacío no es un nombre.

    Las claves se escriben ordenadas para que el archivo no cambie de forma
    según el orden en que se renombró: el mismo mapa produce siempre el mismo
    texto.
    """
    limpios = {
        engine_id.strip(): " ".join(name.split())
        for engine_id, name in names.items()
        if engine_id.strip() and name.split()
    }
    document = _read(path)
    if not limpios:
        if _ENGINE_NAMES_KEY not in document:
            return
        del document[_ENGINE_NAMES_KEY]
        _write(path, document)
        return
    table = tomlkit.table()
    for engine_id in sorted(limpios):
        table[engine_id] = limpios[engine_id]
    document[_ENGINE_NAMES_KEY] = table
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
