"""Varios motores activos por ranura: preferencia, respaldo y procedencia.

Lo que se prueba aquí es lo que hace posible tener **varias fuentes a la vez**
—una agregada y otra directa, por ejemplo— sin que la elección de cuál construye
un swap quede al azar. Tres propiedades, cada una con su razón:

- `activate` sustituye; `add` suma. Son dos intenciones distintas y confundirlas
  haría que elegir un motor en el panel borrase los respaldos sin avisar.
- El orden lo decide `swap_priority`, **no** el orden de activación: si fuera el
  de activación, reordenar la lista en la interfaz cambiaría por dónde se ejecuta
  una orden.
- `planners_for` sólo devuelve motores que declaran esa red, para no ofrecer un
  constructor que no llega.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.models import (
    Quote,
    TradingPair,
    UnsignedTransaction,
    Venue,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest


def _manifest(
    engine_id: str,
    *,
    chains: frozenset[str] = frozenset(),
    priority: int = 100,
) -> EngineManifest:
    """Manifiesto mínimo. Con redes declaradas hay que declarar `PREPARE_TX`."""
    capabilities = {Capability.READ_CHAIN}
    if chains:
        capabilities.add(Capability.PREPARE_TX)
    return EngineManifest(
        engine_id=engine_id,
        name=f"Motor {engine_id}",
        version="1.0.0",
        kind=EngineKind.DEX_QUOTES,
        summary="Doble de prueba.",
        capabilities=frozenset(capabilities),
        swap_chains=chains,
        swap_priority=priority,
    )


class _Reader:
    """Motor de sólo lectura: cotiza y enumera venues, no construye."""

    __slots__ = ("_manifest",)

    def __init__(self, manifest: EngineManifest) -> None:
        self._manifest = manifest

    @property
    def manifest(self) -> EngineManifest:
        return self._manifest

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        return ()

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        return ()


class _Planner(_Reader):
    """Motor que además construye, para las redes que declara."""

    __slots__ = ()

    async def plan_swap(self, quote: Quote, *, recipient: str) -> UnsignedTransaction:
        """No se llega a llamar: estas pruebas miran **quién** sería elegido."""
        raise NotImplementedError

    def expected_destination(self, chain_key: str) -> str | None:
        """Tampoco se llega a llamar: el contraste es del camino de ejecución.

        Existe porque `SwapPlanner` es `runtime_checkable` y comprueba **qué
        métodos hay**: sin él este doble dejaría de ser un planificador y las
        pruebas de prioridad pasarían a medir el conjunto vacío.
        """
        return None


@dataclass(frozen=True, slots=True)
class _Provider:
    engine: _Reader
    manifest: EngineManifest

    def create(self, config: Mapping[str, str]) -> _Reader:
        return self.engine


def _registry() -> EngineRegistry:
    return EngineRegistry(ModeGuard(OperationMode.ASSISTED))


def _register(registry: EngineRegistry, engine: _Reader) -> None:
    registry.register(_Provider(engine=engine, manifest=engine.manifest))


# --------------------------------------------------------------------------- #
# activate frente a add
# --------------------------------------------------------------------------- #
async def test_activate_deja_un_solo_motor_en_la_ranura() -> None:
    """Elegir un motor en el panel retira los demás: es lo que el usuario pidió."""
    registry = _registry()
    first = _Reader(_manifest("first"))
    second = _Reader(_manifest("second"))
    _register(registry, first)
    _register(registry, second)

    await registry.activate("first")
    await registry.activate("second")

    assert registry.active(EngineKind.DEX_QUOTES) is second
    assert registry.active_stack(EngineKind.DEX_QUOTES) == (second,)
    assert not registry.is_active("first")


async def test_add_conserva_los_que_ya_estaban() -> None:
    """Sumar un respaldo no puede retirar al titular."""
    registry = _registry()
    first = _Reader(_manifest("first"))
    second = _Reader(_manifest("second"))
    _register(registry, first)
    _register(registry, second)

    await registry.activate("first")
    await registry.add("second")

    assert registry.active_stack(EngineKind.DEX_QUOTES) == (first, second)
    assert registry.active(EngineKind.DEX_QUOTES) is first


async def test_add_no_duplica_un_motor_ya_activo() -> None:
    """Añadir dos veces el mismo deja una sola entrada y ninguna conexión suelta."""
    registry = _registry()
    engine = _Reader(_manifest("only"))
    _register(registry, engine)

    await registry.activate("only")
    await registry.add("only")

    assert registry.active_stack(EngineKind.DEX_QUOTES) == (engine,)


async def test_remove_saca_solo_el_motor_indicado() -> None:
    registry = _registry()
    first = _Reader(_manifest("first"))
    second = _Reader(_manifest("second"))
    _register(registry, first)
    _register(registry, second)

    await registry.activate("first")
    await registry.add("second")
    await registry.remove("second")

    assert registry.active_stack(EngineKind.DEX_QUOTES) == (first,)
    assert not registry.is_active("second")


# --------------------------------------------------------------------------- #
# planners_for
# --------------------------------------------------------------------------- #
async def test_planners_for_ordena_por_prioridad_no_por_activacion() -> None:
    """La preferencia está declarada, así que activar en otro orden no la cambia.

    Es la prueba de que reordenar la lista en la interfaz no altera por dónde se
    ejecuta una orden: si el orden fuera el de activación, lo alteraría.
    """
    registry = _registry()
    late = _Planner(_manifest("preferido", chains=frozenset({"ethereum"}), priority=10))
    early = _Planner(_manifest("respaldo", chains=frozenset({"ethereum"}), priority=20))
    _register(registry, late)
    _register(registry, early)

    # Se activa primero el de prioridad **peor**, a propósito.
    await registry.activate("respaldo")
    await registry.add("preferido")

    planners = registry.planners_for("ethereum")
    assert [engine.manifest.engine_id for engine in planners] == ["preferido", "respaldo"]


async def test_empate_de_prioridad_desempata_por_engine_id() -> None:
    """Con la misma prioridad el orden tiene que ser determinista, no de inserción."""
    registry = _registry()
    zeta = _Planner(_manifest("zeta", chains=frozenset({"base"})))
    alpha = _Planner(_manifest("alpha", chains=frozenset({"base"})))
    _register(registry, zeta)
    _register(registry, alpha)

    await registry.activate("zeta")
    await registry.add("alpha")

    assert [engine.manifest.engine_id for engine in registry.planners_for("base")] == [
        "alpha",
        "zeta",
    ]


async def test_planners_for_no_devuelve_motores_de_otra_red() -> None:
    """Ofrecer un constructor que no llega a esa red sería ofrecer un fallo."""
    registry = _registry()
    solana = _Planner(_manifest("solana_only", chains=frozenset({"solana"})))
    _register(registry, solana)
    await registry.activate("solana_only")

    assert registry.planners_for("solana")
    assert registry.planners_for("ethereum") == ()


async def test_planners_for_ignora_los_motores_de_solo_lectura() -> None:
    """Un motor que no declara ninguna red no construye, aunque esté activo."""
    registry = _registry()
    reader = _Reader(_manifest("reader"))
    _register(registry, reader)
    await registry.activate("reader")

    assert registry.planners_for("ethereum") == ()
