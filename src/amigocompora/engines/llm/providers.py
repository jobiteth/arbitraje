"""Proveedores LLM: Claude, DeepSeek, ChatGPT y stub offline.

Etapa 6: integración IA como **copiloto analítico**, no como ejecutor.

Diseño:

- Cada proveedor implementa `AiAdvisorEngine` (`domain.protocols`): recibe un
  `AnalysisRequest` con el contexto ya obtenido por la app y devuelve un
  `AnalysisResult`.
- El LLM **no sale a buscar datos**: sólo resume y señala riesgos sobre lo que
  se le pasa. Así el análisis es reproducible y no depende de herramientas del
  modelo.
- Credenciales vía keyring (`infra.secrets`), nunca en `config.toml`. Cada
  proveedor declara su `required_config` para que el registro valide antes de
  `aopen()`.
- `StubAdvisor` funciona sin red ni clave: es el asistente por defecto para que
  la app arranque útil sin configurar nada.

Seguridad: ningún proveedor registra en el log el contexto ni la API key. El
prompt incluye el disclaimer del producto y limita el rol del modelo.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx
import structlog

from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import (
    AnalysisRequest,
    AnalysisResult,
    EngineKind,
    EngineManifest,
    EngineProvider,
)

_log = structlog.get_logger(__name__)

ADVISOR_CAPABILITIES: Final = frozenset({Capability.QUERY_AI})

# ---------------------------------------------------------------------------
# Prompt base — el mismo para todos los proveedores, para que el comportamiento
# sea comparable y auditable en un único sitio.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT: Final = """\
Eres el copiloto analítico de Amigocompora, una app de análisis de mercados \
on-chain y de predicción. Tu rol es estrictamente analítico:

