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
medirlo y este proyecto no publica rutas de código que no haya ejecutado. El
endpoint de swap, medido, tampoco la pide.

### Construir el swap: lo que se midió y por qué el motor re-cotiza

Jupiter construye la transacción en `POST /swap/v1/swap`, y lo que se comprobó
contra la API real decide el diseño entero de `plan_swap`:

- **El endpoint no revalida la cotización que se le pasa.** Enviando el mismo
  `quoteResponse` con el `outAmount` inflado diez veces, responde **HTTP 200** y
  entrega una transacción. Es decir: construir desde una cotización guardada
  produce un payload que nadie ha verificado, con el mínimo garantizado de la
  ruta calculado sobre una cifra inventada. Por eso `plan_swap` **vuelve a
  cotizar** y compara antes de construir; ver `MAX_QUOTE_DRIFT_BPS`.
- **No simula por defecto.** Con una cuenta sin fondos recién generada, la
  respuesta trae `simulationError: None` y `simulationSlot: None`. `None` aquí
  significa «no se simuló», no «la transacción está bien»: leerlo como un visto
  bueno sería exactamente el error que el producto no se puede permitir.
- **El payload caduca y no es determinista.** Dos llamadas consecutivas con la
  misma entrada devuelven `lastValidBlockHeight` distinto (431989574 y
  431989575, medido), y la transacción cambia. No es cacheable —que es lo que
  justifica el `post_json` sin caché de `JsonSource`— y su validez hay que
  publicarla, porque una transacción de Solana que caduca es papel mojado.
- **Los errores de validación son 422**, no 400: pubkey fuera del alfabeto
  base58 devuelve `Parse error: Invalid`, y una de 31 bytes `Parse error:
  WrongSize`. Es la misma regla que aplica `require_solana_address` en el
  dominio, así que el destino se valida antes de gastar la petición.

Lo que **no** se midió: el caso de una cotización caducada de verdad entre dos
llamadas. La comprobación de deriva cubre el caso que sí importa —el precio que
el usuario vio— y el resto lo decide la propia red al emitir.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import structlog

