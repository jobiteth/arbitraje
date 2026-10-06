"""Firma EIP-1559 contra vectores publicados, y la clave fuera de todo rastro.

El valor de estas pruebas está en que **no** comprueban que el código haga lo que
el código hace. La dirección esperada y la forma del hash son hechos publicados
—el par clave/dirección es el vector de ejemplo que usan las implementaciones de
referencia— así que si la firma estuviera mal, estas pruebas lo dicen. Una
prueba que se limita a llamar a la función y a comparar contra su propia salida
demostraría que la función es determinista y nada más.
"""

from __future__ import annotations

import pytest
from eth_utils.crypto import keccak

from amigocompora.domain.errors import (
    AmigocomporaError,
    InvalidAmountError,
    KeyCustodyError,
)
from amigocompora.domain.models import SignedEvmTransaction, UnsignedTransaction
from amigocompora.domain.money import TokenAmount
from amigocompora.infra.evm.signer import address_from_key, sign_eip1559

#: Vector **público** de desarrollo: la clave que aparece en la documentación de
#: ethers.js y en las pruebas de ethereumjs, junto a la dirección que le
#: corresponde. No tiene fondos y no es un secreto; está aquí precisamente para
#: poder comprobar la derivación contra un valor de fuera de este repositorio.
DEV_PRIVATE_KEY = "0x4646464646464646464646464646464646464646464646464646464646464646"
DEV_ADDRESS = "0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F"

_ROUTER = "0x3535353535353535353535353535353535353535"


def _unsigned(*, calldata: str = "0xabcdef", value: int = 0) -> UnsignedTransaction:
    return UnsignedTransaction(
        chain_id=8453,
        to_address=_ROUTER,
        calldata=calldata,
        value=TokenAmount(raw=value, decimals=18, symbol="ETH"),
        description="Prueba de firma",
        gas_limit=200_000,
    )


def _sign(**overrides: int) -> SignedEvmTransaction:
    fees = {
        "nonce": 7,
        "gas_limit": 200_000,
        "max_fee_per_gas": 2_000_000_000,
        "max_priority_fee_per_gas": 100_000_000,
    }
    fees.update(overrides)
    return sign_eip1559(_unsigned(), private_key=DEV_PRIVATE_KEY, **fees)


# --------------------------------------------------------------------------- #
# Derivación de la dirección
# --------------------------------------------------------------------------- #
def test_address_matches_published_vector() -> None:
    """La dirección derivada es la publicada, con su checksum EIP-55.

    Que el valor esperado lleve mayúsculas mezcladas no es cosmético: si la
    implementación devolviera la dirección en minúsculas, esta comparación
    fallaría, y una dirección sin checksum es una donde un carácter cambiado
    pasa desapercibido.
    """
    assert address_from_key(DEV_PRIVATE_KEY) == DEV_ADDRESS


def test_key_without_prefix_is_accepted() -> None:
    """Con y sin `0x` es la misma clave: el prefijo es una convención de escritura."""
    assert address_from_key(DEV_PRIVATE_KEY[2:]) == DEV_ADDRESS


def test_whitespace_around_the_key_is_ignored() -> None:
    """Pegar una clave desde un gestor de contraseñas suele arrastrar espacios."""
    assert address_from_key(f"  {DEV_PRIVATE_KEY}\n") == DEV_ADDRESS


# --------------------------------------------------------------------------- #
# La clave no se repite ni cuando está mal
# --------------------------------------------------------------------------- #
def test_short_key_is_rejected_without_echoing_it() -> None:
    truncated = DEV_PRIVATE_KEY[:40]
    with pytest.raises(KeyCustodyError) as caught:
        address_from_key(truncated)
    assert truncated not in str(caught.value)
    assert DEV_PRIVATE_KEY not in str(caught.value)


def test_non_hex_key_is_rejected_without_echoing_it() -> None:
    """El caso que más fácilmente filtra: un texto que no es una clave.

    Quien pega por error una frase semilla o una API key en el campo de la clave
    privada recibe un error. Ese error es justo el sitio donde el valor acabaría
    en un log si se propagara el mensaje de la librería, así que se comprueba
    que no aparece.
    """
    pasted = "no-soy-una-clave-privada-sino-algo-que-no-deberia-loguearse"
    with pytest.raises(KeyCustodyError) as caught:
        address_from_key(pasted)
    assert pasted not in str(caught.value)


def test_out_of_range_key_is_rejected() -> None:
    """Sesenta y cuatro dígitos que no son un escalar válido tampoco valen."""
    with pytest.raises(KeyCustodyError):
        address_from_key("0x" + "f" * 64)


# --------------------------------------------------------------------------- #
# La firma
# --------------------------------------------------------------------------- #
def test_signature_is_a_type_2_transaction() -> None:
    assert _sign().raw_hex.startswith("0x02")


