"""Motor de puentes sobre Relay, según su documentación vigente.

### Lo que dice la documentación, leído antes de escribir nada

Se leyó la referencia actual —`/references/api/overview`, `/quickstart`,
`/quote-v2` y `/get-intents-status-v3`— y **nada de lo que hay aquí está
inventado**: cada campo que se lee sale de esa referencia, y lo que no se ha
podido comprobar en vivo se dice en vez de suponerse.

- **Base `https://api.relay.link`**, `POST /quote/v2` con `Content-Type:
  application/json`.
- **`x-api-key` es obligatoria desde el 2026-10-02**, y hoy es 2026-10-07. La
  documentación matiza que los campos del esquema la marcan como opcional, pero
  el aviso de la propia página dice que a partir de esa fecha el endpoint la
  exige en cada petición. **La clave vive sólo en el backend**: viaja en la
  cabecera de la petición y no se expone nunca a la interfaz, ni se registra, ni
  se escribe en un fichero del repositorio.
- **Cuerpo**: `user`, `originChainId`, `destinationChainId`, `originCurrency`,
  `destinationCurrency`, `amount` (entero sin signo, en unidades mínimas) y
  `tradeType` (`EXACT_INPUT`). `recipient` es opcional y por omisión es `user`.
- **El token nativo se nombra con la dirección cero.** Está en el ejemplo de la
  documentación: para cruzar ETH usa `0x0000…0000` como `originCurrency`, y por
  eso aquí un nativo **sí** se puede cruzar. En LI.FI se midió después el mismo
  centinela —acepta los dos, la dirección cero y `0xEeee…EEeE`, y normaliza los
  dos a la cero—, así que las dos API nombran igual la moneda nativa.
- **Respuesta**: `requestId`, `steps[]`, `fees`, `details`.
  - `steps[]` trae `id` (`deposit`, `approve`, `authorize`, `swap`, `send`),
    `kind` (`transaction` o `signature`), `items[]`, y en cada ítem un `data` con
    `from`, `to`, `data`, `value`, `chainId`, `gas`, `maxFeePerGas`,
    `maxPriorityFeePerGas`.
  - `details.currencyOut` es un objeto de importe —`amount`, `minimumAmount`,
    `amountFormatted`—, `details.timeEstimate` son segundos y `details.operation`
    vale `bridge`.
  - `fees` trae `gas`, `relayer`, `relayerGas`, `relayerService` y `app`, cada
    uno con `amount`, `currency` y `amountUsd`.

### Las dos cosas que se rechazan, y por qué

- **Un paso `signature`.** La documentación dice que los pasos son de dos clases:
  `transaction` —se emite— y `signature` —se firma fuera de la cadena y Relay la
  usa después—. Nuestro camino de firma construye y emite **una** transacción
  EVM; un paso de firma no cabe en él. Se rechaza la cotización entera en vez de
  emitir la mitad: ejecutar la pata que sí sabemos y quedarnos ahí dejaría fondos
  a medio cruzar, que es peor que no empezar.
- **Más de un paso `transaction`.** Un flujo de dos pasos —autorizar y luego
  depositar— no cabe en un `UnsignedTransaction`, que es **una** transacción. Se
  admite el caso de `approve` + depósito porque la aprobación sí se expresa —el
  ejecutor la construye él mismo a partir del gastador—, y se rechaza cualquier
  otro reparto con los pasos nombrados, para que quien lo lea sepa qué llegó.

### Lo que ya está medido: los contratos de origen

La tabla se empezó a medir el 2026-10-07 —tres usuarios, tres importes y dos
destinos sobre cinco redes de origen— y entonces salió el mismo contrato en
todas las combinaciones: el depósito `0x4cD0…BC31`. El 2026-10-10, probando una
ruta de verdad, una cotización de Polygon apuntó a **otro** contrato y el motor
detuvo la operación, que es lo que tenía que hacer.

Medido a fondo ese día, no era un error de nadie: Relay manda el depósito
**directo** a su contrato de depósito `0x4cD0…BC31`, y las rutas que necesitan
un swap en la red de origen (POL→USDC antes de cruzar, por ejemplo) al router
ERC-20 v3 `0xb92f…Ff4f` —o al proxy de aprobación v3 `0xCcC8…15bE` cuando el
token de origen es un ERC-20, que cobra el permiso y lo reenvía al router—. Los
tres son contratos de Relay con la misma dirección en las cinco redes, y los
tres están publicados por su propia API: `GET https://api.relay.link/chains` los
sirve por red (`erc20Router`, `approvalProxy`, `relayReceiver`), y la página de
direcciones de su documentación confirma la familia v3. En todas las rutas
medidas el gastador de la aprobación fue **el mismo contrato que recibe el
depósito**: el que cobra el token es el que recibe el dinero.

Así que la tabla deja de ser «un contrato por red» y pasa a ser «los contratos
publicados por Relay para esa red»: el depósito medido más los dos de la v3 que
su API publica. El contraste sigue significando lo mismo —un `to` que no sea uno
de ellos no se firma—; lo que cambia es que la lista es la familia entera y no
sólo el primero que se midió. `expected_destination` devuelve ese conjunto, y
`plan_bridge` exige además que el permiso del ERC-20, si lo hay, se conceda al
mismo contrato que recibe el depósito.

### Lo que ya está medido: el seguimiento

`GET /requests/v3?depositTxHash=` devuelve el cruce por el hash de su
transacción de origen —medido el 2026-10-10 con un cruce recién emitido:
estado `success`, la transacción de entrega en destino y, en sus
`stateChanges`, el abono exacto a la cartera—, y un hash que no conoce contesta
la lista vacía. La v3 exige la clave en la cabecera, que este motor ya manda en
cada petición. Es la consulta que sostiene `track_bridge`: no necesita la
cartera del usuario —que el registro del seguimiento no guarda— porque el hash
de origen es justo lo que sí guarda.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import structlog

from amigocompora.domain.chains import chain
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import (
    NoQuotesError,
    QuoteMovedError,
    SourceResponseError,
    UnsupportedOperationError,
)
from amigocompora.domain.models import (
    BridgeProgress,
    BridgeQuote,
    BridgeRequest,
    BridgeTrackState,
    Measurement,
    Token,
    TokenApproval,
    UnsignedTransaction,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import EXACT, BasisPoints, TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.http_source import JsonSource, as_mapping

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "Relay"
HOST: Final = "api.relay.link"
QUOTE_URL: Final = f"https://{HOST}/quote/v2"
STATUS_URL: Final = f"https://{HOST}/intents/status/v3"

#: La consulta del seguimiento: el cruce por el hash de su transacción de
#: origen. Medida el 2026-10-10 contra la API en vivo —encontró el cruce recién
#: emitido y devolvió, con él, la transacción de entrega y el abono a la
#: cartera—; un hash que no conoce contesta la lista vacía, y la clave viaja en
#: la cabecera como en todo lo demás. Es además la ruta a la que la v2 señala al
#: retirarse, así que no hay una segunda forma que mantener.
REQUESTS_URL: Final = f"https://{HOST}/requests/v3"

CONFIG_API_KEY: Final = "api_key"

#: Cómo nombra esta API el token nativo de una red. Tomado de su ejemplo, no
#: elegido: es el valor con el que la documentación cruza ETH.
NATIVE_CURRENCY: Final = "0x0000000000000000000000000000000000000000"

#: El depósito directo de Relay: a donde van los fondos cuando el token de
#: origen ya es el que cruza. Medido el 2026-10-07 en las cinco redes —tres
#: usuarios, tres importes y dos destinos— y vuelto a medir el 2026-10-10.
RELAY_DEPOSITORY: Final = "0x4cD00E387622C35bDDB9b4c962C136462338BC31"

#: El router ERC-20 v3 de Relay: recibe el depósito de las rutas que necesitan
#: un swap en la red de origen (p. ej. POL→USDC antes de cruzar). Publicado por
#: `GET https://api.relay.link/chains` como `erc20Router` en las cinco redes, y
#: medido en vivo el 2026-10-10: una ruta POL→base depositó aquí.
RELAY_V3_ROUTER: Final = "0xb92fe925DC43a0ECdE6c8b1a2709c170Ec4fFf4f"

#: El proxy de aprobación v3: recibe el depósito cuando el token de origen es un
#: ERC-20 y la ruta lleva swap en origen —cobra el permiso y lo reenvía al
#: router—. Publicado como `approvalProxy`; medido el 2026-10-10: el paso de
#: aprobación de esa ruta autoriza a este mismo contrato.
RELAY_V3_APPROVAL_PROXY: Final = "0xCcC88a9d1B4ED6b0EABA998850414b24f1c315bE"

#: La familia publicada, escrita una sola vez: la usan la tabla de cada red y
#: las pruebas, para que no haya dos listas que puedan discrepar.
RELAY_ORIGIN_CONTRACTS: Final[tuple[str, str, str]] = (
    RELAY_DEPOSITORY,
    RELAY_V3_ROUTER,
    RELAY_V3_APPROVAL_PROXY,
)

#: Contratos de Relay admitidos como destino del depósito **desde** cada red de
#: origen. Las cinco redes publican la misma familia —los tres contratos están
#: desplegados con la misma dirección en todas—, y se escribe por red y no como
#: una constante suelta porque es lo que se sigue consultando al construir: una
#: red cuya familia cambie se notará aquí. `BRIDGE_CHAINS` sale de aquí para que
#: no discrepen.
ORIGIN_CONTRACTS: Final[Mapping[str, frozenset[str]]] = {
    "ethereum": frozenset(RELAY_ORIGIN_CONTRACTS),
    "base": frozenset(RELAY_ORIGIN_CONTRACTS),
    "arbitrum": frozenset(RELAY_ORIGIN_CONTRACTS),
    "optimism": frozenset(RELAY_ORIGIN_CONTRACTS),
    "polygon": frozenset(RELAY_ORIGIN_CONTRACTS),
}

#: Las redes **desde** las que este motor puede construir un cruce. Cotizar
#: depende de la misma tabla a propósito: enseñar una ruta que no se puede firmar
#: es peor que no enseñarla, porque el usuario la elige y se queda mirándola.
BRIDGE_CHAINS: Final = frozenset(ORIGIN_CONTRACTS)

#: Selector de `approve(address,uint256)`, el único que se acepta en un paso de
#: aprobación. Se comprueba en vez de suponerlo: un paso cuyo calldata no empiece
#: así será otra cosa —una autorización de Permit2, un `receiveWithAuthorization`
#: de EIP-3009— y autorizar al contrato equivocado se firma, se emite y se paga
#: el gas de descubrirlo.
APPROVE_SELECTOR: Final = "0x095ea7b3"

#: Deriva máxima tolerada entre el importe recibido que se enseñó y el de la
#: cotización fresca que se firma.
MAX_QUOTE_DRIFT_BPS: Final = BasisPoints(100)

#: Dirección con la que se cotiza sin destinatario real. Fija, para que la caché
#: se comparta: el destinatario no cambia el precio.
QUOTE_USER: Final = "0x1111111111111111111111111111111111111111"

MANIFEST: Final = EngineManifest(
    engine_id="relay",
    name="Relay — puentes entre redes",
    version="1.0.0",
    kind=EngineKind.CROSS_CHAIN,
    summary=(
        "Cruza tokens entre redes por la red de resolución de Relay. Cotiza y "
        "construye desde las cinco redes cuyos contratos de origen se midieron, y "
        "compite con LI.FI por la misma operación."
    ),
    capabilities=frozenset(
        {Capability.READ_CHAIN, Capability.COMPUTE_ROUTE, Capability.PREPARE_TX}
    ),
    bridge_chains=BRIDGE_CHAINS,
    # Después de LI.FI. No es un juicio de precio —eso lo decide el importe que
    # cada uno entrega, y lo ordena `rank_bridges`— sino del coste de
    # equivocarse: aquí la firma pasa por varios pasos y uno de ellos puede ser
    # una firma fuera de la cadena que este camino no sabe emitir.
    bridge_priority=100,
    # La clave sigue siendo obligatoria para arrancar, pero **no** por lo que
    # decía este comentario antes. Decía que la documentación la exige para
    # `/quote/v2` desde el 2026-10-02, y eso no es lo que pasa hoy: el OpenAPI
    # del propio Relay marca `x-api-key` como `required: false` en `/quote/v2`
    # («Optional API key for authentication and higher rate limits»), y medido
    # en vivo ese endpoint responde 200 sin cabecera ninguna. La exige en otros
    # —`/requests/v3`, `/execute`, `/balance/{wallet}/events`— que este motor no
    # llama.
    #
    # Se deja obligatoria a propósito, y es una decisión, no una medición: el
    # anuncio de que pasará a exigirse está hecho, una consulta identificada no
    # se confunde con la de un desconocido, y esta máquina tiene clave. Si algún
    # día se quiere que Relay cotice aunque falte —en la línea de que un motor
    # sin credencial no debería apagarse solo—, esto pasa a `()` y la consulta
    # sin cabecera sigue funcionando.
    required_config=(CONFIG_API_KEY,),
    allowed_hosts=(HOST,),
)


class RelayEngine:
    """Cotiza —y construye, cuando sus destinos estén medidos— por Relay."""

    __slots__ = ("_clock", "_source")

    def __init__(
        self,
        *,
        api_key: str,
        clock: Clock | None = None,
        ttl_seconds: float = 5.0,
        timeout_seconds: float = 30.0,
        min_interval_seconds: float = 0.5,
    ) -> None:
        if not api_key:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» necesita una clave de API: su documentación la "
                f"exige en cada petición desde el 2026-10-02. Añádela en el panel "
                f"de motores."
            )
        self._clock = clock or SystemClock()
        self._source = JsonSource(
            name=SOURCE_NAME,
            allowed_hosts=MANIFEST.allowed_hosts,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            min_interval_seconds=min_interval_seconds,
            # La clave viaja en cabecera y `JsonSource` la guarda sin exponerla
            # ni registrarla. No se pasa nunca a la interfaz.
            headers={"x-api-key": api_key},
        )

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    async def aopen(self) -> None:
        await self._source.aopen()

    async def aclose(self) -> None:
        await self._source.aclose()

    # ------------------------------------------------------------- cotizar #
    async def quote_bridge(self, request: BridgeRequest) -> Sequence[BridgeQuote]:
        if request.origin.chain not in BRIDGE_CHAINS:
            _log.debug("relay.origin_chain_not_measured", chain=request.origin.chain)
            return ()

        payload = await self._quote_payload(request, user=QUOTE_USER)
        if payload is None:
            return ()
        quote = self._to_bridge_quote(payload, request)
        return () if quote is None else (quote,)

    # ---------------------------------------------------------- construir #
    async def plan_bridge(
        self, quote: BridgeQuote, *, recipient: str
    ) -> UnsignedTransaction:
        request = quote.request
        if request.origin.chain not in BRIDGE_CHAINS:
            raise UnsupportedOperationError(
                f"este motor sólo cruza desde las redes que tiene medidas: se pidió "
                f"«{request.origin.chain}», y las medidas son "
                f"{', '.join(sorted(BRIDGE_CHAINS))}. Sin el depósito de esa red no "
                f"hay nada contra lo que contrastar el destino antes de firmar, y "
                f"por eso tampoco se cotiza desde ahí: la ruta no se llega a "
                f"enseñar."
            )

        payload = await self._quote_payload(request, user=recipient)
        if payload is None:
            raise NoQuotesError(
                f"«{request.symbol}» ya no tiene ruta en {SOURCE_NAME}: la que "
                f"viste al cotizar se agotó. Vuelve a cotizar."
            )

        fresh = _fresh_out(payload)
        self._require_same_output(quote, fresh)

        steps = _transaction_steps(payload)
        approve = _approve_step(steps, request)
        deposits = [step for step in steps if step is not approve]
        if len(deposits) != 1:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» devolvió {len(deposits)} pasos de transacción "
                f"({', '.join(str(s.get('id')) for s in deposits)}) y este camino "
                f"firma **una** transacción. No se construyó nada: emitir sólo la "
                f"primera dejaría los fondos a medio cruzar."
            )
        return self._to_unsigned(
            quote, deposits[0], approve, recipient=recipient, fresh_raw=fresh
        )

    def expected_destination(self, chain_key: str) -> frozenset[str] | None:
        """Los contratos de origen publicados para esa red, o `None` si no hay."""
        return ORIGIN_CONTRACTS.get(chain_key)

    # ---------------------------------------------------------- seguir #
    async def track_bridge(
        self, tx_hash: str, *, origin_chain: str, destination_chain: str
    ) -> BridgeProgress:
        """Lo que Relay dice de un cruce emitido. Sólo lee.

        `origin_chain` y `destination_chain` los exige el contrato
        `BridgeTracker`; esta consulta no los necesita —el cruce que Relay
        devuelve ya trae sus redes— y por eso no se usan para nada.
        """
        raw = await self._source.get_json(
            REQUESTS_URL,
            params={"depositTxHash": tx_hash},
            absent_statuses=frozenset({404}),
        )
        if raw is None:
            return _unknown_progress(f"{SOURCE_NAME} aún no conoce esta transacción.")
        return progress_from_request(as_mapping(raw, "estado", SOURCE_NAME), tx_hash)

    # ------------------------------------------------------------ interno #
    async def _quote_payload(
        self, request: BridgeRequest, *, user: str
    ) -> Mapping[str, Any] | None:
        raw = await self._source.post_json(
            QUOTE_URL,
            json_body={
                "user": user,
                "originChainId": chain(request.origin.chain).require_eip155_id(),
                "destinationChainId": chain(
                    request.destination.chain
                ).require_eip155_id(),
                "originCurrency": _currency(request.origin),
                "destinationCurrency": _currency(request.destination),
                "amount": str(request.amount_in.raw),
                "tradeType": "EXACT_INPUT",
            },
            # La documentación publica un 400 con `errorCode` para una petición
            # que no tiene ruta. Es una respuesta informativa —«ese par no se
            # puede cruzar ahora»—, no un formato roto, y por eso se declara
            # ausente en vez de dejar que aborte el barrido entero.
            absent_statuses=frozenset({400}),
            # La clave lleva las dos direcciones y no sólo el símbolo: dos tokens
            # distintos pueden compartir símbolo —«USDC» lo hace en varias redes,
            # y hay tokens que lo imitan a propósito— y servir la cotización de
            # uno por la del otro es dar un precio de otra cosa.
            cache_key=(
                f"{chain(request.origin.chain).require_eip155_id()}:"
                f"{_currency(request.origin)}:"
                f"{chain(request.destination.chain).require_eip155_id()}:"
                f"{_currency(request.destination)}:"
                f"{request.amount_in.raw}:{user}"
            ),
        )
        if raw is None:
            return None
        return as_mapping(raw, "respuesta", SOURCE_NAME)

    def _to_bridge_quote(
        self, payload: Mapping[str, Any], request: BridgeRequest
    ) -> BridgeQuote | None:
        details = payload.get("details")
        if not isinstance(details, dict):
            _log.info("relay.quote_without_details", pair=request.symbol)
            return None
        if not _moves_across(details, request):
            # Antes esto miraba `details.operation` y exigía `"bridge"`. Medido
            # el 2026-10-07, Relay contesta `"swap"` en todas las rutas entre
            # redes que se probaron, así que aquella comprobación rechazaba
            # todas y el motor no cotizaba nada. Ver `_moves_across`.
            _log.info(
                "relay.quote_not_cross_chain",
                pair=request.symbol,
                operation=details.get("operation"),
            )
            return None

        out = _amount_object(details.get("currencyOut"))
        if out is None:
            _log.info("relay.quote_without_output", pair=request.symbol)
            return None
        out_raw = _uint(out.get("amount"))
        min_raw = _uint(out.get("minimumAmount"))
        if out_raw is None or out_raw <= 0:
            _log.info("relay.quote_unreadable_amount", pair=request.symbol)
            return None
        if min_raw is None:
            # Sin suelo garantizado no se puede decir lo que el usuario recibe
            # como mínimo, y en un puente eso es la mitad del dato: no se
            # completa con el estimado, que sería prometer lo que nadie promete.
            _log.info("relay.quote_without_minimum", pair=request.symbol)
            return None

        destination = request.destination
        fee = _fee_in_origin(payload, request)
        return BridgeQuote(
            engine_id=MANIFEST.engine_id,
            provider=_provider(payload),
            request=request,
            amount_out=TokenAmount(out_raw, destination.decimals, destination.symbol),
            amount_out_min=TokenAmount(min_raw, destination.decimals, destination.symbol),
            fee=fee,
            fee_basis=Measurement.REPORTED if fee is not None else None,
            duration_seconds=_uint(details.get("timeEstimate")),
            observed_at=self._clock.now(),
            route=_route_note(payload),
            source_note=(
                "El importe recibido ya viene neto de comisión. El mínimo "
                "garantizado es el suelo que Relay se compromete a entregar. "
                "Al construir, el destino se contrasta contra el depósito medido "
                "de la red de origen antes de firmar nada."
            ),
        )

    def _to_unsigned(
        self,
        quote: BridgeQuote,
        step: Mapping[str, Any],
        approve: Mapping[str, Any] | None,
        *,
        recipient: str,
        fresh_raw: int,
    ) -> UnsignedTransaction:
        request = quote.request
        chain_key = request.origin.chain
        spec = chain(chain_key)

        item = _first_item(step)
        data = _item_data(item)

        # El destino se contrasta **antes** de leer nada más.
        destination = _require_known_depository(chain_key, data.get("to"))

        calldata = data.get("data")
        if not isinstance(calldata, str) or not _is_hex(calldata):
            raise SourceResponseError(
                f"«{SOURCE_NAME}» devolvió un `data` que no es hexadecimal válido; "
                f"sin calldata no hay transacción que revisar."
            )
        value_raw = _uint(data.get("value"))
        if value_raw is None:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» no publicó un `value` legible "
                f"({data.get('value')!r}); no se puede saber cuánto nativo mueve "
                f"la transacción."
            )

        approval = _approval(approve, request)
        # El permiso va **al contrato que recibe el depósito**: es el que va a
        # mover el token. Medido el 2026-10-10 en las dos formas que publica
        # Relay —el depósito directo y el proxy de la v3— el gastador del
        # `approve` fue el mismo contrato al que apunta el depósito. Autorizar a
        # otro contrato dejaría el permiso en manos de quien no cobra, así que si
        # alguna vez no coinciden, no se firma.
        if approval is not None and approval.target.lower() != destination.lower():
            raise SourceResponseError(
                f"«{SOURCE_NAME}» pide autorizar {approval.target} y el depósito va "
                f"a {destination} en «{chain_key}». No se construyó nada: el "
                f"permiso tiene que ir al contrato que recibe el dinero, y "
                f"autorizar a otro deja al autorizado sobre tu token sin cobrarlo."
            )
        _log.info(
            "relay.bridge_planned",
            pair=request.symbol,
            origin=chain_key,
            out_raw=quote.amount_out.raw,
            destination=destination,
            value_raw=value_raw,
            approval=approval.target if approval else None,
        )
        return UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=destination,
            calldata=calldata,
            value=TokenAmount(value_raw, spec.native_decimals, spec.native_symbol),
            gas_limit=_uint(data.get("gas")),
            approval=approval,
            description=_description(quote, destination, recipient, fresh_raw=fresh_raw),
        )

    def _require_same_output(self, quote: BridgeQuote, fresh_raw: int) -> None:
        shown_raw = quote.amount_out.raw
        if shown_raw <= 0:
            return
        drift = EXACT.divide(Decimal(abs(fresh_raw - shown_raw)), Decimal(shown_raw))
        drift_bps = BasisPoints.from_ratio(drift)
        if drift_bps.value <= MAX_QUOTE_DRIFT_BPS.value:
            return
        token = quote.request.destination
        _log.info(
            "relay.quote_moved",
            pair=quote.pair_label,
            shown_raw=shown_raw,
            fresh_raw=fresh_raw,
            drift_bps=drift_bps.value,
        )
        raise QuoteMovedError(
            shown=str(quote.amount_out),
            fresh=str(TokenAmount(fresh_raw, token.decimals, token.symbol)),
            drift_bps=drift_bps.value,
            tolerance_bps=MAX_QUOTE_DRIFT_BPS.value,
        )


# --------------------------------------------------------------------------- #
# Lectura del formato de la fuente
# --------------------------------------------------------------------------- #
def _currency(token: Token) -> str:
    """Cómo nombra esta API ese token: su contrato, o la dirección cero si es nativo."""
    return token.address if token.address else NATIVE_CURRENCY


def _amount_object(value: Any) -> Mapping[str, Any] | None:
    """Un objeto de importe de la API, o `None` si no tiene la forma esperada."""
    if not isinstance(value, dict):
        return None
    return value


def _transaction_steps(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Los pasos que hay que **emitir**, o un error si alguno hay que firmar aparte.

    Un paso `signature` no cabe en este camino: se firma fuera de la cadena y
    Relay lo usa después. Emitir la parte que sí sabemos y parar dejaría fondos a
    medio cruzar, así que se rechaza la cotización entera y se nombran los pasos
    que llegaron.
    """
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió una cotización sin `steps`: no se puede saber "
            f"qué hay que emitir."
        )
    transactions: list[Mapping[str, Any]] = []
    firma: list[str] = []
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        kind = step.get("kind")
        if kind == "signature":
            firma.append(str(step.get("id")))
        elif kind == "transaction":
            transactions.append(step)
    if firma:
        raise UnsupportedOperationError(
            f"la ruta de «{SOURCE_NAME}» incluye firmas fuera de la cadena "
            f"({', '.join(firma)}) que este camino no sabe emitir. No se construyó "
            f"nada: emitir sólo las transacciones dejaría el cruce a medias. Elige "
            f"otra ruta."
        )
    if not transactions:
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió una cotización sin ningún paso que emitir."
        )
    return transactions


