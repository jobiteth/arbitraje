"""Retirar fondos: el camino más corto que existe para perder dinero.

Un swap tiene un router que puede equivocarse, una cotización que puede mentir y
un motor al que culpar. Una retirada no tiene nada de eso: se construye la
transferencia, se firma y se emite. Por eso lo que se fija aquí son los sitios
donde una retirada se convierte en dinero perdido, y son seis:

1. **Que no se emita fuera del modo `EJECUCIÓN`** — y que la barrera esté *antes*
   de gastar red, medido en peticiones al nodo y en veces que se pidió la clave.
2. **Que una dirección mal escrita no llegue a firmarse.** Es el fallo que más
   caro sale de todos: una transferencia a una dirección que no existe por un
   carácter de más **no revierte**, se ejecuta, y el dinero se va. La prueba
   corta con la dirección truncada, la de longitud de más, la vacía y la cero.
3. **Que mandarse fondos a la propia cartera se rechace** en el dominio, antes de
   gastar gas en una transferencia que no mueve nada.
4. **Que retirar más de lo que hay se corte antes de firmar**, tanto en un ERC-20
   como —y ahí está lo que se olvida— en el nativo, donde el gas sale de la misma
   cuenta que el importe y por eso «todo mi saldo» nunca cabe.
5. **Que el importe pase por los topes**, valorado en la moneda del tope cuando el
   token no es esa moneda, y **rechazado** cuando no se puede valorar: un tope que
   se salta cuando no se puede medir no es un tope.
6. **Que el `engine_id` sea `wallet`**, porque `allowed_engines` es una lista de
   inclusión y una lista blanca sin `wallet` tiene que bloquear la retirada.

El payload se comprueba por su **forma binaria**, no por lo que devuelve la
función que lo construye: `transfer(address,uint256)` son 4 bytes de selector más
dos palabras de 32, y el selector se escribe aquí a mano desde la definición
pública del estándar. Comprobar el camino con la misma constante que usa el camino
no comprobaría nada.

Se usa un `EvmBroadcaster` **real** sobre un nodo de mentira y una clave de
desarrollo **publicada** (cuenta 0 de Hardhat, sin fondos en ninguna red) para que
la firma sea de verdad: probar el camino del dinero con la firma simulada dejaría
sin probar justo la parte que mueve el dinero.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_utils.crypto import keccak

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
from amigocompora.app.usecases.value_in_reference import ReferenceValuation
from amigocompora.app.usecases.withdraw import NATIVE_TRANSFER_GAS, WithdrawFunds
from amigocompora.domain.chains import ChainSpec
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
    ExecutionError,
    ExecutionLimitExceededError,
    InvalidAmountError,
    ModeNotPermittedError,
    UnsupportedOperationError,
)
from amigocompora.domain.execution import ExecutionLimits, TriggerKind
from amigocompora.domain.models import BroadcastStatus, Token
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import TokenAmount
from amigocompora.engines.catalog import native_token, quote_token
from amigocompora.infra.evm.broadcast import EvmBroadcaster
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool

AHORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
CHAIN_ID = 8453
DESTINO = "0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F"

#: Selector de `transfer(address,uint256)`, escrito a mano desde la definición
#: pública del estándar ERC-20 y no pedido a `build_transfer_calldata`: comprobar
#: el camino con la misma función que usa el camino no comprueba nada. Es el
#: dato que decide **qué** hace la transacción, así que un selector equivocado
#: llamaría a otra función del mismo contrato con los mismos bytes.
SELECTOR_TRANSFER = "a9059cbb"

#: Cuenta 0 de Hardhat. Publicada en la documentación de Hardhat y Anvil, sin
#: fondos en ninguna red real, y con la dirección derivada aquí, no recordada.
CLAVE = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

#: Un ERC-20 cualquiera de Base, con su dirección en minúsculas y su escala.
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TOKEN = Token(symbol="USDC", decimals=6, chain="base", address=USDC)


def _nativo() -> Token:
    return native_token("base")


# --------------------------------------------------------------------------- #
# Dobles
# --------------------------------------------------------------------------- #
class FakeKey(PrivateKeySource):
    """La clave, que se puede pedir, y que **cuenta** cuántas veces se pidió.

    El contador es lo que permite afirmar que en los modos que no ejecutan no se
    llegó a tocar la clave: una barrera que se comprueba después de pedir la
    credencial ya la leyó del llavero.
    """

    def __init__(self, key: str = CLAVE, *, hay: bool = True) -> None:
        self._key = key
        self._hay = hay
        self.pedida = 0

    def available(self) -> bool:
        return self._hay

    def require(self) -> str:
        self.pedida += 1
        if not self._hay:
            raise AssertionError("no había clave y se pidió igualmente")
        return self._key


class _FakePassphrase:
    def get(self) -> str | None:
        return None


class _Valuation:
    """Valoración de mentira: contesta lo que se le diga y cuenta las preguntas."""

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

    def metodos(self) -> list[str]:
        return [name for name, _ in self.calls]


def _hash_of(params: list[Any]) -> str:
    """El hash que el nodo devolvería: es el keccak del propio `raw`.

    Se calcula en vez de inventarlo porque `EvmBroadcaster.send` compara el hash
    que contesta el nodo con el que él calculó al firmar: un hash distinto
    significa que se emitió algo que no es lo que se cree.
    """
    return "0x" + keccak(bytes.fromhex(str(params[0])[2:])).hex()


def _node(
    *,
    saldo_nativo: int = 10**18,
    saldo_token: int = 10**12,
    **overrides: Any,
) -> FakeNode:
    """Un nodo que contesta saldos por omisión **de sobra**.

    Los saldos por omisión son grandes a propósito: casi todas las pruebas de este
    fichero van de otra cosa, y una que fallara por saldo insuficiente estaría
    comprobando el caso que no dice.
    """

    def eth_call(params: list[Any]) -> str:
        llamada = params[0]
        datos = str(llamada.get("data", ""))
        if datos.startswith("0x70a08231"):  # balanceOf(address)
            # El retorno tiene que ser una **palabra** de 32 bytes, no un número
            # corto: `_return_word` corta por índice y un `0xf4240` de cinco bytes
            # le haría creer que le contestó otro contrato. Es el mismo fallo que
            # se midió en la cadena con la primera venta de un ERC-20.
            return "0x" + f"{saldo_token:064x}"
        raise AssertionError(f"eth_call inesperado: {datos[:10]}")

    handlers: dict[str, Any] = {
        "eth_chainId": hex(CHAIN_ID),
        "eth_getBalance": hex(saldo_nativo),
        "eth_getTransactionCount": "0x5",
        "eth_estimateGas": "0x5208",
        "eth_getBlockByNumber": {"baseFeePerGas": "0x3b9aca00"},
        "eth_maxPriorityFeePerGas": "0x3b9aca",
        "eth_call": eth_call,
        "eth_sendRawTransaction": _hash_of,
        "eth_getTransactionReceipt": {"blockNumber": "0x10", "status": "0x1"},
    }
    handlers.update(overrides)
    return FakeNode(**handlers)


def _broadcaster(node: FakeNode, chain_key: str = "base") -> EvmBroadcaster:
    pool = RpcPool(
        chain_key,
        (RpcEndpoint(url=f"https://{chain_key}.example.org", label="falso", priority=10),),
        httpx.AsyncClient(transport=node.transport()),
        clock=FrozenClock(AHORA),
    )
    return EvmBroadcaster(
        pool,
        chain_key,
        clock=FrozenClock(AHORA),
        receipt_poll_interval_seconds=0.0,
        receipt_poll_attempts=2,
    )


# --------------------------------------------------------------------------- #
# Armado
# --------------------------------------------------------------------------- #
def _limits(**overrides: Any) -> ExecutionLimits:
    """Límites que describen una retirada **permitida**.

    El interruptor maestro se prueba aparte y en su propia prueba: apagarlo aquí
    haría que todas las de este fichero fallaran por el mismo motivo —y ninguna
    por el suyo—, que es la forma de tener treinta pruebas comprobando una sola
    cosa.
    """
    values: dict[str, Any] = {
        "enabled": True,
        "max_quote_per_trade": Decimal("5000"),
        "max_quote_per_day": Decimal("20000"),
        "allowed_tokens": frozenset({"USDC", "ETH"}),
        "allowed_chains": frozenset({"base"}),
        # `wallet` está aquí porque una retirada se anota con ese motor, y la
        # lista es de **inclusión**: sin él, retirar está bloqueado. Eso se prueba
        # en su propia prueba, quitándolo.
        "allowed_engines": frozenset({"wallet"}),
    }
    values.update(overrides)
    return ExecutionLimits(**values)


def _armed(
    tmp_path: Path,
    *,
    mode: OperationMode = OperationMode.EXECUTION,
    limits: ExecutionLimits | None = None,
    node: FakeNode | None = None,
    answer: bool = True,
    valuation: ReferenceValuation | None = None,
    key: FakeKey | None = None,
    broadcasters: Mapping[str, EvmBroadcaster] | None = None,
) -> tuple[WithdrawFunds, FakeNode, ExecutionLedger, RecordingPrompt, FakeKey]:
    """Un `WithdrawFunds` completo, con emisor **real** sobre un nodo de mentira."""
    clock = FrozenClock(AHORA)
    ledger = ExecutionLedger(tmp_path / "executions.jsonl", clock=clock)
    keys = key or FakeKey()
    policy = AutonomyPolicy(
        keys=keys,
        passphrase=_FakePassphrase(),
        limits=limits or _limits(),
        ledger=ledger,
        clock=clock,
        trigger=TriggerKind.MANUAL,
    )
    prompt = RecordingPrompt(answer=answer)
    gateway = ConfirmationGateway(ModeGuard(mode), prompt=prompt, clock=clock)
    fake = node or _node()
    mapa = {"base": _broadcaster(fake)} if broadcasters is None else broadcasters
    withdraw = WithdrawFunds(
        gateway=gateway,
        keys=keys,
        policy=policy,
        broadcasters=mapa,
        valuation=valuation,
    )
    return withdraw, fake, ledger, prompt, keys


def _valoracion_ok(valor: str = "1800") -> _Valuation:
    """Una valoración que **cabe** en los topes de `_limits`.

    Casi todas las pruebas del nativo van de otra cosa —la reserva del gas, la
    forma del payload— y con un tope de 5 000 USDC por operación, medio ETH
    valorado en 1 800 pasa. Una que fallara por el tope estaría comprobando el
    caso que no dice.
    """
    return _Valuation(value=TokenAmount(int(Decimal(valor) * 10**6), 6, "USDC"))


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
    se tocó la clave: un caso de uso que comprobara el modo después de leer el
    saldo pasaría una prueba que sólo mirase el error, y estaría preguntándole sus
    fondos a un nodo para una operación que ya sabe que no va a poder emitir.
    """
    withdraw, node, ledger, prompt, keys = _armed(tmp_path, mode=mode)

    with pytest.raises(ModeNotPermittedError):
        await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)

    assert node.calls == []
    assert keys.pedida == 0
    assert prompt.asked == []
    assert ledger.entries() == ()


