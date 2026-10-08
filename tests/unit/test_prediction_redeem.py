"""El cobro de una posición ya resuelta: la codificación y las negativas.

Lo que se fija aquí es dónde se pierde dinero al cobrar, y son tres sitios:

1. **El selector y la codificación del `calldata`.** Un selector equivocado llama
   a otra función del mismo contrato, o revierte. La prueba del selector lo clava
   contra el valor derivado de la firma **escrita a mano aquí**, y la del
   `calldata` compara la cadena entera contra una construida en la prueba a
   partir de la especificación ABI, no de la implementación. Cuando el selector
   se escribió de memoria salió mal —`0x9e7212ad` frente al real `0x01b7037c`—,
   que es exactamente el fallo que estas dos pruebas existen para no dejar pasar.
2. **Cobrar lo que no se puede.** Un mercado sin resolver, uno que comparte
   colateral y no tiene camino medido, o una posición de otro recinto producen
   todos una transacción que revierte y quema gas. Cada uno tiene su negativa.
3. **Agrupar mercados en silencio.** El contrato cobra un mercado por llamada.
   Devolver la transacción de uno cuando se pidieron dos dejaría el segundo sin
   cobrar sin que nadie se entere, así que se rechaza.

La fuente está falseada y el motor no: se ejercita su traducción de verdad —el
booleano que decide contra qué contrato se cobra, el tamaño que hay que leer— y
no una versión simplificada que se comportaría distinto.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from eth_utils.crypto import keccak

from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import ExecutionError, InvalidAmountError
from amigocompora.domain.models import PredictionPosition, Venue, VenueKind
from amigocompora.engines.polymarket.engine import PolymarketEngine
from amigocompora.engines.polymarket.orders import (
    CONDITIONAL_TOKENS,
    SELECTOR_REDEEM,
    build_redeem_calldata,
)

AHORA = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

#: Un `conditionId` real tiene esta forma: 32 bytes en hexadecimal. Se escribe
#: aquí entero y a mano porque es lo que se codifica, y sacarlo de la fuente
#: dejaría la prueba comprobándose a sí misma.
MERCADO = "0x" + "ab" * 32

#: Las direcciones contra las que se firma, en minúsculas y escritas a mano.
COLATERAL_ESPERADO = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"

#: El `tokenId` de cada resultado dentro del mercado. Es el número entero que el
#: contrato condicional calcula a partir de la condición y del índice, y tiene
#: esta pinta. El `noqa` silencia un falso positivo: la regla lee «TOKEN» como si
#: fuera un secreto, y esto es un identificador público que va en la cadena.
TOKEN_SI = "71321045679252212594626385532706912750332728571942532289631379312455583992563"  # noqa: S105
TOKEN_NO = "52114319501245915516055105976744304775366685471852918604325386120244825471694"  # noqa: S105

#: La firma de la función, transcrita de la especificación del contrato
#: condicional. Es la fuente del selector: el nombre va aquí para que un cambio
#: en el módulo no pueda arrastrar consigo el número.
FIRMA = "redeemPositions(address,bytes32,bytes32,uint256[])"


def _palabra(hex_sin_prefijo: str) -> str:
    """Una palabra ABI: lo que se le dé, rellenado a 32 bytes por la izquierda."""
    return hex_sin_prefijo.rjust(64, "0")


def _esperado(condition_id: str, *, index_sets: tuple[int, ...] = (1, 2)) -> str:
    """El `calldata` según la especificación, construido aquí desde cero.

    Se arma con las piezas literales en vez de con las funciones del producto:
    los tipos dinámicos van **fuera de línea** —la cabecera lleva un
    desplazamiento a la cola donde está el array— y esa es justo la parte que una
    implementación puede equivocar sin que nada más se queje.
    """
    cabecera = (
        FIRMA_ESPERADA
        + _palabra(COLATERAL_ESPERADO[2:])
        + _palabra("")
        + _palabra(condition_id[2:])
        + _palabra(f"{4 * 32:x}")
    )
    cola = _palabra(f"{len(index_sets):x}") + "".join(
        _palabra(f"{valor:x}") for valor in index_sets
    )
    return "0x" + cabecera + cola


#: El selector, **escrito a mano desde la firma de arriba**. Si el módulo
#: cambiara de función, esto no cambiaría con él.
FIRMA_ESPERADA = "01b7037c"


# --------------------------------------------------------------------------- #
# La codificación
# --------------------------------------------------------------------------- #
def test_el_selector_es_el_de_la_funcion_del_contrato() -> None:
    """`redeemPositions(address,bytes32,bytes32,uint256[])` y no otra cosa.

    Es la comprobación que atrapó un selector recordado de memoria que estaba
    mal. El valor de la derecha es el keccak de la firma transcrita en `FIRMA`,
    así que lo que se afirma es que el módulo y la especificación hablan de la
    misma función.
    """
    derivado = "0x" + keccak(text=FIRMA)[:4].hex()
    assert derivado == SELECTOR_REDEEM
    assert "0x" + FIRMA_ESPERADA == SELECTOR_REDEEM


def test_el_calldata_lleva_la_cabecera_y_la_cola_del_array() -> None:
    """Los cuatro fijos en la cabecera y el array colgando del desplazamiento."""
    assert build_redeem_calldata(MERCADO) == _esperado(MERCADO)


def test_el_calldata_declara_los_dos_resultados() -> None:
    """Se piden los dos índices, para no tener que saber cuál se posee.

    Es lo que hace que una posición que ya no esté queme cero en vez de revertir:
    el contrato recorre los índices, mira el saldo y quema lo que haya.
    """
    calldata = build_redeem_calldata(MERCADO)
    assert calldata.endswith(_palabra("2") + _palabra("1") + _palabra("2"))


def test_el_colateral_es_el_puenteado_y_no_el_nativo() -> None:
    """Los dos publican `symbol() == "USDC"` y sólo uno es el colateral.

    Se comprueba sobre la palabra del `calldata` y no sobre la constante del
    módulo, porque es la palabra la que llega al contrato.
    """
    calldata = build_redeem_calldata(MERCADO)
    # Se salta el prefijo `0x` y el selector —4 bytes, 8 dígitos— para leer la
    # primera palabra de la cabecera, que es la del colateral.
    palabra_colateral = calldata[2 + 8 : 2 + 8 + 64]
    assert palabra_colateral == _palabra(COLATERAL_ESPERADO[2:])
    # Y no es la del USDC nativo de Polygon, que es otra dirección.
    assert palabra_colateral != _palabra("3c499c542cef5e3811e1192ce70d8cc03d5c3359")


def test_se_acepta_el_identificador_con_y_sin_prefijo() -> None:
    """El dominio lo guarda como llega; el contrato no distingue."""
    assert build_redeem_calldata(MERCADO) == build_redeem_calldata(MERCADO[2:])


def test_un_identificador_que_no_mide_32_bytes_se_rechaza() -> None:
    """Rellenarlo produciría una llamada contra un mercado que no existe."""
    with pytest.raises(ExecutionError, match="no mide 32 bytes"):
        build_redeem_calldata("0x" + "ab" * 16)


def test_un_identificador_que_no_es_hexadecimal_se_rechaza() -> None:
    """El mismo mensaje que da `word_address` para una dirección mal formada."""
    with pytest.raises(ExecutionError, match="no es hexadecimal"):
        build_redeem_calldata("0x" + "zz" * 32)


def test_un_cobro_sin_conjuntos_de_indices_no_se_construye() -> None:
    """Sin índices no se dice qué resultados quemar."""
    with pytest.raises(ExecutionError, match="conjuntos de índices"):
        build_redeem_calldata(MERCADO, index_sets=())


def test_el_destino_es_el_contrato_condicional() -> None:
    """Las participaciones viven ahí: se le pide a él que las queme."""
    assert CONDITIONAL_TOKENS.lower() == "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"


# --------------------------------------------------------------------------- #
# La lectura de posiciones
# --------------------------------------------------------------------------- #
def _posicion(
    *,
    condition_id: str = MERCADO,
    size: str = "12.5",
    redeemable: bool = True,
    neg_risk: bool | None = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Una posición con la forma exacta que publica la fuente."""
    fila: dict[str, Any] = {
        "conditionId": condition_id,
        "asset": TOKEN_SI,
        "size": size,
        "title": "¿Ocurrirá lo medido?",
        "outcome": "Sí",
        "redeemable": redeemable,
        "negativeRisk": neg_risk,
        "curPrice": "0.99",
    }
    fila.update(extra or {})
    return fila


