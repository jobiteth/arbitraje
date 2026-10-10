"""El deslizamiento con el que se construyen los swaps, en vivo.

La tolerancia de deslizamiento es lo que fija el mínimo que un swap acepta
recibir, así que es una decisión del usuario y no una constante del programa.
Vive aquí —y no en `Settings`— porque `Settings` es inmutable: la configuración
se lee una vez al arrancar y el engranaje de la pantalla tiene que poder
cambiarla sin reiniciar la aplicación. Este objeto es la única copia viva del
valor, la que `PrepareSwap` lee al construir, y quien la cambia es sólo la
interfaz.

Arranca con lo que diga el `config.toml`, no con un valor propio: si el fichero
trae una cifra, es la que se aplica desde el primer swap. La persistencia es un
paso aparte —quien cambia el valor escribe el fichero por su cuenta—, y ese
orden importa: así un fallo al escribir no deja al motor construyendo con una
cifra que el usuario no eligió, sino con la que acaba de fijar.

El rango se valida **aquí** y no en la interfaz porque aquí es donde el valor
entra en el motor: una comprobación que sólo vive en el diálogo la esquiva
cualquier otro llamante, y esto decide cuánto puede perder una operación. 10 000
bps es el 100 % —recibir cero se acepta como decisión explícita— y el cero
significa «lo que caiga», que es la tolerancia mínima que un payload puede
llevar.
"""

from __future__ import annotations

from amigocompora.domain.errors import InvalidAmountError

__all__ = ["DefaultSlippage"]


class DefaultSlippage:
    """La tolerancia que se aplica al construir, en puntos básicos.

    Es mutable a propósito —es la copia viva que el engranaje actualiza—, y por
    eso mismo es diminuta: un entero con su rango comprobado en el único sitio
    que puede cambiarlo. Toda lectura pasa por `bps`; no hay forma de tocar el
    valor sin validarlo.
    """

    __slots__ = ("_bps",)

    def __init__(self, bps: int) -> None:
        """Nace con la tolerancia de la configuración, ya validada."""
        self._require_in_range(bps)
        self._bps = bps

    @property
    def bps(self) -> int:
        """La tolerancia vigente, en puntos básicos (50 = 0,5 %)."""
        return self._bps

    def set(self, bps: int) -> None:
        """Fija una tolerancia nueva. Se valida **antes** de cambiar nada.

        Si el valor no cabe en el rango se lanza y el vigente no se toca: un
        cambio a medias dejaría al motor construyendo con una cifra que no es ni
        la vieja ni la pedida.
        """
        self._require_in_range(bps)
        self._bps = bps

    @staticmethod
    def _require_in_range(bps: int) -> None:
        if not 0 <= bps <= 10_000:
            raise InvalidAmountError(
                f"slippage_bps fuera de rango: {bps}. Se espera 0..10000."
            )
