"""El panel «Wallet de depósito»: ver el saldo, ver las posiciones y volver a operar.

Lo que se fija aquí es la pestaña, no el caso de uso: leer de verdad —con su
emisor y sus peticiones— tiene pruebas propias, y lo que esta tarjeta tiene que
demostrar es lo suyo:

1. **Leer enseña el saldo y las posiciones**, y un fallo de lectura se lee con su
   motivo en vez de disfrazarse de «no hay nada».
2. **Cada fila vuelve a la tarjeta de orden**: «Vender» y «Comprar más» cargan el
   mercado de la posición —buscado por su `conditionId`, no por su nombre— con el
   resultado, el lado y el tamaño puestos, y **sin firmar nada**.
3. **Una posición ya resuelta se apaga con el motivo escrito**: se cobra, no se
   opera, y el cobro por este canal todavía no existe.
4. **Añadir saldo es la retirada de siempre**, así que se apaga con los mismos
   motivos que ella: el modo, el interruptor, la red, el token y el motor.

El motor está falseado —los mercados los trae la prueba— y el caso de uso de la
lectura se falsea **en el borde del contenedor**, como `place_prediction_order`
en la prueba de la tarjeta de orden. Todo lo demás es real: el `Container`, el
registro, la guarda de modo y los widgets de Qt.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from amigocompora.app.container import Container, build_container
from amigocompora.app.usecases.read_settlement_wallet import SettlementWalletView
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.models import (
    DepthLevel,
    MarketDepth,
    MarketOutcome,
    MarketTag,
    PredictionMarket,
    PredictionPosition,
    PredictionSide,
    PredictionSort,
    Token,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import OperationMode
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    app_secret_key,
)
from amigocompora.ui.pages.prediction import PredictionPage

AHORA = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

#: La primera cuenta de Hardhat, publicada y sin fondos: da una cartera que leer.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

#: La wallet de depósito de la prueba. Es una dirección, no un contrato: la
#: tarjeta sólo la enseña y la usa como destino.
WALLET = "0x7d7eff9a4AA45A1B798C3f1EEF2957Da9146a16E"

#: El colateral del recinto: el mismo token del catálogo, para que la tarjeta lo
#: nombre como lo nombraría la aplicación.
COLATERAL = Token(
    symbol="pUSD",
    decimals=6,
    chain="polygon",
    address="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
)

MERCADO_A = "0x" + "ab" * 32

#: Identificadores de resultado: números públicos del contrato condicional.
TOKEN_SI = "71321045679252212594626385532706912750332728571942532289631379312455583992563"  # noqa: S105
TOKEN_NO = "52114319501245915516055105976744304775366685471852918604325386120244825471694"  # noqa: S105

VENUE = Venue(
    venue_id="falso", name="Falso", kind=VenueKind.PREDICTION_MARKET, chain="polygon"
)

MANIFEST = EngineManifest(
    engine_id="falso",
    name="Motor falso",
    version="1.0.0",
    kind=EngineKind.PREDICTION_MARKETS,
    summary="Un recinto de lectura, con los mercados que le da la prueba.",
    capabilities=frozenset(),
    required_config=(),
    allowed_hosts=(),
)

#: Con esto, retirar a la wallet queda permitido salvo por lo que cada prueba quite.
_TERMINOS: dict[str, object] = {
    "enabled": True,
    "allowed_chains": ["polygon"],
    "allowed_tokens": ["PUSD"],
    "allowed_engines": ["wallet"],
}


def _mercado() -> PredictionMarket:
    """El mercado de la posición abierta, con lo que la tarjeta pide al cargarla."""
    return PredictionMarket(
        market_id="con-posicion",
        venue=VENUE,
        question="¿Ocurrirá lo medido?",
        outcomes=(
            MarketOutcome(label="Sí", price=Decimal("0.60"), token_id=TOKEN_SI),
            MarketOutcome(label="No", price=Decimal("0.40"), token_id=TOKEN_NO),
        ),
        observed_at=AHORA,
        closes_at=AHORA + timedelta(days=10),
        condition_id=MERCADO_A,
        neg_risk=False,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
    )


_MERCADOS = (_mercado(),)


class MotorFalso:
    """Un recinto de lectura: los mercados de la prueba y los libros pedidos.

    Leer es lo único que hace falta aquí: la tarjeta de la wallet no firma ni
    publica, y del motor sólo toca el mercado de una posición y el libro del
    resultado —para proponer el precio al cargar, como al seleccionar una fila—.
    """

    manifest = MANIFEST

    def __init__(self) -> None:
        self.libros_pedidos: list[str] = []

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def markets(
        self,
        *,
        limit: int = 20,
        search: str | None = None,
        closing_within: timedelta | None = None,
        category: MarketTag | None = None,
        sort: PredictionSort | None = None,
    ) -> Sequence[PredictionMarket]:
        del search, closing_within, category, sort
        return tuple(_MERCADOS[:limit])

    async def market(self, market_id: str) -> PredictionMarket:
        return next(m for m in _MERCADOS if m.market_id == market_id)

    async def market_by_condition(self, condition_id: str) -> PredictionMarket:
        return next(m for m in _MERCADOS if m.condition_id == condition_id)

    async def book(self, token_id: str) -> MarketDepth:
        """Un libro de un nivel por lado, coherente: compra abajo, venta arriba.

        Hace falta aunque no sea lo que esta prueba mide: cargar la tarjeta pide
        el libro para proponer el precio, y un libro que no llegara dejaría la
        tarjeta a medias por una razón que no tiene nada que ver con la wallet.
        """
        self.libros_pedidos.append(token_id)
        return MarketDepth(
            token_id=token_id,
            bids=(DepthLevel(price=Decimal("0.58"), size=Decimal("100")),),
            asks=(DepthLevel(price=Decimal("0.62"), size=Decimal("100")),),
        )


class ProveedorFalso:
    def __init__(self) -> None:
        self.instancia = MotorFalso()

    @property
    def manifest(self) -> EngineManifest:
        return self.instancia.manifest

    def create(self, config: Mapping[str, str]) -> MotorFalso:
        return self.instancia


class LecturaFalsa:
    """El caso de uso de lectura, devolviendo lo que la prueba le dé.

    Se falsea aquí y no el motor porque lo que se mide es **la tarjeta**: qué
    enseña con una foto dada y qué escribe en la tarjeta de orden al pulsar una
    fila. La lectura de verdad —saldo por RPC, posiciones por data-api— tiene sus
    propias pruebas de unidad.
    """

    def __init__(self, vista: SettlementWalletView | None, *, falla: str | None = None) -> None:
        self._vista = vista
        self._falla = falla
        self.lecturas = 0

    def address(self) -> str:
        return WALLET

    def collateral(self) -> Token:
        return COLATERAL

    async def __call__(self) -> SettlementWalletView:
        self.lecturas += 1
        if self._falla is not None:
            raise ExecutionError(self._falla)
        assert self._vista is not None
        return self._vista


def _posicion(
    *,
    outcome_label: str = "Sí",
    token_id: str = TOKEN_SI,
    shares: str = "12.5",
    cur_price: str = "0.80",
    avg_price: str = "0.85",
    cash_pnl: str = "-0.62",
    percent_pnl: str = "-5.8",
    redeemable: bool = False,
) -> PredictionPosition:
    return PredictionPosition(
        venue=VENUE,
        condition_id=MERCADO_A,
        question="¿Ocurrirá lo medido?",
        outcome_label=outcome_label,
        token_id=token_id,
        shares=Decimal(shares),
        observed_at=AHORA,
        redeemable=redeemable,
        neg_risk=False,
        cur_price=Decimal(cur_price),
        avg_price=Decimal(avg_price),
        cash_pnl=Decimal(cash_pnl),
        percent_pnl=Decimal(percent_pnl),
    )


def _vista(
    *positions: PredictionPosition, saldo_raw: int = 950000
) -> SettlementWalletView:
    """La foto que la lectura falsa devuelve: 0.95 pUSD y las posiciones dadas."""
    return SettlementWalletView(
        address=WALLET,
        collateral=COLATERAL,
        balance=TokenAmount(raw=saldo_raw, decimals=6, symbol="pUSD"),
        positions=positions or (_posicion(),),
        observed_at=AHORA,
    )


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _store() -> InMemorySecretStore:
    store = InMemorySecretStore()
    store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    return store


@asynccontextmanager
async def _pagina(
    monkeypatch: pytest.MonkeyPatch,
    lectura: LecturaFalsa,
    *,
    modo: OperationMode = OperationMode.OBSERVATION,
    terminos: dict[str, object] | None = None,
) -> AsyncIterator[tuple[Container, PredictionPage, MotorFalso]]:
    """La pestaña montada sobre un contenedor real, con la lectura falseada."""
    proveedor = ProveedorFalso()
    config: dict[str, object] = {"mode": modo.value}
    if terminos is not None:
        config["execution"] = terminos
    container = await build_container(
        Settings.model_validate(config),
        clock=FrozenClock(AHORA),
        secret_store=_store(),
        discover=False,
        configure_logs=False,
    )
    try:
        container.registry.register(proveedor, source="prueba")
        await container.registry.activate("falso")
        # En el borde del contenedor, como `place_prediction_order` en la prueba
        # de la tarjeta de orden: la página no distingue este doble del real.
        monkeypatch.setattr(container, "read_settlement_wallet", lectura, raising=True)
        yield container, PredictionPage(container), proveedor.instancia
    finally:
        await container.aclose()


async def _ceder() -> None:
    import asyncio

    await asyncio.sleep(0)


async def _abrir(pagina: PredictionPage, fila: int = 0) -> None:
    """Busca, elige una fila y deja la tarjeta del mercado cargada.

    Se pulsan los botones de verdad —Buscar y la fila— porque lo que se prueba
    es la cadena entera: la tarjeta se abre con el mercado y lanza sola la
    lectura del saldo.
    """
    pagina._on_search()
    while not pagina._btn.isEnabled():
        await _ceder()
    pagina._table.selectRow(fila)
    for _ in range(20):
        await _ceder()


async def _leer(pagina: PredictionPage, lectura: LecturaFalsa) -> None:
    """Pulsa «Leer la wallet» y espera a que vuelva, como haría `spawn` en Qt.

    Se espera por la **lectura contada** y no sólo por el botón: el clic programa
    la tarea, y el botón no se apaga hasta que la tarea arranca. Mirar sólo el
    botón volvería antes de que la lectura hubiera corrido.
    """
    pagina._wallet_read_btn.click()
    for _ in range(50):
        await _ceder()
        if lectura.lecturas >= 1 and pagina._wallet_read_btn.isEnabled():
            return
    raise AssertionError("la lectura no volvió")


def _accion(pagina: PredictionPage, fila: int, etiqueta: str) -> QPushButton:
    """El botón de una fila de la tabla de posiciones, exigiendo que exista."""
    contenedor = pagina._wallet_table.cellWidget(fila, 6)
    assert contenedor is not None, f"la fila {fila} no tiene acciones"
    for boton in contenedor.findChildren(QPushButton):
        if boton.text() == etiqueta:
            return boton
    raise AssertionError(f"la fila {fila} no tiene un botón «{etiqueta}»")


def _celda(tabla: QTableWidget, fila: int, columna: int) -> QTableWidgetItem:
    """La celda, exigiendo que exista: una celda sin pintar es lo que se busca."""
    item = tabla.item(fila, columna)
    assert item is not None, f"celda vacía en fila {fila}, columna {columna}"
    return item


# --------------------------------------------------------------------------- #
# Leer: el saldo, las posiciones y el motivo cuando falla
# --------------------------------------------------------------------------- #
async def test_leer_la_wallet_ensena_su_saldo_y_sus_posiciones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La tarjeta que faltaba: cuánto hay dentro y en qué está puesto."""
    lectura = LecturaFalsa(_vista(_posicion()))
    async with _pagina(monkeypatch, lectura) as (_, pagina, _):
        # La dirección se enseña **antes** de leer: es una derivación, no una
        # lectura, y es lo que permite recibir un depósito en una instalación
        # recién configurada.
        assert pagina._wallet_address.text() == WALLET

        await _leer(pagina, lectura)

        assert lectura.lecturas == 1
        assert "0.95 pUSD" in pagina._wallet_balance.text()
        tabla = pagina._wallet_table
        assert tabla.rowCount() == 1
        assert _celda(tabla, 0, 0).text() == "¿Ocurrirá lo medido?"
        assert _celda(tabla, 0, 1).text() == "Sí"
        assert _celda(tabla, 0, 2).text() == "12.5"
        assert _celda(tabla, 0, 3).text() == "0.85"
        # El valor se calcula —participaciones por precio actual—; la ganancia
        # se copia de la fuente.
        assert Decimal(_celda(tabla, 0, 4).text()) == Decimal("10.0")
        assert "-0.62" in _celda(tabla, 0, 5).text()
        assert "-5.8" in _celda(tabla, 0, 5).text()
        assert "En conjunto" in pagina._wallet_total.text()


