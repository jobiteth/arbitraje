"""Preparar un swap: el borrador, sin firmar y sin efectos.

Capacidad requerida: `PREPARE_TX`, que conceden `ASISTIDO` y `EJECUCIÓN`. Y aun
concedida, pasa por `ConfirmationGateway`, así que el usuario ve el payload
exacto y tiene que decir sí.

Lo que devuelve es un `PreparedSwap` —el payload de EVM o el de Solana, según
la red del par, más lo que se pudo saber de su coste de red—: un objeto para
inspeccionar o exportar. Este caso de uso **no firma ni emite**, y por eso sigue
siendo el camino seguro: sirve para mirar qué se haría sin que nada ocurra.

El coste de red se estima aquí, y no al cotizar, porque es una llamada a la red
**por ruta** y ninguna se va a firmar hasta que el usuario elija: hacerlo al
cotizar convertiría cada comparación de precios en una ronda de estimaciones que
nadie pidió. Una estimación que falle **no** frustra la preparación —un borrador
no necesita coste para existir, y una transacción sin allowance revertiría ante
el nodo sin que eso diga que el swap esté mal—: se anota el motivo y se sigue.

Firmar y emitir es otra cosa, con otro caso de uso (`ExecuteSwap`), otra
capacidad y otro modo. La separación no es de estilo: es lo que permite que
alguien use la aplicación para estudiar el mercado sin darle la llave de la
cartera.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.estimate_cost import EstimateNetworkCost, NetworkCost
from amigocompora.domain.addresses import require_evm_address, require_solana_address, shorten
from amigocompora.domain.chains import AddressFormat, chain
from amigocompora.domain.errors import NoActiveEngineError, UnsupportedOperationError
from amigocompora.domain.models import PlannedTransaction, Quote, UnsignedTransaction
from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import EngineKind, SwapPlanner

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedSwap:
    """El borrador preparado, con lo que se supo de su coste de red.

    `network_cost` y `cost_error` cuentan dos desenlaces que no se pueden
    confundir con «coste cero»: la estimación salió (la cifra), o se intentó y
    no salió (el motivo). Los dos en `None` significa que no se intentó —una
    transacción de Solana, o quien llamó no dijo desde qué cartera se firmaría—,
    y eso la interfaz lo enseña como «—», no como cero.
    """

    transaction: PlannedTransaction
    network_cost: NetworkCost | None = None
    cost_error: str | None = None


@dataclass(frozen=True, slots=True)
class PrepareSwap:
    """Construye el payload sin firmar del swap que describe una cotización."""

    registry: EngineRegistry
    gateway: ConfirmationGateway
    #: Sin estimador no se estima nada y todo sigue funcionando: es lo que usan
    #: las pruebas que no van del coste, y es la razón de que el campo tenga
    #: valor por omisión en vez de ser obligatorio.
    estimate: EstimateNetworkCost | None = None

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

    async def __call__(
        self, quote: Quote, *, recipient: str, sender: str | None = None
    ) -> PreparedSwap:
        # 1. Descartar primero lo que el modo no permite: construir un payload
        #    que luego se va a rechazar es trabajo y red gastados en balde.
        self.gateway.precheck(Capability.PREPARE_TX)

        destination = destination_for(quote, recipient)

        # 2. Construir. Sin efectos externos.
        transaction = await self.build(quote, recipient=recipient)

        # 2b. El coste de red, con el payload ya construido: la estimación
        #     necesita el calldata exacto, así que no puede ir antes. Y no
        #     frustra la preparación si falla —ver `_estimate_cost`—.
        coste, cost_error = await self._estimate_cost(transaction, quote, sender)

        # 3. Y ahora sí, pedir el sí explícito, con el payload real delante.
        details = [
            f"Entregas: {quote.amount_in}",
            f"Recibes (estimado): {quote.amount_out}",
            f"Precio de ejecución: {quote.price}",
            f"Comisión del venue: {_fee_line(quote)}",
            f"Impacto de precio: {_impact_line(quote)}",
        ]
        if coste is not None:
            details.append(f"Coste de red: ≈ {coste.native} (estimación de gas)")
        elif cost_error is not None:
            # Decir un cero sería afirmar que es gratis, que es justo lo que no
            # se sabe. Se dice lo que pasa y cuándo se sabrá.
            details.append(
                "Coste de red: la red no pudo estimarlo ahora mismo; se "
                "reintenta al emitir."
            )
        details += [
            f"Destino: {shorten(destination)}",
            "Es un borrador: no se firma ni se emite nada.",
        ]
        await self.gateway.authorize(
            Capability.PREPARE_TX,
            f"Preparar swap en {quote.venue.name}",
            details=tuple(details),
            transaction=transaction,
        )
        return PreparedSwap(
            transaction=transaction, network_cost=coste, cost_error=cost_error
        )

    async def _estimate_cost(
        self, transaction: PlannedTransaction, quote: Quote, sender: str | None
    ) -> tuple[NetworkCost | None, str | None]:
        """El coste de red, o el motivo por el que no lo hay. Nunca lanza.

        Se estima **desde la cartera que firmaría** (`sender`), no desde el
        destino: el gas depende de quién envía —un swap sin allowance revierte
        y el nodo lo dice—. Por eso sin `sender` no se intenta: una cifra
        medida desde otra dirección sería la de otra transacción.

        Un fallo aquí no puede tumbar la preparación. Un borrador sirve para
        mirar qué se haría aunque la red no conteste, y una transacción que hoy
        revertiría —por ejemplo, porque el permiso del token aún no está dado—
        es información para el usuario, no un motivo para no construir. El
        motivo se guarda para poder enseñarlo en el panel.
        """
        if self.estimate is None or sender is None:
            return None, None
        if not isinstance(transaction, UnsignedTransaction):
            # Una transacción de Solana no tiene `eth_estimateGas` que
            # preguntar: no se intenta, y la interfaz lo enseña como «—».
            return None, None
        try:
            coste = await self.estimate(
                transaction, quote.pair.chain, from_address=sender
            )
        except Exception as error:
            _log.warning(
                "prepare_swap.cost_unavailable",
                chain=quote.pair.chain,
                engine=quote.engine_id,
                error=str(error)[:200],
            )
            return None, str(error)
        return coste, None

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
