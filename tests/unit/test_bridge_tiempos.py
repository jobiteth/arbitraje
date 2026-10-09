"""Los tiempos de la modal: cada paso corre hasta su fin y luego se congela."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from amigocompora.app.usecases.track_bridge import TrackedBridge
from amigocompora.domain.money import TokenAmount
from amigocompora.ui.bridge_progress_dialog import (
    acortar,
    formatear_duracion,
    tiempo_total,
    tiempos,
    titulo_ventana,
)
from amigocompora.ui.pages.bridges import _cifra, _nota_corta

CREADO = "2026-10-09T10:00:00+00:00"
PROVEEDOR = "2026-10-09T10:00:10+00:00"
ATESTADO = "2026-10-09T10:00:40+00:00"
FINAL = "2026-10-09T10:02:00+00:00"


def _cruce(**campos: Any) -> TrackedBridge:
    base = TrackedBridge(
        tx_hash="0x" + "ab" * 32,
        origin_chain="polygon",
        destination_chain="base",
        amount_text="1 USDC",
        destination_symbol="USDC",
        destination_decimals=6,
        origin_status="success",
        state="pending",
        created_at=CREADO,
    )
    return replace(base, **campos)


def _ahora(hora: str) -> datetime:
    return datetime.fromisoformat(hora)


def test_formatear_duracion_en_segundos_minutos_y_horas() -> None:
    assert formatear_duracion(59) == "59 s"
    assert formatear_duracion(61) == "1 min 01 s"
    assert formatear_duracion(3725) == "1 h 02 min"


def test_el_paso_en_curso_cuenta_hasta_ahora() -> None:
    cruce = _cruce(provider_at=PROVEEDOR)

    textos = tiempos(cruce, _ahora("2026-10-09T10:01:10+00:00"))

    assert textos == ("—", "10 s", "1 min 00 s", "—")


def test_al_terminar_los_tiempos_quedan_congelados() -> None:
    cruce = _cruce(
        provider_at=PROVEEDOR,
        attested_at=ATESTADO,
        finished_at=FINAL,
        state="done",
    )

    temprano = tiempos(cruce, _ahora("2026-10-09T10:03:00+00:00"))
    tarde = tiempos(cruce, _ahora("2026-10-09T11:00:00+00:00"))

    assert temprano == ("—", "10 s", "30 s", "1 min 20 s")
    assert tarde == temprano
    assert tiempo_total(cruce) == "Tardó 2 min 00 s en total."


def test_sin_marcas_el_total_queda_vacio() -> None:
    assert tiempo_total(_cruce(provider_at=PROVEEDOR)) == ""


def test_un_hash_largo_se_acorta_con_inicio_y_final() -> None:
    hash_largo = "0xd5210" + "a" * 56 + "09A90"

    assert acortar(hash_largo) == "0xd5210...09A90"
    assert acortar("0x12") == "0x12"


def test_el_titulo_empieza_por_info_del_puente() -> None:
    assert titulo_ventana(_cruce()).startswith("Info del puente:")


def test_cifra_recorta_decimales_y_ceros_sobrantes() -> None:
    assert _cifra(TokenAmount(24989261201142690550602, 18, "POL")) == "24989.261201 POL"
    assert _cifra(TokenAmount(1_000_000, 6, "USDC")) == "1 USDC"
    assert _cifra(TokenAmount(1, 18, "POL")) == "<0.000001 POL"


def test_una_nota_larga_se_recorta_al_limite() -> None:
    corta = "mejor ruta de este motor"

    assert _nota_corta(corta) == corta
    assert _nota_corta("a" * 80) == "a" * 59 + "…"
    assert len(_nota_corta("a" * 80)) == 60
