"""Motor de swap de Solana: destino, caducidad y comprobación de precio.

Lo que se fija aquí es la parte del modo ASISTIDO que puede perder dinero si
está mal, y sólo esa: a qué dirección se entrega, hasta cuándo vale lo que se
entrega, y sobre qué precio se construyó.

La comprobación de precio tiene tests propios porque **la API no la hace**:
medido, `POST /swap/v1/swap` acepta un `quoteResponse` con el `outAmount`
inflado diez veces y responde 200. Si esa comprobación se rompe, nada más lo
detecta, y el usuario acaba firmando un swap construido sobre una cifra que
nadie verificó.

Los tests de `plan_swap` van contra el `JsonSource` real con `respx` por debajo,
así que ejercitan de verdad la lista blanca de hosts, la caché, el mapeo de
errores por código de estado y el `post_json` sin caché. Está comprobado que
`respx` atraviesa `AllowlistTransport`.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from amigocompora.app.confirmation import ConfirmationGateway, RecordingPrompt
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.prepare_swap import PrepareSwap
from amigocompora.domain.addresses import (
    SOLANA_SYSTEM_PROGRAM,
    is_solana_address,
    require_solana_address,
)
from amigocompora.domain.chains import chain
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
    InvalidAmountError,
    ModeNotPermittedError,
    NoQuotesError,
    QuoteMovedError,
    SourceResponseError,
    UnsupportedOperationError,
)
from amigocompora.domain.models import (
    Measurement,
    PlannedTransaction,
    Quote,
    Token,
    TradingPair,
    UnsignedSolanaTransaction,
    UnsignedTransaction,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.engines.jupiter.engine import (
    QUOTE_URL,
    SWAP_URL,
    VENUE,
    JupiterEngine,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

#: Mints reales de Solana. Se usan los de verdad y no cadenas inventadas porque
#: el motor los manda tal cual a la API: con un mint de mentira, un test que
#: pasara no probaría que la petición es la correcta.
WSOL = "So11111111111111111111111111111111111111112"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

_EVM_RECIPIENT = "0x" + "a" * 40

#: Contenido del payload simulado. No tiene por qué ser una transacción real: lo
#: que se comprueba es que se decodifica y se entrega sin tocarlo.
_TX_BYTES = b"\x01" * 64

_VALID_HEIGHT = 431_989_501


# --------------------------------------------------------------------------- #
# Direcciones de Solana
# --------------------------------------------------------------------------- #
def test_acepta_un_pubkey_real() -> None:
    assert is_solana_address(WSOL)
    assert is_solana_address(USDC_SOL)
    assert require_solana_address(WSOL) == WSOL


def test_rechaza_una_direccion_evm() -> None:
    assert not is_solana_address(_EVM_RECIPIENT)


def test_rechaza_lo_que_pasa_el_validador_de_forma() -> None:
    """`"z" * 44` son 44 caracteres del alfabeto base58 y **no** son un pubkey.

    Es el test que justifica que el dominio decodifique en vez de mirar la
    forma: el validador del registro de redes lo acepta, y aceptarlo sería
    entregar fondos a una dirección que no existe.
    """
    candidate = "z" * 44
    assert chain("solana").is_valid_address(candidate)  # la forma pasa
    assert not is_solana_address(candidate)  # pero no decodifica a 32 bytes


def test_rechaza_el_programa_system() -> None:
    """Su pubkey es válido y aun así enviarle fondos los pierde, como el cero."""
    assert is_solana_address(SOLANA_SYSTEM_PROGRAM)
    with pytest.raises(InvalidAmountError, match="programa System"):
        require_solana_address(SOLANA_SYSTEM_PROGRAM)


def test_cuenta_los_ceros_a_la_izquierda() -> None:
    """Cada `1` inicial es un byte cero que la conversión a entero pierde.

    Sin contarlos, esta dirección decodificaría a 29 bytes y se rechazaría una
    dirección perfectamente válida.
    """
    encoded = _b58(b"\x00" * 3 + bytes(range(29)))
    assert encoded.startswith("111")
    assert is_solana_address(encoded)


# --------------------------------------------------------------------------- #
# La transacción sin firmar
# --------------------------------------------------------------------------- #
def test_describe_publica_la_caducidad_y_el_pagador() -> None:
    fields = dict(_solana_tx().describe())
    assert fields["Red"] == "Solana"
    assert fields["Pagador de comisión"] == WSOL
    assert fields["Válida hasta la altura"] == str(_VALID_HEIGHT)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"transaction_b64": ""}, "llegó vacía"),
        ({"last_valid_block_height": 0}, "caduca"),
        ({"fee_payer": ""}, "pagador"),
        ({"description": ""}, "descripción legible"),
    ],
)
def test_rechaza_una_transaccion_incompleta(
    overrides: Mapping[str, Any], expected: str
) -> None:
    """Ningún campo ausente se rellena con un valor por defecto.

    A una transacción de Solana a la que le falta la altura de caducidad no le
    falta un adorno: no se sabe si sigue siendo emitible.
    """
    with pytest.raises(InvalidAmountError, match=expected):
        _solana_tx(**overrides)


# --------------------------------------------------------------------------- #
# El caso de uso: a quién se entrega
# --------------------------------------------------------------------------- #
async def test_acepta_un_destino_de_solana_para_un_par_de_solana() -> None:
    prepare, planner = await _prepare(OperationMode.ASSISTED, answer=True)
    tx = await prepare(_solana_quote(), recipient=WSOL)
    assert [recipient for _, recipient in planner.planned] == [WSOL]
    assert isinstance(tx, UnsignedSolanaTransaction)
    assert tx.fee_payer == WSOL


async def test_rechaza_un_destino_evm_para_un_par_de_solana() -> None:
    """Lo que impide que un `0x…` acabe como `userPublicKey` de un swap."""
    prepare, planner = await _prepare(OperationMode.ASSISTED)
    with pytest.raises(InvalidAmountError, match="dirección de destino"):
        await prepare(_solana_quote(), recipient=_EVM_RECIPIENT)
    assert planner.planned == []  # no se llegó a construir nada


async def test_rechaza_un_destino_de_solana_para_un_par_evm() -> None:
    """La regresión que importa: al añadir Solana no se relajó el camino EVM."""
    prepare, planner = await _prepare(OperationMode.ASSISTED)
    with pytest.raises(InvalidAmountError, match="dirección de destino"):
        await prepare(_evm_quote(), recipient=WSOL)
    assert planner.planned == []


async def test_el_dialogo_muestra_el_payload_de_solana() -> None:
    """El usuario confirma lo que ve: el payload viaja dentro de la acción."""
    prepare, _ = await _prepare(OperationMode.ASSISTED)
    await prepare(_solana_quote(), recipient=WSOL)
    action = prepare.gateway.history[-1].action
    assert isinstance(action.transaction, UnsignedSolanaTransaction)
    fields = dict(action.transaction.describe())
    assert fields["Válida hasta la altura"] == str(_VALID_HEIGHT)


async def test_no_construye_con_un_motor_que_no_observo_la_cotizacion() -> None:
    """La cifra que el usuario vio y el payload que firma salen del mismo sitio.

    Construir con otro motor no es un detalle: ese motor volvería a cotizar
    contra **su** fuente y compararía la deriva contra el precio de una fuente
    distinta, con lo que la comprobación dejaría de significar lo que dice. Y el
    usuario acabaría firmando un swap en un venue que no eligió.
    """
    prepare, planner = await _prepare(OperationMode.ASSISTED)

    with pytest.raises(UnsupportedOperationError, match="ya no está activo"):
        await prepare(_solana_quote(engine_id="otro_motor"), recipient=WSOL)

    assert planner.planned == []


async def test_si_el_usuario_dice_no_no_se_devuelve_nada() -> None:
    prepare, _ = await _prepare(OperationMode.ASSISTED, answer=False)
    with pytest.raises(ConfirmationDeniedError):
        await prepare(_solana_quote(), recipient=WSOL)


async def test_en_simulacion_no_se_construye_nada() -> None:
    """`PREPARE_TX` sólo lo concede ASISTIDO, y se descarta antes de gastar red."""
    prepare, planner = await _prepare(OperationMode.SIMULATION)
    with pytest.raises(ModeNotPermittedError):
        await prepare(_solana_quote(), recipient=WSOL)
    assert planner.planned == []


# --------------------------------------------------------------------------- #
# `plan_swap` contra la API simulada
# --------------------------------------------------------------------------- #
@respx.mock
async def test_construye_el_payload_tras_recomprobar_el_precio() -> None:
    swap_route = _mock_api(fresh_out="150000000")

    async with _engine() as engine:
        tx = await engine.plan_swap(_solana_quote(out_raw=150_000_000), recipient=WSOL)

    assert isinstance(tx, UnsignedSolanaTransaction)
    assert tx.last_valid_block_height == _VALID_HEIGHT
    assert tx.fee_payer == WSOL
    assert base64.b64decode(tx.transaction_b64) == _TX_BYTES
    assert swap_route.called, "construyó sin volver a cotizar"


@respx.mock
async def test_aborta_sin_construir_si_el_precio_se_movio() -> None:
    """10 % de deriva sobre lo que el usuario vio: se para y no se construye.

    Lo que se afirma no es sólo que se lance, sino que el endpoint de swap
    **no se llegó a llamar**. Construir y luego avisar dejaría en manos de quien
    confirma detectar la discrepancia comparando dos cifras de un diálogo.
    """
    swap_route = _mock_api(fresh_out="135000000")  # -10 % sobre lo mostrado

    async with _engine() as engine:
        with pytest.raises(QuoteMovedError) as caught:
            await engine.plan_swap(_solana_quote(out_raw=150_000_000), recipient=WSOL)

    assert not swap_route.called, "construyó un payload sobre un precio ya inválido"
    assert "1000 bps" in str(caught.value)


@respx.mock
async def test_tolera_la_deriva_dentro_del_margen() -> None:
    """Exigir identidad convertiría el botón en uno que casi nunca funciona."""
    swap_route = _mock_api(fresh_out="149500000")  # -33 bps

    async with _engine() as engine:
        tx = await engine.plan_swap(_solana_quote(out_raw=150_000_000), recipient=WSOL)

    assert swap_route.called
    # La descripción habla del precio fresco, no del que se mostró.
    assert "149.5" in tx.description


@respx.mock
async def test_sin_ruta_no_sigue_con_la_cotizacion_vieja() -> None:
    """Un 400 `TOKEN_NOT_TRADABLE` es «ya no hay ruta», no «usa la de antes»."""
    respx.get(QUOTE_URL).mock(
        return_value=httpx.Response(400, json={"errorCode": "TOKEN_NOT_TRADABLE"})
    )
    swap_route = respx.post(SWAP_URL).mock(return_value=httpx.Response(200, json={}))

    async with _engine() as engine:
        with pytest.raises(NoQuotesError, match="ya no tiene ruta"):
            await engine.plan_swap(_solana_quote(), recipient=WSOL)

    assert not swap_route.called


@respx.mock
async def test_una_cotizacion_sin_importe_no_se_usa_para_construir() -> None:
    payload = _quote_response("150000000")
    payload["outAmount"] = "no es un número"
    respx.get(QUOTE_URL).mock(return_value=httpx.Response(200, json=payload))
    swap_route = respx.post(SWAP_URL).mock(return_value=httpx.Response(200, json={}))

    async with _engine() as engine:
        with pytest.raises(SourceResponseError, match="sin `outAmount` legible"):
            await engine.plan_swap(_solana_quote(), recipient=WSOL)

    assert not swap_route.called


@respx.mock
async def test_rechaza_un_payload_que_no_es_base64() -> None:
    _mock_api(fresh_out="150000000", tx="no soy base64!!")

    async with _engine() as engine:
        with pytest.raises(SourceResponseError, match="no es base64"):
            await engine.plan_swap(_solana_quote(out_raw=150_000_000), recipient=WSOL)


@respx.mock
async def test_rechaza_una_caducidad_ilegible() -> None:
    """Sin altura de caducidad no se sabe si el payload sigue siendo emitible."""
    _mock_api(fresh_out="150000000", height=None)

    async with _engine() as engine:
        with pytest.raises(SourceResponseError, match="caducidad"):
            await engine.plan_swap(_solana_quote(out_raw=150_000_000), recipient=WSOL)


async def test_no_trabaja_fuera_de_su_red() -> None:
    """El motor es de una sola red y lo dice, en vez de devolver nada."""
    async with _engine() as engine:
        assert await engine.quote(_evm_pair(), TokenAmount(1, 18, "WETH")) == ()
        assert await engine.venues("ethereum") == ()
        with pytest.raises(UnsupportedOperationError, match="sólo construye swaps"):
            await engine.plan_swap(_evm_quote(), recipient=_EVM_RECIPIENT)


# --------------------------------------------------------------------------- #
# Utilería
# --------------------------------------------------------------------------- #
def _b58(raw: bytes) -> str:
    """Codifica en base58. Sólo para construir el caso de los ceros iniciales."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(raw, "big")
    text = ""
    while number:
        number, remainder = divmod(number, 58)
        text = alphabet[remainder] + text
    leading = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading + text


