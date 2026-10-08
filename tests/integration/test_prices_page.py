"""La pestaña de cotizaciones: qué dice cuando no se puede ejecutar, y por qué.

Esta prueba construye la página **de verdad** —con un `Container` de verdad y
widgets de Qt de verdad— en vez de doblar la interfaz. El motivo es el fallo que
la originó: la lista de motivos enumeraba el modo, el interruptor y la cartera,
pero no que hubiera un motor capaz de construir el payload. Con `geckoterminal`
activo —que cotiza pero no construye— el botón «Ejecutar» quedaba **encendido** y
fallaba al pulsarlo, que es exactamente el botón que el repositorio dice que no
puede existir: uno que se pulsa y falla enseña a desconfiar de los botones, y
éste es el que firma.

No se habría visto doblando la interfaz, porque el dato que faltaba no venía de
la vista: venía del registro de motores. Por eso la prueba monta el contenedor
completo y lee lo que la página pone en pantalla.

Qt necesita un `QApplication` vivo, y en una máquina sin pantalla —CI, un
servidor— eso se resuelve con la plataforma `offscreen`, que se fija aquí antes
de que se importe nada de Qt.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableWidgetItem

from amigocompora.app.container import Container, build_container
from amigocompora.app.execution_policy import LedgerEntry
from amigocompora.domain.models import (
    BroadcastStatus,
    PriceComparison,
    Quote,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import OperationMode
from amigocompora.domain.money import BasisPoints
from amigocompora.engines.catalog import quote_token, wrapped_native
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    app_secret_key,
    secret_key,
)
from amigocompora.ui.pages.prices import PricesPage

#: Clave de desarrollo **publicada** (la primera de Hardhat). No es un secreto:
#: que sea conocida es justo lo que la hace útil aquí, porque se puede afirmar
#: sobre la dirección derivada sin que la calcule el código que se está probando.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

#: Con esto la ejecución queda lista salvo por lo que cada prueba quite.
_TERMINOS: dict[str, object] = {
    "enabled": True,
    "allowed_chains": ["base"],
    "allowed_tokens": ["WETH", "USDC"],
    "allowed_engines": ["uniswap"],
}


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _pair() -> TradingPair:
    base = wrapped_native("base")
    quote = quote_token("base")
    assert base is not None
    assert quote is not None
    return TradingPair(base=base, quote=quote)


def _comparison(pair: TradingPair) -> PriceComparison:
    """Una comparación de una sola fila, para poder seleccionarla en la tabla."""
    return PriceComparison(
        pair=pair,
        amount_in=pair.base.amount("1"),
        quotes=(
            Quote(
                venue=Venue(
                    venue_id="fake@base", name="fake", kind=VenueKind.DEX, chain="base"
                ),
                engine_id="uniswap",
                pair=pair,
                amount_in=pair.base.amount("1"),
                amount_out=pair.quote.amount("2000"),
                fee_bps=BasisPoints(5),
                price_impact_bps=BasisPoints(1),
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                # Con nota, como las de verdad: es la columna que dice de dónde
                # sale cada cifra, y sin ella la prueba de que se puede leer
                # entera estaría afirmando sobre una cadena vacía.
                source_note="Leído del pool falso contra el estado de la cadena.",
            ),
        ),
    )


def _comparacion_de(filas: int) -> PriceComparison:
    """Una comparación con tantas rutas como se pidan, para medir la tabla.

    Cada ruta es un venue distinto: dos filas idénticas no dirían nada sobre el
    alto, y la tabla mide lo que mide su contenido.
    """
    pair = _pair()
    venues = (
        Venue(venue_id="fake@base", name="fake", kind=VenueKind.DEX, chain="base"),
        Venue(venue_id="otro@base", name="otro", kind=VenueKind.DEX, chain="base"),
    )
    return PriceComparison(
        pair=pair,
        amount_in=pair.base.amount("1"),
        quotes=tuple(
            Quote(
                venue=venues[i % len(venues)],
                engine_id=f"motor_{i}",
                pair=pair,
                amount_in=pair.base.amount("1"),
                amount_out=pair.quote.amount("2000"),
                fee_bps=BasisPoints(5),
                price_impact_bps=BasisPoints(1),
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                source_note=f"Ruta {i}: leída del pool falso contra la cadena.",
            )
            for i in range(filas)
        ),
    )


def _store(*, con_cartera: bool) -> InMemorySecretStore:
    store = InMemorySecretStore()
    if con_cartera:
        store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    # El motor necesita su clave para activarse, y activarse es lo que lo
    # convierte en planificador. La clave es falsa y no se usa: no hay red aquí.
    store.set(secret_key("uniswap", "api_key"), "clave-de-prueba")
    return store


@asynccontextmanager
async def _pagina(
    *,
    modo: OperationMode,
    con_cartera: bool,
    con_planificador: bool,
    permitidos: list[str] | None = None,
    topes: dict[str, str] | None = None,
) -> AsyncIterator[tuple[Container, PricesPage]]:
    terminos = dict(_TERMINOS)
    if permitidos is not None:
        terminos["allowed_tokens"] = permitidos
    if topes is not None:
        terminos.update(topes)
    config: dict[str, object] = {"mode": modo.value, "execution": terminos}
    if con_planificador:
        config["active_engines"] = {"dex_quotes": "uniswap"}
    container = await build_container(
        Settings.model_validate(config),
        secret_store=_store(con_cartera=con_cartera),
        configure_logs=False,
    )
    try:
        yield container, PricesPage(container)
    finally:
        await container.aclose()


async def test_the_default_configuration_says_everything_that_is_missing() -> None:
    """Sin nada configurado, los cuatro motivos y ninguno de más.

    Se afirma sobre los cuatro y no sobre «hay alguno»: el valor de esta lista
    está en ser completa. Enseñar el primero y callar los demás hace que arreglar
    una cosa destape la siguiente, que es lo que hace pensar que la aplicación
    está rota en vez de a medio configurar.
    """
    from amigocompora.infra.secrets import InMemorySecretStore as _Store

    container = await build_container(
        Settings(), secret_store=_Store(), configure_logs=False
    )
    try:
        page = PricesPage(container)
        motivos = page._execution_blockers()
        assert len(motivos) == 4
        assert any("OBSERVACIÓN" in m for m in motivos)
        assert any("enabled = true" in m for m in motivos)
        assert any("construir el swap" in m for m in motivos)
        assert any("cartera" in m for m in motivos)
    finally:
        await container.aclose()


@pytest.mark.parametrize(
    ("modo", "con_cartera", "con_planificador", "esperado"),
    [
        # Todo puesto: no falta nada, y el botón debe poder pulsarse.
        (OperationMode.EXECUTION, True, True, 0),
        # Sólo falta el modo.
        (OperationMode.ASSISTED, True, True, 1),
        # Sólo falta la cartera.
        (OperationMode.EXECUTION, False, True, 1),
        # Sólo falta el motor que construya: el caso que se midió.
        (OperationMode.EXECUTION, True, False, 1),
    ],
)
async def test_each_missing_piece_is_named_on_its_own(
    modo: OperationMode,
    con_cartera: bool,
    con_planificador: bool,
    esperado: int,
) -> None:
    """Se quita una pieza cada vez, para que se vea cuál produce qué motivo."""
    async with _pagina(
        modo=modo, con_cartera=con_cartera, con_planificador=con_planificador
    ) as (_, page):
        assert len(page._execution_blockers(_pair())) == esperado


async def test_the_execute_button_turns_on_only_when_nothing_is_missing() -> None:
    """El botón que firma, encendido **sólo** cuando de verdad se puede firmar.

    Se selecciona una fila de verdad en la tabla en vez de llamar al método a
    mano: lo que se está comprobando es la cadena «seleccionar → repintar →
    habilitar», y llamar al método se saltaría justo la parte que puede fallar.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (_, page):
        pair = _pair()
        page._comparison = _comparison(pair)
        page._fill_table(page._comparison)
        page._table.selectRow(0)

        assert page._selected_quote() is not None
        assert page._exec_btn.isEnabled() is True
        assert page._exec_note.text() == ""


