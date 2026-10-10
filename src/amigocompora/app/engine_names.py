"""Alias de pantalla de los motores, editables en caliente.

El nombre de un motor tiene dos caras: el **de fábrica** —el del manifiesto, que
viaja con el motor y no se toca— y el **alias**, que elige el usuario en la
pestaña de motores. El alias es sólo de pantalla: el id no cambia, la
configuración no cambia y lo que el motor hace no cambia. Por eso vive aquí y no
en el manifiesto, que es del motor y no del usuario.

Es **mutable** a propósito, como `DefaultSlippage`: `Settings` se lee una vez al
arrancar y esto tiene que cambiar sin reiniciar. Y la instancia es única —la
comparten las pantallas que enseñan nombres— porque un motor que se llamara de
una manera en la pestaña de motores y de otra en la de credenciales sería dos
nombres para la misma cosa, que es exactamente lo que un alias viene a evitar.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

#: Largo máximo del alias, en caracteres. No es una regla de seguridad: es que
#: un nombre de trescientos caracteres no cabe en ninguna columna y acabaría
#: recortado sin decirlo. Se recorta **aquí**, que es el único sitio donde se
#: puede decir que se recortó, en vez de en cada pantalla que lo pinta — donde
#: ya no habría a quién avisar.
MAX_NAME_CHARS: Final = 60


class EngineNames:
    """Los alias por id de motor, con el nombre de fábrica como respaldo."""

    __slots__ = ("_aliases",)

    def __init__(self, overrides: Mapping[str, str] | None = None) -> None:
        self._aliases: dict[str, str] = {}
        for engine_id, alias in (overrides or {}).items():
            self.set(engine_id, alias)

    def resolve(self, engine_id: str, default: str) -> str:
        """El alias si lo hay; si no, el nombre que trae el motor."""
        return self._aliases.get(engine_id, default)

    def set(self, engine_id: str, alias: str) -> None:
        """Fija el alias de un motor. Vacío —o sólo espacios— lo borra.

        Un alias vacío **borra** en vez de guardarse como cadena vacía, y ésa es
        la diferencia entre «vuelve a su nombre de fábrica» y «se llama nada»:
        sin esa regla, limpiar el campo del diálogo dejaría una fila sin nombre.

        La normalización —recortar, colapsar espacios, acotar el largo— se hace
        aquí y no en quien pinta: un alias con un salto de línea se vería
        distinto en cada columna, y el sitio donde se puede arreglar una vez es
        éste. El id vacío no es un motor: se ignora.
        """
        clave = engine_id.strip()
        if not clave:
            return
        limpio = " ".join(alias.split())[:MAX_NAME_CHARS]
        if limpio:
            self._aliases[clave] = limpio
        else:
            self._aliases.pop(clave, None)

    def as_dict(self) -> dict[str, str]:
        """Copia del mapa, para persistirlo sin exponer el estado vivo."""
        return dict(self._aliases)

    def __repr__(self) -> str:
        return f"EngineNames({len(self._aliases)} alias)"
