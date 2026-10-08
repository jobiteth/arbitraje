"""Cobrar posiciones ya resueltas: el camino por el que el dinero **entra**.

Es el tercer camino que emite una transacción real, y el único de los tres que no
saca dinero: entrega unas participaciones que ya se pagaron y recibe el colateral
que valen. De ahí salen las dos cosas que aquí se fijan, y las dos son
consecuencias del mismo hecho:

1. **Que el cobro no consuma el tope de gasto.** Los topes acotan lo que sale;
   medir contra ellos un cobro dejaría el dinero encerrado en el contrato justo
   cuando por fin se puede sacar, y gastaría presupuesto del día por recuperar lo
   propio. La prueba cobra **más** que los dos topes juntos y afirma que pasó y
   que el gasto del día sigue en cero. Quitar el cortocircuito del intento la
   rompe, que es lo que se busca.
2. **Que no pida ningún permiso previo.** El contrato condicional quema las
   participaciones de quien llama; no hay a quién autorizar nada. Se mide en
   peticiones: **ninguna** `eth_call` —no se lee ni un permiso— y una sola
   transacción emitida. Un `approve` «por si acaso» aquí sería conceder un permiso
   que nadie necesita.

Y las barreras de siempre, que en este camino tienen una forma propia: el cobro
va **contra la cartera que firma**, así que leer las posiciones de una y firmar
con otra es el fallo que no se ve en la transacción y sí se ve en el error.

Se usa un `EvmBroadcaster` **real** sobre un nodo de mentira y una clave de
desarrollo publicada —la cuenta 0 de Hardhat— para que la firma sea de verdad, y
el motor falso construye el `calldata` con el **código de producción**: lo que se
dobla es el recinto, no la aritmética que mueve el dinero.
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
from amigocompora.app.usecases.redeem_prediction import RedeemPrediction
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
    PredictionMarket,
    PredictionPosition,
    Token,
    UnsignedTransaction,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.polymarket.engine import PolymarketEngine
from amigocompora.engines.polymarket.orders import (
    CONDITIONAL_TOKENS,
    SELECTOR_REDEEM,
)
from amigocompora.infra.evm.broadcast import EvmBroadcaster
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool

AHORA = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
POLYGON_ID = 137

#: Cuenta 0 de Hardhat: publicada, sin fondos en ninguna red real. La dirección
#: está derivada de la clave, no recordada.
CLAVE = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
#: Otra cartera cualquiera: la que tiene las posiciones cuando la clave no es suya.
OTRA = "0x2c887da24C6E6c939B0Fe3adaE50814b938B44f9"

#: Direcciones de contrato en minúsculas y escritas enteras a mano: son contra lo
#: que se firma, y sacarlas de una constante del producto dejaría la prueba
#: comprobándose a sí misma.
COLATERAL = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
#: El selector de `approve(address,uint256)`, que es lo que **no** puede aparecer
#: en el `calldata` de un cobro.
SELECTOR_APPROVE = "095ea7b3"
#: El máximo de `uint256`, que tampoco.
MAXIMO_UINT256 = "f" * 64

#: Tres mercados distintos, cada uno de 32 bytes.
MERCADO_A = "0x" + "ab" * 32
MERCADO_B = "0x" + "cd" * 32
MERCADO_C = "0x" + "ef" * 32

#: Identificadores de resultado: números públicos del contrato condicional. El
#: `noqa` silencia un falso positivo —la regla lee «TOKEN» como una credencial—.
TOKEN_SI = "71321045679252212594626385532706912750332728571942532289631379312455583992563"  # noqa: S105
TOKEN_NO = "52114319501245915516055105976744304775366685471852918604325386120244825471694"  # noqa: S105

PARTICIPACIONES = Decimal("12.5")

MANIFEST = EngineManifest(
    engine_id="polymarket",
    name="Polymarket",
    version="1.0.0",
    kind=EngineKind.PREDICTION_MARKETS,
    summary="Doble del recinto, con la aritmética de producción.",
    capabilities=frozenset({Capability.PREPARE_TX, Capability.BROADCAST_TX}),
)


def _posicion(
    *,
    condition_id: str = MERCADO_A,
    shares: Decimal = PARTICIPACIONES,
    outcome_label: str = "Sí",
    token_id: str = TOKEN_SI,
    redeemable: bool = True,
    neg_risk: bool | None = False,
    venue: Venue | None = None,
) -> PredictionPosition:
    """Una posición resuelta de Polygon, con lo que se le diga cambiado."""
    return PredictionPosition(
        venue=venue or _recinto(),
        condition_id=condition_id,
        question="¿Ocurrirá lo medido?",
        outcome_label=outcome_label,
        token_id=token_id,
        shares=shares,
        observed_at=AHORA,
        redeemable=redeemable,
        neg_risk=neg_risk,
    )


def _recinto(*, chain: str = "polygon", venue_id: str = "polymarket") -> Venue:
    return Venue(
        venue_id=venue_id,
        name="Polymarket" if venue_id == "polymarket" else "Otro",
        kind=VenueKind.PREDICTION_MARKET,
        chain=chain,
    )


# --------------------------------------------------------------------------- #
# Dobles
# --------------------------------------------------------------------------- #
class FakeKey(PrivateKeySource):
    """Clave que se puede hacer desaparecer, y que **cuenta** cuántas veces se pide.

    El contador es lo que permite afirmar que en los modos que no ejecutan no se
    llegó a tocar la clave. Se cuenta y no se supone: el orden de las llamadas no
    se ve en el resultado.
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