def _approve_step(
    steps: Sequence[Mapping[str, Any]], request: BridgeRequest
) -> Mapping[str, Any] | None:
    """El paso de aprobación, comprobando que de verdad lo es.

    Se acepta porque la aprobación **sí** se expresa en nuestro modelo —el
    ejecutor construye el `approve` a partir del gastador—, pero sólo si su
    calldata es el `approve(address,uint256)` estándar. Cualquier otra cosa
    —Permit2, EIP-3009— se rechaza nombrando el selector: autorizar al contrato
    equivocado se firma, se emite y se paga el gas de descubrirlo.
    """
    aprobaciones = [step for step in steps if step.get("id") == "approve"]
    if not aprobaciones:
        return None
    if len(aprobaciones) > 1:
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió {len(aprobaciones)} pasos de aprobación y "
            f"este camino concede una."
        )
    if request.origin.is_native:
        # Un token nativo no se autoriza: no hay contrato al que autorizar.
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió una aprobación para un token nativo, y el "
            f"nativo no se autoriza. Es una respuesta que se contradice."
        )
    step = aprobaciones[0]
    data = _item_data(_first_item(step))
    # El permiso se concede **al token**, así que la transacción tiene que ir al
    # contrato del token de origen. Si fuera a otro contrato, ese calldata lo
    # interpretaría quien no es: un `approve` con el mismo selector sobre otro
    # contrato autoriza lo que ese otro contrato entienda por autorizar.
    returned_to = data.get("to")
    origin_address = request.origin.address or ""
    if not isinstance(returned_to, str) or returned_to.strip().lower() != origin_address.lower():
        raise SourceResponseError(
            f"la aprobación que pide «{SOURCE_NAME}» va a {returned_to!r} y el token "
            f"de origen es {origin_address}. No se construyó nada: un permiso sobre "
            f"otro contrato autoriza lo que ese contrato entienda por autorizar."
        )
    calldata = data.get("data")
    if not isinstance(calldata, str) or not calldata.lower().startswith(APPROVE_SELECTOR):
        selector = calldata[:10] if isinstance(calldata, str) else repr(calldata)[:32]
        raise UnsupportedOperationError(
            f"la aprobación que pide «{SOURCE_NAME}» no es un `approve` estándar "
            f"(selector {selector}). No se construyó nada: este camino sólo sabe "
            f"conceder el permiso que conoce, y autorizar a ciegas el contrato "
            f"equivocado se firma, se emite y se paga el gas de descubrirlo."
        )
    return step


