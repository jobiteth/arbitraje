"""Motor DEX sobre la API de 0x (ZeroEx): agregado de EVM, con el build opcional.

Es el agregado más ancho de los que hay montados —**nueve** redes EVM, todas las
del registro salvo Arc— y el único que llega a Robinhood Chain, que Uniswap no
cubre. Compite con Uniswap por precio en las ocho que ambos ven.

### Lo que se midió, y lo que decidió cada cosa

- **La clave viaja en `0x-api-key`, con `0x-version: v2`.** Medido: sin ella, 401.
- **El fee es un 15,02 % de punto básico... 15,02 bps plano.** El campo
  `fees.zeroExFee` sale a 4 044 987 sobre 2 692 605 025 (15,02 bps) y sigue en
  15,02 bps a 1, 100 y 1 000 WETH: es un porcentaje del volumen, no un importe
  fijo. **Eso es lo que cobra 0x**, y es la razón de que el build venga apagado.
- **No publica impacto de precio, y no es un olvido.** La v2 lo eliminó: la v1
  tenía `estimatedPriceImpact` y un parámetro para limitarlo, y su documentación
  dice que en la v2 no hay ninguno de los dos. Se comprobó de todas formas
  pidiendo 1, 100 y 1 000 WETH: la respuesta trae **exactamente las mismas
  claves** en los tres casos. Ver el porqué de `price_impact_bps=None` en
  `domain.models.Quote`, que es donde está la trampa que se midió al intentar
  deducirlo.
- **El destino del build cambia por red.** Nueve direcciones distintas para
  nueve redes, igual que en Uniswap y al contrario que en KyberSwap. Ver
  `ROUTERS`.
- **El `value` viene en decimal, no en hexadecimal.** Medido: `"0"`, mientras
  que la API de Uniswap manda `"0x00"` para lo mismo. Leerlo con el lector de
  Uniswap habría dado cero por accidente —el `"0"` no empieza por `0x`— y
  callado un `value` distinto de cero. Los dos lectores aceptan las dos formas.

### El build va apagado, y por qué es una decisión y no una limitación

Técnicamente no hace falta nada más para construirlo: a diferencia de Uniswap,
0x devuelve el `transaction` **dentro** de la propia cotización, así que la
misma llamada que da el precio da el payload. Apagarlo es una decisión sobre el
coste: cada swap que salga por aquí paga ese 15,02 bps, que sobre una operación
de 2 700 $ son unos 4 $. Quien no lo sepa está pagando una comisión que no ve.

Por eso el motor nace **cotizando** —que es gratis y es lo que sirve para
comparar— y sólo construye si se le pide explícitamente con
`enable_swap_build`. Y no basta con negarse a construir: el manifiesto que
publica el motor deja de declarar `PREPARE_TX` y `swap_chains` mientras esté
apagado, así que la interfaz no ofrece un botón que falle al pulsarlo.

### Lo que queda fuera: el endpoint gasless

Se midió `/gasless/quote` —el otro que aparece en la documentación de 0x— y no
se integra, por una razón que no es de formato: ese flujo termina en
`/gasless/submit`, donde **0x emite la transacción** por el usuario. Amigocompora
no firma ni emite, y las capacidades `SIGN_TX` y `BROADCAST_TX` existen en el
enum justo para poder nombrarlas y negarlas. Un motor gasless aquí no podría
completar nunca su propio flujo, y código que no puede terminar no entra.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Final

import structlog

from amigocompora.domain.chains import chain
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
    UnsignedTransaction,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import EXACT, BasisPoints, TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.http_source import JsonSource, as_mapping

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "0x"
HOST: Final = "api.0x.org"
QUOTE_URL: Final = f"https://{HOST}/swap/permit2/quote"

CONFIG_API_KEY: Final = "api_key"
#: Interruptor del build. Es **opcional** y por omisión está apagado: quien lo
#: encienda está aceptando el fee de 0x, que es lo que se mide en el docstring.
CONFIG_ENABLE_BUILD: Final = "enable_swap_build"

#: Lo que se acepta como «sí» al leer el interruptor. Todo lo demás —incluido un
#: valor mal escrito— se lee como «no», que es el lado por el que interesa
#: fallar: encender el build por una errata significaría empezar a cobrarle al
#: usuario sin que lo haya pedido.
_TRUTHY: Final = frozenset({"1", "true", "yes", "on", "si", "sí"})

#: Contrato de destino que la API devolvió para **cada** red, medido el
#: 2026-10-06 cotizando el envoltorio nativo contra la stablecoin del catálogo.
#: Estable entre tamaños distintos de la misma red, y distinto en cada red.
ROUTERS: Final[Mapping[str, str]] = {
    "ethereum": "0x666fedd4cdd4e890a5ad20e7b60975409435a64a",
    "base": "0x4f6f91599858bf0d19fabcf2c5d591fe13f7c059",
    "bsc": "0x2d9d6e538bd3f22323932782aaf89446cacaf9d3",
    "arbitrum": "0xdbcd6d6e3e6ff51648ca73d4274cafd34d22a679",
    "polygon": "0x03f115b015b210f812829ea076a7643ac80c2c97",
    "unichain": "0x4c6112da485a9c86270ecb1b35fed0f776cfb3e9",
    "optimism": "0x44b71d46b00f59f4519f1595b5f9fc7bb6a212c6",
    "avalanche": "0xd377ac1acb5d89683257347827dc9ba9ef166f01",
    "robinhood": "0x6aa80dbbed9ae5ab45fbf61f9644fada3b29326e",
}

#: Las redes que este motor cotiza, derivadas de la tabla de destinos por la
#: misma razón que en Uniswap: son la misma pregunta, y escribirlas dos veces
#: deja abierta la posibilidad de anunciar una red sin destino medido.
SWAP_CHAINS: Final = frozenset(ROUTERS)

#: Dirección con la que se cotiza. `taker` es obligatorio y `cotizar` no recibe
#: destinatario; el centinela fijo hace además que la caché se comparta entre
#: usuarios en vez de tener una entrada por destinatario. El real se usa al
#: construir, que es cuando el `taker` entra en el calldata.
QUOTE_TAKER: Final = "0x1111111111111111111111111111111111111111"

#: Tolerancia de deslizamiento que se pide en la cotización. Afecta al
#: `minBuyAmount` que la API calcula dentro del payload, no a la cifra que se
#: muestra. Medido: con 50 el mínimo es exactamente el 99,5 % del importe.
SLIPPAGE_BPS: Final = 50

#: Deriva máxima tolerada entre la cotización mostrada y la del momento de
#: construir. Mismo valor y mismo razonamiento que en los demás motores.
MAX_QUOTE_DRIFT_BPS: Final = BasisPoints(100)


#: Manifiesto **con el build apagado**, que es como nace el motor. La instancia
#: publica el suyo propio según el interruptor; ver `ZeroExEngine.manifest`.
MANIFEST: Final = EngineManifest(
    engine_id="zeroex",
    name="0x (ZeroEx) — agregado de EVM",
    version="1.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary=(
        "Cotiza repartiendo la orden entre los venues que agrega 0x en nueve "
        "redes EVM. Cotizar es gratis; construir paga una comisión de volumen "
        "del 0,15 %, así que viene desactivado y se activa si se quiere."
    ),
    capabilities=frozenset({Capability.READ_CHAIN, Capability.COMPUTE_ROUTE}),
    # Vacío a propósito: sin `swap_chains` el registro no lo ofrece como
    # constructor de ninguna red, y la interfaz no pinta el botón. Es la forma
    # de que «apagado» sea un hecho comprobable y no una condición escondida
    # dentro de una función.
    swap_chains=frozenset(),
    # Después de Uniswap a propósito, y por un motivo medido: sus 15,02 bps de
    # comisión lo hacen peor en las ocho redes donde compiten. Va detrás, y
    # entra solo donde Uniswap no llega —Robinhood— porque la prioridad la
    # decide el motor, no la red.
    swap_priority=100,
    required_config=(CONFIG_API_KEY,),
    optional_config=(CONFIG_ENABLE_BUILD,),
    allowed_hosts=(HOST,),
)


class ZeroExEngine:
    """Cotizaciones —y, si se activa, payloads— por la API agregadora de 0x."""

    __slots__ = ("_build_enabled", "_clock", "_manifest", "_source")

    def __init__(
        self,
        *,
        api_key: str,
        enable_swap_build: bool = False,
        clock: Clock | None = None,
        ttl_seconds: float = 5.0,
        timeout_seconds: float = 20.0,
        min_interval_seconds: float = 0.5,
    ) -> None:
        if not api_key:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» necesita una clave de API: sin ella la "
                f"cotización responde 401. Añádela en el panel de motores."
            )
        self._clock = clock or SystemClock()
        self._build_enabled = enable_swap_build
        capabilities = {Capability.READ_CHAIN, Capability.COMPUTE_ROUTE}
        if enable_swap_build:
            capabilities.add(Capability.PREPARE_TX)
        self._manifest = replace(
            MANIFEST,
            capabilities=frozenset(capabilities),
            swap_chains=SWAP_CHAINS if enable_swap_build else frozenset(),
        )
        self._source = JsonSource(
            name=SOURCE_NAME,
            allowed_hosts=MANIFEST.allowed_hosts,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            min_interval_seconds=min_interval_seconds,
            # La clave viaja en cabecera; `JsonSource` la guarda sin exponerla ni
            # registrarla. `0x-version` es obligatoria: sin ella la API responde
            # con el formato de la v1, que es otro contrato distinto.
            headers={"0x-api-key": api_key, "0x-version": "v2"},
        )

    @property
    def manifest(self) -> EngineManifest:
        """El manifiesto **de esta instancia**, que depende del interruptor.

        Es lo que hace que apagar el build sea de verdad apagarlo: el registro
        lee este manifiesto al publicar el motor, así que sin `PREPARE_TX` ni
        `swap_chains` no aparece como constructor de ninguna red y la interfaz
        no ofrece un botón que falle. `PROVIDER.manifest` sigue siendo el
        estático, que es el que resuelve la configuración.
        """
        return self._manifest

    @property
    def build_enabled(self) -> bool:
        """Si este motor puede construir, o sólo cotizar."""
        return self._build_enabled

    async def aopen(self) -> None:
        await self._source.aopen()

    async def aclose(self) -> None:
        await self._source.aclose()

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        """El venue agregado, en las redes que la API cotiza.

        Se mira `SWAP_CHAINS` y **no** `self._manifest.swap_chains` a propósito:
        el manifiesto recorta las redes cuando el build está apagado, y cotizar
        sigue funcionando igual. Son dos cosas distintas —dónde se puede
        construir y dónde se puede leer— y confundirlas dejaría la tabla de
        precios vacía al apagar el build.
        """
        return (_venue(chain_key),) if chain_key in SWAP_CHAINS else ()

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        if pair.chain not in SWAP_CHAINS:
            return ()
        if pair.base.address is None or pair.quote.address is None:
            # La API identifica los tokens por dirección de contrato.
            return ()

        payload = await self._quote_payload(pair, amount_in, taker=QUOTE_TAKER)
        if payload is None:
            _log.debug("zeroex.no_route", pair=pair.symbol)
            return ()

        quote = self._to_quote(payload, pair, amount_in)
        return () if quote is None else (quote,)

    async def plan_swap(self, quote: Quote, *, recipient: str) -> UnsignedTransaction:
        """Construye la transacción sin firmar del swap que describe `quote`.

        No firma ni emite: devuelve el payload para que el usuario lo revise.

        Si el build está apagado no se construye **nada**, y el mensaje dice por
        qué: no es una avería, es que cada swap por aquí paga la comisión de
        volumen de 0x y eso lo decide quien opera, no el motor.
        """
        if not self._build_enabled:
            raise UnsupportedOperationError(
                f"el motor «{SOURCE_NAME}» está en modo sólo cotización: construir "
                f"con él paga su comisión de volumen (medida: 0,15 % de lo que "
                f"recibes, ya descontada del importe). Actívalo con "
                f"«{CONFIG_ENABLE_BUILD}» si quieres asumirla, o construye con un "
                f"motor que no la cobre."
            )
        if quote.pair.chain not in SWAP_CHAINS:
            raise UnsupportedOperationError(
                f"este motor sólo construye swaps en las redes EVM que tiene "
                f"medidas: se pidió {quote.pair.chain}."
            )

        # Cotización fresca, a nombre de quien va a recibir: el `taker` entra en
        # el calldata, así que la que se mostró —con el centinela— no sirve para
        # construir. Se usa esta misma respuesta para comparar y para construir.
        payload = await self._quote_payload(quote.pair, quote.amount_in, taker=recipient)
        if payload is None:
            raise NoQuotesError(
                f"«{quote.pair.symbol}» ya no tiene ruta en {SOURCE_NAME}: la que "
                f"viste al cotizar se agotó. Vuelve a cotizar."
            )

        fresh_raw = _uint(payload.get("buyAmount"))
        if fresh_raw is None or fresh_raw <= 0:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» devolvió una cotización sin importe de salida "
                f"legible; no se puede construir nada con ella."
            )
        self._require_same_price(quote, fresh_raw)
        return self._to_unsigned(quote, payload, fresh_raw)

    async def _quote_payload(
        self, pair: TradingPair, amount_in: TokenAmount, *, taker: str
    ) -> Mapping[str, Any] | None:
        """El cuerpo de la respuesta, o `None` si la fuente dice que no hay ruta.

        Se devuelve entero y no la `Quote` del dominio porque el payload de la
        transacción viaja **dentro** de esta misma respuesta: traducir a `Quote`
        y volver a pedir sería gastar una petición de cuota para recibir lo
        mismo.
        """
        chain_key = pair.chain
        raw = await self._source.get_json(
            QUOTE_URL,
            params={
                "chainId": str(_chain_id(chain_key)),
                "sellToken": str(pair.base.address),
                "buyToken": str(pair.quote.address),
                "sellAmount": str(amount_in.raw),
                "taker": taker,
                "slippageBps": str(SLIPPAGE_BPS),
            },
        )
        body = as_mapping(raw, "respuesta", SOURCE_NAME)
        if not body.get("liquidityAvailable"):
            # La API contesta 200 con `liquidityAvailable: false` cuando no hay
            # ruta; no es un error de formato. Medido también en el endpoint
            # gasless. Además, `issues` puede traer avisos de saldo o de
            # allowance del `taker`, que aquí no importan: se cotiza con un
            # centinela que por definición no tiene fondos, y eso no dice nada
            # del par.
            return None
        return body

    def _require_same_price(self, quote: Quote, fresh_raw: int) -> None:
        """Aborta si el precio se movió más de lo tolerado desde lo que se vio."""
        shown_raw = quote.amount_out.raw
        drift = EXACT.divide(Decimal(abs(fresh_raw - shown_raw)), Decimal(shown_raw))
        drift_bps = BasisPoints.from_ratio(drift)
        if drift_bps.value <= MAX_QUOTE_DRIFT_BPS.value:
            return
        token = quote.pair.quote
        _log.info(
            "zeroex.quote_moved",
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
        payload: Mapping[str, Any],
        fresh_raw: int,
    ) -> UnsignedTransaction:
        """Traduce el `transaction` de la respuesta, o falla diciendo qué falló."""
        raw_transaction = payload.get("transaction")
        if raw_transaction is None:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» cotizó pero no devolvió `transaction`: sin "
                f"payload no hay nada que el usuario pueda revisar."
            )
        transaction = as_mapping(raw_transaction, "transaction", SOURCE_NAME)
        chain_key = quote.pair.chain
        spec = chain(chain_key)

        # El destino se contrasta antes de leer nada más: es el dato contra el
        # que el usuario va a firmar, y si no es el medido lo demás da igual.
        destination = _require_known_router(chain_key, transaction.get("to"))

        calldata = transaction.get("data")
        if not isinstance(calldata, str) or not _is_hex(calldata):
            raise SourceResponseError(
                f"«{SOURCE_NAME}» devolvió un `data` que no es hexadecimal válido; "
                f"sin calldata no hay transacción que revisar."
            )

        # Medido: aquí el valor viene en **decimal** (`"0"`), al contrario que en
        # la API de Uniswap, que manda `"0x00"`. El lector acepta las dos formas
        # para no depender de cuál use cada fuente mañana.
        value_raw = _uint(transaction.get("value"))
        if value_raw is None:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» no publicó un `value` legible "
                f"({transaction.get('value')!r}); no se puede saber cuánto nativo "
                f"mueve la transacción."
            )

        gas_limit = _uint(transaction.get("gas"))
        out_token = quote.pair.quote
        fresh = TokenAmount(fresh_raw, out_token.decimals, out_token.symbol)
        _log.info(
            "zeroex.swap_planned",
            pair=quote.pair.symbol,
            chain=chain_key,
            out_raw=fresh_raw,
            destination=destination,
            value_raw=value_raw,
        )
        return UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=destination,
            calldata=calldata,
            value=TokenAmount(value_raw, spec.native_decimals, spec.native_symbol),
            gas_limit=gas_limit,
            description=(
                f"Swap en {quote.venue.name}: entregas {quote.amount_in} y recibes "
                f"{fresh}, con un {SLIPPAGE_BPS / 100:g} % de deslizamiento "
                f"tolerado. El contrato de destino es {destination}, el que "
                f"«{SOURCE_NAME}» tiene medido para {spec.name}. Esta transacción "
                f"no incluye la aprobación previa del token, si hiciera falta."
            ),
        )

    def _to_quote(
        self,
        payload: Mapping[str, Any],
        pair: TradingPair,
        amount_in: TokenAmount,
    ) -> Quote | None:
        """Traduce la respuesta, o `None` si no describe lo que se preguntó."""
        out_raw = _uint(payload.get("buyAmount"))
        in_raw = _uint(payload.get("sellAmount"))
        if out_raw is None or in_raw is None or out_raw <= 0:
            _log.debug("zeroex.quote_skipped_unreadable", pair=pair.symbol)
            return None

        # La API declara el modo. Si no es entrada exacta, la cifra es de otra
        # clase de orden y no se puede comparar contra las demás.
        if payload.get("mode") not in (None, "exact-in"):
            _log.info("zeroex.quote_skipped_mode", pair=pair.symbol, mode=payload.get("mode"))
            return None
        if in_raw != amount_in.raw:
            _log.info(
                "zeroex.quote_skipped_partial",
                pair=pair.symbol,
                requested_raw=amount_in.raw,
                quoted_raw=in_raw,
            )
            return None

        fee = _fee_bps(payload, pair)
        return Quote(
            venue=_venue(pair.chain),
            engine_id=MANIFEST.engine_id,
            pair=pair,
            amount_in=amount_in,
            amount_out=TokenAmount(out_raw, pair.quote.decimals, pair.quote.symbol),
            fee_bps=fee,
            # Los dos campos van juntos o no va ninguno: una comisión sin
            # procedencia no describe nada. Ver `Quote.__post_init__`.
            fee_basis=Measurement.REPORTED if fee is not None else None,
            # Ver `domain.models.Quote`: la v2 de esta API no publica impacto, y
            # deducirlo con el método habitual da un número falso y medido.
            price_impact_bps=None,
            impact_basis=None,
            observed_at=self._clock.now(),
            liquidity=None,
            source_note=_route_note(payload),
        )


# --------------------------------------------------------------------------- #
# Lectura del formato de la fuente
# --------------------------------------------------------------------------- #
def _chain_id(chain_key: str) -> int:
    return chain(chain_key).require_eip155_id()


def _venue(chain_key: str) -> Venue:
    """El venue agregado de esa red. Uno por red, no uno global."""
    return Venue(
        venue_id=f"zeroex@{chain_key}",
        name=f"0x (agregado, {chain(chain_key).name})",
        kind=VenueKind.DEX,
        chain=chain_key,
    )


def _require_known_router(chain_key: str, returned: Any) -> str:
    """Comprueba el destino contra el medido, o se niega a construir.

    Comprueba **identidad**, no forma: un destino distinto es otro contrato, y
    el usuario firmaría contra él. Se indexa la tabla sin defensa porque no
    puede faltar: `SWAP_CHAINS` se deriva de ella.
    """
    expected = ROUTERS[chain_key]
    if not isinstance(returned, str) or returned.strip().lower() != expected.lower():
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió como destino del swap {returned!r} en "
            f"«{chain_key}», y el contrato medido para esa red es {expected}. No "
            f"se construyó nada: un destino distinto es un contrato distinto, y "
            f"firmar contra él entregaría los fondos a quien no es."
        )
    return expected


def _uint(value: Any) -> int | None:
    """Lee un entero sin signo, en decimal **o** en hexadecimal.

    Las dos formas hacen falta: medidas, la API de 0x manda `"0"` y `"264203"`
    —decimal— donde la de Uniswap manda `"0x00"`. Un lector que sólo entendiera
    hexadecimal habría leído ese `"0"` como ilegible y abortado cada swap; uno
    que sólo entendiera decimal habría leído `"0x00"` igual de mal. Se aceptan
    las dos y se documenta cuál usa cada fuente, para que nadie tenga que
    adivinarlo.

    Se rechaza el signo: los importes de esta API son magnitudes, y un `"-1"`
    aquí sería una respuesta que no se entiende, no un número negativo que haya
    que interpretar.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("0x"):
        try:
            return int(text, 16)
        except ValueError:
            return None
    if not text.isdigit():
        # Ni hexadecimal ni entero decimal: `"1.5"` son unidades mínimas de
        # token, y media unidad indivisible no existe.
        return None
    return int(text)