class FuenteFalsa:
    """Devuelve lo que se le da, y anota con qué parámetros se le pidió."""

    def __init__(self, filas: list[dict[str, Any]]) -> None:
        self._filas = filas
        self.parametros: list[dict[str, str]] = []

    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        assert url.endswith("/positions"), url
        self.parametros.append(dict(params or {}))
        return self._filas


def _motor(filas: list[dict[str, Any]]) -> tuple[PolymarketEngine, FuenteFalsa]:
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    fuente = FuenteFalsa(filas)
    engine._source = fuente  # type: ignore[assignment]
    return engine, fuente


async def test_las_posiciones_se_leen_con_la_cartera_y_el_filtro() -> None:
    """El filtro va al servidor: una cartera con historial tiene cientos."""
    engine, fuente = _motor([_posicion()])
    await engine.positions(wallet="0xcartera", redeemable_only=True)
    assert fuente.parametros[0]["user"] == "0xcartera"
    assert fuente.parametros[0]["sizeThreshold"] == "0"
    assert fuente.parametros[0]["redeemable"] == "true"


async def test_sin_solo_cobrables_no_se_manda_el_filtro() -> None:
    """No se pide un filtro que no se quiere: el servidor no siempre lo honra."""
    engine, fuente = _motor([_posicion()])
    await engine.positions(wallet="0xcartera")
    assert "redeemable" not in fuente.parametros[0]


