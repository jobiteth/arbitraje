"""Modos de operación y capacidades — la barrera de seguridad del producto.

Materializa el principio rector del spec: *la IA propone, el usuario decide*.

El modelo es declarativo a propósito. Un caso de uso no pregunta «¿en qué modo
estoy?»; declara la `Capability` que necesita y `ModeGuard` decide. Así la
política vive en un único sitio auditable (la tabla `MODE_CAPABILITIES`) en
lugar de repartida en condicionales por toda la aplicación.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final


class Capability(StrEnum):
    """Acción que un caso de uso necesita poder realizar."""

    #: Leer estado público de una cadena o API (balances, pools, mercados).
    READ_CHAIN = "read_chain"
    #: Calcular rutas, quotes, slippage y price impact de forma local.
    COMPUTE_ROUTE = "compute_route"
    #: Consultar un asistente de IA con datos ya obtenidos.
    QUERY_AI = "query_ai"
    #: Construir el payload de una transacción **sin firmar**.
    PREPARE_TX = "prepare_tx"
    #: Firmar una transacción con una clave.
    #:
    #: A diferencia de las anteriores, ésta mueve dinero de verdad: la firma ya
    #: es un instrumento al portador, y quien la tenga puede emitirla. Por eso
    #: sólo la concede `EXECUTION` y por eso va con límites duros.
    SIGN_TX = "sign_tx"
    #: Emitir una transacción a la red.
    #:
    #: Es la irreversible: a partir de aquí la operación existe y no se
    #: deshace. Es la que se confirma siempre, salvo que la política de
    #: autonomía esté armada y la operación quepa en los límites.
    BROADCAST_TX = "broadcast_tx"

    @property
    def label(self) -> str:
        return _CAPABILITY_LABELS[self]


class OperationMode(StrEnum):
    """Modo de operación activo. El valor es el slug estable para config."""

    OBSERVATION = "observation"
    SIMULATION = "simulation"
    ASSISTED = "assisted"
    EXECUTION = "execution"

    @property
    def label(self) -> str:
        """Nombre en la UI, tal y como lo define el spec."""
        return _MODE_LABELS[self]

    @property
    def description(self) -> str:
        return _MODE_DESCRIPTIONS[self]

    @property
    def capabilities(self) -> frozenset[Capability]:
        return MODE_CAPABILITIES[self]

    def grants(self, capability: Capability) -> bool:
        return capability in MODE_CAPABILITIES[self]


#: **La tabla de política.** Única fuente de verdad sobre qué permite cada modo.
#: Cualquier cambio aquí debe ir acompañado de su caso en la matriz de
#: `tests/unit/test_modes.py`, que además comprueba que la tabla es **monótona**:
#: cada modo concede todo lo del anterior. Subir de modo no puede quitar nada, y
#: si alguien rompiera esa propiedad al añadir una columna, ese test lo dice.
MODE_CAPABILITIES: Final[Mapping[OperationMode, frozenset[Capability]]] = {
    OperationMode.OBSERVATION: frozenset(
        {
            Capability.READ_CHAIN,
            Capability.QUERY_AI,
        }
    ),
    OperationMode.SIMULATION: frozenset(
        {
            Capability.READ_CHAIN,
            Capability.QUERY_AI,
            Capability.COMPUTE_ROUTE,
        }
    ),
    OperationMode.ASSISTED: frozenset(
        {
            Capability.READ_CHAIN,
            Capability.QUERY_AI,
            Capability.COMPUTE_ROUTE,
            Capability.PREPARE_TX,
        }
    ),
    #: El único modo que mueve dinero. Todo lo de `ASSISTED` más firmar y
    #: emitir, que es lo que convierte un payload en una operación.
    OperationMode.EXECUTION: frozenset(
        {
            Capability.READ_CHAIN,
            Capability.QUERY_AI,
            Capability.COMPUTE_ROUTE,
            Capability.PREPARE_TX,
            Capability.SIGN_TX,
            Capability.BROADCAST_TX,
        }
    ),
}

#: Modo por defecto al arrancar: el menos capaz. Subir de modo es siempre un
#: acto explícito del usuario.
DEFAULT_MODE: Final = OperationMode.OBSERVATION

#: Capacidades que, además de estar concedidas por el modo, exigen pasar por
#: `ConfirmationGateway` y recibir una confirmación explícita del usuario.
#:
#: `SIGN_TX` **no** está aquí, y es deliberado. Firmar sin emitir no tiene efecto
#: por sí mismo dentro del flujo de la aplicación: la firma va de la mano al
#: envío, sin pasar por el usuario ni por disco. Pedir dos síes para una sola
#: operación no añade seguridad —añade un diálogo más que se aprende a cerrar sin
#: leer— y gasta la atención donde de verdad hace falta, que es la confirmación
#: de emitir. Ésa sí está, y es la que muestra el hash firmado y los importes.
CONFIRMABLE_CAPABILITIES: Final = frozenset(
    {
        Capability.PREPARE_TX,
        Capability.BROADCAST_TX,
    }
)


_MODE_LABELS: Final[Mapping[OperationMode, str]] = {
    OperationMode.OBSERVATION: "OBSERVACIÓN",
    OperationMode.SIMULATION: "SIMULACIÓN",
    OperationMode.ASSISTED: "ASISTIDO",
    OperationMode.EXECUTION: "EJECUCIÓN",
}

_MODE_DESCRIPTIONS: Final[Mapping[OperationMode, str]] = {
    OperationMode.OBSERVATION: (
        "Sólo lectura. Consulta datos de mercado; no calcula rutas ni prepara nada."
    ),
    OperationMode.SIMULATION: (
        "Lectura y cálculo local de rutas, slippage y price impact. No prepara transacciones."
    ),
    OperationMode.ASSISTED: (
        "Puede preparar transacciones sin firmar. Cada una requiere tu confirmación "
        "explícita; la aplicación no las firma ni las emite."
    ),
    OperationMode.EXECUTION: (
        "Firma y emite transacciones reales e irreversibles con la cartera que "
        "configures. Cada operación se confirma, salvo que la ejecución desatendida "
        "esté armada y la operación quepa en los límites."
    ),
}

_CAPABILITY_LABELS: Final[Mapping[Capability, str]] = {
    Capability.READ_CHAIN: "leer datos de mercado",
    Capability.COMPUTE_ROUTE: "calcular rutas y precios",
    Capability.QUERY_AI: "consultar al asistente de IA",
    Capability.PREPARE_TX: "preparar una transacción sin firmar",
    Capability.SIGN_TX: "firmar una transacción",
    Capability.BROADCAST_TX: "emitir una transacción a la red",
}


def modes_granting(capability: Capability) -> frozenset[OperationMode]:
    """Modos que conceden `capability`. Útil para que la UI sugiera a cuál subir."""
    return frozenset(mode for mode in OperationMode if mode.grants(capability))
