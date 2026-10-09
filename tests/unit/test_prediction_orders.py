"""La aritmética y la firma de una orden de predicción. Sin red, sin fondos.

Lo que se fija aquí es dónde se pierde dinero al firmar una orden, y son tres
sitios:

1. **El redondeo del precio.** Cada mercado publica un salto —0,1 / 0,01 / 0,005
   / 0,0025 / 0,001 / 0,0001— y un precio que no sea múltiplo suyo hace que el
   recinto rechace la orden **entera**. Hay una prueba por cada salto medido,
   porque una fila mal copiada sólo falla en los mercados con ese salto, que es
   el fallo que más tarda en verse.
2. **De qué lado van los importes.** Al comprar se entrega colateral y se reciben
   participaciones; al vender, al revés. Invertirlos produce una orden que el
   recinto acepta y liquida al contrario de lo pedido, así que hay una prueba por
   cada lado que afirma **qué** se entrega y qué se recibe.
3. **Contra qué contrato se firma.** Polymarket tiene un contrato general y otro
   para los mercados que comparten colateral. Firmar contra el que no es produce
   una firma que el correcto rechaza —o, peor, que el incorrecto acepta—.

Y una comprobación que no depende de nadie: la firma se recupera y se afirma que
la dirección es la de la clave. Se hace **desde el cuerpo serializado** que se
enviaría al recinto, no desde el mensaje en memoria, porque es lo único que
prueba que los bytes que viajan son los que se firmaron.

La definición EIP-712 de la orden se transcribe aquí **a mano** en vez de
importarla del módulo. El orden de los campos es parte de la definición —EIP-712
resume la concatenación— y una prueba que usara la misma lista que el camino
seguiría pasando con los dos reordenados a la vez.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak
from structlog.testing import capture_logs

from amigocompora.domain.errors import ExecutionError, InvalidAmountError
from amigocompora.domain.models import (
    MarketOutcome,
    PredictionMarket,
    PredictionOrder,
    PredictionSide,
    SignedPredictionOrder,
    Venue,
    VenueKind,
)
from amigocompora.engines.polymarket.deposit_wallet import (
    derive_beacon_deposit_wallet,
    recover_wrapped_signer,
)
from amigocompora.engines.polymarket.orders import (
    ROUNDING,
    TickRounding,
    build_order,
    exchange_for,
    order_amounts,
    rounded_price,
    rounding_for,
    sign_order,
    typed_data,
    wire_body,
)

AHORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

#: Cuenta 0 de Hardhat. Está publicada en la documentación de Hardhat y Anvil, no
#: tiene fondos en ninguna red real, y la dirección de abajo está **derivada** de
#: la clave, no recordada: `Account.from_key(CLAVE).address`.
CLAVE = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

#: Las direcciones de los contratos, en minúsculas y escritas a mano: son contra
#: lo que se firma, y sacarlas de una constante del producto dejaría la prueba
#: comprobándose a sí misma.
EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
EXCHANGE_NEG_RISK = "0xe2222d279d744050d28e00520010520000310F59"
COLATERAL = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"

#: Los dos resultados de un mercado de sí/no. Son cadenas de dígitos porque el
#: `tokenId` de un ERC-1155 es un `uint256` y en JSON no cabe en un número sin
#: perder cifras —el dominio lo exige así, y de ahí que se escriban entre comillas.
#:
#: El `noqa` de abajo silencia un falso positivo: la regla lee «TOKEN» como si
#: fuera un secreto, y estos no lo son —son el **nombre público** del resultado
#: dentro del contrato, lo que va dentro de la firma y lo que cualquiera puede
#: leer en la cadena—.
TOKEN_SI = "71321045679252212594626385532706912750332728571942532289631379312455583992563"  # noqa: S105
TOKEN_NO = "52114319501245915516055105976744304775366685471852918604325386120244825471694"  # noqa: S105

CONDICION = "0x" + "ab" * 32


# --------------------------------------------------------------------------- #
# La definición publicada, transcrita a mano
# --------------------------------------------------------------------------- #
#: El mensaje EIP-712 que el contrato de intercambio de Polymarket valida.
TIPOS: dict[str, list[dict[str, str]]] = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
        {"name": "timestamp", "type": "uint256"},
        {"name": "metadata", "type": "bytes32"},
        {"name": "builder", "type": "bytes32"},
    ],
}


def _mercado(**overrides: Any) -> PredictionMarket:
    """Un mercado operable de polygon, con lo que se le diga cambiado."""
    values: dict[str, Any] = {
        "market_id": "0xmercado",
        "venue": Venue(
            venue_id="polymarket",
            name="Polymarket",
            kind=VenueKind.PREDICTION_MARKET,
            chain="polygon",
        ),
        "question": "¿Llegará el Bitcoin a 100 000 dólares en octubre?",
        "outcomes": (
            MarketOutcome(label="Sí", price=Decimal("0.62"), token_id=TOKEN_SI),
            MarketOutcome(label="No", price=Decimal("0.38"), token_id=TOKEN_NO),
        ),
        "observed_at": AHORA,
        "condition_id": CONDICION,
        "neg_risk": False,
        "tick_size": Decimal("0.01"),
        "min_order_size": Decimal("5"),
    }
    values.update(overrides)
    return PredictionMarket(**values)


def _orden(
    *,
    side: PredictionSide = PredictionSide.BUY,
    price: str = "0.62",
    size: str = "10",
    market: PredictionMarket | None = None,
) -> PredictionOrder:
    """Una orden construida por el camino de verdad, para firmarla."""
    return build_order(
        market or _mercado(),
        outcome_label="Sí",
        side=side,
        size=Decimal(size),
        price=Decimal(price),
    )


# --------------------------------------------------------------------------- #
# 1. El redondeo
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("salto", "precio", "tamano", "importe"),
    [
        # Medido del cliente oficial: cuántos decimales se conservan en cada
        # campo, por cada salto de precio. Se escriben aquí a mano porque es la
        # tabla que decide si el recinto acepta la orden, y transcribirla del
        # módulo haría que un error de transcripción no se notara.
        ("0.1", 1, 2, 3),
        ("0.01", 2, 2, 4),
        ("0.005", 3, 2, 5),
        ("0.0025", 4, 2, 6),
        ("0.001", 3, 2, 5),
        ("0.0001", 4, 2, 6),
    ],
)
def test_cada_salto_medido_tiene_su_tabla(
    salto: str, precio: int, tamano: int, importe: int
) -> None:
    tabla = rounding_for(Decimal(salto))

    assert (tabla.price, tabla.size, tabla.amount) == (precio, tamano, importe)


def test_la_tabla_tiene_los_seis_saltos_que_existen() -> None:
    """Los seis, no los cuatro del paquete npm.

    El paquete `@polymarket/clob-client` que se instala con npm sólo trae cuatro
    filas; las dos de en medio —0,005 y 0,0025— se midieron en el paquete que la
    web sirve en producción. Faltando, un mercado de medio centavo no se podría
    operar: `rounding_for` lo rechazaría por no estar medido, que es lo correcto
    pero deja fuera mercados que existen y se ven en la lista.
    """
    assert set(ROUNDING) == {"0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"}


def test_el_salto_se_reconoce_aunque_venga_con_ceros_de_mas() -> None:
    """`0.010` y `0.01` son el mismo salto: la fuente los publica como texto."""
    assert rounding_for(Decimal("0.010")) == rounding_for(Decimal("0.01"))
    assert rounding_for(Decimal("0.10")) == rounding_for(Decimal("0.1"))


def test_un_salto_no_medido_no_se_redondea_a_ojo() -> None:
    """Un salto que no está en la tabla no se inventa: se dice que no se sabe.

    La alternativa —redondear el salto a la baja hasta que encaje en alguna fila—
    produciría órdenes que el recinto rechaza, y lo haría sólo en los mercados
    raros, que son justo los que nadie prueba.
    """
    with pytest.raises(ExecutionError, match="no es uno de los medidos"):
        rounding_for(Decimal("0.02"))


# --------------------------------------------------------------------------- #
# 2. Los importes
# --------------------------------------------------------------------------- #
#: Dos decimales de precio, dos de participaciones y cuatro de importe, que es la
#: fila del salto de un centavo. Se construye aquí a mano para que estas pruebas
#: midan la inversión de los lados y no la tabla, que ya tiene las suyas.
TABLA = TickRounding(price=2, size=2, amount=4)


@pytest.mark.parametrize(
    ("salto", "precio", "entregado", "recibido"),
    [
        # Compra: 10 participaciones, y lo que se entrega es precio x
        # participaciones en unidades de 10^-6. Los dos números van escritos a
        # mano en vez de calculados con el mismo `to_token_decimals` que usa el
        # camino, porque una prueba que se calcula con la función que comprueba
        # no comprueba nada.
        ("0.1", "0.6", 6_000_000, 10_000_000),
        ("0.01", "0.62", 6_200_000, 10_000_000),
        ("0.005", "0.625", 6_250_000, 10_000_000),
        ("0.0025", "0.6275", 6_275_000, 10_000_000),
        ("0.001", "0.625", 6_250_000, 10_000_000),
        ("0.0001", "0.6275", 6_275_000, 10_000_000),
    ],
)
def test_comprar_entrega_el_colateral_y_recibe_las_participaciones(
    salto: str, precio: str, entregado: int, recibido: int
) -> None:
    """El que firma una compra paga dólares y recibe participaciones."""
    maker, taker = order_amounts(
        PredictionSide.BUY,
        Decimal("10"),
        Decimal(precio),
        rounding_for(Decimal(salto)),
    )

    assert (maker, taker) == (entregado, recibido)


@pytest.mark.parametrize(
    ("salto", "precio", "entregado", "recibido"),
    [
        # Vender es el mismo intercambio al revés: se entregan participaciones y
        # se recibe el colateral. Es la prueba que impide firmar una venta como
        # una compra, que es una orden que el recinto acepta y liquida al
        # contrario de lo pedido.
        ("0.1", "0.6", 10_000_000, 6_000_000),
        ("0.01", "0.62", 10_000_000, 6_200_000),
        ("0.005", "0.625", 10_000_000, 6_250_000),
        ("0.0025", "0.6275", 10_000_000, 6_275_000),
        ("0.001", "0.625", 10_000_000, 6_250_000),
        ("0.0001", "0.6275", 10_000_000, 6_275_000),
    ],
)
def test_vender_entrega_las_participaciones_y_recibe_el_colateral(
    salto: str, precio: str, entregado: int, recibido: int
) -> None:
    maker, taker = order_amounts(
        PredictionSide.SELL,
        Decimal("10"),
        Decimal(precio),
        rounding_for(Decimal(salto)),
    )

    assert (maker, taker) == (entregado, recibido)


def test_los_dos_lados_son_el_mismo_intercambio_al_reves() -> None:
    """Comprar y vender los mismos números, cada uno por su lado.

    Es la afirmación que no depende de qué números sean: si alguien cambiara el
    orden de los dos importes en un solo camino, esta prueba lo dice sin tener
    que saber cuál de los dos está mal.
    """
    compra = order_amounts(PredictionSide.BUY, Decimal("10"), Decimal("0.62"), TABLA)
    venta = order_amounts(PredictionSide.SELL, Decimal("10"), Decimal("0.62"), TABLA)

    assert compra == (venta[1], venta[0])


def test_las_participaciones_se_recortan_hacia_abajo() -> None:
    """Un tamaño con más decimales de los que el mercado admite se recorta.

    Se recorta y no se redondea al alza porque las participaciones son lo que se
    recibe: pedir más de las que se van a pagar sería pedir dinero de nadie. Y el
    importe se calcula sobre **el tamaño ya recortado**, no sobre el pedido, que
    es lo que hace que el precio siga siendo cierto para lo que de verdad se
    intercambia.
    """
    maker, taker = order_amounts(PredictionSide.BUY, Decimal("10.129"), Decimal("0.62"), TABLA)

    assert taker == 10_120_000, "10,129 recortado a los dos decimales del tamaño"
    assert maker == 6_274_400, "y el importe es 10,12 x 0,62, no 10,129 x 0,62"


def test_el_precio_se_baja_al_salto_y_nunca_se_sube() -> None:
    """El precio es un **límite** para quien firma, y se redondea hacia dentro.

    Bajarlo al comprar nunca hace pagar más de lo dicho; subirlo, en cambio,
    firmaría un precio que el usuario no pidió. Al vender la dirección segura es
    la misma —bajar— por la razón contraria: subirlo le haría pedir más de lo que
    dijo, y con eso la orden quedaría sin cruzarse o cruzada a un precio que no
    autorizó.
    """
    market = _mercado(tick_size=Decimal("0.01"))

    assert rounded_price(Decimal("0.627"), market) == Decimal("0.62")
    assert rounded_price(Decimal("0.6299"), market) == Decimal("0.62")
    assert rounded_price(Decimal("0.62"), market) == Decimal("0.62")


def test_sin_salto_publicado_no_se_puede_redondear_el_precio() -> None:
    """Un mercado del que no consta el salto no es operable, y se dice."""
    with pytest.raises(ExecutionError, match="no publica su salto de precio"):
        rounded_price(Decimal("0.62"), _mercado(tick_size=None))


# --------------------------------------------------------------------------- #
# 3. La construcción
# --------------------------------------------------------------------------- #
def test_la_orden_guarda_el_precio_ya_redondeado_y_el_resultado_elegido() -> None:
    order = build_order(
        _mercado(),
        outcome_label="No",
        side=PredictionSide.BUY,
        size=Decimal("10"),
        price=Decimal("0.387"),
    )

    assert order.outcome_label == "No"
    assert order.price == Decimal("0.38")
    assert order.token_id == TOKEN_NO
    assert order.condition_id == CONDICION
    assert order.cost == Decimal("3.80")


def test_un_resultado_que_no_existe_no_se_inventa() -> None:
    """El error tiene que decir cuáles hay: quien se equivocó de etiqueta lo ve."""
    with pytest.raises(ExecutionError, match="Los que hay son: Sí, No"):
        build_order(
            _mercado(),
            outcome_label="Quizá",
            side=PredictionSide.BUY,
            size=Decimal("10"),
            price=Decimal("0.62"),
        )


@pytest.mark.parametrize(
    ("falta", "texto"),
    [
        ({"condition_id": None}, "identificador del mercado"),
        ({"neg_risk": None}, "comparte colateral"),
        ({"tick_size": None}, "salto mínimo de precio"),
        ({"min_order_size": None}, "mínimo de participaciones"),
    ],
)
def test_un_mercado_al_que_le_falta_un_dato_no_se_opera(falta: dict[str, Any], texto: str) -> None:
    """Se dice **qué** falta, en vez de reventar dentro de la firma.

    Un mercado leído sin estos datos sigue siendo un mercado perfectamente válido
    para mirarlo; lo único que no es es operable. Y el error nombra la pieza que
    falta porque «no se puede» sin decir qué falta manda al usuario a adivinar si
    el problema es su saldo, su configuración o el mercado.
    """
    with pytest.raises(ExecutionError, match=texto):
        build_order(
            _mercado(**falta),
            outcome_label="Sí",
            side=PredictionSide.BUY,
            size=Decimal("10"),
            price=Decimal("0.62"),
        )


def test_un_resultado_sin_identificador_no_se_opera() -> None:
    """Sin el `tokenId` no hay nada que firmar: se nombra el resultado que falta."""
    sin_id = (
        MarketOutcome(label="Sí", price=Decimal("0.62")),
        MarketOutcome(label="No", price=Decimal("0.38"), token_id=TOKEN_NO),
    )

    with pytest.raises(ExecutionError, match="del resultado Sí"):
        build_order(
            _mercado(outcomes=sin_id),
            outcome_label="Sí",
            side=PredictionSide.BUY,
            size=Decimal("10"),
            price=Decimal("0.62"),
        )


def test_por_debajo_del_minimo_no_se_construye() -> None:
    """El recinto rechaza la orden entera, así que no hay forma de comprar menos."""
    with pytest.raises(ExecutionError, match="mínimo de 5 participaciones"):
        build_order(
            _mercado(),
            outcome_label="Sí",
            side=PredictionSide.BUY,
            size=Decimal("4"),
            price=Decimal("0.62"),
        )


def test_un_precio_que_al_bajarlo_se_sale_del_rango_no_se_construye() -> None:
    """Bajar el precio al salto puede dejarlo en cero, y cero no es un precio.

    El recinto no admite precios pegados a los extremos: el rango válido es el
    propio salto. Construir la orden con el precio ya bajado y dejar que la
    entidad lo rechace es lo que evita firmar algo que el recinto no acepta.
    """
    with pytest.raises(InvalidAmountError, match="fuera del rango"):
        build_order(
            _mercado(),
            outcome_label="Sí",
            side=PredictionSide.BUY,
            size=Decimal("10"),
            price=Decimal("0.004"),
        )


# --------------------------------------------------------------------------- #
# 4. El contrato contra el que se firma
# --------------------------------------------------------------------------- #
def test_el_contrato_cambia_segun_el_tipo_de_mercado() -> None:
    assert to_checksum_address(EXCHANGE) == exchange_for(False)
    assert to_checksum_address(EXCHANGE_NEG_RISK) == exchange_for(True)


@pytest.mark.parametrize(
    ("neg_risk", "contrato"),
    [(False, EXCHANGE), (True, EXCHANGE_NEG_RISK)],
)
def test_la_firma_se_hace_contra_el_contrato_del_mercado(neg_risk: bool, contrato: str) -> None:
    """Un mercado de resultados excluyentes se firma contra **otro** contrato.

    Los dos validan la misma estructura de orden, así que firmar contra el que no
    es produce una firma con la forma correcta que el contrato correcto rechaza.
    """
    order = _orden(market=_mercado(neg_risk=neg_risk))

    mensaje = typed_data(order, signer=DIRECCION, salt=1, timestamp=1)

    assert mensaje["domain"]["verifyingContract"] == to_checksum_address(contrato)
    assert mensaje["domain"]["name"] == "Polymarket CTF Exchange"
    assert mensaje["domain"]["version"] == "2"
    assert mensaje["domain"]["chainId"] == 137


def test_la_firma_es_de_una_cuenta_normal_y_sin_etiqueta_ni_builder() -> None:
    """El tipo 0 es una EOA, y `metadata` y `builder` van a cero.

    Se afirma explícitamente porque un builder o una etiqueta distintos de cero
    cambian el mensaje firmado y el recinto los trataría como otra orden.
    """
    mensaje = typed_data(_orden(), signer=DIRECCION, salt=1, timestamp=1)

    assert mensaje["message"]["signatureType"] == 0
    assert mensaje["message"]["maker"] == DIRECCION
    assert mensaje["message"]["metadata"] == "0x" + "00" * 32
    assert mensaje["message"]["builder"] == "0x" + "00" * 32
    assert "taker" not in mensaje["message"]
    assert "nonce" not in mensaje["message"]


def test_el_lado_va_como_numero_dentro_del_mensaje_firmado() -> None:
    """Comprar es 0 y vender es 1, y eso es parte de lo que se firma."""
    compra = typed_data(_orden(side=PredictionSide.BUY), signer=DIRECCION, salt=1, timestamp=1)
    venta = typed_data(_orden(side=PredictionSide.SELL), signer=DIRECCION, salt=1, timestamp=1)

    assert compra["message"]["side"] == 0
    assert venta["message"]["side"] == 1


# --------------------------------------------------------------------------- #
# 5. La firma se comprueba a sí misma
# --------------------------------------------------------------------------- #
def _mensaje_desde_el_cuerpo(cuerpo: dict[str, Any], contrato: str) -> dict[str, Any]:
    """Reconstruye el mensaje EIP-712 a partir de lo que se enviaría.

    Se usan los tipos transcritos a mano de arriba, no los del módulo: si la
    prueba usara la misma lista que el camino, un campo reordenado en los dos
    sitios a la vez seguiría pasando, y el orden de los campos **es** parte de la
    definición.
    """
    return {
        "types": TIPOS,
        "primaryType": "Order",
        "domain": {
            "name": "Polymarket CTF Exchange",
            "version": "2",
            "chainId": 137,
            "verifyingContract": to_checksum_address(contrato),
        },
        "message": {
            "salt": cuerpo["salt"],
            "maker": cuerpo["maker"],
            "signer": cuerpo["signer"],
            "tokenId": int(cuerpo["tokenId"]),
            "makerAmount": int(cuerpo["makerAmount"]),
            "takerAmount": int(cuerpo["takerAmount"]),
            "side": 0 if cuerpo["side"] == "BUY" else 1,
            "signatureType": cuerpo["signatureType"],
            "timestamp": int(cuerpo["timestamp"]),
            "metadata": cuerpo["metadata"],
            "builder": cuerpo["builder"],
        },
    }


@pytest.mark.parametrize("side", [PredictionSide.BUY, PredictionSide.SELL])
def test_la_firma_se_recupera_desde_el_cuerpo_que_se_enviaria(side: PredictionSide) -> None:
    """La comprobación que no depende de la red, ni de fondos, ni del recinto.

    Se firma, se coge **el cuerpo serializado** —los bytes exactos que viajarían—
    y se recupera la dirección desde la firma. Tiene que ser la de la clave. Es lo
    que detecta el fallo más caro y más silencioso de este módulo: una firma
    construida sobre un mensaje distinto del que se cree.
    """
    firmada = sign_order(_orden(side=side), private_key=CLAVE, salt=7)
    cuerpo = json.loads(firmada.payload)["order"]

    recuperada = Account.recover_message(
        encode_typed_data(full_message=_mensaje_desde_el_cuerpo(cuerpo, EXCHANGE)),
        signature=cuerpo["signature"],
    )

    assert recuperada == DIRECCION
    assert firmada.signer == DIRECCION
    assert firmada.signature.startswith("0x")


def test_el_cuerpo_lleva_los_importes_que_se_firmaron() -> None:
    """Lo que se envía y lo que se firma son los mismos números.

    Se leen del cuerpo serializado y se comparan con lo que cuesta la orden, que
    es el dato del usuario: 10 participaciones a 0,62 son 6,20 dólares, y ese es
    el importe que va dentro de la firma.
    """
    firmada = sign_order(_orden(), private_key=CLAVE, salt=7)
    cuerpo = json.loads(firmada.payload)["order"]

    assert cuerpo["side"] == "BUY"
    assert cuerpo["makerAmount"] == str(6_200_000)
    assert cuerpo["takerAmount"] == str(10_000_000)
    assert cuerpo["tokenId"] == TOKEN_SI
    assert cuerpo["signatureType"] == 0
    assert cuerpo["salt"] == 7


def test_una_venta_se_firma_con_el_lado_en_palabras() -> None:
    """El vocabulario del recinto, no el nuestro: `BUY`/`SELL` en mayúsculas."""
    firmada = sign_order(_orden(side=PredictionSide.SELL), private_key=CLAVE, salt=7)
    cuerpo = json.loads(firmada.payload)["order"]

    assert cuerpo["side"] == "SELL"
    assert cuerpo["makerAmount"] == str(10_000_000)
    assert cuerpo["takerAmount"] == str(6_200_000)


def test_dos_ordenes_iguales_no_producen_la_misma_firma() -> None:
    """La sal existe para que dos órdenes idénticas no colisionen en el recinto.

    Sin ella, publicar dos veces «10 participaciones a 0,62» produciría el mismo
    mensaje y la misma firma, y el recinto las leería como la misma orden. Se
    deriva del azar del sistema y no del reloj, porque dos órdenes emitidas en el
    mismo segundo también tienen que ser distintas.
    """
    primera = sign_order(_orden(), private_key=CLAVE)
    segunda = sign_order(_orden(), private_key=CLAVE)

    assert primera.payload != segunda.payload
    assert primera.digest != segunda.digest
    assert primera.signature != segunda.signature


def test_el_cuerpo_con_la_credencial_no_toca_la_firma() -> None:
    """Rellenar el `owner` no invalida nada, y se comprueba en vez de suponerlo.

    La credencial de nivel 2 no existe cuando se firma —se deriva justo antes de
    publicar— así que el cuerpo se reescribe después. Se reescribe **fuera** del
    mensaje firmado: la firma EIP-712 es sobre la orden, y `owner` es un campo de
    enrutado. Si algún día dejara de serlo, esto rompería en silencio, que es la
    clase de detalle que conviene tener escrito.
    """
    firmada = sign_order(_orden(), private_key=CLAVE, salt=7)

    reescrito = json.loads(wire_body(firmada, owner="0xcredencial"))
    original = json.loads(firmada.payload)

    assert reescrito["owner"] == "0xcredencial"
    assert original["owner"] == ""
    assert reescrito["order"] == original["order"]
    # Y sigue verificando contra la misma dirección.
    recuperada = Account.recover_message(
        encode_typed_data(full_message=_mensaje_desde_el_cuerpo(reescrito["order"], EXCHANGE)),
        signature=reescrito["order"]["signature"],
    )
    assert recuperada == DIRECCION


# --------------------------------------------------------------------------- #
# 6. La clave no se filtra
# --------------------------------------------------------------------------- #
def test_la_clave_no_aparece_en_la_orden_ni_en_el_log() -> None:
    """Se busca la cadena literal, no se confía en que nadie la escriba.

    La clave entra por parámetro, se usa para firmar y sale de ámbito sola: lo
    único que sobrevive es `signer`, que es la dirección pública. Y el log anota
    la dirección —que es lo que el usuario necesita ver— y no la clave.
    """
    with capture_logs() as registros:
        firmada = sign_order(_orden(), private_key=CLAVE, salt=7)

    assert CLAVE not in firmada.payload
    assert CLAVE not in repr(firmada)
    volcado = json.dumps(registros, default=str)
    assert CLAVE not in volcado
    assert DIRECCION in volcado


def test_la_orden_firmada_no_se_puede_construir_sin_firma() -> None:
    """El tipo se defiende solo: una firma que no es hexadecimal no es una firma."""
    with pytest.raises(InvalidAmountError, match="no es hexadecimal"):
        SignedPredictionOrder(
            order=_orden(),
            signer=DIRECCION,
            signature="no-hexadecimal",
            digest="0x" + "ab" * 32,
            payload="{}",
        )


# --------------------------------------------------------------------------- #
# 7. Cuenta con deposit wallet (tipo de firma 3)
# --------------------------------------------------------------------------- #
WALLET = derive_beacon_deposit_wallet(DIRECCION)
MARCA_MS = 1_700_000_000_000


def _firma_de_wallet() -> SignedPredictionOrder:
    return sign_order(_orden(), private_key=CLAVE, salt=7, timestamp=MARCA_MS, wallet=WALLET)


def _mensaje_desde_cuerpo_de_wallet(cuerpo: dict[str, Any]) -> dict[str, Any]:
    return {
        "salt": cuerpo["salt"],
        "maker": cuerpo["maker"],
        "signer": cuerpo["signer"],
        "tokenId": int(cuerpo["tokenId"]),
        "makerAmount": int(cuerpo["makerAmount"]),
        "takerAmount": int(cuerpo["takerAmount"]),
        "side": 0 if cuerpo["side"] == "BUY" else 1,
        "signatureType": cuerpo["signatureType"],
        "timestamp": int(cuerpo["timestamp"]),
        "metadata": cuerpo["metadata"],
        "builder": cuerpo["builder"],
    }


def test_la_orden_de_wallet_va_con_tipo_3_y_la_wallet_como_maker_y_signer() -> None:
    """La wallet es la que posee el dinero y la que el recinto valida.

    Con tipo 3 la EOA no aparece en el cuerpo: sale de la clave, pero no es
    `maker` ni `signer`. Si aparecieran, el recinto rechazaría la orden con el
    error de «maker address not allowed».
    """
    cuerpo = json.loads(_firma_de_wallet().payload)["order"]

    assert cuerpo["signatureType"] == 3
    assert cuerpo["maker"] == WALLET
    assert cuerpo["signer"] == WALLET
    assert _firma_de_wallet().signer == WALLET


def test_la_firma_envuelta_recupera_la_eoa_de_la_clave() -> None:
    """La EOA firma por dentro; la comprobación recupera esa EOA desde el cuerpo."""
    cuerpo = json.loads(_firma_de_wallet().payload)["order"]

    recuperada = recover_wrapped_signer(
        _mensaje_desde_cuerpo_de_wallet(cuerpo),
        cuerpo["signature"],
        chain_id=137,
        exchange=EXCHANGE,
        wallet=WALLET,
    )

    assert recuperada == DIRECCION


def test_la_firma_no_verifica_contra_otra_wallet() -> None:
    """La firma va atada a la wallet: con otra dirección la EOA ya no se recupera."""
    cuerpo = json.loads(_firma_de_wallet().payload)["order"]
    otra = derive_beacon_deposit_wallet("0x" + "11" * 20)

    recuperada = recover_wrapped_signer(
        _mensaje_desde_cuerpo_de_wallet(cuerpo),
        cuerpo["signature"],
        chain_id=137,
        exchange=EXCHANGE,
        wallet=otra,
    )

    assert recuperada != DIRECCION


def test_el_digest_de_la_orden_de_wallet_es_el_hash_de_la_firma_envuelta() -> None:
    """El digest que se registra es el de la firma que viaja, no el de la interna."""
    firmada = _firma_de_wallet()
    cuerpo = json.loads(firmada.payload)["order"]

    assert firmada.digest == "0x" + keccak(hexstr=cuerpo["signature"]).hex()
