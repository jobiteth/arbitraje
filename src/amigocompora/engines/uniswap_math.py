"""La aritmética de precio que Uniswap V3 y V4 **comparten**, y por qué se comparte.

V4 no inventó una representación de precio: heredó la de V3 tal cual. El estado de
un pool guarda `sqrtPriceX96`, la raíz del precio desplazada 96 bits, y el precio
marginal es `(sqrtPriceX96 / 2**96)**2` — el mismo número, la misma escala, el
mismo significado en las dos versiones. La única diferencia entre ambas es dónde
vive ese estado: en V3 hay un contrato por pool, en V4 una entrada en el almacén
del `PoolManager` a la que se llega por el `poolId`. La **cuenta** no cambia.

Por eso el impacto de una orden vive aquí y no dentro de cada motor. Escrito dos
veces sería dos sitios donde equivocarse, y el fallo no sería ruidoso: los dos
motores cotizarían el mismo pool y publicarían impactos distintos en la misma
tabla, sin que nada chirriara. Un usuario que compara dos filas del mismo pool y
ve dos impactos diferentes no tiene forma de saber cuál es el bueno.

### Lo que este módulo NO hace

No cotiza: no habla con ningún contrato ni con ninguna fuente. Recibe números ya
leídos —el precio del pool y el importe que dio el cotizador— y devuelve una
cifra. Todo lo que hay aquí es determinista y probable con datos de la cadena.
"""

from __future__ import annotations

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
    """El impacto de precio de esta orden, medido contra el precio marginal.

    La cuenta es una comparación, no una fórmula de libro: se toma el precio
    marginal del pool —`(sqrtPriceX96 / 2**96)**2`, que es el precio de la moneda
    1 en unidades de la moneda 0, en unidades mínimas— y se compara con el precio
    que de verdad se obtiene. La diferencia **es** el impacto, y sale de dos datos
    leídos de la cadena: el precio del pool antes del swap y el importe que el
    cotizador devuelve.

    La comisión se descuenta del marginal antes de comparar, porque el importe que
    devuelve el cotizador ya la lleva cobrada por dentro: sin ese paso, la comisión
    aparecería contada como impacto y un pool del 1 % mostraría un 1 % de impacto
    en cada operación, por pequeña que fuera.

    El sentido se recibe hecho —`token_in_is_token0`— y no se deduce comparando
    direcciones: en V3 el orden lo fija la fábrica al construir el pool y en V4 lo
    fija la regla de que la moneda 0 es la dirección menor, así que cada motor sabe
    el suyo y aquí no hay que elegir. Recibirlo evita que este módulo tenga que
    saber de qué versión viene el precio que le dan.

    Devuelve `None` cuando no hay base de comparación —sin precio, o sin importe—
    y **cero** cuando la resta sale negativa. Un negativo aquí es el error de la
    linealización, no una ganancia: por la forma del producto constante, ejecutar
    siempre se paga igual o peor que el precio marginal, nunca mejor.

    Ni V3 ni V4 descuentan su comisión de protocolo, que el pool se queda por
    dentro y que ya viene restada en lo que devuelve el cotizador. El efecto es que
    la cifra publicada es **conservadoramente alta** en unos pocos puntos básicos,
    nunca baja; descontarla exigiría decodificar un entero empaquetado en dos
    mitades de 12 bits para ganar una precisión menor que el ruido del mercado.
    """
    if amount_in_raw <= 0 or amount_out_raw <= 0 or sqrt_price_x96 <= 0:
        return None
    marginal = EXACT.divide(Decimal(sqrt_price_x96 * sqrt_price_x96), Q192)
    if marginal <= 0:
        return None
    out_per_in = marginal if token_in_is_token0 else EXACT.divide(Decimal(1), marginal)
    effective = EXACT.multiply(out_per_in, EXACT.subtract(Decimal(1), fee.as_ratio()))
    if effective <= 0:
        return None
    executed = EXACT.divide(Decimal(amount_out_raw), Decimal(amount_in_raw))
    ratio = EXACT.subtract(Decimal(1), EXACT.divide(executed, effective))
    return BasisPoints(0) if ratio <= 0 else BasisPoints.from_ratio(ratio)