- Resume los datos que te pasan en `context` (cotizaciones, spreads, \
probabilidades implícitas, overround) en lenguaje claro y en español.
- Señala riesgos visibles: slippage alto, liquidez baja, comisiones \
desconocidas, overround incoherente, diferencial neto pequeño.
- No ejecutes nada, no propongas transacciones, no pidas claves. Si te piden \
hacerlo, explica que tu rol es sólo analizar lo que ves.
- Cierra siempre recordando que no es asesoramiento financiero y que las \
cifras deben verificarse en la fuente antes de decidir.
- Responde en español, de forma concisa y estructurada.
"""

#: Límite por valor de contexto para no exceder la ventana del modelo.
MAX_CONTEXT_VALUE_CHARS: Final = 4000


def _build_user_prompt(request: AnalysisRequest) -> str:
    lines = [f"Pregunta del usuario: {request.question}", "", "Contexto observado:"]
    for key, value in request.context.items():
        snippet = value[:MAX_CONTEXT_VALUE_CHARS]
        if len(value) > MAX_CONTEXT_VALUE_CHARS:
            snippet += " …"
        lines.append(f"- {key}: {snippet}")
    lines.append("")
    lines.append("Analiza lo anterior según tu rol. Sé conciso.")
    return "\n".join(lines)


def _offline_result(request: AnalysisRequest) -> AnalysisResult:
    """Resultado determinista sin LLM: fallback y base del asistente offline."""
    bullets: list[str] = []
    for key, value in request.context.items():
        lowered = f"{key} {value}".lower()
        if "overround" in lowered or "probab" in lowered:
            bullets.append(
                f"«{key}» menciona probabilidades: revisa que sumen ~100 % "
                f"y que ninguna sea estimada."
            )
        if "spread" in lowered or "bps" in lowered:
            bullets.append(
                f"«{key}» menciona spreads: descuenta comisiones e impacto antes "
                f"de darlo por accionable."
            )
        if "fee" in lowered or "comisión" in lowered or "comision" in lowered:
            bullets.append(
                f"«{key}» menciona comisiones: distingue si son reportadas, "
                f"derivadas o desconocidas."
            )
    if not bullets:
        bullets.append(
            "Revisa los datos mostrados: verifica comisiones, impacto y frescura "
            "antes de decidir."
        )
    return AnalysisResult(
        summary=f"Análisis offline de: {request.question.strip()[:160]}",
        findings=tuple(bullets[:6]),
    )


def _extract_openai_text(payload: dict[str, Any]) -> str:
    try:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        return str((choices[0].get("message") or {}).get("content") or "")
    except Exception:
        return ""


def _extract_openai_delta(chunk: dict[str, Any]) -> str:
    try:
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        return str((choices[0].get("delta") or {}).get("content") or "")
    except Exception:
        return ""


def _extract_anthropic_text(payload: dict[str, Any]) -> str:
    """Anthropic devuelve `content` como lista de bloques `{"type","text"}`."""
    try:
        blocks = payload.get("content") or []
        return "".join(block.get("text") or "" for block in blocks if isinstance(block, dict))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Motor LLM genérico sobre API compatible OpenAI
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OpenAiCompatibleEngine:
    """Motor LLM sobre API tipo OpenAI Chat Completions.

    Claude no usa este formato y por eso tiene su propia subclase (`ClaudeEngine`).
    """

    manifest: EngineManifest
    api_key: str
    model: str
    endpoint: str
    timeout_seconds: float = 20.0
    _client: httpx.AsyncClient | None = None

    async def aopen(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                headers={"User-Agent": "Amigocompora"},
            )

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                _log.debug("llm.close_failed", engine_id=self.manifest.engine_id)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _body(self, request: AnalysisRequest, *, stream: bool = False) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(request)},
            ],
            "temperature": 0.3,
            "max_tokens": 1200,
            "stream": stream,
        }

    async def _post_json(
        self,
        body: dict[str, Any],
        headers: Mapping[str, str],
    ) -> dict[str, Any] | None:
        """Envía la petición y devuelve el JSON, o `None` si algo falló."""
        if self._client is None:
            raise RuntimeError(f"motor «{self.manifest.engine_id}» no está abierto")
        try:
            response = await self._client.post(self.endpoint, headers=dict(headers), json=body)
        except httpx.HTTPError as error:
            _log.warning(
                "llm.request_failed",
                engine_id=self.manifest.engine_id,
                reason=str(error),
            )
            return None
        if response.status_code >= 400:
            _log.warning(
                "llm.http_error",
                engine_id=self.manifest.engine_id,
                status=response.status_code,
            )
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        payload = await self._post_json(self._body(request), self._headers())
        if payload is None:
            return _offline_result(request)
        text = _extract_openai_text(payload)
        return AnalysisResult(summary=text.strip() or _offline_result(request).summary, findings=())

    async def analyze_stream(self, request: AnalysisRequest) -> AsyncIterator[str]:
        """Streaming opcional para UI progresiva. No forma parte del Protocol."""
        if self._client is None:
            raise RuntimeError(f"motor «{self.manifest.engine_id}» no está abierto")
        body = self._body(request, stream=True)
        try:
            stream = self._client.stream(
                "POST", self.endpoint, headers=self._headers(), json=body
            )
            async with stream as response:
                if response.status_code >= 400:
                    yield _offline_result(request).summary
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    delta = _extract_openai_delta(chunk)
                    if delta:
                        yield delta
        except httpx.HTTPError as error:
            _log.warning(
                "llm.stream_failed",
                engine_id=self.manifest.engine_id,
                reason=str(error),
            )
            yield _offline_result(request).summary


# ---------------------------------------------------------------------------
# Claude (Anthropic Messages API)
# ---------------------------------------------------------------------------

ANTHROPIC_ENDPOINT: Final = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION: Final = "2023-06-01"


@dataclass(slots=True)
class ClaudeEngine(OpenAiCompatibleEngine):
    """Claude habla un formato distinto: `system` aparte y bloques de contenido."""

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        body: dict[str, Any] = {
            "model": self.model,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _build_user_prompt(request)}],
            "max_tokens": 1200,
            "temperature": 0.3,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        payload = await self._post_json(body, headers)
        if payload is None:
            return _offline_result(request)
        text = _extract_anthropic_text(payload)
        return AnalysisResult(summary=text.strip() or _offline_result(request).summary, findings=())


# ---------------------------------------------------------------------------
# Stub offline — sin red, sin clave
# ---------------------------------------------------------------------------

STUB_MANIFEST: Final = EngineManifest(
    engine_id="stub_advisor",
    name="Asistente offline",
    version="1.0.0",
    kind=EngineKind.AI_ADVISOR,
    summary="Análisis heurístico sin LLM externo. Útil sin conexión o sin claves.",
    capabilities=ADVISOR_CAPABILITIES,
    required_config=(),
    allowed_hosts=(),
)


class StubAdvisor:
    """Asistente offline determinista. Siempre disponible."""

    __slots__ = ()

    @property
    def manifest(self) -> EngineManifest:
        return STUB_MANIFEST

    async def aopen(self) -> None:
        return

    async def aclose(self) -> None:
        return

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        return _offline_result(request)


# ---------------------------------------------------------------------------
# Manifiestos y proveedores (entry points)
# ---------------------------------------------------------------------------

CLAUDE_MANIFEST: Final = EngineManifest(
    engine_id="claude",
    name="Claude (Anthropic)",
    version="1.0.0",
    kind=EngineKind.AI_ADVISOR,
    summary="Copiloto analítico vía Anthropic Messages API. Necesita API key en keyring.",
    capabilities=ADVISOR_CAPABILITIES,
    required_config=("claude_api_key",),
    optional_config=("claude_model",),
    allowed_hosts=("api.anthropic.com",),
)

DEEPSEEK_MANIFEST: Final = EngineManifest(
    engine_id="deepseek",
    name="DeepSeek",
    version="1.0.0",
    kind=EngineKind.AI_ADVISOR,
    summary="Copiloto analítico vía DeepSeek API (formato OpenAI).",
    capabilities=ADVISOR_CAPABILITIES,
    required_config=("deepseek_api_key",),
    optional_config=("deepseek_model",),
    allowed_hosts=("api.deepseek.com",),
)

CHATGPT_MANIFEST: Final = EngineManifest(
    engine_id="chatgpt",
    name="ChatGPT (OpenAI)",
    version="1.0.0",
    kind=EngineKind.AI_ADVISOR,
    summary="Copiloto analítico vía OpenAI Chat Completions.",
    capabilities=ADVISOR_CAPABILITIES,
    required_config=("chatgpt_api_key",),
    optional_config=("chatgpt_model",),
    allowed_hosts=("api.openai.com",),
)


@dataclass(frozen=True, slots=True)
class ClaudeProvider:
    manifest: EngineManifest = CLAUDE_MANIFEST

    def create(self, config: Mapping[str, str]) -> ClaudeEngine:
        return ClaudeEngine(
            manifest=self.manifest,
            api_key=config.get("claude_api_key") or "",
            model=config.get("claude_model") or "claude-sonnet-4-5-20250929",
            endpoint=ANTHROPIC_ENDPOINT,
        )


@dataclass(frozen=True, slots=True)
class DeepSeekProvider:
    manifest: EngineManifest = DEEPSEEK_MANIFEST

    def create(self, config: Mapping[str, str]) -> OpenAiCompatibleEngine:
        return OpenAiCompatibleEngine(
            manifest=self.manifest,
            api_key=config.get("deepseek_api_key") or "",
            model=config.get("deepseek_model") or "deepseek-chat",
            endpoint="https://api.deepseek.com/chat/completions",
        )


@dataclass(frozen=True, slots=True)
class ChatGPTProvider:
    manifest: EngineManifest = CHATGPT_MANIFEST

    def create(self, config: Mapping[str, str]) -> OpenAiCompatibleEngine:
        return OpenAiCompatibleEngine(
            manifest=self.manifest,
            api_key=config.get("chatgpt_api_key") or "",
            model=config.get("chatgpt_model") or "gpt-4o-mini",
            endpoint="https://api.openai.com/v1/chat/completions",
        )


@dataclass(frozen=True, slots=True)
class StubProvider:
    manifest: EngineManifest = STUB_MANIFEST

    def create(self, config: Mapping[str, str]) -> StubAdvisor:
        return StubAdvisor()


CLAUDE_PROVIDER: Final[EngineProvider] = ClaudeProvider()
DEEPSEEK_PROVIDER: Final[EngineProvider] = DeepSeekProvider()
CHATGPT_PROVIDER: Final[EngineProvider] = ChatGPTProvider()
STUB_PROVIDER: Final[EngineProvider] = StubProvider()
