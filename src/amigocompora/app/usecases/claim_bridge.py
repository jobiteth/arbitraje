"""Recibir un puente en destino: la segunda transacción de CCTP. Firma y emite.

Capacidad requerida: `BROADCAST_TX`, que **sólo** concede el modo `EJECUCIÓN`.
Es el paso que mintea el USDC en la red de destino. Sin él, el origen ya quemó
y el dinero se queda en tránsito para siempre.

### Qué lo distingue de `ExecuteBridge`

- **No cuenta para los topes.** Recibe dinero que ya se quemó y autorizó: lo que
  entra no debe consumir el presupuesto del día ni estrechar el tope por dinero
  que vuelve. Lo que sí se comprueba es todo lo demás —modo, red, token y motor—,
  igual que en `RedeemPrediction`.
- **No pide aprobación previa.** La recepción no mueve un token ajeno: el mint lo
  hace el contrato de Circle, así que no hay permiso que conceder.

El destinatario no se elige aquí. Lo fija la quema en origen, y por eso la
descripción que ve el usuario lo dice en vez de pedirlo otra vez.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.execution_policy import (
    RECEIVE_KIND,
    AutonomyPolicy,
    PrivateKeySource,
)
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.single_flight import SingleFlight
from amigocompora.app.usecases.track_bridge import TrackedBridge
from amigocompora.domain.addresses import shorten
from amigocompora.domain.chains import chain
from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.models import BroadcastReceipt
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import BridgeReceiver, EngineKind
from amigocompora.infra.evm.broadcast import EvmBroadcaster, require_evm
from amigocompora.infra.evm.dispatch import sign_and_send
from amigocompora.infra.evm.signer import address_from_key

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ClaimIntent:
    """La recepción en destino, en los términos que la política evalúa.

    Satisface `ExecutableIntent`. `counts_towards_limits` es `False` a propósito:
    el importe que entra no se mide contra los topes, y `notional` queda en cero
    porque el importe minteado no se decodifica desde el mensaje de Circle.
    """

    chain: str
    label: str
    tokens: tuple[str, ...]
    engine_id: str
    recipient: str
    notional: TokenAmount
    reference_value: TokenAmount | None = None

    @property
    def measured(self) -> TokenAmount:
        return self.notional

    @property
    def counts_towards_limits(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ClaimBridge:
    """Firma y emite la recepción de un puente ya atestado."""

    registry: EngineRegistry
    gateway: ConfirmationGateway
    keys: PrivateKeySource
    policy: AutonomyPolicy
    broadcasters: Mapping[str, EvmBroadcaster] = field(default_factory=dict)
    #: Un candado compartido con los demás caminos que firman: mientras una
    #: ejecución está en curso, la siguiente se rechaza en vez de encolarse. La
    #: misma instancia que usan swaps, puentes y retiradas — el recurso escaso,
    #: el nonce de la cartera, es el mismo. Ver `SingleFlight`.
    single_flight: SingleFlight = field(default_factory=SingleFlight)

    async def __call__(self, record: TrackedBridge) -> BroadcastReceipt:
        # Una ejecución a la vez, y la segunda se rechaza —no se encola—: ver
        # `SingleFlight`.
        async with self.single_flight.exclusive(what="una recepción"):
            return await self._run(record)

    async def _run(self, record: TrackedBridge) -> BroadcastReceipt:
        origin = record.origin_chain
        destination = record.destination_chain

        # 1. El modo primero: antes de gastar red, y antes de pedir la clave.
        self.gateway.precheck(Capability.PREPARE_TX)
        self.gateway.precheck(Capability.BROADCAST_TX)

        # 2. La recepción firma en EVM, y en la red de destino.
        spec = chain(destination)
        require_evm(spec)

        # 3. El payload lo construye el motor que emitió la quema. Si Circle aún no
        #    la ha atestado, no hay nada que recibir: se dice y no se firma.
        receiver = self._receiver(record.engine_id)
        transaction = await receiver.prepare_receive(
            record.tx_hash, origin_chain=origin, destination_chain=destination
        )
        if transaction is None:
            raise ExecutionError(
                f"Circle aún no ha atestado la quema de {origin} "
                f"({record.tx_hash}): no hay nada que recibir todavía."
            )

        # 4. Los topes: sólo red, token y motor, porque este intento no cuenta.
        intent = ClaimIntent(
            chain=destination,
            label=f"Recepción CCTP desde {origin}",
            tokens=(record.destination_symbol,),
            engine_id=record.engine_id,
            recipient=self.address() or "",
            notional=TokenAmount.zero(record.destination_decimals, record.destination_symbol),
        )
        self.policy.check_intent(intent)

        broadcaster = self._broadcaster(destination)

        # 5. El permiso: el «sí» del usuario, o la política si está armada.
        await self.gateway.authorize(
            Capability.BROADCAST_TX,
            f"Recibir puente de {origin} en {destination}",
            details=(
                f"Quema de origen: {record.tx_hash}",
                f"Importe quemado: {record.amount_text}",
                f"Contrato que recibe: {shorten(transaction.to_address)}",
                f"Firma la cartera: {self.address() or 'sin clave configurada'}",
                "Circle ya atestó la quema. Esta transacción mintea el USDC en la "
                "red de destino y cuesta gas. No se puede deshacer.",
            ),
            transaction=transaction,
        )

        # 6. Firmar, emitir, anotar. La clave se pide aquí y se suelta al salir.
        receipt = await sign_and_send(
            transaction,
            broadcaster,
            private_key=self.keys.require(),
            expected_to=transaction.to_address,
            event="bridge.claim_sending",
        )
        # El tipo, además del importe cero, para que lo que entra quede dicho
        # por lo que el asiento es: una recepción. Como `transaction` contaría
        # el día que el importe deje de ser cero, que es justo lo que este
        # caso de uso documenta que no debe pasar.
        self.policy.ledger.record_receipt(receipt, intent, kind=RECEIVE_KIND)
        _log.info(
            "bridge.claim_recorded",
            origin=origin,
            destination=destination,
            source_tx=record.tx_hash,
            tx_hash=receipt.tx_hash,
            status=receipt.status.value,
        )
        return receipt

    def address(self) -> str | None:
        """La cartera que firma, o `None` si no hay clave. **Nunca la clave.**"""
        if not self.keys.available():
            return None
        return address_from_key(self.keys.require())

    def _receiver(self, engine_id: str) -> BridgeReceiver:
        for engine in self.registry.active_stack(EngineKind.CROSS_CHAIN):
            if isinstance(engine, BridgeReceiver) and engine.manifest.engine_id == engine_id:
                return engine
        raise ExecutionError(
            f"el motor «{engine_id}» no está activo: sin él no se puede construir la "
            f"recepción del puente. Actívalo en el panel de motores."
        )

    def _broadcaster(self, chain_key: str) -> EvmBroadcaster:
        broadcaster = self.broadcasters.get(chain_key)
        if broadcaster is None:
            raise ExecutionError(
                f"no hay ningún nodo configurado para «{chain_key}»: sin él no se "
                f"puede emitir la recepción del puente."
            )
        return broadcaster
