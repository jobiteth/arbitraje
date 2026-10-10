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
- **Lista blanca de tokens (`allowed_tokens`).** Símbolos con los que la política
  deja operar. Vacía significa «nada» y `["*"]` —el comodín— «cualquiera»: son
  decisiones distintas y no se confunden por accidente. Quién pasa lo decide
  `ExecutionLimits.allows_token`, que compara **los dos lados en mayúsculas**: el
  catálogo tiene símbolos en caja mixta (`pUSD`, el colateral del recinto) y la
  lista la escribe una persona, así que comparar en crudo bloquearía tokens que
  sí están declarados. Es la misma respuesta que usan los avisos de la interfaz
  antes de firmar y `check_token` al firmar.
- **Lista blanca de redes (`allowed_chains`).** La misma pregunta en la otra
  dimensión —dónde—, con las mismas reglas: vacía significa «nada» y `["*"]`
  «cualquiera». Quién pasa lo decide `ExecutionLimits.allows_chain` —espejo de
  `allows_token`; el `*` es `ANY_CHAIN`, definido a partir de `ANY_TOKEN` para
  que el mismo asterisco no pueda leerse de dos formas—, y la usan
  `check_chain` al firmar y los avisos de la tarjeta de predicción: la pantalla
  y la firma dan la misma respuesta. La configuración acepta el comodín en la
  validación de nombres (`infra/config.py`), junto a las redes de verdad.
- **Permiso encadenado por Permit2.** Hay routers que **no mueven el token ellos
  mismos**: el swap lo cobra pidiéndoselo a Permit2
  (`0x000000000022D473030F116dDEE9F6B43aC78BA3`), que es quien tiene permiso
  sobre el ERC-20. En esas redes hacen falta **dos** transacciones de permiso
  antes del swap —la aprobación del ERC-20 a Permit2 y la de Permit2 al
  router—, y el payload lo tiene que **declarar** (`TokenApproval.spender` más
  `via`): aprobar el token directamente contra el router es un permiso que ese
  router nunca lee, y el swap revierte. Se midió con `eth_getCode` el
  2026-10-09: los ocho routers del motor `uniswap` (agregado) lo llevan dentro;
  los de `zeroex`, no, y por eso su camino directo no se toca. El gastador de la
  primera aprobación es Permit2 —no el router— y el relato de los diálogos lo
  distingue («Es la primera de dos aprobaciones…»).
- **Una ejecución a la vez (`SingleFlight`).** La puerta que deja **una sola**
  operación de firma en vuelo, compartida por los cuatro caminos que emiten
  —swap, puente, recepción de un puente y retirada— porque el recurso escaso es
  uno: el nonce de la cartera. La segunda ejecución concurrente se **rechaza**
  con `ExecutionError`; no se encola —encolarla sería firmar «cuando le toque»,
  ya sin el usuario mirando—. Se midió el 2026-10-10: un segundo disparo a mitad
  de un puente volvió a firmar con el mismo nonce, y de dos transacciones con el
  mismo nonce sólo una puede minar. La puerta vive en el caso de uso y no en la
  vista: una pestaña puede olvidarse de apagar su botón; la firma, no. Ver
  `app/single_flight.py`.
- **Diálogo de confirmación sin bucle anidado.** `QDialog.exec()` dentro de una
  corrutina abre un bucle de eventos anidado **dentro** del paso de la tarea y,
  bajo `qasync`, mata el despertar de cualquier otra tarea en vuelo (medido el
  2026-10-10: `RuntimeError: Cannot enter into task …`). Desde una corrutina un
  diálogo se pide con `open()` —modal para la ventana— y se espera el «sí» por
  la señal `finished`; `exec()` queda para los manejadores síncronos. Ver
  `ui/widgets.py` (`QtConfirmationPrompt`).

## Carteras

- **Cartera activa.** La del libro que manda: su dirección es la que leen swap,
  predicción, retiradas y saldos, y la que firma cuando puede. Se elige
  pulsando el nombre en la cabecera de la Cartera. El cambio llega a toda la
  aplicación **por construcción**, no por avisos repartidos: todo lo que
  resuelve la cartera —casos de uso que firman y gates de la interfaz— la pide
  a `container.keys` (`WalletKeyProvider`), el mismo punto único que había
  antes de existir el libro.
- **Cartera heredada (del llavero).** La clave privada que vive en el llavero
  de Windows (`app:execution_private_key`), configurada en Credenciales. Sigue
  leyéndose sin migrar nada —una instalación de antes del libro no nota el
  cambio— y cualquier cambio sobre ella la materializa en el libro («1»,
  `source=KEYRING`); borrarla no borra la copia del llavero, que es el
  respaldo. Mientras el libro no tenga nada, responde el proveedor heredado con
  su comportamiento de siempre —`allow_env_key` incluido, que sólo aplica a
  esta cartera—: una clave cifrada no se lee del entorno.
