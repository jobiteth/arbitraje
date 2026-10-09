"""El mercado de una posición: de un `conditionId` al mercado que se puede operar.

Cargar una posición en la tarjeta de orden exige volver del contrato al mercado
entero —con su tick y su tamaño mínimo—, y esa vuelta la da Gamma filtrando por
`condition_ids` (medido el 2026-10-09 contra el mercado real 665374: una fila, el
mercado correcto). Lo que se fija aquí es la traducción de esa respuesta y lo que
se hace cuando no trae nada operable.

La fuente está falseada, el motor no: se ejercita su `_to_market` de verdad —con
sus descartes por cerrado, sin liquidez o sin precios—, que es lo que decide qué
es «un mercado que se puede operar».
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import EngineNotFoundError
from amigocompora.engines.polymarket.engine import PolymarketEngine

AHORA = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
CONDICION = "0x" + "ab" * 32


def _mercado(*, condicion: str = CONDICION) -> dict[str, Any]:
    """Un mercado con la forma exacta que publica Gamma, y operable."""
    return {
        "id": "665374",
        "question": "¿Ocurrirá lo medido?",
        "closed": False,
        "active": True,
        "liquidityNum": "50000",
        "conditionId": condicion,
        # Gamma manda estas dos como **cadena** que contiene JSON.
        "outcomes": '["Sí", "No"]',
        "outcomePrices": '["0.60", "0.40"]',
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "5",
        "endDate": "2027-01-01T00:00:00Z",
    }


class FuenteFalsa:
    """Devuelve lo que se le da, y anota con qué parámetros se le pidió."""

    def __init__(self, filas: list[dict[str, Any]]) -> None:
        self._filas = filas
        self.parametros: list[dict[str, str]] = []

    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        assert url.endswith("/markets"), url
        self.parametros.append(dict(params or {}))
        return self._filas


def _motor(filas: list[dict[str, Any]]) -> tuple[PolymarketEngine, FuenteFalsa]:
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    fuente = FuenteFalsa(filas)
    engine._source = fuente  # type: ignore[assignment]
    return engine, fuente


async def test_la_condicion_va_al_servidor_y_el_mercado_vuelve_entero() -> None:
    """Se filtra por el contrato y no por el nombre: el nombre se repite."""
    engine, fuente = _motor([_mercado()])
    market = await engine.market_by_condition(CONDICION)
    assert fuente.parametros[0] == {"condition_ids": CONDICION}
    assert market.market_id == "665374"
    assert market.condition_id == CONDICION
    assert market.question == "¿Ocurrirá lo medido?"
    # El tick y el mínimo son lo que la tarjeta de orden necesita de vuelta: sin
    # ellos no se puede proponer un precio ni un tamaño válidos.
    assert market.tick_size is not None
    assert market.min_order_size is not None


async def test_sin_filas_no_hay_mercado_que_cargar() -> None:
    """Una condición que ya no está se dice con el motivo, no con una lista vacía."""
    engine, _ = _motor([])
    with pytest.raises(EngineNotFoundError, match="condición"):
        await engine.market_by_condition(CONDICION)


async def test_un_mercado_que_ya_no_se_opera_no_cuenta() -> None:
    """Resuelto o cerrado se descarta igual que en la búsqueda: aquí se viene a operar."""
    fila = _mercado()
    fila["closed"] = True
    engine, _ = _motor([fila])
    with pytest.raises(EngineNotFoundError, match="condición"):
        await engine.market_by_condition(CONDICION)


async def test_una_fila_ilegible_antes_de_la_buena_no_estorba() -> None:
    """La respuesta es una lista y puede traer ruido delante; se salta, no se cae."""
    engine, _ = _motor([{"id": "", "question": ""}, _mercado()])
    market = await engine.market_by_condition(CONDICION)
    assert market.condition_id == CONDICION