class MotorQueCobra:
    """Un motor de predicción que sabe cobrar, y que **cuenta** lo que se le pide.

    Construye el `calldata` con el código de producción —delegando en un
    `PolymarketEngine` de verdad— porque el `calldata` es justo la parte que mueve
    el dinero: doblarlo dejaría sin probar lo único que no se puede equivocar.

    `construidos` es la única medida de «se gastó trabajo en una operación que el
    modo no permite», y por eso la barrera se comprueba contra él y no sólo contra
    el error.
    """

    def __init__(self) -> None:
        self.construidos = 0
        self._real = PolymarketEngine(clock=FrozenClock(AHORA))

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
        return ()

    async def market(self, market_id: str) -> PredictionMarket:
        raise AssertionError(f"cobrar no pide un mercado suelto: {market_id}")

    async def book(self, token_id: str) -> MarketDepth:
        raise AssertionError(f"cobrar no lee el libro: {token_id}")

    async def positions(
        self, *, wallet: str, redeemable_only: bool = False
    ) -> tuple[PredictionPosition, ...]:
        # El caso de uso recibe las posiciones ya leídas: quien las lee es la
        # pantalla, que necesita el dato para decidir si ofrece el botón. Que este
        # método no se llame aquí es parte de lo que se afirma, así que se dice.
        raise AssertionError(f"el cobro no relee las posiciones de {wallet} ({redeemable_only})")

    def collateral_on(self, chain_key: str) -> Token:
        if chain_key != "polygon":
            raise ExecutionError(f"no hay colateral declarado en «{chain_key}»")
        return Token(symbol="USDC", decimals=6, chain="polygon", address=COLATERAL)

    def build_redeem(
        self, positions: Sequence[PredictionPosition], *, wallet: str
    ) -> UnsignedTransaction:
        self.construidos += 1
        return self._real.build_redeem(positions, wallet=wallet)


