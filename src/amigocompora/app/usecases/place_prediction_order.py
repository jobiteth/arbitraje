"""Publicar una orden en un mercado de predicción: firmar y enviar al recinto.

Capacidad requerida: `BROADCAST_TX`, que **sólo** concede el modo `EJECUCIÓN`.
Es el camino por el que se compromete dinero en un recinto que liquida fuera de
la cadena, y todo lo que hay aquí está ordenado por esa consecuencia.

### En qué se parece y en qué no a `ExecuteSwap`

Se parece en la forma: modo, límites, permisos, confirmación, firma, registro.
Se parece porque ese orden no depende del recinto —es el orden en que se toman
las decisiones que protegen al usuario— y por eso este fichero se lee al lado
del otro.

No se parece en el **desenlace**, y esa diferencia manda en tres sitios:

1. **Aquí no se emite una transacción.** Se publica un mensaje firmado. No hay
   nonce, ni gas, ni bloque, y el identificador que devuelve el recinto no es un
   hash que se pueda buscar en un explorador. El asiento del registro lo dice
   (`record_order`), porque las dos cadenas tienen la misma forma y **no se
   distinguen mirándolas**.
2. **Un permiso previo, o dos, según el lado.** Comprar mueve colateral, y el
   colateral es un ERC-20: se aprueba **el importe exacto** de esta orden.
   Vender mueve participaciones, que son un ERC-1155, y ahí el estándar **no
   tiene permiso por cantidad**: o se le concede al recinto el manejo de toda la
   colección, o no se puede vender. Es una diferencia que el usuario tiene que
   leer antes de aceptarla, no un detalle que se esconda.
3. **La clave se usa en cada paso que firma**, igual que en los swaps: los
   permisos son transacciones **en la cadena** y hay que firmarlas, y publicar
   exige credenciales que el recinto sólo entrega a quien firma un mensaje de
   nivel 1. Se pide en cada momento y se suelta; no se guarda en ningún objeto.

### El orden de los pasos

1. **El modo, antes que nada**: en `OBSERVACIÓN`, `SIMULACIÓN` o `ASISTIDO` esto
   lanza en la primera línea y no se gasta ni una petición de red.
2. **La orden**, construida por el motor: es el **qué**, y se puede tener delante
   antes de que exista ningún permiso ni ninguna firma.
3. **Los límites**, con el importe medido en la unidad del tope.
4. **El permiso del colateral**, si hace falta, como operación propia.
5. **El «sí» del usuario**, con la orden entera delante.
6. **La firma**, que se comprueba a sí misma antes de devolver nada.
7. **Publicar y anotar.** El asiento se escribe con el identificador que dio el
   recinto, y sólo si el recinto aceptó: una orden rechazada no movió nada.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_UP, Decimal
from typing import Final

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.execution_policy import AutonomyPolicy, PrivateKeySource
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.addresses import shorten
from amigocompora.domain.chains import chain
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.models import (
    BroadcastReceipt,
    BroadcastStatus,
    PredictionMarket,
    PredictionOrder,
    PredictionOrderIntent,
    PredictionSide,
    SubmittedPredictionOrder,
    Token,
    UnsignedTransaction,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import PredictionOrderPlanner
from amigocompora.infra.evm.broadcast import (
    EvmBroadcaster,
    build_approve_calldata,
    build_set_approval_for_all_calldata,
    require_evm,
)
from amigocompora.infra.evm.dispatch import sign_and_send
from amigocompora.infra.evm.signer import address_from_key

_log = structlog.get_logger(__name__)

#: Con este nombre se anota en el registro la autorización de participaciones.
#:
#: No es el símbolo de un token, y por eso no se busca en el catálogo: las
#: participaciones de un mercado de predicción son un ERC-1155 que no tiene
#: símbolo, y escribir aquí uno cualquiera —«CTF», o el del colateral— sería
#: poner en el asiento un nombre que no corresponde a nada que exista.
SHARES_LABEL: Final = "participaciones"


@dataclass(frozen=True, slots=True)
class PlacePredictionOrder:
    """Construye, firma y publica una orden. Devuelve lo que contestó el recinto."""

    registry: EngineRegistry
    gateway: ConfirmationGateway
    keys: PrivateKeySource
    policy: AutonomyPolicy
    #: Un emisor por red, para los permisos previos. Se recibe como mapa y no
    #: como registro para que este caso de uso no dependa de `infra.rpc`.
    broadcasters: Mapping[str, EvmBroadcaster] = field(default_factory=dict)
    clock: Clock | None = None

    async def __call__(
        self,
        market: PredictionMarket,
        *,
        outcome_label: str,
        side: PredictionSide,
        size: Decimal,
        price: Decimal,
        recipient: str,
    ) -> SubmittedPredictionOrder:
        # 1. El modo primero: antes de gastar red, y antes de pedir la clave.
        self.gateway.precheck(Capability.PREPARE_TX)
        self.gateway.precheck(Capability.BROADCAST_TX)

        planner = self.registry.prediction_planner()
        spec = chain(market.venue.chain)
        # 2. Esta entrega firma en EVM y sólo en EVM: los permisos previos son
        #    transacciones, y sin ellos no se puede publicar nada.
        require_evm(spec)

        order = planner.build_order(
            market, outcome_label=outcome_label, side=side, size=size, price=price
        )
        collateral = planner.collateral_for(market)
        exchange = planner.exchange_for(market)

        # 3. Los topes. El importe que se mide es el lado del colateral —lo que se
        #    paga al comprar, lo que se cobra al vender—, que ya está en la unidad
        #    en la que están escritos los topes y por eso no hay nada que valorar.
        intent = self._intent(order, planner, collateral, recipient)
        self.policy.check_intent(intent)

        broadcaster = self._broadcaster(market.venue.chain)

        # 4. El permiso previo. Cambia con el lado de la orden, y por eso lo
        #    decide `_ensure_approval`, que es quien sabe qué token se mueve.
        await self._ensure_approval(
            planner, order, collateral, exchange, market, broadcaster
        )

        # 5. El permiso del usuario, con la orden entera delante.
        await self._authorize(order, exchange, recipient, intent)

        # 6 y 7. Firmar, publicar, anotar. Se firma **el mismo objeto** que se
        #        acaba de enseñar: no se reconstruye, para que no pueda haber
        #        dos respuestas que se separen entre lo confirmado y lo firmado.
        key = self.keys.require()
        signed = planner.sign_order(order, private_key=key)
        submitted = await planner.submit_order(signed, private_key=key)

        self.policy.ledger.record_order(
            intent=intent,
            order_id=submitted.order_id,
            status=submitted.status,
            observed_at=self._now(),
            chain_key=market.venue.chain,
            reason=f"{order.side.value} {order.size} a {order.price}",
        )
        _log.info(
            "prediction.order_recorded",
            chain=market.venue.chain,
            market=order.market_id,
            outcome=order.outcome_label,
            side=order.side.value,
            order_id=submitted.order_id,
            status=submitted.status,
        )
        return submitted

    # --------------------------------------------------------------- pasos  #
    def _now(self) -> datetime:
        """El momento del asiento, según el reloj inyectado."""
        return (self.clock or SystemClock()).now()

    def _intent(
        self,
        order: PredictionOrder,
        planner: PredictionOrderPlanner,
        collateral: Token,
        recipient: str,
    ) -> PredictionOrderIntent:
        """El intento que la política evalúa, con el importe en la unidad del tope.

        Se redondea **hacia arriba** al número de decimales del colateral, y eso
        no es un descuido de un céntimo: el tope existe para no pasarse, así que
        el importe que se le enseña tiene que ser el que de verdad se mueve o
        más, nunca menos. Un producto de precio por participaciones que cayera
        justo entre dos unidades mínimas se contaría, redondeando a la baja, por
        debajo del dinero comprometido.

        En la práctica el producto siempre es representable —el salto de precio
        y el de participaciones suman como mucho los decimales del colateral—,
        así que este redondeo no cambia ninguna cifra. Está porque el día que
        cambie la tabla de redondeo, la dirección del error tiene que seguir
        siendo la segura.
        """
        return PredictionOrderIntent(
            order=order,
            recipient=recipient,
            notional=TokenAmount.from_decimal(
                order.cost,
                collateral.decimals,
                collateral.symbol,
                rounding=ROUND_UP,
            ),
            engine_id=planner.manifest.engine_id,
        )

    async def _authorize(
        self,
        order: PredictionOrder,
        exchange: str,
        recipient: str,
        intent: PredictionOrderIntent,
    ) -> None:
        """El permiso para publicar, con la orden entera delante.

        Se muestran las dos direcciones con nombres distintos, igual que en el
        camino de swaps: el contrato contra el que se firma y la cartera que
        recibe. Llamar «destino» a las dos sería no decir ninguna de las dos.

        Y se dice explícitamente que esto **no** es una transacción: quien
        confirma tiene que saber si lo que viene después se puede deshacer, y una
        orden en un libro se puede cancelar mientras nadie la haya cruzado,
        mientras que una transacción emitida no se puede tocar.
        """
        await self.gateway.authorize(
            Capability.BROADCAST_TX,
            f"Publicar orden en {order.venue_id}",
            details=(
                f"Mercado: {order.question}",
                f"Resultado: {order.outcome_label}",
                f"Operación: {'comprar' if order.is_buy else 'vender'} "
                f"{order.shares} participaciones",
                f"Precio límite: {order.price} (salto {order.tick_size})",
                f"{'Pagas' if order.is_buy else 'Cobras'}: {order.cost}",
                f"Importe de referencia: {intent.notional}",
                f"Contrato que valida la firma: {shorten(exchange)}",
                f"Firma la cartera: {self.address() or 'sin clave configurada'}",
                f"Recibe: {shorten(recipient)}",
                "Se firma y se envía al libro de órdenes del recinto. No es una "
                "transacción: no cuesta gas y se puede cancelar mientras nadie "
                "la haya cruzado.",
            ),
            transaction=order,
        )

    # ------------------------------------------------------------ permisos  #
    async def _ensure_approval(
        self,
        planner: PredictionOrderPlanner,
        order: PredictionOrder,
        collateral: Token,
        exchange: str,
        market: PredictionMarket,
        broadcaster: EvmBroadcaster,
    ) -> None:
        """Deja concedido el permiso que el recinto necesita para liquidar.

        Cuál hace falta depende del lado, y no es una preferencia:

        - **Comprar** entrega colateral. El recinto lo cobra con `transferFrom`,
          así que necesita permiso sobre el ERC-20, y se concede **por el importe
          exacto** de esta orden.
        - **Vender** entrega participaciones. Son un ERC-1155, y ahí el estándar
          sólo tiene permiso por colección: `setApprovalForAll`.

        Se comprueba antes de conceder en los dos casos, porque volver a conceder
        lo ya concedido es gas tirado.
        """
        owner = address_from_key(self.keys.require())
        if order.is_buy:
            await self._approve_collateral(
                order, collateral, exchange, owner, broadcaster
            )
            return
        await self._approve_shares(
            planner.shares_collection_for(market),
            order,
            exchange,
            owner,
            broadcaster,
        )

    async def _approve_collateral(
        self,
        order: PredictionOrder,
        collateral: Token,
        exchange: str,
        owner: str,
        broadcaster: EvmBroadcaster,
    ) -> None:
        """Aprueba al recinto el colateral, **por el importe exacto** de la orden.

        Nunca ilimitado, por la misma razón que en los swaps: un permiso sin tope
        deja al recinto autorizado a vaciar ese token para siempre. Aquí hay un
        motivo añadido: el recinto liquida **después** —una orden puede cruzarse
        horas más tarde— así que conceder de más sería darle margen sobre el
        saldo futuro de la cartera, no sobre esta operación.
        """
        if collateral.address is None:
            raise ExecutionError(
                f"el colateral «{collateral.symbol}» del recinto no tiene dirección "
                f"en «{order.chain}», así que no hay contrato al que concederle nada."
            )
        need = TokenAmount.from_decimal(
            order.cost, collateral.decimals, collateral.symbol, rounding=ROUND_UP
        ).raw
        granted = await broadcaster.read_allowance(collateral.address, owner, exchange)
        if granted >= need:
            return

        spec = chain(order.chain)
        approval = UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=collateral.address,
            calldata=build_approve_calldata(exchange, need),
            value=TokenAmount.zero(spec.native_decimals, spec.native_symbol),
            description=(
                f"Autorizar al recinto {shorten(exchange)} a gastar {order.cost} "
                f"de tu {collateral.symbol} para esta orden"
            ),
        )
        await self.gateway.authorize(
            Capability.BROADCAST_TX,
            f"Aprobar {collateral.symbol} para la orden",
            details=(
                f"Token: {collateral.symbol} ({shorten(collateral.address)})",
                f"Importe autorizado: {order.cost} (el justo, no ilimitado)",
                f"Autorizado a gastarlo: {shorten(exchange)} (el recinto)",
                f"Desde la cartera: {shorten(owner)}",
                "Es una transacción real: cuesta gas y no se puede deshacer.",
                "Es el paso previo: con esto solo el recinto todavía no puede "
                "cobrar nada hasta que se publique la orden.",
            ),
            transaction=approval,
        )
        receipt = await self._send(approval, broadcaster, expected_to=collateral.address)
        self.policy.ledger.record_approval(
            receipt,
            pair=_label(order),
            engine_id=order.venue_id,
            token_symbol=collateral.symbol,
            spender=exchange,
            granted_raw=need,
        )
        _require_success(receipt, f"la aprobación de {collateral.symbol}")
        # Se relee en vez de darlo por hecho: la transacción pudo minar bien y el
        # permiso no ser el que se pidió.
        after = await broadcaster.read_allowance(collateral.address, owner, exchange)
        if after < need:
            raise ExecutionError(
                f"tras aprobar, el permiso sobre {collateral.symbol} sigue siendo "
                f"insuficiente ({after} de {need} en unidad mínima). El recinto no "
                f"podría cobrar la orden y la rechazaría."
            )

    async def _approve_shares(
        self,
        collection: str,
        order: PredictionOrder,
        exchange: str,
        owner: str,
        broadcaster: EvmBroadcaster,
    ) -> None:
        """Concede al recinto el manejo de la colección de participaciones.

        **Es un permiso sin importe**, y por eso el diálogo lo dice con esas
        palabras: alcanza a todas las participaciones que esta cartera tenga en
        esa colección, no sólo a las de esta orden. No es una elección de este
        código —el estándar ERC-1155 no tiene permiso por cantidad— pero sí es
        una decisión que el usuario toma, y tomarla sin saberlo no sería tomarla.
        """
        if await broadcaster.read_operator_approval(collection, owner, exchange):
            return

        spec = chain(order.chain)
        approval = UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=collection,
            calldata=build_set_approval_for_all_calldata(exchange, approved=True),
            value=TokenAmount.zero(spec.native_decimals, spec.native_symbol),
            description=(
                f"Autorizar al recinto {shorten(exchange)} a mover tus "
                f"participaciones de «{order.question}»"
            ),
        )
        await self.gateway.authorize(
            Capability.BROADCAST_TX,
            "Autorizar las participaciones al recinto",
            details=(
                f"Colección (ERC-1155): {shorten(collection)}",
                f"Autorizado a moverlas: {shorten(exchange)} (el recinto)",
                f"Desde la cartera: {shorten(owner)}",
                "Aviso: este permiso NO tiene importe. El estándar ERC-1155 sólo "
                "permite autorizar la colección entera, así que alcanza a todas "
                "las participaciones que tengas en este contrato condicional, no "
                "sólo a las de esta orden.",
                "Es una transacción real: cuesta gas y no se puede deshacer.",
            ),
            transaction=approval,
        )
        receipt = await self._send(approval, broadcaster, expected_to=collection)
        self.policy.ledger.record_approval(
            receipt,
            pair=_label(order),
            engine_id=order.venue_id,
            token_symbol=SHARES_LABEL,
            spender=exchange,
            # Cero porque no hay importe que conceder: es un booleano. El asiento
            # no inventa una cifra que nadie autorizó.
            granted_raw=0,
        )
        _require_success(receipt, "la autorización de las participaciones")
        if not await broadcaster.read_operator_approval(collection, owner, exchange):
            raise ExecutionError(
                f"tras autorizar, el recinto sigue sin poder mover las "
                f"participaciones de {shorten(owner)} en {shorten(collection)}. "
                f"No se publica la orden: se quedaría sin liquidar."
            )

    # ---------------------------------------------------------------- firma  #
    async def _send(
        self,
        unsigned: UnsignedTransaction,
        broadcaster: EvmBroadcaster,
        *,
        expected_to: str,
    ) -> BroadcastReceipt:
        """Reúne el estado de red, firma en local y emite. Sólo para los permisos.

        La clave se pide aquí, que es el último momento posible, y se suelta al
        salir de la función. La secuencia vive en `infra.evm.dispatch`, la misma
        que usan el camino de swaps y el del cobro: era idéntica en los tres, y
        tenerla copiada es lo que deja que una copia se desvíe sin que se note.
        """
        return await sign_and_send(
            unsigned,
            broadcaster,
            private_key=self.keys.require(),
            expected_to=expected_to,
            event="prediction.approval_sending",
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
                f"puede leer el permiso que el recinto necesita ni concederlo. "
                f"Declara al menos un endpoint para esa red."
            )
        return broadcaster


def _label(order: PredictionOrder) -> str:
    """Cómo se nombra la operación en el registro: mercado y resultado.

    La pregunta sola no basta: un mercado con dos resultados produce dos
    operaciones opuestas, y un asiento que dijera sólo el mercado no permitiría
    saber cuál de las dos se hizo.
    """
    return f"{order.question} — {order.outcome_label}"


def _require_success(receipt: BroadcastReceipt, what: str) -> None:
    """Corta si la transacción previa no llegó a buen término."""
    if receipt.status is not BroadcastStatus.SUCCESS:
        raise ExecutionError(
            f"{what} no llegó a buen término ({receipt.status.value}, "
            f"{receipt.tx_hash}). Sin ese permiso el recinto rechazaría la orden, "
            f"así que no se firma."
        )
