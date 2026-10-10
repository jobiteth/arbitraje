"""AMM de producto constante por contrato, **sin clave**: la familia V2 entera.

### Un motor para toda la familia, y por qué se puede

Uniswap V2, SushiSwap, QuickSwap y PancakeSwap son el mismo contrato con otro
despliegue: se midió en los seis routers de `addresses` que todos llevan los
mismos selectores, que la comisión va **dentro** del router —no en el
calldata— y que el `path[]` de sus swaps es una lista de direcciones real. Lo
único que cambia de una red a otra es **quién** presta ese ABI, y eso es una
entrada medida en la tabla, no una rama de código: añadir una red es medirla, y
añadir otro DEX donde ya hay uno es otra entrada con su comisión medida en
`amm_protocols`. Eso es lo que hace a este motor modular de verdad: cotiza
cualquier par que la tabla cubra —con cualquier token del catálogo— sin tocar
el motor.

### Cotizar aquí no es simular

El `getAmountsOut` del router ejecuta la **misma aritmética** que el swap
(`UniswapV2Library.getAmountOut`, que el router llama en los dos caminos) sobre
el estado real de los pools en el bloque actual. El número que se publica es el
que la cadena da, ya neto de comisión y ya de ejecución; la fórmula de producto
constante no vive en el motor —sólo en las pruebas, donde sirvió para medir que
el contrato desplegado es el que la tabla dice (así entraron QuickSwap y
PancakeSwap)—. `eth_call` no cambia estado y no gasta gas, que es justo lo que
permite que esto funcione sin clave y sin coste.

### Un pool por par, y el segundo salto

En V2 un par tiene **un** pool por DEX —no hay tramos de comisión que
comparar—, así que el motor le pregunta a la fábrica por él y, **sólo cuando no
lo hay** —o no da salida para ese tamaño—, busca una ruta de dos saltos por los
tokens del catálogo, con las mismas reglas que el motor de V3: los hubs salen de
la tabla del catálogo —no de una lista por red—, compiten en paralelo, no se
cambia un pool directo por una ruta que diera más, y el impacto **compone** los
dos tramos. La diferencia es que aquí el camino entero se cotiza en **una**
llamada —`getAmountsOut` con el `path[]` completo—, que es exactamente la
llamada que el swap repetirá al ejecutarse.

### El nativo no se envuelve a mano

V2 trae funciones dedicadas para el nativo —`swapExactETHForTokens` y
`swapExactTokensForETH`—: el router envuelve y retira él mismo y no hacen falta
ni `multicall` ni `unwrapWETH9`. El camino sí nombra al envuelto —el contrato lo
exige, y lo comprueba—, y de esa traducción se encarga `_on_chain_address`. El
permiso del token es directo contra el router: esta familia no cobra por Permit2.

### Lo que este motor NO hace

**No firma y no emite.** Devuelve una `UnsignedTransaction` para que el usuario
la revise; la clave y el envío viven en `infra.evm`, detrás de la barrera de
modo y del diálogo de confirmación. Tampoco **estima el gas**: para eso hace
falta la dirección que paga, que sólo se conoce al firmar —el motor no la tiene
y no debe tenerla—, así que `gas_limit` va a `None` y lo estima quien firma,
contra el estado del momento.
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
    RouteHop,
    SwapRoute,
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
from amigocompora.engines.amm_protocols import ProtocolRef, parse_dex_id, venue_for
from amigocompora.engines.catalog import hub_tokens, wrapped_native
from amigocompora.engines.evm_rpc import ChainReader, rpc_hosts
from amigocompora.engines.uniswap_math import combine_impact_bps, impact_bps_from_marginal
from amigocompora.engines.uniswap_v2 import calldata as abi
from amigocompora.engines.uniswap_v2.addresses import DEPLOYMENTS, SWAP_CHAINS, Deployment

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "AMM V2 (directo)"

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

#: Cuánto vale el payload antes de caducar. En esta familia el `deadline` existe
#: en **todas** las redes —está en el ABI de los seis routers medidos—, así que
#: la protección no depende de qué contrato esté desplegado, como en V3. Media
#: hora es de sobra para revisar una transacción y no tanto como para firmar a un
#: precio de hace un día.
DEADLINE_SECONDS: Final = 1_800

MANIFEST: Final = EngineManifest(
    engine_id="uniswap_v2",
    name="AMM V2 (Uniswap y forks) — directo, sin clave",
    version="1.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary=(
        "Cotiza y construye swaps contra el AMM de producto constante más "
        "profundo de cada red —Uniswap V2, SushiSwap, QuickSwap o PancakeSwap, "
        "medido— preguntándole al propio router su getAmountsOut: la misma "
        "aritmética que ejecutará el swap. No necesita ninguna clave. Si el par "
        "no tiene pool directo, busca una ruta de dos saltos por los tokens del "
        "catálogo y la ejecuta en una sola transacción."
    ),
    capabilities=frozenset(
        {Capability.READ_CHAIN, Capability.COMPUTE_ROUTE, Capability.PREPARE_TX}
    ),
    swap_chains=SWAP_CHAINS,
    # Detrás de las vías directas de V3 (20) y V4 (25) y delante de 0x (100): su
    # trabajo es que **toda** red del catálogo tenga una vía sin clave, no ganar
    # la comparación por orden. Cuando varios motores cubren la misma red se le
    # pide el payload primero a los que ya están medidos, y la comparación de
    # precios decide con datos.
    swap_priority=30,
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

    pool: str
    amount_out_raw: int


@dataclass(frozen=True, slots=True)
class _RouteQuote:
    """Una ruta cruda de dos saltos por un hub, antes de decidir nada.

    Los dos importes vienen de **una sola** llamada al router —el
    `getAmountsOut` del camino completo—, así que la segunda pata ya está
    cotizada con lo que de verdad daría la primera: encadenar dos cotizaciones
    calculadas por separado produciría un número que ninguna ejecución entrega.
    """

    hub: Token
    first: _PoolQuote
    second: _PoolQuote


class UniswapV2Engine:
    """Cotizaciones y payloads de swap contra la familia V2, sin credenciales."""

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
        """El DEX de esta familia que el motor usa en esa red.

        Se lista **el que se usa**, no los candidatos que existen: en V2 un par
        tiene un pool por DEX y nada más —no hay tramos que comparar—, así que
        lo que hay que decidir es el DEX, y eso se decidió midiendo (ver
        `addresses`). El `venue_id` de esta lista es exactamente el que llevan
        las cotizaciones, para que la tabla no muestre un venue sin filas.
        """
        deployment = DEPLOYMENTS.get(chain_key)
        if deployment is None:
            return ()
        return (_venue(chain_key, deployment),)

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        """Cotiza vender `amount_in`: el pool directo o, si no lo hay, dos saltos.

        El orden es deliberado, igual que en el motor de V3. Primero el par
        directo —un pool, la ruta con menos superficie— y **sólo si no da
        nada** se busca una ruta de dos saltos por los hubs del catálogo. No se
        cambia un pool directo por una ruta que diera más: elegir ruta es una
        decisión con más gas y más superficie de contrato, y no la toma el
        motor a espaldas del usuario.
        """
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

        direct = await self._direct_quote(pair, amount_in, deployment, token_in, token_out)
        if direct is not None:
            return (direct,)
        routed = await self._hub_quote(pair, amount_in, deployment, token_in, token_out)
        return () if routed is None else (routed,)

    async def _direct_quote(
        self,
        pair: TradingPair,
        amount_in: TokenAmount,
        deployment: Deployment,
        token_in: str,
        token_out: str,
    ) -> Quote | None:
        """La cotización del pool directo, o `None` si no hay ninguna.

        Aquí no hay candidatos que comparar —un par tiene un pool por DEX—, así
        que la única decisión es si ese pool sirve: existe, da salida a este
        tamaño y el impacto no es desmedido. Las tres cosas se comprueban antes
        de publicar nada.
        """
        pool = await self._pool_for(pair.chain, deployment, token_in, token_out)
        if pool is None:
            _log.debug("uniswap_v2.no_pool", pair=pair.symbol, chain=pair.chain)
            return None

        # Las dos lecturas son independientes —la cotización del camino y el
        # estado del pool—, así que van juntas: es la mitad del tiempo de
        # respuesta de cada par.
        amounts, reserves = await asyncio.gather(
            self._amounts_out(pair.chain, deployment, (token_in, token_out), amount_in.raw),
            self._pool_reserves(pair.chain, pool, token_in),
        )
        if amounts is None or amounts[-1] <= 0:
            _log.debug("uniswap_v2.no_liquidity", pair=pair.symbol, chain=pair.chain)
            return None
        if reserves is None:
            _log.debug("uniswap_v2.pool_unreadable", pair=pair.symbol, chain=pair.chain)
            return None

        reserve_in, reserve_out = reserves
        marginal = _marginal_from(reserve_in=reserve_in, reserve_out=reserve_out)
        if marginal is None:
            return None
        impact = impact_bps_from_marginal(
            amount_in_raw=amount_in.raw,
            amount_out_raw=amounts[-1],
            marginal_out_per_in=marginal,
            fee=BasisPoints(deployment.fee_bps),
        )
        if impact is None:
            return None
        if abs(impact).value > MAX_PRICE_IMPACT.value:
            _log.debug(
                "uniswap_v2.quote_skipped_impact",
                pair=pair.symbol,
                impact_bps=impact.value,
            )
            return None
        return self._to_quote(
            pair,
            amount_in,
            deployment,
            pool=pool,
            amount_out_raw=amounts[-1],
            reserve_out=reserve_out,
            impact=impact,
        )

    async def _hub_quote(
        self,
        pair: TradingPair,
        amount_in: TokenAmount,
        deployment: Deployment,
        token_in: str,
        token_out: str,
    ) -> Quote | None:
        """La mejor ruta de dos saltos por un hub del catálogo, o `None`.

        Los hubs se buscan **en paralelo** —son independientes y es la mitad del
        tiempo de respuesta de un par sin pool directo— y compiten por el
        importe que da el camino entero. El impacto de la ganadora se compone
        tramo a tramo y, si pasa el umbral, se publica con su camino dentro.
        """
        candidates = [
            token
            for token in hub_tokens(pair.chain)
            if self._is_hub_for(token, token_in, token_out)
        ]
        if not candidates:
            _log.debug("uniswap_v2.no_hubs", pair=pair.symbol, chain=pair.chain)
            return None

        found = await asyncio.gather(
            *(
                self._route_via_hub(
                    pair.chain, deployment, token_in, token_out, amount_in.raw, hub
                )
                for hub in candidates
            )
        )
        routes = [route for route in found if route is not None]
        if not routes:
            _log.debug("uniswap_v2.no_route", pair=pair.symbol, chain=pair.chain)
            return None

        for route in sorted(routes, key=lambda item: item.second.amount_out_raw, reverse=True):
            hub_address = _on_chain_address(route.hub)
            if hub_address is None:
                # No puede pasar —los hubs se filtraron por tener dirección—,
                # pero si pasara no hay tramo dos que leer y se salta.
                continue
            first_reserves, second_reserves = await asyncio.gather(
                self._pool_reserves(pair.chain, route.first.pool, token_in),
                self._pool_reserves(pair.chain, route.second.pool, hub_address),
            )
            if first_reserves is None or second_reserves is None:
                continue
            first_marginal = _marginal_from(
                reserve_in=first_reserves[0], reserve_out=first_reserves[1]
            )
            second_marginal = _marginal_from(
                reserve_in=second_reserves[0], reserve_out=second_reserves[1]
            )
            if first_marginal is None or second_marginal is None:
                continue
            fee = BasisPoints(deployment.fee_bps)
            first_impact = impact_bps_from_marginal(
                amount_in_raw=amount_in.raw,
                amount_out_raw=route.first.amount_out_raw,
                marginal_out_per_in=first_marginal,
                fee=fee,
            )
            second_impact = impact_bps_from_marginal(
                amount_in_raw=route.first.amount_out_raw,
                amount_out_raw=route.second.amount_out_raw,
                marginal_out_per_in=second_marginal,
                fee=fee,
            )
            if first_impact is None or second_impact is None:
                continue
            impact = combine_impact_bps([first_impact, second_impact])
            if impact.value > MAX_PRICE_IMPACT.value:
                _log.debug(
                    "uniswap_v2.route_skipped_impact",
                    pair=pair.symbol,
                    hub=route.hub.symbol,
                    impact_bps=impact.value,
                )
                continue
            _log.debug(
                "uniswap_v2.route_found",
                pair=pair.symbol,
                chain=pair.chain,
                hub=route.hub.symbol,
                out_raw=route.second.amount_out_raw,
            )
            return self._to_route_quote(pair, amount_in, deployment, route, impact)

        _log.debug("uniswap_v2.no_route", pair=pair.symbol, chain=pair.chain)
        return None

    async def _route_via_hub(
        self,
        chain_key: str,
        deployment: Deployment,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        hub: Token,
    ) -> _RouteQuote | None:
        """La ruta de dos saltos por ese hub, o `None` si no llega entera.

        Los dos pools se piden a la fábrica en paralelo y el camino completo se
        cotiza en una sola llamada al router: sus dos importes —el intermedio y
        el final— salen del mismo cálculo que hará el swap, no de componer dos
        lecturas por separado.
        """
        hub_address = _on_chain_address(hub)
        if hub_address is None:
            return None
        pool_first, pool_second = await asyncio.gather(
            self._pool_for(chain_key, deployment, token_in, hub_address),
            self._pool_for(chain_key, deployment, hub_address, token_out),
        )
        if pool_first is None or pool_second is None:
            return None
        amounts = await self._amounts_out(
            chain_key, deployment, (token_in, hub_address, token_out), amount_in_raw
        )
        if amounts is None or amounts[-1] <= 0:
            return None
        return _RouteQuote(
            hub=hub,
            first=_PoolQuote(pool=pool_first, amount_out_raw=amounts[1]),
            second=_PoolQuote(pool=pool_second, amount_out_raw=amounts[2]),
        )

    def _is_hub_for(self, token: Token, token_in: str, token_out: str) -> bool:
        """Si ese token puede hacer de escala en este par.

        No vale el propio token de entrada ni el de salida: un tramo de un
        token a sí mismo no existe, y pasar por la salida para volver a ella
        sería dos comisiones para nada. Se compara por dirección **de pool**
        —el nativo cuenta como su envoltorio—, que es lo que de verdad se le
        pregunta a la fábrica.
        """
        address = _on_chain_address(token)
        return address is not None and address != token_in and address != token_out

    # --------------------------------------------------------------- construir #
    async def plan_swap(self, quote: Quote, *, recipient: str) -> UnsignedTransaction:
        """Construye la transacción sin firmar del swap que describe `quote`.

        ### Por qué vuelve a cotizar

        La cotización que se muestra puede tener minutos, y entre una cosa y otra
        el precio se mueve. Se vuelve a preguntar **al mismo sitio que la
        cotización nombra** —el par directo, o el mismo camino si era de dos
        saltos, no «el mejor de ahora»: cambiar de ruta a espaldas del usuario
        sería cambiar el sitio después de que lo eligiera— y si el importe se ha
        movido más de lo tolerado, se **niega a construir**. Es la misma regla
        que en los demás motores, y el mismo motivo: el payload tiene que
        corresponder a la cotización que el usuario leyó.

        Con ruta de dos saltos, «el mismo sitio» es **el mismo camino**: se
        vuelve a llamar a `getAmountsOut` con ese `path[]` y no se cambia un
        tramo por otro que diera más. Lo que puede cambiar es el importe, y de
        eso avisa la comprobación de deriva.

        La cotización fresca es la que fija el `amountOutMinimum`, así que el
        deslizamiento se calcula sobre el precio de ahora y no sobre el de la
        pantalla.
        """
        chain_key = quote.pair.chain
        deployment = DEPLOYMENTS.get(chain_key)
        if deployment is None:
            raise UnsupportedOperationError(
                f"este motor no construye swaps en «{chain_key}»: no hay ningún "
                f"DEX de la familia V2 medido ahí. Cubre "
                f"{', '.join(sorted(SWAP_CHAINS))}. Activa un motor que cubra esa "
                f"red."
            )
        token_in = _on_chain_address(quote.pair.base)
        token_out = _on_chain_address(quote.pair.quote)
        if token_in is None or token_out is None:
            raise UnsupportedOperationError(
                f"«{quote.pair.symbol}» tiene un lado sin dirección de contrato: no "
                f"hay pool contra el que construir."
            )
        if token_in == token_out:
            raise UnsupportedOperationError(
                f"los dos lados de «{quote.pair.symbol}» existen como el mismo "
                f"contrato en el pool: no hay camino que ejecutar."
            )
        self._require_own_venue(quote, deployment)

        path: tuple[str, ...]
        if quote.route is None:
            path = (token_in, token_out)
        else:
            path = _route_path(quote.route, token_in=token_in, token_out=token_out)
        fresh = await self._fresh_out(chain_key, deployment, path, quote.amount_in.raw)
        if fresh is None:
            if quote.route is None:
                raise NoQuotesError(
                    f"el pool de «{quote.pair.symbol}» ya no cotiza ese tamaño: la "
                    f"liquidez que viste se agotó. Vuelve a cotizar."
                )
            camino = " → ".join(token.symbol for token in quote.route.tokens)
            raise NoQuotesError(
                f"la ruta de «{quote.pair.symbol}» por {camino} ya no cotiza ese "
                f"tamaño: la liquidez que viste se agotó. Vuelve a cotizar."
            )
        self._require_same_price(quote, fresh)

        spec = chain(chain_key)
        out_token = quote.pair.quote
        amount_out = TokenAmount(fresh, out_token.decimals, out_token.symbol)
        minimum = amount_out.scaled_by(
            EXACT.subtract(Decimal(1), BasisPoints.from_percent(SLIPPAGE_PCT).as_ratio()),
            # Hacia abajo, como el SDK de Uniswap: `amountOutMinimum` no puede
            # quedar por encima de lo que los pools entregan de verdad, porque
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

        deadline = int(self._clock.now().timestamp()) + DEADLINE_SECONDS
        if pays_native:
            # El importe va como `value`: la función ni siquiera lo recibe.
            calldata = abi.swap_exact_eth_for_tokens(
                amount_out_min_raw=minimum.raw,
                path=path,
                recipient=recipient,
                deadline=deadline,
            )
        elif receives_native:
            calldata = abi.swap_exact_tokens_for_eth(
                amount_in_raw=quote.amount_in.raw,
                amount_out_min_raw=minimum.raw,
                path=path,
                recipient=recipient,
                deadline=deadline,
            )
        else:
            calldata = abi.swap_exact_tokens_for_tokens(
                amount_in_raw=quote.amount_in.raw,
                amount_out_min_raw=minimum.raw,
                path=path,
                recipient=recipient,
                deadline=deadline,
            )
        # Sin nativo que pagar hay que autorizar el token, y el permiso es
        # directo contra el router: en esta familia el router mueve el token él
        # mismo, y no hay ningún Permit2 de por medio.
        approval = None if pays_native else TokenApproval(spender=deployment.router)
        _log.info(
            "uniswap_v2.swap_planned",
            pair=quote.pair.symbol,
            chain=chain_key,
            hops=len(path) - 1,
            out_raw=fresh,
            minimum_raw=minimum.raw,
            router=deployment.router,
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
    async def _pool_for(
        self,
        chain_key: str,
        deployment: Deployment,
        token_a: str,
        token_b: str,
    ) -> str | None:
        """El pool del par en la fábrica, o `None` si no existe.

        La fábrica contesta la dirección cero cuando no hay pool, que es una
        respuesta normal y no un error: por eso se pregunta a la fábrica y no se
        deriva la dirección —que también se puede, con CREATE2— para luego
        llamar a un contrato que puede no estar.

        Un revert aquí **no** se silencia. `getPair` no tiene ningún motivo para
        revertir con entradas válidas, así que si revierte es que la fábrica o el
        selector no son los que la tabla dice. Eso hay que verlo, y verlo antes
        de firmar nada.
        """
        raw = await self._reader.eth_call(
            chain_key, deployment.factory, abi.get_pair(token_a, token_b)
        )
        pool = abi.decode_address(raw)
        if pool is None or pool == _ZERO_ADDRESS:
            return None
        return pool

    async def _pool_reserves(
        self, chain_key: str, pool: str, token_in: str
    ) -> tuple[int, int] | None:
        """Las reservas del pool **orientadas a la operación**, o `None`.

        Devuelve `(reserva del token que se vende, reserva del que se compra)`,
        que es lo que necesitan el marginal del impacto y la liquidez publicada.
        El orden de `getReserves()` no es ése —es el de `token0`/`token1`—, y el
        sentido **se lee del contrato** en vez de deducirse comparando
        direcciones: la fábrica ordena por dirección al crear el pool, así que
        deducirlo funcionaría hoy; leerlo funciona aunque esa regla cambiara, y
        una comparación invertida daría un marginal con el sentido cambiado sin
        que nada lo delatara.

        Las dos lecturas van juntas porque las dos hacen falta y ninguna sirve
        sola: sin las reservas no hay marginal y sin `token0` no se sabe cuál es
        cuál.
        """
        results = await asyncio.gather(
            self._reader.eth_call(chain_key, pool, abi.get_reserves()),
            self._reader.eth_call(chain_key, pool, abi.token0()),
            return_exceptions=True,
        )
        reserves_raw, token0_raw = results
        if isinstance(reserves_raw, BaseException) or isinstance(token0_raw, BaseException):
            _log.debug("uniswap_v2.pool_state_unreadable", chain=chain_key, pool=pool)
            return None
        reserves = abi.decode_reserves(reserves_raw)
        token0 = abi.decode_address(token0_raw)
        if reserves is None or token0 is None:
            return None
        reserve0, reserve1 = reserves
        return (reserve0, reserve1) if token0 == token_in else (reserve1, reserve0)

    async def _amounts_out(
        self,
        chain_key: str,
        deployment: Deployment,
        path: Sequence[str],
        amount_in_raw: int,
    ) -> tuple[int, ...] | None:
        """Los importes que da el router para ese camino, o `None` si no los da.

        Un revert aquí es una respuesta, no una caída: el router revierte cuando
        el par no existe o las reservas no dan salida a este tamaño —su
        `getAmountOut` exige que la salida sea positiva—, y eso significa «ahí
        no hay cotización». Lo que **no** se silencia es una respuesta con otra
        forma, que la comprueba `decode_amounts_out` al decodificar.
        """
        try:
            raw = await self._reader.eth_call(
                chain_key, deployment.router, abi.get_amounts_out(amount_in_raw, path)
            )
        except SourceResponseError:
            return None
        return abi.decode_amounts_out(raw, hops=len(path) - 1)

    async def _fresh_out(
        self,
        chain_key: str,
        deployment: Deployment,
        path: Sequence[str],
        amount_in_raw: int,
    ) -> int | None:
        """El importe de salida de **ese camino**, ahora mismo.

        Es la misma llamada, con el mismo `path[]`, que hará el swap: para un
        camino de dos saltos, el número no es la composición de dos lecturas
        sino el contrato devolviendo el camino entero, que es como lo ejecuta.
        """
        amounts = await self._amounts_out(chain_key, deployment, path, amount_in_raw)
        return None if amounts is None or amounts[-1] <= 0 else amounts[-1]

    def _require_own_venue(self, quote: Quote, deployment: Deployment) -> None:
        """Corta si la cotización no es la que este motor cotizó para esa red.

        Cada red tiene aquí **su** DEX medido, y el `venue_id` lo dice
        (`sushiswap-v2@30`). Construir una cotización de otro venue —de otro DEX
        de la misma red o de otro motor— contra el router de éste ejecutaría un
        swap distinto del que el usuario leyó, así que se corta antes de
        codificar nada. El contraste es el identificador **exacto**: directo si
        la cotización no traía ruta, y de ruta si la traía, porque una
        cotización de dos saltos no puede construirse como un pool directo.
        """
        if quote.route is not None:
            expected = _route_venue(quote.pair.chain, deployment)
        else:
            expected = _venue(quote.pair.chain, deployment)
        if quote.venue.venue_id != expected.venue_id:
            raise UnsupportedOperationError(
                f"«{quote.venue.venue_id}» no es el venue que este motor cotizó en "
                f"«{quote.pair.chain}» ({expected.venue_id}): construir contra otro "
                f"pool ejecutaría un swap que el usuario no vio."
            )

    def _to_quote(
        self,
        pair: TradingPair,
        amount_in: TokenAmount,
        deployment: Deployment,
        *,
        pool: str,
        amount_out_raw: int,
        reserve_out: int,
        impact: BasisPoints,
    ) -> Quote:
        """La cotización del dominio, con la procedencia de cada cifra.

        `fee_basis` y `impact_basis` van `DERIVED` por el mismo motivo que en el
        motor de V3: la comisión no la publica una fuente, se deriva de la
        constante del protocolo **medida** contra su router (ver
        `amm_protocols`), y el impacto se calcula aquí comparando el importe que
        el router devuelve con el marginal de las reservas, que es un dato
        publicado por el pool.

        `liquidity` publica la reserva del lado quote del pool —el mismo dato
        que publican las fuentes de pools, y en un pool de producto constante
        significa exactamente la profundidad de ese lado—, leída con
        `getReserves()`. En las rutas de dos saltos va a `None`: hay dos pools y
        ninguna reserva suelta los describe.
        """
        protocol = _protocol(deployment)
        fee = BasisPoints(deployment.fee_bps)
        return Quote(
            venue=venue_for(protocol, fee, pair.chain),
            engine_id=MANIFEST.engine_id,
            pair=pair,
            amount_in=amount_in,
            amount_out=TokenAmount(amount_out_raw, pair.quote.decimals, pair.quote.symbol),
            fee_bps=fee,
            fee_basis=Measurement.DERIVED,
            price_impact_bps=impact,
            observed_at=self._clock.now(),
            liquidity=TokenAmount(reserve_out, pair.quote.decimals, pair.quote.symbol),
            impact_basis=Measurement.DERIVED,
            source_note=(
                f"Leído del pool {pool} de {protocol.label} con el "
                f"`getAmountsOut` del propio router —la misma aritmética que "
                f"ejecutará el swap—, contra el estado de la cadena en el bloque "
                f"actual. El importe ya viene neto de la comisión medida del "
                f"{_percent(deployment.fee_bps)} %; el impacto compara ese precio "
                f"con el marginal de las reservas del pool, con la comisión "
                f"descontada aparte para no contarla dos veces."
            ),
        )

    def _to_route_quote(
        self,
        pair: TradingPair,
        amount_in: TokenAmount,
        deployment: Deployment,
        route: _RouteQuote,
        impact: BasisPoints,
    ) -> Quote:
        """La cotización de una ruta de dos saltos, con su camino dentro.

        La comisión que se publica es la **suma** de las dos patas —lo que de
        verdad paga la orden—, y va `DERIVED`: es la constante medida del
        protocolo aplicada dos veces. La ruta se construye con los tokens **del
        pool**: si un lado del par es el nativo, el tramo nombra a su envoltorio,
        que es el token que cruza el pool de verdad. Los extremos siguen siendo
        el par —así lo valida el dominio— porque el envoltorio es la forma en
        que el nativo existe ahí.
        """
        protocol = _protocol(deployment)
        fee = BasisPoints(deployment.fee_bps)
        hops = (
            RouteHop(base=_pool_token(pair.base), quote=route.hub, fee_bps=fee),
            RouteHop(base=route.hub, quote=_pool_token(pair.quote), fee_bps=fee),
        )
        return Quote(
            venue=_route_venue(pair.chain, deployment),
            engine_id=MANIFEST.engine_id,
            pair=pair,
            amount_in=amount_in,
            amount_out=TokenAmount(
                route.second.amount_out_raw, pair.quote.decimals, pair.quote.symbol
            ),
            fee_bps=BasisPoints(2 * deployment.fee_bps),
            fee_basis=Measurement.DERIVED,
            price_impact_bps=impact,
            observed_at=self._clock.now(),
            liquidity=None,
            impact_basis=Measurement.DERIVED,
            source_note=(
                f"Ruta de dos saltos leída de {protocol.label} con el "
                f"`getAmountsOut` del router en una sola llamada, contra el "
                f"estado de la cadena en el bloque actual: "
                f"{_pool_token(pair.base).symbol} → {route.hub.symbol} → "
                f"{_pool_token(pair.quote).symbol}, por la comisión del "
                f"{_percent(deployment.fee_bps)} % en cada tramo. El par directo "
                f"no da salida a este tamaño, así que la orden pasa por "
                f"{route.hub.symbol} y los dos tramos se ejecutan en una sola "
                f"transacción. El impacto es la composición de los dos pools, "
                f"medida contra el marginal de cada uno."
            ),
            route=SwapRoute(hops=hops),
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
            "uniswap_v2.quote_moved",
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
        amount_out: TokenAmount,
        minimum: TokenAmount,
        pays_native: bool,
        receives_native: bool,
    ) -> str:
        """Lo que el usuario tiene que poder leer antes de confirmar."""
        protocol = _protocol(deployment)
        what = "Compra" if pays_native else "Venta"
        parts = [
            f"{what} en {quote.venue.name} ({chain(quote.pair.chain).name}): "
            f"entregas {quote.amount_in} y recibes {amount_out} "
            f"—{minimum} como mínimo, con un {SLIPPAGE_PCT} % de deslizamiento "
            f"tolerado—.",
        ]
        if quote.route is None:
            parts.append(
                f"Se ejecuta contra el pool del par en {protocol.label} a través "
                f"del router {deployment.router}; la comisión del "
                f"{_percent(deployment.fee_bps)} % va dentro del contrato, no en "
                f"el calldata."
            )
        else:
            camino = " → ".join(token.symbol for token in quote.route.tokens)
            parts.append(
                f"Se ejecuta en **un solo swap** de dos tramos —"
                f"{_percent(deployment.fee_bps)} % en cada uno— por el camino "
                f"{camino}: el par directo no da salida a este tamaño, así que la "
                f"orden pasa por el token intermedio en la misma transacción. El "
                f"router es {deployment.router}."
            )
        if pays_native:
            parts.append(
                "El importe va con la transacción y el router envuelve él mismo "
                "la moneda nativa, así que no hace falta autorizar ningún token."
            )
        else:
            parts.append(
                "Antes de esto hay que autorizar el token al router, en una "
                "transacción aparte."
            )
        if receives_native:
            parts.append(
                "El router retira el envuelto dentro de la misma función, así que "
                "lo recibido llega como moneda nativa."
            )
        parts.append(
            f"La transacción caduca en {DEADLINE_SECONDS // 60} minutos: después "
            f"de eso el router la rechaza, para no ejecutar a un precio viejo."
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


def _pool_token(token: Token) -> Token:
    """El token como existe **en el pool**: el nativo es su envoltorio.

    Se usa al describir una ruta, para que sus tramos nombren los tokens que de
    verdad cruzan los pools —un camino que dijera «POL → USDC» describiría un
    pool que no existe; el que existe es «WPOL → USDC»—. Si la red no tiene
    envoltorio se devuelve el token tal cual: quien llama ya comprobó que su
    dirección de pool existe, así que ese caso sólo puede llegar de una ruta
    construida a mano, y devolver el original es lo menos sorprendente.
    """
    if token.is_native:
        wrapped = wrapped_native(token.chain)
        if wrapped is not None:
            return wrapped
    return token


def _marginal_from(*, reserve_in: int, reserve_out: int) -> Decimal | None:
    """El precio marginal de un pool de producto constante, o `None`.

    En unidades mínimas la cuenta es una división: `reserva de salida / reserva
    de entrada` es el precio de la moneda de salida en unidades de la de entrada
    en el momento de mirar. La comisión no entra aquí —la descuenta después
    `impact_bps_from_marginal`, que es donde se compara contra lo ejecutado—.

    Un pool sin reservas en algún lado no cotiza nada: devuelve `None`, que es
    «no hay base de comparación» y no un marginal de cero.
    """
    if reserve_in <= 0 or reserve_out <= 0:
        return None
    return EXACT.divide(Decimal(reserve_out), Decimal(reserve_in))


def _route_path(
    route: SwapRoute, *, token_in: str, token_out: str
) -> tuple[str, ...]:
    """El `path[]` que esa ruta describe, en direcciones de contrato.

    Se construye **desde la ruta publicada**, no desde una búsqueda nueva: el
    payload tiene que ejecutar el camino que el usuario leyó. Al contrario que
    en V3, la comisión de los tramos no hace falta aquí —el router la lleva
    dentro—, así que un tramo sin `fee_bps` se puede ejecutar igual; lo que sí
    se exige es que los extremos sean los del par: una ruta que no empiece donde
    el par y termine donde el par ejecutaría un swap que nadie pidió, y se corta
    aquí, antes de codificar nada.
    """
    tokens: list[str] = []
    for index, hop in enumerate(route.hops):
        address_in = _on_chain_address(hop.base)
        address_out = _on_chain_address(hop.quote)
        if address_in is None or address_out is None:
            raise UnsupportedOperationError(
                f"el tramo {index + 1} de la ruta tiene un lado sin dirección "
                f"de contrato: no hay camino que codificar."
            )
        if index == 0:
            tokens.append(address_in)
        tokens.append(address_out)
    if tokens[0] != token_in or tokens[-1] != token_out:
        raise UnsupportedOperationError(
            f"la ruta de «{route.tokens[0].symbol} → "
            f"{route.tokens[-1].symbol}» no corresponde al par de la "
            f"cotización: no se construye un swap por un camino que el "
            f"usuario no vio."
        )
    return tuple(tokens)


def _protocol(deployment: Deployment) -> ProtocolRef:
    """El protocolo identificado de ese despliegue, con el parser común.

    El identificador sale del que la tabla midió («uniswap_v2», «sushiswap_v2»,
    «quickswap_v2», «pancakeswap_v2»), así que la etiqueta y la clave del venue
    son las mismas que las de cualquier otra fuente que publique ese DEX.
    """
    return parse_dex_id(deployment.protocol)


def _venue(chain_key: str, deployment: Deployment) -> Venue:
    """El venue del DEX medido de esa red, con su comisión dentro de la identidad.

    Se delega en `venue_for` —el mismo que usan las demás fuentes— para que el
    `venue_id` de este motor sea **el mismo** que el de cualquier otro que mire
    ese pool: `uniswap-v2@30` es el mismo sitio lo mire quien lo mire. Si cada
    motor se inventara el suyo, la misma fila aparecería dos veces en la tabla y
    la comparación las cruzaría como si fueran dos sitios.
    """
    return venue_for(_protocol(deployment), BasisPoints(deployment.fee_bps), chain_key)


def _route_venue(chain_key: str, deployment: Deployment) -> Venue:
    """El venue de una ruta: **la comisión del camino**, no la de un pool.

    Una ruta no es un pool: aquí las dos patas pagan la misma comisión, y el
    identificador la lleva repetida (`sushiswap-v2@30+30`) para que la ruta no
    se funda con el pool directo en la misma fila: son sitios de ejecución
    distintos, igual que en V3 dos tramos de comisión lo son.
    """
    protocol = _protocol(deployment)
    fee = BasisPoints(deployment.fee_bps)
    piece = f"{fee.as_percent():f} %"
    return Venue(
        venue_id=f"{protocol.key}@{fee.value}+{fee.value}",
        name=f"{protocol.label} {piece} + {piece}",
        kind=VenueKind.DEX,
        chain=chain_key,
    )


def _percent(fee_bps: int) -> str:
    """La comisión en porcentaje legible: `30` → `"0.3"`, `25` → `"0.25"`."""
    return f"{BasisPoints(fee_bps).as_percent().normalize():f}"


_ZERO_ADDRESS: Final = "0x" + "00" * 20


# --------------------------------------------------------------------------- #
# Publicación
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class UniswapV2Provider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: object) -> UniswapV2Engine:
        # No hay nada que configurar: ni clave, ni host, ni límite. El parámetro
        # está porque lo exige el contrato de `EngineProvider`, y acepta cualquier
        # cosa precisamente porque este motor no lee nada de ahí. Un motor que
        # dependiera de una clave la declararía en `required_config` y llegaría
        # aquí ya validada.
        return UniswapV2Engine()


PROVIDER: Final = UniswapV2Provider()
