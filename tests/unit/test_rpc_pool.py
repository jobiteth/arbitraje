"""Tests de `RpcPool`.

El pool no tenía ninguna prueba, y es el componente que habla con la red: lo que
se fija aquí no es «devuelve datos», sino las cuatro decisiones que lo hacen
seguro y que se rompen en silencio.

1. **La URL nunca se registra.** En un RPC comercial la clave de API viaja dentro
   de la URL. Esto no es hipotético: se midió. Con el procesador de logs de
   `infra.logging` sin instalar —basta con usar el pool desde un script que no
   llame a `configure_logging`— un `endpoint=endpoint.url` escribió la clave
   entera en la consola. La prueba no depende de que el procesador esté puesto.
2. **Un error JSON-RPC no hace failover *cuando es una respuesta*.** El nodo está
   sano y dice que la petición es inválida; repetirla en cinco nodos da cinco
   veces lo mismo, gasta cuota y marca como caídos nodos que funcionan. Pero el
   objeto `error` no basta para saberlo —ver el punto 3—, y creer que sí dejaba a
   Ethereum sin servicio.
3. **Un error JSON-RPC *sí* hace failover cuando el nodo no puede atender la
   petición**, y entonces el cortacircuitos acaba apartándolo. Los tres casos
   están medidos contra los respaldos públicos reales.
4. **Un fallo de transporte sí hace failover**, y el cortacircuitos acaba
   apartando al nodo que falla, de modo que un nodo preferido pero roto deja de
   tener prioridad sobre uno sano.
5. **El error final dice qué se intentó**, sin filtrar ninguna URL.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from structlog.testing import capture_logs

from amigocompora.domain.clock import FrozenClock
from amigocompora.infra.rpc.pool import (
    AllEndpointsFailedError,
    RpcEndpoint,
    RpcError,
    RpcPool,
    _is_endpoint_fault,
)

#: Clave de API de mentira con la forma real: va dentro de la ruta de la URL.
CLAVE = "SUPERSECRETKEY123"
URL_CON_CLAVE = f"https://base-mainnet.infura.io/v3/{CLAVE}"
URL_PUBLICA = "https://base-rpc.publicnode.com"

Handler = Callable[[httpx.Request], httpx.Response]


def _jsonrpc_ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _pool(
    handler: Handler,
    endpoints: tuple[RpcEndpoint, ...],
    *,
    clock: FrozenClock | None = None,
    failure_threshold: int = 2,
) -> RpcPool:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RpcPool(
        "base",
        endpoints,
        client,
        clock=clock or FrozenClock(datetime(2026, 10, 6, tzinfo=UTC)),
        failure_threshold=failure_threshold,
        cooldown_seconds=30.0,
    )


# --------------------------------------------------------------------------- #
# 1. La clave de API no puede acabar en un log
# --------------------------------------------------------------------------- #
async def test_a_failing_endpoint_never_logs_its_url() -> None:
    """La regresión que motivó esta prueba, y por qué no vale confiar en el redactor.

    `infra.logging` redacta los campos llamados `url` o `endpoint`, pero eso sólo
    protege si el procesador está instalado. Se comprobó que no lo estaba cuando
    se llamó al pool desde un script suelto, y la clave salió entera por consola.
    Por eso el pool registra `display_name` —la etiqueta del nodo— y esta prueba
    lo afirma sobre lo que el código *pasa al logger*, con la captura de
    structlog, que no depende de ninguna configuración previa.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    pool = _pool(handler, (RpcEndpoint(url=URL_CON_CLAVE, label="infura", priority=10),))
    with capture_logs() as captured, pytest.raises(AllEndpointsFailedError) as caught:
        await pool.call("eth_chainId")

    volcado = json.dumps(captured, default=str)
    assert CLAVE not in volcado
    # Y sigue diciendo cuál falló: redactar no puede volver el log inútil.
    assert "infura" in volcado
    # El error que ve el usuario tampoco la lleva.
    assert CLAVE not in str(caught.value)
    assert "infura" in str(caught.value)