async def test_the_button_is_off_and_the_screen_says_why() -> None:
    """Apagado y con el motivo escrito, que es lo que evita el atajo.

    Un botón apagado sin explicación no se lee como «falta algo»: se lee como
    «esto no funciona», y la reacción natural es buscar la forma de saltárselo.
    Por eso el motivo está en la pantalla y no sólo en el tooltip.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=True, con_planificador=True
    ) as (_, page):
        pair = _pair()
        page._comparison = _comparison(pair)
        page._fill_table(page._comparison)
        page._table.selectRow(0)

        assert page._exec_btn.isEnabled() is False
        nota = page._exec_note.text()
        assert nota.startswith("No se puede ejecutar:")
        assert "OBSERVACIÓN" in nota
        # Y el botón de preparar sigue disponible: el camino que no firma no
        # depende de nada de esto.
        assert page._swap_btn.isEnabled() is True


# --------------------------------------------------------------------------- #
# Los topes de importe, que antes no miraba la pestaña
# --------------------------------------------------------------------------- #
#: La comparación de estas pruebas da 2 000 USDC por 1 WETH. Es un número
#: redondo y grande a propósito: deja el tope por operación y el diario en
#: cualquier cifra intermedia sin tocar la cotización.
_SALIDA_DE_LA_COMPARACION = "2000"


async def test_el_tope_por_operacion_apaga_el_boton_antes_de_pulsarlo() -> None:
    """El botón que firma no puede prometer una operación que la política rechaza.

    Se midió con la configuración real: el campo de cantidad trae 1,0, la compra
    de 1 WETH vale unos 2 605 USDC, y el tope por operación era 1. El botón se
    encendía igual —esta lista miraba el modo, el interruptor, la lista blanca, el
    planificador y la cartera, pero no el importe— y el rechazo aparecía sólo al
    pulsarlo. No se firmaba nada, porque `ExecuteSwap` comprueba los topes antes
    del diálogo, pero el botón estaba mintiendo, y es justo el botón que no puede.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION,
        con_cartera=True,
        con_planificador=True,
        topes={"max_quote_per_trade": "1"},
    ) as (_, page):
        pair = _pair()
        page._comparison = _comparison(pair)
        page._fill_table(page._comparison)
        page._table.selectRow(0)

        assert page._exec_btn.isEnabled() is False
        nota = page._exec_note.text()
        assert "importe por operación" in nota
        assert _SALIDA_DE_LA_COMPARACION in nota
        # Y el camino que no firma sigue abierto: preparar el payload no gasta.
        assert page._swap_btn.isEnabled() is True


