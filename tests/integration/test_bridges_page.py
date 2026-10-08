"""La tarjeta «Entre redes»: qué se enseña, y qué se dice antes de firmar.

Esta prueba construye la sección **de verdad** —contenedor de verdad, motores de
verdad, widgets de Qt de verdad— y no dobla la interfaz, por la misma razón que
`test_prices_page.py`, más una que es propia de un puente: aquí hay **dos** redes,
y la de destino se comprueba en un sitio distinto de la de origen. Una prueba que
doblará la vista podría pasar con el botón diciendo que sí mientras el caso de uso
diría que no, y éste es el botón que firma.

Los motores que se activan son los de verdad (`lifi`), sin clave y sin red: lo que
se comprueba aquí es qué decide la pantalla con la configuración que hay, no lo que
responde un proveedor. Ninguna prueba de este fichero firma ni emite nada, y
ninguna sale a la red.

Qt necesita un `QApplication` vivo, y en una máquina sin pantalla —CI, un
servidor— eso se resuelve con la plataforma `offscreen`, que se fija aquí antes de
que se importe nada de Qt.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox

from amigocompora.app.container import Container, build_container
from amigocompora.domain.models import (
    BridgeComparison,
    BridgeQuote,
    BridgeRequest,
    Token,
    rank_bridges,
)
from amigocompora.domain.modes import OperationMode
from amigocompora.engines.catalog import quote_token
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    app_secret_key,
)
from amigocompora.ui.pages.bridges import BridgesSection

#: Clave de desarrollo **publicada** (la primera de Hardhat). No es un secreto:
#: que sea conocida es justo lo que la hace útil aquí.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

#: El camino que se pidió y el que la tarjeta trae puesto al abrir.
ORIGEN = "base"
DESTINO = "polygon"

#: Con esto el cruce queda listo salvo por lo que cada prueba quite.
_TERMINOS: dict[str, object] = {
    "enabled": True,
    "allowed_chains": [ORIGEN, DESTINO],
    "allowed_tokens": ["USDC"],
    "allowed_engines": ["lifi"],
}


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _store(*, con_cartera: bool = True) -> InMemorySecretStore:
    store = InMemorySecretStore()
    if con_cartera:
        store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    return store


@asynccontextmanager
async def _seccion(
    *,
    modo: OperationMode = OperationMode.EXECUTION,
    con_cartera: bool = True,
    con_motor: bool = True,
    cadenas: Iterable[str] | None = None,
    tokens: Iterable[str] | None = None,
) -> AsyncIterator[tuple[Container, BridgesSection]]:
    """La tarjeta montada sobre un contenedor de verdad.

    `con_motor` enciende o apaga la ranura de puentes: sin ella —que es como viene
    la configuración por defecto, porque `DEFAULT_ENGINE_IDS` no tiene puente— el
    motivo es otro, y esa diferencia es la que la prueba tiene que poder ver.
    """
    terminos = dict(_TERMINOS)
    if cadenas is not None:
        terminos["allowed_chains"] = list(cadenas)
    if tokens is not None:
        terminos["allowed_tokens"] = list(tokens)
    config: dict[str, object] = {"mode": modo.value, "execution": terminos}
    if con_motor:
        config["active_engines"] = {"cross_chain": "lifi"}
    container = await build_container(
        Settings.model_validate(config),
        secret_store=_store(con_cartera=con_cartera),
        configure_logs=False,
    )
    try:
        yield container, BridgesSection(container)
    finally:
        await container.aclose()


def _elegir(combo: QComboBox, chain_key: str, symbol: str) -> None:
    """Deja elegido, en ese desplegable, el token de esa red con ese símbolo.

    Por símbolo y comprobando la red, no por posición: las posiciones dependen del
    catálogo y de lo que el usuario haya guardado, así que una prueba que eligiera
    «la segunda» estaría afirmando sobre la lista de tokens en vez de sobre lo que
    se está midiendo.
    """
    for position in range(combo.count()):
        token = combo.itemData(position)
        if isinstance(token, Token) and token.chain == chain_key and token.symbol == symbol:
            combo.setCurrentIndex(position)
            return
    raise AssertionError(f"«{symbol}» de «{chain_key}» no está en el desplegable")


def _comparacion(request: BridgeRequest) -> BridgeComparison:
    """Una comparación de una sola ruta, para poder seleccionarla en la tabla."""
    destino = request.destination
    return BridgeComparison(
        request=request,
        routes=rank_bridges(
            (
                BridgeQuote(
                    engine_id="lifi",
                    provider="AcrossV4",
                    request=request,
                    amount_out=destino.amount("0.99"),
                    amount_out_min=destino.amount("0.98"),
                    duration_seconds=90,
                    observed_at=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
                    source_note="Medido contra la API de LI.FI.",
                ),
            )
        ),
    )


# --------------------------------------------------------------------------- #
# La tarjeta se monta
# --------------------------------------------------------------------------- #
async def test_la_tarjeta_abre_en_el_camino_que_se_pidio() -> None:
    """Base → Polygon, y con tokens elegidos en las dos puntas.

    Se comprueba sobre los desplegables y no sobre las constantes: lo que importa
    es que arranque con dos tokens de verdad seleccionados, porque con un
    desplegable vacío la petición no se puede ni construir.
    """
    async with _seccion() as (_, seccion):
        assert seccion._chain_key(seccion._origin_chain) == ORIGEN
        assert seccion._chain_key(seccion._destination_chain) == DESTINO
        origen, destino = seccion._tokens()
        assert origen is not None
        assert destino is not None
        assert (origen.chain, destino.chain) == (ORIGEN, DESTINO)


async def test_la_peticion_la_arma_la_pantalla_con_lo_que_dice_la_pantalla() -> None:
    """La petición es la que el usuario escribió, no una reconstrucción parecida."""
    async with _seccion() as (_, seccion):
        _elegir(seccion._origin_token, ORIGEN, "USDC")
        _elegir(seccion._destination_token, DESTINO, "USDC")
        seccion._amount.setValue(0.5)

        request, motivo = seccion._request()

        assert motivo == ""
        assert request is not None
        assert request.chains == (ORIGEN, DESTINO)
        referencia = quote_token(ORIGEN)
        assert referencia is not None
        assert request.amount_in == referencia.amount("0.5")


async def test_las_dos_patas_en_la_misma_red_se_rechazan_como_lo_que_son() -> None:
    """Eso es un swap, y decirlo evita que alguien espere un puente que no cruza."""
    async with _seccion() as (_, seccion):
        seccion._destination_chain.setCurrentIndex(
            _indice_de_red(seccion._destination_chain, ORIGEN)
        )

        request, motivo = seccion._request()

        assert request is None
        assert "swap" in motivo


def _indice_de_red(combo: QComboBox, chain_key: str) -> int:
    for position in range(combo.count()):
        if combo.itemData(position) == chain_key:
            return position
    raise AssertionError(f"«{chain_key}» no está en el desplegable de redes")


# --------------------------------------------------------------------------- #
# Los motivos, que son el botón
# --------------------------------------------------------------------------- #
async def test_sin_motor_de_puentes_se_dice_cual_activar() -> None:
    """La ranura de puentes viene vacía por defecto: es el primer motivo real."""
    async with _seccion(con_motor=False) as (_, seccion):
        motivos = seccion._blockers(None)

        assert any("lifi" in motivo and "relay" in motivo for motivo in motivos)


async def test_la_red_de_destino_fuera_de_la_lista_blanca_se_nombra() -> None:
    """El motivo nuevo, y el que más se va a encontrar quien pruebe esto.

    Un puente sale de una red y el dinero aparece en otra: una lista blanca que
    sólo cubriera la de origen dejaría cruzar hacia una red que el usuario no
    autorizó. El texto es el de `ExecutionLimits.check_chain` —el motivo que da la
    comprobación de verdad—, no uno escrito aparte para la pantalla.
    """
    async with _seccion(cadenas=(ORIGEN,)) as (_, seccion):
        _elegir(seccion._origin_token, ORIGEN, "USDC")
        _elegir(seccion._destination_token, DESTINO, "USDC")
        seccion._amount.setValue(0.5)

        motivos = seccion._blockers(None)

        assert len(motivos) == 1
        assert f"«{DESTINO}» no está entre las redes habilitadas ({ORIGEN})" in motivos[0]


async def test_con_las_dos_redes_declaradas_no_falta_nada() -> None:
    """El contrapunto: si la lista de motivos no se vacía nunca, no dice nada."""
    async with _seccion() as (_, seccion):
        _elegir(seccion._origin_token, ORIGEN, "USDC")
        _elegir(seccion._destination_token, DESTINO, "USDC")
        seccion._amount.setValue(0.5)

        assert seccion._blockers(None) == ()


async def test_sin_cartera_no_hay_con_que_firmar_y_se_dice() -> None:
    async with _seccion(con_cartera=False) as (_, seccion):
        _elegir(seccion._origin_token, ORIGEN, "USDC")
        _elegir(seccion._destination_token, DESTINO, "USDC")
        seccion._amount.setValue(0.5)

        motivos = seccion._blockers(None)

        assert len(motivos) == 1
        assert "cartera" in motivos[0]


async def test_fuera_de_ejecucion_el_motivo_es_el_modo_y_no_otra_cosa() -> None:
    """Con el modo apagado lo demás da igual, y decir todo a la vez no ordena nada."""
    async with _seccion(modo=OperationMode.ASSISTED) as (_, seccion):
        motivos = seccion._blockers(None)

        assert any("no permite emitir" in motivo for motivo in motivos)


# --------------------------------------------------------------------------- #
# La tabla y el botón
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("cadenas", "se_puede"),
    [
        # La red de destino no está declarada: es lo que se viene a comprobar.
        ((ORIGEN,), False),
        # Las dos declaradas: el botón que firma se enciende.
        ((ORIGEN, DESTINO), True),
    ],
)
async def test_el_boton_de_firmar_sigue_a_la_red_de_destino(
    cadenas: tuple[str, ...], se_puede: bool
) -> None:
    """Se elige una fila de verdad en la tabla en vez de llamar al método a mano.

    Lo que se comprueba es la cadena «seleccionar → repintar → habilitar», y
    llamar al método se saltaría justo la parte que puede fallar: que la fila
    seleccionada sea la que se ejecuta.
    """
    async with _seccion(cadenas=cadenas) as (_, seccion):
        _elegir(seccion._origin_token, ORIGEN, "USDC")
        _elegir(seccion._destination_token, DESTINO, "USDC")
        seccion._amount.setValue(0.5)
        request, _ = seccion._request()
        assert request is not None

        seccion._comparison = _comparacion(request)
        seccion._fill_table(seccion._comparison)
        seccion._table.selectRow(0)

        assert seccion._selected_quote() is not None
        assert seccion._exec_btn.isEnabled() is se_puede
        if se_puede:
            assert seccion._exec_note.text() == ""
            assert seccion._out_estimate.text().startswith("≈")
        else:
            # El aviso va escrito con el motivo completo: un botón apagado sin
            # explicación es lo que empuja a buscar la forma de saltárselo.
            assert DESTINO in seccion._exec_note.text()


async def test_la_mejor_ruta_es_la_primera_fila_y_la_que_se_ejecuta() -> None:
    """El orden lo pone el caso de uso; la pantalla sólo lo pinta.

    Si la tabla ordenara por su cuenta, la ruta que se enseña como mejor podría no
    ser la que se firma — y en un puente eso es la diferencia entre llegar con lo
    prometido y llegar con menos.
    """
    async with _seccion() as (_, seccion):
        _elegir(seccion._origin_token, ORIGEN, "USDC")
        _elegir(seccion._destination_token, DESTINO, "USDC")
        request, _ = seccion._request()
        assert request is not None
        destino = request.destination
        comp = BridgeComparison(
            request=request,
            routes=rank_bridges(
                (
                    BridgeQuote(
                        engine_id="lifi",
                        provider="Paga menos",
                        request=request,
                        amount_out=destino.amount("0.97"),
                        amount_out_min=destino.amount("0.96"),
                        observed_at=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
                    ),
                    BridgeQuote(
                        engine_id="relay",
                        provider="Paga más",
                        request=request,
                        amount_out=destino.amount("0.99"),
                        amount_out_min=destino.amount("0.98"),
                        observed_at=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
                    ),
                )
            ),
        )

        seccion._comparison = comp
        seccion._fill_table(comp)
        seccion._table.selectRow(0)

        proveedor = seccion._table.item(0, 1)
        assert proveedor is not None
        assert proveedor.text() == "Paga más"
        elegida = seccion._selected_quote()
        assert elegida is not None
        assert elegida.provider == "Paga más"


# --------------------------------------------------------------------------- #
# El token guardado por su contrato
# --------------------------------------------------------------------------- #
async def test_un_token_guardado_por_su_contrato_sale_en_el_desplegable() -> None:
    """«Lo pego y aparece en la lista»: también en la tarjeta de puentes.

    Se guarda en el almacén —que es lo que sobrevive al cierre— y se monta la
    tarjeta **después**, que es el orden real: primero se añade el token, y la
    próxima vez que se abre la aplicación ya está en la lista. Un token que sólo
    viviera en el widget que lo pidió no pasaría esta prueba.
    """
    añadido = Token("PEPE", 18, ORIGEN, "0x" + "ab" * 20)
    async with _seccion() as (container, seccion):
        combo = seccion._origin_token
        etiquetas = [combo.itemText(i) for i in range(combo.count())]
        assert "PEPE" not in etiquetas

        assert container.token_store.add(añadido) is True
        otra = BridgesSection(container)
        combo = otra._origin_token
        guardados = [
            combo.itemData(i) for i in range(combo.count())
        ]
        assert any(
            isinstance(token, Token) and token.address == añadido.address
            for token in guardados
        )


async def test_un_token_guardado_en_otra_red_no_se_cuela_en_este_desplegable() -> None:
    """El almacén es de todas las redes, así que el filtro es lo que lo parte."""
    de_polygon = Token("PEPE", 18, DESTINO, "0x" + "cd" * 20)
    async with _seccion() as (container, _):
        container.token_store.add(de_polygon)
        otra = BridgesSection(container)

        etiquetas = [
            otra._origin_token.itemText(i) for i in range(otra._origin_token.count())
        ]
        assert "PEPE" not in etiquetas


async def test_un_motor_activo_que_no_cruza_desde_esa_red_no_manda_a_activar_otro() -> None:
    """`lifi` está activo y aun así no cruza desde cualquier red.

    El motivo de antes era el mismo en los dos casos —«activa `lifi` o `relay`»—,
    y con `lifi` ya activo eso manda al usuario a un panel donde no hay nada que
    activar: el motor está, lo que no cubre es esa red. La salida es cambiar el
    origen, así que el motivo tiene que decir cuál de las dos cosas pasa.
    """
    # `avalanche` no está entre las redes que este motor sabe cruzar.
    async with _seccion(cadenas=("avalanche", DESTINO)) as (_, seccion):
        seccion._origin_chain.setCurrentIndex(
            _indice_de_red(seccion._origin_chain, "avalanche")
        )

        motivos = seccion._blockers(None)

        planificador = [motivo for motivo in motivos if "no cruzan" in motivo]
        assert len(planificador) == 1, "y es un motivo, no dos"
        assert "lifi" in planificador[0], "se nombra el motor que sí está activo"
        assert "avalanche" in planificador[0]
        assert "activa `lifi`" not in planificador[0], (
            "no se puede pedir que se active lo que ya está activo"
        )


async def test_sin_ningun_motor_activo_si_se_dice_cual_activar() -> None:
    """El contrapunto: con la ranura vacía, ofrecer `lifi` y `relay` es correcto."""
    async with _seccion(con_motor=False) as (_, seccion):
        motivos = seccion._blockers(None)

        assert any("ningún motor de puentes activo" in motivo for motivo in motivos)
        assert any("activa `lifi` o `relay`" in motivo for motivo in motivos)
