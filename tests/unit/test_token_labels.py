from __future__ import annotations

from amigocompora.domain.models import Token
from amigocompora.ui.widgets import token_labels

USDC_NATIVO = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_PUENTEADO = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


def test_usdc_puenteado_de_polygon_se_llama_usdc_e() -> None:
    etiquetas = token_labels(
        (
            Token("USDC", 6, "polygon", USDC_NATIVO),
            Token("USDC", 6, "polygon", USDC_PUENTEADO),
        )
    )
    assert etiquetas == ("USDC", "USDC.e")