- **Modo observación (watch-only).** Una cartera sin clave: se lee su dirección
  y sus saldos —y sirve como destino por omisión—, pero no firma. `require()`
  la rechaza con `NoWalletError` y los botones que mueven dinero salen apagados
  con el motivo escrito. Acepta direcciones EVM y Solana, validando la forma.
- **Almacén cifrado de carteras.** El libro `wallets.json` (`config_dir()`,
  escritura atómica y lectura tolerante) con las carteras guardadas: etiqueta,
  dirección, origen y —las que firman— su keystore cifrado. Nunca guarda una
  clave en claro, y borrar una cartera no toca la copia del llavero.
- **Keystore (Web3 Secret Storage).** El formato estándar de Ethereum para
  guardar una clave privada cifrada con una contraseña (V3; aquí scrypt
  n=16384 sobre `eth-account`, sin cripto escrita a mano). Se descifra **al
  firmar**, nunca antes: la clave descifrada no se cachea en ningún objeto.
- **Contraseña de las claves.** La que cifra el almacén. **No hay ninguna
  escrita en el código ni en la configuración**: se crea la primera vez que se
  añade una clave o se enseña la heredada, y todas las claves del almacén
  comparten una —migrar con otra dejaría una clave que el desbloqueo de la
  sesión no abriría, así que el proveedor la verifica contra el almacén ya
  existente antes de cifrar nada—. La de sesión vive **en memoria** mientras la
  cartera esté desbloqueada, porque cada firma descifra con ella; «Bloquear
  cartera» la olvida. Perderla es perder las claves, y el diálogo de creación
  lo dice.

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
  La tarjeta aplica **la regla que toque** según el libro que tenga leído: compra
  que cruza → importe —y como las participaciones se derivan hacia abajo del
  importe escrito, cuando el redondeo se queda corto el motivo da la cifra exacta
  que sí llega: a 0,62 $, «sube el importe a 1.01 $»—; orden que descansa o venta
  → mínimo de participaciones; sin libro leído manda la del tamaño, que es la que
  no depende del libro. Ver `_order_input_blockers` en `ui/pages/prediction.py`.
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
- **Alias de pantalla (`[engine_names]`).** El nombre con el que se enseña un
  motor, editable con el lápiz de su fila en la pestaña de motores y guardado en
  `config.toml`. Es sólo eso —pantalla—: el `engine_id`, su configuración y lo
  que hace no cambian, y por eso las tres pantallas que enseñan nombres
  (motores, APIs de configuración y credenciales) lo resuelven por el mismo
  sitio (`app/engine_names.py`). Un alias en blanco **borra**: el motor vuelve a
  su nombre de fábrica, y la tabla se retira del archivo si no queda ninguno.
- **Encender suma, apagar retira.** El interruptor de cada fila de la pestaña de
  motores: encender un motor lo **añade** a su ranura sin apagar los demás
  —varios por ranura es lo que permite comparar precios— y apagar retira sólo a
  ése. Apagar el último deja la ranura escrita **vacía** (`dex_quotes = []`),
  que es «apagada a propósito»; borrar la clave en vez de vaciarla haría volver
  el motor por omisión al reiniciar. Quién responde primero no lo cambia este
  gesto: lo decide la prioridad del manifiesto.
- **Centinela del nativo.** La dirección con la que cada API agregada nombra la
  moneda de la red, que no tiene contrato: la **dirección cero** en la de Uniswap
  y `0xEeee…` en la de 0x. Medido: cada una responde «sin ruta» con la del otro,
  así que la traducción vive en el motor (`_api_address`/`_api_token`) y no en
  quien llama. Al vender nativo la transacción lleva el importe en `value` y el
  router lo envuelve él mismo; al comprarlo, lo desenvuelve.

- **Seguimiento de puente (`BridgeTracker`).** Protocolo opcional de un motor de
  puente que dice en qué va un cruce ya emitido (`track_bridge`). Sólo lee; un
  motor que no lo implementa no inventa pasos: si está activo se dice así
  —«está activo, pero no sabe seguir cruces todavía»— y se deja de sondear (el
  registro queda sin estado final y se vuelve a sondear al arrancar); si no está
  activo se sigue sondeando, porque puede activarse desde la página de motores.
  `lifi` lo implementa, y `relay` desde el 2026-10-10.
- **Seguimiento de relay (`/requests/v3`).** La consulta con la que relay sigue
  un cruce ya emitido: `GET /requests/v3?depositTxHash=<hash de origen>` —la v2
  se retira el 2026-11-24—, con la clave en la cabecera. De la respuesta se leen
  el estado, la transacción de entrega (`outTxs`) y el importe **recibido de
  verdad**: el cambio de saldo positivo a favor de la cartera en
  `stateChanges`, no lo cotizado. Un hash que no conoce contesta la lista vacía,
  y la respuesta se comprueba otra vez contra sus `inTxs` para que el cruce de
  otro nunca se muestre como propio.
