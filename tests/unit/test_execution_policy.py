"""Cuándo se ejecuta sin preguntar, y por qué la barrera no se puede saltar.

Tres cosas se comprueban aquí, en orden de importancia:

1. **Que el bypass no pueda saltarse el modo.** Es la propiedad que sostiene
   todo lo demás: un bypass que concediera una capacidad convertiría la elección
   del usuario sobre en qué modo está la aplicación en un adorno. La prueba no
   se conforma con ver el error —comprueba además que **no se llegó a
   preguntar**—, y ese par de afirmaciones es lo que distingue esta prueba de
   una que sólo mira que algo falle.
2. **Que la autonomía se desarme sola** cuando desaparece lo que la hace
   posible. Una bandera que quedó puesta seguiría diciendo que sí después de que
   la clave desaparezca, y esto gasta dinero sin preguntar.
3. **Que los límites se apliquen en el dominio.** Un límite que sólo comprueba
   la vista es una advertencia que cualquiera puede saltarse llamando al caso de
   uso.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from amigocompora.app.confirmation import (
    ConfirmationGateway,
    Decision,
    PendingAction,
    RecordingPrompt,
)
from amigocompora.app.execution_policy import (
    POLICY_REASON,
    AutonomyPolicy,
    ExecutionLedger,
    LedgerEntry,
    PolicyBypass,
    limits_from_config,
)
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
    ExecutionError,
    ExecutionLimitExceededError,
    ModeNotPermittedError,
)
from amigocompora.domain.execution import ExecutionLimits, TriggerKind
from amigocompora.domain.models import ExecutionIntent, Quote, TradingPair, Venue, VenueKind
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.engines.catalog import quote_token, wrapped_native

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

#: Con forma de clave real, para poder buscarla en las superficies que se
#: imprimen. No es una clave: son 64 doses.
CLAVE = "0x" + "2" * 64
FRASE = "abre-la-caja-fuerte"


# --------------------------------------------------------------------------- #
# Dobles de los dos secretos
# --------------------------------------------------------------------------- #
class FakeKey:
    """Una clave que se puede hacer desaparecer, que es lo que hay que probar."""

    def __init__(self, key: str | None = CLAVE) -> None:
        self.key = key

    def available(self) -> bool:
        return self.key is not None

    def require(self) -> str:
        if self.key is None:
            raise ExecutionError("no hay clave privada")
        return self.key


class FakePassphrase:
    def __init__(self, value: str | None = FRASE) -> None:
        self.value = value

    def get(self) -> str | None:
        return self.value


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _pair(chain_key: str = "base") -> TradingPair:
    base = wrapped_native(chain_key)
    quote = quote_token(chain_key)
    assert base is not None
    assert quote is not None
    return TradingPair(base=base, quote=quote)


def _intent(
    chain_key: str = "base",
    *,
    notional: str = "100",
    engine_id: str = "zeroex",
) -> ExecutionIntent:
    pair = _pair(chain_key)
    return ExecutionIntent(
        quote=Quote(
            venue=Venue(
                venue_id=f"{engine_id}@{chain_key}",
                name=engine_id,
                kind=VenueKind.DEX,
                chain=chain_key,
            ),
            engine_id=engine_id,
            pair=pair,
            amount_in=pair.base.amount("1"),
            amount_out=pair.quote.amount(notional),
            fee_bps=None,
            fee_basis=None,
            price_impact_bps=None,
            impact_basis=None,
            observed_at=NOW,
        ),
        recipient="0x1111111111111111111111111111111111111111",
        notional=pair.quote.amount(notional),
        engine_id=engine_id,
    )


def _limits(**overrides: object) -> ExecutionLimits:
    base: dict[str, object] = {
        # Permiso de operar: el interruptor maestro tiene su propia prueba.
        "enabled": True,
        "max_quote_per_trade": Decimal("5000"),
        "max_quote_per_day": Decimal("20000"),
        "allowed_tokens": frozenset({"WETH", "USDC"}),
        "allowed_chains": frozenset({"base"}),
        "allowed_engines": frozenset({"zeroex"}),
    }
    base.update(overrides)
    return ExecutionLimits(**base)  # type: ignore[arg-type]


def _policy(
    tmp_path: Path,
    *,
    trigger: TriggerKind = TriggerKind.MANUAL,
    limits: ExecutionLimits | None = None,
    keys: FakeKey | None = None,
    passphrase: FakePassphrase | None = None,
    moment: datetime = NOW,
) -> tuple[AutonomyPolicy, FakeKey, FakePassphrase, ExecutionLedger, FrozenClock]:
    clock = FrozenClock(moment)
    ledger = ExecutionLedger(tmp_path / "executions.jsonl", clock=clock)
    effective_keys = keys or FakeKey()
    effective_passphrase = passphrase or FakePassphrase()
    policy = AutonomyPolicy(
        keys=effective_keys,
        passphrase=effective_passphrase,
        limits=limits or _limits(),
        ledger=ledger,
        clock=clock,
        trigger=trigger,
    )
    return policy, effective_keys, effective_passphrase, ledger, clock


def _armed(policy: AutonomyPolicy) -> bool:
    """Lee el estado de la autonomía a través de una llamada.

    Existe porque mypy estrecha una **propiedad** a un valor literal en cuanto
    se afirma sobre ella: tras `assert policy.armed`, todo `assert not
    policy.armed` posterior en la misma función se da por inalcanzable, aunque
    por medio se haya retirado la clave del almacén. Pasar por una llamada
    devuelve un `bool` sin estrechar nada, y deja que la prueba afirme las dos
    cosas —que estaba armada y que dejó de estarlo—, que es justo lo que
    demuestra la propiedad.
    """
    return policy.armed


def _spend(
    ledger: ExecutionLedger, intent: ExecutionIntent, amount: str, *, when: datetime
) -> None:
    """Anota una ejecución ya ocurrida, para poblar la ventana del tope."""
    ledger.append(
        LedgerEntry(
            occurred_at=when,
            chain=intent.quote.pair.chain,
            pair=intent.quote.pair.symbol,
            engine_id=intent.engine_id,
            notional=amount,
            notional_symbol=intent.notional.symbol,
            tx_hash="0x" + "ab" * 32,
            status="success",
            recipient=intent.recipient,
            description="",
        )
    )


# --------------------------------------------------------------------------- #
# Armado y desarmado
# --------------------------------------------------------------------------- #
def test_autonomy_is_not_armed_by_default(tmp_path: Path) -> None:
    """Lo que gasta dinero sin preguntar no puede venir encendido de fábrica."""
    policy, *_ = _policy(tmp_path)
    assert not policy.armed
    assert policy.events == ()
    assert policy.trigger is TriggerKind.MANUAL


def test_arming_requires_the_correct_passphrase(tmp_path: Path) -> None:
    policy, *_ = _policy(tmp_path)
    policy.arm(FRASE)
    assert policy.armed
    assert [(event.armed, event.reason) for event in policy.events] == [
        (True, "armada por el operador")
    ]


def test_a_wrong_passphrase_does_not_arm_and_does_not_echo_the_phrase(tmp_path: Path) -> None:
    """La frase que llegó no se registra: sólo que falló.

    La traza de auditoría se muestra, se copia en informes y se adjunta a los
    fallos. Escribir en ella lo que alguien tecleó convertiría la auditoría en
    un sitio más del que se puede sacar el secreto.
    """
    policy, *_ = _policy(tmp_path)

    with pytest.raises(ExecutionError, match="no es correcta"):
        policy.arm("no-es-esta")

    assert not policy.armed
    assert [event.reason for event in policy.events] == ["frase de autonomía incorrecta"]
    for event in policy.events:
        assert "no-es-esta" not in event.reason


def test_arming_without_a_configured_passphrase_explains_why(tmp_path: Path) -> None:
    """Sin frase no hay nada que declare que esto se hace a propósito."""
    policy, *_ = _policy(tmp_path, passphrase=FakePassphrase(None))
    with pytest.raises(ExecutionError, match="no hay frase de autonomía configurada"):
        policy.arm(FRASE)
    assert not policy.armed


def test_arming_without_a_key_is_refused(tmp_path: Path) -> None:
    """Sin clave no hay nada que firmar y la autonomía no serviría de nada."""
    policy, *_ = _policy(tmp_path, keys=FakeKey(None))
    with pytest.raises(ExecutionError, match="no hay clave privada"):
        policy.arm(FRASE)
    assert not policy.armed


def test_autonomy_disarms_itself_when_the_key_disappears(tmp_path: Path) -> None:
    """La propiedad que justifica reevaluar en cada consulta.

    Armada y funcionando; se retira la clave del almacén. Una bandera que
    quedara puesta seguiría diciendo que sí, y esto es lo que ejecuta sin
    preguntar.
    """
    policy, keys, *_ = _policy(tmp_path)
    policy.arm(FRASE)
    assert _armed(policy)

    keys.key = None
    assert not _armed(policy)
    # Y la consecuencia, que es lo que de verdad importa: con la clave retirada
    # no se puede volver a armar. Se afirma el efecto y no la bandera
    # `can_arm` porque mypy pliega la bandera a un valor literal y la
    # afirmación sobre ella no sería comprobable.
    with pytest.raises(ExecutionError, match="no hay clave privada"):
        policy.arm(FRASE)


def test_autonomy_disarms_itself_when_the_passphrase_disappears(tmp_path: Path) -> None:
    policy, _, passphrase, *_ = _policy(tmp_path)
    policy.arm(FRASE)
    assert _armed(policy)

    passphrase.value = None
    assert not _armed(policy)


def test_a_listener_hears_that_autonomy_was_armed_and_disarmed(tmp_path: Path) -> None:
    """Quien observa la autonomía se entera de los dos cambios, no sólo de uno.

    Existe por el estado que se observa: es el que autoriza a gastar **sin
    preguntar**. La interfaz necesita pintarlo, y un indicador que sólo se
    enterara de armar se quedaría encendido después de desarmar —que es el peor
    de los dos errores posibles—.
    """
    policy, *_ = _policy(tmp_path)
    visto: list[bool] = []
    policy.subscribe(visto.append)

    policy.arm(FRASE)
    policy.disarm()

    assert visto == [True, False]


def test_a_refused_arm_notifies_the_truth_and_never_a_phantom_arm(
    tmp_path: Path,
) -> None:
    """Un intento de armar que se rechaza avisa de que sigue desarmada.

    No se afirma «no avisa»: avisa, y con `False`, que es lo que de verdad pasa.
    Lo que no puede llegar nunca es un `True` por un armado que no ocurrió — el
    indicador se encendería sin que nada pueda gastar sin preguntar, y un aviso
    que se enciende de más es el que enseña a no mirarlo.
    """
    policy, keys, *_ = _policy(tmp_path)
    visto: list[bool] = []
    policy.subscribe(visto.append)

    with pytest.raises(ExecutionError):
        policy.arm("frase-equivocada")

    # Y sin clave tampoco, aunque la frase sea la correcta.
    keys.key = None
    with pytest.raises(ExecutionError):
        policy.arm(FRASE)

    assert True not in visto
    assert _armed(policy) is False


def test_the_state_that_is_notified_is_the_one_the_policy_reports(
    tmp_path: Path,
) -> None:
    """Y coincide con `armed` leído en el momento de la notificación.

    Se comprueba desde dentro del observador y no después: lo que hay que
    asegurar es que el valor que llega es el vigente **entonces**, que es cuando
    la interfaz lo pinta.
    """
    policy, *_ = _policy(tmp_path)
    coherentes: list[bool] = []
    policy.subscribe(lambda estado: coherentes.append(estado == policy.armed))

    policy.arm(FRASE)
    policy.disarm()
    policy.disarm()  # desarmar dos veces vuelve a notificar, y sigue siendo coherente

    assert coherentes == [True, True, True]


def test_a_listener_that_breaks_does_not_stop_arming(tmp_path: Path) -> None:
    """Un observador roto no puede tumbar una decisión sobre dinero.

    El caso real es una interfaz cerrándose: el widget ya no existe y pintarlo
    lanza. Si eso propagara, desarmar —que es el freno— dejaría de funcionar
    justo cuando algo va mal, que es cuando hace falta.
    """
    policy, *_ = _policy(tmp_path)

    def rompe(_estado: bool) -> None:
        raise RuntimeError("la ventana ya no está")

    policy.subscribe(rompe)
    visto: list[bool] = []
    policy.subscribe(visto.append)

    with capture_logs() as logs:
        policy.arm(FRASE)  # no debe lanzar

    assert _armed(policy) is True
    # Y el observador que sí funciona recibe igualmente lo suyo.
    assert visto == [True]
    assert any(entrada["event"] == "autonomy.listener_failed" for entrada in logs)


def test_unsubscribing_stops_the_notifications(tmp_path: Path) -> None:
    """La baja que devuelve `subscribe` funciona, y dos veces no rompe."""
    policy, *_ = _policy(tmp_path)
    visto: list[bool] = []
    baja = policy.subscribe(visto.append)

    policy.arm(FRASE)
    baja()
    policy.disarm()
    baja()  # idempotente

    assert visto == [True]


def test_disarming_never_fails_even_when_nothing_was_armed(tmp_path: Path) -> None:
    """Es un freno: bloquearlo sería el fallo que no se puede permitir.

    Un freno de emergencia no lleva llave. El coste de desarmar por error es
    perder una oportunidad; el de no poder desarmar es no poder parar una máquina
    que está gastando dinero.
    """
    policy, keys, passphrase, *_ = _policy(tmp_path)
    policy.disarm()  # sin haber armado nunca
    assert not policy.armed

    # Y también cuando ya no quedan las condiciones para armar.
    policy.arm(FRASE)
    keys.key = None
    passphrase.value = None
    policy.disarm("freno de emergencia")
    assert not policy.armed
    assert policy.events[-1].reason == "freno de emergencia"


def test_arming_at_startup_does_nothing_when_the_trigger_is_manual(tmp_path: Path) -> None:
    """Si el usuario no ha pedido ejecución automática, armar sería decidir por él."""
    policy, *_ = _policy(tmp_path, trigger=TriggerKind.MANUAL)
    assert policy.arm_from_config() is False
    assert not policy.armed
    assert policy.events == ()


@pytest.mark.parametrize("trigger", [TriggerKind.AUTO, TriggerKind.PINNED])
def test_arming_at_startup_needs_only_the_configured_secrets(
    tmp_path: Path, trigger: TriggerKind
) -> None:
    """Desatendido quiere decir que no hay nadie para teclear la frase.

    Lo que habilita es **que la frase esté configurada**: un acto deliberado del
    operador y no un valor por omisión que nadie eligió.
    """
    policy, *_ = _policy(tmp_path, trigger=trigger)
    assert policy.arm_from_config() is True
    assert policy.armed
    assert str(trigger) in policy.events[-1].reason


def test_arming_at_startup_without_a_key_says_so_and_stays_disarmed(tmp_path: Path) -> None:
    policy, *_ = _policy(tmp_path, trigger=TriggerKind.AUTO, keys=FakeKey(None))
    with capture_logs() as captured:
        assert policy.arm_from_config() is False

    assert not policy.armed
    assert [item["event"] for item in captured] == ["autonomy.not_armed"]


def test_the_key_and_the_passphrase_never_appear_in_what_gets_printed(tmp_path: Path) -> None:
    """Ninguno de los dos secretos tiene que poder sacarse de un `repr()`."""
    policy, keys, passphrase, ledger, _ = _policy(tmp_path)
    policy.arm(FRASE)

    surfaces = [repr(policy), repr(keys), repr(passphrase), repr(ledger), repr(policy.events)]
    for surface in surfaces:
        assert CLAVE not in surface
        assert FRASE not in surface


# --------------------------------------------------------------------------- #
# Límites
# --------------------------------------------------------------------------- #
def test_a_chain_outside_the_whitelist_is_refused(tmp_path: Path) -> None:
    policy, *_ = _policy(tmp_path)
    with pytest.raises(ExecutionLimitExceededError) as excinfo:
        policy.check_intent(_intent("arbitrum"))
    assert excinfo.value.limit == "redes permitidas"


def test_a_token_outside_the_whitelist_is_refused(tmp_path: Path) -> None:
    """El par toca un token que no está permitido."""
    policy, *_ = _policy(tmp_path, limits=_limits(allowed_tokens=frozenset({"USDC"})))
    with pytest.raises(ExecutionLimitExceededError) as excinfo:
        policy.check_intent(_intent())
    assert excinfo.value.limit == "tokens permitidos"
    assert "WETH" in excinfo.value.detail


def test_an_engine_that_may_not_sign_is_refused(tmp_path: Path) -> None:
    """Un motor que sólo cotiza no debería poder construir lo que se firma."""
    policy, *_ = _policy(tmp_path, limits=_limits(allowed_engines=frozenset({"uniswap"})))
    with pytest.raises(ExecutionLimitExceededError) as excinfo:
        policy.check_intent(_intent())
    assert excinfo.value.limit == "motores permitidos"


def test_an_amount_over_the_per_trade_cap_is_refused(tmp_path: Path) -> None:
    policy, *_ = _policy(tmp_path)
    with pytest.raises(ExecutionLimitExceededError) as excinfo:
        policy.check_intent(_intent(notional="5001"))
    assert excinfo.value.limit == "importe por operación"


def test_an_amount_that_fits_exactly_is_allowed(tmp_path: Path) -> None:
    """El tope es inclusive: «máximo 5000» quiere decir 5000, no 4999,99."""
    policy, *_ = _policy(tmp_path)
    policy.check_intent(_intent(notional="5000"))


def test_the_order_checks_the_chain_before_the_amount(tmp_path: Path) -> None:
    """Se comprueba primero lo que el intento **es**, y el importe al final.

    El importe es lo único que obliga a leer el registro, así que va detrás de
    todo lo que se puede descartar sin tocar el disco. De paso, el mensaje que
    llega al usuario es el de la causa más fundamental.
    """
    policy, *_ = _policy(tmp_path, limits=_limits(allowed_chains=frozenset({"ethereum"})))
    with pytest.raises(ExecutionLimitExceededError) as excinfo:
        # Falla por dos motivos a la vez: red y importe.
        policy.check_intent(_intent("base", notional="99999"))
    assert excinfo.value.limit == "redes permitidas"


def test_the_master_switch_is_off_by_default() -> None:
    """Construir los límites sin decir nada **no** autoriza a operar.

    Es la propiedad que hace que activar la ejecución sea un acto deliberado: si
    el valor por omisión fuera `True`, un `config.toml` al que le faltara la línea
    —o un objeto construido a mano en cualquier otro sitio— autorizaría a mover
    dinero sin que nadie lo hubiera decidido.
    """
    assert ExecutionLimits().enabled is False


def test_with_execution_disabled_nothing_is_allowed(tmp_path: Path) -> None:
    """Un intento impecable se rechaza igual si el interruptor está apagado."""
    policy, *_ = _policy(tmp_path, limits=_limits(enabled=False))
    with pytest.raises(ExecutionLimitExceededError) as excinfo:
        policy.check_intent(_intent())
    assert excinfo.value.limit == "ejecución deshabilitada"
    assert "enabled = true" in excinfo.value.detail


def test_the_master_switch_is_checked_before_everything_else(tmp_path: Path) -> None:
    """El orden importa para el mensaje, no sólo para el gasto.

    El intento falla por tres motivos a la vez —interruptor, red e importe— y el
    que tiene que llegar al usuario es el de la causa que hace inútiles a los
    otros. Si ganara «te pasaste del tope diario», el usuario ajustaría una cifra
    para arreglar algo que no es el problema.
    """
    policy, *_ = _policy(
        tmp_path,
        limits=_limits(
            enabled=False,
            allowed_chains=frozenset({"ethereum"}),
            max_quote_per_trade=Decimal("1"),
        ),
    )
    with pytest.raises(ExecutionLimitExceededError) as excinfo:
        policy.check_intent(_intent("base", notional="99999"))
    assert excinfo.value.limit == "ejecución deshabilitada"


def test_limits_from_config_carries_the_switch() -> None:
    """La configuración es la única fuente del permiso, y llega hasta el dominio."""
    assert limits_from_config(
        enabled=True,
        max_quote_per_trade=None,
        max_quote_per_day=None,
        allowed_tokens=["USDC"],
        allowed_chains=["base"],
        allowed_engines=["zeroex"],
        max_executions_per_cycle=1,
        slippage_bps=50,
    ).enabled is True


def test_the_daily_cap_counts_what_the_ledger_already_recorded(tmp_path: Path) -> None:
    """El tope diario sólo significa algo si sobrevive al reinicio.

    Aquí el registro se puebla como si fueran ejecuciones de esta mañana —con
    otro objeto de política, para que quede claro que el dato viene del disco y
    no de memoria.
    """
    intent = _intent(notional="400")
    _, _, _, ledger, _ = _policy(tmp_path)
    _spend(ledger, intent, "19700", when=NOW - timedelta(hours=2))

    policy = AutonomyPolicy(
        keys=FakeKey(),
        passphrase=FakePassphrase(),
        limits=_limits(),
        ledger=ledger,
        clock=FrozenClock(NOW),
    )
    with pytest.raises(ExecutionLimitExceededError) as excinfo:
        policy.check_intent(intent)
    assert excinfo.value.limit == "importe acumulado en 24 h"
    assert "19700" in excinfo.value.detail


def test_spending_outside_the_window_does_not_count_against_the_cap(tmp_path: Path) -> None:
    """Sin esto el tope diario sería un tope histórico que nunca se libera."""
    intent = _intent(notional="400")
    _, _, _, ledger, _ = _policy(tmp_path)
    _spend(ledger, intent, "19500", when=NOW - timedelta(hours=25))

    policy = AutonomyPolicy(
        keys=FakeKey(),
        passphrase=FakePassphrase(),
        limits=_limits(),
        ledger=ledger,
        clock=FrozenClock(NOW),
    )
    policy.check_intent(intent)  # no lanza


def test_spending_in_another_unit_is_not_summed_and_is_announced(tmp_path: Path) -> None:
    """Sumar unidades distintas sería sumar peras y manzanas.

    Si pasara, el tope dejaría de significar lo que dice. Se cuenta sólo lo que
    corresponde y se avisa de lo demás, para que la discrepancia se vea en vez
    de esconderse dentro de una cifra.
    """
    intent = _intent(notional="100")
    policy, _, _, ledger, _ = _policy(tmp_path)
    _spend(ledger, intent, "500", when=NOW - timedelta(hours=1))
    # Otra unidad dentro de la ventana: no se suma, pero se nombra.
    ledger.append(
        LedgerEntry(
            occurred_at=NOW - timedelta(hours=1),
            chain="base",
            pair="WETH/DAI",
            engine_id="zeroex",
            notional="700",
            notional_symbol="DAI",
            tx_hash="0x" + "cd" * 32,
            status="success",
            recipient=intent.recipient,
            description="",
        )
    )

    with capture_logs() as captured:
        total = policy.spent_in_window(intent.notional.symbol)

    assert total == Decimal("500")
    avisos = [item for item in captured if item["event"] == "autonomy.mixed_notional_units"]
    assert len(avisos) == 1
    assert avisos[0]["ignored"] == ["DAI"]


# --------------------------------------------------------------------------- #
# El bypass: qué puede y qué no puede hacer
# --------------------------------------------------------------------------- #
def test_the_bypass_never_skips_a_question_that_is_not_about_broadcasting(
    tmp_path: Path,
) -> None:
    """Construir o cotizar no tiene efecto: preguntar por ello entrena a decir que sí.

    Se comprueba con la autonomía **armada**, que es el único caso en el que
    podría tener algo que decir.
    """
    policy, *_ = _policy(tmp_path, trigger=TriggerKind.AUTO)
    assert policy.arm_from_config()
    bypass = PolicyBypass(policy)

    for capability in (Capability.PREPARE_TX, Capability.COMPUTE_ROUTE, Capability.READ_CHAIN):
        assert bypass.reason_to_skip(_pending(capability)) is None


def test_the_bypass_says_nothing_while_the_autonomy_is_disarmed(tmp_path: Path) -> None:
    bypass = PolicyBypass(_policy(tmp_path)[0])
    assert bypass.reason_to_skip(_pending(Capability.BROADCAST_TX)) is None


def test_the_bypass_skips_the_question_when_the_autonomy_is_armed(tmp_path: Path) -> None:
    policy, *_ = _policy(tmp_path, trigger=TriggerKind.AUTO)
    assert policy.arm_from_config()
    bypass = PolicyBypass(policy)

    assert bypass.reason_to_skip(_pending(Capability.BROADCAST_TX)) == POLICY_REASON


def test_the_bypass_stops_skipping_when_the_key_goes_away(tmp_path: Path) -> None:
    """La decisión se toma contra el estado de ahora, no contra el de al armar."""
    policy, keys, *_ = _policy(tmp_path, trigger=TriggerKind.AUTO)
    assert policy.arm_from_config()
    bypass = PolicyBypass(policy)
    assert bypass.reason_to_skip(_pending(Capability.BROADCAST_TX)) == POLICY_REASON

    keys.key = None
    assert bypass.reason_to_skip(_pending(Capability.BROADCAST_TX)) is None


# --------------------------------------------------------------------------- #
# El bypass dentro de la pasarela, y la barrera que no puede saltarse
# --------------------------------------------------------------------------- #
def _pending(capability: Capability) -> PendingAction:
    return PendingAction(
        action_id="a1",
        capability=capability,
        title="Emitir swap",
        requested_at=NOW,
    )


def _armed_gateway(
    tmp_path: Path,
    mode: OperationMode,
    *,
    answer: bool = True,
) -> tuple[ConfirmationGateway, RecordingPrompt, PolicyBypass]:
    policy, *_ = _policy(tmp_path, trigger=TriggerKind.AUTO)
    assert policy.arm_from_config()
    bypass = PolicyBypass(policy)
    prompt = RecordingPrompt(answer=answer)
    gateway = ConfirmationGateway(
        ModeGuard(mode), prompt=prompt, clock=FrozenClock(NOW), bypass=bypass
    )
    return gateway, prompt, bypass


async def test_an_armed_bypass_executes_without_asking_and_records_that_it_did(
    tmp_path: Path,
) -> None:
    """La traza tiene que poder contar que algo se hizo sin preguntar.

    Si esto se anotara como una confirmación humana más, el registro diría algo
    falso justo sobre las operaciones que nadie miró.
    """
    gateway, prompt, _ = _armed_gateway(tmp_path, OperationMode.EXECUTION)

    action = await gateway.authorize(Capability.BROADCAST_TX, "Emitir swap")

    assert action.capability is Capability.BROADCAST_TX
    assert prompt.asked == []
    (record,) = gateway.history
    assert record.decision is Decision.APPROVED_BY_POLICY
    assert record.reason == POLICY_REASON


async def test_without_a_bypass_the_question_is_always_asked(tmp_path: Path) -> None:
    """`None` por omisión significa que sólo una persona puede aprobar algo."""
    prompt = RecordingPrompt(answer=True)
    gateway = ConfirmationGateway(
        ModeGuard(OperationMode.EXECUTION), prompt=prompt, clock=FrozenClock(NOW)
    )

    await gateway.authorize(Capability.BROADCAST_TX, "Emitir swap")

    assert len(prompt.asked) == 1
    assert gateway.history[-1].decision is Decision.APPROVED


async def test_a_rejected_action_raises_and_leaves_no_authorisation(tmp_path: Path) -> None:
    prompt = RecordingPrompt(answer=False)
    gateway = ConfirmationGateway(
        ModeGuard(OperationMode.EXECUTION), prompt=prompt, clock=FrozenClock(NOW)
    )

    with pytest.raises(ConfirmationDeniedError):
        await gateway.authorize(Capability.BROADCAST_TX, "Emitir swap")
    assert gateway.history[-1].decision is Decision.REJECTED


@pytest.mark.parametrize(
    "mode",
    [OperationMode.OBSERVATION, OperationMode.SIMULATION, OperationMode.ASSISTED],
)
async def test_an_armed_bypass_cannot_get_past_the_mode_barrier(
    tmp_path: Path, mode: OperationMode
) -> None:
    """**La propiedad central.** Ninguna autonomía concede una capacidad.

    Se comprueban las dos mitades, y las dos hacen falta:

    - que el modo sigue negando la operación, y
    - que **no se llegó a preguntar** — es decir, que el bypass no se consultó
      antes de la barrera y aprobó por la puerta de atrás.

    Sólo con la primera afirmación, un bypass colocado antes de la comprobación
    de modo aprobaría la acción *y* devolvería un objeto autorizado; el error que
    vería la prueba sería el mismo que se ve cuando todo va bien. El par de
    afirmaciones es lo que hace que esta prueba detecte la mutación.
    """
    gateway, prompt, _ = _armed_gateway(tmp_path, mode)

    with pytest.raises(ModeNotPermittedError):
        await gateway.authorize(Capability.BROADCAST_TX, "Emitir swap")

    assert prompt.asked == []
    decisions = [record.decision for record in gateway.history]
    assert decisions == [Decision.BLOCKED_BY_MODE]


async def test_disconnecting_the_bypass_makes_the_question_come_back(tmp_path: Path) -> None:
    """Desarmar la ejecución desatendida tiene que ser posible en caliente."""
    gateway, prompt, _ = _armed_gateway(tmp_path, OperationMode.EXECUTION)
    gateway.set_bypass(None)

    await gateway.authorize(Capability.BROADCAST_TX, "Emitir swap")

    assert len(prompt.asked) == 1


@pytest.mark.parametrize(
    "mode",
    [OperationMode.OBSERVATION, OperationMode.SIMULATION, OperationMode.ASSISTED],
)
def test_the_precheck_is_also_a_barrier_and_the_bypass_is_not_in_it(
    tmp_path: Path, mode: OperationMode
) -> None:
    """La barrera vive en **dos** sitios, y el segundo también hay que cubrirlo.

    Lo encontró una mutación, y no esta prueba: al mover el bloque del bypass
    «antes de la comprobación de modo», el script lo insertó en `precheck` —que
    tiene el mismo `if` y aparece antes en el fichero— y la suite no lo vio,
    porque sólo estaba cubierto el camino de `authorize`. `precheck` es lo que
    usan los casos de uso para descartar el trabajo que el modo no permite
    **antes** de gastar red, así que un bypass que se colara aquí dejaría pasar
    una operación que el modo prohíbe.
    """
    gateway, prompt, _ = _armed_gateway(tmp_path, mode)

    with pytest.raises(ModeNotPermittedError):
        gateway.precheck(Capability.BROADCAST_TX)

    assert prompt.asked == []
    assert gateway.history[-1].decision is Decision.BLOCKED_BY_MODE


async def test_a_read_only_capability_is_approved_without_asking_anyone(
    tmp_path: Path,
) -> None:
    """Leer no tiene efecto: el registro dice que fue de sólo lectura."""
    prompt = RecordingPrompt(answer=False)
    gateway = ConfirmationGateway(
        ModeGuard(OperationMode.EXECUTION), prompt=prompt, clock=FrozenClock(NOW)
    )

    await gateway.authorize(Capability.READ_CHAIN, "Leer saldo")

    assert prompt.asked == []
    assert gateway.history[-1].reason == "capacidad de sólo lectura"


# --------------------------------------------------------------------------- #
# Configuración → límites
# --------------------------------------------------------------------------- #
def test_limits_from_config_normalises_the_case_of_the_lists() -> None:
    """El `config.toml` lo escribe una persona y no tiene por qué respetar la caja.

    «usdc» no casando con «USDC» cortaría una operación por un límite que el
    usuario cree haber configurado bien.
    """
    limits = limits_from_config(
        enabled=True,
        max_quote_per_trade="5000",
        max_quote_per_day="20000",
        allowed_tokens=["weth", "usdc", " USDC "],
        allowed_chains=["Base", "ETHEREUM"],
        allowed_engines=["zeroex"],
        max_executions_per_cycle=2,
        slippage_bps=75,
    )

    assert limits.allowed_tokens == frozenset({"WETH", "USDC"})
    assert limits.allowed_chains == frozenset({"base", "ethereum"})
    assert limits.max_quote_per_trade == Decimal("5000")
    assert limits.max_executions_per_cycle == 2
    assert limits.slippage_bps == 75


def test_limits_from_config_drops_empty_entries() -> None:
    limits = limits_from_config(
        enabled=True,
        max_quote_per_trade=None,
        max_quote_per_day=None,
        allowed_tokens=["WETH", "", "   "],
        allowed_chains=["base", ""],
        allowed_engines=["zeroex"],
        max_executions_per_cycle=1,
        slippage_bps=50,
    )
    assert limits.allowed_tokens == frozenset({"WETH"})
    assert limits.allowed_chains == frozenset({"base"})
    assert limits.max_quote_per_trade is None


def test_limits_from_config_keeps_the_exact_figures() -> None:
    """Sin pasar por `float` en ningún momento.

    Un tope de gasto redondeado en algún punto es un tope que no se cumple, y
    `0.1 + 0.2` en coma flotante es la demostración de siempre.
    """
    limits = limits_from_config(
        enabled=True,
        max_quote_per_trade="0.1",
        max_quote_per_day="0.3",
        allowed_tokens=["USDC"],
        allowed_chains=["base"],
        allowed_engines=["zeroex"],
        max_executions_per_cycle=1,
        slippage_bps=50,
    )
    assert limits.max_quote_per_trade == Decimal("0.1")
    assert limits.max_quote_per_trade + Decimal("0.2") == Decimal("0.3")


def test_an_empty_whitelist_means_nothing_is_allowed() -> None:
    """El fallo tiene que caer del lado de no operar.

    Una lista blanca vacía que permitiera todo convertiría un descuido al
    escribir el `config.toml` en una autorización para operar con cualquier cosa.
    """
    limits = limits_from_config(
        enabled=True,
        max_quote_per_trade=None,
        max_quote_per_day=None,
        allowed_tokens=[],
        allowed_chains=[],
        allowed_engines=[],
        max_executions_per_cycle=1,
        slippage_bps=50,
    )
    with pytest.raises(ExecutionLimitExceededError):
        limits.check_token(("WETH", "USDC"))
    with pytest.raises(ExecutionLimitExceededError):
        limits.check_chain("base")
    with pytest.raises(ExecutionLimitExceededError):
        limits.check_engine("zeroex")


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_quote_per_trade", "0"), ("max_quote_per_day", "-1"), ("max_quote_per_trade", "no")],
)
def test_an_impossible_cap_is_refused_at_construction(field: str, value: str) -> None:
    """Un tope de cero no es un tope laxo: es «no operes nunca» disfrazado."""
    from amigocompora.domain.errors import InvalidAmountError

    arguments: dict[str, object] = {
        "enabled": True,
        "max_quote_per_trade": None,
        "max_quote_per_day": None,
        "allowed_tokens": ["USDC"],
        "allowed_chains": ["base"],
        "allowed_engines": ["zeroex"],
        "max_executions_per_cycle": 1,
        "slippage_bps": 50,
    }
    arguments[field] = value

    with pytest.raises(InvalidAmountError):
        limits_from_config(**arguments)  # type: ignore[arg-type]