def _approval(
    approve: Mapping[str, Any] | None, request: BridgeRequest
) -> TokenApproval | None:
    """A quién hay que autorizar, leído del propio calldata de la aprobación."""
    if approve is None or request.origin.is_native:
        return None
    data = _item_data(_first_item(approve))
    calldata = str(data.get("data"))
    # `approve(address,uint256)`: 4 bytes de selector y luego la dirección en la
    # primera palabra de 32 bytes. Se leen los últimos 20 de esa palabra.
    spender = "0x" + calldata[2:][8:72][24:]
    if not _is_address(spender):
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió una aprobación cuyo gastador no se puede "
            f"leer ({spender!r}); sin saber a quién se autoriza no se construye."
        )
    return TokenApproval(spender=spender.lower())


def _first_item(step: Mapping[str, Any]) -> Mapping[str, Any]:
    items = step.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió un paso sin `items`: no hay transacción que "
            f"revisar."
        )
    return items[0]


def _item_data(item: Mapping[str, Any]) -> Mapping[str, Any]:
    data = item.get("data")
    if not isinstance(data, dict):
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió un paso sin `data`: no hay transacción que "
            f"revisar."
        )
    return data


def _require_known_depository(chain_key: str, returned: Any) -> str:
    """El destino del depósito, comprobado contra los contratos publicados.

    Se lee de `ORIGIN_CONTRACTS` —y no de la constante de la familia— para que
    una tabla inyectada en las pruebas gobierne también este contraste. Devuelve
    la forma publicada del contrato y no la que trajo la respuesta, para que lo
    que se compare después sea siempre la misma cadena: el payload, el permiso y
    el contraste de la emisión.
    """
    admitted = ORIGIN_CONTRACTS[chain_key]
    if isinstance(returned, str):
        text = returned.strip().lower()
        for contract in sorted(admitted):
            if contract.lower() == text:
                return contract
    conocidos = ", ".join(sorted(admitted)) or "ninguno"
    raise SourceResponseError(
        f"«{SOURCE_NAME}» devolvió como destino del puente {returned!r} en "
        f"«{chain_key}», y los contratos que Relay publica para esa red son "
        f"{conocidos}. No se construyó nada: aquí un destino equivocado no pierde "
        f"dinero en una operación de mercado, lo manda a otra parte y no vuelve."
    )


