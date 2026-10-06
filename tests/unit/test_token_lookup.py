"""Resolver un token cualquiera por su dirección, leyendo su contrato.

Lo que se comprueba aquí es la promesa que hace que la aplicación no esté limitada
a una lista: se pregunta al contrato por `symbol()` y `decimals()` y lo que
contesta es lo que vale. Y se comprueba, sobre todo, **lo que se hace cuando el
contrato no contesta lo que debe**, que es donde un resolutor complaciente se
vuelve peligroso: los decimales son el dato con el que se escala todo importe, y
suponerlos no da un error, da una cifra equivocada con toda la apariencia de ser
correcta.

El nodo es de mentira —se falsea el transporte HTTP, no el `RpcPool`— para que se
ejerza también la traducción de errores del pool: que un revert y un nodo mudo
llegan aquí como dos cosas distintas es la mitad del valor de este módulo.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    SourceResponseError,
    SourceUnavailableError,
    UnknownChainError,
    UnsupportedOperationError,
)
from amigocompora.engines.token_lookup import (
    MAX_SYMBOL_LENGTH,
    SELECTOR_DECIMALS,
    SELECTOR_SYMBOL,
    TokenLookup,
)
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool, RpcRegistry

AHORA = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

#: El token del ejemplo, medido en Base.
BANKR = "0x26f79444595cF1C6753bAEd09DbC0C6F73144443"


# --------------------------------------------------------------------------- #
# Nodo de mentira
# --------------------------------------------------------------------------- #
class _Nodo:
    """Nodo JSON-RPC al que se le dicta la respuesta de cada método.

    Un manejador puede devolver un valor —una respuesta normal— o una tupla
    `("error", código, mensaje)`, que es lo que hace falta para ejercer un revert:
    la diferencia entre eso y un nodo que no contesta es justo lo que este módulo
    tiene que saber distinguir.
    """

    def __init__(self, **handlers: Any) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, list[Any]]] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = str(body["method"])
            params = list(body.get("params", []))
            self.calls.append((method, params))
            result = self.handlers.get(method)
            if result is None:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": {"code": -32601, "message": f"no previsto: {method}"},
                    },
                )
            if callable(result):
                result = result(params)
            if isinstance(result, tuple) and result and result[0] == "error":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": {"code": result[1], "message": result[2]},
                    },
                )
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result}
            )

        return httpx.MockTransport(handler)

    def called(self, method: str) -> list[list[Any]]:
        return [params for name, params in self.calls if name == method]

    def eth_calls_to(self, address: str) -> list[str]:
        """Los `data` de los `eth_call` dirigidos a ese contrato, en orden."""
        return [
            str(params[0]["data"])
            for params in self.called("eth_call")
            if str(params[0].get("to", "")).lower() == address.lower()
        ]


def _lookup(node: _Nodo, *, chains: tuple[str, ...] = ("base",)) -> TokenLookup:
    pools = {
        key: RpcPool(
            key,
            (RpcEndpoint(url="https://rpc.example.org", label="falso", priority=10),),
            httpx.AsyncClient(transport=node.transport()),
            clock=FrozenClock(AHORA),
        )
        for key in chains
    }
    return TokenLookup(rpc=RpcRegistry(pools))


def _abi_string(text: str) -> str:
    """El retorno de una `string` de ABI: desplazamiento, longitud y los bytes."""
    raw = text.encode("utf-8")
    relleno = raw.hex().ljust(((len(raw) + 31) // 32) * 32 * 2, "0")
    return "0x" + f"{32:064x}" + f"{len(raw):064x}" + relleno


def _bytes32(text: str) -> str:
    """El retorno de un `bytes32` con el texto y ceros a la derecha."""
    raw = text.encode("utf-8")
    assert len(raw) <= 32
    return "0x" + raw.hex().ljust(64, "0")


def _token_node(*, symbol: Any = None, decimals: Any = 12, code: str = "0x60016001") -> _Nodo:
    """Un nodo que contesta como un token ERC-20 normal.

    `symbol` y `decimals` se pasan tal cual para poder dictar respuestas raras:
    un `0x` vacío, un revert, o un número absurdo.
    """
    def erc20(params: list[Any]) -> Any:
        data = str(params[0]["data"])
        if data == SELECTOR_SYMBOL:
            return _abi_string("bankr") if symbol is None else symbol
        if data == SELECTOR_DECIMALS:
            return f"0x{int(decimals):064x}" if isinstance(decimals, int) else decimals
        return ("error", -32000, f"selector desconocido: {data}")

    return _Nodo(eth_getCode=code, eth_call=erc20)


# --------------------------------------------------------------------------- #
# El camino que funciona
# --------------------------------------------------------------------------- #
async def test_resuelve_el_simbolo_y_los_decimales_del_contrato() -> None:
    """Se pregunta al contrato, y se pregunta **por esos dos selectores**.

    Se afirma sobre el calldata enviado y no sólo sobre el resultado: es lo que
    distingue «lo leyó de la cadena» de «lo acertó». Una tabla de símbolos
    conocidos daría el mismo resultado para este token y fallaría con el
    siguiente.
    """
    node = _token_node(decimals=18)
    token = await _lookup(node).by_address("base", BANKR)

    assert token.symbol == "bankr"
    assert token.decimals == 18
    assert token.chain == "base"
    # La dirección se guarda normalizada: en EVM el *checksum* es decorativo, y
    # el catálogo entero compara en minúsculas.
    assert token.address == BANKR.lower()
    assert node.eth_calls_to(BANKR) == [SELECTOR_SYMBOL, SELECTOR_DECIMALS]


async def test_una_direccion_sin_mayusculas_da_el_mismo_token() -> None:
    """La dirección se normaliza, y el contrato es el mismo con otra caja.

    En EVM el *checksum* EIP-55 es decorativo: la misma dirección escrita en
    minúsculas tiene que resolver al mismo token, no a «otro token» cuya
    dirección difiere en una letra.
    """
    token = await _lookup(_token_node(decimals=6)).by_address("base", BANKR.lower())

    assert token.address == BANKR.lower()
    assert token.decimals == 6


async def test_el_simbolo_de_un_bytes32_tambien_se_lee() -> None:
    """Unos pocos tokens antiguos publican el símbolo como `bytes32`, no `string`.

    MKR es el caso conocido. Leer sólo la forma moderna convertiría a esos tokens
    en irresolubles sin ninguna razón, y el fallo aparecería como «ese token no
    existe» habiendo existido siempre.
    """
    node = _token_node(symbol=_bytes32("MKR"), decimals=18)

    token = await _lookup(node).by_address("base", BANKR)

    assert token.symbol == "MKR"


async def test_un_simbolo_vacio_cae_a_la_direccion() -> None:
    """Un contrato sin símbolo se identifica por su dirección, no con un relleno.

    La dirección acortada es fea, pero es un identificador de verdad: apunta al
    contrato que se va a operar. Un `"?"` o un `"TOKEN"` de relleno parecería un
    nombre y acabaría en el diálogo de confirmación y en el registro.
    """
    node = _token_node(symbol=_abi_string(""), decimals=18)

    token = await _lookup(node).by_address("base", BANKR)

    assert token.symbol.startswith(BANKR[:6])
    assert token.symbol.endswith(BANKR[-4:])
    assert token.decimals == 18


# --------------------------------------------------------------------------- #
# Lo que no se acepta
# --------------------------------------------------------------------------- #
async def test_una_direccion_sin_contrato_se_dice_que_no_es_un_token() -> None:
    """Una cartera es una dirección válida y no es un token.

    Sin la comprobación del código, la respuesta sería el revert de `symbol()` y
    el mensaje hablaría de una llamada fallida, que manda a buscar el problema en
    la red en vez de en la dirección que se acaba de pegar.
    """
    node = _token_node(code="0x")

    with pytest.raises(SourceResponseError, match="no tiene contrato"):
        await _lookup(node).by_address("base", BANKR)

    assert node.eth_calls_to(BANKR) == [], "no se llegó a preguntar por el símbolo"


async def test_un_contrato_sin_decimals_no_se_adivina() -> None:
    """Sin decimales no se supone 18: se para.

    Es el fallo que este módulo existe para no cometer. Suponer los decimales no
    da un error en ningún sitio: da un importe escalado por 10^12 y una operación
    que se firma con un número que nadie ha visto.
    """
    node = _token_node(decimals="0x")

    with pytest.raises(SourceResponseError, match="decimals"):
        await _lookup(node).by_address("base", BANKR)


async def test_unos_decimales_absurdos_se_rechazan() -> None:
    """255 decimales es un contrato que contesta cualquier cosa, no un token.

    El límite no es una regla del estándar: es la comprobación de cordura que
    separa «un token con una escala inusual» de «una dirección que devuelve bytes
    porque no es lo que se cree que es».
    """
    node = _token_node(decimals=255)

    with pytest.raises(SourceResponseError, match="255 decimales"):
        await _lookup(node).by_address("base", BANKR)


async def test_un_revert_no_se_confunde_con_una_red_caida() -> None:
    """Que el contrato diga que no no es que no haya nodos.

    Son dos cosas con dos arreglos distintos —mirar la dirección, o mirar la
    conexión— y colapsarlas obligaría a quien lee el error a adivinar cuál fue.
    """
    node = _token_node(symbol=("error", 3, "execution reverted"))

    with pytest.raises(SourceResponseError, match="execution reverted"):
        await _lookup(node).by_address("base", BANKR)


async def test_una_red_sin_nodos_se_dice_antes_de_salir() -> None:
    """Sin nodos configurados para esa red, no hay contrato que leer.

    Se corta antes de intentarlo: el error del pool habla de endpoints y de
    `config.toml`, que es donde está el problema, y no de un token que no se pudo
    resolver.
    """
    node = _token_node()
    lookup = _lookup(node, chains=("ethereum",))

    with pytest.raises(SourceUnavailableError, match="nodos configurados"):
        await lookup.by_address("base", BANKR)

    assert node.calls == []


async def test_una_red_desconocida_se_rechaza() -> None:
    with pytest.raises(UnknownChainError):
        await _lookup(_token_node()).by_address("marte", BANKR)


async def test_en_solana_no_se_inventa_un_simbolo() -> None:
    """Fuera de EVM no hay `symbol()` que valga, y decirlo es la respuesta.

    La metadata de un token de Solana vive en su cuenta de acuñación, con otra
    codificación: devolver aquí un token «resuelto» sería inventarlo, y el error
    tiene que mandar a la salida que sí existe, que es cotizar el par conocido.
    """
    node = _token_node()
    lookup = _lookup(node, chains=("solana",))

    with pytest.raises(UnsupportedOperationError, match="EVM"):
        await lookup.by_address("solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

    assert node.calls == []


# --------------------------------------------------------------------------- #
# El símbolo no es un dato de confianza
# --------------------------------------------------------------------------- #
async def test_el_simbolo_no_puede_falsear_una_linea_ni_mover_el_cursor() -> None:
    """Lo que devuelve `symbol()` viene de un contrato que no controlamos.

    Y acaba en la lista de tokens, en el diálogo de confirmación y en el registro
    de ejecuciones, que es un fichero de texto con una línea por operación. Un
    símbolo con un salto de línea puede fabricar un renglón entero del registro, y
    uno con secuencias de escape puede mover el cursor de una terminal.
    """
    node = _token_node(symbol=_abi_string("US\nDC\x1b[31m\x00"), decimals=6)

    token = await _lookup(node).by_address("base", BANKR)

    assert token.symbol == "USDC[31m"
    assert all(char.isprintable() for char in token.symbol)


async def test_un_simbolo_larguisimo_se_acota() -> None:
    """El símbolo se copia a media docena de sitios: se acota una vez, aquí."""
    node = _token_node(symbol=_abi_string("X" * 400), decimals=6)

    token = await _lookup(node).by_address("base", BANKR)

    assert token.symbol == "X" * MAX_SYMBOL_LENGTH


async def test_una_longitud_mayor_que_la_respuesta_no_lee_fuera() -> None:
    """Un contrato puede declarar más bytes de los que envió.

    Se lee lo que hay en vez de confiar en el número declarado: fiarse de él sería
    dimensionar una lectura con un entero que elige la contraparte.
    """
    node = _token_node(symbol="0x" + f"{32:064x}" + f"{999:064x}" + "4142", decimals=6)

    token = await _lookup(node).by_address("base", BANKR)

    assert token.symbol == "AB"