async def test_sin_confirmacion_no_se_emite(tmp_path: Path) -> None:
    """Decir que no en el diálogo no puede dejar una transacción a medias.

    Se comprueba además que el saldo **sí** se leyó —la comprobación va antes del
    diálogo— y que aun así no se emitió nada: lo que se corta con un «no» es la
    emisión, no el trabajo previo, que es de sólo lectura.
    """
    withdraw, node, ledger, prompt, _ = _armed(tmp_path, answer=False)

    with pytest.raises(ConfirmationDeniedError):
        await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)

    assert node.sent_raw() == []
    assert ledger.entries() == ()
    # La pregunta se hizo **una** vez, con el destino delante y antes de firmar:
    # un rechazo que llegara después de firmar dejaría una transacción firmada
    # rondando aunque no se emitiera.
    assert len(prompt.asked) == 1
    assert DESTINO in " ".join(prompt.asked[0].details)


async def test_la_barrera_se_apunta_como_bloqueada_por_el_modo(tmp_path: Path) -> None:
    """La traza de auditoría distingue lo bloqueado por el modo de lo denegado."""
    withdraw, _, _, _, _ = _armed(tmp_path, mode=OperationMode.OBSERVATION)
    with pytest.raises(ModeNotPermittedError):
        await withdraw(TOKEN, TOKEN.amount("1"), recipient=DESTINO)