def _fresh_out(payload: Mapping[str, Any]) -> int:
    details = payload.get("details")
    out = _amount_object(details.get("currencyOut")) if isinstance(details, dict) else None
    raw = _uint(out.get("amount")) if out is not None else None
    if raw is None or raw <= 0:
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió una cotización sin importe de salida "
            f"legible; no se puede construir nada con ella."
        )
    return raw


#: Comisiones que **cobra el servicio**. El gas queda fuera a propósito: es lo
#: que cuesta emitir, no lo que cobra el puente, y contarlo inflaría la cifra con
#: un concepto que no es suyo — el mismo criterio que en los demás motores.
_FEE_FIELDS: Final = ("relayer", "relayerGas", "relayerService", "app")


def _fee_in_origin(payload: Mapping[str, Any], request: BridgeRequest) -> TokenAmount | None:
    """La comisión total, **sólo** si todas las partidas están en el token de origen.

    Se para en la primera partida que venga en otro token y se declara
    desconocida, en vez de sumar el resto: una parte presentada como el total
    haría que esta ruta pareciera más barata de lo que es justo en el número con
    el que se la compara con las otras.
    """
    fees = payload.get("fees")
    if not isinstance(fees, dict):
        return None
    origin_address = _currency(request.origin).lower()
    total = 0
    for field in _FEE_FIELDS:
        entry = fees.get(field)
        if not isinstance(entry, dict):
            continue
        amount = _uint(entry.get("amount"))
        if amount is None or amount == 0:
            continue
        currency = entry.get("currency")
        address = currency.get("address") if isinstance(currency, dict) else None
        if not isinstance(address, str) or address.lower() != origin_address:
            _log.info(
                "relay.fee_in_another_token",
                pair=request.symbol,
                field=field,
                token=repr(address)[:64],
            )
            return None
        total += amount
    if total == 0:
        return None
    return TokenAmount(total, request.origin.decimals, request.origin.symbol)


