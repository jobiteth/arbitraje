"""Uniswap V4 por contrato: la segunda vía sin clave, y las formas de equivocarse.

Lo que se fija aquí no es que «cotice». Es lo que puede perder dinero o publicar
una cifra falsa, y en V4 casi todo tiene una forma distinta de la de V3:

1. **La codificación, con tres desplazamientos anidados.** Un pool de V4 no es un
   contrato sino una clave que se deriva; el swap no va a un router por red sino
   al `UniversalRouter`; y dentro viajan tres acciones en un comando. Cada nivel
   tiene su propio desplazamiento dinámico —el struct del cotizador, el `execute`
   y la carga del `V4_SWAP`— y los tres se cuentan desde su propia tabla, no desde
   el principio del calldata. Un byte de desvío no revierte al construirse:
   revienta en la cadena con un error que no dice cuál de los tres está mal.

2. **Dos structs que se parecen y no son iguales.** El del cotizador tiene ocho
   palabras de cabeza y el del swap nueve, porque sólo el segundo lleva
   `amountOutMinimum`. Codificar el del cotizador con la cabeza del swap produce un
   revert **vacío**: el contrato lee el importe del sitio equivocado y no hay
   selector de error que lo explique. Se cometió, se midió así, y por eso hay una
   prueba que cuenta las palabras.

3. **Qué pools existen.** En V3 lo dice la fábrica. Aquí no hay a quién preguntar,
   así que el filtro es la **liquidez** y no el precio: medido en Base, los tramos
   del 0,01 % y el 1 % de WETH/USDC tienen `sqrtPriceX96` válido y cero liquidez.
   Un filtro por precio los daría por buenos.

4. **La comisión que declara el pool.** La clave se deriva **con** la comisión
   dentro, así que si el estado que se lee declara otra, se está leyendo otro
   sitio. Eso lanza en vez de omitir el pool, porque omitirlo convertiría una
   tabla de direcciones equivocada en un motor que no encuentra nada.

5. **El destinatario.** En V4 no lo deduce el contrato: va escrito en la orden. Los
   dos centinelas del campo `recipient` son direcciones perfectamente válidas que
   el contrato traduce a otra cosa, así que hay que rechazarlas antes.

6. **El nativo es una moneda.** V4 tiene pools de nativo —medidos, en las siete
   redes—, así que ETH va como la dirección cero y **no** como su envoltorio.
   Sustituirlo por WETH llevaría al pool equivocado, que es exactamente lo que V3
   hace a propósito y aquí sería un error.

7. **El segundo salto se cotiza entero.** Cuando el par no tiene pool directo, el
   camino se le pide al cotizador de una vez —`quoteExactInput`, con los
   `PathKey` del camino— y el importe que devuelve es el que la ejecución daría,
   no la composición de dos lecturas sueltas. Cada `PathKey` nombra la moneda
   **de llegada** de su tramo —escribirla al revés deriva la clave de otro
   pool—, y el array va con sus propios desplazamientos.

## El vector de oro, y de qué está respaldado

`test_el_vector_de_oro_no_cambia_sin_que_alguien_se_entere` fija el `keccak256`
del calldata completo con el reloj congelado. Un número así, por sí solo, sería
comparar el código consigo mismo; lo que lo respalda es un experimento hecho
contra la cadena: ese mismo calldata —el que produce este motor, y con el mismo
tamaño de 1.124 bytes que se comprueba aquí— se mandó por `eth_call` al
`UniversalRouter` de Base **desde una cartera sin permisos**, y el router contestó
`AllowanceExpired` de Permit2.

Ese error concreto es la prueba: significa que el router decodificó el comando
`V4_SWAP`, abrió el bloqueo del `PoolManager`, ejecutó el swap exacto y sólo se
paró al ir a **cobrar**, que es el último paso. Un solo byte mal puesto en
cualquiera de los tres desplazamientos habría dado `UnsupportedAction`,
`InputLengthMismatch` o un revert vacío mucho antes. La huella fija ese acierto
para que no se pierda en el próximo cambio; no pretende ser la demostración. Y no
coincide con la del día de la medición porque el `deadline` sale del reloj: la de
aquí es la del reloj congelado.

El camino tiene su propio vector medido, y del mismo tipo:
`test_el_camino_reproduce_el_calldata_medido_en_la_cadena` fija los bytes exactos
que se mandaron por `eth_call` al `V4Quoter` de Base el 2026-10-09 —WETH → USDC →
WETH, los dos tramos del 0,30 %, 10¹⁵ wei— y que devolvieron
`[993014236051103, 70983]`: el importe y una estimación de gas. Si el contrato
decodificó eso y contestó, los desplazamientos del `PathKey[]`, el `0xa0` del
`hookData` y la moneda de llegada de cada tramo son los que lee.

Las cifras de pool están **medidas** contra Base el 2026-10-06 con lecturas: los
cuatro `poolId` de WETH/USDC, su liquidez, su `sqrtPriceX96` y la salida del
`V4Quoter` para 1 USDC. El lector se sustituye por un doble, que es la frontera de
red del motor; todo lo demás que se ejercita es el código de verdad.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

import pytest
from eth_utils.crypto import keccak

from amigocompora.domain.chains import chain
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    EngineConfigError,
    InvalidAmountError,
    NoQuotesError,
    QuoteMovedError,
    SourceResponseError,
    UnsupportedOperationError,
)
from amigocompora.domain.models import (
    Measurement,
    Quote,
    RouteHop,
    SwapRoute,
    Token,
    TradingPair,
)
from amigocompora.domain.money import EXACT, BasisPoints, TokenAmount
from amigocompora.engines.catalog import hub_tokens
from amigocompora.engines.uniswap_math import combine_impact_bps, impact_bps
from amigocompora.engines.uniswap_v4 import calldata as abi
from amigocompora.engines.uniswap_v4 import engine as v4
from amigocompora.engines.uniswap_v4.addresses import (
    DEPLOYMENTS,
    FEE_TIERS,
    PERMIT2,
    SWAP_CHAINS,
)

AHORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

#: Los dos tokens del par medido, en minúsculas porque es como los pregunta el
#: motor: `_on_chain_address` normaliza, y una dirección con otra caja sería otro
#: token —y además cambiaría el orden del que sale la clave del pool—.
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
CERO = "0x" + "00" * 20

#: Los cuatro `poolId` de WETH/USDC en Base, **medidos** el 2026-10-06. Se derivan
#: con la comisión y el espaciado dentro, así que un cambio en el espaciado
#: canónico de un tramo los cambia todos — que es justo lo que hay que notar,
#: porque apuntarían a pools que no existen.
POOL_ID = {
    100: "0xf97566d3f65c048c9fa568ac3346c7970fd8030e62eeb5c0bdf409fe17c7c511",
    500: "0x90333bb05c258fe0dddb2840ef66f1a05165aa7dac6815d24e807cc6ebd943a0",
    3_000: "0x1d8c55f347727c0fb4f5e1b65cdb93639e0c7102580a7d345e1144cd5a718f54",
    10_000: "0x4949ca7a256e6eb04214e80f98c890bba07658bc314e42a676bdafa2704662b7",
}

#: Medido: el tramo del 0,05 % es el pozo profundo de este par en Base y el del
#: 0,30 % tiene bastante menos. Los tramos del 0,01 % y el 1 % **no tienen
#: liquidez ninguna** aunque su precio se pueda leer.
LIQUIDEZ_500 = 92_276_942_699_500
LIQUIDEZ_3000 = 23_390_678_248_634_131
SQRT_PRICE_500 = 4_113_905_229_490_580_800_644_426
SQRT_PRICE_3000 = 4_111_693_409_248_451_428_844_584

#: Lo que dio el `V4Quoter` para 1 USDC contra cada uno de esos dos pools.
SALIDA_500 = 370_585_574_453_930
SALIDA_3000 = 369_994_358_556_075
ENTRADA_RAW = 10**6

#: Medido: el calldata de USDC→WETH del tramo del 0,05 %, con este reloj
#: congelado. Mide 1.124 bytes, que es el tamaño que tenía el que se mandó al
#: router de Base.
KECCAK_DEL_VECTOR = "0x5a0065740fd6cd050bd014ad2053d07089216e6c26efaa8aa80829ad06a2793e"
MINIMO_DEL_VECTOR = 368_732_646_581_660

DESTINATARIO = "0x" + "b" * 40
BASE = DEPLOYMENTS["base"]


# --------------------------------------------------------------------------- #
# Piezas
# --------------------------------------------------------------------------- #
def _palabra_uint(value: int) -> str:
    return f"{value:064x}"


def _palabra_addr(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _palabra_de(data: str, selector: str, index: int) -> str:
    """La palabra `index` del cuerpo de ese calldata.

    El contador empieza **después del selector**: el `0x` y sus cuatro bytes
    ocupan sitio, y saltárselos leería una ventana desplazada que ya no es una
    palabra, con lo que la respuesta sería la de otro campo.
    """
    inicio = len(selector) + index * 64
    return data[inicio : inicio + 64]


def _direccion_de(data: str, selector: str, index: int) -> str:
    """La dirección de la palabra `index` del cuerpo de ese calldata."""
    return "0x" + _palabra_de(data, selector, index)[24:]


def _sqrt_para(
    amount_in_raw: int, amount_out_raw: int, *, token_in_is_token0: bool
) -> int:
    """El `sqrtPriceX96` cuyo precio marginal es exactamente esa salida.

    Sirve para los escenarios que no son el par medido: así el impacto que el
    motor calcula es el de la comisión y nada más, y una cifra rara en una prueba
    significa que se ha roto algo y no que el pool de mentira tenía un precio
    incoherente con lo que devolvía.
    """
    ratio = Decimal(amount_out_raw) / Decimal(amount_in_raw)
    precio = ratio if token_in_is_token0 else Decimal(1) / ratio
    return int(precio.sqrt() * Decimal(1 << 96))


def _sqrt_de(precio: Decimal) -> int:
    """`sqrtPriceX96` de un precio marginal, para no escribir el entero a mano."""
    return int(EXACT.multiply(precio.sqrt(), Decimal(1 << 96)))


def _slot0(sqrt_price_x96: int, lp_fee: int) -> str:
    """`getSlot0` real: cuatro palabras —precio, tick, comisión de protocolo,
    comisión del LP—, y las que interesan están en la primera y la última.

    El tick y la comisión de protocolo van a cero porque el motor no los lee:
    `decode_slot0` devuelve el precio y la comisión del LP, que son las dos que se
    usan. Rellenarlos sería afirmar sobre un campo que nadie mira.
    """
    return (
        "0x"
        + _palabra_uint(sqrt_price_x96)
        + _palabra_uint(0)
        + _palabra_uint(0)
        + _palabra_uint(lp_fee)
    )


def _respuesta_quoter(amount_out: int) -> str:
    """El `V4Quoter` devuelve **dos** palabras: el importe y una estimación de gas.

    Medido contra el contrato desplegado; el docstring que decía «cinco» describía
    el retorno del `QuoterV2` de V3, que es otro contrato y otro número. La
    segunda palabra lleva la estimación que devolvió el control del camino medido
    —70.983—, distinta del importe a propósito: si el decodificador leyera la
    palabra equivocada, la prueba lo vería.
    """
    return "0x" + _palabra_uint(amount_out) + _palabra_uint(70_983)


def _token(direccion: str, simbolo: str, decimales: int, chain_key: str = "base") -> Token:
    return Token(symbol=simbolo, decimals=decimales, chain=chain_key, address=direccion)


def _usdc(chain_key: str = "base") -> Token:
    return _token(USDC, "USDC", 6, chain_key)


def _weth(chain_key: str = "base") -> Token:
    return _token(WETH, "WETH", 18, chain_key)


def _nativo(chain_key: str = "base", decimales: int = 18) -> Token:
    return Token(
        symbol=chain(chain_key).native_symbol,
        decimals=decimales,
        chain=chain_key,
        address=None,
    )


def _par(chain_key: str = "base") -> TradingPair:
    return TradingPair(base=_usdc(chain_key), quote=_weth(chain_key))


class _Lector:
    """Doble de `ChainReader`, que despacha por **calldata**.

    Se sustituye el lector entero y no el transporte HTTP porque lo que se
    ejercita es la **decisión** del motor —qué pregunta y qué hace con cada
    respuesta—, y el failover tiene sus propias pruebas. Despachar por calldata y
    no sólo por dirección es lo que permite que `getLiquidity` y `getSlot0` vayan
    al mismo `StateView` sin confundirse: con un doble por dirección, las dos
    lecturas recibirían el mismo hexadecimal y la prueba pasaría por el motivo
    equivocado.
    """

    def __init__(self, handlers: dict[str, object] | None = None) -> None:
        self.handlers = handlers or {}
        self.calls: list[tuple[str, str]] = []

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def eth_call(
        self, chain_key: str, to: str, data: str, *, block: str = "latest"
    ) -> str:
        del chain_key, block
        self.calls.append((to, data))
        # Primero lo declarado y sólo después el comportamiento por defecto: si se
        # evaluara como argumento por omisión de `get`, el caso por defecto se
        # ejecutaría siempre —también cuando hay una respuesta puesta a mano— y una
        # denuncia por lectura no prevista saltaría en una prueba que sí la previó.
        handler = self.handlers.get(data)
        if handler is None:
            handler = self._por_defecto(to, data)
        if handler is None:
            raise AssertionError(f"lectura no prevista a {to} con {data[:10]}")
        if isinstance(handler, BaseException):
            raise handler
        assert isinstance(handler, str)
        return handler

    def _por_defecto(self, to: str, data: str) -> object:
        return None

    def calls_to(self, address: str) -> list[str]:
        return [data for target, data in self.calls if target.lower() == address.lower()]


@dataclass(frozen=True, slots=True)
class _PoolFalso:
    """Lo que un pool de mentira tiene dentro."""

    liquidez: int
    sqrt_price_x96: int
    salida_raw: int
    #: `None` significa «la comisión del tramo», que es lo normal y lo medido.
    lp_fee: int | None = None


@dataclass(frozen=True, slots=True)
class _PoolDeRuta:
    """Un pool de mentira en uno de los pares que forma la búsqueda de ruta.

    Sólo lleva lo que el motor consulta de un pool dentro de una ruta: su
    liquidez —que decide si el par existe— y su precio —contra el que se compone
    el impacto—. La salida **no** vive aquí a propósito: está en la tabla
    `hub_salidas`, indexada por el importe de entrada, porque la segunda pata se
    cotiza con lo que de verdad dio la primera y una tabla que ignorara el
    importe devolvería la misma salida a cualquier entrada, que es justo el error
    que la prueba no debe dejar pasar.
    """

    liquidez: int
    sqrt_price_x96: int


def _hubs_de(par: tuple[str, str]) -> tuple[str, ...]:
    """Las direcciones de los hubs con los que ese par puede formar ruta.

    Se leen del **catálogo** —el mismo `hub_tokens` que consulta el motor— y no
    de una lista escrita aquí: si el catálogo gana un hub, el doble y las pruebas
    tienen que ganarlo con él, o medirían un mundo que ya no es el del motor.
    """
    return tuple(
        v4._on_chain_address(token)
        for token in hub_tokens("base")
        if v4._on_chain_address(token) not in par
    )


class _Escenario(_Lector):
    """Un `PoolManager` de mentira, indexado por el `poolId` **derivado**.

    Se indexa por el identificador derivado y no por el tramo a propósito: es lo
    que convierte el doble en una prueba de la derivación. Si el motor ordenara
    las dos monedas al revés —el error que de verdad se cometió, y que se
    manifestó como `PoolNotInitialized`—, el `poolId` que preguntara no sería
    ninguno de los del par y la lectura se denunciaría como no prevista, en vez de
    devolver un cero que parecería un pool vacío y dejaría pasar el fallo.

    También conoce los pares que el motor puede formar con los hubs del catálogo
    —`_hubs_de`—, que es lo que hace falta desde que hay búsqueda de ruta: un
    `poolId` de ésos que ningún escenario declare contesta liquidez cero, que es
    lo que contesta la cadena para una clave que nadie inicializó. La denuncia
    queda para un `poolId` que no se derive de ninguno de esos pares, que es
    donde de verdad hay un error.
    """

    def __init__(
        self,
        *,
        pools: dict[int, _PoolFalso] | None = None,
        par: tuple[str, str] = (WETH, USDC),
        hub_pools: dict[tuple[str, str, int], _PoolDeRuta] | None = None,
        hub_salidas: dict[tuple[str, str, int, int], int] | None = None,
        caminos: dict[str, int] | None = None,
    ) -> None:
        super().__init__()
        self._pools = pools if pools is not None else {500: _pool(500)}
        self._par = frozenset(par)
        self._tramo_por_id = {
            abi.pool_id(par[0], par[1], fee, espaciado): fee
            for fee, espaciado in FEE_TIERS
        }
        # La pareja se guarda en su orden **canónico** para poder declararla en
        # cualquier orden: la clave de un pool no depende de cómo se escriba, y
        # la tabla del doble tampoco debe.
        self._hub_pools = {
            (*abi.sorted_currencies(a, b), fee): pool
            for (a, b, fee), pool in (hub_pools or {}).items()
        }
        self._hub_salidas = hub_salidas or {}
        self._caminos = caminos or {}
        self._hub_por_id: dict[str, tuple[str, str, int]] = {}
        for hub in _hubs_de(par):
            for fee, espaciado in FEE_TIERS:
                for a, b in ((par[0], hub), (hub, par[1])):
                    self._hub_por_id[abi.pool_id(a, b, fee, espaciado)] = (
                        *abi.sorted_currencies(a, b),
                        fee,
                    )

    def _pool_de(
        self, data: str, pregunta: str
    ) -> tuple[_PoolFalso | _PoolDeRuta | None, int]:
        """El pool al que se refiere una lectura, y el tramo que lo nombra.

        Un `None` como pool es una clave que no se ha inicializado —lo que la
        cadena contesta con un cero—, y una clave que no se deriva de ningún par
        del escenario se denuncia: ésa sólo puede venir de ordenar al revés o de
        preguntar por un par que no toca.
        """
        pool_id = "0x" + data[10 : 10 + 64]
        tramo = self._tramo_por_id.get(pool_id)
        if tramo is not None:
            return self._pools.get(tramo), tramo
        clave = self._hub_por_id.get(pool_id)
        if clave is not None:
            return self._hub_pools.get(clave), clave[2]
        raise AssertionError(
            f"se preguntó {pregunta} por el pool {pool_id}, que no se deriva de "
            f"ninguno de los pares del escenario —el del par ni los que forma con "
            f"los hubs del catálogo—. La clave se calcula ordenando las monedas "
            f"por su valor, así que esto significa que se ordenaron al revés o "
            f"que se preguntó por un par que no era."
        )

    def _cotiza_tramo(self, data: str) -> object:
        """La salida de una pata, despachada por la terna **direccional**.

        En el par directo sale del pool del tramo, como siempre. En un par de
        hub la clave lleva además el **importe**: la segunda pata se cotiza con lo
        que de verdad dio la primera, y una tabla que lo ignorara devolvería la
        misma salida a cualquier entrada. Un importe que no esté en la tabla es un
        tramo que no puede dar salida a ese tamaño, que es una respuesta del
        mercado y no una caída.

        El sentido del intercambio viene dentro del calldata (`zeroForOne`), así
        que la pareja direccional se reconstruye de ahí: preguntar los dos
        argumentos invertidos produciría una lectura distinta que el motor no
        hace nunca.
        """
        selector = abi.SELECTOR_QUOTE_EXACT_INPUT_SINGLE
        c0 = _direccion_de(data, selector, 1)
        c1 = _direccion_de(data, selector, 2)
        tramo = int(_palabra_de(data, selector, 3), 16)
        cero_a_uno = int(_palabra_de(data, selector, 6), 16) == 1
        importe = int(_palabra_de(data, selector, 7), 16)
        entrada, salida = (c0, c1) if cero_a_uno else (c1, c0)
        if frozenset((entrada, salida)) == self._par:
            pool = self._pools.get(tramo)
            return None if pool is None else _respuesta_quoter(pool.salida_raw)
        cantidad = self._hub_salidas.get((entrada, salida, tramo, importe))
        if cantidad is None:
            raise SourceResponseError("execution reverted")
        return _respuesta_quoter(cantidad)

    def _cotiza_camino(self, data: str) -> object:
        """La salida del camino publicado, o se denuncia que no es el publicado.

        Se indexa por el calldata **entero** —el `PathKey[]` con sus
        desplazamientos y la moneda de entrada incluidos— y no por un resumen:
        construir con otro camino, o con la moneda de llegada de un tramo al
        revés, es el fallo que esta tabla existe para ver, y una tabla que lo
        aceptara mediría un mundo donde el camino da igual.
        """
        cantidad = self._caminos.get(data)
        if cantidad is None:
            raise AssertionError(f"se cotizó un camino que no es el publicado: {data}")
        return _respuesta_quoter(cantidad)

    def _por_defecto(self, to: str, data: str) -> object:
        if data.startswith(abi.SELECTOR_GET_LIQUIDITY):
            pool, _tramo = self._pool_de(data, "la liquidez")
            # Un par sin pool devuelve cero, que es lo que devuelve la cadena
            # para una clave que no se ha inicializado nunca.
            return "0x" + _palabra_uint(0 if pool is None else pool.liquidez)
        if data.startswith(abi.SELECTOR_GET_SLOT0):
            pool, tramo = self._pool_de(data, "el estado")
            if pool is None:
                raise AssertionError(
                    f"se pidió el estado del pool del tramo {tramo}, que en el "
                    f"escenario no tiene pool: el motor sólo debe leer el precio "
                    f"de un pool que haya pasado el filtro de liquidez."
                )
            declarada = pool.lp_fee if isinstance(pool, _PoolFalso) else None
            return _slot0(pool.sqrt_price_x96, tramo if declarada is None else declarada)
        if data.startswith(abi.SELECTOR_QUOTE_EXACT_INPUT_SINGLE):
            return self._cotiza_tramo(data)
        if data.startswith(abi.SELECTOR_QUOTE_EXACT_INPUT):
            return self._cotiza_camino(data)
        return None


def _pool(
    tramo: int,
    *,
    liquidez: int | None = None,
    salida_raw: int | None = None,
    sqrt_price_x96: int | None = None,
    lp_fee: int | None = None,
) -> _PoolFalso:
    """Un pool falso con los valores medidos de ese tramo, salvo lo que se cambie."""
    medidos = {
        500: (LIQUIDEZ_500, SALIDA_500, SQRT_PRICE_500),
        3_000: (LIQUIDEZ_3000, SALIDA_3000, SQRT_PRICE_3000),
    }
    liquidez_medida, salida_medida, precio_medido = medidos.get(
        tramo, (LIQUIDEZ_500, SALIDA_500, SQRT_PRICE_500)
    )
    return _PoolFalso(
        liquidez=liquidez_medida if liquidez is None else liquidez,
        sqrt_price_x96=precio_medido if sqrt_price_x96 is None else sqrt_price_x96,
        salida_raw=salida_medida if salida_raw is None else salida_raw,
        lp_fee=lp_fee,
    )


def _motor(lector: _Lector) -> v4.UniswapV4Engine:
    # El doble cumple la superficie que el motor usa del lector, y el motor lo
    # acepta por inyección precisamente para que esto sea posible.
    return v4.UniswapV4Engine(clock=FrozenClock(AHORA), reader=lector)  # type: ignore[arg-type]


def _cotizacion(
    *,
    chain_key: str = "base",
    fee: int = 500,
    salida: int = SALIDA_500,
    base: Token | None = None,
    quote: Token | None = None,
    amount_in: TokenAmount | None = None,
) -> Quote:
    lado_base = base or _usdc(chain_key)
    lado_quote = quote or _weth(chain_key)
    return Quote(
        venue=v4._venue(chain_key, fee),
        engine_id="uniswap_v4",
        pair=TradingPair(base=lado_base, quote=lado_quote),
        amount_in=amount_in or lado_base.amount("1"),
        amount_out=TokenAmount(salida, lado_quote.decimals, lado_quote.symbol),
        fee_bps=BasisPoints(fee // 100),
        fee_basis=Measurement.DERIVED,
        price_impact_bps=BasisPoints(3),
        impact_basis=Measurement.DERIVED,
        observed_at=AHORA,
    )


def _pregunta_del_tramo(fee: int = 500) -> str:
    """El calldata con el que el motor le pide el precio de un tramo a ese pool.

    Se deriva con la misma función que usa el motor y **en el mismo orden** —lo
    que se tiene y lo que se quiere, base y después quote—, y eso importa más de
    lo que parece: el sentido del intercambio (`zeroForOne`) viaja dentro del
    calldata, así que llamar con los dos argumentos invertidos produce una lectura
    **distinta** que el motor no hace nunca. Una respuesta puesta a mano bajo esa
    clave no se encontraría, y la prueba mediría el doble en vez del motor.

    El par que se pregunta es el de los escenarios —USDC por WETH—, que es el que
    `_par()` arma, y no el par medido del docstring.
    """
    return abi.quote_exact_input_single(
        USDC, WETH, fee, v4._TICK_SPACING_BY_FEE[fee], ENTRADA_RAW
    )


def _vector() -> str:
    """El calldata del vector de oro, con el reloj congelado."""
    return v4.UniswapV4Engine(clock=FrozenClock(moment=AHORA))._calldata(
        fee=500,
        tick_spacing=10,
        token_in=USDC,
        token_out=WETH,
        recipient=DESTINATARIO,
        amount_in_raw=ENTRADA_RAW,
        minimum_raw=MINIMO_DEL_VECTOR,
    )


# --------------------------------------------------------------------------- #
# 1. La codificación
# --------------------------------------------------------------------------- #
def test_cada_firma_produce_el_selector_que_se_buscó_en_la_cadena() -> None:
    """Los selectores salieron de escanear el bytecode desplegado.

    Si alguien cambia una firma, el selector deja de coincidir con el que el
    contrato tiene, y la llamada entra por otra función del mismo contrato.
    """
    assert abi.SELECTOR_EXECUTE == "0x3593564c"
    assert abi.SELECTOR_QUOTE_EXACT_INPUT_SINGLE == "0xaa9d21cb"
    # La firma del camino **no** es la que se recordaba —`0xcdca1753` no existe
    # en el contrato desplegado—: ésta salió de leer los `PUSH4` del
    # despachador, y es la que separa cotizar un camino entero de no cotizarlo.
    assert abi.SELECTOR_QUOTE_EXACT_INPUT == "0xca253dc9"
    assert abi.SELECTOR_GET_SLOT0 == "0xc815641c"
    assert abi.SELECTOR_GET_LIQUIDITY == "0xfa6793d5"


def test_la_comprobacion_de_los_selectores_no_es_decorativa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si una firma se desvía de lo medido, el módulo **no** se importa.

    Sin esta prueba, `_require_measured_selectors` sería una función que se llama
    y no comprueba nada, que es peor que no tenerla: da la sensación de que hay
    una verificación.

    La tabla se sustituye entera en vez de mutarla: es un `Mapping` inmutable a
    propósito —que nadie la edite en caliente es parte de lo que la hace una
    medición—, así que se cambia el atributo del módulo y `monkeypatch` lo deja
    como estaba al acabar, pase lo que pase.
    """
    monkeypatch.setattr(abi, "_MEASURED", {**abi._MEASURED, abi.EXECUTE: "0x00000000"})
    with pytest.raises(EngineConfigError, match="no coincide con la que se midió"):
        abi._require_measured_selectors()


