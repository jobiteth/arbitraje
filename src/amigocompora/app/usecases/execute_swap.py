"""Ejecutar un swap: firmar y emitir. La operación irreversible.

Capacidad requerida: `BROADCAST_TX`, que **sólo** concede el modo `EJECUCIÓN`.
Es el único camino de la aplicación por el que sale dinero de una cartera, y todo
lo que hay aquí está ordenado por esa consecuencia.

### El orden no es casual

1. **El modo, antes que nada.** En `OBSERVACIÓN`, `SIMULACIÓN` o `ASISTIDO` esto
   lanza en la primera línea y no se gasta ni una petición de red. La barrera no
   se comprueba al final «por si acaso»: se comprueba antes de trabajar.
2. **La red.** Fuera de EVM no se firma —ver `require_evm`— y es mejor saberlo
   antes de construir nada.
3. **Los límites.** Importe, red, tokens y motor, contra el registro de
   ejecuciones. Un límite que sólo comprueba la vista no es un límite.
4. **El payload.** Del mismo motor que observó la cotización, y sin firmar.
5. **El destino.** Contrastado contra la tabla medida que declara el motor. Un
   payload cuyo `to` no sea uno de sus routers no se firma.
6. **El coste de red**, estimado con los permisos ya concedidos —una estimación
   sin allowance revierte, y lo que se enseña tiene que ser lo que pasará—.
7. **La aprobación de ERC-20**, si hace falta, como operación propia.
8. **El permiso**: el «sí» del usuario, o la política de autonomía si está
   armada y la operación cabe.
9. Firmar, emitir y anotar.

### Dos barreras, no una

El destino se contrasta en el paso 5 y **otra vez** al emitir, dentro de
`EvmBroadcaster.send(expected_to=...)`. No es desconfianza del propio código: son
dos momentos distintos y entre ellos el motor pide red. Lo que se firma tiene que
seguir siendo lo que se comprobó.

### El progreso se narra, y narrarlo no puede romper nada

`__call__` acepta un observador opcional —`on_step`— que recibe qué fase empieza
y cómo termina cada una, incluida cada transacción real con su hash. Existe para
que la interfaz pueda contar la operación en vivo en vez de quedarse muda hasta
el final. Las llamadas al observador van envueltas: un fallo pintando un aviso
**no** puede tumbar una transacción que ya está en marcha, así que se anota y se
sigue.

### La clave privada se pide al final y se suelta

No vive en este objeto ni en el `Container`: se pide al almacén en el momento de
firmar y la variable local se suelta sola. Lo que sí se guarda es la
**dirección**, que es pública y es lo que la interfaz muestra para que el usuario
sepa de qué cartera sale el dinero.

Eso obliga a un detalle que se ve raro y es deliberado: la aprobación de ERC-20
necesita firmar antes que el swap, así que la clave se pide dos veces. Pedirla
dos veces es más barato que tenerla viva durante toda la operación.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.execution_policy import AutonomyPolicy, PrivateKeySource
from amigocompora.app.usecases.estimate_cost import NetworkCost
from amigocompora.app.usecases.prepare_swap import PrepareSwap, destination_for
from amigocompora.app.usecases.value_in_reference import ReferenceValuation
from amigocompora.domain.addresses import shorten
from amigocompora.domain.chains import ChainSpec, chain
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
    ExecutionError,
    InsufficientBalanceError,
    UnsupportedOperationError,
)
from amigocompora.domain.models import (
    BroadcastReceipt,
    BroadcastStatus,
    ExecutionIntent,
    Quote,
    Token,
    TokenApproval,
    UnsignedTransaction,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.engines.catalog import quote_token
from amigocompora.infra.evm.broadcast import (
    EvmBroadcaster,
    build_approve_calldata,
    build_permit2_approve_calldata,
    require_evm,
)
from amigocompora.infra.evm.dispatch import sign_and_send
from amigocompora.infra.evm.signer import address_from_key

_log = structlog.get_logger(__name__)

#: Calldata vacía. Un swap siempre llama a una función del router; una
#: transacción sin datos a un router es una transferencia a ciegas, y el dinero
#: que llega ahí no se puede recuperar.
_EMPTY_CALLDATA = frozenset({"", "0x"})

#: Cuánto vale la autorización que se le da a Permit2. Es una autorización
#: **con importe y con fecha**, y las dos acotan: el importe es el de esta
#: operación —Permit2 lo descuenta al usarlo, así que el swap la consume entera y
#: no queda nada vivo— y la fecha cubre el caso en que el swap no llegue a
#: firmarse. Treinta días es lo que usa el propio frontend de Uniswap, y aquí el
#: importe ya es lo que de verdad limita.
PERMIT2_EXPIRATION_SECONDS = 30 * 24 * 60 * 60


class SwapStep(StrEnum):
    """Las fases de una ejecución, en el orden en que ocurren.

    Las cinco existen para poder narrarlas todas, pero sólo tres son
    transacciones reales —`APPROVE`, `APPROVE_PERMIT2` y `SWAP`—: las otras dos
    son narración para que el usuario no crea que la aplicación se quedó muda.
    """

    PREPARE = "prepare"
    APPROVE = "approve"
    APPROVE_PERMIT2 = "approve_permit2"
    ESTIMATE = "estimate"
    SWAP = "swap"


class StepState(StrEnum):
    """En qué punto está un paso, o cómo terminó."""

    RUNNING = "running"
    DONE = "done"
    #: El usuario lo rechazó en su diálogo: no es un fallo, es una decisión.
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StepUpdate:
    """Un aviso de progreso, para que la interfaz lo pinte mientras ocurre.

    `title` es lo que está pasando ahora —en presente si corre, en resultado si
    terminó—; `detail` añade el motivo cuando algo sale mal o la cifra cuando la
    hay. `tx_hash` sólo viaja en los pasos que son una transacción real, que son
    los únicos que se pueden enlazar en un explorador, y `cost`/`cost_error` sólo
    en el paso de la estimación, que es su desenlace.

    `cost_error` se separa de `detail` aunque suelan contar lo mismo: `detail`
    es texto para leer y `cost_error` es el dato con el que quien pinta decide
    —«se intentó y no salió» frente a «no se intentó»—, que es una distinción
    que el panel de la ruta ya sabe enseñar y no puede adivinar del texto.
    """

    step: SwapStep
    state: StepState
    title: str
    detail: str = ""
    tx_hash: str = ""
    cost: NetworkCost | None = None
    cost_error: str | None = None


def reference_notional(quote: Quote) -> TokenAmount | None:
    """La pata del par que está en la moneda de referencia, si alguna lo está.

    Es la decisión que `_intent` toma en su caso 1 —«el par toca la stablecoin,
    así que el importe del tope **es** esa pata, y se elige en vez de calcularse»—
    extraída aquí para que haya **una sola** regla.

    Existe porque la interfaz necesita aplicar los topes antes de encender el
    botón que firma, y no puede hacerlo copiando esta cuenta: una copia que se
    desviara dejaría el botón prometiendo una operación que la política rechaza,
    que es exactamente lo que ese botón no puede hacer. Con la regla compartida,
    lo que la interfaz dice y lo que el caso de uso hace no pueden separarse.

    Devuelve `None` cuando el par **no** toca la referencia. Ahí el importe hay
    que valorarlo contra los motores, y eso es una petición de red: el botón no
    puede permitírsela en cada repintado, así que se calla. Callar es lo correcto
    en ese caso —la valoración puede cambiar entre el repintado y la firma, así
    que un «sí» del botón tampoco sería una promesa— pero conviene saber que para
    esos pares el botón sólo dice lo que sabe sin red.
    """
    reference = quote_token(quote.pair.chain)
    if reference is None:
        return None
    for amount in (quote.amount_in, quote.amount_out):
        if amount.symbol == reference.symbol:
            return amount
    return None


@dataclass(frozen=True, slots=True)
class ExecuteSwap:
    """Firma y emite el swap que describe una cotización ya observada."""

    prepare: PrepareSwap
    gateway: ConfirmationGateway
    keys: PrivateKeySource
    policy: AutonomyPolicy
    #: Un emisor por red. Se recibe como mapa y no como registro para que este
    #: caso de uso no dependa de `infra.rpc` — sólo de `infra.evm`, que es donde
    #: vive el contrato de emitir.
    broadcasters: Mapping[str, EvmBroadcaster] = field(default_factory=dict)
    clock: Clock | None = None
    #: Con qué se valora lo entregado cuando el par **no** toca la moneda de
    #: referencia. Sin esto, esos pares se rechazan —que era el comportamiento
    #: anterior— y por eso puede faltar: es una capacidad, no un requisito para
    #: operar los pares que sí la tocan.
    valuation: ReferenceValuation | None = None

    async def __call__(
        self,
        quote: Quote,
        *,
        recipient: str,
        on_step: Callable[[StepUpdate], None] | None = None,
    ) -> BroadcastReceipt:
        spec = chain(quote.pair.chain)

        # 1. El modo primero: antes de gastar red, y antes de pedir la clave.
        self.gateway.precheck(Capability.PREPARE_TX)
        self.gateway.precheck(Capability.BROADCAST_TX)

        # 2. Esta entrega firma en EVM y sólo en EVM.
        require_evm(spec)

        # 3. Los topes, contra lo ya ejecutado en las últimas 24 h. El importe que
        #    se mide es el que la cotización ya trae cuando el par toca la moneda
        #    de referencia; si no la toca, aquí se valora —y si no se puede
        #    valorar, esto lanza antes de construir nada.
        intent = await self._intent(quote, recipient)
        self.policy.check_intent(intent)

        broadcaster = self._broadcaster(quote.pair.chain)

        # El destino de la **llamada** —el router— y el de los **fondos** —la
        # cartera— son dos direcciones distintas, y confundirlas tiene las dos
        # consecuencias malas a la vez: se aprueba a quien no debe y se emite hacia
        # donde no toca. Por eso se llaman distinto en todo el fichero, `router` y
        # `wallet`, y no las dos «destination».
        wallet = destination_for(quote, recipient)

        # 4. Y 5. El payload, del motor que cotizó, y el router que declara.
        self._announce(on_step, SwapStep.PREPARE, StepState.RUNNING, "Preparando el swap…")
        try:
            transaction, router = await self._build_checked(quote, recipient)
        except Exception as error:
            self._announce(
                on_step,
                SwapStep.PREPARE,
                StepState.FAILED,
                "Preparación del swap",
                detail=str(error),
            )
            raise
        self._announce(on_step, SwapStep.PREPARE, StepState.DONE, "Swap preparado")

        # 5b. El saldo que este payload va a mover, antes de gastar gas en
        #     permisos: aprobar un permiso para un swap que la cartera no puede
        #     pagar es una transacción entera para nada, y el nodo, al estimar,
        #     contesta un «execution reverted: STF» que no dice a nadie qué
        #     pasa. Ver `_require_sold_balance`.
        await self._require_sold_balance(quote, transaction, broadcaster)

        # 6. La aprobación, si el token entregado la necesita. Puede volver con
        #    un payload nuevo: entre aprobar y firmar pasa una transacción.
        transaction, router = await self._ensure_allowance(
            quote, transaction, recipient, router, broadcaster, on_step
        )

        # 6b. El coste de red, ya con los permisos concedidos: sin ellos la
        #     estimación revertiría y enseñaría un fallo del allowance como si
        #     fuera un problema del swap. Entra en el diálogo del último «sí»
        #     porque es parte de lo que se decide.
        cost, cost_error = await self._estimate_step(on_step, quote, transaction, wallet)

        # 7. El permiso. Con la operación entera delante, o por la política.
        await self._authorize(
            quote, transaction, router, wallet, intent, on_step, cost=cost, cost_error=cost_error
        )

        # 8. Firmar, emitir, anotar. El destino esperado es el **router** que el
        #    motor declara en su tabla medida, que es un dato independiente del
        #    `to` del payload: contrastarlos es lo que detecta que el payload se
        #    desvió.
        swap_title = f"Swap en {quote.venue.name}"
        self._announce(on_step, SwapStep.SWAP, StepState.RUNNING, "Firmando y emitiendo…")
        try:
            receipt = await self._sign_and_send(transaction, broadcaster, expected_to=router)
        except Exception as error:
            self._announce(
                on_step, SwapStep.SWAP, StepState.FAILED, swap_title, detail=str(error)
            )
            raise
        self.policy.ledger.record_receipt(receipt, intent)
        if receipt.status is BroadcastStatus.SUCCESS:
            self._announce(
                on_step, SwapStep.SWAP, StepState.DONE, swap_title, tx_hash=receipt.tx_hash
            )
        else:
            # La transacción salió y minó en rojo: el hash existe y lleva al
            # explorador, donde se puede ver qué pasó. No se lanza porque el
            # recibo es la respuesta —el gas ya se pagó— y quien llama decide
            # qué hacer con él.
            self._announce(
                on_step,
                SwapStep.SWAP,
                StepState.FAILED,
                swap_title,
                detail=f"la transacción no llegó a buen término ({receipt.status.value})",
                tx_hash=receipt.tx_hash,
            )
        _log.info(
            "execution.recorded",
            chain=quote.pair.chain,
            pair=quote.pair.symbol,
            tx_hash=receipt.tx_hash,
            status=receipt.status.value,
        )
        return receipt

    async def _estimate_step(
        self,
        on_step: Callable[[StepUpdate], None] | None,
        quote: Quote,
        transaction: UnsignedTransaction,
        wallet: str,
    ) -> tuple[NetworkCost | None, str | None]:
        """Narra la estimación del coste y devuelve su resultado.

        La cuenta vive en `PrepareSwap.estimate_cost`, que **nunca lanza**: un
        fallo de la red se cuenta como motivo, no como cero, y no frustra la
        ejecución. Aquí sólo se le pone la narración alrededor.
        """
        self._announce(on_step, SwapStep.ESTIMATE, StepState.RUNNING, "Estimando el coste de red…")
        cost, cost_error = await self.prepare.estimate_cost(transaction, quote, wallet)
        if cost is not None:
            self._announce(
                on_step,
                SwapStep.ESTIMATE,
                StepState.DONE,
                "Coste de red",
                detail=f"≈ {cost.native} (estimación de gas)",
                cost=cost,
            )
        else:
            # Dos desenlaces sin cifra, y no son el mismo: se intentó y la red
            # dijo que no, o no se intentó —sin estimador, sin cartera desde la
            # que medir, o una transacción de Solana, que no tiene
            # `eth_estimateGas`—. El segundo se pinta con raya, así que aquí no
            # puede llevar el texto del primero.
            self._announce(
                on_step,
                SwapStep.ESTIMATE,
                StepState.DONE,
                "Coste de red",
                detail=(
                    f"sin cifra: {cost_error}" if cost_error is not None else "no se intentó"
                ),
                cost_error=cost_error,
            )
        return cost, cost_error

    def _announce(
        self,
        on_step: Callable[[StepUpdate], None] | None,
        step: SwapStep,
        state: StepState,
        title: str,
        *,
        detail: str = "",
        tx_hash: str = "",
        cost: NetworkCost | None = None,
        cost_error: str | None = None,
    ) -> None:
        """Cuenta en qué punto va la operación. **Nunca puede romperla.**

        El observador es la interfaz, y un fallo suyo no puede tumbar una
        transacción que ya está en marcha: si el aviso falla, se anota en el
        registro y se sigue. No es defensa contra la UI de este repositorio, es
        que este caso de uso mueve dinero y su curso no puede depender de que
        alguien pinte bien un botón.
        """
        if on_step is None:
            return
        update = StepUpdate(
            step=step,
            state=state,
            title=title,
            detail=detail,
            tx_hash=tx_hash,
            cost=cost,
            cost_error=cost_error,
        )
        try:
            on_step(update)
        except Exception:
            _log.warning(
                "execution.step_observer_failed",
                step=step.value,
                state=state.value,
            )

    # --------------------------------------------------------------- pasos  #
    async def _intent(self, quote: Quote, recipient: str) -> ExecutionIntent:
        """Lo que se propone ejecutar, con el importe **en la unidad del tope**.

        Hay dos casos y el orden importa:

        1. **El par toca la moneda de referencia.** Entonces esa pata del par
           *es* el importe del tope, y se **elige de las dos** en vez de
           calcularse: la que coincida es la que de verdad se movió, así que el
           tope se mide contra un hecho y no contra una estimación. Es el caso de
           siempre y no cuesta ni una petición de red.
        2. **No la toca.** El importe entregado está en otra unidad —ETH contra un
           token, o dos tokens entre sí— y compararlo tal cual contra un tope
           escrito en la stablecoin sería comparar peras con manzanas. Se valora
           lo entregado contra la referencia con los mismos motores que cotizaron
           el par, y **si no se puede valorar no se ejecuta**: un tope que se
           salta cuando no se puede medir no es un tope.
        """
        delivered = quote.amount_in
        reference = quote_token(quote.pair.chain)
        if reference is not None:
            # La misma función que usa la interfaz para saber si el botón puede
            # encenderse. Si aquí se decidiera de otra forma, el botón diría una
            # cosa y esto otra.
            measured = reference_notional(quote)
            if measured is not None:
                return ExecutionIntent(
                    quote=quote,
                    recipient=recipient,
                    notional=measured,
                    engine_id=quote.engine_id,
                )
            return await self._valued_intent(quote, recipient, delivered, reference)

        # Sin stablecoin de referencia elegida para esa red no hay unidad en la
        # que medir nada: se cae al camino de siempre —una pata del par—, que al
        # menos es un hecho comparable consigo mismo.
        return ExecutionIntent(
            quote=quote,
            recipient=recipient,
            notional=delivered,
            engine_id=quote.engine_id,
        )

    async def _valued_intent(
        self,
        quote: Quote,
        recipient: str,
        delivered: TokenAmount,
        reference: Token,
    ) -> ExecutionIntent:
        """Valora lo entregado y devuelve el intento, o se niega a ejecutar."""
        spent = quote.pair.base
        if self.valuation is None:
            raise ExecutionError(
                f"el par {quote.pair.symbol} no toca la moneda de referencia de "
                f"{quote.pair.chain} ({reference.symbol}), y esta ejecución no tiene "
                f"con qué valorar {delivered} en ella. Ejecutar sin poder aplicar el "
                f"tope sería operar sin límite."
            )
        value = await self.valuation(spent, delivered, reference)
        if value is None:
            raise ExecutionError(self.valuation.describe_failure(spent, reference))
        _log.info(
            "execution.valued_in_reference",
            pair=quote.pair.symbol,
            chain=quote.pair.chain,
            delivered=str(delivered),
            reference=reference.symbol,
            value=f"{value.as_decimal():f}",
        )
        return ExecutionIntent(
            quote=quote,
            recipient=recipient,
            notional=delivered,
            engine_id=quote.engine_id,
            reference_value=value,
        )

    def _now(self) -> int:
        """El segundo Unix actual, según el reloj inyectado.

        La caducidad de un permiso de Permit2 se mide contra el reloj de la
        cadena, no contra el del proceso, y aquí se usa el de la aplicación para
        decidir si hace falta volver a aprobar. Si el reloj local fuera por detrás
        del de la red, el permiso parecería vigente un rato más de lo que está; la
        comprobación que de verdad decide es la que hace el contrato al cobrar.
        """
        return int((self.clock or SystemClock()).now().timestamp())

    async def _build_checked(
        self, quote: Quote, recipient: str
    ) -> tuple[UnsignedTransaction, str]:
        """Construye el payload y comprueba que va donde el motor dice que va.

        Devuelve también el router declarado, y no sólo el payload: ese valor es
        el que hay que contrastar en la emisión y el que hay que autorizar en un
        `approve`, y recalcularlo fuera con otra llamada al motor daría una segunda
        respuesta que podría no ser la misma que se acaba de comprobar.
        """
        transaction = await self.prepare.build(quote, recipient=recipient)
        if not isinstance(transaction, UnsignedTransaction):
            # `require_evm` ya lo descartó, pero el tipo es una unión y esto es
            # además lo que permite a mypy saber que a partir de aquí hay
            # `to_address`, `chain_id` y `calldata`.
            raise UnsupportedOperationError(
                f"«{quote.pair.chain}» no devolvió un payload EVM sino una "
                f"transacción de otra forma: no se firma."
            )

        if transaction.calldata.strip().lower() in _EMPTY_CALLDATA:
            raise ExecutionError(
                f"el payload para {quote.pair.symbol} no lleva ninguna llamada: "
                f"sería una transferencia a ciegas a {shorten(transaction.to_address)}, "
                f"sin nada que ejecute el swap. El dinero que llegue ahí no se puede "
                f"recuperar, así que no se firma."
            )

        declared = self.prepare.planner_for(quote).expected_destination(quote.pair.chain)
        if declared is None:
            raise ExecutionError(
                f"el motor «{quote.engine_id}» no declara a qué contrato dirige los "
                f"swaps en «{quote.pair.chain}», así que no hay contra qué comprobar "
                f"el destino del payload y no se puede firmar. Un motor que construye "
                f"lo que se firma tiene que poder decir a dónde va."
            )
        if declared.lower() != transaction.to_address.lower():
            raise ExecutionError(
                f"el payload dirige el swap a {transaction.to_address}, y el motor "
                f"«{quote.engine_id}» declara {declared} para «{quote.pair.chain}». "
                f"No se firma: el destino no es el que el motor dice usar."
            )

        return transaction, declared

    async def _require_sold_balance(
        self,
        quote: Quote,
        transaction: UnsignedTransaction,
        broadcaster: EvmBroadcaster,
    ) -> None:
        """Corta si la cartera no tiene el token que el payload va a mover.

        ### Qué se mira, y por qué es el mismo criterio que el de aprobar

        Quién paga lo decide la **misma** función que decide a quién se aprueba
        (`_token_to_approve`): si devuelve una dirección, ese ERC-20 sale de la
        cartera con `transferFrom` y se mide su saldo; si devuelve `None` —el
        nativo, o el envoltorio que el router envuelve él—, lo que viaja es el
        nativo del propio `value`. Así se mira siempre el saldo del token que de
        verdad se mueve, no el que el par nombra.

        ### Por qué va aquí, y no más tarde

        Después de construir —hace falta el payload para saber qué se mueve— pero
        **antes** de aprobar nada: un permiso para un swap que la cartera no
        puede pagar es una transacción entera de gas para nada. Y es la única
        comprobación que faltaba: la cotización no mira saldos (cotiza para un
        centinela) y la aprobación tampoco (es un permiso, no un gasto), así que
        el déficit se descubría al estimar, como un `execution reverted: STF`
        del nodo —medido el 2026-10-10, Polygon: 0.550579 pUSD en la cartera
        intentando mover 1 pUSD—, que no dice ni cuánto hay ni cuánto falta.

        El mensaje lleva las dos cifras tal como se ven en la interfaz, porque
        la salida —reducir el importe o traer saldo— la decide el usuario.
        """
        spec = chain(quote.pair.chain)
        sold = quote.pair.base
        owner = address_from_key(self.keys.require())
        moved = _token_to_approve(transaction, sold, spec)
        if moved is None:
            available = TokenAmount(
                await broadcaster.native_balance(owner),
                spec.native_decimals,
                spec.native_symbol,
            )
            required = TokenAmount(
                transaction.value.raw, spec.native_decimals, spec.native_symbol
            )
        else:
            available = TokenAmount(
                await broadcaster.token_balance(moved, owner), sold.decimals, sold.symbol
            )
            required = quote.amount_in
        if available.raw >= required.raw:
            return
        _log.info(
            "execution.insufficient_balance",
            pair=quote.pair.symbol,
            token=sold.symbol,
            available_raw=available.raw,
            required_raw=required.raw,
        )
        raise InsufficientBalanceError(available=str(available), required=str(required))

    # ---------------------------------------------------- aprobación ERC-20  #
    async def _ensure_allowance(
        self,
        quote: Quote,
        transaction: UnsignedTransaction,
        recipient: str,
        router: str,
        broadcaster: EvmBroadcaster,
        on_step: Callable[[StepUpdate], None] | None = None,
    ) -> tuple[UnsignedTransaction, str]:
        """Deja los permisos del token listos, y devuelve el payload a firmar.

        Devuelve el payload porque aprobar **caduca el anterior**: entre la
        aprobación y el swap se emite y se confirma una transacción entera, y el
        payload del motor lleva dentro un importe mínimo y una fecha de validez.
        Firmar el viejo sería ejecutar contra un precio que ya no está.

        ### Cuántos permisos hacen falta lo dice el payload, no una tabla

        «Aprobar al router» dejó de ser la respuesta completa. El router V2 y el
        SwapRouter V1 de V3 mueven el token ellos mismos con `transferFrom`, y
        con autorizarles el ERC-20 basta. Pero el SwapRouter02 —**el único router
        V3 que existe en Base**— no lo toca: se lo pide a Permit2, y Permit2 sólo
        lo entrega si el dueño se lo ha autorizado a él. Son dos permisos
        encadenados y ninguno sustituye al otro:

            ERC-20  ---approve-->  Permit2  ---approve-->  router

        Quién es el gastador de cada uno lo declara el motor en el propio payload
        (`TokenApproval`), porque es él quien acaba de leer el `factory()` y el
        `WETH9()` de ese router y sabe cómo cobra en esa red. Deducirlo aquí
        exigiría una tabla de casos por red metida en el camino que firma, que es
        justo donde menos conviene tener una.

        Con la aprobación **directa** nada cambia respecto a antes: el payload no
        declara nada, se resuelve al camino de siempre y el permiso va al router.
        """
        sold = quote.pair.base
        # Una sola función decide **y** devuelve la dirección, en vez de decidir
        # aquí y sacar la dirección después con un `assert`: así no hay dos
        # respuestas que puedan separarse, y no hace falta una aserción —que
        # además desaparece con `-O`— para convencer al comprobador de tipos.
        address = _token_to_approve(transaction, sold, chain(quote.pair.chain))
        if address is None:
            return transaction, router

        approval = transaction.token_approval
        owner = address_from_key(self.keys.require())
        approved = False

        # 1. El permiso del ERC-20, a quien vaya a hacer el `transferFrom`: el
        #    router en el camino directo, Permit2 en el encadenado.
        granted = await broadcaster.read_allowance(address, owner, approval.target)
        if granted < quote.amount_in.raw:
            await self._approve(
                address,
                sold,
                quote,
                approval.target,
                owner,
                broadcaster,
                chained=approval.is_chained,
                on_step=on_step,
            )
            approved = True

        # 2. Y, si el router cobra por Permit2, el permiso de Permit2 al router.
        #    Se comprueba aunque el anterior se acabe de conceder: son dos
        #    permisos distintos sobre contratos distintos, y tener uno no dice
        #    nada del otro.
        if approval.is_chained and await self._grant_permit2(
            approval, address, sold, quote, owner, broadcaster, on_step=on_step
        ):
            approved = True

        if not approved:
            return transaction, router
        # El payload viejo ya no sirve: se vuelve a construir con los permisos ya
        # concedidos, y así el importe mínimo y la validez son de ahora.
        return await self._build_checked(quote, recipient)

    async def _grant_permit2(
        self,
        approval: TokenApproval,
        token_address: str,
        token: Token,
        quote: Quote,
        owner: str,
        broadcaster: EvmBroadcaster,
        *,
        on_step: Callable[[StepUpdate], None] | None = None,
    ) -> bool:
        """Concede a Permit2 permiso para entregar el token al router.

        Devuelve si hubo que concederlo, que es lo que decide si el payload hay
        que reconstruirlo.
        """
        # `is_chained` implica que hay `via`, pero el tipo no lo sabe: se
        # comprueba aquí para no arrastrar un `assert` por todo el método.
        permit2 = approval.via
        if permit2 is None:
            return False

        allowed = await broadcaster.read_permit2_allowance(
            permit2, token_address, owner, approval.spender
        )
        amount_raw = quote.amount_in.raw
        now = self._now()
        if allowed.covers(amount_raw, now=now):
            return False

        spec = chain(quote.pair.chain)
        expiration = now + PERMIT2_EXPIRATION_SECONDS
        _log.info(
            "execution.permit2_required",
            chain=quote.pair.chain,
            token=token.symbol,
            permit2=permit2,
            spender=approval.spender,
            granted_raw=allowed.amount_raw,
            expired=allowed.expiration < now,
            needed=amount_raw,
        )
        grant = UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=permit2,
            calldata=build_permit2_approve_calldata(
                token_address, approval.spender, amount_raw, expiration
            ),
            value=TokenAmount.zero(spec.native_decimals, spec.native_symbol),
            description=(
                f"Autorizar a Permit2 a entregar {quote.amount_in} de tu "
                f"{token.symbol} al router {shorten(approval.spender)}"
            ),
        )
        title = f"Permiso de {token.symbol} en Permit2 (2 de 2)"
        self._announce(
            on_step,
            SwapStep.APPROVE_PERMIT2,
            StepState.RUNNING,
            f"Aprobando {token.symbol} en Permit2 (2 de 2)…",
        )
        try:
            await self.gateway.authorize(
                Capability.BROADCAST_TX,
                f"Aprobar {token.symbol} en Permit2 para el swap",
                details=(
                    f"Token: {token.symbol}",
                    f"Importe autorizado: {quote.amount_in} (el justo, no ilimitado)",
                    f"Cobrarlo puede: {shorten(approval.spender)} (el router)",
                    f"Vía: Permit2 ({shorten(permit2)})",
                    f"Caduca: en {PERMIT2_EXPIRATION_SECONDS // 86_400} días",
                    f"Desde la cartera: {shorten(owner)}",
                    "Es la segunda de dos aprobaciones: el router no mueve el token "
                    "él mismo en esta red.",
                    "Es una transacción real: cuesta gas y no se puede deshacer.",
                ),
                transaction=grant,
            )
        except ConfirmationDeniedError:
            self._announce(
                on_step,
                SwapStep.APPROVE_PERMIT2,
                StepState.REJECTED,
                title,
                detail="lo rechazaste en el diálogo: no se firmó nada",
            )
            raise
        except Exception as error:
            self._announce(
                on_step, SwapStep.APPROVE_PERMIT2, StepState.FAILED, title, detail=str(error)
            )
            raise
        try:
            receipt = await self._sign_and_send(grant, broadcaster, expected_to=permit2)
        except Exception as error:
            self._announce(
                on_step, SwapStep.APPROVE_PERMIT2, StepState.FAILED, title, detail=str(error)
            )
            raise
        # Se anota **antes** de mirar el estado, igual que la aprobación del
        # ERC-20: la transacción salió, se pagó su gas y el permiso existe en la
        # cadena aunque haya minado en rojo. Un asiento que sólo se escribe
        # cuando todo va bien es un asiento que miente justo cuando hace falta.
        #
        # Y anotarla no es cosmético: sin esto, el registro de una venta por
        # Permit2 —que es el único camino que hay en Base— sólo contaba una de
        # las dos transacciones que la hacen posible, y la que faltaba es la que
        # deja al router autorizado sobre el token.
        self.policy.ledger.record_approval(
            receipt,
            pair=quote.pair.symbol,
            engine_id=quote.engine_id,
            token_symbol=token.symbol,
            # El destinatario es el **router**, no Permit2: es a quien este
            # permiso le da poder sobre el token, y es lo que distingue este
            # asiento del anterior. `via` deja dicho por dónde pasa.
            spender=approval.spender,
            granted_raw=amount_raw,
            via=permit2,
        )
        if receipt.status is not BroadcastStatus.SUCCESS:
            self._announce(
                on_step,
                SwapStep.APPROVE_PERMIT2,
                StepState.FAILED,
                title,
                detail=f"la transacción no llegó a buen término ({receipt.status.value})",
                tx_hash=receipt.tx_hash,
            )
            raise ExecutionError(
                f"la aprobación de {token.symbol} en Permit2 no llegó a buen "
                f"término ({receipt.status.value}, {receipt.tx_hash}). Sin ese "
                f"permiso el router no puede cobrar el token y el swap revertiría, "
                f"así que no se firma."
            )
        self._announce(
            on_step, SwapStep.APPROVE_PERMIT2, StepState.DONE, title, tx_hash=receipt.tx_hash
        )
        # Se relee, por la misma razón que en la aprobación del ERC-20: la
        # transacción pudo minar bien y el permiso no ser el que se pidió.
        after = await broadcaster.read_permit2_allowance(
            permit2, token_address, owner, approval.spender
        )
        if not after.covers(amount_raw, now=now):
            raise ExecutionError(
                f"tras aprobar en Permit2, el permiso sobre {token.symbol} sigue "
                f"siendo insuficiente ({after.amount_raw} de {amount_raw}, caduca "
                f"en {after.expiration}). El swap revertiría al cobrar."
            )
        return True

    async def _approve(
        self,
        token_address: str,
        token: Token,
        quote: Quote,
        spender: str,
        owner: str,
        broadcaster: EvmBroadcaster,
        *,
        chained: bool,
        on_step: Callable[[StepUpdate], None] | None = None,
    ) -> BroadcastReceipt:
        """Aprueba el importe **exacto** que hace falta, como operación propia.

        Nunca un importe ilimitado. Un `approve` sin límite deja al router
        autorizado a vaciar ese token para siempre, y es el patrón que convierte
        un router comprometido en una pérdida total. El precio de aprobar lo justo
        es una transacción de más la primera vez de cada par, y es el precio
        correcto.

        `chained` sólo cambia lo que se le **cuenta al usuario**, que es lo que
        decide: cuando el gastador es Permit2, esta aprobación no autoriza a nadie
        a mover el token todavía —hace falta la segunda— y presentarla como «el
        router podrá gastarlo» sería describir un permiso que no existe.
        """
        spec = chain(quote.pair.chain)
        approval = UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=token_address,
            calldata=build_approve_calldata(spender, quote.amount_in.raw),
            value=TokenAmount.zero(spec.native_decimals, spec.native_symbol),
            description=(
                f"Autorizar a {shorten(spender)} a gastar {quote.amount_in} "
                f"de tu {token.symbol}"
            ),
        )
        numbered = " (1 de 2)" if chained else ""
        title = f"Permiso de {token.symbol}{numbered}"
        self._announce(
            on_step, SwapStep.APPROVE, StepState.RUNNING, f"Aprobando {token.symbol}{numbered}…"
        )
        try:
            await self.gateway.authorize(
                Capability.BROADCAST_TX,
                f"Aprobar {token.symbol} para el swap",
                details=(
                    f"Token: {token.symbol}",
                    f"Importe autorizado: {quote.amount_in} (el justo, no ilimitado)",
                    (
                        f"Se autoriza a: Permit2 ({shorten(spender)}), que es quien "
                        f"entrega el token al router"
                        if chained
                        else f"Autorizado a gastarlo: {shorten(spender)}"
                    ),
                    f"Desde la cartera: {shorten(owner)}",
                    (
                        "Es la primera de dos aprobaciones: con ésta sola el router "
                        "todavía no puede cobrar el token."
                        if chained
                        else ""
                    ),
                    "Es una transacción real: cuesta gas y no se puede deshacer.",
                ),
                transaction=approval,
            )
        except ConfirmationDeniedError:
            self._announce(
                on_step,
                SwapStep.APPROVE,
                StepState.REJECTED,
                title,
                detail="lo rechazaste en el diálogo: no se firmó nada",
            )
            raise
        except Exception as error:
            self._announce(on_step, SwapStep.APPROVE, StepState.FAILED, title, detail=str(error))
            raise
        try:
            receipt = await self._sign_and_send(
                approval, broadcaster, expected_to=token_address
            )
        except Exception as error:
            self._announce(on_step, SwapStep.APPROVE, StepState.FAILED, title, detail=str(error))
            raise
        self.policy.ledger.record_approval(
            receipt,
            pair=quote.pair.symbol,
            engine_id=quote.engine_id,
            token_symbol=token.symbol,
            spender=spender,
            granted_raw=quote.amount_in.raw,
        )
        if receipt.status is not BroadcastStatus.SUCCESS:
            self._announce(
                on_step,
                SwapStep.APPROVE,
                StepState.FAILED,
                title,
                detail=f"la transacción no llegó a buen término ({receipt.status.value})",
                tx_hash=receipt.tx_hash,
            )
            raise ExecutionError(
                f"la aprobación de {token.symbol} no llegó a buen término "
                f"({receipt.status.value}, {receipt.tx_hash}). Sin ese permiso el "
                f"swap revertiría, así que no se firma."
            )
        self._announce(on_step, SwapStep.APPROVE, StepState.DONE, title, tx_hash=receipt.tx_hash)
        # Se vuelve a leer en vez de darlo por hecho: la aprobación pudo minar con
        # éxito y aun así no ser suficiente si el token cobra comisión al mover.
        after = await broadcaster.read_allowance(token_address, owner, spender)
        if after < quote.amount_in.raw:
            raise ExecutionError(
                f"tras aprobar, el permiso sobre {token.symbol} sigue siendo "
                f"insuficiente ({after} de {quote.amount_in.raw}). Algunos tokens "
                f"ajustan el importe al transferirlo; el swap revertiría."
            )
        return receipt

    # ---------------------------------------------------------------- firma  #
    async def _authorize(
        self,
        quote: Quote,
        transaction: UnsignedTransaction,
        router: str,
        wallet: str,
        intent: ExecutionIntent,
        on_step: Callable[[StepUpdate], None] | None = None,
        *,
        cost: NetworkCost | None = None,
        cost_error: str | None = None,
    ) -> None:
        """El permiso para emitir, con todo delante.

        La política de autonomía, si está armada, se salta esta pregunta —nunca la
        barrera de modo, que ya pasó al principio— y deja su propio asiento en la
        traza con el motivo. Se incluye el importe de referencia porque es la
        unidad contra la que se miden los topes: no basta con ver la operación,
        hay que ver contra qué se está midiendo.

        Se muestran **las dos** direcciones, y con nombres distintos: la del
        contrato al que se llama y la de la cartera que recibe. Son cosas
        diferentes y el usuario que confirma tiene que poder ver las dos, no una
        etiquetada como «destino» que valdría para cualquiera de las dos.

        El coste de red entra en los detalles porque es parte de lo que se decide:
        quien confirma una transacción real merece ver lo que cuesta emitirla. Si
        no se pudo estimar se dice eso, no un cero.
        """
        if cost is not None:
            cost_line = f"Coste de red: ≈ {cost.native} (estimación de gas)"
        elif cost_error is not None:
            cost_line = (
                "Coste de red: la red no pudo estimarlo ahora mismo; se "
                "reintenta al emitir."
            )
        else:
            cost_line = "Coste de red: sin estimador configurado."
        title = f"Swap en {quote.venue.name}"
        self._announce(on_step, SwapStep.SWAP, StepState.RUNNING, "Esperando tu confirmación…")
        try:
            await self.gateway.authorize(
                Capability.BROADCAST_TX,
                f"Emitir swap en {quote.venue.name}",
                details=(
                    f"Red: {quote.pair.chain}",
                    f"Par: {quote.pair.symbol}",
                    f"Entregas: {quote.amount_in}",
                    f"Recibes (estimado): {quote.amount_out}",
                    f"Importe de referencia: {intent.notional}",
                    f"Motor que construye: {quote.engine_id}",
                    f"Contrato que ejecuta: {shorten(router)}",
                    f"Fondos a tu cartera: {shorten(wallet)}",
                    f"Firma la cartera: {self.address() or 'sin clave configurada'}",
                    cost_line,
                    "Es una operación real e irreversible: se firma y se emite.",
                ),
                transaction=transaction,
            )
        except ConfirmationDeniedError:
            self._announce(
                on_step,
                SwapStep.SWAP,
                StepState.REJECTED,
                title,
                detail="lo rechazaste en el diálogo: no se firmó nada",
            )
            raise
        except Exception as error:
            self._announce(on_step, SwapStep.SWAP, StepState.FAILED, title, detail=str(error))
            raise

    async def _sign_and_send(
        self,
        unsigned: UnsignedTransaction,
        broadcaster: EvmBroadcaster,
        *,
        expected_to: str,
    ) -> BroadcastReceipt:
        """Reúne el estado de red, firma en local y emite.

        La clave se pide aquí, que es el último momento posible, y se suelta al
        salir de la función: no se guarda en el objeto ni se pasa a nadie más.
        La secuencia de red y firma vive en `infra.evm.dispatch`, compartida con
        los otros dos caminos que emiten: era la misma en los tres y tenerla
        copiada es lo que permite que una copia se desvíe sin que se note.
        """
        return await sign_and_send(
            unsigned,
            broadcaster,
            private_key=self.keys.require(),
            expected_to=expected_to,
        )

    # --------------------------------------------------------------- apoyo  #
    def address(self) -> str | None:
        """La cartera que firma, o `None` si no hay clave. **Nunca la clave.**

        Se deriva pidiendo la clave y soltándola aquí mismo: lo único que sale de
        esta función es la dirección, que es pública.
        """
        if not self.keys.available():
            return None
        return address_from_key(self.keys.require())

    def _broadcaster(self, chain_key: str) -> EvmBroadcaster:
        broadcaster = self.broadcasters.get(chain_key)
        if broadcaster is None:
            raise ExecutionError(
                f"no hay ningún nodo configurado para «{chain_key}»: sin él no se "
                f"puede leer el estado de la red (nonce, gas, comisiones) ni emitir "
                f"nada. Declara al menos un endpoint para esa red."
            )
        return broadcaster


def _token_to_approve(
    transaction: UnsignedTransaction,
    sold: Token,
    spec: ChainSpec,
) -> str | None:
    """La dirección del token que hay que aprobar, o `None` si no hay que aprobar.

    Devuelve la dirección y no un booleano a propósito: quien decide que hace
    falta un permiso es exactamente quien sabe sobre qué contrato pedirlo, así que
    separarlo en dos funciones dejaría abierta la posibilidad de que dijeran cosas
    distintas. Devolver `str | None` codifica las dos respuestas en una.

    Dos casos en los que no hay que aprobar:

    - **El nativo.** No se aprueba porque no hay `transferFrom` que autorizar: se
      entrega como `value` de la propia transacción, así que no hay contrato de
      token sobre el que escribir un permiso.
    - **El envoltorio pagado como nativo.** Muchos routers aceptan ETH y lo
      envuelven ellos; el payload entonces manda `value` y no toca el WETH del
      usuario. Aprobar ahí sería una transacción de más y, peor, una autorización
      que se queda concedida sin que nada llegue a usarla.

    Todo lo demás —cualquier ERC-20, y el envoltorio cuando el payload **no**
    manda valor— necesita permiso, porque el router lo moverá con `transferFrom`.
    """
    if sold.address is None:
        return None
    native = spec.wrapped_native
    if (
        native is not None
        and sold.address.lower() == native.lower()
        and transaction.value.raw > 0
    ):
        return None
    return sold.address
