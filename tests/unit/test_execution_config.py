"""La configuración de la ejecución: los topes, y lo que no cabe en el fichero.

Dos cosas se sostienen aquí y son las que más importan de todo el bloque:

1. **Una clave privada no se puede escribir en `config.toml`.** No es una
   recomendación en la documentación: el modelo usa `extra="forbid"`, así que
   una clave escrita por error no se ignora en silencio — aborta el arranque con
   un mensaje que dice dónde va. Un `config.toml` se copia, se pega en un issue
   y se sube a un repositorio, y esto es lo que impide que la cartera viaje con
   él.
2. **Las cifras son exactas.** Los topes se declaran como texto y se convierten
   sin pasar por `float` en ningún momento: un tope de gasto que se hubiera
   redondeado en algún punto es un tope que no se cumple.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from amigocompora.domain.errors import InvalidAmountError
from amigocompora.domain.execution import GasStrategy, TriggerKind
from amigocompora.infra.config import ExecutionSettings, Settings

#: Con forma de clave real. No es una clave: son 64 treses.
CLAVE_FALSA = "0x" + "3" * 64


# --------------------------------------------------------------------------- #
# Lo que viene por omisión
# --------------------------------------------------------------------------- #
def test_execution_is_off_by_default() -> None:
    """Nada se ejecuta hasta que alguien lo enciende, y son tres actos distintos.

    Hay que encender `execution.enabled` **y** estar en modo `EJECUCIÓN` **y**,
    para operar sin confirmar, tener armada la autonomía con su frase. Que el
    valor por omisión de la primera sea `false` es la primera de las tres.
    """
    settings = ExecutionSettings()
    assert settings.enabled is False
    assert settings.trigger is TriggerKind.MANUAL
    assert settings.pinned == ()
    assert settings.allowed_tokens == ()
    assert settings.max_quote_per_trade is None
    assert settings.allow_env_key is False
    # Y lo mismo visto desde la configuración completa.
    assert Settings().execution.enabled is False


def test_a_declared_execution_block_parses_completely() -> None:
    settings = Settings.model_validate(
        {
            "execution": {
                "enabled": True,
                "trigger": "auto",
                "allowed_chains": ["base", "ethereum"],
                "allowed_tokens": ["WETH", "USDC"],
                "allowed_engines": ["zeroex"],
                "max_quote_per_trade": "5000",
                "max_quote_per_day": "20000",
                "max_executions_per_cycle": 2,
                "slippage_bps": 75,
            }
        }
    ).execution

    assert settings.enabled is True
    assert settings.trigger is TriggerKind.AUTO
    assert settings.allowed_chains == ("base", "ethereum")
    assert settings.max_quote_per_trade == "5000"
    assert settings.max_executions_per_cycle == 2
    assert settings.slippage_bps == 75


def test_reading_the_key_from_the_environment_needs_an_explicit_line() -> None:
    """El respaldo apagado por omisión es la mitad de la custodia.

    Sin backend de keyring, una clave privada en el entorno del proceso la ve
    cualquiera que pueda leer ese entorno, y a diferencia de una API key no se
    rota. Que haga falta escribir esta línea es lo que convierte el respaldo en
    una decisión y no en un descuido.
    """
    assert Settings().execution.allow_env_key is False
    assert (
        Settings.model_validate({"execution": {"allow_env_key": True}}).execution.allow_env_key
        is True
    )


def test_declaring_the_env_fallback_does_not_require_enabling_execution() -> None:
    """Encenderlos son dos actos distintos, y no se obliga al orden.

    Exigir `enabled = true` para poder declarar el respaldo convertiría «voy a
    dejarlo preparado» en «no puedo dejarlo listo». La combinación es inerte de
    todas formas: con la ejecución apagada no se firma nada, así que una clave
    que se pueda leer no llega a usarse.
    """
    settings = Settings.model_validate(
        {"execution": {"allow_env_key": True, "enabled": False}}
    ).execution
    assert settings.allow_env_key is True
    assert settings.enabled is False


# --------------------------------------------------------------------------- #
# La clave privada no cabe en el fichero
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field",
    ["private_key", "execution_private_key", "autonomy_passphrase", "passphrase", "key"],
)
def test_a_secret_cannot_be_written_in_the_execution_block(field: str) -> None:
    """Un secreto en `config.toml` aborta el arranque en vez de ignorarse.

    `extra="forbid"` es lo que convierte esto en una salvaguarda y no en una
    recomendación: si el campo desconocido se ignorara, el usuario creería haber
    configurado su clave y la aplicación le diría que no hay ninguna, sin
    relacionar nunca las dos cosas.
    """
    with pytest.raises(ValidationError) as excinfo:
        ExecutionSettings.model_validate({field: CLAVE_FALSA})

    assert "Extra inputs are not permitted" in str(excinfo.value)
    assert field in str(excinfo.value)


def test_a_secret_cannot_be_written_anywhere_in_the_settings() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings.model_validate({"execution": {"private_key": CLAVE_FALSA}})

    assert "Extra inputs are not permitted" in str(excinfo.value)
    # Y el mensaje de la clave no aparece en el error, que también se imprime.
    assert CLAVE_FALSA not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Las cifras son exactas
# --------------------------------------------------------------------------- #
def test_a_cap_written_as_a_toml_float_is_refused() -> None:
    """`max_quote_per_trade = 5000.0` sin comillas no se acepta.

    Podría aceptarse y convertirse, y sería el camino por el que un tope de
    gasto acaba redondeado: el `float` de TOML ya pasó por coma flotante antes
    de que nadie lo viera. Se exige texto para que esa conversión no ocurra
    nunca, y el error explica cómo escribirlo.
    """
    with pytest.raises(ValidationError) as excinfo:
        ExecutionSettings.model_validate({"max_quote_per_trade": 5000.0})

    assert "valid string" in str(excinfo.value)


def test_a_cap_that_is_not_a_number_is_refused_when_it_becomes_a_limit() -> None:
    """El texto se convierte con `parse_amount`, y ahí se comprueba que lo sea."""
    settings = ExecutionSettings(max_quote_per_trade="mucho")
    with pytest.raises(InvalidAmountError, match="no es una cifra válida"):
        _limits(settings)


def _limits(settings: ExecutionSettings) -> object:
    from amigocompora.app.execution_policy import limits_from_config

    return limits_from_config(
        enabled=settings.enabled,
        max_quote_per_trade=settings.max_quote_per_trade,
        max_quote_per_day=settings.max_quote_per_day,
        allowed_tokens=settings.allowed_tokens,
        allowed_chains=settings.allowed_chains,
        allowed_engines=settings.allowed_engines,
        max_executions_per_cycle=settings.max_executions_per_cycle,
        slippage_bps=settings.slippage_bps,
    )


def test_the_figures_reach_the_domain_without_passing_through_a_float() -> None:
    """`0.1` sigue siendo `0.1` exacto después de todo el recorrido."""
    from amigocompora.app.execution_policy import limits_from_config

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


# --------------------------------------------------------------------------- #
# Configuraciones que no pueden funcionar
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "missing", ["allowed_chains", "allowed_tokens", "allowed_engines"]
)
def test_enabling_execution_with_an_empty_list_is_refused(missing: str) -> None:
    """Vacío significa «nada permitido», así que encenderlo con una lista vacía no hace nada.

    Se rechaza al cargar y no al operar porque el fallo en tiempo de ejecución
    describe el síntoma —«no está en la lista blanca (ninguno)»— y no la causa,
    que es un fichero al que le falta una línea.
    """
    complete = {
        "allowed_chains": ["base"],
        "allowed_tokens": ["USDC"],
        "allowed_engines": ["zeroex"],
    }
    complete[missing] = []

    with pytest.raises(ValidationError) as excinfo:
        ExecutionSettings.model_validate({"enabled": True, **complete})

    assert missing in str(excinfo.value)
    assert "nada permitido" in str(excinfo.value)


def test_disabling_execution_lets_the_lists_be_empty() -> None:
    """Apagado no hace falta declarar nada: no se va a ejecutar."""
    settings = ExecutionSettings(enabled=False)
    assert settings.allowed_tokens == ()


def test_a_pinned_trigger_without_pinned_operations_is_refused() -> None:
    """Una tarea programada que no hace nada, para siempre, en silencio."""
    with pytest.raises(ValidationError, match="no hay nada que ejecutar"):
        ExecutionSettings(trigger=TriggerKind.PINNED)


def test_pinned_operations_without_the_pinned_trigger_are_refused() -> None:
    """Al revés: operaciones declaradas que no se ejecutarían nunca."""
    with pytest.raises(ValidationError, match="no se ejecutarían"):
        ExecutionSettings.model_validate(
            {
                "trigger": "auto",
                "pinned": [
                    {
                        "chain": "base",
                        "base": "WETH",
                        "quote": "USDC",
                        "amount": "250",
                        "every_minutes": 60,
                    }
                ],
            }
        )


def test_a_pinned_operation_parses_with_its_schedule() -> None:
    settings = ExecutionSettings.model_validate(
        {
            "enabled": True,
            "trigger": "pinned",
            "allowed_chains": ["base"],
            "allowed_tokens": ["WETH", "USDC"],
            "allowed_engines": ["zeroex"],
            "pinned": [
                {
                    "chain": "Base",
                    "base": "WETH",
                    "quote": "USDC",
                    "amount": "250",
                    "every_minutes": 60,
                }
            ],
        }
    )
    (pinned,) = settings.pinned
    # La red se normaliza a la clave del registro, que va en minúsculas.
    assert pinned.chain == "base"
    assert pinned.amount == Decimal("250")
    assert pinned.every_minutes == 60


@pytest.mark.parametrize("every_minutes", [0, -5])
def test_a_pinned_operation_needs_a_positive_schedule(every_minutes: int) -> None:
    """Cada cero minutos no es una cadencia: es un bucle."""
    with pytest.raises(ValidationError):
        ExecutionSettings.model_validate(
            {
                "trigger": "pinned",
                "pinned": [
                    {
                        "chain": "base",
                        "base": "WETH",
                        "quote": "USDC",
                        "amount": "1",
                        "every_minutes": every_minutes,
                    }
                ],
            }
        )


def test_an_unknown_chain_in_the_whitelist_is_refused() -> None:
    with pytest.raises(ValidationError, match="redes desconocidas"):
        ExecutionSettings.model_validate({"allowed_chains": ["base", "marte"]})


def test_the_chain_wildcard_is_not_an_unknown_chain() -> None:
    """`["*"]` es «cualquiera»: pasa la validación como cualquier otra entrada.

    Sin esta excepción, `allowed_chains = ["*"]` no cargaría y la decisión de
    abrir las redes no tendría forma de escribirse. Se comprueba solo y
    mezclado con una red real: el comodín no estorba a la comprobación de los
    demás nombres —`marte` sigue rechazándose—.
    """
    assert ExecutionSettings.model_validate({"allowed_chains": ["*"]}).allowed_chains == (
        "*",
    )
    assert ExecutionSettings.model_validate(
        {"allowed_chains": ["base", "*"]}
    ).allowed_chains == ("base", "*")


def test_an_unknown_key_under_a_pinned_operation_is_refused() -> None:
    """Un error de escritura en la cadencia no puede cambiar el significado."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExecutionSettings.model_validate(
            {
                "trigger": "pinned",
                "pinned": [
                    {
                        "chain": "base",
                        "base": "WETH",
                        "quote": "USDC",
                        "amount": "1",
                        "every_minute": 60,
                    }
                ],
            }
        )


