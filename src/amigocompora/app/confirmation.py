"""`ConfirmationGateway` — «la IA propone, el usuario decide», en código.

Toda acción con efectos pasa por aquí y necesita dos cosas:

1. que el modo activo conceda la capacidad (`ModeGuard`), y
2. un «sí» **explícito** del usuario para esta acción concreta.

El prompt por defecto es `DenyAllPrompt`: si nadie ha cableado una UI de
confirmación, todo se deniega. Un fallo de cableado no puede convertirse en una
autorización implícita.

Cada decisión queda registrada en `history`, que es la traza de auditoría que la
UI muestra en el panel de actividad.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import uuid4

from amigocompora.app.mode_guard import ModeGuard
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import ConfirmationDeniedError
from amigocompora.domain.models import PlannedTransaction
from amigocompora.domain.modes import Capability


class Decision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    #: Bloqueada por el modo: nunca llegó a preguntarse al usuario.
    BLOCKED_BY_MODE = "blocked_by_mode"


@dataclass(frozen=True, slots=True)
class PendingAction:
    """Lo que se le propone al usuario, en términos que pueda evaluar.

    `title` y `details` son texto legible, no identificadores: el usuario tiene
    que poder entender qué confirma sin leer código ni calldata.
    """

    action_id: str
    capability: Capability
    title: str
    requested_at: datetime
    details: tuple[str, ...] = ()
    transaction: PlannedTransaction | None = None

    @property
    def is_irreversible(self) -> bool:
        """Si la acción tocaría la red. En M1 siempre `False`: no se firma nada."""
        return self.capability in {Capability.SIGN_TX, Capability.BROADCAST_TX}


@dataclass(frozen=True, slots=True)
class ConfirmationRecord:
    """Entrada de la traza de auditoría."""

    action: PendingAction
    decision: Decision
    decided_at: datetime
    reason: str = ""


@runtime_checkable
class ConfirmationPrompt(Protocol):
    """Quien pregunta al usuario. La implementación real es un diálogo Qt."""

    async def ask(self, action: PendingAction) -> bool: ...


class DenyAllPrompt:
    """Prompt por defecto: deniega todo.

    Es deliberado. Sin UI de confirmación conectada, el sistema debe quedarse
    inservible para acciones con efectos, no permisivo.
    """

    __slots__ = ()

    async def ask(self, action: PendingAction) -> bool:
        return False


@dataclass(slots=True)
class RecordingPrompt:
    """Prompt programable, para tests y para el motor demo."""

    answer: bool = True
    asked: list[PendingAction] = field(default_factory=list)

    async def ask(self, action: PendingAction) -> bool:
        self.asked.append(action)
        return self.answer


class ConfirmationGateway:
    """Autoriza acciones cruzando modo + confirmación explícita."""

    __slots__ = ("_clock", "_guard", "_history", "_prompt")

    def __init__(
        self,
        guard: ModeGuard,
        prompt: ConfirmationPrompt | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._guard = guard
        self._prompt: ConfirmationPrompt = prompt or DenyAllPrompt()
        self._clock = clock or SystemClock()
        self._history: list[ConfirmationRecord] = []

    def set_prompt(self, prompt: ConfirmationPrompt) -> None:
        """Conecta la UI de confirmación una vez construida la ventana."""
        self._prompt = prompt

    @property
    def history(self) -> Sequence[ConfirmationRecord]:
        return tuple(self._history)

    def precheck(self, capability: Capability) -> None:
        """Comprueba sólo el modo, sin preguntar al usuario.

        Existe para ordenar bien los casos de uso que deben *construir* algo
        antes de poder enseñárselo al usuario: primero se descarta el trabajo
        que el modo no permite, y la confirmación se pide después, cuando ya
        hay un payload concreto que mostrar en el diálogo.
        """
        if not self._guard.allows(capability):
            blocked = PendingAction(
                action_id=uuid4().hex,
                capability=capability,
                title=f"Comprobación previa: {capability.label}",
                requested_at=self._clock.now(),
            )
            self._record(blocked, Decision.BLOCKED_BY_MODE, self._guard.explain(capability))
        self._guard.require(capability)

    async def authorize(
        self,
        capability: Capability,
        title: str,
        *,
        details: Sequence[str] = (),
        transaction: PlannedTransaction | None = None,
    ) -> PendingAction:
        """Devuelve la acción autorizada, o lanza.

        Lanza `ModeNotPermittedError` si el modo no concede la capacidad, y
        `ConfirmationDeniedError` si el usuario dice no. Nunca devuelve algo
        «a medias»: si retorna, la acción está autorizada.
        """
        action = PendingAction(
            action_id=uuid4().hex,
            capability=capability,
            title=title,
            requested_at=self._clock.now(),
            details=tuple(details),
            transaction=transaction,
        )

        if not self._guard.allows(capability):
            self._record(action, Decision.BLOCKED_BY_MODE, self._guard.explain(capability))
            self._guard.require(capability)  # lanza ModeNotPermittedError

        if not self._guard.requires_confirmation(capability):
            self._record(action, Decision.APPROVED, "capacidad de sólo lectura")
            return action

        approved = await self._prompt.ask(action)
        if not approved:
            self._record(action, Decision.REJECTED, "el usuario rechazó la acción")
            raise ConfirmationDeniedError(f"acción rechazada por el usuario: {title}")

        self._record(action, Decision.APPROVED, "confirmada por el usuario")
        return action

    def _record(self, action: PendingAction, decision: Decision, reason: str) -> None:
        self._history.append(
            ConfirmationRecord(
                action=action,
                decision=decision,
                decided_at=self._clock.now(),
                reason=reason,
            )
        )
