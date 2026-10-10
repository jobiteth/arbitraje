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

### La fila y el detalle

La lista ya no es una tabla: cada token es una **fila pulsable** (`_TokenRow`) y
al pulsarla se abre el detalle, que es donde viven «Enviar», «Recibir» y «Swap»
y la lista de «Actividad». Lo que se mide aquí es esa navegación entera —el
ratón, el cambio de vista, el botón que emite el token y la vuelta a la lista—,
con los clics sintetizados por `QTest` sobre la fila de verdad.

### La clave privada

Es la primera cuenta de Hardhat, publicada y sin fondos. Está en un almacén de
memoria, nunca en el llavero del sistema, y lo que se comprueba con ella es
**qué botones se encienden**, no que se firme nada: aquí no se firma ni se emite.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLabel, QWidget

from amigocompora.app.container import Container, build_container
from amigocompora.app.execution_policy import (
    APPROVAL_KIND,
    ORDER_KIND,
    RECEIVE_KIND,
    REDEEM_KIND,
    LedgerEntry,
)
from amigocompora.domain.chains import CHAINS
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
from amigocompora.infra.wallets import LEGACY_WALLET_ID, StoredWallet, WalletSource
from amigocompora.ui.pages import wallet as wallet_module
from amigocompora.ui.pages.prices import PricesPage
from amigocompora.ui.pages.wallet import (
    ROW_ICON_PX,
    DepositDialog,
    WalletPage,
    _entry_label,
    _format_total,
    _format_value,
    badged_token_pixmap,
    qr_pixmap,
)
from amigocompora.ui.theme import COLOR_CARD
from amigocompora.ui.widgets import Chip, format_amount

#: La primera cuenta de Hardhat, publicada y sin fondos. Que sea conocida es lo
#: que la hace útil: no es un secreto y por eso puede estar escrita aquí.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION_DE_DESARROLLO = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

#: La segunda y la tercera cuenta de Hardhat, por el mismo motivo: para añadir
#: más de una cartera hacen falta claves distintas, y ninguna es un secreto.
CLAVE_DE_DESARROLLO_2 = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
CLAVE_DE_DESARROLLO_3 = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"

#: La contraseña de los tests **no** es la de nadie: la de verdad se escribe al
#: usarla y no se guarda en el repositorio, entre otras cosas porque un test no
#: debe fijarla.
CONTRASENA_DE_PRUEBA = "contrasena-de-prueba"

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


def _asiento(
    *,
    symbol: str,
    kind: str = "transaction",
    engine_id: str = "wallet",
    pair: str = "Retirada RARO → 0x0000…dEaD",
    chain: str = "polygon",
    at: datetime | None = None,
    status: str = "success",
    tokens: tuple[str, ...] | None = None,
    description: str = "",
) -> LedgerEntry:
    """Un renglón del registro como los que escribe la aplicación de verdad."""
    return LedgerEntry(
        occurred_at=at or datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        chain=chain,
        pair=pair,
        engine_id=engine_id,
        notional="1",
        notional_symbol="USDC",
        tx_hash="0x" + "12" * 32,
        status=status,
        recipient="0x" + "34" * 20,
        description=description,
        kind=kind,
        tokens=(symbol,) if tokens is None else tokens,
    )


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
def _filas(pagina: WalletPage) -> list[wallet_module._TokenRow]:
    """Las filas que están pintadas ahora mismo."""
    return pagina._row_widgets


def _simbolos(pagina: WalletPage) -> list[str]:
    """El símbolo de cada fila.

    El nombre de la fila es el símbolo y nada más —el desempate de homónimos
    vive en el tooltip y en el detalle—, así que lo que se compara aquí es lo
    que se ve.
    """
    return [fila._name.text().split(" ")[0] for fila in pagina._row_widgets]


def _fila_de(pagina: WalletPage, symbol: str) -> wallet_module._TokenRow:
    """La fila pintada de ese símbolo, o un fallo si no está."""
    for fila in pagina._row_widgets:
        if fila._name.text().split(" ")[0] == symbol:
            return fila
    raise AssertionError(f"«{symbol}» no está pintado: {_simbolos(pagina)}")


def _abrir_detalle(pagina: WalletPage, symbol: str) -> None:
    """Pulsa la fila de ese símbolo, como haría el ratón."""
    fila = _fila_de(pagina, symbol)
    QTest.mouseClick(fila, Qt.MouseButton.LeftButton)
    assert not pagina._detail_view.isHidden(), "la fila tiene que abrir el detalle"


def _asientos(pagina: WalletPage) -> list[QWidget]:
    """Las filas de «Actividad» pintadas ahora mismo, en orden.

    Se leen del **layout** y no con `findChildren` sobre el contenedor porque
    `_empty_layout` destruye las filas viejas con `deleteLater`: hasta que el
    bucle de Qt procese esa destrucción, las generaciones anteriores siguen
    colgando del padre y un `findChildren` las contaría dos veces. El layout, en
    cambio, sólo tiene las de ahora —`takeAt` las saca al reconstruir—.
    """
    filas: list[QWidget] = []
    for indice in range(pagina._activity_box.count()):
        item = pagina._activity_box.itemAt(indice)
        fila = item.widget() if item is not None else None
        if fila is not None:
            filas.append(fila)
    return filas


def _mirar(pagina: WalletPage, direccion: str) -> None:
    """Mira otra cartera y vuelve a leer, como hace «Mirar otra…»."""
    pagina._address.setText(direccion)
    pagina._profile = None
    pagina.refresh()


def _buscar(pagina: WalletPage, texto: str) -> None:
    """Escribe en el buscador, desplegándolo antes como hace la lupa.

    El campo nace plegado, así que escribir en él sin desplegarlo mediría el
    filtro pero no el gesto con el que se llega a él.
    """
    if pagina._search.isHidden():
        pagina._search_btn.setChecked(True)
    pagina._search.setText(texto)


def _filtrar_red(pagina: WalletPage, chain_key: str | None) -> None:
    """Filtra la lista de la cartera por esa red, por donde entrega el modal.

    Se llama al manejador al que el modal entrega la elección en vez de abrir el
    diálogo —un `exec()` sin persona delante no termina—; el modal de verdad se
    construye y se comprueba aparte.
    """
    pagina._set_chain(chain_key)


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
        await _esperar(lambda: len(_filas(pagina)) > 0)

    assert pagina._address.text() == DIRECCION_DE_DESARROLLO
    assert pagina._profile is not None
    # El perfil lleva la identidad **real** de la cartera del libro: con varias
    # carteras, un «Mi cartera» fijo haría que dos distintas se pintaran igual.
    # La del llavero es la «1» y su identificador es el de la heredada.
    assert pagina._profile.label == "1"
    assert pagina._profile.wallet_id == LEGACY_WALLET_ID
    assert pagina._profile.kind is WalletKind.EVM


