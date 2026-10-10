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

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
import tomlkit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from amigocompora.app.container import Container, build_container
from amigocompora.app.execution_policy import LedgerEntry
from amigocompora.app.usecases.estimate_cost import NetworkCost
from amigocompora.app.usecases.execute_swap import StepState, StepUpdate, SwapStep
from amigocompora.domain.addresses import shorten
from amigocompora.domain.errors import ConfirmationDeniedError, ExecutionError
from amigocompora.domain.models import (
    BroadcastReceipt,
    BroadcastStatus,
    PriceComparison,
    Quote,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import OperationMode
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.engines.catalog import quote_token, wrapped_native
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    app_secret_key,
    secret_key,
)
from amigocompora.ui.pages import prices as prices_module
from amigocompora.ui.pages.prices import PricesPage
from amigocompora.ui.route_list import RouteCard
from amigocompora.ui.theme import COLOR_MUTED, COLOR_WARNING, STYLESHEET

#: Clave de desarrollo **publicada** (la primera de Hardhat). No es un secreto:
#: que sea conocida es justo lo que la hace útil aquí, porque se puede afirmar
#: sobre la dirección derivada sin que la calcule el código que se está probando.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

#: La nota de las cotizaciones de prueba. Es la frase que dice de dónde sale cada
#: cifra; se escribe aquí y no en cada cotización para poder afirmar sobre ella.
_NOTA = "Leído del pool falso contra el estado de la cadena."

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
                # Con nota, como las de verdad: es el dato que dice de dónde
                # sale cada cifra, y sin ella la prueba de que se puede leer
                # entera estaría afirmando sobre una cadena vacía.
                source_note=_NOTA,
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


def _cotizacion(
    pair: TradingPair,
    *,
    engine_id: str,
    venue_id: str,
    nombre: str,
    importe: str,
    nota: str = _NOTA,
) -> Quote:
    """Una cotización con la forma de las de verdad: comisión, impacto y nota."""
    return Quote(
        venue=Venue(venue_id=venue_id, name=nombre, kind=VenueKind.DEX, chain="base"),
        engine_id=engine_id,
        pair=pair,
        amount_in=pair.base.amount("1"),
        amount_out=pair.quote.amount(importe),
        fee_bps=BasisPoints(5),
        price_impact_bps=BasisPoints(1),
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_note=nota,
    )


def _comparacion_mixta() -> PriceComparison:
    """Las dos clases de ruta en una tabla: la firmable y la de sólo cotización.

    Es la pila real: `uniswap` construye el swap y `geckoterminal` sólo publica
    la cifra. La de sólo cotización va **primera** en el orden de la tabla
    —2000 contra 1999—, que es justo lo que hace que ocultarla obligue a que la
    selección apunte a lo que sí se ve.
    """
    pair = _pair()
    return PriceComparison(
        pair=pair,
        amount_in=pair.base.amount("1"),
        quotes=(
            _cotizacion(
                pair,
                engine_id="uniswap",
                venue_id="uniswap-v3@base",
                nombre="uniswap-v3@0.05",
                importe="1999",
            ),
            _cotizacion(
                pair,
                engine_id="geckoterminal",
                venue_id="fake@base",
                nombre="fake",
                importe="2000",
                nota="Observado en GeckoTerminal.",
            ),
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
    motores: list[str] | None = None,
    ocultar_solo_cotiza: bool = False,
) -> AsyncIterator[tuple[Container, PricesPage]]:
    terminos = dict(_TERMINOS)
    if permitidos is not None:
        terminos["allowed_tokens"] = permitidos
    if topes is not None:
        terminos.update(topes)
    config: dict[str, object] = {"mode": modo.value, "execution": terminos}
    if con_planificador:
        # `motores` permite reproducir la pila real —`uniswap` y `geckoterminal`
        # activos a la vez—, que es el caso medido de la ruta que no se puede
        # firmar: el que sólo cotiza está encendido y aun así no construye.
        config["active_engines"] = {"dex_quotes": motores or "uniswap"}
    if ocultar_solo_cotiza:
        config["ui"] = {"hide_quote_only_routes": True}
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
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

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
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

        assert page._exec_btn.isEnabled() is False
        nota = page._exec_note.text()
        assert nota.startswith("No se puede ejecutar:")
        assert "OBSERVACIÓN" in nota


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
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

        assert page._exec_btn.isEnabled() is False
        nota = page._exec_note.text()
        assert "importe por operación" in nota
        assert _SALIDA_DE_LA_COMPARACION in nota


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
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

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
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

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
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

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
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

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
        assert page._routes.count() == 0


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


async def test_la_caja_mixta_del_token_no_lo_deja_fuera_de_la_lista() -> None:
    """`bankr` contra una lista que declara `BANKR` es un token permitido.

    Los símbolos de fuera del catálogo los publica un contrato y vienen en caja
    mixta, y el catálogo también tiene los suyos así (`pUSD`); la lista llega
    normalizada. El botón que firma no puede decir «bankr no está» mientras
    enseña `BANKR` declarado.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION,
        con_cartera=True,
        con_planificador=True,
        permitidos=["WETH", "BANKR"],
    ) as (_, page):
        base = wrapped_native("base")
        assert base is not None

        motivos = page._execution_blockers(TradingPair(base=base, quote=BANKR))
        assert motivos == ()


# --------------------------------------------------------------------------- #
# El alto de la lista y lo que se dice cuando no hay nada
# --------------------------------------------------------------------------- #
async def test_la_lista_de_rutas_no_reserva_alto_de_mas() -> None:
    """Una lista con dos rutas no puede medir como una de veinte.

    `QListWidget` reserva un alto natural que no tiene nada que ver con lo que
    lleva dentro. Sin corregirlo, la tarjeta de rutas dejaba ese alto entero
    para dos filas y un rectángulo vacío debajo de los botones, que se lee como
    un fallo de pintado y no como «aquí cabe más».
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=True, con_planificador=True
    ) as (_, page):
        # Sin cotizar, el rótulo ocupa el sitio y la lista no mide nada.
        assert page._routes.maximumHeight() == 0

        page._comparison = _comparacion_de(1)
        page._fill_routes(page._comparison)
        una = page._routes.maximumHeight()
        assert 0 < una < 192, "el alto tiene que salir de las rutas, no de Qt"

        # Y crece con las rutas, porque la lista de verdad las tiene.
        page._comparison = _comparacion_de(2)
        page._fill_routes(page._comparison)
        dos = page._routes.maximumHeight()
        assert dos > una

        # Hasta un tope: veinte rutas no pueden empujar los botones fuera de la
        # pantalla. A partir de ahí la lista se desplaza, que es su oficio.
        page._comparison = _comparacion_de(20)
        page._fill_routes(page._comparison)
        assert page._routes.count() == 20
        # Diez rutas y no veinte: el tope aguanta.
        assert page._routes.maximumHeight() == una + 9 * (dos - una)


