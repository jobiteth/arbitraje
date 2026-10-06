"""Motores de referencia.

Son deterministas y no usan red, así que el *vertical slice* es ejecutable en
CI y reproducible. Sirven además de plantilla para los motores reales: lo que
hagan los de M2 es cambiar el origen de los datos, no la forma del contrato.
"""

from __future__ import annotations