# --------------------------------------------------------------------------- #
# 2. El destinatario
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "destino",
    [
        "0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4",  # un carácter de menos
        "0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4FF",  # dos de más
        "0x" + "z" * 40,  # hex imposible
        "9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F",  # sin el prefijo
        "",
        "0x" + "0" * 40,  # la dirección cero
    ],
)
async def test_una_direccion_mal_escrita_no_llega_a_firmarse(
    tmp_path: Path, destino: str
) -> None:
    """El fallo más caro de este fichero: una transferencia no se puede deshacer.

    Mandar a una dirección que no existe **no revierte**: se ejecuta, el gas se
    paga y el dinero se queda donde nadie puede moverlo. Por eso se corta antes de
    leer el saldo siquiera — la comprobación no necesita red y no la gasta.
    """
    withdraw, node, ledger, _, keys = _armed(tmp_path)

    with pytest.raises(InvalidAmountError):
        await withdraw(TOKEN, TOKEN.amount("10"), recipient=destino)

    assert node.calls == []
    assert keys.pedida == 0
    assert ledger.entries() == ()


async def test_una_direccion_bien_escrita_pasa(tmp_path: Path) -> None:
    """El contraste: la misma dirección válida sí se emite. Sin esto, la prueba
    anterior pasaría con una validación que rechazara todo."""
    withdraw, node, _, _, _ = _armed(tmp_path)
    receipt = await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)
    assert receipt.status is BroadcastStatus.SUCCESS
    assert len(node.sent_raw()) == 1


async def test_mandarse_fondos_a_uno_mismo_se_rechaza(tmp_path: Path) -> None:
    """Una transferencia a la propia cartera sólo gasta gas.

    Se rechaza en el **dominio**, no aquí, para que no dependa de que cada caso de
    uso se acuerde de comprobarlo. Lo que se fija es que llegue a lanzar antes de
    firmar: es un error de pegado, y el momento de decirlo es antes.
    """
    withdraw, node, _, _, _ = _armed(tmp_path)

    with pytest.raises(InvalidAmountError, match="misma cartera"):
        await withdraw(TOKEN, TOKEN.amount("10"), recipient=DIRECCION)

    assert node.sent_raw() == []


# --------------------------------------------------------------------------- #
# 3. La red
# --------------------------------------------------------------------------- #
async def test_en_solana_se_rechaza_con_un_mensaje_que_lo_dice(tmp_path: Path) -> None:
    """En Solana no se firma todavía, y hay que decirlo antes de gastar red.

    No es una limitación escondida: la transacción de Solana se construye de otra
    forma y su comisión se paga en otra unidad, así que reutilizar este camino
    produciría una transacción que nadie puede emitir.
    """
    solana = quote_token("solana")
    assert solana is not None
    withdraw, node, _, _, _ = _armed(tmp_path)

    with pytest.raises(UnsupportedOperationError, match="no está implementado"):
        await withdraw(solana, solana.amount("10"), recipient=DESTINO)

    assert node.calls == []


async def test_sin_nodo_para_la_red_se_explica_cual_falta(tmp_path: Path) -> None:
    """Sin emisor no hay a quién leerle el saldo ni dónde emitir, y se dice cuál."""
    withdraw, _, _, _, _ = _armed(tmp_path, broadcasters={})

    with pytest.raises(ExecutionError, match="base"):
        await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)


def test_el_guard_no_se_fia_del_nombre_sino_del_hecho() -> None:
    """`require_evm` corta por el **formato de dirección**, no por el nombre.

    Se construye el `ChainSpec` a propósito en vez de sacarlo del catálogo: si
    mañana alguien añade una red EVM y borra la de prueba, esta prueba tiene que
    seguir comprobando lo que dice. Y se pregunta por `is_evm`, que es el hecho
    del que depende de verdad que la firma EIP-1559 aplique.
    """
    from amigocompora.domain.chains import AddressFormat
    from amigocompora.infra.evm.broadcast import require_evm

    falsa = ChainSpec(
        key="falsa",
        name="Red falsa",
        native_symbol="X",
        native_decimals=18,
        address_format=AddressFormat.SOLANA_BASE58,
        eip155_id=None,
    )
    assert not falsa.is_evm
    with pytest.raises(UnsupportedOperationError, match="no es una red EVM"):
        require_evm(falsa)