def _solana_pair() -> TradingPair:
    return TradingPair(
        base=Token("WSOL", 9, "solana", WSOL),
        quote=Token("USDC", 6, "solana", USDC_SOL),
    )


def _evm_pair() -> TradingPair:
    return TradingPair(
        base=Token("WETH", 18, "ethereum", "0x" + "1" * 40),
        quote=Token("USDC", 6, "ethereum", "0x" + "2" * 40),
    )


def _solana_quote(*, out_raw: int = 150_000_000, engine_id: str | None = None) -> Quote:
    return Quote(
        venue=VENUE,
        engine_id=engine_id or _FAKE_MANIFEST.engine_id,
        pair=_solana_pair(),
        amount_in=TokenAmount(1_000_000_000, 9, "WSOL"),
        amount_out=TokenAmount(out_raw, 6, "USDC"),
        # La fuente no desglosa la comisión: ver el docstring del motor.
        fee_bps=None,
        fee_basis=None,
        price_impact_bps=BasisPoints(2),
        observed_at=NOW,
        impact_basis=Measurement.REPORTED,
        source_note="ruta simulada",
    )


def _evm_quote() -> Quote:
    return Quote(
        venue=Venue(venue_id="x@ethereum", name="X", kind=VenueKind.DEX, chain="ethereum"),
        engine_id=_FAKE_MANIFEST.engine_id,
        pair=_evm_pair(),
        amount_in=TokenAmount(1, 18, "WETH"),
        amount_out=TokenAmount(3_000_000, 6, "USDC"),
        fee_bps=BasisPoints(30),
        fee_basis=Measurement.REPORTED,
        price_impact_bps=BasisPoints(2),
        observed_at=NOW,
    )


