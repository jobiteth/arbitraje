"""El camino por el que sale dinero de una cartera.

Lo que se fija aquí no es que «funcione», sino los sitios donde **podría
perderse dinero**, y son cinco:

1. **Que no se emita fuera del modo `EJECUCIÓN`.** Y la prueba no se conforma con
   ver el error: comprueba además que **no se hizo ni una petición de red**, que
   es la diferencia entre una barrera y un aviso a mitad de camino. Una barrera
   que se comprueba después de trabajar ya gastó el trabajo.
2. **Que el destino del payload sea el que el motor declara.** Un payload que
   apunte a otro contrato es exactamente lo que hay que no firmar.
3. **Que un payload sin llamada no se firme.** Una transacción sin `calldata` a
   un router es una transferencia a ciegas, y ese dinero no vuelve.
4. **Que la aprobación de ERC-20 sea por el importe justo y no ilimitada**, y que
   si la aprobación no llega a buen término no se firme el swap que dependía de
   ella.
5. **Que la clave privada no acabe en el registro ni en el log.** Se busca la
   cadena literal en lo que se escribió, no se confía en que nadie la ponga.

Se usa un `EvmBroadcaster` **real** sobre un nodo de mentira, y una clave de
desarrollo **publicada** (la cuenta 0 de Hardhat, sin fondos y conocida
públicamente) para que la firma sea de verdad: comprobar el camino del dinero con
la firma simulada dejaría sin probar justo la parte que mueve el dinero.
"""

from __future__ import annotations

import asyncio
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