async def test_an_endpoint_without_a_label_still_hides_the_path() -> None:
    """Sin etiqueta se cae al host, no a la URL: la ruta es donde va la clave."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    pool = _pool(handler, (RpcEndpoint(url=URL_CON_CLAVE, priority=10),))
    with capture_logs() as captured, pytest.raises(AllEndpointsFailedError):
        await pool.call("eth_chainId")

    assert CLAVE not in json.dumps(captured, default=str)
    assert "base-mainnet.infura.io" in json.dumps(captured, default=str)


def test_the_repr_of_an_endpoint_never_carries_the_key() -> None:
    """Lo que aparece en una traza, en un `print` o al registrar el contenedor."""
    endpoint = RpcEndpoint(url=URL_CON_CLAVE, label="infura", priority=10)
    assert CLAVE not in repr(endpoint)
    assert CLAVE not in str(endpoint)
    # Sin etiqueta, el `repr` cae al host y sigue sin ruta.
    assert CLAVE not in repr(RpcEndpoint(url=URL_CON_CLAVE))


# --------------------------------------------------------------------------- #
# 2. Error de aplicación: el nodo está sano, no se reintenta en otro
# --------------------------------------------------------------------------- #
async def test_a_jsonrpc_error_does_not_fail_over() -> None:
    """`execution reverted` es una respuesta, no una avería.

    Si esto hiciera failover, una petición mal formada se multiplicaría por cada
    nodo de la lista, gastaría la cuota de todos y dejaría el diagnóstico peor:
    cinco errores idénticos en vez de uno.
    """
    llamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32000, "message": "execution reverted"},
            },
        )

    pool = _pool(
        handler,
        (
            RpcEndpoint(url=URL_PUBLICA, label="preferido", priority=10),
            RpcEndpoint(url="https://base.drpc.org", label="respaldo", priority=100),
        ),
    )
    with pytest.raises(RpcError) as caught:
        await pool.call("eth_call")

    assert llamadas == [URL_PUBLICA]
    assert caught.value.code == -32000
    assert "execution reverted" in str(caught.value)
    # Y el nodo no queda marcado como caído: no lo está.
    assert pool.is_degraded is False
    assert pool.status()[0].failures == 0


# --------------------------------------------------------------------------- #
# 3. Error de endpoint disfrazado de error JSON-RPC: sí se pasa al siguiente
# --------------------------------------------------------------------------- #
#: Los tres medidos el 2026-10-06 contra los respaldos públicos de Ethereum, tal
#: cual contestaron. Con los tres tratados como errores de aplicación, `eth_call`
#: moría en el primero de la lista sin llegar a los dos nodos que sí servían.
NODOS_QUE_NO_SIRVEN = (
    (-32603, "Internal error"),
    (-32000, "Unauthorized: You must authenticate your request"),
    (-32601, "rpc method is not whitelisted"),
)


@pytest.mark.parametrize(("codigo", "mensaje"), NODOS_QUE_NO_SIRVEN)
async def test_a_node_that_cannot_serve_the_call_fails_over(codigo: int, mensaje: str) -> None:
    """Un nodo que no puede atender la petición no puede dejar la red sin servicio.

    Los tres casos se midieron en vivo. `cloudflare-eth.com` contesta `-32603` a
    *cualquier* llamada, `rpc.ankr.com/eth` no reconoce la petición sin clave y
    `rpc.flashbots.net` sólo tiene blanqueados unos pocos métodos. Ninguno de los
    tres es una respuesta sobre nuestros datos: son nodos que no sirven, y como
    todos los respaldos comparten prioridad y la latencia arranca a cero, el
    primero de la tupla se intenta siempre — de modo que tratarlos como errores
    de aplicación dejaba Ethereum entera muerta.
    """
    llamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        llamadas.append(url)
        if url == URL_PUBLICA:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": codigo, "message": mensaje}}
            )
        return _jsonrpc_ok("0x1")

    pool = _pool(
        handler,
        (
            RpcEndpoint(url=URL_PUBLICA, label="roto", priority=10),
            RpcEndpoint(url="https://base.drpc.org", label="sano", priority=100),
        ),
    )
    assert await pool.call("eth_chainId") == "0x1"
    assert llamadas == [URL_PUBLICA, "https://base.drpc.org"]
    # Y queda apuntado, que es lo que hace que el cortacircuitos lo acabe apartando.
    assert pool.status()[0].failures == 1
    assert "JSON-RPC" in pool.status()[0].last_error


async def test_the_broken_node_stops_being_tried_once_the_breaker_opens() -> None:
    """Sin esto, el nodo que no sirve cuesta una petición perdida en cada llamada.

    Es la otra mitad del arreglo: no basta con sobrevivir al nodo roto, hay que
    dejar de pagarle el viaje.
    """
    llamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        llamadas.append(url)
        if url == URL_PUBLICA:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32603, "message": "Internal error"},
                },
            )
        return _jsonrpc_ok("0x1")

    clock = FrozenClock(datetime(2026, 10, 6, tzinfo=UTC))
    pool = _pool(
        handler,
        (
            RpcEndpoint(url=URL_PUBLICA, label="roto", priority=10),
            RpcEndpoint(url="https://base.drpc.org", label="sano", priority=100),
        ),
        clock=clock,
        failure_threshold=2,
    )
    assert await pool.call("eth_chainId") == "0x1"
    assert await pool.call("eth_chainId") == "0x1"
    assert pool.status()[0].is_open is True

    llamadas.clear()
    assert await pool.call("eth_chainId") == "0x1"
    assert llamadas == ["https://base.drpc.org"]


async def test_the_ambiguous_code_is_decided_by_the_message_not_the_code() -> None:
    """`-32000` significa las dos cosas, así que el código solo no puede decidir.

    Geth lo usa como cajón de sastre: `execution reverted` —una respuesta sobre
    nuestros datos— y `Unauthorized` —un endpoint que no sirve— llegan con el
    mismo número. Las dos pruebas que fijan la regla usan `-32000` a propósito:
    si alguien «simplificara» esto a una lista de códigos, una de las dos se cae.
    """
    assert _is_endpoint_fault(-32000, "Unauthorized: You must authenticate") is True
    assert _is_endpoint_fault(-32000, "execution reverted") is False
    assert _is_endpoint_fault(-32000, "insufficient funds for gas * price + value") is False
    assert _is_endpoint_fault(-32000, "nonce too low") is False
    assert _is_endpoint_fault(-32000, "already known") is False
    # Un código que sí puede significar «tu petición está mal» no se reintenta
    # aunque el mensaje venga vacío: es un fallo de programación, no del nodo.
    assert _is_endpoint_fault(-32602, "") is False
    assert _is_endpoint_fault(-32600, "") is False


# --------------------------------------------------------------------------- #
# 4. Fallo de transporte: sí se pasa al siguiente, y el cortacircuitos aparta
# --------------------------------------------------------------------------- #
async def test_a_transport_failure_fails_over_to_the_next_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == URL_PUBLICA:
            return httpx.Response(401)
        return _jsonrpc_ok("0x2105")

    pool = _pool(
        handler,
        (
            RpcEndpoint(url=URL_PUBLICA, label="caido", priority=10),
            RpcEndpoint(url="https://base.drpc.org", label="sano", priority=100),
        ),
    )
    assert await pool.call("eth_chainId") == "0x2105"


async def test_the_circuit_breaker_stops_preferring_a_broken_endpoint() -> None:
    """Un nodo preferido que falla deja de tener prioridad sobre uno sano.

    Es el sentido entero del cortacircuitos: sin él, «el preferido» seguiría
    costando una petición perdida en cada llamada, para siempre.
    """
    llamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        llamadas.append(url)
        if url == URL_PUBLICA:
            return httpx.Response(500)
        return _jsonrpc_ok("0x2105")

    clock = FrozenClock(datetime(2026, 10, 6, tzinfo=UTC))
    pool = _pool(
        handler,
        (
            RpcEndpoint(url=URL_PUBLICA, label="preferido", priority=10),
            RpcEndpoint(url="https://base.drpc.org", label="respaldo", priority=100),
        ),
        clock=clock,
        failure_threshold=2,
    )

    # Las dos primeras llamadas intentan el preferido y caen al respaldo.
    assert await pool.call("eth_chainId") == "0x2105"
    assert await pool.call("eth_chainId") == "0x2105"
    assert llamadas.count(URL_PUBLICA) == 2
    assert pool.status()[0].is_open is True

    # A partir de aquí el preferido ni se intenta.
    llamadas.clear()
    assert await pool.call("eth_chainId") == "0x2105"
    assert llamadas == ["https://base.drpc.org"]


async def test_all_open_endpoints_are_still_probed() -> None:
    """Con todo abierto se sondea el que antes cumpla condena, no se aborta.

    Un apagón total es peor que una petición lenta: si se devolviera un fallo
    inmediato, la aplicación se quedaría parada justo cuando la red vuelve.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    clock = FrozenClock(datetime(2026, 10, 6, tzinfo=UTC))
    pool = _pool(handler, (RpcEndpoint(url=URL_PUBLICA, priority=10),), clock=clock)
    for _ in range(2):
        with pytest.raises(AllEndpointsFailedError):
            await pool.call("eth_chainId")
    assert pool.is_degraded is True

    # Aunque el cortacircuitos esté abierto, se sigue intentando: aquí sigue
    # caído, pero la llamada se hace — y por eso tarda en fallar, no falla antes.
    with pytest.raises(AllEndpointsFailedError) as caught:
        await pool.call("eth_chainId")
    assert "503" in str(caught.value)


