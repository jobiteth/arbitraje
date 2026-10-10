"""Motor de puentes sobre la API de LI.FI.

Es el agregado de puentes: no cruza él, elige quién cruza (Across, Stargate,
Hop…) y devuelve el payload del origen ya construido. Compite con Relay por la
misma operación, y la interfaz ordena las dos rutas por lo que entregan.

### Lo medido, y lo que decidió cada cosa

Todo esto se midió en vivo el 2026-10-07 cotizando 1 USDC de Base a Polygon, y
está guardado como dato de prueba. La respuesta entera, tal cual llegó, es el
fichero `tests/data/lifi_usdc_base_polygon.json`.

- **Cotiza sin clave.** `GET https://li.quest/v1/quote` respondió 200 sin
  ninguna credencial. Por eso `api_key` es **opcional** aquí y no obligatoria:
  exigirla dejaría el motor apagado en una instalación que no la tenga, para no
  ganar nada. Con clave sube el límite de peticiones, y ese es el único motivo
  por el que existe el campo.
- **`fromAddress` se manda en minúsculas, y no es un detalle de estilo.** Con
  una dirección con *checksum* EIP-55 correcta la API responde `code 1011`
  —«Invalid extended address»— y no cotiza. Se descubrió midiendo. Como el
  destinatario se pasa a `plan_bridge` tal como lo escriba el usuario, la
  minúscula se aplica **en la frontera** del motor y no se confía en que llegue
  ya así.
- **El contrato de destino es el «diamante» de LI.FI**, y sale igual en todas
  las rutas de la misma red de origen: medido, `0x1231DEB6…4EaE` en Base, tanto
  para USDC→USDC como para USDC→ETH. Está en `DIAMONDS`, y `BRIDGE_CHAINS` se
  **deriva** de esa tabla, así que una red sin dirección medida no se cotiza:
  no se puede contrastar un destino que no se conoce.
- **La comisión viene desglosada, y en el token de origen.** Medido: tres
  partidas —`LIFI Fixed Fee` 2.500, `Relayer fee` 99 y `Relayer gas fee` 7.513—
  las tres en el USDC de Base, que es lo que sale de la cartera. Sumadas dan
  10.112 sobre 1.000.000, o sea 101 bps. **Esa es la razón de que la comisión se
  reste del importe recibido y no se sume aparte**: `amount_out` ya viene neto, y
  quien compare dos rutas por `amount_out` está comparando lo que de verdad
  llega.
- **`toAmount == toAmountMin` en el caso medido**, y no es un error de lectura:
  Across devuelve el mismo número en los dos campos. Enseñar sólo el estimado
  sería prometer lo que el mínimo garantiza; se enseñan los dos y cada uno dice
  lo que es.
- **El `value` viene en hexadecimal** (`"0x0"`), al contrario que en 0x, que lo
  manda decimal. El lector acepta las dos formas por la misma razón que allí.

### Lo que NO se midió, y por eso no está

`/v1/connections` —qué puentes cubren un par de redes— existe, pero no se usa:
la cotización ya dice por dónde va, y una llamada más por ruta es cuota gastada
en un dato que la respuesta trae. `allowExchanges`, `denyBridges` y
`preferBridges` se dejan **fuera** deliberadamente: son parámetros que la
documentación publica y que no se han medido aquí, y un parámetro inventado es
una restricción que el usuario cree tener y no tiene.
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

SOURCE_NAME: Final = "LI.FI"
HOST: Final = "li.quest"
QUOTE_URL: Final = f"https://{HOST}/v1/quote"
STATUS_URL: Final = f"https://{HOST}/v1/status"

CONFIG_API_KEY: Final = "api_key"

#: El «diamante» de LI.FI por red de **origen**, medido. Es el contrato al que se
#: manda el dinero y el que se contrasta antes de firmar.
#:
#: Medido el 2026-10-07 pidiendo una cotización por cada red y leyendo a dónde
#: dice que hay que firmar. Es la **misma** dirección en las cinco, que es lo
#: esperable de un despliegue CREATE2, pero se sigue consultando por red de
#: origen y contrastando contra lo que devuelve cada cotización: si alguna vez
#: devolviera otra, `_require_known_diamond` para la operación en vez de firmar
#: contra un contrato que no se ha comprobado. `BRIDGE_CHAINS` sale de aquí para
#: que no puedan discrepar.
DIAMONDS: Final[Mapping[str, str]] = {
    "ethereum": "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae",
    "base": "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae",
    "arbitrum": "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae",
    "optimism": "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae",
    "polygon": "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae",
}

#: Las redes **desde** las que este motor puede cruzar, derivadas de la tabla de
#: destinos medida. Es el equivalente de `swap_chains` para puentes: ver
#: `EngineManifest.bridge_chains`.
BRIDGE_CHAINS: Final = frozenset(DIAMONDS)

#: Deriva máxima tolerada entre el importe recibido que se enseñó y el de la
#: cotización fresca que se firma. Un puente tarda minutos, así que aquí la
#: deriva no es un detalle de precisión: es lo que separa «recibes casi lo
#: mismo» de «el mercado se movió y vas a recibir bastante menos».
MAX_QUOTE_DRIFT_BPS: Final = BasisPoints(100)

#: Centinela con el que se cotiza sin destinatario real. Fijo, para que la caché
#: se comparta: el destinatario no cambia el precio, sólo entra en el calldata de
#: la transacción que se construye después.
QUOTE_RECIPIENT: Final = "0x1111111111111111111111111111111111111111"

#: Cómo se nombra el **token nativo** de una red en esta API. La API identifica
#: los tokens por dirección de contrato, y el nativo no tiene ninguna, así que
#: usa un centinela.
#:
#: Medido el 2026-10-07. La documentación publica **dos** candidatos —la
#: dirección cero y `0xEeee…EEeE`— y la medición dice que los dos se aceptan,
#: pero que la API **normaliza los dos a la dirección cero**: en las dos
#: respuestas devuelve `symbol: ETH` y `address: 0x0000…0000`. Se guarda la
#: forma canónica, que es la que la propia API usa.
#:
#: Que se acepten los dos no bastaba como medición, porque aceptar cualquier
#: cosa daría el mismo resultado: se probó también con una dirección inventada y
#: con el USDC de otra red, y las dos se rechazan con `code 1011` «is invalid or
#: in deny list». Eso es lo que convierte esto en una medición y no en una
#: coincidencia.
#:
#: Con esto puesto, un origen nativo —«ETH Base → USDC Polygon», que es una de
#: las rutas que se pidieron— ya se cotiza.
#:
#: El tipo es `str` y no `str | None`, que es lo que era mientras la medición no
#: existía: aquel `None` significaba «no sé con qué nombre pedirlo», y con el
#: centinela medido ese estado ya no existe. Dejarlo opcional obligaba a cada
#: llamada a tratar un caso imposible, y un caso imposible tratado en cinco
#: sitios es una rama muerta que nadie se atreve a borrar.
#:
#: `noqa: S105` porque el analizador ve «TOKEN» en el nombre y avisa de una
#: credencial. No lo es: es la dirección con la que LI.FI nombra la moneda
#: nativa, un valor público que viaja en la URL de la petición.
NATIVE_TOKEN_ADDRESS: Final = "0x0000000000000000000000000000000000000000"  # noqa: S105

MANIFEST: Final = EngineManifest(
    engine_id="lifi",
    name="LI.FI — puentes entre redes",
    version="1.0.0",
    kind=EngineKind.CROSS_CHAIN,
    summary=(
        "Cruza tokens entre redes repartiendo la operación entre los puentes que "
        "agrega LI.FI (Across, Stargate…). Cotiza y construye; la comisión la "
        "cobra el puente que elija y viene desglosada en cada ruta."
    ),
    capabilities=frozenset(
        {Capability.READ_CHAIN, Capability.COMPUTE_ROUTE, Capability.PREPARE_TX}
    ),
    bridge_chains=BRIDGE_CHAINS,
    # Antes que Relay: medido, cubre más puentes por par —en USDC Base→Polygon
    # ofreció Across con tres partidas de comisión— y su cotización no depende de
    # una clave obligatoria. La preferencia aquí sólo decide a quién se le
    # **pregunta** primero; el orden que ve el usuario lo decide el importe.
    bridge_priority=50,
    # Opcional y no obligatoria, medido: sin clave cotiza y construye. Con ella
    # sube el límite de peticiones.
    optional_config=(CONFIG_API_KEY,),
    allowed_hosts=(HOST,),
)


class LifIEngine:
    """Cotiza y construye puentes por la API agregadora de LI.FI."""

    __slots__ = ("_clock", "_source")

    def __init__(
        self,
        *,
        api_key: str = "",
        clock: Clock | None = None,
        ttl_seconds: float = 5.0,
        timeout_seconds: float = 30.0,
        min_interval_seconds: float = 0.5,
    ) -> None:
        self._clock = clock or SystemClock()
        headers = {"x-lifi-api-key": api_key} if api_key else {}
        self._source = JsonSource(
            name=SOURCE_NAME,
            allowed_hosts=MANIFEST.allowed_hosts,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            min_interval_seconds=min_interval_seconds,
            headers=headers,
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
        """La ruta que LI.FI ofrece para esa petición, o ninguna.

        Devuelve **una**: LI.FI ya elige el mejor puente disponible y responde
        con él, así que la comparación entre puentes la hace el agregado y no
        nosotros. Comparar sus rutas entre sí exigiría pedirlas una a una con
        `denyBridges`, que es un parámetro no medido — ver el docstring del
        módulo.
        """
        if request.origin.chain not in BRIDGE_CHAINS:
            _log.debug("lifi.origin_chain_not_measured", chain=request.origin.chain)
            return ()

        payload = await self._quote_payload(request, recipient=QUOTE_RECIPIENT)
        if payload is None:
            return ()
        quote = self._to_bridge_quote(payload, request)
        return () if quote is None else (quote,)

    # ---------------------------------------------------------- construir #
    async def plan_bridge(
        self, quote: BridgeQuote, *, recipient: str
    ) -> UnsignedTransaction:
        """Construye el payload de origen sin firmar, o falla diciendo por qué.

        No firma ni emite. Cotiza **otra vez** a nombre de `recipient` porque el
        destinatario forma parte del calldata: el payload de una cotización
        pedida para otro no sirve para este. Y compara el importe de esa
        cotización fresca con el que se enseñó, porque entre las dos pueden pasar
        minutos y el mercado no se queda quieto.
        """
        request = quote.request
        if request.origin.chain not in BRIDGE_CHAINS:
            raise UnsupportedOperationError(
                f"este motor sólo cruza desde las redes que tiene medidas: se pidió "
                f"«{request.origin.chain}», y las medidas son "
                f"{', '.join(sorted(BRIDGE_CHAINS))}."
            )
        payload = await self._quote_payload(request, recipient=recipient)
        if payload is None:
            raise NoQuotesError(
                f"«{request.symbol}» ya no tiene ruta en {SOURCE_NAME}: la que "
                f"viste al cotizar se agotó. Vuelve a cotizar."
            )
        return self._to_unsigned(quote, payload, recipient=recipient)

    def expected_destination(self, chain_key: str) -> frozenset[str] | None:
        """El diamante medido para esa red de origen, o `None` si no se midió.

        Un solo contrato —medido estable en todas las rutas de la misma red—,
        pero se declara como conjunto porque el contrato del camino de ejecución
        es un conjunto: los motores cuyo destino depende de la ruta caben en el
        mismo tipo sin cambiar de forma.
        """
        diamond = DIAMONDS.get(chain_key)
        return None if diamond is None else frozenset({diamond})

    # ---------------------------------------------------------- seguir #
    async def track_bridge(
        self, tx_hash: str, *, origin_chain: str, destination_chain: str
    ) -> BridgeProgress:
        """Lo que LI.FI dice de un puente emitido. Sólo lee."""
        raw = await self._source.get_json(
            STATUS_URL,
            params={
                "txHash": tx_hash,
                "fromChain": str(chain(origin_chain).require_eip155_id()),
                "toChain": str(chain(destination_chain).require_eip155_id()),
            },
            absent_statuses=frozenset({404}),
        )
        if raw is None:
            return BridgeProgress(
                state=BridgeTrackState.UNKNOWN,
                provider_status="",
                substatus="",
                message="LI.FI aún no conoce esta transacción.",
            )
        return progress_from_status(as_mapping(raw, "estado", SOURCE_NAME))

    # ------------------------------------------------------------ interno #
    async def _quote_payload(
        self, request: BridgeRequest, *, recipient: str
    ) -> Mapping[str, Any] | None:
        """El cuerpo de la respuesta, o `None` si la fuente dice que no hay ruta."""
        raw = await self._source.get_json(
            QUOTE_URL,
            params={
                "fromChain": str(chain(request.origin.chain).require_eip155_id()),
                "toChain": str(chain(request.destination.chain).require_eip155_id()),
                "fromToken": _token_address(request.origin),
                "toToken": _token_address(request.destination),
                "fromAmount": str(request.amount_in.raw),
                # En minúsculas siempre: medido, una dirección con checksum
                # EIP-55 inválido —o simplemente con mayúsculas— responde
                # `code 1011` y no cotiza.
                "fromAddress": recipient.strip().lower(),
            },
            # Medido: cuando no hay ruta, la API contesta 404 con un cuerpo de
            # error. Es una respuesta informativa —«ese par de redes no se puede
            # cruzar con lo que hay ahora»—, no un formato roto, y por eso se
            # declara ausente en vez de dejar que aborte el barrido entero.
            absent_statuses=frozenset({404}),
        )
        if raw is None:
            return None
        return as_mapping(raw, "respuesta", SOURCE_NAME)

    def _to_bridge_quote(
        self, payload: Mapping[str, Any], request: BridgeRequest
    ) -> BridgeQuote | None:
        """Traduce la respuesta, o `None` si no describe lo que se preguntó."""
        estimate = payload.get("estimate")
        if not isinstance(estimate, dict):
            _log.info("lifi.quote_without_estimate", pair=request.symbol)
            return None

        out_raw = _uint(estimate.get("toAmount"))
        out_min_raw = _uint(estimate.get("toAmountMin"))
        echo_in = _uint(estimate.get("fromAmount"))
        if out_raw is None or out_raw <= 0 or out_min_raw is None:
            _log.info("lifi.quote_unreadable_amount", pair=request.symbol)
            return None
        # Se comprueba que la respuesta hable del importe que se preguntó: una
        # cotización de otra cantidad se compararía contra las demás como si
        # fuera del mismo tamaño, y ganaría o perdería por el tamaño y no por el
        # precio.
        if echo_in is not None and echo_in != request.amount_in.raw:
            _log.info(
                "lifi.quote_other_amount",
                pair=request.symbol,
                requested=request.amount_in.raw,
                quoted=echo_in,
            )
            return None

        destination = request.destination
        fee, hay_fee_ajena = _fee_in_origin(payload, request)
        duration = _uint(estimate.get("executionDuration"))
        return BridgeQuote(
            engine_id=MANIFEST.engine_id,
            provider=_provider_name(payload),
            request=request,
            amount_out=TokenAmount(out_raw, destination.decimals, destination.symbol),
            amount_out_min=TokenAmount(
                out_min_raw, destination.decimals, destination.symbol
            ),
            fee=fee,
            fee_basis=Measurement.REPORTED if fee is not None else None,
            duration_seconds=duration,
            observed_at=self._clock.now(),
            route=_route_note(payload),
            source_note=_source_note(hay_fee_ajena),
        )

    def _to_unsigned(
        self,
        quote: BridgeQuote,
        payload: Mapping[str, Any],
        *,
        recipient: str,
    ) -> UnsignedTransaction:
        """Traduce el `transactionRequest`, contrastando antes el destino."""
        request = quote.request
        chain_key = request.origin.chain
        spec = chain(chain_key)

        fresh = _fresh_out(payload)
        self._require_same_output(quote, fresh)

        raw_tx = payload.get("transactionRequest")
        if raw_tx is None:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» cotizó pero no devolvió `transactionRequest`: sin "
                f"payload no hay nada que el usuario pueda revisar."
            )
        tx = as_mapping(raw_tx, "transactionRequest", SOURCE_NAME)

        # El destino se contrasta **antes** de leer nada más: es el dato contra
        # el que se firma, y aquí un destino equivocado no pierde dinero en una
        # operación de mercado — lo manda a otra parte, y no vuelve.
        destination = _require_known_diamond(chain_key, tx.get("to"))

        calldata = tx.get("data")
        if not isinstance(calldata, str) or not _is_hex(calldata):
            raise SourceResponseError(
                f"«{SOURCE_NAME}» devolvió un `data` que no es hexadecimal válido; "
                f"sin calldata no hay transacción que revisar."
            )
        # Medido: aquí el `value` viene en hexadecimal (`"0x0"`), al revés que en
        # 0x, que lo manda decimal. El lector acepta las dos formas.
        value_raw = _uint(tx.get("value"))
        if value_raw is None:
            raise SourceResponseError(
                f"«{SOURCE_NAME}» no publicó un `value` legible "
                f"({tx.get('value')!r}); no se puede saber cuánto nativo mueve la "
                f"transacción."
            )

        # Si el token de origen es un ERC-20 hay que autorizar a alguien, y la
        # API dice a quién: medido, `estimate.approvalAddress` vale lo mismo que
        # el destino en la ruta de Base, pero se lee del campo en vez de
        # suponerlo, porque en una ruta con varios pasos puede ser otro contrato.
        approval = _approval(payload, request)

        _log.info(
            "lifi.bridge_planned",
            pair=request.symbol,
            origin=chain_key,
            out_raw=fresh,
            destination=destination,
            value_raw=value_raw,
            approval=approval.target if approval else None,
        )
        return UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=destination,
            calldata=calldata,
            value=TokenAmount(value_raw, spec.native_decimals, spec.native_symbol),
            gas_limit=_uint(tx.get("gasLimit")),
            approval=approval,
            description=_description(quote, fresh, destination, recipient),
        )

    def _require_same_output(self, quote: BridgeQuote, fresh_raw: int) -> None:
        """Aborta si lo que se va a recibir se movió más de lo tolerado."""
        shown_raw = quote.amount_out.raw
        if shown_raw <= 0:
            return
        drift = EXACT.divide(Decimal(abs(fresh_raw - shown_raw)), Decimal(shown_raw))
        drift_bps = BasisPoints.from_ratio(drift)
        if drift_bps.value <= MAX_QUOTE_DRIFT_BPS.value:
            return
        token = quote.request.destination
        _log.info(
            "lifi.quote_moved",
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
def progress_from_status(payload: Mapping[str, Any]) -> BridgeProgress:
    """Traduce la respuesta de `/v1/status` a un estado, sin inventar pasos."""
    status = str(payload.get("status", ""))
    substatus = str(payload.get("substatus", ""))
    message = str(payload.get("substatusMessage", "")) or status
    receiving = payload.get("receiving")
    receiving_map: Mapping[str, Any] = receiving if isinstance(receiving, Mapping) else {}

    if status == "DONE":
        state = BridgeTrackState.DONE
    elif status == "FAILED":
        state = (
            BridgeTrackState.REFUNDED if substatus == "REFUNDED" else BridgeTrackState.FAILED
        )
    elif status == "PENDING":
        state = (
            BridgeTrackState.REFUNDING
            if substatus == "REFUND_IN_PROGRESS"
            else BridgeTrackState.PENDING
        )
    else:
        state = BridgeTrackState.UNKNOWN

    tx = receiving_map.get("txHash")
    chain_id = receiving_map.get("chainId")
    return BridgeProgress(
        state=state,
        provider_status=status,
        substatus=substatus,
        message=message,
        receiving_tx_hash=tx if isinstance(tx, str) and tx else None,
        received_raw=_uint(receiving_map.get("amount")),
        received_chain=str(chain_id) if chain_id is not None else None,
    )


def _token_address(token: Token) -> str:
    """La dirección con la que esta API nombra ese token. Siempre hay una.

    Un ERC-20 la trae en el propio token. El nativo de una red no tiene contrato,
    y esta API lo nombra con el centinela medido —ver `NATIVE_TOKEN_ADDRESS`—,
    así que aquí no queda ningún caso «no se sabe»: los dos caminos devuelven
    algo que la API acepta, y eso es lo que permite cotizar un origen nativo en
    vez de negarse a mirarlo.
    """
    return token.address or NATIVE_TOKEN_ADDRESS


def _fresh_out(payload: Mapping[str, Any]) -> int:
    estimate = payload.get("estimate")
    if not isinstance(estimate, dict):
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió una cotización sin `estimate`; no se puede "
            f"saber cuánto llegaría al destino, que es justo lo que se firma."
        )
    out_raw = _uint(estimate.get("toAmount"))
    if out_raw is None or out_raw <= 0:
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió una cotización sin importe de salida "
            f"legible; no se puede construir nada con ella."
        )
    return out_raw


def _require_known_diamond(chain_key: str, returned: Any) -> str:
    """Comprueba el destino contra el medido, o se niega a construir.

    Comprueba **identidad**, no forma: las dos direcciones se comparan en
    minúsculas. Antes sólo se pasaba a minúsculas la que devolvía la fuente, así
    que el resultado dependía de cómo estuviera escrita la tabla — y la
    presentación con checksum, que es la legible y la que usa el resto del
    proyecto, habría hecho fallar la comprobación contra la dirección correcta.
    Relay ya comparaba las dos; esto lo pone igual.

    Se indexa la tabla sin defensa porque no puede faltar: `BRIDGE_CHAINS` se
    deriva de ella y la llamada ya pasó por ese filtro.
    """
    expected = DIAMONDS[chain_key]
    if not isinstance(returned, str) or returned.strip().lower() != expected.lower():
        raise SourceResponseError(
            f"«{SOURCE_NAME}» devolvió como destino del puente {returned!r} en "
            f"«{chain_key}», y el contrato medido para esa red es {expected}. No se "
            f"construyó nada: aquí un destino equivocado no pierde dinero en una "
            f"operación de mercado, lo manda a otra parte y no vuelve."
        )
    return expected


def _approval(payload: Mapping[str, Any], request: BridgeRequest) -> TokenApproval | None:
    """A quién hay que autorizar el token de origen, si hay que autorizar a alguien.

    Se lee de `estimate.approvalAddress`. Un token nativo no se autoriza —no hay
    contrato que autorizar—, así que ahí se devuelve `None` aunque la API
    publique una dirección: dársela al ejecutor le haría construir un `approve`
    sobre un token que no existe.
    """
    if request.origin.is_native:
        return None
    estimate = payload.get("estimate")
    if not isinstance(estimate, dict):
        return None
    address = estimate.get("approvalAddress")
    if not isinstance(address, str) or not _is_address(address):
        # Sin dirección legible se cae al camino directo contra el destino, que
        # es lo que `UnsignedTransaction` hace cuando no se le dice otra cosa. No
        # se inventa un gastador: autorizar al contrato equivocado es firmar un
        # permiso que no sirve y pagar el gas de descubrirlo.
        _log.info(
            "lifi.approval_address_unreadable",
            pair=request.symbol,
            value=repr(address)[:64],
        )
        return None
    return TokenApproval(spender=address.lower())


def _fee_in_origin(
    payload: Mapping[str, Any], request: BridgeRequest
) -> tuple[TokenAmount | None, bool]:
    """La comisión total, **sólo** si todas las partidas están en el token de origen.

    Devuelve `(comisión, hay_partida_en_otro_token)`. El segundo valor no es
    decorativo: distingue «la fuente no desglosa comisión» de «la desglosa, pero
    una parte se cobra en otro token», y son dos frases distintas en la nota.

    No se suman partidas de tokens distintos porque el total no significaría
    nada: 0,0025 USDC más 0,0001 ETH no es una cifra, es una lista mal sumada. Y
    cuando aparece una, se devuelve `None` —comisión desconocida— **sin seguir
    sumando** las demás: sumar lo que queda daría una parte presentada como el
    total, y esta ruta parecería más barata de lo que es justo en el número con
    el que se la compara con las otras.

    Se incluyen **sólo** las partidas con `included: true`: las que no lo están
    se cobran aparte y no salen de `amount_out`, así que meterlas aquí inflaría
    una comisión con un pago que no se ha hecho.
    """
    estimate = payload.get("estimate")
    if not isinstance(estimate, dict):
        return None, False
    costs = estimate.get("feeCosts")
    if not isinstance(costs, list):
        return None, False

    origin_address = _token_address(request.origin).lower()
    total = 0
    for entry in costs:
        if not isinstance(entry, dict) or entry.get("included") is False:
            continue
        amount = _uint(entry.get("amount"))
        if amount is None or amount == 0:
            continue
        token = entry.get("token")
        address = token.get("address") if isinstance(token, dict) else None
        if not isinstance(address, str) or address.lower() != origin_address:
            _log.info(
                "lifi.fee_in_another_token",
                pair=request.symbol,
                name=entry.get("name"),
                token=repr(address)[:64],
            )
            # Se para aquí y se declara desconocida, en vez de sumar el resto.
            # Sumar lo que queda daría **una parte** de la comisión presentada
            # como el total, y eso hace que esta ruta parezca más barata de lo
            # que es justo en el número con el que se la compara con las otras.
            # Es el lado por el que interesa equivocarse: una ruta que parece
            # cara se descarta, una que parece barata se elige.
            return None, True
        total += amount

    if total == 0:
        # Un cero aquí no es «gratis»: es que la fuente no lo ha puesto. Una
        # comisión publicada de cero no se acepta nunca en este proyecto.
        return None, False
    return TokenAmount(total, request.origin.decimals, request.origin.symbol), False


def _provider_name(payload: Mapping[str, Any]) -> str:
    """El puente que de verdad cruza: `AcrossV4`, `Stargate`…, o el identificador."""
    details = payload.get("toolDetails")
    if isinstance(details, dict):
        name = details.get("name")
        if isinstance(name, str) and name:
            return name
    tool = payload.get("tool")
    return tool if isinstance(tool, str) and tool else "puente sin identificar"


def _route_note(payload: Mapping[str, Any]) -> str:
    """Por dónde pasa la ruta, leído de los pasos que la propia API declara."""
    steps = payload.get("includedSteps")
    if not isinstance(steps, list):
        return "ruta no detallada"
    partes: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        kind = step.get("type")
        if kind == "cross":
            parte = f"cruce por {_step_tool(step)}"
        elif kind == "protocol":
            parte = f"paso previo ({_step_tool(step)})"
        elif isinstance(kind, str):
            parte = kind
        else:
            continue
        if parte not in partes:
            partes.append(parte)
    return " → ".join(partes) if partes else "ruta no detallada"


def _step_tool(step: Mapping[str, Any]) -> str:
    details = step.get("toolDetails")
    if isinstance(details, dict):
        name = details.get("name")
        if isinstance(name, str) and name:
            return name
    tool = step.get("tool")
    return tool if isinstance(tool, str) and tool else "sin identificar"


def _source_note(fee_in_another_token: bool) -> str:
    base = (
        "El importe recibido ya viene neto de comisión: lo que se enseñe es lo que "
        "llega. El mínimo garantizado es el suelo que el puente se compromete a "
        "entregar aunque el mercado se mueva mientras la operación viaja."
    )
    if fee_in_another_token:
        return (
            base
            + " Hay alguna partida de comisión cobrada en otro token, así que el "
            "total no se suma: se dice que no se sabe antes que sumar unidades "
            "distintas."
        )
    return base


def _description(
    quote: BridgeQuote, fresh_raw: int, destination: str, recipient: str
) -> str:
    request = quote.request
    out = TokenAmount(
        fresh_raw, request.destination.decimals, request.destination.symbol
    )
    return (
        f"Puente por {quote.provider} ({SOURCE_NAME}): entregas "
        f"{request.amount_in} en {request.origin.chain} y recibes {out} en "
        f"{request.destination.chain}, con un mínimo garantizado de "
        f"{quote.amount_out_min}. Recibe {recipient}. El contrato de destino es "
        f"{destination}, el que «{SOURCE_NAME}» tiene medido para "
        f"{request.origin.chain}. Esta transacción no incluye la aprobación previa "
        f"del token, si hiciera falta."
    )


def _uint(value: Any) -> int | None:
    """Lee un entero sin signo, en decimal **o** en hexadecimal.

    Las dos formas hacen falta y están medidas: esta API manda el `value` en
    hexadecimal (`"0x0"`) y los importes en decimal (`"989888"`). Un lector que
    sólo entendiera una de las dos leería la otra como cero o como ilegible.
    """
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
    """Si el texto es calldata hexadecimal: `0x` y una longitud par de dígitos."""
    text = value.strip()
    if not text.startswith("0x") or len(text) <= 2:
        return False
    body = text[2:]
    return len(body) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in body)


def _is_address(value: str) -> bool:
    """Si el texto tiene la forma de una dirección EVM: `0x` y 40 dígitos."""
    text = value.strip()
    if not text.startswith("0x") or len(text) != 42:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in text[2:])


@dataclass(frozen=True, slots=True)
class LifIProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> LifIEngine:
        # La clave es opcional y ausente significa «sin clave», que es un modo de
        # funcionamiento medido y no un estado a medias.
        return LifIEngine(api_key=config.get(CONFIG_API_KEY, "").strip())


PROVIDER: Final = LifIProvider()
