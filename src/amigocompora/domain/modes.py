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
    #: Firmar una transacción con una clave. No implementado.
    SIGN_TX = "sign_tx"
    #: Emitir una transacción a la red. No implementado.
    BROADCAST_TX = "broadcast_tx"

    @property
    def label(self) -> str:
        return _CAPABILITY_LABELS[self]


class OperationMode(StrEnum):
    """Modo de operación activo. El valor es el slug estable para config."""

    OBSERVATION = "observation"
    SIMULATION = "simulation"
    ASSISTED = "assisted"

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
#: Cualquier cambio aquí debe ir acompañado de su caso en la matriz exhaustiva
#: de `tests/unit/test_mode_guard.py`.
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
}

#: Modo por defecto al arrancar: el menos capaz. Subir de modo es siempre un
#: acto explícito del usuario.
DEFAULT_MODE: Final = OperationMode.OBSERVATION

#: Capacidades que **ningún** modo concede en esta versión. Firmar y emitir
#: transacciones no está implementado; dejarlo explícito permite que un test
#: detecte si alguien las añade a la tabla sin revisar la barrera.
UNIMPLEMENTED_CAPABILITIES: Final = frozenset(
    {
        Capability.SIGN_TX,
        Capability.BROADCAST_TX,
    }
)

#: Capacidades que, además de estar concedidas por el modo, exigen pasar por
#: `ConfirmationGateway` y recibir una confirmación explícita del usuario.
CONFIRMABLE_CAPABILITIES: Final = frozenset(
    {
        Capability.PREPARE_TX,
        Capability.SIGN_TX,
        Capability.BROADCAST_TX,
    }
)


_MODE_LABELS: Final[Mapping[OperationMode, str]] = {
    OperationMode.OBSERVATION: "OBSERVACIÓN",
    OperationMode.SIMULATION: "SIMULACIÓN",
    OperationMode.ASSISTED: "ASISTIDO",
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
        "explícita; la aplicación nunca firma ni emite."
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
