"""Tests de `EvmBroadcaster`.

Este módulo decide si se mueve dinero, así que lo que se fija aquí no es que
«devuelva datos» sino los casos en que **podría perderlo**. Son cuatro, y los
cuatro se pueden provocar sin tocar la red:

1. **Un envío que sí entró leído como fallo.** El nodo contesta `already known`
   cuando ya tiene esa transacción —lo normal al reintentar el mismo `raw` tras
   perderse una respuesta—. Leerlo como error lleva a firmar otra vez, y con otro
   nonce eso sería una segunda operación idéntica: un gasto doble.
2. **Firmar algo que va a revertir.** `eth_estimateGas` que contesta un revert y
   `eth_estimateGas` que no contesta son cosas distintas: la primera significa
   que la operación no va a ocurrir y firmarla es quemar gas.
3. **Emitir a un destino que no es el esperado.** Es la última puerta antes de
   una firma irreversible, y tiene que cerrarse **antes** de enviar nada.
4. **Emitir para una red en la que no se está.** Una URL de RPC equivocada es un
   error de un carácter.

Los selectores y la codificación se prueban contra los valores estándar de
ERC-20, que son públicos y comprobables: si el `approve` codificara mal el
`spender`, autorizaría a otro contrato y eso no se nota hasta que alguien vacía
la posición.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    BroadcastError,
    ExecutionError,
    InvalidAmountError,
    UnsupportedOperationError,
)
from amigocompora.domain.execution import GasPolicy, GasStrategy
from amigocompora.domain.models import (
    BroadcastStatus,
    SignedEvmTransaction,
    UnsignedTransaction,
)
from amigocompora.domain.money import TokenAmount
from amigocompora.infra.evm.broadcast import (
    SELECTOR_ALLOWANCE,
    SELECTOR_APPROVE,
    EvmBroadcaster,
    build_allowance_calldata,
    build_approve_calldata,
    build_permit2_allowance_calldata,
    require_evm,
)
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool

AHORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
WALLET = "0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ROUTER = "0x4f6f91599858bf0d19fabcf2c5d591fe13f7c059"
OTRO_ROUTER = "0x1111111111111111111111111111111111111111"
#: La dirección canónica de Permit2, igual en todas las redes.
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
HASH = "0x" + "ab" * 32
CHAIN_ID = 8453


# --------------------------------------------------------------------------- #
# Nodo de mentira: despacha por método y anota lo que se le pidió
# --------------------------------------------------------------------------- #
Resultado = Any | Exception | Callable[[list[Any]], Any]


class FakeNode:
    """Un nodo JSON-RPC que responde lo que se le diga y recuerda lo que le pidieron."""

    def __init__(self, **handlers: Resultado) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, list[Any]]] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = str(body["method"])
            params = list(body.get("params", []))
            self.calls.append((method, params))
            if method not in self.handlers:
                # Un método no previsto se contesta como error en vez de con un
                # valor inventado: si el módulo llama a algo que esta prueba no
                # esperaba, tiene que notarse.
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


class _RpcError(Exception):
    """Marca un handler como «el nodo contesta un error JSON-RPC»."""


def _node(**handlers: Resultado) -> FakeNode:
    return FakeNode(**handlers)


def _broadcaster(node: FakeNode, **kwargs: Any) -> EvmBroadcaster:
    pool = RpcPool(
        "base",
        (RpcEndpoint(url="https://base.example.org", label="falso", priority=10),),
        httpx.AsyncClient(transport=node.transport()),
        clock=FrozenClock(AHORA),
    )
    kwargs.setdefault("clock", FrozenClock(AHORA))
    # Sin espera real entre sondeos: el recibo se prueba por lógica, no por reloj.
    kwargs.setdefault("receipt_poll_interval_seconds", 0.0)
    kwargs.setdefault("receipt_poll_attempts", 3)
    return EvmBroadcaster(pool, "base", **kwargs)


def _unsigned(*, gas_limit: int | None = 200_000) -> UnsignedTransaction:
    return UnsignedTransaction(
        chain_id=CHAIN_ID,
        to_address=ROUTER,
        calldata="0xdeadbeef",
        value=TokenAmount(0, 18, "ETH"),
        description="swap de prueba",
        gas_limit=gas_limit,
    )


def _signed(*, tx_hash: str = HASH, to_address: str = ROUTER) -> SignedEvmTransaction:
    return SignedEvmTransaction(
        raw_hex="0x02f8" + "00" * 20,
        tx_hash=tx_hash,
        from_address=WALLET,
        chain_id=CHAIN_ID,
        nonce=7,
        to_address=to_address,
        value=TokenAmount(0, 18, "ETH"),
        gas_limit=200_000,
        max_fee_per_gas=2_000_000_000,
        max_priority_fee_per_gas=1_000_000,
        calldata="0xdeadbeef",
        description="swap de prueba",
    )


# --------------------------------------------------------------------------- #
# 1. Codificación de llamadas ERC-20 — contra los selectores públicos
# --------------------------------------------------------------------------- #
def test_the_selectors_are_the_standard_erc20_ones() -> None:
    """Si esto falla, el `approve` autoriza a un contrato distinto del que se cree."""
    assert SELECTOR_ALLOWANCE == "0xdd62ed3e"
    assert SELECTOR_APPROVE == "0x095ea7b3"


def test_allowance_calldata_pads_both_addresses_to_32_bytes() -> None:
    esperado = (
        "0xdd62ed3e"
        + "0" * 24
        + WALLET[2:].lower()
        + "0" * 24
        + USDC[2:].lower()
    )
    assert build_allowance_calldata(WALLET, USDC) == esperado


def test_approve_carries_the_exact_amount_and_never_the_maximum() -> None:
    """Aprobar el máximo deja al router autorizado a vaciar ese token para siempre."""
    importe = 1_500_000
    calldata = build_approve_calldata(ROUTER, importe)
    assert calldata.startswith("0x095ea7b3")
    assert calldata[10:74] == "0" * 24 + ROUTER[2:].lower()
    assert calldata[74:] == f"{importe:064x}"
    # El máximo de `uint256` es lo que NO debe aparecer.
    assert calldata[74:] != "f" * 64


def test_a_malformed_address_is_rejected_even_at_the_right_length() -> None:
    """40 caracteres no son una dirección: `zz…` tiene la longitud y no es hex."""
    with pytest.raises(InvalidAmountError):
        build_approve_calldata("0x" + "zz" * 20, 1)
    with pytest.raises(InvalidAmountError):
        build_approve_calldata("0x1234", 1)


def test_an_amount_too_large_for_a_word_is_rejected() -> None:
    with pytest.raises(InvalidAmountError):
        build_approve_calldata(ROUTER, 2**256)
    with pytest.raises(InvalidAmountError):
        build_approve_calldata(ROUTER, -1)


# --------------------------------------------------------------------------- #
# 2. Estado de red: nonce y comisiones
# --------------------------------------------------------------------------- #
async def test_the_nonce_is_read_as_pending_not_latest() -> None:
    """Con `latest` dos operaciones seguidas salen con el mismo nonce."""
    node = _node(eth_getTransactionCount="0x7")
    emisor = _broadcaster(node)
    assert await emisor.pending_nonce(WALLET) == 7
    assert node.called("eth_getTransactionCount") == [[WALLET, "pending"]]


async def test_a_node_that_answers_0x_for_an_empty_account_is_read_as_zero() -> None:
    """Medido: hay nodos que devuelven `"0x"` y `int("0x", 16)` lanza."""
    node = _node(eth_getTransactionCount="0x")
    assert await _broadcaster(node).pending_nonce(WALLET) == 0


async def test_the_fee_cap_uses_the_network_and_the_configured_multiplier() -> None:
    node = _node(
        eth_getBlockByNumber={"baseFeePerGas": "0x3b9aca00"},  # 1 gwei
        eth_maxPriorityFeePerGas="0x5f5e100",  # 0,1 gwei
    )
    emisor = _broadcaster(node, gas_policy=GasPolicy(multiplier=Decimal("1.0")))
    tarifas = await emisor.read_fees()
    assert tarifas.base_fee_per_gas == 1_000_000_000
    assert tarifas.observed_tip == 100_000_000
    assert tarifas.tip_source == "maxPriorityFeePerGas"
    # 2 · base + tip, sin escalar.
    assert tarifas.max_fee_per_gas == 2_100_000_000
    assert tarifas.max_priority_fee_per_gas == 100_000_000


async def test_without_max_priority_fee_the_tip_comes_from_the_gas_price() -> None:
    """Hay redes que no implementan `eth_maxPriorityFeePerGas`; no puede detenerse."""
    node = _node(
        eth_getBlockByNumber={"baseFeePerGas": "0x3b9aca00"},
        eth_maxPriorityFeePerGas=_RpcError("method not found"),
        eth_gasPrice=hex(3_000_000_000),  # 3 gwei
    )
    tarifas = await _broadcaster(node).read_fees()
    assert tarifas.tip_source == "gasPrice"
    assert tarifas.observed_tip == 2_000_000_000  # 3 gwei menos 1 gwei


async def test_without_any_readable_tip_a_floor_is_used_instead_of_failing() -> None:
    """Una transacción descartada por propina baja es peor que una cara."""
    node = _node(
        eth_getBlockByNumber={"baseFeePerGas": "0x3b9aca00"},
        eth_maxPriorityFeePerGas=_RpcError("method not found"),
        eth_gasPrice=_RpcError("boom"),
    )
    tarifas = await _broadcaster(node).read_fees()
    assert tarifas.observed_tip == 1_000_000
    assert tarifas.tip_source == "mínimo de seguridad"


async def test_a_chain_without_base_fee_is_refused_explicitly() -> None:
    """Firmar EIP-1559 en una red pre-London exige otra implementación."""
    node = _node(eth_getBlockByNumber={"number": "0x1"})
    with pytest.raises(UnsupportedOperationError) as caught:
        await _broadcaster(node).read_fees()
    assert "EIP-1559" in str(caught.value)


async def test_a_fixed_gas_policy_ignores_the_network() -> None:
    node = _node(
        eth_getBlockByNumber={"baseFeePerGas": "0x3b9aca00"},
        eth_maxPriorityFeePerGas="0x5f5e100",
    )
    politica = GasPolicy(
        strategy=GasStrategy.FIXED,
        fixed_max_fee_per_gas=9_000_000_000,
        fixed_max_priority_fee_per_gas=500_000_000,
    )
    tarifas = await _broadcaster(node, gas_policy=politica).read_fees()
    assert tarifas.max_fee_per_gas == 9_000_000_000
    assert tarifas.max_priority_fee_per_gas == 500_000_000


# --------------------------------------------------------------------------- #
# 3. Gas: un revert no es lo mismo que un nodo que no contesta
# --------------------------------------------------------------------------- #
async def test_a_revert_from_the_node_stops_the_signature() -> None:
    """Firmar algo que revierte es pagar gas por una operación que no ocurrirá."""
    node = _node(eth_estimateGas=_RpcError("execution reverted: INSUFFICIENT_OUTPUT_AMOUNT"))
    with pytest.raises(ExecutionError) as caught:
        await _broadcaster(node).resolve_gas_limit(_unsigned(), from_address=WALLET)
    assert "revertiría" in str(caught.value)
    assert "INSUFFICIENT_OUTPUT_AMOUNT" in str(caught.value)


async def test_without_a_reachable_node_the_payload_limit_is_used() -> None:
    """«No se pudo preguntar» no es «la operación no vale»."""
    node = _node(eth_estimateGas=RuntimeError("caído"))
    limite, origen = await _broadcaster(node).resolve_gas_limit(
        _unsigned(gas_limit=250_000), from_address=WALLET
    )
    assert limite == 250_000
    assert "payload" in origen


async def test_without_a_node_and_without_a_payload_limit_it_fails_loudly() -> None:
    node = _node(eth_estimateGas=RuntimeError("caído"))
    with pytest.raises(ExecutionError) as caught:
        await _broadcaster(node).resolve_gas_limit(_unsigned(gas_limit=None), from_address=WALLET)
    assert "no traía límite" in str(caught.value)


async def test_the_estimate_gets_a_margin_and_never_undercuts_the_payload() -> None:
    node = _node(eth_estimateGas="0x186a0")  # 100 000
    limite, origen = await _broadcaster(node).resolve_gas_limit(
        _unsigned(gas_limit=None), from_address=WALLET
    )
    assert limite == 125_000  # 100 000 · 1,25
    assert "margen" in origen

    # Si el motor ya había calculado más, gana el del motor.
    node2 = _node(eth_estimateGas="0x186a0")
    limite2, origen2 = await _broadcaster(node2).resolve_gas_limit(
        _unsigned(gas_limit=300_000), from_address=WALLET
    )
    assert limite2 == 300_000
    assert "payload" in origen2


# --------------------------------------------------------------------------- #
# 4. La última puerta: destino y red, antes de emitir
# --------------------------------------------------------------------------- #
async def test_a_destination_that_is_not_the_measured_one_never_gets_broadcast() -> None:
    node = _node(eth_chainId="0x2105", eth_sendRawTransaction=HASH)
    emisor = _broadcaster(node)
    with pytest.raises(ExecutionError) as caught:
        await emisor.send(_signed(to_address=OTRO_ROUTER), expected_to=ROUTER)
    assert "no es el esperado" in str(caught.value) or "contrato medido" in str(caught.value)
    # Y lo que importa: no se llegó a enviar nada.
    assert node.called("eth_sendRawTransaction") == []


async def test_broadcasting_for_another_chain_is_refused() -> None:
    """Una URL de RPC apuntando a otra red es un error de un solo carácter."""
    node = _node(eth_chainId="0x1", eth_sendRawTransaction=HASH)
    with pytest.raises(ExecutionError) as caught:
        await _broadcaster(node).send(_signed(), expected_to=ROUTER)
    assert "8453" in str(caught.value)
    assert node.called("eth_sendRawTransaction") == []


async def test_an_unchecked_destination_is_allowed_but_never_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sin destino declarado se emite, pero queda registrado que no se comprobó."""
    node = _node(
        eth_chainId="0x2105",
        eth_sendRawTransaction=HASH,
        eth_getTransactionReceipt={"status": "0x1", "blockNumber": "0x10"},
    )
    recibo = await _broadcaster(node).send(_signed(), expected_to=None)
    assert recibo.status is BroadcastStatus.SUCCESS

    from structlog.testing import capture_logs

    node2 = _node(
        eth_chainId="0x2105",
        eth_sendRawTransaction=HASH,
        eth_getTransactionReceipt={"status": "0x1", "blockNumber": "0x10"},
    )
    with capture_logs() as capturado:
        await _broadcaster(node2).send(_signed(), expected_to=None)
    eventos = {entrada["event"] for entrada in capturado}
    assert "evm.destination_unchecked" in eventos


