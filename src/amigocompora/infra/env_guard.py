r"""Saneamiento del entorno heredado, antes de que nada cree un contexto TLS.

Avast (Web Shield) inyecta `SSLKEYLOGFILE` en los procesos con una ruta de
dispositivo (`\\.\aswMonFltProxy\…`). El `ssl` de Python la lee al crear cada
contexto, y con esa ruta el intérprete del venv **aborta** con
`OPENSSL_Uplink(...): no OPENSSL_Applink` en cuanto un motor abre su primer
cliente HTTP. La variable existe para depurar TLS y una ruta de dispositivo no
es un destino que una persona elija para un fichero de claves, así que se
retira del entorno al arrancar; una ruta normal (`C:\…\keys.log`) se respeta,
que es alguien depurando a propósito.
"""

from __future__ import annotations

import os

#: Raíz del espacio de nombres de dispositivo de Windows (`\\.\`). Bajo esa
#: raíz no vive un fichero que alguien quiera como diario de claves.
DEVICE_PATH_PREFIX = "\\\\.\\"


def drop_foreign_sslkeylogfile() -> str | None:
    """Retira `SSLKEYLOGFILE` si apunta a una ruta de dispositivo.

    Devuelve el valor retirado —para poder avisarlo— o `None` si no había nada
    que retirar. No registra nada: quien llama decide si lo cuenta en el
    diario, y así se prueba sin montar el registro.
    """
    value = os.environ.get("SSLKEYLOGFILE")
    if value is None or not value.startswith(DEVICE_PATH_PREFIX):
        return None
    del os.environ["SSLKEYLOGFILE"]
    return value