def _solana_tx(**overrides: Any) -> UnsignedSolanaTransaction:
    fields: dict[str, Any] = {
        "transaction_b64": base64.b64encode(_TX_BYTES).decode(),
        "last_valid_block_height": _VALID_HEIGHT,
        "fee_payer": WSOL,
        "description": "Swap de prueba",
    }
    fields.update(overrides)
    return UnsignedSolanaTransaction(**fields)


def _quote_response(out_raw: str) -> dict[str, Any]:
    return {
        "inputMint": WSOL,
        "inAmount": "1000000000",
        "outputMint": USDC_SOL,
        "outAmount": out_raw,
        "otherAmountThreshold": out_raw,
        "swapMode": "ExactIn",
        "slippageBps": 50,
        "priceImpactPct": "0.0002",
        "routePlan": [{"swapInfo": {"label": "Whirlpool", "ammKey": WSOL}}],
        "swapUsdValue": "150.00",
    }


def _mock_api(
    *,
    fresh_out: str,
    tx: str | None = None,
    height: int | None = _VALID_HEIGHT,
) -> Any:
    """Simula los dos endpoints y devuelve la ruta del POST, para comprobarla."""
    respx.get(QUOTE_URL).mock(return_value=httpx.Response(200, json=_quote_response(fresh_out)))
    body: dict[str, Any] = {
        "swapTransaction": tx if tx is not None else base64.b64encode(_TX_BYTES).decode(),
    }
    if height is not None:
        body["lastValidBlockHeight"] = height
    return respx.post(SWAP_URL).mock(return_value=httpx.Response(200, json=body))