# --------------------------------------------------------------------------- #
# 5. Emitir: el gasto doble y los recibos
# --------------------------------------------------------------------------- #
async def test_already_known_is_read_as_accepted_not_as_a_failure() -> None:
    """El fallo que más caro sale: creer que no se envió y firmar otra vez.

    Con un nonce nuevo, esa segunda firma sería una operación idéntica más: el
    usuario acabaría comprando dos veces creyendo que no compró ninguna.
    """
    node = _node(
        eth_chainId="0x2105",
        eth_sendRawTransaction=_RpcError("already known"),
        eth_getTransactionReceipt={"status": "0x1", "blockNumber": "0x10"},
    )
    recibo = await _broadcaster(node).send(_signed(), expected_to=ROUTER)
    assert recibo.status is BroadcastStatus.SUCCESS
    assert recibo.tx_hash == HASH


async def test_another_rejection_is_a_broadcast_error_that_keeps_the_hash() -> None:
    """Un rechazo de verdad tiene que decir que la transacción existe y se puede reintentar."""
    node = _node(
        eth_chainId="0x2105",
        eth_sendRawTransaction=_RpcError("insufficient funds for gas * price + value"),
    )
    with pytest.raises(BroadcastError) as caught:
        await _broadcaster(node).send(_signed(), expected_to=ROUTER)
    assert caught.value.tx_hash == HASH
    assert "insufficient funds" in str(caught.value)


