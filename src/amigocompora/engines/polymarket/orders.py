"""Construcción y firma de órdenes de Polymarket. Sin red.

Este fichero es el que convierte «quiero cinco participaciones de este resultado
como mucho a este precio» en el mensaje EIP-712 exacto que el contrato de
intercambio va a validar. Todo lo de aquí es **puro**: entra un mercado y unos
números, sale una firma. Se puede probar entero sin tocar la red, y por eso se
puede probar entero —que es lo que hace que se pueda confiar en él—.

### Lo que decide si la orden vale o se rechaza

Tres cosas, y las tres se midieron contra el cliente oficial de Polymarket
(`py-clob-client` 0.34.6 y `py-order-utils` 0.3.2), no se dedujeron:

1. **El redondeo depende del salto de precio del mercado.** Cada mercado publica
   un `tick_size` —0,1 / 0,01 / 0,005 / 0,0025 / 0,001 / 0,0001— y cada uno tiene
   su tabla. Un precio que no sea múltiplo del salto hace que el recinto rechace
   la **orden entera**, no que la ajuste.
2. **Los dos importes van en unidades de 10⁻⁶**, y el lado que se paga depende de
   la operación: al **comprar**, lo que el que firma entrega es el colateral
   —dólares— y lo que recibe son participaciones; al **vender**, al revés. Hay
   una prueba por cada lado, porque invertirlos produce una orden que el recinto
   acepta y liquida al revés de lo que se pidió.
3. **El contrato de destino cambia según el mercado.** Polymarket tiene un
   contrato general y otro para los mercados de resultados excluyentes
   (`negRisk`) que comparten colateral. Firmar contra el que no es produce una
   firma que el contrato correcto rechaza — o, peor, que el incorrecto acepta.

### La comprobación que no depende de nadie

Firmar y volver a recuperar la dirección desde la firma es una comprobación
cerrada: si la dirección recuperada no es la de la clave, la orden no se
publica. No hace falta red ni fondos, y detecta el fallo más caro y más
silencioso que hay aquí —una firma construida sobre un mensaje distinto del que
se cree— antes de que salga nada.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any, Final

import structlog
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.models import (
    PredictionMarket,
    PredictionOrder,
    PredictionSide,
    SignedPredictionOrder,
)
from amigocompora.engines.polymarket.deposit_wallet import (
    recover_wrapped_signer,
    wrap_order_signature,
)
from amigocompora.infra.evm.broadcast import selector, word_address, word_uint

_log = structlog.get_logger(__name__)

#: Cadena en la que Polymarket liquida. Va aquí y no en la configuración porque
#: no es una preferencia: es el hecho de dónde están desplegados los contratos
#: con los que hay que firmar. Un `chain_id` distinto produce una firma que
#: ningún contrato del recinto reconoce.
CHAIN_ID: Final = 137

#: La misma cadena, con la clave que usa el resto de la aplicación. Las dos
#: constantes existen juntas y a propósito: `CHAIN_ID` es lo que va dentro de un
#: mensaje firmado y tiene que ser el número, y esto es lo que se contrasta
#: contra un `venue.chain` y tiene que ser el nombre. Deducir una de la otra en
#: cada punto de uso es donde se cuela el error de comparar un número con un
#: nombre y no verlo nunca.
CHAIN_KEY: Final = "polygon"

#: Contrato de intercambio general de la versión 2 del recinto (documentación
#: oficial de creación de órdenes). El de la versión 1 ya no acepta órdenes.
EXCHANGE: Final = "0xE111180000d2663C0091e4f400237545B87B996B"

#: Contrato para los mercados de resultados excluyentes, versión 2.
NEG_RISK_EXCHANGE: Final = "0xe2222d279d744050d28e00520010520000310F59"

#: El token condicional ERC-1155, donde viven las participaciones.
CONDITIONAL_TOKENS: Final = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

#: Colateral del recinto: pUSD, según la documentación oficial de Polymarket. Medido
#: on-chain en Polygon: `symbol() == "pUSD"`, `decimals() == 6`.
COLLATERAL: Final = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

#: Escala de los importes dentro de la orden. Medido: `to_token_decimals` del
#: cliente oficial multiplica por 10⁶, que son los decimales del colateral.
AMOUNT_DECIMALS: Final = 6

#: Tipo de firma: una cuenta normal (EOA). Es la que no obliga a crear nada en
#: Polymarket —ni cartera proxy ni Safe—, y la única que esta entrega produce.
SIGNATURE_TYPE_EOA: Final = 0
SIGNATURE_TYPE_DEPOSIT_WALLET: Final = 3

#: `taker` en cero significa «orden pública»: cualquiera puede cruzarla. Es lo
#: que se quiere al publicar en el libro.
ZERO_ADDRESS: Final = "0x0000000000000000000000000000000000000000"

DOMAIN_NAME: Final = "Polymarket CTF Exchange"
DOMAIN_VERSION: Final = "2"

#: `metadata` y `builder` van a cero: ni etiqueta de la orden ni código de builder.
ZERO_BYTES32: Final = "0x" + "00" * 32

#: Límite de la sal dentro del JSON: el recinto la lee como entero de JavaScript,
#: y por encima de 2^53 la orden se rechaza como payload inválido.
SALT_LIMIT: Final = 2**53


@dataclass(frozen=True, slots=True)
class TickRounding:
    """Cuántos decimales se conservan en cada campo, para un salto de precio.

    `price` es el salto del precio, `size` el de las participaciones y `amount`
    el del importe en colateral. Que `amount` tenga más decimales que `size` no
    es un descuido: `precio x participaciones` produce un producto con más
    cifras que sus factores, y recortarlo a los decimales del tamaño perdería
    dinero del que firma.
    """

    price: int
    size: int
    amount: int


#: Medida del cliente oficial. Es la tabla que decide el redondeo de cada
#: mercado, y por eso está aquí entera y con una prueba por fila: una fila mal
#: copiada produciría órdenes rechazadas sólo en los mercados con ese salto, que
#: es justo el fallo que más tarda en verse.
#:
#: Las dos filas de en medio —0,005 y 0,0025— **no están en el paquete npm**
#: `@polymarket/clob-client` 5.8.1, que sólo trae cuatro. Se midieron en el
#: paquete que la web sirve en producción, que trae seis. Sin ellas, un mercado
#: con salto de medio centavo no se podría operar: `rounding_for` lo rechazaría
#: por no estar medido, que es el comportamiento correcto pero deja fuera
#: mercados que sí existen.
ROUNDING: Final[dict[str, TickRounding]] = {
    "0.1": TickRounding(price=1, size=2, amount=3),
    "0.01": TickRounding(price=2, size=2, amount=4),
    "0.005": TickRounding(price=3, size=2, amount=5),
    "0.0025": TickRounding(price=4, size=2, amount=6),
    "0.001": TickRounding(price=3, size=2, amount=5),
    "0.0001": TickRounding(price=4, size=2, amount=6),
}


def rounding_for(tick_size: Decimal) -> TickRounding:
    """La tabla de redondeo de un salto, exigiendo que sea uno de los medidos.

    No se interpola ni se redondea el salto a la baja para que encaje: un salto
    que no esté en la tabla es un mercado cuyo comportamiento no se ha medido, y
    firmar contra una suposición es exactamente lo que este módulo existe para
    no hacer.
    """
    clave = f"{tick_size.normalize():f}"
    tabla = ROUNDING.get(clave)
    if tabla is None:
        raise ExecutionError(
            f"el salto de precio {tick_size} no es uno de los medidos "
            f"({', '.join(sorted(ROUNDING))}): no se sabe redondear una orden "
            f"contra él, y firmar a ojo es firmar mal."
        )
    return tabla


def exchange_for(neg_risk: bool) -> str:
    """El contrato contra el que se firma, según el tipo de mercado."""
    return NEG_RISK_EXCHANGE if neg_risk else EXCHANGE


# --------------------------------------------------------------------------- #
# Redondeo
# --------------------------------------------------------------------------- #
def _places(value: Decimal) -> int:
    """Cuántas cifras decimales significativas tiene un número."""
    exponent = value.normalize().as_tuple().exponent
    return max(0, -int(exponent))


def _shift(value: Decimal, digits: int, mode: str) -> Decimal:
    """Mueve la coma `digits` posiciones y la devuelve, redondeando como se pida.

    Se hace con la coma y no multiplicando por potencias en coma flotante porque
    el resultado de esto acaba dentro de un `uint256` firmado: un error de
    representación de una milmillonésima no se corrige luego, se firma.
    """
    return value.quantize(Decimal(1).scaleb(-digits), rounding=mode)


def round_down(value: Decimal, digits: int) -> Decimal:
    return _shift(value, digits, ROUND_DOWN)


def round_up(value: Decimal, digits: int) -> Decimal:
    return _shift(value, digits, ROUND_UP)


def round_normal(value: Decimal, digits: int) -> Decimal:
    return _shift(value, digits, ROUND_HALF_UP)


def to_token_decimals(value: Decimal) -> int:
    """El importe en unidades de 10⁻⁶, como entero. Es lo que va en la firma."""
    escalado = value.scaleb(AMOUNT_DECIMALS)
    return int(escalado.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def order_amounts(
    side: PredictionSide,
    size: Decimal,
    price: Decimal,
    table: TickRounding,
) -> tuple[int, int]:
    """Los dos importes de la orden, en unidades de 10⁻⁶.

    Devuelve `(maker_amount, taker_amount)`: lo que **entrega** quien firma y lo
    que **recibe**. El lado en el que cae el colateral depende de la operación, y
    ese es todo el asunto de esta función:

    - **Comprar**: se entrega colateral y se reciben participaciones. El importe
      del que firma es `precio x participaciones`, y se redondea **hacia arriba**
      en el último paso —nunca por debajo de lo que cuesta—. Se pasa por
      `amount + 4` decimales antes de recortar, igual que el cliente oficial:
      recortar directamente a `amount` desde un producto con más cifras
      redondearía a la baja un importe que el que firma ya se comprometió a
      pagar.
    - **Vender**: al revés. Se entregan participaciones y se recibe colateral.
    """
    raw_price = round_normal(price, table.price)

    if side is PredictionSide.BUY:
        raw_shares = round_down(size, table.size)
        raw_money = raw_shares * raw_price
        if _places(raw_money) > table.amount:
            raw_money = round_up(raw_money, table.amount + 4)
            if _places(raw_money) > table.amount:
                raw_money = round_down(raw_money, table.amount)
        return to_token_decimals(raw_money), to_token_decimals(raw_shares)

    raw_shares = round_down(size, table.size)
    raw_money = raw_shares * raw_price
    if _places(raw_money) > table.amount:
        raw_money = round_up(raw_money, table.amount + 4)
        if _places(raw_money) > table.amount:
            raw_money = round_down(raw_money, table.amount)
    return to_token_decimals(raw_shares), to_token_decimals(raw_money)


def rounded_price(price: Decimal, market: PredictionMarket) -> Decimal:
    """El precio del usuario, bajado al múltiplo del salto que no le perjudica.

    Se redondea **hacia abajo** siempre, y también al vender. La razón es que el
    precio es un **límite** para quien firma y no una estimación: al comprar,
    bajar el precio nunca le hace pagar más de lo que dijo; al vender, subirlo le
    haría pedir más de lo que dijo. En los dos casos, redondear a la baja es la
    dirección en la que el usuario sigue estando de acuerdo con lo firmado, y la
    contraria sería firmar algo que no pidió.
    """
    if market.tick_size is None:
        raise ExecutionError(
            f"el mercado «{market.question}» no publica su salto de precio, así que "
            f"no se puede redondear una orden contra él."
        )
    return round_down(price, _places(market.tick_size))


def build_order(
    market: PredictionMarket,
    *,
    outcome_label: str,
    side: PredictionSide,
    size: Decimal,
    price: Decimal,
    nonce: int = 0,
    fee_rate_bps: int = 0,
    expiration: int = 0,
) -> PredictionOrder:
    """Construye la orden sin firmar, redondeando a las reglas del mercado.

    Exige que el mercado sea operable —`is_tradeable`— y falla diciendo **qué**
    falta cuando no lo es. Construir una orden contra un mercado del que no
    consta el `tick` o el identificador del resultado no es «casi» posible: es
    imposible, y el error tiene que decir cuál de las piezas falta en vez de
    reventar más tarde dentro de una firma.
    """
    faltas = market.tradeability_blockers()
    if faltas:
        raise ExecutionError(
            f"el mercado «{market.question}» no es operable: " + "; ".join(faltas) + "."
        )

    elegido = market.outcome(outcome_label)
    if elegido is None:
        disponibles = ", ".join(o.label for o in market.outcomes)
        raise ExecutionError(
            f"«{outcome_label}» no es un resultado de «{market.question}». "
            f"Los que hay son: {disponibles}."
        )
    if elegido.token_id is None:  # pragma: no cover - lo garantiza is_tradeable
        raise ExecutionError(f"el resultado «{outcome_label}» no tiene identificador.")

    # Las cuatro piezas operables del mercado. `tradeability_blockers` ya dijo
    # arriba cuál falta cuando falta; esto las reúne en una tupla de tipos no
    # opcionales para que el resto de la función no arrastre comprobaciones.
    condition_id, tick_size, min_order_size, neg_risk = _trading_fields(market)

    tabla = rounding_for(tick_size)
    precio = rounded_price(price, market)
    tamano = round_down(size, tabla.size)

    if tamano < min_order_size:
        raise ExecutionError(
            f"el mercado pide un mínimo de {min_order_size} participaciones "
            f"por orden y se pidieron {tamano}: el recinto rechazaría la orden "
            f"entera. No hay forma de comprar menos."
        )

    return PredictionOrder(
        venue_id=market.venue.venue_id,
        chain=market.venue.chain,
        market_id=market.market_id,
        condition_id=condition_id,
        question=market.question,
        outcome_label=elegido.label,
        token_id=elegido.token_id,
        side=side,
        price=precio,
        size=tamano,
        tick_size=tick_size,
        neg_risk=neg_risk,
        fee_rate_bps=fee_rate_bps,
        nonce=nonce,
        expiration=expiration,
    )


def _trading_fields(market: PredictionMarket) -> tuple[str, Decimal, Decimal, bool]:
    """Las cuatro piezas que hacen operable un mercado, ya sin opcionales.

    Existe para que la comprobación esté en un sitio y el resto de la
    construcción trabaje con tipos que no pueden ser `None`. `build_order` ya
    llamó a `tradeability_blockers` antes, así que aquí esto no debería saltar
    nunca; está porque el tipo no puede saberlo, y porque un `assert` —que es lo
    que pondría en su lugar— desaparece con `-O` y deja el fallo para más tarde.
    """
    if (
        market.condition_id is None
        or market.tick_size is None
        or market.min_order_size is None
        or market.neg_risk is None
    ):
        raise ExecutionError(
            f"el mercado «{market.question}» no tiene los datos que hacen falta "
            f"para firmar: " + "; ".join(market.tradeability_blockers()) + "."
        )
    return (
        market.condition_id,
        market.tick_size,
        market.min_order_size,
        market.neg_risk,
    )


# --------------------------------------------------------------------------- #
# La firma
# --------------------------------------------------------------------------- #
#: Los tipos del mensaje, en el **orden exacto** en que el contrato los declara.
#: El orden es parte de la definición: EIP-712 calcula el resumen sobre la
#: concatenación de los campos, así que reordenarlos produce una firma distinta
#: para los mismos datos, y el contrato la rechaza sin decir por qué.
ORDER_TYPES: Final[dict[str, list[dict[str, str]]]] = {
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


def typed_data(
    order: PredictionOrder,
    *,
    signer: str,
    salt: int,
    timestamp: int,
    maker: str | None = None,
    signature_type: int = SIGNATURE_TYPE_EOA,
) -> dict[str, Any]:
    """El mensaje EIP-712 completo, listo para firmar.

    Se expone aparte de la firma porque es lo que hace **auditable** lo que se
    firma: quien quiera comprobar una orden puede reconstruir este diccionario y
    compararlo, sin tener que leer una firma ni confiar en este módulo.

    `maker` es de quién sale el dinero y `signer` quién firma. Aquí coinciden
    —una cuenta normal firma por sí misma— pero son campos distintos en el
    contrato porque las carteras proxy de Polymarket los separan.
    """
    maker_amount, taker_amount = order_amounts(
        order.side, order.size, order.price, rounding_for(order.tick_size)
    )
    return {
        "types": ORDER_TYPES,
        "primaryType": "Order",
        "domain": {
            "name": DOMAIN_NAME,
            "version": DOMAIN_VERSION,
            "chainId": CHAIN_ID,
            "verifyingContract": to_checksum_address(exchange_for(order.neg_risk)),
        },
        "message": {
            "salt": salt,
            "maker": to_checksum_address(maker or signer),
            "signer": to_checksum_address(signer),
            "tokenId": int(order.token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "side": 0 if order.is_buy else 1,
            "signatureType": signature_type,
            "timestamp": timestamp,
            "metadata": ZERO_BYTES32,
            "builder": ZERO_BYTES32,
        },
    }


def sign_order(
    order: PredictionOrder,
    *,
    private_key: str,
    order_type: str | None = None,
    salt: int | None = None,
    timestamp: int | None = None,
    wallet: str | None = None,
) -> SignedPredictionOrder:
    """Firma la orden y devuelve el cuerpo exacto que se enviará al recinto.

    La clave entra por parámetro y no se guarda: se usa para firmar y la variable
    sale de ámbito sola. La **dirección** es lo único que sobrevive al retorno.

    Antes de devolver nada se comprueba la firma recuperando la dirección desde
    ella. Es una comprobación cerrada —no necesita red, ni saldo, ni que el
    recinto exista— y es la que detecta el fallo más caro de este módulo: una
    firma construida sobre un mensaje distinto del que se cree. Si no cuadra, no
    se devuelve una orden a medias: se lanza.
    """
    eoa = Account.from_key(private_key).address
    if salt is None:
        # La sal evita que dos órdenes idénticas colisionen en el recinto. Se
        # envía como número JSON y el recinto lo lee como entero de JavaScript:
        # por encima de 2^53 la orden se rechaza con «Invalid order payload».
        salt = secrets.randbelow(SALT_LIMIT)
    if timestamp is None:
        timestamp = int(time.time() * 1000)

    if wallet is None:
        mensaje = typed_data(order, signer=eoa, salt=salt, timestamp=timestamp)
        firmado = Account.sign_message(encode_typed_data(full_message=mensaje), private_key)
        firma = _hex(firmado.signature)
        digest = _hex(firmado.message_hash)
        recuperada = Account.recover_message(
            encode_typed_data(full_message=mensaje), signature=firma
        )
    else:
        # Cartera de depósito: la EOA firma por dentro y el recinto valida con la
        # wallet. `maker` y `signer` son la wallet, no la EOA.
        mensaje = typed_data(
            order,
            signer=wallet,
            salt=salt,
            timestamp=timestamp,
            maker=wallet,
            signature_type=SIGNATURE_TYPE_DEPOSIT_WALLET,
        )
        exchange = exchange_for(order.neg_risk)
        firma = wrap_order_signature(
            mensaje["message"],
            chain_id=CHAIN_ID,
            exchange=exchange,
            wallet=wallet,
            private_key=private_key,
        )
        digest = _hex(keccak(hexstr=firma))
        recuperada = recover_wrapped_signer(
            mensaje["message"],
            firma,
            chain_id=CHAIN_ID,
            exchange=exchange,
            wallet=wallet,
        )

    if recuperada.lower() != eoa.lower():
        raise ExecutionError(
            f"la firma de la orden no se corresponde con la clave que la emite "
            f"({recuperada} frente a {eoa}). No se publica: una orden firmada por "
            f"otra cuenta la rechazaría el recinto, y si la aceptara estaría "
            f"operando con dinero ajeno."
        )

    m = mensaje["message"]
    cuerpo = {
        "order": {
            "builder": m["builder"],
            "expiration": str(order.expiration),
            "maker": m["maker"],
            "makerAmount": str(m["makerAmount"]),
            "metadata": m["metadata"],
            "salt": salt,
            "side": "BUY" if order.is_buy else "SELL",
            "signature": firma,
            "signatureType": m["signatureType"],
            "signer": m["signer"],
            "takerAmount": str(m["takerAmount"]),
            "timestamp": str(timestamp),
            "tokenId": str(m["tokenId"]),
        },
        "owner": "",  # lo rellena el cliente con la credencial L2
        "orderType": order_type or order.order_type,
        "deferExec": False,
        "postOnly": False,
    }
    _log.info(
        "polymarket.order_signed",
        market=order.market_id,
        outcome=order.outcome_label,
        side=order.side.value,
        price=str(order.price),
        size=str(order.size),
        signer=m["signer"],
        digest=digest,
    )
    return SignedPredictionOrder(
        order=order,
        signer=m["signer"],
        signature=firma,
        digest=digest,
        payload=json.dumps(cuerpo, separators=(",", ":"), ensure_ascii=False),
    )


def _hex(value: bytes) -> str:
    """Bytes a hexadecimal con el prefijo `0x`.

    `bytes.hex()` **no** lo pone, y aquí todo lo que sale de esta función acaba
    en un campo que el dominio exige con prefijo. Ponerlo en un sitio y no en
    cada llamada evita que la próxima cadena que se añada aquí se olvide de él y
    falle en la validación de una entidad en vez de donde se construyó.
    """
    text = value.hex()
    return text if text.startswith("0x") else f"0x{text}"


def wire_body(signed: SignedPredictionOrder, *, owner: str) -> str:
    """El cuerpo final, con la credencial de quien publica ya dentro.

    Se reescribe aquí en vez de guardarlo ya completo porque la credencial no
    existe cuando se firma —se deriva justo antes de publicar— y porque
    **reescribir el cuerpo invalidaría la firma** si el recinto firmara sobre
    estos bytes. No lo hace: la firma EIP-712 es sobre el mensaje de arriba, y
    `owner` es un campo de enrutado que va fuera de él. Se deja dicho porque es
    la clase de detalle que, cambiado el día que el recinto cambie, rompe en
    silencio.
    """
    cuerpo = json.loads(signed.payload)
    cuerpo["owner"] = owner
    return json.dumps(cuerpo, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# El cobro
# --------------------------------------------------------------------------- #
#: Los dos conjuntos de índices del contrato condicional: el resultado «sí» y el
#: «no». Se piden **siempre los dos**, y no el que se tenga, porque es el
#: contrato quien decide cuánto quemar de cada uno —recorre los índices, mira el
#: saldo y quema lo que haya—. Pedir sólo uno obligaría a saber de antemano cuál
#: de los dos resultados se tiene, que es justo el dato que puede haber cambiado
#: entre la lectura y la firma; y con los dos, una posición que ya no esté
#: simplemente quema cero, que es lo que se quiere.
#:
#: Están escritos como números y no como potencias porque son el valor binario
#: del índice: 1 = 0b01 (primer resultado), 2 = 0b10 (segundo).
_INDEX_SETS: Final = (1, 2)

#: `parentCollectionId` en cero: estos mercados no cuelgan de ninguna colección
#: padre. Es el valor con el que se crearon y el que el contrato espera.
_NO_PARENT: Final = "0x" + "00" * 32

#: El selector de `redeemPositions(address,bytes32,bytes32,uint256[])`. Se deriva
#: de la firma con el mismo `selector` que usan los motores de swap, y hay una
#: prueba que lo clava contra el valor conocido: así el nombre de la función es
#: la fuente y el número queda comprobado por algo que no es él mismo.
SELECTOR_REDEEM: Final = selector("redeemPositions(address,bytes32,bytes32,uint256[])")


def build_redeem_calldata(
    condition_id: str,
    *,
    collateral: str = COLLATERAL,
    index_sets: Sequence[int] = _INDEX_SETS,
) -> str:
    """El `calldata` de `redeemPositions`, listo para firmar. Sin red.

    Codifica `(address, bytes32, bytes32, uint256[])`, que es la firma con la que
    el contrato condicional de Polymarket cambia participaciones ya resueltas por
    su colateral.

    El `condition_id` es un `bytes32` y se acepta con o sin `0x`: el dominio lo
    guarda tal cual llega de la fuente y aquí se normaliza, porque el contrato no
    distingue y quien lea el archivo sí. Uno con la longitud equivocada **se
    rechaza** en vez de rellenarse a cero: rellenar produciría una llamada contra
    un mercado que no existe, y el gas de esa transacción se pierde sin cobrar
    nada.

    El array va **fuera de línea**, que es como el ABI codifica los tipos
    dinámicos: en la cabecera va un desplazamiento a la cola donde está su
    contenido. La cabecera son las tres palabras fijas más la del desplazamiento,
    o sea 4 x 32 bytes.
    """
    limpio = condition_id[2:] if condition_id.startswith("0x") else condition_id
    if len(limpio) != 64:
        raise ExecutionError(
            f"el identificador de mercado «{condition_id}» no mide 32 bytes "
            f"({len(limpio) // 2}): con él no se puede construir un cobro. Se "
            f"rechaza en vez de rellenarlo, porque una llamada contra un mercado "
            f"que no existe quema el gas y no devuelve nada."
        )
    try:
        int(limpio, 16)
    except ValueError as error:
        raise ExecutionError(
            f"el identificador de mercado «{condition_id}» no es hexadecimal: "
            f"no se puede construir un cobro contra él."
        ) from error
    if not index_sets:
        raise ExecutionError(
            "un cobro sin conjuntos de índices no dice qué resultados quemar"
        )

    desplazamiento = word_uint(4 * 32)
    elementos = "".join(word_uint(valor) for valor in index_sets)
    return (
        SELECTOR_REDEEM
        + word_address(collateral)
        + _NO_PARENT[2:]
        + limpio.lower()
        + desplazamiento
        + word_uint(len(index_sets))
        + elementos
    )
