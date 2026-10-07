"""La cuenta atrás de la columna «Cierra».

Es una función pura y por eso se prueba sola: la fecha absoluta que había antes
obligaba a restarla mentalmente contra el reloj, fila a fila, y con treinta
mercados eso no se hace — se lee la columna y se decide con lo que parezca. Lo
que se fija aquí son las fronteras entre unidades, que es donde una cuenta atrás
miente: decir «en 2 días» cuando faltan 47 horas adelanta el cierre, y decir «en 1
día» cuando faltan 25 es lo mismo con otro número.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from amigocompora.ui.pages.prediction import countdown

AHORA = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _en(delta: timedelta) -> str:
    return countdown(AHORA + delta, AHORA)


def test_a_market_without_a_close_date_says_nothing() -> None:
    """Un guion, no un cero: no es que cierre ya, es que no se sabe cuándo."""
    assert countdown(None, AHORA) == "—"


def test_minutes_while_there_is_less_than_an_hour() -> None:
    assert _en(timedelta(minutes=42)) == "en 42 min"
    assert _en(timedelta(seconds=59)) == "en 0 min"


def test_the_hour_boundary_switches_unit() -> None:
    assert _en(timedelta(minutes=59)) == "en 59 min"
    assert _en(timedelta(hours=1)) == "en 1 h"


def test_hours_while_there_are_less_than_two_days() -> None:
    assert _en(timedelta(hours=6)) == "en 6 h"
    assert _en(timedelta(hours=47)) == "en 47 h"


def test_the_two_day_boundary_switches_unit() -> None:
    """47 h son «47 h» y 48 h son «2 días»: no se adelanta ninguna de las dos."""
    assert _en(timedelta(hours=47, minutes=59)) == "en 47 h"
    assert _en(timedelta(hours=48)) == "en 2 días"


def test_days_from_there_on() -> None:
    assert _en(timedelta(days=3)) == "en 3 días"
    assert _en(timedelta(days=30)) == "en 30 días"


def test_it_truncates_instead_of_rounding() -> None:
    """Hacia abajo siempre: redondear hacia arriba adelantaría el cierre."""
    assert _en(timedelta(hours=47, minutes=30)) == "en 47 h"
    assert _en(timedelta(days=2, hours=20)) == "en 2 días"


def test_the_closing_instant_reads_as_closed() -> None:
    assert countdown(AHORA, AHORA) == "cerrado"


def test_a_past_date_reads_as_closed() -> None:
    """Y no como «en -3 h», que es lo que saldría de restar sin mirar el signo."""
    assert _en(timedelta(hours=-3)) == "cerrado"


def test_a_date_without_a_timezone_is_read_as_utc() -> None:
    """La fuente publica con zona; una fecha sin ella no puede tumbar la tabla.

    Pinta mal una fila si la suposición no vale, y por eso el motor descarta los
    mercados sin zona — pero la vista no es el sitio donde reventar: si el dato
    llega, se pinta.
    """
    sin_zona = datetime(2026, 6, 1, 18, 0)  # noqa: DTZ001 — es el caso a propósito
    assert countdown(sin_zona, AHORA) == "en 6 h"
