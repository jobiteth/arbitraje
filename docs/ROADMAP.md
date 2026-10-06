# Roadmap de Amigocompora

Estado por etapa. ✅ completa · 🟡 parcial · 🔴 pendiente.

## Etapa 0 — Descubrimiento y arquitectura ✅

Capas sin dependencias circulares, contratos `Protocol` para motores
hot-swappable, dominio puro verificado. Árbol de carpetas definitivo.

## Etapa 1 — Scaffold, config, logs, esqueletos ✅

- `pydantic-settings` con `extra="forbid"` (un secreto en `config.toml` falla).
- `structlog` con redacción de secretos y URLs.
- `TlsCache` con TTL, `keyring` para credenciales, `httpx` con allowlist.
- Registro de redes (`domain.chains`) y catálogo de tokens medidos.

## Etapa 2 — Conectividad blockchain ✅

- `RpcPool` con failover, circuit-breaker y distinción transporte vs. aplicación.
- 11 redes registradas (`ethereum`…`solana`), EVM y base58.
- `watch_addresses` de sólo lectura.

## Etapa 3 — Motor de precios y comparador DEX ✅

- `geckoterminal` (comisión real, profundidad estimada, V3/V4, nativo V4).
- `dexscreener` (reservas reales, sólo fee constante verificable).
- `amm.py` (`x·y=k`), `amm_protocols.py` (normalización de protocolos y fees).
- `ComparePrices` y `ScanOpportunities`.

## Etapa 4 — Tareas programadas y modos ✅

- `app/scheduler.py`: tareas periódicas, sin solape, jitter, respeto a `ModeGuard`.
- `app/alerts.py`: bandeja deduplicada con severidad y suscripción.
- `app/usecases/watch_scan.py`: barrido de pares vigilados.
- `watch_pairs` configurable en `config.toml`.

## Etapa 5 — Análisis de mercados de predicción ✅

- `polymarket` (API Gamma, mercados abiertos por volumen).
- `AnalyzePredictionMarkets` + `MarketReport` (overround, coherencia, favorito).
- Alertas de discrepancia vía `AlertCenter`.

## Etapa 6 — Integración IA ✅

- Capa abstraída `AiAdvisorEngine` (`domain.protocols`).
- Proveedores: `stub_advisor` (offline, por defecto), `claude`, `deepseek`,
  `chatgpt`. Credenciales en keyring. Prompt con disclaimer.
- `AnalyzeWithAi`: el modelo sólo ve el contexto ya obtenido (reproducible).

## Etapa 7 — UI y notificaciones ✅

- `PySide6` + `qasync` (`ui/app.py`), ventana única con pestañas:
  Cotizaciones, Predicción, Copiloto IA, Motores, Alertas.
- Diálogo de confirmación (`ConfirmationDialog`) conectado a
  `ConfirmationGateway`; banner de alertas y barra de estado.
- Selector de modo de operación que mueve el `ModeGuard`.

## Etapa 8 — Refactor, seguridad y empaquetado 🟡

- ✅ Suite de tests (`pytest`, 44 casos) y `mypy --strict`, `ruff`.
- ✅ `config.example.toml`, spec de PyInstaller (`packaging/`).
- 🟡 Pendiente: `pip-audit` en CI, profiling (`py-spy`) para validar <500 ms,
  y generación y firma del `.exe` en una máquina Windows limpia.

## Etapa 9 — Migración del motor a Rust 🟡

- ✅ Spec de protocolo y mapeo de tipos: `docs/RUST_MIGRATION.md`.
- 🔴 Pendiente: PoC de sidecar (proceso Rust hablando JSON por stdio) y
  medición comparada.

## Rendimiento objetivo

Sin medir todavía: la detección de oportunidades depende de las APIs públicas
(la latencia de red domina). El motor local (`amm.py`) es aritmética `Decimal`.
Validar con `py-spy` antes de dar por bueno el objetivo de <500 ms.
