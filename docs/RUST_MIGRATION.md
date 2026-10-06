# Migración del motor a Rust — especificación

Objetivo: poder sustituir el motor (dominio + aplicación) por una implementación
en Rust **sin tocar la UI**. La UI no debe cambiar más que la forma de obtener
los datos de los casos de uso; el contrato de datos ya está preparado.

Este documento fija el **contrato** y el plan. La PoC (sidecar) queda pendiente.

## 1. Por qué es posible sin tocar la UI

La UI sólo depende de:

- `Container` (los casos de uso y el `ModeGuard`).
- Los **value objects** del dominio (`PriceComparison`, `Quote`, `Opportunity`,
  `MarketReport`, `PredictionMarket`, `AnalysisResult`).
- `EngineRegistry` (listar y activar motores).

Nada de la UI importa `httpx`, `keyring` ni un motor concreto. Y el núcleo habla
con los motores a través de `typing.Protocol`, que se corresponde 1:1 con un
`trait` de Rust.

## 2. Mapeo de tipos Python → Rust

| Python (`domain.models`) | Rust propuesto | Notas |
|---|---|---|
| `TokenAmount(raw: int, decimals, symbol)` | `TokenAmount { raw: i128, decimals: u8, symbol: String }` | `i128` cubre 18 decimales con holgura. Nunca `f64`. |
| `Price(value: Decimal, base, quote)` | `rust_decimal::Decimal` | Precisión decimal exacta. |
| `BasisPoints(value: int)` | `i32` newtype | Aritmética entera. |
| `Token`, `TradingPair`, `Venue` | structs `#[derive(Clone, Serialize, Deserialize)]` | Sin lógica de red. |
| `Quote`, `PriceComparison`, `Opportunity` | structs serde | El contrato serializable. |
| `Measurement` / `VenueKind` / `EngineKind` | `enum` con `#[serde(rename_all="snake_case")]` | Deben coincidir con los `StrEnum` actuales. |
| `Capability` / `OperationMode` | `enum` | La tabla de política se migra tal cual. |
| `Protocol` de motor | `trait DexQuoteEngine` | Ver abajo. |

Regla dura que se mantiene: **ningún valor económico pasa por coma flotante**.
En Rust, `rust_decimal` o enteros; nunca `f64`.

## 3. Contrato de motores en Rust

```rust
#[async_trait::async_trait]
pub trait Engine: Send + Sync {
    fn manifest(&self) -> &EngineManifest;
    async fn aopen(&self) -> Result<(), EngineError>;
    async fn aclose(&self) -> Result<(), EngineError>;
}

#[async_trait::async_trait]
pub trait DexQuoteEngine: Engine {
    async fn venues(&self, chain_key: &str) -> Result<Vec<Venue>, EngineError>;
    async fn quote(
        &self,
        pair: &TradingPair,
        amount_in: &TokenAmount,
    ) -> Result<Vec<Quote>, EngineError>;
}

#[async_trait::async_trait]
pub trait PredictionMarketEngine: Engine {
    async fn markets(&self, limit: usize, search: Option<&str>)
        -> Result<Vec<PredictionMarket>, EngineError>;
    async fn market(&self, market_id: &str) -> Result<PredictionMarket, EngineError>;
}
```

Es un calco de `domain/protocols.py`. El `ModeGuard` se mantiene en la frontera
(el sidecar no decide política; la UI/orquestador le pasa la capacidad ya
autorizada, o el sidecar recibe el modo y aplica la misma tabla).

## 4. Protocolo de comunicación (sidecar)

Recomendado: **proceso sidecar** con JSON-lines por `stdin`/`stdout`. Se elige
frente a PyO3 porque:

- Aísla un *panic* de Rust del proceso de la UI.
- Permite versionar y sustituir el binario sin recompilar Python.
- El contrato es inspeccionable y testeable por separado.

Formato de cada mensaje (una línea):

```json
{"id": 42, "method": "dex.quote", "params": {"pair": {...}, "amountIn": {...}}}
{"id": 42, "result": {...}}
{"id": 42, "error": {"code": "no_quotes", "message": "..."}}
```

`method` propuesto: `engine.list`, `engine.activate`, `dex.venues`, `dex.quote`,
`predict.markets`, `predict.market`, `ai.analyze`.

Los `params` y `result` usan los value objects serde. La UI no cambia: se añade
un `RustEngineProvider` que implementa `EngineProvider`/`DexQuoteEngine`
hablando con el sidecar, y el resto del sistema no se entera.

## 5. Frontera que se queda en Python

- **UI** (PySide6) y **scheduler** — dependen del event loop de Qt.
- **Secretos** (`keyring`) — el sidecar recibe configuración ya resuelta, nunca
  lee credenciales por su cuenta.
- **Casos de uso y `ModeGuard`** — pueden quedarse o migrarse; si se migran, el
  sidecar recibe `mode` en cada petición.

## 6. Plan por fases

1. **Congelar el contrato.** Añadir serialización (serde) a los value objects y
   un test que fije el JSON (golden files). Python y Rust deben producir bytes
   equivalentes.
2. **PoC del sidecar.** Binario Rust mínimo que responda `engine.list` y
   `dex.quote` con datos de un fixture. Sin red.
3. **`RustEngineProvider`.** Implementación del `Protocol` que lanza el proceso,
   serializa peticiones y aplica *timeouts* con reinicio.
4. **Paridad de un motor.** Migrar `geckoterminal` y comparar salida contra el
   motor Python con datos reales; debe coincidir al dígito.
5. **Medición.** `py-spy`/`criterion` para validar el objetivo de <500 ms.
6. **Retirada gradual** de los motores Python, manteniéndolos como respaldo.

## 7. Riesgos

- **Deriva del contrato.** Mitigada por golden files compartidos.
- **Ciclo de vida del proceso.** Un sidecar que cuelga debe reiniciarse sin
  tumbar la UI (supervisor con reintentos y *degradación* a motor Python).
- **Precisión decimal.** `rust_decimal` frente al `Context(prec=60)` de Python:
  fijar la misma política de redondeo (half-even) en ambos lados.