async def test_sin_clave_la_pestana_lo_dice_en_vez_de_quedarse_en_blanco() -> None:
    motor = CarteraFalsa({})
    async with _cartera(motor, con_cartera=False) as container:
        pagina = WalletPage(container)

    assert pagina._address.text() == ""
    assert _filas(pagina) == []
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
        # Lo que se lee de verdad: las redes del catálogo que cubre esta familia
        # y que el motor declara saber leer. Las dos condiciones, y por eso se
        # calcula aquí en vez de escribirse: una lista de once nombres a mano se
        # quedaría vieja en cuanto el catálogo creciera.
        esperadas = {
            key
            for key in declaradas
            if key in CHAINS and WalletKind.of_chain(key) is WalletKind.EVM
        }
        await _esperar(lambda: set(motor.leidas) == esperadas)

        assert set(motor.leidas) == esperadas
        # Y ésas son las que ofrece el selector de red: ni una más. «Todas las
        # redes» no está en la tupla porque no es una red —es la ausencia de
        # filtro, y su fila se comprueba en el modal, que es donde vive—.
        assert set(pagina._chain_keys) == esperadas

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
        await _esperar(lambda: len(_filas(pagina)) > 0)

    assert _simbolos(pagina) == ["POL", "RARO"]
    # El nombre de la fila es el símbolo a secas: ni «(nativo)» ni la dirección.
    # Lo que desempata homónimos vive en el tooltip, que es donde cabe.
    fila = _fila_de(pagina, "POL")
    assert fila._name.text() == "POL"
    assert "POL (nativo)" in fila.toolTip()
    # La cantidad recorta a cuatro decimales —19.49166 se enseña 19.4916— y el
    # valor no se pregunta por un token sin motores que lo coticen.
    assert fila._amount.text() == "19.4916 POL"
    assert "19.49166614" in fila._amount.toolTip()
    assert fila._value.text() == "sin cotización"
    # La red ya no es una columna: va en el tooltip de la fila, que es donde se
    # lee sin ocupar el ancho de un panel de 420 px.
    assert "Polygon PoS" in fila.toolTip()


async def test_la_fila_quita_el_desempate_del_nombre() -> None:
    """En la lista se lee el símbolo; el desempate de homónimos va al tooltip.

    «POL (nativo)» y «RARO 0xabababab…» eran dos formas de meter en el nombre lo
    que ya dice el detalle —la red y el contrato—, y el nombre es lo que se lee
    de un vistazo. La identidad entera sigue a un tooltip de distancia.
    """
    motor = CarteraFalsa(
        {"polygon": (_nativo("polygon", "2"), _token("polygon", "RARO", "3.25"))}
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) == 2)

    nativo = _fila_de(pagina, "POL")
    assert nativo._name.text() == "POL"
    assert "(nativo)" not in nativo._name.text()
    con_contrato = _fila_de(pagina, "RARO")
    assert con_contrato._name.text() == "RARO"
    # Y la dirección no desaparece: espera en el tooltip de la fila, que es
    # donde se lee sin ocupar el ancho del nombre.
    assert "0xabababab" in con_contrato.toolTip()


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
        await _esperar(lambda: len(_filas(pagina)) > 0)
        antes = len(_filas(pagina))

        pagina._only_positive.setChecked(False)
        despues = len(_filas(pagina))

    assert antes == 1
    assert despues == 2


async def test_un_saldo_a_cero_vale_cero_y_no_se_dice_que_falte_precio() -> None:
    """Cero no es «no se sabe».

    La valoración no pregunta por un saldo vacío —no hay nada que valorar—, así
    que el valor se quedaba en «sin cotización», que es el texto de las
    posiciones **con fondos** a las que no se les pudo poner precio. Al lado de
    un 0 eso se lee como que la cotización no llega, y culpa a un motor que
    nunca fue preguntado. La cifra que sí se puede afirmar sin consultar a nadie
    es cero: cero por cualquier precio es cero.
    """
    motor = CarteraFalsa(
        {"polygon": (_nativo("polygon", "19.49166614"), _referencia("polygon", "0"))}
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)
        pagina._only_positive.setChecked(False)
        await _esperar(lambda: len(_filas(pagina)) > 1)

        valor = _fila_de(pagina, "USDC")._value

    assert valor.text() == "0.00 USDC", "la stablecoin de la red es la unidad en la que se valora"
    assert "sin cotización" not in valor.text()
    assert "no se preguntó" in valor.toolTip()
    # Y el caso de verdad —con fondos y sin precio— sigue diciéndolo, que es lo
    # que distingue un hueco de un cero.
    assert _fila_de(pagina, "POL")._value.text() == "sin cotización"


async def test_una_red_caida_no_se_confunde_con_una_red_vacia() -> None:
    """Una red que no contestó y una red vacía se ven igual en la lista —ninguna
    fila— y no son lo mismo: de la primera no se sabe nada."""
    motor = CarteraFalsa(
        {"base": (_referencia("base", "10"),)},
        fallos={"polygon": "ningún nodo de «polygon» contestó"},
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)

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
        await _esperar(lambda: len(_filas(pagina)) > 0)

        _mirar(pagina, "0x1234")

        assert _filas(pagina) == []
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
        # El selector ofrece las redes de **su** familia: una sola. «Todas las
        # redes» se comprueba en el modal, que es donde vive.
        assert pagina._chain_keys == ("solana",)


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
        assert _filas(pagina) == []


# --------------------------------------------------------------------------- #
# 2. El detalle de un token
# --------------------------------------------------------------------------- #
async def test_pulsar_una_fila_abre_el_detalle_y_volver_lo_cierra() -> None:
    """La fila entera es el botón, y la vuelta deja la lista como estaba.

    Se pulsa con `QTest`, que sintetiza el ratón de verdad: es lo que comprueba
    que la fila responde al clic y no sólo que la señal existe.
    """
    motor = CarteraFalsa({"polygon": (_token("polygon", "RARO", "3.25"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)

        assert pagina._list_view.isHidden() is False
        _abrir_detalle(pagina, "RARO")

        assert pagina._list_view.isHidden() is True
        assert pagina._detail_holding is not None
        # El nombre, a secas: el contrato se dice en su línea de abajo, y
        # repetir la dirección aquí era decir dos veces lo mismo.
        assert pagina._d_name.text() == "RARO"
        assert pagina._d_amount.text() == "3.25 RARO"
        # El contrato, abreviado a la vista y completo en el tooltip: es lo que
        # distingue dos tokens con el mismo símbolo.
        assert pagina._d_contract.text() == "Contrato 0xabab…abab"
        assert "0xabab" in pagina._d_contract.toolTip()
        assert pagina._d_copy.isHidden() is False
        assert pagina._d_value.text() == "sin cotización"
        # Sin cotización tampoco hay precio por unidad, y se dice en vez de
        # dividir por un valor que no existe.
        assert pagina._d_price.text() == "sin cotización"

        pagina._on_back()

        assert pagina._detail_view.isHidden() is True
        assert pagina._list_view.isHidden() is False
        assert pagina._detail_holding is None


async def test_el_detalle_de_la_moneda_nativa_no_ofrece_copiar_contrato() -> None:
    """Sin contrato no hay nada que copiar, así que el botón no está.

    Un botón de copiar que copiara la cadena vacía no sería un botón: parecería
    que algo se copió cuando lo que se pegó fue nada.
    """
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "19.49166614"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)

        _abrir_detalle(pagina, "POL")

        assert pagina._d_name.text() == "POL"
        assert pagina._d_copy.isHidden() is True
        assert "no tiene contrato" in pagina._d_contract.text()
        assert "Polygon PoS" in pagina._d_contract.text()
        # La cantidad recortada a la vista y la entera en el tooltip.
        assert pagina._d_amount.text() == "19.4916 POL"
        assert "19.49166614" in pagina._d_amount.toolTip()


async def test_copiar_el_contrato_lo_deja_en_el_portapapeles() -> None:
    """Copiar es lo único que hace el botón, así que se comprueba el portapapeles."""
    motor = CarteraFalsa({"polygon": (_token("polygon", "RARO", "3.25"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)
        _abrir_detalle(pagina, "RARO")

        QApplication.clipboard().setText("")
        pagina._d_copy.click()

    assert QApplication.clipboard().text() == "0x" + "ab" * 20
    assert "0xabab" in pagina._d_copy.toolTip()


async def test_el_detalle_se_cierra_si_el_token_desaparece_en_la_siguiente_lectura() -> None:
    """Un detalle abierto se recuelga de la lectura vigente, y si su token ya no
    está, vuelve a la lista.

    Lo contrario —quedarse con la foto del token— enseñaría el saldo de una
    cartera que ya no es la que se está mirando.
    """
    motor = CarteraConFreno(
        {
            DIRECCION_DE_DESARROLLO: {"polygon": (_token("polygon", "RARO", "3.25"),)},
            OTRA_DIRECCION: {},
        }
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)
        _abrir_detalle(pagina, "RARO")

        # Otra dirección sin ningún saldo: el RARO que se está mirando ya no está.
        _mirar(pagina, OTRA_DIRECCION)
        await _esperar(
            lambda: pagina._snapshot is not None
            and pagina._snapshot.profile.address == OTRA_DIRECCION
        )

        assert pagina._detail_holding is None
        assert pagina._detail_view.isHidden() is True
        assert pagina._list_view.isHidden() is False


# --------------------------------------------------------------------------- #
# 3. «Actividad»: lo que ejecutó la aplicación, y sólo eso
# --------------------------------------------------------------------------- #
async def test_la_actividad_ensena_los_asientos_del_token_y_solo_los_suyos() -> None:
    """El filtro es por token y por red, y el orden es del más nuevo al más viejo."""
    motor = CarteraFalsa({"polygon": (_token("polygon", "RARO", "3.25"),)})
    async with _cartera(motor) as container:
        ledger = container.policy.ledger
        ledger.append(
            _asiento(
                symbol="RARO",
                at=datetime(2026, 10, 9, 10, 0, tzinfo=UTC),
                pair="Retirada RARO → 0x0000…dEaD",
            )
        )
        ledger.append(
            _asiento(
                symbol="RARO",
                kind=REDEEM_KIND,
                engine_id="polymarket",
                at=datetime(2026, 10, 9, 11, 0, tzinfo=UTC),
                pair="Cobro de 5 participaciones",
            )
        )
        # Otro token —y el mismo token en otra red— no son actividad de éste.
        ledger.append(_asiento(symbol="OTRO", at=datetime(2026, 10, 9, 12, 0, tzinfo=UTC)))
        ledger.append(
            _asiento(
                symbol="RARO",
                chain="base",
                at=datetime(2026, 10, 9, 13, 0, tzinfo=UTC),
            )
        )

        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)
        _abrir_detalle(pagina, "RARO")

        filas = _asientos(pagina)
        chips = [chip.text() for fila in filas for chip in fila.findChildren(Chip)]
        textos = [etiqueta.text() for fila in filas for etiqueta in fila.findChildren(QLabel)]

    assert chips == ["Cobro", "Envío"], "el más nuevo primero, y sólo los de este token"
    assert any("Cobro de 5 participaciones" in texto for texto in textos)
    assert pagina._activity_empty.isHidden() is True


async def test_la_actividad_lee_los_asientos_viejos_sin_campo_de_tokens() -> None:
    """Los renglones escritos antes de que existiera `tokens` se emparejan por
    palabra exacta del par, para que la lista no quede vacía al actualizar."""
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "1"),)})
    async with _cartera(motor) as container:
        container.policy.ledger.append(
            _asiento(symbol="POL", pair="WETH/POL", tokens=(), engine_id="uniswap-v3")
        )

        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)
        _abrir_detalle(pagina, "POL")

        chips = [
            chip.text()
            for fila in _asientos(pagina)
            for chip in fila.findChildren(Chip)
        ]

    assert chips == ["Movimiento"], (
        "sin el motor en ninguna ranura del registro, la etiqueta honesta es «Movimiento»"
    )


