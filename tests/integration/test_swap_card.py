"""La tarjeta de intercambio: sus tres operaciones, sus patas y su botón.

### Qué se mide aquí

Es la pantalla que se ve al abrir la aplicación, así que lo que se comprueba es
que **se lea como una cartera** y no como un formulario: que las tres operaciones
—intercambiar, enviar, depositar— estén en un solo sitio y cambien lo que se ve;
que el saldo esté en la pata del token y no en una línea aparte; y que el botón que
firma diga **por qué** está apagado en vez de quedarse mudo.

### Lo que NO se mide

Ni una regla de política. Si se puede firmar lo decide `ui/execution_gate.py`, que
llama a las mismas comprobaciones que correrán al firmar, y eso se prueba ya en
`test_prices_page.py` —la lista de motivos es la misma y no se duplica aquí—. Lo que
se comprueba en este fichero es que ese veredicto **llegue a la pantalla**.

El motor de cartera se falsea —es el único que sale a la red— y nada más: el
contenedor, la composición de la tarjeta y los widgets son los de producción.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
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
from amigocompora.ui.pages.prices import PricesPage
from amigocompora.ui.pages.swap_card import TAB_RECEIVE, TAB_SEND, TAB_SWAP

#: La primera cuenta de Hardhat, publicada y sin fondos. Está aquí porque la
#: tarjeta necesita una **dirección** que enseñar y de la que leer saldos: sin clave
#: configurada no hay cabecera que pintar y media pantalla no se probaría.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION_DE_DESARROLLO = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


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


def _store(*, con_cartera: bool) -> InMemorySecretStore:
    store = InMemorySecretStore()
    if con_cartera:
        store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    return store


def _nativo(chain_key: str, cantidad: str) -> TokenHolding:
    token = native_token(chain_key)
    return TokenHolding(token=token, amount=token.amount(cantidad))


def _referencia(chain_key: str, cantidad: str) -> TokenHolding:
    token = quote_token(chain_key)
    assert token is not None
    return TokenHolding(token=token, amount=token.amount(cantidad))


@asynccontextmanager
async def _tarjeta(
    posiciones: Mapping[str, tuple[TokenHolding, ...]] | None = None,
    *,
    con_cartera: bool = True,
    permitidos: list[str] | None = None,
) -> AsyncIterator[tuple[Container, PricesPage]]:
    """La página de verdad, con una cartera falsa si se pide alguna posición.

    Se devuelve la **página** y no la tarjeta sola porque el estado que se está
    midiendo —el modo, los topes, la cartera— vive en ella; la tarjeta es la vista
    que lo pinta. Se accede a ella con `pagina._card` y con las propiedades
    delegadas, que es como la usa la ventana.
    """
    config: dict[str, object] = {"mode": OperationMode.OBSERVATION.value}
    if permitidos is not None:
        config["execution"] = {"allowed_tokens": permitidos}
    container = await build_container(
        Settings.model_validate(config),
        secret_store=_store(con_cartera=con_cartera),
        discover=False,
        configure_logs=False,
    )
    try:
        if posiciones is not None:
            container.registry.register(
                ProveedorFalso(CarteraFalsa(posiciones)), source="prueba"
            )
            await container.registry.activate("wallet")
        yield container, PricesPage(container)
    finally:
        await container.aclose()


async def _esperar(condicion: Callable[[], bool]) -> None:
    """Deja pasar el turno hasta que la condición se cumpla, o falla con un mensaje."""
    for _ in range(500):
        if condicion():
            return
        await asyncio.sleep(0)
    raise AssertionError("la condición no se cumplió: la lectura no terminó")


def _elegir(combo, chain_key: str, symbol: str) -> None:  # type: ignore[no-untyped-def]
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


def _comparacion(chain_key: str = "base") -> PriceComparison:
    """Una comparación con una sola ruta, para las pantallas que la necesitan.

    Se construye a mano y no cotizando: lo que se mide aquí es qué se **pinta** con
    una ruta delante, no que los motores respondan. Cotizar de verdad exigiría red,
    y una prueba de interfaz que sale a la red falla el día que el nodo esté lento.
    """
    entrega = wrapped_native(chain_key)
    recibe = quote_token(chain_key)
    assert entrega is not None
    assert recibe is not None
    par = TradingPair(base=entrega, quote=recibe)
    venue = Venue(
        venue_id=f"fake@{chain_key}", name="fake", kind=VenueKind.DEX, chain=chain_key
    )
    return PriceComparison(
        pair=par,
        amount_in=par.base.amount("1"),
        quotes=(
            Quote(
                venue=venue,
                engine_id="motor_falso",
                pair=par,
                amount_in=par.base.amount("1"),
                amount_out=par.quote.amount("2510.44"),
                fee_bps=BasisPoints(5),
                price_impact_bps=BasisPoints(1),
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                source_note="Leído del pool falso contra el estado de la cadena.",
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# 1. Las tres operaciones, en un solo sitio
# --------------------------------------------------------------------------- #
async def test_las_tres_operaciones_cambian_lo_que_se_ve() -> None:
    """Intercambiar, Enviar y Depositar son tres formularios, no tres pestañas.

    Tener «depositar» en un diálogo y «enviar» en otra pantalla obliga a recordar
    dónde está cada una. Aquí lo que se comprueba es que las tres están y que cada
    una enseña lo suyo —y lo suyo solo—.
    """
    async with _tarjeta({"polygon": (_nativo("polygon", "19.49"),)}) as (_c, pagina):
        card = pagina._card
        assert [card._segment_btns[i].text() for i in range(3)] == [
            "Intercambiar",
            "Enviar",
            "Depositar",
        ]

        # Al abrir, la pata que se entrega y la que se recibe están visibles.
        assert card.current_segment() == TAB_SWAP
        assert card._give.isVisibleTo(card) is True
        assert card._want.isVisibleTo(card) is True

        card.set_segment(TAB_SEND)
        assert card.current_segment() == TAB_SEND
        assert card._send_leg.isVisibleTo(card) is True
        assert card._give.isVisibleTo(card) is False

        card.set_segment(TAB_RECEIVE)
        assert card.current_segment() == TAB_RECEIVE
        # La dirección de depósito es la de la cartera que firma, y va entera:
        # acortarla escondería justo los caracteres donde vive un error de copia.
        assert card._deposit_address.text() == DIRECCION_DE_DESARROLLO


async def test_depositar_avisa_de_la_red_y_dibuja_el_qr() -> None:
    """El aviso de red es el que evita la pérdida que no tiene reclamación."""
    async with _tarjeta({"polygon": (_nativo("polygon", "1"),)}) as (_c, pagina):
        card = pagina._card
        _elegir_red(pagina, "polygon")
        card.set_segment(TAB_RECEIVE)

        aviso = card._deposit_warning.text()
        assert "Polygon" in aviso
        assert "no se recupera" in aviso
        assert not card._qr.pixmap().isNull(), "tiene que haber QR que escanear"
        assert card._copy_btn.isEnabled() is True


async def test_sin_cartera_depositar_lo_dice_en_vez_de_ensenar_un_hueco() -> None:
    """Sin clave no hay dirección, y decir por qué es más útil que un guion."""
    async with _tarjeta(con_cartera=False) as (_c, pagina):
        card = pagina._card
        card.set_segment(TAB_RECEIVE)

        assert card._deposit_address.text() == "—"
        assert card._qr.pixmap().isNull() is True
        assert "Credenciales" in card._deposit_warning.text()
        assert card._copy_btn.isEnabled() is False


# --------------------------------------------------------------------------- #
# 2. El saldo, en la pata del token
# --------------------------------------------------------------------------- #
async def test_el_saldo_se_lee_en_la_pata_que_se_entrega() -> None:
    """El saldo vive donde se escribe el importe, no en una línea aparte.

    Es la pregunta que se hace justo antes de teclear el número: «¿cuánto tengo de
    esto?». Se comprueba con la cifra **entera** en el tooltip, porque el recorte es
    de la pantalla y tener que irse a la cartera para comprobarla sería esconder un
    dato ya calculado.
    """
    async with _tarjeta({"polygon": (_nativo("polygon", "19.49166614"),)}) as (_c, pagina):
        _elegir_red(pagina, "polygon")
        _elegir(pagina._base, "polygon", "POL")
        await _esperar(lambda: "Disponible" in pagina._balance.text())

        assert pagina._balance.text() == "Disponible: 19.4917 POL"
        assert "19.49166614" in pagina._balance.toolTip()
        # Y el aviso de que el gas sale del mismo saldo, que es donde se comete el
        # error: entregar el nativo entero no cabe nunca.
        assert "comisión" in pagina._balance.toolTip()
        # Se pregunta `isHidden` y no `isVisible`: la ventana de esta prueba no se
        # enseña —no hace falta para medir la lógica— y con una ventana oculta todo
        # es «no visible», incluso lo que sí se vería. `isHidden` distingue lo que
        # alguien escondió a propósito.
        assert pagina._max_btn.isHidden() is False
        assert pagina._max_btn.isEnabled() is True


async def test_max_escribe_lo_que_cabe_y_dice_lo_que_no() -> None:
    """El campo tiene seis decimales y el saldo puede tener dieciocho.

    Se escribe **menos** de lo que hay —nunca más, que es la dirección segura del
    redondeo— y se dice cuánto queda fuera: un «Máx» que deja una milmillonésima sin
    poner y no lo dice es un botón que miente sobre lo que hace.
    """
    async with _tarjeta({"polygon": (_nativo("polygon", "19.49166614"),)}) as (_c, pagina):
        _elegir_red(pagina, "polygon")
        _elegir(pagina._base, "polygon", "POL")
        await _esperar(lambda: "Disponible" in pagina._balance.text())

        pagina._on_max()

        assert pagina._amount.value() == pytest.approx(19.491666, abs=1e-9)
        assert "no cabe el resto" in pagina._status.text()


async def test_una_red_que_no_se_leyó_no_se_pinta_como_cero() -> None:
    """«No se pudo leer» y «cero» no pueden escribir lo mismo.

    Un fallo de red pintado como un cero lleva a no operar sin motivo, y un cero
    pintado como un fallo lleva a buscar un problema que no existe.
    """
    async with _tarjeta({"polygon": (_nativo("polygon", "5"),)}) as (_c, pagina):
        _elegir_red(pagina, "polygon")
        # Base no se lee en esta prueba: el doble devuelve una lista vacía.
        _elegir_red(pagina, "base")
        await _esperar(lambda: pagina._balance.text() != "Leyendo el saldo…")

        assert "0" not in pagina._balance.text(), "no se conocía el saldo, no era cero"


# --------------------------------------------------------------------------- #
# 3. Las dos patas
# --------------------------------------------------------------------------- #
async def test_invertir_cambia_las_dos_patas_y_el_par() -> None:
    """Invertir conserva lo elegido en cada pata y lo cruza.

    La cantidad se escribe siempre en la unidad de lo que **se entrega**, así que
    invertir sin cambiar la etiqueta firmaría el importe al revés.
    """
    async with _tarjeta() as (_c, pagina):
        _elegir_red(pagina, "base")
        _elegir(pagina._base, "base", "WETH")
        _elegir(pagina._contra, "base", "USDC")
        assert pagina._pair.text() == "WETH → USDC"
        assert pagina._unit.text() == "WETH"

        pagina._on_invert()

        assert pagina._pair.text() == "USDC → WETH"
        assert pagina._unit.text() == "USDC"


async def test_el_boton_de_invertir_se_lee_sin_depender_de_la_fuente() -> None:
    """Una palabra y no el glifo suelto, que sale diminuto en casi todas las fuentes."""
    async with _tarjeta() as (_c, pagina):
        assert pagina._invert_btn.text().isalpha()
        assert pagina._invert_btn.toolTip()


# --------------------------------------------------------------------------- #
# 4. Que el botón diga por qué está apagado
# --------------------------------------------------------------------------- #
async def test_la_pantalla_dice_por_que_el_boton_esta_apagado() -> None:
    """Un botón apagado sin motivo es lo que empuja a buscar cómo saltárselo.

    En OBSERVACIÓN no se puede emitir, y eso tiene que verse **antes** de intentarlo
    y no al pulsar. El motivo sale de `execution_gate`, que son las mismas
    comprobaciones que correrán al firmar.

    Se comprueba **con una ruta ya elegida** y no recién abierta la pantalla: el
    motivo se enseña sólo cuando hay una operación concreta sobre la que decidir.
    Antes de cotizar, el botón apagado no dice nada que el usuario no sepa ya, y un
    aviso permanente en rojo se aprende a ignorar —que es exactamente lo contrario
    de lo que se busca—.
    """
    async with _tarjeta({"base": (_nativo("base", "1"),)}) as (_c, pagina):
        motivos = pagina._execution_blockers()
        assert motivos, "en OBSERVACIÓN tiene que haber al menos un motivo"
        assert any("OBSERVACIÓN" in m for m in motivos)

        pagina._comparison = _comparacion()
        pagina._fill_table(pagina._comparison)
        pagina._table.selectRow(0)

        assert pagina._exec_btn.isEnabled() is False
        nota = pagina._exec_note.text()
        assert nota.startswith("No se puede ejecutar: ")
        assert "OBSERVACIÓN" in nota

        # Y el atajo para subir de modo se ofrece **sólo** cuando el modo es lo que
        # bloquea: con otro motivo de por medio no desbloquearía nada.
        assert pagina._upgrade_btn.isHidden() is False
        assert "EJECUCIÓN" in pagina._upgrade_btn.text()


async def test_subir_de_modo_no_enciende_el_boton_por_si_solo() -> None:
    """Cambiar de modo quita un motivo, no todos: el interruptor sigue mandando.

    Es lo que evita el malentendido de creer que subir de modo basta para operar.
    """
    async with _tarjeta({"polygon": (_nativo("polygon", "1"),)}) as (_c, pagina):
        pagina._on_upgrade_mode()
        motivos = pagina._execution_blockers()
        assert any("ejecución está apagada" in m for m in motivos)


# --------------------------------------------------------------------------- #
# 5. La cabecera: quién firma y en qué red
# --------------------------------------------------------------------------- #
async def test_la_cabecera_ensena_la_cartera_y_la_red() -> None:
    """Una operación sin red es una operación en la red equivocada.

    Por eso la cuenta y la red van arriba del todo, antes de cualquier importe.
    """
    async with _tarjeta({"polygon": (_nativo("polygon", "1"),)}) as (_c, pagina):
        card = pagina._card
        # La dirección, acortada en el chip y entera en el tooltip. **Nunca** la
        # clave: lo que sale de aquí es la dirección, que es pública.
        assert card._account.text() == "0xf39F…2266"
        assert DIRECCION_DE_DESARROLLO in card._account.toolTip()
        assert CLAVE_DE_DESARROLLO not in card._account.toolTip()

        _elegir_red(pagina, "polygon")
        assert pagina._chain.currentData() == "polygon"
        assert card._mode_chip.text() == "MODO OBSERVACIÓN"


async def test_sin_cartera_la_cabecera_lo_dice() -> None:
    async with _tarjeta(con_cartera=False) as (_c, pagina):
        assert pagina._card._account.text() == "sin cartera"


# --------------------------------------------------------------------------- #
# 6. Enviar: el destino se escribe en la tarjeta
# --------------------------------------------------------------------------- #
async def test_el_destino_de_un_envio_se_ofrece_y_se_puede_escribir() -> None:
    """Sustituye al diálogo modal que pedía la dirección después de elegir la ruta.

    Se ofrecen las direcciones conocidas —la propia por delante— y el campo sigue
    siendo editable, porque la lista es un atajo y no un límite.
    """
    async with _tarjeta({"base": (_nativo("base", "1.5"),)}) as (_c, pagina):
        card = pagina._card
        _elegir_red(pagina, "base")
        _elegir_red(pagina, "polygon")
        card.set_recipients([DIRECCION_DE_DESARROLLO])

        assert card._recipient.isEditable() is True
        assert card.recipient() == DIRECCION_DE_DESARROLLO

        card._recipient.setEditText("0x000000000000000000000000000000000000dEaD")
        assert card.recipient() == "0x000000000000000000000000000000000000dEaD"


async def test_enviar_sin_destino_no_llama_a_firmar_y_lo_dice() -> None:
    """El caso que más caro sale: firmar una transferencia sin saber a dónde.

    No se llega al caso de uso: el propio formulario dice qué falta. Sin destino no
    hay transferencia, y una dirección vacía no es una dirección.
    """
    async with _tarjeta({"polygon": (_nativo("polygon", "1"),)}) as (_c, pagina):
        card = pagina._card
        card.set_segment(TAB_SEND)
        _elegir_red(pagina, "polygon")
        card._recipient.setEditText("")

        pagina._on_send()

        assert "destino" in card.send_status_text().lower()
