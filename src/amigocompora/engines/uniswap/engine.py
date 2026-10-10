"""Motor DEX sobre la Trading API de Uniswap: la vía **agregada** de EVM.

Ethereum y sus L2 llevaban sin fuente ejecutable desde el principio: las dos que
había —DexScreener y GeckoTerminal— leen pools y publican reservas, pero ninguna
construye una transacción. Este motor cierra ese hueco con la API propia de
Uniswap, que además de cotizar devuelve el payload listo para firmar.

### Por qué hace falta una clave, y qué se hizo antes de pedirla

La API devuelve **HTTP 401 `Unauthenticated api key or session`** a cualquier
petición de cotización sin clave —medido, no supuesto—. Antes de aceptar que
hacía falta, se comprobó si quedaba un camino sin clave, y sí lo hay: los pools
de Uniswap V3 leídos directamente por JSON-RPC público cotizan de verdad. Ese es
el motor `uniswap_v3`, la vía **directa**, y está en su propio paquete porque
usa contratos distintos y una ABI distinta.

Los dos conviven, y no es duplicación: el directo no necesita clave y no se
detiene nunca; el agregado da mejor precio —la misma medición por la API dio
0,00037068 WETH frente a 0,00036836 del router leído por RPC, un 0,63 % más—
porque reparte la orden entre V2, V3, V4 y rutas mixtas en vez de ceñirse a los
pools de una sola versión. Por eso el agregado va primero en `swap_priority` y el
directo queda de respaldo.

### Lo que se midió contra la API real

- **Cubre las ocho redes EVM del registro**: ethereum, base, bsc, arbitrum,
  polygon, unichain, optimism y avalanche. Y no siempre por V3: medido, Unichain
  se resuelve al 100 % por **V4** y Optimism con una ruta **mixta**. Arc queda
  fuera, como en las demás fuentes EVM: cobra el gas en USDC y no tiene
  envoltorio nativo que cotizar.
- **El nativo se nombra con la dirección cero.** Medido el 2026-10-09: con el
  centinela que usan otros agregados (`0xEeee…`) la API responde
  `NoRouteFoundError`; con la dirección cero cotiza y construye el nativo en
  las dos direcciones —1 POL contra pUSD salió por el pool V2 de WPOL, y USDC
  contra POL por el Universal Router—. Al vender nativo la transacción lleva el
  `value` con el importe y el router lo envuelve él mismo; al comprarlo, el
  `value` va a cero y es el router quien lo desenvuelve. Ver `_api_address`.
- **`priceImpact` es un porcentaje, no una fracción.** El campo homónimo de
  Jupiter —`priceImpactPct`— sí es una fracción, así que reutilizar aquel lector
  habría metido un error de cien veces en cada cotización. La prueba: 1 WETH
  (unos 2 700 $) publica `0.01`, y un 1 % de impacto en una orden así sería
  absurdo; los tramos de comisión que la propia ruta declara van de 0,01 % a
  0,3 %, que es el orden de magnitud correcto.
- **Y devuelve `None` cuando no lo puede calcular.** Medido: a partir de unos
  10 WETH el campo llega nulo mientras el precio unitario sí se degrada de
  verdad (0,11 % a 1 000 WETH, 0,24 % a 2 000 WETH). Es decir, el campo **no es
  fiable en las dos direcciones**: ni siempre está, ni siempre es cero. Como
  `Quote.price_impact_bps` no admite «desconocido», una cotización sin impacto
  legible **no se emite**. Inventar un cero ahí diría «esta orden no mueve el
  precio», que es justo lo contrario de lo que pasa en las órdenes grandes.
- **El destino del build cambia por red.** A diferencia de KyberSwap, cuyo
  router es una única dirección en sus nueve redes, aquí salieron **siete
  direcciones distintas en ocho redes** (polygon y bsc comparten una). Por eso no
  se puede fijar un router único: se fija una **tabla medida por red** y se
  rechaza cualquier otra. Ver `ROUTERS`.
- **Los ocho routers cobran el token vía Permit2.** Medido el 2026-10-09 con
  `eth_getCode`: los ocho llevan `0x0000…78BA3` dentro de su bytecode (24 381
  bytes idénticos), así que **no** hacen `transferFrom` ellos mismos. El payload
  declara el permiso encadenado (`TokenApproval(via=PERMIT2)`); sin él, aprobar
  directo al router no sirve de nada y el swap revierte al estimar el gas.

### La defensa que esto permite, y por qué es necesaria

Que la API devuelva el contrato de destino es exactamente el punto por donde un
intermediario —o la propia API, comprometida— podría desviar los fondos: el
usuario firmaría contra un contrato que no es el de Uniswap. La tabla fija esa
posibilidad: si el destino no es el medido para esa red, **no se construye
nada**. Es la misma decisión que en KyberSwap y por la misma razón.

Lo que la tabla **no** garantiza, y conviene decirlo: que esas direcciones sean
los routers canónicos de Uniswap. Se verificó que tienen bytecode en la cadena
—24 KB la de Ethereum, que descarta que sean cuentas sin código— y que son
estables entre cotizaciones de tamaños distintos. No se verificó su identidad
contra una fuente independiente, porque no hay una que las publique. Si Uniswap
rota un router, este motor **falla y lo dice** en vez de construir en silencio
contra otro contrato, y la tabla se vuelve a medir con el mismo procedimiento.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    Token,
    TokenApproval,
    TradingPair,
    UnsignedTransaction,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import EXACT, BasisPoints, TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.http_source import JsonSource, as_mapping, optional_decimal

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "Uniswap"
HOST: Final = "trade-api.gateway.uniswap.org"
BASE_URL: Final = f"https://{HOST}/v1"
QUOTE_URL: Final = f"{BASE_URL}/quote"
SWAP_URL: Final = f"{BASE_URL}/swap"

#: Clave con la que el motor busca su credencial. El nombre es el que usa
#: `infra.secrets.secret_key` para componer la entrada del keyring, así que
#: cambiarlo aquí la invalida: es una clave de configuración, no una etiqueta.
CONFIG_API_KEY: Final = "api_key"

#: Contrato de destino que la API devolvió para **cada** red, medido el
#: 2026-10-06 cotizando el envoltorio nativo contra la stablecoin del catálogo y
#: construyendo la transacción. Se guarda la forma con checksum; la comparación
#: se hace en minúsculas porque el checksum es una propiedad de presentación.
#:
#: Que sean siete direcciones distintas para ocho redes es el dato que obliga a
#: esta tabla: no hay un router único que fijar. Y es la razón de que una red
#: nueva **no** se añada a `swap_chains` sin medirla antes: sin su entrada aquí,
#: el motor no la ofrece siquiera, que es el lado correcto por el que fallar.
ROUTERS: Final[Mapping[str, str]] = {
    "ethereum": "0x23617e59A5925b2A4Bf75d73ff6711cD0b29De85",
    "base": "0xd6145b2D3F379919E8CdEda7B97e37c4b2Ca9c40",
    "arbitrum": "0x2d01411773c8C24805306E89A41F7855C3c4Fe65",
    "polygon": "0xDc264714F68d84CF29BC605589405E78bDBE7C9f",
    "bsc": "0xDc264714F68d84CF29BC605589405E78bDBE7C9f",
    "unichain": "0xD1b797D92d87B688193A2B976eFc8D577D204343",
    "optimism": "0xC09255D86DB563cBc11C2fCf4a0C512e160111B4",
    "avalanche": "0x661E93cca42AfacB172121EF892830cA3b70F08d",
}

#: Las redes donde la API construye, medidas una a una contra el endpoint real.
#:
#: Se **deriva** de la tabla de destinos en vez de escribirse aparte: las dos
#: listas son la misma pregunta —«¿dónde sé construir?»— y mantenerlas a mano
#: dejaba abierta la posibilidad de anunciar una red sin destino medido, que es
#: ofrecer un botón que falla al pulsarlo.
SWAP_CHAINS: Final = frozenset(ROUTERS)

#: Permit2. Se **midió** el 2026-10-09: los ocho routers de `ROUTERS` llevan
#: esta dirección dentro de su bytecode desplegado (los ocho, 24 381 bytes
#: idénticos, con `eth_getCode`), así que cobran el token pidiéndoselo a Permit2
#: y **no** con un `transferFrom` propio. Sin declarar el permiso encadenado, la
#: aprobación directa al router no sirve de nada y el swap revierte al estimar
#: el gas —pasó de verdad, con pUSD contra POL—. Es la misma dirección en todas
#: las redes porque se desplegó con CREATE2 (igual que en uniswap_v3 y v4, que
#: también lo declaran); cada motor escribe la suya para no depender de otro.
PERMIT2: Final = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

#: Dirección con la que se **cotiza**. No es la del usuario y no puede serlo:
#: `cotizar` no recibe destinatario, y la API exige uno. Se usa un centinela
#: fijo en vez de la del usuario por una razón concreta: la cotización es
#: indicativa —precio de un tamaño, no una operación de nadie— y con un
#: centinela la caché se comparte entre usuarios en vez de tener una entrada por
#: destinatario. El `swapper` real se usa al construir, que es cuando importa.
QUOTE_SWAPPER: Final = "0x1111111111111111111111111111111111111111"

#: Dirección con la que la API nombra la **moneda nativa**. Medido el 2026-10-09:
#: con el centinela que usan otros agregados (`0xEeee…`) la API responde
#: `NoRouteFoundError`; con la dirección cero cotiza y construye el nativo en
#: las dos direcciones —1 POL contra pUSD salió por el pool V2 de WPOL, y USDC
#: contra POL por el Universal Router—, con el `value` puesto al vender y a cero
#: al comprar, porque el router envuelve y desenvuelve él mismo. Cada API nombra
#: al nativo como quiere, y este archivo usa la de esta.
NATIVE_ADDRESS: Final = "0x0000000000000000000000000000000000000000"

#: Tolerancia de deslizamiento que se pide **al cotizar** (la pantalla). No se
#: ejecuta nada, así que no protege de nada: es un parámetro obligatorio y afecta
#: al mínimo garantizado que la API calcula dentro del payload, no a la cifra que
#: se muestra. Al **construir** manda la que traiga `plan_swap` —la que el
#: usuario haya fijado con el engranaje— y ésta sólo es el respaldo.
SLIPPAGE_PCT: Final = 0.5

#: Preferencia de ruteo. `BEST_PRICE` es lo que se midió; cambiarla cambiaría el
#: contrato de destino en algunas redes, y la tabla `ROUTERS` dejaría de valer.
ROUTING: Final = "BEST_PRICE"

#: Deriva máxima tolerada entre la cotización que el usuario vio y la que hay al
#: construir. Mismo valor y mismo razonamiento que en Jupiter: el mercado se
#: mueve entre que se pinta la tabla y que se pulsa el botón.
MAX_QUOTE_DRIFT_BPS: Final = BasisPoints(100)

#: Impacto por encima del cual la cotización se omite, como en los demás motores.
MAX_PRICE_IMPACT: Final = BasisPoints(1_000)


MANIFEST: Final = EngineManifest(
    engine_id="uniswap",
    name="Uniswap — agregado de EVM",
    version="1.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary=(
        "Cotiza repartiendo la orden entre los pools V2, V3, V4 y las rutas "
        "mixtas de Uniswap, y construye la transacción sin firmar. Cubre ocho "
        "redes EVM. Necesita una clave de la API; sin ella no responde."
    ),
    capabilities=frozenset(
        {Capability.READ_CHAIN, Capability.COMPUTE_ROUTE, Capability.PREPARE_TX}
    ),
    swap_chains=SWAP_CHAINS,
    # Va **antes** que los motores sin clave. No es una afirmación de que su
    # ruta sea siempre mejor —eso lo decide la comparación, con precios— sino
    # que, cuando los dos llegan a la misma red, se le pide el payload primero a
    # la fuente que ve más venues. El respaldo gratuito existe para que una
    # cuota agotada no deje al usuario sin operar, no para competir por defecto.
    swap_priority=10,
    required_config=(CONFIG_API_KEY,),
    allowed_hosts=(HOST,),
)


class UniswapEngine:
    """Cotizaciones y payloads de swap por la API agregadora de Uniswap."""

    __slots__ = ("_clock", "_source")

    def __init__(
        self,
        *,
        api_key: str,
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
        self._source = JsonSource(
            name=SOURCE_NAME,
            allowed_hosts=MANIFEST.allowed_hosts,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            # Más holgado que el ritmo de una API pública: aquí la cuota la fija
            # la clave del usuario, y medido cada cotización ronda el segundo.
            min_interval_seconds=min_interval_seconds,
            # La clave viaja en cabecera y `JsonSource` la guarda sin exponerla
            # ni registrarla. No hay ningún punto donde se imprima.
            headers={"x-api-key": api_key},
        )

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    async def aopen(self) -> None:
        await self._source.aopen()

    async def aclose(self) -> None:
        await self._source.aclose()

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        """El venue agregado, en las redes que la API cubre.

        No se consulta la API: aquí no hay venues que enumerar. El venue **es**
        el agregador, y existe siempre que exista la red. Gastar una petición en
        confirmarlo sería gastarla para nada.
        """
        return (_venue(chain_key),) if chain_key in SWAP_CHAINS else ()

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        if pair.chain not in SWAP_CHAINS:
            return ()

        payload = await self._quote_payload(pair, amount_in, swapper=QUOTE_SWAPPER)
        if payload is None:
            _log.debug("uniswap.no_route", pair=pair.symbol)
            return ()

        quote = self._to_quote(payload, pair, amount_in)
        return () if quote is None else (quote,)

    async def plan_swap(
        self,
        quote: Quote,
        *,
        recipient: str,
        slippage_bps: int | None = None,
    ) -> UnsignedTransaction:
        """Construye la transacción sin firmar del swap que describe `quote`.

        No firma ni emite: devuelve el payload para que el usuario lo revise.

        ### Por qué vuelve a cotizar, y con el destinatario real

        Se re-cotiza antes de construir por la misma razón que en Jupiter —la
        cotización mostrada puede tener minutos— y además por una propia de esta
        API: la cotización lleva dentro el `swapper`, así que la que se mostró
        apunta al centinela. Construir desde ella daría un payload a nombre de
        una dirección que no es la del usuario.

        Se cotiza una sola vez, con el destinatario real, y esa misma respuesta
        se usa para las dos cosas: comparar contra el precio mostrado y construir.
        Si el `swapper` influyera en el precio, la comparación lo detectaría y se
        negaría a construir — que es la forma correcta de fallar ante algo que no
        se ha medido.

        `slippage_bps` es la tolerancia con la que se fija el mínimo garantizado
        dentro del payload. Sin él se usa la constante del módulo: es lo que
        piden las pruebas y quien llame sin configurar nada.
        """
        slippage_pct = SLIPPAGE_PCT if slippage_bps is None else slippage_bps / 100
        if quote.pair.chain not in SWAP_CHAINS:
            raise UnsupportedOperationError(
                f"este motor sólo construye swaps en las redes EVM que tiene "
                f"medidas: se pidió {quote.pair.chain}. Activa un motor que cubra "
                f"esa red."
            )

        # 1. Cotización fresca, a nombre de quien va a recibir.
        payload = await self._quote_payload(
            quote.pair, quote.amount_in, swapper=recipient, slippage_pct=slippage_pct
        )
        if payload is None:
            raise NoQuotesError(
                f"«{quote.pair.symbol}» ya no tiene ruta en {SOURCE_NAME}: la que "
                f"viste al cotizar se agotó. Vuelve a cotizar."
            )

        fresh_raw = _raw_amount(payload.get("output", {}).get("amount"))
        if fresh_raw is None or fresh_raw <= 0:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» devolvió una cotización sin importe de salida "
                f"legible; no se puede construir nada con ella."
            )
        self._require_same_price(quote, fresh_raw)

        # 2. Construir desde **esa** cotización, la fresca. Sin caché: el
        #    payload describe una transacción y no se puede reutilizar.
        response = await self._source.post_json(SWAP_URL, json_body={"quote": payload})
        body = as_mapping(response, "respuesta de swap", SOURCE_NAME)
        return self._to_unsigned(quote, body, fresh_raw, slippage_pct=slippage_pct)

    async def _quote_payload(
        self,
        pair: TradingPair,
        amount_in: TokenAmount,
        *,
        swapper: str,
        slippage_pct: float = SLIPPAGE_PCT,
    ) -> Mapping[str, Any] | None:
        """El objeto `quote` **crudo** de la API, o `None` si no hay ruta.

        Se devuelve crudo y no la `Quote` del dominio porque es lo que el
        endpoint de swap necesita: ésta es una lectura derivada y no conserva
        los campos —`quoteId`, `route`, `blockNumber`— que la API espera de
        vuelta. Ver `plan_swap`.

        `slippage_pct` entra en la petición y en la clave de caché: la respuesta
        lleva dentro el mínimo garantizado, así que dos tolerancias distintas
        no pueden compartir entrada.
        """
        chain_key = pair.chain
        chain_id = _chain_id(chain_key)
        amount = amount_in.raw
        body = {
            "tokenInChainId": chain_id,
            "tokenIn": _api_address(pair.base),
            "tokenOutChainId": chain_id,
            "tokenOut": _api_address(pair.quote),
            "amount": str(amount),
            "type": "EXACT_INPUT",
            "swapper": swapper,
            "slippageTolerance": slippage_pct,
            "routingPreference": ROUTING,
        }
        raw = await self._source.post_json(
            QUOTE_URL,
            json_body=body,
            # La clave de caché se compone de lo que de verdad decide la
            # respuesta. El `swapper` entra porque la cotización lleva dentro
            # los datos de permiso de **esa** dirección: compartir la entrada
            # entre destinatarios devolvería un payload a nombre de otro.
            cache_key=_cache_key(chain_key, pair, amount_in, swapper, slippage_pct),
        )
        envelope = as_mapping(raw, "respuesta", SOURCE_NAME)
        quote = envelope.get("quote")
        if quote is None:
            # Sin `quote` y sin error HTTP: la API respondió que ahí no hay ruta
            # para ese tamaño. Es una respuesta, no un formato roto.
            return None
        return as_mapping(quote, "quote", SOURCE_NAME)


    def expected_destination(self, chain_key: str) -> str | None:
        """El router de la tabla medida para esa red, o `None` si no la cubre.

        Se lee de `ROUTERS` en vez de escribirse aparte porque es la misma
        pregunta —«¿a qué contrato manda este motor los swaps de esta red?»— y
        mantener dos listas abre la posibilidad de declarar una red cuya
        dirección ya no esté en la tabla de construcción.
        """
        return ROUTERS.get(chain_key)

    def _require_same_price(self, quote: Quote, fresh_raw: int) -> None:
        """Aborta si el precio se movió más de lo tolerado desde lo que se vio."""
        shown_raw = quote.amount_out.raw
        drift = EXACT.divide(Decimal(abs(fresh_raw - shown_raw)), Decimal(shown_raw))
        drift_bps = BasisPoints.from_ratio(drift)
        if drift_bps.value <= MAX_QUOTE_DRIFT_BPS.value:
            return
        token = quote.pair.quote
        _log.info(
            "uniswap.quote_moved",
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
        *,
        slippage_pct: float = SLIPPAGE_PCT,
    ) -> UnsignedTransaction:
        """Traduce la respuesta del swap, o falla diciendo qué campo no cuadró."""
        payload = as_mapping(body.get("swap"), "swap", SOURCE_NAME)
        chain_key = quote.pair.chain
        spec = chain(chain_key)

        # El destino se contrasta con el medido **antes** de leer nada más: si no
        # coincide, lo demás da igual, porque este es el dato contra el que el
        # usuario va a firmar.
        destination = _require_known_router(chain_key, payload.get("to"))

        calldata = payload.get("data")
        if not isinstance(calldata, str) or not _is_hex(calldata):
            raise SourceResponseError(
                f"«{SOURCE_NAME}» devolvió un `data` que no es hexadecimal válido; "
                f"sin calldata no hay transacción que revisar."
            )

        # El valor se lee en hexadecimal, que es como lo manda la API (`"0x00"`).
        # Un ERC-20 contra ERC-20 lo trae a cero, y sólo deja de estarlo cuando
        # entra o sale el nativo.
        value_raw = _hex_int(payload.get("value"))
        if value_raw is None or value_raw < 0:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» no publicó un `value` legible "
                f"({payload.get('value')!r}); no se puede saber cuánto nativo "
                f"mueve la transacción."
            )

        gas_limit = _positive_int(payload.get("gasLimit"))
        out_token = quote.pair.quote
        fresh = TokenAmount(fresh_raw, out_token.decimals, out_token.symbol)
        # El nativo se paga como `value` —y entonces no hay ERC-20 que aprobar—;
        # todo lo demás lo cobra el router vía Permit2 (medido en su bytecode,
        # ver `PERMIT2`). Declararlo aquí es lo que hace que el ejecutor conceda
        # los dos permisos encadenados antes de firmar; sin esta línea aprobaba
        # directo al router, que nunca cobra así, y el swap revertía.
        pays_native = value_raw > 0
        approval = (
            None
            if pays_native
            else TokenApproval(spender=destination, via=PERMIT2)
        )
        _log.info(
            "uniswap.swap_planned",
            pair=quote.pair.symbol,
            chain=chain_key,
            out_raw=fresh_raw,
            destination=destination,
            value_raw=value_raw,
            approval_via=approval.via if approval is not None else None,
        )
        return UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=destination,
            calldata=calldata,
            value=TokenAmount(value_raw, spec.native_decimals, spec.native_symbol),
            gas_limit=gas_limit,
            approval=approval,
            description=(
                f"Swap en {quote.venue.name}: entregas {quote.amount_in} y recibes "
                f"{fresh}, con {slippage_pct:g} % de deslizamiento tolerado. El "
                f"contrato de destino es {destination}, el que «{SOURCE_NAME}» "
                f"tiene medido para {spec.name}. "
                + (
                    "Se entrega el nativo de la red, así que no hay permiso de "
                    "token que conceder."
                    if pays_native
                    else (
                        f"El permiso del token, si hiciera falta, va encadenado "
                        f"por Permit2 ({PERMIT2}): este router no mueve el token "
                        f"él mismo."
                    )
                )
            ),
        )

    def _to_quote(
        self,
        payload: Mapping[str, Any],
        pair: TradingPair,
        amount_in: TokenAmount,
    ) -> Quote | None:
        """Traduce la respuesta, o `None` si no describe lo que se preguntó."""
        out_raw = _raw_amount(payload.get("output", {}).get("amount"))
        in_raw = _raw_amount(payload.get("input", {}).get("amount"))
        if out_raw is None or in_raw is None or out_raw <= 0:
            _log.debug("uniswap.quote_skipped_unreadable", pair=pair.symbol)
            return None

        # En `EXACT_INPUT` el importe de entrada es el que se pidió. Si no
        # coincide, la cotización es de otro tamaño y mezclarla daría un precio
        # que no es el de nadie.
        if in_raw != amount_in.raw:
            _log.info(
                "uniswap.quote_skipped_partial",
                pair=pair.symbol,
                requested_raw=amount_in.raw,
                quoted_raw=in_raw,
            )
            return None

        impact = _impact_bps(payload.get("priceImpact"))
        if impact is None:
            # Ver el docstring del módulo: la API omite el impacto en las órdenes
            # grandes, que son justo donde importa. Una cotización sin él no se
            # emite, porque el dominio exige la cifra y no se inventa.
            _log.info(
                "uniswap.quote_skipped_impact_missing",
                pair=pair.symbol,
                amount_raw=amount_in.raw,
            )
            return None
        if abs(impact).value > MAX_PRICE_IMPACT.value:
            _log.debug(
                "uniswap.quote_skipped_impact",
                pair=pair.symbol,
                impact_bps=impact.value,
            )
            return None

        return Quote(
            venue=_venue(pair.chain),
            engine_id=MANIFEST.engine_id,
            pair=pair,
            amount_in=amount_in,
            amount_out=TokenAmount(out_raw, pair.quote.decimals, pair.quote.symbol),
            # La API no desglosa la comisión del swap: `output.amount` ya viene
            # neto y lo único que publica por separado es el gas, que no es una
            # comisión del venue. Mismo caso que Jupiter, y por eso `None`.
            fee_bps=None,
            fee_basis=None,
            price_impact_bps=impact,
            observed_at=self._clock.now(),
            liquidity=None,
            impact_basis=Measurement.REPORTED,
            source_note=_route_note(payload),
        )


# --------------------------------------------------------------------------- #
# Lectura del formato de la fuente
# --------------------------------------------------------------------------- #
def _chain_id(chain_key: str) -> int:
    """Chain id EIP-155 de una red del registro, con error legible si no lo tiene."""
    return chain(chain_key).require_eip155_id()


def _venue(chain_key: str) -> Venue:
    """El venue agregado de esa red. Uno por red, no uno global.

    `venue_id` lleva la red porque el mismo agregado en dos redes son dos sitios
    donde operar: un precio en Ethereum no es comparable con uno en Base, y
    darles el mismo identificador los haría parecer el mismo mercado.
    """
    return Venue(
        venue_id=f"uniswap@{chain_key}",
        name=f"Uniswap (agregado, {chain(chain_key).name})",
        kind=VenueKind.DEX,
        chain=chain_key,
    )


def _cache_key(
    chain_key: str,
    pair: TradingPair,
    amount_in: TokenAmount,
    swapper: str,
    slippage_pct: float,
) -> str:
    """Clave de caché de una cotización: todo lo que la respuesta depende de.

    Las direcciones se normalizan como en la petición (`_api_address`): la misma
    moneda nativa tiene que dar la misma entrada se nombre como se nombre.
    """
    return "|".join(
        (
            chain_key,
            _api_address(pair.base),
            _api_address(pair.quote),
            str(amount_in.raw),
            swapper.lower(),
            str(slippage_pct),
        )
    )


def _api_address(token: Token) -> str:
    """La dirección con la que la API nombra al token; el nativo tiene la suya.

    El nativo no tiene contrato, y esta API lo nombra con la **dirección cero**
    (ver `NATIVE_ADDRESS`). Traducirlo aquí y no en cada llamada deja una sola
    regla: la petición, su clave de caché y el build hablan el mismo idioma.
    """
    return token.address if token.address is not None else NATIVE_ADDRESS


def _require_known_router(chain_key: str, returned: Any) -> str:
    """Comprueba el destino contra el medido, o se niega a construir.

    No es una validación de forma —`0x` más 40 hex pasarían cualquiera— sino de
    **identidad**: un destino distinto es otro contrato, y el usuario firmaría
    contra él. Ver el docstring del módulo.

    Se indexa la tabla sin defensa porque no puede faltar: `SWAP_CHAINS` se
    deriva de ella, y quien llega hasta aquí ya pasó esa comprobación.
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


