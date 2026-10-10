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
# Ejecución — cuando la aplicación mueve dinero de verdad
# --------------------------------------------------------------------------- #
class ExecutionError(AmigocomporaError):
    """Raíz de los fallos al firmar o emitir una operación."""


class NoWalletError(ExecutionError):
    """No hay clave privada configurada, así que no hay con qué firmar.

    Se distingue de un fallo de firma: aquí no se intentó nada. El mensaje debe
    decir **dónde** se configura, porque quien lo lee es alguien que acaba de
    pulsar «Ejecutar» y no sabe por qué no pasa nada.
    """


class KeyCustodyError(ExecutionError):
    """La clave privada está en un sitio que no es seguro para una clave privada.

    Existe por una asimetría concreta: una API key filtrada se rota en un minuto,
    y una clave privada filtrada vacía la cartera para siempre. Por eso el
    fallback a variable de entorno —razonable para una API key, y la única vía
    que funciona en una máquina sin keyring— no se acepta aquí sin pedirlo
    explícitamente.
    """


class WalletLockedError(ExecutionError):
    """La cartera activa está cifrada y la sesión no está desbloqueada.

    Es distinto de `NoWalletError` —sí hay cartera, y su clave existe— y de
    `KeyCustodyError` —el almacén no falla—: aquí lo que falta es la contraseña,
    que la escriba quien opera. El mensaje tiene que decir **dónde** se
    desbloquea, porque quien lo lee acaba de pulsar «Ejecutar» y no sabe por qué
    no pasa nada.
    """


class WrongPasswordError(ExecutionError):
    """La contraseña no es la del almacén cifrado de carteras.

    Ni el mensaje ni ningún log citan la contraseña ni material de clave: lo
    único que se puede afirmar es que no abre este almacén.
    """


class WalletExistsError(AmigocomporaError):
    """La cartera que se quiere añadir ya está en el libro.

    No hereda de `ExecutionError` porque no falló ninguna operación: es que la
    dirección ya está guardada, y la salida no es reintentar sino usar la que
    hay o cambiar de clave. El mensaje dice **cuál** es, porque con varias
    carteras un «ya existe» sin decir cuál obliga a buscarla a mano.
    """


class ExecutionLimitExceededError(ExecutionError):
    """La operación no cabe en los límites configurados.

    No es un fallo técnico ni del mercado: es el límite haciendo su trabajo. Se
    lanza desde el dominio **antes** de firmar, y el mensaje dice qué límite y
    con qué cifras, porque la salida —subirlo o reducir la orden— la decide el
    usuario.
    """

    def __init__(self, limit: str, detail: str) -> None:
        self.limit = limit
        self.detail = detail
        super().__init__(f"la operación supera el límite de {limit}: {detail}")


class InsufficientBalanceError(ExecutionError):
    """La cartera no tiene el token que la operación va a entregar.

    No es un fallo técnico: es la cartera enseñando lo que hay. Se comprueba
    **antes** de aprobar y de estimar, porque aprobar un permiso para un swap
    que no se puede pagar gasta gas para nada, y el nodo, al estimar, contesta
    un «execution reverted: STF» que no dice a nadie qué pasa —medido el
    2026-10-10, con pUSD en Polygon: 0.550579 en la cartera y un swap de
    1 pUSD—. El mensaje lleva las dos cifras tal como se ven en la interfaz,
    porque la salida —reducir el importe o traer saldo— la decide el usuario.
    """

    def __init__(self, available: str, required: str) -> None:
        self.available = available
        self.required = required
        super().__init__(
            f"no hay saldo para esta operación: tu cartera tiene {available} y "
            f"la operación mueve {required}. Reduce el importe o trae saldo: no "
            f"se aprobó ni se firmó nada."
        )


class BroadcastError(ExecutionError):
    """La transacción se firmó pero la red no la aceptó.

    Importa distinguirlo de un fallo de firma: aquí **existe** una transacción
    firmada y con un hash conocido, y si el nodo la rechazó por algo transitorio
    puede volver a emitirse la misma. El mensaje lleva el hash para que se pueda
    comprobar en un explorador en vez de tener que creer a la aplicación.
    """

    def __init__(self, tx_hash: str, chain: str, reason: str) -> None:
        self.tx_hash = tx_hash
        self.chain = chain
        self.reason = reason
        super().__init__(
            f"la red «{chain}» rechazó la transacción {tx_hash}: {reason}. "
            f"La transacción está firmada: se puede reintentar la misma."
        )


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


class QuoteMovedError(MarketDataError):
    """El precio se movió entre lo que el usuario vio y lo que se iba a construir.

    No es un fallo técnico ni de la fuente: es el mercado siendo el mercado. Se
    lanza **antes** de construir el payload, porque confirmar una operación con
    un precio distinto al que se mostró es exactamente lo que este producto no
    puede hacer. La salida es volver a cotizar y decidir sobre la cifra nueva.
    """

    def __init__(self, *, shown: str, fresh: str, drift_bps: int, tolerance_bps: int) -> None:
        self.shown = shown
        self.fresh = fresh
        self.drift_bps = drift_bps
        self.tolerance_bps = tolerance_bps
        super().__init__(
            f"El precio se movió {drift_bps} bps desde la cotización que viste "
            f"({shown} → {fresh}), más de los {tolerance_bps} bps tolerados. No se "
            f"construyó nada: vuelve a cotizar y decide sobre el precio nuevo."
        )


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