async def test_sin_rutas_la_tarjeta_no_repite_la_instruccion() -> None:
    """Una sola frase diciendo qué hacer, no dos seguidas diciendo lo mismo.

    El rótulo que ocupa el sitio de la lista vacía ya explica que falta cotizar.
    La línea de «selecciona una ruta» sólo hace falta cuando **sí** hay rutas y
    ninguna está elegida, que es cuando el usuario ha hecho lo que se le pidió y
    aun así le falta un paso.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=True, con_planificador=True
    ) as (_, page):
        assert "Cotizar" in page._routes_empty.text()
        assert page._chosen.text() == ""

        # Con rutas y sin selección, la línea vuelve: ahora sí falta un paso.
        page._comparison = _comparison(_pair())
        page._fill_routes(page._comparison)
        page._routes.select_row(-1)
        page._on_quote_selected()
        assert page._chosen.text().startswith("Selecciona una ruta")


async def test_la_nota_recortada_se_puede_leer_entera() -> None:
    """El dato que dice de dónde sale cada cifra no puede ser ilegible.

    La tarjeta lleva la nota entera en su tooltip —la segunda línea se recorta
    para que la lista se lea de un vistazo— y el panel la escribe en su fila
    «Origen», que es donde se lee con calma. Sin ninguno de los dos, la única
    información que explica la procedencia del dato sería justo la que no se
    puede terminar de leer.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=True, con_planificador=True
    ) as (_, page):
        page._comparison = _comparison(_pair())
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

        # La cotización de prueba trae nota: de dónde salió la cifra.
        assert _tarjeta(page, 0).toolTip() == _NOTA
        origen = page._detail.values["origen"]
        assert origen.text() == _NOTA
        assert origen.toolTip() == origen.text()


async def test_una_ruta_que_solo_cotiza_se_marca_antes_de_elegirla() -> None:
    """La tarjeta de un motor que no construye lo que se firma lleva su marca.

    GeckoTerminal observa pools y publica cifras, pero no construye el swap: su
    ruta se puede comparar y no se puede firmar. Hasta ahora eso se decía recién
    al seleccionar la fila, en el aviso del botón, así que la lista dejaba
    elegir a ciegas justo la ruta que no lleva a firmar. La marca va en la
    tarjeta —el dato es del motor, no de la nota de la fuente—, en ámbar, como
    todo lo que avisa sin bloquear; el contraste es la otra tarjeta, que es
    firmable y va sin marca.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION,
        con_cartera=True,
        con_planificador=True,
        # La pila real: el que sólo cotiza está activo y aun así no construye.
        motores=["uniswap", "geckoterminal"],
    ) as (_, page):
        page._comparison = _comparacion_mixta()
        page._fill_routes(page._comparison)

        marcada = _tarjeta_de(page, "geckoterminal")
        limpia = _tarjeta_de(page, "uniswap")

        # `isHidden()` y no `isVisible()`: en una ventana que nunca se ha
        # mostrado todos los widgets son «invisibles», y lo que se afirma es que
        # la marca se ocultó o no, no que se haya pintado todavía.
        assert marcada.mark.text() == "· sólo cotiza"
        assert marcada.mark.isHidden() is False
        assert "no se puede firmar" in marcada.mark.toolTip()
        assert limpia.mark.isHidden() is True

        # El ámbar de la marca lo pone la hoja de estilos —no viaja en el
        # widget—, así que se afirma sobre la regla que lo pinta.
        assert f"QLabel#routeMark {{ color: {COLOR_WARNING}" in STYLESHEET


async def test_con_el_ajuste_encendido_la_lista_solo_ensena_lo_firmable() -> None:
    """`[ui] hide_quote_only_routes = true`: la de sólo cotización no llega a la lista.

    Lo que se afirma aquí es lo que hace seguro el ajuste: la ruta oculta **no
    ocupa un sitio** —la firmable, que iba segunda, pasa a la primera—, la
    selección devuelve la firmable y no la que su índice ocupaba antes, y el
    recuento dice cuántas se ocultaron: una lista con menos rutas de las que hay
    no puede parecer completa.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION,
        con_cartera=True,
        con_planificador=True,
        motores=["uniswap", "geckoterminal"],
        ocultar_solo_cotiza=True,
    ) as (_, page):
        page._comparison = _comparacion_mixta()
        page._fill_routes(page._comparison)

        assert page._routes.count() == 1
        tarjeta = _tarjeta(page, 0)
        assert tarjeta.quote is not None
        assert tarjeta.quote.engine_id == "uniswap"
        recuento = page._route_count.text()
        assert "1 de 2" in recuento
        assert "oculta" in recuento

        page._routes.select_row(0)
        elegida = page._selected_quote()
        assert elegida is not None
        assert elegida.engine_id == "uniswap"
        assert page._exec_btn.isEnabled() is True


