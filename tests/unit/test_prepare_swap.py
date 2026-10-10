"""Preparar un swap: el coste de red que se enseña antes de confirmar.

Lo que se fija aquí es el reparto de responsabilidades de la estimación, que es
donde puede perderse la verdad de la cifra:

1. **La cifra sale en el diálogo y en el resultado**, con las dos cifras que la
   sostienen —límite y tarifa máxima— y no con un cero cuando no se sabe.
2. **Se estima desde la cartera que firmaría**, no desde el destino: el gas
   depende de quién envía, y estimar desde otra dirección mediría otra
   transacción. Aquí el destino y la cartera son direcciones **distintas** a
   propósito, para que la prueba distinga cuál se usó.
3. **Un fallo de estimación no frustra la preparación.** Un revert —por
   ejemplo, sin allowance— o una red sin nodo configurado se anotan como
   motivo y el borrador sale igual: mirar qué se haría no requiere que la red
   conteste.
4. **Sin cartera de firma no se intenta**, y sin intento no hay motivo: los dos
   `None` no son un fallo, son «no aplica», y la interfaz los enseña distintos.

Se usa un `EvmBroadcaster` real sobre un nodo de mentira —el mismo patrón que
el resto de la suite— porque el límite y el error de revert tienen que venir del
objeto que los produce de verdad.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from amigocompora.app.confirmation import ConfirmationGateway, RecordingPrompt
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.slippage import DefaultSlippage
from amigocompora.app.usecases.estimate_cost import EstimateNetworkCost
from amigocompora.app.usecases.prepare_swap import PreparedSwap, PrepareSwap
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.models import (
    Measurement,
    PlannedTransaction,
    Quote,
    Token,
    TradingPair,
    UnsignedSolanaTransaction,
    UnsignedTransaction,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.infra.evm.broadcast import EvmBroadcaster
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool

AHORA = datetime(2026, 1, 1, tzinfo=UTC)

#: La cartera que firmaría y el destino del swap son **distintas** a propósito:
#: es lo que permite afirmar desde cuál se estimó. La primera es la cuenta 0 de
#: Hardhat, publicada y sin fondos en ninguna red real; no se firma nada aquí.
CARTERA = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
DESTINO = "0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"

WSOL = "So11111111111111111111111111111111111111112"

#: Con estas cifras del nodo, el coste máximo escrito a mano es:
#: 21 000 gas por 1,25 de margen = 26 250; techo de la política = (2·base + tip)
#: por 1,2 = 3,6 gwei; 26 250 por 3,6 gwei = 0,0000945 ETH.
COSTE_ESPERADO = "0.0000945 ETH"


class _RpcError(Exception):
    """Marca un handler como «el nodo contesta un error JSON-RPC»."""


class FakeNode:
    """Un nodo JSON-RPC que responde lo que se le diga y recuerda lo que le pidieron."""

    def __init__(self, **handlers: Any) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, list[Any]]] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = str(body["method"])
            params = list(body.get("params", []))
            self.calls.append((method, params))
            if method not in self.handlers:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": {"code": -32601, "message": f"método no previsto: {method}"},
                    },
                )
            result = self.handlers[method]
            if isinstance(result, Exception):
                if isinstance(result, _RpcError):
                    return httpx.Response(
                        200,
                        json={
                            "jsonrpc": "2.0",
                            "id": body.get("id"),
                            "error": {"code": -32000, "message": str(result)},
                        },
                    )
                return httpx.Response(503, text="endpoint caído")
            if callable(result):
                result = result(params)
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result}
            )

        return httpx.MockTransport(handler)

    def called(self, method: str) -> list[list[Any]]:
        return [params for name, params in self.calls if name == method]


def _node(**overrides: Any) -> FakeNode:
    handlers: dict[str, Any] = {
        "eth_chainId": hex(1),
        "eth_estimateGas": "0x5208",  # 21 000
        "eth_getBlockByNumber": {"baseFeePerGas": hex(10**9)},
        "eth_maxPriorityFeePerGas": hex(10**9),
    }
    handlers.update(overrides)
    return FakeNode(**handlers)


def _broadcaster(node: FakeNode) -> EvmBroadcaster:
    pool = RpcPool(
        "ethereum",
        (RpcEndpoint(url="https://ethereum.example.org", label="falso", priority=10),),
        httpx.AsyncClient(transport=node.transport()),
        clock=FrozenClock(AHORA),
    )
    return EvmBroadcaster(pool, "ethereum", clock=FrozenClock(AHORA))


# --------------------------------------------------------------------------- #
# El motor de mentira: lo mínimo del contrato, y construye en las dos redes
# --------------------------------------------------------------------------- #
class _FakePlanner:
    """Motor DEX que cotiza (vacío) y construye: lo mínimo para preparar."""

    def __init__(self) -> None:
        self.planned: list[tuple[Quote, str]] = []
        #: El deslizamiento con el que se le pidió cada payload, en el mismo orden
        #: que `planned`. `None` significa «el que decida el motor».
        self.slippages: list[int | None] = []

    @property
    def manifest(self) -> EngineManifest:
        return _FAKE_MANIFEST

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        return ()

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        return ()

    async def plan_swap(
        self, quote: Quote, *, recipient: str, slippage_bps: int | None = None
    ) -> PlannedTransaction:
        self.planned.append((quote, recipient))
        self.slippages.append(slippage_bps)
        if quote.pair.chain == "solana":
            return UnsignedSolanaTransaction(
                transaction_b64=base64.b64encode(b"\x01" * 64).decode(),
                last_valid_block_height=431_989_501,
                fee_payer=recipient,
                description="Swap de prueba en Solana",
            )
        return UnsignedTransaction(
            chain_id=1,
            to_address=ROUTER,
            calldata="0xdeadbeef",
            value=TokenAmount(0, 18, "ETH"),
            description="Swap de prueba en EVM",
        )

    def expected_destination(self, chain_key: str) -> str | None:
        """El doble no declara routers: el contraste se prueba en su propio sitio."""
        return None


_FAKE_MANIFEST = EngineManifest(
    engine_id="fake_dex",
    name="Motor de prueba",
    version="0.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary="Motor de prueba para los casos de uso.",
    capabilities=frozenset({Capability.READ_CHAIN, Capability.PREPARE_TX}),
    swap_chains=frozenset({"solana", "ethereum"}),
    required_config=(),
    allowed_hosts=(),
)


@dataclass(frozen=True, slots=True)
class _Provider:
    engine: _FakePlanner
    manifest: EngineManifest = _FAKE_MANIFEST

    def create(self, config: Mapping[str, str]) -> _FakePlanner:
        return self.engine


async def _prepare(
    *,
    estimate: EstimateNetworkCost | None,
) -> PrepareSwap:
    guard = ModeGuard(OperationMode.ASSISTED)
    gateway = ConfirmationGateway(guard, prompt=RecordingPrompt(answer=True))
    registry = EngineRegistry(guard)
    registry.register(_Provider(_FakePlanner()))
    await registry.activate("fake_dex")
    return PrepareSwap(registry=registry, gateway=gateway, estimate=estimate)


async def _prepare_con_planificador(
    *, slippage: DefaultSlippage | None
) -> tuple[PrepareSwap, _FakePlanner]:
    """Como `_prepare`, pero dejando el doble a la vista para leerle lo anotado."""
    guard = ModeGuard(OperationMode.ASSISTED)
    gateway = ConfirmationGateway(guard, prompt=RecordingPrompt(answer=True))
    registry = EngineRegistry(guard)
    planner = _FakePlanner()
    registry.register(_Provider(planner))
    await registry.activate("fake_dex")
    return (
        PrepareSwap(registry=registry, gateway=gateway, estimate=None, slippage=slippage),
        planner,
    )


# --------------------------------------------------------------------------- #
# Los dos caminos que se pueden dar
# --------------------------------------------------------------------------- #
async def test_el_coste_estimado_va_al_dialogo_y_al_resultado() -> None:
    """La cifra que ve el usuario al confirmar es la misma que viaja en el resultado."""
    node = _node()
    prepare = await _prepare(estimate=EstimateNetworkCost({"ethereum": _broadcaster(node)}))

    prepared = await prepare(_evm_quote(), recipient=DESTINO, sender=CARTERA)

    assert isinstance(prepared, PreparedSwap)
    assert prepared.network_cost is not None
    assert str(prepared.network_cost.native) == COSTE_ESPERADO
    assert prepared.cost_error is None
    campos = prepare.gateway.history[-1].action.details
    assert any(line.startswith(f"Coste de red: ≈ {COSTE_ESPERADO}") for line in campos)


async def test_se_estima_desde_la_cartera_que_firmaria_no_desde_el_destino() -> None:
    """El `from` de `eth_estimateGas` es la cartera, no el destino del swap."""
    node = _node()
    prepare = await _prepare(estimate=EstimateNetworkCost({"ethereum": _broadcaster(node)}))

    await prepare(_evm_quote(), recipient=DESTINO, sender=CARTERA)

    (llamada,) = node.called("eth_estimateGas")
    assert llamada[0]["from"] == CARTERA


async def test_si_la_red_dice_que_revertiria_se_ensena_el_motivo_y_se_sigue() -> None:
    """Un revert —típicamente, sin allowance— es información, no un muro.

    El borrador sirve para mirar qué se haría aunque la transacción hoy no
    pasara; lo que no puede es callarse el motivo, que es lo que el usuario
    necesita para arreglarlo.
    """
    revertida = _RpcError("execution reverted: ERC20: transfer amount exceeds allowance")
    prepare = await _prepare(
        estimate=EstimateNetworkCost({"ethereum": _broadcaster(_node(eth_estimateGas=revertida))})
    )

    prepared = await prepare(_evm_quote(), recipient=DESTINO, sender=CARTERA)

    assert prepared.network_cost is None
    assert prepared.cost_error is not None
    assert "revertiría" in prepared.cost_error
    assert isinstance(prepared.transaction, UnsignedTransaction)
    detalles = prepare.gateway.history[-1].action.details
    assert "la red no pudo estimarlo ahora mismo" in " ".join(detalles)


async def test_sin_nodo_para_la_red_se_ensena_el_motivo_y_se_sigue() -> None:
    """Red sin RPC configurado: hay borrador, y el panel dirá por qué no hay cifra."""
    prepare = await _prepare(estimate=EstimateNetworkCost({}))

    prepared = await prepare(_evm_quote(), recipient=DESTINO, sender=CARTERA)

    assert prepared.network_cost is None
    assert prepared.cost_error is not None
    assert "endpoint" in prepared.cost_error
    assert isinstance(prepared.transaction, UnsignedTransaction)


async def test_sin_cartera_de_firma_no_se_intenta_y_no_hay_motivo() -> None:
    """`None` y `None` es «no aplica», distinto de «se intentó y falló»."""
    node = _node()
    prepare = await _prepare(estimate=EstimateNetworkCost({"ethereum": _broadcaster(node)}))

    prepared = await prepare(_evm_quote(), recipient=DESTINO)

    assert prepared.network_cost is None
    assert prepared.cost_error is None
    assert node.called("eth_estimateGas") == []
    detalles = prepare.gateway.history[-1].action.details
    assert not any("Coste de red" in detalle for detalle in detalles)


async def test_en_solana_no_se_intenta_la_estimacion() -> None:
    """Sin `eth_estimateGas` que preguntar, no hay intento ni motivo: «—».

    La prueba monta un estimador completo —con su emisor de Ethereum— para que
    el `None` del resultado no pueda venir de que faltara el estimador: lo que
    lo explica es que la transacción no es de EVM.
    """
    prepare = await _prepare(
        estimate=EstimateNetworkCost({"ethereum": _broadcaster(_node())})
    )

    prepared = await prepare(_solana_quote(), recipient=WSOL, sender=CARTERA)

    assert isinstance(prepared.transaction, UnsignedSolanaTransaction)
    assert prepared.network_cost is None
    assert prepared.cost_error is None


async def test_sin_estimador_la_preparacion_sigue_siendo_la_de_antes() -> None:
    """El estimador es opcional: sin él, el diálogo no gana ni pierde líneas."""
    prepare = await _prepare(estimate=None)

    prepared = await prepare(_evm_quote(), recipient=DESTINO, sender=CARTERA)

    assert prepared.network_cost is None
    assert prepared.cost_error is None
    detalles = prepare.gateway.history[-1].action.details
    assert not any("Coste de red" in detalle for detalle in detalles)


# --------------------------------------------------------------------------- #
# El deslizamiento vigente y el contrato público de `estimate_cost`
# --------------------------------------------------------------------------- #
async def test_el_build_pasa_el_deslizamiento_vigente_al_motor() -> None:
    """Con la tolerancia viva puesta, el motor la recibe; sin ella, `None`.

    `None` es «lo que decida el motor», y es lo que deja idénticas a las
    llamadas anteriores al engranaje: se afirman las dos ramas para que la
    omisión no pueda convertirse en un cero sigiloso.
    """
    con, planificador = await _prepare_con_planificador(slippage=DefaultSlippage(250))
    await con.build(_evm_quote(), recipient=DESTINO)

    sin, planificador_sin = await _prepare_con_planificador(slippage=None)
    await sin.build(_evm_quote(), recipient=DESTINO)

    assert planificador.slippages == [250]
    assert planificador_sin.slippages == [None]


async def test_estimate_cost_publico_devuelve_los_dos_desuenlaces_sin_lanzar() -> None:
    """Es el contrato que usa la ejecución, llamado sin pasar por el diálogo.

    Cifra con motivo en `None`, o motivo con cifra en `None`: nunca lanza, que
    es lo que permite llamarlo antes del último sí sin que un nodo caído
    frustre la operación.
    """
    prepare = await _prepare(
        estimate=EstimateNetworkCost({"ethereum": _broadcaster(_node())})
    )
    transaccion = await prepare.build(_evm_quote(), recipient=DESTINO)
    assert isinstance(transaccion, UnsignedTransaction)

    coste, error = await prepare.estimate_cost(transaccion, _evm_quote(), CARTERA)
    assert coste is not None
    assert str(coste.native) == COSTE_ESPERADO
    assert error is None

    caido = await _prepare(
        estimate=EstimateNetworkCost(
            {"ethereum": _broadcaster(_node(eth_estimateGas=_RpcError("execution reverted")))}
        )
    )
    coste, error = await caido.estimate_cost(transaccion, _evm_quote(), CARTERA)
    assert coste is None
    assert error is not None
    assert "revertiría" in error


# --------------------------------------------------------------------------- #
# Utilería
# --------------------------------------------------------------------------- #
def _evm_quote() -> Quote:
    return Quote(
        venue=Venue(venue_id="x@ethereum", name="X", kind=VenueKind.DEX, chain="ethereum"),
        engine_id=_FAKE_MANIFEST.engine_id,
        pair=TradingPair(
            base=Token("WETH", 18, "ethereum", "0x" + "1" * 40),
            quote=Token("USDC", 6, "ethereum", "0x" + "2" * 40),
        ),
        amount_in=TokenAmount(1, 18, "WETH"),
        amount_out=TokenAmount(3_000_000, 6, "USDC"),
        fee_bps=BasisPoints(30),
        fee_basis=Measurement.REPORTED,
        price_impact_bps=BasisPoints(2),
        observed_at=AHORA,
    )


def _solana_quote() -> Quote:
    return Quote(
        venue=Venue(venue_id="jup@solana", name="Júpiter", kind=VenueKind.DEX, chain="solana"),
        engine_id=_FAKE_MANIFEST.engine_id,
        pair=TradingPair(
            base=Token("WSOL", 9, "solana", WSOL),
            quote=Token(
                "USDC", 6, "solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            ),
        ),
        amount_in=TokenAmount(1_000_000_000, 9, "WSOL"),
        amount_out=TokenAmount(150_000_000, 6, "USDC"),
        fee_bps=None,
        fee_basis=None,
        price_impact_bps=BasisPoints(2),
        observed_at=AHORA,
        impact_basis=Measurement.REPORTED,
        source_note="ruta simulada",
    )
