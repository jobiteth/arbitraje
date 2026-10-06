"""Motor DEX sobre la API pública de GeckoTerminal: **comisión real**.

Es el motor por omisión, y el complemento exacto de `dexscreener`:

GeckoTerminal publica el *fee tier* de cada pool —lo lleva en el propio nombre,
`"WETH / USDC 0.05%"`— y cubre todas las versiones de protocolo, no sólo las de
comisión constante. Eso le da muchos más venues por par, y es lo que permite
cotizar Uniswap V3 y V4, que entre los dos son la mayoría de la liquidez
medida: 11 de los 20 pools de más volumen de Ethereum son V4, y 15 de los 20 de
Unichain. Lo que **no** publica son las reservas de cada token por separado:
sólo `reserve_in_usd`, la liquidez agregada en dólares. Así que aquí la comisión
es un dato medido y la profundidad es una estimación, al contrario que en
`dexscreener`.

El supuesto de la estimación, explícito: se reparte `reserve_in_usd` al 50 %
entre los dos lados del pool y se convierte a unidades de cada token con los
precios que la fuente publica. Para un pool de producto constante el reparto es
exacto por construcción. Para uno de liquidez concentrada (V3, V4) la liquidez
útil cerca del precio actual es **mayor** que la de un pool V2 con el mismo TVL,
así que el supuesto infravalora la profundidad y por tanto **sobrestima** el
impacto de precio. El error cae del lado conservador: la cotización promete
menos de lo que se recibiría, nunca más.

Se omiten dos clases de pool, por la misma razón en los dos casos:

- Los que no llevan comisión en el nombre: la fuente no la publica y suponerla
  sería inventarla.
- Los de Uniswap V4 que publican comisión 0. En V4 la comisión puede ser
  dinámica, y `getInitialLPFee` devuelve exactamente 0 para esos pools: un 0 ahí
  no significa «sin comisión», significa «la decide un hook en cada swap». Ver
  `engines.amm_protocols`.

Y una consulta que no es obvia: cuando el token base es el nativo envuelto se
pregunta **dos veces**, por el envoltorio y por la dirección cero. V4 permite
operar el nativo sin envolver y la fuente lo trata como un token aparte, con su
propia lista de pools. Medido: los 20 pools que devuelve la dirección cero en
Ethereum y en Unichain son **disjuntos** de los que devuelve el WETH, e incluyen
los mayores del par —`ETH / USDC 0.3%` con 37,6 M$ en Ethereum, `USDC / ETH
0.05%` con 5,7 M$ en Unichain—. Con una sola consulta esa liquidez era
invisible: Unichain cotizaba un único venue teniendo 15 pools V4 entre los 20
principales. Cuesta una petición más por par, que el límite de ritmo paga.

Sin API key funciona, y eso es deliberado: la herramienta tiene que servir
recién instalada, sin pedirle al usuario que se registre en nada. Lo que el plan
gratuito sí limita es el ritmo, de forma más estricta de lo que anuncia (ver
`RATE_LIMIT_INTERVAL`). Quien ponga una clave Demo de CoinGecko —opcional, y
guardada en el keyring— obtiene los mismos datos unas cinco veces más rápido por
el host de abajo.
"""

from __future__ import annotations

import re
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
from amigocompora.engines.amm_protocols import (
    FeeSource,
    ProtocolRef,
    parse_dex_id,
    venue_for,
)
from amigocompora.engines.catalog import wrapped_native
from amigocompora.engines.http_source import (
    JsonSource,
    as_mapping,
    as_sequence,
    optional_decimal,
)

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "GeckoTerminal"

#: Host público, sin clave. Es el que se usa si no hay ninguna configurada.
HOST: Final = "api.geckoterminal.com"
API_ROOT: Final = f"https://{HOST}/api/v2"