# --------------------------------------------------------------------------- #
# Orden de intentos
# --------------------------------------------------------------------------- #
async def test_with_equal_latency_priority_decides() -> None:
    """`FrozenClock` congela la latencia medida, así que aquí sólo decide la prioridad."""
    llamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas.append(str(request.url))
        return _jsonrpc_ok("0x2105")

    pool = _pool(
        handler,
        (
            RpcEndpoint(url="https://base.drpc.org", label="respaldo", priority=1000),
            RpcEndpoint(url=URL_PUBLICA, label="propio", priority=10),
        ),
    )
    assert await pool.call("eth_chainId") == "0x2105"
    assert llamadas == [URL_PUBLICA]


async def test_priority_dominates_measured_latency() -> None:
    """Un nodo propio lento se sigue prefiriendo a un público rápido.

    Es la razón por la que los respaldos de `infra.rpc.defaults` comparten
    prioridad entre sí: con prioridades distintas el orden lo fijaría el fichero
    y no la red; compartiéndola, dentro del grupo decide la latencia viva.

    La latencia se escribe directamente en el estado del pool porque es la única
    forma de aislar la regla de orden del tiempo real de la red — con
    `MockTransport` las diferencias son de microsegundos y no son una prueba,
    son ruido.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return _jsonrpc_ok("0x2105")

    pool = _pool(
        handler,
        (
            RpcEndpoint(url=URL_PUBLICA, label="propio-lento", priority=10),
            RpcEndpoint(url="https://base.drpc.org", label="publico-rapido", priority=1000),
        ),
    )
    # Se escribe en el estado privado a propósito: es la única forma de aislar la
    # regla de orden del tiempo real de la red. Con `MockTransport` las
    # diferencias son de microsegundos, y una prueba construida sobre ruido no
    # prueba la regla, prueba la máquina.
    salud = pool._health
    salud[URL_PUBLICA].latency_ms = 900.0
    salud["https://base.drpc.org"].latency_ms = 5.0

    assert [endpoint.label for endpoint in pool._ordered()] == [
        "propio-lento",
        "publico-rapido",
    ]


async def test_no_endpoints_is_an_explicit_error_not_an_empty_pool() -> None:
    """Construir un pool vacío se detecta al construirlo, no en la primera llamada."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return _jsonrpc_ok("0x1")

    with pytest.raises(AllEndpointsFailedError) as caught:
        _pool(handler, ())
    assert "no tiene endpoints configurados" in str(caught.value)