def test_la_tabla_de_acciones_es_la_del_contrato_y_estaba_mal() -> None:
    """Los tres números que se llevaban mal, y por qué importan.

    La tabla que se arrastraba tenía `SETTLE_ALL` en `0x0b` y `TAKE_ALL` en
    `0x0c`, cuando el contrato tiene `SETTLE` en `0x0b`, `SETTLE_ALL` en `0x0c` y
    `TAKE_ALL` en `0x0f`. Se leyeron del `Actions.sol` verificado, no de memoria:
    un número cambiado no da un error de codificación, da una acción distinta —y
    `SETTLE` no es `SETTLE_ALL`, ni `0x0c` es `TAKE_ALL`—, así que el swap podría
    ejecutarse y la deuda quedaría a medias.
    """
    assert abi.ACTION_SWAP_EXACT_IN_SINGLE == 0x06
    #: La del camino: misma familia que la de un solo pool, un número más.
    assert abi.ACTION_SWAP_EXACT_IN == 0x07
    assert abi.ACTION_SETTLE_ALL == 0x0C
    assert abi.ACTION_TAKE == 0x0E
    assert abi.COMMAND_V4_SWAP == 0x10
    #: El cero del importe significa «todo el saldo», no «nada».
    assert abi.OPEN_DELTA == 0


