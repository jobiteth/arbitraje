"""Preparar un swap: el único caso de uso que produce algo con efectos.

Capacidad requerida: `PREPARE_TX`, que sólo concede el modo `ASISTIDO`. Y aun
concedida, pasa por `ConfirmationGateway`, así que el usuario ve el payload
exacto y tiene que decir sí.

Lo que devuelve es una `UnsignedTransaction`: un objeto para inspeccionar o
exportar. Amigocompora no almacena claves privadas, no firma y no emite. Las
capacidades `SIGN_TX` y `BROADCAST_TX` existen en el enum para que la barrera
pueda nombrarlas y negarlas, no porque haya código que las implemente.
"""

from __future__ import annotations

from dataclasses import dataclass

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.addresses import require_evm_address, shorten
from amigocompora.domain.errors import UnsupportedOperationError
from amigocompora.domain.models import Quote, UnsignedTransaction
from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import EngineKind, SwapPlanner


@dataclass(frozen=True, slots=True)
class PrepareSwap:
    """Construye el payload sin firmar del swap que describe una cotización."""

    registry: EngineRegistry
    gateway: ConfirmationGateway

    def is_available(self) -> bool:
        """Si el motor DEX activo sabe construir transacciones.

        La UI lo consulta para habilitar o deshabilitar el botón, en vez de
        ofrecer una acción que va a fallar.
        """
        engine = self.registry.active_or_none(EngineKind.DEX_QUOTES)
        return isinstance(engine, SwapPlanner)

    async def __call__(self, quote: Quote, *, recipient: str) -> UnsignedTransaction:
        # 1. Descartar primero lo que el modo no permite: construir un payload
        #    que luego se va a rechazar es trabajo y red gastados en balde.
        self.gateway.precheck(Capability.PREPARE_TX)

        destination = require_evm_address(recipient, "dirección de destino")

        engine = self.registry.active_dex()
        if not isinstance(engine, SwapPlanner):
            raise UnsupportedOperationError(
                f"el motor «{engine.manifest.name}» sólo lee precios: no construye "
                f"transacciones. Activa un motor que implemente SwapPlanner."
            )

        # 2. Construir. Sin efectos externos: sólo codifica el calldata.
        transaction = await engine.plan_swap(quote, recipient=destination)

        # 3. Y ahora sí, pedir el sí explícito, con el payload real delante.
        await self.gateway.authorize(
            Capability.PREPARE_TX,
            f"Preparar swap en {quote.venue.name}",
            details=(
                f"Entregas: {quote.amount_in}",
                f"Recibes (estimado): {quote.amount_out}",
                f"Precio de ejecución: {quote.price}",
                f"Comisión del venue: {_fee_line(quote)}",
                f"Impacto de precio: {quote.price_impact_bps}",
                f"Destino: {shorten(destination)}",
                "Amigocompora no firmará ni emitirá esta transacción.",
            ),
            transaction=transaction,
        )
        return transaction


def _fee_line(quote: Quote) -> str:
    """Comisión para el diálogo de confirmación, dejando claro si no se sabe.

    Lo que el usuario está a punto de confirmar no puede decir «0 bps» cuando la
    cifra es desconocida: eso sería afirmar que el swap no tiene comisión. Se
    dice que va dentro del importe recibido, que es lo que de verdad pasa.
    """
    if quote.fee_bps is None:
        return "no desglosada por la fuente (ya descontada de lo que recibes)"
    return str(quote.fee_bps)
