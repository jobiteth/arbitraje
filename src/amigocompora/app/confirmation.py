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
    #: Aprobada **sin** preguntar, por la política de autonomía. Es un valor
    #: propio y no un `APPROVED` con una nota: quien lea la traza tiene que poder
    #: distinguir de un vistazo lo que una persona autorizó de lo que una máquina
    #: hizo por su cuenta, y eso no puede depender de interpretar un texto libre.
    APPROVED_BY_POLICY = "approved_by_policy"


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
        """Si la acción tocaría la red de verdad, sin vuelta atrás.

        Con el modo `EJECUCIÓN` esto pasa a ser `True` —y el diálogo tiene que
        decirlo—, porque desde ahí sí se firma y se emite. Dejar aquí el
        comentario de una versión anterior en la que nunca había nada
        irreversible sería exactamente la clase de mentira que importa: la que
        se lee justo en el momento de decidir.
        """
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


@runtime_checkable
class ConfirmationBypass(Protocol):
    """Quien puede decidir que no hace falta preguntar.

    El protocolo vive aquí, junto a quien lo consulta, y no en la política de
    autonomía que lo implementa: así `app.confirmation` no importa
    `app.execution_policy` —que sí importa a este módulo— y no hay ciclo.

    Devolver un motivo significa «adelante sin preguntar»; `None` significa
    «pregunta». **Un bypass no puede conceder una capacidad**: sólo puede
    ahorrarse la pregunta, y por eso se consulta después de la barrera de modo
    y nunca antes.
    """

    def reason_to_skip(self, action: PendingAction) -> str | None: ...


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

    __slots__ = ("_bypass", "_clock", "_guard", "_history", "_prompt")

    def __init__(
        self,
        guard: ModeGuard,
        prompt: ConfirmationPrompt | None = None,
        clock: Clock | None = None,
        bypass: ConfirmationBypass | None = None,
    ) -> None:
        self._guard = guard
        self._prompt: ConfirmationPrompt = prompt or DenyAllPrompt()
        self._clock = clock or SystemClock()
        self._history: list[ConfirmationRecord] = []
        # Por omisión **no hay bypass**, y eso no es un descuido: `None` aquí
        # significa que la única vía de aprobar algo es que una persona lo
        # apruebe. Cablear la autonomía es un acto explícito.
        self._bypass: ConfirmationBypass | None = bypass

    def set_bypass(self, bypass: ConfirmationBypass | None) -> None:
        """Conecta (o desconecta) la política de autonomía.

        Se puede desconectar en caliente —con `None`— porque desarmar tiene que
        ser siempre posible sin reconstruir el grafo de objetos.
        """
        self._bypass = bypass

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

        ### El orden de las tres comprobaciones es la salvaguarda

        Primero el modo, después el bypass, y sólo entonces se pregunta. El
        bypass va en medio a propósito: consultado antes, podría aprobar algo
        que el modo no concede y la elección del usuario sobre en qué modo está
        la aplicación sería decorativa; consultado después de preguntar, no
        serviría de nada. Aquí sólo puede saltarse **la pregunta**.
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

        if self._bypass is not None:
            reason = self._bypass.reason_to_skip(action)
            if reason is not None:
                # Se registra **siempre**, y con un valor de decisión propio: una
                # operación que se hizo sin que nadie la mirara tiene que quedar
                # contada como tal, no confundida con una confirmación humana.
                self._record(action, Decision.APPROVED_BY_POLICY, reason)
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
