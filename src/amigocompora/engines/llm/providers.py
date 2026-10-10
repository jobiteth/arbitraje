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
from amigocompora.infra.http import build_client

_log = structlog.get_logger(__name__)

#: Los motivos de fallo se enseñan en el chat: se recortan para que quepan en
#: una burbuja sin dejar de llevar el diagnóstico dentro.
MAX_REASON_CHARS: Final = 200

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
    """Resultado determinista sin LLM: es el asistente offline.

    Sólo lo usa `StubAdvisor`. Un proveedor con API **no** cae aquí cuando falla:
    un fallo se dice con su motivo (ver `_sin_respuesta`), porque un texto que
    ningún modelo escribió no puede ocupar el sitio de una respuesta.
    """
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


def _motivo_de_fallo(error: httpx.HTTPError, *, timeout_seconds: float) -> str:
    """El motivo de un fallo de red, en una línea que quepa en el chat.

    Los fallos de TLS llegan como `ConnectError` con el texto de OpenSSL dentro,
    y ese texto **es** el diagnóstico —«certificate verify failed» explica por sí
    solo por qué el modelo no contestó—, así que se conserva; lo que se recorta
    es la longitud.
    """
    if isinstance(error, httpx.TimeoutException):
        return f"no contestó en {timeout_seconds:g} s"
    texto = str(error).strip() or type(error).__name__
    if len(texto) <= MAX_REASON_CHARS:
        return texto
    return f"{texto[:MAX_REASON_CHARS]}…"