async def test_una_posicion_se_traduce_entera() -> None:
    """Los campos que deciden si se puede cobrar llegan todos."""
    engine, _ = _motor([_posicion(size="12.5")])
    (posicion,) = await engine.positions(wallet="0xcartera")
    assert posicion.condition_id == MERCADO
    assert posicion.outcome_label == "Sí"
    assert posicion.shares == Decimal("12.5")
    assert posicion.redeemable is True
    assert posicion.neg_risk is False
    assert posicion.cur_price == Decimal("0.99")
    # La fuente es una foto: el instante es el de la lectura, no una fecha del
    # payload que hablaría de otra cosa.
    assert posicion.observed_at == AHORA
    assert posicion.venue.venue_id == "polymarket"


async def test_un_mercado_sin_decir_si_comparte_colateral_queda_en_duda() -> None:
    """`negativeRisk` ausente es `None`, no `False`.

    La diferencia decide contra qué contrato se cobra, y afirmar «no comparte
    colateral» porque la fuente calló es la suposición que el dominio se niega a
    hacer. Con `None`, el dominio bloquea el cobro y lo dice.
    """
    fila = _posicion()
    del fila["negativeRisk"]
    engine, _ = _motor([fila])
    (posicion,) = await engine.positions(wallet="0xcartera")
    assert posicion.neg_risk is None
    assert not posicion.is_redeemable


async def test_una_fila_sin_mercado_no_es_una_posicion() -> None:
    """Sin `conditionId` no hay nada que cobrar ni nada que decir."""
    fila = _posicion()
    del fila["conditionId"]
    engine, _ = _motor([fila])
    assert await engine.positions(wallet="0xcartera") == ()


async def test_una_posicion_sin_identificador_de_resultado_se_dice() -> None:
    """Pero una posición **existente** a la que le falta un dato, no se tira.

    Descartarla en silencio dejaría la pantalla diciendo «no tienes nada que
    cobrar» mientras la cartera sí tiene algo, y el usuario no tendría forma de
    saber que el problema es que la fuente cambió de formato.
    """
    fila = _posicion()
    del fila["asset"]
    engine, _ = _motor([fila])
    with pytest.raises(ExecutionError, match="identificador del resultado"):
        await engine.positions(wallet="0xcartera")


async def test_una_posicion_de_tamano_ilegible_se_dice() -> None:
    """El tamaño es el importe que se cobra: sin él no hay posición."""
    engine, _ = _motor([_posicion(size="cero")])
    with pytest.raises(ExecutionError, match="el tamaño"):
        await engine.positions(wallet="0xcartera")


# --------------------------------------------------------------------------- #
# Construir el cobro
# --------------------------------------------------------------------------- #
def _entidad(
    *,
    condition_id: str = MERCADO,
    shares: str = "12.5",
    redeemable: bool = True,
    neg_risk: bool | None = False,
    venue: Venue | None = None,
    token_id: str = TOKEN_SI,
    outcome_label: str = "Sí",
) -> PredictionPosition:
    return PredictionPosition(
        venue=venue
        or Venue(
            venue_id="polymarket",
            name="Polymarket",
            kind=VenueKind.PREDICTION_MARKET,
            chain="polygon",
        ),
        condition_id=condition_id,
        question="¿Ocurrirá lo medido?",
        outcome_label=outcome_label,
        token_id=token_id,
        shares=Decimal(shares),
        observed_at=AHORA,
        redeemable=redeemable,
        neg_risk=neg_risk,
    )


def test_el_cobro_va_contra_el_contrato_condicional_sin_mandar_valor() -> None:
    """Cobrar no transfiere nada consigo: el gas se paga aparte."""
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    tx = engine.build_redeem([_entidad()], wallet="0xcartera")
    assert tx.to_address == CONDITIONAL_TOKENS
    assert tx.calldata == build_redeem_calldata(MERCADO)
    assert tx.chain_id == 137
    assert tx.value.raw == 0
    assert "12.5" in tx.description


def test_un_cobro_sin_posiciones_no_es_un_cobro() -> None:
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    with pytest.raises(ExecutionError, match="sin posiciones"):
        engine.build_redeem([], wallet="0xcartera")


