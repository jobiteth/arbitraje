"""El coste de red: una estimación que no se puede confundir con un cobro.

Lo que se fija aquí son las cuatro cosas que sostienen la cifra que el usuario
va a leer justo antes de preparar un swap:

1. **El coste es el producto del límite por la tarifa máxima** —la tarifa, no la
   propina—, acuñado en la moneda nativa de la red que corresponde. La fórmula
   la pone la política de gas, que ya tiene sus pruebas; lo que se afirma aquí
   es que este camino usa **esas** dos cifras y no otras.
2. **El límite sale de la red** cuando el payload no trae uno —los motores
   directos no lo traen—, con el margen de `resolve_gas_limit`, y se pregunta
   **desde la cartera que firmaría**: el gas depende de quién envía, porque un
   swap sin allowance revierte y el nodo lo dice.
3. **Sin nodo para la red no se estima**, y se dice por qué: la interfaz enseña
   «no se pudo estimar», nunca un cero que se leería como «gratis».
4. **Si la red dice que la transacción revertiría, el error se propaga.** Quien
   prepara decide qué hacer con eso —enseñarlo sin bloquear—, pero el hecho no
   se entierra.

Se usa un `EvmBroadcaster` **real** sobre un nodo de mentira: la multiplicación
y el envoltorio son de este módulo, pero el límite, la tarifa y el error de
revert tienen que venir del mismo objeto que los produce al firmar.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from amigocompora.app.usecases.estimate_cost import EstimateNetworkCost
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.models import UnsignedTransaction
from amigocompora.domain.money import TokenAmount
from amigocompora.engines.catalog import native_token
from amigocompora.infra.evm.broadcast import EvmBroadcaster
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool

AHORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
CHAIN_ID = 8453
#: Cuenta 0 de Hardhat: publicada y sin fondos en ninguna red real. Sólo hace
#: falta como dirección `from` de la estimación; aquí no se firma nada.
CARTERA = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"

#: La base fee y la propina que contestará el nodo, en wei: 1 gwei cada una.
#: Con ellas, la política por omisión —`2 · base + tip`, factor 1,2— da
#: 3,6 gwei de techo, una cifra que se escribe a mano y no se deduce del código.
BASE_FEE = 10**9
PROPINA = 10**9
TECHO_ESPERADO = 3_600_000_000


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
                # Un método no previsto se contesta como error en vez de con un
                # valor inventado: si el camino llama a algo que la prueba no
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


def _node(**overrides: Any) -> FakeNode:
    handlers: dict[str, Any] = {
        "eth_chainId": hex(CHAIN_ID),
        "eth_estimateGas": "0x30d40",  # 200 000
        "eth_getBlockByNumber": {"baseFeePerGas": hex(BASE_FEE)},
        "eth_maxPriorityFeePerGas": hex(PROPINA),
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
    return EvmBroadcaster(pool, chain_key, clock=FrozenClock(AHORA))


def _unsigned(*, gas_limit: int | None = None) -> UnsignedTransaction:
    """El payload de un swap: sin `gas_limit`, que es lo que pasan los motores."""
    return UnsignedTransaction(
        chain_id=CHAIN_ID,
        to_address=ROUTER,
        calldata="0xdeadbeef",
        value=TokenAmount(0, 18, "ETH"),
        description="swap de prueba",
        gas_limit=gas_limit,
    )


def _estimador(node: FakeNode, chain_key: str = "base") -> EstimateNetworkCost:
    return EstimateNetworkCost(broadcasters={chain_key: _broadcaster(node, chain_key)})


# --------------------------------------------------------------------------- #
# 1. La cifra: techo de la política por el límite, en la moneda nativa
# --------------------------------------------------------------------------- #
async def test_el_coste_es_el_limite_por_la_tarifa_maxima_de_la_politica() -> None:
    """Con el nodo dado, el techo es 3,6 gwei: la tarifa **máxima**, no la propina.

    Se escriben a mano los dos factores —200 000 de gas, 3,6 gwei de techo— y no
    se derivan del módulo, porque una prueba que recalcula lo que el código hace
    no comprueba que la cifra sea la que dice ser.
    """
    estimador = _estimador(_node())

    coste = await estimador(_unsigned(), "base", from_address=CARTERA)

    assert coste.gas_limit == 250_000  # 200 000 medidos, con el margen 1,25
    assert coste.gas_price_wei == TECHO_ESPERADO
    assert coste.native.raw == 250_000 * TECHO_ESPERADO
    # Y la moneda es la nativa de la red, del registro, no del payload.
    nativo = native_token("base")
    assert (coste.native.symbol, coste.native.decimals) == (nativo.symbol, nativo.decimals)
    # El origen del límite viaja en el resultado: la interfaz enseña de dónde
    # salió la cifra, y «medido por la red» y «declarado por el motor» no son
    # la misma confianza.
    assert "eth_estimateGas" in coste.source


async def test_se_pregunta_desde_la_cartera_que_firmaria() -> None:
    """`from` es la cartera, no el router: el gas depende de quién envía.

    Sin allowance la transacción revierte y la estimación falla; estimar desde
    otra dirección daría una cifra de otra transacción, no la de ésta.
    """
    node = _node()
    await _estimador(node)(_unsigned(), "base", from_address=CARTERA)

    (llamada,) = node.called("eth_estimateGas")
    assert llamada[0]["from"] == CARTERA
    assert llamada[0]["to"] == ROUTER


async def test_el_margen_no_encoge_lo_que_el_payload_ya_traia() -> None:
    """Si el motor declaró un límite mayor que la estimación, manda el mayor."""
    node = _node(eth_estimateGas="0x186a0")  # 100 000, con margen 125 000
    coste = await _estimador(node)(
        _unsigned(gas_limit=300_000), "base", from_address=CARTERA
    )

    assert coste.gas_limit == 300_000
    assert "payload" in coste.source


# --------------------------------------------------------------------------- #
# 2. Los fallos: no se estima, y se dice por qué
# --------------------------------------------------------------------------- #
async def test_sin_nodo_para_la_red_no_se_estima_y_se_dice_por_que() -> None:
    """Un cero se leería como «gratis»: lo correcto es un error con salida."""
    estimador = EstimateNetworkCost(broadcasters={})

    with pytest.raises(ExecutionError) as excinfo:
        await estimador(_unsigned(), "base", from_address=CARTERA)

    mensaje = str(excinfo.value)
    assert "base" in mensaje
    assert "endpoint" in mensaje


async def test_si_la_red_dice_que_revertiria_el_error_se_propaga() -> None:
    """El revert no se traga: quien prepara decide, pero la verdad no se entierra."""
    revertida = _RpcError("execution reverted: ERC20: transfer amount exceeds allowance")
    node = _node(eth_estimateGas=revertida)

    with pytest.raises(ExecutionError) as excinfo:
        await _estimador(node)(_unsigned(), "base", from_address=CARTERA)

    assert "revertiría" in str(excinfo.value)


async def test_sin_limite_estimable_ni_declarado_tampoco_hay_cifra() -> None:
    """Nodo caído y payload sin `gas_limit`: no hay nada que multiplicar."""
    node = _node(eth_estimateGas=RuntimeError("caído"))

    with pytest.raises(ExecutionError):
        await _estimador(node)(_unsigned(), "base", from_address=CARTERA)
