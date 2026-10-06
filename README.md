# Amigocompora

Herramienta de escritorio (Windows-first) para **análisis y asistencia en la exploración de mercados on-chain y mercados de predicción**.

> Principio rector: **la IA propone, el usuario decide**. Ninguna ruta del código ejecuta una acción con efectos sin confirmación explícita. La aplicación lee datos públicos, calcula y prepara payloads sin firmar; y **sólo en modo `EJECUCIÓN`**, con `execution.enabled = true` y un «sí» explícito, firma y emite transacciones reales con tu cartera. Arranca en modo `OBSERVACIÓN` y nunca firma por iniciativa propia.

---

## Qué hace

- **Cotizaciones DEX comparadas.** Cotiza el mismo tamaño de orden en todos los venues de un par y muestra, por cada uno, el importe neto que se recibe, la comisión, el impacto de precio, la liquidez y —clave— si cada cifra es *publicada por la fuente*, *calculada* o *estimada*.
- **Detección de oportunidades.** Cruza la mejor ejecución contra el resto y calcula el diferencial bruto y **neto** (descontando comisiones de ambos lados). Es un hallazgo analítico, no una orden.
- **Mercados de predicción.** Lee mercados reales de Polymarket, calcula la probabilidad implícita de cada resultado y el *overround* (cuánto se desvía de 1 la suma de probabilidades).
- **Copiloto de IA.** Un asistente que resume y señala riesgos sobre **los datos que ya están en pantalla** (nunca sale a buscar por su cuenta). Funciona offline por defecto y con Claude / DeepSeek / ChatGPT si se configuran sus claves.
- **Motores intercambiables en caliente.** GeckoTerminal, DexScreener, Uniswap, 0x, Jupiter y Polymarket son motores descubribles por *entry points*. Cambiar de motor no toca el núcleo.
- **Tareas programadas y alertas.** Barridos periódicos que publican discrepancias en una bandeja de alertas deduplicada.
- **Ejecución real, si la pides.** En modo `EJECUCIÓN` la aplicación firma y emite la operación con tu cartera, contra las listas blancas de red, token y motor y los topes de gasto que declares. El resto del tiempo no toca ninguna clave.

## Modos de operación (la barrera de seguridad)

| Modo | Lee | Calcula rutas | Prepara payloads | Firma y emite |
|---|---|---|---|---|
| `OBSERVACIÓN` | ✅ | — | — | — |
| `SIMULACIÓN` | ✅ | ✅ | — | — |
| `ASISTIDO` | ✅ | ✅ | ✅ (con confirmación) | — |
| `EJECUCIÓN` | ✅ | ✅ | ✅ (con confirmación) | ✅ (con confirmación) |

Sólo `EJECUCIÓN` concede `SIGN_TX` y `BROADCAST_TX`, que son las dos capacidades
que mueven dinero. El modo por sí solo **no** basta: hacen falta también
`execution.enabled = true` en la configuración y —salvo que la ejecución
desatendida esté armada con su frase— un «sí» tuyo en el diálogo, que describe la
operación como real e irreversible antes de firmarla.

La autonomía salta el diálogo, nunca el modo. Arranca siempre en `OBSERVACIÓN`.

## Arquitectura

```
UI (PySide6 + qasync)          src/amigocompora/ui
     │
Application (casos de uso)     src/amigocompora/app
     │
Engines (DEX, predicción, IA)  src/amigocompora/engines
     │
Domain (puro, sin I/O)         src/amigocompora/domain
Infra (config, logs, secrets)  src/amigocompora/infra
```

- El **dominio** no hace I/O ni importa dependencias de terceros.
- El **núcleo nunca importa una clase concreta de motor**: los motores se descubren por *entry points* y se usan a través de `Protocol`s.
- El **contrato UI ↔ motor** es serializable y está pensado para poder migrarse a Rust (ver `docs/RUST_MIGRATION.md`).

## Instalación

Requiere **Python 3.13+** y [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --dev          # crea el entorno con dependencias y herramientas
uv run amigocompora    # arranca la interfaz gráfica
```

Sin interfaz (para integración, CI o CLI):

```python
import asyncio
from amigocompora.app.container import build_container

async def main() -> None:
    async with await build_container() as container:
        print(container.registry)
```

## Configuración

`config.toml` vive en `%APPDATA%/Amigocompora/config.toml` (ver `config.example.toml`).

**Las credenciales nunca van en ese fichero.** El modelo usa `extra="forbid"`, así que una clave desconocida —`api_key` incluido— hace fallar el arranque. Las API keys se guardan en el **administrador de credenciales del sistema** (keyring), o como variables de entorno `AMIGOCOMPORA_ENGINE_<ENGINE>_<OPTION>` para CI.

Eso vale también para los dos secretos de la ejecución, que son el caso extremo de
esta regla: la clave privada que firma (`AMIGOCOMPORA_EXECUTION_PRIVATE_KEY`) y la
frase que arma la ejecución desatendida
(`AMIGOCOMPORA_EXECUTION_AUTONOMY_PASSPHRASE`). El bloque `[execution]` decide
**si** se puede operar —`enabled`, `trigger`, las listas blancas y los topes—,
pero no contiene ninguna credencial. Y la clave privada **no** se lee del entorno
salvo que se autorice de forma explícita con `allow_env_key = true`: una API key
filtrada se rota, una clave privada filtrada vacía la cartera.

## Motores disponibles

| ID | Ranura | Lee de | Clave | Construye swaps |
|---|---|---|---|---|
| `geckoterminal` | DEX | GeckoTerminal (comisión real por pool) | opcional (sube el ritmo) | no |
| `dexscreener` | DEX | DexScreener (reservas reales del pool) | no | no |
| `jupiter` | DEX | Jupiter (ejecución agregada de Solana) | no | sí (Solana) |
| `uniswap` | DEX | Trading API de Uniswap (8 redes EVM) | **sí** | sí |
| `zeroex` | DEX | API de 0x (9 redes EVM) | **sí** | sólo con `enable_swap_build` |
| `polymarket` | Predicción | Polymarket (API Gamma) | no | — |
| `stub_advisor` | IA | Heurística local offline | no | — |
| `claude` / `deepseek` / `chatgpt` | IA | APIs de LLM | sí (keyring) | — |

Sólo los motores que **construyen swaps** pueden alimentar el botón de ejecutar.
Los que sólo cotizan sirven para comparar precios, y con ellos activos la
interfaz lo dice en vez de ofrecer un botón que fallaría al pulsarlo. 0x viene
con el build apagado porque cobra una comisión de volumen del 0,15 %.

## Desarrollo

```bash
uv run pytest                    # tests
uv run pytest --cov              # con cobertura
uv run ruff check src tests      # lint
uv run mypy src                  # tipado estricto
```

## Empaquetado

```bash
uv run pyinstaller packaging/amigocompora.spec
```

## Estado del proyecto

Ver [`docs/ROADMAP.md`](docs/ROADMAP.md). Las etapas 0–7 están completas, la 10
(ejecución real: firmar y emitir) está en curso, y las etapas 8
(endurecimiento/empaquetado) y 9 (migración del motor a Rust) siguen pendientes.

## Licencia

Uso privado. Todos los derechos reservados salvo indicación en contrario.
