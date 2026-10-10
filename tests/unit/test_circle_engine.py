from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from eth_abi.abi import decode

from amigocompora.domain.models import BridgeRequest, Token
from amigocompora.domain.money import TokenAmount
from amigocompora.engines.catalog import quote_token
from amigocompora.engines.circle.engine import (
    CCTP_DOMAINS,
    NATIVE_USDC,
    SELECTOR_DEPOSIT_FOR_BURN,
    STANDARD_FINALITY,
    TOKEN_MESSENGER,
    CircleEngine,
    _fee_raw,
    _standard_fee,
    encode_deposit_for_burn,
)

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_POLYGON = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
RECIPIENT = "0x" + "ab" * 20


class _FuenteFalsa:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    async def get_json(self, url: str, **_: Any) -> Any:
        return self.payload


def _solicitud(origin: str = "base", destination: str = "polygon") -> BridgeRequest:
    return BridgeRequest(
        origin=Token("USDC", 6, origin, USDC_BASE),
        destination=Token("USDC", 6, destination, USDC_POLYGON),
        amount_in=TokenAmount(1_000_000, 6, "USDC"),
    )


def _motor(payload: Any) -> CircleEngine:
    motor = CircleEngine()
    motor._source = _FuenteFalsa(payload)  # type: ignore[assignment]
    return motor


def test_calldata_se_decodifica_con_los_mismos_campos() -> None:
    calldata = encode_deposit_for_burn(
        amount=1_000_000,
        destination_domain=CCTP_DOMAINS["polygon"],
        recipient=RECIPIENT,
        burn_token=USDC_BASE,
        max_fee=130,
    )

    assert calldata.startswith(SELECTOR_DEPOSIT_FOR_BURN)
    amount, domain, mint, burn, caller, max_fee, finality = decode(
        ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
        bytes.fromhex(calldata[2 + 8 :]),
    )
    assert amount == 1_000_000
    assert domain == 7
    assert mint == b"\x00" * 12 + bytes.fromhex(RECIPIENT[2:])
    assert burn.lower() == USDC_BASE.lower()
    assert caller == b"\x00" * 32
    assert max_fee == 130
    assert finality == STANDARD_FINALITY


def test_cotiza_neto_de_la_comision_publicada() -> None:
    motor = _motor(
        [
            {"finalityThreshold": 1000, "minimumFee": 5},
            {"finalityThreshold": STANDARD_FINALITY, "minimumFee": "1.3"},
        ]
    )
    (quote,) = asyncio.run(motor.quote_bridge(_solicitud()))

    assert quote.fee is not None
    assert quote.fee.raw == 130
    assert quote.amount_out.raw == 999_870
    assert quote.request.destination.chain == "polygon"


def test_sin_comision_publicada_no_hay_ruta() -> None:
    assert asyncio.run(_motor([]).quote_bridge(_solicitud())) == ()


def test_red_fuera_de_la_tabla_cctp_no_se_cotiza() -> None:
    motor = _motor([{"finalityThreshold": STANDARD_FINALITY, "minimumFee": 0}])
    assert asyncio.run(motor.quote_bridge(_solicitud(origin="bsc"))) == ()


def test_destino_esperado_solo_en_redes_cctp() -> None:
    motor = _motor([])
    assert motor.expected_destination("base") == frozenset({TOKEN_MESSENGER})
    assert motor.expected_destination("bsc") is None


def test_comision_se_redondea_hacia_arriba() -> None:
    assert _fee_raw(1, Decimal("1.3")) == 1
    assert _fee_raw(1_000_000, Decimal("0")) == 0


def test_usdc_puenteado_no_se_cotiza() -> None:
    usdc_e = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    solicitud = BridgeRequest(
        origin=Token("USDC", 6, "base", USDC_BASE),
        destination=Token("USDC", 6, "polygon", usdc_e),
        amount_in=TokenAmount(1_000_000, 6, "USDC"),
    )
    motor = _motor([{"finalityThreshold": STANDARD_FINALITY, "minimumFee": 0}])
    assert asyncio.run(motor.quote_bridge(solicitud)) == ()


def test_tabla_de_usdc_nativo_coincide_con_el_catalogo() -> None:
    for chain_key, address in NATIVE_USDC.items():
        assert quote_token(chain_key).address.lower() == address  # type: ignore[union-attr]


def test_comision_ilegible_se_descarta() -> None:
    assert _standard_fee([{"finalityThreshold": STANDARD_FINALITY, "minimumFee": True}]) is None
    assert _standard_fee([{"finalityThreshold": STANDARD_FINALITY, "minimumFee": "-1"}]) is None
    assert _standard_fee({"error": "x"}) is None
