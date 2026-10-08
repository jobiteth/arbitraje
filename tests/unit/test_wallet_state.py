"""Los saldos compartidos: una lectura por red, y de nadie más.

### Qué se comprueba aquí, y por qué importa

Lo que este módulo evita es un fallo **silencioso**: dos vistas de la misma
pantalla pidiendo la misma red al mismo nodo por el mismo dato. No rompe nada, no
da error y no se ve en una prueba de la interfaz; lo que hace es gastar cuota de un
nodo público —y en Solana, racionado por ventana— sin que nadie lo note.

Así que se comprueba lo que de verdad lo evita: que la segunda petición no salga,
que una lectura **en vuelo** no se repita, y que la caché se tire cuando cambia la
dirección que firma. Lo último no es una optimización: un saldo de otra cartera
bajo la dirección de la propia es la única clase de error que aquí no se puede
permitir, porque el usuario decide si tiene dinero mirando eso.

Se falsea el lector —que es la frontera de red— y nada más: `WalletBalances` es el
de producción, con su caché, su marca de lecturas en vuelo y su dueño.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from amigocompora.domain.models import Token
from amigocompora.domain.wallet import ChainHoldings, TokenHolding, WalletProfile, WalletSnapshot
from amigocompora.engines.catalog import native_token
from amigocompora.ui.wallet_state import WalletBalances

#: Una dirección EVM de verdad. Que sea conocida no importa: aquí no se firma nada,
#: sólo se etiqueta de quién son los saldos.
DIRECCION = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
OTRA = "0x000000000000000000000000000000000000dEaD"


def _holding(chain_key: str, symbol: str, cantidad: str) -> TokenHolding:
    token = Token(symbol=symbol, decimals=18, chain=chain_key, address=None)
    return TokenHolding(token=token, amount=token.amount(cantidad))


class LectorFalso:
    """Un lector que cuenta cuántas veces se le pide cada red, y se puede frenar.

    El freno es lo que permite comprobar la marca de «en vuelo»: con una lectura
    retenida, la segunda petición de la misma red tiene que **no** salir, que es
    justo el caso real —elegir la red repinta las dos patas antes de que la primera
    lectura haya vuelto—.
    """

    def __init__(self, *, puerta: asyncio.Event | None = None) -> None:
        self.pedidas: list[str] = []
        self._puerta = puerta

    async def __call__(
        self,
        profile: WalletProfile,
        *,
        chains: tuple[str, ...] | None = None,
    ) -> WalletSnapshot:
        objetivo = chains or ("polygon",)
        if self._puerta is not None:
            await self._puerta.wait()
        self.pedidas.extend(objetivo)
        return WalletSnapshot(
            profile=profile,
            chains=tuple(
                ChainHoldings(
                    chain=chain_key,
                    holdings=(_holding(chain_key, "POL", "19.49"),),
                )
                for chain_key in objetivo
            ),
            read_at=_ahora(),
        )


class LectorQueFalla(LectorFalso):
    """Un lector que revienta. Es el caso del nodo caído a mitad de la lectura."""

    async def __call__(
        self,
        profile: WalletProfile,
        *,
        chains: tuple[str, ...] | None = None,
    ) -> WalletSnapshot:
        self.pedidas.extend(chains or ("polygon",))
        raise RuntimeError("el nodo no contesta")


def _ahora() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


async def _dejar_pasar(veces: int = 5) -> None:
    """Deja correr el bucle lo suficiente para que termine una lectura."""
    for _ in range(veces):
        await asyncio.sleep(0)


async def _esperar(condicion: Callable[[], bool], *, nombre: str) -> None:
    for _ in range(200):
        if condicion():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"la condición no se cumplió: {nombre}")


# --------------------------------------------------------------------------- #
# 1. Una lectura por red, no una por repintado
# --------------------------------------------------------------------------- #
async def test_la_segunda_peticion_de_la_misma_red_no_sale() -> None:
    """Pedir dos veces la misma red devuelve lo cacheado, sin tocar el nodo.

    Es el caso normal: elegir la red repinta las dos patas, y cada repintado pide el
    saldo de lo que se entrega en ese instante.
    """
    lector = LectorFalso()
    balances = WalletBalances(lector)

    balances.ensure("polygon", owner=DIRECCION)
    await _esperar(lambda: lector.pedidas == ["polygon"], nombre="la primera lectura")
    balances.ensure("polygon", owner=DIRECCION)
    await _dejar_pasar()

    assert lector.pedidas == ["polygon"], "la segunda petición no puede salir a la red"
    assert balances.holdings("polygon") is not None


async def test_una_lectura_en_vuelo_no_se_repite() -> None:
    """La marca de «en vuelo» cubre el hueco entre pedir y recibir.

    Sin ella, la caché —todavía vacía— no evita nada, y las tres peticiones que
    dispara un repintado salen a la red a la vez. Se mide con una lectura retenida,
    que es la única forma de estar **dentro** de ese hueco y no después.
    """
    puerta = asyncio.Event()
    lector = LectorFalso(puerta=puerta)
    balances = WalletBalances(lector)

    for _ in range(3):
        balances.ensure("polygon", owner=DIRECCION)
    assert balances.reading("polygon") is True
    assert lector.pedidas == [], "la lectura está retenida, así que no ha salido todavía"

    puerta.set()
    await _esperar(lambda: balances.holdings("polygon") is not None, nombre="la lectura")
    await _dejar_pasar()

    assert lector.pedidas == ["polygon"], "tres repintados son una sola lectura"


async def test_una_lectura_que_falla_suelta_la_marca() -> None:
    """Un fallo de red no puede dejar el saldo sin poder volver a pedirse.

    La marca se suelta en un `finally`, y esto es lo que lo fija: si se quedara
    puesta al fallar, un corte momentáneo convertiría ese saldo en un dato que ya no
    se puede volver a pedir hasta reiniciar la aplicación —justo lo contrario de lo
    que hace falta después de un fallo—.
    """
    lector = LectorQueFalla()
    balances = WalletBalances(lector)

    balances.ensure("polygon", owner=DIRECCION)
    await _esperar(lambda: not balances.reading("polygon"), nombre="soltar la marca")

    assert balances.holdings("polygon") is None
    balances.ensure("polygon", owner=DIRECCION)
    await _dejar_pasar()
    assert lector.pedidas == ["polygon", "polygon"], "el reintento tiene que salir"


# --------------------------------------------------------------------------- #
# 2. La caché es de una sola cartera
# --------------------------------------------------------------------------- #
async def test_cambiar_de_cartera_tira_lo_leido() -> None:
    """Los saldos de una cartera no pueden quedar bajo la dirección de otra."""
    lector = LectorFalso()
    balances = WalletBalances(lector)

    balances.ensure("polygon", owner=DIRECCION)
    await _esperar(lambda: balances.holdings("polygon") is not None, nombre="la lectura")

    balances.ensure("polygon", owner=OTRA)
    assert balances.holdings("polygon") is None, "lo del dueño anterior no vale"
    await _esperar(lambda: balances.holdings("polygon") is not None, nombre="la del nuevo")
    assert lector.pedidas == ["polygon", "polygon"]


async def test_una_lectura_que_llega_tarde_no_pisa_la_del_nuevo_dueno() -> None:
    """La lectura del dueño viejo se descarta al llegar, no al pedirse.

    Es el caso que de verdad se da: se lee una cartera, y mientras la lectura está
    en vuelo el usuario guarda otra credencial. La respuesta que llega entonces es
    de la cartera anterior, y publicarla pintaría el dinero de una bajo el nombre de
    la otra.
    """
    puerta = asyncio.Event()
    lector = LectorFalso(puerta=puerta)
    balances = WalletBalances(lector)

    balances.ensure("polygon", owner=DIRECCION)
    balances.ensure("polygon", owner=OTRA)  # cambia el dueño con la primera en vuelo

    puerta.set()
    await _dejar_pasar(20)

    assert balances.owner == OTRA
    assert balances.holdings("polygon") is None, "la respuesta tardía no se publica"


# --------------------------------------------------------------------------- #
# 3. Lo que publica la lista de la cartera
# --------------------------------------------------------------------------- #
async def test_publicar_ahorra_la_lectura_de_la_tarjeta() -> None:
    """Una lectura que hizo la cartera entera sirve para la tarjeta de swap.

    Sin esto, arrancar la aplicación lee cada red dos veces: una para sumar el
    patrimonio en la lista de la cartera y otra para el saldo de la pata.
    """
    lector = LectorFalso()
    balances = WalletBalances(lector)
    balances.ensure("polygon", owner=DIRECCION)  # fija el dueño de la caché
    await _esperar(lambda: balances.holdings("polygon") is not None, nombre="la primera")
    lector.pedidas.clear()

    leida = ChainHoldings(chain="polygon", holdings=(_holding("polygon", "POL", "1.5"),))
    balances.publish("polygon", leida, owner=DIRECCION)

    assert balances.holdings("polygon") is leida
    assert lector.pedidas == [], "lo publicado no se vuelve a pedir"


async def test_no_se_publica_lo_de_otra_cartera() -> None:
    """Mirar otra dirección no puede dejar sus saldos en la caché de la que firma."""
    lector = LectorFalso()
    balances = WalletBalances(lector)
    balances.ensure("polygon", owner=DIRECCION)
    await _esperar(lambda: balances.holdings("polygon") is not None, nombre="la lectura")

    ajena = ChainHoldings(chain="polygon", holdings=(_holding("polygon", "POL", "999"),))
    balances.publish("polygon", ajena, owner=OTRA)

    guardado = balances.holdings("polygon")
    assert guardado is not None, "la lectura propia no puede desaparecer"
    assert guardado is not ajena, "el saldo del otro no entra"


# --------------------------------------------------------------------------- #
# 4. Los observadores
# --------------------------------------------------------------------------- #
async def test_los_observadores_se_avisan_al_llegar_la_lectura() -> None:
    """Sin el aviso, la vista que no pidió la lectura no se repinta nunca.

    Y la que no pidió existe: es la tarjeta de swap cuando la lectura la hizo la
    lista de la cartera.
    """
    lector = LectorFalso()
    balances = WalletBalances(lector)
    vistos: list[str] = []
    balances.subscribe(vistos.append)

    balances.ensure("polygon", owner=DIRECCION)
    await _esperar(lambda: vistos == ["polygon"], nombre="el aviso")

    balances.publish(
        "polygon",
        ChainHoldings(chain="polygon", holdings=(_holding("polygon", "POL", "2"),)),
        owner=DIRECCION,
    )
    assert vistos == ["polygon", "polygon"]


async def test_un_observador_roto_no_rompe_la_lectura() -> None:
    """Una vista a medio cerrar no puede tumbar el dato que acaba de llegar."""
    lector = LectorFalso()
    balances = WalletBalances(lector)
    visto: list[str] = []

    def rompe(_chain: str) -> None:
        raise RuntimeError("widget destruido")

    balances.subscribe(rompe)
    balances.subscribe(visto.append)

    balances.ensure("polygon", owner=DIRECCION)
    await _esperar(lambda: visto == ["polygon"], nombre="el segundo observador")

    assert balances.holdings("polygon") is not None


# --------------------------------------------------------------------------- #
# 5. Buscar un token dentro de la lectura
# --------------------------------------------------------------------------- #
async def test_find_distingue_dos_tokens_que_se_llaman_igual() -> None:
    """En Polygon hay dos USDC y los dos publican el mismo símbolo.

    Se busca por identidad —dirección— y no por símbolo: devolver el saldo del que
    no es sería el error que este método existe para no cometer.
    """
    nativo_real = native_token("polygon")
    otro = Token(
        symbol="USDC",
        decimals=6,
        chain="polygon",
        address="0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
    )
    balances = WalletBalances(LectorFalso())
    balances.ensure("polygon", owner=DIRECCION)
    await _esperar(lambda: balances.holdings("polygon") is not None, nombre="la lectura")

    leida = ChainHoldings(
        chain="polygon",
        holdings=(
            TokenHolding(token=nativo_real, amount=nativo_real.amount("19.49")),
            TokenHolding(token=otro, amount=otro.amount("3")),
        ),
    )
    balances.publish("polygon", leida, owner=DIRECCION)

    encontrado = balances.find("polygon", otro)
    assert encontrado is not None
    assert encontrado.token.address == otro.address

    # Y el nativo no se confunde con el ERC-20 homónimo.
    assert balances.find("polygon", Token(symbol="USDC", decimals=6, chain="polygon")) is None


async def test_find_no_devuelve_nada_de_una_red_que_fallo() -> None:
    """Una red que no se pudo leer no tiene saldos, y decir «cero» sería mentir."""
    balances = WalletBalances(LectorFalso())
    balances.ensure("polygon", owner=DIRECCION)
    await _esperar(lambda: balances.holdings("polygon") is not None, nombre="la lectura")

    balances.publish(
        "polygon",
        ChainHoldings(chain="polygon", error="el nodo no contestó"),
        owner=DIRECCION,
    )
    assert balances.find("polygon", native_token("polygon")) is None
