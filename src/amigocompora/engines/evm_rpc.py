"""Lectura de la cadena por JSON-RPC público, para motores que construyen swaps.

### Por qué existe

Los motores que cotizan contra una API necesitan una clave y una cuota. Los que
hablan directamente con el contrato no: el nodo es público y la llamada es un
`eth_call`, que no cuesta gas porque no cambia nada. Eso es lo que permite que
la vía directa **no se detenga nunca** —ni por cuota agotada, ni por una clave
que caducó, ni porque el usuario no se haya registrado en ningún sitio.

Este módulo es esa pieza, una sola vez para todos los motores directos, en vez
de que cada uno reimplemente el failover.

### Qué NO hace, y por qué está escrito aquí

**No firma y no emite.** No hay ninguna función que acepte una clave privada, ni
`eth_sendRawTransaction`, ni `eth_sendTransaction` en la superficie que expone.
No es una omisión: es la propiedad que hace que un motor —que puede venir de un
paquete de terceros y declara sus propios hosts— no pueda mover dinero. Firmar
vive en `infra.evm`, y sólo lo alcanza el camino de ejecución, que pasa por la
barrera de modo y por el diálogo de confirmación.

Se apoya en lo que ya existe y está medido, en vez de repetirlo:

- `infra.rpc.pool.RpcPool` para el failover: ordena por salud y latencia, y
  distingue un nodo que **contesta con un error** (revert: no se reintenta en
  otro nodo, sería el mismo resultado) de uno que **no contesta** (ahí sí).
- `infra.rpc.defaults.fallback_endpoints` para los nodos públicos, medidos uno a
  uno contra la red y no copiados de una lista.
- `infra.http.build_client` para el cliente, que además impone la lista blanca
  de hosts que el motor declara en su manifiesto.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

import structlog
from httpx import AsyncClient

from amigocompora.domain.errors import SourceResponseError, SourceUnavailableError
from amigocompora.infra.http import build_client
from amigocompora.infra.rpc.defaults import DEFAULT_ENDPOINTS, fallback_endpoints
from amigocompora.infra.rpc.pool import (
    AllEndpointsFailedError,
    RpcEndpoint,
    RpcError,
    RpcPool,
)

_log = structlog.get_logger(__name__)

#: Tiempo máximo de una lectura. Corto a propósito: esto está en el camino de
#: pintar una cotización, y un nodo que tarda ocho segundos es un nodo que ya
#: ha perdido — para entonces el pool de respaldo ya respondió.
READ_TIMEOUT_SECONDS: Final = 8.0


def rpc_hosts(chain_keys: Iterable[str]) -> tuple[str, ...]:
    """Los hosts que hay que declarar en el manifiesto para leer esas redes.

    Se **deriva** de la tabla de nodos medidos en vez de escribirse aparte. Un
    motor que declare a mano una lista de hosts y lea de otra distinta falla en
    el transporte con un error que habla de permisos y no de la causa; aquí las
    dos salen del mismo sitio, así que no pueden separarse.
    """
    hosts = {
        endpoint.url.split("//", 1)[-1].split("/", 1)[0]
        for key in chain_keys
        for endpoint in DEFAULT_ENDPOINTS.get(key, ())
    }
    return tuple(sorted(hosts))


class ChainReader:
    """Un `RpcPool` por red, construido al vuelo y compartido.

    Perezoso por red y no por llamada: construir un pool cuesta poco, pero
    construirlo en cada cotización tiraría la salud acumulada —la media de
    latencia y el enfriamiento de los nodos caídos—, que es justo el estado por
    el que el failover mejora con el uso.

    La superficie es deliberadamente diminuta: `call` para una lectura cruda y
    `eth_call` para el caso que de verdad se usa. Todo lo demás —codificar,
    interpretar— es del motor, que es quien conoce su ABI.
    """

    __slots__ = ("_allowed", "_chains", "_client", "_clock", "_pools", "_timeout")

    def __init__(
        self,
        chain_keys: Iterable[str],
        *,
        allowed_hosts: Sequence[str] | None = None,
        clock: Any | None = None,
        timeout_seconds: float = READ_TIMEOUT_SECONDS,
    ) -> None:
        self._chains = tuple(dict.fromkeys(chain_keys))
        self._timeout = timeout_seconds
        self._clock = clock
        self._pools: dict[str, RpcPool] = {}
        self._client: AsyncClient | None = None
        # Vacío significa «sin lista blanca», que es lo correcto para un lector
        # interno: quien la impone es el cliente HTTP, y aquí sólo se reenvía.
        self._allowed = tuple(allowed_hosts or ())

    async def aopen(self) -> None:
        """Abre el cliente HTTP. Idempotente."""
        if self._client is None:
            self._client = build_client(
                allowed_hosts=self._allowed or rpc_hosts(self._chains),
                timeout_seconds=self._timeout,
            )

    async def aclose(self) -> None:
        """Cierra el cliente y los pools. Idempotente y no lanza."""
        client, self._client = self._client, None
        self._pools.clear()
        if client is not None:
            try:
                await client.aclose()
            except Exception as error:
                _log.debug("chain_reader.close_failed", reason=str(error))

    @property
    def is_open(self) -> bool:
        return self._client is not None

    def chains(self) -> tuple[str, ...]:
        return self._chains

    def _pool(self, chain_key: str) -> RpcPool:
        """El pool de esa red, creado la primera vez que se pide.

        Un pool sin endpoints no se construye: `RpcPool` lanza al hacerlo, y esa
        excepción —`AllEndpointsFailedError`— es la respuesta correcta, porque
        una red sin nodos medidos no se puede leer y decir lo contrario sería
        inventar. Se deja propagar con el mensaje del pool, que ya nombra la red.
        """
        pool = self._pools.get(chain_key)
        if pool is not None:
            return pool
        if self._client is None:
            raise SourceUnavailableError(
                "el lector de cadena no está abierto: falta llamar a aopen()"
            )
        pool = RpcPool(
            chain_key,
            fallback_endpoints(chain_key),
            self._client,
            clock=self._clock,
        )
        self._pools[chain_key] = pool
        return pool

    async def call(self, chain_key: str, method: str, params: Sequence[Any] = ()) -> Any:
        """Ejecuta una lectura, con failover entre nodos.

        Traduce los dos fallos a la distinción que le importa a quien llama, que
        es la misma que hace `RpcPool` por dentro:

        - El nodo **contestó** que no (`RpcError`: un revert —incluido el de un
          selector que ese contrato no tiene—, un parámetro inválido, un saldo
          insuficiente) → `SourceResponseError`. No se reintenta en otro nodo,
          porque el mismo estado da el mismo resultado; y **no** es una fuente
          caída, es una respuesta. Quien llama decide qué significa: para una
          lectura de identidad —comprobar qué contrato hay en una dirección— un
          revert es un fallo grave; para cotizar en un pool concreto, es
          sencillamente que ese pool no cotiza. Colapsar las dos cosas aquí
          obligaría a quien sabe la diferencia a adivinarla desde el mensaje.
          (Que el nodo no exponga el *método* JSON-RPC, en cambio, no llega
          hasta aquí: `RpcPool` lo trata como endpoint que no sirve y prueba el
          siguiente. Una cosa es que el contrato diga que no y otra que el nodo
          no sepa preguntar.)
        - **Ningún** nodo contestó (`AllEndpointsFailedError`) → sí es la fuente
          caída, y eso se dice con `SourceUnavailableError`.
        """
        try:
            return await self._pool(chain_key).call(method, params)
        except RpcError as error:
            raise SourceResponseError(
                f"«{chain_key}» respondió a {method} con un error: {error.rpc_message}"
            ) from error
        except AllEndpointsFailedError as error:
            raise SourceUnavailableError(
                f"ningún nodo público de «{chain_key}» respondió a {method}. "
                f"Comprueba la conexión y vuelve a intentarlo."
            ) from error

    async def eth_call(self, chain_key: str, to: str, data: str, *, block: str = "latest") -> str:
        """El caso que de verdad se usa: leer un contrato sin cambiar nada.

        Se devuelve el hexadecimal crudo y sin interpretar: la forma del retorno
        la conoce el motor, que es quien escribió el selector. Interpretarlo aquí
        exigiría que este módulo supiera de cada ABI.

        Un `eth_call` que revierte **no lanza aquí**: la cadena contesta con un
        error JSON-RPC y se traduce a `SourceResponseError`, que es un dato y no
        una caída. Se distingue de `SourceUnavailableError` —nadie contestó— para
        que el motor pueda tratarlos distinto, que es lo correcto: un revert al
        preguntar por un pool significa una cosa y un nodo mudo, otra.
        """
        raw = await self.call(chain_key, "eth_call", [{"to": to, "data": data}, block])
        if not isinstance(raw, str):
            raise SourceUnavailableError(
                f"«{chain_key}» devolvió {type(raw).__name__} donde se esperaba el "
                f"hexadecimal de un eth_call"
            )
        return raw


#: Reexportado para que un motor pueda tipar su tabla de nodos sin importar de
#: `infra.rpc.pool` directamente. Es el mismo objeto, no una copia.
Endpoint = RpcEndpoint

#: Vista de sólo lectura de los nodos por red, por la misma razón.
PUBLIC_NODES: Final[Mapping[str, tuple[RpcEndpoint, ...]]] = MappingProxyType(
    dict(DEFAULT_ENDPOINTS)
)
