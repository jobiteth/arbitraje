"""El símbolo y los decimales de un token cualquiera, leídos de su contrato.

### Por qué existe

El catálogo (`catalog.py`) cubre los tokens que se piden el 99 % de las veces y
lo dice: «cualquier otro token se resuelve preguntando a la fuente por su
dirección». Esto es esa frase hecha código, y es lo que hace que la aplicación
pueda operar con **cualquier** token en vez de con los que alguien escribió en
una tabla. Un token creado hace un minuto no está en ninguna lista y sí tiene
contrato.

### De dónde salen los datos

De la cadena, con dos `eth_call` —`symbol()` y `decimals()`—, no de un índice
externo. Tres razones, y las tres importan:

- **No hay lista que mantener.** Un agregador consulta la lista del token o se la
  inventa; aquí se pregunta al contrato, que es lo que de verdad manda.
- **Lo gratis no se cae.** Estos `eth_call` van por los mismos nodos que ya se
  usan para cotizar, con sus respaldos públicos: no hay una cuota nueva que
  agotar ni una clave más que pegar.
- **Los decimales son el dato crítico.** Un decimal equivocado no da error: da un
  precio mil veces mayor o menor, y una operación con el importe mal escalado. Por
  eso se leen del contrato en vez de aceptarse tecleados.

### Lo que no hace

No inventa el símbolo de un contrato que no lo publica. Si el contrato no
contesta, la respuesta es un error que lo dice, no `"?"`: un símbolo de relleno
acabaría en la lista de tokens, en el diálogo de confirmación y en el registro de
ejecuciones, y alguien acabaría firmando «? → USDC».

Sólo EVM. En Solana la metadata de un token vive en su *mint account*, con otra
codificación y otro programa, y devolver aquí un símbolo leído con selectores de
EVM sería inventarlo. Las redes no EVM se rechazan con un mensaje que lo explica.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import structlog

from amigocompora.domain.chains import CHAINS, AddressFormat
from amigocompora.domain.errors import (
    SourceResponseError,
    SourceUnavailableError,
    UnknownChainError,
    UnsupportedOperationError,
)
from amigocompora.domain.models import Token
from amigocompora.infra.rpc.pool import AllEndpointsFailedError, RpcError, RpcRegistry

_log = structlog.get_logger(__name__)

#: Los dos selectores de ERC-20 que hacen falta. Son parte del estándar y están
#: en todos los tokens, incluidas las stablecoins que publican `decimals` como
#: `uint8` y las que lo publican como `uint256`: en la ABI las dos ocupan una
#: palabra, así que se leen igual.
SELECTOR_SYMBOL: Final = "0x95d89b41"
SELECTOR_DECIMALS: Final = "0x313ce567"

#: Tope de decimales que se acepta. No es una regla del estándar —`decimals` es un
#: `uint8` en la mayoría— sino un límite de cordura: por encima de esto el importe
#: mínimo representable es tan pequeño que cualquier cifra deja de significar
#: nada, y es más probable que sea un contrato que contesta basura que un token
#: real. Se rechaza con el valor a la vista para que se pueda comprobar.
MAX_DECIMALS: Final = 36

#: Cuánto puede medir un símbolo. El más largo del catálogo real —`BTC.b`,
#: `WETH.e`— no llega a diez; 32 deja sitio de sobra y acota lo que se copia a
#: todas las superficies.
MAX_SYMBOL_LENGTH: Final = 32


@dataclass(frozen=True, slots=True)
class TokenLookup:
    """Resuelve un token a partir de su dirección, leyendo su propio contrato.

    Se le pasa el registro de nodos que ya tiene la aplicación en vez de construir
    su propio lector: son los mismos nodos, con la misma salud acumulada y el
    mismo cliente HTTP, y montar un segundo camino daría dos ideas distintas de
    «qué nodos funcionan».
    """

    rpc: RpcRegistry

    async def by_address(self, chain_key: str, address: str) -> Token:
        """El token de esa dirección en esa red, con su símbolo y sus decimales.

        Valida el **formato** de la dirección antes de salir a la red —una
        dirección de Solana en una red EVM no da un error de red, da una llamada
        silenciosamente vacía— y después comprueba que haya contrato: una
        dirección válida sin código es una cartera o un token que no existe, y
        merece que se diga eso y no «la llamada falló».
        """
        spec = CHAINS.get(chain_key)
        if spec is None:
            raise UnknownChainError(
                f"«{chain_key}» no es una red conocida: no se puede resolver un token ahí"
            )
        if spec.address_format is not AddressFormat.EVM_HEX:
            raise UnsupportedOperationError(
                f"resolver un token por su dirección sólo está implementado en redes "
                f"EVM, y {spec.name} no lo es: ahí la metadata del token vive en su "
                f"cuenta de acuñación y se lee de otra forma. Cotiza el par por el "
                f"catálogo."
            )
        if chain_key not in self.rpc:
            raise SourceUnavailableError(
                f"no hay nodos configurados para {spec.name}, así que no se puede leer "
                f"el contrato del token. Añade un endpoint de esa red en config.toml."
            )
        resolved = spec.require_address(address)

        if not await self._has_code(chain_key, resolved):
            raise SourceResponseError(
                f"la dirección {resolved} no tiene contrato en {spec.name}: es una "
                f"cartera, o un token que todavía no existe en esta red."
            )

        symbol = await self._symbol(chain_key, resolved)
        decimals = await self._decimals(chain_key, resolved)
        _log.info(
            "token.resolved",
            chain=chain_key,
            address=resolved,
            symbol=symbol,
            decimals=decimals,
        )
        return Token(symbol=symbol, decimals=decimals, chain=chain_key, address=resolved)

    # ------------------------------------------------------------------ #
    # Lecturas
    # ------------------------------------------------------------------ #
    async def _has_code(self, chain_key: str, address: str) -> bool:
        raw = await self._call(chain_key, "eth_getCode", [address, "latest"], address)
        return isinstance(raw, str) and raw not in ("0x", "0x0", "")

    async def _symbol(self, chain_key: str, address: str) -> str:
        raw = await self._call(
            chain_key,
            "eth_call",
            [{"to": address, "data": SELECTOR_SYMBOL}, "latest"],
            address,
        )
        text = _clean_symbol(_decode_text(raw))
        if not text:
            # Un contrato que contesta con la cadena vacía sí tiene `symbol()`,
            # pero no dice nada. Se cae a la dirección acortada —un identificador
            # feo pero verdadero— en vez de a un relleno que parezca un símbolo.
            return f"{address[:6]}…{address[-4:]}"
        return text

    async def _decimals(self, chain_key: str, address: str) -> int:
        raw = await self._call(
            chain_key,
            "eth_call",
            [{"to": address, "data": SELECTOR_DECIMALS}, "latest"],
            address,
        )
        word = _first_word(raw)
        if word is None:
            raise SourceResponseError(
                f"el contrato {address} de {chain_key} no publica `decimals()`: sin "
                f"ellos no se puede escalar ningún importe, y suponerlos daría una "
                f"cifra equivocada sin ningún error a la vista."
            )
        decimals = int(word, 16)
        if decimals > MAX_DECIMALS:
            raise SourceResponseError(
                f"el contrato {address} de {chain_key} dice tener {decimals} decimales, "
                f"más de los {MAX_DECIMALS} que se aceptan. Es más probable que sea un "
                f"contrato que contesta cualquier cosa que un token real."
            )
        return decimals

    async def _call(
        self, chain_key: str, method: str, params: list[object], address: str
    ) -> object:
        """Una lectura, con los dos fallos traducidos a lo que significan aquí.

        Un revert **no** es una fuente caída: es el contrato diciendo que no. Se
        distinguen porque el mensaje que hay que dar es distinto —«esa dirección
        no es un token» frente a «no hay ningún nodo que conteste»— y porque
        colapsarlos obligaría a quien lee el error a adivinar cuál de los dos fue.
        """
        try:
            return await self.rpc.pool(chain_key).call(method, params)
        except RpcError as error:
            raise SourceResponseError(
                f"el contrato {address} de {chain_key} rechazó {method}: "
                f"{error.rpc_message}"
            ) from error
        except AllEndpointsFailedError as error:
            raise SourceUnavailableError(
                f"ningún nodo de {chain_key} respondió a {method}. Comprueba la "
                f"conexión y vuelve a intentarlo."
            ) from error


# --------------------------------------------------------------------------- #
# Decodificación del retorno
# --------------------------------------------------------------------------- #
def _hex(raw: object) -> str:
    """El cuerpo hexadecimal del retorno, sin `0x` y validado.

    Un cuerpo **vacío** no es un error: `0x` es exactamente lo que contesta el
    nodo por una dirección sin contrato, y quien llama tiene que poder leerlo
    como «no hay dato» en vez de como «la respuesta está rota». Lo que sí se
    rechaza es una longitud impar, que no puede ser hexadecimal de nada.
    """
    if not isinstance(raw, str):
        raise SourceUnavailableError(
            f"la cadena devolvió {type(raw).__name__} donde se esperaba el hexadecimal "
            f"de un eth_call"
        )
    text = raw[2:] if raw[:2].lower() == "0x" else raw
    if len(text) % 2 != 0:
        raise SourceResponseError(f"la respuesta «{raw[:24]}…» no es hexadecimal de longitud par")
    return text


def _first_word(raw: object) -> str | None:
    """La primera palabra de 32 bytes del retorno, o `None` si no llega.

    `None` y no un cero: `0x` es lo que contesta un nodo por una dirección sin
    contrato, y un cero ahí se leería como «0 decimales», que es un valor
    legítimo y daría un precio equivocado con toda la apariencia de ser correcto.
    """
    body = _hex(raw)
    return body[:64] if len(body) >= 64 else None


def _decode_text(raw: object) -> str:
    """El texto de un retorno de `symbol()`, en sus dos formas reales.

    La mayoría de los tokens devuelven una `string` de ABI —un desplazamiento, una
    longitud y los bytes—, pero unos pocos antiguos devuelven directamente un
    `bytes32` relleno de ceros: MKR es el caso conocido. Leer sólo la forma
    moderna convertiría a esos tokens en irresolubles sin ninguna razón.

    El desplazamiento **no se da por bueno**: se comprueba que quepa en la
    respuesta. Un contrato puede devolver cualquier cosa, y confiar en él sería
    leer fuera del buffer con un `int` gigante.
    """
    body = _hex(raw)
    if len(body) < 64:
        return ""
    if len(body) == 64:
        # `bytes32`: el texto va relleno de ceros a la derecha.
        return _ascii(body)
    offset = int(body[:64], 16) * 2
    if offset + 64 > len(body):
        return ""
    length = int(body[offset : offset + 64], 16)
    start = offset + 64
    end = start + length * 2
    # Una longitud declarada mayor que lo que llegó es un contrato que miente; se
    # lee lo que hay en vez de fallar, porque el símbolo es un adorno comparado
    # con los decimales, que sí son un dato.
    return _ascii(body[start : min(end, len(body))])


def _ascii(body_hex: str) -> str:
    try:
        return bytes.fromhex(body_hex).decode("utf-8", errors="replace")
    except ValueError:
        return ""


def _clean_symbol(text: str) -> str:
    """El símbolo, sin los caracteres que lo convierten en algo más que un nombre.

    Esto viene de un contrato que no controlamos, y acaba en la lista de tokens,
    en el diálogo de confirmación y en el registro de ejecuciones. Un símbolo con
    un salto de línea puede falsear una línea del registro y uno con secuencias de
    escape puede mover el cursor de una terminal. Nada de eso es un nombre de
    token: fuera todo lo que no sea imprimible.
    """
    limpio = "".join(char for char in text if char.isprintable() and char != "\x00").strip()
    return limpio[:MAX_SYMBOL_LENGTH]
