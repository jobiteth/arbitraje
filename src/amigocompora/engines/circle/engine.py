"""Motor de puentes sobre CCTP v2 de Circle: USDC nativo entre redes.

Quema el USDC en el origen con `TokenMessengerV2.depositForBurn` y lo recibe en el
destino cuando Circle atesta el mensaje. Este módulo sólo **cotiza y construye** el
paso de origen. El paso de recepción —consultar la atestación y llamar a
`MessageTransmitterV2.receiveMessage`— vive en el caso de uso de traslado, porque
es una segunda transacción separada por minutos de espera.

Los contratos y los dominios son los que publica Circle en su tabla de direcciones
de CCTP v2 (mainnet). Las redes que no aparecen en esa tabla no se cotizan.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import structlog
from eth_abi.abi import encode
from eth_utils.crypto import keccak

from amigocompora.domain.chains import chain
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import (
    NoQuotesError,
    QuoteMovedError,
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
from amigocompora.engines.circle.attestation import (
    COMPLETE,
    MESSAGE_TRANSMITTER,
    encode_receive_message,
    fetch_attestation,
    fetch_message_status,
)
from amigocompora.engines.http_source import JsonSource

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "Circle CCTP v2"
HOST: Final = "iris-api.circle.com"
FEES_URL: Final = f"https://{HOST}/v2/burn/USDC/fees"

#: Dirección de `TokenMessengerV2`, la misma en todas las redes de la tabla.
TOKEN_MESSENGER: Final = "0x28b5a0e9C621a5BadaA536219b3a228C8168cf5d"  # noqa: S105  # dirección pública

#: Dominio de CCTP por red de origen o destino. Sólo las redes de esta tabla tienen
#: contrato medido en la documentación de Circle; BSC, Robinhood y Solana no están.
CCTP_DOMAINS: Final[Mapping[str, int]] = {
    "ethereum": 0,
    "avalanche": 1,
    "optimism": 2,
    "arbitrum": 3,
    "base": 6,
    "polygon": 7,
    "unichain": 10,
    "arc": 26,
}

#: USDC nativo de Circle por red. CCTP sólo quema y mintea esta moneda: el USDC.e
#: puenteado de Polygon (`0x2791…4174`) revierte si se intenta quemar. Coincide con
#: `engines/catalog.py`, y una prueba lo comprueba.
NATIVE_USDC: Final[Mapping[str, str]] = {
    "ethereum": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "arbitrum": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    "optimism": "0x0b2c639c533813f4aa9d7837caf62653d097ff85",
    "avalanche": "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e",
    "polygon": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
    "unichain": "0x078d782b760474a361dda0af3839290b0ef57ad6",
    "arc": "0x3600000000000000000000000000000000000000",
}

#: Espera que Circle aplica antes de atestar una quema estándar (umbral 2000), por red
#: de origen. Base espera la finalidad L1: unos 65 bloques de Ethereum.
ESPERA_FINALIDAD: Final[Mapping[str, str]] = {
    "base": "unos 65 bloques de Ethereum, entre 15 y 20 minutos",
}


def _mensaje_espera(origin_chain: str) -> str:
    espera = ESPERA_FINALIDAD.get(origin_chain)
    if espera is None:
        return "Circle espera a que la quema tenga confirmaciones suficientes."
    return (
        f"Circle espera la finalidad de «{origin_chain}» antes de atestar: {espera}. "
        "Después la recepción en destino se emite sola."
    )

#: Finalidad estándar: la que Circle cobra a 0 o al mínimo y la única que se usa
#: aquí. La rápida (1000) cobra comisión y no se ofrece todavía.
STANDARD_FINALITY: Final = 2000

#: Deriva máxima tolerada entre lo enseñado y lo que se firma, como en LI.FI.
MAX_QUOTE_DRIFT_BPS: Final = BasisPoints(100)

DEPOSIT_FOR_BURN_SIGNATURE: Final = (
    "depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)"
)
DEPOSIT_FOR_BURN_TYPES: Final = (
    "uint256",
    "uint32",
    "bytes32",
    "address",
    "bytes32",
    "uint256",
    "uint32",
)
SELECTOR_DEPOSIT_FOR_BURN: Final = "0x" + keccak(text=DEPOSIT_FOR_BURN_SIGNATURE)[:4].hex()

#: `destinationCaller` a cero: cualquiera puede llamar a `receiveMessage` en destino.
#: Con una dirección fija sólo esa dirección podría completar el traslado.
ANY_CALLER: Final = b"\x00" * 32

MANIFEST: Final = EngineManifest(
    engine_id="circle",
    name="Circle CCTP — USDC nativo entre redes",
    version="1.0.0",
    kind=EngineKind.CROSS_CHAIN,
    summary=(
        "Quema USDC en la red de origen y lo recibe como USDC nativo en la de "
        "destino, sin pasar por un puente con liquidez. Es un traslado en dos "
        "pasos: el segundo lo completa la aplicación cuando Circle lo atesta."
    ),
    capabilities=frozenset(
        {Capability.READ_CHAIN, Capability.COMPUTE_ROUTE, Capability.PREPARE_TX}
    ),
    bridge_chains=frozenset(CCTP_DOMAINS),
    bridge_priority=40,
    allowed_hosts=(HOST,),
)


class CircleEngine:
    """Cotiza y construye el `depositForBurn` de CCTP v2 para USDC."""

    __slots__ = ("_clock", "_source")

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        ttl_seconds: float = 15.0,
        timeout_seconds: float = 30.0,
        min_interval_seconds: float = 0.5,
    ) -> None:
        self._clock = clock or SystemClock()
        self._source = JsonSource(
            name=SOURCE_NAME,
            allowed_hosts=MANIFEST.allowed_hosts,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            min_interval_seconds=min_interval_seconds,
            headers={},
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
        """La ruta de USDC a USDC entre las dos redes, o ninguna."""
        if not _is_usdc_route(request):
            return ()
        bps = await self._standard_fee_bps(request)
        if bps is None:
            return ()
        fee_raw = _fee_raw(request.amount_in.raw, bps)
        out_raw = request.amount_in.raw - fee_raw
        if out_raw <= 0:
            return ()
        destination = request.destination
        origin = request.origin
        return (
            BridgeQuote(
                engine_id=MANIFEST.engine_id,
                provider="Circle CCTP v2",
                request=request,
                amount_out=TokenAmount(out_raw, destination.decimals, destination.symbol),
                amount_out_min=TokenAmount(
                    out_raw, destination.decimals, destination.symbol
                ),
                fee=TokenAmount(fee_raw, origin.decimals, origin.symbol),
                fee_basis=Measurement.REPORTED,
                duration_seconds=None,
                observed_at=self._clock.now(),
                route=(
                    f"quema en {request.origin.chain} → atestación de Circle → "
                    f"mint en {request.destination.chain}"
                ),
                source_note=(
                    "USDC nativo, sin pool de liquidez: lo que recibes es lo que "
                    "quema Circle menos la comisión publicada. El segundo paso se "
                    "completa solo cuando Circle atesta el mensaje."
                ),
            ),
        )

    # ---------------------------------------------------------- construir #
    async def plan_bridge(
        self, quote: BridgeQuote, *, recipient: str
    ) -> UnsignedTransaction:
        """El `depositForBurn` de origen, o falla diciendo por qué.

        No firma ni emite. Vuelve a pedir la comisión: si ha subido respecto a la que
        se enseñó, se niega a construir en vez de firmar con un tope distinto.
        """
        request = quote.request
        if not _is_usdc_route(request):
            raise UnsupportedOperationError(
                f"Circle sólo cruza USDC entre redes CCTP: se pidió "
                f"«{request.symbol}» de «{request.origin.chain}» a "
                f"«{request.destination.chain}»."
            )
        bps = await self._standard_fee_bps(request)
        if bps is None:
            raise NoQuotesError(
                f"Circle no publica comisión para «{request.origin.chain}» → "
                f"«{request.destination.chain}» ahora mismo. Vuelve a cotizar."
            )
        fresh_fee = _fee_raw(request.amount_in.raw, bps)
        fresh_out = request.amount_in.raw - fresh_fee
        self._require_same_output(quote, fresh_out)

        burn_token = request.origin.address
        if burn_token is None:
            raise UnsupportedOperationError("el USDC de origen no tiene dirección de contrato")

        calldata = encode_deposit_for_burn(
            amount=request.amount_in.raw,
            destination_domain=CCTP_DOMAINS[request.destination.chain],
            recipient=recipient,
            burn_token=burn_token,
            max_fee=fresh_fee,
        )
        spec = chain(request.origin.chain)
        fee = TokenAmount(fresh_fee, request.origin.decimals, request.origin.symbol)
        _log.info(
            "circle.burn_planned",
            origin=request.origin.chain,
            destination=request.destination.chain,
            amount_raw=request.amount_in.raw,
            max_fee_raw=fresh_fee,
        )
        return UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=TOKEN_MESSENGER,
            calldata=calldata,
            value=TokenAmount(0, spec.native_decimals, spec.native_symbol),
            gas_limit=None,
            approval=TokenApproval(spender=TOKEN_MESSENGER.lower()),
            description=(
                f"Circle CCTP: quemas {request.amount_in} en {request.origin.chain} y "
                f"recibes hasta {quote.amount_out_min} en {request.destination.chain}, "
                f"con comisión máxima de {fee}. "
                f"Recibe {recipient}. El segundo paso (mint en destino) lo hace la "
                f"aplicación cuando Circle atesta."
            ),
        )

    async def track_bridge(
        self, tx_hash: str, *, origin_chain: str, destination_chain: str
    ) -> BridgeProgress:
        """Lo que Circle dice de la quema: indexada, esperando o atestada. Sólo lee.

        Nunca devuelve `DONE`: que Circle atese no prueba que el mint en destino se
        haya hecho, y eso sólo lo sabe la recepción.
        """
        status = await fetch_message_status(
            self._source, source_domain=CCTP_DOMAINS[origin_chain], tx_hash=tx_hash
        )
        if status is None:
            return BridgeProgress(
                state=BridgeTrackState.UNKNOWN,
                provider_status="",
                substatus="",
                message="Circle aún no ha indexado la quema.",
            )
        if status == COMPLETE:
            return BridgeProgress(
                state=BridgeTrackState.PENDING,
                provider_status=status,
                substatus="atestado",
                message=(
                    f"Circle ha atestado la quema. Falta la recepción en "
                    f"«{destination_chain}»."
                ),
            )
        return BridgeProgress(
            state=BridgeTrackState.PENDING,
            provider_status=status,
            substatus="esperando confirmaciones",
            message=_mensaje_espera(origin_chain),
        )

    async def prepare_receive(
        self, tx_hash: str, *, origin_chain: str, destination_chain: str
    ) -> UnsignedTransaction | None:
        """El `receiveMessage` de destino, o `None` si Circle aún no ha atestado.

        No firma ni emite. Sin atestación no hay nada que recibir, y eso no es un
        fallo: quien llama vuelve a preguntar más tarde.
        """
        attested = await fetch_attestation(
            self._source, source_domain=CCTP_DOMAINS[origin_chain], tx_hash=tx_hash
        )
        if attested is None:
            return None
        spec = chain(destination_chain)
        return UnsignedTransaction(
            chain_id=spec.require_eip155_id(),
            to_address=MESSAGE_TRANSMITTER,
            calldata=encode_receive_message(attested),
            value=TokenAmount(0, spec.native_decimals, spec.native_symbol),
            description=(
                f"Circle CCTP: recibir en {destination_chain} la quema de "
                f"{origin_chain} (transacción {tx_hash}). Mintea el USDC al "
                f"destinatario que fijó la quema."
            ),
        )

    def expected_destination(self, chain_key: str) -> str | None:
        """El `TokenMessengerV2` desde esa red de origen, o `None` si no es CCTP."""
        return TOKEN_MESSENGER if chain_key in CCTP_DOMAINS else None

    # ------------------------------------------------------------ interno #
    async def _standard_fee_bps(self, request: BridgeRequest) -> Decimal | None:
        """La comisión estándar publicada, en puntos básicos, o `None` si no hay."""
        raw = await self._source.get_json(
            f"{FEES_URL}/{CCTP_DOMAINS[request.origin.chain]}/"
            f"{CCTP_DOMAINS[request.destination.chain]}",
            absent_statuses=frozenset({404}),
        )
        return _standard_fee(raw)

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
            "circle.quote_moved",
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


def encode_deposit_for_burn(
    *,
    amount: int,
    destination_domain: int,
    recipient: str,
    burn_token: str,
    max_fee: int,
) -> str:
    """El calldata de `depositForBurn` con finalidad estándar, en hexadecimal."""
    mint_recipient = b"\x00" * 12 + bytes.fromhex(recipient.strip()[2:])
    args = encode(
        list(DEPOSIT_FOR_BURN_TYPES),
        [
            amount,
            destination_domain,
            mint_recipient,
            burn_token,
            ANY_CALLER,
            max_fee,
            STANDARD_FINALITY,
        ],
    )
    return "0x" + (bytes.fromhex(SELECTOR_DEPOSIT_FOR_BURN[2:]) + args).hex()


def _is_usdc_route(request: BridgeRequest) -> bool:
    return _es_usdc_nativo(request.origin) and _es_usdc_nativo(request.destination)


def _es_usdc_nativo(token: Token) -> bool:
    return token.address is not None and NATIVE_USDC.get(token.chain) == token.address.lower()


def _standard_fee(payload: Any) -> Decimal | None:
    """La comisión de la finalidad estándar, en puntos básicos, o `None`."""
    if not isinstance(payload, list):
        return None
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if entry.get("finalityThreshold") != STANDARD_FINALITY:
            continue
        raw = entry.get("minimumFee")
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            return None
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            return None
        return value if value >= 0 else None
    return None


def _fee_raw(amount_raw: int, bps: Decimal) -> int:
    """La comisión en unidades base, redondeada hacia arriba para no quedarse corta."""
    return math.ceil(Decimal(amount_raw) * bps / Decimal(10_000))


@dataclass(frozen=True, slots=True)
class CircleProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> CircleEngine:
        return CircleEngine()


PROVIDER: Final = CircleProvider()
