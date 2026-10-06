"""Jerarquía de errores del dominio y de la aplicación.

Toda excepción propia hereda de `AmigocomporaError`, de modo que la UI puede
distinguir un fallo esperado del producto de un bug real del intérprete.
"""

from __future__ import annotations


class AmigocomporaError(Exception):
    """Raíz de todos los errores propios del producto."""


# --------------------------------------------------------------------------- #
# Aritmética monetaria
# --------------------------------------------------------------------------- #
class MoneyError(AmigocomporaError):
    """Error en la representación o la aritmética de valores económicos."""


class InvalidAmountError(MoneyError):
    """La cantidad no se puede representar de forma exacta."""


class CurrencyMismatchError(MoneyError):
    """Se intentó combinar cantidades de tokens distintos o con otra escala."""


# --------------------------------------------------------------------------- #
# Modos de operación — la barrera de seguridad del producto
# --------------------------------------------------------------------------- #
class ModeNotPermittedError(AmigocomporaError):
    """El modo activo no concede la capacidad que el caso de uso requiere.

    No es un bug: es la barrera funcionando. La UI debe mostrar el motivo y,
    si procede, ofrecer al usuario subir de modo de forma explícita.
    """

    def __init__(self, mode_label: str, capability: str) -> None:
        self.mode_label = mode_label
        self.capability = capability
        super().__init__(
            f"El modo {mode_label} no permite «{capability}». "
            f"Cambia de modo explícitamente para habilitarlo."
        )


class ConfirmationRequiredError(AmigocomporaError):
    """Se intentó ejecutar una acción pendiente que el usuario no ha confirmado."""


class ConfirmationDeniedError(AmigocomporaError):
    """El usuario rechazó la acción propuesta."""


# --------------------------------------------------------------------------- #
# Datos de mercado
# --------------------------------------------------------------------------- #
class MarketDataError(AmigocomporaError):
    """Los datos de mercado no permiten responder a lo que se preguntó."""


class NoQuotesError(MarketDataError):
    """Ningún venue devolvió cotización para ese par y tamaño.

    Condición de mercado normal (sin liquidez suficiente), no un fallo técnico.
    """


class SourceUnavailableError(MarketDataError):
    """No se pudo contactar con la fuente de datos, o respondió con un error.

    Se distingue de `SourceResponseError` a propósito: esto es reintentable
    —la red falló, la fuente está caída o nos está limitando el ritmo— y la UI
    puede ofrecer «volver a intentar».
    """


class SourceResponseError(MarketDataError):
    """La fuente respondió, pero con algo que no encaja con lo que promete.

    Reintentar no arregla esto: o la fuente cambió su formato, o estamos
    leyéndolo mal. El mensaje debe decir qué campo falló, porque el que lo va a
    leer es quien tenga que actualizar el motor.
    """


class UnsupportedOperationError(AmigocomporaError):
    """El motor activo no implementa la capacidad opcional solicitada."""


class UnknownChainError(AmigocomporaError):
    """La red pedida no está en el registro, o no sirve para esta operación.

    Cubre dos casos que el usuario vive igual —«aquí no se puede»— y que
    conviene no separar en dos excepciones: una red que no conocemos, y una red
    que conocemos pero a la que le falta lo que la operación necesita (pedirle
    el chain id EIP-155 a Solana, por ejemplo).
    """


# --------------------------------------------------------------------------- #
# Motores
# --------------------------------------------------------------------------- #
class EngineError(AmigocomporaError):
    """Error relacionado con un motor intercambiable."""


class EngineNotFoundError(EngineError):
    """No existe ningún motor registrado con ese identificador o de ese tipo."""


class EngineConfigError(EngineError):
    """Falta configuración obligatoria para activar el motor."""


class EngineNotReadyError(EngineError):
    """Se usó un motor que no está abierto (`aopen()` no se ha llamado)."""


class NoActiveEngineError(EngineError):
    """No hay motor activo para el tipo solicitado."""