# --------------------------------------------------------------------------- #
# 4. El saldo
# --------------------------------------------------------------------------- #
async def test_un_erc20_por_encima_del_saldo_se_corta_antes_de_firmar(tmp_path: Path) -> None:
    """Sin esta comprobación la transferencia revertiría y el gas se pagaría igual."""
    node = _node(saldo_token=10**6)  # 1 USDC
    withdraw, node, ledger, _, _ = _armed(tmp_path, node=node)

    with pytest.raises(ExecutionError, match="tiene 1 USDC"):
        await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)

    assert node.sent_raw() == []
    assert ledger.entries() == ()


async def test_un_erc20_por_debajo_del_saldo_pasa(tmp_path: Path) -> None:
    """El contraste exacto del anterior."""
    node = _node(saldo_token=10**7)  # 10 USDC
    withdraw, node, _, _, _ = _armed(tmp_path, node=node)
    receipt = await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)
    assert receipt.status is BroadcastStatus.SUCCESS


async def test_en_el_nativo_hay_que_dejar_el_gas(tmp_path: Path) -> None:
    """«Todo mi saldo» no cabe nunca, y ése es el error que se evita.

    En el nativo el importe y la comisión salen de la **misma** cuenta, así que
    mandar el saldo entero deja la cartera sin con qué pagar el movimiento. La
    reserva se calcula con el gas fijo de una transferencia sin calldata y el
    precio máximo, que es el peor caso: si pasa la comprobación, pasa de verdad.

    Se mide el borde **por los dos lados** y contra las comisiones que el emisor
    de verdad usa —leídas del propio emisor, no supuestas—, porque una reserva
    calculada con la base en vez del máximo pasaría una prueba que sólo mirara
    que «el saldo entero falla».
    """
    nativo = _nativo()
    node = _node(saldo_nativo=10**18)
    withdraw, node, _, _, _ = _armed(tmp_path, node=node, valuation=_valoracion_ok())

    # El saldo entero, que es justo lo que no cabe.
    with pytest.raises(ExecutionError, match="gas"):
        await withdraw(nativo, nativo.amount("1"), recipient=DESTINO)
    assert node.sent_raw() == []

    # El borde exacto: el saldo menos la reserva al precio que el emisor cobra.
    emisor = _broadcaster(node)
    fees = await emisor.read_fees()
    reserva = NATIVE_TRANSFER_GAS * fees.max_fee_per_gas
    exacto = TokenAmount(10**18 - reserva, nativo.decimals, nativo.symbol)
    receipt = await withdraw(nativo, exacto, recipient=DESTINO)
    assert receipt.status is BroadcastStatus.SUCCESS

    # Y un solo wei por encima de ese borde ya no cabe. Es la mitad que convierte
    # la reserva en una cifra con significado: sin ella, reservar de más pasaría.
    demasiado = TokenAmount(10**18 - reserva + 1, nativo.decimals, nativo.symbol)
    with pytest.raises(ExecutionError, match="gas"):
        await withdraw(nativo, demasiado, recipient=DESTINO)


async def test_la_reserva_no_deja_pasar_una_retirada_que_no_cubriria_el_gas(
    tmp_path: Path,
) -> None:
    """Con muy poco saldo, la reserva del gas es lo que corta y no el importe.

    Es el caso que de verdad ocurría en la cartera medida: 0,00000258 ETH en Base.
    Retirar el equivalente a la mitad del saldo dejaba la cuenta sin gas para
    moverlo, y el nodo lo habría rechazado después de firmar.
    """
    nativo = _nativo()
    # 5 000 wei, muy por debajo de los 21 000 por el precio que cuesta moverlo.
    node = _node(saldo_nativo=5_000)
    withdraw, node, _, _, _ = _armed(tmp_path, node=node, valuation=_valoracion_ok())

    with pytest.raises(ExecutionError, match="gas"):
        await withdraw(nativo, TokenAmount(1, nativo.decimals, nativo.symbol),
                       recipient=DESTINO)

    assert node.sent_raw() == []


# --------------------------------------------------------------------------- #
# 5. El payload
# --------------------------------------------------------------------------- #
async def test_un_erc20_llama_a_transfer_en_su_contrato(tmp_path: Path) -> None:
    """La forma binaria del payload, comprobada byte a byte.

    El destino de la **llamada** es el contrato del token y el de los **fondos**
    va dentro de la calldata: son dos direcciones distintas y confundirlas manda
    el dinero a la dirección del contrato.
    """
    withdraw, node, _, _, _ = _armed(tmp_path)
    await withdraw(TOKEN, TOKEN.amount("1.5"), recipient=DESTINO)

    crudo = node.sent_raw()[0]
    tx = _decode_raw(crudo)
    assert tx["to"].lower() == USDC.lower(), "la llamada no va al contrato del token"
    assert tx["value"] == 0, "un ERC-20 no manda valor nativo"

    datos = tx["data"]
    assert datos[:10] == "0x" + SELECTOR_TRANSFER, (
        "el selector no es transfer(address,uint256): llamaría a otra función del "
        "mismo contrato con los mismos bytes"
    )
    assert len(datos) == 2 + 8 + 128, "transfer son un selector y dos palabras de 32 bytes"
    assert int(datos[10:74], 16) == int(DESTINO, 16), "el destinatario va en la 1ª palabra"
    assert int(datos[74:138], 16) == 1_500_000, "el importe va en la 2ª palabra, en raw"