async def test_si_todo_lo_que_se_cotizo_solo_cotiza_la_lista_lo_dice() -> None:
    """Con el ajuste encendido y nada firmable, la lista vacía explica el porqué.

    Sin esto la pantalla diría «ningún motor devolvió una ruta», que es falso:
    los motores sí contestaron y es el ajuste el que no las enseña. El rótulo
    nombra la clave exacta de la configuración, que es lo único que el usuario
    puede hacer al respecto.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION,
        con_cartera=True,
        con_planificador=True,
        motores=["uniswap", "geckoterminal"],
        ocultar_solo_cotiza=True,
    ) as (_, page):
        pair = _pair()
        page._comparison = PriceComparison(
            pair=pair,
            amount_in=pair.base.amount("1"),
            quotes=(
                _cotizacion(
                    pair,
                    engine_id="geckoterminal",
                    venue_id="fake@base",
                    nombre="fake",
                    importe="2000",
                    nota="Observado en GeckoTerminal.",
                ),
            ),
        )
        page._fill_routes(page._comparison)

        assert page._routes.count() == 0
        texto = page._routes_empty.text()
        assert "sólo cotizan" in texto
        assert "hide_quote_only_routes" in texto


# --------------------------------------------------------------------------- #
# Cambiar algo vacía lo de antes, en el acto
# --------------------------------------------------------------------------- #
async def test_cambiar_el_importe_vacia_lo_viejo_en_el_acto() -> None:
    """Lo que está en pantalla pertenece al importe que se ve; si cambia, se va.

    El caso medido: se escribía otro importe y la lista, el panel y los botones
    seguían enseñando la cotización anterior —y la anterior tarda lo que tarda
    una cotización—, así que lo que se leía como «esto es lo que hay» era en
    realidad lo que había. Ahora el cambio vacía en el acto y el rótulo lo
    dice: se está cotizando otra vez.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION,
        con_cartera=True,
        con_planificador=True,
        motores=["uniswap", "geckoterminal"],
    ) as (_, page):
        page._chain.setCurrentIndex(page._chain.findData("base"))
        page._base.setCurrentIndex(page._base.findText("WETH"))
        page._contra.setCurrentIndex(page._contra.findText("USDC"))
        page._comparison = _comparacion_mixta()
        page._fill_routes(page._comparison)
        # La primera fila es la de geckoterminal —mejor importe, no firmable—,
        # así que la elegida para esta prueba es la de uniswap, que sí lo es.
        for fila, tarjeta in enumerate(page._routes.cards()):
            if tarjeta.quote is not None and tarjeta.quote.engine_id == "uniswap":
                page._routes.select_row(fila)
                break
        assert page._exec_btn.isEnabled() is True

        # Como si la operación anterior hubiera terminado en esta ruta: el
        # desenlace y su lista son de lo anterior y no pueden sobrevivir al
        # cambio. Se simula el estado terminal directamente —el recorrido
        # completo se prueba en su propia prueba— porque lo que se mide aquí es
        # que el cambio de importe lo retira.
        page._exec_done = True
        page._executed_quote = page._selected_quote()
        page._exec_btn.setText("✓ Completado")

        # El importe cambia: lo de antes describía otro número.
        page._card.amount.setValue(page._card.amount.value() + 1)

        assert page._routes.count() == 0
        assert page._opp_table.rowCount() == 0
        assert page._detail.isHidden() is True
        assert page._route_count.text() == ""
        assert page._chosen.text() == ""
        assert page._exec_btn.isEnabled() is False
        # Y el desenlace de la operación anterior no se queda pegado al botón:
        # volvía a decir «✓ Completado» sobre una ruta que ya no está.
        assert page._exec_btn.text() == "Ejecutar: firmar y emitir…"
        assert page._exec_done is False
        assert "Cotizando" in page._routes_empty.text()
        assert page._routes_empty.text() == page._opps_empty.text()