def _is_hex(value: str) -> bool:
    """Si el texto es calldata hexadecimal: `0x` y una longitud par de dígitos."""
    text = value.strip()
    if not text.startswith("0x") or len(text) <= 2:
        return False
    body = text[2:]
    return len(body) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in body)


def _fee_bps(payload: Mapping[str, Any], pair: TradingPair) -> BasisPoints | None:
    """La comisión de 0x, medida sobre lo que se recibe.

    Se suman `zeroExFee` y `integratorFee` —las dos son comisiones que paga el
    usuario— y **sólo** si están expresadas en el token que se recibe: la
    proporción se calcula contra `buyAmount`, así que una comisión cobrada en
    otro token haría que la división no significara nada. Si alguna viene en
    otro token se devuelve `None`, que es «no se sabe», antes que un número que
    mezcla unidades.

    El gas queda fuera a propósito: `totalNetworkFee` es lo que cuesta emitir,
    no lo que cobra el venue, y contarlo como comisión inflaría la cifra con un
    concepto que no es suyo — el mismo criterio que en los demás motores.
    """
    fees = payload.get("fees")
    if not isinstance(fees, dict):
        return None
    buy_address = pair.quote.address
    out_raw = _uint(payload.get("buyAmount"))
    if out_raw is None or out_raw <= 0:
        return None

    total = 0
    for field in ("zeroExFee", "integratorFee"):
        entry = fees.get(field)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            return None
        token = entry.get("token")
        if not isinstance(token, str) or token.lower() != str(buy_address).lower():
            _log.info("zeroex.fee_in_another_token", field=field, token=token)
            return None
        amount = _uint(entry.get("amount"))
        if amount is None:
            return None
        total += amount

    if total == 0:
        # Medido: nunca llega a cero con la clave actual. Un cero aquí no es
        # «gratis», es que la fuente no lo ha puesto; y una comisión publicada
        # de 0 no se acepta nunca en este proyecto.
        return None
    return BasisPoints.from_ratio(EXACT.divide(Decimal(total), Decimal(out_raw)))