async def test_el_nativo_va_en_el_value_y_sin_calldata(tmp_path: Path) -> None:
    """El caso opuesto, y por eso se prueba aparte.

    Aquí el destinatario de la llamada y el de los fondos son **el mismo**, y no
    hay contrato al que llamar. Una calldata en una transferencia nativa sería
    código que se ejecuta en la dirección de destino.
    """
    nativo = _nativo()
    withdraw, node, _, _, _ = _armed(tmp_path, valuation=_valoracion_ok("450"))
    await withdraw(nativo, nativo.amount("0.25"), recipient=DESTINO)

    tx = _decode_raw(node.sent_raw()[0])
    assert tx["to"].lower() == DESTINO.lower()
    assert tx["value"] == 250_000_000_000_000_000
    assert tx["data"] in ("", "0x"), "una transferencia nativa no lleva calldata"


def _decode_raw(crudo: str) -> dict[str, Any]:
    """El `chain_id`, `to`, `value` y `data` de una transacción EIP-1559 firmada.

    Se decodifica el RLP a mano en vez de pedírselo a `sign_eip1559` porque lo que
    hay que comprobar es justo **lo que se emitió**: preguntarle a la función que
    construye por lo que construyó sería comprobar que es coherente consigo misma.

    El `raw` de una EIP-1559 es el byte `0x02` —el tipo— seguido de una lista de
    doce campos. Los índices que se leen aquí salen de la definición del estándar,
    no de contar los que devuelve el decodificador: contar dejaría la prueba
    leyendo el campo de al lado si algún día cambia el número de campos.
    """
    cuerpo = bytes.fromhex(crudo[2:])
    assert cuerpo[0] == 0x02, f"no es una transacción EIP-1559: el tipo es {cuerpo[0]:#x}"
    campos = _rlp_field(cuerpo, 1)[0]
    assert isinstance(campos, list), "el cuerpo de una EIP-1559 es una lista RLP"
    assert len(campos) == 12, f"una EIP-1559 tiene 12 campos, llegaron {len(campos)}"
    return {
        "chain_id": int.from_bytes(campos[0], "big"),
        "to": "0x" + campos[5].hex(),
        "value": int.from_bytes(campos[6], "big"),
        "data": "0x" + campos[7].hex(),
    }


def _rlp_field(payload: bytes, i: int) -> tuple[Any, int]:
    """Un elemento RLP a partir del índice `i`: el valor y dónde acaba.

    Veinte líneas para no compartir dependencia ni código con el camino que se
    comprueba: leer el `raw` es todo lo que hace falta, y hacerlo aquí garantiza
    que lo que se mide es el byte emitido y no la intención de quien lo emitió.
    """
    prefijo = payload[i]
    if prefijo < 0x80:
        return payload[i : i + 1], i + 1
    if prefijo <= 0xB7:
        largo = prefijo - 0x80
        return payload[i + 1 : i + 1 + largo], i + 1 + largo
    if prefijo <= 0xBF:
        ancho = prefijo - 0xB7
        largo = int.from_bytes(payload[i + 1 : i + 1 + ancho], "big")
        inicio = i + 1 + ancho
        return payload[inicio : inicio + largo], inicio + largo
    if prefijo <= 0xF7:
        cuerpo = payload[i + 1 : i + 1 + (prefijo - 0xC0)]
        return _rlp_list(cuerpo), i + 1 + (prefijo - 0xC0)
    ancho = prefijo - 0xF7
    largo = int.from_bytes(payload[i + 1 : i + 1 + ancho], "big")
    inicio = i + 1 + ancho
    return _rlp_list(payload[inicio : inicio + largo]), inicio + largo


def _rlp_list(cuerpo: bytes) -> list[Any]:
    """Los elementos de una lista RLP ya recortada a su contenido."""
    campos: list[Any] = []
    i = 0
    while i < len(cuerpo):
        valor, i = _rlp_field(cuerpo, i)
        campos.append(valor)
    return campos


# --------------------------------------------------------------------------- #
# 6. Los topes
# --------------------------------------------------------------------------- #
async def test_una_retirada_por_encima_del_tope_por_operacion_se_rechaza(
    tmp_path: Path,
) -> None:
    """El tope se aplica al importe que sale, que es literalmente lo que acota."""
    withdraw, node, ledger, _, _ = _armed(
        tmp_path, limits=_limits(max_quote_per_trade=Decimal("100"))
    )

    with pytest.raises(ExecutionLimitExceededError):
        await withdraw(TOKEN, TOKEN.amount("500"), recipient=DESTINO)

    assert node.sent_raw() == []
    assert ledger.entries() == ()


async def test_la_lista_blanca_de_motores_tiene_que_nombrar_wallet(tmp_path: Path) -> None:
    """`allowed_engines` es de **inclusión**: sin `wallet`, retirar está bloqueado.

    Es el comportamiento que se quiere y por eso se fija: una lista vacía
    significa «nada permitido», así que retirar —sacar dinero sin pasar por ningún
    motor— tiene que estar nombrado a propósito.
    """
    withdraw, node, _, _, _ = _armed(
        tmp_path, limits=_limits(allowed_engines=frozenset({"uniswap_v3"}))
    )

    with pytest.raises(ExecutionLimitExceededError, match="wallet"):
        await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)

    assert node.sent_raw() == []


