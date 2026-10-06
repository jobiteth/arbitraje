"""Motor de mercados de predicción sobre la API pública de Polymarket.

Lee los mercados abiertos reales y sus precios reales. En un mercado de
predicción el precio de un resultado **es** su probabilidad implícita, así que
el análisis que pide el spec —probabilidades implícitas y discrepancias— se
calcula sobre el dato tal cual llega, sin transformarlo.

Dos cosas que conviene saber al leer la tabla que produce:

- La suma de probabilidades de un mercado binario de Polymarket sale casi
  siempre muy cerca de 100 %, porque la propia plataforma publica los dos
  precios como complementarios. Las desviaciones interesantes aparecen entre
  mercados del mismo evento (los `negRisk`, varios candidatos a lo mismo), no
  dentro de un mercado binario. El `overround_bps` del dominio mide lo primero;
  comparar entre mercados relacionados es trabajo del caso de uso.
- Se piden sólo mercados **abiertos** y ordenados por volumen. Un mercado
  cerrado tiene precios congelados en 0 y 1, y mezclarlos con los abiertos
  llenaría la tabla de probabilidades del 100 % que no son una predicción de
  nada.

Sin API key: la API Gamma es pública y de sólo lectura.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

import structlog

from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import EngineNotFoundError
from amigocompora.domain.models import (
    MarketOutcome,
    PredictionMarket,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.http_source import (
    JsonSource,
    as_mapping,
    as_sequence,
    optional_decimal,
)

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "Polymarket"
HOST: Final = "gamma-api.polymarket.com"
API_ROOT: Final = f"https://{HOST}"

#: Polymarket liquida en Polygon. La red va en el venue para que la UI pueda
#: decir en qué cadena vive lo que está mirando. Es la **clave** del registro de
#: redes, no un chain id numérico: ver `domain.chains`.
SETTLEMENT_CHAIN: Final = "polygon"

MANIFEST: Final = EngineManifest(
    engine_id="polymarket",
    name="Polymarket — mercados reales",
    version="1.0.0",
    kind=EngineKind.PREDICTION_MARKETS,
    summary=(
        "Mercados de predicción abiertos de Polymarket, con sus precios reales "
        "leídos de la API pública Gamma. El precio de cada resultado es su "
        "probabilidad implícita."
    ),
    capabilities=frozenset({Capability.READ_CHAIN}),
    required_config=(),
    allowed_hosts=(HOST,),
)

VENUE: Final = Venue(
    venue_id="polymarket",
    name="Polymarket",
    kind=VenueKind.PREDICTION_MARKET,
    chain=SETTLEMENT_CHAIN,
)

#: Liquidez mínima para que un mercado entre en la tabla. Un mercado sin
#: liquidez tiene un precio que no refleja ninguna opinión agregada: es el
#: último precio que alguien puso, y leerlo como probabilidad engaña.
MIN_LIQUIDITY_USD: Final = Decimal("1000")

#: Cuántos mercados pedir de más para compensar los que se descarten por
#: liquidez o por formato, y seguir devolviendo `limit` filas útiles.
_OVERFETCH: Final = 3
_MAX_FETCH: Final = 200


class PolymarketEngine:
    """Mercados de predicción reales, de sólo lectura."""

    __slots__ = ("_clock", "_source")

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        ttl_seconds: float = 20.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._clock = clock or SystemClock()
        self._source = JsonSource(
            name=SOURCE_NAME,
            allowed_hosts=MANIFEST.allowed_hosts,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            min_interval_seconds=0.3,
        )

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    async def aopen(self) -> None:
        await self._source.aopen()

    async def aclose(self) -> None:
        await self._source.aclose()

    # ----------------------------------------------------------------- #
    # Consultas
    # ----------------------------------------------------------------- #
    async def markets(
        self,
        *,
        limit: int = 20,
        search: str | None = None,
    ) -> Sequence[PredictionMarket]:
        """Mercados abiertos, los de más volumen primero.

        El filtro por texto se aplica en cliente sobre la pregunta del mercado:
        así el comportamiento del buscador no depende de parámetros de la API
        que no estén documentados, y es idéntico para cualquier fuente futura.
        """
        if limit <= 0:
            return ()

        needle = (search or "").strip().lower()
        # Con búsqueda hay que traer bastante más, porque el filtro cae después.
        requested = min(limit * (_OVERFETCH * 4 if needle else _OVERFETCH), _MAX_FETCH)
        payload = await self._source.get_json(
            f"{API_ROOT}/markets",
            params={
                "closed": "false",
                "active": "true",
                "limit": str(requested),
                "order": "volumeNum",
                "ascending": "false",
            },
        )

        observed_at = self._clock.now()
        found: list[PredictionMarket] = []
        for raw in as_sequence(payload, "markets", SOURCE_NAME):
            entry = raw if isinstance(raw, dict) else {}
            if needle and needle not in str(entry.get("question") or "").lower():
                continue
            market = self._to_market(entry, observed_at)
            if market is not None:
                found.append(market)
            if len(found) >= limit:
                break
        return tuple(found)

    async def market(self, market_id: str) -> PredictionMarket:
        payload = as_mapping(
            await self._source.get_json(f"{API_ROOT}/markets/{market_id}"),
            "market",
            SOURCE_NAME,
        )
        market = self._to_market(payload, self._clock.now())
        if market is None:
            raise EngineNotFoundError(
                f"el mercado «{market_id}» existe en {SOURCE_NAME} pero no se "
                f"puede leer como mercado de predicción: puede estar cerrado, "
                f"sin liquidez o sin precios publicados."
            )
        return market

    # ----------------------------------------------------------------- #
    # Traducción
    # ----------------------------------------------------------------- #
    def _to_market(
        self,
        entry: Mapping[str, Any],
        observed_at: datetime,
    ) -> PredictionMarket | None:
        """Traduce un mercado, o `None` si no es utilizable.

        Un mercado descartado se registra en el log pero no interrumpe la
        lectura de los demás: con datos reales siempre hay filas raras, y la
        tabla debe mostrar las buenas en vez de caerse por una mala.
        """
        market_id = str(entry.get("id") or "").strip()
        question = str(entry.get("question") or "").strip()
        if not market_id or not question:
            return None
        if entry.get("closed") is True or entry.get("active") is False:
            return None

        liquidity = optional_decimal(entry.get("liquidityNum"))
        if liquidity is None or liquidity < MIN_LIQUIDITY_USD:
            return None

        outcomes = self._to_outcomes(entry, market_id)
        if outcomes is None:
            return None

        try:
            return PredictionMarket(
                market_id=market_id,
                venue=VENUE,
                question=question,
                outcomes=outcomes,
                observed_at=observed_at,
                closes_at=_parse_moment(entry.get("endDate")),
            )
        except Exception as error:
            _log.debug("polymarket.market_skipped", market_id=market_id, reason=str(error))
            return None

    def _to_outcomes(
        self,
        entry: Mapping[str, Any],
        market_id: str,
    ) -> tuple[MarketOutcome, ...] | None:
        labels = _decode_json_array(entry.get("outcomes"))
        prices = _decode_json_array(entry.get("outcomePrices"))
        if labels is None or prices is None or len(labels) != len(prices):
            _log.debug("polymarket.outcomes_unreadable", market_id=market_id)
            return None
        if len(labels) < 2:
            return None

        outcomes: list[MarketOutcome] = []
        seen: set[str] = set()
        for label, raw_price in zip(labels, prices, strict=True):
            text = str(label).strip()
            price = optional_decimal(raw_price)
            if not text or price is None or not (Decimal(0) <= price <= Decimal(1)):
                return None
            if text in seen:
                # Etiquetas repetidas: el dominio lo rechazaría, y aquí se sabe
                # de qué mercado viene, así que se descarta con contexto.
                _log.debug("polymarket.duplicate_outcome", market_id=market_id, label=text)
                return None
            seen.add(text)
            outcomes.append(MarketOutcome(label=text, price=price))
        return tuple(outcomes)


# --------------------------------------------------------------------------- #
# Lectura del formato de la fuente
# --------------------------------------------------------------------------- #
def _decode_json_array(value: Any) -> list[Any] | None:
    """Decodifica una lista que la fuente manda **como cadena JSON**.

    Gamma publica `outcomes` y `outcomePrices` así: `'["Yes", "No"]'`. Es un
    JSON dentro de otro JSON, no un descuido nuestro al leerlo.
    """
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, list) else None


def _parse_moment(value: Any) -> datetime | None:
    """Convierte una marca ISO-8601 a `datetime` con zona horaria.

    El dominio exige instantes con zona: una fecha de cierre sin zona es
    ambigua en varias horas, y de esas horas puede depender si un mercado está
    abierto. Si no se puede determinar, se devuelve `None` en vez de suponer UTC.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class PolymarketProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> PolymarketEngine:
        return PolymarketEngine()


PROVIDER: Final = PolymarketProvider()
