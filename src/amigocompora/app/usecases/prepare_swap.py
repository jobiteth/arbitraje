"""Preparar un swap: el borrador, sin firmar y sin efectos.

Capacidad requerida: `PREPARE_TX`, que conceden `ASISTIDO` y `EJECUCIÓN`. Y aun
concedida, pasa por `ConfirmationGateway`, así que el usuario ve el payload
exacto y tiene que decir sí.

Lo que devuelve es una `PlannedTransaction` —el payload de EVM o el de Solana,
según la red del par—: un objeto para inspeccionar o exportar. Este caso de uso
**no firma ni emite**, y por eso sigue siendo el camino seguro: sirve para mirar
qué se haría sin que nada ocurra.

Firmar y emitir es otra cosa, con otro caso de uso (`ExecuteSwap`), otra
capacidad y otro modo. La separación no es de estilo: es lo que permite que
alguien use la aplicación para estudiar el mercado sin darle la llave de la
cartera.
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

    def can_build(self, quote: Quote) -> bool:
        """Si el motor que cotizó **esta** ruta sabe construir su swap.

        Una ruta de un motor que sólo cotiza —GeckoTerminal— aparece en la tabla,
        pero no tiene con qué construirse: el swap sale del motor que la observó.
        Preguntarlo por la ruta concreta, y no por la red, evita que el botón de
        firmar quede encendido y falle al pulsarlo.
        """
        try:
            self.planner_for(quote)
        except (NoActiveEngineError, UnsupportedOperationError):
            return False
        return True

    async def build(self, quote: Quote, *, recipient: str) -> PlannedTransaction:
        """Construye el payload sin firmar. **No pide confirmación.**

        Está separado de `__call__` porque el camino de ejecución necesita
        exactamente esto —el payload, del mismo motor que observó la cotización—
        y volver a escribirlo allí sería tener dos versiones del mismo paso, una
        de las cuales se quedaría atrás. Lo que **no** se comparte es el
        permiso: aquí no se comprueba ninguna capacidad, así que quien llame a
        este método tiene que haber comprobado la suya antes. Los dos llamantes
        lo hacen, y cada uno por su cuenta porque son permisos distintos.
        """
        destination = destination_for(quote, recipient)
        engine = self.planner_for(quote)
        # El motor reúne el payload y responde de que corresponde a la
        # cotización que se le pasa: es él quien detecta la deriva de precio.
        return await engine.plan_swap(quote, recipient=destination)

    async def __call__(self, quote: Quote, *, recipient: str) -> PlannedTransaction:
        # 1. Descartar primero lo que el modo no permite: construir un payload
        #    que luego se va a rechazar es trabajo y red gastados en balde.
        self.gateway.precheck(Capability.PREPARE_TX)

        destination = destination_for(quote, recipient)

        # 2. Construir. Sin efectos externos.
        transaction = await self.build(quote, recipient=recipient)

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
                "Es un borrador: no se firma ni se emite nada.",
            ),
            transaction=transaction,
        )
        return transaction

    def planner_for(self, quote: Quote) -> SwapPlanner:
        """El motor que **observó** esa cotización, de entre los que están activos.

        Es público porque el camino de ejecución necesita el **mismo** motor, y
        por la misma razón que aquí: para preguntarle a qué contrato dirige los
        swaps antes de firmar. Resolverlo por segunda vez allí, con otro criterio,
        sería tener dos respuestas a «quién construye esto» que podrían separarse
        justo entre el contraste y la firma.

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


def destination_for(quote: Quote, recipient: str) -> str:
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

    Es pública porque el camino de ejecución necesita **el mismo** destino ya
    validado —para contrastarlo contra el del payload antes de firmar— y tenerlo
    calculado dos veces por dos funciones distintas sería tener dos reglas de
    validación que podrían separarse.
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
