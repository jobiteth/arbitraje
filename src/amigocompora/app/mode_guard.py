"""`ModeGuard` — el único punto del sistema que autoriza capacidades.

Diseño: los casos de uso **declaran** lo que necesitan (`Capability`) y el guard
decide a partir de la tabla `MODE_CAPABILITIES`. Nada más en la aplicación
pregunta por el modo activo, así que la política es auditable leyendo un solo
fichero en vez de persiguiendo condicionales por toda la base de código.

El guard es también la razón por la que la ejecución desatendida no puede
saltarse el modo. La política de autonomía vive en otro sitio y lo único que
puede hacer es **omitir la pregunta**; las capacidades las sigue concediendo
esta clase, y firmar o emitir exige estar en `EXECUTION`. Un fallo en la
política de autonomía no puede, por tanto, convertirse en una operación con
efectos desde un modo que no la permite.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from amigocompora.domain.errors import ModeNotPermittedError
from amigocompora.domain.modes import (
    CONFIRMABLE_CAPABILITIES,
    DEFAULT_MODE,
    Capability,
    OperationMode,
    modes_granting,
)

#: Firma de los observadores de cambio de modo. La UI se suscribe para
#: repintar el indicador sin que la capa de aplicación conozca Qt.
ModeListener = Callable[[OperationMode], None]


def _as_mode(mode: OperationMode | str) -> OperationMode:
    """El modo de verdad, venga como venga.

    Hace falta por un tropiezo de Qt que ya rompió la aplicación una vez:
    `QComboBox.addItem(etiqueta, modo)` guarda el dato del ítem como texto —y
    `OperationMode` es un `StrEnum`, o sea un `str`—, así que `currentData()` lo
    devuelve como una cadena pelada. Con el desplegable de modo, elegir
    cualquiera de ellos dejaba al guard con un `str` dentro y **toda** consulta
    de permisos reventaba con `'str' object has no attribute 'grants'`: dejaban
    de funcionar a la vez la cotización, los puentes y los mercados.

    El guard decide qué se puede firmar, así que no se fía de quien lo llama:
    convierte aquí, y lo que hay dentro es siempre un modo.
    """
    if isinstance(mode, OperationMode):
        return mode
    try:
        return OperationMode(mode)
    except ValueError:
        validos = ", ".join(opcion.value for opcion in OperationMode)
        raise ValueError(
            f"«{mode}» no es un modo de operación. Los que hay son: {validos}."
        ) from None


class ModeGuard:
    """Guarda el modo activo y autoriza capacidades contra él."""

    __slots__ = ("_listeners", "_mode")

    def __init__(self, mode: OperationMode | str = DEFAULT_MODE) -> None:
        self._mode = _as_mode(mode)
        self._listeners: list[ModeListener] = []

    # ------------------------------------------------------------- estado  #
    @property
    def mode(self) -> OperationMode:
        return self._mode

    def set_mode(self, mode: OperationMode | str) -> None:
        """Cambia el modo activo.

        Sólo debe invocarse desde una acción explícita del usuario: ni un caso
        de uso ni un motor pueden subir de modo para desbloquearse a sí mismos.

        Acepta el modo o su texto porque quien lo llama desde la interfaz lo
        recibe de un `QComboBox`, que devuelve texto; ver `_as_mode`. Los
        observadores reciben siempre el modo, nunca la cadena.
        """
        destino = _as_mode(mode)
        if destino == self._mode:
            return
        self._mode = destino
        for listener in tuple(self._listeners):
            listener(destino)

    def subscribe(self, listener: ModeListener) -> Callable[[], None]:
        """Registra un observador y devuelve la función para darse de baja."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    # ---------------------------------------------------------- autorizar  #
    def allows(self, capability: Capability) -> bool:
        return self._mode.grants(capability)

    def require(self, capability: Capability) -> None:
        """Autoriza o lanza `ModeNotPermittedError`."""
        if not self.allows(capability):
            raise ModeNotPermittedError(self._mode.label, capability.label)

    def requires_confirmation(self, capability: Capability) -> bool:
        """Si además del modo hace falta un «sí» explícito del usuario."""
        return capability in CONFIRMABLE_CAPABILITIES

    # -------------------------------------------------------------- ayuda  #
    def upgrade_path(self, capability: Capability) -> OperationMode | None:
        """Modo mínimo al que subir para obtener `capability`, o `None`.

        Devuelve `None` si ningún modo la concede, para que la UI distinga
        «sube de modo» de «esto no existe en esta versión».
        """
        candidates = modes_granting(capability)
        if not candidates:
            return None
        return min(candidates, key=lambda mode: len(mode.capabilities))

    def explain(self, capability: Capability) -> str:
        """Mensaje accionable para la UI cuando algo está bloqueado."""
        if self.allows(capability):
            return f"El modo {self._mode.label} permite {capability.label}."
        target = self.upgrade_path(capability)
        if target is None:
            return (
                f"«{capability.label.capitalize()}» no está disponible en esta versión "
                f"de Amigocompora, en ningún modo."
            )
        return (
            f"El modo {self._mode.label} no permite {capability.label}. "
            f"Cambia a {target.label} si quieres habilitarlo."
        )

    def __iter__(self) -> Iterator[Capability]:
        """Capacidades efectivamente disponibles ahora mismo."""
        return (capability for capability in Capability if self.allows(capability))

    def __repr__(self) -> str:
        return f"ModeGuard(mode={self._mode.label})"
