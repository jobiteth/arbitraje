from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from amigocompora.infra.rpc.probe import probe_node

URL = "https://nodo.example/v3/clave-secreta"

Handler = Callable[[httpx.Request], httpx.Response]


def _transport(handler: Handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _responde(result_hex: str) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result_hex})

    return handler


@pytest.mark.asyncio
async def test_red_correcta_es_ok() -> None:
    result = await probe_node(URL, 8453, transport=_transport(_responde("0x2105")))

    assert result.ok
    assert "8453" in result.detail
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_red_distinta_se_rechaza() -> None:
    result = await probe_node(URL, 8453, transport=_transport(_responde("0x89")))

    assert not result.ok
    assert "137" in result.detail
    assert "8453" in result.detail


@pytest.mark.asyncio
async def test_detalle_no_muestra_la_clave() -> None:
    def caido(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("caído", request=request)

    result = await probe_node(URL, 8453, transport=_transport(caido))

    assert not result.ok
    assert "clave-secreta" not in result.detail


@pytest.mark.asyncio
async def test_http_de_error_se_informa() -> None:
    result = await probe_node(URL, 8453, transport=_transport(lambda _r: httpx.Response(401)))

    assert not result.ok
    assert "401" in result.detail


@pytest.mark.asyncio
async def test_error_jsonrpc_se_informa() -> None:
    def error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}})

    result = await probe_node(URL, 8453, transport=_transport(error))

    assert not result.ok
