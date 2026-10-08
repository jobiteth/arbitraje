"""El comodín `"*"` de `allowed_tokens`: operar con cualquier token, a sabiendas.

### Por qué hay una prueba para esto

Dejar la lista blanca abierta es la decisión de seguridad más fácil de tomar
mal: un cambio que hiciera que `"*"` se leyera como una lista vacía cerraría las
operaciones en silencio, y uno que hiciera que cualquier cosa pasara sin el
comodín las abriría para todos. Las dos cosas tienen que fallar en la dirección
segura, y eso es lo que se afirma aquí.
"""

from __future__ import annotations

import pytest

from amigocompora.domain.errors import ExecutionLimitExceededError
from amigocompora.domain.execution import ANY_TOKEN, ExecutionLimits


def _limites(tokens: frozenset[str]) -> ExecutionLimits:
    return ExecutionLimits(
        enabled=True,
        max_quote_per_trade=None,
        max_quote_per_day=None,
        allowed_tokens=tokens,
        allowed_chains=frozenset({"polygon"}),
        allowed_engines=frozenset({"uniswap_v3"}),
    )


def test_el_comodin_es_el_asterisco() -> None:
    assert ANY_TOKEN == "*"  # noqa: S105  # símbolo de la lista, no un secreto


def test_con_el_comodin_cualquier_token_pasa() -> None:
    limites = _limites(frozenset({ANY_TOKEN}))
    limites.check_token(("POL", "USDC"))
    limites.check_token(("TOKEN_DESCONOCIDO_CUALQUIERA",))


def test_sin_el_comodin_un_token_fuera_de_la_lista_se_rechaza() -> None:
    limites = _limites(frozenset({"POL", "USDC"}))
    with pytest.raises(ExecutionLimitExceededError):
        limites.check_token(("POL", "PEPE"))


def test_una_lista_vacia_sigue_significando_nada_permitido() -> None:
    limites = _limites(frozenset())
    with pytest.raises(ExecutionLimitExceededError):
        limites.check_token(("POL",))
