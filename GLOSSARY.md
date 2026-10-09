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

## Polymarket

- **Deposit wallet.** Contrato proxy ERC-1967 con beacon, desplegado por la factoría
  de Polymarket con CREATE2. La controla la clave de la EOA y posee el colateral y
  las participaciones. Es la cuenta que opera desde que el recinto rechaza la EOA.
- **Canal de la deposit wallet.** El camino que opera por la wallet: con
  credenciales del relayer configuradas el caso de uso despliega la wallet si hace
  falta, comprueba fondos, concede el permiso por el relayer (por el máximo, que
  es lo que el relayer exige) y firma tipo 3. Sin credenciales se opera por la
  EOA. La wallet **no se fondea sola**: mover fondos de la cartera a la wallet es
  una transferencia real que decide el usuario (la retirada de la aplicación sirve
  para eso).
- **Mínimo de orden marketable.** Regla del recinto, no de la aplicación: una
  orden que **cruza el libro** necesita importe ≥ $1; si no, se rechaza con
  «invalid amount for a marketable BUY order (importe), min size: $1». Con el
  tamaño mínimo de 5 participaciones, una compra que cruza por debajo de $0.20 por
  participación no llega al mínimo. La regla vista nombra la **compra**: una venta
  que cruza de 5 participaciones ($0.75) se aceptó (2026-10-09); a la venta le
  aplica el tamaño mínimo del mercado (participaciones), no el umbral en dólares.
- **Tipo de firma 3 (`DEPOSIT_WALLET`).** Órdenes firmadas por la deposit wallet con
  `maker` = `signer` = la wallet. Su firma va envuelta en ERC-7739.
- **Envoltura ERC-7739.** Firma de la EOA sobre un `TypedDataSign` cuyo contenido es
  la orden, seguida de los datos que la wallet necesita para validarla
  (separador del intercambio, hash del contenido, cadena del tipo y su longitud).
- **Builder key.** Credencial de Polymarket con clave pública, secreto y frase de
  paso. Firma con HMAC las peticiones `POLY_BUILDER_*`. Es la única que admite
  `WALLET-CREATE`.
- **Relayer key.** Clave de la API del relayer, ligada a una dirección
  (`RELAYER_API_KEY_ADDRESS`). Admite los lotes `WALLET` y sus consultas, pero no
  `WALLET-CREATE`.

## Motores

- **Motor (engine).** Componente descubrible por entry points que implementa un
  `Protocol` (`DexQuoteEngine`, `PredictionMarketEngine`, `AiAdvisorEngine`).
  El núcleo nunca importa una clase concreta.
- **Ranura (`EngineKind`).** Una clase de motor, con su propia pila activa:
  `DEX_QUOTES`, `PREDICTION_MARKETS`, `AI_ADVISOR`, `CROSS_CHAIN` y `WALLET`. Cada
  una trae un motor por omisión (`DEFAULT_ENGINE_IDS`), y el que se elige nunca
  puede exigir credencial: uno con `required_config` no arrancaría en una
  instalación nueva y dejaría la ranura vacía, que es una pantalla que se abre y
  no puede responder. Declararla vacía en `config.toml` (`cross_chain = []`) la
  apaga a propósito, y eso **no** es lo mismo que omitir la clave.
- **Hot-swap.** Sustituir el motor activo sin reiniciar; se abre el nuevo antes
  de cerrar el viejo.
- **Manifiesto.** Metadatos de un motor (id, tipo, capacidades, hosts
  permitidos, config), legibles sin instanciarlo.

- **Seguimiento de puente (`BridgeTracker`).** Protocolo opcional de un motor de
  puente que dice en qué va un cruce ya emitido (`track_bridge`). Sólo lee; un
  motor sin él se muestra «sin seguimiento», nunca con pasos inventados.
- **`BridgeTrackState`.** Estado de un cruce según el proveedor: `PENDING`,
  `REFUNDING`, `REFUNDED`, `DONE`, `FAILED`, `UNKNOWN`. Sólo `DONE`, `REFUNDED` y
  `FAILED` son finales.
- **Reembolso (`REFUNDING`).** El proveedor devuelve el importe a la dirección de
  origen porque no llegó al destino. No es un fallo del emisor, pero tampoco es un
  éxito: se muestra en ámbar.

## Datos de mercado

- **Venue.** Un sitio donde operar: un pool de un DEX o un mercado de
  predicción. El *fee tier* forma parte de su identidad (`uniswap-v3@5`).
- **Cotización (`Quote`).** Resultado de vender `amount_in` de base por quote en
  un venue. `amount_out` es **neto** (ya incluye comisión e impacto).
- **Ruta ejecutable.** Cotización cuyo motor sabe además **construir** el swap
  (implementa `SwapPlanner` y declara la red en `swap_chains`). No es una
  propiedad de la cifra sino de quién la observó: `PrepareSwap` resuelve el
  constructor por `quote.engine_id`, así que la misma cifra vista por una fuente
  de sólo lectura —GeckoTerminal— no se puede firmar. Por eso, cuando dos
  motores miran el mismo pool, el desempate lo gana **el que construye** y no el
  primero de la pila ni el del mejor importe. Ver `app/usecases/compare_prices.py`.
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
- **Resultado abierto.** Lo que una posición gana o pierde si el mercado cerrara
  ya, al precio actual. El **valor** se calcula (participaciones × precio actual)
  pero el resultado no: `cashPnl`/`percentPnl` —y el `avgPrice` con el que se
  contrasta— los **publica** la fuente, y si no vienen quedan ausentes, no en
  cero: un cero inventado se leería como «esta posición no se movió» y miente.
  El total «En conjunto» de la tarjeta sólo suma lo conocido y lo dice.
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
- **Marcador `${NOMBRE}`.** Hueco dentro de un valor de `config.toml` —en
  práctica, la clave de API dentro de la URL de un nodo— que se rellena al
  arrancar desde el llavero (`app:NOMBRE`) o desde `AMIGOCOMPORA_NOMBRE`. Es lo
  que permite declarar un nodo propio en un fichero que no guarda secretos. Si no
  se resuelve, se descarta **ese endpoint** con un aviso que nombra la credencial;
  no se deja la URL a medias ni se sustituye por vacío, porque los dos síntomas
  —fallo de DNS, 401 sin explicación— no se parecen a la causa.