def _provider(payload: Mapping[str, Any]) -> str:
    """Por dónde va el cruce según la propia respuesta.

    La documentación publica `details.route` con `origin` y `destination`, y en
    cada uno un `router` y las fuentes de swap incluidas. Relay resuelve por su
    red de solvers, así que lo que se puede nombrar es eso: el router de la pata
    de origen, o la red de solvers cuando no viene.
    """
    details = payload.get("details")
    route = details.get("route") if isinstance(details, dict) else None
    origin = route.get("origin") if isinstance(route, dict) else None
    router = origin.get("router") if isinstance(origin, dict) else None
    if isinstance(router, str) and router:
        return f"router {router}"
    return "red de solvers"


def _route_note(payload: Mapping[str, Any]) -> str:
    details = payload.get("details")
    route = details.get("route") if isinstance(details, dict) else None
    origin = route.get("origin") if isinstance(route, dict) else None
    fuentes = origin.get("includedSwapSources") if isinstance(origin, dict) else None
    partes: list[str] = []
    if isinstance(payload.get("steps"), list):
        for step in payload["steps"]:
            if isinstance(step, dict) and isinstance(step.get("id"), str):
                partes.append(f"{step['id']} ({step.get('kind')})")
    detalle = " → ".join(partes) if partes else "ruta no detallada"
    if isinstance(fuentes, list) and fuentes:
        detalle += f" · fuentes de swap: {', '.join(str(f) for f in fuentes)}"
    return detalle


