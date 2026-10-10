"""El puente en la capa de aplicación: dos redes, un tope y un solo motor.

Lo que se fija aquí, y por qué cada cosa:

1. **Que la lista blanca de redes cubra las dos.** Es la propiedad que justifica
   que `BridgeIntent` exista en vez de reutilizar `ExecutionIntent`: con una sola
   red comprobada, un puente que sale de una red permitida hacia una que no lo
   está pasaría la comprobación entera — y la de destino es precisamente donde el
   dinero va a acabar. La prueba comprueba las dos direcciones: que falla por la
   red de destino, y que **no** falla cuando las dos están permitidas.
2. **Que las dos redes se comprueben por el camino de varios eslabones.** El
   `isinstance(intent, MultiChainIntent)` de `check_intent` es lo único que hace
   que esto pase, y quitarlo tiene que romper la suite.
3. **Que el motor se resuelva por `engine_id` y no por «el primero que sepa».**
   La cifra que el usuario vio y el payload que va a firmar tienen que salir del
   mismo sitio.
4. **Que la lista de tokens no cuente dos veces el mismo símbolo.** Un puente de
   USDC a USDC toca USDC en dos redes, y es un permiso, no dos.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from amigocompora.app.confirmation import ConfirmationGateway, RecordingPrompt
from amigocompora.app.execution_policy import (
    AutonomyPolicy,
    ExecutionLedger,
    MultiChainIntent,
    PrivateKeySource,
)
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.execute_bridge import BridgeIntent
from amigocompora.app.usecases.prepare_bridge import PrepareBridge, recipient_for
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    CurrencyMismatchError,
    ExecutionLimitExceededError,
    NoActiveEngineError,
    UnsupportedOperationError,
)
from amigocompora.domain.execution import ExecutionLimits, TriggerKind
from amigocompora.domain.models import (
    BridgeQuote,
    BridgeRequest,
    Measurement,
    Token,
    UnsignedTransaction,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest

AHORA = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_POLYGON = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
CONTRATO_BASE = "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae"
RECEPTOR = "0x2c887da24c6e6c939b0fe3adae50814b938b44f9"

CLAVE = "0x" + "2" * 64


class _FakeKey(PrivateKeySource):
    def available(self) -> bool:
        return True

    def require(self) -> str:
        return CLAVE


class _FakePassphrase:
    def get(self) -> str | None:
        return None


def _solicitud(**overrides: object) -> BridgeRequest:
    values: dict[str, object] = {
        "origin": Token("USDC", 6, "base", USDC_BASE),
        "destination": Token("USDC", 6, "polygon", USDC_POLYGON),
        "amount_in": TokenAmount(1_000_000, 6, "USDC"),
    }
    values.update(overrides)
    return BridgeRequest(**values)  # type: ignore[arg-type]


def _cotizacion(
    *,
    engine_id: str = "fake",
    request: BridgeRequest | None = None,
) -> BridgeQuote:
    """Una cotización de esa solicitud, con las cifras en la escala del destino.

    La escala se toma de la solicitud y no se escribe fija: `BridgeQuote` exige
    que los dos importes sean del token de destino —símbolo y decimales—, y una
    prueba que los escribiera a mano estaría describiendo un puente que el dominio
    rechaza.
    """
    peticion = request or _solicitud()
    destino = peticion.destination
    return BridgeQuote(
        engine_id=engine_id,
        provider="AcrossV4",
        request=peticion,
        amount_out=TokenAmount(989_888, destino.decimals, destino.symbol),
        amount_out_min=TokenAmount(989_888, destino.decimals, destino.symbol),
        fee=TokenAmount(10_112, 6, "USDC"),
        fee_basis=Measurement.REPORTED,
        duration_seconds=60,
        observed_at=AHORA,
    )


class _Planner:
    """Doble de `CrossChainPlanner`: declara lo que se le diga y construye igual."""

    __slots__ = ("_declara", "_manifest", "_to")

    def __init__(
        self,
        *,
        engine_id: str = "fake",
        priority: int = 50,
        chains: frozenset[str] = frozenset({"base"}),
        declares: str | None = CONTRATO_BASE,
        to_address: str = CONTRATO_BASE,
    ) -> None:
        self._manifest = EngineManifest(
            engine_id=engine_id,
            name=f"Motor falso {engine_id}",
            version="1.0.0",
            kind=EngineKind.CROSS_CHAIN,
            summary="Doble de prueba.",
            capabilities=frozenset({Capability.READ_CHAIN, Capability.PREPARE_TX}),
            bridge_chains=chains,
            bridge_priority=priority,
        )
        self._declara = declares
        self._to = to_address

    @property
    def manifest(self) -> EngineManifest:
        return self._manifest

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def quote_bridge(self, request: BridgeRequest) -> Sequence[BridgeQuote]:
        return (_cotizacion(engine_id=self._manifest.engine_id, request=request),)

    async def plan_bridge(
        self, quote: BridgeQuote, *, recipient: str
    ) -> UnsignedTransaction:
        return UnsignedTransaction(
            chain_id=8453,
            to_address=self._to,
            calldata="0x1234",
            value=TokenAmount(0, 18, "ETH"),
            description=f"puente de prueba hacia {recipient}",
        )

    def expected_destination(self, chain_key: str) -> frozenset[str] | None:
        return None if self._declara is None else frozenset({self._declara})


class _Provider:
    def __init__(self, engine: _Planner) -> None:
        self.engine = engine

    @property
    def manifest(self) -> EngineManifest:
        return self.engine.manifest

    def create(self, config: object) -> _Planner:
        del config
        return self.engine


async def _registry(mode: OperationMode, *engines: _Planner) -> EngineRegistry:
    """Un registro con esos motores en la ranura de puentes.

    El primero se **activa** y los demás se **suman**: `activate` deja al motor
    como único de su ranura —es lo que hace el panel al elegir uno—, así que
    usarlo para todos dejaría sólo al último, y una prueba que creyera estar
    comparando dos motores estaría comparando uno consigo mismo.
    """
    registry = EngineRegistry(ModeGuard(mode))
    for position, engine in enumerate(engines):
        registry.register(_Provider(engine))
        if position == 0:
            await registry.activate(engine.manifest.engine_id)
        else:
            await registry.add(engine.manifest.engine_id)
    return registry


def _gateway(mode: OperationMode = OperationMode.ASSISTED) -> ConfirmationGateway:
    return ConfirmationGateway(
        ModeGuard(mode), prompt=RecordingPrompt(answer=True), clock=FrozenClock(AHORA)
    )


def _limits(**overrides: object) -> ExecutionLimits:
    values: dict[str, object] = {
        "enabled": True,
        "max_quote_per_trade": Decimal("5000"),
        "max_quote_per_day": Decimal("20000"),
        "allowed_tokens": frozenset({"USDC", "WETH"}),
        "allowed_chains": frozenset({"base", "polygon"}),
        "allowed_engines": frozenset({"fake"}),
    }
    values.update(overrides)
    return ExecutionLimits(**values)  # type: ignore[arg-type]


def _policy(tmp_path: Path, **overrides: object) -> AutonomyPolicy:
    clock = FrozenClock(AHORA)
    return AutonomyPolicy(
        keys=_FakeKey(),
        passphrase=_FakePassphrase(),
        limits=_limits(**overrides),
        ledger=ExecutionLedger(tmp_path / "executions.jsonl", clock=clock),
        clock=clock,
        trigger=TriggerKind.MANUAL,
    )


# --------------------------------------------------------------------------- #
# El intento: dos redes, no una
# --------------------------------------------------------------------------- #
def test_el_intento_declara_las_dos_redes() -> None:
    intent = BridgeIntent(
        quote=_cotizacion(),
        recipient=RECEPTOR,
        notional=TokenAmount(1_000_000, 6, "USDC"),
        engine_id="fake",
    )
    assert intent.chains == ("base", "polygon")
    # `chain` sigue siendo la de origen: es donde se emite y donde se anota.
    assert intent.chain == "base"
    assert isinstance(intent, MultiChainIntent)


def test_la_red_de_destino_tambien_se_comprueba(tmp_path: Path) -> None:
    """Es la razón de existir de `MultiChainIntent`.

    Con la lista blanca limitada a la red de origen, este puente pasaría entero
    —y el dinero acabaría en una red que el usuario no autorizó—. La prueba mira
    **qué red** nombra el error, no sólo que falle: fallar por el origen sería
    fallar por otro motivo.
    """
    policy = _policy(tmp_path, allowed_chains=frozenset({"base"}))
    intent = BridgeIntent(
        quote=_cotizacion(),
        recipient=RECEPTOR,
        notional=TokenAmount(1_000_000, 6, "USDC"),
        engine_id="fake",
    )

    with pytest.raises(ExecutionLimitExceededError, match="polygon"):
        policy.check_intent(intent)


def test_con_las_dos_redes_permitidas_pasa(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy.check_intent(
        BridgeIntent(
            quote=_cotizacion(),
            recipient=RECEPTOR,
            notional=TokenAmount(1_000_000, 6, "USDC"),
            engine_id="fake",
        )
    )


def test_el_mismo_simbolo_en_dos_redes_es_un_token_no_dos() -> None:
    """La lista blanca es de símbolos: repetir «USDC, USDC» no comprueba más."""
    intent = BridgeIntent(
        quote=_cotizacion(),
        recipient=RECEPTOR,
        notional=TokenAmount(1_000_000, 6, "USDC"),
        engine_id="fake",
    )
    assert intent.tokens == ("USDC",)


def test_dos_tokens_distintos_se_declaran_los_dos() -> None:
    request = _solicitud(
        destination=Token("WETH", 18, "polygon", "0x" + "7" * 40),
        amount_in=TokenAmount(1_000_000, 6, "USDC"),
    )
    intent = BridgeIntent(
        quote=_cotizacion(request=request),
        recipient=RECEPTOR,
        notional=TokenAmount(1_000_000, 6, "USDC"),
        engine_id="fake",
    )
    assert intent.tokens == ("USDC", "WETH")


def test_la_etiqueta_del_asiento_nombra_las_dos_patas() -> None:
    intent = BridgeIntent(
        quote=_cotizacion(),
        recipient=RECEPTOR,
        notional=TokenAmount(1_000_000, 6, "USDC"),
        engine_id="fake",
    )
    assert intent.label == "USDC@base → USDC@polygon"


def test_lo_medido_contra_el_tope_es_la_valoracion_cuando_la_hay() -> None:
    """El importe que ve la política es el valorado, no lo entregado.

    Comparar un importe en ETH contra un tope escrito en dólares tendría el
    resultado que se merece: dejar pasar un token carísimo por debajo de un tope
    pensado para otra unidad.
    """
    intent = BridgeIntent(
        quote=_cotizacion(),
        recipient=RECEPTOR,
        notional=TokenAmount(10**18, 18, "WETH"),
        engine_id="fake",
        reference_value=TokenAmount(3_000_000_000, 6, "USDC"),
    )
    assert intent.measured == TokenAmount(3_000_000_000, 6, "USDC")
    assert intent.counts_towards_limits


def test_un_puente_de_una_sola_red_no_existe() -> None:
    """Dos tokens de la misma red se cambian con un swap, no con un puente.

    Lo dice el dominio al construir la **solicitud**, antes de pedirle nada a
    ningún motor: un puente que no cruza no tiene ruta que buscar, y descubrirlo
    después de gastar una petición sería gastarla en balde.
    """
    with pytest.raises(CurrencyMismatchError, match="se usa un swap"):
        _solicitud(destination=Token("WETH", 18, "base", "0x" + "7" * 40))


def test_un_puente_hacia_otra_red_pero_con_otro_token_vale() -> None:
    """Cruzarse a otra red con otro token es un puente, y no un error."""
    peticion = _solicitud(destination=Token("WETH", 18, "polygon", "0x" + "7" * 40))
    assert peticion.chains == ("base", "polygon")


# --------------------------------------------------------------------------- #
# El registro
# --------------------------------------------------------------------------- #
async def test_la_ranura_ordena_por_prioridad_y_no_por_activacion() -> None:
    tardio = _Planner(engine_id="tardio", priority=10)
    temprano = _Planner(engine_id="temprano", priority=90)
    registry = await _registry(OperationMode.ASSISTED, temprano, tardio)

    assert [e.manifest.engine_id for e in registry.bridge_planners()] == [
        "tardio",
        "temprano",
    ]


async def test_solo_salen_los_motores_que_cruzan_desde_esa_red() -> None:
    """Un puente empieza donde está el dinero.

    Un motor que no sabe salir de esa red no ofrece ninguna ruta, por mucho que
    sepa llegar a ella. Y se descarta **sin preguntarle**: el manifiesto ya lo
    dice, y preguntar costaría una conexión por cada motor y cada repintado.
    """
    desde_base = _Planner(engine_id="desde_base", chains=frozenset({"base"}))
    desde_solana = _Planner(engine_id="desde_solana", chains=frozenset({"solana"}))
    registry = await _registry(OperationMode.ASSISTED, desde_base, desde_solana)

    assert [e.manifest.engine_id for e in registry.bridge_planners_for("base")] == [
        "desde_base"
    ]
    assert registry.bridge_planners_for("solana")[0].manifest.engine_id == "desde_solana"
    assert registry.bridge_planners_for("arbitrum") == ()


async def test_una_ranura_vacia_lo_dice_con_su_nombre() -> None:
    registry = await _registry(OperationMode.ASSISTED)
    with pytest.raises(NoActiveEngineError, match="Puentes entre redes"):
        registry.bridge_planners()


# --------------------------------------------------------------------------- #
# Preparar
# --------------------------------------------------------------------------- #
def test_el_destinatario_tiene_que_ser_una_direccion_evm() -> None:
    assert recipient_for(_cotizacion(), RECEPTOR) == RECEPTOR
    with pytest.raises(Exception, match="dirección de destino"):
        recipient_for(_cotizacion(), "no-es-una-direccion")


async def test_se_construye_con_el_motor_que_cotizo() -> None:
    """Cambiar de motor al construir sería cambiar de proveedor sin decirlo."""
    primero = _Planner(engine_id="primero", priority=10)
    segundo = _Planner(engine_id="segundo", priority=90)
    registry = await _registry(OperationMode.ASSISTED, primero, segundo)
    prepare = PrepareBridge(registry=registry, gateway=_gateway())

    elegido = prepare.planner_for(_cotizacion(engine_id="segundo"))
    assert elegido.manifest.engine_id == "segundo"


async def test_un_motor_que_ya_no_esta_activo_no_construye() -> None:
    registry = await _registry(OperationMode.ASSISTED, _Planner(engine_id="primero"))
    prepare = PrepareBridge(registry=registry, gateway=_gateway())

    with pytest.raises(UnsupportedOperationError, match="ya no está"):
        prepare.planner_for(_cotizacion(engine_id="fantasma"))


async def test_sin_ranura_activa_no_hay_a_quien_preguntar() -> None:
    registry = await _registry(OperationMode.ASSISTED)
    prepare = PrepareBridge(registry=registry, gateway=_gateway())

    assert not prepare.is_available("base")
    with pytest.raises(NoActiveEngineError, match="panel de motores"):
        prepare.planner_for(_cotizacion())


async def test_se_prepara_el_payload_del_puente() -> None:
    registry = await _registry(OperationMode.ASSISTED, _Planner())
    prepare = PrepareBridge(registry=registry, gateway=_gateway())

    tx = await prepare(_cotizacion(), recipient=RECEPTOR)
    assert tx.to_address == CONTRATO_BASE
    assert RECEPTOR in tx.description
