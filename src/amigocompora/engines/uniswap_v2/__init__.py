"""Motor DEX contra los AMM de producto constante —la familia V2—, sin clave.

`addresses` tiene la tabla medida de contratos por red y `calldata` la ABI;
aquí sólo se expone el motor y su fábrica.
"""

from __future__ import annotations

from amigocompora.engines.uniswap_v2.engine import (
    MANIFEST,
    PROVIDER,
    UniswapV2Engine,
    UniswapV2Provider,
)

__all__ = ["MANIFEST", "PROVIDER", "UniswapV2Engine", "UniswapV2Provider"]