#: Host de CoinGecko, que sirve los mismos datos on-chain bajo `/onchain` y
#: acepta la clave del plan gratuito. Verificado: devuelve el mismo formato
#: JSON:API con los mismos campos, así que el parser es exactamente el mismo.
KEYED_HOST: Final = "api.coingecko.com"
KEYED_API_ROOT: Final = f"https://{KEYED_HOST}/api/v3/onchain"

#: Clave de configuración **opcional**: una API key Demo de CoinGecko.
#:
#: Sin ella el motor funciona, sólo más despacio. Con ella el límite de ritmo
#: pasa de 6 peticiones por ventana a 30 por minuto, medido. Se guarda en el
#: keyring del sistema como cualquier otro secreto.
API_KEY_OPTION: Final = "coingecko_api_key"

#: Nombre de la cabecera con la que viaja la clave.
#:
#: CoinGecko la acepta también como parámetro `x_cg_demo_api_key` en la URL, y
#: esa forma **no se usa**: una clave en la URL acaba en el log de peticiones de
#: `httpx`, en los logs de cualquier proxy intermedio y en la caché de la
#: fuente. En cabecera no aparece en ninguno de los tres.
API_KEY_HEADER: Final = "x-cg-demo-api-key"

MANIFEST: Final = EngineManifest(
    engine_id="geckoterminal",
    name="GeckoTerminal — comisión real",
    version="2.1.0",
    kind=EngineKind.DEX_QUOTES,
    summary=(
        "Lee la comisión real de cada pool y cubre todas las versiones de "
        "protocolo, incluidas Uniswap V3 y V4. La profundidad es una estimación "
        "conservadora a partir de la liquidez agregada en dólares, así que el "
        "impacto de precio sale sobrestimado antes que al revés. Con una API key "
        "gratuita de CoinGecko va unas cinco veces más rápido."
    ),
    capabilities=frozenset({Capability.READ_CHAIN, Capability.COMPUTE_ROUTE}),
    required_config=(),
    optional_config=(API_KEY_OPTION,),
    allowed_hosts=(HOST, KEYED_HOST),
)

#: Clave de red del registro → identificador de red de esta API.
#:
#: **Añadir una red es una línea aquí.** Los slugs están verificados contra
#: `/api/v2/networks`, que publica las 226 redes que cubre la fuente; los de
#: abajo son los del registro de `domain.chains`.
CHAIN_SLUGS: Final[Mapping[str, str]] = {
    "ethereum": "eth",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "polygon": "polygon_pos",
    "unichain": "unichain",
    "optimism": "optimism",
    "avalanche": "avax",
    "arc": "arc",
    "robinhood": "robinhood",
    "solana": "solana",
}

#: La comisión viaja al final del nombre del pool: `"WETH / USDC 0.05%"`.
_FEE_IN_NAME: Final = re.compile(r"(\d+(?:[.,]\d+)?)\s*%\s*$")

#: Reparto supuesto de `reserve_in_usd` entre los dos lados del pool.
_BALANCED_SHARE: Final = Decimal("0.5")

MAX_PRICE_IMPACT: Final = BasisPoints(1_000)

#: Por debajo de esto la estimación de profundidad deja de ser informativa: un
#: pool con liquidez residual produce impactos enormes que sólo añaden ruido.
MIN_RESERVE_USD: Final = Decimal("10000")

#: Intervalo mínimo entre peticiones **sin clave**, medido contra la API real.
#:
#: No es un límite por segundo, es de ventana: pasan 6 peticiones y la séptima
#: devuelve 429 *sea cual sea el espaciado* —comprobado con 2,1 s y con 2,5 s—, y
#: se libera unos 10 s después. Eso son ~6 peticiones cada 30 s, es decir unas 12
#: por minuto y no las 30 que anuncia el plan público. 5 s es el ritmo sostenible
#: que sale de ahí. Por encima de esto, el intervalo adaptativo de `JsonSource`
#: sube solo si la fuente sigue limitando.
RATE_LIMIT_INTERVAL: Final = 5.0