async def test_un_fallo_de_lectura_se_lee_con_su_motivo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«No se pudo leer» con el motivo entero, y no un «no hay nada» que miente."""
    lectura = LecturaFalsa(
        None, falla="no hay ningún nodo configurado para «polygon»"
    )
    async with _pagina(monkeypatch, lectura) as (_, pagina, _):
        await _leer(pagina, lectura)

        assert "No se pudo leer la wallet" in pagina._wallet_empty.text()
        assert "no hay ningún nodo configurado" in pagina._wallet_empty.text()
        assert pagina._wallet_table.rowCount() == 0


# --------------------------------------------------------------------------- #
# Cada fila vuelve a la tarjeta de orden
# --------------------------------------------------------------------------- #
async def test_vender_desde_una_posicion_carga_la_tarjeta_de_orden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vender una posición empieza aquí: el mercado de la posición, en la tarjeta.

    El mercado se busca por su `conditionId` y el resultado por su `token_id`,
    que es lo que la posición publica. Cargar **no firma**: el precio y el
    «Publicar» siguen siendo los de la tarjeta de arriba, con sus comprobaciones.
    """
    lectura = LecturaFalsa(_vista(_posicion()))
    async with _pagina(monkeypatch, lectura) as (_, pagina, motor):
        await _leer(pagina, lectura)
        # Al cerrarse el panel —si estuviera abierto— y saltar a la tarjeta se
        # avisa con la señal de Qt, que es la verdad de «se cerró».
        cerrados: list[int] = []
        pagina._wallet_panel.finished.connect(cerrados.append)
        _accion(pagina, 0, "Vender").click()
        for _ in range(50):
            # Se espera también al libro: la carga lo pide en segundo plano, y
            # afirmar sobre él antes de que vuelva sería afirmar sobre la carrera.
            if pagina._chosen_market is not None and motor.libros_pedidos:
                break
            await _ceder()

        assert pagina._chosen_market is not None
        assert pagina._chosen_market.market_id == "con-posicion"
        # La tarjeta sustituye a la lista: revisar y publicar pasa allí, y el
        # panel de la wallet se cierra para no taparlo.
        assert pagina._views.currentIndex() == 1
        assert cerrados == [int(QDialog.DialogCode.Accepted)]
        assert pagina._side_value() is PredictionSide.SELL
        # La posición entera, sin redondear hacia arriba: se vende lo que hay.
        assert Decimal(str(pagina._shares.value())) == Decimal("12.5")
        assert pagina._outcome.currentText().startswith("Sí")
        assert "Cargada desde tu posición" in pagina._order_status.text()
        assert "no firma" in pagina._order_status.text()
        # El libro se pidió para proponer el precio, como al seleccionar una fila.
        assert TOKEN_SI in motor.libros_pedidos