def _description(
    quote: BridgeQuote, destination: str, recipient: str, *, fresh_raw: int
) -> str:
    """Lo que el usuario va a confirmar antes de firmar.

    El importe que se nombra es el **fresco** y no el que se enseñó al cotizar:
    entre cotizar y firmar pasa un tiempo, y la cifra que importa es la que la
    transacción va a entregar de verdad. Enseñar la vieja sería confirmar una
    promesa que ya no es la que se firma.
    """
    request = quote.request
    recibido = TokenAmount(
        fresh_raw, request.destination.decimals, request.destination.symbol
    )
    return (
        f"Puente por {SOURCE_NAME} ({quote.provider}): entregas "
        f"{request.amount_in} en {request.origin.chain} y recibes {recibido} en "
        f"{request.destination.chain}, con un mínimo garantizado de "
        f"{quote.amount_out_min}. Recibe {recipient}. El contrato de destino es "
        f"{destination}, el que «{SOURCE_NAME}» tiene medido para "
        f"{request.origin.chain}."
    )


def _chain_of(amount: Any) -> int | None:
    """La red que declara un objeto de importe de Relay, si la declara."""
    objeto = _amount_object(amount)
    if objeto is None:
        return None
    moneda = objeto.get("currency")
    if not isinstance(moneda, dict):
        return None
    return _uint(moneda.get("chainId"))