async def test_el_tope_diario_cuenta_lo_que_ya_se_gasto() -> None:
    """El tope de 24 h no es lo de esta sesión: sale del registro de ejecuciones.

    Es la mitad que un «suma lo que lleves en memoria» no cubriría, y por eso el
    asiento se escribe en el registro del contenedor y no se pasa por parámetro:
    lo que se comprueba es que la pestaña **lee el registro**, que es lo único que
    hace que reiniciar la aplicación no borre el tope.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION,
        con_cartera=True,
        con_planificador=True,
        topes={"max_quote_per_trade": "2000", "max_quote_per_day": "2500"},
    ) as (container, page):
        container.policy.ledger.append(
            LedgerEntry(
                occurred_at=datetime.now(UTC),
                chain="base",
                pair="WETH/USDC",
                engine_id="uniswap",
                notional="1000",
                notional_symbol="USDC",
                tx_hash="0x" + "ab" * 32,
                status=BroadcastStatus.SUCCESS.value,
                recipient="0x" + "cd" * 20,
                description="asiento de prueba",
            )
        )

        pair = _pair()
        page._comparison = _comparison(pair)
        page._fill_table(page._comparison)
        page._table.selectRow(0)

        assert page._exec_btn.isEnabled() is False
        nota = page._exec_note.text()
        assert "24 h" in nota
        # Los tres números, para que la cuenta se pueda rehacer leyendo: lo que ya
        # había, lo que sumaría y contra qué máximo.
        assert "1000" in nota
        assert _SALIDA_DE_LA_COMPARACION in nota
        assert "2500" in nota


async def test_sin_topes_el_boton_no_se_apaga_por_este_motivo() -> None:
    """Sin topes declarados no hay nada que decir, y el botón sigue encendido.

    Es la guarda contra el arreglo de más: la comprobación nueva se añade a una
    lista que, cuando está vacía, es lo único que enciende el botón. Una
    comprobación que se equivocara hacia «bloqueado» apagaría el botón en toda
    instalación sin topes, que es la instalación por omisión.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (container, page):
        assert container.policy.limits.max_quote_per_trade is None
        assert container.policy.limits.max_quote_per_day is None

        pair = _pair()
        page._comparison = _comparison(pair)
        page._fill_table(page._comparison)
        page._table.selectRow(0)

        assert page._exec_btn.isEnabled() is True
        assert page._exec_note.text() == ""


