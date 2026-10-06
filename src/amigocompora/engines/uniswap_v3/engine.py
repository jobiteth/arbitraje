"""Uniswap V3 por contrato, **sin clave**: la vía que no se detiene.

### Qué es y por qué existe

Los motores que cotizan contra una API —Uniswap agregado, 0x— necesitan una
credencial y una cuota. Cuando la cuota se agota o la clave caduca, esa vía se
para. Éste no: habla con la fábrica y el QuoterV2 de Uniswap V3 por nodos
públicos, y construir un swap no cuesta nada porque **no se mueve nada hasta que
el usuario firma**. Es el respaldo que hace que la aplicación siga sirviendo
recién instalada, sin registro, y el que garantiza que «las gratuitas no se
detienen nunca».

### Cotizar aquí no es simular

La cotización se obtiene con `eth_call` contra el QuoterV2, que ejecuta el
cálculo del swap **sobre el estado real del pool en el bloque actual** y devuelve
el importe exacto que daría ahora mismo. No hay ningún valor estimado por
nosotros, ninguna fórmula aproximada y ningún dato de mentira: el número que se
publica es el que la cadena da. `eth_call` no cambia estado y no gasta gas, que
es justo lo que permite que esto funcione sin clave y sin coste.

Y no se cotiza «un pool»: se le pregunta a la fábrica por los cuatro tramos de
comisión (**0,01 %, 0,05 %, 0,30 % y 1 %**) y se comparan. Un par puede tener
pool en varios tramos —medido: bankr/WETH en Base sólo lo tiene en el 1 %, y
WETH/USDC en Ethereum en tres— y quedarse con el primero es quedarse con un
precio peor sin decirlo.

### Los dos routers, y el permiso que decide cuál se puede usar

Medido leyendo el bytecode desplegado: Uniswap V3 tiene **dos** routers y no
comparten ABI ni forma de cobrar. El SwapRouter V1 mueve el token él mismo, así
que basta autorizarle el ERC-20, y su `exactInputSingle` lleva `deadline`. El
SwapRouter02 no toca el token: se lo pide a **Permit2**, y eso son dos permisos
encadenados; además su `exactInputSingle` **no** tiene `deadline`.

La consecuencia no es teórica: **en Base el SwapRouter V1 no está desplegado**.
Sólo existe SwapRouter02, que cobra por Permit2, así que el camino de «un solo
`approve`» no existe en la red donde se hace la prueba. El motor declara el
permiso que corresponde a cada red en el propio payload (`TokenApproval`), y con
eso el camino de ejecución sabe qué autorizar antes de firmar.

La tabla de direcciones está en `addresses.py` y es **medida**, no copiada: cada
contrato atestigua su identidad —la fábrica devolviendo un pool coherente, el
router devolviendo su `factory()` y su `WETH9()`— y cada selector se comprobó
buscándolo literalmente en el dispatcher desplegado. Añadir una red es medirla.

### Lo que este motor NO hace

**No firma y no emite.** Devuelve una `UnsignedTransaction` para que el usuario la
revise; la clave y el envío viven en `infra.evm`, detrás de la barrera de modo y
del diálogo de confirmación. Tampoco **estima el gas**: para eso hace falta la
dirección que paga, que sólo se conoce al firmar —el motor no la tiene y no debe
tenerla—, así que `gas_limit` va a `None` y lo estima quien firma, contra el
estado del momento.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Final

import structlog

from amigocompora.domain.chains import chain
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import (
    InvalidAmountError,
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
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import EXACT, BasisPoints, TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.amm_protocols import parse_dex_id, venue_for
from amigocompora.engines.catalog import wrapped_native
from amigocompora.engines.evm_rpc import ChainReader, rpc_hosts
from amigocompora.engines.uniswap_math import impact_bps
from amigocompora.engines.uniswap_v3 import calldata as abi
from amigocompora.engines.uniswap_v3.addresses import (
    DEPLOYMENTS,
    FEE_TIERS,
    SWAP_CHAINS,
    Deployment,
)

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "Uniswap V3 (directo)"

#: El protocolo, identificado una vez. `parse_dex_id` es el mismo que usan las
#: demás fuentes, así que dos motores que miran el mismo pool producen el mismo
#: `venue_id` — que es lo que permite compararlos en la misma tabla.
PROTOCOL: Final = parse_dex_id("uniswap_v3")

#: Deslizamiento tolerado al construir. Es **la única protección** que el payload
#: lleva contra el movimiento del precio entre que se cotiza y que se mina: el
#: `amountOutMinimum` sale de aquí. Se aplica sobre la cotización fresca, no sobre
#: la que se mostró.
SLIPPAGE_PCT: Final = Decimal("0.5")

#: Deriva máxima tolerada entre la cotización que el usuario vio y la que hay al
#: construir, con el mismo valor y el mismo razonamiento que en los demás motores
#: que construyen: el mercado se mueve entre pintar la tabla y pulsar el botón.
MAX_QUOTE_DRIFT_BPS: Final = BasisPoints(100)

#: Impacto por encima del cual la cotización se omite, como en los demás motores.
#: Un pool con liquidez residual produce impactos enormes que sólo añaden ruido.
MAX_PRICE_IMPACT: Final = BasisPoints(1_000)

#: Cuánto vale el payload antes de caducar. Sólo aplica en las redes con
#: SwapRouter V1, que es el único cuyo `exactInputSingle` lleva `deadline`; en el
#: SwapRouter02 no existe el campo, así que allí la protección es la comprobación
#: de deriva y el diálogo de confirmación. Media hora es de sobra para revisar una
#: transacción y no tanto como para firmar a un precio de hace un día.
DEADLINE_SECONDS: Final = 1_800

#: Cuántas unidades de comisión de V3 hay en un punto básico. V3 cuenta la
#: comisión en **centésimas de punto básico** —el tramo `3000` es el 0,30 %—,
#: mientras que el dominio la cuenta en puntos básicos, donde ese mismo tramo es
#: `30`. Se nombra la constante en vez de escribir `* 100` para que la conversión
#: tenga un solo sitio y se pueda buscar.
_V3_FEE_UNITS_PER_BPS: Final = 100

MANIFEST: Final = EngineManifest(
    engine_id="uniswap_v3",
    name="Uniswap V3 — directo, sin clave",
    version="1.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary=(
        "Cotiza y construye swaps contra los pools de Uniswap V3 leyendo la "
        "fábrica y el QuoterV2 por nodos públicos. No necesita ninguna clave: es "
        "la vía que sigue funcionando cuando la cuota de las APIs se agota. "
        "Compara los cuatro tramos de comisión y se queda con el que más da."
    ),
    capabilities=frozenset(
        {Capability.READ_CHAIN, Capability.COMPUTE_ROUTE, Capability.PREPARE_TX}
    ),
    swap_chains=SWAP_CHAINS,
    # Detrás del agregado (10) y delante de 0x (100): cuando los dos llegan a la
    # misma red se le pide el payload primero a la fuente que ve más venues, y
    # ésta existe para que una cuota agotada no deje al usuario sin operar. No es
    # una afirmación de que su ruta sea peor —la comparación de precios lo decide
    # con datos— sino de quién construye cuando hay que elegir.
    swap_priority=20,
    # Ni una sola clave. Es la propiedad que hace que este motor no se detenga.
    required_config=(),
    # Derivados de la tabla de nodos medidos, no escritos a mano: un manifiesto
    # que declare hosts distintos de los que se usan falla en el transporte con un
    # error que habla de permisos y no de la causa.
    allowed_hosts=rpc_hosts(SWAP_CHAINS),
)


@dataclass(frozen=True, slots=True)
class _PoolQuote:
    """Una cotización cruda de un pool concreto, antes de decidir nada."""

    fee: int
    pool: str
    amount_out_raw: int


class UniswapV3Engine:
    """Cotizaciones y payloads de swap contra Uniswap V3, sin credenciales."""

    __slots__ = ("_clock", "_reader")

    def __init__(self, *, clock: Clock | None = None, reader: ChainReader | None = None) -> None:
        self._clock = clock or SystemClock()
        # El lector se puede inyectar —y las pruebas lo hacen— porque es la
        # frontera de red del motor: todo lo demás es aritmética determinista.
        self._reader = reader or ChainReader(SWAP_CHAINS, allowed_hosts=MANIFEST.allowed_hosts)

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    async def aopen(self) -> None:
        await self._reader.aopen()

    async def aclose(self) -> None:
        await self._reader.aclose()

    # ------------------------------------------------------------------ leer #
    async def venues(self, chain_key: str) -> Sequence[Venue]:
        """Los tramos de comisión que este motor sondea en esa red.

        Se listan los cuatro tramos **que se preguntan**, no los pools que
        existen: cuáles existen depende del par y sólo se sabe preguntando a la
        fábrica, y hacerlo aquí gastaría cuatro lecturas por cada vez que alguien
        dibuja la lista de venues. Un tramo sin pool no produce cotización, que es
        lo que de verdad importa; y los `venue_id` de esta lista son exactamente
        los que llevan las cotizaciones, para que la tabla no muestre un venue que
        después no case con ninguna fila.
        """
        if chain_key not in SWAP_CHAINS:
            return ()
        return tuple(_venue(chain_key, fee) for fee in FEE_TIERS)

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        """Cotiza vender `amount_in` de la base contra el mejor pool disponible."""
        deployment = DEPLOYMENTS.get(pair.chain)
        if deployment is None or not amount_in.is_positive:
            return ()
        token_in = _on_chain_address(pair.base)
        token_out = _on_chain_address(pair.quote)
        if token_in is None or token_out is None or token_in == token_out:
            # Sin dirección de contrato no hay pool que preguntar. El nativo se
            # cotiza por su envoltorio: los AMM no operan con el nativo, y
            # `wrapped_native` ya devuelve su dirección con los decimales de la
            # red. Un par que se resuelve al mismo contrato por los dos lados
            # —nativo contra su propio envoltorio— no es un par.
            return ()

        pools = await self._pools_for(pair.chain, deployment, token_in, token_out)
        if not pools:
            _log.debug("uniswap_v3.no_pool", pair=pair.symbol, chain=pair.chain)
            return ()

        candidates = await self._quotes_for(
            pair.chain, deployment, token_in, token_out, amount_in.raw, pools
        )
        if not candidates:
            _log.debug("uniswap_v3.no_liquidity", pair=pair.symbol, chain=pair.chain)
            return ()

        for candidate in sorted(candidates, key=lambda item: item.amount_out_raw, reverse=True):
            state = await self._pool_state(pair.chain, candidate.pool, token_in)
            if state is None:
                continue
            impact = impact_bps(
                amount_in_raw=amount_in.raw,
                amount_out_raw=candidate.amount_out_raw,
                sqrt_price_x96=state[0],
                token_in_is_token0=state[1],
                fee=BasisPoints(candidate.fee // 100),
            )
            if impact is None:
                continue
            if abs(impact).value > MAX_PRICE_IMPACT.value:
                _log.debug(
                    "uniswap_v3.quote_skipped_impact",
                    pair=pair.symbol,
                    fee=candidate.fee,
                    impact_bps=impact.value,
                )
                continue
            return (self._to_quote(pair, amount_in, candidate, impact),)

        _log.debug("uniswap_v3.quote_unreadable_state", pair=pair.symbol, chain=pair.chain)
        return ()

    # --------------------------------------------------------------- construir #
    async def plan_swap(self, quote: Quote, *, recipient: str) -> UnsignedTransaction:
        """Construye la transacción sin firmar del swap que describe `quote`.

        ### Por qué vuelve a cotizar

        La cotización que se muestra puede tener minutos, y entre una cosa y otra
        el precio se mueve. Se vuelve a preguntar al mismo pool —el del tramo que
        la cotización nombra, no «el mejor de ahora»: cambiar de pool a espaldas
        del usuario sería cambiar el venue después de que lo eligiera— y si el
        importe se ha movido más de lo tolerado, se **niega a construir**. Es la
        misma regla que en los demás motores, y el mismo motivo: el payload tiene
        que corresponder a la cotización que el usuario leyó.

        La cotización fresca es la que fija el `amountOutMinimum`, así que el
        deslizamiento se calcula sobre el precio de ahora y no sobre el de la
        pantalla.
        """
        chain_key = quote.pair.chain
        deployment = DEPLOYMENTS.get(chain_key)
        if deployment is None:
            raise UnsupportedOperationError(
                f"este motor no construye swaps en «{chain_key}»: sus contratos de "
                f"V3 no están medidos ahí. Cubre {', '.join(sorted(SWAP_CHAINS))}. "
                f"Activa un motor que cubra esa red."
            )
        fee = _fee_from_venue(quote.venue.venue_id)
        token_in = _on_chain_address(quote.pair.base)
        token_out = _on_chain_address(quote.pair.quote)
        if token_in is None or token_out is None:
            raise UnsupportedOperationError(
                f"«{quote.pair.symbol}» tiene un lado sin dirección de contrato: no "
                f"hay pool contra el que construir."
            )

        fresh = await self._fresh_out(
            chain_key, deployment, token_in, token_out, quote.amount_in.raw, fee
        )
        if fresh is None:
            raise NoQuotesError(
                f"el pool de «{quote.pair.symbol}» al {_percent(fee)} % ya no cotiza "
                f"ese tamaño: la liquidez que viste se agotó. Vuelve a cotizar."
            )
        self._require_same_price(quote, fresh)

        spec = chain(chain_key)
        out_token = quote.pair.quote
        amount_out = TokenAmount(fresh, out_token.decimals, out_token.symbol)
        minimum = amount_out.scaled_by(
            EXACT.subtract(Decimal(1), BasisPoints.from_percent(SLIPPAGE_PCT).as_ratio()),
            # Hacia abajo, como el SDK de Uniswap: `amountOutMinimum` no puede
            # quedar por encima de lo que el pool entrega de verdad, porque
            # entonces la transacción revierte y el gas se pierde.
            rounding=ROUND_DOWN,
        )

        pays_native = quote.pair.base.is_native
        receives_native = quote.pair.quote.is_native
        if pays_native and quote.amount_in.decimals != spec.native_decimals:
            raise InvalidAmountError(
                f"«{quote.pair.base.symbol}» está declarado con "
                f"{quote.amount_in.decimals} decimales y el nativo de {spec.name} "
                f"tiene {spec.native_decimals}: el importe que se mandaría con la "
                f"transacción no sería el que se cree."
            )

        calldata = self._calldata(
            deployment=deployment,
            fee=fee,
            token_in=token_in,
            token_out=token_out,
            recipient=recipient,
            amount_in_raw=quote.amount_in.raw,
            minimum_raw=minimum.raw,
            pays_native=pays_native,
            receives_native=receives_native,
        )
        approval = (
            None
            if pays_native
            else TokenApproval(spender=deployment.router, via=deployment.permit2)
        )
        _log.info(
            "uniswap_v3.swap_planned",
            pair=quote.pair.symbol,
            chain=chain_key,
            fee=fee,
            out_raw=fresh,
            minimum_raw=minimum.raw,
            router=deployment.router,
            permit2=deployment.permit2,
        )
        return UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=deployment.router,
            calldata=calldata,
            value=(
                TokenAmount(quote.amount_in.raw, spec.native_decimals, spec.native_symbol)
                if pays_native
                else TokenAmount.zero(spec.native_decimals, spec.native_symbol)
            ),
            # `None` a propósito: estimar el gas necesita la dirección que paga y
            # el motor no la conoce. Lo estima quien firma, contra el estado del
            # momento, que además es lo único que vale.
            gas_limit=None,
            description=self._describe(
                quote=quote,
                deployment=deployment,
                fee=fee,
                amount_out=amount_out,
                minimum=minimum,
                pays_native=pays_native,
                receives_native=receives_native,
            ),
            approval=approval,
        )

    def expected_destination(self, chain_key: str) -> str | None:
        """El router medido para esa red, o `None` si no está en la tabla.

        Es el contraste contra el que el camino de ejecución comprueba el destino
        del payload antes de firmar: si el `to` no es éste, no se firma. Se lee
        de la tabla de despliegues en vez de escribirse aparte porque es la misma
        pregunta —«¿a qué contrato manda este motor los swaps?»— y dos listas
        acabarían discrepando.
        """
        deployment = DEPLOYMENTS.get(chain_key)
        return None if deployment is None else deployment.router

    # ------------------------------------------------------------------ pasos #
    async def _pools_for(
        self,
        chain_key: str,
        deployment: Deployment,
        token_in: str,
        token_out: str,
    ) -> tuple[tuple[int, str], ...]:
        """Los pools que existen para el par, tramo por tramo.

        Los cuatro `getPool` van en paralelo: son independientes y son la primera
        ronda, la que más se nota en el tiempo de respuesta.

        Un revert aquí **no** se silencia. `getPool` no tiene ningún motivo para
        revertir con entradas válidas —devuelve la dirección cero cuando no hay
        pool—, así que si revierte es que la fábrica o el selector no son los que
        la tabla dice. Eso hay que verlo, y verlo antes de firmar nada.
        """
        results = await asyncio.gather(
            *(
                self._reader.eth_call(
                    chain_key, deployment.factory, abi.get_pool(token_in, token_out, fee)
                )
                for fee in FEE_TIERS
            ),
            return_exceptions=True,
        )
        pools: list[tuple[int, str]] = []
        for fee, result in zip(FEE_TIERS, results, strict=True):
            if isinstance(result, BaseException):
                raise result
            pool = abi.decode_address(result)
            # La dirección cero es «no hay pool en este tramo», que es la
            # respuesta normal y no un error.
            if pool is not None and pool != _ZERO_ADDRESS:
                pools.append((fee, pool))
        return tuple(pools)

    async def _quotes_for(
        self,
        chain_key: str,
        deployment: Deployment,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        pools: Sequence[tuple[int, str]],
    ) -> tuple[_PoolQuote, ...]:
        """El importe que daría cada pool para ese tamaño, en paralelo.

        Un quoter que revierte significa que ese pool no puede dar salida a esta
        orden —liquidez insuficiente en el rango activo, o ninguna—, y eso es una
        respuesta, no una caída: se omite ese tramo y se sigue con los demás. Lo
        que **no** se omite es que ningún nodo conteste, que sí es la fuente
        caída y se propaga.
        """
        results = await asyncio.gather(
            *(
                self._reader.eth_call(
                    chain_key,
                    deployment.quoter,
                    abi.quote_exact_input_single(token_in, token_out, amount_in_raw, fee),
                )
                for fee, _pool in pools
            ),
            return_exceptions=True,
        )
        found: list[_PoolQuote] = []
        for (fee, pool), result in zip(pools, results, strict=True):
            if isinstance(result, SourceResponseError):
                _log.debug(
                    "uniswap_v3.pool_without_quote", chain=chain_key, fee=fee, pool=pool
                )
                continue
            if isinstance(result, BaseException):
                raise result
            amount_out = abi.decode_uint(result, 0)
            if amount_out is None or amount_out <= 0:
                continue
            found.append(_PoolQuote(fee=fee, pool=pool, amount_out_raw=amount_out))
        return tuple(found)

    async def _pool_state(
        self, chain_key: str, pool: str, token_in: str
    ) -> tuple[int, bool] | None:
        """`(sqrtPriceX96, ¿el token que se vende es el token0?)` del pool.

        Las dos lecturas van juntas porque las dos hacen falta para el impacto:
        sin el precio no hay referencia contra la que comparar, y sin saber cuál
        de los dos tokens es el 0 no se sabe si el precio marginal va en el mismo
        sentido que la operación o en el contrario.

        El orden **se lee del contrato** en vez de deducirse comparando
        direcciones. La fábrica ordena por dirección al construir el pool, así que
        deducirlo funcionaría; leerlo funciona aunque esa regla cambiara, y una
        comparación invertida daría un impacto con el signo cambiado sin que nada
        lo delatara.
        """
        results = await asyncio.gather(
            self._reader.eth_call(chain_key, pool, abi.slot0()),
            self._reader.eth_call(chain_key, pool, abi.token0()),
            return_exceptions=True,
        )
        slot0_raw, token0_raw = results
        if isinstance(slot0_raw, BaseException) or isinstance(token0_raw, BaseException):
            _log.debug("uniswap_v3.pool_state_unreadable", chain=chain_key, pool=pool)
            return None
        sqrt_price = abi.decode_uint(slot0_raw, 0)
        token0 = abi.decode_address(token0_raw, 0)
        if sqrt_price is None or sqrt_price <= 0 or token0 is None:
            return None
        return sqrt_price, token0 == token_in

    async def _fresh_out(
        self,
        chain_key: str,
        deployment: Deployment,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        fee: int,
    ) -> int | None:
        """El importe de salida del pool **de ese tramo**, ahora mismo."""
        try:
            raw = await self._reader.eth_call(
                chain_key,
                deployment.quoter,
                abi.quote_exact_input_single(token_in, token_out, amount_in_raw, fee),
            )
        except SourceResponseError:
            # El pool no puede dar salida a este tamaño ahora: se trata como «ya
            # no hay cotización», que es lo que es.
            return None
        amount_out = abi.decode_uint(raw, 0)
        return None if amount_out is None or amount_out <= 0 else amount_out

    def _calldata(
        self,
        *,
        deployment: Deployment,
        fee: int,
        token_in: str,
        token_out: str,
        recipient: str,
        amount_in_raw: int,
        minimum_raw: int,
        pays_native: bool,
        receives_native: bool,
    ) -> str:
        """El calldata del swap, con el envoltorio y el reembolso que toquen.

        Tres formas, y las tres salen del mismo sitio:

        - **Se paga con el nativo.** El router envuelve el `value` él mismo, así
          que el `tokenIn` del swap es el envoltorio y el nativo viaja con la
          transacción. Va dentro de un `multicall` con `refundETH` para que el
          sobrante vuelva al usuario en vez de quedarse en el contrato.
        - **Se recibe el nativo.** V3 no entrega nativo: entrega el envoltorio. Se
          le pide al router que se lo mande a sí mismo y se añade `unwrapWETH9`
          para que lo retire y lo transfiera al destinatario real. Sin eso, el
          usuario recibiría WETH creyendo que recibió ETH.
        - **Token contra token.** Una sola llamada, directa al destinatario.

        El `deadline` viaja sólo si el router de esa red lo tiene en su struct:
        el SwapRouter02 no lo acepta, y pasarle el ABI del V1 sería llamar a otra
        función.
        """
        deadline = (
            int(self._clock.now().timestamp()) + DEADLINE_SECONDS
            if deployment.has_deadline
            else None
        )
        swap = abi.exact_input_single(
            token_in=token_in,
            token_out=token_out,
            fee=fee,
            # Cuando se recibe el nativo, el router es el destinatario
            # intermedio: el envoltorio se le queda a él y él lo retira después.
            recipient=deployment.router if receives_native else recipient,
            amount_in_raw=amount_in_raw,
            amount_out_min_raw=minimum_raw,
            deadline=deadline,
        )
        calls = [swap]
        if receives_native:
            calls.append(abi.unwrap_weth9(minimum_raw, recipient))
        if pays_native:
            calls.append(abi.refund_eth())
        return abi.multicall(calls) if len(calls) > 1 else swap

    def _to_quote(
        self,
        pair: TradingPair,
        amount_in: TokenAmount,
        candidate: _PoolQuote,
        impact: BasisPoints,
    ) -> Quote:
        """La cotización del dominio, con la procedencia de cada cifra.

        `fee_basis` es `DERIVED` y no `REPORTED` por una razón concreta: la
        comisión no la publica una fuente, la deducimos del tramo con el que la
        fábrica construyó ese pool —la dirección del pool se calcula con la
        comisión dentro, así que el tramo **es** la comisión del pool—. `REPORTED`
        diría que alguien la publica y no es el caso.

        `liquidity` va a `None` a propósito. La liquidez de un pool V3 es su `L`,
        que no es una cantidad de ningún token: convertirla a una exigiría suponer
        un rango de precios, y compararla contra las reservas de un pool V2 que
        aparezca en la misma tabla compararía dos cosas distintas. El impacto de
        precio —que sí se mide, con el precio real del pool— cubre el trabajo de
        avisar de que un pool es poco profundo.
        """
        return Quote(
            venue=_venue(pair.chain, candidate.fee),
            engine_id=MANIFEST.engine_id,
            pair=pair,
            amount_in=amount_in,
            amount_out=TokenAmount(
                candidate.amount_out_raw, pair.quote.decimals, pair.quote.symbol
            ),
            fee_bps=BasisPoints(candidate.fee // 100),
            fee_basis=Measurement.DERIVED,
            price_impact_bps=impact,
            observed_at=self._clock.now(),
            liquidity=None,
            impact_basis=Measurement.DERIVED,
            source_note=(
                f"Leído del pool de {SOURCE_NAME} en el tramo {_percent(candidate.fee)} % "
                f"({candidate.pool}) con el QuoterV2, contra el estado de la cadena "
                f"en el bloque actual. El importe de salida es el que daría la "
                f"ejecución ahora mismo, y ya viene neto de comisión; el impacto es "
                f"la diferencia entre ese precio y el marginal del pool, con la "
                f"comisión descontada aparte para no contarla dos veces."
            ),
        )

    def _require_same_price(self, quote: Quote, fresh_raw: int) -> None:
        """Aborta si el precio se movió más de lo tolerado desde lo que se vio."""
        shown_raw = quote.amount_out.raw
        if shown_raw <= 0:
            raise NoQuotesError(
                "la cotización no traía importe de salida: no hay nada contra lo "
                "que comparar el precio de ahora."
            )
        drift = EXACT.divide(Decimal(abs(fresh_raw - shown_raw)), Decimal(shown_raw))
        drift_bps = BasisPoints.from_ratio(drift)
        if drift_bps.value <= MAX_QUOTE_DRIFT_BPS.value:
            return
        token = quote.pair.quote
        _log.info(
            "uniswap_v3.quote_moved",
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

    def _describe(
        self,
        *,
        quote: Quote,
        deployment: Deployment,
        fee: int,
        amount_out: TokenAmount,
        minimum: TokenAmount,
        pays_native: bool,
        receives_native: bool,
    ) -> str:
        """Lo que el usuario tiene que poder leer antes de confirmar."""
        what = "Compra" if pays_native else "Venta"
        parts = [
            f"{what} en {quote.venue.name} ({chain(quote.pair.chain).name}): "
            f"entregas {quote.amount_in} y recibes {amount_out} "
            f"—{minimum} como mínimo, con un {SLIPPAGE_PCT} % de deslizamiento "
            f"tolerado—.",
            f"Se ejecuta contra el pool del tramo {_percent(fee)} % a través del "
            f"router {deployment.router}.",
        ]
        if pays_native:
            parts.append(
                "El importe va con la transacción, así que no hace falta autorizar "
                "ningún token."
            )
        elif deployment.permit2 is not None:
            parts.append(
                f"Antes de esto hacen falta **dos** permisos, no uno: el del token "
                f"a Permit2 ({deployment.permit2}) y el de Permit2 al router. El "
                f"router no mueve el token él mismo en esta red."
            )
        else:
            parts.append(
                "Antes de esto hay que autorizar el token al router, en una "
                "transacción aparte."
            )
        if receives_native:
            parts.append(
                "Lo recibido se retira del envoltorio en la misma transacción, así "
                "que llega como moneda nativa."
            )
        if deployment.has_deadline:
            parts.append(
                f"La transacción caduca en {DEADLINE_SECONDS // 60} minutos: después "
                f"de eso el router la rechaza, para no ejecutar a un precio viejo."
            )
        else:
            parts.append(
                "Este router no admite caducidad en la orden, así que el precio "
                "queda protegido por el mínimo de arriba."
            )
        return " ".join(parts)


# --------------------------------------------------------------------------- #
# Derivaciones puras
# --------------------------------------------------------------------------- #
def _on_chain_address(token: Token) -> str | None:
    """La dirección con la que ese token existe en el pool, o `None`.

    El nativo no tiene dirección: se opera con su envoltorio ERC-20, que es el
    token que los AMM conocen. Se normaliza a minúsculas porque las fuentes
    discrepan en el *checksum* y comparar dos formas de la misma dirección daría
    dos tokens distintos.

    Devuelve `None` —y no lanza— cuando la red no tiene envoltorio nativo: no es
    un error de nadie, es que ahí no hay pool que preguntar.
    """
    if token.is_native:
        wrapped = wrapped_native(token.chain)
        return None if wrapped is None or wrapped.address is None else wrapped.address.lower()
    return None if token.address is None else token.address.lower()


def _venue(chain_key: str, fee: int) -> Venue:
    """El venue de un pool, con el tramo de comisión dentro de la identidad.

    Se delega en `venue_for` —el mismo que usan las demás fuentes— para que el
    `venue_id` de este motor sea **el mismo** que el de cualquier otro que mire
    ese pool. Si cada motor se inventara el suyo, la misma fila aparecería dos
    veces en la tabla y la comparación las cruzaría como si fueran dos sitios.
    """
    return venue_for(PROTOCOL, BasisPoints(fee // 100), chain_key)


def _fee_from_venue(venue_id: str) -> int:
    """El tramo de comisión que nombra un `venue_id`, **en unidades de V3**.

    Es el camino de vuelta de `_venue`: la cotización identifica el pool por su
    tramo y al construir hay que recuperarlo, porque el router deriva el pool de
    `(tokenIn, tokenOut, fee)` y construir contra otro tramo sería ejecutar en un
    sitio distinto del que el usuario vio.

    La conversión no es un detalle. `venue_for` escribe el `venue_id` con la
    comisión en **puntos básicos** —`uniswap-v3@100` es el 1 %— porque es la
    unidad del dominio y la comparten todos los motores; pero el router de V3
    quiere el tramo en **centésimas de punto básico**, donde ese mismo pool es
    `10000`. Devolver el número del `venue_id` tal cual construiría contra el
    pool del 0,01 % —que en la mayoría de los pares ni existe— sin que nada
    chirriara hasta que la transacción revirtiera.
    """
    prefix, _, tier = venue_id.partition("@")
    if prefix != PROTOCOL.key or not tier.isdigit() or int(tier) <= 0:
        raise UnsupportedOperationError(
            f"«{venue_id}» no es un venue de {SOURCE_NAME}: este motor sólo "
            f"construye contra pools identificados como «{PROTOCOL.key}@<tramo>»."
        )
    return int(tier) * _V3_FEE_UNITS_PER_BPS


def _percent(fee: int) -> str:
    """El tramo en porcentaje legible, sin ceros de relleno: `3000` → `"0.3"`."""
    return f"{BasisPoints(fee // 100).as_percent().normalize():f}"


_ZERO_ADDRESS: Final = "0x" + "00" * 20


# --------------------------------------------------------------------------- #
# Publicación
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class UniswapV3Provider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: object) -> UniswapV3Engine:
        # No hay nada que configurar: ni clave, ni host, ni límite. El parámetro
        # está porque lo exige el contrato de `EngineProvider`, y acepta cualquier
        # cosa precisamente porque este motor no lee nada de ahí. Un motor que
        # dependiera de una clave la declararía en `required_config` y llegaría
        # aquí ya validada.
        return UniswapV3Engine()


PROVIDER: Final = UniswapV3Provider()
