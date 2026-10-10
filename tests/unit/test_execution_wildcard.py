"""El comodín `"*"` de `allowed_tokens` y `allowed_chains`: a sabiendas, no por descuido.

### Por qué hay una prueba para esto

Dejar una lista blanca abierta es la decisión de seguridad más fácil de tomar
mal: un cambio que hiciera que `"*"` se leyera como una lista vacía cerraría las
operaciones en silencio, y uno que hiciera que cualquier cosa pasara sin el
comodín las abriría para todos. Las dos cosas tienen que fallar en la dirección
segura, y eso es lo que se afirma aquí —en las dos listas, la de tokens y la de
redes, que comparten semántica—.
"""

from __future__ import annotations

import pytest

from amigocompora.domain.errors import ExecutionLimitExceededError
from amigocompora.domain.execution import ANY_CHAIN, ANY_TOKEN, ExecutionLimits


def _limites(
    tokens: frozenset[str], *, chains: frozenset[str] = frozenset({"polygon"})
) -> ExecutionLimits:
    return ExecutionLimits(
        enabled=True,
        max_quote_per_trade=None,
        max_quote_per_day=None,
        allowed_tokens=tokens,
        allowed_chains=chains,
        allowed_engines=frozenset({"uniswap_v3"}),
    )


def test_el_comodin_es_el_asterisco() -> None:
    assert ANY_TOKEN == "*"  # noqa: S105  # símbolo de la lista, no un secreto
    # El de redes es el mismo valor: si se separaran, la misma decisión se
    # escribiría de dos formas distintas en el fichero.
    assert ANY_CHAIN == ANY_TOKEN


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


def test_con_el_comodin_cualquier_red_pasa() -> None:
    """El caso medido: «siempre me dice que no está en la lista de redes»."""
    limites = _limites(frozenset({ANY_TOKEN}), chains=frozenset({ANY_CHAIN}))
    limites.check_chain("base")
    limites.check_chain("polygon")
    limites.check_chain("una-red-que-no-existe-todavia")


def test_sin_el_comodin_una_red_fuera_de_la_lista_se_rechaza() -> None:
    limites = _limites(frozenset({ANY_TOKEN}))
    limites.check_chain("polygon")
    with pytest.raises(ExecutionLimitExceededError):
        limites.check_chain("base")


def test_una_lista_de_redes_vacia_sigue_significando_nada_permitido() -> None:
    limites = _limites(frozenset({ANY_TOKEN}), chains=frozenset())
    with pytest.raises(ExecutionLimitExceededError):
        limites.check_chain("polygon")


def test_allows_chain_responde_con_la_lista_normalizada_en_los_dos_lados() -> None:
    """`allows_chain` es la respuesta que la interfaz usa antes de firmar.

    Unos límites construidos a mano pueden traer la lista sin normalizar; que
    la comprobación no dependa de que alguien la normalice antes es lo mismo
    que se decidió para los tokens.
    """
    limites = _limites(frozenset({ANY_TOKEN}), chains=frozenset({"POLYGON"}))
    assert limites.allows_chain("polygon") is True
    assert limites.allows_chain("POLYGON") is True
    assert limites.allows_chain("base") is False
