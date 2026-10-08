"""`EngineRegistry` — descubrimiento y sustitución en caliente de motores.

Descubre por dos vías:

- **entry points** del grupo `amigocompora.engines`: permite añadir motores
  instalando un paquete, sin tocar el núcleo.
- **registro manual** (`register`): lo que usan los tests y el arranque local.

Hay **varios motores activos por `EngineKind`, en orden de preferencia**. El
primero es el titular de la ranura; los siguientes son respaldo y se usan cuando
el primero no llega a una red o no puede responder.

Que sean varios y no uno es lo que permite las dos cosas que se piden a la vez:
**comparar** —dos fuentes cotizando la misma red dan dos precios, y la mejor
ejecución sale de tenerlas las dos— y **no detenerse** —si una fuente se cae o
se agota su cuota, la siguiente de la lista construye el swap sin que el usuario
note nada más que una espera—.

Añadir un motor a la ranura es una operación en caliente: se abre el nuevo
**antes** de publicarlo, de modo que si falla al abrir, los que estaban siguen
funcionando y la aplicación no se queda sin ninguno.

Un motor de terceros roto no puede tumbar el arranque: cada entry point se carga
de forma aislada y los fallos se registran y se omiten.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import cast

import structlog

from amigocompora.app.mode_guard import ModeGuard
from amigocompora.domain.errors import (
    EngineError,
    EngineNotFoundError,
    NoActiveEngineError,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import (
    ENTRY_POINT_GROUP,
    AiAdvisorEngine,
    ConfigResolver,
    CrossChainPlanner,
    DexQuoteEngine,
    Engine,
    EngineKind,
    EngineManifest,
    EngineProvider,
    PredictionMarketEngine,
    PredictionOrderPlanner,
    PredictionRedeemer,
    SwapPlanner,
    validate_config,
)

_log = structlog.get_logger(__name__)

#: Observador de cambio de motor activo. La UI se suscribe para repoblar sus
#: vistas tras un swap, sin que esta capa conozca Qt.
EngineListener = Callable[[EngineKind, Engine | None], None]


@dataclass(frozen=True, slots=True)
class RegisteredEngine:
    """Un motor instalado y disponible, todavía sin instanciar."""

    provider: EngineProvider
    #: De dónde salió: `entry-point:<nombre>` o `manual`. Se muestra en la UI
    #: para que el usuario sepa qué código está ejecutando y de dónde viene.
    source: str

    @property
    def manifest(self) -> EngineManifest:
        return self.provider.manifest

    @property
    def engine_id(self) -> str:
        return self.manifest.engine_id

    def blocked_capabilities(self, guard: ModeGuard) -> frozenset[Capability]:
        """Capacidades del motor que el modo activo no concede.

        No impide activarlo: un motor DEX que además sepa preparar
        transacciones debe poder cotizar precios en `OBSERVACIÓN`.
        """
        return frozenset(
            capability for capability in self.manifest.capabilities if not guard.allows(capability)
        )


class EngineRegistry:
    """Catálogo de motores disponibles y titular de los motores activos por ranura."""

    __slots__ = ("_active", "_available", "_config_resolver", "_guard", "_listeners")

    def __init__(
        self,
        guard: ModeGuard,
        config_resolver: ConfigResolver | None = None,
    ) -> None:
        self._guard = guard
        self._config_resolver: ConfigResolver = config_resolver or (lambda _manifest: {})
        self._available: dict[str, RegisteredEngine] = {}
        #: Una **lista** por ranura, no un motor suelto: el primero es el
        #: preferido y los demás son respaldo. Ver el docstring del módulo.
        self._active: dict[EngineKind, list[Engine]] = {}
        self._listeners: list[EngineListener] = []

    # ------------------------------------------------------ descubrimiento #
    def register(self, provider: EngineProvider, *, source: str = "manual") -> RegisteredEngine:
        """Añade un motor al catálogo. Registrar el mismo id dos veces lo sustituye."""
        entry = RegisteredEngine(provider=provider, source=source)
        self._available[entry.engine_id] = entry
        return entry

    def discover(self) -> tuple[RegisteredEngine, ...]:
        """Carga los motores publicados en los entry points.

        Cada uno se carga de forma aislada: un paquete de terceros roto se
        registra en el log y se omite, pero no impide arrancar.
        """
        found: list[RegisteredEngine] = []
        for entry_point in entry_points(group=ENTRY_POINT_GROUP):
            try:
                provider = entry_point.load()
                if not isinstance(provider, EngineProvider):
                    raise TypeError(  # noqa: TRY301
                        f"{entry_point.value} no cumple el protocolo EngineProvider"
                    )
                found.append(self.register(provider, source=f"entry-point:{entry_point.name}"))
            except Exception:
                _log.exception(
                    "engine.discovery_failed",
                    entry_point=entry_point.name,
                    target=entry_point.value,
                )
        _log.info("engine.discovered", count=len(found))
        return tuple(found)

    # ------------------------------------------------------------ catálogo #
    def available(self, kind: EngineKind | None = None) -> tuple[RegisteredEngine, ...]:
        entries = self._available.values()
        if kind is not None:
            entries = [entry for entry in entries if entry.manifest.kind == kind]  # type: ignore[assignment]
        return tuple(sorted(entries, key=lambda entry: entry.manifest.name))

    def kinds_available(self) -> frozenset[EngineKind]:
        return frozenset(entry.manifest.kind for entry in self._available.values())

    def lookup(self, engine_id: str) -> RegisteredEngine:
        try:
            return self._available[engine_id]
        except KeyError:
            known = ", ".join(sorted(self._available)) or "ninguno"
            raise EngineNotFoundError(
                f"no hay motor registrado con id «{engine_id}»; disponibles: {known}"
            ) from None

    # --------------------------------------------------------- activación  #
    async def _open(self, engine_id: str) -> Engine:
        """Instancia y abre un motor **sin publicarlo** todavía.

        Existe separada de `activate` y `add` porque las dos necesitan lo mismo
        —abrir antes de publicar— y ese orden es la garantía de que un motor que
        no arranca deja la ranura exactamente como estaba.
        """
        entry = self.lookup(engine_id)
        manifest = entry.manifest
        config = self._config_resolver(manifest)
        validate_config(manifest, config)

        incoming = entry.provider.create(config)
        try:
            await incoming.aopen()
        except Exception as error:
            await _close_quietly(incoming)
            raise EngineError(f"el motor «{manifest.name}» no pudo arrancar: {error}") from error
        return incoming

    async def activate(self, engine_id: str) -> Engine:
        """Deja ese motor como **único** activo de su ranura.

        Es la operación del panel de motores: elegir uno retira los que hubiera.
        Para sumar un motor conservando los demás, ver `add`.
        """
        incoming = await self._open(engine_id)
        kind = incoming.manifest.kind
        outgoing = self._active.get(kind, [])

        self._active[kind] = [incoming]
        for engine in outgoing:
            await _close_quietly(engine)

        _log.info(
            "engine.activated",
            kind=kind.value,
            engine_id=incoming.manifest.engine_id,
            replaced=len(outgoing),
        )
        self._notify(kind, incoming)
        return incoming

    async def add(self, engine_id: str) -> Engine:
        """Suma un motor a su ranura **sin retirar** los que ya estaban.

        El nuevo entra el último de la lista, pero eso **no** lo deja en segundo
        lugar: la preferencia la decide `swap_priority` del manifiesto, no el
        orden de llegada. Así, añadir un respaldo no cambia quién responde
        primero, que es lo que pasaría si el orden de la lista fuera la
        preferencia.
        """
        incoming = await self._open(engine_id)
        kind = incoming.manifest.kind
        stack = self._active.setdefault(kind, [])

        if any(engine.manifest.engine_id == incoming.manifest.engine_id for engine in stack):
            # Ya estaba activo. Se cierra el recién abierto: dejarlo sin
            # publicar sería una conexión viva que nadie va a usar ni cerrar.
            await _close_quietly(incoming)
            return incoming

        stack.append(incoming)
        _log.info(
            "engine.added",
            kind=kind.value,
            engine_id=incoming.manifest.engine_id,
            # La clave **no** se llama `stack`: structlog la reserva para el
            # traceback y espera texto, así que un entero ahí rompe el log.
            stack_size=len(stack),
        )
        self._notify(kind, incoming)
        return incoming

    async def remove(self, engine_id: str) -> None:
        """Retira un motor concreto de su ranura, si estaba activo."""
        for kind, stack in tuple(self._active.items()):
            found = next(
                (engine for engine in stack if engine.manifest.engine_id == engine_id),
                None,
            )
            if found is None:
                continue
            stack.remove(found)
            await _close_quietly(found)
            if not stack:
                del self._active[kind]
            _log.info("engine.removed", kind=kind.value, engine_id=engine_id)
            self._notify(kind, stack[0] if stack else None)
            return

    async def deactivate(self, kind: EngineKind) -> None:
        outgoing = self._active.pop(kind, [])
        if not outgoing:
            return
        for engine in outgoing:
            await _close_quietly(engine)
        _log.info("engine.deactivated", kind=kind.value, count=len(outgoing))
        self._notify(kind, None)

    async def aclose(self) -> None:
        """Cierra todos los motores activos. Idempotente."""
        for kind in tuple(self._active):
            await self.deactivate(kind)

    # ---------------------------------------------------------------- uso  #
    def active(self, kind: EngineKind) -> Engine:
        """Motor **preferido** de esa ranura, o `NoActiveEngineError`."""
        stack = self._active.get(kind)
        if not stack:
            raise NoActiveEngineError(
                f"no hay motor activo para «{kind.label}». Selecciona uno en el panel de motores."
            )
        return stack[0]

    def active_stack(self, kind: EngineKind) -> tuple[Engine, ...]:
        """Todos los motores activos de la ranura, el preferido primero."""
        return tuple(self._active.get(kind, ()))

    def planners_for(self, chain_key: str) -> tuple[SwapPlanner, ...]:
        """Motores activos capaces de construir un swap **en esa red**, en orden.

        Se filtra por lo que el manifiesto declara (`swap_chains`) y no
        preguntándole a cada motor, porque hay que saber quién puede antes de
        usarlo y el manifiesto ya lo responde sin gastar una conexión.

        Devolver **varios** y no el mejor es deliberado: quien llama los recorre
        y pasa al siguiente cuando uno no responde. Es lo que hace que una fuente
        caída no detenga la operación. Ver `app.usecases.prepare_swap`.

        El orden es por `swap_priority` y, en caso de empate, por `engine_id`:
        determinista, y sin depender del orden en que se activaron los motores.
        """
        candidates = [
            engine
            for engine in self._active.get(EngineKind.DEX_QUOTES, ())
            if chain_key in engine.manifest.swap_chains and isinstance(engine, SwapPlanner)
        ]
        candidates.sort(
            key=lambda engine: (engine.manifest.swap_priority, engine.manifest.engine_id)
        )
        return tuple(candidates)

    def bridge_planners_for(self, origin_chain: str) -> tuple[CrossChainPlanner, ...]:
        """Motores activos capaces de cruzar **desde esa red**, en orden.

        El filtro es la red de **origen** y no la de destino: un puente empieza
        donde está el dinero, y una red de la que no se puede partir no ofrece
        ninguna ruta por mucho que se pueda llegar a ella. Se filtra por lo que
        el manifiesto declara (`bridge_chains`), como en `planners_for`, para
        saber quién puede antes de gastar una conexión.

        Devolver **varios** y no el mejor es lo que hace que la mejor ruta la
        decida el importe recibido y no el orden en que se activaron los motores:
        quien llama los recorre a todos, y `rank_bridges` ordena después por lo
        que cada uno entrega. El orden de aquí sólo desempata.

        El orden es por `bridge_priority` y, a falta de eso, por `engine_id`:
        determinista, y sin depender del orden de activación.
        """
        candidates = [
            engine
            for engine in self._active.get(EngineKind.CROSS_CHAIN, ())
            if origin_chain in engine.manifest.bridge_chains
            and isinstance(engine, CrossChainPlanner)
        ]
        candidates.sort(key=_bridge_order)
        return tuple(candidates)

    def active_or_none(self, kind: EngineKind) -> Engine | None:
        stack = self._active.get(kind)
        return stack[0] if stack else None
    # Accesores tipados por ranura. Se escriben uno a uno —y no con un
    # `active_as(kind, protocol)` genérico— porque mypy no admite pasar una
    # clase `Protocol` donde se espera `type[T]` (`type-abstract`), y la
    # alternativa sería sembrar `type: ignore` en cada punto de llamada.
    # `EngineKind` es un enum cerrado, así que la lista no crece sola.
    def active_dex_stack(self) -> tuple[DexQuoteEngine, ...]:
        """Toda la ranura DEX, el preferido primero, o error si está vacía.

        Devuelve **todas** y no sólo la preferida porque hay una operación que
        las quiere todas: comparar precios. Cotizar con una sola daría una tabla
        con las filas de un único motor y ninguna comparación; el sentido de
        tener varios activos es que sus cifras se vean juntas y gane la mejor.
        Quien sólo necesite la titular —el caso habitual— la tiene en `[0]`.

        Se valida el tipo de **todas** antes de devolver ninguna: si una entrada
        de la ranura no cumpliera el protocolo, quien llama recibiría una tupla
        a medias y fallaría más tarde, lejos de la causa.
        """
        stack = self.active_stack(EngineKind.DEX_QUOTES)
        for engine in stack:
            if not isinstance(engine, DexQuoteEngine):
                raise EngineError(_wrong_slot(engine, EngineKind.DEX_QUOTES, "DexQuoteEngine"))
        if not stack:
            raise NoActiveEngineError(
                f"no hay motor activo para «{EngineKind.DEX_QUOTES.label}». "
                f"Selecciona uno en el panel de motores."
            )
        return cast("tuple[DexQuoteEngine, ...]", stack)

    def bridge_planners(self) -> tuple[CrossChainPlanner, ...]:
        """Toda la ranura de puentes, la preferida primero, o error si está vacía.

        Devuelve **todas** y no sólo la preferida por la misma razón que la ranura
        DEX: aquí lo que se pide es comparar rutas, y una lista con las rutas de
        un solo motor no compara nada. Quien sólo necesite la titular la tiene en
        `[0]`.

        A diferencia de `bridge_planners_for`, no filtra por red: pregunta quién
        sabe cruzar, en general. Sirve para enseñar la ranura y para saber si
        hay algún motor de puentes activo antes de ofrecer la pestaña.

        El orden es el mismo que el de `bridge_planners_for` —por
        `bridge_priority` y, a falta de eso, por `engine_id`—, y se ordena con la
        **misma** función: dos órdenes escritos por separado podrían discrepar, y
        entonces la lista que se enseña y la lista que se recorre no coincidirían.
        """
        stack = self.active_stack(EngineKind.CROSS_CHAIN)
        for engine in stack:
            if not isinstance(engine, CrossChainPlanner):
                raise EngineError(
                    _wrong_slot(engine, EngineKind.CROSS_CHAIN, "CrossChainPlanner")
                )
        if not stack:
            raise NoActiveEngineError(
                f"no hay motor activo para «{EngineKind.CROSS_CHAIN.label}». "
                f"Selecciona uno en el panel de motores."
            )
        return tuple(sorted(cast("tuple[CrossChainPlanner, ...]", stack), key=_bridge_order))

    def active_prediction(self) -> PredictionMarketEngine:
        engine = self.active(EngineKind.PREDICTION_MARKETS)
        if not isinstance(engine, PredictionMarketEngine):
            raise EngineError(
                _wrong_slot(engine, EngineKind.PREDICTION_MARKETS, "PredictionMarketEngine")
            )
        return engine

    def prediction_planner(self) -> PredictionOrderPlanner:
        """El motor de predicción activo, **exigiendo** que sepa operar.

        Es un accesor distinto de `active_prediction` y no una comprobación
        dentro de él porque son dos preguntas distintas: «¿quién lee mercados?»
        la responde cualquier motor de predicción, y «¿quién puede firmar una
        orden?» sólo la responde uno que implemente el protocolo de escritura.
        Mezclarlas haría que leer un mercado fallara por un método que leer no
        necesita.

        Se comprueba el tipo aquí y no se deja al caso de uso porque el fallo
        tiene nombre propio —el motor activo no sabe operar— y decirlo aquí es
        lo que evita que el usuario reciba un `AttributeError` sobre un método
        que su motor nunca tuvo.
        """
        engine = self.active(EngineKind.PREDICTION_MARKETS)
        if not isinstance(engine, PredictionOrderPlanner):
            raise EngineError(
                f"el motor «{engine.manifest.engine_id}» lee mercados de "
                f"predicción pero no sabe construir ni publicar órdenes: activa "
                f"uno que sí, o usa la pestaña sólo para mirar."
            )
        return engine

    def prediction_redeemer(self) -> PredictionRedeemer:
        """El motor de predicción activo, **exigiendo** que sepa cobrar.

        Tercer accesor del mismo motor, y por la misma razón que el segundo: leer
        lo que se tiene, construir una orden y cobrar una posición resuelta son
        tres preguntas distintas, y cada una la responde un protocolo distinto.
        Un motor que sólo lee mercados no tiene por qué saber firmar, y uno que
        sepa firmar órdenes no tiene por qué saber cobrar.

        Se comprueba el tipo aquí y no se deja al caso de uso porque el fallo
        tiene nombre propio —el motor activo no sabe cobrar— y decirlo aquí es lo
        que evita que el usuario reciba un `AttributeError` sobre un método que su
        motor nunca tuvo.
        """
        engine = self.active(EngineKind.PREDICTION_MARKETS)
        if not isinstance(engine, PredictionRedeemer):
            raise EngineError(
                f"el motor «{engine.manifest.engine_id}» lee mercados de "
                f"predicción pero no sabe cobrar una posición ya resuelta: activa "
                f"uno que sí, o usa la pestaña sólo para mirar."
            )
        return engine

    def active_advisor(self) -> AiAdvisorEngine:
        engine = self.active(EngineKind.AI_ADVISOR)
        if not isinstance(engine, AiAdvisorEngine):
            raise EngineError(_wrong_slot(engine, EngineKind.AI_ADVISOR, "AiAdvisorEngine"))
        return engine

    def active_manifest(self, kind: EngineKind) -> EngineManifest | None:
        stack = self._active.get(kind)
        return stack[0].manifest if stack else None

    def is_active(self, engine_id: str) -> bool:
        return any(
            engine.manifest.engine_id == engine_id
            for stack in self._active.values()
            for engine in stack
        )

    # --------------------------------------------------------- observación #
    def subscribe(self, listener: EngineListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self, kind: EngineKind, engine: Engine | None) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(kind, engine)
            except Exception:
                # Un observador roto (una vista a medio construir) no debe
                # abortar un swap que ya se ha completado.
                _log.exception("engine.listener_failed", kind=kind.value)

    def __repr__(self) -> str:
        active = {
            kind.value: [engine.manifest.engine_id for engine in stack]
            for kind, stack in self._active.items()
        }
        return f"EngineRegistry(disponibles={len(self._available)}, activos={active})"


def _bridge_order(engine: CrossChainPlanner) -> tuple[int, str]:
    """El orden de la ranura de puentes: prioridad y, a falta de ella, identidad.

    Se escribe una vez y la usan los dos accesores. Dos órdenes escritos por
    separado podrían discrepar, y entonces la lista que la interfaz enseña y la
    lista que el caso de uso recorre no coincidirían — con lo que «la mejor ruta
    primero» dejaría de ser cierto justo en el sitio donde se elige.

    Y no es el orden de activación a propósito: `add` mete al motor nuevo el
    último de la lista, así que ordenar por posición haría que añadir un respaldo
    cambiara quién responde primero.
    """
    return (engine.manifest.bridge_priority, engine.manifest.engine_id)


def _wrong_slot(engine: Engine, kind: EngineKind, protocol_name: str) -> str:
    return (
        f"el motor «{engine.manifest.name}» está registrado en la ranura "
        f"«{kind.label}» pero no implementa {protocol_name}"
    )


async def _close_quietly(engine: Engine) -> None:
    """Cierra un motor sin propagar fallos.

    `aclose()` se documenta como «no debe lanzar», pero un motor de terceros
    puede incumplirlo y no queremos que eso rompa un swap ni el cierre de la app.
    """
    try:
        await engine.aclose()
    except Exception:
        _log.exception("engine.close_failed", engine_id=engine.manifest.engine_id)


def sole_engine_for(entries: Sequence[RegisteredEngine], kind: EngineKind) -> str | None:
    """Id del único motor de esa ranura, si hay exactamente uno.

    Lo usa el arranque para autoactivar cuando no hay ambigüedad, en vez de
    dejar al usuario ante una app vacía.
    """
    matching = [entry for entry in entries if entry.manifest.kind == kind]
    return matching[0].engine_id if len(matching) == 1 else None
