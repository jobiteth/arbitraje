"""Diario de errores: captura sólo avisos y fallos, con capacidad acotada."""

from __future__ import annotations

import pytest

from amigocompora.infra import error_journal
from amigocompora.infra.error_journal import DiarioErrores, capturar_errores


@pytest.fixture
def diario(monkeypatch: pytest.MonkeyPatch) -> DiarioErrores:
    nuevo = DiarioErrores()
    monkeypatch.setattr(error_journal, "DIARIO", nuevo)
    return nuevo


def test_registra_avisos_y_errores_y_deja_pasar_el_evento(diario: DiarioErrores) -> None:
    evento = {"event": "rpc_fallo", "level": "error", "endpoint": "https://nodo.test/…"}

    devuelto = capturar_errores(None, "error", evento)

    assert devuelto is evento
    entrada = diario.entradas()[0]
    assert entrada.nivel == "error"
    assert entrada.evento == "rpc_fallo"
    assert ("endpoint", "https://nodo.test/…") in entrada.detalles


def test_ignora_los_niveles_informativos(diario: DiarioErrores) -> None:
    capturar_errores(None, "info", {"event": "arranque", "level": "info"})
    capturar_errores(None, "debug", {"event": "detalle", "level": "debug"})

    assert diario.entradas() == ()


def test_convierte_valores_no_textuales_a_texto(diario: DiarioErrores) -> None:
    capturar_errores(None, "warning", {"event": "reintento", "level": "warning", "intentos": 3})

    assert ("intentos", "3") in diario.entradas()[0].detalles


def test_la_capacidad_descarta_las_entradas_mas_antiguas() -> None:
    pequeno = DiarioErrores(capacidad=2)
    for n in range(3):
        pequeno.registrar("error", f"fallo {n}", [])

    assert [e.evento for e in pequeno.entradas()] == ["fallo 1", "fallo 2"]


def test_vaciar_deja_el_diario_en_blanco(diario: DiarioErrores) -> None:
    diario.registrar("error", "fallo", [])

    diario.vaciar()

    assert diario.entradas() == ()
