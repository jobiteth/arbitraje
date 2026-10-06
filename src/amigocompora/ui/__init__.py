"""UI de Amigocompora (PySide6 + qasync).

Arquitectura:

- `MainWindow` orquesta `Container` (app) y `AlertCenter` / `Scheduler`.
- Cada pestaña es un `QWidget` autocontenido que recibe `Container` y no importa
  nada de `infra`. Así la UI es reemplazable (Tauri/Rust) sin tocar el motor.
- Toda I/O es `async` vía `qasync.QEventLoop`: ningún `time.sleep` ni `QThread`.
"""

from __future__ import annotations
