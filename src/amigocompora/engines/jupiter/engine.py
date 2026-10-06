"""Motor DEX sobre Jupiter: **la ejecución real de Solana**.

Solana era la única red medida que no devolvía ni una cotización. La razón no era
falta de liquidez —hay unos 30 M$ sólo en el SOL/USDC de Orca— sino que sus AMM
dominantes (Orca, Raydium, Meteora) fijan la comisión **por pool**, y ninguna de
las dos fuentes anteriores la publica: `dexscreener` los omite por no poder medir
la comisión y `geckoterminal` no la trae en el nombre del pool. Las dos están
haciendo lo correcto; lo que faltaba era una fuente que diera el resultado de la
ejecución en vez de los ingredientes para calcularla.

Eso es Jupiter: un agregador que simula la ruta y devuelve el `outAmount` exacto,
en unidades raw, ya neto de comisiones e impacto. No hay que reconstruir nada,
así que no hay nada que suponer.

### Lo que esta fuente no da, y cómo se dice

Medido contra la API, cada tramo de `routePlan.swapInfo` expone sólo
`['ammKey','inAmount','inputMint','label','outAmount','outputMint',
'updateContextSlot']`: **no hay `feeAmount`**. La comisión está cobrada dentro
del `outAmount` y no es atribuible por separado. Por eso este motor es el único
que emite `fee_bps=None` —ver `domain.models.Quote` para por qué eso es `None` y
no `0`—, y por eso sus cotizaciones sirven para comparar precios pero no entran
en el cálculo de diferenciales netos, que es una resta.

Tampoco publica reservas, así que `liquidity` es `None`. El campo `swapUsdValue`
que sí trae es el valor en dólares **del trade**, no del pool: pasarlo como
liquidez sería mentir con un número de la propia fuente, que es la peor clase.

### Un venue, no varios

`mostReliableAmmsQuoteReport` viene indexado por pubkey de pool y sin etiqueta de
protocolo (`{"Czfq3xZZ…": "120232940863", "BZtgQEyS…": "Not used RG: …"}`), y
`otherRoutePlans` llega como `None`. Medido además en llamadas consecutivas, la
ruta cambia sola (Obsidian; Kipseli→BisonFi→Manifest; TesseraV; Whirlpool). Una
ruta que cambia entre dos peticiones no es la identidad de un venue, así que
Jupiter se presenta como **un** venue agregado y la composición de la ruta de esa
cotización concreta va en `source_note`, que es donde pertenece.

Por eso este motor no usa `amm_protocols.venue_for`: ese helper mete la comisión
en el `venue_id`, y aquí no hay comisión que meter.

Sin API key: se usa el endpoint abierto `lite-api.jup.ag`. Jupiter ofrece también
un `api.jup.ag` con clave y límites más altos, pero no hay clave disponible para
medirlo y este proyecto no publica rutas de código que no haya ejecutado.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import structlog

from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.models import (
    Measurement,
    Quote,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.http_source import JsonSource, as_mapping, optional_decimal

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "Jupiter"
HOST: Final = "lite-api.jup.ag"
QUOTE_URL: Final = f"https://{HOST}/swap/v1/quote"

#: La única red de este motor. Jupiter es el agregador **de Solana**: no hay una
#: tabla de redes que ampliar aquí, y fingir que la hay sugeriría que añadir otra
#: red es cuestión de una línea cuando sería otra API entera.
CHAIN_KEY: Final = "solana"

MANIFEST: Final = EngineManifest(
    engine_id="jupiter",
    name="Jupiter — ejecución agregada de Solana",
    version="1.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary=(
        "Simula la ruta real en los AMM de Solana y devuelve la cantidad exacta "
        "que se recibiría, ya neta de comisión e impacto. Es la única fuente que "
        "cotiza Solana, porque sus AMM fijan la comisión por pool y nadie más la "
        "publica. En cambio no desglosa esa comisión, así que sus cotizaciones "
        "comparan precios pero no se usan para diferenciales netos."
    ),
    capabilities=frozenset({Capability.READ_CHAIN, Capability.COMPUTE_ROUTE}),
    required_config=(),
    allowed_hosts=(HOST,),
)

#: El venue, uno y agregado. Ver el porqué en el docstring del módulo.
VENUE: Final = Venue(
    venue_id="jupiter@solana",
    name="Jupiter (agregado)",
    kind=VenueKind.DEX,
    chain=CHAIN_KEY,
)

#: Tolerancia de deslizamiento que se pide al simular. No se ejecuta nada, así
#: que no protege de nada: es un parámetro obligatorio de la API y afecta sólo a
#: `otherAmountThreshold`, que este motor no lee. 50 bps es su valor habitual.
SLIPPAGE_BPS: Final = 50

#: Jupiter lee el importe como un u64 de Solana. Un valor mayor devuelve
#: HTTP 400 con `ParseIntError { kind: PosOverflow }` —medido—, que es el mismo
#: código que usa para «este par no es negociable». Se corta antes de pedir, para
#: que un 400 recibido signifique una sola cosa.
MAX_U64: Final = 2**64 - 1

#: Códigos que en esta API quieren decir «no hay ruta», no «el formato cambió».
#: Medido: un mint sin pools responde 400 con `errorCode: TOKEN_NOT_TRADABLE`.
_ABSENT_STATUSES: Final = frozenset({400, 404})

#: Impacto por encima del cual la cotización se omite, igual que en los demás
#: motores: una ejecución que se come el 10 % no informa, estorba.
MAX_PRICE_IMPACT: Final = BasisPoints(1_000)


class JupiterEngine:
    """Cotizaciones de Solana tal como las ejecutaría el agregador."""

    __slots__ = ("_clock", "_source")

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        ttl_seconds: float = 5.0,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._clock = clock or SystemClock()
        self._source = JsonSource(
            name=SOURCE_NAME,
            allowed_hosts=MANIFEST.allowed_hosts,
            ttl_seconds=ttl_seconds,
            # Más holgado que el resto: cada petición simula una ruta sobre
            # decenas de AMM, y medido tarda del orden de un segundo.
            timeout_seconds=timeout_seconds,
            # El endpoint abierto no manda **ninguna** cabecera de límite
            # —comprobado—, así que no hay forma de saber la cuota salvo
            # agotarla. Se elige un ritmo conservador y el intervalo adaptativo
            # de `JsonSource` se encarga del resto si aun así nos limita.
            min_interval_seconds=1.0,
        )

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    async def aopen(self) -> None:
        await self._source.aopen()

    async def aclose(self) -> None:
        await self._source.aclose()

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        """El venue agregado, si se pregunta por Solana.

        No consulta la API, al contrario que `dexscreener`, que enumera los
        venues que observa. Aquí no hay nada que observar: el venue **es** el
        agregador, y existe siempre que exista la red. Gastar una petición para
        confirmarlo sería gastarla para nada.
        """
        return (VENUE,) if chain_key == CHAIN_KEY else ()

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        if pair.chain != CHAIN_KEY:
            return ()
        if pair.base.address is None or pair.quote.address is None:
            # Jupiter identifica los tokens por mint. El SOL nativo se cotiza
            # como WSOL, que `catalog.wrapped_native` ya devuelve con su mint.
            return ()
        if amount_in.raw > MAX_U64:
            _log.info(
                "jupiter.amount_over_u64",
                pair=pair.symbol,
                amount_raw=amount_in.raw,
            )
            return ()

        payload = await self._source.get_json(
            QUOTE_URL,
            params={
                "inputMint": pair.base.address,
                "outputMint": pair.quote.address,
                "amount": str(amount_in.raw),
                "slippageBps": str(SLIPPAGE_BPS),
                "swapMode": "ExactIn",
                # Restringe los tokens intermedios a los de liquidez probada.
                # Lo recomienda la propia API para que la ruta simulada sea la
                # que de verdad se podría ejecutar, y no una que existe sobre el
                # papel y falla al enviarla.
                "restrictIntermediateTokens": "true",
            },
            absent_statuses=_ABSENT_STATUSES,
        )
        if payload is None:
            # Par sin ruta. Es una respuesta, no un fallo: la red sigue y los
            # demás pares del barrido también.
            _log.debug("jupiter.no_route", pair=pair.symbol)
            return ()

        quote = self._to_quote(as_mapping(payload, "respuesta", SOURCE_NAME), pair, amount_in)
        return () if quote is None else (quote,)

    def _to_quote(
        self,
        payload: Mapping[str, Any],
        pair: TradingPair,
        amount_in: TokenAmount,
    ) -> Quote | None:
        """Traduce la respuesta, o `None` si no describe lo que se preguntó."""
        out_raw = _raw_amount(payload.get("outAmount"))
        in_raw = _raw_amount(payload.get("inAmount"))
        if out_raw is None or in_raw is None or out_raw <= 0:
            _log.debug(
                "jupiter.quote_skipped_unreadable",
                pair=pair.symbol,
                in_amount=payload.get("inAmount"),
                out_amount=payload.get("outAmount"),
            )
            return None

        # En `ExactIn` el importe de entrada es el que se pidió. Si no coincide,
        # la cotización es de otro tamaño y mezclarla en la comparación daría un
        # precio que no es el de nadie. `PriceComparison` lo rechazaría de todas
        # formas; se descarta aquí para poder decir por qué.
        if in_raw != amount_in.raw:
            _log.info(
                "jupiter.quote_skipped_partial",
                pair=pair.symbol,
                requested_raw=amount_in.raw,
                quoted_raw=in_raw,
            )
            return None

        impact = _impact_bps(payload.get("priceImpactPct"))
        if impact is None:
            return None
        if abs(impact).value > MAX_PRICE_IMPACT.value:
            _log.debug(
                "jupiter.quote_skipped_impact",
                pair=pair.symbol,
                impact_bps=impact.value,
            )
            return None

        return Quote(
            venue=VENUE,
            pair=pair,
            amount_in=amount_in,
            amount_out=TokenAmount(out_raw, pair.quote.decimals, pair.quote.symbol),
            # La fuente cobra la comisión dentro de `amount_out` y no la
            # desglosa. Ver el docstring del módulo y el de `Quote`.
            fee_bps=None,
            fee_basis=None,
            price_impact_bps=impact,
            observed_at=self._clock.now(),
            # No publica reservas. `swapUsdValue` es el valor del trade, no del
            # pool, y pasarlo aquí sería inventar con un dato real.
            liquidity=None,
            # El impacto lo mide la propia simulación; no lo calculamos nosotros.
            impact_basis=Measurement.REPORTED,
            source_note=_route_note(payload),
        )


# --------------------------------------------------------------------------- #
# Lectura del formato de la fuente
# --------------------------------------------------------------------------- #
def _raw_amount(value: Any) -> int | None:
    """Lee un importe en unidades mínimas, que la API manda como cadena.

    Exige que sea un entero exacto: estas cifras son unidades mínimas de token,
    y aceptar `"1.5"` aquí sería aceptar media unidad indivisible. Un valor
    ilegible devuelve `None` para que quien llama descarte la cotización en vez
    de tumbar el barrido.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.isdigit():
        return None
    return int(text)


