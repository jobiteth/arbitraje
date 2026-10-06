"""Asistente IA offline: el stub es determinista y siempre disponible."""

from __future__ import annotations

from amigocompora.domain.protocols import AnalysisRequest
from amigocompora.engines.llm.providers import (
    STUB_MANIFEST,
    StubAdvisor,
)


async def test_stub_lifecycle_is_noop() -> None:
    advisor = StubAdvisor()
    await advisor.aopen()
    await advisor.aopen()  # idempotente
    await advisor.aclose()
    await advisor.aclose()


async def test_stub_produces_result_with_disclaimer() -> None:
    advisor = StubAdvisor()
    result = await advisor.analyze(
        AnalysisRequest(question="¿compensa?", context={"spread": "40 bps", "fee": "30 bps"})
    )
    assert result.summary
    assert "no es asesoramiento" in result.disclaimer.lower()


async def test_stub_flags_overround_context() -> None:
    advisor = StubAdvisor()
    result = await advisor.analyze(
        AnalysisRequest(question="q", context={"overround": "mercado con 120 bps"})
    )
    assert any("probabilidades" in finding.lower() for finding in result.findings)


def test_stub_manifest_is_ai_slot() -> None:
    from amigocompora.domain.protocols import EngineKind

    assert STUB_MANIFEST.kind is EngineKind.AI_ADVISOR
    assert STUB_MANIFEST.required_config == ()
    assert STUB_MANIFEST.allowed_hosts == ()