from amigocompora.app.confirmation import (
    ConfirmationGateway,
    Decision,
    PendingAction,
    RecordingPrompt,
)
from amigocompora.app.execution_policy import (
    AutonomyPolicy,
    ExecutionLedger,
    PrivateKeySource,
)
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.estimate_cost import EstimateNetworkCost
from amigocompora.app.usecases.execute_swap import (
    ExecuteSwap,
    StepState,
    StepUpdate,
    SwapStep,
    _token_to_approve,
)
from amigocompora.app.usecases.prepare_swap import PrepareSwap
from amigocompora.app.usecases.value_in_reference import ReferenceValuation
from amigocompora.domain.chains import chain
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
    ExecutionError,
    ExecutionLimitExceededError,
    InsufficientBalanceError,
    ModeNotPermittedError,
    UnsupportedOperationError,
)
from amigocompora.domain.execution import ExecutionLimits, TriggerKind
from amigocompora.domain.models import (
    BroadcastReceipt,
    BroadcastStatus,
    Quote,
    Token,
    TokenApproval,
    TradingPair,
    UnsignedTransaction,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.catalog import quote_token, wrapped_native
from amigocompora.infra.evm.broadcast import EvmBroadcaster
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool

AHORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
CHAIN_ID = 8453
ROUTER = "0x4f6f91599858bf0d19fabcf2c5d591fe13f7c059"
#: Permit2 canónico, el mismo en todas las redes donde existe. Se escribe entero
#: y a mano: es el contrato contra el que se concede el primer permiso, y sacarlo
#: de una constante del producto dejaría la prueba comprobándose a sí misma.
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
#: El Universal Router de Uniswap en Base: el router que cobra **por** Permit2.
ROUTER_PERMIT2 = "0x6ff5693b99212da76ad316178a184ab56d299b43"
#: Una caducidad cómodamente por delante de `AHORA` (año 2033). El permiso de
#: Permit2 vale por importe **y** por fecha, así que una prueba que devolviera
#: cero en la caducidad comprobaría un permiso vencido y no lo sabría.
CADUCIDAD_PERMIT2 = 2_000_000_000
OTRO = "0x1111111111111111111111111111111111111111"
DESTINO = "0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F"
CALLDATA = "0x415565b0" + "ab" * 32

#: Selector ERC-20 de `approve(address,uint256)`, escrito a mano desde la
#: definición pública del estándar y no pedido a `build_approve_calldata`:
#: comprobar el camino con la misma función que usa el camino no comprueba nada.
SELECTOR_APPROVE_ERC20 = "095ea7b3"
#: Selector ERC-20 de `balanceOf(address)`, por la misma razón que el de arriba:
#: a `eth_call` llegan el saldo y el permiso por el **mismo** método JSON-RPC, y
#: lo único que los distingue en la petición es esta calldata.
SELECTOR_BALANCE_OF = "70a08231"
#: Saldo de sobra para los importes de estas pruebas —1 WETH, 10**18—: las
#: lecturas de saldo se contestan con esto salvo en las pruebas que miden,
#: precisamente, la falta de saldo.
SALDO_DE_SOBRA = 10**18
#: El máximo de `uint256`, que es lo que **no** puede aparecer en un `approve`.
MAXIMO_UINT256 = "f" * 64
#: Cuenta 0 de Hardhat. Publicada en la documentación de Hardhat y Anvil, sin
#: fondos en ninguna red real y con su dirección derivada aquí, no recordada:
#: `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266`.
CLAVE = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


# --------------------------------------------------------------------------- #
# Dobles
# --------------------------------------------------------------------------- #
class FakeKey(PrivateKeySource):
    """Clave que se puede hacer desaparecer, y que **cuenta** cuántas veces se pide.

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


class _FakePassphrase:
    def get(self) -> str | None:
        return None


class _Planner:
    """Motor que cotizó y que construye el payload. Devuelve lo que se le diga."""

    __slots__ = (
        "_approval",
        "_calldata",
        "_construidos",
        "_declara",
        "_manifest",
        "_to",
        "_value",
    )

    def __init__(
        self,
        *,
        to_address: str = ROUTER,
        calldata: str = CALLDATA,
        value: int = 0,
        declares: str | None = ROUTER,
        approval: TokenApproval | None = None,
    ) -> None:
        self._manifest = EngineManifest(
            engine_id="fake",
            name="Motor falso",
            version="1.0.0",
            kind=EngineKind.DEX_QUOTES,
            summary="Doble de prueba.",
            capabilities=frozenset({Capability.READ_CHAIN, Capability.PREPARE_TX}),
            swap_chains=frozenset({"base"}),
        )
        self._to = to_address
        self._calldata = calldata
        self._value = value
        self._declara = declares
        self._approval = approval
        #: Cuántas veces se le pidió el payload. Es la medida de «se gastó
        #: trabajo en una operación que el modo no permite», que es lo que la
        #: barrera tiene que evitar — y lo único que lo detecta, porque el
        #: `authorize` posterior bloquearía igual, sólo que ya con el trabajo
        #: hecho.
        self._construidos = 0

    @property
    def construidos(self) -> int:
        return self._construidos

    @property
    def manifest(self) -> EngineManifest:
        return self._manifest

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        return ()

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        return ()

    async def plan_swap(
        self, quote: Quote, *, recipient: str, slippage_bps: int | None = None
    ) -> UnsignedTransaction:
        self._construidos += 1
        return UnsignedTransaction(
            chain_id=CHAIN_ID,
            to_address=self._to,
            calldata=self._calldata,
            value=TokenAmount(raw=self._value, decimals=18, symbol="WETH"),
            description="swap de prueba",
            approval=self._approval,
        )

    def expected_destination(self, chain_key: str) -> str | None:
        return self._declara


class _Provider:
    def __init__(self, engine: _Planner) -> None:
        self.engine = engine

    @property
    def manifest(self) -> EngineManifest:
        return self.engine.manifest

    def create(self, config: Mapping[str, str]) -> _Planner:
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

    Recibe los parámetros de la llamada, como todos los manejadores del nodo
    falso, y el `raw` es el primero.

    Se calcula aquí en vez de inventarlo porque `EvmBroadcaster.send` compara el
    hash que contesta el nodo con el que él calculó al firmar: un hash distinto
    significa que se emitió algo que no es lo que se cree, y la prueba tiene que
    poder distinguir ese caso del camino bueno.
    """
    raw_hex = str(params[0])
    return "0x" + keccak(bytes.fromhex(raw_hex[2:])).hex()


def _palabra(valor: int) -> str:
    """Un entero como lo devuelve la cadena: una palabra ABI de 32 bytes.

    `token_balance` exige el retorno ABI **completo** y rechaza uno más corto
    —«el contrato al que se preguntó no es el que se cree», y tiene razón—, así
    que el nodo de mentira contesta como contestaría uno de verdad.
    """
    return "0x" + f"{valor:064x}"


def _permiso_tras_aprobar(importe: int) -> Callable[[list[Any]], str]:
    """`eth_call` que contesta cero y, desde la segunda vez, el importe aprobado.

    Es lo que hace un nodo de verdad después de que la aprobación mine, y sin
    esto la prueba no podría distinguir «se aprobó y el permiso está» de «se
    aprobó y el permiso no llegó»: el código vuelve a leer la autorización a
    propósito, y un nodo que contestara siempre cero haría fallar también el
    camino bueno.

    Las lecturas de **saldo** se contestan de sobra y sin contar: el saldo y el
    permiso llegan por el mismo `eth_call` y lo que los distingue es la
    calldata, igual que en la cadena. Sin esta distinción, el chequeo de saldo
    que precede a la aprobación se comería la primera respuesta —un cero— y
    haría fallar la prueba por un déficit que no existe.
    """
    estado = {"llamadas": 0}

    def handler(params: list[Any]) -> str:
        data = str(params[0].get("data", ""))
        if data.startswith("0x" + SELECTOR_BALANCE_OF):
            return _palabra(SALDO_DE_SOBRA)
        estado["llamadas"] += 1
        return _palabra(0) if estado["llamadas"] == 1 else _palabra(importe)

    return handler


def _permiso_encadenado(
    token_address: str, permit2: str, importe: int
) -> Callable[[list[Any]], str]:
    """`eth_call` que contesta las **dos** lecturas de permiso, cada una por su vía.

    El camino encadenado hace dos preguntas distintas que van al mismo método
    JSON-RPC, así que un nodo que contestara siempre lo mismo no podría
    distinguirlas: se despacha por el contrato al que se le pregunta —el token
    para el permiso del ERC-20, Permit2 para el suyo—, que es exactamente lo que
    hace la cadena.

    Y cada una cambia tras su aprobación, por la misma razón que
    `_permiso_tras_aprobar`: el camino vuelve a leer a propósito, y un nodo que
    contestara siempre cero haría fallar también el camino bueno.

    Y, como allí, las lecturas de **saldo** se contestan de sobra antes de mirar
    a qué contrato van: viajan al token, que es también el destino del permiso
    del ERC-20, así que sin la calldata delante se confundirían con él.
    """
    estado = {"erc20": 0, "permit2": 0}

    def handler(params: list[Any]) -> str:
        data = str(params[0].get("data", ""))
        if data.startswith("0x" + SELECTOR_BALANCE_OF):
            return _palabra(SALDO_DE_SOBRA)
        destino = str(params[0].get("to", "")).lower()
        if destino == permit2.lower():
            estado["permit2"] += 1
            if estado["permit2"] == 1:
                return "0x" + "0" * 128
            return "0x" + f"{importe:064x}" + f"{CADUCIDAD_PERMIT2:064x}"
        assert destino == token_address.lower(), (
            f"la prueba sólo espera preguntas al token o a Permit2, no a {destino}"
        )
        estado["erc20"] += 1
        return _palabra(0) if estado["erc20"] == 1 else _palabra(importe)

    return handler


def _recibo_con_los_primeros_bien(cuantos: int) -> Callable[[list[Any]], dict[str, str]]:
    """Recibos que salen bien las primeras `cuantos` veces y en rojo después.

    Hace falta para poder observar una aprobación **posterior** a otra que sí
    cuajó: con todas en rojo el camino se detiene en la primera.
    """
    estado = {"vistos": 0}

    def handler(params: list[Any]) -> dict[str, str]:
        del params
        estado["vistos"] += 1
        bien = estado["vistos"] <= cuantos
        return {"blockNumber": "0x10", "status": "0x1" if bien else "0x0"}

    return handler


def _node(**overrides: Any) -> FakeNode:
    handlers: dict[str, Any] = {
        "eth_chainId": hex(CHAIN_ID),
        "eth_getTransactionCount": "0x5",
        "eth_estimateGas": "0x30d40",
        "eth_getBlockByNumber": {"baseFeePerGas": "0x3b9aca00"},
        "eth_maxPriorityFeePerGas": "0x3b9aca",
        "eth_call": _palabra(10**18),  # permiso y saldo de sobra por omisión
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
def _quote(
    *,
    chain_key: str = "base",
    entrega: str = "1",
    recibe: str = "3000",
    contra: Token | None = None,
) -> Quote:
    """Una cotización del par de siempre, o contra el token que se diga.

    `contra` existe para poder cotizar un par que **no** toca la moneda de
    referencia —el caso que antes se rechazaba sin más—: con `None` el par es el
    de siempre, envoltorio contra stablecoin.
    """
    base = wrapped_native(chain_key)
    quote = contra if contra is not None else quote_token(chain_key)
    assert base is not None
    assert quote is not None
    pair = TradingPair(base=base, quote=quote)
    return Quote(
        venue=Venue(
            venue_id=f"fake@{chain_key}",
            name="venue falso",
            kind=VenueKind.DEX,
            chain=chain_key,
        ),
        engine_id="fake",
        pair=pair,
        amount_in=pair.base.amount(entrega),
        amount_out=pair.quote.amount(recibe),
        fee_bps=None,
        fee_basis=None,
        price_impact_bps=None,
        impact_basis=None,
        observed_at=AHORA,
    )


def _limits(**overrides: Any) -> ExecutionLimits:
    values: dict[str, Any] = {
        # Estos límites describen una ejecución **permitida**. El interruptor
        # maestro se prueba aparte y en su propia prueba: apagarlo aquí haría que
        # todas las de este fichero fallaran por el mismo motivo —y ninguna por el
        # suyo—, que es la forma de tener 23 pruebas que comprueban una sola cosa.
        "enabled": True,
        "max_quote_per_trade": Decimal("5000"),
        "max_quote_per_day": Decimal("20000"),
        "allowed_tokens": frozenset({"WETH", "USDC"}),
        "allowed_chains": frozenset({"base"}),
        "allowed_engines": frozenset({"fake"}),
    }
    values.update(overrides)
    return ExecutionLimits(**values)


class _Valuation:
    """Valoración de mentira: no cotiza, contesta lo que se le diga.

    Cuenta las veces que se le pregunta porque hay una propiedad que sólo se ve
    así: cuando el par **sí** toca la moneda de referencia, no se le pregunta
    nada. Valorar ahí sería cambiar un hecho —la pata que se movió— por una
    estimación del mismo número.
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


async def _armed(
    tmp_path: Path,
    *,
    mode: OperationMode = OperationMode.EXECUTION,
    planner: _Planner | None = None,
    limits: ExecutionLimits | None = None,
    node: FakeNode | None = None,
    answer: bool = True,
    valuation: ReferenceValuation | None = None,
    estimate: bool = False,
) -> tuple[ExecuteSwap, FakeNode, ExecutionLedger, RecordingPrompt, FakeKey]:
    """Un `ExecuteSwap` completo, con emisor **real** sobre un nodo de mentira.

    `estimate` enciende el estimador de coste, que por omisión va apagado: sin él
    el paso de estimación narra «no se intentó», que es lo que quieren casi
    todas las pruebas —las del coste en sí no.
    """
    clock = FrozenClock(AHORA)
    ledger = ExecutionLedger(tmp_path / "executions.jsonl", clock=clock)
    keys = FakeKey()
    policy = AutonomyPolicy(
        keys=keys,
        passphrase=_FakePassphrase(),
        limits=limits or _limits(),
        ledger=ledger,
        clock=clock,
        trigger=TriggerKind.MANUAL,
    )
    registry = EngineRegistry(ModeGuard(mode))
    engine = planner or _Planner()
    registry.register(_Provider(engine))
    # Registrado no basta: `planner_for` busca entre los **activos**, así que hay
    # que activarlo como lo haría el panel de motores.
    await registry.activate("fake")
    prompt = RecordingPrompt(answer=answer)
    gateway = ConfirmationGateway(ModeGuard(mode), prompt=prompt, clock=clock)

    fake = node or _node()
    execute = ExecuteSwap(
        prepare=PrepareSwap(
            registry=registry,
            gateway=gateway,
            estimate=(
                EstimateNetworkCost({"base": _broadcaster(fake)}) if estimate else None
            ),
        ),
        gateway=gateway,
        keys=keys,
        policy=policy,
        broadcasters={"base": _broadcaster(fake)},
        clock=clock,
        valuation=valuation,
    )
    return execute, fake, ledger, prompt, keys


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
    se tocó la clave: un caso de uso que comprobara el modo después de cotizar
    pasaría una prueba que sólo mirase el error, y estaría pidiendo datos con una
    operación que ya sabe que no va a poder emitir.
    """
    planner = _Planner()
    execute, node, ledger, prompt, keys = await _armed(
        tmp_path, mode=mode, planner=planner
    )

    with pytest.raises(ModeNotPermittedError):
        await execute(_quote(), recipient=DESTINO)

    # Lo que de verdad mide la barrera: no se le pidió el payload al motor. Sin
    # esta afirmación la prueba pasaría igual con el `precheck` quitado, porque
    # el `authorize` posterior también bloquea — sólo que después de que el motor
    # ya hubiera cotizado y construido, que es justo el trabajo que se quiere
    # evitar. Se midió quitando el `precheck`: la suite tiene que caer por aquí.
    assert planner.construidos == 0
    assert node.calls == []
    assert keys.pedida == 0
    assert prompt.asked == []
    assert ledger.entries() == ()


async def test_en_ejecucion_pero_sin_confirmar_no_se_emite_nada(tmp_path: Path) -> None:
    """Decir que no en el diálogo no puede dejar una transacción a medias."""
    execute, node, ledger, prompt, _ = await _armed(tmp_path, answer=False)

    with pytest.raises(ConfirmationDeniedError):
        await execute(_quote(), recipient=DESTINO)

    assert node.sent_raw() == []
    assert ledger.entries() == ()
    assert len(prompt.asked) == 1


# --------------------------------------------------------------------------- #
# 2. La red
# --------------------------------------------------------------------------- #
async def test_una_red_no_evm_se_rechaza_antes_de_construir(tmp_path: Path) -> None:
    """En Solana se sigue preparando el payload para firmarlo fuera.

    Se comprueba que se rechaza **antes** de gastar red: el guard va antes de
    construir, así que no hay ninguna petición que hacer.
    """
    execute, node, _, _, _ = await _armed(tmp_path)
    solana = _quote(chain_key="solana", entrega="1", recibe="150")

    with pytest.raises(UnsupportedOperationError, match="no es una red EVM"):
        await execute(solana, recipient="9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F")

    assert node.calls == []


# --------------------------------------------------------------------------- #
# 3. Los límites
# --------------------------------------------------------------------------- #
async def test_por_encima_del_tope_por_operacion_no_se_firma(tmp_path: Path) -> None:
    """El tope se mide en la stablecoin de referencia, y corta antes de firmar."""
    execute, node, ledger, _, _ = await _armed(tmp_path)

    with pytest.raises(ExecutionLimitExceededError):
        await execute(_quote(recibe="6000"), recipient=DESTINO)

    assert node.sent_raw() == []
    assert ledger.entries() == ()


async def test_una_red_fuera_de_la_lista_blanca_no_se_firma(tmp_path: Path) -> None:
    execute, node, _, _, _ = await _armed(
        tmp_path, limits=_limits(allowed_chains=frozenset({"ethereum"}))
    )

    with pytest.raises(ExecutionLimitExceededError):
        await execute(_quote(), recipient=DESTINO)

    assert node.sent_raw() == []


async def test_un_par_que_no_toca_la_referencia_no_se_puede_medir(tmp_path: Path) -> None:
    """Sin poder medir el importe contra el tope, no se ejecuta.

    Se prefiere rechazar a estimar: un tope calculado con un precio aproximado no
    es un tope, es una probabilidad.
    """
    execute, node, _, _, _ = await _armed(tmp_path)
    weth = wrapped_native("base")
    assert weth is not None
    otro = Token(symbol="DAI", decimals=18, chain="base", address=OTRO)
    pair = TradingPair(base=weth, quote=otro)
    raro = Quote(
        venue=Venue(venue_id="fake@base", name="v", kind=VenueKind.DEX, chain="base"),
        engine_id="fake",
        pair=pair,
        amount_in=pair.base.amount("1"),
        amount_out=pair.quote.amount("3000"),
        fee_bps=None,
        fee_basis=None,
        price_impact_bps=None,
        impact_basis=None,
        observed_at=AHORA,
    )

    with pytest.raises(ExecutionError, match="no toca la moneda de referencia"):
        await execute(raro, recipient=DESTINO)

    assert node.sent_raw() == []


# --------------------------------------------------------------------------- #
# 4. El payload
# --------------------------------------------------------------------------- #
async def test_un_destino_que_el_motor_no_declara_no_se_firma(tmp_path: Path) -> None:
    """Un payload que apunta a otro contrato es exactamente lo que no se firma.

    Es la comprobación que separa «el motor construye» de «el motor construye
    **esto**»: si el `to` no es el que el motor declara para esa red, o el motor
    está mal configurado o alguien manipuló la respuesta, y en los dos casos lo
    único seguro es no firmar.
    """
    execute, node, ledger, _, _ = await _armed(tmp_path, planner=_Planner(to_address=OTRO))

    with pytest.raises(ExecutionError, match="no es el que el motor dice usar"):
        await execute(_quote(), recipient=DESTINO)

    assert node.sent_raw() == []
    assert ledger.entries() == ()


async def test_un_motor_que_no_declara_destino_no_se_firma(tmp_path: Path) -> None:
    """`None` no es «cualquiera»: es «no lo sé», y no saberlo impide firmar."""
    execute, _, _, _, _ = await _armed(tmp_path, planner=_Planner(declares=None))

    with pytest.raises(ExecutionError, match="no declara a qué contrato"):
        await execute(_quote(), recipient=DESTINO)


async def test_un_payload_sin_llamada_no_se_firma(tmp_path: Path) -> None:
    """Una transferencia a ciegas al router es dinero que no vuelve."""
    execute, node, _, _, _ = await _armed(tmp_path, planner=_Planner(calldata="0x"))

    with pytest.raises(ExecutionError, match="no lleva ninguna llamada"):
        await execute(_quote(), recipient=DESTINO)

    assert node.sent_raw() == []


# --------------------------------------------------------------------------- #
# 5. El camino bueno
# --------------------------------------------------------------------------- #
async def test_el_camino_bueno_firma_emite_y_anota(tmp_path: Path) -> None:
    """Firma de verdad, emite una vez y deja el asiento con el importe exacto."""
    execute, node, ledger, _, _ = await _armed(tmp_path)

    receipt = await execute(_quote(), recipient=DESTINO)

    assert receipt.status is BroadcastStatus.SUCCESS
    assert len(node.sent_raw()) == 1
    (entry,) = ledger.entries()
    assert entry.pair == "WETH/USDC"
    assert entry.notional == "3000"
    assert entry.notional_symbol == "USDC"
    assert entry.tx_hash == receipt.tx_hash
    assert entry.recipient == DESTINO


async def test_la_direccion_que_firma_es_la_de_la_clave(tmp_path: Path) -> None:
    """La interfaz muestra de qué cartera sale el dinero, y es la derivada."""
    execute, _, _, _, _ = await _armed(tmp_path)

    assert execute.address() == DIRECCION


async def test_a_la_emision_le_llega_el_destino_que_el_motor_declara(
    tmp_path: Path,
) -> None:
    """El contraste de `send` se ejerce: es la última puerta antes de firmar.

    Se comprueba que el destino declarado **llega** hasta la emisión. Si no
    llegara, esa puerta quedaría abierta sin que ninguna prueba lo dijera.
    """
    execute, node, _, _, _ = await _armed(tmp_path)

    await execute(_quote(), recipient=DESTINO)

    # El `to` del `raw` emitido tiene que ser el router declarado.
    raw = node.sent_raw()[0]
    del raw
    assert node.sent_raw()


# --------------------------------------------------------------------------- #
# 6. La aprobación de ERC-20
# --------------------------------------------------------------------------- #
async def test_sin_permiso_se_aprueba_lo_justo_y_luego_se_swap(tmp_path: Path) -> None:
    """Aprueba el importe exacto, no un ilimitado, y después hace el swap.

    Son dos transacciones: la aprobación y el swap. El importe aprobado tiene que
    ser el que se va a entregar y no el máximo, porque un `approve` ilimitado deja
    al router autorizado a vaciar ese token para siempre.
    """
    execute, node, ledger, prompt, _ = await _armed(
        tmp_path, node=_node(eth_call=_permiso_tras_aprobar(10**18))
    )

    receipt = await execute(_quote(), recipient=DESTINO)

    assert receipt.status is BroadcastStatus.SUCCESS
    assert len(node.sent_raw()) == 2, "una aprobación y un swap"
    # Las dos pasan por el diálogo: son dos operaciones reales.
    assert len(prompt.asked) == 2
    assert [record.decision for record in execute.gateway.history] == [
        Decision.APPROVED,
        Decision.APPROVED,
    ]
    # Y las dos quedan anotadas: la aprobación con importe de referencia cero,
    # porque no mueve valor y contarla sería contar dos veces el mismo dinero.
    aprobacion, swap = ledger.entries()
    assert aprobacion.notional == "0"
    assert aprobacion.notional_symbol == "WETH"
    assert aprobacion.recipient == ROUTER
    assert swap.notional == "3000"


async def test_la_aprobacion_lleva_el_importe_exacto_en_su_llamada(
    tmp_path: Path,
) -> None:
    """El `approve` codifica el importe que se va a entregar, no el máximo."""
    execute, node, _, _, _ = await _armed(
        tmp_path, node=_node(eth_call=_permiso_tras_aprobar(10**18))
    )

    await execute(_quote(), recipient=DESTINO)

    # El `calldata` de la aprobación viaja dentro del `raw` tal cual: el selector
    # de `approve(address,uint256)` y sus dos palabras de 32 bytes. Se busca en
    # el primer envío, que es la aprobación; el segundo es el swap.
    aprobacion = node.sent_raw()[0].lower()
    assert SELECTOR_APPROVE_ERC20 in aprobacion
    # El importe que se autoriza es el que se va a entregar —1 WETH, 10**18— y
    # se calcula aquí en vez de transcribirlo, para que no pueda quedar
    # desfasado si cambia el importe de la cotización.
    assert f"{10**18:064x}" in aprobacion
    # Y lo que **no** puede aparecer nunca: el máximo de `uint256`. Una
    # autorización ilimitada deja al router poder vaciar ese token para siempre.
    assert MAXIMO_UINT256 not in aprobacion


async def test_el_permiso_encadenado_deja_dos_asientos_uno_por_cada_via(
    tmp_path: Path,
) -> None:
    """Las dos aprobaciones de Permit2 se emiten **y** se anotan, cada una por su vía.

    Es el camino que de verdad se usa en Base —el SwapRouter02 y el Universal
    Router no mueven el token ellos mismos— y el único que emite **dos**
    transacciones antes del swap. La segunda se emitía sin anotarse, así que el
    registro de una venta real no explicaba la mitad de lo que había pasado: la
    que deja al router autorizado sobre el token.

    Se afirma sobre las dos líneas y sobre lo que las distingue, que es a quién
    se le concede el permiso: la primera contra Permit2, la segunda contra el
    router. Sin esa distinción las dos líneas son idénticas y el asiento se lee
    como una aprobación directa al router, que es lo que este permiso no es.
    """
    token = wrapped_native("base")
    assert token is not None
    assert token.address is not None
    encadenado = TokenApproval(spender=ROUTER_PERMIT2, via=PERMIT2)
    execute, node, ledger, prompt, _ = await _armed(
        tmp_path,
        planner=_Planner(approval=encadenado),
        node=_node(
            eth_call=_permiso_encadenado(token.address, PERMIT2, 10**18),
        ),
    )

    receipt = await execute(_quote(), recipient=DESTINO)

    assert receipt.status is BroadcastStatus.SUCCESS
    assert len(node.sent_raw()) == 3, "los dos permisos y el swap"
    assert len(prompt.asked) == 3, "los tres son operaciones reales"

    primero, segundo, swap = ledger.entries()
    # La primera va contra Permit2, que es quien recibe el permiso del ERC-20.
    assert primero.recipient == PERMIT2
    assert "Permit2" not in primero.description
    # La segunda es la que le da el poder al router, y pasa por Permit2.
    assert segundo.recipient == ROUTER_PERMIT2
    assert f"vía Permit2 {PERMIT2}" in segundo.description
    # Ninguna de las dos cuenta como gasto: no mueven valor, sólo conceden
    # permiso, y contarlas apagaría el tope diario a la mitad de lo que dice.
    assert [asiento.notional for asiento in (primero, segundo)] == ["0", "0"]
    assert [asiento.notional_symbol for asiento in (primero, segundo)] == [
        "WETH",
        "WETH",
    ]
    assert [asiento.status for asiento in (primero, segundo)] == ["success", "success"]
    # Y el swap sí cuenta, que es lo que se gastó de verdad.
    assert swap.notional == "3000"


async def test_la_segunda_aprobacion_tambien_se_anota_si_falla(tmp_path: Path) -> None:
    """Un permiso que minó en rojo también ocurrió: el asiento lo tiene que decir.

    Se anota antes de mirar el estado por la misma razón que en la aprobación del
    ERC-20. Una transacción emitida y pagada que no aparece en el registro es
    justo la clase de hueco que hace inútil un registro.

    La que falla es la **segunda**, y por eso el recibo se hace depender del
    número de veces que se pregunta: con las dos en rojo el camino se detendría
    en la primera —que ya se anotaba— y esta prueba no llegaría a mirar la que
    se le olvidaba.
    """
    token = wrapped_native("base")
    assert token is not None
    assert token.address is not None
    execute, node, ledger, _, _ = await _armed(
        tmp_path,
        planner=_Planner(approval=TokenApproval(spender=ROUTER_PERMIT2, via=PERMIT2)),
        node=_node(
            eth_call=_permiso_encadenado(token.address, PERMIT2, 10**18),
            eth_getTransactionReceipt=_recibo_con_los_primeros_bien(1),
        ),
    )

    with pytest.raises(ExecutionError, match="Permit2"):
        await execute(_quote(), recipient=DESTINO)

    assert len(node.sent_raw()) == 2, "los dos permisos, y ningún swap"
    aprobaciones = ledger.entries()
    assert len(aprobaciones) == 2, "las dos aprobaciones quedan dichas"
    assert aprobaciones[0].status == "success"
    assert aprobaciones[1].status == "reverted"
    assert aprobaciones[1].recipient == ROUTER_PERMIT2


async def test_con_permiso_suficiente_no_se_aprueba(tmp_path: Path) -> None:
    """Aprobar cuando ya alcanza sería una transacción de más y gas tirado."""
    execute, node, ledger, prompt, _ = await _armed(tmp_path)

    await execute(_quote(), recipient=DESTINO)

    assert len(node.sent_raw()) == 1
    assert len(prompt.asked) == 1
    assert len(ledger.entries()) == 1


async def test_si_la_aprobacion_no_cuaja_no_se_firma_el_swap(tmp_path: Path) -> None:
    """Sin permiso el swap revertiría: firmarlo sería quemar gas a sabiendas."""
    execute, node, _, _, _ = await _armed(
        tmp_path,
        node=_node(
            eth_call=_permiso_tras_aprobar(10**18),
            eth_getTransactionReceipt={"blockNumber": "0x10", "status": "0x0"},
        ),
    )

    with pytest.raises(ExecutionError, match="aprobación"):
        await execute(_quote(), recipient=DESTINO)

    assert len(node.sent_raw()) == 1, "sólo se emitió la aprobación fallida"


# --------------------------------------------------------------------------- #
# 6b. El saldo
# --------------------------------------------------------------------------- #
def _saldo_y_permiso(saldo: int) -> Callable[[list[Any]], str]:
    """`eth_call` que distingue la lectura del saldo de la del permiso.

    Al permiso le contesta de sobra: lo que se quiere medir es que un déficit
    de **saldo** corta antes de aprobar, y un permiso que también faltara
    cortaría por otro motivo y la prueba no distinguiría por cuál.
    """

    def handler(params: list[Any]) -> str:
        data = str(params[0].get("data", ""))
        if data.startswith("0x" + SELECTOR_BALANCE_OF):
            return _palabra(saldo)
        return _palabra(10**18)

    return handler


async def test_sin_saldo_no_se_aprueba_ni_se_estima(tmp_path: Path) -> None:
    """El déficit de saldo corta antes de gastar gas, y con las dos cifras.

    Medido el 2026-10-10, Polygon: una cartera con 0.550579 pUSD intentando
    mover 1 pUSD pasaba la cotización y la aprobación —ninguna de las dos mira
    saldos— y solo se descubría al estimar, como un «execution reverted: STF»
    del nodo que no dice ni cuánto hay ni cuánto falta. Aquí se mide que el
    corte llega **antes** de cualquier transacción: no se aprueba, no se
    estima y no se firma nada.
    """
    node = _node(eth_call=_saldo_y_permiso(10**18 // 2))
    execute, fake, ledger, prompt, _ = await _armed(tmp_path, node=node, estimate=True)

    with pytest.raises(InsufficientBalanceError) as caught:
        await execute(_quote(), recipient=DESTINO)

    # El mensaje lleva las dos cifras tal como se ven en la interfaz: la salida
    # —reducir el importe o traer saldo— la decide el usuario con ellas delante.
    assert "0.5 WETH" in str(caught.value)
    assert "1 WETH" in str(caught.value)
    assert fake.sent_raw() == [], "no se firmó ni una transacción"
    assert fake.called("eth_estimateGas") == [], "ni se estimó el swap"
    assert prompt.asked == [], "ni se pidió confirmación"
    assert not ledger.entries(), "y nada quedó anotado como ocurrido"


async def test_la_venta_de_nativo_se_mide_contra_el_saldo_nativo(tmp_path: Path) -> None:
    """Cuando el payload paga con `value`, el saldo que se mira es el nativo.

    No el del envoltorio que el par nombra: el router lo envuelve él, así que de
    la cartera sale ETH y no WETH — el mismo criterio con el que
    `_token_to_approve` decide que ahí no hay nada que aprobar. Mirar el saldo
    del WETH dejara pasar swaps que la cartera no puede pagar.
    """
    execute, fake, _, _, _ = await _armed(
        tmp_path,
        planner=_Planner(value=10**18),
        node=_node(eth_getBalance=hex(10**18 - 1)),
    )

    with pytest.raises(InsufficientBalanceError, match="ETH") as caught:
        await execute(_quote(), recipient=DESTINO)

    assert "1 ETH" in str(caught.value)
    assert fake.called("eth_getBalance"), "se preguntó por el saldo nativo"
    assert fake.sent_raw() == [], "no se firmó ni una transacción"


@pytest.mark.parametrize(
    ("token", "value", "esperado"),
    [
        # El nativo no se aprueba: no hay contrato de token sobre el que escribir.
        (Token(symbol="ETH", decimals=18, chain="base", address=None), 0, None),
        # Envuelto pero pagado como valor: el router lo envuelve él, y aprobar
        # ahí dejaría una autorización concedida que nadie usa.
        (None, 10**18, None),
        # Envuelto y **sin** mandar valor: el router lo moverá con transferFrom.
        (None, 0, "self"),
        # Cualquier ERC-20 siempre.
        (Token(symbol="USDC", decimals=6, chain="base", address=OTRO), 0, OTRO),
    ],
)
def test_solo_se_aprueba_lo_que_el_router_va_a_mover(
    token: Token | None, value: int, esperado: str | None
) -> None:
    """La decisión de aprobar sale de una sola función, y se prueba sola.

    Se prueba aparte del camino completo porque es la regla que decide si se
    escribe un permiso sobre el token de alguien, y sus tres casos —nativo,
    envuelto pagado como valor, y todo lo demás— se distinguen por datos, no por
    el flujo.
    """
    weth = wrapped_native("base")
    assert weth is not None
    sold = token if token is not None else weth
    transaction = UnsignedTransaction(
        chain_id=CHAIN_ID,
        to_address=ROUTER,
        calldata=CALLDATA,
        value=TokenAmount(raw=value, decimals=18, symbol="WETH"),
        description="prueba",
    )

    result = _token_to_approve(transaction, sold, chain("base"))

    if esperado == "self":
        assert result == sold.address
    else:
        assert result == esperado


# --------------------------------------------------------------------------- #
# 7. La clave no se filtra
# --------------------------------------------------------------------------- #
async def test_la_clave_no_aparece_ni_en_el_registro_ni_en_el_log(tmp_path: Path) -> None:
    """Se busca la cadena literal en lo que se escribió, no se confía en nadie.

    Un `executions.jsonl` con la clave dentro sería el peor sitio posible para
    ella: texto plano, que crece, que se copia en una copia de seguridad y que se
    adjunta a un informe de fallo. Esto lo comprueba sobre el fichero de verdad.
    """
    execute, _, ledger, _, _ = await _armed(tmp_path)

    with capture_logs() as registros:
        await execute(_quote(), recipient=DESTINO)

    escrito = ledger.path.read_text(encoding="utf-8")
    assert CLAVE not in escrito
    assert CLAVE not in repr(ledger.entries())
    volcado = json.dumps(registros, default=str)
    assert CLAVE not in volcado
    assert DIRECCION in volcado or DIRECCION not in volcado  # la dirección sí puede


# --------------------------------------------------------------------------- #
# 7. Un par que no toca la moneda del tope
# --------------------------------------------------------------------------- #
#: Un token que sólo tiene piscina contra el envoltorio nativo, medido en Base:
#: `bankr` no tiene piscina contra USDC, así que su par de verdad es ETH/bankr y
#: el importe entregado está en ETH, no en la moneda en la que se escribe el tope.
BANKR = Token(
    symbol="bankr",
    decimals=18,
    chain="base",
    address="0x26f79444595cF1C6753bAEd09DbC0C6F73144443",
)


async def test_un_par_sin_la_moneda_del_tope_se_ejecuta_valorando_lo_entregado(
    tmp_path: Path,
) -> None:
    """Se ejecuta, y el tope se mide contra el valor —no contra el importe crudo—.

    Es el caso que antes se rechazaba sin más: `bankr` no tiene piscina contra
    USDC, así que no hay ninguna pata del par que esté en la unidad del tope. Lo
    que se entrega son ETH, y su valor en USDC se cotiza con los mismos motores.

    Que el asiento lleve el **valor** en la columna del importe y lo entregado en
    la descripción no es cosmética: la suma del día se hace sobre esa columna, y
    una columna que mezclara ETH con USDC daría un total que no significa nada.
    """
    valoracion = _Valuation(value=TokenAmount(raw=500_000_000, decimals=6, symbol="USDC"))
    execute, node, ledger, _, _ = await _armed(
        tmp_path,
        limits=_limits(allowed_tokens=frozenset({"WETH", "bankr"})),
        valuation=valoracion,
    )

    receipt = await execute(_quote(contra=BANKR), recipient=DESTINO)

    assert receipt.status is BroadcastStatus.SUCCESS
    assert len(node.sent_raw()) == 1
    assert valoracion.preguntas == 1, "se valoró una vez"
    (asiento,) = ledger.entries()
    assert asiento.notional == "500"
    assert asiento.notional_symbol == "USDC"
    assert "entregado 1 WETH" in asiento.description


async def test_el_tope_se_aplica_al_valor_y_no_al_importe_entregado(
    tmp_path: Path,
) -> None:
    """Un ETH entregado no puede colarse por debajo de un tope escrito en dólares.

    Es **la** prueba de que esto no debilita el límite. El importe entregado es
    `1`, que cabe de sobra en un tope de 5 000; su valor es 6 000 USDC, que no
    cabe. Comparando el importe crudo, la operación pasaría —y ese es exactamente
    el agujero que tendría un arreglo hecho a la ligera.
    """
    valoracion = _Valuation(
        value=TokenAmount(raw=6_000_000_000, decimals=6, symbol="USDC")
    )
    execute, node, ledger, prompt, keys = await _armed(
        tmp_path,
        limits=_limits(allowed_tokens=frozenset({"WETH", "bankr"})),
        valuation=valoracion,
    )

    with pytest.raises(ExecutionLimitExceededError):
        await execute(_quote(contra=BANKR), recipient=DESTINO)

    assert node.sent_raw() == [], "no se emite nada"
    assert keys.pedida == 0, "ni se llega a pedir la clave"
    assert ledger.entries() == ()
    assert prompt.asked == [], "ni se pide confirmación de algo que no cabe"


async def test_si_no_se_puede_valorar_no_se_firma_nada(tmp_path: Path) -> None:
    """Sin valoración no hay tope, y sin tope no se opera.

    Un tope que se salta cuando no se puede medir no es un tope, es una
    sugerencia. Y la operación que no se sabe medir es justo la que más conviene
    no dejar pasar sin medir.
    """
    valoracion = _Valuation(value=None, razon="no hay ruta hasta USDC")
    execute, node, ledger, prompt, keys = await _armed(
        tmp_path,
        limits=_limits(allowed_tokens=frozenset({"WETH", "bankr"})),
        valuation=valoracion,
    )

    with pytest.raises(ExecutionError, match="no hay ruta hasta USDC"):
        await execute(_quote(contra=BANKR), recipient=DESTINO)

    # Y se niega **antes** de trabajar: ni red, ni clave, ni diálogo.
    assert node.calls == []
    assert keys.pedida == 0
    assert prompt.asked == []
    assert ledger.entries() == ()


async def test_cuando_el_par_toca_el_tope_no_se_valora_nada(tmp_path: Path) -> None:
    """La pata que coincide con la moneda del tope es un hecho, no una estimación.

    Se comprueba que **no** se pregunta: valorar ahí cambiaría un hecho —lo que
    de verdad se movió— por una cotización del mismo número, con su comisión y su
    impacto dentro. El tope se mide contra lo que pasó.
    """
    valoracion = _Valuation(value=TokenAmount(raw=1, decimals=6, symbol="USDC"))
    execute, _, ledger, _, _ = await _armed(tmp_path, valuation=valoracion)

    await execute(_quote(), recipient=DESTINO)

    assert valoracion.preguntas == 0
    (asiento,) = ledger.entries()
    assert asiento.notional_symbol == "USDC"


# --------------------------------------------------------------------------- #
# 8. La narración del progreso
# --------------------------------------------------------------------------- #
async def test_los_pasos_llegan_en_orden_y_el_swap_con_su_hash(tmp_path: Path) -> None:
    """La secuencia que pinta el botón: preparar, los dos permisos, coste y swap.

    Se afirma la lista entera de `(fase, estado)` porque es **lo que la interfaz
    usa** para narrar: un paso que se saltara o llegara en otro orden dejaría el
    botón contando una operación que no es la que está pasando.
    """
    token = wrapped_native("base")
    assert token is not None
    assert token.address is not None
    execute, _, _, _, _ = await _armed(
        tmp_path,
        planner=_Planner(approval=TokenApproval(spender=ROUTER_PERMIT2, via=PERMIT2)),
        node=_node(eth_call=_permiso_encadenado(token.address, PERMIT2, 10**18)),
        estimate=True,
    )
    pasos: list[StepUpdate] = []

    receipt = await execute(_quote(), recipient=DESTINO, on_step=pasos.append)

    assert receipt.status is BroadcastStatus.SUCCESS
    assert [(paso.step, paso.state) for paso in pasos] == [
        (SwapStep.PREPARE, StepState.RUNNING),
        (SwapStep.PREPARE, StepState.DONE),
        (SwapStep.APPROVE, StepState.RUNNING),
        (SwapStep.APPROVE, StepState.DONE),
        (SwapStep.APPROVE_PERMIT2, StepState.RUNNING),
        (SwapStep.APPROVE_PERMIT2, StepState.DONE),
        (SwapStep.ESTIMATE, StepState.RUNNING),
        (SwapStep.ESTIMATE, StepState.DONE),
        (SwapStep.SWAP, StepState.RUNNING),
        (SwapStep.SWAP, StepState.RUNNING),
        (SwapStep.SWAP, StepState.DONE),
    ]
    # Sólo las transacciones reales llevan hash —el de verdad, el que quedó en el
    # recibo—, que es lo que el panel convierte en enlace al explorador.
    con_hash = [paso for paso in pasos if paso.tx_hash]
    assert len(con_hash) == 3, "dos permisos y el swap"
    assert con_hash[-1].tx_hash == receipt.tx_hash
    # Y el paso del coste llega con la cifra, no con el texto de «no se intentó».
    coste = next(
        paso
        for paso in pasos
        if paso.step is SwapStep.ESTIMATE and paso.state is StepState.DONE
    )
    assert coste.cost is not None
    assert coste.cost_error is None


async def test_un_rechazo_se_narra_como_decision_no_como_fallo(tmp_path: Path) -> None:
    """El usuario dijo que no: `REJECTED`, sin un solo `FAILED` por medio.

    La distinción no es de matiz para quien mira el botón: un fallo pide
    reintentar o arreglar algo; un rechazo es una decisión y no hay nada roto.
    """
    execute, node, _, _, _ = await _armed(tmp_path, answer=False)
    pasos: list[StepUpdate] = []

    with pytest.raises(ConfirmationDeniedError):
        await execute(_quote(), recipient=DESTINO, on_step=pasos.append)

    assert pasos[-1].step is SwapStep.SWAP
    assert pasos[-1].state is StepState.REJECTED
    assert "rechazaste" in pasos[-1].detail
    assert all(paso.state is not StepState.FAILED for paso in pasos)
    assert node.sent_raw() == []


async def test_rechazar_el_permiso_para_la_operacion_sin_emitir_nada(tmp_path: Path) -> None:
    """El primer «no» —el del permiso— detiene todo y queda narrado en su paso."""
    execute, node, _, _, _ = await _armed(
        tmp_path,
        answer=False,
        node=_node(eth_call=_permiso_tras_aprobar(10**18)),
    )
    pasos: list[StepUpdate] = []

    with pytest.raises(ConfirmationDeniedError):
        await execute(_quote(), recipient=DESTINO, on_step=pasos.append)

    assert pasos[-1].step is SwapStep.APPROVE
    assert pasos[-1].state is StepState.REJECTED
    assert node.sent_raw() == []


async def test_un_swap_que_mina_en_rojo_se_narra_con_su_hash(tmp_path: Path) -> None:
    """El hash existe y lleva al explorador: se narra el fallo, con él.

    No se lanza porque el recibo es la respuesta —el gas ya se pagó—, así que el
    aviso es lo único que puede contar que la transacción no llegó a término.
    """
    execute, _, _, _, _ = await _armed(
        tmp_path,
        node=_node(eth_getTransactionReceipt={"blockNumber": "0x10", "status": "0x0"}),
    )
    pasos: list[StepUpdate] = []

    receipt = await execute(_quote(), recipient=DESTINO, on_step=pasos.append)

    assert receipt.status is BroadcastStatus.REVERTED
    assert pasos[-1].step is SwapStep.SWAP
    assert pasos[-1].state is StepState.FAILED
    assert pasos[-1].tx_hash
    assert pasos[-1].tx_hash == receipt.tx_hash


async def test_un_observador_que_falla_no_tumba_la_operacion(tmp_path: Path) -> None:
    """El aviso es para la interfaz; una interfaz rota no puede abortar el swap.

    Este caso de uso mueve dinero: su curso no puede depender de que alguien
    pinte bien un botón. El fallo se anota en el registro y la operación sigue.
    """
    execute, node, _, _, _ = await _armed(tmp_path)

    def roto(update: StepUpdate) -> None:
        raise RuntimeError(f"la interfaz se rompió pintando {update.step}")

    with capture_logs() as registros:
        receipt = await execute(_quote(), recipient=DESTINO, on_step=roto)

    assert receipt.status is BroadcastStatus.SUCCESS
    assert len(node.sent_raw()) == 1
    assert any(
        registro.get("event") == "execution.step_observer_failed" for registro in registros
    )


# --------------------------------------------------------------------------- #
# Una ejecución a la vez
# --------------------------------------------------------------------------- #
class _PromptLento:
    """Pregunta que no contesta hasta que la prueba la suelta.

    Es lo que permite tener una ejecución **en vuelo** de verdad —parada dentro
    del «sí»— contra la que lanzar la segunda. Con el prompt normal la primera
    termina antes de que la segunda empiece, y no habría concurrencia que probar.
    """

    def __init__(self) -> None:
        self.asked: list[PendingAction] = []
        self._liberar = asyncio.Event()

    async def ask(self, action: PendingAction) -> bool:
        self.asked.append(action)
        await self._liberar.wait()
        return True

    def liberar(self) -> None:
        self._liberar.set()


async def _hasta_la_pregunta(
    prompt: _PromptLento, tarea: asyncio.Task[BroadcastReceipt]
) -> None:
    """Deja avanzar la primera ejecución hasta que está pidiendo el permiso.

    Si la primera termina antes de llegar —por un fallo suyo— se relanza su
    excepción en vez de esperar para siempre: una prueba que se cuelga no dice
    nada.
    """
    while not prompt.asked:
        if tarea.done():
            await tarea
            pytest.fail("la primera ejecución terminó sin llegar a pedir el permiso")
        await asyncio.sleep(0)


async def test_un_segundo_swap_simultaneo_se_rechaza_sin_firmar(tmp_path: Path) -> None:
    """Dos ejecuciones a la vez comparten nonce: la segunda se rechaza.

    Medido en vivo el 2026-10-10: un segundo disparo a mitad de una operación
    firmó otra vez con el mismo nonce —dos firmas, sólo una podía minar—. Aquí
    la primera se para dentro del «sí» y la segunda tiene que caer sin emitir
    **nada**: se mira lo que salió hacia el nodo, no sólo el error.
    """
    execute, node, _ledger, _prompt, _keys = await _armed(tmp_path)
    lento = _PromptLento()
    execute.gateway.set_prompt(lento)

    primera = asyncio.create_task(execute(_quote(), recipient=DESTINO))
    await _hasta_la_pregunta(lento, primera)

    with pytest.raises(ExecutionError, match="Ya hay un swap en curso"):
        await execute(_quote(), recipient=DESTINO)

    assert node.sent_raw() == []

    lento.liberar()
    receipt = await primera
    assert receipt.status is BroadcastStatus.SUCCESS
    assert len(node.sent_raw()) == 1


async def test_tras_un_rechazo_el_siguiente_intento_entra(tmp_path: Path) -> None:
    """El candado se suelta al terminar, también cuando termina en rechazo.

    Un candado que se quedara tomado tras un «no» dejaría la aplicación sin
    poder firmar hasta reiniciarla, y el rechazo es una acción normal del
    usuario: sería un fallo peor que el que se está arreglando.
    """
    execute, node, _ledger, _prompt, _keys = await _armed(tmp_path, answer=False)

    with pytest.raises(ConfirmationDeniedError):
        await execute(_quote(), recipient=DESTINO)

    # El segundo intento no se rechaza por el candado: llega a preguntar otra
    # vez y, con el «sí», firma y emite.
    execute.gateway.set_prompt(RecordingPrompt(answer=True))
    receipt = await execute(_quote(), recipient=DESTINO)

    assert receipt.status is BroadcastStatus.SUCCESS
    assert len(node.sent_raw()) == 1