def test_la_clave_del_pool_no_depende_del_orden_en_que_se_pregunte() -> None:
    """`currency0` es la de dirección numéricamente menor, se pase como se pase.

    Invertirlas da el identificador de **otro** pool, y el único aviso sería un
    revert del `PoolManager` que no dice nada de esto.
    """
    for fee, espaciado in FEE_TIERS:
        assert abi.pool_id(WETH, USDC, fee, espaciado) == abi.pool_id(
            USDC, WETH, fee, espaciado
        )
    # Y en este par la menor es WETH, no USDC: el orden no es el alfabético ni el
    # de la lista de tokens, es el valor de la dirección.
    assert abi.sorted_currencies(WETH, USDC) == (WETH, USDC)
    assert int(WETH, 16) < int(USDC, 16)


def test_los_poolid_medidos_son_los_de_la_cadena() -> None:
    """Los cuatro identificadores de WETH/USDC en Base, tal como se midieron.

    La comisión y el espaciado van **dentro** de la clave, así que esto también
    fija que el espaciado canónico de cada tramo es el que se cree: cambiar uno
    cambia el identificador y el motor apuntaría a un pool distinto del que tiene
    el estado.
    """
    for fee, espaciado in FEE_TIERS:
        assert abi.pool_id(WETH, USDC, fee, espaciado) == POOL_ID[fee]
    assert dict(v4._TICK_SPACING_BY_FEE) == {100: 1, 500: 10, 3_000: 60, 10_000: 200}


def test_el_cotizador_lleva_ocho_palabras_de_cabeza_y_el_swap_nueve() -> None:
    """Los dos structs se parecen y **no** son iguales: contar sale caro.

    `QuoteExactSingleParams` no tiene `amountOutMinimum`, así que su cabeza son
    ocho palabras —el desplazamiento a sí misma y cinco de la clave, más el
    sentido, el importe y el desplazamiento de `hookData`— y la de
    `ExactInputSingleParams` son nueve. Usar la del swap para el cotizador produce
    un revert **vacío**, sin dato: el contrato lee el importe del sitio equivocado
    y no hay selector de error que lo explique. Se cometió, se midió así, y por eso
    se cuentan.
    """
    cotizacion = abi.quote_exact_input_single(USDC, WETH, 500, 10, ENTRADA_RAW)
    swap = abi.swap_exact_in_single(
        USDC, WETH, 500, 10, amount_in_raw=ENTRADA_RAW, amount_out_min_raw=1
    )
    # La cotización es una **llamada** —lleva sus cuatro bytes de selector— y el
    # swap es el parámetro de una acción, así que su cuerpo empieza en el byte
    # cero y no hay nada que saltar. El desplazamiento a `hookData` es el que vale
    # la cabeza entera —lo que sigue a la clave, el sentido, el importe y, en el
    # swap, el mínimo—, así que el número está escrito en su última palabra: la
    # novena en la cotización y la décima en el swap.
    assert cotizacion[10 + 8 * 64 : 10 + 9 * 64] == _palabra_uint(8 * 32)
    assert swap[9 * 64 : 10 * 64] == _palabra_uint(9 * 32)
    # Y la diferencia se ve en el tamaño: con el `hookData` vacío, la cotización
    # mide diez palabras además de los ocho dígitos del selector, y el swap once
    # palabras — una más, que es el mínimo.
    assert len(cotizacion[2:]) == 8 + 10 * 64
    assert len(swap) == 11 * 64


def test_los_tres_desplazamientos_anidados_van_donde_el_contrato_los_lee() -> None:
    """`execute` → carga del `V4_SWAP` → acciones y parámetros.

    Son tres niveles y cada uno tiene su propia tabla de desplazamientos,
    **relativa a sí misma**. Un desplazamiento contado desde el principio del
    calldata en vez de desde su propia tabla no revienta al construirse: se manda,
    y el contrato lee una longitud donde había un desplazamiento.
    """
    carga = abi.v4_swap_input(
        (abi.ACTION_SWAP_EXACT_IN_SINGLE, abi.ACTION_SETTLE_ALL, abi.ACTION_TAKE),
        tuple("0x" + _palabra_uint(n) for n in (7, 8, 9)),
    )
    # Nivel 2: dentro de la carga, la cabeza son las dos palabras de los dos
    # desplazamientos y los parámetros empiezan justo detrás de las acciones, que
    # ocupan la palabra de longitud más su contenido relleno a 32 bytes —dos
    # palabras, porque tres bytes no llenan ninguna—.
    assert carga[:64] == _palabra_uint(2 * 32)
    assert carga[64:128] == _palabra_uint(2 * 32 + 2 * 32)

    calldata = abi.execute((abi.COMMAND_V4_SWAP,), (carga,), 1_791_289_800)
    # Nivel 1: la cabeza de `execute` son tres palabras —los dos desplazamientos y
    # el plazo—, y los desplazamientos se cuentan desde ahí.
    assert calldata.startswith(abi.SELECTOR_EXECUTE)
    assert calldata[10:74] == _palabra_uint(3 * 32)
    assert calldata[74:138] == _palabra_uint(3 * 32 + 64)  # un comando: longitud + relleno
    assert calldata[138:202] == _palabra_uint(1_791_289_800)


def test_las_acciones_van_empaquetadas_sin_relleno() -> None:
    """Un byte por acción, y ni uno más.

    El contrato recorre `actions.length` y exige que coincida con el número de
    parámetros, así que un byte de relleno no se ignora: revienta con
    `InputLengthMismatch` y el calldata entero se rechaza.
    """
    carga = abi.v4_swap_input(
        (abi.ACTION_SWAP_EXACT_IN_SINGLE, abi.ACTION_SETTLE_ALL, abi.ACTION_TAKE),
        tuple("0x" + _palabra_uint(1) for _ in range(3)),
    )
    # Las acciones son un `bytes` codificado como cualquier otro: la palabra de
    # longitud —con el 3 en su último byte— y el contenido pegado detrás. La cabeza
    # son dos palabras, así que las acciones empiezan en la tercera.
    assert carga[128:192] == _palabra_uint(3)
    assert carga[192 : 192 + 6] == "060c0e"
    # Y el resto de la palabra va a cero: no hay basura detrás de las acciones.
    assert carga[198:256] == "0" * 58


def test_un_numero_de_acciones_y_de_parametros_distinto_no_se_codifica() -> None:
    """Que coincidan lo exige el contrato; comprobarlo aquí evita un revert caro."""
    with pytest.raises(SourceResponseError, match="comandos"):
        abi.execute((abi.COMMAND_V4_SWAP,), (), 1)
    with pytest.raises(SourceResponseError, match="acciones"):
        abi.v4_swap_input((abi.ACTION_SWAP_EXACT_IN_SINGLE,), ())


