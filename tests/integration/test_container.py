"""Integración: el contenedor completo se construye y cablea sin red."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from amigocompora.app.confirmation import Decision
from amigocompora.app.container import build_container
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
    ExecutionError,
    ModeNotPermittedError,
)
from amigocompora.domain.execution import TriggerKind
from amigocompora.domain.models import Quote, TradingPair, Venue, VenueKind
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import BasisPoints
from amigocompora.domain.protocols import EngineKind
from amigocompora.engines.catalog import quote_token, wrapped_native
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    AUTONOMY_PASSPHRASE_SECRET,
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    app_env_var_name,
    app_secret_key,
)

#: Clave de desarrollo **publicada** (la primera de Hardhat). No es un secreto y
#: su dirección es conocida de antemano, que es justo lo que hace falta para
#: comprobar que la derivación llega bien desde la configuración hasta el final.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION_ESPERADA = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

#: Frase de autonomía de la prueba. No protege nada: lo que se comprueba es que
#: armarla sea lo único que abre la puerta, y que una distinta no la abra.
FRASE_DE_AUTONOMIA = "frase-de-la-prueba"


class _SpyStore:
    """Almacén que anota **qué** credencial se pidió.

    Se anota el nombre y no un contador porque lo que hay que poder afirmar es
    algo más fuerte que «no se leyó nada»: que en concreto la clave privada no se
    pidió. Un contador pasaría aunque el código hubiera leído justo la credencial
    que no debía, siempre que lo hiciera el mismo número de veces.
    """

    def __init__(self) -> None:
        self._inner = InMemorySecretStore()
        self.reads: list[str] = []

    def get(self, key: str) -> str | None:
        self.reads.append(key)
        return self._inner.get(key)

    def set(self, key: str, value: str) -> None:
        self._inner.set(key, value)

    def delete(self, key: str) -> None:
        self._inner.delete(key)


def _eth_pair() -> TradingPair:
    base = wrapped_native("ethereum")
    quote = quote_token("ethereum")
    assert base is not None
    assert quote is not None
    return TradingPair(base=base, quote=quote)


def _quote(pair: TradingPair) -> Quote:
    return Quote(
        venue=Venue(
            venue_id="fake@ethereum",
            name="fake",
            kind=VenueKind.DEX,
            chain="ethereum",
        ),
        engine_id="fake",
        pair=pair,
        amount_in=pair.base.amount("1"),
        amount_out=pair.quote.amount("2000"),
        fee_bps=BasisPoints(30),
        price_impact_bps=BasisPoints(5),
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_container_builds_with_all_slots() -> None:
    container = await build_container(
        Settings(),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        # Las tres ranuras quedan con un motor activo por defecto.
        assert container.registry.active_manifest(EngineKind.DEX_QUOTES) is not None
        assert container.registry.active_manifest(EngineKind.PREDICTION_MARKETS) is not None
        advisor = container.registry.active_manifest(EngineKind.AI_ADVISOR)
        assert advisor is not None
        # El asistente por defecto es el offline.
        assert advisor.engine_id == "stub_advisor"
    finally:
        await container.aclose()


async def test_observation_mode_blocks_route_compute() -> None:
    container = await build_container(
        Settings(mode=OperationMode.OBSERVATION),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        pair = _eth_pair()
        with pytest.raises(ModeNotPermittedError):
            await container.scan_opportunities(pair, pair.base.amount(1))
    finally:
        await container.aclose()


async def test_alert_center_and_scheduler_present() -> None:
    container = await build_container(
        Settings(),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        assert container.alert_center.unacknowledged_count == 0
        assert not container.scheduler.is_running
        assert Capability.QUERY_AI in container.guard.mode.capabilities
    finally:
        await container.aclose()


async def test_a_missing_rpc_secret_does_not_stop_the_application(
    tmp_path: Path,
) -> None:
    """Regresión: un `${MARCADOR}` sin resolver no puede impedir abrir la aplicación.

    Se midió. El `config.toml` de esta máquina declara Infura con
    `${INFURA_API_KEY}`; en cuanto la clave no estaba disponible, construir el
    contenedor lanzaba `MissingSecretError` y la aplicación **no arrancaba**. El
    precio es desproporcionado: los nodos de una red son intercambiables por
    diseño —esa es la razón de existir de la lista de respaldos—, así que perder
    el nodo propio deja la red con menos cuota, no deja al usuario sin programa.

    Lo que **sí** tiene que pasar es que se note: el endpoint se descarta con un
    aviso que nombra el secreto y dónde configurarlo.
    """
    from structlog.testing import capture_logs

    config = tmp_path / "config"
    config.mkdir()
    (config / "config.toml").write_text(
        "[[chains]]\n"
        'chain = "base"\n'
        "include_public_fallbacks = true\n"
        "[[chains.endpoints]]\n"
        'url = "https://base-mainnet.infura.io/v3/${CLAVE_QUE_NO_ESTA}"\n'
        'label = "infura"\n'
        "priority = 10\n"
    )

    # El almacén está vacío y el conftest retira las variables del entorno, así
    # que el marcador no se puede resolver por ninguna de las dos vías.
    with capture_logs() as capturado:
        container = await build_container(
            Settings(),
            secret_store=InMemorySecretStore(),
            configure_logs=False,
        )
    try:
        pool = container.rpc.pool("base")
        etiquetas = {endpoint.label for endpoint in pool._endpoints}
        assert "infura" not in etiquetas
        # La red sigue siendo utilizable por los respaldos públicos medidos.
        assert etiquetas
    finally:
        await container.aclose()

    avisos = [
        entrada for entrada in capturado if entrada["event"] == "rpc.endpoint_without_secret"
    ]
    assert len(avisos) == 1
    assert avisos[0]["endpoint"] == "infura"
    assert "CLAVE_QUE_NO_ESTA" in avisos[0]["reason"]


# --------------------------------------------------------------------------- #
# El cableado de la ejecución
# --------------------------------------------------------------------------- #
async def test_the_container_wires_signing_and_broadcasting() -> None:
    """Las tres piezas existen y llegan **apagadas**.

    Se construyen aunque no haya nada configurado, y el motivo está escrito en
    `_build_execution`: son también la explicación que la interfaz tiene que dar.
    Lo que se fija aquí es lo otro, que es lo que puede pudrirse en silencio —
    que el valor por omisión siga siendo «apagado» en el objeto que de verdad
    decide, y no sólo en el fichero de configuración.
    """
    container = await build_container(
        Settings(),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        assert container.execute_swap is not None
        # Sin clave configurada no hay cartera, y eso no puede lanzar: la
        # interfaz pregunta esto para pintar la pantalla.
        assert container.keys.available() is False
        assert container.keys.address() is None
        # Los topes llegan con el interruptor maestro apagado.
        assert container.policy.limits.enabled is False
        assert container.policy.trigger is TriggerKind.MANUAL
        assert container.policy.ledger.entries() == ()
        assert container.policy.armed is False
    finally:
        await container.aclose()


@pytest.mark.parametrize("permitido", [True, False])
async def test_the_env_fallback_flag_reaches_the_provider(
    permitido: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`allow_env_key` viaja de la configuración al proveedor, de punta a punta.

    La prueba unitaria de `test_secrets.py` fija que el proveedor **obedece** la
    bandera; ésta fija que la bandera **llega**. Son dos afirmaciones distintas y
    la segunda es la que se rompe al tocar `container.py`: un `allow_env_fallback`
    que se quede en su valor por omisión deja al usuario con una clave que
    escribió, que la aplicación no ve, y ningún mensaje que relacione las dos
    cosas.

    Se afirma sobre la **dirección derivada** y no sobre la clave: es lo único
    que sale del proveedor sin ser el secreto, y una clave de desarrollo
    publicada tiene una dirección conocida de antemano, así que el valor esperado
    no lo calcula el código que se está probando.
    """
    monkeypatch.setenv(app_env_var_name(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)

    container = await build_container(
        Settings.model_validate({"execution": {"allow_env_key": permitido}}),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        assert container.keys.address() == (DIRECCION_ESPERADA if permitido else None)
        assert container.keys.available() is permitido
    finally:
        await container.aclose()


async def test_the_autonomy_bypass_is_wired_but_only_skips_when_armed() -> None:
    """Conectar el bypass no es aprobar nada: lo que aprueba es **armarlo**.

    `gateway.set_bypass` se llama siempre, sin condición, y eso sólo es seguro
    porque `PolicyBypass.reason_to_skip` devuelve `None` salvo que se trate de
    emitir **y** la autonomía esté armada. Esta prueba es la que sostiene esa
    afirmación: mismo contenedor, mismo modo, y la única diferencia entre el
    «no» y el «sí» es la frase de autonomía.

    Se ejerce sobre la puerta de confirmación y no sobre `execute_swap` completo
    a propósito: pasar de ahí exige red, y lo que se quiere aislar aquí es quién
    da el permiso.
    """
    store = InMemorySecretStore()
    store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    store.set(app_secret_key(AUTONOMY_PASSPHRASE_SECRET), FRASE_DE_AUTONOMIA)

    container = await build_container(
        Settings(mode=OperationMode.EXECUTION),
        secret_store=store,
        configure_logs=False,
    )
    try:
        # El modo concede emitir y hay cartera y frase: se puede armar.
        assert container.policy.can_arm is True

        # Sin armar, la única vía de aprobar es una persona, y aquí no hay
        # ninguna: el diálogo por omisión del contenedor es `DenyAllPrompt`.
        # Que esto lance **es** la afirmación de que no está armada — afirmar
        # además la bandera sería afirmar la implementación y no el efecto.
        with pytest.raises(ConfirmationDeniedError):
            await container.gateway.authorize(
                Capability.BROADCAST_TX, "Firmar y emitir"
            )

        container.policy.arm(FRASE_DE_AUTONOMIA)
        assert container.policy.armed is True

        # Armada, la política responde por la puerta y no se pregunta.
        action = await container.gateway.authorize(
            Capability.BROADCAST_TX, "Firmar y emitir"
        )
        assert action.capability is Capability.BROADCAST_TX

        # Y el permiso queda contado como lo que fue, no como un «sí» humano.
        assert Decision.APPROVED_BY_POLICY in {
            registro.decision for registro in container.gateway.history
        }
    finally:
        await container.aclose()


async def test_a_wrong_passphrase_does_not_arm_anything() -> None:
    """La frase es la que declara que esto se hace a propósito.

    Sin esta comprobación, `set_bypass` incondicional sería un agujero: bastaría
    con llamar a `arm` con cualquier cosa para que la aplicación dejara de
    preguntar. El guardián es `hmac.compare_digest`, y se prueba por el borde:
    una frase distinta no arma, y la puerta sigue cerrada.
    """
    store = InMemorySecretStore()
    store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    store.set(app_secret_key(AUTONOMY_PASSPHRASE_SECRET), FRASE_DE_AUTONOMIA)

    container = await build_container(
        Settings(mode=OperationMode.EXECUTION),
        secret_store=store,
        configure_logs=False,
    )
    try:
        with pytest.raises(ExecutionError):
            container.policy.arm("no-es-la-frase")
        # Y la puerta sigue cerrada: es lo que de verdad se está afirmando.
        with pytest.raises(ConfirmationDeniedError):
            await container.gateway.authorize(Capability.BROADCAST_TX, "Firmar y emitir")
    finally:
        await container.aclose()


@pytest.mark.parametrize(
    "mode", [OperationMode.SIMULATION, OperationMode.ASSISTED]
)
async def test_below_execution_mode_the_key_is_never_even_looked_up(
    mode: OperationMode,
) -> None:
    """La barrera corta **antes** de que exista nada que firmar.

    La prueba no afirma sólo que lance: afirma **dónde** lanza. Un modo que no
    concede emitir tiene que detener la operación antes de pedir la clave al
    almacén, porque una clave pedida es una clave que estuvo en memoria y en un
    `repr()` a un fallo de distancia. Por eso el almacén es un espía y se afirma
    sobre los nombres leídos y no sobre un contador.

    Y se prueban los dos modos que quedan por debajo: `SIMULATION` ni siquiera
    puede construir el payload, y `ASSISTED` sí, pero no puede emitirlo. Los dos
    se paran en la misma puerta y por motivos distintos, que es justo lo que la
    tabla de capacidades promete.
    """
    store = _SpyStore()
    store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)

    container = await build_container(
        Settings(mode=mode),
        secret_store=store,
        configure_logs=False,
    )
    try:
        marca = len(store.reads)
        with pytest.raises(ModeNotPermittedError):
            await container.execute_swap(
                _quote(_eth_pair()), recipient=DIRECCION_ESPERADA
            )
        assert app_secret_key(PRIVATE_KEY_SECRET) not in store.reads[marca:]
    finally:
        await container.aclose()


# --------------------------------------------------------------------------- #
# Varios motores en una misma ranura
# --------------------------------------------------------------------------- #
async def test_a_slot_can_hold_several_engines_without_evicting_any() -> None:
    """Elegir V4 no puede llevarse por delante los precios de V3.

    Es la razón de que una ranura admita una lista y no un solo nombre. La
    operación que usa el panel de motores —`activate`— deja un motor **solo**,
    así que apilar con ella borraría al anterior; para sumar hay que usar `add`,
    y la diferencia no se ve hasta que alguien escribe dos nombres y se queda
    con la mitad sin saberlo.

    V3 y V4 son pools distintos con precios distintos, así que verlos juntos es
    el producto, no un lujo: quien compara precios quiere los dos.
    """
    container = await build_container(
        Settings(active_engines={"dex_quotes": ("uniswap_v3", "uniswap_v4")}),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        pila = container.registry.active_stack(EngineKind.DEX_QUOTES)
        assert [engine.manifest.engine_id for engine in pila] == ["uniswap_v3", "uniswap_v4"]
    finally:
        await container.aclose()


async def test_both_direct_engines_can_plan_a_swap_on_the_same_chain() -> None:
    """La pila tiene que llegar hasta quien construye la transacción, no sólo a la vista.

    Que los dos estén activos no basta: `prepare_swap` resuelve el motor por el
    `engine_id` de la cotización, así que un motor activo cuyas cadenas no
    incluyan la red no podría construir nada aunque apareciera en la lista.

    Y que devuelva **dos** es lo que hace que una fuente caída no detenga la
    operación: quien llama los recorre y pasa al siguiente.
    """
    container = await build_container(
        Settings(active_engines={"dex_quotes": ("uniswap_v3", "uniswap_v4")}),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        planificadores = container.registry.planners_for("base")
        assert [engine.manifest.engine_id for engine in planificadores] == [
            "uniswap_v3",
            "uniswap_v4",
        ]
    finally:
        await container.aclose()


async def test_a_typo_late_in_the_list_does_not_take_down_the_engine_before_it() -> None:
    """Un nombre mal escrito se anota y se sigue, no se lleva por delante la ranura.

    Se activa por motor y se registra por motor justamente por esto: si la lista
    se tratara como una sola operación, un error al final dejaría la ranura como
    estuviera —o peor, a medias— y el aviso hablaría de la lista entera en vez
    del nombre que sobra.
    """
    container = await build_container(
        Settings(active_engines={"dex_quotes": ("uniswap_v3", "no_existe_este_motor")}),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        pila = container.registry.active_stack(EngineKind.DEX_QUOTES)
        assert [engine.manifest.engine_id for engine in pila] == ["uniswap_v3"]
    finally:
        await container.aclose()
