"""Los tokens añadidos a mano se recuerdan entre sesiones.

Lo que se fija aquí es la promesa que el usuario pidió con sus palabras: pegar el
contrato de un token y que **aparezca en la lista**, la próxima vez también. Hasta
ahora lo resuelto vivía en el widget que lo pidió y se perdía al cerrar.

Los dos casos que más importan y que no se ven a simple vista:

- **Los decimales viajan exactos.** Son el dato que no produce error cuando está
  mal: produce un precio mil veces mayor o menor que parece correcto. Por eso hay
  una prueba que los compara, y no sólo que el símbolo vuelva.
- **Dos tokens homónimos conviven.** En Polygon el USDC nativo y el puenteado
  publican los dos `symbol() == "USDC"` —está medido— y son tokens distintos. Un
  almacén que dedujera por símbolo perdería uno de los dos.
"""

from __future__ import annotations

import json
from pathlib import Path

from amigocompora.domain.models import Token
from amigocompora.engines.token_store import UserTokenStore

#: Los dos USDC de Polygon, medidos: publican el mismo `symbol()` y son tokens
#: distintos. El nativo es la stablecoin de referencia de la red; el puenteado
#: desde Ethereum es el colateral que acepta Polymarket. Es el caso que obliga a
#: identificar un token por su dirección y no por su nombre.
USDC_NATIVO = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
USDC_PUENTEADO = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"


def _token(
    symbol: str = "PEPE",
    decimals: int = 18,
    *,
    chain: str = "base",
    address: str | None = "0x00000000000000000000000000000000000000aa",
) -> Token:
    return Token(symbol=symbol, decimals=decimals, chain=chain, address=address)


def test_a_saved_token_comes_back_in_a_new_store(tmp_path: Path) -> None:
    """El caso que motivó el módulo: cerrar y volver a abrir."""
    path = tmp_path / "tokens.json"
    UserTokenStore(path).add(_token())

    recovered = UserTokenStore(path).load()
    assert len(recovered) == 1
    assert recovered[0].symbol == "PEPE"
    assert recovered[0].chain == "base"
    assert recovered[0].address == "0x00000000000000000000000000000000000000aa"


def test_the_decimals_survive_exactly(tmp_path: Path) -> None:
    """El decimal mal guardado no da error: da un precio falso."""
    path = tmp_path / "tokens.json"
    # 6 decimales en una red donde casi todo tiene 18: si el almacén adivinara,
    # acertaría por casualidad con el caso común y fallaría justo aquí.
    UserTokenStore(path).add(_token("USDC", 6, chain="polygon"))

    assert UserTokenStore(path).load()[0].decimals == 6


def test_two_tokens_with_the_same_symbol_are_both_kept(tmp_path: Path) -> None:
    """El USDC nativo y el puenteado de Polygon: mismo símbolo, distinto token."""
    path = tmp_path / "tokens.json"
    store = UserTokenStore(path)
    store.add(_token("USDC", 6, chain="polygon", address=USDC_NATIVO))
    store.add(_token("USDC", 6, chain="polygon", address=USDC_PUENTEADO))

    recovered = UserTokenStore(path).load()
    assert len(recovered) == 2
    assert {token.address for token in recovered} == {USDC_NATIVO, USDC_PUENTEADO}


def test_adding_the_same_token_twice_does_not_duplicate_it(tmp_path: Path) -> None:
    store = UserTokenStore(tmp_path / "tokens.json")
    # El mismo token en otra caja: el *checksum* EIP-55 es presentación, no
    # identidad, así que no puede producir un renglón de más.
    store.add(_token("PEPE", 18, address="0x00000000000000000000000000000000000000AA"))
    store.add(_token("PEPE", 18, address="0x00000000000000000000000000000000000000aa"))

    assert len(store.load()) == 1


def test_readding_with_other_decimals_corrects_the_saved_row(tmp_path: Path) -> None:
    """Un decimal viejo y equivocado en disco se corrige, no se conserva."""
    path = tmp_path / "tokens.json"
    UserTokenStore(path).add(_token("PEPE", 9))
    UserTokenStore(path).add(_token("PEPE", 18))

    recovered = UserTokenStore(path).load()
    assert len(recovered) == 1
    assert recovered[0].decimals == 18


def test_forgetting_removes_only_the_named_contract(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    store = UserTokenStore(path)
    store.add(_token("USDC", 6, chain="polygon", address=USDC_NATIVO))
    store.add(_token("USDC", 6, chain="polygon", address=USDC_PUENTEADO))

    # Con el *checksum* en mayúsculas, que es como se copia de un explorador: si
    # el borrado comparara letra a letra, no encontraría el que sí está.
    assert store.forget("polygon", "0x2791BCA1f2DE4661ED88A30C99A7a9449Aa84174") is True
    left = UserTokenStore(path).load()
    assert [token.address for token in left] == [USDC_NATIVO]


def test_forgetting_something_that_was_not_there_says_so(tmp_path: Path) -> None:
    assert UserTokenStore(tmp_path / "tokens.json").forget("base", "0x" + "1" * 40) is False


def test_a_broken_file_is_read_as_empty_and_does_not_raise(tmp_path: Path) -> None:
    """Un JSON a medio escribir cuesta un token, no la aplicación."""
    path = tmp_path / "tokens.json"
    path.write_text('{"symbol": "PEPE", "dec', encoding="utf-8")

    assert UserTokenStore(path).load() == ()
    # Y se puede seguir añadiendo encima: el fichero roto se reescribe entero.
    store = UserTokenStore(path)
    store.add(_token())
    assert len(UserTokenStore(path).load()) == 1


def _nativo_ok() -> dict[str, object]:
    return {
        "symbol": "BUENO",
        "decimals": 8,
        "chain": "base",
        "address": "0x00000000000000000000000000000000000000ff",
    }


def test_a_row_that_is_not_a_token_is_skipped_without_losing_the_others(
    tmp_path: Path,
) -> None:
    """La lista se edita a mano alguna vez; un renglón malo no es la lista entera."""
    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            [
                {"symbol": "", "decimals": 18, "chain": "base", "address": "0xaa"},
                {"symbol": "SINDEC", "chain": "base", "address": "0xbb"},
                {"symbol": "NATIVO", "decimals": 18, "chain": "base", "address": None},
                _nativo_ok(),
            ]
        ),
        encoding="utf-8",
    )

    recovered = UserTokenStore(path).load()
    assert [token.symbol for token in recovered] == ["BUENO"]


def test_a_token_without_address_is_refused_rather_than_half_saved(tmp_path: Path) -> None:
    """El nativo no tiene contrato, y guardarlo lo haría desaparecer en silencio.

    El modo de fallo que se evita es concreto: `Token` admite `address=None`, así
    que sin el filtro el renglón se escribiría y la lectura siguiente lo
    descartaría sola. El token estaría en la lista durante esta sesión y no en la
    próxima, sin que nada explicara por qué — que es la peor forma de perder un
    dato.
    """
    path = tmp_path / "tokens.json"
    store = UserTokenStore(path)

    assert store.add(_token("ETH", 18, address=None)) is False
    assert store.load() == ()
    assert UserTokenStore(path).load() == ()