class MotorSoloLectura:
    """Un motor de predicción que sólo mira: no sabe cobrar."""

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
    cobra y otro que sólo lee—, y lo que se mide es lo que el **registro** hace
    con ellos.
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
    significa que se emitió algo que no es lo que se cree.
    """
    return "0x" + keccak(bytes.fromhex(str(params[0])[2:])).hex()


def _recibos(*estados: str) -> Callable[[list[Any]], dict[str, str]]:
    """Recibos en orden, repitiendo el último si se acaban.

    Hace falta que sean varios porque el fallo a mitad de una tanda de mercados es
    justo lo que se quiere medir: el primero tiene que salir bien y el segundo no,
    y un nodo que contestara siempre lo mismo no podría distinguirlo.
    """
    pendientes = list(estados)

    def handler(params: list[Any]) -> dict[str, str]:
        del params
        estado = pendientes.pop(0) if len(pendientes) > 1 else pendientes[0]
        return {"blockNumber": "0x10", "status": estado}

    return handler


def _node(**overrides: Any) -> FakeNode:
    handlers: dict[str, Any] = {
        "eth_chainId": hex(POLYGON_ID),
        "eth_getTransactionCount": "0x5",
        "eth_estimateGas": "0x30d40",
        "eth_getBlockByNumber": {"baseFeePerGas": "0x3b9aca00"},
        "eth_maxPriorityFeePerGas": "0x3b9aca",
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
def _limites(**overrides: Any) -> ExecutionLimits:
    """Límites que describen una ejecución **permitida**.

    El interruptor maestro se prueba aparte y en su propia prueba: apagarlo aquí
    haría que todas las de este fichero fallaran por el mismo motivo —y ninguna
    por el suyo—.
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
) -> tuple[RedeemPrediction, FakeNode, ExecutionLedger, RecordingPrompt, FakeKey, Any]:
    """Un `RedeemPrediction` completo, con emisor **real** sobre un nodo falso."""
    clock = FrozenClock(AHORA)
    ledger = ExecutionLedger(tmp_path / "executions.jsonl", clock=clock)
    keys = FakeKey(available=hay_clave)
    policy = AutonomyPolicy(
        keys=keys,
        passphrase=_FakePassphrase(),
        limits=limits or _limites(),
        ledger=ledger,
        clock=clock,
        trigger=TriggerKind.MANUAL,
    )
    registry = EngineRegistry(ModeGuard(mode))
    engine = motor if motor is not None else MotorQueCobra()
    registry.register(_Provider(engine))
    # Registrado no basta: el accesor busca entre los **activos**, así que hay que
    # activarlo como lo haría el panel de motores.
    await registry.activate(engine_id)
    prompt = RecordingPrompt(answer=answer)
    gateway = ConfirmationGateway(ModeGuard(mode), prompt=prompt, clock=clock)

    fake = node or _node()
    use_case = RedeemPrediction(
        registry=registry,
        gateway=gateway,
        keys=keys,
        policy=policy,
        broadcasters=({"polygon": _broadcaster(fake)} if broadcasters is None else broadcasters),
        clock=clock,
    )
    return use_case, fake, ledger, prompt, keys, engine


