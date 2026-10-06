"""Uniswap V4 por contrato, **sin clave**: la segunda vía que no se detiene.

### Qué es y por qué existe

Es el hermano de `uniswap_v3` y existe por el mismo motivo: cotizar y construir
sin credencial, para que la aplicación siga sirviendo cuando la cuota de las APIs
se agota. Los dos hablan con contratos por nodos públicos y ninguno necesita una
clave; juntos son la garantía de que «las gratuitas no se detienen nunca».

Pero **no son el mismo protocolo con otro nombre**, y las diferencias no son de
detalle: cambian qué se lee, cómo se identifica un pool y qué se firma.

### Cotizar aquí no es simular

El `V4Quoter` ejecuta el cálculo del swap **sobre el estado real del pool en el
bloque actual** y devuelve el importe exacto que daría ahora. Es `eth_call`: no
cambia estado, no cuesta gas y no necesita clave. Medido contra Base mainnet: una
entrada de 1 USDC devuelve el importe de WETH al wei, y lo que cuesta sobre el
precio marginal del pool coincide con el tramo más el impacto de la orden.

### Un pool de V4 no es un contrato, y eso cambia la primera pregunta

En V3 lo primero que se hace es preguntarle a la fábrica si existe un pool para
ese par y ese tramo. Aquí **no hay fábrica**: un pool es una entrada en el almacén
del `PoolManager`, identificada por el `keccak256` de su clave
—`(currency0, currency1, fee, tickSpacing, hooks)`—, así que la pregunta «¿existe
este pool?» se responde derivando el identificador y leyendo su liquidez. Si el
orden de las dos monedas se invierte, el `keccak256` da otro número y se acaba
preguntando por un pool distinto: medido, el resultado es `PoolNotInitialized()`
del `PoolManager`, envolviendo el revert que lanza el cotizador. Por eso el orden
lo deriva `calldata.orient`, que es el único sitio que decide.

**El filtro es la liquidez, no el precio.** Un pool de V4 se puede inicializar con
liquidez cero y precio válido: medido en Base, los tramos del 0,01 % y el 1 % de
WETH/USDC tienen `sqrtPriceX96` distinto de cero y `getLiquidity` en cero. Filtrar
por el precio los daría por buenos y el motor cotizaría pools que no pueden
ejecutar nada. Se lee la liquidez de cada tramo aunque eso sea una lectura más:
la alternativa es un revert **vacío** —que es como revierte todo lo que no
decodifica— y un revert vacío no se distingue de un nodo roto ni de un pool
inexistente. Una lectura extra compra que el fallo se explique.

### El nativo es una moneda de pleno derecho

Ésta es la diferencia de fondo con V3. En V3 el nativo no existe para el AMM: se
envuelve siempre y se opera contra el pool de WETH. En V4 el nativo es una
`Currency` como cualquier otra —se representa como la dirección cero— y hay
**pools de ETH nativo**, medidos en las siete redes. Consecuencia que se nota en
el payload: pagar con ETH **no** envuelve nada, liquida con `msg.value` contra el
pool nativo; y cobrar en ETH **no** desenvuelve nada, el `PoolManager` entrega la
moneda nativa directamente al destinatario. Es un camino más corto y con una
transacción menos que el de V3, y sale del diseño del protocolo, no de una
elección nuestra.

### El destinatario: la trampa que no se ve en la firma

La acción natural para cobrar es `TAKE_ALL`, que no lleva destinatario. Medido
leyendo la fuente verificada del `UniversalRouter`: `TAKE_ALL` entrega a
`msgSender()`, que es quien llamó a `execute` — el firmante. Es decir: **con
`TAKE_ALL` el destinatario no se elige, se hereda**.

Este motor declara un destinatario, así que usar `TAKE_ALL` haría que el payload
dijera una cosa y la cadena hiciera otra en el único detalle que no se puede
deshacer: a dónde va el dinero. Se usa `TAKE` con delta abierto, que cobra el
saldo **íntegro** igual que `TAKE_ALL` y sí lleva destinatario explícito. La
protección de precio no se pierde por eso: viaja dentro del propio swap, en el
`amountOutMinimum` de la struct, que es donde el importe se conoce.

Y hay dos direcciones que en `TAKE` **no son direcciones**: la 1 y la 2 son
centinelas que el contrato traduce a «quien firma» y «el propio router». Pasar la
2 dejaría los fondos dentro del router. `calldata.take` las rechaza, porque es
conocimiento del protocolo que quien valida la dirección aguas arriba no puede
tener.

### Los dos permisos encadenados

El `UniversalRouter` **no mueve el token él mismo**: se lo pide a Permit2. Un swap
con token exige dos autorizaciones —el ERC-20 a Permit2 y Permit2 al router— y
ninguna sustituye a la otra. Medido: sin ellas el fallo no viene del router sino
de Permit2, que responde `AllowanceExpired`. El motor declara el permiso que
corresponde en el propio payload (`TokenApproval`), y con eso el camino de
ejecución sabe qué autorizar antes de firmar.

A diferencia de V3, aquí **el `deadline` existe en todas las redes**: el
`UniversalRouter` lo lleva en su `execute`, así que no hay ninguna red donde el
payload dependa sólo del mínimo.

Las direcciones están en `addresses.py` y son **medidas**: cada contrato atestigua
su identidad exponiendo su `poolManager()`, los tres coinciden, y cada selector se
comprobó buscándolo literalmente en el bytecode desplegado. Añadir una red es
medirla.

### Lo que este motor NO hace

**No firma y no emite.** Devuelve una `UnsignedTransaction` para que el usuario la
revise; la clave y el envío viven en `infra.evm`, detrás de la barrera de modo y
del diálogo de confirmación. Tampoco **estima el gas**: para eso hace falta la
dirección que paga, que sólo se conoce al firmar —el motor no la tiene y no debe
tenerla—, así que `gas_limit` va a `None` y lo estima quien firma, contra el
estado del momento.

Y **no sondea pools con hooks**. Un pool con ganchos es otra clave, y ejecutar sus
ganchos puede hacer cualquier cosa —incluso cambiar la comisión en cada
operación—, así que no se puede prometer nada sobre el resultado. No se listan
pools que no se puedan prometer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from types import MappingProxyType
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
from amigocompora.engines.evm_rpc import ChainReader, rpc_hosts
from amigocompora.engines.uniswap_math import impact_bps
from amigocompora.engines.uniswap_v4 import calldata as abi
from amigocompora.engines.uniswap_v4.addresses import (
    DEPLOYMENTS,
    FEE_TIERS,
    PERMIT2,
    SWAP_CHAINS,
    Deployment,
)

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "Uniswap V4 (directo)"

#: El protocolo, identificado una vez. `parse_dex_id` es el mismo que usan las
#: demás fuentes, así que dos motores que miran el mismo pool producen el mismo
#: `venue_id` — que es lo que permite compararlos en la misma tabla. Y como el
#: identificador lleva la versión, el de V4 (`uniswap-v4@30`) no se confunde con
#: el de V3 (`uniswap-v3@30`): son dos sitios distintos aunque el tramo coincida.
PROTOCOL: Final = parse_dex_id("uniswap_v4")

#: Deslizamiento tolerado al construir. Es **la única protección** que el payload
#: lleva contra el movimiento del precio entre que se cotiza y que se mina, junto
#: con la caducidad: el `amountOutMinimum` sale de aquí y se comprueba dentro del
#: propio swap. Se aplica sobre la cotización fresca, no sobre la que se mostró.
SLIPPAGE_PCT: Final = Decimal("0.5")

#: Deriva máxima tolerada entre la cotización que el usuario vio y la que hay al
#: construir, con el mismo valor y el mismo razonamiento que en los demás motores
#: que construyen: el mercado se mueve entre pintar la tabla y pulsar el botón.
MAX_QUOTE_DRIFT_BPS: Final = BasisPoints(100)

#: Impacto por encima del cual la cotización se omite, como en los demás motores.
#: Un pool con liquidez residual produce impactos enormes que sólo añaden ruido.
MAX_PRICE_IMPACT: Final = BasisPoints(1_000)

#: Cuánto vale el payload antes de caducar. Aquí aplica **en todas las redes**,
#: porque el `UniversalRouter` lleva el plazo en su `execute`: pasadas estas
#: horas el contrato rechaza la orden y no se ejecuta a un precio viejo. Media
#: hora es de sobra para revisar una transacción y no tanto como para firmar a un
#: precio de hace un día.
DEADLINE_SECONDS: Final = 1_800

#: Cuántas unidades de comisión de V4 hay en un punto básico. V4 cuenta la
#: comisión en **centésimas de punto básico** —el tramo `3000` es el 0,30 %—,
#: igual que V3, mientras que el dominio la cuenta en puntos básicos, donde ese
#: mismo tramo es `30`. La conversión tiene un solo sitio para poder buscarla.
_V4_FEE_UNITS_PER_BPS: Final = 100

#: El espaciado de ticks canónico de cada tramo, en la unidad de V4. La clave del
#: pool se deriva de la pareja, así que hay que poder recuperarla desde el
#: `venue_id`, que sólo lleva la comisión. Se construye desde `FEE_TIERS` para que
#: no exista una segunda lista que pueda desincronizarse.
_TICK_SPACING_BY_FEE: Final[Mapping[int, int]] = MappingProxyType(dict(FEE_TIERS))

MANIFEST: Final = EngineManifest(
    engine_id="uniswap_v4",
    name="Uniswap V4 — directo, sin clave",
    version="1.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary=(
        "Cotiza y construye swaps contra los pools de Uniswap V4 leyendo el "
        "estado del PoolManager y el V4Quoter por nodos públicos. Sin ninguna "
        "clave, como V3, y con una diferencia que sí se nota: en V4 la moneda "
        "nativa es una moneda más, así que opera con ETH nativo en vez de "
        "envolverlo. Compara los cuatro tramos de comisión."
    ),
    capabilities=frozenset(
        {Capability.READ_CHAIN, Capability.COMPUTE_ROUTE, Capability.PREPARE_TX}
    ),
    swap_chains=SWAP_CHAINS,
    # Detrás de V3 (20) y delante de 0x (100). V3 va primero porque está medido
    # contra más pares y porque en las redes donde existe el SwapRouter V1 su
    # camino de permiso es uno y no dos; V4 va justo detrás para que la misma
    # orden tenga una segunda medición sin clave, y no para decir que su ruta sea
    # peor —la comparación de precios lo decide con datos, y cada motor publica su
    # propio `venue_id`— sino para fijar quién construye cuando hay que elegir.
    swap_priority=25,
    # Ni una sola clave. Es la propiedad que hace que este motor no se detenga.
    required_config=(),
    # Derivados de la tabla de nodos medidos, no escritos a mano: un manifiesto
    # que declare hosts distintos de los que se usan falla en el transporte con un
    # error que habla de permisos y no de la causa.
    allowed_hosts=rpc_hosts(SWAP_CHAINS),
)


@dataclass(frozen=True, slots=True)
class _Pool:
    """Un pool de V4 localizado: su clave derivada y lo que tiene dentro."""

    fee: int
    tick_spacing: int
    #: El `keccak256` del `PoolKey`. Es la dirección del pool a todos los efectos:
    #: con él se leen el estado y la liquidez.
    pool_id: str
    #: La liquidez del rango activo. Se guarda porque es lo que decidió que este
    #: pool exista para el motor; **no** se publica en la cotización, y el porqué
    #: está en `_to_quote`.
    liquidity: int


@dataclass(frozen=True, slots=True)
class _PoolQuote:
    """Una cotización cruda de un pool concreto, antes de decidir nada."""

    pool: _Pool
    amount_out_raw: int


class UniswapV4Engine:
    """Cotizaciones y payloads de swap contra Uniswap V4, sin credenciales."""

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
        existen: en V4 cuáles existen depende del par —y del espaciado con el que
        alguien los inicializara— y sólo se sabe derivando la clave de cada uno.
        Preguntarlo aquí gastaría cuatro lecturas por cada vez que alguien dibuja
        la lista de venues. Un tramo sin pool no produce cotización, que es lo que
        de verdad importa; y los `venue_id` de esta lista son exactamente los que
        llevan las cotizaciones, para que la tabla no muestre un venue que después
        no case con ninguna fila.

        Medido: la lista de tramos **no** es la misma en todas las redes. Optimism
        sólo tiene el 0,05 % y Unichain sólo el 0,30 % para los pares probados, y
        en Base el pozo profundo es el 0,30 % en vez del 0,05 %. Aquí se listan
        los cuatro porque son los cuatro que se preguntan; los que no existen
        simplemente no devuelven nada.
        """
        if chain_key not in SWAP_CHAINS:
            return ()
        return tuple(_venue(chain_key, fee) for fee, _spacing in FEE_TIERS)

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        """Cotiza vender `amount_in` de la base contra el mejor pool disponible."""
        deployment = DEPLOYMENTS.get(pair.chain)
        if deployment is None or not amount_in.is_positive:
            return ()
        token_in = _on_chain_address(pair.base)
        token_out = _on_chain_address(pair.quote)
        if token_in == token_out:
            # Las dos partes resuelven a la misma moneda, así que no hay par que
            # cotizar: la clave del pool saldría con las dos iguales. Ocurre con
            # dos tokens declarados como nativos, que son el mismo.
            #
            # Y no es el caso de un nativo contra su envoltorio, que es lo que
            # podría parecer: a diferencia de V3, aquí el nativo tiene dirección
            # propia —la cero— y WETH es otra moneda, así que ése es un par como
            # cualquier otro y tiene su pool.
            return ()

        pools = await self._pools_for(pair.chain, deployment, token_in, token_out)
        if not pools:
            _log.debug("uniswap_v4.no_pool", pair=pair.symbol, chain=pair.chain)
            return ()

        candidates = await self._quotes_for(
            pair.chain, deployment, token_in, token_out, amount_in.raw, pools
        )
        if not candidates:
            _log.debug("uniswap_v4.no_liquidity", pair=pair.symbol, chain=pair.chain)
            return ()

        for candidate in sorted(candidates, key=lambda item: item.amount_out_raw, reverse=True):
            state = await self._pool_state(pair.chain, deployment, candidate.pool)
            if state is None:
                continue
            # El sentido del precio lo deriva el mismo `orient` que construye el
            # swap, así que la cifra que se publica y la orden que se firma salen
            # de la misma decisión sobre cuál de las dos monedas es la 0.
            par = abi.orient(
                token_in, token_out, candidate.pool.fee, candidate.pool.tick_spacing
            )
            impact = impact_bps(
                amount_in_raw=amount_in.raw,
                amount_out_raw=candidate.amount_out_raw,
                sqrt_price_x96=state[0],
                token_in_is_token0=par.zero_for_one,
                fee=BasisPoints(candidate.pool.fee // _V4_FEE_UNITS_PER_BPS),
            )
            if impact is None:
                continue
            if abs(impact.value) > MAX_PRICE_IMPACT.value:
                _log.debug(
                    "uniswap_v4.quote_skipped_impact",
                    pair=pair.symbol,
                    fee=candidate.pool.fee,
                    impact_bps=impact.value,
                )
                continue
            return (self._to_quote(pair, amount_in, candidate, impact),)

        _log.debug("uniswap_v4.quote_unreadable_state", pair=pair.symbol, chain=pair.chain)
        return ()

    # --------------------------------------------------------------- construir #
    async def plan_swap(self, quote: Quote, *, recipient: str) -> UnsignedTransaction:
        """Construye la transacción sin firmar del swap que describe `quote`.

        ### Por qué vuelve a cotizar

        La cotización que se muestra puede tener minutos, y entre una cosa y otra
        el precio se mueve. Se vuelve a preguntar al mismo pool —el del tramo que
        la cotización nombra, no «el mejor de ahora»: cambiar de pool a espaldas
        del usuario sería cambiar el venue después de que lo eligiera— y si el
        importe se ha movido más de lo tolerado, se **niega a construir**.

        La cotización fresca es la que fija el `amountOutMinimum`, así que el
        deslizamiento se calcula sobre el precio de ahora y no sobre el de la
        pantalla.
        """
        chain_key = quote.pair.chain
        deployment = DEPLOYMENTS.get(chain_key)
        if deployment is None:
            raise UnsupportedOperationError(
                f"este motor no construye swaps en «{chain_key}»: sus contratos de "
                f"V4 no están medidos ahí. Cubre {', '.join(sorted(SWAP_CHAINS))}. "
                f"Activa un motor que cubra esa red."
            )
        fee, tick_spacing = _tier_from_venue(quote.venue.venue_id)
        token_in = _on_chain_address(quote.pair.base)
        token_out = _on_chain_address(quote.pair.quote)
        if token_in == token_out:
            raise UnsupportedOperationError(
                f"«{quote.pair.symbol}» resuelve a una sola moneda de {chain_key}: "
                f"sin dos partes distintas no hay pool contra el que construir, y la "
                f"clave saldría con las dos iguales."
            )

        fresh = await self._fresh_out(
            chain_key, deployment, token_in, token_out, quote.amount_in.raw, fee, tick_spacing
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
            fee=fee,
            tick_spacing=tick_spacing,
            token_in=token_in,
            token_out=token_out,
            recipient=recipient,
            amount_in_raw=quote.amount_in.raw,
            minimum_raw=minimum.raw,
        )
        approval = (
            None
            if pays_native
            else TokenApproval(spender=deployment.universal_router, via=PERMIT2)
        )
        _log.info(
            "uniswap_v4.swap_planned",
            pair=quote.pair.symbol,
            chain=chain_key,
            fee=fee,
            tick_spacing=tick_spacing,
            out_raw=fresh,
            minimum_raw=minimum.raw,
            router=deployment.universal_router,
            permit2=PERMIT2,
        )
        return UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=deployment.universal_router,
            calldata=calldata,
            value=(
                # Cuando se paga con el nativo, el importe viaja con la
                # transacción y el PoolManager lo cobra de ahí. No se envuelve
                # nada: el pool en el que se opera tiene el nativo dentro.
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
        """El `UniversalRouter` medido para esa red, o `None` si no está.

        Es el contraste contra el que el camino de ejecución comprueba el destino
        del payload antes de firmar: si el `to` no es éste, no se firma. Se lee de
        la tabla de despliegues en vez de escribirse aparte porque es la misma
        pregunta —«¿a qué contrato manda este motor los swaps?»— y dos listas
        acabarían discrepando.
        """
        deployment = DEPLOYMENTS.get(chain_key)
        return None if deployment is None else deployment.universal_router

    # ------------------------------------------------------------------ pasos #
    async def _pools_for(
        self,
        chain_key: str,
        deployment: Deployment,
        token_in: str,
        token_out: str,
    ) -> tuple[_Pool, ...]:
        """Los pools que existen para el par y **tienen liquidez**, tramo a tramo.

        Dos pasos por tramo y los dos hacen falta:

        1. Derivar el `poolId` con `calldata.pool_id`, que ordena las monedas por
           su valor. Invertirlas daría el identificador de otro pool, y el único
           aviso sería un revert del `PoolManager`.
        2. Leer su liquidez. **Éste es el filtro**, no el precio: medido en Base,
           los tramos del 0,01 % y el 1 % de WETH/USDC tienen precio válido y cero
           liquidez. Un pool así se puede leer pero no puede ejecutar nada, y
           cotizarlo revienta sin decir por qué.

        Las cuatro lecturas van en paralelo: son independientes y son la primera
        ronda, la que más se nota en el tiempo de respuesta.

        Un revert aquí **no** se silencia. El `StateView` no tiene ningún motivo
        para revertir con un `bytes32` válido —devuelve cero para un pool que no
        existe—, así que si revierte es que la dirección o el selector no son los
        que la tabla dice. Eso hay que verlo, y verlo antes de firmar nada.
        """
        derived = [
            (fee, spacing, abi.pool_id(token_in, token_out, fee, spacing))
            for fee, spacing in FEE_TIERS
        ]
        results = await asyncio.gather(
            *(
                self._reader.eth_call(
                    chain_key, deployment.state_view, abi.get_liquidity(pool_id)
                )
                for _fee, _spacing, pool_id in derived
            ),
            return_exceptions=True,
        )
        pools: list[_Pool] = []
        for (fee, spacing, pool_id), result in zip(derived, results, strict=True):
            if isinstance(result, BaseException):
                raise result
            liquidity = abi.decode_uint(result, 0)
            if liquidity is not None and liquidity > 0:
                pools.append(
                    _Pool(
                        fee=fee,
                        tick_spacing=spacing,
                        pool_id=pool_id,
                        liquidity=liquidity,
                    )
                )
        return tuple(pools)

    async def _quotes_for(
        self,
        chain_key: str,
        deployment: Deployment,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        pools: Sequence[_Pool],
    ) -> tuple[_PoolQuote, ...]:
        """El importe que daría cada pool para ese tamaño, en paralelo.

        Un cotizador que revierte significa que ese pool no puede dar salida a
        esta orden —liquidez insuficiente en el rango activo, o ninguna—, y eso es
        una respuesta, no una caída: se omite ese tramo y se sigue con los demás.
        Lo que **no** se omite es que ningún nodo conteste, que sí es la fuente
        caída y se propaga.
        """
        results = await asyncio.gather(
            *(
                self._reader.eth_call(
                    chain_key,
                    deployment.quoter,
                    abi.quote_exact_input_single(
                        token_in, token_out, pool.fee, pool.tick_spacing, amount_in_raw
                    ),
                )
                for pool in pools
            ),
            return_exceptions=True,
        )
        found: list[_PoolQuote] = []
        for pool, result in zip(pools, results, strict=True):
            if isinstance(result, SourceResponseError):
                _log.debug(
                    "uniswap_v4.pool_without_quote",
                    chain=chain_key,
                    fee=pool.fee,
                    pool_id=pool.pool_id,
                )
                continue
            if isinstance(result, BaseException):
                raise result
            amount_out = abi.decode_quote(result)
            if amount_out is None or amount_out <= 0:
                continue
            found.append(_PoolQuote(pool=pool, amount_out_raw=amount_out))
        return tuple(found)

    async def _pool_state(
        self, chain_key: str, deployment: Deployment, pool: _Pool
    ) -> tuple[int, int] | None:
        """`(sqrtPriceX96, comisión del LP)` del pool, o `None` si no se puede leer.

        Las dos cifras vienen de la misma lectura, que es lo que las hace
        baratas: el precio es la referencia contra la que se mide el impacto, y la
        comisión es un contraste.

        Ese contraste no es decorativo. La comisión que el pool declara tiene que
        ser **la del tramo con el que se derivó su clave**, porque el `poolId` sale
        de la clave y no de lo que el pool tenga dentro: si no coincidieran, el
        precio que se acaba de leer sería el de otro sitio. Medido: coincide en los
        cuatro tramos de Base y de Ethereum. Por eso una discrepancia **lanza** en
        vez de omitir el pool — omitirlo convertiría una tabla de direcciones
        equivocada en un motor que simplemente no encuentra nada, que es el fallo
        mudo que este motor no se puede permitir.

        El `tick` y la comisión de protocolo que también devuelve el `StateView`
        no se leen: el primero no se usa, y la segunda se la queda el pool por
        dentro y ya viene descontada en lo que da el cotizador.
        """
        try:
            raw = await self._reader.eth_call(
                chain_key, deployment.state_view, abi.get_slot0(pool.pool_id)
            )
        except SourceResponseError:
            _log.debug(
                "uniswap_v4.pool_state_unreadable",
                chain=chain_key,
                fee=pool.fee,
                pool_id=pool.pool_id,
            )
            return None
        leido = abi.decode_slot0(raw)
        if leido is None:
            _log.debug(
                "uniswap_v4.pool_state_unreadable",
                chain=chain_key,
                fee=pool.fee,
                pool_id=pool.pool_id,
            )
            return None
        sqrt_price, lp_fee = leido
        if sqrt_price <= 0:
            return None
        if lp_fee != pool.fee:
            raise SourceResponseError(
                f"el pool {pool.pool_id} de {chain_key} declara una comisión de "
                f"{lp_fee} cuando su clave se derivó con el tramo {pool.fee}: la "
                f"clave de un pool de V4 se calcula **con** la comisión dentro, así "
                f"que esto significa que el estado que se está leyendo no es el de "
                f"ese pool. Lo más probable es que la dirección del StateView de "
                f"{chain_key} no sea la de la tabla. No se cotiza con un precio que "
                f"no se sabe de dónde viene."
            )
        return sqrt_price, lp_fee

    async def _fresh_out(
        self,
        chain_key: str,
        deployment: Deployment,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        fee: int,
        tick_spacing: int,
    ) -> int | None:
        """El importe de salida del pool **de ese tramo**, ahora mismo."""
        try:
            raw = await self._reader.eth_call(
                chain_key,
                deployment.quoter,
                abi.quote_exact_input_single(token_in, token_out, fee, tick_spacing, amount_in_raw),
            )
        except SourceResponseError:
            # El pool no puede dar salida a este tamaño ahora: se trata como «ya
            # no hay cotización», que es lo que es.
            return None
        amount_out = abi.decode_quote(raw)
        return None if amount_out is None or amount_out <= 0 else amount_out

    def _calldata(
        self,
        *,
        fee: int,
        tick_spacing: int,
        token_in: str,
        token_out: str,
        recipient: str,
        amount_in_raw: int,
        minimum_raw: int,
    ) -> str:
        """El calldata del swap: tres acciones dentro de un comando.

        Las tres van juntas y en este orden porque el `PoolManager` liquida por
        diferencias dentro de un mismo bloqueo: el swap deja una deuda en la
        moneda que entra y un saldo a favor en la que sale, y las dos acciones
        siguientes cierran esas dos cuentas. Separarlas en transacciones distintas
        no tendría sentido —el bloqueo no sobrevive— y en otro orden tampoco:
        primero se intercambia, y sólo entonces hay algo que pagar y algo que
        cobrar.

        - `SWAP_EXACT_IN_SINGLE` lleva dentro el `amountOutMinimum`, así que **la
          protección de precio viaja en el propio swap** y es donde el importe se
          conoce. Es lo que permite usar `TAKE` en vez de `TAKE_ALL` sin perder
          nada: `TAKE_ALL` comprobaría un mínimo sobre el saldo, que después de un
          solo swap es el mismo número, a cambio de renunciar a elegir el
          destinatario.
        - `SETTLE_ALL` paga la deuda íntegra. El importe que se le pasa es un
          **tope**, y va con el número exacto que se calculó: si el pool pidiera
          más, la transacción se niega en vez de pagar de más. Con el nativo, el
          contrato lo cobra del `value` que viaja con la transacción.
        - `TAKE` cobra el saldo a favor íntegro —el cero es «todo», no «nada»— y lo
          entrega al destinatario que el motor declara. Con el nativo, el
          `PoolManager` lo entrega directamente, sin desenvolver nada.

        El `deadline` va siempre: el `UniversalRouter` lo lleva en su `execute` en
        todas las redes, así que no hay ninguna donde la orden pueda quedar viva
        para siempre.
        """
        deadline = int(self._clock.now().timestamp()) + DEADLINE_SECONDS
        actions = (abi.ACTION_SWAP_EXACT_IN_SINGLE, abi.ACTION_SETTLE_ALL, abi.ACTION_TAKE)
        params = (
            abi.swap_exact_in_single(
                token_in,
                token_out,
                fee,
                tick_spacing,
                amount_in_raw=amount_in_raw,
                amount_out_min_raw=minimum_raw,
            ),
            abi.settle_all(token_in, amount_in_raw),
            abi.take(token_out, recipient, abi.OPEN_DELTA),
        )
        return abi.execute((abi.COMMAND_V4_SWAP,), (abi.v4_swap_input(actions, params),), deadline)

    def _to_quote(
        self,
        pair: TradingPair,
        amount_in: TokenAmount,
        candidate: _PoolQuote,
        impact: BasisPoints,
    ) -> Quote:
        """La cotización del dominio, con la procedencia de cada cifra.

        `fee_basis` es `DERIVED` y no `REPORTED` por una razón concreta: la
        comisión no la publica una fuente, la deducimos del tramo con el que se
        derivó la clave de ese pool —el `poolId` se calcula **con** la comisión
        dentro, así que el tramo **es** la comisión del pool—. `REPORTED` diría
        que alguien la publica y no es el caso. Que el pool lo confirme al leer su
        estado es lo que `_pool_state` comprueba.

        `liquidity` va a `None` a propósito, y aquí se nota más que en V3 porque
        este motor **sí** lee la liquidez: se lee para decidir si el pool puede
        ejecutar algo, no para publicarla. La `L` de un pool de liquidez
        concentrada no es una cantidad de ninguna moneda —convertirla a una
        exigiría suponer un rango de precios—, y ponerla en la misma columna en la
        que un pool de producto constante publica sus reservas compararía dos
        cosas distintas. El impacto de precio, que sí se mide con el precio real
        del pool, cubre el trabajo de avisar de que un pool es poco profundo.
        """
        return Quote(
            venue=_venue(pair.chain, candidate.pool.fee),
            engine_id=MANIFEST.engine_id,
            pair=pair,
            amount_in=amount_in,
            amount_out=TokenAmount(
                candidate.amount_out_raw, pair.quote.decimals, pair.quote.symbol
            ),
            fee_bps=BasisPoints(candidate.pool.fee // _V4_FEE_UNITS_PER_BPS),
            fee_basis=Measurement.DERIVED,
            price_impact_bps=impact,
            observed_at=self._clock.now(),
            liquidity=None,
            impact_basis=Measurement.DERIVED,
            source_note=(
                f"Leído del pool de {SOURCE_NAME} del tramo "
                f"{_percent(candidate.pool.fee)} % ({candidate.pool.pool_id}) con el "
                f"V4Quoter, contra el estado de la cadena en el bloque actual. El "
                f"importe de salida es el que daría la ejecución ahora mismo, y ya "
                f"viene neto de comisión; el impacto es la diferencia entre ese "
                f"precio y el marginal del pool, con la comisión descontada aparte "
                f"para no contarla dos veces."
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
            "uniswap_v4.quote_moved",
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
            f"Universal Router {deployment.universal_router}.",
        ]
        if pays_native:
            parts.append(
                "El importe va con la transacción y se paga con la moneda nativa "
                "contra un pool de nativo: **no se envuelve nada** y no hace falta "
                "autorizar ningún token."
            )
        else:
            parts.append(
                f"Antes de esto hacen falta **dos** permisos, no uno: el del token "
                f"a Permit2 ({PERMIT2}) y el de Permit2 al router. El Universal "
                f"Router no mueve el token él mismo."
            )
        if receives_native:
            parts.append(
                "Lo recibido llega como moneda nativa directamente del contrato, "
                "sin pasar por un envoltorio."
            )
        parts.append(
            f"El destinatario es el de arriba y va escrito en la orden: en V4 el "
            f"contrato no lo deduce del firmante. La transacción caduca en "
            f"{DEADLINE_SECONDS // 60} minutos."
        )
        return " ".join(parts)


# --------------------------------------------------------------------------- #
# Derivaciones puras
# --------------------------------------------------------------------------- #
def _on_chain_address(token: Token) -> str:
    """La moneda con la que ese token existe en el pool.

    Aquí V4 se separa de V3 y la diferencia es deliberada. En V3 el nativo no
    existe para el AMM, así que se sustituye por su envoltorio ERC-20 y se opera
    contra el pool de WETH. En V4 el nativo **es** una moneda: se representa como
    la dirección cero y hay pools de nativo —medidos, en las siete redes—, así que
    sustituirlo por el envoltorio llevaría al pool equivocado en vez de al que
    corresponde. Pagar con ETH usaría el pool de WETH, que es otra cosa.

    No devuelve `None` **nunca**, y por eso tampoco se comprueba: en el dominio un
    token es nativo exactamente cuando no tiene dirección, así que todo token cae
    en uno de los dos casos. En V3 sí hay un tercero —una red sin envoltorio
    nativo, donde un token nativo no tiene ninguna dirección con la que operar—;
    aquí no existe, porque el nativo tiene la suya.

    Se normaliza a minúsculas porque las fuentes discrepan en el *checksum* y
    comparar dos formas de la misma dirección daría dos monedas distintas —y aquí
    además el orden de las dos decide la clave del pool—.
    """
    address = token.address
    return _ZERO_ADDRESS if address is None else address.lower()


def _venue(chain_key: str, fee: int) -> Venue:
    """El venue de un pool, con el tramo de comisión dentro de la identidad.

    Se delega en `venue_for` —el mismo que usan las demás fuentes— para que el
    `venue_id` de este motor sea **el mismo** que el de cualquier otro que mire
    ese pool. El identificador del protocolo lleva la versión, así que un pool de
    V4 no se confunde con el de V3 del mismo par y el mismo tramo: son dos sitios
    distintos donde operar y tienen que salir en dos filas.
    """
    return venue_for(PROTOCOL, BasisPoints(fee // _V4_FEE_UNITS_PER_BPS), chain_key)


def _tier_from_venue(venue_id: str) -> tuple[int, int]:
    """El tramo y el espaciado de ticks que nombra un `venue_id`.

    Es el camino de vuelta de `_venue`: la cotización identifica el pool por su
    tramo y al construir hay que recuperar la pareja completa, porque la clave del
    pool se deriva de `(comisión, espaciado)` y construir con otro espaciado
    apuntaría a otro pool —o a ninguno— sin que nada chirriara hasta el revert.

    El `venue_id` sólo lleva la comisión porque es lo que distingue dos pools en
    la tabla, y el espaciado se recupera de la misma tabla de tramos que se
    sondea. Pedir un tramo que no esté en ella es un `venue_id` de otro motor o
    inventado, y se rechaza en vez de suponer un espaciado.

    La conversión de unidades no es un detalle: `venue_for` escribe la comisión en
    **puntos básicos** —`uniswap-v4@30` es el 0,30 %— porque es la unidad del
    dominio y la comparten todos los motores; pero la clave del pool quiere el
    tramo en **centésimas de punto básico**, donde ese mismo pool es `3000`.
    Devolver el número del `venue_id` tal cual apuntaría al pool del 0,01 % —que
    en la mayoría de los pares ni existe— y el fallo aparecería como un revert del
    `PoolManager` sin relación aparente con la causa.
    """
    prefix, _, tier = venue_id.partition("@")
    if prefix != PROTOCOL.key or not tier.isdigit() or int(tier) <= 0:
        raise UnsupportedOperationError(
            f"«{venue_id}» no es un venue de {SOURCE_NAME}: este motor sólo "
            f"construye contra pools identificados como «{PROTOCOL.key}@<tramo>»."
        )
    fee = int(tier) * _V4_FEE_UNITS_PER_BPS
    spacing = _TICK_SPACING_BY_FEE.get(fee)
    if spacing is None:
        raise UnsupportedOperationError(
            f"«{venue_id}» nombra el tramo {fee} en unidades de V4, que este motor "
            f"no sondea: sondea {', '.join(str(known) for known in sorted(_TICK_SPACING_BY_FEE))}. "
            f"Sin espaciado de ticks no se puede derivar la clave del pool."
        )
    return fee, spacing


def _percent(fee: int) -> str:
    """El tramo en porcentaje legible, sin ceros de relleno: `3000` → `"0.3"`."""
    return f"{BasisPoints(fee // _V4_FEE_UNITS_PER_BPS).as_percent().normalize():f}"


_ZERO_ADDRESS: Final = "0x" + "00" * 20


# --------------------------------------------------------------------------- #
# Publicación
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class UniswapV4Provider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: object) -> UniswapV4Engine:
        # No hay nada que configurar: ni clave, ni host, ni límite. El parámetro
        # está porque lo exige el contrato de `EngineProvider`, y acepta cualquier
        # cosa precisamente porque este motor no lee nada de ahí.
        return UniswapV4Engine()


PROVIDER: Final = UniswapV4Provider()
