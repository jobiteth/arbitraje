"""El camino por el que el dinero sale de una red y aparece en otra.

Lo que se fija aquí es lo mismo que en `test_execute_swap` —dónde podría perderse
dinero— más lo que sólo existe en un puente:

1. **Que no se emita fuera del modo `EJECUCIÓN`**, y que la barrera se compruebe
   **antes de trabajar**: la prueba mira que al motor no se le llegó a pedir el
   payload y que la clave no se tocó, no sólo que lanzara.
2. **Que la red de destino también pase la lista blanca.** Un puente que sale de
   una red permitida hacia una que no lo está tiene que caer, y tiene que caer
   antes de construir nada: la de destino es donde el dinero va a acabar.
3. **Que el destino del payload sea el que el motor declara.** Aquí un `to`
   equivocado no pierde dinero en una operación de mercado: lo manda a otra parte
   y no vuelve.
4. **Que no se firme una aprobación encadenada.** Conceder la primera de las dos
   dejaría al contrato autorizado sobre el token sin que el puente ocurra.
5. **Que el asiento se escriba en la red de origen**, que es de donde salió el
   dinero, y con la etiqueta de las dos patas.
6. **Que la clave privada no acabe ni en el registro ni en el log.**

Se usa un `EvmBroadcaster` **real** sobre un nodo de mentira y la cuenta 0 de
Hardhat —publicada, sin fondos, conocida— para que la firma sea de verdad.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_utils.crypto import keccak
from structlog.testing import capture_logs

from amigocompora.app.confirmation import ConfirmationGateway, RecordingPrompt
from amigocompora.app.execution_policy import (
    AutonomyPolicy,
    ExecutionLedger,
    PrivateKeySource,
)
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.execute_bridge import ExecuteBridge
from amigocompora.app.usecases.prepare_bridge import PrepareBridge
from amigocompora.domain.addresses import shorten
from amigocompora.domain.chains import chain
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
    ExecutionError,
    ExecutionLimitExceededError,
    ModeNotPermittedError,
    UnsupportedOperationError,
)
from amigocompora.domain.execution import ExecutionLimits, TriggerKind
from amigocompora.domain.models import (
    BridgeQuote,
    BridgeRequest,
    BroadcastStatus,
    Measurement,
    Token,
    TokenApproval,
    UnsignedTransaction,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.infra.evm.broadcast import EvmBroadcaster, build_approve_calldata
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool

AHORA = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
CHAIN_ID = 8453
#: El diamante de LI.FI en Base, medido en vivo. Se escribe entero y a mano: es
#: el contrato contra el que se contrasta el destino, y sacarlo de una constante
#: del producto dejaría la prueba comprobándose a sí misma.
CONTRATO = "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae"
OTRO = "0x00000000000000000000000000000000deadbeef"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_POLYGON = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
RECEPTOR = "0x2c887da24c6e6c939b0fe3adae50814b938b44f9"
CALLDATA = "0x1794958f" + "ab" * 32

#: Cuenta 0 de Hardhat: publicada, sin fondos en ninguna red real.
CLAVE = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

#: Selector ERC-20 de `approve(address,uint256)`, escrito a mano desde el
#: estándar y no pedido a `build_approve_calldata`.
SELECTOR_APPROVE = "095ea7b3"
MAXIMO_UINT256 = "f" * 64


# --------------------------------------------------------------------------- #
# Dobles
# --------------------------------------------------------------------------- #
class FakeKey(PrivateKeySource):
    """Clave que cuenta cuántas veces se pide.

    El contador no es decorativo: es lo que permite afirmar que en los modos que
    no ejecutan no se llegó a tocar la clave.
    """

    def __init__(self) -> None:
        self.pedida = 0

    def available(self) -> bool:
        return True

    def require(self) -> str:
        self.pedida += 1
        return CLAVE


class FakePassphrase:
    def get(self) -> str | None:
        return None


class _Planner:
    """Motor de puentes que construye lo que se le diga y cuenta lo que construyó."""

    __slots__ = (
        "_approval",
        "_calldata",
        "_construidos",
        "_declara",
        "_manifest",
        "_to",
        "_value",
        "al_planear",
    )

    def __init__(
        self,
        *,
        engine_id: str = "fake",
        to_address: str = CONTRATO,
        calldata: str = CALLDATA,
        value: int = 0,
        declares: str | None = CONTRATO,
        approval: TokenApproval | None = None,
        chains: frozenset[str] = frozenset({"base"}),
    ) -> None:
        self._manifest = EngineManifest(
            engine_id=engine_id,
            name="Motor de puentes falso",
            version="1.0.0",
            kind=EngineKind.CROSS_CHAIN,
            summary="Doble de prueba.",
            capabilities=frozenset({Capability.READ_CHAIN, Capability.PREPARE_TX}),
            bridge_chains=chains,
        )
        self._to = to_address
        self._calldata = calldata
        self._value = value
        self._declara = declares
        self._approval = approval
        #: Cuántas veces se le pidió el payload. Mide «se gastó trabajo en una
        #: operación que el modo no permite», que es lo que la barrera evita y lo
        #: único que lo detecta: el `authorize` posterior bloquearía igual, sólo
        #: que ya con el trabajo hecho.
        self._construidos = 0
        #: Se llama justo antes de construir. Sirve para mirar el estado del mundo
        #: en ese instante —lo usa la prueba que comprueba que construir un payload
        #: no necesita la clave privada—.
        self.al_planear: Callable[[], None] | None = None

    @property
    def construidos(self) -> int:
        return self._construidos

    @property
    def manifest(self) -> EngineManifest:
        return self._manifest

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def quote_bridge(self, request: BridgeRequest) -> Sequence[BridgeQuote]:
        return ()

    async def plan_bridge(
        self, quote: BridgeQuote, *, recipient: str
    ) -> UnsignedTransaction:
        self._construidos += 1
        if self.al_planear is not None:
            self.al_planear()
        return UnsignedTransaction(
            chain_id=CHAIN_ID,
            to_address=self._to,
            calldata=self._calldata,
            value=TokenAmount(self._value, 18, "ETH"),
            description=f"puente de prueba hacia {recipient}",
            approval=self._approval,
        )

    def expected_destination(self, chain_key: str) -> str | None:
        return self._declara


class _Valuation:
    """Valoración de mentira: no cotiza, contesta lo que se le diga.

    Cuenta las veces que se le pregunta porque hay una propiedad que sólo se ve
    así: cuando la pata de origen **ya** es la moneda de referencia —el caso normal
    de un puente, que cruza la stablecoin de su red— no se le pregunta nada.
    Valorar ahí sería cambiar un hecho por una estimación del mismo número.
    """

    def __init__(self, *, value: TokenAmount | None, razon: str = "no hay ruta") -> None:
        self._value = value
        self._razon = razon
        self.preguntas = 0

    async def __call__(
        self, spent: Token, amount: TokenAmount, reference: Token
    ) -> TokenAmount | None:
        self.preguntas += 1
        del spent, amount, reference
        return self._value

    def describe_failure(self, spent: Token, reference: Token) -> str:
        return f"no se pudo valorar {spent.symbol} en {reference.symbol}: {self._razon}"


class _Provider:
    def __init__(self, engine: _Planner) -> None:
        self.engine = engine

    @property
    def manifest(self) -> EngineManifest:
        return self.engine.manifest

    def create(self, config: Mapping[str, str]) -> _Planner:
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
                # Un método no previsto se contesta como error y no con un valor
                # inventado: si el camino llama a algo que la prueba no esperaba,
                # tiene que notarse.
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
    """El hash que el nodo devolvería: el keccak del propio `raw` emitido.

    Se calcula y no se inventa porque el emisor compara el hash que contesta el
    nodo con el que él calculó al firmar: un hash distinto significa que se emitió
    algo que no es lo que se cree, y la prueba tiene que poder distinguirlo.
    """
    return "0x" + keccak(bytes.fromhex(str(params[0])[2:])).hex()


def _permiso_tras_aprobar(importe: int) -> Callable[[list[Any]], str]:
    """`eth_call` que contesta cero y, desde la segunda vez, el importe aprobado.

    Es lo que hace un nodo de verdad después de que la aprobación mine, y sin
    esto la prueba no podría distinguir «se aprobó y el permiso está» de «se
    aprobó y el permiso no llegó»: el camino vuelve a leer la autorización a
    propósito.
    """
    estado = {"llamadas": 0}

    def handler(params: list[Any]) -> str:
        del params
        estado["llamadas"] += 1
        return "0x0" if estado["llamadas"] == 1 else hex(importe)

    return handler


def _node(**overrides: Any) -> FakeNode:
    handlers: dict[str, Any] = {
        "eth_chainId": hex(CHAIN_ID),
        "eth_getTransactionCount": "0x5",
        "eth_estimateGas": "0x30d40",
        "eth_getBlockByNumber": {"baseFeePerGas": "0x3b9aca00"},
        "eth_maxPriorityFeePerGas": "0x3b9aca",
        "eth_call": hex(10**18),  # permiso de sobra por omisión
        "eth_sendRawTransaction": _hash_of,
        "eth_getTransactionReceipt": {"blockNumber": "0x10", "status": "0x1"},
    }
    handlers.update(overrides)
    return FakeNode(**handlers)


def _broadcaster(node: FakeNode) -> EvmBroadcaster:
    pool = RpcPool(
        "base",
        (RpcEndpoint(url="https://base.example.org", label="falso", priority=10),),
        httpx.AsyncClient(transport=node.transport()),
        clock=FrozenClock(AHORA),
    )
    return EvmBroadcaster(
        pool,
        "base",
        clock=FrozenClock(AHORA),
        receipt_poll_interval_seconds=0.0,
        receipt_poll_attempts=2,
    )


# --------------------------------------------------------------------------- #
# Armado
# --------------------------------------------------------------------------- #
def _solicitud(
    *, origen: Token | None = None, cantidad: Decimal = Decimal("1")
) -> BridgeRequest:
    """La solicitud de la prueba, con el importe **en la escala del origen**.

    La escala se deriva del token y no se escribe fija: `BridgeRequest` exige que
    el importe sea del token de origen, así que escribir «1 000 000» con un origen
    de 18 decimales estaría describiendo un puente de 0,000000000001 ETH —válido
    para el dominio y con un tope que no se parece al que se cree estar probando—.
    """
    token = origen or Token("USDC", 6, "base", USDC_BASE)
    return BridgeRequest(
        origin=token,
        destination=Token("USDC", 6, "polygon", USDC_POLYGON),
        amount_in=TokenAmount(int(cantidad * 10**token.decimals), token.decimals, token.symbol),
    )


def _quote(*, request: BridgeRequest | None = None, engine_id: str = "fake") -> BridgeQuote:
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


def _limits(**overrides: Any) -> ExecutionLimits:
    values: dict[str, Any] = {
        # Estos límites describen una ejecución **permitida**: el interruptor
        # maestro se prueba aparte, apagarlo aquí haría fallar todas las pruebas
        # por el mismo motivo y ninguna por el suyo.
        "enabled": True,
        "max_quote_per_trade": Decimal("5000"),
        "max_quote_per_day": Decimal("20000"),
        "allowed_tokens": frozenset({"USDC", "WETH"}),
        "allowed_chains": frozenset({"base", "polygon"}),
        "allowed_engines": frozenset({"fake"}),
    }
    values.update(overrides)
    return ExecutionLimits(**values)


async def _armed(
    tmp_path: Path,
    *,
    mode: OperationMode = OperationMode.EXECUTION,
    planner: _Planner | None = None,
    limits: ExecutionLimits | None = None,
    node: FakeNode | None = None,
    answer: bool = True,
    valuation: _Valuation | None = None,
) -> tuple[ExecuteBridge, FakeNode, ExecutionLedger, RecordingPrompt, FakeKey, _Planner]:
    """Un `ExecuteBridge` completo, con emisor **real** sobre un nodo de mentira."""
    clock = FrozenClock(AHORA)
    ledger = ExecutionLedger(tmp_path / "executions.jsonl", clock=clock)
    keys = FakeKey()
    policy = AutonomyPolicy(
        keys=keys,
        passphrase=FakePassphrase(),
        limits=limits or _limits(),
        ledger=ledger,
        clock=clock,
        trigger=TriggerKind.MANUAL,
    )
    registry = EngineRegistry(ModeGuard(mode))
    engine = planner or _Planner()
    registry.register(_Provider(engine))
    # Registrado no basta: se busca entre los **activos**, así que hay que
    # activarlo como lo haría el panel de motores.
    await registry.activate(engine.manifest.engine_id)
    prompt = RecordingPrompt(answer=answer)
    gateway = ConfirmationGateway(ModeGuard(mode), prompt=prompt, clock=clock)

    fake = node or _node()
    execute = ExecuteBridge(
        prepare=PrepareBridge(registry=registry, gateway=gateway),
        gateway=gateway,
        keys=keys,
        policy=policy,
        broadcasters={"base": _broadcaster(fake)},
        clock=clock,
        valuation=valuation,
    )
    return execute, fake, ledger, prompt, keys, engine


# --------------------------------------------------------------------------- #
# 1. La barrera
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mode",
    [OperationMode.OBSERVATION, OperationMode.SIMULATION, OperationMode.ASSISTED],
)
async def test_fuera_de_ejecucion_no_se_trabaja(
    tmp_path: Path, mode: OperationMode
) -> None:
    """Que lance es la mitad de la propiedad; la otra es que no se trabajó.

    Un caso de uso que comprobara el modo después de construir pasaría una prueba
    que sólo mirara el error, y estaría pidiendo red con una operación que ya sabe
    que no puede emitir. Se afirma sobre el contador del motor y sobre la clave.
    """
    planner = _Planner()
    execute, node, _ledger, _prompt, keys, _ = await _armed(
        tmp_path, mode=mode, planner=planner
    )

    with pytest.raises(ModeNotPermittedError):
        await execute(_quote(), recipient=RECEPTOR)

    assert planner.construidos == 0
    assert keys.pedida == 0
    assert node.calls == []


async def test_sin_confirmar_no_se_emite_nada(tmp_path: Path) -> None:
    execute, node, ledger, prompt, _keys, _ = await _armed(tmp_path, answer=False)

    with pytest.raises(ConfirmationDeniedError):
        await execute(_quote(), recipient=RECEPTOR)

    assert node.sent_raw() == []
    assert ledger.entries() == ()
    assert prompt.asked


# --------------------------------------------------------------------------- #
# 2. Los topes, y las dos redes
# --------------------------------------------------------------------------- #
async def test_la_red_de_destino_fuera_de_la_lista_blanca_no_se_cruza(
    tmp_path: Path,
) -> None:
    """Es la comprobación que justifica mirar las dos redes y no sólo el origen.

    Y se hace **antes** de construir: la prueba lo mide en el contador del motor,
    porque un límite comprobado después del trabajo ya gastó el trabajo.
    """
    planner = _Planner()
    execute, node, _ledger, _prompt, _keys, _ = await _armed(
        tmp_path, planner=planner, limits=_limits(allowed_chains=frozenset({"base"}))
    )

    with pytest.raises(ExecutionLimitExceededError, match="polygon"):
        await execute(_quote(), recipient=RECEPTOR)

    assert planner.construidos == 0
    assert node.sent_raw() == []


async def test_por_encima_del_tope_por_operacion_no_se_firma(tmp_path: Path) -> None:
    execute, node, _ledger, _prompt, _keys, _ = await _armed(
        tmp_path, limits=_limits(max_quote_per_trade=Decimal("0.5"))
    )

    with pytest.raises(ExecutionLimitExceededError, match="importe por operación"):
        await execute(_quote(), recipient=RECEPTOR)

    assert node.sent_raw() == []


async def test_un_motor_fuera_de_la_lista_blanca_no_firma(tmp_path: Path) -> None:
    execute, _node, _ledger, _prompt, _keys, _ = await _armed(
        tmp_path, limits=_limits(allowed_engines=frozenset({"otro"}))
    )

    with pytest.raises(ExecutionLimitExceededError, match="motores permitidos"):
        await execute(_quote(), recipient=RECEPTOR)


# --------------------------------------------------------------------------- #
# 3. El destino
# --------------------------------------------------------------------------- #
async def test_un_destino_que_el_motor_no_declara_no_se_firma(tmp_path: Path) -> None:
    """Un payload que apunte a otro contrato es exactamente lo que no hay que firmar.

    En un swap eso perdería dinero en una operación de mercado; aquí lo manda a
    otra parte y no vuelve.
    """
    planner = _Planner(to_address=OTRO)
    execute, node, _ledger, _prompt, _keys, _ = await _armed(tmp_path, planner=planner)

    with pytest.raises(ExecutionError, match="no es el que el motor dice usar"):
        await execute(_quote(), recipient=RECEPTOR)

    assert node.sent_raw() == []


async def test_un_motor_que_no_declara_destino_no_se_firma(tmp_path: Path) -> None:
    """Sin tabla medida no hay contra qué comprobar, y en un puente eso no es una
    formalidad: es la única comprobación que impide mandar el dinero a otro sitio."""
    planner = _Planner(declares=None)
    execute, node, _ledger, _prompt, _keys, _ = await _armed(tmp_path, planner=planner)

    with pytest.raises(ExecutionError, match="no declara a qué contrato"):
        await execute(_quote(), recipient=RECEPTOR)

    assert node.sent_raw() == []


async def test_una_red_no_evm_se_rechaza_antes_de_construir(tmp_path: Path) -> None:
    planner = _Planner()
    execute, _node, _ledger, _prompt, _keys, _ = await _armed(tmp_path, planner=planner)
    # Se sube el límite a lo que haga falta: lo que se prueba es el orden, no el tope.
    otra = BridgeRequest(
        origin=Token("USDC", 6, "solana", None),
        destination=Token("USDC", 6, "polygon", USDC_POLYGON),
        amount_in=TokenAmount(1_000_000, 6, "USDC"),
    )

    with pytest.raises(UnsupportedOperationError):
        await execute(_quote(request=otra), recipient=RECEPTOR)

    assert planner.construidos == 0


# --------------------------------------------------------------------------- #
# 4. El camino bueno
# --------------------------------------------------------------------------- #
async def test_el_camino_bueno_firma_emite_y_anota(tmp_path: Path) -> None:
    execute, node, ledger, _prompt, _keys, _ = await _armed(tmp_path)

    receipt = await execute(_quote(), recipient=RECEPTOR)

    assert receipt.status is BroadcastStatus.SUCCESS
    assert len(node.sent_raw()) == 1
    (entrada,) = ledger.entries()
    # El asiento va en la red de **origen**, que es de donde salió el dinero, y
    # la etiqueta nombra las dos patas: sin la de destino, un renglón de un puente
    # no se distingue del de un swap en la red de origen.
    assert entrada.chain == "base"
    assert entrada.pair == "USDC@base → USDC@polygon"
    assert entrada.engine_id == "fake"


async def test_la_direccion_que_firma_es_la_de_la_clave(tmp_path: Path) -> None:
    execute, _node, _ledger, _prompt, _keys, _ = await _armed(tmp_path)
    assert execute.address() == DIRECCION


async def test_a_la_emision_le_llega_el_contrato_que_el_motor_declara(
    tmp_path: Path,
) -> None:
    """La segunda barrera, al emitir, y por eso se mira la transacción que salió.

    Entre el contraste y la emisión el motor pide red, así que son dos momentos
    distintos: lo que se firma tiene que seguir siendo lo que se comprobó.
    """
    execute, node, _ledger, _prompt, _keys, _ = await _armed(tmp_path)

    await execute(_quote(), recipient=RECEPTOR)

    raw = node.sent_raw()[0]
    assert CONTRATO[2:] in raw.lower()


# --------------------------------------------------------------------------- #
# 5. La aprobación
# --------------------------------------------------------------------------- #
async def test_sin_permiso_se_aprueba_lo_justo_y_luego_se_cruza(tmp_path: Path) -> None:
    """La aprobación es por el importe **justo**, nunca ilimitada.

    Un `approve` sin límite deja al contrato autorizado a vaciar ese token para
    siempre, y es el patrón que convierte un contrato comprometido en una pérdida
    total.
    """
    node = _node(eth_call=_permiso_tras_aprobar(1_000_000))
    execute, _, ledger, _prompt, _keys, _ = await _armed(tmp_path, node=node)

    await execute(_quote(), recipient=RECEPTOR)

    # Dos transacciones: la aprobación y el puente.
    assert len(node.sent_raw()) == 2
    aprobacion = node.sent_raw()[0]
    assert SELECTOR_APPROVE in aprobacion
    assert MAXIMO_UINT256 not in aprobacion
    assert f"{1_000_000:064x}" in aprobacion
    # Las dos quedan anotadas: la aprobación es un hecho económico aunque no
    # cruce nada, y sin su asiento el registro sólo contaría la mitad.
    assert [e.pair for e in ledger.entries()] == ["USDC@base → USDC@polygon"] * 2


async def test_con_permiso_suficiente_no_se_aprueba(tmp_path: Path) -> None:
    """El nodo contesta un permiso de sobra: no hay nada que conceder."""
    execute, node, _ledger, _prompt, _keys, _ = await _armed(tmp_path)

    await execute(_quote(), recipient=RECEPTOR)

    assert len(node.sent_raw()) == 1


async def test_un_origen_nativo_no_se_aprueba(tmp_path: Path) -> None:
    """El nativo se entrega como `value`: no hay contrato sobre el que autorizar.

    No es una suposición: `_token_to_approve` devuelve la dirección del token
    entregado, así que la única forma de que este camino no apruebe es que el
    nativo no tenga dirección. Se afirma sobre lo emitido y sobre el registro, que
    es lo que se vería después.
    """
    origen = Token("ETH", 18, "base", None)
    planner = _Planner(value=10**15)
    execute, node, ledger, _prompt, _keys, _ = await _armed(
        tmp_path,
        planner=planner,
        limits=_limits(allowed_tokens=frozenset({"ETH", "USDC"})),
        valuation=_Valuation(value=TokenAmount(2_500_000, 6, "USDC")),
    )

    await execute(_quote(request=_solicitud(origen=origen)), recipient=RECEPTOR)

    assert len(node.sent_raw()) == 1
    # Una firma y **dos** asientos sería un desastre silencioso: el registro
    # contaría una aprobación que nunca ocurrió.
    assert len(ledger.entries()) == 1


async def test_sin_saber_cuanto_vale_no_se_cruza(tmp_path: Path) -> None:
    """Un tope que se salta cuando no se puede medir no es un tope.

    Se entrega ETH, que no es la moneda en la que están escritos los límites: sin
    valorador, la única salida honesta es no cruzar. Y se mira que no se construyó
    nada, no sólo que fallara.
    """
    planner = _Planner(value=10**15)
    execute, node, _ledger, _prompt, _keys, _ = await _armed(
        tmp_path,
        planner=planner,
        limits=_limits(allowed_tokens=frozenset({"ETH", "USDC"})),
    )

    with pytest.raises(ExecutionError, match="operar sin límite"):
        await execute(
            _quote(
                request=_solicitud(
                    origen=Token("ETH", 18, "base", None), cantidad=Decimal("0.001")
                )
            ),
            recipient=RECEPTOR,
        )

    assert planner.construidos == 0
    assert node.sent_raw() == []


async def test_una_aprobacion_encadenada_no_se_concede_a_medias(
    tmp_path: Path,
) -> None:
    """Conceder la primera de las dos dejaría el token autorizado sin cruzar nada.

    Y no se construyó nada: la prueba lo mide en el nodo y en el registro, no sólo
    en el error.
    """
    planner = _Planner(approval=TokenApproval(spender=CONTRATO, via=OTRO))
    execute, node, ledger, _prompt, _keys, _ = await _armed(tmp_path, planner=planner)

    with pytest.raises(UnsupportedOperationError, match="encadenada"):
        await execute(_quote(), recipient=RECEPTOR)

    assert node.sent_raw() == []
    assert ledger.entries() == ()


def test_solo_se_aprueba_lo_que_el_contrato_va_a_mover() -> None:
    """El envoltorio pagado como nativo no se aprueba: el payload manda `value`."""
    from amigocompora.app.usecases.execute_bridge import _token_to_approve

    spec = chain("base")
    nativo_envuelto = Token(
        "WETH", 18, "base", spec.wrapped_native or "0x" + "0" * 40
    )
    con_valor = UnsignedTransaction(
        chain_id=CHAIN_ID,
        to_address=CONTRATO,
        calldata=CALLDATA,
        value=TokenAmount(10**15, 18, "ETH"),
        description="con valor",
    )
    sin_valor = UnsignedTransaction(
        chain_id=CHAIN_ID,
        to_address=CONTRATO,
        calldata=CALLDATA,
        value=TokenAmount(0, 18, "ETH"),
        description="sin valor",
    )
    assert _token_to_approve(con_valor, nativo_envuelto, spec) is None
    assert _token_to_approve(sin_valor, nativo_envuelto, spec) == nativo_envuelto.address
    assert _token_to_approve(con_valor, Token("ETH", 18, "base", None), spec) is None


def test_el_calldata_de_la_aprobacion_es_el_del_estandar() -> None:
    """Se comprueba contra el selector escrito a mano y no contra sí mismo."""
    calldata = build_approve_calldata(CONTRATO, 1_000_000)
    assert calldata[2:10] == SELECTOR_APPROVE
    assert calldata[10:74] == "0" * 24 + CONTRATO[2:]
    assert int(calldata[74:], 16) == 1_000_000


# --------------------------------------------------------------------------- #
# 6. La clave no aparece
# --------------------------------------------------------------------------- #
async def test_la_clave_no_aparece_ni_en_el_registro_ni_en_el_log(
    tmp_path: Path,
) -> None:
    """Se busca la cadena literal en lo escrito, no se confía en que nadie la ponga.

    Un `executions.jsonl` con la clave dentro sería el peor sitio posible para
    ella: un fichero de texto plano que crece y que se copia en una copia de
    seguridad.
    """
    execute, _node, ledger, _prompt, _keys, _ = await _armed(tmp_path)

    with capture_logs() as logs:
        await execute(_quote(), recipient=RECEPTOR)

    escrito = ledger.path.read_text(encoding="utf-8")
    assert CLAVE not in escrito
    assert CLAVE not in json.dumps(logs, default=str)


async def test_construir_el_payload_no_necesita_la_clave(tmp_path: Path) -> None:
    """La clave se toca al firmar, y construir no es firmar.

    Se mira en el instante en que el motor construye —dentro de `plan_bridge`, con
    la petición a medio camino— y no al final: al final la clave ya se pidió para
    averiguar la cartera, y una prueba que sólo contara el total no distinguiría
    «se pidió para firmar» de «se pidió para cualquier cosa».
    """
    execute, _node, _ledger, _prompt, keys, planner = await _armed(tmp_path)
    vistas: list[int] = []
    planner.al_planear = lambda: vistas.append(keys.pedida)

    await execute(_quote(), recipient=RECEPTOR)

    assert vistas == [0]


async def test_el_permiso_se_muestra_antes_de_firmar(tmp_path: Path) -> None:
    """El diálogo tiene que decir de qué red sale y en cuál aparece el dinero.

    Se comprueba sobre lo que se le **enseñó al usuario**, no sobre el registro:
    el usuario decide con lo que ve, y una operación que se anota bien pero se
    explica mal se aprueba a ciegas.
    """
    execute, _node, _ledger, prompt, _keys, _ = await _armed(tmp_path)

    await execute(_quote(), recipient=RECEPTOR)

    (accion,) = prompt.asked
    texto = " ".join(accion.details)
    assert accion.title == "Emitir puente por AcrossV4"
    # Irreversible porque el modo es EJECUCIÓN: desde aquí se firma y se emite de
    # verdad, y el diálogo tiene que decirlo.
    assert accion.is_irreversible
    assert "base" in texto
    assert "polygon" in texto
    assert "USDC@base → USDC@polygon" in texto
    # Las dos direcciones, con nombres distintos: el contrato al que se llama y la
    # cartera que recibe. Una etiqueta «destino» que valiera para las dos dejaría
    # al usuario sin poder ver que en un puente son dos cosas diferentes.
    assert f"Contrato que ejecuta: {shorten(CONTRATO)}" in texto
    assert f"Fondos a tu cartera: {shorten(RECEPTOR)}" in texto
    assert f"Firma la cartera: {DIRECCION}" in texto
    assert CLAVE not in texto