from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import (
    NoQuotesError,
    QuoteMovedError,
    SourceResponseError,
    UnsupportedOperationError,
)
from amigocompora.domain.models import (
    Measurement,
    Quote,
    TradingPair,
    UnsignedSolanaTransaction,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import EXACT, BasisPoints, TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.http_source import JsonSource, as_mapping, optional_decimal

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "Jupiter"
HOST: Final = "lite-api.jup.ag"
QUOTE_URL: Final = f"https://{HOST}/swap/v1/quote"
SWAP_URL: Final = f"https://{HOST}/swap/v1/swap"

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
    capabilities=frozenset(
        {Capability.READ_CHAIN, Capability.COMPUTE_ROUTE, Capability.PREPARE_TX}
    ),
    # La única red que sabe construir. Cualquier otro motor que declare
    # `solana` aquí compite con éste, y la preferencia se decide en
    # configuración, no en el código.
    swap_chains=frozenset({CHAIN_KEY}),
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

#: Deriva máxima tolerada entre la cotización que el usuario vio y la que hay en
#: el momento de construir el swap. 100 bps = 1 %.
#:
#: El endpoint de swap **no** revalida la cotización que se le pasa —medido: un
#: `outAmount` inflado x10 se acepta con un 200—, así que la comprobación es
#: nuestra o no existe. El umbral no es cero porque un mercado se mueve entre
#: que se pinta la tabla y que el usuario pulsa: exigir identidad convertiría el
#: botón en un botón que casi nunca funciona. Un 1 % es lo bastante holgado para
#: absorber ese parpadeo y lo bastante estrecho para que el usuario no firme algo
#: distinto de lo que decidió.
MAX_QUOTE_DRIFT_BPS: Final = BasisPoints(100)


class JupiterEngine:
    """Cotizaciones de Solana tal como las ejecutaría el agregador."""

    __slots__ = ("_clock", "_source")

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        ttl_seconds: float = 5.0,
        timeout_seconds: float = 15.0,
        min_interval_seconds: float = 1.0,
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
            min_interval_seconds=min_interval_seconds,
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
        if amount_in.raw > MAX_U64:
            _log.info(
                "jupiter.amount_over_u64",
                pair=pair.symbol,
                amount_raw=amount_in.raw,
            )
            return ()

        payload = await self._quote_payload(pair, amount_in)
        if payload is None:
            # Par sin ruta. Es una respuesta, no un fallo: la red sigue y los
            # demás pares del barrido también.
            _log.debug("jupiter.no_route", pair=pair.symbol)
            return ()

        quote = self._to_quote(payload, pair, amount_in)
        return () if quote is None else (quote,)

    async def _quote_payload(
        self, pair: TradingPair, amount_in: TokenAmount
    ) -> Mapping[str, Any] | None:
        """La cotización **cruda** de la fuente, o `None` si no hay nada que cotizar.

        Existe separada de `quote()` porque el endpoint de swap necesita el
        `quoteResponse` completo, no la `Quote` del dominio: ésta es una lectura
        derivada y no conserva lo que Jupiter necesita para construir. Ver
        `plan_swap`.
        """
        input_mint = pair.base.address
        output_mint = pair.quote.address
        if input_mint is None or output_mint is None:
            # Jupiter identifica los tokens por mint. El SOL nativo se cotiza
            # como WSOL, que `catalog.wrapped_native` ya devuelve con su mint.
            return None
        raw = await self._source.get_json(
            QUOTE_URL,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
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
        if raw is None:
            return None
        return as_mapping(raw, "respuesta", SOURCE_NAME)

    async def plan_swap(self, quote: Quote, *, recipient: str) -> UnsignedSolanaTransaction:
        """Construye la transacción sin firmar del swap que describe `quote`.

        No firma ni emite: devuelve el payload para que el usuario lo revise.

        ### Por qué vuelve a cotizar

        El endpoint de swap acepta cualquier `quoteResponse` sin comprobarlo
        —medido: con el `outAmount` inflado x10 responde 200 y construye—, así
        que pasarle la cotización que el usuario vio en pantalla equivale a
        construir sobre un precio que nadie ha verificado y que puede ser de
        hace minutos. Se re-cotiza, se compara con lo que se mostró y sólo se
        construye si la diferencia cabe en `MAX_QUOTE_DRIFT_BPS`; si no, se
        lanza `QuoteMovedError` y no se construye **nada**. La alternativa
        —construir y avisar— dejaría en manos del usuario detectar la
        discrepancia comparando dos cifras de un diálogo, que es justo el trabajo
        que no se le puede pedir a quien está confirmando una operación.
        """
        if quote.pair.chain != CHAIN_KEY:
            raise UnsupportedOperationError(
                f"este motor sólo construye swaps en {CHAIN_KEY}: se pidió "
                f"{quote.pair.chain}. Activa un motor que cubra esa red."
            )

        # 1. Cotización fresca, y comprobación de que sigue siendo la misma
        #    operación que el usuario decidió.
        payload = await self._quote_payload(quote.pair, quote.amount_in)
        if payload is None:
            raise NoQuotesError(
                f"«{quote.pair.symbol}» ya no tiene ruta en {SOURCE_NAME}: la que "
                f"viste al cotizar se agotó. Vuelve a cotizar."
            )
        fresh_raw = _raw_amount(payload.get("outAmount"))
        if fresh_raw is None or fresh_raw <= 0:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» devolvió una cotización sin `outAmount` legible "
                f"({payload.get('outAmount')!r}); no se puede construir nada con ella."
            )
        self._require_same_price(quote, fresh_raw)

        # 2. Construir. Sin caché en `post_json`: ver su docstring y el del
        #    módulo — el payload caduca y no es determinista.
        response = await self._source.post_json(
            SWAP_URL,
            json_body={
                "quoteResponse": payload,
                "userPublicKey": recipient,
                "wrapAndUnwrapSol": True,
            },
        )
        body = as_mapping(response, "respuesta de swap", SOURCE_NAME)
        return self._to_unsigned(quote, body, fresh_raw, recipient)

    def expected_destination(self, chain_key: str) -> str | None:
        """`None`: en Solana no hay una tabla de routers que contrastar.

        No es un hueco, es la forma de la red. En EVM el swap se dirige a un
        contrato con una dirección fija y conocida por adelantado, y ahí tiene
        sentido exigir que el `to` del payload sea exactamente ese. En Solana la
        transacción es un mensaje con sus instrucciones y sus cuentas, y el
        programa que la ejecuta —Jupiter— no se identifica por un `to` del
        payload, así que no hay un valor contra el que comparar.

        Devolver `None` es lo que hace que el camino de ejecución **no firme**
        una operación de Solana por accidente: quien pregunte recibe «este motor
        no declara destino», y ahí la decisión correcta es parar. Firmar en
        Solana es la entrega siguiente, con `solders` y su propio contraste.
        """
        return None

    def _require_same_price(self, quote: Quote, fresh_raw: int) -> None:
        """Aborta si el precio se movió más de lo tolerado desde lo que se vio."""
        shown_raw = quote.amount_out.raw
        drift = EXACT.divide(Decimal(abs(fresh_raw - shown_raw)), Decimal(shown_raw))
        drift_bps = BasisPoints.from_ratio(drift)
        if drift_bps.value <= MAX_QUOTE_DRIFT_BPS.value:
            return
        token = quote.pair.quote
        _log.info(
            "jupiter.quote_moved",
            pair=quote.pair.symbol,
            shown_raw=shown_raw,
            fresh_raw=fresh_raw,
            drift_bps=drift_bps.value,
        )
        raise QuoteMovedError(
            shown=str(quote.amount_out),
            fresh=str(TokenAmount(fresh_raw, token.decimals, token.symbol)),
            drift_bps=drift_bps.value,
            tolerance_bps=MAX_QUOTE_DRIFT_BPS.value,
        )

    def _to_unsigned(
        self,
        quote: Quote,
        body: Mapping[str, Any],
        fresh_raw: int,
        recipient: str,
    ) -> UnsignedSolanaTransaction:
        """Traduce la respuesta del swap, o falla diciendo qué campo no cuadró."""
        payload = body.get("swapTransaction")
        if not isinstance(payload, str) or not payload:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» respondió al swap sin `swapTransaction`; no hay "
                f"transacción que revisar."
            )
        # Se decodifica de verdad y no se confía en el nombre del campo: si algún
        # día dejara de ser base64, mejor saberlo aquí que en el diálogo de
        # confirmación, donde el usuario ya está decidiendo.
        try:
            size = len(base64.b64decode(payload, validate=True))
        except (binascii.Error, ValueError) as error:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» devolvió un `swapTransaction` que no es base64 "
                f"válido ({error})."
            ) from error

        height = body.get("lastValidBlockHeight")
        if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» no publicó una altura de caducidad legible "
                f"({height!r}); sin ella no se puede saber hasta cuándo vale."
            )

        token = quote.pair.quote
        fresh = TokenAmount(fresh_raw, token.decimals, token.symbol)
        _log.info(
            "jupiter.swap_planned",
            pair=quote.pair.symbol,
            out_raw=fresh_raw,
            last_valid_block_height=height,
            tx_bytes=size,
        )
        return UnsignedSolanaTransaction(
            transaction_b64=payload,
            last_valid_block_height=height,
            fee_payer=recipient,
            description=(
                f"Swap en {quote.venue.name}: entregas {quote.amount_in} y recibes "
                f"{fresh} según la ruta simulada, con {SLIPPAGE_BPS} bps de "
                f"deslizamiento tolerado. La transacción caduca en la altura "
                f"{height}."
            ),
        )

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
            engine_id=MANIFEST.engine_id,
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