async def test_una_red_fuera_de_la_lista_blanca_se_rechaza(tmp_path: Path) -> None:
    withdraw, node, _, _, _ = _armed(
        tmp_path, limits=_limits(allowed_chains=frozenset({"polygon"}))
    )
    with pytest.raises(ExecutionLimitExceededError):
        await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)
    assert node.sent_raw() == []


async def test_un_token_fuera_de_la_lista_blanca_se_rechaza(tmp_path: Path) -> None:
    withdraw, node, _, _, _ = _armed(
        tmp_path, limits=_limits(allowed_tokens=frozenset({"ETH"}))
    )
    with pytest.raises(ExecutionLimitExceededError):
        await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)
    assert node.sent_raw() == []


async def test_con_la_ejecucion_apagada_no_se_retira(tmp_path: Path) -> None:
    """El interruptor maestro está por delante de todo lo demás."""
    withdraw, node, _, _, _ = _armed(tmp_path, limits=_limits(enabled=False))

    with pytest.raises(ExecutionLimitExceededError):
        await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)

    assert node.sent_raw() == []


# --------------------------------------------------------------------------- #
# 7. La valoración
# --------------------------------------------------------------------------- #
async def test_la_referencia_no_se_valora_a_si_misma(tmp_path: Path) -> None:
    """Si el token **es** la moneda del tope, el importe ya está en esa unidad.

    Valorarlo sería cambiar un hecho —el importe que sale— por una estimación del
    mismo número, y encima costaría una petición por retirada.
    """
    valoracion = _Valuation(value=TOKEN.amount("99"))
    withdraw, _, _, _, _ = _armed(tmp_path, valuation=valoracion)
    await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)
    assert valoracion.preguntas == 0


async def test_un_token_que_no_es_la_referencia_se_valora(tmp_path: Path) -> None:
    """Una retirada de ETH contra un tope escrito en dólares no se mide sola.

    Sin valorar, comparar `0,5` con un tope de `5000` dejaría pasar medio ETH
    como si fueran cincuenta céntimos.
    """
    valoracion = _Valuation(value=TokenAmount(1_800 * 10**6, 6, "USDC"))
    nativo = _nativo()
    withdraw, _, ledger, _, _ = _armed(tmp_path, valuation=valoracion)

    await withdraw(nativo, nativo.amount("0.5"), recipient=DESTINO)

    assert valoracion.preguntas == 1
    asiento = ledger.entries()[0]
    # El asiento guarda lo **valorado**, que es lo que se suma contra el tope del
    # día: 1 800 USDC, no 0,5 ETH.
    assert Decimal(asiento.notional) == Decimal("1800")
    assert asiento.notional_symbol == "USDC"


async def test_si_no_se_puede_valorar_no_se_retira(tmp_path: Path) -> None:
    """Un tope que se salta cuando no se puede medir no es un tope.

    Se rechaza, y el mensaje dice qué falta: sin esto, la salida fácil sería
    retirar sin aplicar el límite, que es exactamente lo que el límite existe
    para impedir.
    """
    valoracion = _Valuation(value=None, razon="ningún motor cotiza ETH/USDC")
    nativo = _nativo()
    withdraw, node, ledger, _, _ = _armed(tmp_path, valuation=valoracion)

    with pytest.raises(ExecutionError, match="no se pudo valorar ETH en USDC"):
        await withdraw(nativo, nativo.amount("0.5"), recipient=DESTINO)

    assert node.sent_raw() == []
    assert ledger.entries() == ()


async def test_sin_valorador_un_token_que_no_es_la_referencia_se_rechaza(
    tmp_path: Path,
) -> None:
    """Sin con qué valorar, se rechaza en vez de medirse mal."""
    nativo = _nativo()
    withdraw, node, _, _, _ = _armed(tmp_path, valuation=None)

    with pytest.raises(ExecutionError, match="no tiene con qué valorarlo"):
        await withdraw(nativo, nativo.amount("0.5"), recipient=DESTINO)

    assert node.sent_raw() == []


async def test_una_valoracion_que_no_cabe_en_el_tope_se_rechaza(tmp_path: Path) -> None:
    """El tope se aplica sobre la valoración, no sobre el importe en crudo.

    Es la comprobación que justifica todo el módulo: medio ETH son «0,5» en crudo
    —muy por debajo de cualquier tope— y 1 800 dólares en la unidad del tope.
    """
    valoracion = _Valuation(value=TokenAmount(1_800 * 10**6, 6, "USDC"))
    nativo = _nativo()
    withdraw, node, _, _, _ = _armed(
        tmp_path,
        valuation=valoracion,
        limits=_limits(max_quote_per_trade=Decimal("1000")),
    )

    with pytest.raises(ExecutionLimitExceededError):
        await withdraw(nativo, nativo.amount("0.5"), recipient=DESTINO)

    assert node.sent_raw() == []