async def test_comprar_mas_propone_el_minimo_del_mercado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Comprar más» no propone vender nada: propone el menor compromiso posible.

    El mínimo del mercado se pone **en dinero** —el lado en el que se escribe la
    compra— al precio propuesto, y las participaciones salen de ahí. Con el
    libro a la vista ese precio es el mejor ask; si es más alto que el
    publicado, el importe mínimo ya no llega al mínimo de participaciones y la
    tarjeta lo dice con su motivo en vez de subir el dinero sola —subirlo sería
    comprar más de lo que se escribió—.
    """
    lectura = LecturaFalsa(_vista(_posicion()))
    async with _pagina(monkeypatch, lectura) as (_, pagina, motor):
        await _leer(pagina, lectura)
        _accion(pagina, 0, "Comprar más").click()
        for _ in range(50):
            if pagina._chosen_market is not None and motor.libros_pedidos:
                break
            await _ceder()

        assert pagina._chosen_market is not None
        assert pagina._side_value() is PredictionSide.BUY
        # El mínimo en dinero al precio publicado (0,60): 5 x 0,60 = 3,00 $.
        assert Decimal(str(pagina._amount.value())) == Decimal("3.00")
        # Con el ask del libro (0,62) son 4,83 participaciones: por debajo del
        # mínimo, y eso se dice —en la línea de reglas del mercado, que es
        # donde el mínimo vive ahora y se lee en las dos caras—.
        assert Decimal(str(pagina._shares.value())) == Decimal("4.83")
        assert "mínimo del mercado: 5" in pagina._market_limits.text()


async def test_una_posicion_resuelta_se_apaga_con_el_motivo_escrito(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cobrar y operar no son lo mismo, y una fila resuelta lo dice.

    El cobro desde la wallet de depósito todavía no está en la aplicación —pide
    un lote del relayer que aún no se construye—, así que la fila no ofrece un
    botón que no haría nada: apaga los dos y escribe por qué.
    """
    lectura = LecturaFalsa(
        _vista(
            _posicion(),
            _posicion(
                outcome_label="No",
                token_id=TOKEN_NO,
                shares="5",
                cur_price="0.99",
                avg_price="0.20",
                cash_pnl="0.30",
                percent_pnl="79.0",
                redeemable=True,
            ),
        )
    )
    async with _pagina(monkeypatch, lectura) as (_, pagina, _):
        await _leer(pagina, lectura)

        vender = _accion(pagina, 1, "Vender")
        comprar = _accion(pagina, 1, "Comprar más")
        assert not vender.isEnabled()
        assert not comprar.isEnabled()
        assert "ya resolvió" in vender.toolTip()
        # La fila abierta sigue operativa: apagar es de la resuelta, no de la tabla.
        assert _accion(pagina, 0, "Vender").isEnabled()


