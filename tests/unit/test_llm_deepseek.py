"""DeepSeek como copiloto: lo que se midió contra la API el 2026-10-07.

El motor ya existía y nunca estuvo activo, así que nadie había llamado a su
puerta. Al llamarla por primera vez devolvía **el respaldo offline con un 200**:
la API contestaba, sí, pero con `content` vacío, y el motor lo tomaba por una
respuesta buena. Lo que se fija aquí es lo que se midió para entenderlo y lo que
se cambió después.

| petición | desenlace medido |
|---|---|
| `max_tokens=1200` (lo que se mandaba) | `length`, `content` vacío, 1200 tok. razonando |
| `max_tokens=8000` | contesta, pero 4667 tokens en total |
| `thinking: {"type": "disabled"}` | contesta, 772 tokens, ninguno razonando |
| `thinking: false` | HTTP 422 — el campo no acepta un booleano |
| `reasoning_effort: "low"` | `content` vacío — bajarlo no basta |

Y `GET /models` sirve hoy exactamente dos: `deepseek-flash` y `deepseek-v4-pro`.
`deepseek-chat`, que era el modelo escrito a mano, sigue respondiendo 200 pero la
API lo **aliasa** en silencio a `deepseek-flash` y lo devuelve así en `model`.

Ninguna prueba de aquí sale a la red: lo medido está en los comentarios y lo que
se comprueba es que el código siga haciendo lo que la medición justificó.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import respx

from amigocompora.domain.protocols import AnalysisRequest
from amigocompora.engines.llm.providers import (
    CONFIG_API_KEY,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_ENDPOINT,
    DEEPSEEK_MANIFEST,
    DEEPSEEK_PROVIDER,
    ChatGPTProvider,
    DeepSeekEngine,
)
from amigocompora.infra.secrets import env_var_name

PREGUNTA = AnalysisRequest(question="¿compensa?", context={"spread": "40 bps"})


def _motor(modelo: str | None = None) -> DeepSeekEngine:
    config = {"api_key": "no-es-una-clave-de-verdad"}
    if modelo is not None:
        config["model"] = modelo
    motor = DEEPSEEK_PROVIDER.create(config)
    assert isinstance(motor, DeepSeekEngine)
    return motor


@asynccontextmanager
async def _abierto(motor: DeepSeekEngine) -> AsyncIterator[DeepSeekEngine]:
    await motor.aopen()
    try:
        yield motor
    finally:
        await motor.aclose()


def _respuesta(texto: str) -> dict[str, Any]:
    return {
        "model": DEEPSEEK_DEFAULT_MODEL,
        "choices": [{"message": {"role": "assistant", "content": texto}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 10},
    }


# --------------------------------------------------------------------------- #
# La variable de entorno y el modelo
# --------------------------------------------------------------------------- #
def test_la_variable_de_entorno_no_repite_el_nombre_del_motor() -> None:
    """`AMIGOCOMPORA_ENGINE_DEEPSEEK_API_KEY`, no `…_DEEPSEEK_DEEPSEEK_API_KEY`.

    `env_var_name` compone la variable con el `engine_id` **y** la opción, así
    que la opción se llamaba `deepseek_api_key` y el nombre salía repetido. La
    misma idea escrita de dos formas según el motor: `lifi` y `relay` usan
    `api_key` y dan `…_LIFI_API_KEY`. Esta prueba es lo que impide que vuelva.
    """
    assert env_var_name("deepseek", CONFIG_API_KEY) == "AMIGOCOMPORA_ENGINE_DEEPSEEK_API_KEY"
    assert DEEPSEEK_MANIFEST.required_config == (CONFIG_API_KEY,)


def test_el_modelo_por_omision_esta_en_el_catalogo_medido() -> None:
    """`deepseek-flash`, que es lo que `GET /models` sirve hoy."""
    assert DEEPSEEK_DEFAULT_MODEL == "deepseek-flash"
    assert _motor().model == DEEPSEEK_DEFAULT_MODEL


def test_el_nombre_de_la_clave_en_el_llavero_lleva_el_motor_delante() -> None:
    """La opción cambió de nombre, así que la entrada del llavero también.

    Se fija para que el cambio sea visible: quien tuviera la clave guardada como
    `deepseek:deepseek_api_key` tiene que volver a guardarla como
    `deepseek:api_key`. Nadie la tenía —el motor nunca estuvo activo— pero eso no
    es una razón para que el cambio pase desapercibido.
    """
    from amigocompora.infra.secrets import secret_key

    assert secret_key("deepseek", CONFIG_API_KEY) == "deepseek:api_key"


# --------------------------------------------------------------------------- #
# El razonamiento apagado
# --------------------------------------------------------------------------- #
def test_al_razonamiento_se_le_pide_que_no() -> None:
    """Sin esto el motor no puede contestar: se le acaba el tope pensando."""
    cuerpo = _motor()._body(PREGUNTA)
    assert cuerpo["thinking"] == {"type": "disabled"}


def test_el_campo_de_deepseek_no_se_le_manda_a_openai() -> None:
    """`thinking` sólo existe en DeepSeek; a OpenAI sería un 400, no un ignorado.

    Es la razón de que el parámetro viva en un método que se sobrescribe y no en
    el cuerpo compartido: los dos motores son la misma clase.
    """
    ajeno = ChatGPTProvider().create({"api_key": "x"})
    assert "thinking" not in ajeno._body(PREGUNTA)
    assert "thinking" in _motor()._body(PREGUNTA)


def test_el_tope_de_tokens_sigue_siendo_el_de_antes() -> None:
    """No se subió: se apagó el razonamiento, que es lo que gastaba el tope.

    Subirlo a 8000 también funcionaba, pero costaba 4667 tokens en vez de 772
    para el mismo resumen. Si alguien lo sube «por si acaso», que sea a
    conciencia: esta prueba lo hace visible.
    """
    assert _motor()._body(PREGUNTA)["max_tokens"] == 1200


# --------------------------------------------------------------------------- #
# La respuesta vacía deja de pasar por buena
# --------------------------------------------------------------------------- #
@respx.mock
async def test_una_respuesta_con_texto_se_devuelve_tal_cual() -> None:
    respx.post(DEEPSEEK_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_respuesta("Resumen de verdad."))
    )
    async with _abierto(_motor()) as motor:
        resultado = await motor.analyze(PREGUNTA)
    assert resultado.summary == "Resumen de verdad."


@respx.mock
async def test_una_respuesta_vacia_no_se_hace_pasar_por_analisis() -> None:
    """Es el fallo que tenía el motor: 200, `content` vacío y nadie se enteraba.

    Se sigue cayendo al respaldo offline —sin red la aplicación tiene que
    funcionar— pero el respaldo es reconocible por su prefijo, y el registro
    deja dicho por qué.
    """
    respx.post(DEEPSEEK_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "pensando y pensando…",
                        },
                        "finish_reason": "length",
                    }
                ]
            },
        )
    )
    async with _abierto(_motor()) as motor:
        resultado = await motor.analyze(PREGUNTA)
    assert resultado.summary.startswith("Análisis offline de:")


@respx.mock
async def test_un_error_de_la_api_tambien_cae_al_respaldo() -> None:
    respx.post(DEEPSEEK_ENDPOINT).mock(return_value=httpx.Response(500, text="boom"))
    async with _abierto(_motor()) as motor:
        resultado = await motor.analyze(PREGUNTA)
    assert resultado.summary.startswith("Análisis offline de:")
