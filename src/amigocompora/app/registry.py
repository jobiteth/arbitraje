"""`EngineRegistry` — descubrimiento y sustitución en caliente de motores.

Descubre por dos vías:

- **entry points** del grupo `amigocompora.engines`: permite añadir motores
  instalando un paquete, sin tocar el núcleo.
- **registro manual** (`register`): lo que usan los tests y el arranque local.

Hay un motor activo por `EngineKind`. Sustituirlo es una operación en caliente:
se abre el nuevo **antes** de cerrar el anterior, de modo que si el nuevo falla
al abrir, el que estaba sigue funcionando y la aplicación no se queda sin motor.

Un motor de terceros roto no puede tumbar el arranque: cada entry point se carga
de forma aislada y los fallos se registran y se omiten.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points

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
    DexQuoteEngine,
    Engine,
    EngineKind,
    EngineManifest,
    EngineProvider,
    PredictionMarketEngine,
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
    """Catálogo de motores disponibles y titular del motor activo por ranura."""

    __slots__ = ("_active", "_available", "_config_resolver", "_guard", "_listeners")

    def __init__(
        self,
        guard: ModeGuard,
        config_resolver: ConfigResolver | None = None,
    ) -> None:
        self._guard = guard
        self._config_resolver: ConfigResolver = config_resolver or (lambda _manifest: {})
        self._available: dict[str, RegisteredEngine] = {}
        self._active: dict[EngineKind, Engine] = {}
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
    async def activate(self, engine_id: str) -> Engine:
        """Activa un motor en su ranura, sustituyendo al anterior si lo había.

        Orden deliberado: instanciar y abrir el nuevo primero. Si falla, el
        motor que estaba activo sigue intacto y la excepción sube sin dejar la
        ranura vacía.
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

        outgoing = self._active.get(manifest.kind)
        self._active[manifest.kind] = incoming
        if outgoing is not None:
            await _close_quietly(outgoing)

        _log.info(
            "engine.activated",
            kind=manifest.kind.value,
            engine_id=manifest.engine_id,
            replaced=outgoing is not None,
        )
        self._notify(manifest.kind, incoming)
        return incoming

    async def deactivate(self, kind: EngineKind) -> None:
        outgoing = self._active.pop(kind, None)
        if outgoing is None:
            return
        await _close_quietly(outgoing)
        _log.info("engine.deactivated", kind=kind.value)
        self._notify(kind, None)

    async def aclose(self) -> None:
        """Cierra todos los motores activos. Idempotente."""
        for kind in tuple(self._active):
            await self.deactivate(kind)

    # ---------------------------------------------------------------- uso  #
    def active(self, kind: EngineKind) -> Engine:
        """Motor activo de esa ranura, o `NoActiveEngineError`."""
        engine = self._active.get(kind)
        if engine is None:
            raise NoActiveEngineError(
                f"no hay motor activo para «{kind.label}». Selecciona uno en el panel de motores."
            )
        return engine

    def active_or_none(self, kind: EngineKind) -> Engine | None:
        return self._active.get(kind)

    # Accesores tipados por ranura. Se escriben uno a uno —y no con un
    # `active_as(kind, protocol)` genérico— porque mypy no admite pasar una
    # clase `Protocol` donde se espera `type[T]` (`type-abstract`), y la
    # alternativa sería sembrar `type: ignore` en cada punto de llamada.
    # `EngineKind` es un enum cerrado, así que la lista no crece sola.
    def active_dex(self) -> DexQuoteEngine:
        engine = self.active(EngineKind.DEX_QUOTES)
        if not isinstance(engine, DexQuoteEngine):
            raise EngineError(_wrong_slot(engine, EngineKind.DEX_QUOTES, "DexQuoteEngine"))
        return engine

    def active_prediction(self) -> PredictionMarketEngine:
        engine = self.active(EngineKind.PREDICTION_MARKETS)
        if not isinstance(engine, PredictionMarketEngine):
            raise EngineError(
                _wrong_slot(engine, EngineKind.PREDICTION_MARKETS, "PredictionMarketEngine")
            )
        return engine

    def active_advisor(self) -> AiAdvisorEngine:
        engine = self.active(EngineKind.AI_ADVISOR)
        if not isinstance(engine, AiAdvisorEngine):
            raise EngineError(_wrong_slot(engine, EngineKind.AI_ADVISOR, "AiAdvisorEngine"))
        return engine

    def active_manifest(self, kind: EngineKind) -> EngineManifest | None:
        engine = self._active.get(kind)
        return engine.manifest if engine is not None else None

    def is_active(self, engine_id: str) -> bool:
        return any(engine.manifest.engine_id == engine_id for engine in self._active.values())

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
        active = {kind.value: engine.manifest.engine_id for kind, engine in self._active.items()}
        return f"EngineRegistry(disponibles={len(self._available)}, activos={active})"


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