async def test_a_lost_response_leaves_the_operation_unknown_not_failed() -> None:
    """La petición pudo llegar y perderse la respuesta: no se sabe, y se dice."""
    node = _node(eth_chainId="0x2105", eth_sendRawTransaction=RuntimeError("timeout"))
    recibo = await _broadcaster(node).send(_signed(), expected_to=ROUTER)
    assert recibo.status is BroadcastStatus.UNKNOWN
    assert recibo.tx_hash == HASH
    assert recibo.is_proof is False
    assert "puede estar viva" in recibo.reason


async def test_a_hash_from_the_node_that_is_not_ours_stops_everything() -> None:
    node = _node(eth_chainId="0x2105", eth_sendRawTransaction="0x" + "cd" * 32)
    with pytest.raises(ExecutionError) as caught:
        await _broadcaster(node).send(_signed(), expected_to=ROUTER)
    assert "No se puede asegurar" in str(caught.value)


async def test_a_reverted_receipt_says_so_and_carries_the_block() -> None:
    node = _node(
        eth_chainId="0x2105",
        eth_sendRawTransaction=HASH,
        eth_getTransactionReceipt={"status": "0x0", "blockNumber": "0x1a"},
    )
    recibo = await _broadcaster(node).send(_signed(), expected_to=ROUTER)
    assert recibo.status is BroadcastStatus.REVERTED
    assert recibo.block_number == 26
    assert recibo.is_proof is True  # existe y es pública, aunque revirtiera