async def test_elegir_el_mismo_token_en_las_dos_patas_vacia_lo_de_antes() -> None:
    """Un par imposible no puede dejar en pantalla las rutas del par anterior.

    Antes, ese cambio ni siquiera programaba cotización —no hay nada que
    cotizar— y lo de antes se quedaba: rutas de un par que ya no está en los
    desplegables, todavía seleccionables para preparar y firmar. Ahora el
    cambio vacía y el rótulo dice por qué.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION,
        con_cartera=True,
        con_planificador=True,
        motores=["uniswap", "geckoterminal"],
    ) as (_, page):
        page._chain.setCurrentIndex(page._chain.findData("base"))
        page._comparison = _comparacion_mixta()
        page._fill_routes(page._comparison)
        assert page._routes.count() == 2

        en_base = page._base.findText("WETH")
        en_contra = page._contra.findText("WETH")
        assert en_base >= 0
        assert en_contra >= 0
        page._base.setCurrentIndex(en_base)
        page._contra.setCurrentIndex(en_contra)

        assert page._routes.count() == 0
        assert page._detail.isHidden() is True
        assert "dos tokens distintos" in page._routes_empty.text()


async def test_pedir_otra_cotizacion_cancela_la_que_estaba_en_vuelo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una petición que ya no interesa se cancela: gastaba cuota y competía.

    Sin esto, cambiar el importe dos veces dejaba dos rondas completas de
    consultas corriendo a la vez y sólo se pintaba la última: la primera seguía
    gastando peticiones a las fuentes públicas —que van racionadas por ventana—
    y competía con la nueva por el mismo hueco de red.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=False, con_planificador=False
    ) as (_, page):

        async def lenta(seq: int | None = None) -> None:
            await asyncio.sleep(30)

        monkeypatch.setattr(page, "_do_quote", lenta, raising=True)

        page._on_quote()
        primera = page._quote_task
        page._on_quote()
        await asyncio.sleep(0.05)

        assert primera is not None
        assert primera.cancelled()
        assert page._quote_task is not primera

        # La última no se queda viva al acabar la prueba.
        page._invalidate_quote()
        await asyncio.sleep(0)


def _tarjeta(pagina: PricesPage, fila: int) -> RouteCard:
    """La tarjeta de una fila de la lista, que es donde vive lo que se pinta."""
    tarjeta = pagina._routes.card(fila)
    assert tarjeta is not None, f"sin tarjeta en la fila {fila}"
    return tarjeta


def _tarjeta_de(pagina: PricesPage, engine_id: str) -> RouteCard:
    """La tarjeta del motor nombrado, sin depender del orden de la comparación."""
    for tarjeta in pagina._routes.cards():
        if tarjeta.quote is not None and tarjeta.quote.engine_id == engine_id:
            return tarjeta
    raise AssertionError(f"ninguna tarjeta es del motor «{engine_id}»")


# --------------------------------------------------------------------------- #
# El panel de detalle: lo que el usuario está a punto de firmar
# --------------------------------------------------------------------------- #
async def test_el_panel_ensena_la_ruta_elegida_con_lo_que_no_se_veia() -> None:
    """Los datos que existían y no se enseñaban en ninguna parte.

    El camino de la ruta, la cartera que recibe y el deslizamiento máximo
    configurado. Se elige la fila como lo haría un clic —la señal de la lista
    mueve el panel— en vez de llamar al método a mano: lo que se comprueba es
    que el panel sigue a la selección, no que sepa pintarse.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (container, page):
        page._comparison = _comparison(_pair())
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

        detalle = page._detail.values
        # El camino: un solo pool, que es el par que ya está sobre la tarjeta.
        assert detalle["ruta"].text() == "fake · WETH → USDC"
        # La cartera: la que firmaría, recortada, con la dirección entera en el
        # tooltip —una dirección recortada que no se puede expandir es una
        # dirección que no se puede comprobar—.
        cartera = container.keys.address()
        assert cartera is not None
        assert detalle["cartera"].text() == shorten(cartera)
        assert detalle["cartera"].toolTip() == cartera
        # El deslizamiento: la tolerancia **viva** —la que el engranaje cambia y
        # con la que se construye el swap—, no la que traía la configuración al
        # arrancar.
        assert detalle["deslizamiento"].text() == f"{container.slippage.bps} bps"
        assert "engranaje" in detalle["deslizamiento"].toolTip()


#: Un coste de red con la forma del de verdad: 21 000 de gas a 1 gwei son
#: 0,000021 ETH, el producto que enseña el panel.
_COSTE = NetworkCost(
    native=TokenAmount(raw=21_000 * 10**9, decimals=18, symbol="ETH"),
    gas_limit=21_000,
    gas_price_wei=10**9,
    source="estimado por el nodo",
)


def _paso_de_coste(
    coste: NetworkCost | None, error: str | None, *, detail: str = ""
) -> StepUpdate:
    """El aviso del paso ESTIMATE, con el desenlace de coste que se quiera medir."""
    return StepUpdate(
        step=SwapStep.ESTIMATE,
        state=StepState.DONE,
        title="Coste de red",
        detail=detail,
        cost=coste,
        cost_error=error,
    )