#: Intervalo mínimo **con clave Demo**. Medido: 10 peticiones seguidas a 0,75 s
#: de separación, ninguna rechazada. El plan Demo publica 30 por minuto, así que
#: 2 s se queda del lado conservador y aun así va 2,5 veces más rápido.
KEYED_RATE_LIMIT_INTERVAL: Final = 2.0


@dataclass(frozen=True, slots=True)
class _PoolRow:
    venue: Venue
    pool_address: str
    fee_bps: BasisPoints
    fee_basis: Measurement
    protocol: ProtocolRef
    reserve_base: TokenAmount
    mid_price: Decimal
    reserve_usd: Decimal
    trades_native: bool


class GeckoTerminalEngine:
    """Cotizaciones con comisión medida y profundidad estimada."""

    __slots__ = ("_clock", "_root", "_source")

    def __init__(
        self,
        *,
        api_key: str | None = None,
        clock: Clock | None = None,
        ttl_seconds: float = 15.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._clock = clock or SystemClock()
        keyed = bool(api_key)
        # La clave decide host y ritmo a la vez: son el mismo hecho visto dos
        # veces, y tenerlos juntos evita el estado imposible de pedirle al host
        # gratuito el ritmo del de pago.
        self._root = KEYED_API_ROOT if keyed else API_ROOT
        self._source = JsonSource(
            name=SOURCE_NAME,
            allowed_hosts=MANIFEST.allowed_hosts,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            min_interval_seconds=(KEYED_RATE_LIMIT_INTERVAL if keyed else RATE_LIMIT_INTERVAL),
            headers={API_KEY_HEADER: api_key} if api_key else None,
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
    async def _pools_for(self, token: Token, spec: ChainSpec) -> list[dict[str, Any]]:
        """Pools del token, incluidos los que operan su nativo sin envolver.

        La deduplicación es por identificador de pool porque las dos consultas
        **podrían** solaparse —ahora mismo no lo hacen, medido— y un mismo pool
        contado dos veces aparecería como dos venues con el mismo precio. Un
        pool sin identificador no se descarta: se le da una clave que no puede
        colisionar, porque dejar fuera liquidez real por un campo ausente sería
        peor que no poder nombrarla.
        """
        network = CHAIN_SLUGS.get(token.chain)
        if network is None or token.address is None:
            return []

        merged: dict[str, dict[str, Any]] = {}
        for address in _query_addresses(token, spec):
            payload = as_mapping(
                await self._source.get_json(
                    f"{self._root}/networks/{network}/tokens/{address}/pools"
                ),
                "respuesta",
                SOURCE_NAME,
            )
            entries = as_sequence(payload.get("data") or [], "data", SOURCE_NAME)
            for index, raw in enumerate(entries):
                if not isinstance(raw, dict):
                    continue
                key = str(raw.get("id") or "") or f"{address}#{index}"
                merged.setdefault(key, raw)
        return list(merged.values())

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        if chain_key not in CHAIN_SLUGS:
            return ()
        reference = wrapped_native(chain_key)
        if reference is None:
            return ()

        spec = chain(chain_key)
        seen: dict[str, Venue] = {}
        for entry in await self._pools_for(reference, spec):
            protocol = parse_dex_id(_dex_id(entry))
            if not protocol.is_quotable:
                continue
            # Mismo criterio que `_to_row`, a propósito: la lista de venues y
            # las cotizaciones tienen que coincidir, o la interfaz ofrecería
            # sitios que luego no cotizan.
            fee, _basis = _fee_for(protocol, _attributes(entry).get("name"))
            if fee is None:
                continue
            venue = venue_for(protocol, fee, chain_key)
            seen.setdefault(venue.venue_id, venue)
        return tuple(seen.values())

    # ----------------------------------------------------------------- #
    # Cotización
    # ----------------------------------------------------------------- #
    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        if pair.chain not in CHAIN_SLUGS:
            return ()
        if pair.base.address is None or pair.quote.address is None:
            return ()

        spec = chain(pair.chain)
        observed_at = self._clock.now()
        rows = [
            row
            for entry in await self._pools_for(pair.base, spec)
            if (row := self._to_row(entry, pair, spec)) is not None
        ]
        return tuple(
            quote
            for row in rows
            if (quote := self._to_quote(row, pair, amount_in, observed_at)) is not None
        )

    def _to_row(
        self,
        entry: dict[str, Any],
        pair: TradingPair,
        spec: ChainSpec,
    ) -> _PoolRow | None:
        """Traduce un pool, o `None` si no sirve para este par."""
        attributes = _attributes(entry)
        api_base = _token_address(entry, "base_token", spec)
        api_quote = _token_address(entry, "quote_token", spec)

        sides = _match_sides(api_base, api_quote, pair, spec)
        if sides is None:
            return None
        inverted, trades_native = sides

        protocol = parse_dex_id(_dex_id(entry))
        if not protocol.is_quotable:
            _log.debug(
                "geckoterminal.pool_skipped_unknown_protocol",
                dex=_dex_id(entry),
                pair=pair.symbol,
            )
            return None

        fee, fee_basis = _fee_for(protocol, attributes.get("name"))
        if fee is None or fee_basis is None:
            _log.debug(
                "geckoterminal.pool_skipped_unknown_fee",
                pool=attributes.get("name"),
                protocol=protocol.key,
                fee_source=protocol.fee_source.value,
                pair=pair.symbol,
            )
            return None

        reserve_usd = optional_decimal(attributes.get("reserve_in_usd"))
        if reserve_usd is None or reserve_usd < MIN_RESERVE_USD:
            return None

        # `base_token_price_quote_token` es el precio del base **de la API** en
        # unidades de su quote. Si el pool está listado al revés, el precio que
        # nos sirve es el recíproco, que la fuente también publica.
        price_field = "quote_token_price_base_token" if inverted else "base_token_price_quote_token"
        mid_price = optional_decimal(attributes.get(price_field))
        if mid_price is None or mid_price <= 0:
            return None

        base_price_usd = optional_decimal(
            attributes.get("quote_token_price_usd" if inverted else "base_token_price_usd")
        )
        if base_price_usd is None or base_price_usd <= 0:
            return None

        # Supuesto documentado: la mitad del TVL está en el lado base.
        base_reserve_human = EXACT.divide(
            EXACT.multiply(reserve_usd, _BALANCED_SHARE),
            base_price_usd,
        )
        if base_reserve_human <= 0:
            return None

        return _PoolRow(
            venue=venue_for(protocol, fee, pair.chain),
            pool_address=str(attributes.get("address") or ""),
            fee_bps=fee,
            fee_basis=fee_basis,
            protocol=protocol,
            reserve_base=TokenAmount.from_decimal(
                base_reserve_human,
                pair.base.decimals,
                pair.base.symbol,
                rounding=ROUND_DOWN,
            ),
            mid_price=mid_price,
            reserve_usd=reserve_usd,
            trades_native=trades_native,
        )

    def _to_quote(
        self,
        row: _PoolRow,
        pair: TradingPair,
        amount_in: TokenAmount,
        observed_at: datetime,
    ) -> Quote | None:
        if not row.reserve_base.is_positive:
            return None

        reserve_quote_human = EXACT.multiply(row.reserve_base.as_decimal(), row.mid_price)
        try:
            pool = ConstantProductPool(
                reserve_base=row.reserve_base,
                reserve_quote=TokenAmount.from_decimal(
                    reserve_quote_human,
                    pair.quote.decimals,
                    pair.quote.symbol,
                    rounding=ROUND_DOWN,
                ),
                fee_bps=row.fee_bps,
            )
        except Exception as error:
            _log.debug(
                "geckoterminal.pool_unusable",
                pool=row.pool_address,
                reason=str(error),
            )
            return None

        if not pool.has_depth_for(amount_in, max_impact=MAX_PRICE_IMPACT):
            return None
        amount_out = pool.output_for(amount_in)
        if not amount_out.is_positive:
            return None

        return Quote(
            venue=row.venue,
            pair=pair,
            amount_in=amount_in,
            amount_out=amount_out,
            fee_bps=row.fee_bps,
            price_impact_bps=pool.price_impact(amount_in),
            observed_at=observed_at,
            liquidity=pool.reserve_quote,
            fee_basis=row.fee_basis,
            # La profundidad sale de repartir el TVL al 50 %: es un supuesto.
            impact_basis=Measurement.ESTIMATED,
            source_note=_note(row),
        )


def _note(row: _PoolRow) -> str:
    origin = (
        "fijada por el protocolo"
        if row.fee_basis is Measurement.DERIVED
        else "publicada por la fuente"
    )
    native = (
        " El pool opera con el token nativo, no con su envoltorio: lo que se "
        "recibe o se entrega es el nativo."
        if row.trades_native
        else ""
    )
    return (
        f"{row.protocol.label}. Comisión {row.fee_bps} {origin}. "
        f"Profundidad estimada sobre {row.reserve_usd:,.0f} USD de liquidez "
        f"agregada, repartida al 50 %.{native}"
    )


# --------------------------------------------------------------------------- #
# Lectura del formato JSON:API de la fuente
# --------------------------------------------------------------------------- #
def _attributes(entry: Mapping[str, Any]) -> dict[str, Any]:
    return as_mapping(entry.get("attributes") or {}, "attributes", SOURCE_NAME)


def _query_addresses(token: Token, spec: ChainSpec) -> tuple[str, ...]:
    """Direcciones por las que preguntar para cubrir todos los pools del token.

    Normalmente una: la del token. Dos cuando el token **es** el nativo
    envuelto de una red EVM, porque entonces hay pools que operan el nativo
    directamente y la fuente los cuelga de la dirección cero, no del
    envoltorio. Verificado pidiendo `/networks/unichain/tokens/0x0000…0000`:
    responde 200 con `Ether`, símbolo `ETH` y 18 decimales, así que ahí la
    dirección cero es un token consultable de pleno derecho.

    El centinela se pide a la red (`ChainSpec.native_sentinel`) y no se toma de
    la constante, porque fuera de EVM no existe: en Solana, WSOL **es** el
    nativo envuelto, y preguntar por `0x0000…0000` devuelve 404 —medido—, que es
    la fuente diciendo con razón que eso no es una dirección suya.

    Que los decimales del nativo coincidan con los del envoltorio es irrelevante
    para las cuentas, y conviene decir por qué: todo lo que se lee de esta fuente
    —precios y `reserve_in_usd`— viene en unidades humanas, y la conversión a
    entero usa los decimales del token del par. Lo único que se asume es que un
    nativo vale exactamente un envuelto, que es la definición de envolver.
    """
    address = token.address
    if not address:
        return ()
    wrapped = spec.wrapped_native
    sentinel = spec.native_sentinel
    if wrapped is None or sentinel is None:
        return (address,)
    if spec.normalize_address(address) != spec.normalize_address(wrapped):
        return (address,)
    return (address, sentinel)


def _token_address(entry: Mapping[str, Any], relationship: str, spec: ChainSpec) -> str:
    """Saca la dirección del identificador `"<red>_<dirección>"` de JSON:API.

    Se parte por el **último** `_` y no por el primero: hay redes cuyo
    identificador lo contiene —`polygon_pos_0x8f3c…`— y partir por el primero
    devolvería `pos_0x8f3c…`, una dirección que no existe. Las direcciones no
    contienen `_` en ninguno de los dos formatos, así que el último separador
    es siempre el que divide red y dirección.
    """
    relationships = entry.get("relationships")
    if not isinstance(relationships, dict):
        return ""
    node = relationships.get(relationship)
    if not isinstance(node, dict):
        return ""
    data = node.get("data")
    if not isinstance(data, dict):
        return ""
    identifier = str(data.get("id") or "")
    return spec.normalize_address(identifier.rpartition("_")[2])


def _match_sides(
    api_base: str,
    api_quote: str,
    pair: TradingPair,
    spec: ChainSpec,
) -> tuple[bool, bool] | None:
    """¿Es este pool el par que se pregunta? Devuelve `(invertido, es_nativo)`.

    Uniswap V4 permite operar el **token nativo** sin envolverlo, y lo identifica
    con la dirección cero. Medido, esos pools son parte grande de la liquidez
    real: 8 de los 20 pools principales de Unichain son de ETH nativo. Tratarlos
    como un token distinto del envoltorio dejaría la herramienta ciega a buena
    parte del mercado, así que se aceptan como el mismo lado del par —valen lo
    mismo, uno a uno— y la cotización lo dice en su nota para que quien la lea
    sepa que ahí se recibe el nativo y no el ERC-20.
    """
    wanted_base = spec.normalize_address(pair.base.address or "")
    wanted_quote = spec.normalize_address(pair.quote.address or "")
    native = spec.normalize_address(spec.wrapped_native or "")
    sentinel = spec.native_sentinel
    zero = spec.normalize_address(sentinel) if sentinel else ""

    def side_of(address: str) -> str | None:
        if address == wanted_base:
            return "base"
        if address == wanted_quote:
            return "quote"
        if zero and address == zero and native:
            if wanted_base == native:
                return "base"
            if wanted_quote == native:
                return "quote"
        return None

    first, second = side_of(api_base), side_of(api_quote)
    if first is None or second is None or first == second:
        return None
    return first == "quote", bool(zero) and zero in {api_base, api_quote}


def _dex_id(entry: Mapping[str, Any]) -> str:
    relationships = entry.get("relationships")
    if not isinstance(relationships, dict):
        return ""
    dex = relationships.get("dex")
    if not isinstance(dex, dict):
        return ""
    data = dex.get("data")
    if not isinstance(data, dict):
        return ""
    return str(data.get("id") or "").lower()


def _fee_for(
    protocol: ProtocolRef,
    pool_name: Any,
) -> tuple[BasisPoints | None, Measurement | None]:
    """Comisión del pool y de dónde sale, o `(None, None)` si no se sabe.

    La constante del protocolo gana sobre la comisión publicada cuando existe:
    está leída del contrato, mientras que la publicada viene de parsear el
    nombre del pool. Donde no hay constante, se usa la publicada —y se marca
    como tal, que es el punto de `Measurement`.
    """
    if protocol.fee_source is FeeSource.PROTOCOL_CONSTANT and protocol.constant_fee is not None:
        return protocol.constant_fee, Measurement.DERIVED
    reported = _fee_from_name(pool_name)
    if reported is None or not protocol.accepts_reported_fee(reported):
        return None, None
    return reported, Measurement.REPORTED


def _fee_from_name(name: Any) -> BasisPoints | None:
    """Extrae la comisión del nombre del pool, o `None` si no la lleva.

    `"WETH / USDC 0.05%"` → 5 bps. Un nombre sin porcentaje significa que la
    fuente no publica la comisión de ese pool, no que sea cero.
    """
    if not isinstance(name, str):
        return None
    match = _FEE_IN_NAME.search(name)
    if match is None:
        return None
    percent = optional_decimal(match.group(1).replace(",", "."))
    if percent is None or percent < 0:
        return None
    return BasisPoints.from_percent(percent)


@dataclass(frozen=True, slots=True)
class GeckoTerminalProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> GeckoTerminalEngine:
        return GeckoTerminalEngine(api_key=config.get(API_KEY_OPTION) or None)


PROVIDER: Final = GeckoTerminalProvider()
