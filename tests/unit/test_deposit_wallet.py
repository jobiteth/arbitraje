"""La dirección de la deposit wallet: contra el cálculo oficial, sin red."""

from __future__ import annotations

from amigocompora.engines.polymarket.deposit_wallet import (
    BEACON,
    FACTORY,
    derive_beacon_deposit_wallet,
)

#: Vector producido por `deriveBeaconDepositWallet` del paquete
#: `@polymarket/builder-relayer-client` 0.0.10 con este mismo dueño, factoría y beacon.
OWNER = "0x1111111111111111111111111111111111111111"
ESPERADA = "0x574548bC296A44a39a7828343FC262244f37a7e5"


def test_coincide_con_el_calculo_oficial() -> None:
    assert derive_beacon_deposit_wallet(OWNER) == ESPERADA


def test_factoria_y_beacon_son_los_publicados() -> None:
    assert FACTORY == "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
    assert BEACON == "0x7A18EDfe055488A3128f01F563e5B479D92ffc3a"


def test_la_direccion_depende_del_dueno() -> None:
    otra = "0x2222222222222222222222222222222222222222"
    assert derive_beacon_deposit_wallet(otra) != ESPERADA


#: Clave de prueba sin fondos. Sólo para reproducir la firma de referencia.
CLAVE_PRUEBA = "0x" + "11" * 32
EOA_PRUEBA = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
WALLET_PRUEBA = "0x8154deDBDAAA46c07453E47b5b6C0Ff60B8542b9"
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
FIRMA_REFERENCIA = (
    "0xc49f43cef310749f1af6e068b511f048477dea7bd84096ee7e792900ee274818700b88e4e5e1c5"
    "cf85d6b4a37037434a84b23532e9a9c641ff302afbe4a2ed5f1c3264e159346253e26a64e00b6903"
    "2db0e7d32f94628de3e6eecb50304d7af3d2d2d4befd6b5151ab844b625561755ff19af45fa2ddac"
    "7b29e797da9a2ef755a34f726465722875696e743235362073616c742c61646472657373206d616b"
    "65722c61646472657373207369676e65722c75696e7432353620746f6b656e49642c75696e743235"
    "36206d616b6572416d6f756e742c75696e743235362074616b6572416d6f756e742c75696e743820"
    "736964652c75696e7438207369676e6174757265547970652c75696e743235362074696d65737461"
    "6d702c62797465733332206d657461646174612c62797465733332206275696c6465722900ba"
)


def test_la_eoa_de_prueba_deriva_la_wallet_de_referencia() -> None:
    from eth_account import Account

    assert Account.from_key(CLAVE_PRUEBA).address == EOA_PRUEBA
    assert derive_beacon_deposit_wallet(EOA_PRUEBA) == WALLET_PRUEBA


def test_firma_envuelta_coincide_con_el_cliente_oficial() -> None:
    from amigocompora.engines.polymarket.deposit_wallet import wrap_order_signature

    mensaje = {
        "salt": 12345,
        "maker": WALLET_PRUEBA,
        "signer": WALLET_PRUEBA,
        "tokenId": 123456789,
        "makerAmount": 5_000_000,
        "takerAmount": 5_000_000,
        "side": 0,
        "signatureType": 3,
        "timestamp": 1_700_000_000_000,
        "metadata": "0x" + "00" * 32,
        "builder": "0x" + "00" * 32,
    }
    firma = wrap_order_signature(
        mensaje,
        chain_id=137,
        exchange=EXCHANGE_V2,
        wallet=WALLET_PRUEBA,
        private_key=CLAVE_PRUEBA,
    )
    assert firma == FIRMA_REFERENCIA