def test_una_respuesta_truncada_es_sin_dato_y_no_un_cero() -> None:
    """Media palabra no es un número pequeño: es que no hay respuesta.

    Devolver cero ante una respuesta corta publicaría un pool «sin liquidez» que
    en realidad sólo contestó mal, y esa confusión es la que hace que un motor
    parezca funcionar mientras descarta todo. Un cero, en cambio, es un valor
    legítimo y tiene que leerse como tal.
    """
    assert abi.decode_uint("0x" + _palabra_uint(5)) == 5
    assert abi.decode_uint("0x" + _palabra_uint(0)) == 0
    assert abi.decode_uint("0x1234") is None
    assert abi.decode_uint("0x") is None
    assert abi.decode_quote("0x" + _palabra_uint(9)) == 9
    # El retorno medido son **dos** palabras —el importe y una estimación de
    # gas—: el importe va primero, y la segunda no se lee ni se confunde con él.
    assert abi.decode_quote("0x" + _palabra_uint(3) + _palabra_uint(70_983)) == 3
    assert abi.decode_quote("0x") is None
    assert abi.decode_slot0(_slot0(1234, 500)) == (1234, 500)
    # Tres palabras: el precio llega, la comisión contra la que se contrasta no.
    assert abi.decode_slot0("0x" + _palabra_uint(1) * 3) is None


def test_la_comision_del_lp_se_lee_de_la_cuarta_palabra() -> None:
    """`getSlot0` devuelve cuatro palabras desempaquetadas, no una empaquetada.

    El `Slot0` vive empaquetado en el `PoolManager`, pero el `StateView` lo
    devuelve suelto, así que no hay máscaras ni desplazamientos de bits que hacer.
    Leer la palabra equivocada daría por bueno un pool que cobra otra cosa, que es
    lo que la prueba del contraste vigila.
    """
    for tramo in (100, 500, 3_000, 10_000):
        assert abi.decode_slot0(_slot0(1234, tramo)) == (1234, tramo)


# --------------------------------------------------------------------------- #
# 2. El vector de oro
# --------------------------------------------------------------------------- #
def test_el_vector_de_oro_no_cambia_sin_que_alguien_se_entere() -> None:
    """La huella del calldata congelado, y el tamaño del que sí se midió.

    El respaldo de esto está en el docstring del módulo: el payload que produce
    esta misma función se mandó al `UniversalRouter` por `eth_call` desde una
    cartera sin permisos y contestó `AllowanceExpired` de Permit2, que es el
    **último** paso —cobrar—. Llegar hasta ahí exige que los tres
    desplazamientos, el orden de las palabras y la tabla de acciones estén bien.

    Aquí sólo se fija que no cambie. Si esta prueba se cae, lo primero que hay que
    preguntarse no es cómo actualizarla, sino qué se ha tocado de la codificación.
    """
    calldata = _vector()
    assert (len(calldata) - 2) // 2 == 1124
    assert "0x" + keccak(hexstr=calldata[2:]).hex() == KECCAK_DEL_VECTOR

    # Y la estructura, que es lo que enseña algo cuando la huella se caiga: un
    # solo comando, con la acción del `V4_SWAP`, y el plazo del reloj congelado.
    # La cabeza son tres palabras —los dos desplazamientos y el plazo— y el
    # `bytes` de los comandos empieza justo detrás.
    assert calldata.startswith(abi.SELECTOR_EXECUTE)
    assert calldata[10:74] == _palabra_uint(3 * 32)
    assert calldata[74:138] == _palabra_uint(3 * 32 + 64)
    assert calldata[138:202] == _palabra_uint(int(AHORA.timestamp()) + v4.DEADLINE_SECONDS)
    assert calldata[202:266] == _palabra_uint(1)  # longitud de `commands`
    assert calldata[266:268] == f"{abi.COMMAND_V4_SWAP:02x}"


def test_las_tres_acciones_del_vector_van_empaquetadas_en_su_sitio() -> None:
    """Las tres acciones, leídas de su sitio exacto dentro del calldata.

    Es la mitad barata del vector: si alguien reordena las acciones o mete una de
    más, esto lo dice con un número en vez de con una huella distinta.
    """
    calldata = _vector()
    esperado = _palabra_uint(3) + "060c0e" + "0" * 58
    assert calldata.count(esperado) == 1
    assert calldata.find(esperado) > 0


# --------------------------------------------------------------------------- #
# 3. Qué pools existen
# --------------------------------------------------------------------------- #
async def test_el_filtro_es_la_liquidez_y_no_el_precio() -> None:
    """Un pool con precio y sin liquidez no existe para operar.

    Está medido en Base: los tramos del 0,01 % y el 1 % de WETH/USDC tienen
    `sqrtPriceX96` válido y **cero** liquidez. Un filtro por precio —«el precio no
    es cero, luego hay pool»— los daría por buenos, y cotizarlos revienta sin decir
    por qué.
    """
    lector = _Escenario(pools={500: _pool(500, liquidez=0), 3_000: _pool(3_000)})
    cotizaciones = await _motor(lector).quote(_par(), _usdc().amount("1"))

    assert [cotizacion.venue.venue_id for cotizacion in cotizaciones] == [
        "uniswap-v4@30"
    ]
    # Se le pregunta la liquidez a los cuatro tramos, y el que la tiene a cero se
    # descarta **sin llegar a leerle el precio**: el filtro es la liquidez.
    leidas = lector.calls_to(BASE.state_view)
    assert sum(data.startswith(abi.SELECTOR_GET_LIQUIDITY) for data in leidas) == 4
    assert [data for data in leidas if data.startswith(abi.SELECTOR_GET_SLOT0)] == [
        abi.get_slot0(POOL_ID[3_000])
    ]


async def test_sin_ningun_pool_con_liquidez_no_se_cotiza_ni_se_lee_un_precio() -> None:
    """Ninguno de los cuatro tramos tiene liquidez: no hay nada que cotizar.

    Y «ninguno» es también el veredicto de cada par que el par forma con los
    hubs del catálogo, porque desde que hay búsqueda de ruta se cotizan los dos
    caminos: se les pregunta la liquidez —cuatro tramos por par— y el cero corta
    **antes del precio**, que es lo que fija la última comprobación: no se pidió
    ni un `slot0`. Tampoco se llegó a la segunda pata de ningún hub: sin primera
    pata no hay ruta que cotizar.
    """
    lector = _Escenario(pools={})
    assert not await _motor(lector).quote(_par(), _usdc().amount("1"))
    leidas = lector.calls_to(BASE.state_view)
    assert sum(data.startswith(abi.SELECTOR_GET_LIQUIDITY) for data in leidas) == 4 * (
        1 + len(_hubs_de((WETH, USDC)))
    )
    assert not any(data.startswith(abi.SELECTOR_GET_SLOT0) for data in leidas)


async def test_se_queda_con_el_pool_que_mas_da() -> None:
    """Con dos pools vivos se cotiza el que entrega más, y no el primero que salga."""
    lector = _Escenario(
        pools={
            500: _pool(500),
            3_000: _pool(
                3_000,
                salida_raw=SALIDA_500 * 2,
                # Con un precio coherente con esa salida: si no, el impacto la
                # descartaría y la prueba mediría el filtro en vez de la elección.
                sqrt_price_x96=_sqrt_para(
                    ENTRADA_RAW, SALIDA_500 * 2, token_in_is_token0=False
                ),
            ),
        }
    )
    cotizaciones = await _motor(lector).quote(_par(), _usdc().amount("1"))
    assert len(cotizaciones) == 1
    assert cotizaciones[0].venue.venue_id == "uniswap-v4@30"
    assert cotizaciones[0].amount_out.raw == SALIDA_500 * 2


async def test_un_quoter_que_revierte_omite_ese_tramo_y_no_rompe_la_cotizacion() -> None:
    """Un pool que no puede dar salida a este tamaño es una respuesta, no una caída.

    Es la diferencia entre «este pool no cotiza» y «la fuente no contesta», y
    colapsarlas haría que una caída de red se leyera como falta de liquidez.
    """
    lector = _Escenario(pools={500: _pool(500), 3_000: _pool(3_000)})
    lector.handlers[_pregunta_del_tramo(500)] = SourceResponseError("execution reverted")
    cotizaciones = await _motor(lector).quote(_par(), _usdc().amount("1"))
    assert [cotizacion.venue.venue_id for cotizacion in cotizaciones] == ["uniswap-v4@30"]


async def test_un_stateview_que_revierte_aborta_y_no_se_silencia() -> None:
    """El `StateView` no tiene motivo para revertir con un `bytes32` válido.

    Devuelve cero para un pool que no existe. Así que si revierte, la dirección o
    el selector no son los que la tabla dice — y eso hay que verlo antes de firmar
    nada, no convertir el motor en uno que no encuentra pools.
    """
    lector = _Escenario(pools={500: _pool(500)})
    lector.handlers[abi.get_liquidity(POOL_ID[500])] = SourceResponseError(
        "execution reverted"
    )
    with pytest.raises(SourceResponseError):
        await _motor(lector).quote(_par(), _usdc().amount("1"))


async def test_dos_monedas_que_resuelven_a_la_misma_no_son_un_par() -> None:
    """Dos tokens declarados como nativos son la misma moneda, no un par.

    Y esto **no** es el caso de un nativo contra su envoltorio, que es lo que
    podría parecer: en V4 el nativo tiene dirección propia —la cero— y WETH es
    otra moneda, así que ése sí es un par con su pool. Se comprueba el otro lado
    para que la diferencia quede fijada.
    """
    lector = _Escenario()
    otro_nativo = Token(symbol="ETH2", decimals=18, chain="base", address=None)
    assert not await _motor(lector).quote(
        TradingPair(base=_nativo(), quote=otro_nativo), _nativo().amount("1")
    )
    assert lector.calls == []
    # Y nativo contra WETH sí que se cotiza: son dos monedas distintas.
    lector = _Escenario(par=(CERO, WETH))
    assert await _motor(lector).quote(
        TradingPair(base=_nativo(), quote=_weth()), _nativo().amount("0.001")
    )


async def test_en_una_red_sin_contratos_medidos_no_se_cotiza() -> None:
    """Ni se pregunta: sin despliegues no hay dirección a la que llamar."""
    lector = _Escenario()
    par = TradingPair(base=_usdc("solana"), quote=_weth("solana"))
    assert not await _motor(lector).quote(par, _usdc("solana").amount("1"))
    assert lector.calls == []


async def test_los_venues_son_los_cuatro_tramos_que_se_sondean() -> None:
    """Se listan los tramos que se preguntan, no los pools que existen.

    En V4 cuáles existen depende del par y del espaciado con el que alguien los
    inicializara, y eso sólo se sabe derivando la clave de cada uno: preguntarlo
    al dibujar la lista costaría cuatro lecturas por dibujo. Los `venue_id` de
    esta lista son exactamente los que llevan las cotizaciones.
    """
    venues = await _motor(_Escenario()).venues("base")
    assert [venue.venue_id for venue in venues] == [
        "uniswap-v4@1",
        "uniswap-v4@5",
        "uniswap-v4@30",
        "uniswap-v4@100",
    ]
    assert not await _motor(_Escenario()).venues("solana")


# --------------------------------------------------------------------------- #
# 4. La comisión que declara el pool
# --------------------------------------------------------------------------- #
async def test_un_pool_que_declara_otra_comision_aborta() -> None:
    """La comisión va dentro de la clave, así que si no coincide se lee otro sitio.

    Omitir el pool sería peor que abortar: convertiría una tabla de direcciones
    equivocada en un motor que simplemente no encuentra nada, que es el fallo mudo
    que este motor no se puede permitir. El mensaje tiene que decir de dónde
    sospecha, porque el número solo no lleva a ninguna parte.
    """
    lector = _Escenario(pools={500: _pool(500, lp_fee=3_000)})
    with pytest.raises(SourceResponseError, match="StateView"):
        await _motor(lector).quote(_par(), _usdc().amount("1"))


