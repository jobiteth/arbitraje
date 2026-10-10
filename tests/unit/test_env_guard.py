r"""El saneamiento del entorno: `SSLKEYLOGFILE` inyectado por Avast.

El valor real (`\\.\aswMonFltProxy\…`) aborta el intérprete de verdad al crear
un contexto TLS; las pruebas usan la misma **forma** de ruta —raíz de
dispositivo— con un nombre inventado, que en el peor caso falla con
`FileNotFoundError` en lugar de tumbar el proceso de pytest.
"""

from __future__ import annotations

import os
import ssl

import pytest

from amigocompora.infra.env_guard import drop_foreign_sslkeylogfile

RUTA_DE_DISPOSITIVO = "\\\\.\\aswMonFltProxy\\0454639831092eac"


def test_la_ruta_de_dispositivo_se_retira(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSLKEYLOGFILE", RUTA_DE_DISPOSITIVO)

    retirado = drop_foreign_sslkeylogfile()

    assert retirado == RUTA_DE_DISPOSITIVO
    assert "SSLKEYLOGFILE" not in os.environ


def test_tras_retirarla_un_contexto_tls_se_crea(monkeypatch: pytest.MonkeyPatch) -> None:
    """La prueba que importa: sin la variable ya no hay aborto al crear TLS."""
    monkeypatch.setenv("SSLKEYLOGFILE", RUTA_DE_DISPOSITIVO)
    drop_foreign_sslkeylogfile()

    ssl.create_default_context()


def test_la_ruta_normal_se_respeta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSLKEYLOGFILE", "C:\\temp\\keys.log")

    assert drop_foreign_sslkeylogfile() is None
    assert os.environ["SSLKEYLOGFILE"] == "C:\\temp\\keys.log"


def test_sin_variable_no_hay_nada_que_quitar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)

    assert drop_foreign_sslkeylogfile() is None