- **`BridgeTrackState`.** Estado de un cruce según el proveedor: `PENDING`,
  `REFUNDING`, `REFUNDED`, `DONE`, `FAILED`, `UNKNOWN`. Sólo `DONE`, `REFUNDED` y
  `FAILED` son finales.
- **Reembolso (`REFUNDING`).** El proveedor devuelve el importe a la dirección de
  origen porque no llegó al destino. No es un fallo del emisor, pero tampoco es un
  éxito: se muestra en ámbar.
- **Contratos de origen (del puente).** Los contratos **de la red de salida** a
  los que un motor de puente puede mandar el dinero. No es uno por red: Relay
  usa tres —el depósito directo, el router de su v3 para las rutas con swap en
  origen y su proxy de aprobación para las de un ERC-20—, los publica en
  `GET /chains` y se midieron en vivo (el depósito el 2026-10-07; la familia
  entera el 2026-10-10). `expected_destination` los declara y el payload tiene
  que dirigirse **a alguno de ellos**: es la comprobación que separa un puente de
  un swap, porque aquí un destino equivocado no pierde dinero en una operación de
  mercado — lo manda a otra parte y no vuelve. En todas las rutas observadas el
  gastador de la aprobación es el mismo contrato que recibe el depósito, y que no
  coincidan se rechaza antes de construir.

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
  En la lista de rutas, las que no lo son se marcan **antes de elegirlas** —«·
  sólo cotiza», en ámbar, en su tarjeta—: la tarjeta se queda para comparar
  cifras, pero la marca evita firmar una ruta que no lleva a ninguna parte. El
  ajuste `[ui] hide_quote_only_routes` (apagado por omisión) las quita de la
  lista en vez de marcarlas; el recuento entonces dice cuántas se ocultaron
  —«N de M»— para que una lista con menos rutas de las que hay no parezca
  completa.
- **Ruta multi-salto (`SwapRoute`).** El camino de un swap que cruza más de un
  pool: tramos contiguos —lo que sale de uno entra en el siguiente—, todos en la
  misma red, y `Quote.route` no nulo. El motor que la publica se compromete a
  ejecutar **ese** camino: al construir lo vuelve a cotizar entero y no busca
  otro que diera más. `route=None` significa «un solo pool», que es el caso
  normal.
- **Hub (token de paso).** El token intermedio por el que la ruta cruza cuando
  el par no tiene pool directo con liquidez. Salen del catálogo
  (`hub_tokens`), en su orden —envoltorio nativo, stablecoin de referencia,
  extras medidos—, y se filtran los del propio par. Es lo que hace que la
  búsqueda funcione en cualquier red sin código nuevo: añadir un token al
  catálogo le da una escala más a todos los pares.
- **Camino empaquetado (`packed path`).** El `bytes path` del multi-salto de
  V3: tokens de 20 bytes con la comisión de 3 —en centésimas de punto básico—
  entre ellos, sin longitud por medio; 66 bytes para dos saltos. No es una lista
  de direcciones como el `path[]` de V2, y una dirección de 19 bytes corre todos
  los bytes siguientes sin revertir al codificar: la forma se valida en el
  motor, no se supone.
- **Camino de claves (`PathKey[]`).** El camino del multi-salto de V4: cada
  clave nombra la moneda **de llegada** de su tramo —la de entrada va aparte, en
  el struct—, con la comisión, el espaciado de ticks y el gancho (siempre
  vacío: los pools con gancho no se sondean). Es un array ABI con su propia
  tabla de desplazamientos y un `hookData` que lleva dentro otro más. Se cotiza
  y se ejecuta con el mismo array: `quoteExactInput` lo devuelve entero
  (`0xca253dc9`, medido —el `0xcdca1753` que se recordaba es de otro
  cotizador—) y `SWAP_EXACT_IN` (`0x07`) lo ejecuta.
- **Impacto compuesto.** El de una ruta: los tramos **componen**
  (`1 - Π(1 - i_k)`), no se suman —dos tramos del 1 % cuestan 1,99 %, no 2 %—.
  Se calcula en un solo sitio (`combine_impact_bps`) para que dos motores no
  publiquen cifras distintas del mismo camino.
- **`Measurement`.** Procedencia de una cifra: `REPORTED` (la publica la
  fuente), `DERIVED` (la calculamos sobre datos publicados), `ESTIMATED` (con un
  supuesto documentado).
- **Comisión desconocida (`fee_bps is None`).** La fuente no la desglosa (caso
  medido: Jupiter). No significa «gratis»: `amount_out` sigue siendo neto. Esas
  cotizaciones se excluyen del cálculo de diferencial **neto**.