# --------------------------------------------------------------------------- #
# El modo: decirlo, y dejar cambiarlo a quien puede cambiarlo
# --------------------------------------------------------------------------- #
async def test_el_modo_que_bloquea_se_ofrece_con_su_atajo() -> None:
    """Cuando lo único que falta es el modo, al lado del aviso va el cambio.

    El mensaje decía «cambia a EJECUCIÓN» y dejaba al usuario buscando dónde.
    El atajo no cambia el modo por su cuenta: lo cambia **cuando alguien lo
    pulsa**, que es la única forma que `ModeGuard.set_mode` admite.

    Se comprueba sobre `isHidden()` y no sobre `isVisible()` a propósito: en una
    ventana que nunca se ha mostrado —que es como corre esto— todos los widgets
    son «invisibles», así que `isVisible()` no distinguiría el botón que se
    ocultó del que simplemente no se ha pintado todavía.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=True, con_planificador=True
    ) as (container, page):
        pair = _pair()
        page._comparison = _comparison(pair)
        page._fill_table(page._comparison)
        page._table.selectRow(0)

        assert page._upgrade_btn.isHidden() is False
        assert "EJECUCIÓN" in page._upgrade_btn.text()

        page._on_upgrade_mode()

        assert container.guard.mode is OperationMode.EXECUTION
        assert page._exec_btn.isEnabled() is True
        assert page._exec_note.text() == ""
        # Y el atajo desaparece en cuanto deja de hacer falta.
        assert page._upgrade_btn.isHidden() is True


async def test_el_atajo_no_aparece_si_el_modo_no_es_lo_que_falta() -> None:
    """Ofrecer «cambia de modo» cuando el modo ya está bien sería un botón inútil.

    Es peor que no tenerlo: promete desbloquear algo y al pulsarlo no cambia
    nada, que es exactamente lo que enseña a desconfiar de los botones.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=False, con_planificador=True
    ) as (_, page):
        pair = _pair()
        page._comparison = _comparison(pair)
        page._fill_table(page._comparison)
        page._table.selectRow(0)

        # Falta la cartera, no el modo.
        assert "cartera" in page._exec_note.text()
        assert page._upgrade_btn.isHidden() is True


