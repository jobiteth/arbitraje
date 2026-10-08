"""La pantalla de swap: que quepa, que se entienda y que la cartera esté dentro.

### Qué se arregla aquí, y por qué hace falta una prueba

La queja, literal: «debería ser algo como wallet y swap, como la interfaz de
MetaMask; algunos botones se esconden, algunos campos no se ven; en predicciones
es imposible ver o hacer algo; es obsoleta, difícil de operar».

Lo medido detrás de esa queja, sobre capturas reales a 1400x900:

- **Ninguna página se desplazaba** y la ventana no baja de 1100x720, así que lo
  que no cabía **se recortaba**: los botones de «Entre redes» salían amputados a
  media altura y tres campos de Predicción se quedaban en 133 px cuando
  necesitaban 140. La primera prueba de este fichero es la que habría cazado eso
  y la que impide que vuelva.
- **La cartera vivía en su propia pestaña**, así que leer un saldo que se está a
  punto de firmar obligaba a ir y volver. Ahora es una sección dentro del swap,
  justo debajo de la tarjeta de conversión, que es el orden de una cartera de
  verdad: arriba lo que intercambias, debajo tu cuenta y tus tokens.

Las dos se afirman sobre la ventana **de verdad** y a 1100x720, que es el mínimo
declarado y el tamaño donde se recortaba, no sobre un tamaño cómodo que no usa
nadie.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QWidget,
)

from amigocompora.app.container import Container, build_container
from amigocompora.domain.models import (
    PriceComparison,
    Quote,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import OperationMode
from amigocompora.domain.money import BasisPoints
from amigocompora.domain.protocols import EngineManifest
from amigocompora.domain.wallet import ChainHoldings, TokenHolding, WalletProfile
from amigocompora.engines.catalog import native_token, quote_token, wrapped_native
from amigocompora.engines.wallet.engine import MANIFEST as WALLET_MANIFEST
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    app_secret_key,
)
from amigocompora.ui.main_window import MainWindow
from amigocompora.ui.widgets import Card

#: El tamaño mínimo declarado por la ventana. Se prueba aquí, y no a 1400x900,
#: porque es el tamaño en el que el contenido no cabía.
ANCHO_MINIMO = 1100
ALTO_MINIMO = 720

#: La primera cuenta de Hardhat, publicada y sin fondos. Está aquí para que la
#: cartera tenga una **dirección** que leer: sin clave configurada la página no
#: sale a la red, y una tabla de tokens vacía no probaría nada.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


class CarteraFalsa:
    """Una cartera con las posiciones que se le den, sin salir a la red."""

    manifest: EngineManifest = WALLET_MANIFEST

    def __init__(self, posiciones: Mapping[str, tuple[TokenHolding, ...]]) -> None:
        self._posiciones = dict(posiciones)

    async def aopen(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def holdings(
        self, profile: WalletProfile, chain_key: str, *, tokens: tuple[Token, ...] = ()
    ) -> ChainHoldings:
        del profile
        return ChainHoldings(chain=chain_key, holdings=self._posiciones.get(chain_key, ()))


class ProveedorFalso:
    def __init__(self, motor: CarteraFalsa) -> None:
        self._motor = motor
        self.manifest = motor.manifest

    def create(self, config: Mapping[str, str]) -> CarteraFalsa:
        del config
        return self._motor


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _store() -> InMemorySecretStore:
    """Un almacén con una cartera configurada, para que la pestaña lea de verdad."""
    store = InMemorySecretStore()
    store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    return store


@asynccontextmanager
async def _ventana(
    posiciones: Mapping[str, tuple[TokenHolding, ...]] | None = None,
) -> AsyncIterator[tuple[Container, MainWindow]]:
    """La ventana de verdad, con una cartera falsa si se pide alguna posición.

    Se falsea el motor de cartera —el único que sale a la red— y nada más: el
    contenedor, la guarda de modo, la composición de las pestañas y los widgets
    son los de producción, que es lo que se está midiendo.
    """
    container = await build_container(
        Settings.model_validate({"mode": OperationMode.OBSERVATION.value}),
        secret_store=_store(),
        discover=False,
        configure_logs=False,
    )
    try:
        if posiciones is not None:
            container.registry.register(ProveedorFalso(CarteraFalsa(posiciones)), source="prueba")
            await container.registry.activate("wallet")
        yield container, MainWindow(container, container.alert_center)
    finally:
        await container.aclose()


async def _esperar(condicion: Callable[[], bool]) -> None:
    """Deja pasar el turno hasta que la condición se cumpla, o falla con un mensaje."""
    for _ in range(500):
        if condicion():
            return
        await asyncio.sleep(0)
    raise AssertionError("la condición no se cumplió: la lectura no terminó")


def _campos(pagina: QWidget) -> list[QWidget]:
    """Los campos de una pestaña, a cualquier profundidad.

    Se mide el campo, no sus tripas: un `QDoubleSpinBox` lleva dentro un
    `QLineEdit` —`qt_spinbox_lineedit`— que es la parte escribible del mismo
    campo, y su ancho es el del campo menos sus botones. Medirlo daría recorte
    siempre, en todos los campos numéricos de la aplicación, y un aviso que suena
    siempre es un aviso que nadie lee. Lo que se mide es el spinbox entero, que es
    lo que el usuario ve.
    """
    campos: list[QWidget] = [
        *pagina.findChildren(QComboBox),
        *pagina.findChildren(QAbstractSpinBox),
    ]
    campos.extend(
        linea
        for linea in pagina.findChildren(QLineEdit)
        if not isinstance(linea.parent(), QAbstractSpinBox)
    )
    return campos


def _con_rutas(filas: int) -> PriceComparison:
    """Una comparación con las rutas que se pidan, una por venue.

    Dos filas idénticas no dirían nada sobre el alto: la tabla mide lo que mide su
    contenido, y el contenido tiene que ser distinto.
    """
    entrega = wrapped_native("base")
    recibe = quote_token("base")
    assert entrega is not None
    assert recibe is not None
    par = TradingPair(base=entrega, quote=recibe)
    venues = (
        Venue(venue_id="fake@base", name="fake", kind=VenueKind.DEX, chain="base"),
        Venue(venue_id="otro@base", name="otro", kind=VenueKind.DEX, chain="base"),
    )
    return PriceComparison(
        pair=par,
        amount_in=par.base.amount("1"),
        quotes=tuple(
            Quote(
                venue=venues[i % len(venues)],
                engine_id=f"motor_{i}",
                pair=par,
                amount_in=par.base.amount("1"),
                amount_out=par.quote.amount(str(2000 + i)),
                fee_bps=BasisPoints(5),
                price_impact_bps=BasisPoints(1),
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                source_note="Leído del pool falso contra el estado de la cadena.",
            )
            for i in range(filas)
        ),
    )


# --------------------------------------------------------------------------- #
# 1. Que quepa
# --------------------------------------------------------------------------- #
def _dentro_de_un_scroll(widget: QWidget, tope: QWidget) -> bool:
    """Si hay un armazón que se desplace entre este widget y la pestaña.

    Un widget metido en un `QScrollArea` es alcanzable aunque hoy caiga por
    debajo del borde: se baja hasta él. Uno que cuelga directamente de la pestaña
    y se sale, no está —lo que no cabe se recorta, y ése fue el defecto.
    """
    padre = widget.parentWidget()
    while padre is not None and padre is not tope:
        if isinstance(padre, QScrollArea):
            return True
        padre = padre.parentWidget()
    return False


def _amputados(pagina: QWidget) -> list[str]:
    """Lo que se sale de la pestaña y no hay forma de alcanzar.

    Es la medida del defecto literal —«algunos botones se esconden»—: la ventana
    no baja de 1100x720 y ninguna página se desplazaba, así que todo lo que no
    cabía quedaba fuera del borde inferior, recortado a media altura y sin manera
    de llegar a ello.
    """
    alto = pagina.height()
    fuera: list[str] = []
    for hijo in pagina.findChildren(QWidget):
        if not hijo.isVisible() or hijo.width() <= 1:
            continue
        if _dentro_de_un_scroll(hijo, pagina):
            continue
        borde = hijo.mapTo(pagina, hijo.rect().bottomLeft()).y()
        if borde > alto + 2:
            fuera.append(
                f"{type(hijo).__name__} «{hijo.objectName()}» llega a {borde} px "
                f"en una pestaña de {alto}"
            )
    return fuera


async def test_ninguna_pestana_recorta_su_contenido() -> None:
    """Ningún campo es más estrecho que su contenido y nada se sale sin salida.

    Son las dos formas del mismo defecto, y las dos que se midieron en las
    capturas originales:

    - **A lo ancho**: un campo más estrecho que lo que necesita deja el texto
      cortado o el hueco sin sitio para escribir. Es «algunos campos no se ven».
    - **A lo alto**: la ventana no baja de 1100x720 y ninguna página se desplazaba,
      así que lo que no cabía se recortaba contra el borde. Es «algunos botones se
      esconden» —y lo que hay debajo de ellos, que no se veía y por tanto no se
      echaba de menos—.

    Se recorre cada pestaña a 1100x720, el mínimo de la ventana: en un tamaño más
    holgado el contenido cabe por suerte, y una prueba que mide la suerte no mide
    nada. Y se pregunta por `isVisible()` —al revés que en el resto de la suite—
    porque la ventana se enseña de verdad, con la plataforma `offscreen`, para que
    Qt reparta el espacio: lo que se mide es un recorte real, y lo que no se ve no
    se recorta.
    """
    async with _ventana() as (_container, window):
        window.resize(ANCHO_MINIMO, ALTO_MINIMO)
        window.show()
        QApplication.processEvents()

        estrechos: list[str] = []
        cortados: list[str] = []
        for indice in range(window._tabs.count()):
            window._tabs.setCurrentIndex(indice)
            QApplication.processEvents()
            pagina = window._tabs.widget(indice)
            assert pagina is not None
            pestaña = window._tabs.tabText(indice)

            for campo in _campos(pagina):
                if not campo.isVisible():
                    continue
                necesita = campo.sizeHint().width()
                if necesita > campo.width() + 2:
                    estrechos.append(
                        f"{pestaña}: {type(campo).__name__} «{campo.objectName()}» "
                        f"mide {campo.width()} px y necesita {necesita}"
                    )
            cortados.extend(f"{pestaña}: {aviso}" for aviso in _amputados(pagina))

        assert estrechos == [], "campos recortados a lo ancho: " + "; ".join(estrechos)
        assert cortados == [], "contenido fuera de la pestaña: " + "; ".join(cortados)


# --------------------------------------------------------------------------- #
# 2. La cartera dentro del swap
# --------------------------------------------------------------------------- #
async def test_la_pantalla_de_swap_tiene_la_lista_de_tokens() -> None:
    """La cartera es una columna de la pestaña de swap, y el swap es la primera.

    Se afirma sobre las seis pestañas enteras, en orden, y no sólo sobre la
    primera: la composición es lo que hace que esto sea «una pantalla, tipo
    MetaMask» en vez de seis formularios, y colar una pestaña en medio es
    exactamente lo que la volvería a romper.
    """
    async with _ventana() as (_container, window):
        assert [window._tabs.tabText(i) for i in range(window._tabs.count())] == [
            "Swap",
            "Entre redes",
            "Predicción",
            "Copiloto IA",
            "Motores",
            "Alertas",
        ]
        # La cartera cuelga de la pestaña de swap: no es una pestaña.
        assert window._tabs.indexOf(window._wallet) == -1
        assert window._prices.isAncestorOf(window._wallet)
        # Y está en la columna de la **derecha**, que es el sitio que la página
        # reserva para ella. Antes iba debajo de la tarjeta de conversión; con dos
        # tarjetas apiladas, el saldo que se mira antes de escribir un importe
        # quedaba a media pantalla de distancia del importe.
        assert window._prices._side_lay.indexOf(window._wallet) >= 0
        # La columna está a la derecha de verdad, y no sólo dentro de su layout. Se
        # comparan las coordenadas **llevadas a la misma pestaña**: `x()` es relativo
        # al padre de cada widget, así que la cartera y la tarjeta tienen cada una el
        # suyo —el panel y el armazón que se desplaza— y compararlos en crudo daría
        # dos números que no significan lo mismo. `mapTo` los pone en el mismo sistema,
        # que es lo que distingue «al lado» de «debajo».
        x_cartera = window._wallet.mapTo(window._prices, window._wallet.rect().topLeft()).x()
        x_tarjeta = window._prices._card.mapTo(
            window._prices, window._prices._card.rect().topLeft()
        ).x()
        assert x_cartera > x_tarjeta


# --------------------------------------------------------------------------- #
# 3. Operar desde la lista
# --------------------------------------------------------------------------- #
async def test_cada_fila_de_tokens_tiene_su_boton_de_intercambiar() -> None:
    """Cada fila lleva su propio ⇄, y lleva **su** token dentro.

    El botón no lee la fila seleccionada a propósito: pulsar un widget dentro de
    una celda no selecciona la fila, así que leerla mandaría a la tarjeta el token
    de la última fila que se tocó —y eso se paga firmando lo que no se quería—.
    Por eso lo que se comprueba es que el token que sale es el de la fila pulsada.
    """
    pol = native_token("polygon")
    async with _ventana({"polygon": (TokenHolding(token=pol, amount=pol.amount("3")),)}) as (
        _container,
        window,
    ):
        wallet = window._wallet
        await _esperar(lambda: wallet._table.rowCount() > 0)

        boton = wallet._table.cellWidget(0, 5)
        assert isinstance(boton, QPushButton), "la última columna tiene que ser el botón ⇄"

        elegidos: list[object] = []
        wallet.swap_requested.connect(elegidos.append)
        boton.click()

        assert len(elegidos) == 1, "el botón de la fila tiene que emitir el token"
        assert elegidos[0] is pol


# --------------------------------------------------------------------------- #
# 4. Lo que se pliega, se pliega de verdad
# --------------------------------------------------------------------------- #
async def test_una_tarjeta_plegada_no_ocupa_y_se_abre() -> None:
    """Plegar esconde el contenido **y el alto que ocupaba**, y abrir lo devuelve.

    Se mide sobre la tarjeta de rutas de la página de verdad, y con el mismo
    contenido en los dos estados: comparar una tarjeta vacía plegada con una llena
    desplegada mediría el alto de las filas, no el del plegado.

    `isVisibleTo` es lo que se pregunta porque la ventana no se ha enseñado: dice
    si el widget se vería **si** se enseñara su antepasado, que es justo lo que
    distingue «lo esconde el plegado» de «no se ha pintado todavía».
    """
    async with _ventana() as (_container, window):
        page = window._prices

        # Nace plegada: sin cotización no hay nada dentro que enseñar.
        assert page._routes_card._cuerpo.isHidden() is True
        assert page._routes_empty.isVisibleTo(page._routes_card) is False

        # Cotizar la abre sola: plegada, el botón parecería no haber hecho nada.
        page._fill_table(_con_rutas(2))
        assert page._routes_card._cuerpo.isHidden() is False
        assert page._table.isVisibleTo(page._routes_card) is True
        abierta = page._routes_card.sizeHint().height()

        # Y plegarla devuelve el sitio que ocupaba, sin perder el contenido.
        page._routes_card.set_expanded(False)
        assert page._routes_card.sizeHint().height() < abierta
        page._routes_card.set_expanded(True)
        assert page._routes_card.sizeHint().height() == abierta


def test_el_plegado_no_toca_lo_que_hay_dentro(qt_app: QApplication) -> None:
    """Plegar esconde la caja que envuelve el cuerpo, no a sus hijos.

    Es la diferencia que hace que el plegado no pueda desincronizarse: el
    contenido que se esconde solo —la tabla de rutas cuando no hay rutas y el
    rótulo que la sustituye— conserva su estado mientras la tarjeta está cerrada,
    así que abrir no tiene que reconstruir ninguna foto vieja. Si el plegado
    escondiera a los hijos uno a uno, ese estado habría que apuntarlo y volver a
    ponerlo, y el día que cambiara con la tarjeta cerrada se restauraría mal.
    """
    del qt_app
    card = Card("Una tarjeta")
    dentro = QPushButton("dentro")
    card.body().addWidget(dentro)
    card.set_collapsible(expanded=False)

    assert card._cuerpo.isHidden() is True
    # El hijo no está «escondido»: está dentro de una caja que no se enseña.
    assert dentro.isHidden() is False
    assert dentro.isVisibleTo(card) is False

    card.set_expanded(True)
    assert dentro.isVisibleTo(card) is True
