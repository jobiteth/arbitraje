# Changelog

El formato sigue [Keep a Changelog](https://keepachangelog.com/es/1.1.0/) y el
versionado [SemVer](https://semver.org/lang/es/).

## [No publicado]

### Añadido — Ejecución real: la aplicación firma y emite

- **Modo `EJECUCIÓN`**, el único que concede `SIGN_TX` y `BROADCAST_TX`. Con él
  la aplicación deja de ser sólo analítica: firma con la cartera que configures y
  emite la transacción a la red. Los otros tres modos siguen sin poder hacerlo.
- **Tres cosas a la vez, y a propósito.** El modo, `execution.enabled = true` y
  —salvo que la ejecución desatendida esté armada— un «sí» explícito en el
  diálogo. Ninguna de las tres basta sola, y las tres se activan en sitios
  distintos: el modo en la barra superior, el interruptor en el fichero, el «sí»
  en el momento de operar.
- **El interruptor maestro se comprueba en el dominio**, no en la vista. Un
  interruptor que sólo mira la interfaz no es un interruptor: `check_intent` corta
  por `enabled` antes de mirar red, token, motor o importe, y por eso ninguna
  ruta —ni siquiera una que no pase por la pantalla— puede saltárselo.
- **Listas blancas que fallan del lado de no operar.** `allowed_chains`,
  `allowed_tokens` y `allowed_engines` vacías significan **nada permitido**, no
  «sin restricción». Con esto moviendo dinero, el valor por omisión tiene que ser
  el que no gasta.
- **Topes contra lo ya gastado.** `max_quote_per_trade` y `max_quote_per_day`
  se comparan contra el registro `executions.jsonl` de las últimas 24 h, no
  contra un contador en memoria que se reinicia al cerrar la ventana.
- **La clave privada se lee sólo al firmar** y no se cachea en el contenedor. No
  cabe en `config.toml` —que usa `extra="forbid"` y aborta el arranque— ni sale
  en los logs. Por omisión **no** se lee del entorno: hace falta
  `execution.allow_env_key = true`, que es una decisión de custodia y no de
  dinero. Una API key filtrada se rota en un minuto; una clave privada filtrada
  vacía la cartera y no hay rotación que lo arregle.
- **Ejecución desatendida armada con frase.** `AutonomyPolicy.arm(frase)` compara
  contra el llavero con `hmac.compare_digest`, y sólo entonces se salta el
  diálogo —y sólo el `BROADCAST_TX` que quepa en los límites—. **La autonomía
  salta el diálogo, nunca el `ModeGuard`**: sin modo `EJECUCIÓN` no se firma ni
  con la frase correcta, y el `PolicyBypass` sólo puede omitir el prompt, nunca
  la comprobación de modo.
- **Comprar y vender.** El par se arma como `token/stable` al vender y como
  `stable/token` al comprar, y la etiqueta del importe dice en qué unidad está:
  un importe ambiguo, en una pantalla que firma, es un importe equivocado. El
  destinatario por omisión es la dirección derivada de tu clave, ofrecida por
  delante de las direcciones vigiladas.
- **El botón de ejecutar dice por qué está apagado** cuando falta algo, y nombra
  las cuatro piezas que pueden faltar: el modo, el interruptor, una cartera y un
  motor capaz de construir el payload. Las cuatro se enseñan a la vez en vez de
  la primera, porque arreglar una y destapar la siguiente es lo que hace pensar
  que la aplicación está rota en lugar de a medio configurar.
- **El aviso de confirmación depende de la capacidad.** Emitir se describe como
  lo que es —real, irreversible y no cancelable—; preparar, como lo que no firma
  ni emite. Antes era un texto fijo que afirmaba que la aplicación nunca firma:
  con esta entrega esa frase habría sido falsa justo en el instante en que el
  usuario decide.
- **La barrera, verificada por mutación**, no por lectura: conceder `SIGN_TX` a
  `ASISTIDO`, quitar las comprobaciones previas de `ExecuteSwap`, forzar el
  interruptor maestro o anular el bloqueo por motor hacen fallar la suite.

### Cambiado — Comparar precios usa **todos** los motores activos

- `ComparePrices` cotiza todos los motores de la ranura DEX y **fusiona** sus
  cotizaciones, en vez de preguntarle sólo al preferido. Hasta ahora tener un
  respaldo servía para cuando el titular se cayera, pero no para comparar: con
  dos motores activos la tabla mostraba las filas de uno. Ahora gana el mejor
  `amount_out` sea de quien sea, así que dos agregados como 0x y Uniswap
  aparecen por fin frente a frente.
- Un motor que falla **no detiene** la comparación —se sigue con los demás— pero
  tampoco desaparece en silencio: su id viaja en
  `PriceComparison.failed_engines` y la interfaz lo dice en la misma línea del
  resultado. Una tabla con menos filas de las que fuentes había se lee como
  «aquí no hay nada mejor», y puede que la mejor fuera justo la que no respondió.
- Si **todas** fallan se propaga el error real de la preferida, en lugar de
  informar de que no hay cotizaciones: no se pudo mirar, que es otra cosa
  distinta de haber mirado y no encontrar.
- Deduplicación por `venue_id`. El identificador nombra el protocolo y la red,
  no la fuente, así que dos motores mirando el mismo pool producían dos filas y
  un `spread` que era la diferencia entre dos mediciones del mismo contrato:
  ruido con aspecto de oportunidad. Se queda la del motor preferido —y **no** la
  mejor de las dos, que sería escoger la cifra que más conviene de dos
  observaciones del mismo pool— y el desacuerdo entre fuentes queda en el log.
- La tabla de cotizaciones gana una columna **Motor** con el `engine_id` de cada
  fila: con varios motores alimentando una sola tabla, hay que poder ver de dónde
  sale cada cifra.

### Añadido — Motor de 0x (ZeroEx), con el build apagado por omisión

- `engines/zeroex`: cotiza repartiendo la orden entre los venues que agrega 0x
  en **nueve** redes EVM —las ocho de Uniswap más Robinhood Chain, que aquel no
  cubre— y construye el payload sin firmar **sólo si se le pide**.
- El build viene **desactivado** porque 0x cobra una comisión de volumen medida
  en 15,02 bps (el 0,15 % de lo que se recibe), ya descontada del importe.
  Apagado no es una condición escondida: el manifiesto de la instancia deja de
  declarar `PREPARE_TX` y `swap_chains`, así que ni el registro lo ofrece como
  constructor de ninguna red ni la interfaz pinta un botón que falle. Se enciende
  con `enable_swap_build`, y cualquier valor que no se reconozca se lee como
  «no», para que una errata no empiece a cobrar sin que nadie lo haya pedido.
- Se publica la comisión que **de verdad** se paga (`fees.zeroExFee` más
  `integratorFee`), y sólo si está expresada en el token que se recibe; cobrada
  en otro token se publica `None` antes que una cifra que mezcla unidades.
- El `value` del payload se lee en decimal **o** hexadecimal: medido, esta API
  manda `"0"` donde la de Uniswap manda `"0x00"`. Un lector de una sola forma
  habría abortado cada swap, o callado un valor nativo distinto de cero.
- No integra `/gasless/quote`: ese flujo termina en `/gasless/submit`, donde 0x
  **emite** la transacción por el usuario. Amigocompora firma localmente con la
  clave del usuario y no delega ni la firma ni el envío en un tercero, así que
  ese flujo no encaja en el motor.

### Cambiado — El impacto de precio puede faltar

- `Quote.price_impact_bps` pasa a ser opcional, simétrico con `fee_bps`: la v2
  de 0x **eliminó** el campo. Estimarlo está descartado por una razón medida, no
  por prudencia —la resta habitual confunde el excedente por deslizamiento con
  el impacto, y calcularlo contra una orden diminuta del mismo par da un 0,93 %
  que es de ruta y no de profundidad—, así que `None` significa «la fuente no lo
  dice» y la interfaz lo escribe así en lugar de pintar un cero.
- `Quote.impact_basis` e `impact_is_known` acompañan al campo, con la misma
  coherencia que la comisión: o están los dos o no está ninguno. Un impacto
  desconocido **no** degrada `is_exact`, porque `amount_out` sigue medido y es
  la cifra con la que se compara.

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