# --------------------------------------------------------------------------- #
# La lista blanca, dicha antes de cotizar
# --------------------------------------------------------------------------- #
async def test_un_token_fuera_de_la_lista_blanca_se_marca_en_el_desplegable() -> None:
    """El desplegable sabe qué se puede ejecutar, y lo enseña desde el principio.

    Descubrirlo al final —cotizar, elegir ruta, llegar al diálogo y que entonces
    digan que ese token no está permitido— es descubrirlo tarde. Se marca el
    texto en gris y se explica en el tooltip **sin tocar la etiqueta**: la
    etiqueta identifica el token y hay código que la busca por texto.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION,
        con_cartera=True,
        con_planificador=True,
        permitidos=["WETH"],
    ) as (_, page):
        page._chain.setCurrentIndex(page._chain.findData("base"))

        fuera = page._base.findText("USDC")
        assert fuera >= 0
        assert page._base.itemData(fuera, Qt.ItemDataRole.ForegroundRole) is not None
        assert "allowed_tokens" in page._base.itemData(fuera, Qt.ItemDataRole.ToolTipRole)

        dentro = page._base.findText("WETH")
        assert dentro >= 0
        assert page._base.itemData(dentro, Qt.ItemDataRole.ForegroundRole) is None


async def test_the_amount_label_follows_what_you_give() -> None:
    """La cantidad se escribe en la unidad de lo que **entregas**, no en la otra.

    Es la corrección que hace posible comprar: antes el par se armaba siempre
    como `token/stable`, o sea vender, y la etiqueta no decía en qué unidad
    estaba el número. Un importe ambiguo, en una pantalla que firma, es un
    importe equivocado.

    Y el botón de invertir tiene que cambiar **la unidad**, no sólo la etiqueta:
    si diera la vuelta al par sin mover el importe, el mismo número pasaría a
    significar diez mil veces otra cosa.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=False, con_planificador=False
    ) as (_, page):
        page._chain.setCurrentIndex(page._chain.findData("base"))
        page._base.setCurrentIndex(page._base.findText("USDC"))
        page._contra.setCurrentIndex(page._contra.findText("WETH"))
        assert page._unit.text() == "USDC"
        assert page._pair.text() == "USDC → WETH"

        page._on_invert()

        assert page._unit.text() == "WETH"
        assert page._pair.text() == "WETH → USDC"


async def test_the_wallet_is_offered_first_as_the_destination() -> None:
    """La cartera propia es la primera de la lista, y sin repetirse.

    Se ofrece la dirección derivada y no la clave, que es lo único que la
    interfaz necesita saber de la cartera. Ir primera no es un detalle: teclear
    una dirección a mano es la forma más común de perder fondos.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (container, page):
        propia = container.keys.address()
        assert propia == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

        candidatas = page._recipient_candidates("base")
        assert candidatas[0] == propia

        # Y si además está declarada en `watch_addresses`, aparece una sola vez.
        assert candidatas.count(propia) == 1


# --------------------------------------------------------------------------- #
# Cualquier token, no sólo los del catálogo
# --------------------------------------------------------------------------- #
#: El token del ejemplo medido en Base. Sólo tiene mercado contra WETH, así que
#: con la pata contraria fija en la stablecoin no se podía ni cotizar.
BANKR = Token(
    symbol="bankr",
    decimals=18,
    chain="base",
    address="0x26f79444595cF1C6753bAEd09DbC0C6F73144443",
)


class _LookupFalso:
    """Devuelve un token sin salir a la red.

    Se falsea el resolutor y no el nodo: lo que se está probando aquí es que la
    página **usa** el token que le dan —lo mete en las dos listas, lo deja
    elegido—, y el camino que va de la dirección al token ya tiene sus propias
    pruebas contra un nodo falso en `test_token_lookup.py`.
    """

    def __init__(self, token: Token) -> None:
        self.token = token
        self.preguntas: list[tuple[str, str]] = []

    async def by_address(self, chain_key: str, address: str) -> Token:
        self.preguntas.append((chain_key, address))
        return self.token


async def test_un_token_por_direccion_entra_en_las_dos_listas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Es lo que hace que la lista deje de ser el límite de lo que se puede operar.

    Se afirma sobre las **dos** listas porque el token puede caer en cualquiera de
    las dos patas según la dirección: si sólo entrara en una, comprarlo sería
    posible y venderlo no.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=False, con_planificador=False
    ) as (container, page):
        page._chain.setCurrentIndex(page._chain.findData("base"))
        lookup = _LookupFalso(BANKR)
        monkeypatch.setattr(container, "token_lookup", lookup, raising=True)

        assert BANKR.address is not None
        await page._do_lookup_token("base", BANKR.address)

        assert lookup.preguntas == [("base", BANKR.address)]
        assert page._base.findText("bankr") >= 0
        assert page._contra.findText("bankr") >= 0
        # Y queda elegido: añadirlo para tener que buscarlo otra vez sería hacer
        # el trabajo dos veces.
        assert page._base.currentData().address == BANKR.address
        assert "18 decimales" in page._status.text()


async def test_anadir_un_token_no_deshace_la_pata_contraria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repintar las listas no es motivo para deshacer una elección recién hecha.

    El caso que lo haría mal es el natural: añadir un token reconstruye las dos
    listas, y reconstruirlas devolviendo la pata contraria a su valor por omisión
    obligaría a volver a elegirla cada vez —un desplegable que se mueve solo
    cuando el usuario no se lo pide—.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=False, con_planificador=False
    ) as (container, page):
        page._chain.setCurrentIndex(page._chain.findData("base"))
        monkeypatch.setattr(container, "token_lookup", _LookupFalso(BANKR), raising=True)
        page._contra.setCurrentIndex(page._contra.findText("WETH"))

        assert BANKR.address is not None
        await page._do_lookup_token("base", BANKR.address)

        assert page._contra.currentData().symbol == "WETH"


async def test_la_pata_contraria_no_esta_clavada_en_la_stablecoin() -> None:
    """La segunda pata se elige, y con eso se arma un par que no toca el USDC.

    Es el caso que bloqueaba la compra de `bankr`: su única piscina es contra
    WETH, así que un par armado siempre contra la stablecoin no tenía por dónde
    cotizarse.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=False, con_planificador=False
    ) as (_, page):
        page._chain.setCurrentIndex(page._chain.findData("base"))
        page._base.setCurrentIndex(page._base.findText("cbBTC"))
        page._contra.setCurrentIndex(page._contra.findText("WETH"))

        entrega, recibe = page._legs()
        assert entrega is not None
        assert recibe is not None
        assert (entrega.symbol, recibe.symbol) == ("cbBTC", "WETH")
        assert page._pair.text() == "cbBTC → WETH"
        assert page._unit.text() == "cbBTC"


