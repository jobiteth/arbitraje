"""Preparar un puente: el borrador, sin firmar y sin efectos.

Es al camino de puentes lo que `PrepareSwap` es al de swaps, y existe por la
misma razón: poder mirar qué se haría sin que nada ocurra. Capacidad `PREPARE_TX`,
que conceden `ASISTIDO` y `EJECUCIÓN`; firmar y emitir es otro caso de uso, otra
capacidad y otro modo.

### Por qué se resuelve el motor por `engine_id` y no por «el primero que sepa»

Es la misma regla que en los swaps y aquí pesa más. En un puente la cifra que el
usuario vio —lo que recibe en la otra red— y el payload que va a firmar tienen
que salir del **mismo** motor. Construir con otro sería cambiar de proveedor sin
decirlo, y el `plan_bridge` de otro motor volvería a cotizar contra su fuente y
compararía contra el importe de una fuente distinta: la comprobación de deriva
dejaría de significar lo que dice, y en un puente eso es la diferencia entre
llegar con lo prometido y llegar con menos.

### La red que se comprueba es la de origen

Un puente empieza donde está el dinero, así que quien tiene que saber cruzar
**desde** esa red es el motor. La de destino la comprueba la política de límites,
que para eso `BridgeIntent` declara las dos.
"""

from __future__ import annotations

from dataclasses import dataclass

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.addresses import require_evm_address, shorten
from amigocompora.domain.errors import NoActiveEngineError, UnsupportedOperationError
from amigocompora.domain.models import BridgeQuote, UnsignedTransaction
from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import CrossChainPlanner, EngineKind


@dataclass(frozen=True, slots=True)
class PrepareBridge:
    """Construye el payload sin firmar del puente que describe una cotización."""

    registry: EngineRegistry
    gateway: ConfirmationGateway

    def is_available(self, origin_chain: str | None = None) -> bool:
        """Si hay algún motor activo capaz de construir un puente.

        Con `origin_chain` responde por esa red concreta: no tiene sentido ofrecer
        el botón cuando ningún motor activo sabe salir de la red donde está el
        dinero. Sin argumento responde por la ranura entera, que es lo que la
        interfaz necesita para decidir si enseña la sección.
        """
        if origin_chain is None:
            return bool(self.registry.active_stack(EngineKind.CROSS_CHAIN))
        return bool(self.registry.bridge_planners_for(origin_chain))

    async def build(
        self, quote: BridgeQuote, *, recipient: str
    ) -> UnsignedTransaction:
        """Construye el payload sin firmar. **No pide confirmación.**

        Separado de `__call__` por la misma razón que en los swaps: el camino de
        ejecución necesita exactamente esto —el payload, del mismo motor que
        cotizó— y volver a escribirlo allí sería tener dos versiones del mismo
        paso, una de las cuales se quedaría atrás. Lo que **no** se comparte es el
        permiso: aquí no se comprueba ninguna capacidad, así que quien llame tiene
        que haber comprobado la suya antes.
        """
        destination = recipient_for(quote, recipient)
        engine = self.planner_for(quote)
        # El motor reúne el payload y responde de que corresponde a la cotización
        # que se le pasa: es él quien detecta que el importe recibido se movió.
        return await engine.plan_bridge(quote, recipient=destination)

    async def __call__(
        self, quote: BridgeQuote, *, recipient: str
    ) -> UnsignedTransaction:
        # 1. Descartar primero lo que el modo no permite: construir un payload que
        #    luego se va a rechazar es trabajo y red gastados en balde.
        self.gateway.precheck(Capability.PREPARE_TX)

        transaction = await self.build(quote, recipient=recipient)

        # 2. Y ahora sí, el sí explícito, con el payload real delante.
        await self.gateway.authorize(
            Capability.PREPARE_TX,
            f"Preparar puente por {quote.provider}",
            details=(
                f"Cruzas: {quote.pair_label}",
                f"Entregas: {quote.request.amount_in} en {quote.request.origin.chain}",
                f"Recibes (estimado): {quote.amount_out}",
                f"Mínimo garantizado: {quote.amount_out_min}",
                f"Comisión: {_fee_line(quote)}",
                f"Duración estimada: {quote.duration_label}",
                f"Recibe: {shorten(recipient_for(quote, recipient))}",
                "Es un borrador: no se firma ni se emite nada.",
            ),
            transaction=transaction,
        )
        return transaction

    def planner_for(self, quote: BridgeQuote) -> CrossChainPlanner:
        """El motor que **observó** esa cotización, de entre los que están activos.

        Público porque el camino de ejecución necesita el **mismo** motor: para
        preguntarle a qué contrato dirige los puentes antes de firmar. Resolverlo
        por segunda vez allí, con otro criterio, sería tener dos respuestas a
        «quién construye esto» que podrían separarse justo entre el contraste y la
        firma.
        """
        origin = quote.request.origin.chain
        if not self.registry.active_stack(EngineKind.CROSS_CHAIN):
            raise NoActiveEngineError(
                "no hay ningún motor de puentes activo: sin él no hay a quién "
                "pedirle el payload. Selecciona uno en el panel de motores."
            )

        for engine in self.registry.bridge_planners_for(origin):
            if engine.manifest.engine_id == quote.engine_id:
                return engine

        raise UnsupportedOperationError(
            f"la cotización la observó «{quote.engine_id}» y ese motor ya no está "
            f"activo o no construye puentes desde «{origin}». Vuelve a cotizar para "
            f"que la comparación salga de los motores activos."
        )


def recipient_for(quote: BridgeQuote, recipient: str) -> str:
    """Valida el destinatario como dirección EVM, que es lo único que hay aquí.

    Los dos motores de puente que existen construyen sobre EVM y ninguno declara
    redes que no lo sean —LI.FI y Relay cotizan `eip155`—, así que la validación
    es la de EVM y se hace con el validador del dominio, que comprueba el formato
    de verdad y no la pinta de la cadena.

    Es pública porque el camino de ejecución necesita **el mismo** destinatario ya
    validado, para contrastarlo contra el que llevan los pasos del payload antes
    de firmar. Validarlo dos veces por dos funciones distintas serían dos reglas
    que podrían separarse.

    Si algún día un motor declara una red no-EVM, esto tiene que dejar de ser una
    función y pasar a despachar por `ChainSpec.address_format`, como
    `destination_for` en los swaps. Hoy no lo es, y escribir el despacho para una
    rama que no existe sería código que nadie ejecuta.
    """
    return require_evm_address(recipient, "dirección de destino")


def _fee_line(quote: BridgeQuote) -> str:
    """Comisión para el diálogo, dejando claro cuándo no se sabe.

    Un `None` aquí no es un cero y no puede pintarse como tal: diría que el puente
    no cobra. Se dice que no se pudo desglosar y que el importe recibido ya la
    lleva dentro, que es lo que de verdad pasa.
    """
    if quote.fee_bps is None:
        return "no desglosada por la fuente (ya descontada de lo que recibes)"
    return f"{quote.fee_bps} ({quote.fee})"
