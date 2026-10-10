"""Leer la wallet de depósito: dirección, saldo y posiciones, **sin la clave**.

Es el caso de uso más inofensivo de la aplicación —no firma, no emite, no pide
confirmación— y por eso lo que hay que fijar es justo lo que **no** hace:

1. **No toca la clave privada.** La dirección sale del planificador —una
   derivación pura, sin red y sin credenciales—, y el doble de claves cuenta las
   veces que se le pidió: cero. Tiparlo contra `AddressSource` y no contra
   `PrivateKeySource` es lo que hace imposible que un saldo lea la clave «de
   refilón», y la prueba lo mide en vez de prometerlo.
2. **Lee todas las posiciones, no sólo las cobrables.** Lo contrario que la
   tarjeta de cobro: aquí lo abierto es lo que se quiere, porque es sobre lo que
   se opera.
3. **Cada negativa tiene su motivo**: sin cartera, sin nodo y con un motor que
   sólo lee, el error dice cuál de las tres falta.

Se usa un `EvmBroadcaster` **real** sobre un nodo de mentira —como en el cobro— y
la fuente de datos falseada: el motor es el de producción, y lo único doblado es
la red.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from amigocompora.app.execution_policy import AddressSource
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.read_settlement_wallet import ReadSettlementWallet
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import EngineError, ExecutionError
from amigocompora.domain.models import (
    MarketDepth,
    MarketTag,
    PredictionMarket,
    PredictionSort,
)
from amigocompora.domain.modes import OperationMode
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.polymarket.engine import PolymarketEngine
from amigocompora.infra.evm.broadcast import EvmBroadcaster
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool

AHORA = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

#: La cartera que lee: la cuenta 0 de Hardhat, publicada y sin fondos.
DIRECCION = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

#: El saldo que el nodo contesta: 950 000 en unidad mínima de pUSD = 0.95.
SALDO_RAW = 950000

MERCADO = "0x" + "ab" * 32
TOKEN_SI = "71321045679252212594626385532706912750332728571942532289631379312455583992563"  # noqa: S105


class FakeKeys(AddressSource):
    """Da la dirección sin tener clave, y **cuenta** las veces que se le pidió.

    El contador es la mitad de lo que esta prueba fija: una lectura no debe
    poder ver la clave privada ni aunque esté configurada.
    """

    def __init__(self, *, available: bool = True) -> None:
        self.pedida = 0
        self._available = available

    def available(self) -> bool:
        return self._available

    def address(self) -> str | None:
        return DIRECCION if self._available else None

    def require(self) -> str:
        # No está en `AddressSource`: está aquí para que pedirla cuente, y para
        # que el día que alguien tipara el caso de uso contra la clave, el
        # contador lo delatara.
        self.pedida += 1
        return "0x" + "11" * 32


class FuenteFalsa:
    """Devuelve las filas que se le dan, y anota con qué parámetros se le pidió."""

    def __init__(self, filas: list[dict[str, Any]]) -> None:
        self._filas = filas
        self.parametros: list[dict[str, str]] = []

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        assert url.endswith("/positions"), url
        self.parametros.append(dict(params or {}))
        return self._filas


def _fila(*, redeemable: bool = False) -> dict[str, Any]:
    """Una posición con la forma exacta que publica la fuente."""
    return {
        "conditionId": MERCADO,
        "asset": TOKEN_SI,
        "size": "12.5",
        "title": "¿Ocurrirá lo medido?",
        "outcome": "Sí",
        "redeemable": redeemable,
        "negativeRisk": False,
        "curPrice": "0.80",
        "avgPrice": "0.85",
        "cashPnl": "-0.62",
        "percentPnl": "-5.8",
    }


class _Provider:
    """Envuelve un motor para que el registro lo pueda abrir."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    @property
    def manifest(self) -> EngineManifest:
        manifest: EngineManifest = self.engine.manifest
        return manifest

    def create(self, config: Mapping[str, str]) -> Any:
        del config
        return self.engine


class MotorQueSoloLee:
    """Un motor de predicción de lectura: sin planificador ni cobrador.

    Es el caso de una instalación que puede mirar mercados pero no operar, y lo
    que se afirma es que la negativa tiene **nombre propio** —la del registro— y
    no un `AttributeError` sobre un método que el motor nunca tuvo.
    """

    manifest = EngineManifest(
        engine_id="solo-lectura",
        name="Sólo lectura",
        version="1.0.0",
        kind=EngineKind.PREDICTION_MARKETS,
        summary="Lee mercados y nada más.",
        capabilities=frozenset(),
        required_config=(),
        allowed_hosts=(),
    )

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def markets(
        self,
        *,
        limit: int = 20,
        search: str | None = None,
        closing_within: timedelta | None = None,
        category: MarketTag | None = None,
        sort: PredictionSort | None = None,
    ) -> Sequence[PredictionMarket]:
        del limit, search, closing_within, category, sort
        return ()

    async def market(self, market_id: str) -> PredictionMarket:
        raise AssertionError(market_id)

    async def market_by_condition(self, condition_id: str) -> PredictionMarket:
        raise AssertionError(condition_id)

    async def book(self, token_id: str) -> MarketDepth:
        raise AssertionError(token_id)


class FakeNode:
    """Nodo JSON-RPC que contesta lo que se le diga y recuerda lo que le pidieron."""

    def __init__(self, **handlers: Any) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, list[Any]]] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = str(body["method"])
            params = list(body.get("params", []))
            self.calls.append((method, params))
            result = self.handlers.get(method)
            if result is None:
                # Un método no previsto se contesta como error en vez de con un
                # valor inventado: si el camino llama a algo que la prueba no
                # esperaba, tiene que notarse.
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": {"code": -32601, "message": f"no previsto: {method}"},
                    },
                )
            if callable(result):
                result = result(params)
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result}
            )

        return httpx.MockTransport(handler)


