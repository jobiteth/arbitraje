# Changelog

El formato sigue [Keep a Changelog](https://keepachangelog.com/es/1.1.0/) y el
versionado [SemVer](https://semver.org/lang/es/).

## [No publicado]

### Añadido — Motor de Uniswap (agregado de EVM)

- `engines/uniswap`: cotiza y construye por la Trading API de Uniswap en las
  ocho redes EVM del registro (ethereum, base, bsc, arbitrum, polygon, unichain,
  optimism y avalanche). Necesita clave y la pide por el keyring.
- El destino del build se contrasta contra una **tabla medida por red** —siete
  direcciones distintas para ocho redes— y cualquier desviación aborta la
  construcción, en vez de firmar contra un contrato que no es el medido.
- `priceImpact` se lee como **porcentaje** (no como fracción, que es lo que
  publica Jupiter): una orden de 1 WETH da 1 bp, no 100. Cuando la fuente omite
  el campo —medido a partir de unos 10 WETH— la cotización **no se emite**, en
  lugar de rellenarlo con un cero que afirmaría que la orden no mueve el precio.
- `plan_swap` vuelve a cotizar con el destinatario real y compara contra el
  precio mostrado antes de construir (`QuoteMovedError`, tolerancia de 100 bps).

### Añadido — Varios motores por ranura

- `EngineRegistry` guarda una **pila** por tipo de motor: `activate` sustituye,
  `add` suma un respaldo sin retirar al titular, `remove` saca uno concreto.
- `swap_priority` en `EngineManifest` declara la preferencia, para que el orden
  no dependa de en qué orden se activaron desde la interfaz. Los empates se
  desempatan por `engine_id`.
- `planners_for(chain_key)` devuelve los motores que construyen en esa red,
  ordenados; `PrepareSwap` construye con el que **observó** la cotización.
- `Quote` incorpora `engine_id`: el `venue_id` identifica el protocolo, no el
  motor, así que sin él no se podía saber quién produjo una cifra y construir
  con otro motor habría cambiado el venue sin decirlo.

### Añadido — Oportunidades de predicción (UI)

- Pestaña Predicción: tabla **Cestas con margen**, con el coste de comprar todos
  los resultados frente al pago garantizado de 1.
- Umbral de margen mínimo ajustable en la vista (por defecto 50 bps). El
  recálculo usa `FindPredictionOpportunities.from_reports` sobre los informes ya
  traídos, así que moverlo **no vuelve a consultar la fuente**.
- Aviso permanente de que el margen es bruto: no descuenta gas ni comisiones, no
  comprueba profundidad y las patas hay que ejecutarlas simultáneamente.
- `FindPredictionOpportunities` en el `Container` y resolución de un almacén de
  credenciales caído sin bloquear motores de claves opcionales.

### Añadido — Etapa 7 (UI)

- Interfaz de escritorio con PySide6 + qasync (`src/amigocompora/ui`).
- Ventana principal con pestañas: Cotizaciones, Predicción, Copiloto IA,
  Motores y Alertas.
- Diálogo de confirmación conectado a `ConfirmationGateway` (la UI real que pide
  el «sí» explícito antes de preparar un swap).
- Banner de alertas y barra de estado con modo y capacidades activas.
- Selector de modo de operación enlazado al `ModeGuard`.
- Punto de entrada `amigocompora` / `python -m amigocompora` (`__main__.py`).

### Añadido — Etapa 6 (IA)

- Capa `AiAdvisorEngine` y proveedores `stub_advisor`, `claude`, `deepseek` y
  `chatgpt` (`engines/llm/providers.py`), descubribles por entry points.
- `AnalyzeWithAi`: análisis sobre contexto observado, nunca sobre datos que el
  modelo busque por su cuenta.
- Asistente offline determinista por defecto, sin clave ni red.

### Añadido — Etapa 5 (predicción)

- Alertas de discrepancia de mercados a través de `AlertCenter`.

### Añadido — Etapa 4 (scheduler y alertas)

- `app/scheduler.py`: tareas periódicas con respeto a `ModeGuard`, sin solape,
  con jitter y ciclo de vida explícito.
- `app/alerts.py`: centro de alertas deduplicado con severidad.
- `app/usecases/watch_scan.py`: barrido de pares vigilados.
- Configuración `watch_pairs` en `config.toml`.

### Añadido — Etapa 8/9 (endurecimiento y docs)

- Suite de tests (`tests/unit`, `tests/integration`) con 44 casos.
- `config.example.toml`, spec de PyInstaller (`packaging/amigocompora.spec`).
- `CLAUDE.md`, `docs/ROADMAP.md`, `docs/RUST_MIGRATION.md`, `GLOSSARY.md`.
- Ampliación de `pyproject.toml`: overrides de mypy para la capa UI y entry
  points de los motores de IA.

### Cambiado

- `Container` pasa a `dataclass(slots=True)` y expone `alert_center`, `scheduler`,
  `watch_scan` y `analyze_with_ai`.
- `Container.aclose()` detiene el scheduler antes de cerrar los motores.

## [0.1.0] — Base

### Añadido

- Dominio: aritmética exacta (`Decimal`, nunca `float`), modelos inmutables,
  registro de 11 redes, modos de operación y `ModeGuard`.
- Infra: config, logging con redacción, caché TTL, secretos en keyring,
  cliente HTTP con allowlist y pool JSON-RPC con failover.
- Motores: GeckoTerminal, DexScreener, Polymarket.
- Casos de uso: `ComparePrices`, `ScanOpportunities`,
  `AnalyzePredictionMarkets`, `PrepareSwap`.