def test_dos_mercados_en_una_transaccion_se_rechazan() -> None:
    """El contrato cobra un mercado por llamada.

    Agrupar por dentro devolvería la transacción de uno y dejaría el otro sin
    cobrar en silencio, que es la forma de perder dinero sin que se note.
    """
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    with pytest.raises(ExecutionError, match="mercados distintos"):
        engine.build_redeem(
            [_entidad(), _entidad(condition_id="0x" + "cd" * 32)], wallet="0xcartera"
        )


def test_un_mercado_sin_resolver_no_se_cobra() -> None:
    """Cobrar antes de que resuelva revierte y quema gas."""
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    with pytest.raises(ExecutionError, match="todavía no ha resuelto"):
        engine.build_redeem([_entidad(redeemable=False)], wallet="0xcartera")


def test_un_mercado_que_comparte_colateral_no_se_cobra() -> None:
    """Su camino pasa por un adaptador que **no está medido** en este programa.

    Se rechaza en vez de rellenar el hueco con una dirección copiada de la
    documentación: firmar contra una suposición es lo que este módulo existe para
    no hacer, y un cobro contra el contrato equivocado quema gas sin devolver
    nada.
    """
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    with pytest.raises(ExecutionError, match="comparte colateral"):
        engine.build_redeem([_entidad(neg_risk=True)], wallet="0xcartera")


def test_una_posicion_sin_saber_si_comparte_colateral_no_se_cobra() -> None:
    """`None` no es `False`: sin el dato no se sabe contra qué contrato firmar."""
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    with pytest.raises(ExecutionError, match="no consta"):
        engine.build_redeem([_entidad(neg_risk=None)], wallet="0xcartera")


def test_dos_resultados_del_mismo_mercado_se_cobran_juntos() -> None:
    """Es la razón de que el método tome una lista y no una posición.

    Pasa de verdad: una cartera puede tener participaciones de los dos lados del
    mismo mercado, y son dos filas con el mismo `conditionId` y distinto
    `tokenId`. Cobrarlas por separado costaría dos veces el gas de la misma
    llamada.
    """
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    tx = engine.build_redeem(
        [
            _entidad(shares="1", token_id=TOKEN_SI),
            _entidad(shares="2", token_id=TOKEN_NO, outcome_label="No"),
        ],
        wallet="0xcartera",
    )
    # El importe de la descripción es la suma, no la última: se cobra todo.
    assert "3" in tx.description
    assert tx.calldata == build_redeem_calldata(MERCADO)


def test_una_posicion_de_otro_recinto_no_se_cobra() -> None:
    """Un cobro contra otro recinto es una transacción contra un contrato ajeno."""
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    otro = Venue(
        venue_id="otro",
        name="Otro",
        kind=VenueKind.PREDICTION_MARKET,
        chain="polygon",
    )
    with pytest.raises(ExecutionError, match="otro recinto"):
        engine.build_redeem([_entidad(venue=otro)], wallet="0xcartera")


def test_una_posicion_de_otra_red_no_se_cobra() -> None:
    """No se firma en una red donde el contrato condicional no existe."""
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    otra = Venue(
        venue_id="polymarket",
        name="Polymarket",
        kind=VenueKind.PREDICTION_MARKET,
        chain="base",
    )
    with pytest.raises(ExecutionError, match="viven en"):
        engine.build_redeem([_entidad(venue=otra)], wallet="0xcartera")


def test_un_cobro_sin_cartera_no_se_construye() -> None:
    """Sin saber de quién son las participaciones no se sabe qué quemar."""
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    with pytest.raises(ExecutionError, match="a qué cartera"):
        engine.build_redeem([_entidad()], wallet="")


# --------------------------------------------------------------------------- #
# La entidad
# --------------------------------------------------------------------------- #
def test_una_posicion_de_cero_participaciones_no_es_una_posicion() -> None:
    with pytest.raises(InvalidAmountError, match="cero participaciones"):
        _entidad(shares="0")


def test_una_posicion_sin_mercado_no_se_puede_cobrar() -> None:
    with pytest.raises(InvalidAmountError, match="sin el identificador del mercado"):
        _entidad(condition_id="")


def test_un_precio_fuera_de_cero_uno_no_es_un_precio() -> None:
    """Una participación vale entre 0 y 1: fuera de ahí el dato está mal."""
    with pytest.raises(InvalidAmountError, match=r"\[0, 1\]"):
        PredictionPosition(
            venue=_entidad().venue,
            condition_id=MERCADO,
            question="¿?",
            outcome_label="Sí",
            token_id=TOKEN_SI,
            shares=Decimal("1"),
            observed_at=AHORA,
            cur_price=Decimal("1.5"),
        )
