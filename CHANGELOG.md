# Changelog

El formato sigue [Keep a Changelog](https://keepachangelog.com/es/1.1.0/) y el
versionado [SemVer](https://semver.org/lang/es/).

## [No publicado]

### Arreglado — El importe se escribe como se lee: «0.000243» ya no se convierte en «24»

- **«Si escribo 0.000243 me lo convierte en 24.»** Medido el 2026-10-10. El campo
  del puente **mostraba** con punto —como toda la aplicación: tablas, recibos,
  exploradores— pero dejaba que Qt **interpretara** con la configuración regional
  del sistema: en español el punto es el separador de millares, así que los
  dígitos se concatenaban («0000243» → 243, y a mitad del tecleo, 24). En los
  campos del intercambio la trampa era la misma al revés: con la coma como
  separador decimal del sistema, teclear «0.5» daba «5» —diez veces el importe,
  sin decirlo—.
- **Un solo campo de importe para toda la aplicación.** `AmountSpinBox`
  (`ui/widgets.py`) fija la convención que ya usa el resto de la aplicación
  —punto decimal, sin ceros de relleno— de forma que mostrar e interpretar no
  puedan discrepar, y además acepta la coma al teclear, que es la tecla decimal
  del bloque numérico en un teclado español. Puentes, intercambio y predicción
  usan ya el mismo campo, en vez de tres variantes con trampas distintas.
- Regresión cubierta con tecleo real, tecla a tecla
  (`tests/unit/test_amount_spinbox.py` y el caso del puente en
  `tests/integration/test_bridges_page.py`): lo que viaja en la petición es lo
  que se escribió.

### Arreglado — El seguimiento de los cruces de Relay ya sigue: el puente deja de quedarse en el primer paso

- **«Hice un swap entre redes y se quedó en la primera posición.»** El paso 1 era
  verdad —el origen se emitió y su transacción tuvo éxito—, pero no podía
  avanzar: el motor `relay` no implementaba `BridgeTracker` (sólo `lifi` lo
  hacía), así que el registro de cruces guardaba cada 15 s «El motor «relay» no
  está activo: no se puede seguir este cruce» —falso: estaba activo; lo que no
  sabía era seguir—. El cruce **sí se había completado**: medido el 2026-10-10
  contra la API en vivo, Relay lo da por `success`, con la entrega en Base
  (`0x9df356…b829`) y un abono de +0,00015229895039858 ETH a la cartera, la
  misma cifra medida por diferencia de saldo.
- **Relay ahora sabe seguir sus cruces.** `track_bridge` consulta
  `GET /requests/v3?depositTxHash=<hash de origen>` —medido en vivo; la v2 se
  retira el 2026-11-24— y traduce el estado sin inventar pasos: `success` →
  entregado, `failure` → fallo con el motivo que Relay dé (`failReason`),
  `refund` → devuelto, `waiting`/`depositing`/`pending`/`submitted`/`delayed` →
  en vuelo con su frase, cualquier otro → «desconocido» nombrando el estado
  crudo. El importe recibido sale del cambio de saldo **positivo a favor de la
  cartera** en la transacción de entrega —lo que llegó, no lo cotizado— y la
  respuesta se comprueba otra vez contra sus `inTxs` para que el cruce de otro
  nunca se muestre como propio.
- **Y el registro ya no miente cuando no hay seguimiento.** Motor activo que no
  sabe seguir → «está activo, pero no sabe seguir cruces todavía» y deja de
  sondear (el registro queda sin estado final y se vuelve a sondear al arrancar,
  tras actualizar el motor); motor inactivo → se sigue sondeando, porque puede
  activarse desde la página de motores. Verificado en vivo: el registro atascado
  pasó a `done` con la entrega y `0.00015229895039858 ETH` recibidos. Ver
  `engines/relay/engine.py` (`track_bridge`, `REQUESTS_URL`,
  `progress_from_request`) y `app/usecases/track_bridge.py`.

### Arreglado — Cerrar la aplicación ya no termina en error

- Cerrar la ventana salía con traza y código 1: `MainWindow.closeEvent` ya
  orquestaba el cierre asíncrono (`container.aclose()`), pero `ui/app.py::main`
  esperaba además la señal `aboutToQuit`, que llega **justo cuando el bucle de
  Qt ya va a salir** —el `await` de este lado no llegaba a despertar y el bucle
  se apagaba con la tarea a medias («Event loop stopped before Future
  completed»; reproducido también sin pantalla)—. Ahora el cierre tiene un solo
  camino: Qt no sale por su cuenta al cerrarse la última ventana
  (`setQuitOnLastWindowClosed(False)`) y el bucle de asyncio se apaga cuando
  `_serve` retorna, después de que la ventana terminó de cerrarse. Verificado
  con la aplicación real: salida limpia, código 0.

### Arreglado — La pestaña de puentes ya no se cuelga: el diálogo de confirmación no abre un bucle anidado, y sólo se firma una operación a la vez

- **«Intenté pasar 3 POL de Polygon a ETH en Base y se quedó colgado».** La
  causa, medida en vivo el 2026-10-10: `QtConfirmationPrompt.ask` llamaba a
  `QDialog.exec()` **dentro de la corrutina**, y ese `exec()` abre un bucle de
  eventos anidado dentro del paso de la tarea. Bajo `qasync` eso mata el
  despertar de cualquier otra tarea en vuelo (`RuntimeError: Cannot enter into
  task … while another task … is being executed`): la pestaña quedó muda a
  mitad de la operación. No hubo pérdidas — se comprobó en la cadena: ninguna
  de las dos firmas existía para el nodo, el nonce no se consumió y el saldo
  quedó intacto. Ahora `ask` muestra el diálogo con `open()` —modal para la
  ventana, sin bucle— y espera el «sí» por la señal `finished`, que resuelve
  una `Future`: esperar es cederle el turno al bucle de `qasync`, que sigue
  atendiendo todo lo demás. Ver `ui/widgets.py` (`QtConfirmationPrompt`).
- **Una ejecución a la vez, y la segunda se rechaza.** Factor contribuyente del
  mismo cuelgue: el repintado de la pestaña volvía a encender «Ejecutar» con
  una operación en vuelo, y un segundo clic firmó otra vez **con el mismo
  nonce** (medido: dos firmas a 30 s de diferencia, al router de Relay, por el
  importe entero). Ahora los cuatro caminos que firman —swap, puente, recepción
  y retirada— comparten un solo `SingleFlight` (`app/single_flight.py`, montado
  en el contenedor): la segunda ejecución concurrente se **rechaza** con
  `ExecutionError` en vez de encolarse —una operación que se firma sola «cuando
  le toque el turno» es una firma que el usuario ya no está mirando—. La puerta
  vive en el caso de uso y no en la vista: una pestaña puede olvidarse de
  repintar su botón —y se olvidó—; ahora además la pestaña de puentes tiene su
  bandera `_executing` y el botón no se reenciende a mitad de vuelo. Ver
  `app/usecases/execute_bridge.py`, `execute_swap.py`, `claim_bridge.py`,
  `withdraw.py` y `ui/pages/bridges.py`.

### Arreglado — El puente ya no se niega por su propia tabla: los contratos de origen de Relay son una familia medida

- **«El puente no funciona: dice que apunta a otra wallet».** Lo que decía, con
  sus palabras, era que el payload dirigía el depósito a un contrato que no era
  el único que la tabla tenía apuntado — y por eso se negaba a construir. La
  tabla era demasiado estrecha, no la respuesta: **Relay no deposita en un solo
  contrato por red, sino en tres**. Estaba medido el depósito directo
  (`0x4cD00E…BC31`, 2026-10-07); el 2026-10-10 se midió en vivo lo que faltaba:
  una ruta de POL con swap en origen deposita en el **router ERC-20 de la v3**
  (`0xb92fe925…Ff4f`) y una de un ERC-20 con swap en origen en su **proxy de
  aprobación** (`0xCcC88a9d…15bE`) —los dos publicados por la propia API de
  Relay en `GET /chains` (`erc20Router`, `approvalProxy`), con bytecode en las
  cinco redes—, y en todas las rutas observadas **el gastador de la aprobación
  es el mismo contrato que recibe el depósito**. El motor ahora declara la
  familia entera por red y tanto él como el ejecutor comprueban **pertenencia**
  (el payload tiene que ir a alguno de los declarados) en vez de igualdad con
  uno solo; una aprobación cuyo gastador no sea el destinatario del depósito se
  rechaza antes de construir nada, porque autorizar a otro deja al autorizado
  sobre el token sin cobrarlo. Verificado en vivo: POL→base, POL→bankr,
  bankr→POL (×2) y ETH→POL construyen, cada uno hacia un contrato de la
  familia, y el destinatario aparece en el calldata. Ver
  `engines/relay/engine.py` (`RELAY_ORIGIN_CONTRACTS`, `ORIGIN_CONTRACTS`),
  `domain/protocols.py` (`BridgePlanner.expected_destination`) y
  `app/usecases/execute_bridge.py` (`_build_checked`).
- **Una clave de API rechazada ahora se distingue de una API que cambió.** La
  prueba real dejó ver que la clave de LI.FI guardada ya no vale —su API
  contesta `401 Invalid API key`— y el mensaje del motor decía sólo «respuesta
  no válida»: ahora, si la petición llevaba credencial del motor y el rechazo es
  401/403, lo dice y señala dónde se cambia (Motores → Credenciales), porque
  puede estar caducada, revocada o ser de otra cuenta. Ver
  `engines/http_source.py` (`_rejection_hint`).

### Arreglado — El copiloto llega al modelo con el TLS de la app, y un fallo se dice en vez de disfrazarse

- **Los proveedores de IA abren su cliente con `infra.http`, como todos los demás
  motores.** Construían un `httpx.AsyncClient` a secas, que verifica sólo contra
  `certifi`: en una máquina cuyo antivirus intercepta el TLS, la llamada a
  DeepSeek moría con `CERTIFICATE_VERIFY_FAILED` mientras el resto de motores
  contestaba 200, porque ellos sí cargan el contexto de la app —almacén del
  sistema **y** `certifi`—. Reportado por el usuario: «cuanto tengo en bankr» y el
  copiloto sin contestar. Ahora hablan por el mismo cliente que los demás, con el
  `allowed_hosts` del manifiesto aplicado de verdad (un proveedor de IA no puede
  hablar con un host que no declaró). Medido tras el arreglo:
  `POST https://api.deepseek.com/chat/completions` responde **HTTP 401** —sin
  clave— en vez de fallar el certificado.
- **Un modelo que no contesta ya no se lee como si hubiera contestado.** El
  respaldo offline copiaba el principio de la pregunta como si fuera un análisis,
  y con el protocolo del copiloto delante de la pregunta lo que aparecía en el
  chat era el protocolo en JSON bajo la etiqueta «Copiloto». Ahora la respuesta
  nombra el modelo y el motivo real —el error de TLS, el HTTP, la respuesta vacía—
  y no lleva la advertencia de «análisis generado por IA», porque no hay análisis.
  El protocolo pasa al contexto (`instrucciones_de_respuesta`) y la pregunta se
  queda con las palabras del usuario. El análisis offline sigue existiendo donde
  promete funcionar: el asistente offline. Ver `engines/llm/providers.py`,
  `app/copilot.py` e `infra/http.py`.

### Cambiado — El copiloto es un chat: consulta por su cuenta, guarda el historial y propone operaciones

- **La pestaña del copiloto deja de ser un formulario y pasa a ser una
  conversación, con el modelo y con las herramientas.** Pedido por el usuario:
  «mejorar diseño que sea un chat con llm conectado que se conecte a las
  herramientas incluso si le paso una dirección y le pregunto si tiene saldo
  averigua, que vaya dejando el historial con chats archivados, modular
  refactorizado con sus componentes, opciones de elegir el modelo y de hacer
  operaciones en automático». Antes había que pegar a mano las cifras de las
  otras pantallas, así que «¿tiene saldo esta dirección?» no se podía preguntar
  de ninguna manera; ahora las trae la aplicación —saldos de una dirección,
  cotizaciones, token por dirección y puentes— por los mismos casos de uso que
  las demás pestañas, y mientras el turno ocurre se narra qué se está
  consultando. Cada burbuja del copiloto guarda, plegada, la prueba de dónde
  salió cada cifra: los hallazgos y lo consultado, con la salida en crudo.
- **El historial: un chat por conversación, con sus archivados.** La lista va a
  la izquierda, los de ahora arriba y los archivados en una sección plegada y
  aparte; archivar no pregunta —se deshace de un clic— y borrar sí, con un aviso
  que dice que no hay vuelta atrás. El mensaje del usuario se guarda **antes** de
  preguntarle al modelo: si el motor se cae, lo escrito no se pierde y al
  reabrir el chat se ve qué se preguntó y que no hubo respuesta. Al reabrir un
  chat **no** se vuelve a ofrecer la propuesta de entonces: sería una cotización
  vieja con un botón de firmar al lado.
- **Cuando el copiloto propone una operación, el chat la ofrece por el camino de
  siempre** (decisión del usuario). La tarjeta dice lo que se propone, avisa de
  que todavía no se ha firmado nada y, si el camino no está abierto, enumera
  **qué falta** con las mismas funciones que consultan las otras pantallas
  (`ui/execution_gate.py`): modo, interruptor, lista blanca, planificador,
  cartera y topes. El botón entra por `execute_swap`/`execute_bridge`, con su
  confirmación por transacción —o con la autonomía ya armada, dentro de sus
  límites—, y una propuesta ya ejecutada **no** vuelve a encenderse aunque el
  estado cambie: la cifra que llevaba ya se gastó.
- **Elegir el modelo, con uno encendido a la vez.** El desplegable se rellena con
  todos los asesores instalados —elegir uno apagado es la forma normal de
  encenderlo— y al elegir se enciende el nuevo **antes** de apagar los demás: en
  el peor caso quedan dos encendidos, que es lo que ya había, y nunca ninguno.
  La ranura se escribe en `config.toml` y, si no se pudo, se dice: vale para esta
  sesión. Un modelo que no arranca —«claude» sin su clave— deja su motivo en la
  pantalla y no se lleva por delante al que estaba contestando.
- **La autonomía se arma desde aquí, y desarmarla no pide nada.** La tira enseña
  si está armada, cuánto se ha gastado en 24 h **por unidad** —un número sin
  unidad no dice si son cien dólares o cien mil, y es la cifra que se mira para
  dejar que la aplicación firme sola— y hasta dónde llegan los topes. Armar pide
  la frase (texto oculto, contra la guardada) y desarmar es el freno: no
  pregunta, no pide frase y no falla. El aviso de la barra de estado ahora manda
  a esta tira en vez de a un método de Python. Ver `ui/pages/ai.py`,
  `ui/copilot_chat.py`, `ui/copilot_history.py` y `ui/copilot_controls.py`.

### Cambiado — Configuración se divide en tres subpestañas, y el nodo se agrega y se edita en su ventana

- **La pestaña Configuración pasa a ser tres subpestañas —Nodos RPC, APIs de
  motores, Credenciales— y cada credencial vive en un solo sitio.** Pedido por
  el usuario: «que las celdas se adapten al contenido en lo ancho y que al final
  esté un botón de agregar nodo que al pulsarse se abra la ventana para añadirlo
  […] y en una segunda subpestaña poner las API que puedan agregarse/editarse, no
  de manera duplicada». El formulario fijo del final de la tarjeta desaparece:
  «Agregar nodo…» abre una ventana (`ui/node_dialog.py`) que sirve para el alta y
  —con el lápiz de la fila— para editar, con la ✕ para eliminar. Un nodo **nuevo**
  sólo se guarda si la prueba (`eth_chainId`) responde con la red elegida; al
  editar sólo se exige si cambió la URL o se escribió una clave nueva, porque
  etiqueta y prioridad no son nada que la prueba mida. La clave del nodo viaja
  dentro de su URL como `${NOMBRE}` y se escribe en esa misma ventana, junto al
  nodo que la usa; dejarla en blanco al editar **no** borra la guardada.
- **Las claves de los motores y su modelo dejan de enseñarse en dos sitios.**
  La tabla de sólo lectura de Configuración y la casilla por opción de la tarjeta
  de credenciales eran dos vistas del mismo dato, y se separan en cuanto una
  cambia: ahora es una sola (`ui/pages/apis.py`), con el lápiz de la fila para
  editar. Se guardan como `<motor>:<opción>` en el llavero; el **modelo** —que no
  es un secreto— se lee en claro y en blanco es «el de fábrica»; un campo en
  blanco no borra lo guardado («Borrar las guardadas» es su botón); y guardar una
  clave aquí **no** enciende el motor, cosa que se dice porque una clave guardada
  que nadie usa parece estar funcionando. La tarjeta de Credenciales queda con lo
  que es de la aplicación entera: la clave que firma, la frase de la ejecución
  desatendida, Polymarket y la fila «Otra credencial».
- **Las tablas de la pestaña reparten el ancho por contenido**, como la de
  motores: cada columna mide lo suyo y la larga —la URL de un nodo, la
  configuración de un motor— se lleva el ancho que sobra, en vez de recortarse
  todas por igual. La columna «Clave» de la tabla de nodos dice el estado de cada
  marcador (`INFURA_API_KEY: configurada`) sin enseñar nunca el valor, y su ayuda
  nombra el llavero, la variable de entorno y cómo escribirla (la ventana del
  nodo). Ver `ui/pages/settings.py`, `ui/pages/apis.py` y `ui/node_dialog.py`.

### Cambiado — La pestaña de motores enciende y apaga por fila, y el nombre se edita

- **El desplegable y el botón «Activar» de la pestaña de motores se sustituyen
  por un interruptor en cada fila, y cada motor se puede renombrar.** Pedido por
  el usuario: «un slide en cada motor para activar/desactivar en vez de un combo
  y un botón […] que permita editar nombres […] sin romper lo que tenemos».
  Encender **suma** a la ranura —los motores de una ranura se comparan entre sí,
  así que apagar los demás al encender uno sería lo contrario de para lo que
  están— y apagar retira sólo ese motor. Apagar el último deja la ranura escrita
  **vacía** (`dex_quotes = []`), que en la carga significa «apagada a
  propósito»: si se borrara la clave, el motor por omisión volvería al
  reiniciar. Cada gesto escribe **sólo esa ranura** en `config.toml` —con
  `tomlkit`, sin llevarse comentarios ni claves ajenas— y, si la escritura
  falla, se dice: rige esta sesión, pero no se guardó.
- **El nombre de un motor es un alias de pantalla editable con el lápiz de su
  fila**, guardado en la nueva tabla `[engine_names]` y resuelto por las tres
  pantallas que enseñan nombres (motores, APIs de configuración y credenciales):
  el `engine_id`, su configuración y lo que hace no cambian, y borrar el alias
  devuelve el nombre de fábrica. Un motor que no arranca deja su interruptor
  apagado con el motivo a la vista, y el repintado —la tabla se reconstruye
  entera desde el registro, que es la única verdad— no arrastra a los que ya
  estaban. La presentación se adapta al contenido: cada columna mide lo suyo, el
  nombre se lleva el ancho que sobra y las filas tienen aire para el
  interruptor. Ver `ui/pages/engines.py`, `ui/widgets.py` (`Switch`),
  `app/engine_names.py` e `infra/config_writer.py` (`set_active_engines`,
  `set_engine_names`).

### Arreglado — La ejecución mira el saldo antes de aprobar, y el déficit se dice con sus cifras

- **«1 pUSD → POL» moría al estimar con «execution reverted: STF» sin decir
  por qué.** Medido el 2026-10-10 en Polygon, con las lecturas de `balanceOf` y
  `allowance` delante: la cartera tenía **0.550579 pUSD** e intentaba mover
  **1 pUSD** — el permiso directo al router estaba de sobra y el router, al
  cobrar más de lo que hay, revierte con su `STF` (*safe transfer from*), que no
  dice ni cuánto hay ni cuánto falta. Ni la cotización ni la aprobación miran
  saldos (la cotización cotiza para un centinela; aprobar es un permiso, no un
  gasto), así que el déficit solo se descubría al final. Ahora `ExecuteSwap`
  comprueba el saldo del token que el payload va a mover **después de construir
  y antes de aprobar** —aprobar para un swap impagable era una transacción de
  gas para nada— y lo corta con las dos cifras en el mensaje («tienes 0.550579
  pUSD y la operación mueve 1 pUSD»), sin firmar ni estimar nada. Qué se mira
  sale del mismo criterio con el que se decide a quién aprobar: el ERC-20 que
  el router moverá, o el nativo del `value` cuando es él quien viaja. Ver
  `domain/errors.py` (`InsufficientBalanceError`) y
  `app/usecases/execute_swap.py` (`_require_sold_balance`).

### Arreglado — El swap de Uniswap (agregado) ya no revierte: su router cobra por Permit2

- **«pUSD → POL» revertía al estimar el gas («execution reverted») y no se
  firmaba.** La causa se midió en la cadena, no se supuso: los **ocho routers**
  del motor `uniswap` (agregado) llevan
  `0x000000000022D473030F116dDEE9F6B43aC78BA3` (Permit2) dentro de su bytecode
  —cobran el token pidiéndoselo a Permit2, no con un `transferFrom` propio—, y
  la aplicación aprobaba el ERC-20 directamente contra el router: un permiso que
  ese router nunca lee (quedó en la cadena, es inofensivo y no se revoca). El
  payload del motor ahora **declara el permiso**
  (`TokenApproval(spender=router, via=Permit2)`) y la máquina de dos permisos
  encadenados —que ya existía y ya usaba Base— concede los dos con importe
  exacto, cada uno con su diálogo y su relato de «primera/segunda de dos
  aprobaciones». Cuando lo que se entrega es el nativo no hay permiso que
  declarar: se paga como `value`. Los routers de `zeroex` se midieron igual y
  **no** llevan Permit2: su camino directo se queda como estaba. Ver
  `engines/uniswap/engine.py` (`PERMIT2`, `_to_unsigned`).

### Cambiado — Un solo botón de ejecución, que narra la operación y la deja listada

- **«Preparar swap…», «Guardar payload…» y el botón de firmar se funden en uno
  solo.** Pedido por el usuario: «un solo botón con estados, cada estado va
  cambiando a medida que voy avanzando […] hasta completar». Una pulsación
  arranca la operación entera y cada transacción real conserva su propio «sí»
  —la regla de la casa no cambia—; el texto del botón sigue a la fase en curso
  («Aprobando pUSD (1 de 2)…», «Esperando tu confirmación…», «Firmando y
  emitiendo…») y termina en «✓ Completado». El bloque entero —botón, ruta
  elegida y panel— vive **entre la tarjeta del swap y las rutas**, centrado:
  pegado al importe y al par que va a firmarse, y por encima de la lista de
  rutas, que es información para elegir. El panel lista las
  **transacciones realmente realizadas** con su glifo, su hash —acortado, entero
  en el tooltip— y el enlace a su explorador; los rechazos y los fallos también,
  con su motivo, porque un «no» del usuario y un revert son parte del resultado.
  Un **rechazo no se cuenta como error** («Cancelaste la operación…», y el botón
  vuelve: los permisos ya concedidos se reaprovechan) y «Empezar de nuevo» barre
  el relato sin tocar la ruta elegida. La estimación del coste de red pasó al
  paso ESTIMATE de la ejecución —después de los permisos, con los que ya no
  revierte por allowance— y su cifra entra en el diálogo del swap. La narración
  es opcional y **no puede tumbar la operación**: un observador que falle queda
  en el log. Ver `app/usecases/execute_swap.py` (`SwapStep`, `StepUpdate`),
  `app/usecases/prepare_swap.py` (`estimate_cost`) y `ui/swap_steps.py`.

### Añadido — Engranaje del deslizamiento por defecto, aplicado de verdad al construir

- **La cabecera de la tarjeta de intercambio trae un engranaje que fija la
  tolerancia de deslizamiento** —el mínimo que un swap acepta recibir—, la
  guarda en `[execution] slippage_bps` de `config.toml` y **se aplica de
  verdad**: los seis motores que construyen reciben `slippage_bps` al construir
  el payload (el mínimo del calldata en V2/V3/V4 y el `slippageTolerance`/
  `slippageBps` de la recotización en agregado, zeroex y Júpiter). Si sólo
  cambiara la etiqueta sería mentir en el sitio donde más importa. El valor vive
  en `DefaultSlippage` —la configuración es inmutable, así que el número vivo no
  cabe en `Settings`—, el panel de detalle enseña el vigente y, si la escritura
  del archivo falla, se dice: rige esta sesión, pero no se guardó. Ver
  `app/slippage.py`, `ui/slippage_dialog.py` e `infra/config_writer.py`.

### Arreglado — Cotizar pregunta a los motores a la vez, y una sola vez por ronda

- **La comparación de precios consulta todos los motores en paralelo y el
  análisis de oportunidades se deriva de la comparación ya obtenida.** Antes,
  cada cotización era una ronda **secuencial** por los motores de la ranura
  —el tiempo era la suma de las latencias, y un motor lento retrasaba a los que
  ya habían contestado— seguida de una **segunda** ronda completa, la del
  barrido de oportunidades, que volvía a preguntar lo mismo antes de calcular
  nada. Reportado por el usuario: «¿por qué tarda tanto la cotización?». El
  pliegue de los resultados conserva el orden de la pila aunque las respuestas
  lleguen al revés —de ese orden dependen los desempates por pool y cuál de los
  fallos se propaga— y una cancelación no se traga: se re-lanza. Ver
  `app/usecases/compare_prices.py` y `app/usecases/scan_opportunities.py`
  —`comparison=` es opcional; `WatchScan` sigue cotizando por su cuenta, y una
  vez—.

### Cambiado — Cambiar el importe, el par o la red vacía lo anterior en el acto

- **Rutas, panel de detalle, oportunidades, línea de la ruta elegida y borrador
  preparado se retiran en cuanto cambia algo que los describía**, en vez de
  quedarse en pantalla —todavía seleccionables para preparar y firmar— hasta
  que llegara la cotización nueva. El rótulo vacío dice «Cotizando…» mientras
  llega; si el par queda imposible (mismo token en las dos patas, o falta una)
  dice por qué y no se pide nada a la red. La cotización en vuelo se **cancela**
  al pedir otra: una petición que ya no interesa no sigue gastando cuota de las
  fuentes públicas ni compite con la nueva. Reportado por el usuario: «cuando
  cambio los montos no se limpia lo viejo, rutas etc». Ver `ui/pages/prices.py`
  (`_schedule_quote`, `_invalidate_quote`, `_clear_results`).

### Cambiado — Las rutas son tarjetas con panel de detalle, no una tabla de ocho columnas

- **La tabla de rutas de la pestaña de precios se sustituye por una lista de
  tarjetas de dos líneas y un panel de detalle debajo.** Pedido por el usuario:
  «¿cuál elijo?» se contesta comparando —icono del motor, venue e importe
  recibido en grande, el desglose debajo, y el importe en **verde** en la mejor—,
  y «¿qué voy a firmar?» se contesta de una en una en el panel de etiqueta y
  valor de la ruta elegida. La segunda línea de cada tarjeta se recorta con
  puntos suspensivos —una fila por ruta, para comparar de un vistazo— y la nota
  entera vive en su tooltip. **No cambia ninguna funcionalidad**: se conservan
  la selección que enciende los botones, la marca ámbar «· sólo cotiza» **antes**
  de elegir, el ajuste `[ui] hide_quote_only_routes` con su recuento «N de M · K
  ocultas» y su estado vacío, la casilla «Todos los decimales», la tarjeta
  plegable y los tres botones con sus motivos. Ver `ui/route_list.py` —el
  resaltado de la tarjeta elegida se pinta por propiedad dinámica porque el
  `itemWidget` tapa el `::item:selected`— y `ui/pages/prices.py`.

### Añadido — Coste de red estimado al preparar, y camino, cartera y deslizamiento en el panel

- **El panel de la ruta elegida enseña tres datos que existían y no se veían en
  ninguna parte**: el **camino** de la ruta cuando cruza varios pools
  (`Quote.route`, con los tramos y sus comisiones en el tooltip), la **cartera
  receptora** —recortada, con la dirección entera en el tooltip— y el
  **deslizamiento máximo** configurado, etiquetado como lo que es: la tolerancia
  de `execution.slippage_bps`, que no cambia el mínimo que acepta el swap —lo
  fija cada motor al construir el payload—.
- **`PrepareSwap.__call__` devuelve `PreparedSwap`** —la transacción más lo que
  se supo de su coste de red— y estima el coste con el payload ya construido,
  **desde la cartera que firmaría** (`eth_estimateGas` con su `from`: el gas
  depende de quién envía) y con el **mismo** mapa de emisores que firma, subido
  un nivel en `container.py` para que no haya dos. La cifra
  —`gas_limit × max_fee_per_gas`, la semántica de `SignedTransaction.max_cost_wei`,
  un techo y no un cobro— va al diálogo de confirmación y al panel.
- **Un fallo de estimación no bloquea nada**: el borrador sale igual, con «no se
  pudo estimar» y su motivo en el tooltip —un revert por falta de permiso es
  información, no un muro—. Antes de preparar la fila dice «se estima al
  preparar» —la estimación es una llamada a la red por ruta y cotizar compara
  muchas—, en Solana se enseña «—» —no hay `eth_estimateGas` que preguntar— y
  nunca se escribe un cero en su lugar. Al cambiar de ruta la cifra se retira:
  era la de otro swap. Ver `app/usecases/estimate_cost.py`.

### Añadido — La tabla de rutas se puede quedar sólo con lo que se puede firmar

- **`[ui] hide_quote_only_routes`** (apagado por omisión): encendido, las rutas
  de motores que **sólo cotizan** —GeckoTerminal, DexScreener— no llegan a la
  tabla de rutas de la pestaña de precios, en vez de quedarse marcadas en
  ámbar. Pedido por el usuario: la fila es un precio de referencia que compara
  contra las rutas firmables (`PrepareSwap.can_build`), y quien la vea como
  ruido puede apagarla sin tocar código. La tabla no miente al hacerlo —el
  recuento dice «N de M · K de sólo cotización ocultas» y, si ninguna es
  firmable, el rótulo vacío explica que las hay y dónde se encienden, no que
  ningún motor contestara— y la selección apunta a lo que de verdad se ve
  (`_shown_quotes`), no al índice de la comparación entera. Ver
  `infra/config.py` y `ui/pages/prices.py`.

### Añadido — La vía directa de Uniswap V4 ya cruza por dos saltos, sin clave ni cuota

- **El par sin pool directo se cotiza por un hub del catálogo, como en V3 y
  V2.** El camino entero se le pide al `V4Quoter` de una vez
  —`quoteExactInput` con los `PathKey[]` del camino— y el importe que devuelve
  es el que la ejecución daría, no la composición de dos lecturas sueltas.
  Medido contra Base el 2026-10-09: WETH → USDC → WETH (los dos tramos del
  0,30 %, 10¹⁵ wei) devolvió `[993014236051103, 70983]` —importe y estimación
  de gas— y el selector salió del despachador desplegado: **`0xca253dc9`**; el
  recordado `0xcdca1753` no existe en el contrato desplegado —es de otro
  cotizador—, y el retorno son **dos** palabras, no las cinco que decía el
  docstring heredado del `QuoterV2` de V3.
- **Cada `PathKey` nombra la moneda de llegada de su tramo** y el array lleva su
  propia tabla de desplazamientos, con el `hookData` vacío en su sitio: escribir
  la moneda al revés deriva la clave de otro pool, y desplazar la tabla una
  palabra hace que el contrato lea otra cosa sin revertir al codificar. El
  calldata se fija byte a byte contra el que se mandó por `eth_call` y contestó
  con cifras; el payload ejecuta las dos patas en **una sola transacción** con
  `SWAP_EXACT_IN` (**`0x07`**, medido), `SETTLE_ALL` y `TAKE`, y **sin**
  `minHopPriceX36`: la revisión desplegada (`v4-periphery@444c526b77d8`) no lo
  lleva y meterlo desplazaría la tabla una palabra.
- **Las mismas reglas que la vía de un solo pool, y el mismo contrato de
  confianza que el multi-salto de V3:** el pool directo con liquidez sigue
  ganando —la ruta sólo entra cuando no hay ninguno—; las rutas se buscan en
  paralelo por los hubs del catálogo (`hub_tokens`, filtrados los del par), la
  segunda pata se cotiza con lo que de verdad dio la primera, compiten por la
  salida del segundo tramo, el impacto **compone** con tope de 1.000 bps y el
  venue propio (`uniswap-v4@30+5`) nombra el camino y no un pool. Al construir
  se vuelve a cotizar **ese** camino —`quoteExactInput` otra vez, deriva del
  1 %— y sólo entonces se codifica. Ver `engines/uniswap_v4/calldata.py` y
  `engines/uniswap_v4/engine.py`.

### Añadido — La familia V2 completa la vía directa: AMM de producto constante, sin clave y para cualquier token

- **Un solo motor cubre Uniswap V2, SushiSwap, QuickSwap y PancakeSwap en las
  seis redes medidas.** Son el mismo contrato con otro despliegue: se midió en
  los seis routers que llevan los mismos selectores, que la comisión va dentro
  del contrato —no en el calldata— y que su `path[]` es una lista de direcciones
  real. Lo que cambia por red es una entrada de la tabla medida, no una rama de
  código: **Ethereum** Uniswap V2 (4.061 WETH en el par USDC, sobre los 55,8 de
  SushiSwap), **Optimism** SushiSwap (el router de Uniswap V2 ahí devuelve 0
  bytes), **Arbitrum** SushiSwap (Camelot queda fuera: sus swaps no llevan los
  selectores estándar en el dispatcher medido), **Polygon** QuickSwap (**USDC
  nativo confirmado hoy** con `token1()`: 0x3c49…3359), **Base** Uniswap V2
  (178,7 WETH sobre 0,7) y **BNB Chain** PancakeSwap. La comisión se midió con
  aritmética entera contra el contrato desplegado: `997/1000` en los cinco DEX
  del 0,3 % y `9975/10000` —**25 bps**— en PancakeSwap, cuya discrepancia entre
  repositorio y documentación queda resuelta por el router real. Ver
  `engines/uniswap_v2/`.
- **Cotizar aquí no es simular: es la aritmética que ejecutará el swap.**
  `getAmountsOut` vive en el propio router y devuelve, sobre el estado del
  bloque actual, exactamente lo que el swap entregará —neto de comisión—, así
  que el AMM V2 publica la cifra de la cadena y no una fórmula de este lado. El
  impacto sale del marginal de las reservas del pool **con la función compartida
  que ya usaban V3 y V4** (`impact_bps_from_marginal`), de modo que el mismo
  pool publicado por dos motores da el mismo impacto; la liquidez publicada es
  la reserva del lado quote, como en las fuentes de pools.
- **Sin pool directo, dos saltos por los hubs del catálogo en una sola llamada.**
  El camino entero se cotiza con **un** `getAmountsOut` —el mismo que repetirá
  el swap—, no componiendo dos lecturas; el impacto de la ruta compone los dos
  tramos y la comisión publicada es la suma. Un pool directo con liquidez sigue
  ganando, y el payload vuelve a cotizar **ese mismo camino** con la tolerancia
  de deriva del 1 % y mínimo redondeado hacia abajo. El nativo se compra y se
  vende con las funciones dedicadas de la familia (`swapExactETHForTokens`,
  `swapExactTokensForETH`): el router envuelve y retira él mismo, sin
  `multicall` ni `unwrapWETH9`, el permiso del token es directo contra el router
  y el `deadline` existe en todas las redes medidas.
- **Activado por defecto en el ejemplo, detrás de las vías directas de V3 y V4**
  (prioridad 30, delante de 0x): su trabajo es que **toda** red medida tenga una
  vía sin clave ni cuota, y la comparación de precios decide con datos.

### Añadido — La vía directa de Uniswap V3 ya cruza por dos saltos, sin clave ni cuota

- **«1 POL → pUSD» se cotiza y se construye por contrato, sin agregado alguno.**
  Medido contra Polygon el 2026-10-09: ese par no tiene pool directo en ningún
  tramo de comisión —los cuatro `getPool` devuelven la dirección cero—, así que
  el motor V3 directo ahora busca una ruta de dos saltos por los tokens del
  catálogo (WPOL → USDC → pUSD, tramos 0,05 % + 0,01 %) y la ejecuta en **una
  sola transacción** con `exactInput` y el camino empaquetado: el QuoterV2
  devuelve por el camino entero exactamente lo que componen sus dos patas
  (99594 unidades de 6 decimales por 1 POL). Sin `api_key`, sin cuota y sin
  tabla por token: los hubs salen del catálogo —envoltorio nativo, stablecoin de
  referencia y extras medidos—, así que cualquier token que se añada en
  cualquier red de la tabla enruta solo. Un pool directo con liquidez sigue
  ganando —la ruta sólo entra cuando no hay ninguno—, y la segunda pata se
  cotiza con lo que de verdad daría la primera, no con la entrada original.
- **El payload ejecuta la ruta publicada, no una búsqueda nueva.** Al firmar se
  vuelve a cotizar con `quoteExactInput` **ese mismo camino** —los tramos con
  los que se construyó cada pool—, con la misma tolerancia de deriva del 1 % de
  los demás motores, y el `amountOutMinimum` sale de esa cotización fresca
  redondeado hacia abajo. Los selectores se midieron en el bytecode desplegado
  (`exactInput` del SwapRouter V1 `0xc04b8d59` y del SwapRouter02 `0xb858183f`,
  `quoteExactInput` `0xcdca1753`) y el calldata se contrastó contra el router
  real: con la forma correcta decodifica el struct entero y sólo echa en falta
  los fondos (`STF`), mientras que un desplazamiento interno corrompido revierte
  antes, en `slice_outOfBounds`.
- **La ruta viaja dentro de la cotización** (`Quote.route`, con `RouteHop` y
  `SwapRoute` en el dominio): sus tramos, sus comisiones y su venue propio
  (`uniswap-v3@5+1`, que nombra el camino y no un pool), para que la tabla y el
  aviso antes de firmar digan por dónde pasa. El impacto de los dos pools
  **compone** —`1 - Π(1 - i_k)`, no la suma: dos tramos del 1 % cuestan 1,99 %—
  y viaja como `DERIVED` junto a la comisión sumada. Ver
  `engines/uniswap_v3/calldata.py` y `engines/uniswap_v3/engine.py`.

### Arreglado — El nativo del swap: 0x y Uniswap ya lo cotizan, y «POL → pUSD» con ellos

- **Los dos agregados se rendían ante la moneda nativa antes de preguntar.** La
  dirección de un token nativo es `None` —no tiene contrato— y ambos motores lo
  leían como «no hay nada que cotizar»: justo el caso que más se usa, vender la
  moneda de la red. Medido el 2026-10-09: la API de Uniswap nombra al nativo con
  la **dirección cero** (con el centinela de 0x responde `NoRouteFoundError`) y
  la de 0x con **`0xEeee…`** (con la dirección cero no encuentra ruta). Con la
  suya, cada API cotiza y construye el nativo en las dos direcciones, y al
  venderlo la transacción lleva el importe en `value` y el router lo envuelve
  él mismo — sin aprobación previa que firmar.
- **«1 POL → pUSD» tiene dos rutas donde antes no había ninguna.** Medido con
  los motores reales: Uniswap entrega 0,099586 pUSD por el pool V2 de WPOL
  (una transacción, construible) y 0x entrega 0,098973 pUSD por una ruta de
  **dos saltos por USDC** (dos pools V3). Y es general, que es lo que importaba:
  cualquier token que se añada por su contrato se cotiza igual en las nueve
  redes de los agregados, sin tocar código por token.

### Añadido — Multi-cartera: varias direcciones, una contraseña y el almacén cifrado

- **La Cartera es un libro de carteras, y la activa manda en toda la
  aplicación.** La cabecera enseña quién es la cartera activa —su nombre, «1»,
  «2» o el que se le haya puesto— y su dirección con el botón de copiar
  animado; el nombre es un botón que abre la lista y cambia de cartera **de un
  clic**, y el cambio sigue en todo lo que firma y lee: swap, predicción,
  retiradas y saldos pasan a mirar la cartera elegida sin reiniciar nada. Añadir
  otra por su clave privada, añadir en **modo observación**, renombrar,
  eliminar, mirar una dirección sin guardarla y bloquear o desbloquear la
  sesión viven en el menú ☰ de la cabecera; los botones «Otra cartera…» y «Mi
  cartera» desaparecen —mirar una dirección era lo único que hacían, y eso
  ahora está en el menú—.
- **Las claves privadas se guardan cifradas, con una contraseña que se crea la
  primera vez.** No hay ninguna escrita en el código ni en la configuración: el
  diálogo la pide al añadir la primera clave —y avisa de que perderla es
  perderlas—. El cifrado es el keystore estándar de Ethereum (Web3 Secret
  Storage V3, con scrypt) sobre `eth-account`, sin cripto escrita a mano. La
  contraseña de sesión queda **en memoria** mientras la cartera esté
  desbloqueada, porque cada firma descifra la clave con ella; «Bloquear
  cartera» la olvida y una cartera bloqueada no firma —lo dice la interfaz
  antes de pulsar y lo dice `WalletLockedError` al firmar—. La clave descifrada
  no se cachea: se descifra en cada firma.
- **«Mostrar clave privada» pide la contraseña siempre, y la primera vez migra
  la del llavero.** La cartera heredada —la clave del llavero de Windows, que
  se sigue configurando en Credenciales— se lee sin migrar nada mientras no se
  toque; al enseñarla por primera vez se cifra con la contraseña y queda en el
  libro, **sin borrar la copia del llavero**, que es el respaldo. Todas las
  claves del almacén comparten una única contraseña, y el proveedor la verifica
  contra el almacén ya existente antes de cifrar nada: migrar con otra dejaría
  una clave que el desbloqueo de la sesión no abriría, y el fallo se
  descubriría al firmar.
- **Modo observación: se leen los saldos y no se firma.** Una cartera
  watch-only —EVM o Solana, con la forma de la dirección validada— enseña su
  dirección y sus saldos, sirve como destino por omisión y no puede firmar:
  `require()` la rechaza con `NoWalletError` y los botones que mueven dinero se
  apagan con su motivo escrito.
- **La cartera se resuelve en un solo punto, como antes del libro.**
  `container.keys` pasa de `SpendingKeyProvider` a `WalletKeyProvider` con la
  misma interfaz —`available/address/get/require`—, así que los casos de uso
  que firman y los gates de la interfaz no cambiaron ni una línea: elegir otra
  cartera se propaga por construcción, y con ella llegan los tres «no» de
  siempre —ninguna cartera, en observación, bloqueada— cada uno con su motivo y
  su arreglo. El libro vive en `wallets.json` (escritura atómica, lectura
  tolerante) y nunca guarda una clave en claro —lo fija un test—. Ver
  `infra/wallets.py` y `ui/pages/wallet.py`.

### Arreglado — El «+» de añadir token y el modal de red, con un solo clic

- **El «+» de token ya funciona con el filtro en «Todas las redes».** Sin red
  elegida, antes sólo aparecía un aviso y ahí se acababa: la dirección de un
  contrato sólo significa algo dentro de su red. Ahora el flujo **pregunta
  primero la red** —«¿En qué red está el token?», con las redes que esa cartera
  puede leer— y después la dirección del contrato; sin ninguna red que leer lo
  dice, y con una red ya puesta el comportamiento es el de siempre. Ver
  `_on_add_token` en `ui/pages/wallet.py`.
- **El modal de red elige con un solo clic.** El selector de red de la cartera
  —y el que abre el «+»— cierran y aplican al pulsar una fila; el botón
  «Elegir» se queda para el teclado, pero ya no hay que tocarlo: seleccionar y
  luego confirmar era el paso de más que nadie esperaba.

### Cambiado — La cartera: logo redondo, cifras en columna y el selector de red

- **El logo del token es redondo y la insignia de su red va en un cuadro** de
  esquinas redondeadas del color de la tarjeta —antes era un anillo circular—:
  redondo el token y cuadrado el marco, cada pieza se distingue de la otra, que
  es lo que hace legible una esquina con dos logos pegados.
- **El nombre de la fila es el símbolo, y nada más**: fuera «(nativo)» y la
  dirección del contrato —el desempate de homónimos sigue en el tooltip de la
  fila y en el detalle—, y la cantidad y el valor pasan a su **propia columna**
  a la derecha, alineadas al borde para que los números de la lista se comparen
  de un salto vertical. En el detalle, el nombre también deja de repetir la
  dirección: su línea de contrato ya la dice entera.
- **Los mandos de la lista viven en la cabecera de «Tokens con saldo»**: una
  **lupa** que despliega el buscador al pulsarla —y al plegarlo lo vacía, para
  que no quede un filtro escondido junto a su campo—, un **«+»** que añade un
  token por la dirección de su contrato, y el botón de releer. La barra de
  búsqueda fija y el botón «+ Añadir token» desaparecen: gastaban una fila
  entera de un panel de 420 px.
- **El filtro de red es un selector de la lista**: «▼ Red: Base» —o «▼ Todas
  las redes» cuando no hay ninguna puesta— abre un modal con las redes que esa
  cartera puede leer, «Todas las redes» la primera, y filtra sin volver a pedir
  un nodo. El desplegable «RED» de la cabecera de la cartera desaparece: el
  filtro es de la lista y ahora vive en ella. Ver `ui/pages/wallet.py`.

### Cambiado — La cartera: la lista por filas, el detalle de cada token y su actividad

- **Cada token es una fila que se lee y se pulsa, no una fila de tabla con un
  botón al final.** La fila lleva el logo del token con el de su red en la
  esquina —es lo que distingue el USDC de Polygon del de Base—, el nombre, lo
  que hay y lo que vale: la cantidad **recortada a cuatro decimales** (19,49166
  se enseña 19,4916, nunca redondeando hacia arriba, y con la cifra entera en el
  tooltip) y el valor con **dos decimales**, o su verdad cuando no la hay —«sin
  cotización» si tiene fondos y nadie lo cotiza, «0.00» si está a cero—. El
  recorte a «0.0000» no existe: un saldo diminuto se enseña entero, porque un
  cero falso es la única lectura peor que un número largo.
- **La fila entera abre el detalle del token** —al soltar el ratón dentro y con
  Enter o espacio— y desaparecen el ⇄ de la última columna y la barra de
  acciones: operar con un token empieza por mirarlo, y para llegar al ⇄ había
  que seleccionar la fila, bajar a la barra y cambiar de pestaña. La red ya no
  es una columna: va en el tooltip de la fila, que es donde se lee sin gastar el
  ancho de un panel de 420 px.
- **El detalle** enseña el nombre con su contrato —abreviado a la vista, entero
  en el tooltip, y con un botón de copiar que acusa el copiado con una marca
  verde; la moneda nativa no lo tiene, porque no hay contrato que copiar—, el
  precio por unidad, la cantidad y el valor, y debajo tres botones: **Enviar**
  —la retirada de siempre con este token ya elegido—, **Recibir** —el QR en la
  red de este token— y **Swap** —lo lleva a la tarjeta de conversión con la red
  y la pata de entrega puestas—.
- **«Actividad» es lo que ejecutó esta aplicación, y sólo eso**: los asientos del
  registro (`executions.jsonl`) que tocan a ese token y esa red, del más nuevo
  al más viejo, con su etiqueta —Envío, Swap, Puente, Recepción, Cobro, Orden,
  Permiso o Movimiento—, su descripción, su estado cuando no es «success» y su
  hash. Una transferencia que llegue de fuera **no aparece** —no pasa por el
  registro— y el texto de la lista vacía lo dice con esas palabras en vez de
  dejar creer que no pasó nada. Los asientos ganan un campo `tokens` y dos tipos
  nuevos, `receive` (la recepción de un puente, dinero que entra) y `approval`
  (el permiso de un ERC-20, que no mueve valor): ninguno de los dos consume el
  tope del día, y el filtro por token ya no depende del texto del par —los
  renglones viejos, escritos antes del campo, se siguen emparejando por palabra
  exacta («USDC» encuentra «WETH/USDC», «USDC.e» no)—. Ver
  `ui/pages/wallet.py`, `app/execution_policy.py` y `app/usecases/claim_bridge.py`.

### Arreglado — Las compras pequeñas en predicción se validan como lo hace el recinto

- **La tarjeta aplicaba a toda compra el mínimo de participaciones del mercado
  —5—, también a las que cruzan el libro.** A 0,62 $ cualquier importe por
  debajo de ~3,10 $ se apagaba con «el mercado pide un mínimo de 5
  participaciones», aunque el recinto sí acepta esa orden: era el caso de «no me
  deja comprar 1 $». El recinto valida **por importe** la compra que cruza el
  libro —mínimo 1 $, medido: «invalid amount for a marketable BUY order …,
  min size: $1»— y reserva las 5 participaciones para las órdenes que
  **descansan** en el libro y para las ventas (una venta que cruza de 5
  participaciones = 0,75 $ se aceptó, medido el 2026-10-09).
- Ahora la tarjeta aplica **la regla que toque**, y la decide el libro leído:
  cruza si el precio alcanza el mejor `ask` → importe ≥ 1 $; descansa → mínimo
  de participaciones; sin libro leído manda la del tamaño. También arregla la
  dirección contraria: 5 participaciones a 0,18 $ cruzando (0,90 $) ya no pasan
  el filtro para ser rechazadas por el recinto.
- **La cifra exacta va en el motivo.** Las participaciones se derivan del
  importe redondeando hacia abajo —nunca se compra más de lo escrito—, así que a
  0,62 $, 1,00 $ son 1,61 participaciones = 0,9982 $ y no llegan al dólar: el
  motivo dice «…se queda en 0.9982 $; sube el importe a 1.01 $». A precios
  redondos (0,50, 0,25, 0,20, 0,80…) 1,00 $ exacto sí funciona. Ver
  `ui/pages/prediction.py` (`_order_input_blockers`, `_crosses_book`,
  `_smallest_marketable_amount`).

### Añadido — El comodín `"*"` en `allowed_chains`: cualquier red, a sabiendas

- **`allowed_chains = ["*"]` significa «cualquier red»**, como `["*"]` ya
  significaba «cualquier token» en `allowed_tokens`. Hasta ahora la lista de
  redes sólo admitía nombres concretos: pedir un puente y encontrarse ««base» no
  está entre las redes habilitadas» una y otra vez no tenía salida salvo
  enumerar las diez redes a mano —y volver a tocarlo con cada red nueva—. El
  comodín es una decisión escrita a propósito, no un vacío: la lista vacía sigue
  siendo «nada», la validación de `config.toml` lo acepta sin colar con él redes
  escritas mal, y `allows_chain` (nuevo, espejo de `allows_token`) lo reconoce
  tanto en `check_chain` al firmar como en los avisos de la tarjeta de
  predicción —retirar, cobrar y publicar—. `ANY_CHAIN` se define a partir de
  `ANY_TOKEN` para que el mismo asterisco no pueda leerse de dos formas.
- Los **motores no se abren**: `allowed_engines` sigue diciendo «sólo firma
  quien construye» y GeckoTerminal queda fuera, que es lo que impide que una
  fuente de sólo lectura firme. Lo que acota sigue siendo el modo, la
  confirmación de cada operación y los topes de gasto.

### Añadido — El mínimo del mercado, a la vista siempre en la tarjeta de predicción

- **«Mínimo del mercado: 5 participaciones · salto de precio: 0.01»**, bajo las
  dos caras de la tarjeta de mercado —comprar y vender—. Antes el mínimo sólo se
  nombraba en la línea derivada de la compra, así que al vender no había forma
  de saberlo sin que algo fallara; la línea vive fuera de las caras porque el
  mínimo aplica igual a las dos. Se dice **lo que publica la fuente**, sin los
  valores de socorro que usan los campos (1 y 0,01): sin dato se lee «no consta
  el mínimo de participaciones», que es lo mismo que bloquea el botón con su
  motivo. La línea derivada de la compra se queda con su oficio —traducir
  importe a participaciones— sin repetir el mínimo. Ver
  `ui/pages/prediction.py` (`_refresh_market_limits`).

### Añadido — Las rutas que sólo cotizan se marcan en la tabla de swaps

- **La fila de un motor que no construye lo que se firma se marca antes de
  elegirla.** GeckoTerminal observa pools y publica cifras, pero no construye el
  swap (`can_build` en falso): su ruta se puede comparar y no se puede firmar, y
  eso la tabla sólo lo decía **después** de seleccionar la fila, en el aviso del
  botón. Ahora la columna «Motor» de esa fila lleva «· sólo cotiza» en ámbar
  —avisa sin bloquear: la cifra sigue sirviendo para comparar— y su tooltip
  explica que el swap lo tiene que construir otro motor. Ver
  `ui/pages/prices.py` (`_fill_table`).

### Arreglado — Un cruce en POL ya se puede firmar: el aviso de valoración no apaga el botón

- **Entregar un token que no es la stablecoin de su red dejaba el botón de firmar
  apagado, y con él la puerta entera.** Cuando la pata de origen no era la moneda
  de los topes, la pantalla del puente metía el aviso «se valorará al firmar, y
  si no se puede valorar no se cruzará» en la lista de **motivos** —la que apaga
  el botón—: cruzar 2 POL desde Polygon quedaba imposible de empezar siquiera.
  Ahora ese texto es un **aviso** (ámbar, «Aviso:»), el botón sigue encendido, y
  la valoración real la hace `ExecuteBridge` al firmar, que se niega a cruzar si
  no puede medirla —que es lo que el aviso y el propio `bridge_notional` decían
  que pasaba—. Bloquear ahí era hacer inalcanzable el camino que el motor de
  ejecución tiene montado para valorar.

### Arreglado — El arranque bajo Avast (`SSLKEYLOGFILE` inyectado)

- **`OPENSSL_Uplink(...): no OPENSSL_Applink` al arrancar no vuelve a tumbar la
  aplicación.** Avast (Web Shield) añade a los procesos `SSLKEYLOGFILE` con una
  ruta de dispositivo (`\\.\aswMonFltProxy\…`); el `ssl` de Python la lee al
  crear cada contexto y el intérprete del venv aborta con esa ruta en cuanto un
  motor abre su primer cliente HTTP —por eso moría justo al activar `deepseek`—.
  La aplicación la retira del entorno al arrancar, **sólo si apunta a una ruta
  de dispositivo** (que no es un fichero de claves legítimo; una ruta normal se
  respeta, que es alguien depurando TLS a propósito) y lo cuenta en el diario
  (`env.keylogfile_dropped`). Ya no hace falta lanzarla con
  `env -u SSLKEYLOGFILE`. Ver `infra/env_guard.py`.

### Arreglado — El botón de publicar y el colateral en caja mixta (`pUSD`)

- **«No se puede publicar: el colateral «pUSD» no está en `allowed_tokens`»
  —con `PUSD` declarado— no vuelve a aparecer.** La comprobación estaba escrita
  tres veces en la tarjeta de predicción —cobrar, retirar, publicar— y la del
  camino que firma comparaba **en crudo**: el símbolo del catálogo es `pUSD` y
  la lista llega normalizada a mayúsculas, así que `pUSD` «no estaba» en una
  lista que decía `PUSD`. La comprobación vive ahora en
  `ExecutionLimits.allows_token`, que normaliza **los dos lados**, y la usan
  los tres caminos de la tarjeta, el par del swap, la tarjeta de precios y el
  selector de tokens —con la misma respuesta que dará `check_token` al firmar:
  un token no puede estar en gris en una pantalla y ser firmable en la
  siguiente—.
- El comodín `*` y la lista vacía se deciden ahora en `allows_token`, así que
  el selector de tokens también respeta el `*` (antes pintaba en gris **todo**
  con `allowed_tokens = ["*"]`).

### Añadido — Predicción: tendencia, «cuánto paga» y la lista con categorías y orden

- **La tarjeta de mercado enseña la tendencia y cuánto paga.** Bajo la pregunta,
  la línea «1 h · 24 h · 1 sem» con cada tramo en verde/rojo —deltas por
  participación, la unidad en la que los publica la fuente: el «sí» que pasó de
  0.445 a 0.235 trae −0.21— y guion donde no hay dato; debajo, volumen de 24 h,
  liquidez y diferencial, y las **etiquetas del evento** con las que Polymarket
  organiza el mercado. Y junto al pago de la orden, «Al precio 0,62: ×1,61 ·
  +61,3 %»: lo que devuelve una participación al precio límite, recalculado al
  cambiar de resultado o de precio.
- **La lista se ordena por más nuevas y lleva filtros.** «Cierran en:» suma
  «5 minutos» y «10 minutos» —para lo que cierra ya— «Categoría:» —las etiquetas
  tal cual las da la API, ordenadas por número de mercados— y «Orden:» (más
  nuevas, más volumen, lo incoherente). «Tendencia» es la primera opción de
  categoría y una **vista sintética**, no una etiqueta: pide lo que más se mueve
  en 24 h (`volume24hr`) y apaga «Orden». La tabla gana la columna «24 h» con el
  cambio del día, con signo y color, y «Categorías». Cambiar cualquier selector
  re-filtra **en cliente** sobre lo ya traído, sin red; la siguiente búsqueda es
  la que lleva categoría y orden al motor.
- **`PredictionMarket` gana los datos de actividad** (`created_at`, `volume`,
  `volume_24h`, `liquidity`, `spread`, `price_change_1h/24h/1w`) y `tags`
  (`MarketTag`), todos opcionales: son para mirar, no para operar, y un `None`
  nunca se convierte en cero. `PredictionSort` nombra los cuatro órdenes, y
  `sort_reports`/`filter_reports_by_tag` los aplican también del lado del caso
  de uso —la interfaz los reaplica sin salir a la red—.
- **El motor pide a Gamma y vuelve a aplicar**: `order=createdAt|volume24hr`,
  `tag_id` y **`liquidity_num_min`** en `/markets`, y las etiquetas se traen de
  `/events` en lotes de 40 con `id` repetido —la fuente no las publica en el
  mercado—. El umbral de liquidez a la fuente es lo que arregla «más nuevas» con
  datos reales: medido el 2026-10-09, los cien mercados recién creados no
  llegaban al umbral —aún sin liquidez— y la vista se quedaba en cero filas
  porque el `limit` se gastaba en filas que el motor tira después. El filtro de
  categoría sólo descarta a quien **consta** que no la lleva; un lote de
  etiquetas que falla cuesta la etiqueta, nunca la fila.
- La barra de la lista pasa a **dos filas** —buscar y acciones arriba, filtros
  debajo—: en una sola no cabía en el ancho mínimo de la ventana y los campos se
  recortaban.

### Cambiado — Predicción: la pestaña es la lista, y cada mercado tiene su tarjeta

- La pestaña de predicciones enseña **sólo la lista de mercados abiertos**; al
  pulsar una fila, su **tarjeta de mercado** sustituye a la lista, con «← Volver a
  la lista». Volver no vacía la tarjeta —es un borrador—, y una búsqueda nueva sí
  devuelve a la lista: la tarjeta de un mercado que ya no está sería operar sobre
  lo que no se está mirando.
- **Se compra por importe y se vende por participaciones**, porque cada lado
  piensa en la cifra que compromete. La compra deriva el tamaño del importe
  —hacia abajo, a los céntimos del campo— y enseña la derivación y el coste
  exacto; la venta conserva su campo. Los dos lados llevan **atajos** editables:
  $1/$5/$20/$100 al comprar, 5/10/25/50/100 participaciones al vender. El mínimo
  del mercado se aplica distinto por lado: al comprar lo vigila el bloqueo con el
  motivo escrito —subir el importe solo sería comprar más de lo que se escribió—,
  y al vender el campo no baja de él. El botón firma «orden de compra» o «de
  venta» según el lado.
- **El saldo de la wallet, en las dos caras**, junto al formulario, con «Operar
  wallet…» al lado: abre el panel de la wallet —saldo, posiciones con sus
  acciones, «Añadir saldo» y el QR—, que era la tarjeta «Wallet de depósito» y
  ahora es un panel. La tarjeta lee el saldo sola **una vez por sesión** al
  abrirse; estaban el panel, las órdenes publicadas y «Añadir saldo» para
  refrescarlo. Las acciones de una posición cierran el panel y saltan a la
  tarjeta.
- **«Cestas con margen» y «Por cobrar» son paneles** que abren dos botones junto a
  «Buscar»: dejaban de tener sentido apilados bajo la lista. Sus flujos no
  cambian —«Buscar lo que puedo cobrar» sigue siendo manual—.
- Sin cambios en `domain`, `app`, `engines` ni `infra`: la tarjeta sólo enseña
  datos que ya estaban mapeados, y `refresh_execution_state` sigue repintando la
  tarjeta, el cobro y la wallet aunque sus paneles estén cerrados.

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
