"""Raíz de composición: construye el grafo de objetos de la aplicación.

Inyección de dependencias a mano, sin framework. El grafo es pequeño y
explícito se lee mejor: este fichero es el único sitio donde hay que mirar para
saber qué está conectado con qué.

Es también la única frontera donde `app` y `infra` se encuentran. Ni los casos
de uso ni el registro saben que existen `keyring`, `httpx` o ficheros TOML.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Self

import structlog

from amigocompora.app.alerts import AlertCenter, AlertKind, severity_for_bps
from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry, sole_engine_for
from amigocompora.app.scheduler import Scheduler
from amigocompora.app.usecases.analyze_prediction_market import AnalyzePredictionMarkets
from amigocompora.app.usecases.analyze_with_ai import AnalyzeWithAi
from amigocompora.app.usecases.compare_prices import ComparePrices
from amigocompora.app.usecases.prepare_swap import PrepareSwap
from amigocompora.app.usecases.scan_opportunities import ScanOpportunities
from amigocompora.app.usecases.watch_scan import WatchedPair, WatchScan
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.models import Opportunity, TradingPair
from amigocompora.domain.protocols import EngineKind
from amigocompora.infra.config import Settings, load_settings
from amigocompora.infra.logging import configure_logging
from amigocompora.infra.secrets import (
    KeyringSecretStore,
    SecretStore,
    build_config_resolver,
)

_log = structlog.get_logger(__name__)

#: Motor que se activa en cada ranura cuando el usuario no ha elegido ninguno.
#:
#: Vive aquí, en la raíz de composición, y no en el registro: elegir un motor
#: concreto es una decisión de producto sobre qué debe ver el usuario al abrir
#: la aplicación por primera vez, no una regla del mecanismo de motores. El
#: registro sigue sin conocer ningún motor por su nombre.
#:
#: Se escogen los de mayor cobertura: `geckoterminal` cotiza todas las
#: versiones de protocolo, así que la primera tabla que ve el usuario sale
#: poblada. Puede cambiarlo en el panel de motores o fijarlo en `config.toml`.
DEFAULT_ENGINE_IDS: Final[Mapping[str, str]] = {
    EngineKind.DEX_QUOTES.value: "geckoterminal",
    EngineKind.PREDICTION_MARKETS.value: "polymarket",
    # Asistente offline por defecto: la ranura de IA queda utilizable sin
    # configurar ninguna clave. El usuario puede cambiar a Claude/DeepSeek/ChatGPT
    # desde el panel de motores o fijarlo en config.toml.
    EngineKind.AI_ADVISOR.value: "stub_advisor",
}


@dataclass(slots=True)
class Container:
    """Todo lo que la UI necesita, ya cableado."""

    settings: Settings
    clock: Clock
    guard: ModeGuard
    gateway: ConfirmationGateway
    registry: EngineRegistry
    compare_prices: ComparePrices
    scan_opportunities: ScanOpportunities
    analyze_markets: AnalyzePredictionMarkets
    prepare_swap: PrepareSwap
    analyze_with_ai: AnalyzeWithAi
    alert_center: AlertCenter
    scheduler: Scheduler
    watch_scan: WatchScan

    async def aclose(self) -> None:
        """Cierra scheduler y motores. La UI lo llama al salir."""
        try:
            await self.scheduler.stop()
        finally:
            await self.registry.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


async def build_container(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    secret_store: SecretStore | None = None,
    discover: bool = True,
    configure_logs: bool = True,
) -> Container:
    """Construye y arranca la aplicación sin UI.

    Útil también para tests de integración y para un futuro modo CLI: todo lo
    que no es Qt se puede ejercitar desde aquí.
    """
    effective_settings = settings or load_settings()
    if configure_logs:
        configure_logging(
            level=effective_settings.log_level,
            as_json=effective_settings.log_json,
        )

    effective_clock = clock or SystemClock()
    guard = ModeGuard(effective_settings.mode)
    # El prompt real (el diálogo Qt) se conecta después, al construir la
    # ventana. Hasta entonces rige `DenyAllPrompt`.
    gateway = ConfirmationGateway(guard, clock=effective_clock)

    resolver = build_config_resolver(secret_store or KeyringSecretStore())
    registry = EngineRegistry(guard, resolver)

    if discover:
        registry.discover()
        await _activate_configured_engines(registry, effective_settings)

    compare_prices = ComparePrices(registry=registry, gateway=gateway)
    scan_opportunities = ScanOpportunities(
        compare_prices=compare_prices,
        gateway=gateway,
        clock=effective_clock,
    )
    alert_center = AlertCenter(clock=effective_clock)
    scheduler = Scheduler(guard=guard, clock=effective_clock)
    watch_scan = WatchScan(scan=scan_opportunities, gateway=gateway)
    watch_scan.set_pairs(_watched_pairs(effective_settings))
    # Publicar oportunidades del barrido periódico como alertas.
    watch_scan.subscribe(lambda opps: _publish_opportunities(alert_center, opps))
    return Container(
        settings=effective_settings,
        clock=effective_clock,
        guard=guard,
        gateway=gateway,
        registry=registry,
        compare_prices=compare_prices,
        scan_opportunities=scan_opportunities,
        analyze_markets=AnalyzePredictionMarkets(registry=registry, gateway=gateway),
        prepare_swap=PrepareSwap(registry=registry, gateway=gateway),
        analyze_with_ai=AnalyzeWithAi(registry=registry, gateway=gateway),
        alert_center=alert_center,
        scheduler=scheduler,
        watch_scan=watch_scan,
    )


def _watched_pairs(settings: Settings) -> list[WatchedPair]:
    """Resuelve los pares vigilados de la config contra el catálogo de tokens.

    Un par cuya base o quote no estén en el catálogo se omite con un aviso: la
    configuración puede quedar desactualizada y la app debe abrir igual, no
    morir por una entrada que no resuelve.
    """
    from amigocompora.domain.money import BasisPoints
    from amigocompora.engines import catalog

    resolved: list[WatchedPair] = []
    for entry in settings.watch_pairs:
        base = catalog.token_by_symbol(entry.base, entry.chain)
        quote = catalog.token_by_symbol(entry.quote, entry.chain)
        if base is None or quote is None:
            _log.warning(
                "watch_pair.unknown_token",
                chain=entry.chain,
                base=entry.base,
                quote=entry.quote,
            )
            continue
        try:
            pair = TradingPair(base=base, quote=quote)
            amount = base.amount(entry.amount)
        except Exception as error:
            _log.warning(
                "watch_pair.invalid",
                pair=f"{entry.base}/{entry.quote}",
                reason=str(error),
            )
            continue
        resolved.append(
            WatchedPair(
                pair=pair,
                amount_in=amount,
                min_net_bps=BasisPoints(entry.min_net_bps),
            )
        )
    return resolved


def _publish_opportunities(
    alert_center: AlertCenter,
    opportunities: tuple[Opportunity, ...],
) -> None:
    """Traduce oportunidades del barrido en alertas deduplicadas."""
    for opportunity in opportunities:
        alert_center.publish(
            kind=AlertKind.OPPORTUNITY,
            severity=severity_for_bps(opportunity.net_spread_bps.value),
            title=f"{opportunity.pair.symbol}: {opportunity.net_spread_bps} neto",
            detail=(
                f"{opportunity.best.venue.name} vs {opportunity.reference.venue.name} · "
                f"{opportunity.best.amount_out} vs {opportunity.reference.amount_out}"
            ),
            dedup_key=(
                f"opp:{opportunity.pair.symbol}:{opportunity.best.venue.venue_id}:"
                f"{opportunity.reference.venue.venue_id}"
            ),
        )


async def _activate_configured_engines(registry: EngineRegistry, settings: Settings) -> None:
    """Activa un motor en cada ranura: el configurado, el preferido, o el único.

    El orden importa y es, de más a menos específico: lo que el usuario haya
    puesto en `config.toml` gana siempre; si no ha puesto nada, el preferido de
    la ranura, pero sólo si está instalado; y si tampoco, el único motor
    disponible de esa clase, cuando no haya ambigüedad.

    Un fallo al activar **no** aborta el arranque: la aplicación debe abrir y
    dejar que el usuario elija otro motor en el panel, no morir en el `main`.
    """
    catalog = registry.available()
    installed = {manifest.engine_id for manifest in catalog}
    for kind in EngineKind:
        preferred = DEFAULT_ENGINE_IDS.get(kind.value)
        engine_id = (
            settings.active_engines.get(kind.value)
            or (preferred if preferred in installed else None)
            or sole_engine_for(catalog, kind)
        )
        if engine_id is None:
            _log.info("engine.slot_empty", kind=kind.value)
            continue
        try:
            await registry.activate(engine_id)
        except Exception as error:
            _log.warning(
                "engine.autostart_failed",
                kind=kind.value,
                engine_id=engine_id,
                reason=str(error),
            )
