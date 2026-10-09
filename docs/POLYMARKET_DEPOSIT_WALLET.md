# Polymarket: cuenta con deposit wallet (tipo de firma 3)

Estado: el canal funciona de punta a punta y quedó **verificado con una compra y
una venta reales** (2026-10-09, mercado 665374: 5 «Yes» compradas —`status=matched`,
participaciones en la wallet— y esas mismas 5 vendidas —permiso ERC-1155 por el
relayer, `status=matched`, colateral de vuelta en la wallet—), y la **fase 6 quedó
implementada**: la pestaña de predicciones enseña el saldo y las posiciones de la
wallet y ofrece los dos caminos para añadirle fondos. Este documento recoge lo
verificado, lo implementado y lo que falta.

## Por qué hace falta

El recinto rechaza órdenes firmadas por una cuenta normal (EOA) desde las cuentas
creadas después del 4 de mayo de 2026. El error que devuelve es
«maker address not allowed, please use the deposit wallet flow». La cuenta debe
operar desde una **deposit wallet**: un contrato inteligente que posee el
colateral y las participaciones, y que la clave de la EOA controla.

## Hechos verificados (documentación oficial)

Fuentes:
- https://docs.polymarket.com/trading/deposit-wallets
- https://docs.polymarket.com/trading/wallets-auth