@pytest.mark.parametrize(
    ("coste", "error", "esperado", "color"),
    [
        # La estimación salió: la cifra, con «≈» porque es un techo.
        (_COSTE, None, "≈ 0.000021 ETH", ""),
        # Se intentó y no salió: el motivo, en ámbar y sin bloquear nada.
        (
            None,
            "execution reverted: ERC20: transfer amount exceeds allowance",
            "no se pudo estimar",
            COLOR_WARNING,
        ),
        # No se intentó —una transacción de Solana, o sin estimador—: raya en
        # gris apagado, que no es un cero ni tampoco un fallo.
        (None, None, "—", COLOR_MUTED),
    ],
)
async def test_el_coste_de_red_del_panel_cuenta_el_desenlace(
    coste: NetworkCost | None,
    error: str | None,
    esperado: str,
    color: str,
) -> None:
    """Los tres desenlaces del coste, y ninguno disfrazado de cero.

    La fila empieza en «se estima al preparar» —la cifra es una llamada a la red
    por ruta y no se hace al comparar precios— y la rellena el paso ESTIMATE de
    la ejecución, que es donde la estimación vive ahora. Lo que no puede pasar
    es que un fallo de estimación tumbe la operación ni que el hueco se rellene
    con un cero: el motivo se lee en el tooltip, y «se intentó y no salió» se
    distingue de «no se intentó», que no son el mismo desenlace.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (_, page):
        page._comparison = _comparison(_pair())
        page._fill_routes(page._comparison)
        page._routes.select_row(0)
        assert page._detail.values["coste"].text() == "se estima al preparar"

        page._on_step(
            _paso_de_coste(coste, error, detail="lo que quedó en la lista"),
            page._shown_quotes[0],
        )

        etiqueta = page._detail.values["coste"]
        assert etiqueta.text() == esperado
        # La cifra no lleva color; el fallo va en ámbar y la raya en gris apagado,
        # para que ninguno de los dos se lea como una cifra.
        assert etiqueta.styleSheet() == (f"color: {color};" if color else "")
        if error is not None:
            assert error in etiqueta.toolTip()


async def test_el_coste_estimado_no_sobrevive_a_cambiar_de_ruta() -> None:
    """La cifra es de **una** ruta: al elegir otra vuelve a «se estima al preparar».

    Dejar escrita la estimación de otro swap al lado de la ruta nueva sería
    enseñar un coste que no es el suyo, que es una forma de firmar algo distinto
    de lo que se leyó.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION,
        con_cartera=True,
        con_planificador=True,
        motores=["uniswap", "geckoterminal"],
    ) as (_, page):
        page._comparison = _comparacion_mixta()
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

        page._on_step(
            _paso_de_coste(_COSTE, None), page._shown_quotes[0]
        )
        assert page._detail.values["coste"].text() == "≈ 0.000021 ETH"

        page._routes.select_row(1)

        assert page._detail.values["coste"].text() == "se estima al preparar"


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


# --------------------------------------------------------------------------- #
# El botón único: la narración en vivo y la lista de lo que pasó de verdad
# --------------------------------------------------------------------------- #
#: Los hashes de las tres transacciones que emite una operación encadenada por
#: Permit2: los dos permisos y el swap. Son inventados y con la forma exacta de
#: los de verdad —«0x» y 64 dígitos— porque el recibo y la lista los validan al
#: pintarlos, y un hash inválido estaría midiendo una pantalla que no es la real.
_HASH_DEL_PERMISO = "0x" + "1a" * 32
_HASH_DE_PERMIT2 = "0x" + "2b" * 32
_HASH_DEL_SWAP = "0x" + "3c" * 32

#: La operación narrada **como la narra `ExecuteSwap`**: los títulos son los
#: suyos, copiados de `execute_swap.py` —donde los fija `test_execute_swap.py`
#: contra nodos falsos—, y el orden es el de una ejecución encadenada de verdad:
#: preparar, permiso del ERC-20, permiso en Permit2, coste de red y swap.
_OPERACION = (
    StepUpdate(SwapStep.PREPARE, StepState.RUNNING, "Preparando el swap…"),
    StepUpdate(SwapStep.PREPARE, StepState.DONE, "Swap preparado"),
    StepUpdate(SwapStep.APPROVE, StepState.RUNNING, "Aprobando pUSD (1 de 2)…"),
    StepUpdate(
        SwapStep.APPROVE,
        StepState.DONE,
        "Permiso de pUSD (1 de 2)",
        tx_hash=_HASH_DEL_PERMISO,
    ),
    StepUpdate(
        SwapStep.APPROVE_PERMIT2,
        StepState.RUNNING,
        "Aprobando pUSD en Permit2 (2 de 2)…",
    ),
    StepUpdate(
        SwapStep.APPROVE_PERMIT2,
        StepState.DONE,
        "Permiso de pUSD en Permit2 (2 de 2)",
        tx_hash=_HASH_DE_PERMIT2,
    ),
    StepUpdate(SwapStep.ESTIMATE, StepState.RUNNING, "Estimando el coste de red…"),
    StepUpdate(SwapStep.ESTIMATE, StepState.DONE, "Coste de red", cost=_COSTE),
    StepUpdate(SwapStep.SWAP, StepState.RUNNING, "Esperando tu confirmación…"),
    StepUpdate(SwapStep.SWAP, StepState.RUNNING, "Firmando y emitiendo…"),
    StepUpdate(SwapStep.SWAP, StepState.DONE, "Swap en fake", tx_hash=_HASH_DEL_SWAP),
)

#: El desenlace del primer permiso cuando el usuario dice que no en su diálogo:
#: el paso queda rechazado y el caso de uso vuelve a lanzar la negativa.
_RECHAZO_DEL_PERMISO = (
    *_OPERACION[:3],
    StepUpdate(
        SwapStep.APPROVE,
        StepState.REJECTED,
        "Permiso de pUSD (1 de 2)",
        detail="lo rechazaste en el diálogo: no se firmó nada",
    ),
)