async def test_el_nativo_se_valora_por_su_envoltorio(tmp_path: Path) -> None:
    """A los AMM no se les puede preguntar por ETH: operan con WETH.

    Preguntar por «ETH» no encuentra ninguna piscina, y la retirada se rechazaría
    por no poder medirse —un error, porque el activo sí se puede medir—. Se
    sustituye por su envoltorio, que es el **mismo** activo: envolver es un
    depósito uno a uno, sin comisión ni deslizamiento, no un intercambio. Lo que
    se fija aquí es que la pregunta que se hace es la que tiene respuesta.
    """
    visto: list[str] = []

    class _Apuntando(_Valuation):
        async def __call__(
            self, spent: Token, amount: TokenAmount, reference: Token
        ) -> TokenAmount | None:
            visto.append(spent.symbol)
            assert spent.address is not None, "se preguntó por el nativo y no por WETH"
            assert amount.raw == 25 * 10**16, "el importe tiene que viajar intacto"
            return await super().__call__(spent, amount, reference)

    nativo = _nativo()
    withdraw, _, ledger, _, _ = _armed(
        tmp_path, valuation=_Apuntando(value=TokenAmount(450 * 10**6, 6, "USDC"))
    )
    await withdraw(nativo, nativo.amount("0.25"), recipient=DESTINO)

    assert visto == ["WETH"]
    # Y lo que se anota es la retirada de **ETH**, no la de WETH: la sustitución
    # es para preguntar el precio, no para cambiar lo que el usuario retiró.
    assert ledger.entries()[0].pair.startswith("Retirada ETH")
    assert ledger.entries()[0].notional_symbol == "USDC"


# --------------------------------------------------------------------------- #
# 8. El asiento y el secreto
# --------------------------------------------------------------------------- #
async def test_la_retirada_queda_anotada_con_su_importe_y_su_destino(
    tmp_path: Path,
) -> None:
    """El registro es lo único que después explica qué salió y a dónde fue."""
    withdraw, _, ledger, _, _ = _armed(tmp_path)
    await withdraw(TOKEN, TOKEN.amount("10"), recipient=DESTINO)

    asiento = ledger.entries()[0]
    assert asiento.counts_towards_limits is True, "una retirada sale: tiene que contar"
    assert asiento.chain == "base"
    assert "Retirada" in asiento.pair
    assert Decimal(asiento.notional) == Decimal("10")
    assert asiento.notional_symbol == "USDC"
    assert asiento.engine_id == "wallet"


async def test_la_clave_no_acaba_en_el_asiento(tmp_path: Path) -> None:
    """Se busca la cadena literal en lo escrito, no se confía en que nadie la ponga."""
    withdraw, _, ledger, _, _ = _armed(tmp_path)
    await withdraw(TOKEN, TOKEN.amount("1"), recipient=DESTINO)

    volcado = (tmp_path / "executions.jsonl").read_text()
    assert CLAVE not in volcado
    assert CLAVE[2:] not in volcado
    assert ledger.entries()[0].counts_towards_limits is True


async def test_sin_clave_configurada_se_dice_y_no_se_emite(tmp_path: Path) -> None:
    """Sin credencial no hay de dónde retirar, y el mensaje dice dónde ponerla."""
    sin_clave = FakeKey(hay=False)
    withdraw, node, _, _, keys = _armed(tmp_path, key=sin_clave)

    with pytest.raises(ExecutionError, match="Credenciales"):
        await withdraw(TOKEN, TOKEN.amount("1"), recipient=DESTINO)

    assert keys.pedida == 0
    assert node.calls == []


async def test_si_la_clave_cambia_entre_el_saldo_y_la_firma_no_se_emite(
    tmp_path: Path,
) -> None:
    """La comprobación que une el saldo leído con la cartera que firma.

    El saldo se lee de la dirección de la clave **de entonces**. Si entre esa
    lectura y la firma la credencial configurada cambia, se firmaría desde una
    cartera cuyo saldo nadie miró —y el registro diría que salió de la primera—.
    Se corta antes de emitir, que es cuando todavía no ha pasado nada.
    """
    from amigocompora.infra.evm.signer import address_from_key

    # Una segunda clave publicada de desarrollo (cuenta 1 de Hardhat), distinta de
    # la primera: es el cambio de credencial, hecho con claves que no valen nada.
    otra = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    assert address_from_key(otra).lower() != DIRECCION.lower()

    class _Cambia(FakeKey):
        """Devuelve una clave la primera vez y otra después: el cambio en caliente."""

        def __init__(self) -> None:
            super().__init__()
            self._vistas = 0

        def require(self) -> str:
            self._vistas += 1
            self.pedida += 1
            return CLAVE if self._vistas == 1 else otra

    withdraw, node, ledger, _, _ = _armed(tmp_path, key=_Cambia())

    with pytest.raises(ExecutionError, match="credencial cambió"):
        await withdraw(TOKEN, TOKEN.amount("1"), recipient=DESTINO)

    assert node.sent_raw() == []
    assert ledger.entries() == ()


async def test_el_dialogo_ensena_el_destino_entero(tmp_path: Path) -> None:
    """La dirección no se acorta en el diálogo, y aquí está el porqué.

    Es el único dato que decide si el dinero llega o se pierde: acortarla
    escondería justo los caracteres donde vive un error de copia. Se comprueba
    sobre el texto que verá el usuario, no sobre una constante.
    """
    withdraw, _, _, prompt, _ = _armed(tmp_path)
    await withdraw(TOKEN, TOKEN.amount("1"), recipient=DESTINO)

    detalles = " ".join(prompt.asked[0].details)
    assert DESTINO in detalles
    assert "…" not in detalles.split("Va a esta dirección:")[1].split("\n")[0]


async def test_el_dialogo_dice_que_es_irreversible(tmp_path: Path) -> None:
    """El aviso tiene que estar donde se decide, no en la documentación."""
    withdraw, _, _, prompt, _ = _armed(tmp_path)
    await withdraw(TOKEN, TOKEN.amount("1"), recipient=DESTINO)

    detalles = " ".join(prompt.asked[0].details)
    assert "irreversible" in detalles
    assert DESTINO in detalles


