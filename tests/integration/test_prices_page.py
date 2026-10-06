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

from PySide6.QtWidgets import QApplication

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
    *, modo: OperationMode, con_cartera: bool, con_planificador: bool
) -> AsyncIterator[tuple[Container, PricesPage]]:
    config: dict[str, object] = {"mode": modo.value, "execution": _TERMINOS}
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


async def test_the_amount_label_follows_the_direction() -> None:
    """La cantidad se escribe en la unidad de lo que se entrega, y eso cambia.

    Es la corrección que hace posible comprar: antes el par se armaba siempre
    como `token/stable`, o sea vender, y la etiqueta no decía en qué unidad
    estaba el número. Un importe ambiguo, en una pantalla que firma, es un
    importe equivocado.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=False, con_planificador=False
    ) as (_, page):
        page._direction.setCurrentIndex(0)  # Comprar
        assert page._unit.text() == "USDC"
        assert page._pair.text() == "USDC → WETH"

        page._direction.setCurrentIndex(1)  # Vender
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


async def test_un_par_contra_el_nativo_se_puede_armar() -> None:
    """La pata contraria se elige, y con eso se arma un par que no toca el USDC.

    Es el caso que bloqueaba la compra de `bankr`: su única piscina es contra
    WETH, así que un par armado siempre contra la stablecoin no tenía por dónde
    cotizarse.
    """
    async with _pagina(
        modo=OperationMode.OBSERVATION, con_cartera=False, con_planificador=False
    ) as (_, page):
        page._contra.setCurrentIndex(page._contra.findText("WETH"))
        page._base.setCurrentIndex(page._base.findText("USDC"))
        page._direction.setCurrentIndex(0)  # Comprar

        entrega, recibe = page._legs()
        assert entrega is not None
        assert recibe is not None
        assert (entrega.symbol, recibe.symbol) == ("WETH", "USDC")
        assert page._pair.text() == "WETH → USDC"
        assert page._unit.text() == "WETH"


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
