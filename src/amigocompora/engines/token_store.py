"""Los tokens que el usuario añadió a mano, guardados entre sesiones.

El catálogo (`engines.catalog`) es una tabla escrita en el código: cubre los
tokens que se piden el 99 % de las veces y no crece sola. Todo lo demás se
resuelve pegando la dirección del contrato y leyéndola de la cadena
(`TokenLookup`), que devuelve el símbolo y **los decimales medidos**.

Lo que faltaba era guardarlo. Hasta ahora lo resuelto vivía en el widget que lo
pidió, así que cerrar la aplicación borraba el token y había que volver a pegar
la dirección en cada arranque — que es exactamente lo contrario de «lo añado y
aparece en la lista».

### Los decimales que se guardan están medidos, no escritos

Es la razón de que este fichero exista en vez de admitir que alguien edite el
catálogo a mano. Un decimal equivocado no da un error: da un precio mil veces
mayor o menor y una comparación que parece correcta. Aquí sólo entra lo que
devolvió una lectura del contrato, y el fichero guarda además de qué dirección
salió, para poder volver a comprobarlo.

### Un fichero ilegible no impide arrancar

Se trata como vacío y se registra el motivo. Es una lista de atajos, no un
estado del que dependa nada crítico: perderla cuesta volver a pegar una
dirección, y negarse a abrir la aplicación por un JSON a medio escribir costaría
mucho más.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from amigocompora.domain.models import Token

_log = structlog.get_logger(__name__)


@dataclass(slots=True)
class UserTokenStore:
    """Los tokens añadidos a mano, en un JSON dentro del directorio de configuración.

    Recibe el `Path` y **no** lo calcula: `infra.config.config_dir()` es de la
    capa de infraestructura, y una clase de `engines` que la llamara ataría el
    motor al sitio donde el usuario tenga su configuración. Quien construye el
    contenedor es quien sabe dónde va, igual que con `ExecutionLedger`.
    """

    path: Path
    #: Copia en memoria. `None` = todavía no se ha leído del disco.
    _cache: tuple[Token, ...] | None = field(default=None, init=False, repr=False)
    #: Si ya se avisó de que el fichero no se puede leer, para no repetirlo en
    #: cada lectura y convertir un aviso útil en ruido.
    _warned: bool = field(default=False, init=False, repr=False)

    # --------------------------------------------------------------- lectura #
    def load(self) -> tuple[Token, ...]:
        """Los tokens guardados, en el orden en que se añadieron.

        Se lee una vez y se recuerda: el fichero lo escribe sólo esta clase, así
        que la única forma de que la copia quede vieja es editar el JSON a mano
        mientras la aplicación corre, y para eso ya está `reload()`.
        """
        if self._cache is None:
            self._cache = self._read()
        return self._cache

    def reload(self) -> tuple[Token, ...]:
        """Vuelve a leer del disco, descartando la copia en memoria."""
        self._cache = self._read()
        return self._cache

    def _read(self) -> tuple[Token, ...]:
        if not self.path.is_file():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            self._warn_once(
                "token_store.unreadable",
                reason=str(error),
                hint=(
                    "se ignora la lista guardada y se sigue con el catálogo; "
                    "vuelve a añadir el token si lo necesitas"
                ),
            )
            return ()
        if not isinstance(raw, list):
            self._warn_once("token_store.not_a_list", kind=type(raw).__name__)
            return ()

        found: list[Token] = []
        seen: set[tuple[str, str]] = set()
        for entry in raw:
            token = _parse(entry)
            if token is None:
                continue
            # Se deduplica por red y dirección en minúsculas, y no por símbolo:
            # en Polygon el USDC nativo y el puenteado publican los dos
            # `symbol() == "USDC"` y son tokens distintos. La misma regla que
            # aplica `domain.chains` para comparar direcciones EVM.
            key = (token.chain, (token.address or "").lower())
            if key in seen:
                continue
            seen.add(key)
            found.append(token)
        return tuple(found)

    def _warn_once(self, event: str, **fields: Any) -> None:
        if self._warned:
            return
        self._warned = True
        _log.warning(event, **fields)

    # -------------------------------------------------------------- escritura #
    def add(self, token: Token) -> bool:
        """Guarda el token. Devuelve si era nuevo; si ya estaba, no hace nada.

        La identidad la decide `Token.is_same_asset` —red, símbolo y
        dirección—, no el símbolo solo, por la misma razón que la lectura
        deduplica por dirección: dos tokens distintos pueden llamarse igual.

        Volver a añadir el mismo token **actualiza sus decimales** en vez de
        ignorarlo: si el contrato los cambió, el guardado viejo es el que está
        mal, y negarse a corregirlo dejaría el error fijado en disco.

        Un token sin dirección **no se guarda**, y se dice: este almacén existe
        para lo que se añadió pegando un contrato, y el nativo de una red no tiene
        ninguno. Guardarlo produciría un renglón que la lectura siguiente
        descartaría sola, así que el token aparecería en la lista durante esta
        sesión y desaparecería en la próxima sin que nada explicara por qué.
        """
        if token.address is None:
            _log.warning(
                "token_store.no_address",
                chain=token.chain,
                symbol=token.symbol,
                hint=(
                    "sólo se guardan tokens identificados por su contrato; el "
                    "nativo de una red ya está en el catálogo"
                ),
            )
            return False

        current = list(self.load())
        for index, existing in enumerate(current):
            if existing.is_same_asset(token):
                if existing == token:
                    return False
                current[index] = token
                self._write(tuple(current))
                _log.info(
                    "token_store.updated",
                    chain=token.chain,
                    address=token.address,
                    decimals=token.decimals,
                )
                return False
        current.append(token)
        self._write(tuple(current))
        _log.info(
            "token_store.added",
            chain=token.chain,
            address=token.address,
            decimals=token.decimals,
        )
        return True

    def forget(self, chain_key: str, address: str) -> bool:
        """Quita el token de esa red y dirección. Devuelve si estaba.

        Se identifica por dirección y no por símbolo porque es lo único que
        distingue dos tokens homónimos, y borrar «el USDC» de Polygon sin decir
        cuál sería borrar el que no era la mitad de las veces.
        """
        wanted = address.strip().lower()
        current = list(self.load())
        kept = [
            token
            for token in current
            if not (token.chain == chain_key and (token.address or "").lower() == wanted)
        ]
        if len(kept) == len(current):
            return False
        self._write(tuple(kept))
        _log.info("token_store.forgotten", chain=chain_key, address=wanted)
        return True

    def _write(self, tokens: Iterable[Token]) -> None:
        """Escribe la lista entera, sustituyendo la copia en memoria al hacerlo.

        Se reescribe el fichero completo en vez de añadir una línea: es una lista
        corta, y un formato donde borrar exige reescribir igualmente no gana nada
        por ser incremental.

        Un fallo de escritura **no** se propaga. El token ya está resuelto y ya se
        puede operar con él en esta sesión; no poder recordarlo para la próxima
        es una molestia, y abortar la operación que el usuario acaba de pedir por
        eso sería convertirla en un error.
        """
        resolved = tuple(tokens)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Se escribe a un temporal y se mueve: un corte a mitad de escritura
            # dejaría un JSON truncado, que es justo el estado que la lectura
            # tiene que saber tolerar y que es más barato no llegar a producir.
            scratch = self.path.with_suffix(self.path.suffix + ".tmp")
            scratch.write_text(
                json.dumps([_dump(t) for t in resolved], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            scratch.replace(self.path)
        except OSError as error:
            _log.warning(
                "token_store.write_failed",
                path=str(self.path),
                reason=str(error),
                hint="el token funciona en esta sesión pero no se recordará",
            )
        self._cache = resolved

    def __iter__(self) -> Iterator[Token]:
        return iter(self.load())

    def __len__(self) -> int:
        return len(self.load())


def _dump(token: Token) -> dict[str, Any]:
    """Un token como diccionario, con las claves que se leen al volver."""
    return {
        "symbol": token.symbol,
        "decimals": token.decimals,
        "chain": token.chain,
        "address": token.address,
    }


def _parse(entry: Any) -> Token | None:
    """Lee una entrada del fichero, o `None` si no describe un token.

    Se valida campo a campo en vez de dejar que `Token` lance: un renglón roto no
    puede llevarse por delante a los demás. Lo que sí se exige es que la
    dirección sea una cadena no vacía —un token del fichero **tiene** dirección,
    porque se añadió por dirección—, y que los decimales sean un entero.
    """
    if not isinstance(entry, dict):
        return None
    symbol = entry.get("symbol")
    decimals = entry.get("decimals")
    chain_key = entry.get("chain")
    address = entry.get("address")
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    if not isinstance(decimals, int) or isinstance(decimals, bool) or decimals < 0:
        return None
    if not isinstance(chain_key, str) or not chain_key.strip():
        return None
    if not isinstance(address, str) or not address.strip():
        return None
    try:
        return Token(
            symbol=symbol.strip(),
            decimals=decimals,
            chain=chain_key.strip(),
            address=address.strip(),
        )
    except Exception:
        # Un token guardado que hoy no se puede construir no es motivo para
        # perder los demás.
        return None