def _exec_de_mentira(abiertos: list[str]) -> Callable[[], int]:
    """Un `exec` que no bloquea: anota la apertura y vuelve como «aceptado».

    Se sustituye en la instancia —Shiboken no deja tocar la clase— para que el
    modal no se quede esperando en una prueba sin pantalla.
    """

    def _exec() -> int:
        abiertos.append("panel")
        return int(QDialog.DialogCode.Accepted)

    return _exec


# --------------------------------------------------------------------------- #
# El saldo en la tarjeta del mercado, y el panel que abre
# --------------------------------------------------------------------------- #
async def test_abrir_una_tarjeta_lee_el_saldo_y_lo_ensena_en_las_dos_caras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El saldo va junto al formulario de las dos caras, y se lee una vez.

    La tarjeta lo lee sola al abrirse —es la cifra que decide si una compra
    cabe—, y una sola vez por sesión: pasear por mercados no puede golpear la
    fuente en cada fila.
    """
    lectura = LecturaFalsa(_vista(_posicion()))
    async with _pagina(monkeypatch, lectura) as (_, pagina, _):
        assert pagina._wallet_balance_buy.text() == "—"

        await _abrir(pagina)
        for _ in range(50):
            if "0.95" in pagina._wallet_balance_buy.text():
                break
            await _ceder()

        assert "0.95 pUSD" in pagina._wallet_balance_buy.text()
        assert "0.95 pUSD" in pagina._wallet_balance_sell.text()
        assert lectura.lecturas == 1

        # Abrir otra vez —aunque sea el mismo mercado— no vuelve a leer.
        await _abrir(pagina)
        for _ in range(10):
            await _ceder()
        assert lectura.lecturas == 1


async def test_el_saldo_de_las_dos_caras_abre_el_panel_de_la_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El botón de al lado abre el panel: operar la wallet sin salir de la tarjeta.

    `exec` se sustituye en la instancia —Shiboken no deja tocar la clase— para
    que el modal no bloquee; lo que se comprueba es que las dos caras tienen su
    botón y que las dos abren el mismo panel, el que enseña el saldo, las
    posiciones, «Añadir saldo» y el QR.
    """
    lectura = LecturaFalsa(_vista(_posicion()))
    async with _pagina(monkeypatch, lectura) as (_, pagina, _):
        abiertos: list[str] = []
        pagina._wallet_panel.exec = _exec_de_mentira(abiertos)  # type: ignore[method-assign]

        pagina._wallet_open_btn_buy.click()
        pagina._side.setCurrentIndex(pagina._side.findData(PredictionSide.SELL))
        pagina._wallet_open_btn_sell.click()
        for _ in range(50):
            if lectura.lecturas >= 1:
                break
            await _ceder()

        assert abiertos == ["panel", "panel"]
        # El panel abierto sin lectura previa la lanza: se viene a mirarla.
        assert lectura.lecturas >= 1


