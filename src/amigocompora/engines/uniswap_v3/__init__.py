"""Motor DEX contra los contratos de Uniswap V3, la vía **directa** y sin clave.

`addresses` tiene la tabla medida de contratos por red y `calldata` la ABI; aquí
sólo se expone el motor y su fábrica.
"""

from __future__ import annotations

from amigocompora.engines.uniswap_v3.engine import (
    MANIFEST,
    PROVIDER,
    UniswapV3Engine,
    UniswapV3Provider,
)

__all__ = ["MANIFEST", "PROVIDER", "UniswapV3Engine", "UniswapV3Provider"]