async def test_el_mismo_token_en_las_dos_patas_se_dice_y_no_se_cotiza() -> None:
    """Dos desplegables libres dejan elegir un par imposible, y se explica.

    Bloquear combinaciones entre los dos desplegables los vuelve esquivos —se
    mueven solos y no se entiende por qué—, así que se permite elegirlo y se
    contesta que eso no es una operación. Sin esta comprobación el par llegaría
    al motor, que no encontraría pool y diría «sin liquidez», que es una
    explicación falsa: no falta liquidez, falta que sean dos tokens distintos.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=False, con_planificador=False
    ) as (_, page):
        mismo = page._base.findText("WETH")
        page._base.setCurrentIndex(mismo)
        page._contra.setCurrentIndex(mismo)

        await page._do_quote()

        assert "dos tokens distintos" in page._status.text()
        assert page._table.rowCount() == 0


async def test_un_token_fuera_de_la_lista_blanca_se_nombra() -> None:
    """La lista blanca deja de ser un detalle de configuración que se supone bien.

    Con el catálogo, los símbolos eran un conjunto cerrado y pequeño; con un token
    añadido por dirección, el símbolo es lo que diga un contrato cualquiera. El
    botón que firma tiene que decir cuál falta, y no apagarse sin motivo, que es
    lo que empuja a buscar la forma de saltárselo.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (_, page):
        base = wrapped_native("base")
        assert base is not None

        motivos = page._execution_blockers(TradingPair(base=base, quote=BANKR))
        assert any("bankr" in motivo and "allowed_tokens" in motivo for motivo in motivos)

        # Y con los dos en la lista no queda ningún motivo por este concepto.
        permitido = page._execution_blockers(
            TradingPair(base=base, quote=quote_token("base") or base)
        )
        assert permitido == ()


