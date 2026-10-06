"""Firma y emisión de transacciones EVM.

La criptografía vive en `signer` (pura, sin red, verificable con vectores
publicados) y el trato con la red en `broadcast`. Están separados porque son
dos problemas distintos con dos formas distintas de fallar: un fallo de firma es
un bug que se puede reproducir sin conexión, y un fallo de emisión es el mundo.
"""

from __future__ import annotations
