"""La pestaña de cartera y el salto a la de swap, sobre un contenedor de verdad.

### Qué se falsea y qué no

Se falsea el **motor de cartera** —el único que sale a la red— y nada más. El
contenedor, el caso de uso `ReadWallet`, la valoración, la guarda de modo, el
almacén de tokens y las dos pestañas son los de producción. Lo que devuelve el
doble es un `ChainHoldings`, que es exactamente el contrato que la pestaña
consume: la pantalla no puede distinguir este motor de uno que hable con nueve
nodos, así que lo que se mide aquí es la pantalla.

La valoración **no** se falsea, y de ahí sale la prueba que más importa: sin
ningún motor de cotización activo, un token que no es la stablecoin de su red se
queda sin precio, y entonces el total tiene que negarse a existir. Es el caso
real —una cartera con POL y USDC— y el que separa una cifra de una cifra que
miente.

### La clave privada

Es la primera cuenta de Hardhat, publicada y sin fondos. Está en un almacén de
memoria, nunca en el llavero del sistema, y lo que se comprueba con ella es
**qué botones se encienden**, no que se firme nada: aquí no se firma ni se emite.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QLabel

from amigocompora.app.container import Container, build_container
from amigocompora.domain.models import Token
from amigocompora.domain.modes import OperationMode
from amigocompora.domain.protocols import EngineManifest
from amigocompora.domain.wallet import (
    ChainHoldings,
    TokenHolding,
    WalletKind,
    WalletProfile,
)
from amigocompora.engines.catalog import native_token, quote_token
from amigocompora.engines.wallet.engine import MANIFEST as WALLET_MANIFEST
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    app_secret_key,
)
from amigocompora.ui.pages.prices import PricesPage
from amigocompora.ui.pages.wallet import DepositDialog, WalletPage, qr_pixmap
from amigocompora.ui.widgets import format_amount

#: La primera cuenta de Hardhat, publicada y sin fondos. Que sea conocida es lo
#: que la hace útil: no es un secreto y por eso puede estar escrita aquí.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION_DE_DESARROLLO = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

#: Otra dirección cualquiera, para mirar una cartera que no es la que firma.
OTRA_DIRECCION = "0x000000000000000000000000000000000000dEaD"

#: Un pubkey de Solana de verdad: 32 bytes en base58, y **no** el programa
#: System. La diferencia importa y se comprueba más abajo: el cero es una
#: dirección con la forma correcta que la aplicación rechaza, así que usarlo aquí
#: como «una cartera de Solana» mediría el rechazo creyendo medir la familia.
PUBKEY_SOLANA = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"

#: El pubkey todo a ceros: el programa System.
PROGRAMA_SYSTEM = "1" * 32

#: El contrato del USDC de Polygon, tal como lo publica el catálogo: es lo que se
#: pega cuando el token no aparece en la lista.
CONTRATO_USDC = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# El motor de cartera, falseado
# --------------------------------------------------------------------------- #
def _nativo(chain_key: str, cantidad: str) -> TokenHolding:
    token = native_token(chain_key)
    return TokenHolding(token=token, amount=token.amount(cantidad))


def _referencia(chain_key: str, cantidad: str) -> TokenHolding:
    """La stablecoin de la red, que es el único token que se valora sin motores.

    `ValueInReference` devuelve el importe tal cual cuando el token **es** la
    moneda de referencia —medir en sí misma no es una operación—, así que este es
    el caso que se puede comprobar entero sin falsear la valoración.
    """
    token = quote_token(chain_key)
    assert token is not None
    return TokenHolding(token=token, amount=token.amount(cantidad))


def _token(chain_key: str, symbol: str, cantidad: str, *, decimals: int = 6) -> TokenHolding:
    """Un token con contrato, para el caso que no está en el catálogo."""
    token = Token(
        symbol=symbol,
        decimals=decimals,
        chain=chain_key,
        address="0x" + "ab" * 20,
    )
    return TokenHolding(token=token, amount=token.amount(cantidad))


class CarteraFalsa:
    """Devuelve las posiciones que se le dan, red por red.

    Las redes que no se le declaran se devuelven **vacías y no fallidas**, que es
    lo que devolvería un nodo que contesta «esta dirección no tiene nada de lo que
    me preguntas». Distinguirlo de un fallo importa: una red vacía no impide
    afirmar un total y una red caída sí, y ésa es justo la decisión que se prueba.
    """

    manifest: EngineManifest = WALLET_MANIFEST

    def __init__(
        self,
        posiciones: Mapping[str, tuple[TokenHolding, ...]],
        *,
        fallos: Mapping[str, str] | None = None,
        parciales: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._posiciones = dict(posiciones)
        self._fallos = dict(fallos or {})
        self._parciales = dict(parciales or {})
        #: Redes que se pidieron, en orden y con repeticiones: es lo que permite
        #: afirmar que una lectura no se repite.
        self.leidas: list[str] = []

    async def aopen(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def holdings(
        self, profile: WalletProfile, chain_key: str, *, tokens: tuple[Token, ...] = ()
    ) -> ChainHoldings:
        self.leidas.append(chain_key)
        if chain_key in self._fallos:
            return ChainHoldings(chain=chain_key, error=self._fallos[chain_key])
        return ChainHoldings(
            chain=chain_key,
            holdings=self._posiciones.get(chain_key, ()),
            missing=self._parciales.get(chain_key, ()),
        )


class ProveedorFalso:
    """Publica la cartera falsa por el mismo contrato que la de verdad."""

    def __init__(self, motor: CarteraFalsa) -> None:
        self._motor = motor
        self.manifest = motor.manifest

    def create(self, config: Mapping[str, str]) -> CarteraFalsa:
        del config
        return self._motor


def _store(*, con_cartera: bool = True) -> InMemorySecretStore:
    store = InMemorySecretStore()
    if con_cartera:
        store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    return store


@asynccontextmanager
async def _cartera(
    motor: CarteraFalsa,
    *,
    con_cartera: bool = True,
    modo: OperationMode = OperationMode.OBSERVATION,
) -> AsyncIterator[Container]:
    """Un contenedor real con la cartera falsa en su ranura y ningún otro motor."""
    container = await build_container(
        Settings.model_validate({"mode": modo.value}),
        secret_store=_store(con_cartera=con_cartera),
        discover=False,
        configure_logs=False,
    )
    try:
        container.registry.register(ProveedorFalso(motor), source="prueba")
        await container.registry.activate("wallet")
        yield container
    finally:
        await container.aclose()


async def _esperar(condicion: Callable[[], bool]) -> None:
    """Cede el turno hasta que se cumpla, o falla con un mensaje.

    La pestaña lee en una tarea de Qt —`spawn`—, así que la prueba tiene que
    dejarle el turno al bucle. Se acota el número de vueltas para que una
    condición que no se cumple nunca falle en vez de colgarse.
    """
    for _ in range(500):
        if condicion():
            return
        await asyncio.sleep(0)
    raise AssertionError("la condición no se cumplió: la lectura no terminó")


async def _dejar_pasar() -> None:
    """Deja terminar lo que ya está lanzado y no se está esperando.

    Se usa antes de contar lecturas: una petición que ya salió pero todavía no
    volvió contaría como «no pedida» y haría pasar la prueba midiendo el instante
    en vez de medir el comportamiento.
    """
    for _ in range(20):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Leer lo pintado
# --------------------------------------------------------------------------- #
def _celda(pagina: WalletPage, fila: int, columna: int) -> str:
    """El texto de una celda, o un fallo si no hay nada en ella.

    `item()` devuelve `None` si la celda está vacía, y comparar ese `None` con el
    texto esperado fallaría más abajo con un mensaje que habla de `None` y no del
    dato: aquí se convierte en el fallo de verdad, que es que esa celda no se
    pintó.
    """
    item = pagina._table.item(fila, columna)
    assert item is not None, f"la celda ({fila}, {columna}) no se pintó"
    return item.text()


def _simbolos(pagina: WalletPage) -> list[str]:
    """El símbolo de cada fila, sin lo que desempata homónimos.

    La primera columna lleva `qualified_symbol` —«POL (nativo)», «USDC 0x3c499c54…»—
    porque en Polygon hay dos USDC y el símbolo a secas no distingue cuál es cuál.
    El desempate es para la pantalla, así que lo que se compara aquí es el símbolo.
    """
    return [_celda(pagina, fila, 0).split(" ")[0] for fila in range(pagina._table.rowCount())]


def _mirar(pagina: WalletPage, direccion: str) -> None:
    """Mira otra cartera y vuelve a leer, como hace «Mirar otra…»."""
    pagina._address.setText(direccion)
    pagina._profile = None
    pagina.refresh()


def _elegir(combo: QComboBox, chain_key: str, symbol: str) -> None:
    """Elige, en ese desplegable, el token de esa red con ese símbolo."""
    for posicion in range(combo.count()):
        token = combo.itemData(posicion)
        if isinstance(token, Token) and token.chain == chain_key and token.symbol == symbol:
            combo.setCurrentIndex(posicion)
            return
    raise AssertionError(f"«{symbol}» de «{chain_key}» no está en el desplegable")


def _elegir_red(pagina: PricesPage, chain_key: str) -> None:
    indice = pagina._chain.findData(chain_key)
    assert indice >= 0, chain_key
    pagina._chain.setCurrentIndex(indice)


def _preparar_saldo(pagina: PricesPage, symbol: str, chain_key: str = "polygon") -> None:
    """Deja la tarjeta de conversión en esa red y ese token, sin esperar el saldo."""
    _elegir_red(pagina, chain_key)
    _elegir(pagina._base, chain_key, symbol)


# --------------------------------------------------------------------------- #
# 1. Leer y pintar
# --------------------------------------------------------------------------- #
async def test_abre_con_la_cartera_de_la_clave_configurada() -> None:
    """Sin esto la pestaña arranca en blanco teniendo cartera que enseñar."""
    motor = CarteraFalsa({"base": (_nativo("base", "1.5"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)

    assert pagina._address.text() == DIRECCION_DE_DESARROLLO
    assert pagina._profile is not None
    assert pagina._profile.label == "Mi cartera"
    assert pagina._profile.kind is WalletKind.EVM


async def test_sin_clave_la_pestana_lo_dice_en_vez_de_quedarse_en_blanco() -> None:
    motor = CarteraFalsa({})
    async with _cartera(motor, con_cartera=False) as container:
        pagina = WalletPage(container)

    assert pagina._address.text() == ""
    assert pagina._table.rowCount() == 0
    assert "Credenciales" in pagina._notice.text()
    assert not pagina._deposit_btn.isEnabled()


async def test_se_leen_las_redes_que_el_motor_declara_y_solo_esas() -> None:
    """Las redes de la familia de la cartera, y ninguna más.

    El motor declara las que tienen nodos medidos —nueve, y entre ellas Solana—,
    pero una cartera EVM no puede leer Solana: leerla sería pedir una red de otra
    familia, que contesta con un error seguro. Y no se piden las del catálogo que
    el motor no declara: una consulta que siempre falla no es una consulta, es una
    fila de error esperando.
    """
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        declaradas = set(motor.manifest.wallet_chains)
        esperadas = {key for key in declaradas if WalletKind.of_chain(key) is WalletKind.EVM}
        await _esperar(lambda: set(motor.leidas) == esperadas)

        assert set(motor.leidas) == esperadas
        assert pagina._chain.count() == len(esperadas) + 1  # y «Todas las redes»

    # La premisa de la que sale el filtro: el motor declara más de lo que esta
    # cartera puede leer, así que quedarse con el manifiesto a secas habría pedido
    # una red de otra familia.
    assert declaradas > esperadas
    assert len(esperadas) > 5


async def test_solo_se_ensena_lo_que_tiene_fondos() -> None:
    motor = CarteraFalsa(
        {
            "polygon": (
                _nativo("polygon", "19.49166614"),
                _referencia("polygon", "0"),
                _token("polygon", "RARO", "3.25"),
            )
        }
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)

    assert _simbolos(pagina) == ["POL", "RARO"]
    # La moneda de la red se dice que lo es: es lo único que la distingue de un
    # token con el mismo símbolo, y es lo que paga el gas.
    assert _celda(pagina, 0, 0) == "POL (nativo)"
    assert _celda(pagina, 0, 1) == "19.4917"
    assert _celda(pagina, 0, 2) == "sin cotización"
    assert _celda(pagina, 0, 3) == "Polygon PoS"
    assert _celda(pagina, 0, 4) == "—"  # el nativo no tiene contrato


async def test_el_interruptor_ensena_tambien_los_que_estan_a_cero() -> None:
    """Volver a enseñar los ceros demuestra que el token está en la lista aunque
    ahora no tenga nada, así que las filas se guardan enteras y se filtran al
    pintar. Si el filtro se aplicara al leer, desmarcarlo no tendría nada que
    volver a enseñar y el interruptor parecería roto."""
    motor = CarteraFalsa(
        {"polygon": (_nativo("polygon", "19.49166614"), _referencia("polygon", "0"))}
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)
        antes = pagina._table.rowCount()

        pagina._only_positive.setChecked(False)
        despues = pagina._table.rowCount()

    assert antes == 1
    assert despues == 2


async def test_un_saldo_a_cero_vale_cero_y_no_se_dice_que_falte_precio() -> None:
    """Cero no es «no se sabe».

    La valoración no pregunta por un saldo vacío —no hay nada que valorar—, así
    que la columna del valor se quedaba en «sin cotización», que es el texto de
    las posiciones **con fondos** a las que no se les pudo poner precio. Al lado
    de un 0 eso se lee como que la cotización no llega, y culpa a un motor que
    nunca fue preguntado. La cifra que sí se puede afirmar sin consultar a nadie
    es cero: cero por cualquier precio es cero.
    """
    motor = CarteraFalsa(
        {"polygon": (_nativo("polygon", "19.49166614"), _referencia("polygon", "0"))}
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)
        pagina._only_positive.setChecked(False)
        await _esperar(lambda: pagina._table.rowCount() > 1)
        fila = _simbolos(pagina).index("USDC")

        valor = _celda(pagina, fila, 2)

    assert valor == "0 USDC", "la stablecoin de la red es la unidad en la que se valora"
    assert "sin cotización" not in valor
    # Y el caso de verdad —con fondos y sin precio— sigue diciéndolo, que es lo
    # que distingue un hueco de un cero.
    assert _celda(pagina, 0, 2) == "sin cotización"


async def test_una_red_caida_no_se_confunde_con_una_red_vacia() -> None:
    """Una red que no contestó y una red vacía se ven igual en la lista —ninguna
    fila— y no son lo mismo: de la primera no se sabe nada."""
    motor = CarteraFalsa(
        {"base": (_referencia("base", "10"),)},
        fallos={"polygon": "ningún nodo de «polygon» contestó"},
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)

    aviso = pagina._notice.text()
    assert "Polygon PoS" in aviso
    assert "ningún nodo" in aviso
    # Y la red que sí se leyó sigue pintada: una caída no tira las otras.
    assert _simbolos(pagina) == ["USDC"]


async def test_una_direccion_con_basura_no_tumba_la_ventana() -> None:
    """El caso real: un pegado a medias en «Mirar otra…».

    `refresh()` corre dentro de un manejador de señal de Qt y no hay ningún `try`
    por encima, así que una excepción que salga de ahí no la recoge nadie y en las
    versiones que la propagan se lleva por delante la aplicación entera. Pegar mal
    una dirección tiene que costar un mensaje, no la ventana.
    """
    motor = CarteraFalsa({"base": (_referencia("base", "10"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)

        _mirar(pagina, "0x1234")

        assert pagina._table.rowCount() == 0
        assert "0x1234" in pagina._notice.text()
        assert pagina._total.text() == "—"
        # Sin perfil no queda nada que firmar: las dos acciones se apagan. El
        # depósito también, porque sin cartera no hay dirección que enseñar.
        assert not pagina._withdraw_btn.isEnabled()
        assert not pagina._deposit_btn.isEnabled()


async def test_una_cartera_de_solana_no_se_lee_como_si_fuera_evm() -> None:
    """La familia se deduce de la forma de la dirección, y cambia lo que se lee."""
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        # La primera lectura, la de la cartera que firma, ya se hizo: lo que se
        # mide es lo que se pide **a partir de aquí**, no el total acumulado.
        await _esperar(lambda: pagina._snapshot is not None)
        antes = len(motor.leidas)

        _mirar(pagina, PUBKEY_SOLANA)
        await _esperar(lambda: len(motor.leidas) > antes)

        assert pagina._profile is not None
        assert pagina._profile.kind is WalletKind.SOLANA
        assert set(motor.leidas[antes:]) == {"solana"}
        # El desplegable ofrece las redes de **su** familia: una sola, y «Todas».
        assert pagina._chain.count() == 2


async def test_el_programa_system_de_solana_se_rechaza() -> None:
    """No es una cartera: es el pubkey todo a ceros, y enviarle fondos los pierde.

    La misma regla que la dirección cero en EVM. Se comprueba aquí y no en el
    dominio porque la pantalla es donde se pega, y porque es el sitio donde el
    rechazo tiene que llegar como un mensaje y no como una excepción.
    """
    from amigocompora.domain.addresses import is_solana_address

    # Las dos tienen forma de pubkey y sólo una es una cartera: es la premisa de
    # la que depende que esta prueba mida el rechazo y no el formato.
    assert is_solana_address(PROGRAMA_SYSTEM)
    assert is_solana_address(PUBKEY_SOLANA)

    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        _mirar(pagina, PROGRAMA_SYSTEM)

        assert "System" in pagina._notice.text()
        assert pagina._table.rowCount() == 0


# --------------------------------------------------------------------------- #
# 2. El total, que es donde se puede mentir
# --------------------------------------------------------------------------- #
async def test_el_total_no_se_afirma_si_una_posicion_no_tiene_precio() -> None:
    """El caso real: POL sin cotización y USDC valorado.

    Un total de 12,5 USDC —el USDC solo, callando el POL— diría que la cartera
    vale menos de lo que vale, con el mismo aspecto que la cifra buena. Aquí no
    se enseña cifra y se dice por qué.
    """
    motor = CarteraFalsa(
        {"polygon": (_nativo("polygon", "19.49166614"), _referencia("polygon", "12.5"))}
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 1)

    assert pagina._total.text() == "—"
    assert "sin cotización" in pagina._total_hint.text()
    assert "POL" in pagina._notice.text()


async def test_el_total_se_ensena_cuando_esta_todo_valorado() -> None:
    motor = CarteraFalsa({"base": (_referencia("base", "12.5"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._total.text() != "—")

    assert pagina._total.text() == "12.5 USDC"
    assert "Todas las posiciones" in pagina._total_hint.text()


async def test_el_total_no_se_afirma_si_falta_una_red() -> None:
    """Y el motivo se nombra: «faltan datos» no permite hacer nada, «no se pudo
    leer Polygon» sí."""
    motor = CarteraFalsa(
        {"base": (_referencia("base", "12.5"),)},
        fallos={"polygon": "sin nodos disponibles"},
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)

    assert pagina._total.text() == "—"
    assert "Polygon PoS" in pagina._total_hint.text()


async def test_una_red_leida_a_medias_tambien_impide_el_total() -> None:
    """`missing` no es `error`: hay saldos, y no son todos.

    Es el caso de Solana con un programa de tokens caído, y el de una red cuyo
    presupuesto de valoración se agotó. Sumar lo que llegó y callar el resto es
    la versión silenciosa del mismo error.
    """
    motor = CarteraFalsa(
        {"base": (_referencia("base", "12.5"),)},
        parciales={"base": ("3 posiciones sin valorar",)},
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)

    assert pagina._total.text() == "—"
    assert "a medias" in pagina._total_hint.text()


# --------------------------------------------------------------------------- #
# 3. Las cifras: recortadas para leer, enteras para comprobar
# --------------------------------------------------------------------------- #
def test_el_recorte_por_digitos_y_no_por_decimales() -> None:
    """Una cartera tiene las dos cosas a la vez: polvo y cantidades largas.

    Con dos decimales, 0,0000012 ETH se leería «0,00»; con dieciocho, 19,491666
    POL ocuparía media tabla para no decir nada más que «19,49».
    """
    assert format_amount(Decimal("0.000001234")) == "0.000001234"
    assert format_amount(Decimal("19.491666140000000")) == "19.4917"
    assert format_amount(Decimal("0")) == "0"
    # Y el modo completo no recorta nada: es el número que se comprueba.
    assert format_amount(Decimal("19.491666140000000"), full=True) == "19.491666140000000"


def test_un_saldo_minusculo_no_se_imprime_como_cero() -> None:
    """El error que hace que el recorte no valga cualquier recorte.

    Un saldo de 0,0000001 ETH con seis decimales es «0.000000», que se lee como un
    cero y no lo es. Un saldo que parece cero es peor que un saldo largo: el largo
    se mira, el cero se da por hecho y se decide con él.
    """
    assert format_amount(Decimal("0.0000001")) == "0.0000001"
    assert format_amount(Decimal("0.00000012345678")) == "0.000000123457"


def test_una_cantidad_larga_se_escribe_entera_y_no_en_cientifica() -> None:
    """«1.23457e+6» es lo que devuelve el formato corto, y en un saldo no se lee."""
    assert format_amount(Decimal("1234567.891")) == "1234568"


async def test_la_columna_ensena_la_cifra_recortada_y_su_entera_en_el_tooltip() -> None:
    """El recorte es de la columna, no del dato: la cifra entera está siempre a un
    tooltip de distancia, sin tener que pulsar nada."""
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "19.49166614"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)

        celda = pagina._table.item(0, 1)
        assert celda is not None
        assert celda.text() == "19.4917"
        assert "19.49166614" in celda.toolTip()

        pagina._full_btn.setChecked(True)
        assert _celda(pagina, 0, 1) == "19.49166614"


async def test_el_recorte_no_toca_lo_que_se_firmaria() -> None:
    """Lo que se recorta es una cadena de texto; el importe vive en el `TokenAmount`.

    Se comprueba sobre el propio objeto: el raw del saldo es el de la cadena, no
    el que resultaría de redondear el texto que se pinta.
    """
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "19.49166614"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)

        _, holding = pagina._rows[0]
        pintado = _celda(pagina, 0, 1)

    assert holding.amount.raw == 19_491_666_140_000_000_000
    assert pintado == "19.4917"
    assert pintado != f"{holding.as_decimal():f}"


# --------------------------------------------------------------------------- #
# 4. El buscador
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "aguja",
    [
        CONTRATO_USDC,  # el contrato entero, como se copia del explorador
        CONTRATO_USDC.upper(),  # sin el prefijo y en mayúsculas
        "0x3C499C54",  # un trozo pegado de cualquier manera
        "3c499c54",  # sin el prefijo
    ],
)
def test_el_buscador_encuentra_por_contrato(aguja: str) -> None:
    """El contrato se busca porque es lo único que distingue dos tokens con el
    mismo símbolo —y lo que se pega justo cuando el token no está en la lista—.

    Se compara en minúsculas y sin exigir el `0x`, que es como se pega la mitad de
    las veces: una búsqueda que falla por mayúsculas es una búsqueda que parece
    decir «no lo tienes».
    """
    usdc = _referencia("polygon", "12.5")
    assert WalletPage._matches(usdc, aguja.lower())


def test_el_buscador_encuentra_por_simbolo() -> None:
    pol = _nativo("polygon", "19.49166614")
    assert WalletPage._matches(pol, "pol")
    assert WalletPage._matches(pol, "POL".lower())


def test_el_buscador_no_encuentra_lo_que_no_esta() -> None:
    usdc = _referencia("polygon", "12.5")
    assert not WalletPage._matches(usdc, "0xdeadbeef")
    assert not WalletPage._matches(usdc, "otro-token")


async def test_el_buscador_dice_que_anada_el_token_cuando_pegan_un_contrato() -> None:
    """La única respuesta útil a «este token no está» es «añádelo, así»."""
    motor = CarteraFalsa({"base": (_referencia("base", "1"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)

        pagina._search.setText("0x" + "cd" * 20)

        assert pagina._table.rowCount() == 0
        # `isHidden` y no `isVisible`: la ventana no se enseña en la prueba, así
        # que `isVisible` sería falso pasara lo que pasara.
        assert not pagina._empty.isHidden()
        assert "Añadir token" in pagina._empty.text()

        # Y el token que sí está vuelve al borrar el filtro, sin releer nada: el
        # buscador filtra lo que ya se leyó.
        leidas = len(motor.leidas)
        pagina._search.setText("")
        assert pagina._table.rowCount() == 1
        assert len(motor.leidas) == leidas


# --------------------------------------------------------------------------- #
# 5. Depositar y retirar
# --------------------------------------------------------------------------- #
def test_el_qr_lleva_la_direccion_modulo_a_modulo() -> None:
    """El QR se dibuja a mano desde la matriz, así que se comprueba contra ella.

    No hay lector de QR en las dependencias y no hace falta: lo que puede fallar
    aquí es el **mapeo** de matriz a píxeles —un desplazamiento, un módulo
    invertido, el marco de silencio—, y eso se ve muestreando el centro de cada
    módulo. El marco importa: sin él, medio móvil no lee un QR sobre fondo oscuro,
    y el fallo se descubre justo cuando hace falta que funcione.
    """
    import segno

    matriz = segno.make(DIRECCION_DE_DESARROLLO, error="m").matrix
    imagen = qr_pixmap(DIRECCION_DE_DESARROLLO).toImage()
    lados = len(matriz)
    modulo = imagen.width() // (lados + 4)

    assert modulo * (lados + 4) == imagen.width()
    assert imagen.width() == imagen.height()
    for fila, celdas in enumerate(matriz):
        for columna, celda in enumerate(celdas):
            x = (columna + 2) * modulo + modulo // 2
            y = (fila + 2) * modulo + modulo // 2
            pintado = imagen.pixelColor(x, y).name()
            assert (pintado == "#000000") is bool(celda), (fila, columna)

    # Y el marco de silencio, en blanco: las tres esquinas y una orilla.
    esquinas = ((0, 0), (imagen.width() - 1, 0), (0, imagen.height() - 1))
    for x, y in esquinas:
        assert imagen.pixelColor(x, y).name() == "#ffffff"


def test_el_dialogo_de_deposito_avisa_de_la_red_equivocada() -> None:
    """El aviso más importante de la pantalla: lo que llegue por otra red no se
    recupera, y no hay a quién reclamárselo."""
    dialogo = DepositDialog(DIRECCION_DE_DESARROLLO, "polygon")
    textos = " ".join(etiqueta.text() for etiqueta in dialogo.findChildren(QLabel))

    assert dialogo.windowTitle() == "Depositar en Polygon PoS"
    assert dialogo._address.text() == DIRECCION_DE_DESARROLLO
    assert dialogo._address.isReadOnly()
    assert "no se recupera" in textos
    assert "Polygon PoS" in textos


async def test_retirar_se_apaga_sin_clave_y_dice_donde_se_pone() -> None:
    """Sin clave no hay desde dónde sacar dinero, y el botón lo dice con el sitio
    exacto donde se pone la clave en vez de quedarse mudo.

    Depositar sigue encendido, y no es una excepción: mirar una dirección es
    mirarla, y cobrar en ella no necesita ninguna credencial. Lo que se apaga sin
    clave es lo único que sí la necesita.
    """
    motor = CarteraFalsa({"base": (_nativo("base", "1"),)})
    async with _cartera(motor, con_cartera=False) as container:
        pagina = WalletPage(container)
        # Al abrir, sin clave, no hay ni dirección que mirar: ahí se apagan las dos.
        assert not pagina._deposit_btn.isEnabled()

        _mirar(pagina, OTRA_DIRECCION)
        await _esperar(lambda: pagina._snapshot is not None)

    assert not pagina._withdraw_btn.isEnabled()
    assert "Credenciales" in pagina._withdraw_btn.toolTip()
    assert pagina._deposit_btn.isEnabled()


async def test_retirar_se_apaga_si_la_cartera_mirada_no_es_la_que_firma() -> None:
    """Firmar desde otra cartera mandaría el dinero desde donde no se está
    mirando. Se apaga al pintar, no al pulsar."""
    motor = CarteraFalsa({"base": (_nativo("base", "1"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        _mirar(pagina, OTRA_DIRECCION)
        await _esperar(lambda: pagina._snapshot is not None)

    assert not pagina._withdraw_btn.isEnabled()
    assert "no es la cartera que firma" in pagina._withdraw_btn.toolTip()
    # Depositar sí: enseñar una dirección pública no necesita clave.
    assert pagina._deposit_btn.isEnabled()


async def test_con_la_cartera_propia_retirar_se_enciende() -> None:
    motor = CarteraFalsa({"base": (_nativo("base", "1"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: pagina._table.rowCount() > 0)

    assert pagina._withdraw_btn.isEnabled()
    assert "irreversible" in pagina._withdraw_btn.toolTip()


async def test_una_cartera_de_solana_no_ofrece_retirar_pero_si_depositar() -> None:
    """El camino de firma de Solana no está hecho, y un botón que existe y falla es
    peor que uno apagado que explica por qué.

    Y el motivo que sale es el de Solana, no el de «no es tu cartera»: cambiar de
    clave no lo arreglaría, así que mandar a cambiarla sería mandar a chocar otra
    vez con lo mismo.
    """
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        _mirar(pagina, PUBKEY_SOLANA)
        await _esperar(lambda: pagina._snapshot is not None)

        assert not pagina._withdraw_btn.isEnabled()
        assert "Solana" in pagina._withdraw_btn.toolTip()
        assert "no es la cartera que firma" not in pagina._withdraw_btn.toolTip()
        assert pagina._deposit_btn.isEnabled()


# --------------------------------------------------------------------------- #
# 6. El salto a la pestaña de swap
# --------------------------------------------------------------------------- #
async def test_intercambiar_prepara_el_par_en_la_red_del_token() -> None:
    """«Intercambiar este token» significa: la red, la pata de entrega y la de
    enfrente puestas, para no volver a buscar el token en dos desplegables."""
    motor = CarteraFalsa({"polygon": (_token("polygon", "RARO", "5"),)})
    async with _cartera(motor) as container:
        wallet = WalletPage(container)
        await _esperar(lambda: wallet._table.rowCount() > 0)

        swap = PricesPage(container)
        _, holding = wallet._rows[0]
        swap.prepare_with_token(holding.token)
        entrega, recibe = swap._legs()

    assert swap._chain.currentData() == "polygon"
    assert entrega is not None
    assert entrega.symbol == "RARO"
    assert recibe is not None
    assert recibe.symbol == "USDC"


async def test_intercambiar_la_moneda_nativa_la_pone_en_la_pata_de_entrega() -> None:
    """ETH y POL son lo que más se tiene y lo que más se convierte.

    Es el caso que estaba roto: el nativo no tiene contrato que pegar, así que no
    se puede guardar en el almacén de tokens añadidos, y la lista de swap lo dejaba
    fuera. «Intercambiar este token» sobre el POL de una cuenta acababa en un botón
    que no llevaba a ninguna parte.
    """
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "19.49166614"),)})
    async with _cartera(motor) as container:
        wallet = WalletPage(container)
        await _esperar(lambda: wallet._table.rowCount() > 0)

        swap = PricesPage(container)
        _, holding = wallet._rows[0]
        assert holding.token.is_native
        swap.prepare_with_token(holding.token)
        entrega, recibe = swap._legs()

        assert entrega is not None
        assert entrega.is_native
        assert entrega.symbol == "POL"
        assert recibe is not None
        assert recibe.symbol == "USDC"
        # Y no ha quedado guardado como si fuera un token añadido: no tiene
        # contrato, así que el almacén no tendría con qué identificarlo.
        assert container.token_store.load() == ()


async def test_intercambiar_un_token_que_no_estaba_lo_anade_a_la_lista() -> None:
    """Un token que la cartera ve porque lo leyó un nodo, pero que nadie añadió,
    tiene que entrar en la lista de swap en vez de fallar en silencio."""
    raro = _token("polygon", "RARO", "5", decimals=18)
    motor = CarteraFalsa({"polygon": (raro,)})
    async with _cartera(motor) as container:
        wallet = WalletPage(container)
        await _esperar(lambda: wallet._table.rowCount() > 0)

        swap = PricesPage(container)
        swap.prepare_with_token(raro.token)
        entrega, _ = swap._legs()
        guardado = [token.symbol for token in container.token_store.load()]

    assert entrega is not None
    assert entrega.symbol == "RARO"
    assert "RARO" in guardado


# --------------------------------------------------------------------------- #
# 7. El saldo en la propia tarjeta de conversión
# --------------------------------------------------------------------------- #
async def test_la_tarjeta_de_swap_ensena_el_saldo_de_lo_que_se_entrega() -> None:
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "19.49166614"),)})
    async with _cartera(motor) as container:
        swap = PricesPage(container)
        _preparar_saldo(swap, "POL")
        await _esperar(lambda: "Disponible" in swap._balance.text())

    assert swap._balance.text() == "Disponible: 19.4917 POL"
    assert "19.49166614" in swap._balance.toolTip()
    # Y el aviso del gas, en el sitio donde se comete el error: la comisión de la
    # operación sale del mismo saldo que se está intentando mover.
    assert "comisión" in swap._balance.toolTip()
    assert swap._max_btn.isEnabled()


async def test_max_pone_el_saldo_y_dice_lo_que_no_cabe() -> None:
    """El campo tiene seis decimales y el saldo dieciocho: escribe de menos, y lo
    dice. Un «Máx» que deja un resto sin poner y lo calla miente sobre lo que
    hace."""
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "19.49166614"),)})
    async with _cartera(motor) as container:
        swap = PricesPage(container)
        _preparar_saldo(swap, "POL")
        await _esperar(lambda: "Disponible" in swap._balance.text())

        swap._on_max()

    assert swap._amount.value() == pytest.approx(19.491666, abs=1e-9)
    assert "no cabe el resto" in swap._status.text()


async def test_sin_clave_la_tarjeta_no_ensena_saldo_y_dice_por_que() -> None:
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "19.49166614"),)})
    async with _cartera(motor, con_cartera=False) as container:
        swap = PricesPage(container)
        _elegir_red(swap, "polygon")

    assert swap._balance.text() == ""
    assert not swap._max_btn.isEnabled()
    assert "Credenciales" in swap._max_btn.toolTip()


async def test_una_red_se_lee_una_sola_vez_aunque_se_repinte() -> None:
    """Una lectura de una red trae todos sus tokens, así que pedirla dos veces es
    pedir el mismo dato dos veces.

    El caso no es teórico: elegir la red repinta las dos patas, y cada repintado
    pide el saldo de lo que se entrega en ese instante. Con la caché todavía vacía
    —la primera lectura aún no ha vuelto— las tres peticiones salían a la red, y
    eran tres pasadas completas de nodos por un saldo. En Solana, con un nodo
    público racionado por ventana, eso es lo que no se puede permitir.
    """
    motor = CarteraFalsa(
        {"polygon": (_nativo("polygon", "19.49166614"), _referencia("polygon", "3"))}
    )
    async with _cartera(motor) as container:
        swap = PricesPage(container)
        _preparar_saldo(swap, "POL")
        await _esperar(lambda: "Disponible" in swap._balance.text())
        await _dejar_pasar()

        assert motor.leidas.count("polygon") == 1

        # Y cambiar de pata dentro de la misma red tampoco: lo que llega de esa
        # lectura son todos los saldos de la red, no sólo el del token pedido.
        _elegir(swap._base, "polygon", "USDC")
        await _dejar_pasar()

        assert motor.leidas.count("polygon") == 1
        assert swap._balance.text() == "Disponible: 3 USDC"


# --------------------------------------------------------------------------- #
# Dos lecturas a la vez
# --------------------------------------------------------------------------- #
class CarteraConFreno(CarteraFalsa):
    """La misma cartera, con datos por dirección y un freno por dirección.

    El freno existe para poder **ordenar** dos lecturas sin depender de tiempos de
    reloj: una prueba que espera «a ver si tarda más» mide la máquina, no el
    programa. Aquí una lectura se queda parada donde se le diga, y quien escribe
    la prueba decide cuál termina primero.
    """

    def __init__(
        self, por_direccion: Mapping[str, Mapping[str, tuple[TokenHolding, ...]]]
    ) -> None:
        super().__init__({})
        self._por_direccion = {clave.lower(): valor for clave, valor in por_direccion.items()}
        self._frenos: dict[str, asyncio.Event] = {}
        #: Direcciones que ya han llegado a su freno. Es la señal que espera la
        #: prueba para saber que la lectura salió de verdad, y no que todavía no
        #: se haya lanzado.
        self.llegadas: set[str] = set()

    def frenar(self, address: str) -> asyncio.Event:
        self._frenos[address.lower()] = asyncio.Event()
        return self._frenos[address.lower()]

    async def holdings(
        self, profile: WalletProfile, chain_key: str, *, tokens: tuple[Token, ...] = ()
    ) -> ChainHoldings:
        self.leidas.append(chain_key)
        clave = profile.address.lower()
        if (freno := self._frenos.get(clave)) is not None:
            self.llegadas.add(clave)
            await freno.wait()
        return ChainHoldings(
            chain=chain_key,
            holdings=self._por_direccion.get(clave, {}).get(chain_key, ()),
        )


def _textos(pagina: WalletPage) -> list[str]:
    """Todo el texto pintado en la tabla, celda a celda."""
    return [
        item.text()
        for fila in range(pagina._table.rowCount())
        for columna in range(pagina._table.columnCount())
        if (item := pagina._table.item(fila, columna)) is not None
    ]


async def test_una_lectura_vieja_no_pisa_a_la_nueva() -> None:
    """Lo que llega tarde no pinta, aunque llegue después.

    El caso es el de abrir la pestaña —que ya lee sola la cartera de la clave— y
    escribir otra dirección antes de que aquella lectura acabe. Las dos lecturas
    corren a la vez y, sin nada que las ordene, pinta la que termina última; como
    la de la clave mira ocho redes y además valora, casi siempre es la vieja.

    Medido en vivo antes de arreglarlo: la lectura de la clave tardaba 9,8 s y la
    de Solana 1,4 s, así que la pantalla acababa con la dirección nueva en la caja
    y **los saldos de la cartera del usuario** en la tabla. Aquí la carrera se
    ordena a mano y se comprueba lo mismo: que la vieja no llega a pintar.
    """
    motor = CarteraConFreno(
        {
            DIRECCION_DE_DESARROLLO: {
                "polygon": (_nativo("polygon", "19.49166614"),),
                "base": (_nativo("base", "0.5"),),
            },
            OTRA_DIRECCION: {"polygon": (_nativo("polygon", "7"),)},
        }
    )
    async with _cartera(motor) as container:
        # La pestaña abre con la cartera de la clave, y su lectura se queda parada.
        freno_viejo = motor.frenar(DIRECCION_DE_DESARROLLO)
        pagina = WalletPage(container)
        await _esperar(lambda: DIRECCION_DE_DESARROLLO.lower() in motor.llegadas)

        # El usuario escribe otra dirección sin esperar a que aquélla termine.
        freno_nuevo = motor.frenar(OTRA_DIRECCION)
        pagina._address.setText(OTRA_DIRECCION)
        pagina.refresh()
        await _esperar(lambda: OTRA_DIRECCION.lower() in motor.llegadas)

        # La nueva termina primero: su saldo es el que se ve.
        freno_nuevo.set()
        await _esperar(lambda: "7" in _textos(pagina))

        # Y ahora termina la vieja, ocho segundos tarde como en la medición real.
        freno_viejo.set()
        await _dejar_pasar()

        pintado = _textos(pagina)
        assert not any("19.4917" in texto for texto in pintado), (
            f"la lectura vieja pisó a la nueva: {pintado}"
        )
        assert "0.5" not in pintado, f"la lectura vieja pintó su red de más: {pintado}"
        assert "7" in pintado, pintado
        assert pagina._snapshot is not None
        assert pagina._snapshot.profile.address == OTRA_DIRECCION