- **Nodo RPC.** Endpoint JSON-RPC declarado para una red, con su prioridad y su
  etiqueta. Se guarda en `config.toml` y se gestiona desde la pestaña
  Configuración.
- **Respaldo público.** Endpoint público de una red que se añade al final de los
  declarados (`include_public_fallbacks`). Su prioridad es mayor que la de
  cualquier nodo propio (1000 frente a 1–999, y en `RpcPool` un número menor se
  prefiere), así que se intenta después de los declarados.
- **Prueba de nodo.** Llamada `eth_chainId` a un nodo antes de guardarlo. Comprueba
  que responde y que es la red que dice ser; el detalle nunca muestra la URL
  completa.
- **Diario de errores.** Memoria acotada con los avisos y fallos de la sesión,
  alimentada por structlog después de la redacción de secretos. Se muestra en la
  pestaña Errores y no se persiste.
- **Atestación.** Firma de Circle sobre un mensaje de CCTP, que habilita la
  recepción en destino. Se obtiene de Iris cuando el estado es `complete`.
- **Recepción en destino.** Llamada `receiveMessage` al `MessageTransmitterV2` de la
  red de destino, que mintea el USDC. Es un segundo paso separado de la quema.
- **Reclamar.** Emitir la recepción en destino de un cruce atestado. Se hace solo al
  atestar; si falla queda «Reclamo fallido» y el usuario lo repite desde Transacciones.
- **Tiempo por paso.** Cuánto lleva o tardó cada paso de la modal, derivado de los
  instantes del registro. Se congela al terminar el cruce.
- **Escritura atómica con tomlkit.** Reemplazo de `config.toml` mediante un
  temporal y `replace`, con copia `config.toml.bak`. Conserva comentarios y el
  resto de bloques.

## Interfaz

- **«Wallet de depósito» (tarjeta).** La tarjeta de la pestaña de predicciones que
  enseña la dirección de la wallet, su saldo del colateral y sus posiciones. La
  dirección se **deriva** (pura, sin red ni clave) y por eso se ve antes de leer
  nada; el saldo y las posiciones se **leen** al pulsar «Leer la wallet». Cada
  fila de la tabla trae «Vender» y «Comprar más», que sólo **cargan** la tarjeta
  de orden de encima: puesto el mercado, el resultado y el tamaño, el precio y
  «Publicar» siguen siendo los de arriba, con sus comprobaciones y su diálogo.
  Una fila ya resuelta apaga las acciones con el motivo escrito —se cobra, no se
  opera, y el cobro por este canal todavía no existe—. «Añadir saldo» tiene dos
  caminos: desde la cartera es la retirada de siempre con la wallet prefijada
  como destino, y desde fuera es la dirección con su QR.
- **Pata.** Uno de los dos lados de un par: lo que se entrega y lo que se recibe.
  En pantalla es un recuadro (`QFrame#legBox`) con su red, su importe, la píldora
  de su token y su saldo. Las dos patas se ven a la vez y del mismo tamaño, porque
  lo que se describe es un intercambio entre ellas.
- **Píldora del token.** La cara visible de la elección de token dentro de una
  pata. En «Entre redes» es un desplegable —la lista es la de una red— y en swaps
  un botón que abre el selector con buscador.
- **«Máx».** Atajo que pone en el importe todo el saldo disponible de la pata de
  origen, truncado hacia abajo a los decimales del campo: siempre por debajo del
  saldo, nunca por encima. Sin saldo queda apagado, no escondido.
- **«Invertir».** Cambia las dos patas de sitio, cada una con su red y su token. El
  token se busca por su equivalente en la red de enfrente, no con `is_same_asset`,
  que compara la red y nunca acertaría al cruzarla. No firma ni emite nada.
- **Icono de marca.** El logo que acompaña al nombre de una red, un token o un
  motor en los listados —desplegables, tablas—; el nombre va además en el tooltip.
  El icono **no sustituye** al nombre: adelanta lo que el texto confirma. Se busca
  en la carpeta que se mantiene a mano (`assets/chains/`, `coin/`, `plataform/`)
  y, si no está, en la de web3icons (`networks/`, `tokens/`, `brands/`): una
  descarga nunca tapa un fichero elegido a mano. Sin fichero no se dibuja ningún
  logo ajeno —nunca un logo de relleno—: un hueco no dice «esta marca no existe»,
  dice «su logo no está descargado». Fuera de los listados no se usa ninguno.
- **Genérico (icono).** El dibujo que enseñan un token o una plataforma cuando no
  tienen logo propio (`coin/token.svg`, `plataform/plataform.svg`). No es la marca
  de nadie: dice «esto es un token» o «esto es un proveedor», y sólo aparece si el
  icono propio falta. Las redes no tienen genérico.
- **Slug de icono.** El nombre con el que web3icons publica un logo (`base`,
  `binance-smart-chain`, `USDC`). La traducción desde nuestros nombres vive en una
  sola tabla (`ui/icons.py`), compartida con `tools/fetch_icons.py`.
