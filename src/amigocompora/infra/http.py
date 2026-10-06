"""Cliente HTTP con allowlist de hosts.

Mínimo privilegio aplicado a la red: un motor declara en su manifiesto los hosts
que necesita (`allowed_hosts`) y recibe un cliente que **sólo** puede hablar con
ellos. Una petición a cualquier otro host falla en el transporte, antes de abrir
la conexión.

Qué compra esto: un motor de terceros instalado desde PyPI no puede exfiltrar
los datos que ve a un host que no haya declarado, y lo que declara es visible en
el panel de motores antes de activarlo.
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping
from functools import cache
from typing import Final

import certifi
import httpx

from amigocompora import __version__
from amigocompora.domain.errors import AmigocomporaError

#: Hosts a los que se permite hablar sin TLS. Sólo desarrollo local.
LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})

USER_AGENT: Final = f"Amigocompora/{__version__}"

#: Límites conservadores: esta app hace muchas peticiones pequeñas a pocos
#: hosts, así que interesa reutilizar conexiones, no abrir cientos.
DEFAULT_LIMITS: Final = httpx.Limits(max_connections=20, max_keepalive_connections=10)


class HostNotAllowedError(AmigocomporaError):
    """El motor intentó hablar con un host que no declaró en su manifiesto."""


class InsecureTransportError(AmigocomporaError):
    """Se intentó enviar una petición sin cifrar a un host no local."""


def host_allowed(host: str | None, patterns: frozenset[str]) -> bool:
    """Coincidencia exacta, o comodín de subdominio (`*.example.com`).

    `*.example.com` cubre `api.example.com` y `example.com`, pero no
    `notexample.com`.
    """
    if not host:
        return False
    candidate = host.lower()
    for pattern in patterns:
        normalised = pattern.lower()
        if normalised.startswith("*."):
            suffix = normalised[2:]
            if candidate == suffix or candidate.endswith(f".{suffix}"):
                return True
        elif candidate == normalised:
            return True
    return False


class AllowlistTransport(httpx.AsyncBaseTransport):
    """Envuelve un transporte y rechaza lo que no esté en la allowlist."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        allowed_hosts: frozenset[str],
    ) -> None:
        self._inner = inner
        self._allowed = allowed_hosts

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host_allowed(host, self._allowed):
            allowed = ", ".join(sorted(self._allowed)) or "ninguno"
            raise HostNotAllowedError(
                f"petición a «{host}» bloqueada: el motor sólo declara {allowed}. "
                f"Si el host es legítimo, añádelo a allowed_hosts en su manifiesto."
            )
        if request.url.scheme != "https" and host not in LOCAL_HOSTS:
            raise InsecureTransportError(f"petición sin cifrar a «{host}» bloqueada: usa https.")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@cache
def build_ssl_context() -> ssl.SSLContext:
    """Contexto TLS de la aplicación. Se construye una vez y se reutiliza.

    Deliberadamente **no** usamos `ssl.create_default_context()`, por dos razones
    independientes, y las dos importan:

    1. **No volcamos claves de sesión.** `create_default_context()` lee la
       variable de entorno `SSLKEYLOGFILE` y, si existe, escribe en ese fichero
       las claves con las que se cifra cada conexión. Cualquiera que lo lea
       descifra todo nuestro tráfico, API keys incluidas. Hay antivirus y
       proxies que dejan esa variable puesta en el entorno del usuario sin que
       él lo sepa, así que aquí se ignora siempre: una herramienta que maneja
       credenciales no exporta el material con el que se protegen.
    2. **Confiamos en el almacén del sistema operativo.** `certifi` no conoce
       las CA corporativas ni las de los antivirus que interceptan TLS, y en
       Windows son habituales. Se cargan las dos fuentes: el almacén del SO
       (que es la decisión de confianza del administrador de la máquina) y el
       paquete `certifi` (por si el almacén viene escaso). La unión, no la
       sustitución — y `CERT_REQUIRED` con verificación de hostname en ambos
       casos: ampliar los emisores de confianza no es desactivar la validación.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    context.load_verify_locations(cafile=certifi.where())
    context.set_alpn_protocols(["h2", "http/1.1"])
    return context


def build_client(
    *,
    allowed_hosts: frozenset[str] | tuple[str, ...],
    timeout_seconds: float = 10.0,
    retries: int = 0,
    headers: Mapping[str, str] | None = None,
) -> httpx.AsyncClient:
    """Cliente asíncrono restringido a `allowed_hosts`.

    `retries` sólo cubre fallos de conexión. Los reintentos con sentido
    semántico (otro endpoint, backoff) los hace `RpcPool`, que sí sabe si la
    petición era idempotente.

    `headers` existe para las credenciales, y conviene decir por qué: muchas
    APIs aceptan su clave de dos formas, en la URL o en una cabecera, y no son
    equivalentes. Una clave en la URL acaba en los logs de cada proxy del camino,
    en el historial del navegador y en el log de peticiones de `httpx`, que
    imprime la línea de petición completa. En una cabecera no aparece en ninguno
    de esos sitios. Siempre que la API lo permita, la clave va aquí.
    """
    allowed = frozenset(allowed_hosts)
    if not allowed:
        raise HostNotAllowedError(
            "no se puede construir un cliente HTTP sin hosts permitidos: "
            "un motor sin red no debería pedir uno"
        )
    return httpx.AsyncClient(
        transport=AllowlistTransport(
            httpx.AsyncHTTPTransport(
                retries=retries,
                limits=DEFAULT_LIMITS,
                verify=build_ssl_context(),
            ),
            allowed,
        ),
        timeout=httpx.Timeout(timeout_seconds),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})},
        follow_redirects=False,
    )