def _raw_amount(value: Any) -> int | None:
    """Lee un importe en unidades mínimas, que la API manda **como cadena**.

    Medido: `output.amount` llega como `"370676399815965"`, no como número. Se
    exige un entero exacto: son unidades mínimas de token, y aceptar `"1.5"`
    aquí sería aceptar media unidad indivisible.
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


def _hex_int(value: Any) -> int | None:
    """Lee un entero en hexadecimal (`"0x00"`), o `None` si no lo es."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("0x"):
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _positive_int(value: Any) -> int | None:
    """Lee un entero positivo, o `None`. Un gas de cero no es un gas válido."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _is_hex(value: str) -> bool:
    """Si el texto es calldata hexadecimal: `0x` y una longitud par de dígitos."""
    text = value.strip()
    if not text.startswith("0x") or len(text) <= 2:
        return False
    body = text[2:]
    return len(body) % 2 == 0 and all(character in "0123456789abcdefABCDEF" for character in body)


def _impact_bps(value: Any) -> BasisPoints | None:
    """Convierte `priceImpact` a puntos básicos, sabiendo que es un porcentaje.

    **El campo es un porcentaje**, al contrario que `priceImpactPct` de Jupiter,
    que es una fracción. Medido con el precio unitario delante: 1 WETH —unos
    2 700 $— publica `0.01`, que leído como fracción sería un 1 % de impacto en
    una orden minúscula, imposible; y los tramos de comisión que la propia ruta
    declara (`[0.01%]`, `[0.3%]`) fijan el orden de magnitud en centésimas.

    Devuelve `None` cuando la API no publica el campo —medido: lo omite a partir
    de unos 10 WETH—. `None` significa «no lo dice», y quien llama decide: aquí
    se descarta la cotización, porque `Quote` exige la cifra y rellenarla con
    cero afirmaría que la orden no mueve el precio.
    """
    percent = optional_decimal(value)
    if percent is None:
        return None
    return BasisPoints.from_percent(percent)


def _route_note(payload: Mapping[str, Any]) -> str:
    """Describe por dónde pasa la ruta y qué no se desglosa."""
    route = payload.get("routeString")
    detail = route.strip() if isinstance(route, str) and route.strip() else "ruta no detallada"
    parts = [
        f"Ruta calculada por {SOURCE_NAME}: {detail}",
        "El importe recibido ya viene neto de comisión e impacto; la fuente no "
        "desglosa la comisión del swap, sólo el gas.",
    ]
    reasons = payload.get("txFailureReasons")
    if isinstance(reasons, list) and reasons:
        readable = ", ".join(str(reason) for reason in reasons[:3])
        parts.append(f"Avisos de la fuente sobre la transacción: {readable}.")
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class UniswapProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> UniswapEngine:
        # `required_config` garantiza que la clave está: `validate_config` corre
        # antes de llegar aquí y falla con un mensaje accionable si no lo está.
        return UniswapEngine(api_key=config[CONFIG_API_KEY])


PROVIDER: Final = UniswapProvider()
