# Glosario

Términos que en este proyecto tienen un significado preciso y del que conviene
no desviarse.

## Producto y seguridad

- **Principio rector.** «La IA propone, el usuario decide». Ninguna ruta ejecuta
  una acción con efectos sin confirmación explícita.
- **Modo de operación.** `OBSERVACIÓN`, `SIMULACIÓN`, `ASISTIDO` o `EJECUCIÓN`.
  Define qué `Capability` están concedidas. Ver `domain/modes.py`.
- **Capability.** Acción concreta que un caso de uso declara necesitar
  (`READ_CHAIN`, `COMPUTE_ROUTE`, `QUERY_AI`, `PREPARE_TX`, `SIGN_TX`,
  `BROADCAST_TX`). La UI nunca decide «en qué modo estoy»; declara la capacidad.
- **`CONFIRMABLE_CAPABILITIES`.** `PREPARE_TX` y `BROADCAST_TX`: las que, además
  de estar concedidas por el modo, exigen un «sí» del usuario. `SIGN_TX` **no**
  está aquí, y es deliberado: firmar sin emitir no tiene efecto propio dentro del
  flujo —la firma va de la mano al envío, sin pasar por el usuario ni por disco—
  y un segundo diálogo para una sola operación se aprende a cerrar sin leer.
- **Ejecución desatendida.** Ejecutar sin que pregunte cada vez. Está **desarmada**
  por defecto; se arma con `AutonomyPolicy.arm(frase)`, que exige una frase del
  llavero —o del entorno, si `allow_env_key` lo autoriza— y la compara con
  `hmac.compare_digest`. Sólo se salta el diálogo el `BROADCAST_TX` que quepa en
  los límites. **La autonomía salta el diálogo, nunca el `ModeGuard`**: sin modo
  `EJECUCIÓN` no se llega a firmar ni con la frase correcta.
- **`ConfirmationGateway`.** Cruza modo + un «sí» explícito del usuario. Sin
  prompt conectado rige `DenyAllPrompt` (todo denegado).

## Motores

- **Motor (engine).** Componente descubrible por entry points que implementa un
  `Protocol` (`DexQuoteEngine`, `PredictionMarketEngine`, `AiAdvisorEngine`).
  El núcleo nunca importa una clase concreta.
- **Ranura (`EngineKind`).** Hay un motor activo por ranura: `DEX_QUOTES`,
  `PREDICTION_MARKETS`, `AI_ADVISOR`.
- **Hot-swap.** Sustituir el motor activo sin reiniciar; se abre el nuevo antes
  de cerrar el viejo.
- **Manifiesto.** Metadatos de un motor (id, tipo, capacidades, hosts
  permitidos, config), legibles sin instanciarlo.

## Datos de mercado

- **Venue.** Un sitio donde operar: un pool de un DEX o un mercado de
  predicción. El *fee tier* forma parte de su identidad (`uniswap-v3@5`).
- **Cotización (`Quote`).** Resultado de vender `amount_in` de base por quote en
  un venue. `amount_out` es **neto** (ya incluye comisión e impacto).
- **`Measurement`.** Procedencia de una cifra: `REPORTED` (la publica la
  fuente), `DERIVED` (la calculamos sobre datos publicados), `ESTIMATED` (con un
  supuesto documentado).
- **Comisión desconocida (`fee_bps is None`).** La fuente no la desglosa (caso
  medido: Jupiter). No significa «gratis»: `amount_out` sigue siendo neto. Esas
  cotizaciones se excluyen del cálculo de diferencial **neto**.
- **Impacto de precio.** Cuánto empeora el precio por el tamaño de la orden, sin
  contar la comisión. En un AMM de producto constante, siempre ≥ 0.
- **Spread bruto / neto.** Diferencia entre mejor y peor ejecución, antes y
  después de restar comisiones de ambos lados.
- **Overround.** Cuánto se desvía de 1 la suma de probabilidades de un mercado
  de predicción. Positivo = margen; negativo = discrepancia.
- **Cesta (`BasketOpportunity`).** Comprar una participación de **cada**
  resultado de un mercado. Paga exactamente 1 ocurra lo que ocurra, así que si
  cuesta menos de 1 el margen es `1 − coste`. Sólo existe con overround negativo,
  y es bruto: no descuenta gas ni profundidad. Ver
  `app/usecases/find_prediction_opportunities.py`.
- **Descuento vs. retorno s/ capital.** Sobre una cesta que cuesta 0,96 el
  descuento es 400 bps (el margen de 0,04 medido contra el pago de 1) pero el
  retorno es 417 bps (medido contra el capital desembolsado, 0,96). Se publican
  los dos: cuál importa depende de si el coste de oportunidad se mide contra el
  pago o contra el desembolso.
- **`x·y=k`.** Fórmula del pool de producto constante (Uniswap V2 y clones).

## Infraestructura

- **`JsonSource`.** Base común de los motores HTTP: caché, límite de ritmo
  adaptativo, reintentos acotados y dos clases de fallo.
- **`RpcPool`.** Endpoints JSON-RPC de una red con failover y circuit-breaker.
  Distingue fallo de transporte (reintenta otro endpoint) de error de aplicación
  JSON-RPC (falla rápido).
- **Allowlist de hosts.** Un motor sólo puede hablar con los hosts que declara.
- **Keyring.** Almacén de credenciales del sistema operativo. Los secretos
  nunca van a `config.toml` (`extra="forbid"`) ni a los logs.