- **Comisión medida (`Deployment.fee_bps`).** La de un DEX de la familia V2,
  leída del contrato desplegado y no del nombre del protocolo: el
  `getAmountsOut` del router tiene que reproducir en aritmética entera la
  fórmula `x·y=k` con una única comisión candidata —y con ninguna otra—. Así se
  resolvió el **0,25 %** de PancakeSwap, cuya discrepancia entre repositorio y
  documentación zanjó el router real, y así entraron QuickSwap y SushiSwap. La
  tabla de direcciones y la de constantes (`amm_protocols.CONSTANT_FEES`) se
  contrastan al construir cada entrada: una comisión publicada que el router no
  cobra envenena el impacto y la tabla de precios sin que nada chirríe.
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
- **Tendencia.** Cuánto se ha movido el precio de un mercado en la última hora,
  día o semana, en **unidades de precio** —deltas por participación, no
  porcentajes—: el «sí» que pasó de 0.445 a 0.235 publica `oneDayPriceChange`
  de −0.21. La tarjeta la pinta tramo a tramo en verde/rojo y la tabla la enseña
  en céntimos («−23 ¢»); un tramo sin dato es un guion, nunca un cero. En la
  interfaz, la categoría **«Tendencia»** no es una etiqueta de la fuente sino
  una vista sintética: ordena por volumen de las últimas 24 h, que es lo que se
  está moviendo ahora.
- **Cuánto paga.** Lo que devuelve una participación si el resultado se cumple,
  al precio actual: ×(1/precio) y su porcentaje. A 0,62 paga ×1,61 (+61,3 %). Es
  bruto —no descuenta comisión ni gas— y se calcula con el precio límite del
  formulario, así que se recalcula al cambiar de resultado o de precio.
- **Categoría (etiqueta, `MarketTag`).** Cómo organiza Polymarket un mercado: la
  etiqueta del **evento** que lo contiene (`Politics`, `Crypto`…), con su `id`
  —con el que se pide el filtro a la fuente— y su `slug` —con el que se vuelve a
  comprobar de este lado—. Los mercados no traen etiquetas propias: se leen de
  `/events`, en una consulta de adorno que nunca tumba la lista. Se enseñan **tal
  cual las publica la fuente**, incluidas las suyas de organización
  (`Recurring`). Sin etiquetas no es «sin categoría»: es que no consta, y esos
  mercados no se descartan nunca al filtrar —no se puede afirmar lo que no se
  sabe—.
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
- **`SSLKEYLOGFILE` inyectada.** Variable de entorno con la que Avast (Web
  Shield) mete en los procesos una ruta de dispositivo (`\\.\aswMonFltProxy\…`).
  El `ssl` de Python la lee al crear cada contexto y con esa ruta el intérprete
  aborta (`OPENSSL_Uplink`), así que la aplicación la retira del entorno al
  arrancar —sólo si apunta a una ruta de dispositivo: una ruta normal es alguien
  depurando TLS a propósito, y se respeta—. Ver `infra/env_guard.py`.
- **Marcador `${NOMBRE}`.** Hueco dentro de un valor de `config.toml` —en
  práctica, la clave de API dentro de la URL de un nodo— que se rellena al
  arrancar desde el llavero (`app:NOMBRE`) o desde `AMIGOCOMPORA_NOMBRE`. Es lo
  que permite declarar un nodo propio en un fichero que no guarda secretos. Si no
  se resuelve, se descarta **ese endpoint** con un aviso que nombra la credencial;
  no se deja la URL a medias ni se sustituye por vacío, porque los dos síntomas
  —fallo de DNS, 401 sin explicación— no se parecen a la causa.
- **Nodo RPC.** Endpoint JSON-RPC declarado para una red, con su prioridad y su
  etiqueta. Se guarda en `config.toml` y se gestiona desde la pestaña
  Configuración —alta y edición en su ventana (`ui/node_dialog.py`), con la
  prueba antes de guardar y las acciones en cada fila propia—.
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
- **Asiento (del registro).** Una línea de `executions.jsonl`: lo que la
  aplicación **ejecutó**, con su red, su par o etiqueta, su motor, su importe de
  referencia, su hash y su desenlace. Lleva un **tipo** que decide si consume el
  tope del día: `transaction` (un gasto, cuenta según su estado), `order` (una
  orden publicada, cuenta siempre: el dinero queda comprometido aunque no haya
  tocado la cadena), `redeem` (un cobro), `receive` (la recepción en destino de
  un puente) y `approval` (el permiso de un ERC-20). Los tres últimos **no
  cuentan** —entra dinero o sólo se concede permiso—, y lo dice el tipo y no la
  cifra: un importe que un día deje de ser cero no debe cambiar el tope por
  sorpresa. Lleva además el campo **`tokens`**, los símbolos que toca, que es
  como se lee la actividad de un token sin depender del texto del par; los
  renglones anteriores al campo se emparejan por palabra exacta del par («USDC»
  encuentra «WETH/USDC»; «USDC.e» no). Ver
  `app/execution_policy.py` (`LedgerEntry`, `for_token`).
