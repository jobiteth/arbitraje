"""Motor DEX sobre la API pública de DexScreener: **reservas reales**.

Qué aporta y qué no, que es lo que decide cuándo conviene activarlo:

DexScreener publica las reservas de cada token en cada pool (`liquidity.base` y
`liquidity.quote`), así que el impacto de precio **no se estima**: se calcula
con la fórmula de producto constante sobre las reservas que de verdad tiene el
pool. Lo que no publica es la comisión. Para los protocolos cuya comisión es una
constante del contrato —Uniswap V1 y V2, Sushiswap V2— se conoce de todas
formas; para un pool V3 o V4 depende de ese pool concreto y esta API no lo dice.

De ahí la decisión central del motor: **sólo cotiza pools cuya comisión es una
constante verificable del protocolo, y omite los demás.** Eso lo deja con menos
venues que `geckoterminal`, pero con la propiedad de que ninguna cifra que
devuelve es un supuesto. Inventar una comisión para los pools V3 habría dado
una tabla más poblada y un `amount_out` que promete más de lo que se recibiría,
que es exactamente el error que esta aplicación no debe cometer.

Consecuencia que conviene anticipar al cambiar de red: en las redes donde la
liquidez está en protocolos de comisión por pool, este motor devuelve pocas
filas o ninguna. En Solana, donde los AMM dominantes (Raydium, Orca) son de
comisión por pool, no devuelve ninguna. No es un fallo: es el motor diciendo
que ahí no puede medir sin inventar. Para esas redes está `geckoterminal`.

Sin API key: la API es pública. El motor no declara `required_config`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Final

import structlog

from amigocompora.domain.chains import ChainSpec, chain
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.models import Measurement, Quote, Token, TradingPair, Venue
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import EXACT, BasisPoints, TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.amm import ConstantProductPool
from amigocompora.engines.amm_protocols import ProtocolRef, parse_dex_id, venue_for
from amigocompora.engines.catalog import wrapped_native
from amigocompora.engines.http_source import (
    JsonSource,
    as_mapping,
    as_sequence,
    optional_decimal,
)

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "DexScreener"
HOST: Final = "api.dexscreener.com"
TOKENS_URL: Final = f"https://{HOST}/latest/dex/tokens/"

MANIFEST: Final = EngineManifest(
    engine_id="dexscreener",
    name="DexScreener — reservas reales",
    version="2.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary=(
        "Lee las reservas reales de cada pool y calcula el impacto de precio "
        "sobre ellas. Sólo cotiza pools cuya comisión es una constante del "
        "protocolo (Uniswap V1/V2 y forks verificados), así que todas sus cifras "
        "son medidas, no estimadas. Cubre menos venues que GeckoTerminal."
    ),
    capabilities=frozenset({Capability.READ_CHAIN, Capability.COMPUTE_ROUTE}),
    required_config=(),
    allowed_hosts=(HOST,),
)

#: Clave de red del registro → `chainId` que usa esta API.
#:
#: **Añadir una red es una línea aquí.** Los identificadores están verificados
#: consultando la dirección conocida de USDC en cada red y leyendo el `chainId`
#: que devuelven sus pools: por eso Polygon es `polygon` aquí y `polygon_pos` en
#: GeckoTerminal, y Avalanche es `avalanche` aquí y `avax` allí. Suponer que las
#: dos fuentes nombran las redes igual habría dejado dos redes sin cotizar sin
#: que ningún error lo dijera.
CHAIN_SLUGS: Final[Mapping[str, str]] = {
    "ethereum": "ethereum",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "polygon": "polygon",
    "unichain": "unichain",
    "optimism": "optimism",
    "avalanche": "avalanche",
    "arc": "arc",
    "robinhood": "robinhood",
    "solana": "solana",
}

#: Impacto por encima del cual el pool se omite en vez de ensuciar la
#: comparación con una ejecución catastrófica.
MAX_PRICE_IMPACT: Final = BasisPoints(1_000)

#: Desviación máxima tolerada entre el precio que implica el ratio de reservas
#: y el precio que publica la fuente. Si se supera, las dos cifras no describen
#: el mismo pool —datos desactualizados, o liquidez agregada de algo que no es
#: un producto constante— y el impacto calculado sobre ellas no valdría nada.
MAX_RESERVE_PRICE_DRIFT: Final = BasisPoints(200)


@dataclass(frozen=True, slots=True)
class _PoolRow:
    """Una fila de la respuesta, ya validada y en nuestras unidades."""

    venue: Venue
    pair_address: str
    fee_bps: BasisPoints
    protocol: ProtocolRef
    reserve_base: TokenAmount
    reserve_quote: TokenAmount


class DexScreenerEngine:
    """Cotizaciones calculadas sobre las reservas reales de cada pool."""

    __slots__ = ("_clock", "_source")

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        ttl_seconds: float = 5.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._clock = clock or SystemClock()
        self._source = JsonSource(
            name=SOURCE_NAME,
            allowed_hosts=MANIFEST.allowed_hosts,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            # DexScreener no documenta un límite estricto; un intervalo mínimo
            # pequeño basta para no dispararle ráfagas al cambiar de pestaña.
            min_interval_seconds=0.2,
        )

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    async def aopen(self) -> None:
        await self._source.aopen()

    async def aclose(self) -> None:
        await self._source.aclose()

    # ----------------------------------------------------------------- #
    # Lectura de la fuente
    # ----------------------------------------------------------------- #
    async def _pairs_for(self, token: Token) -> list[Any]:
        if token.address is None:
            return []
        payload = as_mapping(
            await self._source.get_json(f"{TOKENS_URL}{token.address}"),
            "respuesta",
            SOURCE_NAME,
        )
        pairs = payload.get("pairs")
        # `pairs: null` es la respuesta normal para un token sin pools: es
        # ausencia de datos, no un error de formato.
        return [] if pairs is None else as_sequence(pairs, "pairs", SOURCE_NAME)

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        """Venues cotizables en esta red, observados en la propia fuente.

        No hay lista estática de venues: se enumeran los que aparecen de verdad
        en los pools de un token de referencia y para los que conocemos la
        comisión del protocolo.
        """
        slug = CHAIN_SLUGS.get(chain_key)
        reference = wrapped_native(chain_key)
        if slug is None or reference is None:
            return ()

        seen: dict[str, Venue] = {}
        for raw in await self._pairs_for(reference):
            entry = raw if isinstance(raw, dict) else {}
            if entry.get("chainId") != slug:
                continue
            protocol = _protocol_of(entry)
            if protocol.constant_fee is None:
                continue
            venue = venue_for(protocol, protocol.constant_fee, chain_key)
            seen.setdefault(venue.venue_id, venue)
        return tuple(seen.values())

    # ----------------------------------------------------------------- #
    # Cotización
    # ----------------------------------------------------------------- #
    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        slug = CHAIN_SLUGS.get(pair.chain)
        if slug is None or pair.base.address is None or pair.quote.address is None:
            return ()

        spec = chain(pair.chain)
        rows = self._rows_for_pair(await self._pairs_for(pair.base), pair, slug, spec)
        observed_at = self._clock.now()
        return tuple(
            quote
            for quote in (self._to_quote(row, pair, amount_in, observed_at) for row in rows)
            if quote is not None
        )

    def _rows_for_pair(
        self,
        raw_pairs: list[Any],
        pair: TradingPair,
        slug: str,
        spec: ChainSpec,
    ) -> list[_PoolRow]:
        base_address = spec.normalize_address(pair.base.address or "")
        quote_address = spec.normalize_address(pair.quote.address or "")
        rows: list[_PoolRow] = []

        for raw in raw_pairs:
            entry = raw if isinstance(raw, dict) else {}
            if entry.get("chainId") != slug:
                continue
            row = self._to_row(entry, pair, base_address, quote_address, spec)
            if row is not None:
                rows.append(row)
        return rows

    def _to_row(
        self,
        entry: dict[str, Any],
        pair: TradingPair,
        base_address: str,
        quote_address: str,
        spec: ChainSpec,
    ) -> _PoolRow | None:
        """Traduce una fila de la API, o `None` si no sirve para este par.

        Devolver `None` en vez de lanzar es deliberado: la respuesta trae todos
        los pools del token, y la mayoría no son del par que se pregunta. Un
        pool descartado no es un error.
        """
        api_base = as_mapping(entry.get("baseToken") or {}, "baseToken", SOURCE_NAME)
        api_quote = as_mapping(entry.get("quoteToken") or {}, "quoteToken", SOURCE_NAME)
        api_base_address = spec.normalize_address(str(api_base.get("address") or ""))
        api_quote_address = spec.normalize_address(str(api_quote.get("address") or ""))

        if {api_base_address, api_quote_address} != {base_address, quote_address}:
            return None

        protocol = _protocol_of(entry)
        if protocol.constant_fee is None:
            # Pool real, pero con una comisión que esta API no publica y que el
            # protocolo no fija. Se omite antes que suponerla.
            _log.debug(
                "dexscreener.pool_skipped_unknown_fee",
                dex=entry.get("dexId"),
                labels=entry.get("labels"),
                fee_source=protocol.fee_source.value,
                pair=pair.symbol,
            )
            return None
        fee = protocol.constant_fee

        liquidity = as_mapping(entry.get("liquidity") or {}, "liquidity", SOURCE_NAME)
        api_base_reserve = optional_decimal(liquidity.get("base"))
        api_quote_reserve = optional_decimal(liquidity.get("quote"))
        if api_base_reserve is None or api_quote_reserve is None:
            return None
        if api_base_reserve <= 0 or api_quote_reserve <= 0:
            return None

        if not self._reserves_agree(entry, api_base_reserve, api_quote_reserve, pair):
            return None

        # La API puede listar el pool en cualquiera de las dos orientaciones:
        # para WETH/USDC puede devolver `USDC/WETH`. Las reservas van con **su**
        # orientación, así que hay que cruzarlas al orden de nuestro par.
        inverted = api_base_address != base_address
        base_reserve = api_quote_reserve if inverted else api_base_reserve
        quote_reserve = api_base_reserve if inverted else api_quote_reserve

        return _PoolRow(
            venue=venue_for(protocol, fee, pair.chain),
            pair_address=str(entry.get("pairAddress") or ""),
            fee_bps=fee,
            protocol=protocol,
            reserve_base=_amount(base_reserve, pair.base),
            reserve_quote=_amount(quote_reserve, pair.quote),
        )

    def _reserves_agree(
        self,
        entry: dict[str, Any],
        api_base_reserve: Decimal,
        api_quote_reserve: Decimal,
        pair: TradingPair,
    ) -> bool:
        """Comprueba que las reservas y el precio publicado describen lo mismo.

        Se compara en la orientación de la propia API —reserva quote dividida
        entre reserva base, contra su `priceNative`— para no arrastrar una
        inversión de más. Si no cuadran, el pool se descarta: preferimos una
        comparación con menos filas a una fila con un impacto inventado.
        """
        reported = optional_decimal(entry.get("priceNative"))
        if reported is None or reported <= 0:
            return False

        implied = EXACT.divide(api_quote_reserve, api_base_reserve)
        drift = abs(
            BasisPoints.from_ratio(EXACT.divide(EXACT.subtract(implied, reported), reported))
        )
        if drift.value > MAX_RESERVE_PRICE_DRIFT.value:
            _log.debug(
                "dexscreener.pool_skipped_price_drift",
                dex=entry.get("dexId"),
                pair=pair.symbol,
                drift_bps=drift.value,
            )
            return False
        return True

    def _to_quote(
        self,
        row: _PoolRow,
        pair: TradingPair,
        amount_in: TokenAmount,
        observed_at: datetime,
    ) -> Quote | None:
        pool = ConstantProductPool(
            reserve_base=row.reserve_base,
            reserve_quote=row.reserve_quote,
            fee_bps=row.fee_bps,
        )
        if not pool.has_depth_for(amount_in, max_impact=MAX_PRICE_IMPACT):
            return None

        amount_out = pool.output_for(amount_in)
        if not amount_out.is_positive:
            return None

        return Quote(
            venue=row.venue,
            engine_id=MANIFEST.engine_id,
            pair=pair,
            amount_in=amount_in,
            amount_out=amount_out,
            fee_bps=row.fee_bps,
            price_impact_bps=pool.price_impact(amount_in),
            observed_at=observed_at,
            liquidity=row.reserve_quote,
            # La comisión no la publica la fuente: se deduce de la versión del
            # protocolo que sí publica, y es una constante del contrato.
            fee_basis=Measurement.DERIVED,
            # El impacto sale de las reservas reales del pool. Sin supuestos.
            impact_basis=Measurement.DERIVED,
            source_note=(
                f"{row.protocol.label}. Reservas reales del pool "
                f"{row.pair_address[:10]}… Comisión {row.fee_bps} fijada por el "
                f"protocolo."
            ),
        )


# --------------------------------------------------------------------------- #
# Lectura del formato de la fuente
# --------------------------------------------------------------------------- #
def _protocol_of(entry: Mapping[str, Any]) -> ProtocolRef:
    """Identifica el protocolo de un pool a partir de `dexId` + `labels`.

    DexScreener reparte la información en dos campos: `dexId` lleva la familia
    (`"uniswap"`) y `labels` la versión (`["v2"]`). Se juntan antes de
    normalizar porque sin la versión no hay forma de saber si la comisión es
    constante: `"uniswap"` a secas podría ser V2 (30 bps fijos) o V3 (depende
    del pool), y confundirlos es la diferencia entre medir e inventar.
    """
    dex_id = str(entry.get("dexId") or "").strip().lower()
    if not dex_id:
        return parse_dex_id("")
    raw_labels = entry.get("labels")
    labels = (
        [str(item).strip().lower() for item in raw_labels] if isinstance(raw_labels, list) else []
    )
    version = next((label for label in labels if label), "")
    return parse_dex_id(f"{dex_id}-{version}" if version else dex_id)


def _amount(human_value: Decimal, token: Token) -> TokenAmount:
    """Pasa una cantidad en unidades humanas a las unidades mínimas del token.

    Se redondea hacia abajo porque las fuentes publican las reservas con más
    decimales de los que el token admite, y una reserva declarada por encima de
    la real haría que la cotización prometiera de más.
    """
    return TokenAmount.from_decimal(
        human_value,
        token.decimals,
        token.symbol,
        rounding=ROUND_DOWN,
    )


@dataclass(frozen=True, slots=True)
class DexScreenerProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> DexScreenerEngine:
        return DexScreenerEngine()


PROVIDER: Final = DexScreenerProvider()
