"""La confirmación de una operación emitida: estado claro y enlace sólo si se conoce.

### Qué se fija aquí

Dos cosas que un usuario nota de inmediato. Que cada estado de la red diga algo
distinto —«revirtió» no puede parecer «confirmada», ni «pendiente» parecer un
fallo—. Y que el enlace al explorador aparezca únicamente para redes cuyo explorador
conocemos: inventar una URL para una red sin explorador daría un enlace que no lleva
a nada.
"""

from __future__ import annotations

from datetime import UTC, datetime

from amigocompora.domain.models import BroadcastStatus
from amigocompora.ui.receipt_dialog import _titulo_y_color, explorer_url

HASH = "0x" + "ab" * 32


def test_polygon_tiene_enlace_al_explorador() -> None:
    assert explorer_url("polygon", HASH) == f"https://polygonscan.com/tx/{HASH}"


def test_una_red_sin_explorador_conocido_no_inventa_enlace() -> None:
    assert explorer_url("unichain", HASH) is None


def test_cada_estado_tiene_su_titulo_propio() -> None:
    titulos = {status: _titulo_y_color(status)[0] for status in BroadcastStatus}
    distintos = len(set(titulos.values()))
    assert distintos == len(BroadcastStatus), "dos estados no pueden decir lo mismo"


def test_revertida_no_se_parece_a_confirmada() -> None:
    confirmada = _titulo_y_color(BroadcastStatus.SUCCESS)[0]
    revertida = _titulo_y_color(BroadcastStatus.REVERTED)[0]
    assert confirmada != revertida
    assert "revirtió" in revertida


def test_pendiente_avisa_de_que_el_dinero_ya_salio() -> None:
    _, explicacion, _ = _titulo_y_color(BroadcastStatus.PENDING)
    assert "ya salió" in explicacion


def test_la_hora_se_formatea_sin_zona_visible() -> None:
    from amigocompora.ui.receipt_dialog import _formatear

    texto = _formatear(datetime(2026, 1, 1, 12, 30, 5, tzinfo=UTC))
    assert len(texto) == 8
    assert texto.count(":") == 2