#: El desenlace de un permiso que minó en rojo: el paso queda fallado y el caso
#: de uso lanza el error —que es la mitad que un rechazo no tiene—.
_FALLO_DEL_PERMISO = (
    *_OPERACION[:3],
    StepUpdate(
        SwapStep.APPROVE,
        StepState.FAILED,
        "Permiso de pUSD (1 de 2)",
        detail="la transacción no llegó a buen término (reverted)",
        tx_hash=_HASH_DEL_PERMISO,
    ),
)

#: El recibo de la operación que salió bien, con la forma del que da el nodo.
_RECIBO = BroadcastReceipt(
    tx_hash=_HASH_DEL_SWAP,
    chain="base",
    status=BroadcastStatus.SUCCESS,
    observed_at=datetime(2026, 1, 1, tzinfo=UTC),
)


class _EjecucionSimulada:
    """Un `ExecuteSwap` de mentira que cuenta la operación como la cuenta el real.

    Sustituye a `container.execute_swap` para poder recorrer la pantalla entera
    —botón, lista y desenlaces— sin firmar nada: lo que se prueba aquí es la
    interfaz, y la narración de verdad ya tiene sus pruebas contra nodos falsos
    en `test_execute_swap.py`. `textos` guarda lo que el botón decía **tras cada
    aviso**, que es la única forma de medir «va contando a medida que avanza»:
    la narración ocurre dentro de la llamada y al volver sólo quedaría el final.
    """

    def __init__(
        self,
        page: PricesPage,
        *,
        pasos: tuple[StepUpdate, ...] = _OPERACION,
        desenlace: BaseException | None = None,
    ) -> None:
        self._page = page
        self._pasos = pasos
        self._desenlace = desenlace
        self.textos: list[str] = []

    async def __call__(
        self, quote: Quote, *, recipient: str, on_step: Callable[[StepUpdate], None]
    ) -> BroadcastReceipt:
        for update in self._pasos:
            on_step(update)
            self.textos.append(self._page._exec_btn.text())
        if self._desenlace is not None:
            raise self._desenlace
        return _RECIBO


async def test_el_boton_narra_cada_fase_y_acaba_en_completado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un solo botón que va contando la operación de verdad, fase a fase.

    Es lo que sustituye a la antigua cadena de botones —«Preparar», «Guardar»,
    «Ejecutar»—: una pulsación arranca la operación entera y el texto del botón
    sigue al último paso que empezó, que es lo que deja ver el avance sin abrir
    nada. Cada paso «terminado» no retrocede el texto —el botón dice en qué se
    está, no lo último que acabó— y al final queda el estado terminal con su
    hash, con el botón apagado: terminado no es reintentable sin empezar de
    nuevo.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (container, page):
        page._comparison = _comparison(_pair())
        page._fill_routes(page._comparison)
        page._routes.select_row(0)
        ruta = page._selected_quote()

        doble = _EjecucionSimulada(page)
        monkeypatch.setattr(container, "execute_swap", doble)

        page._on_execute()

        # Nada más pulsar, la pantalla ya dice que la operación arrancó: el
        # botón se apaga —no se puede lanzar dos veces— y la línea avisa de que
        # cada transacción pedirá su propio «sí».
        assert page._exec_btn.isEnabled() is False
        assert page._exec_btn.text() == "Preparando el swap…"
        assert "cada transacción pedirá su propia confirmación" in page._status.text()

        await asyncio.sleep(0.05)

        # El botón recorrió los títulos de los pasos que empezaron, en orden y
        # sin saltarse ninguno: la secuencia sin repetir es la de los `RUNNING`.
        empezados = [
            update.title for update in _OPERACION if update.state is StepState.RUNNING
        ]
        assert list(dict.fromkeys(doble.textos)) == empezados
        # Y los «terminado» no reescriben el texto: tras el DONE de preparar, el
        # botón sigue diciendo en qué estaba.
        assert doble.textos[1] == "Preparando el swap…"
        # Igual tras el último DONE —el «✓ Completado» lo pone el desenlace—.
        assert doble.textos[-1] == "Firmando y emitiendo…"

        # El desenlace: estado terminal, con el hash a la vista y sin poder
        # volver a pulsar el mismo botón.
        assert page._exec_btn.text() == "✓ Completado"
        assert page._exec_btn.isEnabled() is False
        assert page._exec_done is True
        assert page._executed_quote is ruta
        assert page._status.text() == f"Emitida {_HASH_DEL_SWAP} · estado success."
        # La lista quedó con una fila por paso real, con su glifo y su título.
        assert page._steps.steps() == (
            SwapStep.PREPARE,
            SwapStep.APPROVE,
            SwapStep.APPROVE_PERMIT2,
            SwapStep.ESTIMATE,
            SwapStep.SWAP,
        )
        assert page._steps.glyph_of(SwapStep.SWAP) == "✓"
        assert page._steps.title_of(SwapStep.SWAP) == "Swap en fake"
        # El coste de red que narró el paso ESTIMATE acabó en el panel.
        assert page._detail.values["coste"].text() == "≈ 0.000021 ETH"
        # Y «Empezar de nuevo» está a la vista para poder barrer el relato.
        assert page._new_btn.isHidden() is False