def _impact_bps(value: Any) -> BasisPoints | None:
    """Convierte `priceImpactPct` a puntos básicos.

    **El campo es una fracción, no un porcentaje**, pese a lo que dice su nombre.
    Está medido, no supuesto: con 50 000 SOL la degradación real del precio
    unitario frente a una orden mínima es del 1,883 % y el campo trae
    `0.0189069740`; la razón entre ambos es 0,996. A tamaños pequeños la razón se
    aleja de 1 porque la ruta cambia y el precio de referencia arrastra su propia
    comisión, y por eso la prueba válida es la grande, donde el impacto domina.

    Leerlo como porcentaje habría dado un error de cien veces en cada cotización
    de Solana, en la dirección de hacer parecer aceptable una ejecución ruinosa.
    """
    ratio = optional_decimal(value)
    if ratio is None:
        return None
    return BasisPoints.from_ratio(ratio)


def _route_note(payload: Mapping[str, Any]) -> str:
    """Describe por dónde pasa la ruta y advierte de lo que no se desglosa.

    La ruta va en la nota y no en la identidad del venue porque cambia entre
    peticiones: ver el docstring del módulo.
    """
    labels: list[str] = []
    raw_plan = payload.get("routePlan")
    if isinstance(raw_plan, list):
        for leg in raw_plan:
            info = leg.get("swapInfo") if isinstance(leg, dict) else None
            label = info.get("label") if isinstance(info, dict) else None
            if isinstance(label, str) and label.strip():
                labels.append(label.strip())
    route = " → ".join(labels) if labels else "ruta no detallada"
    usd = optional_decimal(payload.get("swapUsdValue"))
    size = f" Valor simulado: {usd:,.2f} $." if usd is not None and usd > 0 else ""
    return (
        f"Ruta simulada por Jupiter: {route}. Cantidad neta de comisión e "
        f"impacto; la fuente no desglosa la comisión.{size}"
    )


@dataclass(frozen=True, slots=True)
class JupiterProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> JupiterEngine:
        return JupiterEngine()


PROVIDER: Final = JupiterProvider()
