"""Ejecutar un puente: firmar y emitir. La operación irreversible.

Capacidad requerida: `BROADCAST_TX`, que **sólo** concede el modo `EJECUCIÓN`.
Está calcado de `ExecuteSwap` y el orden es el mismo, por las mismas razones:

1. **El modo, antes que nada.** En `OBSERVACIÓN`, `SIMULACIÓN` o `ASISTIDO` esto
   lanza en la primera línea y no se gasta ni una petición de red.
2. **La red de origen.** Fuera de EVM no se firma, y es mejor saberlo antes.
3. **Los límites**, contra el registro de ejecuciones — y aquí contra **las dos
   redes**, no sólo la de origen. Ver `BridgeIntent`.
4. **El payload**, del mismo motor que observó la cotización, y sin firmar.
5. **El destino**, contrastado contra la tabla medida que declara el motor.
6. **La aprobación de ERC-20**, si el token entregado la necesita.
7. **El permiso**: el «sí» del usuario, o la política de autonomía si está armada.
8. Firmar, emitir y anotar.

### Lo que cambia respecto a un swap, y sólo eso

- **Dos redes.** El asiento se escribe en la red de **origen**, que es de donde
  sale el dinero, y la política comprueba las dos contra la lista blanca.
- **El importe medido.** En un swap el importe entregado es, casi siempre, una de
  las dos patas del par y ya está en la unidad del tope. En un puente la pata de
  origen es lo que se entrega y suele ser la stablecoin de la red de origen —que
  es exactamente la unidad en la que están escritos los topes—, así que el caso
  de siempre es más simple todavía. Cuando no lo sea, se valora igual que en los
  swaps y con el mismo valorador.
- **Ni Permit2 ni aprobación encadenada.** Los dos motores de puente declaran una
  aprobación directa: se lo pide al ERC-20 y punto. Si alguna vez llegara una
  encadenada, esto se niega en vez de conceder medio permiso — ver
  `_ensure_allowance`.

### El destinatario es la cartera, no el contrato

En un swap el payload va al router y los fondos acaban en la cartera del usuario;
en un puente pasa lo mismo, con la diferencia de que los fondos cruzan de red
antes de llegar. Por eso el destinatario se valida **antes** de construir —el
motor lo mete dentro del payload, en el calldata de origen— y el contraste de
destino se hace contra el contrato que el motor declara para la red de origen.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.execution_policy import AutonomyPolicy, PrivateKeySource
from amigocompora.app.usecases.prepare_bridge import PrepareBridge, recipient_for
from amigocompora.app.usecases.value_in_reference import ReferenceValuation
from amigocompora.domain.addresses import shorten
from amigocompora.domain.chains import ChainSpec, chain
from amigocompora.domain.clock import Clock
from amigocompora.domain.errors import ExecutionError, UnsupportedOperationError
from amigocompora.domain.models import (
    BridgeQuote,
    BridgeRequest,
    BroadcastReceipt,
    BroadcastStatus,
    Token,
    UnsignedTransaction,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.engines.catalog import quote_token
from amigocompora.infra.evm.broadcast import (
    EvmBroadcaster,
    build_approve_calldata,
    require_evm,
)
from amigocompora.infra.evm.dispatch import sign_and_send
from amigocompora.infra.evm.signer import address_from_key

_log = structlog.get_logger(__name__)


def bridge_notional(request: BridgeRequest) -> TokenAmount | None:
    """La pata entregada, si ya está en la moneda en la que se escriben los topes.

    Es la regla del caso 1 de `ExecuteBridge._intent` —«la pata de origen ya es la
    stablecoin de su red, así que el importe del tope **es** ese importe y se elige
    en vez de calcularse»— extraída aquí para que haya **una sola**.

    Existe porque la interfaz necesita aplicar los topes antes de encender el botón
    que firma, y no puede hacerlo copiando la comparación: una copia que se desviara
    dejaría el botón prometiendo un cruce que la política rechaza, que es
    exactamente lo que ese botón no puede hacer.

    Devuelve `None` cuando la pata de origen **no** es la referencia —o cuando la red
    no declara ninguna—. Ahí el importe hay que valorarlo cotizando, y eso es una
    petición de red: quien repinta la pantalla no puede permitírsela en cada
    repintado. Callar es lo correcto en ese caso —la valoración puede cambiar entre
    el repintado y la firma, así que un «sí» tampoco sería una promesa—, pero quien
    ejecuta **no** se calla: `ExecuteBridge` valora antes de cruzar y, si no puede,
    se niega. Ver `_valued_intent`.
    """
    reference = quote_token(request.origin.chain)
    if reference is None or request.amount_in.symbol != reference.symbol:
        return None
    return request.amount_in


@dataclass(frozen=True, slots=True)
class BridgeIntent:
    """Lo que se propone cruzar, en los términos que la política evalúa.

    Satisface `ExecutableIntent` —red, etiqueta, tokens, motor, importe— **y**
    `MultiChainIntent`, que es lo que hace que la lista blanca de redes cubra
    también aquella en la que el dinero acaba. Sin lo segundo, un puente desde
    una red permitida hacia una que no lo está pasaría la comprobación entera,
    porque la única red que se miraría es la de origen — y la de destino es
    precisamente donde el dinero va a acabar.

    No hereda de `ExecutionIntent` por una razón medida: `ExecutableIntent` es
    `runtime_checkable`, así que un miembro nuevo obligatorio ahí invalidaría de
    golpe a los tres intentos que ya existen. Son dos objetos distintos porque son
    dos operaciones distintas: un `ExecutionIntent` describe un par de una red, y
    esto describe dos patas de dos redes.

    `notional` es lo que **se entrega**, y por eso es la pata de origen: es un
    hecho, no una estimación. `reference_value` sólo aparece cuando esa pata no
    está ya en la unidad del tope.
    """

    quote: BridgeQuote
    recipient: str
    notional: TokenAmount
    engine_id: str
    reference_value: TokenAmount | None = None

    @property
    def chain(self) -> str:
        """La red de **origen**: de donde sale el dinero y donde se emite."""
        return self.quote.request.origin.chain

    @property
    def chains(self) -> tuple[str, ...]:
        """Las dos redes que la operación toca, en orden determinista.

        Incluye `chain`, y no sólo la de destino: quien lea esto no debería tener
        que saber que la de origen ya viene por otro lado. El orden no importa
        —se comprueban todas— pero se declara para que un mensaje de error sea
        reproducible.
        """
        return self.quote.request.chains

    @property
    def label(self) -> str:
        """Lo que se escribe en el registro: «USDC@base → USDC@polygon»."""
        return self.quote.pair_label

    @property
    def tokens(self) -> tuple[str, ...]:
        """Los símbolos que la operación mueve, sin repetir.

        Sin repetir importa: un puente de USDC a USDC toca el mismo símbolo en dos
        redes, y escribir «USDC, USDC» en la lista blanca de tokens sería contar
        dos veces el mismo permiso. La lista blanca es de símbolos, no de pares
        símbolo-red —es lo que hay— y no hay nada que ganar repitiéndolo.
        """
        request = self.quote.request
        return tuple(dict.fromkeys((request.origin.symbol, request.destination.symbol)))

    @property
    def measured(self) -> TokenAmount:
        """El importe que se mide contra los topes, **en la unidad del tope**."""
        return self.reference_value if self.reference_value is not None else self.notional

    @property
    def counts_towards_limits(self) -> bool:
        """Un puente entrega dinero y lo saca de la cartera: cuenta, siempre."""
        return True


@dataclass(frozen=True, slots=True)
class ExecuteBridge:
    """Firma y emite el puente que describe una cotización ya observada."""

    prepare: PrepareBridge
    gateway: ConfirmationGateway
    keys: PrivateKeySource
    policy: AutonomyPolicy
    #: Un emisor por red. La clave es la red de **origen**, que es donde se emite.
    broadcasters: Mapping[str, EvmBroadcaster] = field(default_factory=dict)
    clock: Clock | None = None
    #: Con qué se valora lo entregado cuando la pata de origen no está ya en la
    #: moneda de referencia. Puede faltar: sin ella, los puentes que salen en la
    #: stablecoin de su red —el caso normal— funcionan igual.
    valuation: ReferenceValuation | None = None

    async def __call__(
        self, quote: BridgeQuote, *, recipient: str
    ) -> BroadcastReceipt:
        origin = quote.request.origin.chain
        spec = chain(origin)

        # 1. El modo primero: antes de gastar red, y antes de pedir la clave.
        self.gateway.precheck(Capability.PREPARE_TX)
        self.gateway.precheck(Capability.BROADCAST_TX)

        # 2. Esta entrega firma en EVM y sólo en EVM.
        require_evm(spec)

        # 3. Los topes, contra lo ya ejecutado en las últimas 24 h, y contra las
        #    **dos** redes: la de destino es donde el dinero acaba.
        intent = await self._intent(quote, recipient)
        self.policy.check_intent(intent)

        broadcaster = self._broadcaster(origin)

        # El destino de la **llamada** —el contrato del puente— y el de los
        # **fondos** —la cartera— son dos direcciones distintas. Se llaman
        # distinto en todo el fichero por la misma razón que en los swaps.
        wallet = recipient_for(quote, recipient)

        # 4. Y 5. El payload, del motor que cotizó, y el contrato que declara.
        transaction, contract = await self._build_checked(quote, recipient)

        # 6. La aprobación, si el token entregado la necesita. Puede volver con un
        #    payload nuevo: entre aprobar y firmar pasa una transacción entera.
        transaction, contract = await self._ensure_allowance(
            quote, transaction, recipient, contract, broadcaster
        )

        # 7. El permiso. Con la operación entera delante, o por la política.
        await self._authorize(quote, transaction, contract, wallet, intent)

        # 8. Firmar, emitir, anotar. El asiento va en la red de **origen**, que es
        #    de donde salió el dinero.
        receipt = await self._sign_and_send(transaction, broadcaster, expected_to=contract)
        self.policy.ledger.record_receipt(receipt, intent)
        _log.info(
            "bridge.recorded",
            origin=origin,
            destination=quote.request.destination.chain,
            pair=quote.pair_label,
            engine_id=quote.engine_id,
            tx_hash=receipt.tx_hash,
            status=receipt.status.value,
        )
        return receipt

    # --------------------------------------------------------------- pasos  #
    async def _intent(self, quote: BridgeQuote, recipient: str) -> BridgeIntent:
        """Lo que se propone cruzar, con el importe **en la unidad del tope**.

        Hay dos casos, y el orden importa:

        1. **La pata de origen ya está en la moneda de referencia.** Entonces ese
           importe *es* el del tope y se elige en vez de calcularse: el tope se
           mide contra un hecho y no contra una estimación. Es el caso normal de
           un puente —se cruza la stablecoin de la red— y no cuesta ni una
           petición de red.
        2. **No lo está.** Se entrega ETH o un token cualquiera, y compararlo tal
           cual contra un tope escrito en la stablecoin sería comparar peras con
           manzanas. Se valora con los mismos motores que cotizan, y **si no se
           puede valorar no se cruza**: un tope que se salta cuando no se puede
           medir no es un tope.
        """
        delivered = quote.request.amount_in
        origin = quote.request.origin.chain
        reference = quote_token(origin)
        # El caso 1, con la regla compartida: si la pata que se entrega ya está en
        # la moneda del tope, ese importe **es** el del tope.
        notional = bridge_notional(quote.request)
        if notional is not None:
            return BridgeIntent(
                quote=quote,
                recipient=recipient,
                notional=notional,
                engine_id=quote.engine_id,
            )
        if reference is None:
            # La red no declara moneda de referencia, así que no hay nada contra lo
            # que valorar y no hay a qué convertir: el importe entregado se mide tal
            # cual. Es el único caso en el que se mide algo que no está en la unidad
            # del tope, y no se puede hacer otra cosa — pero la red no tiene tope
            # con el que compararlo, así que tampoco se está saltando ninguno.
            return BridgeIntent(
                quote=quote,
                recipient=recipient,
                notional=delivered,
                engine_id=quote.engine_id,
            )
        return await self._valued_intent(quote, recipient, delivered, reference)

    async def _valued_intent(
        self,
        quote: BridgeQuote,
        recipient: str,
        delivered: TokenAmount,
        reference: Token,
    ) -> BridgeIntent:
        """Valora lo entregado y devuelve el intento, o se niega a cruzar."""
        origin = quote.request.origin
        if self.valuation is None:
            raise ExecutionError(
                f"el puente sale de {origin.chain} en {origin.symbol}, que no es la "
                f"moneda de referencia de esa red ({reference.symbol}), y esta "
                f"ejecución no tiene con qué valorar {delivered} en ella. Cruzar sin "
                f"poder aplicar el tope sería operar sin límite."
            )
        value = await self.valuation(origin, delivered, reference)
        if value is None:
            raise ExecutionError(self.valuation.describe_failure(origin, reference))
        _log.info(
            "bridge.valued_in_reference",
            pair=quote.pair_label,
            origin=origin.chain,
            delivered=str(delivered),
            reference=reference.symbol,
            value=f"{value.as_decimal():f}",
        )
        return BridgeIntent(
            quote=quote,
            recipient=recipient,
            notional=delivered,
            engine_id=quote.engine_id,
            reference_value=value,
        )

    async def _build_checked(
        self, quote: BridgeQuote, recipient: str
    ) -> tuple[UnsignedTransaction, str]:
        """Construye el payload y comprueba que va donde el motor dice que va.

        Devuelve también el contrato declarado, y no sólo el payload: ese valor es
        el que hay que contrastar al emitir, y recalcularlo fuera con otra llamada
        al motor daría una segunda respuesta que podría no ser la misma que se
        acaba de comprobar.
        """
        transaction = await self.prepare.build(quote, recipient=recipient)

        origin = quote.request.origin.chain
        contract = self.prepare.planner_for(quote).expected_destination(origin)
        if contract is None:
            raise ExecutionError(
                f"el motor «{quote.engine_id}» no declara a qué contrato dirige los "
                f"puentes desde «{origin}», así que no hay contra qué comprobar el "
                f"destino del payload y no se puede firmar. En un puente esto no es "
                f"una formalidad: un destino equivocado no pierde dinero en una "
                f"operación de mercado, lo manda a otra parte y no vuelve."
            )
        if contract.lower() != transaction.to_address.lower():
            raise ExecutionError(
                f"el payload dirige el puente a {transaction.to_address}, y el motor "
                f"«{quote.engine_id}» declara {contract} para «{origin}». No se firma: "
                f"el destino no es el que el motor dice usar."
            )

        return transaction, contract

    # ---------------------------------------------------- aprobación ERC-20  #
    async def _ensure_allowance(
        self,
        quote: BridgeQuote,
        transaction: UnsignedTransaction,
        recipient: str,
        contract: str,
        broadcaster: EvmBroadcaster,
    ) -> tuple[UnsignedTransaction, str]:
        """Deja el permiso del token listo, y devuelve el payload a firmar.

        Devuelve el payload porque aprobar **caduca el anterior**: entre la
        aprobación y el puente se emite y se confirma una transacción entera, y el
        payload del motor lleva dentro un importe mínimo y una fecha de validez.
        Firmar el viejo sería cruzar contra un importe que ya no está.

        ### Sólo la aprobación directa, y se dice por qué

        Los dos motores de puente declaran una aprobación directa —el permiso del
        ERC-20 al contrato que lo va a mover—, y por eso aquí no hay el camino
        encadenado de Permit2 que sí tienen los swaps. Si alguna vez llegara una
        aprobación encadenada, esto **se niega** en vez de conceder la primera de
        las dos: conceder medio permiso y parar dejaría al contrato autorizado
        sobre el token sin que el puente llegue a ocurrir, que es exactamente el
        estado que no se quiere dejar atrás.
        """
        sold = quote.request.origin
        address = _token_to_approve(transaction, sold, chain(sold.chain))
        if address is None:
            return transaction, contract

        approval = transaction.token_approval
        if approval.is_chained:
            raise UnsupportedOperationError(
                f"el motor «{quote.engine_id}» pide una aprobación encadenada para "
                f"cruzar desde {sold.chain}, y este camino sólo sabe conceder la "
                f"directa del ERC-20. No se construyó nada: conceder la primera de "
                f"las dos dejaría al contrato autorizado sobre tu {sold.symbol} sin "
                f"que el puente llegue a ocurrir."
            )

        owner = address_from_key(self.keys.require())
        granted = await broadcaster.read_allowance(address, owner, approval.target)
        if granted >= quote.request.amount_in.raw:
            return transaction, contract

        await self._approve(
            address, sold, quote, approval.target, owner, broadcaster, contract
        )
        # El payload viejo ya no sirve: se vuelve a construir con el permiso ya
        # concedido, y así el importe mínimo y la validez son de ahora.
        return await self._build_checked(quote, recipient)

    async def _approve(
        self,
        token_address: str,
        token: Token,
        quote: BridgeQuote,
        spender: str,
        owner: str,
        broadcaster: EvmBroadcaster,
        contract: str,
    ) -> BroadcastReceipt:
        """Aprueba el importe **exacto** que hace falta, como operación propia.

        Nunca un importe ilimitado. Un `approve` sin límite deja al contrato
        autorizado a vaciar ese token para siempre, y es el patrón que convierte un
        contrato comprometido en una pérdida total. El precio de aprobar lo justo
        es una transacción de más la primera vez de cada token, y es el precio
        correcto.
        """
        spec = chain(quote.request.origin.chain)
        approval = UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=token_address,
            calldata=build_approve_calldata(spender, quote.request.amount_in.raw),
            value=TokenAmount.zero(spec.native_decimals, spec.native_symbol),
            description=(
                f"Autorizar a {shorten(spender)} a gastar {quote.request.amount_in} "
                f"de tu {token.symbol} para cruzar a {quote.request.destination.chain}"
            ),
        )
        await self.gateway.authorize(
            Capability.BROADCAST_TX,
            f"Aprobar {token.symbol} para el puente",
            details=(
                f"Red: {quote.request.origin.chain}",
                f"Token: {token.symbol}",
                f"Importe autorizado: {quote.request.amount_in} (el justo, no ilimitado)",
                f"Autorizado a gastarlo: {shorten(spender)}",
                f"Desde la cartera: {shorten(owner)}",
                "El permiso va al contrato que mueve el token para el puente.",
                "Es una transacción real: cuesta gas y no se puede deshacer.",
            ),
            transaction=approval,
        )
        receipt = await self._sign_and_send(
            approval, broadcaster, expected_to=token_address
        )
        self.policy.ledger.record_approval(
            receipt,
            pair=quote.pair_label,
            engine_id=quote.engine_id,
            token_symbol=token.symbol,
            spender=spender,
            granted_raw=quote.request.amount_in.raw,
        )
        if receipt.status is not BroadcastStatus.SUCCESS:
            raise ExecutionError(
                f"la aprobación de {token.symbol} no llegó a buen término "
                f"({receipt.status.value}, {receipt.tx_hash}). Sin ese permiso el "
                f"puente revertiría, así que no se firma."
            )
        # Se vuelve a leer en vez de darlo por hecho: la aprobación pudo minar con
        # éxito y aun así no ser suficiente si el token cobra comisión al mover.
        after = await broadcaster.read_allowance(token_address, owner, spender)
        if after < quote.request.amount_in.raw:
            raise ExecutionError(
                f"tras aprobar, el permiso sobre {token.symbol} sigue siendo "
                f"insuficiente ({after} de {quote.request.amount_in.raw}). Algunos "
                f"tokens ajustan el importe al transferirlo; el puente revertiría."
            )
        # `contract` entra sólo para que el registro de arriba y el valor devuelto
        # sean el mismo dato: el contraste del destino ya se hizo al construir.
        _log.info(
            "bridge.approval_granted",
            token=token.symbol,
            spender=spender,
            contract=contract,
        )
        return receipt

    # ---------------------------------------------------------------- firma  #
    async def _authorize(
        self,
        quote: BridgeQuote,
        transaction: UnsignedTransaction,
        contract: str,
        wallet: str,
        intent: BridgeIntent,
    ) -> None:
        """El permiso para emitir, con todo delante.

        Se muestran **las dos redes** y **las dos direcciones**, con nombres
        distintos: la del contrato al que se llama y la de la cartera que recibe.
        En un puente esto importa más que en un swap, porque el usuario tiene que
        poder ver de dónde sale el dinero y en qué red va a aparecer — y una
        etiqueta «destino» que valiera para las dos cosas no dejaría verlo.
        """
        request = quote.request
        await self.gateway.authorize(
            Capability.BROADCAST_TX,
            f"Emitir puente por {quote.provider}",
            details=(
                f"Cruzas: {quote.pair_label}",
                f"Entregas: {request.amount_in} en {request.origin.chain}",
                f"Recibes (estimado): {quote.amount_out} en {request.destination.chain}",
                f"Mínimo garantizado: {quote.amount_out_min}",
                f"Duración estimada: {quote.duration_label}",
                f"Importe de referencia: {intent.measured}",
                f"Motor que construye: {quote.engine_id}",
                f"Contrato que ejecuta: {shorten(contract)}",
                f"Fondos a tu cartera: {shorten(wallet)}",
                f"Firma la cartera: {self.address() or 'sin clave configurada'}",
                "Es una operación real e irreversible: se firma y se emite. Una vez "
                "emitida, el dinero sale de esta red y el tiempo del cruce no lo "
                "controla la aplicación.",
            ),
            transaction=transaction,
        )

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
        """
        return await sign_and_send(
            unsigned,
            broadcaster,
            private_key=self.keys.require(),
            expected_to=expected_to,
        )

    # --------------------------------------------------------------- apoyo  #
    def address(self) -> str | None:
        """La cartera que firma, o `None` si no hay clave. **Nunca la clave.**"""
        if not self.keys.available():
            return None
        return address_from_key(self.keys.require())

    def _broadcaster(self, chain_key: str) -> EvmBroadcaster:
        broadcaster = self.broadcasters.get(chain_key)
        if broadcaster is None:
            raise ExecutionError(
                f"no hay ningún nodo configurado para «{chain_key}»: sin él no se "
                f"puede leer el estado de la red (nonce, gas, comisiones) ni emitir "
                f"nada. Declara al menos un endpoint para la red de origen."
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
    distintas.

    Dos casos en los que no hay que aprobar, los mismos que en los swaps:

    - **El nativo.** No hay `transferFrom` que autorizar: se entrega como `value`
      de la propia transacción.
    - **El envoltorio pagado como nativo.** El payload manda `value` y el contrato
      lo envuelve él; aprobar ahí sería una transacción de más y, peor, una
      autorización que se queda concedida sin que nada llegue a usarla.
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