- **Escritura atómica con tomlkit.** Reemplazo de `config.toml` mediante un
  temporal y `replace`, con copia `config.toml.bak`. Conserva comentarios y el
  resto de bloques.

## Interfaz

- **Aviso y bloqueo (no son lo mismo).** Un **motivo** apaga el botón que firma y
  se enseña en rojo con «No se puede ejecutar»; un **aviso** se enseña en ámbar y
  el botón sigue encendido, porque lo que anuncia lo decide la firma. El caso
  medido: cruzar desde un token que no es la moneda de los topes —el importe se
  valorará al firmar y, si no se puede valorar, no se cruzará—. Confundirlos
  deja puertas que no se abren nunca.
- **«Tarjeta de mercado».** La tarjeta que **sustituye a la lista** en la pestaña
  de predicciones cuando se pulsa una fila, con «← Volver a la lista». Es el panel
  de operación de ese mercado —resultados, lado, precio límite, tamaño, libro— y
  se **compra por importe** ($1/$5/$20/$100 y campo editable) con las
  participaciones derivadas hacia abajo a la vista, mientras se **vende por
  participaciones** (5/10/25/50/100 y campo editable). Volver no la vacía —es un
  borrador—; una búsqueda nueva sí, porque el mercado puede haber dejado de estar
  en la lista. Las **reglas del mercado** —mínimo de participaciones y salto de
  precio— van siempre a la vista, bajo las dos caras: lo que publica la fuente,
  o «no consta» si no lo publicó, nunca los valores de socorro de los campos.
- **«Wallet de depósito» (panel).** El panel de la pestaña de predicciones —lo
  abre «Operar wallet…», el botón que va al lado del saldo en las dos caras de la
  tarjeta de mercado— que enseña la dirección de la wallet, su saldo del colateral
  y sus posiciones. La dirección se **deriva** (pura, sin red ni clave) y por eso
  se ve antes de leer nada; el saldo y las posiciones se **leen** al abrir el
  panel por primera vez o al pulsar «Leer la wallet». Cada fila de la tabla trae
  «Vender» y «Comprar más», que cierran el panel y **cargan la tarjeta de
  mercado** con el mercado, el resultado y el tamaño puestos: el precio y
  «Publicar» siguen siendo los de la tarjeta, con sus comprobaciones y su diálogo.
  Una fila ya resuelta apaga las acciones con el motivo escrito —se cobra, no se
  opera, y el cobro por este canal todavía no existe—. «Añadir saldo» tiene dos
  caminos: desde la cartera es la retirada de siempre con la wallet prefijada
  como destino, y desde fuera es la dirección con su QR.
- **Pata.** Uno de los dos lados de un par: lo que se entrega y lo que se recibe.
  En pantalla es un recuadro (`QFrame#legBox`) con su red, su importe, la píldora
  de su token y su saldo. Las dos patas se ven a la vez y del mismo tamaño, porque
  lo que se describe es un intercambio entre ellas.
- **Importe (convención de escritura).** Los importes se escriben y se leen con
  **punto** decimal en toda la aplicación —campos, tablas, recibos, exploradores—:
  es la cifra que viaja a la cadena. El campo de importe (`AmountSpinBox`,
  `ui/widgets.py`) fija esa convención en vez de heredarla de la configuración
  regional del sistema, y además acepta la **coma** al teclear —la tecla decimal
  del bloque numérico español—, normalizándola al punto al confirmar. La trampa
  medida el 2026-10-10: en el puente, un campo que *mostraba* con punto pero
  *interpretaba* con el sistema (donde el punto es separador de millares)
  convertía «0.000243» en «24» a mitad del tecleo; en la dirección contraria,
  teclear «0.5» daba «5» —diez veces el importe—. Por eso ninguna pantalla usa
  `QDoubleSpinBox` a secas: puentes, intercambio y predicción van por
  `AmountSpinBox`.
- **Tarjeta de ruta.** Una cotización de la lista de rutas, en dos líneas: arriba
  el icono del motor, el venue, lo que se recibe —en verde si es la mejor de la
  comparación— y la marca ámbar de las que sólo cotizan; debajo el desglose
  —comisión, impacto, liquidez— y, si la ruta cruza varios pools, su camino
  («WETH → USDC → …»). La segunda línea se recorta antes que envolverse —una fila
  por ruta, para comparar de un vistazo— y el texto entero se recupera en el
  tooltip, igual que la nota de la fuente. Sustituye a la fila de la tabla de
  ocho columnas, que mezclaba siete cifras de anchos distintos en cada línea.