def _sin_respuesta(*, nombre: str, motivo: str) -> AnalysisResult:
    """Lo que se devuelve cuando el modelo no contestó: el motivo, no un análisis.

    Antes caía aquí el respaldo offline, que copiaba el principio de la pregunta
    como si fuera una respuesta. Con el protocolo del copiloto dentro de esa
    pregunta, lo que se leía en el chat era el protocolo en JSON: la pantalla
    decía «Copiloto» y debajo había un texto que ningún modelo había escrito. Un
    fallo puede dejarse ver —y aquí se ve, con su motivo—; lo que no puede es
    disfrazarse de respuesta. Sin advertencia: no hay análisis del que avisar.

    Quien llama ya dejó el fallo en el registro con su motor y su causa
    (`llm.request_failed`, `llm.http_error`, `llm.empty_completion`).
    """
    return AnalysisResult(
        summary=(
            f"El modelo «{nombre}» no respondió: {motivo}.\n\n"
            "No hay análisis que enseñar. Comprueba la clave y la conexión en la "
            "pestaña Motores —o elige otro modelo— y vuelve a intentarlo."
        ),
        disclaimer="",
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


def _result_from_text(text: str, *, engine_id: str, nombre: str) -> AnalysisResult:
    """Lo que dijo el modelo; si no dijo nada, se dice por qué.

    El respaldo offline **no** cabe aquí. Medido el 2026-10-07: los modelos de
    DeepSeek razonan antes de escribir, y con el tope de tokens corto se lo
    gastan entero en razonar y devuelven `content` vacío con un 200. Eso daba un
    análisis sin una sola línea del modelo, idéntico en apariencia a uno bueno,
    y sin nada en el registro que explicara por qué. El aviso deja la causa
    escrita y el texto lo dice con el nombre del motor delante.
    """
    limpio = text.strip()
    if limpio:
        return AnalysisResult(summary=limpio, findings=())
    _log.warning(
        "llm.empty_completion",
        engine_id=engine_id,
        hint=(
            "la respuesta llegó vacía. Si el modelo razona, suele ser el tope de "
            "tokens consumido en razonar."
        ),
    )
    return _sin_respuesta(nombre=nombre, motivo="la respuesta llegó con el texto vacío")


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
            # El mismo cliente que usa el resto de motores: el contexto TLS de la
            # app —almacén del sistema **y** certifi— y los hosts del manifiesto.
            # Un `httpx.AsyncClient` a secas verifica sólo contra certifi, y en
            # una máquina cuyo antivirus intercepta el TLS eso es
            # `CERTIFICATE_VERIFY_FAILED`: el copiloto no contesta y el motivo no
            # está en ninguna parte. Medido el 2026-10-10 con DeepSeek.
            self._client = build_client(
                allowed_hosts=self.manifest.allowed_hosts,
                timeout_seconds=self.timeout_seconds,
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

    def _provider_extras(self) -> dict[str, Any]:
        """Parámetros que sólo entiende un proveedor concreto.

        Existe para que un proveedor con sus propias maneras no contamine el
        cuerpo que se manda a los demás: el mismo `OpenAiCompatibleEngine` habla
        con ChatGPT, y mandarle a OpenAI un campo que sólo existe en DeepSeek no
        es un campo ignorado, es un 400 y una llamada perdida. Por omisión no se
        añade nada, y quien tenga algo que añadir lo hereda y lo sobrescribe —
        la misma forma que ya usa `ClaudeEngine` para cambiar el cuerpo entero.
        """
        return {}

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
            **self._provider_extras(),
        }

    async def _post_json(
        self,
        body: dict[str, Any],
        headers: Mapping[str, str],
    ) -> tuple[dict[str, Any] | None, str]:
        """Envía la petición y devuelve el JSON y, si no lo hubo, el motivo.

        El motivo se devuelve en vez de quedarse en el registro porque acaba en
        la pantalla: es la diferencia entre «el copiloto no contesta» y «el
        copiloto no contesta porque el certificado no se pudo verificar».
        """
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
            return None, _motivo_de_fallo(error, timeout_seconds=self.timeout_seconds)
        if response.status_code >= 400:
            _log.warning(
                "llm.http_error",
                engine_id=self.manifest.engine_id,
                status=response.status_code,
            )
            return None, f"la API respondió HTTP {response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            _log.warning("llm.bad_payload", engine_id=self.manifest.engine_id, reason="no json")
            return None, "la respuesta no era JSON"
        if not isinstance(payload, dict):
            _log.warning(
                "llm.bad_payload",
                engine_id=self.manifest.engine_id,
                reason=type(payload).__name__,
            )
            return None, "la respuesta no era un objeto JSON"
        return payload, ""

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        payload, motivo = await self._post_json(self._body(request), self._headers())
        if payload is None:
            return _sin_respuesta(nombre=self.manifest.name, motivo=motivo)
        return _result_from_text(
            _extract_openai_text(payload),
            engine_id=self.manifest.engine_id,
            nombre=self.manifest.name,
        )

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
                    yield _sin_respuesta(
                        nombre=self.manifest.name,
                        motivo=f"la API respondió HTTP {response.status_code}",
                    ).summary
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
            yield _sin_respuesta(
                nombre=self.manifest.name,
                motivo=_motivo_de_fallo(error, timeout_seconds=self.timeout_seconds),
            ).summary


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
        payload, motivo = await self._post_json(body, headers)
        if payload is None:
            return _sin_respuesta(nombre=self.manifest.name, motivo=motivo)
        return _result_from_text(
            _extract_anthropic_text(payload),
            engine_id=self.manifest.engine_id,
            nombre=self.manifest.name,
        )


# ---------------------------------------------------------------------------
# DeepSeek
# ---------------------------------------------------------------------------

#: Endpoint medido el 2026-10-07 contra la API en vivo. Sin `/v1`: el catálogo
#: de modelos responde igual en `https://api.deepseek.com/v1/models`, pero la
#: ruta de chat que documenta el proveedor es esta y es la que contesta.
DEEPSEEK_ENDPOINT: Final = "https://api.deepseek.com/chat/completions"

#: Modelo por omisión, **medido**: `GET /models` sirve hoy exactamente dos,
#: `deepseek-flash` y `deepseek-v4-pro`. El código traía `deepseek-chat` escrito
#: a mano, que la API sigue aceptando pero **aliasa en silencio** —contesta 200 y
#: devuelve `model: deepseek-flash`—, así que apuntar al alias es pedir un modelo
#: que ya no está en el catálogo y enterarse sólo cuando lo retiren.
#:
#: De los dos, éste: `deepseek-v4-pro` gasta `reasoning_tokens` antes de escribir
#: nada (en una prueba corta, 37 de 40 tokens se fueron en razonar), y para
#: resumir unas cifras que ya vienen calculadas eso es pagar por pensar de más.
DEEPSEEK_DEFAULT_MODEL: Final = "deepseek-flash"


@dataclass(slots=True)
class DeepSeekEngine(OpenAiCompatibleEngine):
    """DeepSeek, con el razonamiento apagado a propósito.

    Los dos modelos que sirve la API hoy **razonan antes de escribir**: gastan
    `reasoning_tokens` en un `reasoning_content` aparte y sólo después llenan
    `content`. Medido el 2026-10-07 con el cuerpo exacto de este motor y un
    contexto de tres líneas:

    | petición | desenlace |
    |---|---|
    | `max_tokens=1200` (lo que se mandaba) | `length`, **`content` vacío**, 1200 tok. |
    | `max_tokens=8000` | contesta, pero 4667 tokens en total, 3895 razonando |
    | `thinking: {"type": "disabled"}` | contesta, **772 tokens**, ninguno razonando |
    | `thinking: false` | HTTP 422: el campo no acepta un booleano |
    | `reasoning_effort: "low"` | `length`, `content` vacío — no basta con bajarlo |

    O sea que el motor, tal y como estaba, **no podía contestar nunca**: se le
    acababa el presupuesto pensando antes de escribir la primera palabra. Y el
    fallo era mudo, porque una respuesta de 200 con el texto vacío caía al
    respaldo offline sin decir por qué. Ahora ese silencio se registra
    (`llm.empty_completion`), que es como se encontró esto.

    Se apaga en vez de subir el tope porque el trabajo que se le pide es resumir
    unas cifras que ya vienen calculadas: razonar sobre ellas cuesta seis veces
    más y no mejora la respuesta.
    """

    def _provider_extras(self) -> dict[str, Any]:
        return {"thinking": {"type": "disabled"}}


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

# Las opciones se llaman `api_key` y `model`, **sin** el nombre del motor
# delante, y eso no es cosmética: `env_var_name` compone la variable como
# `AMIGOCOMPORA_ENGINE_<engine_id>_<option>` (infra/secrets.py), así que
# `deepseek_api_key` daba `AMIGOCOMPORA_ENGINE_DEEPSEEK_DEEPSEEK_API_KEY`
# —el nombre repetido— mientras `lifi` y `relay`, que usan `api_key` a secas,
# daban `…_LIFI_API_KEY`. El nombre del motor ya está en la variable; repetirlo
# dentro de la opción sólo servía para que la misma idea se escribiera de dos
# formas según el motor. La clave del keyring queda como `deepseek:api_key`.
CONFIG_API_KEY: Final = "api_key"
CONFIG_MODEL: Final = "model"

CLAUDE_MANIFEST: Final = EngineManifest(
    engine_id="claude",
    name="Claude (Anthropic)",
    version="1.0.0",
    kind=EngineKind.AI_ADVISOR,
    summary="Copiloto analítico vía Anthropic Messages API. Necesita API key en keyring.",
    capabilities=ADVISOR_CAPABILITIES,
    required_config=(CONFIG_API_KEY,),
    optional_config=(CONFIG_MODEL,),
    allowed_hosts=("api.anthropic.com",),
)

DEEPSEEK_MANIFEST: Final = EngineManifest(
    engine_id="deepseek",
    name="DeepSeek",
    version="1.0.0",
    kind=EngineKind.AI_ADVISOR,
    summary="Copiloto analítico vía DeepSeek API (formato OpenAI).",
    capabilities=ADVISOR_CAPABILITIES,
    required_config=(CONFIG_API_KEY,),
    optional_config=(CONFIG_MODEL,),
    allowed_hosts=("api.deepseek.com",),
)

CHATGPT_MANIFEST: Final = EngineManifest(
    engine_id="chatgpt",
    name="ChatGPT (OpenAI)",
    version="1.0.0",
    kind=EngineKind.AI_ADVISOR,
    summary="Copiloto analítico vía OpenAI Chat Completions.",
    capabilities=ADVISOR_CAPABILITIES,
    required_config=(CONFIG_API_KEY,),
    optional_config=(CONFIG_MODEL,),
    allowed_hosts=("api.openai.com",),
)


@dataclass(frozen=True, slots=True)
class ClaudeProvider:
    manifest: EngineManifest = CLAUDE_MANIFEST

    def create(self, config: Mapping[str, str]) -> ClaudeEngine:
        return ClaudeEngine(
            manifest=self.manifest,
            api_key=config.get(CONFIG_API_KEY) or "",
            model=config.get(CONFIG_MODEL) or "claude-sonnet-5-5",
            endpoint=ANTHROPIC_ENDPOINT,
        )


@dataclass(frozen=True, slots=True)
class DeepSeekProvider:
    manifest: EngineManifest = DEEPSEEK_MANIFEST

    def create(self, config: Mapping[str, str]) -> DeepSeekEngine:
        return DeepSeekEngine(
            manifest=self.manifest,
            api_key=config.get(CONFIG_API_KEY) or "",
            model=config.get(CONFIG_MODEL) or DEEPSEEK_DEFAULT_MODEL,
            endpoint=DEEPSEEK_ENDPOINT,
        )


@dataclass(frozen=True, slots=True)
class ChatGPTProvider:
    manifest: EngineManifest = CHATGPT_MANIFEST

    def create(self, config: Mapping[str, str]) -> OpenAiCompatibleEngine:
        return OpenAiCompatibleEngine(
            manifest=self.manifest,
            api_key=config.get(CONFIG_API_KEY) or "",
            model=config.get(CONFIG_MODEL) or "gpt-4o-mini",
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