def _node() -> FakeNode:
    return FakeNode(
        eth_chainId=hex(137),
        # `balanceOf` devuelve una palabra de 32 bytes: el saldo en unidad mínima.
        eth_call="0x" + format(SALDO_RAW, "064x"),
    )


def _emisor(node: FakeNode) -> EvmBroadcaster:
    pool = RpcPool(
        "polygon",
        (RpcEndpoint(url="https://polygon.example.org", label="falso", priority=10),),
        httpx.AsyncClient(transport=node.transport()),
        clock=FrozenClock(AHORA),
    )
    return EvmBroadcaster(
        pool,
        "polygon",
        clock=FrozenClock(AHORA),
        receipt_poll_interval_seconds=0.0,
        receipt_poll_attempts=2,
    )


async def _leer(
    *,
    filas: list[dict[str, Any]] | None = None,
    keys: FakeKeys | None = None,
    broadcasters: Mapping[str, EvmBroadcaster] | None = None,
    motor: Any | None = None,
) -> tuple[ReadSettlementWallet, FuenteFalsa, FakeKeys]:
    """El caso de uso sobre el registro y el motor de verdad, con la red doblada."""
    engine = PolymarketEngine(clock=FrozenClock(AHORA))
    fuente = FuenteFalsa(filas if filas is not None else [_fila()])
    engine._source = fuente  # type: ignore[assignment]

    registry = EngineRegistry(ModeGuard(OperationMode.OBSERVATION))
    activo = motor if motor is not None else engine
    registry.register(_Provider(activo), source="prueba")
    await registry.activate(activo.manifest.engine_id)

    claves = keys if keys is not None else FakeKeys()
    emisores: Mapping[str, EvmBroadcaster] = (
        broadcasters if broadcasters is not None else {"polygon": _emisor(_node())}
    )
    use_case = ReadSettlementWallet(
        registry=registry,
        keys=claves,
        broadcasters=emisores,
        clock=FrozenClock(AHORA),
    )
    return use_case, fuente, claves


# --------------------------------------------------------------------------- #
# La lectura
# --------------------------------------------------------------------------- #
async def test_la_wallet_se_lee_sin_tocar_la_clave() -> None:
    """Dirección, saldo y posiciones, y ni una lectura de la clave privada.

    La dirección se **deriva** —es lo que permite enseñarla, y recibir en ella,
    antes de haber leído nada—, y por eso no hace falta la clave ni una petición
    de red para conocerla.
    """
    use_case, fuente, claves = await _leer()

    # Se enseña antes de leer: es una derivación pura, sin red.
    direccion = use_case.address()
    assert direccion.startswith("0x")
    assert len(direccion) == 42
    assert use_case.address() == direccion
    assert claves.pedida == 0

    vista = await use_case()
    assert vista.address == direccion
    assert vista.collateral.symbol == "pUSD"
    assert vista.collateral.decimals == 6
    # El raw del nodo se traduce con la escala del colateral: 950 000 → 0.95.
    assert vista.balance.as_decimal() == Decimal("0.95")
    assert vista.balance.symbol == "pUSD"
    assert vista.observed_at == AHORA
    assert claves.pedida == 0

    (posicion,) = vista.positions
    assert posicion.condition_id == MERCADO
    assert posicion.outcome_label == "Sí"
    assert posicion.shares == Decimal("12.5")
    assert posicion.avg_price == Decimal("0.85")
    assert posicion.cash_pnl == Decimal("-0.62")
    # Las posiciones se leen de **la wallet derivada**, no de la EOA.
    assert fuente.parametros[0]["user"] == direccion


async def test_se_leen_todas_las_posiciones_no_solo_las_cobrables() -> None:
    """Lo contrario que la tarjeta de cobro: aquí lo abierto es lo que se opera.

    Pedir sólo las cobrables dejaría la tarjeta enseñando únicamente lo resuelto
    —justo lo que no se puede operar— y escondiendo las posiciones abiertas, que
    son el motivo de la tarjeta.
    """
    use_case, fuente, _ = await _leer(filas=[_fila(redeemable=False)])
    vista = await use_case()
    assert "redeemable" not in fuente.parametros[0]
    assert len(vista.positions) == 1
    assert vista.positions[0].redeemable is False


# --------------------------------------------------------------------------- #
# Las negativas, cada una con su motivo
# --------------------------------------------------------------------------- #
async def test_sin_cartera_no_hay_wallet_que_leer() -> None:
    """Sin cartera no hay dirección que derivar, y se dice así —no con un `None`."""
    use_case, _, _ = await _leer(keys=FakeKeys(available=False))
    with pytest.raises(ExecutionError, match="ninguna cartera"):
        use_case.address()
    with pytest.raises(ExecutionError, match="ninguna cartera"):
        await use_case()


async def test_sin_nodo_no_se_puede_leer_el_saldo() -> None:
    """La dirección se conoce sin red; el saldo no: se dice qué falta."""
    use_case, _, _ = await _leer(broadcasters={})
    with pytest.raises(ExecutionError, match="nodo"):
        await use_case()


async def test_un_motor_que_solo_lee_no_deriva_la_wallet() -> None:
    """El mensaje es el del registro —con nombre propio— y no un `AttributeError`."""
    use_case, _, _ = await _leer(motor=MotorQueSoloLee())
    with pytest.raises(EngineError, match="no sabe construir"):
        use_case.address()
