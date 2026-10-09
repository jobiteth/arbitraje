"""Comprobación de un nodo antes de guardarlo: responde y es la red que dice ser."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from amigocompora.infra.logging import safe_url

PROBE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    detail: str
    latency_ms: int | None = None


async def probe_node(
    url: str,
    expected_chain_id: int | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProbeResult:
    """Pregunta `eth_chainId` y compara la respuesta con la red esperada.

    El detalle nunca incluye la URL completa: una clave de nodo viaja dentro de
    ella y no debe acabar en pantalla ni en el registro.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as error:
        return ProbeResult(False, f"no responde ({type(error).__name__}) en {safe_url(url)}")
    latency = round((time.perf_counter() - started) * 1000)

    if response.status_code != 200:
        return ProbeResult(False, f"HTTP {response.status_code}", latency)
    try:
        body = response.json()
    except ValueError:
        return ProbeResult(False, "la respuesta no es JSON-RPC", latency)
    if "error" in body:
        return ProbeResult(False, "el nodo devolvió un error de JSON-RPC", latency)

    try:
        chain_id = int(str(body.get("result", "")), 16)
    except ValueError:
        return ProbeResult(False, "chain id no válido en la respuesta", latency)
    if expected_chain_id is not None and chain_id != expected_chain_id:
        return ProbeResult(
            False,
            f"responde con la red {chain_id}, y la red elegida es la {expected_chain_id}",
            latency,
        )
    return ProbeResult(True, f"red {chain_id} correcta", latency)