- **Panel de detalle de ruta.** El panel de etiqueta y valor bajo la lista, que
  contesta «¿qué estoy a punto de firmar?» sobre **una** ruta: lo que se recibe,
  el camino, el motor, el precio, la comisión, el impacto, la liquidez, el
  deslizamiento máximo configurado, la cartera receptora, el coste de red
  estimado y el origen de la cifra. Es donde por fin se enseñan el camino, la
  cartera y el deslizamiento, que existían y no aparecían en ninguna parte.
  Sigue a la selección de la lista y, sin ninguna ruta elegida, lo dice en vez
  de quedarse en blanco.
- **Coste de red estimado.** Lo que puede costar emitir el swap, en la moneda
  nativa de su red: `gas_limit × max_fee_per_gas`, la misma semántica que
  `SignedTransaction.max_cost_wei`, así que es un **techo**, no un cobro. Se
  estima en el **paso ESTIMATE de la ejecución** —una llamada a la red por
  operación, y cotizar compara muchas— desde la cartera que firmaría y con el
  mismo mapa de emisores que firma. Va después de los permisos —concedidos
  éstos, la estimación ya no revierte por allowance— y antes del último «sí»:
  la cifra entra en el diálogo del swap y en la fila del panel. Sus estados no
  se confunden entre sí: «se estima al preparar» antes de que el paso llegue, la
  cifra con «≈», «no se pudo estimar» con el motivo en el tooltip —un fallo de
  estimación no frustra la operación: se decide con el motivo a la vista— y «—»
  para lo que no aplica (Solana no tiene `eth_estimateGas`). No incluye las
  aprobaciones del token, que son otras transacciones. Ver
  `app/usecases/estimate_cost.py` y `app/usecases/execute_swap.py`.
- **Deslizamiento por defecto.** La tolerancia con la que se construyen los
  swaps —el mínimo que un swap acepta recibir—, en puntos básicos. Se cambia con
  el **engranaje** de la cabecera de la tarjeta de intercambio, vive en
  `DefaultSlippage` (la configuración es inmutable, así que el número vivo no
  cabe en `Settings`, se lee sin reiniciar) y se guarda en `[execution]
  slippage_bps` de `config.toml`. Viaja **de verdad**: los seis motores que
  construyen reciben `slippage_bps` al construir el payload, y el panel de
  detalle enseña el valor vigente —no el de la configuración cargada—. Si la
  escritura del archivo falla, se dice: rige esta sesión, pero no se guardó.
  Ver `app/slippage.py`, `ui/slippage_dialog.py` e `infra/config_writer.py`.
- **Paso de ejecución.** Cada fase de una operación, narrada en vivo por el
  botón único (`SwapStep`: preparar, permiso del ERC-20, permiso en Permit2,
  coste de red, swap) y listada bajo los botones con su glifo (… corriendo, ✓
  hecho, ✕ rechazado o fallado), su hash —acortado, entero en el tooltip— y su
  enlace al explorador de la red cuando existe. Un **rechazo** del usuario y un
  **fallo** se listan los dos, pero no se pintan igual: decidir que no no es una
  avería. El panel no decide nada —pinta lo que le llega del caso de uso— y un
  observador que falle no tumba la transacción en marcha. «Empezar de nuevo»
  barre la lista y devuelve el botón al estado inicial sin perder la ruta
  elegida.
- **Saldo del token vendido.** Lo primero que la ejecución comprueba después de
  construir el payload y **antes** de aprobar nada: si la cartera no tiene lo
  que el swap va a mover, se corta con las dos cifras delante («tienes 0.550579
  pUSD y la operación mueve 1 pUSD») y no se firma ni se estima nada. Qué saldo
  se mira sale del mismo criterio que decide a quién se aprueba: el ERC-20 que
  el router moverá, o el nativo del `value` cuando es él quien viaja. Sin este
  corte, un déficit solo se descubría al estimar, como un `execution reverted:
  STF` del router que no dice ni cuánto hay ni cuánto falta —medido el
  2026-10-10, con pUSD en Polygon—. Ver `InsufficientBalanceError` en
  `domain/errors.py` y `_require_sold_balance` en `app/usecases/execute_swap.py`.
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
- **Fila de token (cartera).** Un token de la lista de la cartera: el logo
  redondo con la insignia de su red, el nombre —el símbolo a secas; el desempate
  de homónimos vive en el tooltip— y, en su propia columna a la derecha, la
  cantidad y el valor alineados al borde. La **fila entera** es el botón que
  abre su detalle —al soltar el ratón dentro y con Enter o espacio—, y por eso
  no lleva ningún botón dentro. La cantidad va **recortada** a cuatro decimales
  (nunca redondeada hacia arriba: 19,49166 se enseña 19,4916, y la cifra entera
  vive en el tooltip), y el valor con **dos decimales**; un saldo que no llega al
  cuarto decimal se enseña entero, porque «0.0000» se lee como «no hay nada».