async def test_un_rechazo_devuelve_el_boton_sin_llamarlo_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decir que no en un diálogo no es una avería, y la pantalla no puede mentir.

    El caso que enseñó a separar los dos desenlaces: el usuario rechaza la
    primera aprobación —que es lo correcto si no lo tiene claro— y la pantalla
    respondía «Error: …», empujando a tratar una decisión legítima como algo que
    hay que arreglar. El rechazo vuelve al botón de ejecutar —reintentable: los
    permisos ya concedidos se reaprovechan— y queda **listado** como paso
    rechazado, que es parte del relato de lo que pasó.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (container, page):
        page._comparison = _comparison(_pair())
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

        doble = _EjecucionSimulada(
            page,
            pasos=_RECHAZO_DEL_PERMISO,
            desenlace=ConfirmationDeniedError("rechazaste la aprobación"),
        )
        monkeypatch.setattr(container, "execute_swap", doble)

        page._on_execute()
        await asyncio.sleep(0.05)

        assert page._status.text() == "Cancelaste la operación. No se firmó ni se emitió nada."
        assert "Error" not in page._status.text()
        assert page._exec_btn.text() == "Ejecutar: firmar y emitir…"
        assert page._exec_btn.isEnabled() is True
        assert page._exec_done is False
        # El rechazo se lista con su motivo: la lista cuenta la historia entera.
        assert page._steps.glyph_of(SwapStep.APPROVE) == "✕"
        assert page._new_btn.isHidden() is False


async def test_un_fallo_al_emitir_se_cuenta_como_error_y_el_boton_vuelve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un fallo de verdad —la transacción no llegó a buen término— sí es un error.

    La otra cara del rechazo: aquí sí hubo algo que arreglar (el permiso minó
    en rojo), y la línea de estado lo dice con el motivo. El botón vuelve a su
    sitio igual —se puede reintentar— pero la lista deja el paso fallado con su
    hash, que es lo que hace falta para mirarlo en el explorador.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (container, page):
        page._comparison = _comparison(_pair())
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

        doble = _EjecucionSimulada(
            page,
            pasos=_FALLO_DEL_PERMISO,
            desenlace=ExecutionError("la aprobación de pUSD no llegó a buen término (reverted)"),
        )
        monkeypatch.setattr(container, "execute_swap", doble)

        page._on_execute()
        await asyncio.sleep(0.05)

        assert page._status.text().startswith("Error: ")
        assert "no llegó a buen término" in page._status.text()
        assert page._exec_btn.text() == "Ejecutar: firmar y emitir…"
        assert page._exec_btn.isEnabled() is True
        assert page._exec_done is False
        assert page._steps.glyph_of(SwapStep.APPROVE) == "✕"
        assert page._new_btn.isHidden() is False


async def test_empezar_de_nuevo_barre_el_relato_y_deja_el_boton_listo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminada la operación, «Empezar de nuevo» devuelve la pantalla al inicio.

    Barre lo único que sobra tras una operación —los pasos, que ya cumplieron su
    función de contar qué pasó— y no toca la cotización ni la ruta elegida: el
    usuario sigue donde estaba, con el botón como recién llegado para hacer otro
    swap sobre la misma ruta. El click se da sobre el botón de verdad, que es lo
    que prueba que está conectado y no sólo pintado.
    """
    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (container, page):
        page._comparison = _comparison(_pair())
        page._fill_routes(page._comparison)
        page._routes.select_row(0)

        monkeypatch.setattr(container, "execute_swap", _EjecucionSimulada(page))
        page._on_execute()
        await asyncio.sleep(0.05)
        assert page._exec_btn.text() == "✓ Completado"
        assert page._steps.steps() != ()

        page._new_btn.click()

        assert page._steps.steps() == ()
        # `isHidden()` y no `isVisible()`: la ventana nunca se ha mostrado, así
        # que lo que se afirma es que el panel se ocultó, no que se haya pintado.
        assert page._steps.isHidden() is True
        assert page._new_btn.isHidden() is True
        assert page._exec_btn.text() == "Ejecutar: firmar y emitir…"
        assert page._exec_btn.isEnabled() is True
        assert page._exec_done is False
        assert page._executed_quote is None
        assert page._status.text() == ""
        # Y la ruta sigue elegida: volver al estado inicial no es perder el sitio.
        assert page._selected_quote() is not None
        assert page._detail.isHidden() is False


# --------------------------------------------------------------------------- #
# El engranaje: la tolerancia con la que se construyen los swaps
# --------------------------------------------------------------------------- #
#: La configuración que encontrará el engranaje al guardar, con un comentario y
#: claves que no son suyas: la escritura edita la tabla `[execution]`, no la
#: recrea, y esto lo mide.
_CONFIG_CON_EJECUCION = """\
# Comentario que debe sobrevivir.
mode = "execution"

