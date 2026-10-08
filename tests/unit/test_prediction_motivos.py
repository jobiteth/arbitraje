"""Cómo se escribe la lista de motivos que apaga un botón.

Es una función pura y por eso se prueba sola. La frase que produce es la única
que el usuario lee cuando un botón está apagado, así que se fija aquí lo que
tiene que cumplir: **un punto y sólo uno** al final, y los motivos separados de
forma que se puedan contar de un vistazo.

El caso que la motivó es real y se midió: el registro de motores lanza su error
con el punto incluido, y al pegarle el de la frase salía «...panel de
motores..» en pantalla. Un detalle así no rompe nada, y es exactamente el que
hace dudar de que haya alguien leyendo lo que la aplicación escribe.
"""

from __future__ import annotations

from amigocompora.ui.pages.prediction import _motivos


def test_a_motive_that_already_ends_in_a_period_does_not_get_a_second_one() -> None:
    assert _motivos(("no hay motor activo. Selecciona uno.",)) == (
        "no hay motor activo. Selecciona uno."
    )


def test_several_motives_are_joined_and_end_in_one_period() -> None:
    """Se separan con el punto medio, que no se confunde con una frase nueva."""
    unidos = _motivos(("la ejecución está apagada", "falta el colateral.", "y el motor"))
    assert unidos == "la ejecución está apagada · falta el colateral · y el motor."
    assert unidos.count(".") == 1


def test_a_single_motive_without_a_period_gets_one() -> None:
    """Sin punto, la frase quedaría abierta justo debajo de un botón apagado."""
    assert _motivos(("no hay cartera configurada",)) == "no hay cartera configurada."