class _FakePlanner:
    """Motor DEX de sólo lectura que además construye: lo mínimo del contrato."""

    def __init__(self) -> None:
        self.planned: list[tuple[Quote, str]] = []

    @property
    def manifest(self) -> EngineManifest:
        return _FAKE_MANIFEST

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        return ()

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        return ()

    async def plan_swap(self, quote: Quote, *, recipient: str) -> PlannedTransaction:
        self.planned.append((quote, recipient))
        # Devuelve la forma que corresponde a su red, igual que el motor real: es
        # lo que la UI tiene que saber pintar sin preguntar de qué tipo es.
        if quote.pair.chain == "solana":
            return _solana_tx(fee_payer=recipient)
        return UnsignedTransaction(
            chain_id=1,
            to_address=recipient,
            calldata="0xdeadbeef",
            value=TokenAmount(0, 18, "ETH"),
            description="Swap de prueba en EVM",
        )


_FAKE_MANIFEST = EngineManifest(
    engine_id="fake_dex",
    name="Motor de prueba",
    version="0.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary="Motor de prueba para los casos de uso.",
    capabilities=frozenset({Capability.READ_CHAIN, Capability.PREPARE_TX}),
    # El doble construye en las dos redes, así que las declara: es lo que el
    # registro usa para encontrarle un constructor a cada cotización.
    swap_chains=frozenset({"solana", "ethereum"}),
    required_config=(),
    allowed_hosts=(),
)


@dataclass(frozen=True, slots=True)
class _Provider:
    engine: _FakePlanner
    manifest: EngineManifest = _FAKE_MANIFEST

    def create(self, config: Mapping[str, str]) -> _FakePlanner:
        return self.engine


async def _prepare(
    mode: OperationMode,
    *,
    answer: bool = True,
) -> tuple[PrepareSwap, _FakePlanner]:
    guard = ModeGuard(mode)
    gateway = ConfirmationGateway(guard, prompt=RecordingPrompt(answer=answer))
    registry = EngineRegistry(guard)
    planner = _FakePlanner()
    registry.register(_Provider(planner))
    await registry.activate("fake_dex")
    return PrepareSwap(registry=registry, gateway=gateway), planner


@asynccontextmanager
async def _engine() -> AsyncIterator[JupiterEngine]:
    """Motor abierto y cerrado aunque el test falle.

    Sin intervalo entre peticiones: el ritmo real defiende de la cuota de la
    API, y en un test sólo añadiría un segundo de espera por petición.
    """
    engine = JupiterEngine(min_interval_seconds=0.0)
    await engine.aopen()
    try:
        yield engine
    finally:
        await engine.aclose()
