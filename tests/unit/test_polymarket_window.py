"""La ventana de cierre y el catálogo: lo que no se le confía a la fuente.

Lo que se fija aquí es que el filtro sea **correcto aunque la fuente no ayude**.
La ventana se pide a Gamma con parámetros —`order=endDate`, `end_date_min`,
`end_date_max`— y se vuelve a aplicar sobre lo que llega. Lo primero gasta el
`limit` en lo que cierra pronto; lo segundo es lo que hace que la lista sea
correcta. Un filtro que sólo confiara en los parámetros sería una promesa sobre
el comportamiento de un servidor ajeno, y el día que los ignorase la pestaña
mostraría mercados que cierran dentro de un año bajo el título «terminando».

Con el mismo criterio se prueba el resto del catálogo: el orden («más nuevas»,
«tendencia»), la categoría y las etiquetas. El orden y la categoría se piden
—`createdAt`, `volume24hr`, `tag_id`— y se vuelven a aplicar; la categoría,
además, sólo descarta a quien **consta** que no la lleva, porque una etiqueta
que no se pudo traer no es una etiqueta que no esté. Y las etiquetas en sí
viajan en una llamada de adorno —la fuente las publica en el evento, no en el
mercado— que cuando falla cuesta la etiqueta, nunca la lista.

La fuente está falseada, el motor no: se ejercita su traducción de verdad —los
decimales, las listas que vienen como cadena JSON, los mercados que se descartan
por liquidez— y no una versión simplificada que se comportaría distinto.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

import pytest

from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.models import MarketTag, PredictionSort
from amigocompora.engines.polymarket.engine import PolymarketEngine

AHORA = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

#: Dos etiquetas de verdad, con la forma que publica Gamma (el id, cadena).
POLITICA = {"id": "2", "label": "Politics", "slug": "politics"}
CRIPTO = {"id": "21", "label": "Crypto", "slug": "crypto"}
RECURRENTE = {"id": "100", "label": "Recurring", "slug": "recurring"}

#: Las mismas dos, ya traducidas al dominio: lo que se pasa y se espera.
POLITICA_MARCADA = MarketTag(tag_id="2", label="Politics", slug="politics")
CRIPTO_MARCADA = MarketTag(tag_id="21", label="Crypto", slug="crypto")


def _mercado(
    market_id: str,
    *,
    cierra_en: timedelta | None,
    liquidez: str = "50000",
    pregunta: str | None = None,
    creado_en: timedelta | None = None,
    volumen: str | None = None,
    volumen_24h: str | None = None,
    spread: str | None = None,
    cambio_1h: str | None = None,
    cambio_24h: str | None = None,
    cambio_1w: str | None = None,
    eventos: list[dict[str, Any]] | None = None,
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
    if creado_en is not None:
        entrada["createdAt"] = (AHORA + creado_en).isoformat().replace("+00:00", "Z")
    for clave, valor in (
        ("volumeNum", volumen),
        ("volume24hr", volumen_24h),
        ("spread", spread),
        ("oneHourPriceChange", cambio_1h),
        ("oneDayPriceChange", cambio_24h),
        ("oneWeekPriceChange", cambio_1w),
    ):
        if valor is not None:
            entrada[clave] = valor
    if eventos is not None:
        entrada["events"] = eventos
    return entrada


class FuenteFalsa:
    """Devuelve lo que se le da, y anota con qué parámetros se le pidió.

    `obedece` decide si hace el trabajo del servidor o si —como haría una API
    ajena el día que cambiase— ignora los parámetros y devuelve todo. Las dos
    ramas se prueban, porque la segunda es la que justifica filtrar aquí.

    Enruta por URL: las consultas a `/events` —las de las etiquetas— se
    contestan con `eventos`, filtrando por los `id` repetidos de la consulta
    (que van cosidos en la URL, no en `params`); `falla_eventos` simula el lote
    que la fuente rechaza.
    """

    def __init__(
        self,
        mercados: list[dict[str, Any]],
        *,
        obedece: bool,
        eventos: list[dict[str, Any]] | None = None,
        falla_eventos: bool = False,
    ) -> None:
        self._mercados = mercados
        self._obedece = obedece
        self._eventos = eventos or []
        self._falla_eventos = falla_eventos
        self.parametros: list[dict[str, str]] = []
        self.urls: list[str] = []

    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        self.urls.append(url)
        if "/events" in url:
            if self._falla_eventos:
                raise RuntimeError("la fuente rechazó el lote de eventos")
            consulta = url.split("?", 1)[1] if "?" in url else ""
            pedidos = {
                unquote(par.split("=", 1)[1])
                for par in consulta.split("&")
                if par.startswith("id=")
            }
            return [e for e in self._eventos if str(e["id"]) in pedidos]
        assert url.endswith("/markets"), url
        self.parametros.append(dict(params or {}))
        if not self._obedece:
            return self._mercados
        params = params or {}
        filas = self._mercados
        minimo = params.get("liquidity_num_min")
        if minimo is not None:
            # Como el servidor: el umbral de liquidez se aplica antes que nada.
            filas = [
                m for m in filas if float(m.get("liquidityNum") or 0) >= float(minimo)
            ]
        desde = params.get("end_date_min")
        hasta = params.get("end_date_max")
        if desde is None or hasta is None:
            return filas
        return [
            m
            for m in filas
            if m.get("endDate") is not None and desde <= m["endDate"] <= hasta
        ]


def _motor(
    mercados: list[dict[str, Any]],
    *,
    obedece: bool,
    eventos: list[dict[str, Any]] | None = None,
    falla_eventos: bool = False,
) -> tuple[PolymarketEngine, FuenteFalsa]:
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    fuente = FuenteFalsa(
        mercados, obedece=obedece, eventos=eventos, falla_eventos=falla_eventos
    )
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


# --------------------------------------------------------------------------- #
# El orden y la categoría: pedidos a la fuente y vueltos a aplicar
# --------------------------------------------------------------------------- #
async def test_newest_asks_the_source_for_the_creation_date() -> None:
    engine, fuente = _motor([_mercado("m1", cierra_en=timedelta(days=2))], obedece=True)
    await engine.markets(sort=PredictionSort.NEWEST)
    assert fuente.parametros[0]["order"] == "createdAt"
    assert fuente.parametros[0]["ascending"] == "false"


async def test_trending_asks_the_source_for_the_last_day_of_volume() -> None:
    engine, fuente = _motor([_mercado("m1", cierra_en=timedelta(days=2))], obedece=True)
    await engine.markets(sort=PredictionSort.TRENDING)
    assert fuente.parametros[0]["order"] == "volume24hr"
    assert fuente.parametros[0]["ascending"] == "false"


async def test_the_window_outranks_any_sort() -> None:
    """«Lo que está terminando» manda: la vista es la del reloj, no la pedida."""
    engine, fuente = _motor([_mercado("m1", cierra_en=timedelta(hours=3))], obedece=True)
    await engine.markets(closing_within=timedelta(days=1), sort=PredictionSort.NEWEST)
    assert fuente.parametros[0]["order"] == "endDate"
    assert fuente.parametros[0]["ascending"] == "true"


async def test_a_category_is_asked_by_the_identifier_the_source_uses() -> None:
    engine, fuente = _motor([_mercado("m1", cierra_en=timedelta(days=2))], obedece=True)
    await engine.markets(category=POLITICA_MARCADA)
    assert fuente.parametros[0]["tag_id"] == "2"


async def test_the_liquidity_threshold_is_asked_to_the_source() -> None:
    """El umbral viaja a la fuente: sin él, «más nuevas» se queda en cero filas.

    Medido el 2026-10-09 con datos reales: los cien mercados más nuevos no
    llegaban al umbral —recién creados, todavía sin liquidez—, así que el
    `limit` se gastaba entero en filas que el motor tira después y la vista
    quedaba vacía con el orden bien pedido. Pidiéndolo, el límite se gasta en
    las que sobreviven, y aquí se sigue aplicando por si la fuente lo ignora.
    """
    engine, fuente = _motor(
        [
            _mercado("seco", cierra_en=timedelta(days=2), liquidez="10"),
            _mercado("hondo", cierra_en=timedelta(days=2), liquidez="50000"),
        ],
        obedece=True,
    )
    found = await engine.markets(sort=PredictionSort.NEWEST, limit=10)
    assert fuente.parametros[0]["liquidity_num_min"] == "1000"
    assert [m.market_id for m in found] == ["hondo"]


async def test_the_newest_order_holds_even_if_the_source_ignores_it() -> None:
    """Pedirlo gasta el límite en lo que se quiere; aplicarlo lo hace correcto.

    La fuente devuelve las filas en su propio orden —como el día que ignore
    `order`—, y aun así la lista sale por fecha de creación descendente.
    """
    engine, fuente = _motor(
        [
            _mercado("viejo", cierra_en=timedelta(days=2), creado_en=timedelta(days=-40)),
            _mercado("sin_fecha", cierra_en=timedelta(days=2)),
            _mercado("nuevo", cierra_en=timedelta(days=2), creado_en=timedelta(days=-1)),
        ],
        obedece=False,
    )
    found = await engine.markets(sort=PredictionSort.NEWEST, limit=10)
    # Sin fecha conocida, al final: no consta que sea el más nuevo.
    assert [m.market_id for m in found] == ["nuevo", "viejo", "sin_fecha"]
    assert fuente.parametros[0]["order"] == "createdAt"


async def test_the_trending_order_holds_even_if_the_source_ignores_it() -> None:
    engine, _ = _motor(
        [
            _mercado("parado", cierra_en=timedelta(days=2), volumen_24h="10"),
            _mercado("sin_dato", cierra_en=timedelta(days=2)),
            _mercado("movido", cierra_en=timedelta(days=2), volumen_24h="900"),
        ],
        obedece=False,
    )
    found = await engine.markets(sort=PredictionSort.TRENDING, limit=10)
    assert [m.market_id for m in found] == ["movido", "parado", "sin_dato"]


async def test_the_category_filter_only_drops_what_it_knows_is_not() -> None:
    """Un mercado sin etiquetas se queda: no consta que no sea de la categoría.

    La fuente ignoró `tag_id` y devolvió todo —como una API ajena que cambia—,
    así que la única lista en la que se puede confiar es la que se comprueba
    con las etiquetas del evento. Y al que no se le pudo leer ninguna etiqueta
    no se le descarta: eso sería afirmar lo que no se sabe.
    """
    engine, _ = _motor(
        [
            _mercado("politico", cierra_en=timedelta(days=2), eventos=[{"id": 11}]),
            _mercado("cripto", cierra_en=timedelta(days=2), eventos=[{"id": 22}]),
            _mercado("sin_eventos", cierra_en=timedelta(days=2)),
        ],
        obedece=False,
        eventos=[
            {"id": 11, "tags": [POLITICA]},
            {"id": 22, "tags": [CRIPTO]},
        ],
    )
    found = await engine.markets(category=CRIPTO_MARCADA, limit=10)
    assert {m.market_id for m in found} == {"cripto", "sin_eventos"}


# --------------------------------------------------------------------------- #
# Las etiquetas, que viven en el evento
# --------------------------------------------------------------------------- #
async def test_tags_come_from_the_events_in_a_single_call() -> None:
    """El evento se pide una vez aunque lo compartan dos mercados.

    Los dos ids de un mercado caben en una llamada con `id` repetido —la coma
    la rechaza la fuente, medido el 2026-10-09—, y una etiqueta compartida por
    dos eventos se queda con su primera aparición, sin repetir el slug.
    """
    engine, fuente = _motor(
        [
            _mercado("m1", cierra_en=timedelta(days=2), eventos=[{"id": 11}]),
            _mercado("m2", cierra_en=timedelta(days=2), eventos=[{"id": 11}, {"id": 22}]),
        ],
        obedece=True,
        eventos=[
            {"id": 11, "tags": [POLITICA, RECURRENTE]},
            {"id": 22, "tags": [CRIPTO, RECURRENTE]},
        ],
    )
    found = await engine.markets(limit=10)
    por_id = {m.market_id: m.tags for m in found}
    (pedido,) = [url for url in fuente.urls if "/events" in url]
    assert "id=11&id=22" in pedido
    assert "limit=2" in pedido
    assert [t.slug for t in por_id["m1"]] == ["politics", "recurring"]
    assert [t.slug for t in por_id["m2"]] == ["politics", "recurring", "crypto"]


async def test_a_failed_tag_batch_costs_the_tags_not_the_list() -> None:
    """La etiqueta decora; si el lote falla, los mercados salen sin ella."""
    engine, fuente = _motor(
        [
            _mercado("m1", cierra_en=timedelta(days=2), eventos=[{"id": 11}]),
            _mercado("m2", cierra_en=timedelta(days=2), eventos=[{"id": 22}]),
        ],
        obedece=True,
        eventos=[{"id": 11, "tags": [POLITICA]}],
        falla_eventos=True,
    )
    found = await engine.markets(limit=10)
    assert [m.market_id for m in found] == ["m1", "m2"]
    assert all(m.tags == () for m in found)
    assert any("/events" in url for url in fuente.urls)


async def test_a_failed_tag_batch_keeps_every_row_of_a_category() -> None:
    """Sin etiquetas no consta que nadie sea de fuera: no se descarta a nadie."""
    engine, _ = _motor(
        [
            _mercado("a", cierra_en=timedelta(days=2), eventos=[{"id": 11}]),
            _mercado("b", cierra_en=timedelta(days=2), eventos=[{"id": 22}]),
            _mercado("c", cierra_en=timedelta(days=2)),
        ],
        obedece=False,
        falla_eventos=True,
    )
    found = await engine.markets(category=CRIPTO_MARCADA, limit=10)
    assert {m.market_id for m in found} == {"a", "b", "c"}


# --------------------------------------------------------------------------- #
# La actividad del mercado, traducida
# --------------------------------------------------------------------------- #
async def test_the_activity_fields_arrive_as_published() -> None:
    """La tendencia son deltas por participación, en unidades de precio.

    Medido el 2026-10-09 contra el histórico del libro: un mercado cuyo «sí»
    pasó de 0.445 a 0.235 publica `oneDayPriceChange = -0.21`, el delta exacto.
    """
    engine, _ = _motor(
        [
            _mercado(
                "m1",
                cierra_en=timedelta(days=2),
                creado_en=timedelta(days=-5),
                volumen="1234.5",
                volumen_24h="99",
                spread="0.02",
                cambio_1h="0.01",
                cambio_24h="-0.21",
                cambio_1w="0.35",
            )
        ],
        obedece=True,
    )
    (mercado,) = await engine.markets(limit=10)
    assert mercado.created_at == AHORA - timedelta(days=5)
    assert mercado.volume == Decimal("1234.5")
    assert mercado.volume_24h == Decimal("99")
    assert mercado.liquidity == Decimal("50000")
    assert mercado.spread == Decimal("0.02")
    assert mercado.price_change_1h == Decimal("0.01")
    assert mercado.price_change_24h == Decimal("-0.21")
    assert mercado.price_change_1w == Decimal("0.35")
    assert mercado.tags == ()


async def test_an_absent_activity_field_is_absent_not_zero() -> None:
    """Un cero afirmaría un dato que la fuente no dio; el hueco se dice (—)."""
    engine, _ = _motor([_mercado("m1", cierra_en=timedelta(days=2))], obedece=True)
    (mercado,) = await engine.markets(limit=10)
    assert mercado.created_at is None
    assert mercado.volume is None
    assert mercado.volume_24h is None
    assert mercado.spread is None
    assert mercado.price_change_1h is None
    assert mercado.price_change_24h is None
    assert mercado.price_change_1w is None


async def test_a_nonsense_activity_figure_is_dropped_not_the_market() -> None:
    """Un volumen negativo o un delta fuera de [-1, 1] no existen: fuera el dato.

    La actividad es para mirar: una fila mal publicada deja la celda en hueco y
    el mercado sigue en la lista, que es lo que la tabla necesita.
    """
    engine, _ = _motor(
        [
            _mercado(
                "m1",
                cierra_en=timedelta(days=2),
                volumen="-5",
                cambio_24h="2",
            )
        ],
        obedece=True,
    )
    (mercado,) = await engine.markets(limit=10)
    assert mercado.volume is None
    assert mercado.price_change_24h is None
