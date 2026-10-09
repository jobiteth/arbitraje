# Changelog

El formato sigue [Keep a Changelog](https://keepachangelog.com/es/1.1.0/) y el
versionado [SemVer](https://semver.org/lang/es/).

## [No publicado]

### Añadido — Fase 6: la wallet de depósito en la pestaña de predicciones

- Tarjeta **«Wallet de depósito»** en la pestaña de predicciones, entre la fila de
  tabla/tarjeta de orden y «Cestas con margen»: la dirección de la wallet —que se
  **enseña antes de leer nada**, porque es una derivación pura y es lo que permite
  recibir un depósito en una instalación recién configurada— con «Copiar» y «Ver
  QR», el saldo del colateral, «Leer la wallet» y «Añadir saldo…».
- **Tabla de posiciones** (Mercado · Resultado · Participaciones · Precio medio ·
  Valor · Resultado · acciones) con el resultado abierto en verde/rojo y una línea
  de total «En conjunto» que suma sólo lo conocido y lo dice. Cada fila trae
  **«Vender»** y **«Comprar más»**, que **cargan la tarjeta de orden** de encima
  —el mercado, buscado por su `conditionId`, y el resultado por su `token_id`, no
  por la etiqueta—: vender propone la posición entera truncada hacia abajo a los
  decimales de la participación (el campo redondearía hacia arriba) y comprar más
  propone el mínimo del mercado. Cargar **no firma nada**: el precio y «Publicar»
  siguen siendo los de la tarjeta de orden, con sus comprobaciones y su diálogo.
- **Añadir saldo, dos caminos explícitos.** «Desde mi cartera» es la **retirada de
  siempre** con la wallet prefijada como destino (`WithdrawDialog` con
  `recipient`): mismo modo, mismos topes, misma confirmación y mismo asiento — la
  tarjeta no duplica nada. «Desde fuera» enseña la dirección y el QR
  (`DepositDialog` con una nota: aquí sólo hace falta pUSD, y en Polygon).
- `ReadSettlementWallet`: caso de uso **de sólo lectura**, sin gateway. La
  dirección sale del planificador —derivación pura— y las claves se piden contra
  un protocolo mínimo nuevo (`AddressSource`, no `PrivateKeySource`): enseñar un
  saldo no puede leer la clave privada, y una prueba lo mide en vez de prometerlo.
  El saldo se lee por el emisor (`token_balance`) y las posiciones por el cobrador
  (`positions(redeemable_only=False)`: aquí lo abierto es lo que se opera).
- `PredictionMarketEngine.market_by_condition(condition_id)`: gamma
  `/markets?condition_ids=…` —medido contra la API real antes de implementarlo—
  cierra el camino de una posición a su mercado; `_to_market` ya mapeaba
  `conditionId`.
- `PredictionPosition` gana `avg_price`, `cash_pnl` y `percent_pnl` opcionales,
  mapeados de `avgPrice`/`cashPnl`/`percentPnl` de la fuente: si no vienen, quedan
  en `None` — la tarjeta calla en vez de inventar un cero.
- Tras publicar una orden con éxito o tras un ingreso, si la tarjeta ya estaba
  leída se vuelve a leer sola: una compra baja el saldo y sube posiciones, y
  sospechar de un dato viejo cuesta más que la relectura.
- Lo que queda fuera: **cobrar posiciones resueltas de la wallet** —el cobro por
  el canal de la wallet (lote del relayer) no existe todavía—. Sus filas apagan
  las acciones con el motivo escrito, no con un botón que no haría nada.

### Corregido — La lista blanca de tokens comparaba por caja

- `ExecutionLimits.check_token` comparaba el símbolo en crudo contra la lista
  normalizada: `pUSD` —el colateral del recinto de predicción— y `BTC.b` nunca
  casaban con `PUSD` o `BTC.B` declarados en `config.toml`, y el error decía
  «pUSD no está» mientras enseñaba `PUSD` en la propia lista. Ahora se normalizan
  **los dos lados** —también los límites construidos a mano, que pueden llegar sin
  normalizar—. Bloqueaba justo la retirada que fondea la wallet de depósito.

### Añadido — Polymarket: canal de la deposit wallet, verificado con una compra real

- El caso de uso de órdenes opera por la **deposit wallet** cuando hay credenciales
  del relayer (`PolymarketWalletChannelProvider`, llavero): despliega la wallet si
  no tiene código, comprueba los fondos **antes de firmar** —sin moverlos: no hay
  fondeo automático y el error nombra las dos direcciones— y concede el permiso del
  colateral por el relayer **por el máximo** —lo exige el relayer; contradice la
  regla del camino de la EOA y el diálogo lo dice con esas palabras—, espera su
  confirmación y relee el permiso en la cadena. Sin credenciales se opera por la
  EOA, con las reglas de siempre.
- El diálogo nombra las dos direcciones —«Firma la clave» y «Opera la wallet de
  depósito (es la que paga y cobra)»— y el **destinatario efectivo es la wallet**:
  es su `maker`, y ahí quedan las participaciones al comprar (y el colateral al
  vender). Medido con la compra real: las 5 participaciones aparecieron en la
  wallet y ninguna en la EOA.
- `GET /transaction?id=…` devuelve una **lista** de fichas: se elige la del
  identificador pedido, y una ficha con **otro** identificador no se lee como
  estado propio aunque venga sola.
- Verificado de punta a punta con una compra real (2026-10-09, `5 Yes` del mercado
  665374, límite 0.20, cruzada a 0.16): el recinto aceptó la firma tipo 3 enviada
  con las credenciales L2 de la EOA —el mismo camino que el cliente oficial
  (`clob-client-v2` usa el EOA en `POLY_ADDRESS` también para órdenes tipo 3)—,
  `status=matched`, `size_matched` 5/5, y la API pública de posiciones confirmó las
  5 participaciones en la wallet. La wallet se recargó con una retirada real
  (0.15 pUSD) por el camino de la aplicación.
- **El camino de venta quedó verificado** con la venta real de esas 5 «Yes»
  (2026-10-09, límite 0.15, cruzada): la primera venta de la wallet ejercitó
  `_approve_wallet_shares` —`setApprovalForAll` de la colección ERC-1155 al
  recinto, por el relayer, sin gas, sin importe y sin asiento; el diálogo lo
  advierte—, se releyó el permiso en la cadena, y la orden tipo 3 (vendedor =
  wallet) salió `status=matched`, `size_matched` 5/5. El colateral cobrado
  (0.75 pUSD) apareció en la wallet a los segundos y la posición desapareció de
  la API pública.
- Regla del recinto aprendida en la prueba: una **compra** que **cruza el libro**
  se rechaza si su importe es menor de $1 («invalid amount for a marketable BUY
  order, min size: $1»). No es un límite de la aplicación y el rechazo llega tal
  cual del recinto. La venta medida no lo tiene: una **venta que cruza** de 5
  participaciones (el mínimo del mercado, $0.75) se aceptó; a la venta le aplica
  el tamaño mínimo del mercado, no el $1 de la compra.

### Añadido — Iconos de marca en los listados

- `ui/icons.py` resuelve, por red, token y motor, el fichero de su icono y lo
  cachea. Sin fichero devuelve un icono vacío y el listado se queda con su texto,
  exactamente como antes: **ningún logo de relleno** —un logo ajeno en una fila de
  dinero es una marca que el usuario no eligió—, y el nombre **nunca** se
  sustituye por el logo. Un fichero ilegible o con la extensión cambiada tampoco
  lo estropea: se intenta cargar por contenido (un PNG o un WebP guardados como
  `.svg`) y, si no, queda vacío.
- Un token o una plataforma sin logo propio enseñan su **genérico** —`token.svg`,
  `plataform.svg`—: no fingen ser una marca, dicen sólo «esto es un token» o
  «esto es un proveedor». Las redes no tienen genérico; sin el fichero, la fila
  se queda con su texto.
- Cada tipo se busca en dos carpetas: la que se mantiene a mano —`assets/chains/`,
  `assets/coin/`, `assets/plataform/`— y la de web3icons —`networks/`, `tokens/`,
  `brands/`, que es donde escribe el script de descarga—. Manda la primera: una
  descarga no puede tapar un fichero elegido a mano.
- El nombre del fichero no tiene que ser exacto. En las redes: el slug de
  web3icons, la clave cruda (`arbitrum.svg` donde el catálogo publica
  `arbitrum-one`) y los sinónimos de casa (`avalanche` → `avax.svg`). En los
  tokens: el símbolo tal cual, sin puntos y en minúsculas (`usdce.svg` para
  `USDC.e`, `btcb.svg` para `BTC.b`) y, sólo al final, el alias a la moneda base
  (`WETH` → `ETH`), que es una aproximación.
- El icono acompaña al nombre en los listados —desplegables de red y de token,
  tablas de saldos, rutas, precios, motores y nodos— y el nombre va también en el
  tooltip. Fuera de los listados (avisos, mensajes, títulos) no se pone ninguno.
- Las traducciones de slug que web3icons necesita (`bsc` → `binance-smart-chain`,
  `arbitrum` → `arbitrum-one`, el USDC puenteado → USDC, el envoltorio nativo → su
  moneda) viven en una sola tabla, la misma que usa el script de descarga.
- `tools/fetch_icons.py` descarga los SVG en la variante «background» —el logo
  sobre el cuadrado de su marca, el mismo estilo de las carpetas de casa— de los
  nombres que la aplicación usa de verdad (redes del registro, tokens del catálogo
  y motores). No pisa lo que ya está, ni vuelve a bajar lo que la aplicación ya
  resuelve —lo dejaría compitiendo con lo que se ve—, usa el contexto TLS de la
  aplicación (almacén del sistema operativo, sin `SSLKEYLOGFILE`) e informa de los
  nombres que no existen en web3icons, cuyo logo, si se quiere, viene de su propio
  kit de marca y con su propia licencia.

### Cambiado — «Entre redes» con las dos patas en recuadro

- Origen y destino son ahora dos recuadros, uno por pata, como en la tarjeta de
  intercambio: cada uno lleva su red arriba, el importe en grande, la píldora del
  token a la derecha y el saldo de **esa** red y **ese** token debajo. Se quitan las
  dos filas de campos y la fila común de saldos, donde no se sabía de qué mitad
  hablaba cada cifra.
- «Máx» pone todo el saldo disponible de la red de origen, truncando hacia abajo a
  los seis decimales del campo —nunca por encima del saldo— y diciendo en la línea
  de estado lo que no cupo. Sin saldo el botón queda apagado, no escondido, y el
  tooltip explica por qué.
- «Invertir» cambia las dos patas de sitio, con su red y su token; el token se busca
  por su equivalente en la red de enfrente (`USDC.e` ↔ `USDC`). No firma ni emite.
- Desaparece el rótulo de unidad junto al importe: el símbolo ya lo lleva la píldora
  del token, en la misma fila.
- El desplegable de red se ajusta a su contenido en vez de estirarse a lo ancho de
  la tarjeta.

### Corregido — El importe grande no se aplicaba

- La regla de 26 px del importe se escribía para `QLineEdit#amountInput` y el campo
  es un `QDoubleSpinBox`: un selector de tipo de Qt no cruza la jerarquía, así que
  la regla nunca llegó a aplicarse —tampoco en la tarjeta de intercambio—. Ahora el
  selector nombra las tres clases y apaga el borde del `QLineEdit` interno.

### Añadido — Modal de un puente: tiempo por paso y total congelado

- Cada paso de la modal muestra cuánto lleva o cuánto tardó. Los pasos en curso
  cuentan en vivo; al terminar el cruce el tiempo de cada paso y el total quedan
  congelados. El título de la ventana empieza por «Info del puente».
- `TrackedBridge` guarda los instantes `created_at`, `provider_at`, `attested_at` y
  `finished_at`. Los registros anteriores sin estos campos muestran «—».

### Cambiado — Rejilla de rutas legible

- Las cantidades muestran como mucho 6 decimales, sin ceros sobrantes, alineadas a
  la derecha. La cantidad exacta sale en el tooltip.
- Los títulos de columna son cortos («#», «Puente», «Motor», «Mínimo»); el nombre
  completo se lee en el tooltip de la cabecera.
- Cada celda mide su contenido, sin anchos fijos. Una nota de más de 60 caracteres se
  recorta con «…» y se lee entera en el tooltip.
- La tabla de motores va plegada detrás de un botón «Mostrar motores».

### Cambiado — Espera de confirmaciones de Circle visible

- Mientras Circle espera la finalidad de la red de origen, la modal indica cuánto
  tarda (en Base, unos 65 bloques de Ethereum, entre 15 y 20 minutos).

### Cambiado — Tabla de transacciones compacta

- Los hashes se muestran abreviados (`0xd5210...09A90`) con un icono para copiar el
  hash completo. Las columnas se ajustan al contenido.

### Añadido — Reclamar la recepción en destino (Circle CCTP)

- Al quemar, Circle atesta el mensaje y la app emite `receiveMessage` en destino sola,
  pasando por la confirmación habitual. Estados «Reclamando», «Reclamado» y
  «Reclamo fallido»; un fallo se reclama a mano desde Transacciones.

### Añadido — Circle CCTP: atestación y `receiveMessage` (sin envíos)

- `engines/circle/attestation.py` consulta `GET /v2/messages/{dominio}?transactionHash=`
  en Iris y devuelve el mensaje y la atestación sólo con estado `complete`. Un 404 se
  trata como pendiente. Codifica el calldata de `receiveMessage` para el
  `MessageTransmitterV2` de destino. Pruebas sin red.
- `CircleEngine.track_bridge` informa del estado de la quema (no indexada, esperando
  confirmaciones o atestada). Nunca da el cruce por entregado: la recepción en destino
  todavía no la completa la aplicación.

### Cambiado — Seguimiento de puentes por motor

- Cada cruce lo sigue el motor que lo emitió (`TrackedBridge.engine_id`), y no el primer
  motor activo. Los registros anteriores sin motor siguen usando el primero que sepa seguir.
- La modal de un cruce de Circle ya no muestra el enlace de seguimiento de LI.FI.

### Añadido — Polymarket: firma de órdenes con deposit wallet (tipo 3, sin envíos)

- `orders.sign_order` acepta `wallet` y firma con `signatureType` 3, `maker` = `signer`
  = la wallet, con la envoltura ERC-7739 del cliente oficial. Se comprueba que la firma
  recupera la EOA de la clave. Las pruebas son sin red.
- La publicación de la orden envía la firma tipo 3 con las credenciales L2 de la
  EOA; el recinto la aceptó tal cual (ver «canal de la deposit wallet» más arriba).

### Añadido — Polymarket: cliente del relayer (fase 2, sin envíos reales)

- `engines/polymarket/relayer.py` prepara `WALLET-CREATE` (Builder key) y lotes
  `WALLET` firmados (Relayer key o Builder key), y consulta nonce y estado. Ninguna
  llamada se ejecuta todavía desde la aplicación.
- La firma de lote EIP-712 coincide con un vector producido con viem; la firma HMAC de
  la Builder key se recalcula de forma independiente en los tests.
- La pestaña de credenciales guarda en el llavero la Clave API del relayer, su
  dirección y la Builder key (clave, secreto y frase de paso), todos enmascarados.

### Añadido — Polymarket: cálculo de la deposit wallet (fase 1, sin red)

- `engines/polymarket/deposit_wallet.py` calcula la dirección de la deposit wallet por
  CREATE2 y la envoltura ERC-7739 de la firma de una orden tipo 3. Ambas coinciden con
  los clientes oficiales (`builder-relayer-client` 0.0.10 y `clob-client-v2` 1.2.0) en
  vectores fijados en `tests/unit/test_deposit_wallet.py`.
- No hay todavía ninguna operación contra el relayer ni el recinto. Ver
  `docs/POLYMARKET_DEPOSIT_WALLET.md`.
- El colateral de Polymarket pasa de USDC.e a pUSD (documentación oficial; contrato
  medido en Polygon: símbolo `pUSD`, 6 decimales). El catálogo de Polygon incluye pUSD.

### Añadido — Seguimiento de puentes: modal de pasos y transacciones

- Tras emitir un puente, una modal enseña cada paso: origen emitido, registro en el
  proveedor, puente en curso y entrega en destino. El check verde «Puente completado»
  sólo aparece cuando el proveedor confirma la entrega; un reembolso o un fallo nunca
  se muestran como éxito.
- Sección «Transacciones» en la pestaña de puentes: estado con emoji (🔵 en espera,
  ✅ completado, 🟡 reembolso, ❌ fallido), enlaces al explorador del origen y del
  destino y al seguimiento de LI.FI, y botón «Ver» para reabrir la modal.
- Los cruces se guardan en `bridge_tracking.json` y se vuelven a sondear al arrancar
  mientras no tengan estado final. Sólo lee: no firma ni emite nada.
- El seguimiento lo lee `LifIEngine.track_bridge` desde `/v1/status`.

### Cambiado — Tarjeta «Operar» de Predicción

- La tarjeta de orden tiene el aspecto de un panel de operación: Comprar/Vender y
  resultado como botones, precio límite con −/+, participaciones con atajos
  −100, −10, +10, +100, +150, y un botón azul «Realizar orden de compra».
  Los combos ocultos siguen siendo la fuente de verdad.

### Corregido — Colateral con `allowed_tokens = ["*"]`

- Predicción bloqueaba la orden aunque `allowed_tokens` declarase el comodín:
  sólo comparaba con el símbolo del colateral. Ahora acepta `ANY_TOKEN`.

### Añadido — Pestaña Errores

- Pestaña **Errores** con los avisos y fallos de la sesión, más recientes primero,
  con filtro por nivel, «Actualizar» y «Limpiar». Se alimenta del log **después**
  de la redacción de secretos, así que no muestra claves.
- El diario guarda sólo las últimas 500 entradas en memoria; no escribe en disco.

### Añadido — Pestaña Configuración: nodos RPC y APIs

- Pestaña **Configuración** con los nodos RPC: añadir un nodo (red, URL, prioridad
  y clave opcional), probarlo con `eth_chainId` antes de guardarlo y eliminar los
  que se declararon en `config.toml`.
- La clave del nodo va al llavero con el nombre `app:NOMBRE` y la URL guarda el
  marcador `${NOMBRE}`; ni la clave ni su valor llegan a `config.toml` ni al log.
- Los nodos se escriben con **tomlkit** (dependencia nueva): sólo se reemplaza el
  bloque `[[chains]]` afectado, se conservan los comentarios, se valida el
  resultado con el modelo antes de escribir y se deja `config.toml.bak`.
- Los nodos nuevos se usan **al reiniciar** la aplicación: los pools se construyen
  al arrancar.
- **Motores** queda sólo para activar y desactivar motores. Las credenciales se
  mueven a Configuración.
- Respaldos públicos para Arc y Robinhood, que se leen también en la cartera.
  Sus endpoints tienen límite de peticiones y Arc es lento (≈2,5 s).

### Corregido — La pestaña «Entre redes» se abría sin ningún motor de puentes

- **Síntoma.** La pestaña dejaba elegir redes, tokens e importe, y al pulsar
  «Buscar rutas» contestaba *«ningún motor activo sabe cruzar desde «base»: no hay
  a quién preguntar»*. No era una red caída ni una ruta inexistente: en una
  instalación nueva **nunca** había un motor de puentes activo.
- **Causa.** `DEFAULT_ENGINE_IDS` no tenía entrada para la ranura `cross_chain`, y
  el arranque sólo autoactiva sin valor por omisión cuando hay **un** motor
  instalado de esa clase (`sole_engine_for`). Desde que hay dos puentes —LI.FI y
  Relay— ninguno es el único, así que la ranura se quedaba vacía con un
  `engine.slot_empty` en el log y nada más.
- **Arreglo.** La ranura trae **LI.FI** por omisión. No es una preferencia de
  gusto: Relay declara su clave en `required_config`, así que autoactivarlo sin
  credencial fallaría en el arranque y dejaría la ranura igual de vacía. LI.FI
  cotiza y construye sin clave, y su manifiesto ya se declaraba «antes que Relay»
  con `bridge_priority=50`. Relay sigue disponible para apilarlo en cuanto tengas
  su clave.
- Medido contra la API después del arreglo: `0,01 ETH` Base → Polygon devuelve
  ruta por NearIntents (252,28 POL, mínimo 251,02, 39 s) y `50 USDC` Base →
  Polygon por AcrossV4 (49,86 USDC, comisión 0,138, 1 s).
- **Por qué no lo vio ninguna prueba.** `test_container_builds_with_all_slots`
  comprobaba «las tres ranuras» porque tres eran las que había cuando se escribió,
  y siguió en verde al añadirse puentes y cartera; las pruebas de la pestaña, por
  su parte, activaban LI.FI a mano y usaban «no declarar nada» como forma de
  apagar la ranura, que era exactamente la configuración real. Ahora la prueba
  recorre `EngineKind` entero —una ranura nueva sin valor por omisión rompe ahí— y
  otra exige que ningún motor por omisión tenga `required_config`.

### Cambiado — Una ranura declarada vacía se queda vacía

- `cross_chain = []` en `config.toml` apaga la ranura a propósito, y ya no es lo
  mismo que omitir la clave: sin la clave rige el valor por omisión. Es la misma
  lectura que `allowed_tokens`, donde una lista vacía significa «nada» y no «sin
  restricción». Hasta ahora las dos formas acababan en el mismo sitio, así que
  apagar una ranura no se podía ni escribir.

### Corregido — La ruta que se puede firmar desaparecía de la comparación

- **Síntoma.** Con `geckoterminal` por delante de `uniswap_v3` en la pila, la
  fila de Uniswap se esfumaba de la tabla; con importes algo mayores no quedaba
  **ninguna** ruta ejecutable y el botón de firmar se apagaba sin decir por qué.
  Nada aparecía en `failed_engines`, así que el diagnóstico apuntaba a la red
  cuando la causa era determinista y estaba en el código.
- **Causa.** La deduplicación por `venue_id` se quedaba con el **primero de la
  pila**. `venue_id` nombra el pool (`uniswap-v3@5`), no la fuente, así que
  cuando el pool elegido por GeckoTerminal coincidía con el del motor directo,
  ganaba la observación de GeckoTerminal —que sólo sabe leer— y se descartaba en
  silencio la de `uniswap_v3`, que es la única que `PrepareSwap` puede convertir
  en transacción (resuelve el constructor por `quote.engine_id`).
- **Arreglo.** El desempate entre dos mediciones del mismo pool ya no lo decide
  el orden de la pila sino **quién sabe construir el swap**, consultado a
  `registry.planners_for(chain)` —manifiesto, sin tocar la red— antes de cotizar.
  No se elige la mejor cifra: se elige la que se puede firmar. Entre dos que
  construyen, sigue mandando la pila; el desacuerdo de importes se sigue
  registrando en el log.
- Medido en Polygon con POL/USDC: antes, con 5 POL salían tres rutas y la de
  `uniswap_v3` no estaba; con 25 POL las dos rutas eran de `geckoterminal` y no
  había nada firmable. Después, `uniswap-v3@5` sale con motor `uniswap_v3` en
  los cuatro importes probados.

### Añadido — Las claves de los nodos RPC se escriben desde la aplicación

- La pestaña «Motores» → «Credenciales» ofrece ahora una casilla por cada
  `${NOMBRE}` que aparezca en las URL de `config.toml`, con el nodo y la red en
  los que se usa. **La lista de proveedores no está escrita en la interfaz**: se
  lee del fichero, igual que las credenciales de motor salen del manifiesto, así
  que Infura, Alchemy, QuickNode o un nodo propio aparecen sin tocar código.
- Y una fila «otra credencial» de nombre libre, que rompe el huevo y la gallina:
  la clave se puede guardar **antes** de que ninguna URL la nombre, y entonces la
  nota lo dice con todas las letras —«guardada, pero ningún nodo la usa
  todavía»— junto al `${NOMBRE}` que hay que escribir. Una credencial que nadie
  lee es justo lo que parece estar funcionando.
- La ayuda de estas claves **no** arrastra la condición de `allow_env_key`: ese
  interruptor gobierna a los proveedores que firman, no a los marcadores de
  `config.toml`, que el contenedor resuelve siempre. Contarlo aquí mandaba a
  encender una opción que no tiene nada que ver.
- Un marcador sin resolver **descarta sólo ese endpoint**, con un aviso que
  nombra la credencial y su variable de entorno; la red sigue funcionando con
  los respaldos públicos y el arranque no se interrumpe.

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
  ruido con aspecto de oportunidad. Se queda **una**, y **no** la mejor de las
  dos —eso sería escoger la cifra que más conviene de dos observaciones del
  mismo pool—; el desacuerdo entre fuentes queda en el log. El criterio del
  desempate se corrigió después (ver «Corregido — La ruta que se puede firmar
  desaparecía de la comparación»): manda quién construye el swap, no el orden
  de la pila.
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
