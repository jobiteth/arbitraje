"""Casos de uso. Cada uno declara la `Capability` que necesita y la exige.

Patrón común: el caso de uso pide autorización al `ConfirmationGateway` antes
de tocar un motor. Nunca consulta el modo activo directamente; eso mantiene la
política en un solo sitio (`domain.modes`).
"""

from __future__ import annotations
