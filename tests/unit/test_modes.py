"""La barrera de seguridad: qué concede cada modo y qué nunca se concede.

Estas pruebas son las que sostienen la afirmación de que la aplicación no puede
mover dinero sin que alguien lo haya decidido. Por eso no comprueban una lista
de casos, sino **invariantes**: que firmar y emitir sólo los concede un modo, que
subir de modo nunca quita nada, y que ninguna capacidad se queda sin dueño. Una
tabla de política puede crecer con el tiempo; estas propiedades son lo que tiene
que seguir siendo cierto cuando crezca.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from amigocompora.app.mode_guard import ModeGuard
from amigocompora.domain.errors import ModeNotPermittedError
from amigocompora.domain.modes import (
    CONFIRMABLE_CAPABILITIES,
    DEFAULT_MODE,
    MODE_CAPABILITIES,
    Capability,
    OperationMode,
    modes_granting,
)

#: Los modos de menos a más capaz. El orden es una decisión del producto, no una
#: propiedad del enum —de ahí que se escriba aquí en vez de deducirlo del orden
#: de declaración—, y es lo que da sentido a la prueba de monotonía.
_ESCALATION = (
    OperationMode.OBSERVATION,
    OperationMode.SIMULATION,
    OperationMode.ASSISTED,
    OperationMode.EXECUTION,
)

#: Las que mueven dinero de verdad. La lista está aquí para que la prueba que las
#: restringe no dependa de la tabla que está probando.
_MONEY_CAPABILITIES = frozenset({Capability.SIGN_TX, Capability.BROADCAST_TX})


@pytest.mark.parametrize(
    ("mode", "capability", "allowed"),
    [
        (OperationMode.OBSERVATION, Capability.READ_CHAIN, True),
        (OperationMode.OBSERVATION, Capability.QUERY_AI, True),
        (OperationMode.OBSERVATION, Capability.COMPUTE_ROUTE, False),
        (OperationMode.OBSERVATION, Capability.PREPARE_TX, False),
        (OperationMode.SIMULATION, Capability.COMPUTE_ROUTE, True),
        (OperationMode.SIMULATION, Capability.PREPARE_TX, False),
        (OperationMode.ASSISTED, Capability.PREPARE_TX, True),
        (OperationMode.ASSISTED, Capability.SIGN_TX, False),
        (OperationMode.ASSISTED, Capability.BROADCAST_TX, False),
        (OperationMode.EXECUTION, Capability.PREPARE_TX, True),
        (OperationMode.EXECUTION, Capability.SIGN_TX, True),
        (OperationMode.EXECUTION, Capability.BROADCAST_TX, True),
    ],
)
def test_mode_matrix(mode: OperationMode, capability: Capability, allowed: bool) -> None:
    assert mode.grants(capability) is allowed


def test_default_mode_is_least_capable() -> None:
    """Al arrancar no se puede hacer nada que mueva dinero."""
    guard = ModeGuard()
    assert guard.mode is OperationMode.OBSERVATION
    assert guard.mode is DEFAULT_MODE
    for capability in _MONEY_CAPABILITIES:
        assert guard.allows(capability) is False


# --------------------------------------------------------------------------- #
# Las invariantes que protegen de verdad
# --------------------------------------------------------------------------- #
def test_firmar_y_emitir_solo_los_concede_execution() -> None:
    """Ni uno más. Es la invariante central de todo el producto.

    Si alguien añadiera `SIGN_TX` a `ASSISTED` —que es exactamente el cambio que
    parece razonable cuando se pide «que pueda operar»— esta prueba lo dice antes
    de que llegue a `main`.
    """
    for capability in _MONEY_CAPABILITIES:
        granting = modes_granting(capability)
        assert granting == frozenset({OperationMode.EXECUTION})


def test_la_tabla_es_monotona() -> None:
    """Subir de modo nunca quita una capacidad.

    Sin esto, subir de `SIMULATION` a `ASSISTED` podría desactivar algo que ya
    funcionaba, y el usuario aprendería a no subir de modo. El test también
    protege contra el error de escribir una columna nueva y olvidarla en un modo
    intermedio.
    """
    for lower, higher in pairwise(_ESCALATION):
        assert lower.capabilities <= higher.capabilities, (
            f"{higher.label} no concede todo lo que concede {lower.label}: "
            f"falta {sorted(lower.capabilities - higher.capabilities)}"
        )


def test_ninguna_capacidad_se_queda_sin_dueno() -> None:
    """Toda capacidad del enum la concede algún modo.

    Una capacidad que nadie concede es una función que no se puede usar y un
    botón que nunca se habilita. `upgrade_path` devuelve `None` en ese caso, y la
    UI lo lee como «esto no existe en esta versión»: una mentira si la capacidad
    está declarada.
    """
    for capability in Capability:
        assert modes_granting(capability), f"nadie concede «{capability.label}»"


def test_solo_se_confirma_lo_que_tiene_efecto() -> None:
    """`SIGN_TX` no exige confirmación; `BROADCAST_TX` sí, y es la que importa.

    Firmar sin emitir no tiene efecto dentro del flujo de la aplicación. Pedir
    dos «síes» para una sola operación no añade seguridad: entrena a pulsar
    «Confirmar» sin leer, y gasta la atención que hace falta en el diálogo que de
    verdad decide, que es el de emitir.
    """
    assert Capability.BROADCAST_TX in CONFIRMABLE_CAPABILITIES
    assert Capability.PREPARE_TX in CONFIRMABLE_CAPABILITIES
    assert Capability.SIGN_TX not in CONFIRMABLE_CAPABILITIES


def test_require_raises_with_actionable_message() -> None:
    guard = ModeGuard(OperationMode.OBSERVATION)
    with pytest.raises(ModeNotPermittedError):
        guard.require(Capability.COMPUTE_ROUTE)


def test_upgrade_path_points_to_simulation() -> None:
    guard = ModeGuard(OperationMode.OBSERVATION)
    assert guard.upgrade_path(Capability.COMPUTE_ROUTE) is OperationMode.SIMULATION


def test_upgrade_path_to_execution_for_signing() -> None:
    """Para firmar, la UI puede decir exactamente a qué modo subir."""
    guard = ModeGuard(OperationMode.ASSISTED)
    assert guard.upgrade_path(Capability.SIGN_TX) is OperationMode.EXECUTION
    assert guard.upgrade_path(Capability.BROADCAST_TX) is OperationMode.EXECUTION


def test_explain_says_which_mode_to_switch_to() -> None:
    """El mensaje de bloqueo nombra el modo destino, no sólo que no se puede."""
    guard = ModeGuard(OperationMode.ASSISTED)
    assert OperationMode.EXECUTION.label in guard.explain(Capability.BROADCAST_TX)


def test_mode_listeners_notified() -> None:
    guard = ModeGuard()
    seen: list[OperationMode] = []
    guard.subscribe(seen.append)
    guard.set_mode(OperationMode.ASSISTED)
    assert seen == [OperationMode.ASSISTED]


def test_el_guard_digiere_el_modo_servido_como_texto() -> None:
    """Lo que devuelve Qt es una cadena pelada, y el guard tiene que aceptarla.

    No es una comodidad: es el fallo que dejó la aplicación inservible. El
    desplegable de modo guarda el `OperationMode` como texto —es un `StrEnum`— y
    lo devuelve como texto, así que `set_mode` recibía `"execution"` en vez del
    modo. Al guardarle una cadena, la siguiente consulta de permisos —cotizar,
    buscar un puente, pedir mercados— reventaba con `'str' object has no
    attribute 'grants'`. Como el guard es quien decide qué se firma, la
    conversión va dentro y no en quien lo llama.
    """
    guard = ModeGuard()
    guard.set_mode("execution")
    assert guard.mode is OperationMode.EXECUTION
    assert guard.allows(Capability.BROADCAST_TX)


def test_el_guard_tambien_nace_de_una_cadena() -> None:
    """La otra puerta: construirlo con el texto no puede dejar un `str` dentro."""
    guard = ModeGuard("assisted")
    assert guard.mode is OperationMode.ASSISTED
    assert guard.allows(Capability.PREPARE_TX)


def test_los_observadores_reciben_el_modo_y_no_la_cadena() -> None:
    """Quien se apunta a los cambios pinta con el modo; con texto no podría."""
    guard = ModeGuard()
    visto: list[OperationMode] = []
    guard.subscribe(visto.append)
    guard.set_mode("simulation")
    assert visto == [OperationMode.SIMULATION]
    assert isinstance(visto[0], OperationMode)


def test_un_modo_que_no_existe_se_rechaza_diciendo_cuales_hay() -> None:
    """Un texto inventado no puede colarse como modo activo."""
    guard = ModeGuard()
    with pytest.raises(ValueError, match="observation"):
        guard.set_mode("turbo")
    assert guard.mode is OperationMode.OBSERVATION


def test_modes_granting_finds_observer_of_read() -> None:
    assert modes_granting(Capability.READ_CHAIN) == frozenset(OperationMode)


def test_the_table_covers_every_mode() -> None:
    """Un modo sin entrada en la tabla reventaría con `KeyError` en el guard."""
    assert set(MODE_CAPABILITIES) == set(OperationMode)