async def test_la_comision_que_se_publica_es_la_del_tramo_en_puntos_basicos() -> None:
    """`venue_for` escribe puntos básicos; la clave quiere centésimas.

    El mismo pool es `3000` en la clave y `30` en el `venue_id`. Confundir las
    unidades apuntaría al pool del 0,01 %, que en la mayoría de los pares ni
    existe, y el fallo aparecería como un revert del `PoolManager` sin relación
    aparente con la causa.
    """
    motor = _motor(_Escenario(pools={3_000: _pool(3_000)}))
    cotizacion = (await motor.quote(_par(), _usdc().amount("1")))[0]
    assert cotizacion.venue.venue_id == "uniswap-v4@30"
    assert cotizacion.fee_bps == BasisPoints(30)
    assert cotizacion.fee_basis is Measurement.DERIVED


# --------------------------------------------------------------------------- #
# 5. El nativo es una moneda
# --------------------------------------------------------------------------- #
def test_el_nativo_va_como_la_direccion_cero_y_no_como_su_envoltorio() -> None:
    """La diferencia con V3, y es deliberada.

    En V3 el nativo no existe para el AMM y se sustituye por WETH. En V4 el nativo
    **es** una moneda —se representa como la dirección cero y hay pools de nativo,
    medidos en las siete redes—, así que sustituirlo por el envoltorio llevaría al
    pool equivocado en vez de al que corresponde: pagar con ETH usaría el pool de
    WETH, que es otra cosa. Y no devuelve `None` nunca: todo token tiene dirección,
    porque «nativo» **es** «sin dirección» y el nativo tiene la suya.
    """
    assert v4._on_chain_address(_nativo()) == CERO
    assert v4._on_chain_address(_nativo()) != WETH
    assert v4._on_chain_address(_weth()) == WETH


async def test_comprar_con_el_nativo_lleva_el_importe_y_no_pide_permiso() -> None:
    """Pagar en nativo contra un pool de nativo no envuelve nada.

    El `PoolManager` cobra del `value` que viaja con la transacción y entrega el
    nativo directamente, así que no hay permiso que dar: ni el del token a Permit2
    ni el de Permit2 al router.
    """
    lector = _Escenario(
        par=(CERO, WETH),
        pools={
            500: _pool(
                500, sqrt_price_x96=_sqrt_para(10**15, SALIDA_500, token_in_is_token0=True)
            )
        },
    )
    motor = _motor(lector)
    cotizaciones = await motor.quote(
        TradingPair(base=_nativo(), quote=_weth()), _nativo().amount("0.001")
    )
    assert cotizaciones
    payload = await motor.plan_swap(cotizaciones[0], recipient=DESTINATARIO)
    assert payload.value.raw == 10**15
    assert payload.value.symbol == chain("base").native_symbol
    assert payload.approval is None
    assert "no se envuelve nada" in payload.description


async def test_recibir_el_nativo_no_desenvuelve_nada() -> None:
    """El nativo sale del `PoolManager` sin pasar por un envoltorio que romper."""
    lector = _Escenario(
        par=(WETH, CERO),
        pools={
            500: _pool(
                500,
                sqrt_price_x96=_sqrt_para(10**15, SALIDA_500, token_in_is_token0=False),
            )
        },
    )
    motor = _motor(lector)
    cotizaciones = await motor.quote(
        TradingPair(base=_weth(), quote=_nativo()), _weth().amount("0.001")
    )
    assert cotizaciones
    payload = await motor.plan_swap(cotizaciones[0], recipient=DESTINATARIO)
    assert payload.value.raw == 0
    assert "sin pasar por un envoltorio" in payload.description


async def test_un_no_nativo_si_pide_los_dos_permisos_encadenados() -> None:
    """El Universal Router no mueve el token él mismo: se lo pide a Permit2.

    Así que un swap con token exige **dos** permisos y ninguno sustituye al otro.
    Medido: intentar el swap sin ellos devuelve `AllowanceExpired` de Permit2, no
    un error del router — que es lo que confirma que la orden llegó a cobrar.
    """
    payload = await _motor(_Escenario()).plan_swap(_cotizacion(), recipient=DESTINATARIO)
    assert payload.approval is not None
    assert payload.approval.spender == BASE.universal_router
    assert payload.approval.via == PERMIT2


async def test_un_declarado_de_decimales_distinto_del_nativo_no_se_firma() -> None:
    """Un importe nativo con decimales equivocados sería otro importe.

    El `value` de la transacción se escribe en unidades mínimas del nativo de la
    red, así que un token que se declare con otros decimales mandaría una cantidad
    distinta de la que dice el payload. Se corta antes de construir.
    """
    lector = _Escenario(
        par=(CERO, WETH),
        pools={
            500: _pool(
                500, sqrt_price_x96=_sqrt_para(10**15, SALIDA_500, token_in_is_token0=True)
            )
        },
    )
    motor = _motor(lector)
    malo = _nativo(decimales=6)
    cotizaciones = await motor.quote(
        TradingPair(base=malo, quote=_weth()), TokenAmount(10**15, 6, malo.symbol)
    )
    assert cotizaciones
    with pytest.raises(InvalidAmountError, match="decimales"):
        await motor.plan_swap(cotizaciones[0], recipient=DESTINATARIO)


# --------------------------------------------------------------------------- #
# 6. El destinatario
# --------------------------------------------------------------------------- #
async def test_el_destinatario_va_escrito_en_la_orden() -> None:
    """En V4 el contrato **no** lo deduce del firmante: hay que escribirlo.

    Y esto es lo que obliga a usar `TAKE` en vez de `TAKE_ALL`: `TAKE_ALL` paga
    siempre a quien llama a `execute`, así que no podría respetar un destinatario
    declarado.
    """
    payload = await _motor(_Escenario()).plan_swap(_cotizacion(), recipient=DESTINATARIO)
    assert _palabra_addr(DESTINATARIO) in payload.calldata
    assert _palabra_addr(DESTINATARIO) != _palabra_addr(BASE.universal_router)


def test_los_centinelas_del_contrato_no_valen_como_destinatario() -> None:
    """Son direcciones válidas que el contrato traduce a otra cosa.

    `1` es «quien firma» y `2` es «el propio router»: una orden que los declarara
    entregaría los fondos a un sitio distinto del que dice. Cualquier validación
    de direcciones los daría por buenos, así que se rechazan aquí.
    """
    for centinela in ("0x" + "00" * 19 + "01", "0x" + "00" * 19 + "02"):
        with pytest.raises(SourceResponseError, match="destinatario en V4"):
            abi.take(WETH, centinela)
    # Y una dirección normal, aunque sea pequeña, sí vale.
    assert abi.take(WETH, "0x" + "00" * 19 + "03")


def test_la_accion_de_cobro_lleva_el_cero_que_significa_todo() -> None:
    """`OPEN_DELTA` en el importe cobra el saldo íntegro, no cero.

    La protección de precio no se pierde por no usar `TAKE_ALL`: el
    `amountOutMinimum` viaja dentro de `SWAP_EXACT_IN_SINGLE`, que es donde el
    importe se conoce — y después de un solo intercambio el saldo y el importe son
    el mismo número.
    """
    assert abi.take(WETH, DESTINATARIO) == (
        _palabra_addr(WETH) + _palabra_addr(DESTINATARIO) + _palabra_uint(0)
    )
    assert len(abi.take(WETH, DESTINATARIO)) == 3 * 64  # tres campos estáticos


# --------------------------------------------------------------------------- #
# 7. El venue y el camino de vuelta
# --------------------------------------------------------------------------- #
def test_el_venue_lleva_el_tramo_dentro_y_se_puede_deshacer() -> None:
    """`_venue` y `_tier_from_venue` son el mismo número en dos unidades.

    Construir con otro espaciado apuntaría a otro pool —o a ninguno— sin que nada
    chirriara hasta el revert, así que el camino de vuelta tiene que devolver la
    pareja completa y no sólo la comisión.
    """
    for fee, espaciado in FEE_TIERS:
        assert v4._tier_from_venue(v4._venue("base", fee).venue_id) == (fee, espaciado)


def test_el_venue_de_v4_no_se_confunde_con_el_de_v3() -> None:
    """El mismo par y el mismo tramo son dos sitios distintos donde operar.

    El identificador del protocolo lleva la versión, así que salen en dos filas en
    vez de pisarse la una a la otra.
    """
    from amigocompora.engines.uniswap_v3 import engine as v3

    assert v4._venue("base", 3_000).venue_id == "uniswap-v4@30"
    assert v3._venue("base", 3_000).venue_id == "uniswap-v3@30"
    assert v4._venue("base", 3_000).venue_id != v3._venue("base", 3_000).venue_id


def test_un_venue_de_otro_motor_o_de_otro_tramo_no_se_acepta() -> None:
    """Suponer un espaciado para un tramo desconocido apuntaría a otro pool.

    Y un `venue_id` que no sea de este motor es una cotización que no se puede
    construir: se rechaza en vez de adivinar con qué espaciado se derivó su clave.
    """
    for ajeno in ("uniswap-v3@30", "uniswap-v4@", "ethereum@uniswap-v4@30", "uniswap-v4@x"):
        with pytest.raises(UnsupportedOperationError, match="no es un venue"):
            v4._tier_from_venue(ajeno)
    with pytest.raises(UnsupportedOperationError, match="no sondea"):
        v4._tier_from_venue("uniswap-v4@7")


# --------------------------------------------------------------------------- #
# 8. Construir
# --------------------------------------------------------------------------- #
async def test_el_minimo_es_el_de_ahora_y_nunca_por_encima_de_lo_que_el_pool_da() -> None:
    """`amountOutMinimum` sale de la cotización **fresca**, no de la de pantalla.

    Y se redondea hacia abajo, como el SDK de Uniswap: un mínimo por encima de lo
    que el pool entrega de verdad hace que la transacción revierta y el gas se
    pierda. Se comprueba con la cuenta hecha aparte, no copiando la del motor.
    """
    a_mano = (Decimal(SALIDA_500) * (Decimal(1) - Decimal("0.005"))).to_integral_value(
        rounding=ROUND_DOWN
    )
    assert a_mano == MINIMO_DEL_VECTOR
    payload = await _motor(_Escenario()).plan_swap(_cotizacion(), recipient=DESTINATARIO)
    assert _palabra_uint(MINIMO_DEL_VECTOR) in payload.calldata
    assert Decimal("0.5") == v4.SLIPPAGE_PCT


async def test_si_el_precio_se_movio_mas_de_lo_tolerado_no_se_construye() -> None:
    """El payload tiene que corresponder al precio que el usuario leyó.

    Se tolera un 1 %: por debajo el mercado se mueve solo; por encima, el usuario
    estaría firmando una operación distinta de la que vio.
    """
    lector = _Escenario(pools={500: _pool(500, salida_raw=int(SALIDA_500 * 1.05))})
    with pytest.raises(QuoteMovedError):
        await _motor(lector).plan_swap(_cotizacion(), recipient=DESTINATARIO)


async def test_un_movimiento_pequeno_del_precio_no_impide_construir() -> None:
    """Un movimiento dentro de la tolerancia es el mercado funcionando."""
    lector = _Escenario(pools={500: _pool(500, salida_raw=int(SALIDA_500 * 1.005))})
    payload = await _motor(lector).plan_swap(_cotizacion(), recipient=DESTINATARIO)
    assert payload.calldata


async def test_si_el_pool_ya_no_cotiza_se_dice_que_vuelva_a_cotizar() -> None:
    """La liquidez se agota entre que se pinta la tabla y se pulsa el botón."""
    lector = _Escenario()
    lector.handlers[_pregunta_del_tramo(500)] = SourceResponseError("execution reverted")
    with pytest.raises(NoQuotesError, match="ya no cotiza"):
        await _motor(lector).plan_swap(_cotizacion(), recipient=DESTINATARIO)


async def test_el_payload_caduca_donde_el_router_admite_caducidad() -> None:
    """`execute` lleva el plazo en **todas** las redes, así que nunca sobra.

    Es la diferencia con V3, donde sólo lo tenían las redes con SwapRouter V1: una
    orden sin caducidad puede ejecutarse mucho después del precio que se miró.
    """
    payload = await _motor(_Escenario()).plan_swap(_cotizacion(), recipient=DESTINATARIO)
    assert _palabra_uint(int(AHORA.timestamp()) + v4.DEADLINE_SECONDS) in payload.calldata


