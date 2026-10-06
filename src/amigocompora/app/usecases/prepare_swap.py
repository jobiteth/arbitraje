"""Preparar un swap: el único caso de uso que produce algo con efectos.

Capacidad requerida: `PREPARE_TX`, que sólo concede el modo `ASISTIDO`. Y aun
concedida, pasa por `ConfirmationGateway`, así que el usuario ve el payload
exacto y tiene que decir sí.

Lo que devuelve es una `PlannedTransaction` —el payload de EVM o el de Solana,
según la red del par—: un objeto para inspeccionar o exportar. Amigocompora no
almacena claves privadas, no firma y no emite. Las capacidades `SIGN_TX` y
`BROADCAST_TX` existen en el enum para que la barrera pueda nombrarlas y
negarlas, no porque haya código que las implemente.
"""

from __future__ import annotations

from dataclasses import dataclass

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.addresses import require_evm_address, require_solana_address, shorten
from amigocompora.domain.chains import AddressFormat, chain
from amigocompora.domain.errors import NoActiveEngineError, UnsupportedOperationError
from amigocompora.domain.models import PlannedTransaction, Quote
from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import EngineKind, SwapPlanner


@dataclass(frozen=True, slots=True)
class PrepareSwap:
    """Construye el payload sin firmar del swap que describe una cotización."""

    registry: EngineRegistry
    gateway: ConfirmationGateway

    def is_available(self, chain_key: str | None = None) -> bool:
        """Si hay algún motor activo capaz de construir un swap.

        Con `chain_key` responde por esa red concreta, que es la pregunta que de
        verdad hace la interfaz: no tiene sentido ofrecer el botón cuando el
        motor activo no llega a la red del par que se está mirando.
        """
        if chain_key is None:
            return any(
                isinstance(engine, SwapPlanner)
                for engine in self.registry.active_stack(EngineKind.DEX_QUOTES)
            )
        return bool(self.registry.planners_for(chain_key))

    async def __call__(self, quote: Quote, *, recipient: str) -> PlannedTransaction:
        # 1. Descartar primero lo que el modo no permite: construir un payload
        #    que luego se va a rechazar es trabajo y red gastados en balde.
        self.gateway.precheck(Capability.PREPARE_TX)

        destination = _recipient_for(quote, recipient)
        engine = self._planner_for(quote)

        # 2. Construir. Sin efectos externos: el motor reúne el payload, y
        #    responde de que corresponde a la cotización que se le pasa.
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
                f"Impacto de precio: {_impact_line(quote)}",
                f"Destino: {shorten(destination)}",
                "Amigocompora no firmará ni emitirá esta transacción.",
            ),
            transaction=transaction,
        )
        return transaction

    def _planner_for(self, quote: Quote) -> SwapPlanner:
        """El motor que **observó** esa cotización, de entre los que están activos.

        Se busca por `engine_id` y no se toma «el primero que sepa construir»: la
        cifra que el usuario vio y el payload que va a firmar tienen que salir del
        mismo sitio. Construir con otro motor sería cambiar el venue sin decirlo,
        y el `plan_swap` de otro motor volvería a cotizar contra **su** fuente y
        compararía contra el precio de una fuente distinta — con lo que la
        comprobación de deriva dejaría de significar lo que dice.

        Que el motor cotizara esa red es lo que garantiza que sepa construirla:
        para llegar aquí tuvo que declararla en `swap_chains`.
        """
        if self.registry.active_or_none(EngineKind.DEX_QUOTES) is None:
            raise NoActiveEngineError(
                "no hay ningún motor DEX activo: sin él no hay a quién pedirle el "
                "payload. Selecciona uno en el panel de motores."
            )

        for engine in self.registry.planners_for(quote.pair.chain):
            if engine.manifest.engine_id == quote.engine_id:
                return engine

        raise UnsupportedOperationError(
            f"la cotización la observó «{quote.engine_id}» y ese motor ya no está "
            f"activo o no construye swaps en {quote.pair.chain}. Vuelve a cotizar "
            f"para que la comparación salga de los motores activos."
        )


def _recipient_for(quote: Quote, recipient: str) -> str:
    """Valida el destino según el formato de dirección de la red del par.

    Se despacha por lo que declara el registro de redes, no por la pinta de la
    cadena: que una dirección de Solana no empiece por `0x` es una coincidencia,
    no una regla, y basar la validación en ella funcionaría hasta que dejara de
    hacerlo.

    En Solana se usa el validador del dominio, que **decodifica** el base58 y
    exige 32 bytes, en lugar de `ChainSpec.is_valid_address`, que sólo mira la
    forma: `"z" * 44` son 44 caracteres del alfabeto y no son un pubkey. Este
    destino es lo último que se revisa antes de entregar algo que el usuario
    firmará fuera de la aplicación, así que se comprueba de verdad.
    """
    spec = chain(quote.pair.chain)
    if spec.address_format is AddressFormat.SOLANA_BASE58:
        return require_solana_address(recipient, "dirección de destino")
    return require_evm_address(recipient, "dirección de destino")


def _fee_line(quote: Quote) -> str:
    """Comisión para el diálogo de confirmación, dejando claro si no se sabe.

    Lo que el usuario está a punto de confirmar no puede decir «0 bps» cuando la
    cifra es desconocida: eso sería afirmar que el swap no tiene comisión. Se
    dice que va dentro del importe recibido, que es lo que de verdad pasa.
    """
    if quote.fee_bps is None:
        return "no desglosada por la fuente (ya descontada de lo que recibes)"
    return str(quote.fee_bps)


def _impact_line(quote: Quote) -> str:
    """Impacto de precio, con el mismo cuidado que la comisión.

    Un `None` aquí no es un cero y no puede pintarse como tal: diría que la
    orden no mueve el precio, que es justo lo que no se sabe. Se dice que la
    fuente no lo publica **y** que el importe recibido ya lo lleva dentro, que
    es lo que sí se sabe.
    """
    if quote.price_impact_bps is None:
        return "no publicado por la fuente (ya está dentro de lo que recibes)"
    return str(quote.price_impact_bps)