async def test_la_confirmacion_se_registra_como_aprobada(tmp_path: Path) -> None:
    """La traza de auditoría tiene que poder distinguir quién autorizó qué."""
    from amigocompora.app.confirmation import ConfirmationGateway

    clock = FrozenClock(AHORA)
    ledger = ExecutionLedger(tmp_path / "executions.jsonl", clock=clock)
    keys = FakeKey()
    policy = AutonomyPolicy(
        keys=keys,
        passphrase=_FakePassphrase(),
        limits=_limits(),
        ledger=ledger,
        clock=clock,
        trigger=TriggerKind.MANUAL,
    )
    prompt = RecordingPrompt(answer=True)
    gateway = ConfirmationGateway(ModeGuard(OperationMode.EXECUTION), prompt=prompt, clock=clock)
    node = _node()
    withdraw = WithdrawFunds(
        gateway=gateway,
        keys=keys,
        policy=policy,
        broadcasters={"base": _broadcaster(node)},
    )
    await withdraw(TOKEN, TOKEN.amount("1"), recipient=DESTINO)

    assert [r.decision for r in gateway.history][-1] is Decision.APPROVED
    assert gateway.history[-1].action.capability is Capability.BROADCAST_TX


# --------------------------------------------------------------------------- #
# El intento, por separado
# --------------------------------------------------------------------------- #
def test_el_intento_rechaza_un_destinatario_vacio() -> None:
    from amigocompora.domain.models import WithdrawIntent

    with pytest.raises(InvalidAmountError):
        WithdrawIntent(
            chain="base", token=TOKEN, amount=TOKEN.amount("1"),
            recipient="", source=DIRECCION, engine_id="wallet",
        )


def test_el_intento_rechaza_un_importe_cero() -> None:
    from amigocompora.domain.models import WithdrawIntent

    with pytest.raises(InvalidAmountError):
        WithdrawIntent(
            chain="base", token=TOKEN, amount=TokenAmount(0, 6, "USDC"),
            recipient=DESTINO, source=DIRECCION, engine_id="wallet",
        )


def test_el_intento_rechaza_un_importe_de_otro_token() -> None:
    """`TokenAmount` sólo lleva símbolo y escala, así que se comprueba aquí."""
    from amigocompora.domain.errors import CurrencyMismatchError
    from amigocompora.domain.models import WithdrawIntent

    with pytest.raises(CurrencyMismatchError):
        WithdrawIntent(
            chain="base", token=TOKEN, amount=TokenAmount(10**6, 18, "WETH"),
            recipient=DESTINO, source=DIRECCION, engine_id="wallet",
        )


def test_el_intento_rechaza_un_token_de_otra_red() -> None:
    from amigocompora.domain.errors import CurrencyMismatchError
    from amigocompora.domain.models import WithdrawIntent

    otro = Token(symbol="USDC", decimals=6, chain="polygon", address=USDC)
    with pytest.raises(CurrencyMismatchError):
        WithdrawIntent(
            chain="base", token=otro, amount=TokenAmount(10**6, 6, "USDC"),
            recipient=DESTINO, source=DIRECCION, engine_id="wallet",
        )


def test_el_intento_expone_lo_que_la_politica_necesita() -> None:
    """Los miembros de `ExecutableIntent`, uno a uno y con su valor.

    Se comprueban explícitamente y no con `isinstance` porque el protocolo es
    estructural: lo que importa es que cada propiedad devuelva lo que la política
    espera, no que la clase diga que lo implementa.
    """
    from amigocompora.domain.models import WithdrawIntent

    valorado = TokenAmount(1_800 * 10**6, 6, "USDC")
    intent = WithdrawIntent(
        chain="base", token=TOKEN, amount=TOKEN.amount("10"),
        recipient=DESTINO, source=DIRECCION, engine_id="wallet",
        reference_value=valorado,
    )
    assert intent.chain == "base"
    assert intent.tokens == ("USDC",)
    assert intent.engine_id == "wallet"
    assert intent.recipient == DESTINO
    assert intent.notional == TOKEN.amount("10")
    assert intent.measured == valorado
    assert intent.reference_value == valorado
    assert intent.counts_towards_limits is True
    assert "Retirada" in intent.label
    assert DESTINO in intent.label


def test_el_intento_sin_valoracion_mide_lo_entregado() -> None:
    """Cuando el token es la referencia, lo medido y lo entregado coinciden."""
    from amigocompora.domain.models import WithdrawIntent

    intent = WithdrawIntent(
        chain="base", token=TOKEN, amount=TOKEN.amount("10"),
        recipient=DESTINO, source=DIRECCION, engine_id="wallet",
    )
    assert intent.measured == intent.notional
    assert intent.reference_value is None


def test_el_intento_se_describe_para_el_dialogo() -> None:
    """`describe` es lo que se enseña antes de firmar, así que dice las dos direcciones."""
    from amigocompora.domain.models import WithdrawIntent

    intent = WithdrawIntent(
        chain="base", token=TOKEN, amount=TOKEN.amount("10"),
        recipient=DESTINO, source=DIRECCION, engine_id="wallet",
    )
    texto = dict(intent.describe())
    assert texto["Sale de"] == DIRECCION
    assert texto["Va a"] == DESTINO
    assert texto["Red"] == "base"


def test_el_intento_rechaza_una_valoracion_negativa() -> None:
    from amigocompora.domain.models import WithdrawIntent

    with pytest.raises(InvalidAmountError):
        WithdrawIntent(
            chain="base", token=TOKEN, amount=TOKEN.amount("10"),
            recipient=DESTINO, source=DIRECCION, engine_id="wallet",
            reference_value=TokenAmount(0, 6, "USDC"),
        )