def _moves_across(details: Mapping[str, Any], request: BridgeRequest) -> bool:
    """¿La respuesta dice de verdad que el dinero sale de una red y entra en otra?

    Esto sustituye a la comprobación que miraba `details.operation` y exigía
    `"bridge"`. Medido el 2026-10-07 contra la API en vivo, Relay contesta
    `"swap"` en **todas** las rutas entre redes que se probaron —incluida
    nativo→nativo, que es un puente sin discusión—, así que esa comprobación
    rechazaba todas las rutas y el motor no cotizaba nada por mucho que se
    activara. El OpenAPI del propio Relay enumera los valores posibles
    (`send`, `swap`, `wrap`, `unwrap`, `bridge`) y **no define qué significa
    cada uno**, así que `operation` no sirve para decidir esto.

    Lo que sí lo dice es el cuerpo: `currencyIn` y `currencyOut` llevan cada uno
    su `chainId`. Se exige que sean los que se pidieron y que sean distintos
    entre sí. Es un dato de la respuesta, no una suposición sobre un enumerado —
    y es más estricto que lo anterior: rechaza cualquier cosa que no mueva valor
    desde la red de origen pedida hasta la de destino, diga lo que diga
    `operation`.
    """
    entrada = _chain_of(details.get("currencyIn"))
    salida = _chain_of(details.get("currencyOut"))
    if entrada is None or salida is None:
        return False
    return (
        entrada == chain(request.origin.chain).require_eip155_id()
        and salida == chain(request.destination.chain).require_eip155_id()
        and entrada != salida
    )