# --------------------------------------------------------------------------- #
# Gas
# --------------------------------------------------------------------------- #
def test_the_gas_block_becomes_a_domain_policy() -> None:
    settings = Settings.model_validate(
        {"execution": {"gas": {"strategy": "auto", "multiplier": "1.25"}}}
    )
    policy = settings.execution.gas.to_policy()

    assert policy.strategy is GasStrategy.AUTO
    assert policy.multiplier == Decimal("1.25")
    # Y la política calcula de verdad: 2 · base + propina, con el factor.
    max_fee, tip = policy.cap(base_fee_per_gas=1_000_000_000, observed_tip=1_000_000)
    assert tip == 1_000_000
    assert max_fee == int(Decimal(2_001_000_000) * Decimal("1.25"))


def test_a_fixed_gas_strategy_needs_both_figures() -> None:
    """La regla vive en el dominio y se aplica al traducir, sin repetirla aquí."""
    settings = Settings.model_validate({"execution": {"gas": {"strategy": "fixed"}}})
    with pytest.raises(InvalidAmountError, match="precio máximo por"):
        settings.execution.gas.to_policy()


def test_a_fixed_gas_strategy_with_both_figures_is_accepted() -> None:
    settings = Settings.model_validate(
        {
            "execution": {
                "gas": {
                    "strategy": "fixed",
                    "fixed_max_fee_per_gas": 3_000_000_000,
                    "fixed_max_priority_fee_per_gas": 1_000_000,
                }
            }
        }
    )
    policy = settings.execution.gas.to_policy()
    assert policy.cap(0, 0) == (3_000_000_000, 1_000_000)


def test_a_gas_multiplier_that_is_not_a_number_is_refused() -> None:
    settings = Settings.model_validate({"execution": {"gas": {"multiplier": "bastante"}}})
    with pytest.raises(InvalidAmountError, match="no es una cifra válida"):
        settings.execution.gas.to_policy()