async def test_an_unmined_transaction_stays_pending_and_is_still_proof() -> None:
    """No se espera a que mine: en una red congestionada eso son minutos."""
    node = _node(
        eth_chainId="0x2105",
        eth_sendRawTransaction=HASH,
        eth_getTransactionReceipt=None,
    )
    recibo = await _broadcaster(node).send(_signed(), expected_to=ROUTER)
    assert recibo.status is BroadcastStatus.PENDING
    assert recibo.is_proof is True
    # Se sondeó el número de veces pedido, ni una más.
    assert len(node.called("eth_getTransactionReceipt")) == 3


async def test_a_receipt_without_status_is_unknown_and_not_a_success() -> None:
    """Decir «salió bien» sin saberlo es justo lo que un recibo no debe hacer."""
    node = _node(
        eth_chainId="0x2105",
        eth_sendRawTransaction=HASH,
        eth_getTransactionReceipt={"blockNumber": "0x1a"},
    )
    recibo = await _broadcaster(node).send(_signed(), expected_to=ROUTER)
    assert recibo.status is BroadcastStatus.UNKNOWN
    assert recibo.block_number == 26
    assert "no se sabe" in recibo.reason


async def test_a_receipt_with_an_unreadable_status_fails_loudly() -> None:
    node = _node(
        eth_chainId="0x2105",
        eth_sendRawTransaction=HASH,
        eth_getTransactionReceipt={"status": "0x2", "blockNumber": "0x1a"},
    )
    with pytest.raises(ExecutionError):
        await _broadcaster(node).send(_signed(), expected_to=ROUTER)