def _uint(value: Any) -> int | None:
    """Lee un entero sin signo. La documentación los publica como cadenas decimales."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith(("0x", "0X")):
        try:
            return int(text, 16)
        except ValueError:
            return None
    if not text.isdigit():
        return None
    return int(text)


def _is_hex(value: str) -> bool:
    text = value.strip()
    if not text.startswith("0x") or len(text) <= 2:
        return False
    body = text[2:]
    return len(body) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in body)


def _is_address(value: str) -> bool:
    text = value.strip()
    if not text.startswith("0x") or len(text) != 42:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in text[2:])


# --------------------------------------------------------------------------- #
# El seguimiento: qué se lee de la respuesta que devuelve el cruce
# --------------------------------------------------------------------------- #
#: Los estados en vuelo y lo que cada uno significa, según la documentación de
#: Relay para su estado por intención: `waiting` es que el depósito aún no se
#: confirma y `delayed` que el relleno va con retraso pero sigue.
IN_FLIGHT_MESSAGES: Final[Mapping[str, str]] = {
    "waiting": "Esperando la confirmación del depósito en la red de origen.",
    "depositing": "Depósito confirmado: Relay está rellenando el cruce.",
    "pending": "Depósito confirmado; pendiente de la entrega en destino.",
    "submitted": "Entrega emitida en destino; pendiente de su confirmación.",
    "delayed": "La entrega en destino va con retraso; Relay sigue con el cruce.",
}


def _unknown_progress(message: str) -> BridgeProgress:
    """Un progreso sin estado del proveedor: lo que aún no se sabe, dicho."""
    return BridgeProgress(
        state=BridgeTrackState.UNKNOWN,
        provider_status="",
        substatus="",
        message=message,
    )


def progress_from_request(payload: Mapping[str, Any], tx_hash: str) -> BridgeProgress:
    """Traduce el cruce que Relay devuelve para ese hash de origen.

    El hash se comprueba **otra vez** contra las transacciones de entrada del
    cruce aunque la consulta ya lo pidiera por él: si algún día el filtro
    dejara de filtrar, el primer resultado sería el de otro cruce, y enseñarlo
    como propio es la clase de error que aquí no se firma.
    """
    requests = payload.get("requests")
    if not isinstance(requests, list):
        return _unknown_progress(
            f"«{SOURCE_NAME}» devolvió una respuesta sin `requests`: no se puede "
            f"leer el estado del cruce."
        )
    request = _request_with_origin(requests, tx_hash)
    if request is None:
        return _unknown_progress(f"{SOURCE_NAME} aún no conoce esta transacción.")

    status = str(request.get("status", ""))
    out_txs = _out_txs(request)
    if status == "success":
        state = BridgeTrackState.DONE
        message = "Relay entregó el cruce en destino: la transacción se confirmó en la red."
    elif status == "failure":
        state = BridgeTrackState.FAILED
        message = "Relay no pudo completar el cruce."
        motivo = _fail_reason(request)
        if motivo:
            message = f"{message} Motivo: {motivo}."
    elif status == "refund":
        state = BridgeTrackState.REFUNDED
        message = "Relay devolvió los fondos a la dirección de origen."
    elif status in IN_FLIGHT_MESSAGES:
        state = BridgeTrackState.PENDING
        message = IN_FLIGHT_MESSAGES[status]
    else:
        return BridgeProgress(
            state=BridgeTrackState.UNKNOWN,
            provider_status=status,
            substatus="",
            message=f"Relay informó un estado que este camino no conoce ({status!r}).",
        )
    return BridgeProgress(
        state=state,
        provider_status=status,
        substatus="",
        message=message,
        receiving_tx_hash=_receiving_tx_hash(out_txs),
        received_raw=_received_amount(out_txs, request.get("recipient")),
        received_chain=_receiving_chain(out_txs),
    )


def _request_with_origin(requests: Sequence[Any], tx_hash: str) -> Mapping[str, Any] | None:
    """El cruce cuya transacción de entrada es ese hash, o `None`."""
    wanted = tx_hash.strip().lower()
    for request in requests:
        if not isinstance(request, dict):
            continue
        data = request.get("data")
        in_txs = data.get("inTxs") if isinstance(data, dict) else None
        if not isinstance(in_txs, list):
            continue
        for tx in in_txs:
            if not isinstance(tx, dict):
                continue
            # `txHash` es el nombre que publica la v3 —la v2 lo llamaba `hash`—
            # y se lee el vigente: el viejo no se adivina.
            hash_ = tx.get("txHash")
            if isinstance(hash_, str) and hash_.strip().lower() == wanted:
                return request
    return None


def _out_txs(request: Mapping[str, Any]) -> list[Any]:
    """Las transacciones de entrega que el cruce publique, si hay."""
    data = request.get("data")
    raw = data.get("outTxs") if isinstance(data, dict) else None
    return raw if isinstance(raw, list) else []


def _receiving_tx_hash(out_txs: Sequence[Any]) -> str | None:
    """El hash de la transacción que entrega en destino, si Relay ya la emitió."""
    for tx in out_txs:
        if not isinstance(tx, dict):
            continue
        hash_ = tx.get("txHash")
        if isinstance(hash_, str) and hash_:
            return hash_
    return None


def _receiving_chain(out_txs: Sequence[Any]) -> str | None:
    """La red del destino tal cual la declara esa transacción, si la declara."""
    for tx in out_txs:
        if not isinstance(tx, dict):
            continue
        chain_id = _uint(tx.get("chainId"))
        if chain_id is not None:
            return str(chain_id)
    return None


def _received_amount(out_txs: Sequence[Any], recipient: Any) -> int | None:
    """Lo que de verdad llegó: el abono a la dirección de destino.

    Se lee del cambio de saldo de la transacción de entrega —medido el
    2026-10-10: el abono de la cartera aparece con su dirección y un
    `balanceDiff` positivo— y no del importe cotizado: lo cotizado es lo que se
    prometió, y esto es lo que llegó. Sin abono no hay cifra: no se rellena con
    la promesa.
    """
    if not isinstance(recipient, str) or not recipient.strip():
        return None
    wanted = recipient.strip().lower()
    for tx in out_txs:
        if not isinstance(tx, dict):
            continue
        state_changes = tx.get("stateChanges")
        if not isinstance(state_changes, list):
            continue
        for change in state_changes:
            if not isinstance(change, dict):
                continue
            address = change.get("address")
            if not isinstance(address, str) or address.strip().lower() != wanted:
                continue
            data = change.get("change")
            amount = _uint(data.get("balanceDiff")) if isinstance(data, dict) else None
            if amount is not None and amount > 0:
                return amount
    return None


def _fail_reason(request: Mapping[str, Any]) -> str:
    """El motivo del fallo, si Relay dio uno que no sea el relleno `N/A`."""
    data = request.get("data")
    reason = data.get("failReason") if isinstance(data, dict) else None
    if isinstance(reason, str) and reason and reason != "N/A":
        return reason
    return ""


@dataclass(frozen=True, slots=True)
class RelayProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> RelayEngine:
        # `required_config` garantiza la clave: `validate_config` corre antes de
        # llegar aquí.
        return RelayEngine(api_key=config[CONFIG_API_KEY])


PROVIDER: Final = RelayProvider()