async def test_sin_asientos_la_actividad_dice_que_solo_consta_lo_ejecutado_aqui() -> None:
    """La ausencia de movimientos no se disfraza de «no ha pasado nada».

    Una transferencia que llegue de fuera no pasa por el registro, así que una
    lista vacía no significa que la cartera no se moviera: significa que esta
    aplicación no movió nada. El texto lo dice con esas palabras.
    """
    motor = CarteraFalsa({"polygon": (_token("polygon", "RARO", "3.25"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)
        _abrir_detalle(pagina, "RARO")

        assert pagina._activity_empty.isHidden() is False
        assert "esta aplicación" in pagina._activity_empty.text()
        assert _asientos(pagina) == []


def test_la_etiqueta_de_un_asiento_sale_del_tipo_y_del_motor() -> None:
    """«Envío», «Swap» y «Puente» se distinguen por el motor que lo ejecutó —que
    es el dato— y no por el texto del par, que es una frase hecha."""
    puentes = frozenset({"lifi"})
    swaps = frozenset({"uniswap-v3"})

    def etiqueta(entry: LedgerEntry) -> str:
        return _entry_label(entry, bridges=puentes, dexes=swaps)

    assert etiqueta(_asiento(symbol="X", kind=REDEEM_KIND)) == "Cobro"
    assert etiqueta(_asiento(symbol="X", kind=ORDER_KIND)) == "Orden"
    assert etiqueta(_asiento(symbol="X", kind=RECEIVE_KIND)) == "Recepción"
    assert etiqueta(_asiento(symbol="X", kind=APPROVAL_KIND)) == "Permiso"
    assert etiqueta(_asiento(symbol="X", engine_id="wallet")) == "Envío"
    assert etiqueta(_asiento(symbol="X", engine_id="lifi")) == "Puente"
    assert etiqueta(_asiento(symbol="X", engine_id="uniswap-v3")) == "Swap"
    assert etiqueta(_asiento(symbol="X", engine_id="desconocido")) == "Movimiento"


# --------------------------------------------------------------------------- #
# 4. El total, que es donde se puede mentir
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
        await _esperar(lambda: len(_filas(pagina)) > 1)

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
        await _esperar(lambda: len(_filas(pagina)) > 0)

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
        await _esperar(lambda: len(_filas(pagina)) > 0)

    assert pagina._total.text() == "—"
    assert "a medias" in pagina._total_hint.text()


# --------------------------------------------------------------------------- #
# 5. Las cifras: recortadas para leer, enteras para comprobar
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


def test_la_cantidad_de_una_fila_recorta_a_cuatro_decimales_sin_mentir() -> None:
    """La cifra grande de una fila **recorta** en vez de redondear: es un saldo, y
    decir 19.4917 cuando hay 19.49166 enseña un número que la cadena no tiene.

    Y no puede colapsar el polvo a cero: una cantidad que no llega al cuarto
    decimal se enseña entera, porque «0.0000» se lee como «no hay nada».
    """
    assert _format_total(Decimal("19.49166614")) == "19.4916"
    assert _format_total(Decimal("12.5")) == "12.5"
    assert _format_total(Decimal("0")) == "0"
    assert _format_total(Decimal("0.00006")) == "0.00006"
    assert _format_total(Decimal("123456789012345678.987654")) == "123456789012345678.9876"


def test_el_valor_se_ensena_con_dos_decimales_y_sin_un_cero_falso() -> None:
    """El valor sí redondea al céntimo —es una estimación— salvo cuando redondear
    diría «0.00» de algo que no es cero, que es la única lectura peor."""
    assert _format_value(Decimal("12.4823")) == "12.48"
    assert _format_value(Decimal("12.4851")) == "12.49"
    assert _format_value(Decimal("12.5")) == "12.50"
    assert _format_value(Decimal("0")) == "0.00"
    assert _format_value(Decimal("0.003")) == "0.003"


def test_la_insignia_pega_el_logo_de_la_red_al_del_token() -> None:
    """El logo redondo del token con el de su red en un cuadro, abajo a la derecha.

    Es lo que distingue el USDC de Polygon del de Base en la lista, así que se
    comprueba sobre los píxeles: hay tinta en el centro —el token— y en la
    insignia, el marco de la insignia es un **cuadrado** del color de la
    tarjeta —y ahí el anillo redondo habría dejado pasar el pixel del token—, y
    la esquina del lienzo está vacía porque el logo es redondo. La escala
    declarada es la que evita el borrón en una pantalla al 200 %.
    """
    pol = native_token("polygon")
    pixmap = badged_token_pixmap(pol)
    imagen = pixmap.toImage()

    assert pixmap.width() == ROW_ICON_PX * 2
    assert pixmap.devicePixelRatio() == 2
    assert imagen.pixelColor(pixmap.width() // 2, pixmap.width() // 2).alpha() > 0
    # La insignia va pegada a la esquina: marco de 4 px (2 lógicos) y 30 px de
    # icono, así que su centro cae en 68 - 4 - 15 = 49.
    assert imagen.pixelColor(49, 49).alpha() > 0
    # Ese pixel cae en el marco de la insignia —la caja va de x=30 a 68 y el
    # icono empieza en 34—, a la altura del centro, lejos de las esquinas
    # redondeadas: con el anillo viejo ahí habría tinta del token, no de la
    # tarjeta, y es lo que distingue el cuadro del anillo.
    assert imagen.pixelColor(32, 49) == QColor(COLOR_CARD)
    # Y la esquina superior izquierda cae fuera del círculo del logo: el token
    # está recortado en redondo, no es un cuadrado con las esquinas pintadas.
    assert imagen.pixelColor(1, 1).alpha() == 0


async def test_la_fila_ensena_la_cifra_recortada_y_su_entera_en_el_tooltip() -> None:
    """El recorte es de la pantalla, no del dato: la cifra entera está siempre a
    un tooltip de distancia, sin tener que pulsar nada."""
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "19.49166614"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)

        fila = _fila_de(pagina, "POL")

    assert fila._amount.text() == "19.4916 POL"
    assert "19.49166614" in fila._amount.toolTip()


async def test_el_recorte_no_toca_lo_que_se_firmaria() -> None:
    """Lo que se recorta es una cadena de texto; el importe vive en el `TokenAmount`.

    Se comprueba sobre el propio objeto: el raw del saldo es el de la cadena, no
    el que resultaría de redondear el texto que se pinta.
    """
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "19.49166614"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)

        _, holding = pagina._rows[0]
        pintado = _fila_de(pagina, "POL")._amount.text()

    assert holding.amount.raw == 19_491_666_140_000_000_000
    assert pintado == "19.4916 POL"
    assert pintado != f"{holding.as_decimal():f}"


# --------------------------------------------------------------------------- #
# 6. El buscador
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
        await _esperar(lambda: len(_filas(pagina)) > 0)

        _buscar(pagina, "0x" + "cd" * 20)

        assert _filas(pagina) == []
        # `isHidden` y no `isVisible`: la ventana no se enseña en la prueba, así
        # que `isVisible` sería falso pasara lo que pasara.
        assert not pagina._empty.isHidden()
        assert "añádelo" in pagina._empty.text()

        # Y el token que sí está vuelve al borrar el filtro, sin releer nada: el
        # buscador filtra lo que ya se leyó.
        leidas = len(motor.leidas)
        pagina._search.setText("")
        assert len(_filas(pagina)) == 1
        assert len(motor.leidas) == leidas


# --------------------------------------------------------------------------- #
# 7. Los mandos de la lista: la lupa y el selector de red
# --------------------------------------------------------------------------- #
async def test_la_lupa_despliega_el_buscador_y_al_plegarlo_lo_vacia() -> None:
    """El buscador vive plegado tras la lupa, y plegarlo no deja el filtro puesto.

    Un filtro escondido junto a su campo es un filtro que desaparece sin decirlo:
    si plegar dejara el texto dentro, faltarían filas y nada en la pantalla
    diría por qué. Lo que se ve es lo que está puesto.
    """
    motor = CarteraFalsa(
        {"polygon": (_nativo("polygon", "2"), _token("polygon", "RARO", "3.25"))}
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) == 2)

        assert pagina._search.isHidden() is True, "nace plegado"

        pagina._search_btn.setChecked(True)
        assert pagina._search.isHidden() is False, "la lupa lo despliega"
        pagina._search.setText("RARO")
        assert _simbolos(pagina) == ["RARO"], "y filtra como siempre"

        pagina._search_btn.setChecked(False)
        assert pagina._search.isHidden() is True
        assert pagina._search.text() == "", "plegarlo vacía el filtro"
        assert len(_filas(pagina)) == 2, "y la lista entera vuelve"


class _SelectorFalso:
    """El modal de redes, falseado: mira lo que recibe y elige lo que se le diga.

    Sustituye al diálogo entero porque un `exec()` sin persona delante no
    termina nunca: lo que se comprueba con esto es el camino botón → elección →
    filtro. El modal de verdad se construye a mano en la prueba de al lado.
    """

    elegido: ClassVar[str | None] = None
    visto: ClassVar[tuple[tuple[str, ...], str | None] | None] = None

    def __init__(
        self,
        chains: tuple[str, ...],
        current: str | None,
        parent: QWidget | None = None,
    ) -> None:
        del parent
        type(self).visto = (tuple(chains), current)

    def setWindowTitle(self, title: str) -> None:  # noqa: N802
        """El «+» de token abre este modal con su propio título; aquí no hay ventana."""
        del title

    def exec(self) -> int:
        return int(QDialog.DialogCode.Accepted)

    def chosen(self) -> str | None:
        return type(self).elegido


async def test_el_selector_de_red_filtra_la_lista_y_el_boton_dice_cual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El botón de red abre el modal, y lo elegido filtra sin releer nada.

    Se comprueban las tres piezas del gesto: el modal recibe las redes que esta
    cartera puede leer junto al filtro puesto, elegir una deja sólo sus filas, y
    el botón dice cuál está puesta —«▼ Red: Base»— o vuelve a «▼ Todas las
    redes». El filtro es de la vista: la lectura no se repite.
    """
    motor = CarteraFalsa(
        {"polygon": (_nativo("polygon", "2"),), "base": (_referencia("base", "5"),)}
    )
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) == 2)
        # Se espera también a la pasada valorada —la stablecoin sí se valora,
        # el nativo sin motores no—: el orden de las filas depende de si hay
        # valor, y medirlo a medias mediría el instante, no el filtro.
        await _esperar(
            lambda: any(fila._value.text() != "sin cotización" for fila in _filas(pagina))
        )
        assert pagina._chain_btn.text() == "▼ Todas las redes"

        monkeypatch.setattr(wallet_module, "ChainPickerDialog", _SelectorFalso)
        _SelectorFalso.elegido = "base"
        leidas = len(motor.leidas)
        pagina._chain_btn.click()

        assert _SelectorFalso.visto == (pagina._chain_keys, None)
        # El filtro se lee por una local anotada y no por el atributo: mypy
        # estrecha `pagina._chain_key` a «base» en el primer `assert` y luego la
        # comprobación de `None` le parecería código muerto.
        elegida: str | None = pagina._chain_key
        assert elegida == "base"
        assert _simbolos(pagina) == ["USDC"]
        assert pagina._chain_btn.text() == "▼ Red: Base"
        assert len(motor.leidas) == leidas, "filtrar no vuelve a pedir un nodo"

        _SelectorFalso.elegido = None
        pagina._chain_btn.click()
        vuelta: str | None = pagina._chain_key
        assert vuelta is None
        assert _simbolos(pagina) == ["USDC", "POL"], "«Todas» devuelve todo"
        assert pagina._chain_btn.text() == "▼ Todas las redes"


def test_el_modal_de_redes_trae_todas_las_redes_y_marca_la_puesta(
    qt_app: QApplication,
) -> None:
    """El contenido del modal: «Todas» primero, una fila por red, y la puesta marcada.

    Se construye el diálogo de verdad —sin abrirlo— porque lo que se mide es su
    contenido: qué filas ofrece, cuál viene elegida y qué devuelve al aceptar.
    Decidir por la vía de los hechos que «Todas las redes» es el filtro sin red
    es lo que permite que el botón vuelva a su estado de partida con el mismo
    gesto que cambia de una red a otra.
    """
    del qt_app
    dialogo = wallet_module.ChainPickerDialog(("base", "polygon"), "polygon")

    assert [
        dialogo._list.item(fila).text() for fila in range(dialogo._list.count())
    ] == ["Todas las redes", "Base", "Polygon PoS"]
    assert dialogo._list.currentRow() == 2, "la red puesta viene marcada"
    assert dialogo.chosen() == "polygon"

    dialogo._list.setCurrentRow(0)
    assert dialogo.chosen() is None, "«Todas las redes» es el filtro sin red"


# --------------------------------------------------------------------------- #
# 8. Depositar, retirar y el detalle como puerta
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


class _RetiradaFalsa:
    """El diálogo de retirada, sustituido: recoge cómo se abrió y no hace nada.

    Se sustituye el **símbolo del módulo** y no un método del diálogo porque el
    diálogo lo construye la propia página dentro de `_open_withdraw`; lo que se
    mide es con qué se le llamó —el token por defecto y la lista—, que es la
    parte que la página decide.
    """

    abiertos: ClassVar[list[dict[str, object]]] = []

    def __init__(
        self,
        tokens: tuple[Token, ...],
        *,
        default: Token | None = None,
        owner: str = "",
        parent: object = None,
    ) -> None:
        _ = parent
        self.abiertos.append({"tokens": tuple(tokens), "default": default, "owner": owner})

    def exec(self) -> QDialog.DialogCode:
        return QDialog.DialogCode.Rejected


class _DepositoFalso:
    """El diálogo de depósito, sustituido: recoge la dirección y la red."""

    abiertos: ClassVar[list[dict[str, str]]] = []

    def __init__(
        self,
        address: str,
        chain_key: str,
        parent: object = None,
        *,
        note: str | None = None,
    ) -> None:
        _ = (parent, note)
        self.abiertos.append({"address": address, "chain": chain_key})

    def exec(self) -> QDialog.DialogCode:
        return QDialog.DialogCode.Rejected


async def test_retirar_se_apaga_sin_clave_y_dice_donde_se_pone() -> None:
    """Sin cartera con clave no hay desde dónde sacar dinero, y el botón lo dice
    con el sitio exacto donde se añade en vez de quedarse mudo.

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
    assert "menú ☰" in pagina._withdraw_btn.toolTip()
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
    assert "no es la cartera activa" in pagina._withdraw_btn.toolTip()
    # Depositar sí: enseñar una dirección pública no necesita clave.
    assert pagina._deposit_btn.isEnabled()


async def test_con_la_cartera_propia_retirar_se_enciende() -> None:
    motor = CarteraFalsa({"base": (_nativo("base", "1"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)

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


async def test_enviar_abre_el_formulario_con_el_token_del_detalle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Enviar» es la retirada de siempre, con el token ya elegido.

    Un token que la cartera leyó del nodo pero que nadie añadió al catálogo
    tampoco puede quedarse fuera: se ofrece igual, porque es el que el usuario
    acaba de mirar.
    """
    _RetiradaFalsa.abiertos.clear()
    monkeypatch.setattr(wallet_module, "WithdrawDialog", _RetiradaFalsa)

    motor = CarteraFalsa({"polygon": (_token("polygon", "RARO", "3.25"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)
        _abrir_detalle(pagina, "RARO")

        assert pagina._send_btn.isEnabled()
        pagina._send_btn.click()

    assert len(_RetiradaFalsa.abiertos) == 1
    abierto = _RetiradaFalsa.abiertos[0]
    por_defecto = abierto["default"]
    assert isinstance(por_defecto, Token)
    assert por_defecto.symbol == "RARO"
    tokens = abierto["tokens"]
    assert isinstance(tokens, tuple)
    assert isinstance(tokens[0], Token)
    assert tokens[0].symbol == "RARO", "el token del detalle va primero"
    assert abierto["owner"] == DIRECCION_DE_DESARROLLO


async def test_retirar_sin_token_prefijado_abre_el_formulario_de_la_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El botón de la cabecera no prefija token: abre la red del desplegable."""
    _RetiradaFalsa.abiertos.clear()
    monkeypatch.setattr(wallet_module, "WithdrawDialog", _RetiradaFalsa)

    motor = CarteraFalsa({"polygon": (_nativo("polygon", "3"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)

        pagina._withdraw_btn.click()

    assert len(_RetiradaFalsa.abiertos) == 1
    abierto = _RetiradaFalsa.abiertos[0]
    assert abierto["default"] is None
    tokens = abierto["tokens"]
    assert isinstance(tokens, tuple)
    assert any(isinstance(token, Token) and token.is_native for token in tokens), (
        "la moneda de la red tiene que estar en la lista"
    )


async def test_recibir_abre_el_deposito_en_la_red_del_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Recibir» es el diálogo del QR con la red ya puesta en la del token.

    Es la única red que tiene sentido desde su detalle: con otra seleccionada, el
    aviso del diálogo y el QR dirían cosas distintas.
    """
    _DepositoFalso.abiertos.clear()
    monkeypatch.setattr(wallet_module, "DepositDialog", _DepositoFalso)

    motor = CarteraFalsa({"polygon": (_nativo("polygon", "3"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)
        _abrir_detalle(pagina, "POL")

        pagina._receive_btn.click()

    assert _DepositoFalso.abiertos == [
        {"address": DIRECCION_DE_DESARROLLO, "chain": "polygon"}
    ]


# --------------------------------------------------------------------------- #
# 9. El salto a la pestaña de swap
# --------------------------------------------------------------------------- #
async def test_swap_en_el_detalle_prepara_el_par_en_la_red_del_token() -> None:
    """«Swap» significa: la red, la pata de entrega y la de enfrente puestas, para
    no volver a buscar el token en dos desplegables.

    El camino es el de verdad —pulsar la fila, pulsar el botón—, porque lo que se
    comprueba es que el botón del detalle lleva **su** token y no el de la última
    fila tocada.
    """
    motor = CarteraFalsa({"polygon": (_token("polygon", "RARO", "5"),)})
    async with _cartera(motor) as container:
        wallet = WalletPage(container)
        await _esperar(lambda: len(_filas(wallet)) > 0)

        swap = PricesPage(container)
        wallet.swap_requested.connect(swap.prepare_with_token)

        _abrir_detalle(wallet, "RARO")
        wallet._swap_btn.click()
        entrega, recibe = swap._legs()

    assert swap._chain.currentData() == "polygon"
    assert entrega is not None
    assert entrega.symbol == "RARO"
    assert recibe is not None
    assert recibe.symbol == "USDC"


async def test_swap_la_moneda_nativa_la_pone_en_la_pata_de_entrega() -> None:
    """ETH y POL son lo que más se tiene y lo que más se convierte.

    Es el caso que estaba roto: el nativo no tiene contrato que pegar, así que no
    se puede guardar en el almacén de tokens añadidos, y la lista de swap lo dejaba
    fuera. «Swap» sobre el POL de una cuenta acababa en un botón que no llevaba a
    ninguna parte.
    """
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "19.49166614"),)})
    async with _cartera(motor) as container:
        wallet = WalletPage(container)
        await _esperar(lambda: len(_filas(wallet)) > 0)

        swap = PricesPage(container)
        wallet.swap_requested.connect(swap.prepare_with_token)

        _abrir_detalle(wallet, "POL")
        assert wallet._detail_holding is not None
        assert wallet._detail_holding.token.is_native
        wallet._swap_btn.click()
        entrega, recibe = swap._legs()

        assert entrega is not None
        assert entrega.is_native
        assert entrega.symbol == "POL"
        assert recibe is not None
        assert recibe.symbol == "USDC"
        # Y no ha quedado guardado como si fuera un token añadido: no tiene
        # contrato, así que el almacén no tendría con qué identificarlo.
        assert container.token_store.load() == ()


async def test_swap_un_token_que_no_estaba_lo_anade_a_la_lista() -> None:
    """Un token que la cartera ve porque lo leyó un nodo, pero que nadie añadió,
    tiene que entrar en la lista de swap en vez de fallar en silencio."""
    raro = _token("polygon", "RARO", "5", decimals=18)
    motor = CarteraFalsa({"polygon": (raro,)})
    async with _cartera(motor) as container:
        wallet = WalletPage(container)
        await _esperar(lambda: len(_filas(wallet)) > 0)

        swap = PricesPage(container)
        wallet.swap_requested.connect(swap.prepare_with_token)

        _abrir_detalle(wallet, "RARO")
        wallet._swap_btn.click()
        entrega, _ = swap._legs()
        guardado = [token.symbol for token in container.token_store.load()]

    assert entrega is not None
    assert entrega.symbol == "RARO"
    assert "RARO" in guardado


# --------------------------------------------------------------------------- #
# 10. El saldo en la propia tarjeta de conversión
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
    """Todo el texto pintado en las filas de la lista, etiqueta a etiqueta.

    Es lo que se compara para saber **de qué cartera** es lo que se ve: el nombre
    —que lleva la dirección del contrato—, la cantidad y el valor.
    """
    return [
        etiqueta.text()
        for fila in pagina._row_widgets
        for etiqueta in (fila._name, fila._amount, fila._value)
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
        await _esperar(lambda: any("7" in texto for texto in _textos(pagina)))

        # Y ahora termina la vieja, ocho segundos tarde como en la medición real.
        freno_viejo.set()
        await _dejar_pasar()

        pintado = _textos(pagina)
        assert not any("19.4916" in texto for texto in pintado), (
            f"la lectura vieja pisó a la nueva: {pintado}"
        )
        assert not any("0.5" in texto for texto in pintado), (
            f"la lectura vieja pintó su red de más: {pintado}"
        )
        assert any("7" in texto for texto in pintado), pintado
        assert pagina._snapshot is not None
        assert pagina._snapshot.profile.address == OTRA_DIRECCION


# --------------------------------------------------------------------------- #
# 11. El libro de carteras: la activa, el menú y sus diálogos
# --------------------------------------------------------------------------- #
# La pestaña dejó de ser «la» cartera para ser la del libro: arriba se elige
# cuál manda, el menú ☰ hace todo lo demás, y las claves que no vienen del
# llavero se guardan cifradas con una contraseña que se crea la primera vez.
# Lo que se mide aquí es esa gestión entera —con los diálogos falseados, como
# en el resto del archivo—: qué recibe cada uno, qué queda en el libro y qué se
# le dice al usuario.
def _desplegar_menu(pagina: WalletPage) -> None:
    """Emite el mismo aviso que Qt da justo antes de desplegar el menú ☰.

    Se emite la señal y no se llama al método porque lo que se comprueba es
    eso: que el menú **se repinta al abrirse**, no que exista el método.
    """
    pagina._menu.aboutToShow.emit()


class _CarterasFalso:
    """El modal de carteras, falseado: mira lo que recibe y elige lo que se le diga."""

    elegida: ClassVar[str | None] = None
    visto: ClassVar[tuple[tuple[StoredWallet, ...], str | None] | None] = None

    def __init__(
        self,
        wallets: Sequence[StoredWallet],
        current_id: str | None,
        parent: QWidget | None = None,
    ) -> None:
        del parent
        type(self).visto = (tuple(wallets), current_id)

    def exec(self) -> int:
        return int(QDialog.DialogCode.Accepted)

    def chosen(self) -> str | None:
        return type(self).elegida


class _FormularioFalso:
    """Un diálogo de dos campos, falseado: lo que la prueba pone es lo que devuelve."""

    visto: ClassVar[str | None] = None
    elegido: ClassVar[tuple[str, str]] = ("", "")

    def __init__(self, default_label: str, parent: QWidget | None = None) -> None:
        del parent
        type(self).visto = default_label

    def exec(self) -> int:
        return int(QDialog.DialogCode.Accepted)

    def chosen(self) -> tuple[str, str]:
        return type(self).elegido


class _ClaveFalsa(_FormularioFalso):
    """Sustituye a `SigningWalletDialog`: nombre y clave privada."""


class _ObservacionFalsa(_FormularioFalso):
    """Sustituye a `WatchWalletDialog`: nombre y dirección."""


class _ContrasenaFalsa:
    """El diálogo de la contraseña, falseado: entrega las respuestas **en orden**.

    Una por intento, porque el caso difícil que hay que poder medir es el bucle:
    con la contraseña equivocada el diálogo no se cierra, se vuelve a abrir con
    el error dentro. Sin respuestas se cancela, que es lo que evita que una
    prueba mal montada se quede girando en vez de fallar.
    """

    respuestas: ClassVar[list[str]] = []
    pedida: ClassVar[list[dict[str, object]]] = []

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        create: bool,
        motivo: str = "",
        error: str | None = None,
    ) -> None:
        del parent
        type(self).pedida.append({"create": create, "motivo": motivo, "error": error})

    def exec(self) -> int:
        return (
            int(QDialog.DialogCode.Accepted)
            if type(self).respuestas
            else int(QDialog.DialogCode.Rejected)
        )

    def password(self) -> str:
        return type(self).respuestas.pop(0)


class _ClaveVistaFalsa:
    """El diálogo que enseña la clave privada, falseado: recoge qué se le entregó."""

    visto: ClassVar[tuple[str, str] | None] = None

    def __init__(self, label: str, private_key: str, parent: QWidget | None = None) -> None:
        del parent
        type(self).visto = (label, private_key)

    def exec(self) -> int:
        return int(QDialog.DialogCode.Rejected)


class _BorradoFalso:
    """El diálogo de eliminar, falseado: confirma o no según lo que se le diga."""

    abierto: ClassVar[StoredWallet | None] = None
    confirmando: ClassVar[bool] = True

    def __init__(self, wallet: StoredWallet, parent: QWidget | None = None) -> None:
        del parent
        type(self).abierto = wallet

    def exec(self) -> int:
        return int(QDialog.DialogCode.Accepted)

    def confirmed(self) -> bool:
        return type(self).confirmando


class _EntradaFalsa:
    """El `QInputDialog` de texto, falseado: la respuesta la pone la prueba.

    Se sustituye el símbolo del **módulo** y no el método de Qt por el mismo
    motivo que los demás diálogos —los tipos de PySide6 no se dejan parchear—,
    y porque lo que hay que medir es con qué texto se pregunta.
    """

    respuesta: ClassVar[tuple[str, bool]] = ("", False)
    preguntas: ClassVar[list[str]] = []

    @staticmethod
    def getText(  # noqa: N802
        parent: QWidget | None,
        title: str,
        label: str,
        *,
        text: str = "",
        **extra: object,
    ) -> tuple[str, bool]:
        del parent, text, extra
        _EntradaFalsa.preguntas.append(f"{title}: {label}")
        return _EntradaFalsa.respuesta


def _guardada(wallet_id: str, label: str, address: str, source: WalletSource) -> StoredWallet:
    """Una cartera del libro, para los diálogos que sólo leen lo guardado."""
    return StoredWallet(
        wallet_id=wallet_id, label=label, address=address, kind=WalletKind.EVM, source=source
    )


async def test_la_cabecera_ensena_la_direccion_de_la_activa_con_su_copia() -> None:
    """La dirección activa, arriba y copiable con el mismo botón animado del detalle."""
    motor = CarteraFalsa({"polygon": (_referencia("polygon", "12.5"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)

        assert pagina._wallet_btn.text() == "1 ▾"
        assert pagina._address.text() == DIRECCION_DE_DESARROLLO
        assert pagina._copy_btn.isEnabled() is True
        assert "la dirección de la cartera" in pagina._copy_btn.toolTip()

        QApplication.clipboard().setText("")
        pagina._copy_btn.click()

        assert QApplication.clipboard().text() == DIRECCION_DE_DESARROLLO


async def test_el_nombre_abre_la_lista_y_cambiar_de_cartera_cambia_la_lectura(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Elegir cartera manda en toda la aplicación, y la lectura sigue a la elegida.

    Es el gesto que hace que «varias carteras» signifique algo: sin él, la
    nueva sería una entrada en un fichero que no cambia nada de lo que se ve.
    """
    motor = CarteraConFreno(
        {
            DIRECCION_DE_DESARROLLO: {"polygon": (_nativo("polygon", "19.49"),)},
            OTRA_DIRECCION: {"polygon": (_nativo("polygon", "7"),)},
        }
    )
    async with _cartera(motor) as container:
        miron = container.keys.add_watch(OTRA_DIRECCION, "Mirón")
        mia = next(w for w in container.keys.wallets() if w.source is WalletSource.KEYRING)
        pagina = WalletPage(container)
        # Abre con la activa del libro —la de observación—, no con la primera.
        await _esperar(lambda: any("7" in texto for texto in _textos(pagina)))
        assert pagina._wallet_btn.text() == "Mirón ▾"
        assert pagina._address.text() == OTRA_DIRECCION

        avisos: list[int] = []
        pagina.wallet_changed.connect(lambda: avisos.append(1))
        monkeypatch.setattr(wallet_module, "WalletPickerDialog", _CarterasFalso)
        _CarterasFalso.elegida = mia.wallet_id
        pagina._wallet_btn.click()

        visto = _CarterasFalso.visto
        assert visto is not None
        carteras, actual = visto
        assert [w.wallet_id for w in carteras] == [mia.wallet_id, miron.wallet_id]
        assert actual == miron.wallet_id, "la que está puesta viene marcada"
        activa = container.keys.active()
        assert activa is not None
        assert activa.wallet_id == mia.wallet_id
        assert avisos == [1], "el resto de la aplicación tiene que enterarse"
        assert pagina._wallet_btn.text() == "1 ▾"
        assert pagina._address.text() == DIRECCION_DE_DESARROLLO
        assert "Cartera activa" in pagina._notice.text()

        # Y la lista se relee para la nueva: no se queda la foto de la anterior
        # bajo la dirección recién puesta.
        await _esperar(lambda: any("19.49" in texto for texto in _textos(pagina)))


async def test_el_menu_de_carteras_se_repinta_con_lo_que_se_puede_hacer_ahora() -> None:
    """Gris con motivo, y «bloquear» sólo si hay almacén cifrado que bloquear.

    Un menú pintado una sola vez miente en cuanto cambia algo que no pasa por
    él —una contraseña que se crea, una cartera que se añade—, y el momento en
    el que no puede mentir es justo antes de que alguien lo use.
    """
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        _desplegar_menu(pagina)
        assert pagina._act_reveal.isEnabled() is True
        assert pagina._act_rename.isEnabled() is True
        assert pagina._act_delete.isEnabled() is True
        assert pagina._act_lock.isVisible() is False, "sin contraseña no hay sesión"

        # Una cartera en observación como activa: no hay clave que enseñar.
        container.keys.add_watch(OTRA_DIRECCION, "Mirón")
        _desplegar_menu(pagina)
        assert pagina._act_reveal.isEnabled() is False
        assert "observación" in pagina._act_reveal.toolTip()
        assert pagina._act_delete.isEnabled() is True

        # Una cartera cifrada: la sesión nace desbloqueada, y se puede bloquear.
        container.keys.add_signing(CLAVE_DE_DESARROLLO_2, "2", CONTRASENA_DE_PRUEBA)
        _desplegar_menu(pagina)
        assert pagina._act_lock.isVisible() is True
        assert pagina._act_lock.text() == "Bloquear cartera"
        assert pagina._act_reveal.isEnabled() is True

        container.keys.lock()
        _desplegar_menu(pagina)
        assert pagina._act_lock.text() == "Desbloquear cartera…"


async def test_anadir_en_modo_observacion_queda_en_el_libro_y_como_activa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Añadir una cartera de sólo lectura: queda en la lista y pasa a ser la mirada."""
    motor = CarteraFalsa({"polygon": (_nativo("polygon", "7"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(_filas(pagina)) > 0)

        monkeypatch.setattr(wallet_module, "WatchWalletDialog", _ObservacionFalsa)
        _ObservacionFalsa.elegido = ("Mirón", OTRA_DIRECCION)
        pagina._act_add_watch.trigger()

        assert _ObservacionFalsa.visto == "2", "el nombre propuesto es el primer número libre"
        assert [w.label for w in container.keys.wallets()] == ["1", "Mirón"]
        activa = container.keys.active()
        assert activa is not None
        assert activa.label == "Mirón"
        assert pagina._wallet_btn.text() == "Mirón ▾"
        assert pagina._address.text() == OTRA_DIRECCION
        assert "modo observación" in pagina._notice.text()
        await _dejar_pasar()


async def test_renombrar_la_cartera_activa_la_renombra_en_todo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El nombre es la etiqueta de la cartera, y cambiarlo cambia lo que la enseña."""
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)

        monkeypatch.setattr(wallet_module, "QInputDialog", _EntradaFalsa)
        _EntradaFalsa.preguntas = []
        _EntradaFalsa.respuesta = ("Ahorros", True)
        pagina._act_rename.trigger()

        assert _EntradaFalsa.preguntas[-1].startswith("Renombrar cartera")
        activa = container.keys.active()
        assert activa is not None
        assert activa.label == "Ahorros"
        assert pagina._wallet_btn.text() == "Ahorros ▾"
        assert "se llama «Ahorros»" in pagina._notice.text()
        await _dejar_pasar()


async def test_eliminar_la_cartera_pide_confirmacion_y_dice_que_queda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Borrar de verdad —el que no resucita— se confirma, y se dice qué se pierde."""
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)

        monkeypatch.setattr(wallet_module, "DeleteWalletDialog", _BorradoFalso)
        _BorradoFalso.confirmando = True
        pagina._act_delete.trigger()

        abierto = _BorradoFalso.abierto
        assert abierto is not None
        assert abierto.wallet_id == LEGACY_WALLET_ID
        assert container.keys.wallets() == ()
        assert pagina._wallet_btn.text() == "Sin cartera ▾"
        assert "La clave del llavero no se ha tocado" in pagina._notice.text()
        assert "No queda ninguna" in pagina._notice.text()
        await _dejar_pasar()


async def test_eliminar_sin_confirmar_no_toca_el_libro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelar la confirmación no borra nada: el «no» es un «no»."""
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)

        monkeypatch.setattr(wallet_module, "DeleteWalletDialog", _BorradoFalso)
        _BorradoFalso.confirmando = False
        pagina._act_delete.trigger()

        assert [w.label for w in container.keys.wallets()] == ["1"]
        assert pagina._wallet_btn.text() == "1 ▾"


async def test_mostrar_la_clave_la_primera_vez_crea_la_contrasena_y_migra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin contraseña todavía, «Mostrar clave» es donde se crea: y migra la del llavero.

    La clave del llavero pasa al libro cifrada con esa contraseña; la copia del
    llavero se queda como respaldo, y se dice.
    """
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)

        monkeypatch.setattr(wallet_module, "PasswordDialog", _ContrasenaFalsa)
        monkeypatch.setattr(wallet_module, "RevealKeyDialog", _ClaveVistaFalsa)
        _ContrasenaFalsa.pedida = []
        _ContrasenaFalsa.respuestas = [CONTRASENA_DE_PRUEBA]

        pagina._act_reveal.trigger()

        assert [p["create"] for p in _ContrasenaFalsa.pedida] == [True], "se crea, no se pide"
        assert _ClaveVistaFalsa.visto == ("1", CLAVE_DE_DESARROLLO)
        activa = container.keys.active()
        assert activa is not None
        assert activa.source is WalletSource.KEYSTORE
        assert activa.keystore is not None
        assert "llavero" in pagina._notice.text()
        await _dejar_pasar()


async def test_mostrar_la_clave_con_almacen_ya_creado_verifica_y_reintenta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con la contraseña ya creada se verifica descifrando, y el error se enseña dentro.

    Una contraseña equivocada no cierra el diálogo: vuelve a abrirse con el
    motivo escrito, que es lo que permite corregirla sin perder el gesto.
    """
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        container.keys.add_signing(CLAVE_DE_DESARROLLO_2, "2", CONTRASENA_DE_PRUEBA)

        monkeypatch.setattr(wallet_module, "PasswordDialog", _ContrasenaFalsa)
        monkeypatch.setattr(wallet_module, "RevealKeyDialog", _ClaveVistaFalsa)
        _ContrasenaFalsa.pedida = []
        _ContrasenaFalsa.respuestas = ["equivocada", CONTRASENA_DE_PRUEBA]

        pagina._act_reveal.trigger()

        assert [p["create"] for p in _ContrasenaFalsa.pedida] == [False, False]
        assert _ContrasenaFalsa.pedida[0]["error"] is None
        assert "no abre el almacén" in str(_ContrasenaFalsa.pedida[1]["error"])
        assert _ClaveVistaFalsa.visto == ("2", CLAVE_DE_DESARROLLO_2)
        await _dejar_pasar()


async def test_anadir_una_cartera_con_clave_la_cifra_con_la_contrasena_creada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La clave que se añade por el menú se cifra antes de guardarse, y es la activa."""
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)

        monkeypatch.setattr(wallet_module, "SigningWalletDialog", _ClaveFalsa)
        monkeypatch.setattr(wallet_module, "PasswordDialog", _ContrasenaFalsa)
        _ClaveFalsa.elegido = ("2", CLAVE_DE_DESARROLLO_2)
        _ContrasenaFalsa.pedida = []
        _ContrasenaFalsa.respuestas = [CONTRASENA_DE_PRUEBA]

        pagina._act_add_signing.trigger()

        assert _ClaveFalsa.visto == "2", "el nombre propuesto es el primer número libre"
        assert _ContrasenaFalsa.pedida[0]["create"] is True, "la primera vez se crea"
        activa = container.keys.active()
        assert activa is not None
        assert activa.source is WalletSource.KEYSTORE
        assert activa.keystore is not None
        assert "cifrada" in pagina._notice.text()
        assert pagina._wallet_btn.text() == "2 ▾"
        await _dejar_pasar()


async def test_anadir_otra_cartera_con_clave_pide_la_misma_contrasena(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Todas las claves comparten un único almacén, así que la contraseña se verifica."""
    motor = CarteraFalsa({})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        container.keys.add_signing(CLAVE_DE_DESARROLLO_2, "2", CONTRASENA_DE_PRUEBA)

        monkeypatch.setattr(wallet_module, "SigningWalletDialog", _ClaveFalsa)
        monkeypatch.setattr(wallet_module, "PasswordDialog", _ContrasenaFalsa)
        _ClaveFalsa.elegido = ("3", CLAVE_DE_DESARROLLO_3)
        _ContrasenaFalsa.pedida = []
        _ContrasenaFalsa.respuestas = [CONTRASENA_DE_PRUEBA]

        pagina._act_add_signing.trigger()

        assert _ContrasenaFalsa.pedida[0]["create"] is False, "ya existe: se verifica"
        assert [w.label for w in container.keys.wallets()] == ["1", "2", "3"]
        await _dejar_pasar()


def test_un_clic_en_la_lista_de_carteras_elige_y_cierra(qt_app: QApplication) -> None:
    """Un clic elige: no hay que acertar después con el botón «Elegir»."""
    carteras = (
        _guardada("a", "1", DIRECCION_DE_DESARROLLO, WalletSource.KEYRING),
        _guardada("b", "Mirón", OTRA_DIRECCION, WalletSource.WATCH),
    )
    dialogo = wallet_module.WalletPickerDialog(carteras, "a")
    dialogo.show()
    qt_app.processEvents()
    assert dialogo._list.currentRow() == 0, "la activa viene marcada"

    QTest.mouseClick(
        dialogo._list.viewport(),
        Qt.MouseButton.LeftButton,
        pos=dialogo._list.visualItemRect(dialogo._list.item(1)).center(),
    )

    assert dialogo.result() == int(QDialog.DialogCode.Accepted)
    assert dialogo.chosen() == "b"


def test_un_clic_en_el_modal_de_redes_elige_y_cierra(qt_app: QApplication) -> None:
    """El mismo gesto en el selector de red: un clic sobre una red la elige y vuelve."""
    dialogo = wallet_module.ChainPickerDialog(("base", "polygon"), None)
    dialogo.show()
    qt_app.processEvents()

    QTest.mouseClick(
        dialogo._list.viewport(),
        Qt.MouseButton.LeftButton,
        pos=dialogo._list.visualItemRect(dialogo._list.item(1)).center(),
    )

    assert dialogo.result() == int(QDialog.DialogCode.Accepted)
    assert dialogo.chosen() == "base"


async def test_el_mas_de_token_pregunta_la_red_antes_que_la_direccion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con «Todas las redes» el «+» pregunta la red en el mismo gesto.

    La dirección de un contrato sólo significa algo dentro de su red: antes
    sólo salía un aviso que obligaba a ir al selector, elegir la red y volver a
    pulsar el «+», y el gesto se quedaba a medias.
    """
    motor = CarteraFalsa({"base": (_referencia("base", "1"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(pagina._chain_keys) > 0)
        assert pagina._chain_key is None, "abre en «Todas las redes»"

        monkeypatch.setattr(wallet_module, "ChainPickerDialog", _SelectorFalso)
        monkeypatch.setattr(wallet_module, "QInputDialog", _EntradaFalsa)
        _SelectorFalso.elegido = "base"
        _EntradaFalsa.preguntas = []
        _EntradaFalsa.respuesta = (CONTRATO_USDC, True)

        pagina._add_btn.click()

        assert _SelectorFalso.visto == (pagina._chain_keys, pagina._chain_keys[0])
        assert "Base" in _EntradaFalsa.preguntas[-1], "la dirección se pide ya en su red"
        # Sin nodos configurados no se puede leer el contrato, y se dice en vez
        # de dejar el botón apagado sin explicación.
        await _esperar(lambda: pagina._add_btn.isEnabled())
        assert "no hay nodos configurados" in pagina._notice.text()


async def test_el_mas_de_token_no_adivina_la_red_por_el_usuario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Volver del modal sin una red concreta no es elegir «Todas»: se dice y no se sigue.

    Adivinar la red sería leer el contrato en la red equivocada —o en ocho— y
    guardar un token que no es.
    """
    motor = CarteraFalsa({"base": (_referencia("base", "1"),)})
    async with _cartera(motor) as container:
        pagina = WalletPage(container)
        await _esperar(lambda: len(pagina._chain_keys) > 0)

        monkeypatch.setattr(wallet_module, "ChainPickerDialog", _SelectorFalso)
        monkeypatch.setattr(wallet_module, "QInputDialog", _EntradaFalsa)
        _SelectorFalso.elegido = None
        _EntradaFalsa.preguntas = []
        pagina._add_btn.click()

        assert _EntradaFalsa.preguntas == [], "sin red no hay dirección que pedir"
        assert "sólo significa algo dentro de su red" in pagina._notice.text()


async def test_el_mas_de_token_sin_cartera_dice_donde_estan_las_redes() -> None:
    """Sin dirección que leer no hay ninguna red que ofrecer, y se dice."""
    motor = CarteraFalsa({})
    async with _cartera(motor, con_cartera=False) as container:
        pagina = WalletPage(container)

        pagina._add_btn.click()

        assert "No hay ninguna red que leer" in pagina._notice.text()