| Tema | Dato |
|---|---|
| Relayer | `https://relayer-v2.polymarket.com` |
| Crear wallet | `POST /submit` con `type: "WALLET-CREATE"`, `from` = EOA, `to` = `0x00000000000Fb5C9ADea0298D729A0CB3823Cc07`, `metadata` |
| Confirmación | Sondear hasta `STATE_CONFIRMED`; la respuesta trae `proxyAddress` (la deposit wallet). `STATE_FAILED` e `STATE_INVALID` son terminales |
| Dirección previa | CREATE2 desde la factoría `0x00000000000Fb5C9ADea0298D729A0CB3823Cc07` y el beacon `0x7A18EDfe055488A3128f01F563e5B479D92ffc3a`; `walletId = bytes32(signer)` |
| Llamadas | `POST /submit` con `type: "WALLET"`, nonce de `GET /v1/account/transactions/params?address=…&type=WALLET` |
| Firma de llamadas | EIP-712: dominio `DepositWallet` v1, chainId 137, `verifyingContract` = la wallet; tipos `Call(address target, uint256 value, bytes data)` y `Batch(address wallet, uint256 nonce, uint256 deadline, Call[] calls)`; `primaryType` = `Batch` |
| Tipo de firma de órdenes | `signatureType = 3` (`DEPOSIT_WALLET`); `maker` = `signer` = la wallet |
| Saldo y permiso | `GET /balance-allowance` con `signature_type=3` |
| Colateral | pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` según la documentación |
| Aprobaciones | pUSD `approve(spender, max)` a los tres intercambios; CTF `setApprovalForAll` a los dos intercambios estándar y negRisk; PositionManager `0x006F54F7f9A22e0000CC2AB60031000000ae9fEF` `setApprovalForAll` al intercambio V2 |
| Auth L1/L2 | `ClobAuth` firmado por la EOA y `POLY_ADDRESS` = EOA. **Verificado**: también vale para órdenes tipo 3 (medido con la compra real; es el mismo camino que hace `clob-client-v2`, que usa el EOA en `POLY_ADDRESS` aunque el `maker` sea la wallet) |

## Pendiente de verificar (no documentado en las fuentes leídas)

1. **Envoltura ERC-7739 de la firma de la orden.** Resuelto: la envoltura sale del
   código oficial (`clob-client-v2` 1.2.0, vector byte a byte en los tests) y el
   recinto aceptó una orden firmada así el 2026-10-09.
2. **Colateral.** Resuelto: el mercado liquida en pUSD (`0xC011…2DFB`, 6
   decimales); `balance-allowance` autenticado lo confirma por canal y la compra
   real liquidó con él.
3. **Credenciales del relayer.** Resuelto en la fase 2: cada operación admite una
   credencial distinta (ver «Estado de implementación»). Se guardan en el llavero,
   nunca en `config.toml`.
4. **Mínimo de orden marketable (medido).** Una compra que **cruza el libro** por
   menos de $1 se rechaza: «invalid amount for a marketable BUY order ($0.85),
   min size: $1». Con el tamaño mínimo de 5 participaciones, cruzar por debajo de
   $0.20 por participación no llega al mínimo; una orden en reposo no cruza y no
   le aplica esa regla. La regla vista nombra la **compra**: una venta que cruza
   de 5 participaciones ($0.75) se aceptó (2026-10-09); a la venta le aplica el
   tamaño mínimo del mercado (5 participaciones), no el umbral en dólares.

## Decisiones de seguridad

- Ninguna transacción on-chain ni envío al relayer sin confirmación explícita del
  usuario, como exige `CLAUDE.md`. Crear la wallet, aprobar y retirar son
  operaciones distintas y cada una pide su «sí».
- La clave privada se lee sólo al firmar (`orders.py`, `sign_order`).
- Los pasos de cálculo (dirección, codificación de llamadas, firma) son puros y se
  prueban sin red.

## Fases

1. **Cálculo puro (sin red).** Dirección de la deposit wallet por CREATE2, hash de
   `Batch`, firma y comprobación de recuperación de la dirección. Pruebas unitarias.
2. **Cliente del relayer.** `POST /submit` con `WALLET-CREATE` y `WALLET`, sondeo
   de estado, nonce. Pruebas con `respx`. Sin ejecutar nada real.
3. **Configuración del usuario.** Relayer key en el llavero y selección de tipo de
   cuenta en la pestaña de predicciones.
4. **Operaciones con confirmación.** Crear la wallet, aprobaciones, depósito y
   retiro, cada una tras un «sí» explícito.
5. **Órdenes tipo 3.** Firma con la envoltura confirmada en el punto 1 de pendientes.
6. **Pestaña de predicciones.** Saldo de la wallet, depósito y retiro.

## Estado de implementación

- **Fase 1: hecha** (`engines/polymarket/deposit_wallet.py`, `tests/unit/test_deposit_wallet.py`).
  - `derive_beacon_deposit_wallet`: dirección CREATE2 igual que `deriveBeaconDepositWallet`
    de `@polymarket/builder-relayer-client` 0.0.10 (vector de prueba en los tests).
  - `wrap_order_signature`: envoltura ERC-7739 igual que `buildOrderSignature` de
    `@polymarket/clob-client-v2` 1.2.0 para `signatureType` 3 (vector byte a byte).
  - Con esto el pendiente 1 queda resuelto: la envoltura sale del código oficial, no de
    la documentación. Sigue sin probarse contra el recinto real.
- **Fase 2: hecha, sin envíos reales** (`engines/polymarket/relayer.py`,
  `tests/unit/test_relayer.py`).
  - Credencial por operación, según la documentación del relayer:
    - `WALLET-CREATE`: sólo **Builder key** (cabeceras `POLY_BUILDER_*`, firma HMAC-SHA256
      sobre marca, método, ruta y cuerpo exacto).
    - `WALLET` (lotes), nonce y estado de lote: **Relayer key** (`RELAYER_API_KEY` y
      `RELAYER_API_KEY_ADDRESS`, la dirección debe ser la dueña de la clave). Si sólo
      hay Builder key, se usa ella.
    - Estado de `WALLET-CREATE` (`GET /transaction?id=`): la documentación no indica
      credencial. El cliente no envía ninguna.
  - Firma de lote EIP-712 comprobada contra un vector producido con viem.
  - Un 400 se trata como respuesta inesperada y un 500 como fuente no disponible; no hay
    reintentos automáticos.
  - Pendiente de verificar contra el relayer real: forma exacta de la respuesta del
    nonce, campos de metadatos de `WALLET-CREATE`, y si la firma HMAC debe excluir la
    query string de `GET /transaction`.
  - Las credenciales se piden en la pestaña de credenciales (Clave API del relayer,
    dirección, y Builder key con su secreto y frase de paso).
- **Firma tipo 3: hecha y verificada** (`engines/polymarket/orders.py`,
  `tests/unit/test_prediction_orders.py`). `sign_order(..., wallet=...)` firma con
  `signatureType` 3 y `maker` = `signer` = la wallet, y comprueba que la firma recupera
  la EOA de la clave. `PredictionOrderPlanner.sign_order` y el motor aceptan `wallet`.
- **Canal de la wallet en las órdenes: hecho y verificado** (2026-10-09).
  - `PlacePredictionOrder` opera por la wallet cuando hay credenciales
    (`PolymarketWalletChannelProvider`): despliega si falta, comprueba fondos antes
    de firmar —sin fondeo automático—, concede el permiso del colateral por el
    relayer (por el máximo, que es lo que el relayer exige; se espera la
    confirmación y se relee en la cadena) y publica con las credenciales L2 de la
    EOA. El recinto aceptó la orden tipo 3 tal cual: las incidencias de
    `signer != api_key` para POLY_1271 no reprodujeron (el cliente oficial usa el
    mismo camino: EOA en `POLY_ADDRESS`, wallet como `maker`).
  - El destinatario efectivo es la wallet (es su `maker`): así se enseña en el
    diálogo y así queda en el asiento. Medido: las 5 participaciones de la compra
    aparecieron en la wallet y ninguna en la EOA.
  - **El camino de venta quedó verificado** con la venta real de esas 5 «Yes»
    (2026-10-09): la primera venta de la wallet ejercitó `_approve_wallet_shares`
    —`setApprovalForAll` de la colección ERC-1155 al recinto, por el relayer, sin
    gas, **sin importe** y sin asiento; el diálogo lo advierte—, se releyó el
    permiso en la cadena y la orden tipo 3 (vendedor = wallet) salió
    `status=matched`, `size_matched` 5/5. El colateral cobrado apareció en la
    wallet a los segundos y la posición desapareció de la API pública.
  - La wallet no se fondea sola: la recarga del 2026-10-09 se hizo con una
    retirada real (0.15 pUSD) por el camino de la aplicación.
- **Fase 6 (interfaz): hecha** (2026-10-09). La pestaña de predicciones trae la
  tarjeta «Wallet de depósito» (`ui/pages/prediction.py`, con el caso de uso de
  sólo lectura `app/usecases/read_settlement_wallet.py`):
  - La dirección se **deriva** —pura, sin red y sin clave: `settlement_wallet` del
    planificador— y por eso se enseña con «Copiar» y «Ver QR» antes de leer nada;
    el saldo y las posiciones se leen al pulsar «Leer la wallet» (saldo por el
    emisor, posiciones por la API pública con `redeemable_only=False`: todas, no
    sólo las cobrables). El caso de uso no recibe gateway —no firma ni emite— y
    las claves se tipan contra `AddressSource`, no `PrivateKeySource`: una lectura
    de saldo no puede ver la clave privada.
  - **Añadir saldo, dos caminos explícitos**: «Desde mi cartera» ejecuta la
    retirada de siempre con la wallet prefijada como destino (mismo modo, topes,
    confirmación y asiento — la tarjeta no duplica nada) y «Desde fuera» enseña la
    dirección y el QR: sólo pUSD, sólo Polygon.
  - **Posiciones con acciones por fila**: «Vender» y «Comprar más» resuelven el
    mercado por su `conditionId` (`market_by_condition`, gamma
    `/markets?condition_ids=…`) y **cargan** la tarjeta de orden de encima —el
    resultado por `token_id`, no por la etiqueta— sin firmar nada: el precio y
    «Publicar» siguen siendo los de arriba, con sus comprobaciones y su diálogo.
    La venta propone la posición entera truncada hacia abajo a los decimales de la
    participación; la compra, el mínimo del mercado. La tabla trae además precio
    medio y resultado abierto (`avgPrice`/`cashPnl`/`percentPnl` de la fuente;
    ausentes → en blanco, no un cero inventado).
  - **Lo que queda**: cobrar posiciones **resueltas de la wallet** —necesita un
    lote del relayer que aún no se construye—. Sus filas apagan las acciones con
    el motivo escrito en vez de ofrecer un botón que no haría nada.

## Pendientes tras la fase 1

- **Colateral: corregido a pUSD y confirmado por el recinto.** `orders.py`
  (`COLLATERAL`) apunta a pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, según
  la documentación oficial. Medido on-chain en Polygon (RPC público, sólo lectura):
  `symbol() = "pUSD"`, `decimals() = 6`, `name() = "Polymarket USD"`. El catálogo
  (`engines/catalog.py`) incluye pUSD junto al USDC.e, que se mantiene para los
  puentes. El recinto lo acepta: `balance-allowance` con `signature_type=3` lo
  reporta y la compra real del 2026-10-09 liquidó con él.
- **Contradicción resuelta.** El comentario anterior de `orders.py` afirmaba que el
  USDC.e era el colateral que acepta Polymarket, sin una medición en el repositorio que
  lo respalde.
- **Auth L1 para POLY_1271. Resuelto por medición.** Las incidencias de
  `py-clob-client-v2` (#70, #77) y `clob-client-v2` (#64, #65) no reprodujeron: la
  orden tipo 3 enviada con las credenciales L2 derivadas de la EOA se aceptó
  (2026-10-09). El código oficial hace lo mismo: `createL2Headers` pone
  `POLY_ADDRESS` = la dirección del firmante (la EOA), no la del `maker`.
- **Credenciales del relayer.** Resuelto en la fase 2 (Builder para `WALLET-CREATE`,
  Relayer o Builder para lotes), siempre en el llavero.

## Fuentes de referencia (fuera del repositorio)

- `@polymarket/builder-relayer-client` 0.0.10: `dist/builder/derive.js`.
- `@polymarket/clob-client-v2` 1.2.0: `dist/order-utils/exchangeOrderBuilderV2.js`.
- Se obtuvieron con `npm pack` y se leyeron en local; no se ha ejecutado código de red.