- **Mandos de la lista (cartera).** Los tres controles de la cabecera de «Tokens
  con saldo»: la **lupa** despliega el buscador —y al plegarlo lo vacía, para que
  no quede un filtro escondido junto a su campo—, el **«+»** añade un token por
  la dirección de su contrato y el botón de releer los saldos. La barra de
  búsqueda fija y el botón «+ Añadir token» que había antes gastaban una fila
  entera del panel.
- **Selector de red (cartera).** La línea «▼ Red: Base» —o «▼ Todas las redes»
  cuando no hay ninguna puesta— que filtra la lista sin volver a leer nada.
  Abre un modal con las redes que esa cartera puede leer —«Todas las redes» la
  primera, sin icono porque no es una red— y sustituye al desplegable «RED» que
  vivía en la cabecera de la cartera: el filtro es de la lista. Un clic en una
  fila elige y cierra; el botón «Elegir» se queda para el teclado.
- **Insignia de red.** El logo de la red sobre la esquina inferior derecha del
  logo del token, dentro de un cuadro de esquinas redondeadas del color de la
  tarjeta —el token se recorta en redondo y la insignia en cuadrado, y así cada
  pieza se distingue de la otra—. Es lo que distingue el USDC de Polygon del de
  Base en la lista. Se compone en la interfaz (`badged_token_pixmap`) y no en
  `ui/icons.py`, que sólo resuelve ficheros. Una red sin logo no dibuja
  insignia: un logo de relleno diría «esta red tiene marca y es ésta».
- **Detalle del token (cartera).** La vista que sustituye a la lista al pulsar
  una fila, con «← Volver a la lista»: nombre y contrato —abreviado a la vista,
  entero en el tooltip, con botón de copiar que acusa el copiado; la moneda
  nativa no lo tiene, porque no hay contrato que copiar—, precio por unidad,
  cantidad y valor, los botones **Enviar** (la retirada con este token ya
  elegido), **Recibir** (el QR en la red de este token) y **Swap** (lleva el
  token a la tarjeta de conversión), y debajo su «Actividad». Un detalle abierto
  se recuelga de la última lectura —enseña el saldo de ahora, no el de cuando se
  pulsó— y se cierra solo si su token desaparece de ella.
- **«Actividad» (cartera).** La lista de asientos del registro que tocan a un
  token **y su red**, del más nuevo al más viejo, cada uno con su etiqueta —Envío,
  Swap, Puente, Recepción, Cobro, Orden, Permiso o Movimiento, decidida por el
  tipo del asiento y por la ranura del motor que lo ejecutó—, su descripción, su
  estado cuando no es `success` y su hash abreviado. Es lo que **esta
  aplicación** ejecutó, y sólo eso: una transferencia que llegue de fuera no
  pasa por el registro y no aparece, así que la lista vacía lo dice con esas
  palabras en vez de dejar creer que no pasó nada.
- **Subpestañas de Configuración.** La pestaña son tres —**Nodos RPC**, **APIs de
  motores** y **Credenciales**— y cada credencial vive en **un solo sitio**: la
  clave de un nodo en la ventana de ese nodo, las de motor y su modelo en APIs,
  y lo que es de la aplicación entera —la clave que firma, la frase de la
  autonomía, Polymarket— en Credenciales. Antes el mismo dato estaba en dos
  vistas a la vez —una tabla de sólo lectura y una casilla por opción—, que es
  lo que se separa en cuanto una cambia. Las tablas reparten el ancho por
  contenido: cada columna mide lo suyo y la larga —la URL— se lleva lo que
  sobra. Ver `ui/pages/settings.py`.
- **Ventana de nodo RPC.** La ventana —una sola para agregar y editar— que abren
  «Agregar nodo…» y el lápiz de cada fila propia de la tabla de nodos; la ✕ de
  la fila elimina el nodo de `config.toml` (su clave, si la tenía, sigue en el
  llavero). Un nodo **nuevo** sólo se guarda si la prueba (`eth_chainId`)
  respondió con la red elegida; al editar sólo se exige si cambió la URL o se
  escribió una clave nueva —etiqueta y prioridad no son nada que la prueba
  mida—, y la red no se cambia: mudar un nodo de red sería borrarlo y crearlo en
  otra, gesto que ya existe y donde la prueba vuelve a exigirse. La clave va al
  llavero (`app:NOMBRE`) y al fichero sólo el `${NOMBRE}` de la URL; dejarla en
  blanco al editar **no** borra la guardada, y escribir una nueva la reemplaza.
  Ver `ui/node_dialog.py`.
- **APIs de motores (subpestaña).** Una fila por motor registrado, con el estado
  de cada opción de su manifiesto —«api_key: configurada», «model: …»—, y el
  lápiz abre la ventana donde se escriben. Se guardan en el llavero como
  `<motor>:<opción>`; el **modelo** —que no es un secreto: viaja en cada
  petición— se lee en claro, y en blanco es el de fábrica del motor. Guardar una
  clave aquí **no** enciende el motor —eso se decide en la pestaña Motores— y se
  dice, porque una clave guardada que nadie usa parece estar funcionando. Un
  campo en blanco no borra lo guardado: para eso está «Borrar las guardadas».
  Ver `ui/pages/apis.py`.

