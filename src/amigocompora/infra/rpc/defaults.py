"""Endpoints públicos de respaldo, medidos — no copiados de una lista.

## Por qué existe este fichero

Los nodos que se declaran aquí **se intentan antes que nada** cuando el usuario no
ha configurado ninguno, y **después** de los suyos cuando sí lo ha hecho. La regla
que se cumple es la del producto: la red no se queda sin nadie a quien preguntar.

## Cómo se eligieron

Medidos el 2026-10-06 desde esta máquina con un `eth_chainId` real, y varios
candidatos populares quedaron **fuera** por lo que devolvieron, no por lo que
prometen:

- `eth.llamarpc.com` y `base.llamarpc.com`: HTTP 525.
- `base.blockpi.network`: HTTP 521.
- `rpc.ankr.com/eth` funciona, pero compite con otros más rápidos.
- `bsc.drpc.org`: HTTP 429 en la primera llamada, sin haber gastado cuota.
- `polygon-rpc.com`: HTTP 401 — pide clave, así que no es público.

Que un endpoint estuviera caído ese día no significa que lo esté siempre; lo que
significa es que **no puede ser la primera opción de nadie**.

## Por qué todos los respaldos comparten prioridad

`RpcPool` ordena por `(priority, latencia_medida)`, y eso hace que la prioridad
**domine**: con prioridades distintas, el orden lo fija este fichero y no la red,
y «el más rápido» sería el que era más rápido el día que alguien midió. Dándoles
la misma prioridad, la latencia —que el pool lleva en media móvil y actualiza en
cada llamada— es la que ordena, y el nodo más rápido de ahora mismo es el que se
usa ahora mismo. La prioridad queda para lo único que debe decidirla: que los
endpoints del usuario vayan antes que estos.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from amigocompora.infra.rpc.pool import RpcEndpoint

#: Prioridad de todos los respaldos públicos. Alta para que cualquier endpoint
#: declarado por el usuario —que usa 100 por omisión— se intente antes.
FALLBACK_PRIORITY: Final = 1000


def _public(host: str, label: str) -> RpcEndpoint:
    return RpcEndpoint(url=f"https://{host}", label=label, priority=FALLBACK_PRIORITY)


#: Medidos el 2026-10-06, en orden de lo que tardaron ese día. El orden dentro de
#: la tupla no cambia la selección —todos comparten prioridad y decide la
#: latencia— pero se deja tal cual se midió para que sea legible de un vistazo.
DEFAULT_ENDPOINTS: Final[Mapping[str, tuple[RpcEndpoint, ...]]] = {
    "ethereum": (
        _public("cloudflare-eth.com", "cloudflare"),
        _public("eth.drpc.org", "drpc"),
        _public("ethereum-rpc.publicnode.com", "publicnode"),
        _public("rpc.ankr.com/eth", "ankr"),
        _public("rpc.flashbots.net", "flashbots"),
    ),
    "base": (
        _public("base.drpc.org", "drpc"),
        _public("mainnet.base.org", "base-oficial"),
        _public("base-rpc.publicnode.com", "publicnode"),
    ),
    "bsc": (
        _public("bsc-rpc.publicnode.com", "publicnode"),
        _public("bsc-dataseed.binance.org", "binance"),
    ),
    "arbitrum": (
        _public("arbitrum.drpc.org", "drpc"),
        _public("arbitrum-one-rpc.publicnode.com", "publicnode"),
        _public("arb1.arbitrum.io", "arbitrum-oficial"),
    ),
    "polygon": (
        _public("polygon.drpc.org", "drpc"),
        _public("polygon-bor-rpc.publicnode.com", "publicnode"),
    ),
    "optimism": (
        _public("optimism.drpc.org", "drpc"),
        _public("optimism-rpc.publicnode.com", "publicnode"),
        _public("mainnet.optimism.io", "optimism-oficial"),
    ),
    "avalanche": (
        _public("api.avax.network/ext/bc/C/rpc", "avalanche-oficial"),
        _public("avalanche.drpc.org", "drpc"),
        _public("avalanche-c-chain-rpc.publicnode.com", "publicnode"),
    ),
    "unichain": (
        _public("unichain.drpc.org", "drpc"),
        _public("unichain-rpc.publicnode.com", "publicnode"),
        _public("mainnet.unichain.org", "unichain-oficial"),
    ),
}


def fallback_endpoints(chain_key: str) -> tuple[RpcEndpoint, ...]:
    """Los respaldos públicos de una red, o vacío si no hay ninguno medido.

    Vacío es una respuesta legítima: una red registrada a la que todavía no se le
    han medido nodos públicos no debe recibir una lista inventada. Se prefiere
    que el usuario vea «esta red no tiene endpoints» —que se arregla añadiendo
    uno— a que vea un fallo de red que no puede diagnosticar.
    """
    return DEFAULT_ENDPOINTS.get(chain_key, ())