async def test_el_payload_no_lleva_limite_de_gas_y_eso_es_deliberado() -> None:
    """Estimar el gas necesita la dirección que paga, y el motor no la conoce.

    Además, lo único que vale es la estimación contra el estado del momento, que
    la hace quien firma.
    """
    payload = await _motor(_Escenario()).plan_swap(_cotizacion(), recipient=DESTINATARIO)
    assert payload.gas_limit is None


async def test_el_destino_declarado_es_el_universal_router_de_la_tabla() -> None:
    """El contraste contra el que el camino de ejecución comprueba antes de firmar.

    Se lee de la tabla en vez de escribirse aparte porque es la misma pregunta, y
    dos listas acabarían discrepando — que es como se firma hacia un contrato que
    no es el que se cree.
    """
    motor = _motor(_Escenario())
    payload = await motor.plan_swap(_cotizacion(), recipient=DESTINATARIO)
    assert motor.expected_destination("base") == BASE.universal_router
    assert payload.to_address == motor.expected_destination("base")
    assert payload.chain_id == chain("base").require_eip155_id()
    assert motor.expected_destination("solana") is None


async def test_construir_en_una_red_sin_contratos_medidos_es_un_error_explicito() -> None:
    """Un motor no debe construir donde no ha medido los contratos.

    Y el error tiene que decir qué redes sí cubre, porque el usuario no puede
    adivinarlo desde «no soportado».
    """
    with pytest.raises(UnsupportedOperationError, match="no están medidos ahí"):
        await _motor(_Escenario()).plan_swap(
            _cotizacion(chain_key="solana", base=_usdc("solana"), quote=_weth("solana")),
            recipient=DESTINATARIO,
        )


async def test_una_cotizacion_cuyo_par_es_una_sola_moneda_no_se_construye() -> None:
    """Un swap de una moneda contra sí misma no es un swap.

    La cotización se puede construir a mano —el camino de ejecución acepta
    cualquiera—, así que la comprobación no puede vivir sólo en `quote`.
    """
    motor = _motor(_Escenario(par=(CERO, CERO)))
    par = TradingPair(base=_nativo(), quote=Token("ETH2", 18, "base", None))
    with pytest.raises(UnsupportedOperationError, match="una sola moneda"):
        await motor.plan_swap(
            _cotizacion(base=par.base, quote=par.quote), recipient=DESTINATARIO
        )


async def test_la_descripcion_dice_lo_que_el_usuario_tiene_que_saber_antes_de_firmar() -> None:
    """Lo que se firma es irreversible, y la descripción es lo que se lee.

    Tiene que nombrar el venue, las dos cifras —lo que sale y el mínimo—, el
    deslizamiento tolerado, el contrato al que se manda, los dos permisos que
    hacen falta y la caducidad.
    """
    payload = await _motor(_Escenario()).plan_swap(_cotizacion(), recipient=DESTINATARIO)
    texto = payload.description
    assert "Uniswap V4" in texto
    assert "Universal Router" in texto
    assert BASE.universal_router in texto
    assert PERMIT2 in texto
    assert "dos" in texto
    assert "permisos" in texto
    assert "caduca en 30 minutos" in texto
    assert "0.5 %" in texto
    assert "0.05 %" in texto  # el tramo por el que se ejecuta
    # Y no promete nada que no vaya a pasar: el destinatario va en la orden.
    assert "no lo deduce del firmante" in texto


# --------------------------------------------------------------------------- #
# 9. El segundo salto: la ruta por un hub
# --------------------------------------------------------------------------- #
#: El token que abre el caso. No está en el catálogo de ninguna red, así que su
#: par con USDC no tiene pool directo en ningún tramo y la única vía es pasar
#: por un hub. Su dirección es **mayor** que la de WETH —y que la de cbBTC—, y
#: eso decide el sentido (`zeroForOne`) de sus tramos: es la mitad del contrato
#: de este escenario.
NEW = "0x" + "9" * 40
ENTRADA_NEW = 10**18
#: Lo que da cada pata del camino: 1 NEW → 0,5 WETH → 1.000 USDC.
PATA_WETH = 5 * 10**17
PATA_USDC = 1_000 * 10**6
#: Los marginales de las dos patas. El de NEW/WETH sale de 1/0,51 WETH por NEW
#: —un precio por encima de lo que dio la pata, que es la dirección en la que la
#: orden se paga— y el de WETH/USDC, de 2,02·10⁻⁹.
SQRT_NEW_WETH = _sqrt_de(Decimal(1) / Decimal("0.51"))
SQRT_WETH_USDC = _sqrt_de(Decimal("2.02e-9"))
#: El camino que el motor publica: por WETH al 0,30 %, y de ahí a USDC al 0,05 %.
LLAVES_NEW_USDC = (
    abi.PathKey(currency=WETH, fee=3_000, tick_spacing=60),
    abi.PathKey(currency=USDC, fee=500, tick_spacing=10),
)
CAMINO_NEW_USDC = abi.quote_exact_input(NEW, LLAVES_NEW_USDC, ENTRADA_NEW)
#: El segundo hub del catálogo que compite, con sus cifras: 0,05 cbBTC por la
#: primera pata y 1.100 USDC por la segunda —más que los 1.000 del WETH—.
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
PATA_CBBTC = 5 * 10**6
PATA_CBBTC_USDC = 1_100 * 10**6


def _new() -> Token:
    """El token del escenario: uno que no está en el catálogo de ninguna red."""
    return _token(NEW, "NEW", 18)


def _hub(simbolo: str) -> Token:
    """El token del catálogo de Base que hace de escala.

    Se busca en el **catálogo** —el mismo que consulta la búsqueda de ruta— y
    no se copia a mano: una dirección copiada aquí describiría un mundo que la
    búsqueda no visita, y la prueba mediría el doble en vez del motor.
    """
    for token in hub_tokens("base"):
        if token.symbol == simbolo:
            return token
    raise AssertionError(f"el catálogo de Base ya no tiene «{simbolo}»")


def _par_new_usdc() -> TradingPair:
    return TradingPair(base=_new(), quote=_usdc())


def _escenario_del_camino() -> _Escenario:
    """El caso: NEW no tiene pool directo con USDC y la ruta pasa por WETH.

    La segunda pata se declara por el importe que dio la primera —`PATA_WETH`,
    no la entrada original—: si el motor la cotizara con otro tamaño, la tabla
    no la encontraría y no habría ruta, que es exactamente lo que hay que
    distinguir.
    """
    return _Escenario(
        par=(NEW, USDC),
        pools={},
        hub_pools={
            (NEW, WETH, 3_000): _PoolDeRuta(LIQUIDEZ_3000, SQRT_NEW_WETH),
            (WETH, USDC, 500): _PoolDeRuta(LIQUIDEZ_500, SQRT_WETH_USDC),
        },
        hub_salidas={
            (NEW, WETH, 3_000, ENTRADA_NEW): PATA_WETH,
            (WETH, USDC, 500, PATA_WETH): PATA_USDC,
        },
        caminos={CAMINO_NEW_USDC: PATA_USDC},
    )


def _escenario_de_dos_hubs() -> _Escenario:
    """Dos hubs compiten: el WETH da 1.000 USDC y el cbBTC, 1.100.

    El cbBTC va **después** del WETH en el catálogo, así que la ganadora no es
    la primera que se sondea: es la que más da por la segunda pata, que es lo
    que de verdad recibe el usuario.
    """
    return _Escenario(
        par=(NEW, USDC),
        pools={},
        hub_pools={
            (NEW, WETH, 3_000): _PoolDeRuta(LIQUIDEZ_3000, SQRT_NEW_WETH),
            (WETH, USDC, 500): _PoolDeRuta(LIQUIDEZ_500, SQRT_WETH_USDC),
            (NEW, CBBTC, 10_000): _PoolDeRuta(
                LIQUIDEZ_3000,
                _sqrt_para(ENTRADA_NEW, PATA_CBBTC, token_in_is_token0=True),
            ),
            (CBBTC, USDC, 10_000): _PoolDeRuta(
                LIQUIDEZ_500,
                _sqrt_para(PATA_CBBTC, PATA_CBBTC_USDC, token_in_is_token0=False),
            ),
        },
        hub_salidas={
            (NEW, WETH, 3_000, ENTRADA_NEW): PATA_WETH,
            (WETH, USDC, 500, PATA_WETH): PATA_USDC,
            (NEW, CBBTC, 10_000, ENTRADA_NEW): PATA_CBBTC,
            (CBBTC, USDC, 10_000, PATA_CBBTC): PATA_CBBTC_USDC,
        },
    )


def test_las_direcciones_del_escenario_son_las_del_catalogo() -> None:
    """Si el catálogo cambia una dirección, el escenario tiene que cambiar con él.

    Los hubs se buscan por nombre en el catálogo, así que esta prueba no repite
    esa búsqueda: fija que los nombres existen y que las direcciones que el
    escenario escribe a mano son las que el motor va a visitar.
    """
    assert v4._on_chain_address(_hub("WETH")) == WETH
    assert v4._on_chain_address(_hub("USDC")) == USDC
    assert v4._on_chain_address(_hub("cbBTC")) == CBBTC


async def test_si_el_par_directo_cotiza_no_se_buscan_rutas() -> None:
    """La ruta no compite con el pool directo: sólo entra cuando no hay ninguno."""
    lector = _Escenario()
    cotizaciones = await _motor(lector).quote(_par(), _usdc().amount("1"))

    assert len(cotizaciones) == 1
    assert cotizaciones[0].route is None
    # Cuatro lecturas de liquidez, las del par directo: ninguna pareja de hub
    # llegó a preguntarse.
    leidas = lector.calls_to(BASE.state_view)
    assert sum(data.startswith(abi.SELECTOR_GET_LIQUIDITY) for data in leidas) == 4


async def test_una_ruta_de_dos_saltos_cotiza_el_camino_entero() -> None:
    """El par sin pool directo cae a la ruta por hub, y la publica dentro."""
    lector = _escenario_del_camino()
    par = _par_new_usdc()
    cotizaciones = await _motor(lector).quote(par, _new().amount("1"))

    assert len(cotizaciones) == 1
    cotizacion = cotizaciones[0]
    assert cotizacion.venue.venue_id == "uniswap-v4@30+5"
    assert cotizacion.venue.name == "Uniswap V4 0.3 % + 0.05 %"
    assert cotizacion.fee_bps == BasisPoints(35)
    assert cotizacion.fee_basis is Measurement.DERIVED
    assert cotizacion.impact_basis is Measurement.DERIVED
    assert cotizacion.liquidity is None
    assert cotizacion.amount_out.raw == PATA_USDC
    assert cotizacion.amount_out.symbol == "USDC"

    assert cotizacion.route is not None
    assert [token.symbol for token in cotizacion.route.tokens] == ["NEW", "WETH", "USDC"]
    assert [hop.fee_bps for hop in cotizacion.route.hops] == [
        BasisPoints(30),
        BasisPoints(5),
    ]
    assert cotizacion.route.total_fee_bps == BasisPoints(35)

    # El impacto es la composición de los dos tramos, con los marginales que el
    # doble dio a cada pata —incluida la segunda, cotizada con lo que dio la
    # primera— y no la suma ni el impacto de un solo pool.
    primera = impact_bps(
        amount_in_raw=ENTRADA_NEW,
        amount_out_raw=PATA_WETH,
        sqrt_price_x96=SQRT_NEW_WETH,
        # NEW es mayor que WETH, así que la moneda 0 es WETH y la que entra no
        # lo es: el precio divide.
        token_in_is_token0=False,
        fee=BasisPoints(30),
    )
    segunda = impact_bps(
        amount_in_raw=PATA_WETH,
        amount_out_raw=PATA_USDC,
        sqrt_price_x96=SQRT_WETH_USDC,
        # Aquí WETH sí es la moneda 0 —es menor que USDC—: el precio multiplica.
        token_in_is_token0=True,
        fee=BasisPoints(5),
    )
    assert primera is not None
    assert segunda is not None
    assert cotizacion.price_impact_bps == combine_impact_bps([primera, segunda])
    assert cotizacion.price_impact_bps.value < primera.value + segunda.value
    assert 0 < cotizacion.price_impact_bps.value < 500

    assert "dos saltos" in cotizacion.source_note
    assert "NEW → WETH → USDC" in cotizacion.source_note