# --------------------------------------------------------------------------- #
# 6. Guardas de red y de encuadre
# --------------------------------------------------------------------------- #
def test_require_evm_cuts_on_a_non_evm_chain() -> None:
    from amigocompora.domain.chains import chain

    require_evm(chain("base"))  # no lanza
    with pytest.raises(UnsupportedOperationError) as caught:
        require_evm(chain("solana"))
    assert "solders" in str(caught.value)


async def test_the_allowance_call_encodes_owner_and_spender_in_that_order() -> None:
    """Invertirlos devolvería un allowance de otro par y aprobaría de más."""
    node = _node(eth_call="0x" + f"{1234:064x}")
    emisor = _broadcaster(node)
    assert await emisor.read_allowance(USDC, WALLET, ROUTER) == 1234
    (params,) = node.called("eth_call")
    llamada = params[0]
    assert llamada["to"] == USDC
    assert llamada["data"] == build_allowance_calldata(WALLET, ROUTER)
    assert params[1] == "latest"


# --------------------------------------------------------------------------- #
# 7. El permiso de Permit2: dos palabras, y la que trae letras
# --------------------------------------------------------------------------- #
async def test_permit2_allowance_reads_the_amount_and_the_expiry_from_their_words() -> None:
    """La primera palabra es el importe y la segunda la caducidad.

    El importe se elige a propósito con **letras hexadecimales** —`0x…0c573b`—
    porque es el caso que devolvió la cadena en la primera venta real, y el que
    dejó a la aplicación sin poder vender ningún ERC-20. Con una palabra hecha
    sólo de dígitos la lectura pasaba, y pasaba por casualidad: sin el prefijo
    `0x`, `_parse_quantity` la interpretaba como decimal sin quejarse. Elegir un
    ejemplo cómodo aquí es lo que dejaría volver el fallo.
    """
    importe = 0x0C573B
    caducidad = 0x68E5A2C0
    node = _node(eth_call="0x" + f"{importe:064x}" + f"{caducidad:064x}")
    emisor = _broadcaster(node)

    permiso = await emisor.read_permit2_allowance(PERMIT2, USDC, WALLET, ROUTER)

    assert permiso.amount_raw == importe
    assert permiso.expiration == caducidad


async def test_permit2_allowance_asks_permit2_with_owner_token_and_spender() -> None:
    """El orden `(owner, token, spender)` es el del contrato: invertirlo leería
    el permiso de otro par y daría por bueno un swap que iba a revertir."""
    node = _node(eth_call="0x" + f"{0:064x}" * 2)
    emisor = _broadcaster(node)

    await emisor.read_permit2_allowance(PERMIT2, USDC, WALLET, ROUTER)

    (params,) = node.called("eth_call")
    assert params[0]["to"] == PERMIT2
    assert params[0]["data"] == build_permit2_allowance_calldata(WALLET, USDC, ROUTER)


async def test_a_short_return_cuts_instead_of_reading_a_zero() -> None:
    """Un retorno de una sola palabra donde hacen falta dos se rechaza.

    Rellenar con ceros sería leer «permiso caducado en el epoch» como si fuera un
    hecho, y eso es una decisión distinta de «no se pudo preguntar»."""
    node = _node(eth_call="0x" + f"{1:064x}")
    emisor = _broadcaster(node)

    with pytest.raises(ExecutionError) as caught:
        await emisor.read_permit2_allowance(PERMIT2, USDC, WALLET, ROUTER)

    assert "no es el que se cree" in str(caught.value)
