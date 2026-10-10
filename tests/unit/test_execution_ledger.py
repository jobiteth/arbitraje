"""El registro de ejecuciones: lo que hace que un tope diario sea un tope.

Lo que se comprueba aquí, y por qué cada cosa importa:

- Que el fichero sobreviva al proceso. Si el gasto del día sólo se contara en
  memoria, cerrar y abrir la aplicación lo borraría y el tope no sería un tope.
- Que lo que cuenta y lo que no cuenta esté decidido por el **desenlace** y no
  por el optimismo: lo emitido y pendiente consume tope, lo revertido y lo
  desconocido no.
- Que un renglón roto no impida leer los demás. El registro lo lee la aplicación
  al arrancar, y un fichero que impide abrir el programa por su propio contenido
  es peor que el renglón perdido.
- Que los asientos se puedan leer por token y por red, que es de donde sale
  «Actividad» en el detalle de la cartera, y que un renglón viejo —de antes de
  que existiera el campo de tokens— siga emparejándose por el texto del par.
- Que no haya secretos dentro. Es un fichero de texto que crece, que se copia en
  las copias de seguridad y que acaba adjunto en un informe de fallo.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from amigocompora.app.execution_policy import (
    APPROVAL_KIND,
    RECEIVE_KIND,
    REDEEM_KIND,
    ExecutionLedger,
    LedgerEntry,
)
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.models import (
    BroadcastReceipt,
    BroadcastStatus,
    ExecutionIntent,
    Quote,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.engines.catalog import quote_token, wrapped_native

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
#: Una clave con la forma de una de verdad, para poder buscarla en el fichero.
#: No es una clave: son 64 ceros.
CLAVE_FALSA = "0x" + "0" * 64


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _pair(chain_key: str = "base") -> TradingPair:
    base = wrapped_native(chain_key)
    quote = quote_token(chain_key)
    assert base is not None
    assert quote is not None
    return TradingPair(base=base, quote=quote)


def _intent(chain_key: str = "base", *, notional: str = "1500") -> ExecutionIntent:
    """Un intento con un importe de referencia concreto.

    `notional` se construye sobre la pata de cotización del par, que es lo que
    `ExecutionIntent` exige: el importe de referencia tiene que ser una de las
    dos patas, y la unidad en la que se piensa es la stablecoin.
    """
    pair = _pair(chain_key)
    return ExecutionIntent(
        quote=Quote(
            venue=Venue(
                venue_id=f"zeroex@{chain_key}",
                name=f"0x ({chain_key})",
                kind=VenueKind.DEX,
                chain=chain_key,
            ),
            engine_id="zeroex",
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
        engine_id="zeroex",
    )


def _receipt(
    *,
    status: BroadcastStatus = BroadcastStatus.SUCCESS,
    when: datetime = NOW,
    tx_hash: str = "0x" + "ab" * 32,
    chain: str = "base",
    reason: str = "",
) -> BroadcastReceipt:
    return BroadcastReceipt(
        tx_hash=tx_hash,
        chain=chain,
        status=status,
        observed_at=when,
        block_number=123,
        reason=reason,
    )


def _ledger(tmp_path: Path, moment: datetime = NOW) -> tuple[ExecutionLedger, FrozenClock]:
    clock = FrozenClock(moment)
    return ExecutionLedger(tmp_path / "config" / "executions.jsonl", clock=clock), clock


# --------------------------------------------------------------------------- #
# Escritura y lectura
# --------------------------------------------------------------------------- #
def test_an_execution_is_appended_as_one_json_line(tmp_path: Path) -> None:
    """Una línea por ejecución, y no un documento que se reescribe entero.

    Un fichero que se reescribe se puede corromper a medias si el proceso muere
    mientras escribe, y entonces se pierde todo el histórico —justo lo que el
    tope diario necesita—. Con líneas se pierde la última, y sólo si el proceso
    murió en el peor instante posible.
    """
    ledger, _ = _ledger(tmp_path)
    ledger.append(
        LedgerEntry(
            occurred_at=NOW,
            chain="base",
            pair="WETH/USDC",
            engine_id="zeroex",
            notional="1500",
            notional_symbol="USDC",
            tx_hash="0x" + "ab" * 32,
            status="success",
            recipient="0x1111111111111111111111111111111111111111",
            description="",
        )
    )

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["notional"] == "1500"


def test_the_parent_directory_is_created_on_first_write(tmp_path: Path) -> None:
    """El directorio de configuración puede no existir todavía en una máquina nueva."""
    ledger, _ = _ledger(tmp_path)
    assert not ledger.path.parent.exists()
    ledger.append(_entry())
    assert ledger.path.is_file()


def test_what_was_written_is_what_comes_back(tmp_path: Path) -> None:
    ledger, _ = _ledger(tmp_path)
    original = _entry()
    ledger.append(original)

    (read, *rest) = ledger.entries()
    assert rest == []
    assert read == original


def test_an_entry_records_the_notional_exactly(tmp_path: Path) -> None:
    """El importe va como texto decimal, sin notación científica ni redondeo.

    Se comprobó al escribir esta prueba: `f"{...:f}"` sobre lo que resultó ser un
    **método** y no una propiedad no lo habría cazado nadie hasta que una
    ejecución real intentara anotarse y reventara con un `TypeError` justo
    después de mover dinero.
    """
    ledger, clock = _ledger(tmp_path)
    entry = ledger.record_receipt(_receipt(), _intent(notional="1500"))

    assert entry.notional == "1500"
    assert entry.notional_value == Decimal("1500")
    assert entry.notional_symbol == "USDC"
    assert entry.occurred_at == clock.now()
    # Y lo mismo leído del disco, que es lo que cuenta tras un reinicio.
    assert ledger.entries()[0].notional_value == Decimal("1500")


def test_a_fractional_notional_keeps_its_decimals(tmp_path: Path) -> None:
    ledger, _ = _ledger(tmp_path)
    entry = ledger.record_receipt(_receipt(), _intent(notional="0.25"))
    assert entry.notional == "0.25"
    assert ledger.entries()[0].notional_value == Decimal("0.25")


# --------------------------------------------------------------------------- #
# Qué cuenta para los topes
# --------------------------------------------------------------------------- #
def test_pending_consumes_budget_and_reverted_does_not(tmp_path: Path) -> None:
    """El criterio lo decide el desenlace, no el optimismo.

    `PENDING` cuenta porque el dinero ya salió de la cartera y contarlo es el
    lado por el que interesa equivocarse. `REVERTED` no cuenta porque el swap no
    ocurrió: se gastó gas, pero el importe de referencia no se intercambió.
    """
    ledger, _ = _ledger(tmp_path)
    for status, when in (
        (BroadcastStatus.SUCCESS, NOW - timedelta(hours=1)),
        (BroadcastStatus.PENDING, NOW - timedelta(hours=2)),
    ):
        ledger.record_receipt(_receipt(status=status, when=when), _intent(notional="100"))

    assert ledger.spent_since(NOW - timedelta(hours=24)) == Decimal("200")

    ledger.record_receipt(
        _receipt(
            status=BroadcastStatus.REVERTED,
            when=NOW - timedelta(minutes=5),
            reason="INSUFFICIENT_OUTPUT_AMOUNT",
        ),
        _intent(notional="1000"),
    )
    # Sigue habiendo 200: lo revertido no se intercambió.
    assert ledger.spent_since(NOW - timedelta(hours=24)) == Decimal("200")


def test_an_unknown_outcome_is_written_down_but_does_not_consume_budget(tmp_path: Path) -> None:
    """Se anota —explica por qué una operación lanzada no aparece— pero no cuenta.

    Anotarla como hecha sería afirmar algo que no se sabe; no anotarla dejaría al
    usuario sin explicación para el hueco.
    """
    ledger, _ = _ledger(tmp_path)
    entry = ledger.record_receipt(
        _receipt(status=BroadcastStatus.UNKNOWN), _intent(notional="900")
    )

    assert not entry.counts_towards_limits
    assert len(ledger.entries()) == 1
    assert ledger.spent_since(NOW - timedelta(hours=24)) == Decimal("0")


def test_entries_older_than_the_window_are_left_out(tmp_path: Path) -> None:
    """Sin esto el tope diario sería un tope histórico que nunca se libera."""
    ledger, _ = _ledger(tmp_path)
    ledger.record_receipt(
        _receipt(when=NOW - timedelta(hours=25)), _intent(notional="500")
    )
    ledger.record_receipt(
        _receipt(when=NOW - timedelta(hours=23), tx_hash="0x" + "cd" * 32),
        _intent(notional="300"),
    )

    assert ledger.spent_since(NOW - timedelta(hours=24)) == Decimal("300")
    assert ledger.spent_today() == Decimal("300")


def test_the_window_is_the_clock_and_not_the_wall_clock(tmp_path: Path) -> None:
    """Se usa el reloj inyectado: `spent_today` tiene que ser reproducible."""
    ledger, clock = _ledger(tmp_path)
    ledger.record_receipt(_receipt(), _intent(notional="100"))
    assert ledger.spent_today() == Decimal("100")

    clock.advance(timedelta(hours=24, seconds=1).total_seconds())
    assert ledger.spent_today() == Decimal("0")


# --------------------------------------------------------------------------- #
# Tolerancia a un registro dañado
# --------------------------------------------------------------------------- #
def test_a_broken_line_in_the_middle_does_not_hide_the_others(tmp_path: Path) -> None:
    """Un renglón roto no puede impedir calcular el gasto del día.

    Parar en el primer renglón ilegible dejaría a la aplicación sin poder contar
    lo gastado por un byte mal puesto, que es justo el fallo que este formato
    existe para evitar.
    """
    ledger, _ = _ledger(tmp_path)
    ledger.record_receipt(_receipt(), _intent(notional="100"))
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"occurred_at": "2026-03-01T12:00:00+00:00", "chain": "bas\n')
    ledger.record_receipt(_receipt(tx_hash="0x" + "ef" * 32), _intent(notional="250"))

    with capture_logs() as captured:
        total = ledger.spent_today()

    assert total == Decimal("350")
    skipped = [item for item in captured if item["event"] == "execution.ledger_line_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["line"] == 2


def test_a_partial_last_line_does_not_swallow_the_next_entry(tmp_path: Path) -> None:
    """Regresión medida: el fragmento final se llevaba por delante a la entrada siguiente.

    Un corte de luz en mitad de un `write` deja la última línea sin salto. Al
    arrancar de nuevo y anotar, la entrada nueva se pegaba a ese fragmento y
    desaparecía **también ella**: dos renglones perdidos en vez de uno. Y una
    ejecución que no queda anotada no es sólo un hueco en la traza — es gasto que
    el tope del día deja de contar, así que la siguiente operación podría
    pasarse del tope sin que nada lo impidiera.

    Lo correcto es lo que hace `append`: cerrar la línea rota antes de añadir, de
    modo que lo único que se pierde sea el fragmento.
    """
    ledger, _ = _ledger(tmp_path)
    ledger.record_receipt(_receipt(), _intent(notional="100"))
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"occurred_at": "2026-03-01T11:00:00+00:00", "chain": "bas')

    ledger.record_receipt(_receipt(tx_hash="0x" + "ef" * 32), _intent(notional="250"))

    with capture_logs() as captured:
        total = ledger.spent_today()

    # Lo anotado después del corte sobrevive y cuenta.
    assert total == Decimal("350")
    assert len(ledger.entries()) == 2
    skipped = [item for item in captured if item["event"] == "execution.ledger_line_skipped"]
    assert [item["line"] for item in skipped] == [2]


def test_appending_to_an_empty_file_does_not_write_a_leading_blank_line(tmp_path: Path) -> None:
    """El cierre de línea sólo se escribe si hay algo que cerrar."""
    ledger, _ = _ledger(tmp_path)
    ledger.append(_entry())
    assert ledger.path.read_bytes().startswith(b"{")


def test_a_line_that_is_valid_json_but_not_an_entry_is_skipped(tmp_path: Path) -> None:
    """Un JSON correcto con la forma equivocada no es una entrada."""
    ledger, _ = _ledger(tmp_path)
    ledger.record_receipt(_receipt(), _intent(notional="100"))
    with ledger.path.open("a", encoding="utf-8") as handle:
        # Faltan campos: `from_record` devuelve `None` en vez de lanzar.
        handle.write('{"chain": "base"}\n')
        handle.write("[1, 2, 3]\n")
        handle.write("\n")

    with capture_logs() as captured:
        entries = ledger.entries()

    assert len(entries) == 1
    skipped = [item for item in captured if item["event"] == "execution.ledger_line_skipped"]
    assert [item["line"] for item in skipped] == [2, 3]


def test_a_ledger_that_does_not_exist_yet_reads_as_empty(tmp_path: Path) -> None:
    ledger, _ = _ledger(tmp_path)
    assert ledger.entries() == ()
    assert ledger.spent_today() == Decimal("0")


def test_a_write_failure_never_raises(tmp_path: Path) -> None:
    """Perder el registro no puede tumbar una operación que la red ya aceptó.

    Aquí se provoca el fallo de la forma más directa posible: el «fichero» es un
    directorio, así que abrirlo para añadir es imposible. Lo que importa es que
    `append` no lance —el dinero ya se movió y negarlo no lo devuelve— y que
    quede un error en el log, porque un registro que se pierde en silencio es
    peor que uno que falta.
    """
    blocked = tmp_path / "ocupado.jsonl"
    blocked.mkdir()
    ledger = ExecutionLedger(blocked, clock=FrozenClock(NOW))

    with capture_logs() as captured:
        ledger.append(_entry())  # no lanza

    errores = [item for item in captured if item["event"] == "execution.ledger_write_failed"]
    assert len(errores) == 1
    assert errores[0]["tx_hash"] == _entry().tx_hash


# --------------------------------------------------------------------------- #
# Sin secretos
# --------------------------------------------------------------------------- #
def test_the_ledger_never_contains_the_private_key(tmp_path: Path) -> None:
    """Un fichero de texto plano que crece no es sitio para una clave privada.

    Se busca la clave en el fichero entero después de anotar ejecuciones, y
    también en el `repr()` de lo anotado: es el sitio por el que una clave se
    filtra sin que nadie la escriba a propósito.
    """
    ledger, _ = _ledger(tmp_path)
    receipt = _receipt()
    intent = _intent()

    entry = ledger.record_receipt(receipt, intent)
    content = ledger.path.read_text(encoding="utf-8")

    for surface in (content, repr(entry), repr(receipt), repr(intent)):
        assert CLAVE_FALSA not in surface
        assert "0" * 64 not in surface
    # Ni la frase de autonomía, que es el otro secreto que habilita gastar.
    assert "passphrase" not in content.lower()


def _entry() -> LedgerEntry:
    return LedgerEntry(
        occurred_at=NOW,
        chain="base",
        pair="WETH/USDC",
        engine_id="zeroex",
        notional="1500",
        notional_symbol="USDC",
        tx_hash="0x" + "ab" * 32,
        status="success",
        recipient="0x1111111111111111111111111111111111111111",
        description="",
    )


@pytest.mark.parametrize("status", ["success", "pending"])
def test_the_counted_statuses_are_the_two_that_spent_money(status: str) -> None:
    entry = LedgerEntry(
        occurred_at=NOW,
        chain="base",
        pair="WETH/USDC",
        engine_id="zeroex",
        notional="1",
        notional_symbol="USDC",
        tx_hash="0x" + "ab" * 32,
        status=status,
        recipient="0x1111111111111111111111111111111111111111",
        description="",
    )
    assert entry.counts_towards_limits


# --------------------------------------------------------------------------- #
# Los asientos que no gastan: lo que entra y los permisos
# --------------------------------------------------------------------------- #
def test_a_receive_is_written_down_but_does_not_consume_budget(tmp_path: Path) -> None:
    """Una recepción es dinero que **entra**: contarla gastaría el tope del día
    por recuperar lo propio.

    Y lo que decide no es la cifra sino el tipo del asiento, que es lo que se
    comprueba aquí anotando una recepción **con un importe grande**: si algún día
    la recepción de un puente dejara de anotarse con importe cero —hoy lo hace
    porque el mensaje de Circle no se decodifica—, el tope tiene que seguir sin
    consumirse.
    """
    ledger, _ = _ledger(tmp_path)
    entry = ledger.record_receipt(_receipt(), _intent(notional="1500"), kind=RECEIVE_KIND)

    assert entry.kind == RECEIVE_KIND
    assert entry.notional == "1500", "el importe se anota igual: es lo que entró"
    assert not entry.counts_towards_limits
    assert ledger.spent_today() == Decimal("0")
    assert len(ledger.entries()) == 1


def test_an_approval_is_written_down_but_does_not_consume_budget(tmp_path: Path) -> None:
    """Aprobar no mueve valor, sólo concede permiso: sumar lo autorizado contaría
    dos veces el mismo dinero —una al aprobarlo y otra al cambiarlo— y agotaría el
    tope a la mitad de lo que dice.

    El hecho queda, y con él el token, que es lo que hace que la aprobación salga
    en la «Actividad» del token al que se le dio el permiso.
    """
    ledger, _ = _ledger(tmp_path)
    entry = ledger.record_approval(
        _receipt(),
        pair="USDC/WETH",
        engine_id="zeroex",
        token_symbol="USDC",  # noqa: S106 - el nombre del argumento, no un secreto
        spender="0x2222222222222222222222222222222222222222",
        granted_raw=2**256 - 1,
    )

    assert entry.kind == APPROVAL_KIND
    assert entry.tokens == ("USDC",)
    assert entry.notional == "0"
    assert not entry.counts_towards_limits
    assert ledger.spent_today() == Decimal("0")
    assert [asiento.tx_hash for asiento in ledger.for_token("USDC")] == [entry.tx_hash]


@pytest.mark.parametrize("kind", [RECEIVE_KIND, APPROVAL_KIND, REDEEM_KIND])
def test_the_incoming_and_permission_kinds_never_consume_budget(kind: str) -> None:
    """El tipo manda sobre el desenlace: un `success` de estos no gasta tope."""
    entry = LedgerEntry(
        occurred_at=NOW,
        chain="base",
        pair="WETH/USDC",
        engine_id="zeroex",
        notional="1",
        notional_symbol="USDC",
        tx_hash="0x" + "ab" * 32,
        status="success",
        recipient="0x1111111111111111111111111111111111111111",
        description="",
        kind=kind,
    )
    assert not entry.counts_towards_limits


# --------------------------------------------------------------------------- #
# Los asientos de un token: lo que alimenta «Actividad»
# --------------------------------------------------------------------------- #
def _con_asiento(
    ledger: ExecutionLedger,
    *,
    symbol: str,
    chain: str = "base",
    kind: str = "transaction",
    pair: str | None = None,
    when: datetime = NOW,
    tx_hash: str = "0x" + "ab" * 32,
    tokens: tuple[str, ...] | None = None,
) -> LedgerEntry:
    """Un asiento anotado a mano, con los campos que mira el filtro por token."""
    entry = LedgerEntry(
        occurred_at=when,
        chain=chain,
        pair=pair if pair is not None else f"{symbol}/USDC",
        engine_id="zeroex",
        notional="1",
        notional_symbol="USDC",
        tx_hash=tx_hash,
        status="success",
        recipient="0x1111111111111111111111111111111111111111",
        description="",
        kind=kind,
        tokens=(symbol,) if tokens is None else tokens,
    )
    ledger.append(entry)
    return entry


def test_for_token_returns_only_that_token_and_newest_first(tmp_path: Path) -> None:
    """La lista de movimientos de un token: suyos, de su red, y lo último arriba.

    Es el filtro entero de «Actividad»: ni asientos de otro token ni los del
    mismo símbolo en otra red —el USDC de base no es el de polygon— y en el orden
    en que se lee una lista de movimientos, que es del más nuevo al más viejo.
    """
    ledger, _ = _ledger(tmp_path)
    viejo = _con_asiento(ledger, symbol="RARO", when=NOW - timedelta(hours=2))
    nuevo = _con_asiento(
        ledger, symbol="RARO", kind=REDEEM_KIND, when=NOW, tx_hash="0x" + "cd" * 32
    )
    _con_asiento(ledger, symbol="OTRO", tx_hash="0x" + "ef" * 32)
    otro_mismo_simbolo = _con_asiento(
        ledger,
        symbol="RARO",
        chain="polygon",
        when=NOW - timedelta(hours=1),
        tx_hash="0x" + "12" * 32,
    )

    # Sin red, el símbolo es el que manda: los tres RARO, lo último arriba.
    assert [entry.tx_hash for entry in ledger.for_token("RARO")] == [
        nuevo.tx_hash,
        otro_mismo_simbolo.tx_hash,
        viejo.tx_hash,
    ]
    # Y con red, el mismo símbolo en otra cadena no se cuela en su lista.
    assert [entry.tx_hash for entry in ledger.for_token("RARO", chain="polygon")] == [
        otro_mismo_simbolo.tx_hash
    ]


def test_for_token_reads_entries_written_before_the_tokens_field(tmp_path: Path) -> None:
    """Los renglones viejos se emparejan por palabra exacta del par.

    En el registro real de antes de este campo, el par es una frase —«WETH/USDC»—
    y el símbolo se busca como palabra entera: «USDC» encuentra «WETH/USDC», y
    «USDC.e» **no** —una subcadena diría que un movimiento de USDC.e es de USDC,
    y al revés—. Los dos pares de aquí salen del catálogo, que es de donde salen
    los reales.
    """
    ledger, _ = _ledger(tmp_path)
    plano = _con_asiento(ledger, symbol="", pair="WETH/USDC", tokens=())
    con_sufijo = _con_asiento(
        ledger, symbol="", pair="USDC.e/WETH", tokens=(), tx_hash="0x" + "cd" * 32
    )

    assert [entry.tx_hash for entry in ledger.for_token("USDC")] == [plano.tx_hash]
    assert [entry.tx_hash for entry in ledger.for_token("USDC.e")] == [
        con_sufijo.tx_hash
    ]


def test_the_tokens_of_an_entry_survive_a_restart(tmp_path: Path) -> None:
    """Los tokens del asiento se releen del disco.

    Es lo que hace que «Actividad» siga sabiendo de quién era cada movimiento
    después de cerrar la aplicación: el filtro por token no se apoya en el texto
    del par, que cambia, sino en el campo que el caso de uso escribió.
    """
    ledger, _ = _ledger(tmp_path)
    pair = _pair()
    ledger.record_receipt(_receipt(), _intent())

    (read,) = ledger.entries()
    assert read.tokens == (pair.base.symbol, pair.quote.symbol)
    assert ledger.for_token(pair.base.symbol) == (read,)
    assert ledger.for_token("NOEXISTE") == ()


def test_a_line_without_the_tokens_field_reads_as_empty(tmp_path: Path) -> None:
    """Un renglón de antes del campo no se descarta ni inventa tokens: se lee con
    la lista vacía y el filtro cae al texto del par."""
    ledger, _ = _ledger(tmp_path)
    antiguo = {
        "occurred_at": NOW.isoformat(),
        "chain": "base",
        "pair": "WETH/USDC",
        "engine_id": "zeroex",
        "notional": "1500",
        "notional_symbol": "USDC",
        "tx_hash": "0x" + "ab" * 32,
        "status": "success",
        "recipient": "0x1111111111111111111111111111111111111111",
    }
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(json.dumps(antiguo) + "\n", encoding="utf-8")

    (read,) = ledger.entries()
    assert read.tokens == ()
    assert ledger.for_token("USDC") == (read,)