# --------------------------------------------------------------------------- #
# El alto de las tablas y lo que se dice cuando no hay nada
# --------------------------------------------------------------------------- #
async def test_la_tabla_de_rutas_no_reserva_alto_de_mas() -> None:
    """Una tabla con dos filas no puede medir como una de veinte.

    `QTableWidget` publica un alto natural de 192 px que no tiene nada que ver
    con lo que lleva dentro. Sin corregirlo, la tarjeta de rutas reservaba ese
    alto entero para dos filas y dejaba un rectángulo gris debajo de los
    botones, que se lee como un fallo de pintado y no como «aquí cabe más».
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=True, con_planificador=True
    ) as (_, page):
        # Sin cotizar, el rótulo ocupa el sitio y la tabla no mide nada.
        assert page._table.maximumHeight() == 0

        page._comparison = _comparacion_de(1)
        page._fill_table(page._comparison)
        una = page._table.maximumHeight()
        assert 0 < una < 192, "el alto tiene que salir de las filas, no de Qt"

        # Y crece con las filas, porque la tabla de verdad las tiene.
        page._comparison = _comparacion_de(2)
        page._fill_table(page._comparison)
        dos = page._table.maximumHeight()
        assert dos > una

        # Hasta un tope: veinte rutas no pueden empujar los botones fuera de la
        # pantalla. A partir de ahí la tabla se desplaza, que es su oficio.
        page._comparison = _comparacion_de(20)
        page._fill_table(page._comparison)
        assert page._table.rowCount() == 20
        # Diez filas y no veinte: el tope aguanta.
        assert page._table.maximumHeight() == una + 9 * (dos - una)


async def test_sin_rutas_la_tarjeta_no_repite_la_instruccion() -> None:
    """Una sola frase diciendo qué hacer, no dos seguidas diciendo lo mismo.

    El rótulo que ocupa el sitio de la tabla vacía ya explica que falta cotizar.
    La línea de «selecciona una ruta» sólo hace falta cuando **sí** hay rutas y
    ninguna está elegida, que es cuando el usuario ha hecho lo que se le pidió y
    aun así le falta un paso.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=True, con_planificador=True
    ) as (_, page):
        assert "Cotizar" in page._routes_empty.text()
        assert page._chosen.text() == ""

        # Con filas y sin selección, la línea vuelve: ahora sí falta un paso.
        page._comparison = _comparison(_pair())
        page._fill_table(page._comparison)
        page._table.clearSelection()
        page._on_quote_selected()
        assert page._chosen.text().startswith("Selecciona una ruta")


async def test_la_nota_recortada_se_puede_leer_entera() -> None:
    """La columna que dice de dónde sale cada cifra no puede ser ilegible.

    La nota se dibuja en una línea y se recorta —una fila por ruta, para poder
    comparar de un vistazo—, así que el texto completo tiene que estar en algún
    sitio. Sin el tooltip, la única columna que explica la procedencia del dato
    sería justo la que no se puede terminar de leer.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=True, con_planificador=True
    ) as (_, page):
        page._comparison = _comparison(_pair())
        page._fill_table(page._comparison)
        nota = _celda(page, 0, 7)
        # La cotización de prueba trae nota: de dónde salió la cifra.
        assert nota.text()
        assert nota.toolTip() == nota.text()


def _celda(pagina: PricesPage, fila: int, columna: int) -> QTableWidgetItem:
    item = pagina._table.item(fila, columna)
    assert item is not None, f"celda vacía en fila {fila}, columna {columna}"
    return item


async def test_el_boton_de_invertir_se_lee_sin_depender_de_la_fuente() -> None:
    """El único control sin texto al lado lleva una palabra, no un pictograma.

    Con el glifo «⇅» el botón se dibujaba con la fuente que hubiera instalada y
    salía un signo diminuto que se leía como una letra suelta. Una palabra se
    lee siempre, y aquí es lo único que dice qué hace el botón.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=True, con_planificador=True
    ) as (_, page):
        assert page._invert_btn.text().isalpha()
        assert page._invert_btn.toolTip()