# --------------------------------------------------------------------------- #
# Añadir saldo: la retirada de siempre, con sus mismos motivos
# --------------------------------------------------------------------------- #
async def test_anadir_saldo_se_apaga_con_los_mismos_motivos_que_la_retirada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin ejecución no se firma una retirada, y la tarjeta no promete firmarla.

    Los motivos son los de `WithdrawFunds` —el modo, el interruptor, la red, el
    token y el motor—, y aquí se ven en el modo de observación, que es donde la
    pestaña tiene que seguir siendo legible sin poder operar.
    """
    lectura = LecturaFalsa(_vista())
    async with _pagina(monkeypatch, lectura) as (_, pagina, _):
        assert not pagina._wallet_add_btn.isEnabled()
        assert pagina._wallet_note.text().startswith("No se puede añadir saldo:")
        assert "EJECUCIÓN" in pagina._wallet_note.text()
        assert "config.toml" in pagina._wallet_note.text()


async def test_con_todo_puesto_anadir_saldo_se_enciende_sin_nota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Los términos del recinto y la cartera puestos: el botón se enciende y calla."""
    lectura = LecturaFalsa(_vista())
    async with _pagina(
        monkeypatch,
        lectura,
        modo=OperationMode.EXECUTION,
        terminos=_TERMINOS,
    ) as (_, pagina, _):
        assert pagina._wallet_add_btn.isEnabled(), pagina._wallet_note.text()
        assert pagina._wallet_note.text() == ""
