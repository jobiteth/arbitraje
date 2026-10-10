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
    from amigocompora.infra.env_guard import drop_foreign_sslkeylogfile

    # Lo primero de todo, y antes de cualquier import que pueda crear un
    # contexto TLS: si Avast inyectó `SSLKEYLOGFILE` con una ruta de
    # dispositivo, el intérprete del venv aborta en cuanto se cree el primero.
    # Se hace aquí y no dentro de un motor porque el motor no es el culpable.
    retirado = drop_foreign_sslkeylogfile()
    if retirado is not None:
        import structlog

        structlog.get_logger().warning(
            "env.keylogfile_dropped",
            variable="SSLKEYLOGFILE",
            valor=retirado,
            motivo="ruta de dispositivo (Avast); el ssl de Python abortaría al crear "
            "un contexto TLS",
        )

    from amigocompora.ui.app import main as run_ui

    return run_ui()


if __name__ == "__main__":
    sys.exit(main())