async def test_gana_la_ruta_que_mas_da_aunque_su_hub_no_sea_el_primero() -> None:
    """Las rutas compiten por la salida del segundo tramo, no por el orden."""
    cotizaciones = await _motor(_escenario_de_dos_hubs()).quote(
        _par_new_usdc(), _new().amount("1")
    )

    assert len(cotizaciones) == 1
    cotizacion = cotizaciones[0]
    assert cotizacion.amount_out.raw == PATA_CBBTC_USDC
    assert cotizacion.venue.venue_id == "uniswap-v4@100+100"
    assert cotizacion.venue.name == "Uniswap V4 1 % + 1 %"
    assert cotizacion.route is not None
    assert [token.symbol for token in cotizacion.route.tokens] == ["NEW", "cbBTC", "USDC"]


async def test_la_ruta_se_ejecuta_en_un_solo_swap_con_el_camino_publicado() -> None:
    """Construir vuelve a cotizar **ese** camino, no busca otro que diera más.

    El doble sólo contesta al camino exacto que el motor publicó y con el
    importe exacto: si al construir se buscara de nuevo, o se cambiara un tramo
    o el tamaño, la lectura no casaría y la prueba lo vería. La comparación del
    calldata es entera porque es lo que se firma: las mismas tres acciones
    dentro de un comando, con la del camino (`SWAP_EXACT_IN`) en vez de la de
    un salto.
    """
    lector = _escenario_del_camino()
    motor = _motor(lector)
    par = _par_new_usdc()
    cotizacion = (await motor.quote(par, _new().amount("1")))[0]
    payload = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    minimo = (Decimal(PATA_USDC) * (Decimal(1) - Decimal("0.005"))).to_integral_value(
        rounding=ROUND_DOWN
    )
    assert payload.calldata == abi.execute(
        (abi.COMMAND_V4_SWAP,),
        (
            abi.v4_swap_input(
                (abi.ACTION_SWAP_EXACT_IN, abi.ACTION_SETTLE_ALL, abi.ACTION_TAKE),
                (
                    abi.swap_exact_in_path(
                        NEW,
                        LLAVES_NEW_USDC,
                        amount_in_raw=ENTRADA_NEW,
                        amount_out_min_raw=int(minimo),
                    ),
                    abi.settle_all(NEW, ENTRADA_NEW),
                    abi.take(USDC, DESTINATARIO, abi.OPEN_DELTA),
                ),
            ),
        ),
        int(AHORA.timestamp()) + v4.DEADLINE_SECONDS,
    )
    assert payload.to_address == BASE.universal_router
    assert payload.value.raw == 0
    assert payload.approval is not None
    assert payload.approval.spender == BASE.universal_router
    assert payload.approval.via == PERMIT2


async def test_la_descripcion_de_una_ruta_dice_por_donde_pasa() -> None:
    """Lo que el usuario tiene que leer antes de firmar: el camino y los tramos."""
    motor = _motor(_escenario_del_camino())
    par = _par_new_usdc()
    cotizacion = (await motor.quote(par, _new().amount("1")))[0]
    payload = await motor.plan_swap(cotizacion, recipient=DESTINATARIO)

    texto = payload.description
    assert "un solo swap" in texto
    assert "dos tramos" in texto
    assert "0.3 % y 0.05 %" in texto
    assert "NEW → WETH → USDC" in texto
    assert BASE.universal_router in texto


async def test_si_el_camino_se_movio_mas_de_lo_tolerado_no_se_construye() -> None:
    """La deriva se mide contra la cotización fresca del mismo camino."""
    lector = _escenario_del_camino()
    motor = _motor(lector)
    par = _par_new_usdc()
    cotizacion = (await motor.quote(par, _new().amount("1")))[0]
    lector.handlers[CAMINO_NEW_USDC] = _respuesta_quoter(int(PATA_USDC * 1.05))

    with pytest.raises(QuoteMovedError):
        await motor.plan_swap(cotizacion, recipient=DESTINATARIO)


async def test_si_el_camino_ya_no_cotiza_se_dice_por_donde_iba() -> None:
    """La liquidez se agota entre la tabla y el botón, también en una ruta."""
    lector = _escenario_del_camino()
    lector.handlers[CAMINO_NEW_USDC] = SourceResponseError("execution reverted")
    motor = _motor(lector)
    par = _par_new_usdc()
    cotizacion = (await motor.quote(par, _new().amount("1")))[0]

    with pytest.raises(NoQuotesError, match="NEW → WETH → USDC"):
        await motor.plan_swap(cotizacion, recipient=DESTINATARIO)


async def test_una_ruta_que_no_corresponde_al_par_no_se_construye() -> None:
    """Un camino que no empiece o no termine donde el par ejecutaría otro swap."""
    motor = _motor(_Escenario(par=(NEW, USDC)))
    ajena = SwapRoute(
        hops=(
            RouteHop(base=_new(), quote=_weth(), fee_bps=BasisPoints(30)),
            RouteHop(base=_weth(), quote=_hub("cbBTC"), fee_bps=BasisPoints(5)),
        )
    )
    cotizacion = Quote(
        venue=v4._route_venue("base", (3_000, 500)),
        engine_id="uniswap_v4",
        pair=_par_new_usdc(),
        amount_in=_new().amount("1"),
        amount_out=TokenAmount(PATA_USDC, 6, "USDC"),
        fee_bps=BasisPoints(35),
        fee_basis=Measurement.DERIVED,
        price_impact_bps=BasisPoints(5),
        impact_basis=Measurement.DERIVED,
        observed_at=AHORA,
        route=ajena,
    )
    with pytest.raises(UnsupportedOperationError, match="no corresponde al par"):
        await motor.plan_swap(cotizacion, recipient=DESTINATARIO)


def test_el_camino_de_la_ruta_va_en_claves_de_v4() -> None:
    """De la ruta publicada a las claves: la moneda es la de **llegada**.

    Cada `PathKey` nombra la moneda a la que llega su tramo, y escribirla al
    revés deriva la clave de otro pool —o de ninguno— sin que nada chirríe
    hasta el revert. El espaciado sale de la tabla canónica que el motor
    sondea, porque el `venue_id` sólo lleva la comisión.
    """
    ruta = SwapRoute(
        hops=(
            RouteHop(base=_new(), quote=_weth(), fee_bps=BasisPoints(30)),
            RouteHop(base=_weth(), quote=_usdc(), fee_bps=BasisPoints(5)),
        )
    )
    claves = _motor(_Escenario(par=(NEW, USDC)))._route_path(
        ruta, token_in=NEW, token_out=USDC
    )

    assert claves == LLAVES_NEW_USDC
    assert claves[0].currency == WETH  # la de llegada del primer tramo
    assert claves[0].tick_spacing == 60
    assert claves[1].currency == USDC
    assert claves[1].tick_spacing == 10


def test_una_ruta_sin_comisiones_no_se_puede_construir() -> None:
    """Sin la comisión de cada tramo no hay clave: se dice, no se inventa un 0."""
    ruta = SwapRoute(
        hops=(
            RouteHop(base=_new(), quote=_weth()),
            RouteHop(base=_weth(), quote=_usdc()),
        )
    )
    with pytest.raises(UnsupportedOperationError, match="comisión"):
        _motor(_Escenario(par=(NEW, USDC)))._route_path(ruta, token_in=NEW, token_out=USDC)


def test_un_tramo_que_el_motor_no_sondea_no_se_construye() -> None:
    """Un tramo fuera de la tabla no tiene espaciado conocido: se rechaza."""
    ruta = SwapRoute(
        hops=(
            RouteHop(base=_new(), quote=_weth(), fee_bps=BasisPoints(3)),
            RouteHop(base=_weth(), quote=_usdc(), fee_bps=BasisPoints(5)),
        )
    )
    with pytest.raises(UnsupportedOperationError, match="no sondea"):
        _motor(_Escenario(par=(NEW, USDC)))._route_path(ruta, token_in=NEW, token_out=USDC)


def test_el_venue_de_una_ruta_lleva_los_dos_tramos_dentro() -> None:
    """Dos rutas del mismo par por hubs distintos son sitios distintos, no una fila."""
    venue = v4._route_venue("base", (3_000, 500))
    assert venue.venue_id == "uniswap-v4@30+5"
    assert venue.name == "Uniswap V4 0.3 % + 0.05 %"
    # Y no se puede deshacer con el camino de vuelta de un pool: no nombra un
    # tramo, nombra un camino.
    with pytest.raises(UnsupportedOperationError, match="no es un venue"):
        v4._tier_from_venue("uniswap-v4@30+5")


