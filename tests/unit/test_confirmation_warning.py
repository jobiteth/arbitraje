"""El aviso que el diálogo enseña justo antes de que alguien decida.

Esta prueba existe por una línea concreta, y es la que más importa de toda la
entrega de ejecución real: el diálogo decía, escrito a mano, «Amigocompora
**nunca firma ni emite** transacciones». Con la ejecución en marcha eso dejó de
ser cierto, así que la aplicación habría estado afirmando lo contrario de lo que
estaba a punto de hacer **en el instante exacto en que el usuario decide si lo
hace**. No hay ningún otro sitio del programa donde una frase falsa cueste dinero.

El aviso depende ahora de `PendingAction.capability`, que es un dato que la acción
ya traía. Lo que se fija aquí es que la dependencia exista de verdad —y que siga
existiendo cuando alguien añada una capacidad confirmable nueva, que es la forma
en que esto se rompería en silencio—.
"""

from __future__ import annotations

import re

import pytest

from amigocompora.domain.modes import CONFIRMABLE_CAPABILITIES, Capability
from amigocompora.ui.theme import COLOR_DANGER, COLOR_WARNING
from amigocompora.ui.widgets import warning_for

#: Se quitan las etiquetas y se normaliza el espaciado: lo que se comprueba es lo
#: que lee una persona, no cómo quedó partido el literal en el código fuente. Sin
#: esto, mover una palabra de línea rompería la prueba sin que cambiara nada real.
_TAGS = re.compile(r"<[^>]+>")


def _plain(capability: Capability) -> str:
    text, _ = warning_for(capability)
    return " ".join(_TAGS.sub("", text).split())


def test_confirming_a_broadcast_says_the_operation_is_real() -> None:
    """Emitir es irreversible, y el aviso tiene que decirlo con esas palabras."""
    texto = _plain(Capability.BROADCAST_TX)
    assert "firmar y emitir" in texto
    assert "irreversible" in texto
    # Y el aviso viejo —el que mentía— ya no está.
    assert "nunca firma" not in texto


def test_confirming_a_broadcast_is_visually_different_from_preparing() -> None:
    """El color no es decoración: es lo que se ve antes de leer nada.

    Un aviso rojo y uno amarillo para dos acciones de consecuencias opuestas. Si
    los dos se pintaran igual, el aviso correcto pasaría desapercibido justo por
    parecerse al otro.
    """
    assert warning_for(Capability.BROADCAST_TX)[1] == COLOR_DANGER
    assert warning_for(Capability.PREPARE_TX)[1] == COLOR_WARNING


def test_confirming_a_prepare_still_says_nothing_is_signed() -> None:
    """Y en el camino que no gasta dinero, la promesa de antes sigue siendo cierta.

    «No firma ni emite» es correcto **de esta acción**; lo que estaba mal era
    decir que valía para la aplicación entera. La distinción importa: quitar la
    frase en vez de acotarla habría dejado al usuario sin la información de que
    construir un payload no mueve nada.
    """
    texto = _plain(Capability.PREPARE_TX)
    assert "no firma ni emite" in texto
    assert "irreversible" not in texto


@pytest.mark.parametrize("capability", sorted(CONFIRMABLE_CAPABILITIES))
def test_every_confirmable_capability_has_a_warning(capability: Capability) -> None:
    texto, color = warning_for(capability)
    assert texto.strip()
    assert color in {COLOR_DANGER, COLOR_WARNING}


def test_no_two_confirmable_capabilities_share_a_warning() -> None:
    """La comprobación que convierte un olvido silencioso en un fallo.

    Se recorre `CONFIRMABLE_CAPABILITIES` —la lista del dominio, no la de esta
    prueba— y se exige que cada una tenga un texto **distinto**. El día que
    alguien añada una tercera capacidad confirmable que mueva dinero, heredará el
    aviso de construir payloads y esto lo dirá; sin esta línea, se colaría hasta
    producción con el mismo aspecto que el fallo original, que fue exactamente
    eso: una frase escrita para un caso y mostrada en otro.
    """
    textos = [_plain(capability) for capability in CONFIRMABLE_CAPABILITIES]
    assert len(set(textos)) == len(textos)