async def _cobrar(
    use_case: RedeemPrediction,
    *positions: PredictionPosition,
    wallet: str = DIRECCION,
    recipient: str = DIRECCION,
) -> tuple[Any, ...]:
    return await use_case(list(positions) or [_posicion()], wallet=wallet, recipient=recipient)


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
    bloquea — sólo que después de que el motor ya hubiera construido el cobro, que
    es justo el trabajo que se quiere evitar.
    """
    motor = MotorQueCobra()
    use_case, node, ledger, prompt, keys, _ = await _armed(tmp_path, mode=mode, motor=motor)

    with pytest.raises(ModeNotPermittedError):
        await _cobrar(use_case)

    assert motor.construidos == 0
    assert node.calls == []
    assert keys.pedida == 0
    assert prompt.asked == []
    assert ledger.entries() == ()


async def test_en_ejecucion_pero_sin_confirmar_no_se_emite_nada(tmp_path: Path) -> None:
    """Decir que no en el diálogo no puede dejar una transacción emitida.

    Un cobro emitido no se puede deshacer: lo que no puede pasar es que la
    negativa llegue después de la firma.
    """
    use_case, node, ledger, prompt, _, _ = await _armed(tmp_path, answer=False)

    with pytest.raises(ConfirmationDeniedError):
        await _cobrar(use_case)

    assert node.sent_raw() == []
    assert ledger.entries() == ()
    assert len(prompt.asked) == 1


# --------------------------------------------------------------------------- #
# 2. Lo que no es un cobro
# --------------------------------------------------------------------------- #
async def test_una_lista_vacia_no_se_firma(tmp_path: Path) -> None:
    """Cobrar nada no es una operación, y firmarla costaría gas por nada."""
    use_case, node, ledger, _, keys, _ = await _armed(tmp_path)

    with pytest.raises(ExecutionError, match="ninguna posición que cobrar"):
        await use_case([], wallet=DIRECCION, recipient=DIRECCION)

    assert node.calls == []
    assert keys.pedida == 0
    assert ledger.entries() == ()


async def test_una_red_que_no_es_evm_se_rechaza_antes_de_trabajar(tmp_path: Path) -> None:
    """El cobro es una transacción: sin firmar en esa cadena no hay nada que hacer."""
    motor = MotorQueCobra()
    use_case, node, _, _, _, _ = await _armed(tmp_path, motor=motor)
    otra = _posicion(venue=_recinto(chain="solana"))

    with pytest.raises(UnsupportedOperationError, match="no es una red EVM"):
        await _cobrar(use_case, otra)

    assert motor.construidos == 0
    assert node.calls == []


async def test_un_motor_que_solo_lee_no_cobra(tmp_path: Path) -> None:
    """Leer mercados y cobrar son dos superficies distintas, y se distinguen.

    El accesor tiene que decirlo con un error propio en vez de reventar con un
    `AttributeError` sobre un método que el motor nunca tuvo.
    """
    use_case, node, _, _, _, _ = await _armed(
        tmp_path, motor=MotorSoloLectura(), engine_id="solo-lectura"
    )

    with pytest.raises(EngineError, match="no sabe cobrar"):
        await _cobrar(use_case)

    assert node.calls == []


async def test_sin_nodo_para_la_red_no_se_firma_nada(tmp_path: Path) -> None:
    """Sin nodo no hay nonce, ni gas, ni comisión: nada que firmar."""
    use_case, node, ledger, _, _, _ = await _armed(tmp_path, broadcasters={})

    with pytest.raises(ExecutionError, match="no hay ningún nodo configurado"):
        await _cobrar(use_case)

    assert node.calls == []
    assert ledger.entries() == ()


async def test_sin_clave_no_hay_cartera_que_cobre(tmp_path: Path) -> None:
    """El cobro va contra quien firma: sin clave no hay a quién cobrarle."""
    use_case, node, ledger, _, keys, motor = await _armed(tmp_path, hay_clave=False)

    with pytest.raises(ExecutionError, match="no hay ninguna clave configurada"):
        await _cobrar(use_case)

    assert keys.pedida == 0, "ni se pide: no hay ninguna"
    assert motor.construidos == 1, "el cobro se construye antes: es puro"
    assert node.calls == []
    assert ledger.entries() == ()


async def test_posiciones_de_otra_cartera_no_se_cobran_con_esta_clave(
    tmp_path: Path,
) -> None:
    """`redeemPositions` quema las participaciones de **quien llama**.

    Leer las posiciones de una cartera y firmar con otra es el fallo que no se ve
    mirando la transacción: se emite, revierte, y el gas se pagó. Se comprueba
    antes de gastar una sola petición y se dice de qué cartera a qué cartera.
    """
    use_case, node, ledger, prompt, _, _ = await _armed(tmp_path)

    with pytest.raises(ExecutionError, match="la clave configurada") as fallo:
        await _cobrar(use_case, wallet=OTRA)

    assert "las posiciones son de" in str(fallo.value)
    assert node.calls == [], "no se gasta ni una petición"
    assert prompt.asked == []
    assert ledger.entries() == ()


async def test_una_posicion_sin_resolver_no_se_cobra(tmp_path: Path) -> None:
    """El motor lo rechaza, y el caso de uso no lo rescata.

    Está aquí además de en la prueba del motor porque lo que se fija es la
    **ausencia** de un camino alternativo: no hay ninguna rama del caso de uso que
    construya el cobro por su cuenta saltándose la comprobación del motor.
    """
    use_case, node, ledger, _, _, _ = await _armed(tmp_path)

    with pytest.raises(ExecutionError, match="todavía no ha resuelto"):
        await _cobrar(use_case, _posicion(redeemable=False))

    assert node.calls == []
    assert ledger.entries() == ()


# --------------------------------------------------------------------------- #
# 3. El cobro
# --------------------------------------------------------------------------- #
async def test_el_cobro_va_al_contrato_condicional_sin_pedir_permisos(
    tmp_path: Path,
) -> None:
    """Una transacción, y **ninguna** lectura de permisos.

    El contrato condicional quema las participaciones de quien firma, así que no
    hay ningún token ajeno que autorizar. Se mide en peticiones porque es la única
    forma de verlo: un `approve` «por si acaso» se vería en el `calldata`, y una
    lectura de permiso en un `eth_call` que aquí no tiene por qué existir.
    """
    use_case, node, ledger, prompt, _, _ = await _armed(tmp_path)

    (recibo,) = await _cobrar(use_case)

    assert len(node.sent_raw()) == 1, "una transacción, ni una más"
    assert node.called("eth_call") == [], "no se lee ningún permiso: no hace falta"
    emitido = node.sent_raw()[0].lower()
    # El destino —el contrato condicional— y la función que se llama, sobre lo que
    # de verdad viajó al nodo, no sobre lo que el código dice que construyó.
    assert CONDITIONAL_TOKENS[2:].lower() in emitido
    assert SELECTOR_REDEEM[2:] in emitido
    # Y lo que no puede aparecer por ningún lado.
    assert SELECTOR_APPROVE not in emitido
    assert MAXIMO_UINT256 not in emitido
    assert recibo.status.value == "success"
    assert len(prompt.asked) == 1
    assert [entrada.decision for entrada in use_case.gateway.history] == [Decision.APPROVED]
    assert len(ledger.entries()) == 1


async def test_lo_que_se_confirma_es_lo_que_se_firma(tmp_path: Path) -> None:
    """El diálogo muestra la transacción, y es la misma que se emite.

    Si se reconstruyera entre el «sí» y la firma, podrían ser dos cosas distintas y
    el usuario habría aprobado una que no se emitió. Se comprueba sobre la
    transacción que el diálogo recibió, palabra por palabra.
    """
    use_case, node, _, prompt, _, _ = await _armed(tmp_path)

    await _cobrar(use_case)

    (accion,) = prompt.asked
    transaction = accion.transaction
    assert transaction is not None
    # Lo que se confirma es una transacción de EVM y no otra cosa: ni una orden
    # firmada del recinto ni un payload de otra cadena. La afirmación hace además
    # de filtro para el comprobador de tipos, que si no vería la unión entera.
    assert isinstance(transaction, UnsignedTransaction)
    assert transaction.to_address == CONDITIONAL_TOKENS
    assert transaction.chain_id == POLYGON_ID
    assert transaction.value.raw == 0, "cobrar no manda valor consigo"
    detalles = " ".join(accion.details)
    assert "¿Ocurrirá lo medido?" in detalles
    assert f"{PARTICIPACIONES:f}" in detalles
    assert SELECTOR_REDEEM not in detalles, "el texto es para leer, no calldata"
    # Es irreversible y el diálogo lo tiene que poder decir.
    assert accion.is_irreversible
    # La transacción que se firmó es la misma: se busca su destino en el `raw`.
    assert CONDITIONAL_TOKENS[2:].lower() in node.sent_raw()[0].lower()


async def test_el_dialogo_dice_que_se_cobra_y_no_que_se_gasta(tmp_path: Path) -> None:
    """Quien venga de operar puede esperar un gasto, y esto es lo contrario.

    El dinero **entra** en la cartera que firma, y decirlo con esas palabras es lo
    que evita que alguien confirme creyendo que está pagando.
    """
    use_case, _, _, prompt, _, _ = await _armed(tmp_path)

    await _cobrar(use_case)

    (accion,) = prompt.asked
    aviso = " ".join(accion.details)
    assert "entra en la cartera que firma" in aviso
    assert "cuesta gas" in aviso
    assert "no se puede deshacer" in aviso


# --------------------------------------------------------------------------- #
# 4. El asiento
# --------------------------------------------------------------------------- #
async def test_el_asiento_dice_que_el_dinero_entra(tmp_path: Path) -> None:
    """Las columnas del registro no tienen signo, así que lo lleva el texto.

    Quien sume la columna del importe para reconstruir el gasto del día contaría
    un cobro como un gasto si el renglón no dijera lo contrario. Y se nombra el
    mercado por su condición, que es lo que hace falta para volver a mirarlo en el
    explorador.
    """
    use_case, _, ledger, _, _, _ = await _armed(tmp_path)

    await _cobrar(use_case)

    (asiento,) = ledger.entries()
    assert asiento.kind == "redeem"
    assert asiento.notional == "12.5"
    assert asiento.notional_symbol == "USDC"
    assert asiento.recipient == DIRECCION
    assert asiento.engine_id == "polymarket"
    assert asiento.chain == "polygon"
    assert asiento.pair == "cobro ¿Ocurrirá lo medido? — Sí"
    assert "entra colateral, no sale" in asiento.description
    assert MERCADO_A in asiento.description
    # Y lo que decide todo: no cuenta para el tope.
    assert asiento.counts_towards_limits is False


async def test_dos_resultados_del_mismo_mercado_son_una_sola_transaccion(
    tmp_path: Path,
) -> None:
    """Una cartera puede tener los dos lados del mismo mercado, y es una llamada.

    El contrato recorre los índices y quema lo que haya, así que cobrarlos por
    separado costaría dos veces el gas de la misma transacción.
    """
    use_case, node, ledger, _, _, _ = await _armed(tmp_path)
    dos = (
        _posicion(shares=Decimal("10"), outcome_label="Sí", token_id=TOKEN_SI),
        _posicion(shares=Decimal("2.5"), outcome_label="No", token_id=TOKEN_NO),
    )

    (recibo,) = await _cobrar(use_case, *dos)

    assert len(node.sent_raw()) == 1
    (asiento,) = ledger.entries()
    # El importe es la suma de lo que pagan las dos, y una sola fila.
    assert asiento.notional == "12.5"
    assert recibo.status.value == "success"


# --------------------------------------------------------------------------- #
# 5. Varios mercados
# --------------------------------------------------------------------------- #
async def test_cada_mercado_es_su_propia_transaccion_y_en_su_orden(
    tmp_path: Path,
) -> None:
    """El contrato cobra un mercado por llamada: dos mercados, dos transacciones.

    Y en el orden en que llegaron, porque el usuario las confirma una detrás de
    otra y firmarlas en otro orden haría que el segundo diálogo apareciera antes
    de lo que él cree.
    """
    use_case, node, ledger, prompt, _, motor = await _armed(tmp_path)
    tres = (
        _posicion(condition_id=MERCADO_A),
        _posicion(condition_id=MERCADO_B, outcome_label="No", token_id=TOKEN_NO),
        _posicion(condition_id=MERCADO_C),
    )

    recibos = await _cobrar(use_case, *tres)

    assert len(recibos) == 3
    assert motor.construidos == 3
    assert len(prompt.asked) == 3
    emitidas = [raw.lower() for raw in node.sent_raw()]
    assert len(emitidas) == 3
    # Cada identificador de mercado viaja en su propia transacción, y en su sitio.
    for esperado, emitido in zip((MERCADO_A, MERCADO_B, MERCADO_C), emitidas, strict=True):
        assert esperado[2:] in emitido
    assert len(ledger.entries()) == 3
    assert [asiento.kind for asiento in ledger.entries()] == ["redeem"] * 3


async def test_un_mercado_que_no_cabe_deja_la_tanda_sin_empezar(tmp_path: Path) -> None:
    """Todo se comprueba antes de emitir nada, y se mide en transacciones.

    Es la razón de que la comprobación de cada mercado se haga fuera del bucle de
    emisión: lo que se comprueba ahí es **puro** —no gasta red ni pide la clave—,
    así que adelantarlo no cuesta nada y evita lo que de verdad cuesta dinero, que
    es cobrar el primero y descubrir en el segundo algo que se sabía desde el
    principio. Sin esta prueba, devolver la construcción al bucle no rompería
    nada, y el fallo volvería en silencio.
    """
    use_case, node, ledger, prompt, _, motor = await _armed(tmp_path)

    with pytest.raises(ExecutionError, match="todavía no ha resuelto"):
        await _cobrar(
            use_case,
            _posicion(condition_id=MERCADO_A),
            _posicion(condition_id=MERCADO_B, redeemable=False),
        )

    # Los dos se construyeron —el segundo es el que falla— y **ninguno** se emitió.
    assert motor.construidos == 2
    assert node.sent_raw() == []
    assert prompt.asked == [], "no se pide confirmar nada de una tanda que no va"
    assert ledger.entries() == ()


async def test_si_falla_un_mercado_se_dice_cuantos_se_cobraron(tmp_path: Path) -> None:
    """Parar y contarlo: lo callado sería dinero cobrado sin recibo.

    Se para en el primero que falle en vez de seguir firmando: las siguientes
    transacciones se firmarían sobre un estado que el usuario ya no está viendo.
    Pero lo que sí pasó tiene que decirse, porque un cobro emitido y no contado es
    justo la clase de hueco que hace inútil un registro.
    """
    use_case, node, ledger, _, _, _ = await _armed(
        tmp_path, node=_node(eth_getTransactionReceipt=_recibos("0x1", "0x0"))
    )
    dos = (_posicion(condition_id=MERCADO_A), _posicion(condition_id=MERCADO_B))

    with pytest.raises(ExecutionError, match=r"se cobraron 1 mercado") as fallo:
        await _cobrar(use_case, *dos)

    assert "el cobro no llegó a buen término" in str(fallo.value)
    assert len(node.sent_raw()) == 2, "la segunda se emitió y revirtió: existe"
    # Las dos quedan anotadas: una cobrada y otra revertida. Esconder la segunda
    # dejaría el registro sin la transacción que sí se pagó.
    primera, segunda = ledger.entries()
    assert primera.status == "success"
    assert segunda.status == "reverted"
    assert all(asiento.counts_towards_limits is False for asiento in ledger.entries())


# --------------------------------------------------------------------------- #
# 6. Los límites: el cobro no gasta presupuesto
# --------------------------------------------------------------------------- #
async def test_cobrar_mas_que_el_tope_por_operacion_no_se_bloquea(tmp_path: Path) -> None:
    """Los topes acotan lo que **sale**; medir un cobro contra ellos lo encerraría.

    Cuanto más ganada está una posición, más grande es el cobro, así que aplicarle
    el tope tendría la consecuencia absurda de bloquear precisamente las que más
    merecen cobrarse.
    """
    use_case, node, ledger, _, _, _ = await _armed(
        tmp_path, limits=_limites(max_quote_per_trade=Decimal("1"))
    )

    (recibo,) = await _cobrar(use_case)

    assert recibo.status.value == "success"
    assert len(node.sent_raw()) == 1
    assert ledger.entries()[0].notional == "12.5", "12,5 cobrados con tope de 1"


async def test_cobrar_no_consume_el_presupuesto_del_dia(tmp_path: Path) -> None:
    """Ni lo gasta ni lo mira: recuperar lo propio no es operar.

    Si contara, cobrar una posición grande acercaría al usuario a quedarse sin
    poder operar — y bastaría cobrar para agotar el día.
    """
    use_case, _, ledger, _, _, _ = await _armed(
        tmp_path,
        limits=_limites(max_quote_per_day=Decimal("1"), max_quote_per_trade=Decimal("1")),
    )

    await _cobrar(use_case)

    assert ledger.spent_since(AHORA - timedelta(days=1)) == Decimal("0")
    assert ledger.entries()[0].counts_towards_limits is False


async def test_el_cobro_si_pasa_por_el_interruptor_maestro(tmp_path: Path) -> None:
    """No le aplica el importe, pero sí todo lo demás: apagado, no se cobra.

    Se comprueba explícitamente para que la excepción del importe no se lea como
    una excepción de la comprobación entera.
    """
    use_case, node, ledger, prompt, keys, motor = await _armed(
        tmp_path, limits=_limites(enabled=False)
    )

    with pytest.raises(ExecutionLimitExceededError, match="ejecución deshabilitada"):
        await _cobrar(use_case)

    # El cobro sí se construyó, y no es un descuido: `build_redeem` es **puro** —no
    # gasta red ni ve la clave— y va antes de la política igual que en los otros dos
    # caminos, para que un mercado sin resolver se explique como tal en vez de
    # quedar tapado por el interruptor. Lo que importa es lo de abajo.
    assert motor.construidos == 1
    assert node.calls == [], "no se gasta ni una petición"
    assert keys.pedida == 0, "ni se llega a pedir la clave"
    assert prompt.asked == [], "ni se pide confirmación de algo que no cabe"
    assert ledger.entries() == ()


async def test_el_cobro_comprueba_la_red(tmp_path: Path) -> None:
    """«Redes permitidas» es una lista de redes en las que se firma, y el cobro firma."""
    use_case, node, _, _, _, _ = await _armed(
        tmp_path, limits=_limites(allowed_chains=frozenset({"base"}))
    )

    with pytest.raises(ExecutionLimitExceededError, match="redes permitidas"):
        await _cobrar(use_case)

    assert node.sent_raw() == []


async def test_el_cobro_comprueba_el_colateral(tmp_path: Path) -> None:
    """Lo único que se mueve en un cobro es el colateral, y es lo que se comprueba."""
    use_case, node, _, _, _, _ = await _armed(
        tmp_path, limits=_limites(allowed_tokens=frozenset({"WETH"}))
    )

    with pytest.raises(ExecutionLimitExceededError, match="tokens permitidos"):
        await _cobrar(use_case)

    assert node.sent_raw() == []


async def test_el_cobro_comprueba_el_motor(tmp_path: Path) -> None:
    """Quien apaga un motor lo apaga entero: leer, operar y cobrar."""
    use_case, node, _, _, _, _ = await _armed(
        tmp_path, limits=_limites(allowed_engines=frozenset({"otro"}))
    )

    with pytest.raises(ExecutionLimitExceededError, match="motores permitidos"):
        await _cobrar(use_case)

    assert node.sent_raw() == []


# --------------------------------------------------------------------------- #
# 7. La clave no se filtra
# --------------------------------------------------------------------------- #
async def test_la_clave_no_aparece_ni_en_el_registro_ni_en_el_log(tmp_path: Path) -> None:
    """Se busca la cadena literal en lo que se escribió, no se confía en nadie.

    Un `executions.jsonl` con la clave dentro sería el peor sitio posible para
    ella: texto plano, que crece y que se copia en una copia de seguridad. Y el
    cuerpo que viaja al nodo tampoco puede llevarla: lleva la **firma**, que es
    pública.
    """
    use_case, node, ledger, _, _, _ = await _armed(tmp_path)

    with capture_logs() as registros:
        await _cobrar(use_case)

    escrito = ledger.path.read_text(encoding="utf-8")
    assert CLAVE not in escrito
    assert CLAVE not in repr(ledger.entries())
    assert CLAVE not in node.sent_raw()[0]
    volcado = json.dumps(registros, default=str)
    assert CLAVE not in volcado
    assert DIRECCION in volcado, "la dirección sí: es lo que el usuario necesita ver"


# --------------------------------------------------------------------------- #
# 8. Apoyo
# --------------------------------------------------------------------------- #
async def test_la_direccion_que_firma_es_la_de_la_clave(tmp_path: Path) -> None:
    """La interfaz muestra de qué cartera sale el cobro, y es la derivada."""
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
