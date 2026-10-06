"""Caso de uso: análisis con copiloto IA sobre datos ya observados.

Etapa 6. Capacidad `QUERY_AI` (disponible en los tres modos).

El LLM no hace I/O por su cuenta: todo el contexto lo aporta quien llama,
a partir de `PriceComparison` / `PredictionMarket` ya obtenidos. Así se
garantiza reproducibilidad y se evita que el modelo alucine datos.
"""

from __future__ import annotations

from dataclasses import dataclass

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import AnalysisRequest, AnalysisResult


@dataclass(frozen=True, slots=True)
class AnalyzeWithAi:
    registry: EngineRegistry
    gateway: ConfirmationGateway
    max_context_chars: int = 8000

    async def __call__(self, request: AnalysisRequest) -> AnalysisResult:
        await self.gateway.authorize(
            Capability.QUERY_AI,
            "Consultar al copiloto de IA",
            details=(f"Pregunta: {request.question[:120]}",),
        )
        # Truncar contexto por valor para no exceder ventana del modelo.
        trimmed = {k: v[: self.max_context_chars] for k, v in request.context.items()}
        engine = self.registry.active_advisor()
        return await engine.analyze(AnalysisRequest(question=request.question, context=trimmed))
