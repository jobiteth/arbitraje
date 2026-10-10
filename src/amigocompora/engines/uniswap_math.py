"""La aritmética de precio que los pools comparten, y por qué se comparte.

Uniswap V3 y V4 comparten representación de precio: V4 no la inventó, heredó la
de V3 tal cual. El estado de un pool guarda `sqrtPriceX96`, la raíz del precio
desplazada 96 bits, y el precio marginal es `(sqrtPriceX96 / 2**96)**2` — el
mismo número, la misma escala, el mismo significado en las dos versiones. La
única diferencia entre ambas es dónde vive ese estado: en V3 hay un contrato por
pool, en V4 una entrada en el almacén del `PoolManager` a la que se llega por el
`poolId`. La **cuenta** no cambia.

Los pools de producto constante —Uniswap V2 y sus forks— no tienen esa raíz: su
precio marginal sale de las reservas, `reserva de salida / reserva de entrada`
en unidades mínimas. Lo que **sí** comparten con los concentrados es la forma de
medir el impacto: comparar lo que la orden ejecuta contra ese marginal, con la
comisión descontada una sola vez. Por eso la comparación vive en
`impact_bps_from_marginal`, que recibe el marginal ya calculado, y `impact_bps`
—el de los concentrados— sólo aporta de dónde sale el suyo. Un motor V2 llama a
la primera con sus reservas y publica el mismo número que un V3 que mirara el
mismo par por el suyo.

Por eso el impacto de una orden vive aquí y no dentro de cada motor. Escrito dos
veces sería dos sitios donde equivocarse, y el fallo no sería ruidoso: los dos
motores cotizarían el mismo pool y publicarían impactos distintos en la misma
tabla, sin que nada chirriara. Un usuario que compara dos filas del mismo pool y
ve dos impactos diferentes no tiene forma de saber cuál es el bueno.

Lo mismo vale para el impacto de un camino de varios tramos: `combine_impact_bps`
compone los de cada tramo con una sola regla, y cualquier motor que enrute por
más de un pool la usa en vez de inventarse su propia cuenta.

### Lo que este módulo NO hace

No cotiza: no habla con ningún contrato ni con ninguna fuente. Recibe números ya
leídos —el precio del pool y el importe que dio el cotizador— y devuelve una
cifra. Todo lo que hay aquí es determinista y probable con datos de la cadena.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from amigocompora.domain.money import EXACT, BasisPoints

#: `2**192`, la escala de `sqrtPriceX96`. Se escribe como entero y se convierte
#: con `Decimal(...)`, que es exacto; elevarlo con el contexto del dominio
#: redondearía a 60 dígitos significativos una constante que cabe entera.
Q192: Final = Decimal(1 << 192)


def impact_bps(
    *,
    amount_in_raw: int,
    amount_out_raw: int,
    sqrt_price_x96: int,
    token_in_is_token0: bool,
    fee: BasisPoints,
) -> BasisPoints | None:
    """El impacto de esta orden en un pool concentrado (V3, V4).

    El marginal sale del estado del pool —`(sqrtPriceX96 / 2**96)**2`, que es el
    precio de la moneda 1 en unidades de la moneda 0, en unidades mínimas— y la
    comparación contra lo que de verdad se obtiene es la de
    `impact_bps_from_marginal`, que es la misma para cualquier pool. Aquí sólo se
    calcula el marginal y su sentido.

    El sentido se recibe hecho —`token_in_is_token0`— y no se deduce comparando
    direcciones: en V3 el orden lo fija la fábrica al construir el pool y en V4 lo
    fija la regla de que la moneda 0 es la dirección menor, así que cada motor sabe
    el suyo y aquí no hay que elegir. Recibirlo evita que este módulo tenga que
    saber de qué versión viene el precio que le dan.

    Devuelve `None` cuando no hay base de comparación: sin precio, o sin importe.
    """
    if amount_in_raw <= 0 or amount_out_raw <= 0 or sqrt_price_x96 <= 0:
        return None
    marginal = EXACT.divide(Decimal(sqrt_price_x96 * sqrt_price_x96), Q192)
    if marginal <= 0:
        return None
    out_per_in = marginal if token_in_is_token0 else EXACT.divide(Decimal(1), marginal)
    return impact_bps_from_marginal(
        amount_in_raw=amount_in_raw,
        amount_out_raw=amount_out_raw,
        marginal_out_per_in=out_per_in,
        fee=fee,
    )


def impact_bps_from_marginal(
    *,
    amount_in_raw: int,
    amount_out_raw: int,
    marginal_out_per_in: Decimal,
    fee: BasisPoints,
) -> BasisPoints | None:
    """El impacto de una orden, contra un marginal ya calculado.

    Es la parte que comparten los pools concentrados —que sacan el marginal de
    `sqrtPriceX96`— y los de producto constante —que lo sacan de las reservas—:
    una comparación, no una fórmula de libro. Se toma el precio marginal del pool
    —el precio de la moneda de salida en unidades de la de entrada, en unidades
    mínimas— y se compara con el precio que de verdad se obtiene; la diferencia
    **es** el impacto, y sale de dos datos leídos de la cadena: el precio del
    pool antes del swap y el importe que el cotizador devuelve.

    La comisión se descuenta del marginal antes de comparar, porque el importe
    que devuelve el cotizador ya la lleva cobrada por dentro: sin ese paso, la
    comisión aparecería contada como impacto y un pool del 1 % mostraría un 1 %
    de impacto en cada operación, por pequeña que fuera.

    Devuelve `None` cuando no hay base de comparación —sin marginal, o sin
    importe— y **cero** cuando la resta sale negativa. Un negativo aquí es el
    error de la linealización, no una ganancia: por la forma de los pools que se
    cotizan aquí —producto constante y concentrado— ejecutar siempre se paga
    igual o peor que el precio marginal, nunca mejor.

    En V3 y V4 el efecto de la comisión de protocolo, que el pool se queda por
    dentro y que ya viene restada en lo que devuelve el cotizador, es que la
    cifra publicada es **conservadoramente alta** en unos pocos puntos básicos,
    nunca baja; descontarla exigiría decodificar un entero empaquetado en dos
    mitades de 12 bits para ganar una precisión menor que el ruido del mercado.
    """
    if amount_in_raw <= 0 or amount_out_raw <= 0 or marginal_out_per_in <= 0:
        return None
    effective = EXACT.multiply(
        marginal_out_per_in, EXACT.subtract(Decimal(1), fee.as_ratio())
    )
    if effective <= 0:
        return None
    executed = EXACT.divide(Decimal(amount_out_raw), Decimal(amount_in_raw))
    ratio = EXACT.subtract(Decimal(1), EXACT.divide(executed, effective))
    return BasisPoints(0) if ratio <= 0 else BasisPoints.from_ratio(ratio)


def combine_impact_bps(parts: Sequence[BasisPoints]) -> BasisPoints:
    """El impacto de un camino: los tramos **componen**, no se suman.

    Un swap que cruza dos pools paga los dos impactos, pero sumarlos cuenta de
    más y componerlos cuenta lo que es. Cada tramo entrega menos de lo que su
    marginal promete por un factor `(1 - i_k)`, y como los pools son
    independientes el factor del camino entero es el producto: el impacto total
    es `1 - Π(1 - i_k)`. Con dos tramos del 1 % la suma diría 2 % y el producto
    1,99 %; la diferencia es pequeña pero el razonamiento de la suma está mal, y
    aquí se escribe una vez.

    Una secuencia vacía da impacto **cero**, que es lo correcto: un camino sin
    tramos no ejecuta nada y no mueve ningún precio.
    """
    surviving = Decimal(1)
    for part in parts:
        surviving = EXACT.multiply(
            surviving, EXACT.subtract(Decimal(1), part.as_ratio())
        )
    ratio = EXACT.subtract(Decimal(1), surviving)
    return BasisPoints(0) if ratio <= 0 else BasisPoints.from_ratio(ratio)