#: El calldata medido el 2026-10-09 contra el `V4Quoter` de Base —`eth_call` a
#: `0x0d5e…048d`—: WETH → USDC → WETH, los dos tramos del 0,30 %, 10¹⁵ wei de
#: entrada, y devolvió `[993014236051103, 70983]`. Va palabra a palabra, una por
#: línea, para que además de la igualdad se lea el esqueleto.
CALLDATA_MEDIDO_DEL_CAMINO = (
    "0xca253dc9"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000004200000000000000000000000000000000000006"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "00000000000000000000000000000000000000000000000000038d7ea4c68000"
    "0000000000000000000000000000000000000000000000000000000000000002"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000100"
    "000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    "0000000000000000000000000000000000000000000000000000000000000bb8"
    "000000000000000000000000000000000000000000000000000000000000003c"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000a0"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000004200000000000000000000000000000000000006"
    "0000000000000000000000000000000000000000000000000000000000000bb8"
    "000000000000000000000000000000000000000000000000000000000000003c"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000a0"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
#: Los dos tramos del vector medido: WETH → USDC al 0,30 % y USDC → WETH al
#: 0,30 % —el mismo tramo dos veces, que es como se midió la composición—.
LLAVES_MEDIDAS = (
    abi.PathKey(currency=USDC, fee=3_000, tick_spacing=60),
    abi.PathKey(currency=WETH, fee=3_000, tick_spacing=60),
)


def test_el_camino_reproduce_el_calldata_medido_en_la_cadena() -> None:
    """El calldata del camino, byte a byte contra el que se mandó a la cadena.

    La huella de un vector se puede actualizar sin mirar; esto no: es el
    hexadecimal que el `eth_call` mandó de verdad al `V4Quoter` desplegado y que
    devolvió la cotización. Si deja de reproducirse es que la codificación se ha
    movido —la moneda de un tramo, el orden de las palabras, los
    desplazamientos de la tabla—, no que la prueba esté desactualizada.
    """
    medido = abi.quote_exact_input(WETH, LLAVES_MEDIDAS, 10**15)
    assert medido == CALLDATA_MEDIDO_DEL_CAMINO
    assert (len(medido) - 2) // 2 == 612

    # Y el esqueleto, que es lo que enseña algo cuando la igualdad se caiga: el
    # contrato lee la moneda de entrada y la tabla de claves por
    # desplazamiento, así que cada número tiene que estar en su palabra.
    selector = abi.SELECTOR_QUOTE_EXACT_INPUT
    assert _palabra_de(medido, selector, 0) == _palabra_uint(0x20)
    assert _direccion_de(medido, selector, 1) == WETH
    assert _palabra_de(medido, selector, 2) == _palabra_uint(0x60)
    assert _palabra_de(medido, selector, 3) == _palabra_uint(10**15)
    assert _palabra_de(medido, selector, 4) == _palabra_uint(2)
    assert _palabra_de(medido, selector, 5) == _palabra_uint(0x40)
    assert _palabra_de(medido, selector, 6) == _palabra_uint(0x100)
    assert _direccion_de(medido, selector, 7) == USDC
    assert _direccion_de(medido, selector, 13) == WETH


def test_los_desplazamientos_del_camino_se_cuentan_desde_su_tabla() -> None:
    """El array se lee por su propia tabla: los desplazamientos son relativos a ella.

    La tabla son la cuenta y los desplazamientos; cada elemento empieza donde
    termina el anterior, y el `hookData` lleva dentro su **propio**
    desplazamiento —cinco palabras de cabeza—, no el de nadie más. Escribir
    estos números en otra base produce un camino que el contrato lee como otra
    cosa, sin revertir por ello.
    """
    tabla = abi.path_keys(LLAVES_NEW_USDC)

    assert tabla[0:64] == _palabra_uint(2)  # cuántas claves
    assert tabla[64:128] == _palabra_uint(0x40)  # la primera, tras la tabla
    assert tabla[128:192] == _palabra_uint(0x100)  # la segunda, tras la primera
    # La primera clave, palabra a palabra: WETH de llegada, 0,30 %, 60 de
    # espaciado, sin gancho, y el `hookData` vacío con su desplazamiento.
    assert tabla[192:256] == _palabra_addr(WETH)
    assert tabla[256:320] == _palabra_uint(3_000)
    assert tabla[320:384] == _palabra_uint(60)
    assert tabla[384:448] == _palabra_uint(0)
    assert tabla[448:512] == _palabra_uint(5 * 32)
    assert tabla[512:576] == _palabra_uint(0)
    # Quince palabras: la tabla —cuenta y dos desplazamientos— y dos claves de
    # seis palabras cada una con el `hookData` vacío.
    assert len(tabla) // 2 == (1 + 2 + 2 * 6) * 32


def test_las_cabezas_del_camino_son_las_que_el_contrato_lee() -> None:
    """Los dos structs del camino: la llamada lleva un desplazamiento de más.

    El de la cotización tiene cuatro palabras de cabeza —desplazamiento propio,
    moneda de entrada, desplazamiento a la tabla y el importe— y el del swap
    cinco: la misma cabeza y el mínimo, que es lo único que separa las dos
    structs. Y el swap **no** lleva `minHopPriceX36`: es de una versión
    posterior del repositorio de v4-periphery, y meterlo desplazaría la tabla
    una palabra —el contrato leería el mínimo donde está la cuenta—.
    """
    selector = abi.SELECTOR_QUOTE_EXACT_INPUT
    cotizacion = abi.quote_exact_input(NEW, LLAVES_NEW_USDC, ENTRADA_NEW)
    assert _palabra_de(cotizacion, selector, 0) == _palabra_uint(32)
    assert _direccion_de(cotizacion, selector, 1) == NEW
    assert _palabra_de(cotizacion, selector, 2) == _palabra_uint(3 * 32)
    assert _palabra_de(cotizacion, selector, 3) == _palabra_uint(ENTRADA_NEW)
    assert cotizacion[len(selector) + 4 * 64 :] == abi.path_keys(LLAVES_NEW_USDC)

    swap = abi.swap_exact_in_path(
        NEW, LLAVES_NEW_USDC, amount_in_raw=ENTRADA_NEW, amount_out_min_raw=7
    )
    # Sin selector: es el parámetro de una acción, no una llamada.
    assert swap[0:64] == _palabra_uint(32)
    assert swap[64:128] == _palabra_addr(NEW)
    assert swap[128:192] == _palabra_uint(4 * 32)
    assert swap[192:256] == _palabra_uint(ENTRADA_NEW)
    assert swap[256:320] == _palabra_uint(7)
    assert swap[320:] == abi.path_keys(LLAVES_NEW_USDC)
    assert len(swap) // 2 == (5 + 3 + 2 * 6) * 32


def test_un_camino_vacio_o_un_importe_imposible_no_se_codifican() -> None:
    """Sin camino no hay nada que cotizar, y un importe que no cabe no se trunca.

    El importe es un `uint128`: truncarlo en silencio cotizaría por otro tamaño
    —el usuario vería un número y se firmaría otro—, y un camino vacío
    codificaría una tabla de cero claves que el contrato leería como un
    camino de cero saltos, que no es un swap.

    La comprobación del importe vive en las llamadas al cotizador, que son las
    que viajan a la cadena; el `swap` reutiliza la tabla de claves, así que su
    guarda de camino vacío es la misma, pero no repite la del importe —el
    número que codifica viene de una cotización que ya pasó por ella—.
    """
    with pytest.raises(SourceResponseError, match="camino"):
        abi.path_keys(())
    with pytest.raises(SourceResponseError, match="camino"):
        abi.quote_exact_input(NEW, (), ENTRADA_NEW)
    with pytest.raises(SourceResponseError, match="camino"):
        abi.swap_exact_in_path(NEW, (), amount_in_raw=1, amount_out_min_raw=1)
    with pytest.raises(SourceResponseError, match="no se cotiza"):
        abi.quote_exact_input(NEW, LLAVES_NEW_USDC, 0)
    with pytest.raises(SourceResponseError, match="no se cotiza"):
        abi.quote_exact_input(NEW, LLAVES_NEW_USDC, -1)
    with pytest.raises(SourceResponseError, match="uint128"):
        abi.quote_exact_input(NEW, LLAVES_NEW_USDC, 1 << 128)


# --------------------------------------------------------------------------- #
# 10. El impacto
# --------------------------------------------------------------------------- #
def test_el_impacto_se_mide_contra_el_marginal_del_pool() -> None:
    """Se deriva, y derivarlo mal publica un número que parece medido.

    El marginal sale del `sqrtPriceX96` del propio pool, así que la cuenta se puede
    rehacer a mano: en este par `currency0` es WETH, así que comprar WETH con USDC
    divide por el precio en vez de multiplicarlo. La cifra que se publica es la
    diferencia entre ese marginal y lo que dio el cotizador.
    """
    marginal = (Decimal(SQRT_PRICE_500) / Decimal(1 << 96)) ** 2
    salida_marginal = int(Decimal(ENTRADA_RAW) / marginal)
    assert salida_marginal > SALIDA_500  # la orden se paga; nunca se gana
    impacto = impact_bps(
        amount_in_raw=ENTRADA_RAW,
        amount_out_raw=SALIDA_500,
        sqrt_price_x96=SQRT_PRICE_500,
        token_in_is_token0=False,
        fee=BasisPoints(5),
    )
    # Medido: 3 puntos básicos para 1 USDC. Ni órdenes de magnitud por encima ni
    # por debajo, que es lo que saldría con el sentido del precio invertido.
    assert impacto is not None
    assert 0 <= impacto.value <= 10


def test_la_comision_no_se_cuenta_dos_veces() -> None:
    """La comisión se descuenta del marginal **una sola vez**.

    Si se contara también como impacto, un pool del 1 % aparecería con un 1 % de
    impacto en cada operación por pequeña que fuera, y el motor rechazaría
    operaciones buenas por un coste que no existe. La comprobación es exacta: una
    salida igual al marginal no tiene impacto ninguno.
    """
    marginal = (Decimal(SQRT_PRICE_500) / Decimal(1 << 96)) ** 2
    salida_marginal = int(Decimal(ENTRADA_RAW) / marginal)
    impacto = impact_bps(
        amount_in_raw=ENTRADA_RAW,
        amount_out_raw=salida_marginal,
        sqrt_price_x96=SQRT_PRICE_500,
        token_in_is_token0=False,
        # Un tramo del 1 %: si la comisión se contara dos veces, saldría 100.
        fee=BasisPoints(100),
    )
    assert impacto is not None
    assert impacto.value == 0


def test_un_pool_poco_profundo_da_un_impacto_que_no_se_publica() -> None:
    """Un impacto por encima del tope no se publica: se descarta esa ruta.

    Medido en Ethereum: el pool de DAI/USDC del 0,01 % existe, pero 1.000 DAI
    contra él cuestan un 45 %, y el del 0,3 % un 99,7 %. Se comprobó que la cifra
    es real —el marginal leído del `Slot0` coincide con el cotizador dentro de la
    comisión—, así que el motor está rechazando una operación que de verdad cuesta
    eso y no perdiendo una oportunidad.
    """
    impacto = impact_bps(
        amount_in_raw=ENTRADA_RAW,
        amount_out_raw=SALIDA_500 // 100,
        sqrt_price_x96=SQRT_PRICE_500,
        token_in_is_token0=False,
        fee=BasisPoints(5),
    )
    assert impacto is not None
    assert impacto.value > v4.MAX_PRICE_IMPACT.value


async def test_un_pool_que_se_agota_se_descarta_en_vez_de_publicarse() -> None:
    """Y el motor sigue: si otro tramo cotiza bien, se usa ése."""
    lector = _Escenario(
        pools={500: _pool(500, salida_raw=SALIDA_500 // 100), 3_000: _pool(3_000)}
    )
    cotizaciones = await _motor(lector).quote(_par(), _usdc().amount("1"))
    assert [cotizacion.venue.venue_id for cotizacion in cotizaciones] == ["uniswap-v4@30"]


async def test_la_liquidez_del_rango_activo_no_se_publica_como_si_fuera_reservas() -> None:
    """Se lee para decidir si el pool existe, no para publicarla.

    La `L` de un pool de liquidez concentrada no es una cantidad de ninguna moneda
    —convertirla a una exigiría suponer un rango de precios—, y ponerla en la
    columna donde otro motor publica sus reservas compararía dos cosas distintas.
    """
    cotizacion = (await _motor(_Escenario()).quote(_par(), _usdc().amount("1")))[0]
    assert cotizacion.liquidity is None
    assert cotizacion.impact_basis is Measurement.DERIVED
    assert cotizacion.engine_id == "uniswap_v4"


# --------------------------------------------------------------------------- #
# 11. Publicación
# --------------------------------------------------------------------------- #
def test_el_manifiesto_no_necesita_ninguna_clave() -> None:
    """Es la propiedad que hace que este motor no se detenga.

    Sin clave no hay cuota que agotar ni credencial que caduque: la única forma de
    que pare es que no haya ningún nodo público que responda.
    """
    assert v4.MANIFEST.required_config == ()
    assert v4.MANIFEST.engine_id == "uniswap_v4"


def test_el_manifiesto_declara_lo_que_el_motor_sabe_hacer() -> None:
    """Sin `PREPARE_TX` y con cadenas de swap, el manifiesto no se construye.

    Es la comprobación que impide publicar un motor que dice operar en una red y
    no sabe construir la transacción para hacerlo.
    """
    from amigocompora.domain.modes import Capability

    assert Capability.PREPARE_TX in v4.MANIFEST.capabilities
    assert Capability.READ_CHAIN in v4.MANIFEST.capabilities
    assert v4.MANIFEST.swap_chains == SWAP_CHAINS
    assert "base" in SWAP_CHAINS
    assert "ethereum" in SWAP_CHAINS


def test_el_motor_va_detras_de_v3_al_construir_pero_cotiza_igual() -> None:
    """`swap_priority` sólo decide quién construye cuando hay que elegir.

    No dice que la ruta de V4 sea peor: cada motor publica su propio `venue_id`,
    así que la comparación de precios decide con datos y los dos salen en la tabla.
    Lo que fija es quién responde primero, y V3 va delante porque está medido
    contra más pares y su camino de permiso es uno y no dos.
    """
    from amigocompora.engines.uniswap_v3 import engine as v3

    assert v3.MANIFEST.swap_priority < v4.MANIFEST.swap_priority
    assert v4.MANIFEST.swap_priority < 100  # delante de los agregados


def test_el_proveedor_no_necesita_configuracion() -> None:
    """Sin clave que leer: crear el motor no depende de lo que le pasen."""
    motor = v4.PROVIDER.create(object())
    assert isinstance(motor, v4.UniswapV4Engine)
    assert motor.manifest.engine_id == "uniswap_v4"