def test_tx_hash_is_the_keccak_of_the_signed_bytes() -> None:
    """El hash que se le muestra al usuario es el que la red va a calcular.

    Se recalcula aquí a mano en vez de confiar en el campo: el hash es lo que el
    usuario pega en un explorador, y un hash que no correspondiera a los bytes
    emitidos mandaría a comprobar una transacción que no existe.
    """
    signed = _sign()
    assert signed.tx_hash == "0x" + keccak(bytes.fromhex(signed.raw_hex[2:])).hex()


def test_signed_bytes_recover_to_the_signing_wallet() -> None:
    """Los bytes firmados se recuperan como la cartera que firma.

    Es la comprobación que ata la firma a la dirección: si la recuperación no
    diera `from_address`, el usuario estaría viendo una cartera y emitiendo desde
    otra.
    """
    from eth_account import Account

    signed = _sign()
    assert Account.recover_transaction(signed.raw_hex) == DEV_ADDRESS
    assert signed.from_address == DEV_ADDRESS


def test_signing_is_deterministic() -> None:
    """La misma entrada da exactamente los mismos bytes.

    Importa más de lo que parece: si cada firma usara un `k` distinto y
    predecible, dos firmas de la misma clave bastarían para reconstruirla. Que
    dos ejecuciones coincidan es la señal de que se está usando el nonce
    determinista de RFC 6979.
    """
    first, second = _sign(), _sign()
    assert first.raw_hex == second.raw_hex
    assert first.tx_hash == second.tx_hash


def test_the_network_state_travels_with_the_signature() -> None:
    """Nonce y comisiones quedan fijados en el resultado, no se releen."""
    signed = _sign(nonce=42)
    assert signed.nonce == 42
    assert signed.max_fee_per_gas == 2_000_000_000
    assert signed.max_priority_fee_per_gas == 100_000_000
    assert signed.chain_id == 8453


def test_empty_calldata_serializes_as_0x() -> None:
    """Una transferencia nativa no lleva datos, y se firma igual."""
    signed = sign_eip1559(
        _unsigned(calldata=""),
        private_key=DEV_PRIVATE_KEY,
        nonce=0,
        gas_limit=21_000,
        max_fee_per_gas=1_000_000_000,
        max_priority_fee_per_gas=1_000_000,
    )
    assert signed.calldata == ""


# --------------------------------------------------------------------------- #
# Coherencia de comisiones
# --------------------------------------------------------------------------- #
def test_fee_below_the_tip_is_rejected_before_broadcasting() -> None:
    """Una comisión máxima por debajo de la propina nunca sería aceptable.

    Se corta en el dominio, no en la firma: si esto llegara a firmarse, la
    transacción existiría con un hash y ningún nodo la aceptaría.
    """
    with pytest.raises(InvalidAmountError):
        _sign(max_fee_per_gas=1_000, max_priority_fee_per_gas=2_000)


def test_zero_gas_limit_is_rejected() -> None:
    with pytest.raises(InvalidAmountError):
        _sign(gas_limit=0)


# --------------------------------------------------------------------------- #
# La clave no deja rastro en lo que se emite
# --------------------------------------------------------------------------- #
def test_the_signed_transaction_carries_no_trace_of_the_key() -> None:
    """Ni en el `repr()`, ni en los campos, ni en lo que se le muestra al usuario.

    Es la prueba que respalda la promesa del producto. Todo lo que sale de una
    ejecución —el objeto, su descripción para el diálogo, los bytes que se
    emiten— se recorre buscando la clave, en sus dos formas (con y sin `0x`),
    porque una fuga por una sola de esas vías basta.
    """
    signed = _sign()
    haystack = " ".join(
        [
            repr(signed),
            str(signed.describe()),
            signed.raw_hex,
            signed.tx_hash,
            signed.description,
        ]
    )
    for form in (DEV_PRIVATE_KEY, DEV_PRIVATE_KEY[2:]):
        assert form not in haystack


def test_signing_failure_does_not_echo_the_key() -> None:
    """El error de firma describe el problema sin citar la clave.

    Se comprueba contra la raíz de la jerarquía propia y no contra una excepción
    concreta: lo que importa es que un dato malo produzca **un error del
    producto**, con un mensaje que nombre el campo. Antes de validar los datos
    antes de firmar, esto salía como un `ObjectSerializationError` de `rlp`
    hablando de sedes y campos — inútil para quien lo lee y ajeno a esta
    jerarquía.
    """
    with pytest.raises(AmigocomporaError) as caught:
        sign_eip1559(
            _unsigned(),
            private_key=DEV_PRIVATE_KEY,
            nonce=-1,
            gas_limit=200_000,
            max_fee_per_gas=2_000_000_000,
            max_priority_fee_per_gas=100_000_000,
        )
    assert DEV_PRIVATE_KEY not in str(caught.value)


def test_a_bad_network_parameter_names_the_field() -> None:
    """El mensaje dice qué campo falló, no qué clase de excepción saltó."""
    with pytest.raises(InvalidAmountError) as caught:
        _sign(nonce=-1)
    assert "nonce" in str(caught.value)