[execution]
enabled = true
max_value_usd = 250
"""


class _SlippageFalso:
    """Un `SlippageDialog` de mentira: contesta sin abrir nada.

    Mismo patrón que los dobles de diálogo del resto de la suite —la retirada
    falsa de `test_wallet_page.py`—: se sustituye la clase en el módulo de la
    página, se anota con qué valor vigente se abrió y se contesta lo que la
    prueba pida por las clases, para poder medir el guardado y la cancelación
    con el mismo doble.
    """

    Accepted = QDialog.DialogCode.Accepted
    respuesta: QDialog.DialogCode = QDialog.DialogCode.Accepted
    elegido = 250
    abiertos: ClassVar[list[int]] = []

    def __init__(self, bps: int, parent: object = None) -> None:
        _SlippageFalso.abiertos.append(bps)

    def exec(self) -> QDialog.DialogCode:
        return _SlippageFalso.respuesta

    def value_bps(self) -> int:
        return _SlippageFalso.elegido


def _enganchar_engranaje(
    monkeypatch: pytest.MonkeyPatch, archivo: Path, *, cancelar: bool = False
) -> type[_SlippageFalso]:
    """Deja el engranaje apuntando al doble y a un `config.toml` de verdad.

    El archivo es el de `tmp_path`: `_on_slippage` escribe en el `config.toml`
    de verdad —no se dobla el escritor, porque lo que se prueba es que la
    escritura ocurre—, pero nunca en el de la máquina que corre las pruebas.
    """
    _SlippageFalso.abiertos.clear()
    monkeypatch.setattr(
        _SlippageFalso,
        "respuesta",
        QDialog.DialogCode.Rejected if cancelar else QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(prices_module, "SlippageDialog", _SlippageFalso)
    monkeypatch.setattr(prices_module, "config_file", lambda: archivo)
    return _SlippageFalso


async def test_el_engranaje_aplica_el_deslizamiento_y_lo_guarda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El número del diálogo pasa a la copia viva, a la pantalla y al archivo.

    Las tres a la vez porque las tres son la misma promesa: que lo que se firma
    usa esa tolerancia. `config.toml` se escribe de verdad —contra un archivo de
    `tmp_path`— porque el fallo que esta prueba existe para impedir es el de un
    engranaje que cambia una etiqueta y no el número: la tolerancia viaja al
    motor al construir el swap, y eso lo fija `test_prepare_swap.py`.
    """
    archivo = tmp_path / "config.toml"
    archivo.write_text(_CONFIG_CON_EJECUCION, encoding="utf-8")

    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (container, page):
        page._comparison = _comparison(_pair())
        page._fill_routes(page._comparison)
        page._routes.select_row(0)
        assert container.slippage.bps == 50

        _enganchar_engranaje(monkeypatch, archivo)
        # El click va sobre el engranaje de verdad: lo que se comprueba es la
        # cadena «pulsar → señal → diálogo», no que el método sepa guardar.
        page._card._slippage_btn.click()

        # El diálogo se abrió con el valor vigente, no con el de fábrica.
        assert _SlippageFalso.abiertos == [50]
        # La copia viva —la que lee el constructor del swap— cambió.
        assert container.slippage.bps == 250
        # El engranaje enseña el valor nuevo sin tener que abrir nada.
        assert "2.5 %" in page._card._slippage_btn.toolTip()
        # El panel de detalle repinta su fila con el valor vigente.
        assert page._detail.values["deslizamiento"].text() == "250 bps"
        # Y quedó guardado sin llevarse por delante lo que no era suyo.
        texto = archivo.read_text(encoding="utf-8")
        assert tomlkit.parse(texto)["execution"]["slippage_bps"] == 250
        assert "# Comentario que debe sobrevivir." in texto
        assert "max_value_usd = 250" in texto
        # La línea de estado lo dice, en la unidad que se escribió.
        assert page._status.text().startswith("Deslizamiento por defecto: 2.5 %")


async def test_cancelar_el_engranaje_no_cambia_nada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decir que no en el engranaje deja la tolerancia y el archivo como estaban.

    Cancelar es la salida honesta del diálogo, así que no puede quedar ni un
    rastro: ni la copia viva, ni el archivo, ni una línea de estado prometiendo
    algo que no pasó.
    """
    archivo = tmp_path / "config.toml"
    archivo.write_text(_CONFIG_CON_EJECUCION, encoding="utf-8")

    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (container, page):
        _enganchar_engranaje(monkeypatch, archivo, cancelar=True)

        page._card._slippage_btn.click()

        assert _SlippageFalso.abiertos == [50]
        assert container.slippage.bps == 50
        assert archivo.read_text(encoding="utf-8") == _CONFIG_CON_EJECUCION
        assert not archivo.with_name("config.toml.bak").exists()
        assert page._status.text() == ""


async def test_si_el_guardado_falla_se_dice_y_la_tolerancia_rige_la_sesion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La escritura que falla se cuenta; callarla dejaría al usuario creyendo que sí.

    El valor **sí** rige esta sesión —vive en la copia viva, que es la que lee el
    motor—, y eso es cierto y se aplica; lo que no puede es darse por guardado.
    El aviso dice las dos cosas: que no se pudo escribir y que vale hasta cerrar.
    """
    archivo = tmp_path / "config.toml"
    archivo.write_text(_CONFIG_CON_EJECUCION, encoding="utf-8")

    def _revienta(path: Path, bps: int) -> None:
        raise OSError("disco lleno")

    async with _pagina(
        modo=OperationMode.EXECUTION, con_cartera=True, con_planificador=True
    ) as (container, page):
        _enganchar_engranaje(monkeypatch, archivo)
        monkeypatch.setattr(prices_module, "set_execution_slippage_bps", _revienta)

        page._card._slippage_btn.click()

        # Se aplicó en el acto: la tolerancia de esta sesión es la nueva.
        assert container.slippage.bps == 250
        assert "No se pudo guardar en config.toml" in page._status.text()
        assert "esta sesión" in page._status.text()
        # Y el archivo quedó como estaba: no hay guardado a medias.
        assert archivo.read_text(encoding="utf-8") == _CONFIG_CON_EJECUCION