## Copiloto

- **Turno del copiloto.** Una pregunta y todo lo que la aplicación hace para
  contestarla: pedir herramientas a la red, devolverle los resultados al modelo y
  quedarse con su respuesta. Se resuelve en `app/copilot.py` —un bucle con
  presupuesto de pasos (MAX_STEPS)— y se narra mientras ocurre: `TOOL` abre una
  fila en la burbuja, `TOOL_RESULT` la cierra, `ERROR` añade la suya en rojo y
  `ANSWER`/`PROPOSAL` se leen en el texto. Un turno puede acabar en **respuesta**
  o en **propuesta**, nunca en una firma: el copiloto no tiene `sign` ni
  `broadcast` en su contrato y su dataclass es inmutable por construcción.
- **Instrucciones de respuesta (el protocolo del copiloto).** El texto que le
  cuenta al modelo, en cada vuelta, las tres formas de contestar —pedir una
  herramienta, responder o proponer— viaja en el **contexto**, bajo
  `instrucciones_de_respuesta`, y no dentro de la pregunta: para los proveedores
  las dos cosas acaban en el mismo mensaje del usuario, así que el modelo lo lee
  igual, pero la pregunta se queda con las palabras de la persona, que son lo que
  la aplicación enseña cuando el modelo no contesta. Y **un fallo del modelo se
  dice con su motivo**: `_sin_respuesta` nombra el modelo y la causa —el error de
  TLS, el HTTP, la respuesta vacía— en vez de devolver un texto que nadie
  escribió. `_offline_result` queda para el asistente offline, que es el que
  promete contestar sin red.
- **Lo que se consultó.** La letra pequeña plegada de cada respuesta: los
  hallazgos y la salida en crudo de cada herramienta, con la fecha del turno. Es
  la prueba de dónde salió cada cifra, y por eso se guarda con el mensaje
  (`ChatMessage.detail`). Se abre para comprobar una cifra, no cada vez: por eso
  nace plegada y con lo consultado recortado a `MAX_ACTIVITY_CHARS` (el texto
  entero vive en el tooltip).
- **Historial de chats.** Un fichero JSON por conversación bajo
  `chats/` del directorio de configuración (`app/chat_store.py`), y no un
  documento con todos: escribir un chat no toca a los demás y un JSON a medio
  escribir se lleva por delante **ese** chat, no el historial. El título se
  deduce del primer mensaje del usuario, el mensaje se guarda antes de
  preguntar, y un fichero ilegible se salta con un aviso —el historial es
  memoria, no un estado del que dependa arrancar—. **Archivado no es borrado**:
  el chat apartado sigue explicando por qué se hizo lo que se hizo, así que su
  sección es una lista plegada aparte, y sólo borrar pregunta.
- **Tarjeta de propuesta.** Lo que el copiloto propone —un swap o un puente— con
  su cotización observada, un aviso de que **todavía no se ha firmado nada** y el
  botón «Revisar y ejecutar». No ejecuta por su cuenta: la lista de lo que falta
  se la pasa la página desde `ui/execution_gate.py` —las mismas funciones que
  consultan las otras pantallas— y el botón entra por `execute_swap` /
  `execute_bridge`. Una propuesta ya ejecutada no vuelve a encenderse aunque el
  estado cambie (`mark_spent`): la cifra que llevaba ya se gastó. Y una
  propuesta **reabierta** de un chat guardado no se reofrece: la cotización ya es
  vieja, así que para ejecutar se vuelve a preguntar.
- **Desplegable de modelo.** El mando con el que se elige con qué asesor de IA
  conversa el copiloto. El copiloto pregunta a **uno** —el preferido de la
  ranura—, así que al elegir se enciende el nuevo **antes** de apagar los demás:
  en el peor caso quedan dos encendidos, que es lo que ya había, y nunca ninguno.
  Lo que se apagó se nombra, y volver a encenderlo es cosa de la pestaña de
  Motores: un mando que además tocara otras ranuras haría dos cosas.
- **Tira de autonomía.** La franja de la pestaña del copiloto donde se lee el
  estado de la ejecución desatendida —armada o no, con el color de peligro
  cuando lo está—, el gasto de las últimas 24 h **desglosado por unidad** y los
  topes. Un total sin unidad no dice si son cien dólares o cien mil, y es la
  cifra que se mira para dejar que la aplicación firme sola. Armar pide la frase
  de autonomía con el texto oculto; desarmar no pregunta, no pide frase y no
  falla nunca, porque un freno que puede negarse a frenar no sirve.