def _route_note(payload: Mapping[str, Any]) -> str:
    """Describe por dónde pasa la ruta y qué no publica la fuente."""
    route = payload.get("route")
    raw_fills = route.get("fills") if isinstance(route, dict) else None
    sources: list[str] = []
    if isinstance(raw_fills, list):
        for fill in raw_fills:
            if isinstance(fill, dict) and isinstance(fill.get("source"), str):
                name = fill["source"]
                if name not in sources:
                    sources.append(name)
    detail = ", ".join(sources) if sources else "ruta no detallada"
    return (
        f"Ruta calculada por {SOURCE_NAME} sobre: {detail}. "
        f"El importe recibido ya viene neto de comisión e impacto. La fuente no "
        f"publica el impacto de precio en su versión 2, así que no se muestra; "
        f"el importe recibido ya lo lleva dentro."
    )


@dataclass(frozen=True, slots=True)
class ZeroExProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> ZeroExEngine:
        # `required_config` garantiza la clave: `validate_config` corre antes de
        # llegar aquí. El interruptor es opcional y ausente significa apagado.
        raw = config.get(CONFIG_ENABLE_BUILD, "").strip().lower()
        return ZeroExEngine(
            api_key=config[CONFIG_API_KEY],
            enable_swap_build=raw in _TRUTHY,
        )


PROVIDER: Final = ZeroExProvider()
