"""Punto de entrada: `python -m amigocompora` y script `amigocompora`.

Arranca la interfaz de escritorio (PySide6 + qasync). Para uso sin UI
(tests, CLI, integración) se usa `amigocompora.app.container.build_container`.
"""

from __future__ import annotations

import sys


def main() -> int:
    """Arranca la interfaz gráfica.

    Importa Qt aquí dentro y no en la cabecera para que `python -m amigocompora
    --help` no pague el coste de cargar todo PySide6 si en el futuro se añaden
    subcomandos de consola.
    """
    from amigocompora.ui.app import main as run_ui

    return run_ui()


if __name__ == "__main__":
    sys.exit(main())
