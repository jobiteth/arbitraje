"""Motor DEX contra los contratos de Uniswap V4, la vía **directa** y sin clave.

`addresses` tiene la tabla medida de contratos por red y `calldata` la ABI —con la
particularidad de que aquí un pool no es un contrato sino una clave que se deriva,
y de que el swap se manda al `UniversalRouter` en vez de a un router por red—;
aquí sólo se expone el motor y su fábrica.
"""

from __future__ import annotations

from amigocompora.engines.uniswap_v4.engine import (
    MANIFEST,
    PROVIDER,
    UniswapV4Engine,
    UniswapV4Provider,
)

__all__ = ["MANIFEST", "PROVIDER", "UniswapV4Engine", "UniswapV4Provider"]
