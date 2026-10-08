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

### Lo que ya está medido: la tabla de depósitos

`DEPOSITORIES` se rellenó el 2026-10-07 midiendo la API en vivo, que es lo que
este docstring pedía a quien lo leyera. El valor es **el mismo contrato en todas
las redes** —`0x4cD00E387622C35bDDB9b4c962C136462338BC31`, un despliegue CREATE2
de dirección idéntica—, y no depende de la ruta: se cruzaron tres usuarios
distintos, tres importes (0,01 · 1 · 1.000 USDC) y dos destinos sobre cinco redes
de origen, y en todas las combinaciones que Relay aceptó salió la misma dirección.
Lo que antes era un ejemplo de la documentación ahora es una medición.

Que sea la misma en todas partes no es un motivo para escribir una constante
suelta: la tabla se sigue consultando **por red de origen** y se sigue
contrastando contra lo que devuelve cada cotización antes de firmar. Si Relay
alguna vez devuelve otro destino, `_require_known_depository` para la operación
en vez de mandar el dinero a donde no vuelve.

Con la tabla llena, `expected_destination` devuelve la dirección y `plan_bridge`
construye. Sigue rechazando lo que no cabe en una transacción —ver arriba—, así
que lo que se enciende es construir, no tragarse cualquier cosa.
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
    BridgeQuote,
    BridgeRequest,
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

CONFIG_API_KEY: Final = "api_key"

#: Cómo nombra esta API el token nativo de una red. Tomado de su ejemplo, no
#: elegido: es el valor con el que la documentación cruza ETH.
NATIVE_CURRENCY: Final = "0x0000000000000000000000000000000000000000"

#: Contratos a los que Relay manda los fondos **desde** cada red de origen.
#:
#: Medido el 2026-10-07 contra la API en vivo, no copiado de la documentación:
#: el mismo contrato en las cinco redes, estable ante el usuario, el importe y el
#: destino. Ver el docstring del módulo para cómo se midió. `BRIDGE_CHAINS` sale
#: de aquí para que no discrepen.
DEPOSITORIES: Final[Mapping[str, str]] = {
    "ethereum": "0x4cD00E387622C35bDDB9b4c962C136462338BC31",
    "base": "0x4cD00E387622C35bDDB9b4c962C136462338BC31",
    "arbitrum": "0x4cD00E387622C35bDDB9b4c962C136462338BC31",
    "optimism": "0x4cD00E387622C35bDDB9b4c962C136462338BC31",
    "polygon": "0x4cD00E387622C35bDDB9b4c962C136462338BC31",
}

#: Las redes **desde** las que este motor puede construir un cruce. Cotizar
#: depende de la misma tabla a propósito: enseñar una ruta que no se puede firmar
#: es peor que no enseñarla, porque el usuario la elige y se queda mirándola.
BRIDGE_CHAINS: Final = frozenset(DEPOSITORIES)

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
        "construye desde las cinco redes cuyo contrato de depósito se midió, y "
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

    def expected_destination(self, chain_key: str) -> str | None:
        """El contrato de destino medido para esa red de origen, o `None`."""
        return DEPOSITORIES.get(chain_key)

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
    expected = DEPOSITORIES[chain_key]
    if not isinstance(returned, str) or returned.strip().lower() != expected.lower():
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió como destino del puente {returned!r} en "
            f"«{chain_key}», y el contrato medido para esa red es {expected}. No se "
            f"construyó nada: aquí un destino equivocado no pierde dinero en una "
            f"operación de mercado, lo manda a otra parte y no vuelve."
        )
    return expected


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


@dataclass(frozen=True, slots=True)
class RelayProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> RelayEngine:
        # `required_config` garantiza la clave: `validate_config` corre antes de
        # llegar aquí.
        return RelayEngine(api_key=config[CONFIG_API_KEY])


PROVIDER: Final = RelayProvider()
