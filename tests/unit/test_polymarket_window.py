"""La ventana de cierre: «qué está terminando», sobre datos reales de la fuente.

Lo que se fija aquí es que el filtro sea **correcto aunque la fuente no ayude**.
La ventana se pide a Gamma con parámetros —`order=endDate`, `end_date_min`,
`end_date_max`— y se vuelve a aplicar sobre lo que llega. Lo primero gasta el
`limit` en lo que cierra pronto; lo segundo es lo que hace que la lista sea
correcta. Un filtro que sólo confiara en los parámetros sería una promesa sobre
el comportamiento de un servidor ajeno, y el día que los ignorase la pestaña
mostraría mercados que cierran dentro de un año bajo el título «terminando».

La fuente está falseada, el motor no: se ejercita su traducción de verdad —los
decimales, las listas que vienen como cadena JSON, los mercados que se descartan
por liquidez— y no una versión simplificada que se comportaría distinto.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from amigocompora.domain.clock import FrozenClock
from amigocompora.engines.polymarket.engine import PolymarketEngine

AHORA = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _mercado(
    market_id: str,
    *,
    cierra_en: timedelta | None,
    liquidez: str = "50000",
    pregunta: str | None = None,
) -> dict[str, Any]:
    """Un mercado con la forma exacta que publica Gamma."""
    entrada: dict[str, Any] = {
        "id": market_id,
        "question": pregunta or f"¿Ocurrirá lo {market_id}?",
        "closed": False,
        "active": True,
        "liquidityNum": liquidez,
        # Gamma manda estas dos como **cadena** que contiene JSON.
        "outcomes": '["Sí", "No"]',
        "outcomePrices": '["0.60", "0.40"]',
    }
    if cierra_en is not None:
        entrada["endDate"] = (AHORA + cierra_en).isoformat().replace("+00:00", "Z")
    return entrada


class FuenteFalsa:
    """Devuelve lo que se le da, y anota con qué parámetros se le pidió.

    `obedece` decide si hace el trabajo del servidor o si —como haría una API
    ajena el día que cambiase— ignora los parámetros y devuelve todo. Las dos
    ramas se prueban, porque la segunda es la que justifica filtrar aquí.
    """

    def __init__(self, mercados: list[dict[str, Any]], *, obedece: bool) -> None:
        self._mercados = mercados
        self._obedece = obedece
        self.parametros: list[dict[str, str]] = []

    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        assert url.endswith("/markets"), url
        self.parametros.append(dict(params or {}))
        if not self._obedece:
            return self._mercados
        desde = (params or {}).get("end_date_min")
        hasta = (params or {}).get("end_date_max")
        if desde is None or hasta is None:
            return self._mercados
        return [
            m
            for m in self._mercados
            if m.get("endDate") is not None and desde <= m["endDate"] <= hasta
        ]


def _motor(
    mercados: list[dict[str, Any]], *, obedece: bool
) -> tuple[PolymarketEngine, FuenteFalsa]:
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    fuente = FuenteFalsa(mercados, obedece=obedece)
    engine._source = fuente  # type: ignore[assignment]
    return engine, fuente


# --------------------------------------------------------------------------- #
# El orden que se pide
# --------------------------------------------------------------------------- #
async def test_without_a_window_it_asks_for_volume() -> None:
    """La vista normal sigue siendo la de los mercados que importan."""
    engine, fuente = _motor([_mercado("m1", cierra_en=timedelta(days=2))], obedece=True)
    await engine.markets()
    assert fuente.parametros[0]["order"] == "volumeNum"
    assert fuente.parametros[0]["ascending"] == "false"
    assert "end_date_min" not in fuente.parametros[0]


async def test_with_a_window_it_asks_for_the_clock_and_the_range() -> None:
    """Y la petición lleva la ventana, para no gastar el límite en lo lejano."""
    engine, fuente = _motor([_mercado("m1", cierra_en=timedelta(hours=6))], obedece=True)
    await engine.markets(closing_within=timedelta(days=1))
    params = fuente.parametros[0]
    assert params["order"] == "endDate"
    assert params["ascending"] == "true"
    assert params["end_date_min"] == "2026-06-01T12:00:00Z"
    assert params["end_date_max"] == "2026-06-02T12:00:00Z"


# --------------------------------------------------------------------------- #
# El filtro, que es lo que tiene que ser correcto
# --------------------------------------------------------------------------- #
async def test_the_window_leaves_out_what_closes_later() -> None:
    engine, _ = _motor(
        [
            _mercado("pronto", cierra_en=timedelta(hours=3)),
            _mercado("tarde", cierra_en=timedelta(days=90)),
        ],
        obedece=True,
    )
    encontrados = await engine.markets(closing_within=timedelta(days=1))
    assert [m.market_id for m in encontrados] == ["pronto"]


async def test_the_window_holds_even_if_the_source_ignores_the_parameters() -> None:
    """La prueba que justifica repetir el filtro aquí.

    Si la fuente devuelve todo —porque ignora los parámetros, porque cambia su
    API o porque un proxy los come—, la lista sigue siendo la de lo que cierra
    pronto. Es la diferencia entre un filtro y una confianza.
    """
    engine, fuente = _motor(
        [
            _mercado("tarde", cierra_en=timedelta(days=90)),
            _mercado("pronto", cierra_en=timedelta(hours=3)),
            _mercado("medio", cierra_en=timedelta(hours=30)),
        ],
        obedece=False,
    )
    encontrados = await engine.markets(closing_within=timedelta(days=1), limit=10)
    assert {m.market_id for m in encontrados} == {"pronto"}
    # Y consta que se le pidió la ventana, aunque no la respetara.
    assert "end_date_max" in fuente.parametros[0]


async def test_a_market_without_a_close_date_is_left_out_of_the_window() -> None:
    """No se puede afirmar que cierre pronto quien no dice cuándo cierra.

    Colarlo sería afirmarlo, y es justo lo que la vista promete: lo que está
    terminando. En la vista por volumen sí aparece, porque ahí no se afirma eso.
    """
    mercados = [
        _mercado("sin_fecha", cierra_en=None),
        _mercado("con_fecha", cierra_en=timedelta(hours=2)),
    ]
    engine, _ = _motor(mercados, obedece=False)

    con_ventana = await engine.markets(closing_within=timedelta(days=1), limit=10)
    assert [m.market_id for m in con_ventana] == ["con_fecha"]

    sin_ventana = await engine.markets(limit=10)
    assert {m.market_id for m in sin_ventana} == {"sin_fecha", "con_fecha"}


async def test_a_market_that_already_closed_is_not_closing_soon() -> None:
    """La fecha está en la ventana por detrás, no por delante."""
    engine, _ = _motor(
        [
            _mercado("cerrado", cierra_en=timedelta(hours=-1)),
            _mercado("abierto", cierra_en=timedelta(hours=1)),
        ],
        obedece=False,
    )
    encontrados = await engine.markets(closing_within=timedelta(days=1), limit=10)
    assert [m.market_id for m in encontrados] == ["abierto"]


async def test_the_window_and_the_search_apply_together() -> None:
    """Los dos filtros son independientes y se componen."""
    engine, _ = _motor(
        [
            _mercado("a", cierra_en=timedelta(hours=3), pregunta="¿Lloverá mañana?"),
            _mercado("b", cierra_en=timedelta(hours=3), pregunta="¿Ganará el Betis?"),
            _mercado("c", cierra_en=timedelta(days=40), pregunta="¿Lloverá en 2027?"),
        ],
        obedece=False,
    )
    encontrados = await engine.markets(
        closing_within=timedelta(days=1), search="llover", limit=10
    )
    assert [m.market_id for m in encontrados] == ["a"]


async def test_the_window_keeps_the_engines_other_discards() -> None:
    """Un mercado sin liquidez sigue fuera: la ventana no relaja lo demás."""
    engine, _ = _motor(
        [
            _mercado("seco", cierra_en=timedelta(hours=1), liquidez="10"),
            _mercado("hondo", cierra_en=timedelta(hours=1)),
        ],
        obedece=False,
    )
    encontrados = await engine.markets(closing_within=timedelta(days=1), limit=10)
    assert [m.market_id for m in encontrados] == ["hondo"]


@pytest.mark.parametrize("limite", [0, -1])
async def test_a_limit_of_zero_asks_nothing(limite: int) -> None:
    engine, fuente = _motor([_mercado("m1", cierra_en=timedelta(hours=1))], obedece=True)
    assert await engine.markets(limit=limite) == ()
    assert fuente.parametros == []
