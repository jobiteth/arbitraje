"""El camino por el que se publica una orden en un mercado de predicción.

Lo que se fija aquí son los sitios donde **podría perderse dinero**, y son cinco,
los mismos cinco del camino de swaps leídos al lado del otro:

1. **Que no se publique fuera del modo `EJECUCIÓN`**, y que la barrera corte
   **antes** de trabajar: se mide en peticiones de red y en veces que se le pidió
   la orden al motor, no en el error. Una barrera comprobada después del trabajo
   ya gastó el trabajo.
2. **Que comprar apruebe el importe exacto y nunca un ilimitado.** Un permiso sin
   tope deja al recinto autorizado a vaciar ese token para siempre, y aquí hay un
   motivo añadido: el recinto liquida **después**, así que conceder de más sería
   darle margen sobre el saldo futuro de la cartera.
3. **Que vender autorice la colección y lo diga sin importe.** El estándar
   ERC-1155 no tiene permiso por cantidad, así que el diálogo lo tiene que decir
   con esas palabras: alcanza a todas las participaciones, no sólo a las de esta
   orden.
4. **Que el asiento de la orden diga que no es una transacción.** Las dos cadenas
   tienen la misma forma y no se distinguen mirándolas, así que el registro lleva
   un campo que lo dice y la descripción lo repite.
5. **Que la clave privada no acabe ni en el registro ni en el log ni en el
   cuerpo que se envía.** Se busca la cadena literal en lo que se escribió.

Se usa un `EvmBroadcaster` **real** sobre un nodo de mentira, y una clave de
desarrollo publicada —la cuenta 0 de Hardhat, sin fondos y conocida
públicamente— para que la firma sea de verdad. Y el motor falso construye y firma
con el **código de producción** de `engines/polymarket/orders.py`: es un doble
del recinto, no de la aritmética, porque la aritmética es justo la parte que
mueve el dinero.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_utils.crypto import keccak
from structlog.testing import capture_logs

from amigocompora.app.confirmation import (
    ConfirmationGateway,
    Decision,
    RecordingPrompt,
)
from amigocompora.app.execution_policy import (
    AutonomyPolicy,
    ExecutionLedger,
    PrivateKeySource,
)
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.place_prediction_order import PlacePredictionOrder
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
    EngineError,
    ExecutionError,
    ExecutionLimitExceededError,
    ModeNotPermittedError,
    UnsupportedOperationError,
)
from amigocompora.domain.execution import ExecutionLimits, TriggerKind
from amigocompora.domain.models import (
    MarketDepth,
    MarketOutcome,
    PredictionMarket,
    PredictionOrder,
    PredictionSide,
    SignedPredictionOrder,
    SubmittedPredictionOrder,
    Token,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.polymarket.orders import (
    build_order as build_prediction_order,
)
from amigocompora.engines.polymarket.orders import (
    exchange_for,
    sign_order,
)
from amigocompora.infra.evm.broadcast import EvmBroadcaster
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool

AHORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
POLYGON_ID = 137

#: Cuenta 0 de Hardhat, publicada y sin fondos en ninguna red real. La dirección
#: está derivada de la clave, no recordada.
CLAVE = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

#: Direcciones de contratos, escritas a mano y en minúsculas. Se escriben enteras
#: y no se sacan de una constante del producto: son contra lo que se firma y a
#: quién se le concede el permiso, y sacarlas de donde las saca el camino dejaría
#: la prueba comprobándose a sí misma.
EXCHANGE = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
COLATERAL = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
COLECCION = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"

#: Selectores escritos a mano desde la definición pública de cada estándar.
#: `approve(address,uint256)` es ERC-20; `setApprovalForAll(address,bool)` es el
#: permiso **sin importe** de ERC-1155, que es lo que hay que ver en el `raw` de
#: una venta y lo que no puede aparecer en el de una compra.
SELECTOR_APPROVE_ERC20 = "095ea7b3"
SELECTOR_SET_APPROVAL_FOR_ALL = "a22cb465"
#: El máximo de `uint256`, que es lo que **no** puede aparecer en un `approve`.
MAXIMO_UINT256 = "f" * 64

#: El identificador que el recinto le da a la orden. Tiene la misma forma que un
#: hash de transacción, y esa es exactamente la razón de que el registro lleve un
#: campo `kind`: sin él, los dos renglones se leen igual.
ORDEN_ID = "0x" + "cd" * 32
#: El estado, en el vocabulario del recinto y sin traducir.
ESTADO = "live"

CONDICION = "0x" + "ab" * 32
#: El `tokenId` de cada resultado: el nombre público del resultado dentro del
#: contrato, no un secreto. Va como cadena de dígitos porque es un `uint256` y en
#: JSON no cabe en un número sin perder cifras. De ahí el `noqa`: la regla lee
#: «TOKEN» como si fuera una credencial.
TOKEN_SI = "71321045679252212594626385532706912750332728571942532289631379312455583992563"  # noqa: S105
TOKEN_NO = "52114319501245915516055105976744304775366685471852918604325386120244825471694"  # noqa: S105

#: 10 participaciones a 0,62 son 6,20 dólares, en unidades de 10^-6 del colateral.
COSTE_RAW = 6_200_000
PRECIO = Decimal("0.62")
TAMANO = Decimal("10")

MANIFEST = EngineManifest(
    engine_id="polymarket",
    name="Polymarket",
    version="1.0.0",
    kind=EngineKind.PREDICTION_MARKETS,
    summary="Doble del recinto, con la aritmética de producción.",
    capabilities=frozenset({Capability.PREPARE_TX, Capability.BROADCAST_TX}),
)


# --------------------------------------------------------------------------- #
# Dobles
# --------------------------------------------------------------------------- #
class FakeKey(PrivateKeySource):
    """Clave que se puede hacer desaparecer, y que **cuenta** cuántas veces se pide.

    El contador es lo que permite afirmar que en los modos que no ejecutan no se
    llegó a tocar la clave; el interruptor existe para poder preguntar por la
    dirección —que la interfaz muestra— sin que haya clave configurada.
    """

    def __init__(self, *, available: bool = True) -> None:
        self.pedida = 0
        self._available = available

    def available(self) -> bool:
        return self._available

    def require(self) -> str:
        self.pedida += 1
        return CLAVE


class _FakePassphrase:
    def get(self) -> str | None:
        return None


def _mercado(**overrides: Any) -> PredictionMarket:
    """Un mercado operable de Polygon, con lo que se le diga cambiado."""
    values: dict[str, Any] = {
        "market_id": "0xmercado",
        "venue": Venue(
            venue_id="polymarket",
            name="Polymarket",
            kind=VenueKind.PREDICTION_MARKET,
            chain="polygon",
        ),
        "question": "¿Llegará el Bitcoin a 100 000 dólares en octubre?",
        "outcomes": (
            MarketOutcome(label="Sí", price=Decimal("0.62"), token_id=TOKEN_SI),
            MarketOutcome(label="No", price=Decimal("0.38"), token_id=TOKEN_NO),
        ),
        "observed_at": AHORA,
        "condition_id": CONDICION,
        "neg_risk": False,
        "tick_size": Decimal("0.01"),
        "min_order_size": Decimal("5"),
    }
    values.update(overrides)
    return PredictionMarket(**values)


class MotorFalso:
    """Un motor de predicción que sabe operar, y que **cuenta** lo que se le pide.

    Construye y firma con el código de producción: lo que se dobla aquí es el
    recinto —lo que contesta por la red—, no la aritmética de la orden. Doblar la
    aritmética dejaría sin probar la parte que mueve el dinero.

    Los contadores no son decorativos: `construidos` es la única medida de «se
    gastó trabajo en una operación que el modo no permite», y `claves_vistas`
    permite afirmar que la clave llega al firmar y **no** al construir.
    """

    def __init__(
        self,
        *,
        estado: str = ESTADO,
        orden_id: str = ORDEN_ID,
        coleccion: str = COLECCION,
    ) -> None:
        self.construidos = 0
        self.claves_vistas: list[str] = []
        self.publicadas: list[SignedPredictionOrder] = []
        self._estado = estado
        self._orden_id = orden_id
        self._coleccion = coleccion

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def markets(
        self,
        *,
        limit: int = 20,
        search: str | None = None,
        closing_within: timedelta | None = None,
    ) -> Sequence[PredictionMarket]:
        del limit, search, closing_within
        return (_mercado(),)

    async def market(self, market_id: str) -> PredictionMarket:
        raise AssertionError(f"no se pidió un mercado suelto: {market_id}")

    async def book(self, token_id: str) -> MarketDepth:
        # `book` está en el protocolo de **lectura**, así que un motor que no lo
        # implemente no se reconoce como motor de predicción. Publicar una orden
        # no lo usa, y por eso se dice que no se esperaba en vez de inventar un
        # libro que nadie pidió.
        raise AssertionError(f"publicar una orden no lee el libro: {token_id}")

    def build_order(
        self,
        market: PredictionMarket,
        *,
        outcome_label: str,
        side: PredictionSide,
        size: Decimal,
        price: Decimal,
    ) -> PredictionOrder:
        self.construidos += 1
        return build_prediction_order(
            market,
            outcome_label=outcome_label,
            side=side,
            size=size,
            price=price,
        )

    def sign_order(self, order: PredictionOrder, *, private_key: str) -> SignedPredictionOrder:
        self.claves_vistas.append(private_key)
        return sign_order(order, private_key=private_key)

    async def submit_order(
        self, signed: SignedPredictionOrder, *, private_key: str
    ) -> SubmittedPredictionOrder:
        self.claves_vistas.append(private_key)
        self.publicadas.append(signed)
        return SubmittedPredictionOrder(order_id=self._orden_id, status=self._estado)

    def exchange_for(self, market: PredictionMarket) -> str:
        del market
        return exchange_for(False)

    def collateral_for(self, market: PredictionMarket) -> Token:
        del market
        return Token(symbol="USDC", decimals=6, chain="polygon", address=COLATERAL)

    def shares_collection_for(self, market: PredictionMarket) -> str:
        del market
        return self._coleccion


class MotorSoloLectura:
    """Un motor de predicción que sólo mira: no sabe construir ni publicar."""

    manifest = EngineManifest(
        engine_id="solo-lectura",
        name="Sólo lectura",
        version="1.0.0",
        kind=EngineKind.PREDICTION_MARKETS,
        summary="Lee mercados y nada más.",
        capabilities=frozenset({Capability.READ_CHAIN}),
    )

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def markets(self, **kwargs: Any) -> Sequence[PredictionMarket]:
        del kwargs
        return ()

    async def market(self, market_id: str) -> PredictionMarket:
        raise AssertionError(market_id)

    async def book(self, token_id: str) -> MarketDepth:
        raise AssertionError(token_id)


class _Provider:
    """Envuelve un motor para que el registro lo pueda abrir.

    El motor entra como `Any` porque aquí se registran dobles distintos —uno que
    opera y otro que sólo lee—, y lo que la prueba mide es lo que el **registro**
    hace con ellos, no sus tipos.
    """

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    @property
    def manifest(self) -> EngineManifest:
        manifest: EngineManifest = self.engine.manifest
        return manifest

    def create(self, config: Mapping[str, str]) -> Any:
        del config
        return self.engine


# --------------------------------------------------------------------------- #
# Nodo de mentira
# --------------------------------------------------------------------------- #
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

    def called(self, method: str) -> list[list[Any]]:
        return [params for name, params in self.calls if name == method]

    def sent_raw(self) -> list[str]:
        return [str(params[0]) for params in self.called("eth_sendRawTransaction")]


def _hash_of(params: list[Any]) -> str:
    """El hash que el nodo devolvería: es el keccak del propio `raw`.

    Se calcula en vez de inventarlo porque `EvmBroadcaster.send` compara el hash
    que contesta el nodo con el que él calculó al firmar: un hash distinto
    significa que se emitió algo que no es lo que se cree, y la prueba tiene que
    poder distinguir ese caso del camino bueno.
    """
    return "0x" + keccak(bytes.fromhex(str(params[0])[2:])).hex()


def _permisos() -> Callable[[list[Any]], str]:
    """`eth_call` que contesta las dos preguntas de permiso, cada una por su vía.

    Las dos van al mismo método JSON-RPC, así que un nodo que contestara siempre
    lo mismo no podría distinguirlas: se despacha por el **contrato** al que se le
    pregunta —el colateral para el permiso del ERC-20, la colección para el del
    ERC-1155—, que es exactamente lo que hace la cadena.

    Y cada una cambia después de su aprobación, por la misma razón que en el
    camino de swaps: el camino vuelve a leer a propósito para no dar por hecho que
    la transacción dejó el permiso que se pidió, así que un nodo que contestara
    siempre cero haría fallar también el camino bueno.

    Para el caso contrario —el permiso ya concedido— no hace falta esto: sólo se
    hace **una** pregunta, así que basta con contestar el valor que se quiera.
    """
    estado = {"erc20": 0, "coleccion": 0}

    def handler(params: list[Any]) -> str:
        destino = str(params[0].get("to", "")).lower()
        if destino == COLATERAL:
            estado["erc20"] += 1
            # Antes de aprobar, cero; después, de sobra. El importe exacto que se
            # autoriza se comprueba sobre el `calldata`, que es donde viaja.
            return "0x0" if estado["erc20"] == 1 else hex(10**18)
        assert destino == COLECCION, (
            f"la prueba sólo espera preguntas al colateral o a la colección, no a {destino}"
        )
        estado["coleccion"] += 1
        concedido = estado["coleccion"] > 1
        return "0x" + ("0" * 63) + ("1" if concedido else "0")

    return handler


def _node(**overrides: Any) -> FakeNode:
    handlers: dict[str, Any] = {
        "eth_chainId": hex(POLYGON_ID),
        "eth_getTransactionCount": "0x5",
        "eth_estimateGas": "0x30d40",
        "eth_getBlockByNumber": {"baseFeePerGas": "0x3b9aca00"},
        "eth_maxPriorityFeePerGas": "0x3b9aca",
        "eth_call": _permisos(),
        "eth_sendRawTransaction": _hash_of,
        "eth_getTransactionReceipt": {"blockNumber": "0x10", "status": "0x1"},
    }
    handlers.update(overrides)
    return FakeNode(**handlers)


def _broadcaster(node: FakeNode) -> EvmBroadcaster:
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


# --------------------------------------------------------------------------- #
# Armado
# --------------------------------------------------------------------------- #
def _limits(**overrides: Any) -> ExecutionLimits:
    """Límites que describen una ejecución **permitida**.

    El interruptor maestro se prueba aparte y en su propia prueba: apagarlo aquí
    haría que todas las de este fichero fallaran por el mismo motivo —y ninguna
    por el suyo—, que es la forma de tener veinte pruebas que comprueban una sola
    cosa.
    """
    values: dict[str, Any] = {
        "enabled": True,
        "max_quote_per_trade": Decimal("5000"),
        "max_quote_per_day": Decimal("20000"),
        "allowed_tokens": frozenset({"USDC"}),
        "allowed_chains": frozenset({"polygon"}),
        "allowed_engines": frozenset({"polymarket"}),
    }
    values.update(overrides)
    return ExecutionLimits(**values)


async def _armed(
    tmp_path: Path,
    *,
    mode: OperationMode = OperationMode.EXECUTION,
    node: FakeNode | None = None,
    limits: ExecutionLimits | None = None,
    answer: bool = True,
    motor: Any | None = None,
    engine_id: str = "polymarket",
    broadcasters: Mapping[str, EvmBroadcaster] | None = None,
    hay_clave: bool = True,
) -> tuple[PlacePredictionOrder, FakeNode, ExecutionLedger, RecordingPrompt, FakeKey, Any]:
    """Un `PlacePredictionOrder` completo, con emisor **real** sobre un nodo falso."""
    clock = FrozenClock(AHORA)
    ledger = ExecutionLedger(tmp_path / "executions.jsonl", clock=clock)
    keys = FakeKey(available=hay_clave)
    policy = AutonomyPolicy(
        keys=keys,
        passphrase=_FakePassphrase(),
        limits=limits or _limits(),
        ledger=ledger,
        clock=clock,
        trigger=TriggerKind.MANUAL,
    )
    registry = EngineRegistry(ModeGuard(mode))
    engine = motor if motor is not None else MotorFalso()
    registry.register(_Provider(engine))
    # Registrado no basta: `prediction_planner` busca entre los **activos**, así
    # que hay que activarlo como lo haría el panel de motores.
    await registry.activate(engine_id)
    prompt = RecordingPrompt(answer=answer)
    gateway = ConfirmationGateway(ModeGuard(mode), prompt=prompt, clock=clock)

    fake = node or _node()
    use_case = PlacePredictionOrder(
        registry=registry,
        gateway=gateway,
        keys=keys,
        policy=policy,
        broadcasters=({"polygon": _broadcaster(fake)} if broadcasters is None else broadcasters),
        clock=clock,
    )
    return use_case, fake, ledger, prompt, keys, engine


async def _comprar(
    use_case: PlacePredictionOrder,
    *,
    market: PredictionMarket | None = None,
    **overrides: Any,
) -> SubmittedPredictionOrder:
    values: dict[str, Any] = {
        "outcome_label": "Sí",
        "side": PredictionSide.BUY,
        "size": TAMANO,
        "price": PRECIO,
        "recipient": DIRECCION,
    }
    values.update(overrides)
    return await use_case(market or _mercado(), **values)


# --------------------------------------------------------------------------- #
# 1. La barrera
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mode",
    [OperationMode.OBSERVATION, OperationMode.SIMULATION, OperationMode.ASSISTED],
)
async def test_fuera_de_ejecucion_no_se_hace_ni_una_peticion(
    tmp_path: Path, mode: OperationMode
) -> None:
    """La barrera se comprueba antes de trabajar, y eso se mide en peticiones.

    Que lance es la mitad de la propiedad. La otra mitad es que no se gastó red ni
    se tocó la clave. Sin la afirmación sobre `construidos`, esta prueba pasaría
    igual con el `precheck` quitado, porque el `authorize` posterior también
    bloquea — sólo que después de que el motor ya hubiera construido la orden, que
    es justo el trabajo que se quiere evitar.
    """
    motor = MotorFalso()
    use_case, node, ledger, prompt, keys, _ = await _armed(tmp_path, mode=mode, motor=motor)

    with pytest.raises(ModeNotPermittedError):
        await _comprar(use_case)

    assert motor.construidos == 0
    assert node.calls == []
    assert keys.pedida == 0
    assert prompt.asked == []
    assert ledger.entries() == ()


async def test_en_ejecucion_pero_sin_confirmar_no_se_publica_nada(
    tmp_path: Path,
) -> None:
    """Decir que no en el diálogo no puede dejar una orden publicada.

    Es la primera de las dos confirmaciones —la de la aprobación previa— la que
    se rechaza aquí: sin permiso, el recinto no podría cobrar la orden, así que
    negarse a aprobar ya detiene todo lo demás.
    """
    use_case, node, ledger, prompt, _, motor = await _armed(tmp_path, answer=False)

    with pytest.raises(ConfirmationDeniedError):
        await _comprar(use_case)

    assert node.sent_raw() == []
    assert motor.publicadas == []
    assert ledger.entries() == ()
    assert len(prompt.asked) == 1


# --------------------------------------------------------------------------- #
# 2. La red
# --------------------------------------------------------------------------- #
async def test_una_red_que_no_es_evm_se_rechaza_antes_de_construir(
    tmp_path: Path,
) -> None:
    """Los permisos previos son transacciones: sin firmar en la cadena no hay nada.

    Se comprueba que se rechaza **antes** de gastar red: el guard va antes de
    construir, así que no hay ninguna petición que hacer.
    """
    motor = MotorFalso()
    use_case, node, _, _, _, _ = await _armed(tmp_path, motor=motor)
    solana = _mercado(
        venue=Venue(
            venue_id="polymarket",
            name="Polymarket",
            kind=VenueKind.PREDICTION_MARKET,
            chain="solana",
        )
    )

    with pytest.raises(UnsupportedOperationError, match="no es una red EVM"):
        await _comprar(use_case, market=solana)

    assert motor.construidos == 0
    assert node.calls == []


async def test_sin_nodo_para_la_red_no_se_firma_nada(tmp_path: Path) -> None:
    """Sin nodo no se puede leer el permiso ni concederlo, y sin eso no se publica."""
    use_case, node, ledger, _, _, _ = await _armed(tmp_path, broadcasters={})

    with pytest.raises(ExecutionError, match="no hay ningún nodo configurado"):
        await _comprar(use_case)

    assert node.calls == []
    assert ledger.entries() == ()


async def test_un_motor_que_solo_lee_no_publica(tmp_path: Path) -> None:
    """Leer mercados y operarlos son dos superficies distintas, y se distinguen.

    El accesor tiene que decirlo con un error propio en vez de reventar con un
    `AttributeError` sobre un método que el motor nunca tuvo.
    """
    use_case, node, _, _, _, _ = await _armed(
        tmp_path, motor=MotorSoloLectura(), engine_id="solo-lectura"
    )

    with pytest.raises(EngineError, match="no sabe construir ni publicar"):
        await _comprar(use_case)

    assert node.calls == []


# --------------------------------------------------------------------------- #
# 3. Los límites
# --------------------------------------------------------------------------- #
async def test_por_encima_del_tope_no_se_toca_la_red_ni_la_clave(
    tmp_path: Path,
) -> None:
    """El tope se mide en el colateral, y corta antes de firmar y de aprobar."""
    use_case, node, ledger, prompt, keys, _ = await _armed(
        tmp_path, limits=_limits(max_quote_per_trade=Decimal("5"))
    )

    with pytest.raises(ExecutionLimitExceededError):
        await _comprar(use_case)

    assert node.calls == [], "no se emite ni se lee nada"
    assert keys.pedida == 0, "ni se llega a pedir la clave"
    assert prompt.asked == [], "ni se pide confirmación de algo que no cabe"
    assert ledger.entries() == ()


async def test_una_red_fuera_de_la_lista_blanca_no_se_publica(tmp_path: Path) -> None:
    use_case, node, _, _, _, _ = await _armed(
        tmp_path, limits=_limits(allowed_chains=frozenset({"base"}))
    )

    with pytest.raises(ExecutionLimitExceededError):
        await _comprar(use_case)

    assert node.sent_raw() == []


async def test_un_colateral_fuera_de_la_lista_blanca_no_se_publica(
    tmp_path: Path,
) -> None:
    """Lo único que se mueve en una orden es el colateral, y es lo que se comprueba.

    Las participaciones son un ERC-1155 que no está en el catálogo y que cambia
    con cada mercado: si entraran en la lista blanca habría que mantener a mano
    una lista de resultados que se renueva cada pocos minutos.
    """
    use_case, node, _, _, _, _ = await _armed(
        tmp_path, limits=_limits(allowed_tokens=frozenset({"WETH"}))
    )

    with pytest.raises(ExecutionLimitExceededError):
        await _comprar(use_case)

    assert node.sent_raw() == []


# --------------------------------------------------------------------------- #
# 4. Comprar: el permiso del colateral
# --------------------------------------------------------------------------- #
async def test_comprar_aprueba_el_importe_exacto_y_nunca_el_maximo(
    tmp_path: Path,
) -> None:
    """El `approve` lleva lo que cuesta **esta** orden, no un ilimitado.

    Un permiso sin tope deja al recinto autorizado a vaciar ese token para
    siempre. Aquí hay un motivo añadido: el recinto liquida después —una orden
    puede cruzarse horas más tarde— así que conceder de más sería darle margen
    sobre el saldo futuro de la cartera, no sobre esta operación.
    """
    use_case, node, _, prompt, _, _ = await _armed(tmp_path)

    await _comprar(use_case)

    assert len(node.sent_raw()) == 1, "una aprobación, y ninguna transacción más"
    aprobacion = node.sent_raw()[0].lower()
    assert SELECTOR_APPROVE_ERC20 in aprobacion
    # El importe se calcula aquí a partir de lo que cuesta la orden —10 x 0,62—
    # en vez de transcribirlo, para que no pueda quedar desfasado.
    assert f"{COSTE_RAW:064x}" in aprobacion
    assert MAXIMO_UINT256 not in aprobacion
    # Las dos operaciones pasan por el diálogo: son dos cosas reales.
    assert len(prompt.asked) == 2
    assert [record.decision for record in use_case.gateway.history] == [
        Decision.APPROVED,
        Decision.APPROVED,
    ]


async def test_con_el_permiso_ya_concedido_no_se_emite_ninguna_transaccion(
    tmp_path: Path,
) -> None:
    """Aprobar cuando ya alcanza sería una transacción de más y gas tirado."""
    use_case, node, ledger, prompt, _, motor = await _armed(
        tmp_path, node=_node(eth_call=hex(10**18))
    )

    recibido = await _comprar(use_case)

    assert recibido.order_id == ORDEN_ID
    assert node.sent_raw() == []
    assert len(prompt.asked) == 1, "sólo la orden"
    assert len(ledger.entries()) == 1, "sólo el asiento de la orden"
    assert len(motor.publicadas) == 1


async def test_si_la_aprobacion_no_cuaja_no_se_publica_la_orden(
    tmp_path: Path,
) -> None:
    """Sin permiso el recinto rechazaría la orden: publicarla sería a sabiendas.

    Y la aprobación fallida **sí** queda anotada: una transacción emitida y pagada
    que no aparece en el registro es justo la clase de hueco que hace inútil un
    registro.
    """
    use_case, node, ledger, _, _, motor = await _armed(
        tmp_path,
        node=_node(eth_getTransactionReceipt={"blockNumber": "0x10", "status": "0x0"}),
    )

    with pytest.raises(ExecutionError, match="aprobación"):
        await _comprar(use_case)

    assert len(node.sent_raw()) == 1, "sólo se emitió la aprobación fallida"
    assert motor.publicadas == []
    (asiento,) = ledger.entries()
    assert asiento.status == "reverted"


# --------------------------------------------------------------------------- #
# 5. Vender: el permiso de las participaciones
# --------------------------------------------------------------------------- #
async def test_vender_autoriza_la_coleccion_entera_y_lo_dice(tmp_path: Path) -> None:
    """Vender entrega participaciones, y el permiso de un ERC-1155 no tiene importe.

    El estándar sólo permite autorizar la colección completa, así que el diálogo
    tiene que decirlo con esas palabras: alcanza a **todas** las participaciones
    de esa cartera en ese contrato, no sólo a las de esta orden. No es una
    elección de este código, pero sí una decisión que el usuario toma, y tomarla
    sin saberlo no sería tomarla.
    """
    use_case, node, ledger, prompt, _, _ = await _armed(tmp_path)

    await _comprar(use_case, side=PredictionSide.SELL)

    assert len(node.sent_raw()) == 1
    permiso = node.sent_raw()[0].lower()
    assert SELECTOR_SET_APPROVAL_FOR_ALL in permiso
    # El operador autorizado es el recinto, y la bandera va en uno.
    assert f"{'0' * 24}{EXCHANGE[2:]}" in permiso
    assert f"{'0' * 63}1" in permiso
    # Y lo que **no** puede aparecer: un importe. No hay ninguno que autorizar.
    assert MAXIMO_UINT256 not in permiso

    aviso = " ".join(prompt.asked[0].details)
    assert "NO tiene importe" in aviso
    assert "todas las participaciones" in aviso

    aprobacion, orden = ledger.entries()
    # Se anota como «participaciones» y con importe cero: no hay cifra que
    # conceder, y el asiento no inventa una que nadie autorizó.
    assert aprobacion.notional_symbol == "participaciones"
    assert aprobacion.notional == "0"
    assert orden.notional == "6.2", "la orden sí mueve valor"


async def test_vender_con_el_permiso_ya_concedido_no_emite_nada(tmp_path: Path) -> None:
    """Quien ya autorizó la colección no tiene por qué volver a pagar el gas."""
    use_case, node, ledger, prompt, _, _ = await _armed(tmp_path, node=_node(eth_call="0x1"))

    await _comprar(use_case, side=PredictionSide.SELL)

    assert node.sent_raw() == []
    assert len(prompt.asked) == 1
    (asiento,) = ledger.entries()
    assert asiento.kind == "order"


async def test_comprar_nunca_autoriza_la_coleccion_de_participaciones(
    tmp_path: Path,
) -> None:
    """Comprar mueve colateral, no participaciones: el permiso tiene que ser el otro.

    Es la mitad que impide que el camino conceda de más por ir por la rama
    equivocada: un `setApprovalForAll` en una compra daría al recinto el manejo de
    toda la colección sin que hiciera falta.
    """
    use_case, node, _, _, _, _ = await _armed(tmp_path)

    await _comprar(use_case, side=PredictionSide.BUY)

    assert SELECTOR_SET_APPROVAL_FOR_ALL not in node.sent_raw()[0].lower()


# --------------------------------------------------------------------------- #
# 6. El asiento de la orden
# --------------------------------------------------------------------------- #
async def test_el_asiento_dice_que_no_es_una_transaccion(tmp_path: Path) -> None:
    """Las dos cadenas tienen la misma forma y no se distinguen mirándolas.

    Por eso el registro lleva un campo que lo dice, y la descripción lo repite:
    quien lea el fichero tiene que poder saber que ese identificador es del
    recinto y que no hay ningún bloque donde buscarlo.
    """
    use_case, _, ledger, _, _, _ = await _armed(tmp_path)

    await _comprar(use_case)

    _, orden = ledger.entries()
    assert orden.kind == "order"
    assert orden.tx_hash == ORDEN_ID
    assert orden.status == ESTADO
    assert orden.recipient == DIRECCION
    assert orden.engine_id == "polymarket"
    assert orden.pair == "¿Llegará el Bitcoin a 100 000 dólares en octubre? — Sí"
    assert "no es una transacción" in orden.description
    # Una orden cuenta **siempre** contra el tope: sólo se anota cuando el recinto
    # ya la aceptó, así que el renglón es la prueba de que el dinero se movió.
    assert orden.counts_towards_limits


async def test_el_estado_del_recinto_se_anota_sin_traducir(tmp_path: Path) -> None:
    """`matched` no es `live`, y traducirlo perdería la palabra que hay que buscar.

    Un estado traducido a un vocabulario nuestro perdería justo el término con el
    que se consulta la documentación del recinto el día que algo no cuadre.
    """
    use_case, _, ledger, _, _, _ = await _armed(
        tmp_path,
        motor=MotorFalso(estado="matched"),
        node=_node(eth_call=hex(10**18)),
    )

    await _comprar(use_case)

    (orden,) = ledger.entries()
    assert orden.status == "matched"


async def test_la_orden_anotada_es_la_que_se_firmo_y_se_publico(tmp_path: Path) -> None:
    """Se firma el mismo objeto que se enseñó, y se publica el mismo que se firmó.

    No se reconstruye nada entre el diálogo y la firma: si se reconstruyera,
    podría haber dos respuestas que se separaran entre lo confirmado y lo firmado.
    Se comprueba sobre el objeto que el recinto recibió.
    """
    use_case, _, ledger, prompt, _, motor = await _armed(tmp_path, node=_node(eth_call=hex(10**18)))

    await _comprar(use_case)

    (publicada,) = motor.publicadas
    assert publicada.order.price == PRECIO
    assert publicada.order.size == TAMANO
    assert publicada.order.outcome_label == "Sí"
    assert publicada.signer == DIRECCION
    assert publicada.order is prompt.asked[0].transaction
    (orden,) = ledger.entries()
    assert orden.notional == "6.2"
    assert orden.notional_symbol == "USDC"


# --------------------------------------------------------------------------- #
# 7. La clave no se filtra
# --------------------------------------------------------------------------- #
async def test_la_clave_no_aparece_ni_en_el_registro_ni_en_el_log(
    tmp_path: Path,
) -> None:
    """Se busca la cadena literal en lo que se escribió, no se confía en nadie.

    Un `executions.jsonl` con la clave dentro sería el peor sitio posible para
    ella: texto plano, que crece, que se copia en una copia de seguridad y que se
    adjunta a un informe de fallo. Y el cuerpo que viaja al recinto tampoco puede
    llevarla: lleva la **firma**, que es pública.
    """
    use_case, _, ledger, _, _, motor = await _armed(tmp_path)

    with capture_logs() as registros:
        await _comprar(use_case)

    escrito = ledger.path.read_text(encoding="utf-8")
    assert CLAVE not in escrito
    assert CLAVE not in repr(ledger.entries())
    assert CLAVE not in motor.publicadas[0].payload
    volcado = json.dumps(registros, default=str)
    assert CLAVE not in volcado
    assert DIRECCION in volcado, "la dirección sí: es lo que el usuario necesita ver"


async def test_la_clave_no_se_pide_hasta_que_hace_falta(tmp_path: Path) -> None:
    """Construir la orden no ve la clave; firmarla y publicarla sí.

    Es lo que permite enseñar la orden entera antes de que exista ningún secreto
    en juego. Se mide contando, porque el orden de las llamadas no se ve en el
    resultado.
    """
    use_case, _, _, _, keys, motor = await _armed(tmp_path)

    await _comprar(use_case)

    assert motor.claves_vistas == [CLAVE, CLAVE], "una al firmar y otra al publicar"
    assert keys.pedida >= 2


# --------------------------------------------------------------------------- #
# 8. Apoyo
# --------------------------------------------------------------------------- #
async def test_la_direccion_que_firma_es_la_de_la_clave(tmp_path: Path) -> None:
    """La interfaz muestra de qué cartera sale el dinero, y es la derivada."""
    use_case, _, _, _, _, _ = await _armed(tmp_path)

    assert use_case.address() == DIRECCION


async def test_sin_clave_no_se_dice_ninguna_direccion(tmp_path: Path) -> None:
    """`None` es «no hay clave», y la interfaz lo tiene que poder decir.

    Se distingue de «hay clave y su dirección es ésta» porque son dos cosas
    distintas para quien lee la pantalla: una dice que hay que configurar algo, y
    la otra que ya está.
    """
    sin_clave, _, _, _, _, _ = await _armed(tmp_path, hay_clave=False)
    con_clave, _, _, _, _, _ = await _armed(tmp_path)

    assert sin_clave.address() is None
    assert con_clave.address() == DIRECCION
