"""`RpcPool` — un grupo de endpoints JSON-RPC para una cadena, con failover.

La decisión de diseño que más importa aquí es **distinguir dos clases de fallo**:

- *Fallo de endpoint* (timeout, conexión rechazada, 5xx, 429, 401/403): el nodo
  no sirve. Se pasa al siguiente y se le apunta un fallo; tras varios
  consecutivos su cortacircuitos se abre y deja de intentarse durante un rato.
- *Error de aplicación JSON-RPC* (objeto `error` en la respuesta, p. ej.
  `execution reverted`): el nodo funciona perfectamente y nos está diciendo que
  nuestra petición es inválida. Reintentarla en otros cinco endpoints da el
  mismo error, cuesta tiempo y puede agotar cuotas. Falla rápido.

Confundir ambos es el bug clásico de estos pools: convierte un error de datos
en una tormenta de peticiones y marca como caídos nodos que están sanos.

Si todos los cortacircuitos están abiertos no se devuelve un fallo inmediato:
se sondea el endpoint cuyo enfriamiento acabe antes (*half-open*). Un apagón
total es peor que una petición lenta.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
import structlog

from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import AmigocomporaError
from amigocompora.infra.logging import safe_url

_log = structlog.get_logger(__name__)

#: Peso de la última medición en la media móvil de latencia. 0,3 reacciona a
#: una degradación en pocas peticiones sin oscilar con un pico aislado.
LATENCY_ALPHA: Final = 0.3

#: Códigos HTTP que se interpretan como «este endpoint no sirve ahora».
#: 401/403 incluidos: suele ser una API key mala *de ese proveedor*, así que
#: tiene sentido probar otro en vez de abortar.
ENDPOINT_FAULT_STATUSES: Final = frozenset({401, 403, 408, 429, 500, 502, 503, 504})


class RpcError(AmigocomporaError):
    """El nodo respondió con un error JSON-RPC. No se hace failover."""

    def __init__(self, code: int, message: str, method: str) -> None:
        self.code = code
        self.rpc_message = message
        self.method = method
        super().__init__(f"{method} devolvió error JSON-RPC {code}: {message}")


class RpcTransportError(AmigocomporaError):
    """Un endpoint concreto falló. Se intenta el siguiente."""


class AllEndpointsFailedError(AmigocomporaError):
    """Ningún endpoint de la cadena respondió."""


@dataclass(frozen=True, slots=True)
class RpcEndpoint:
    """Un nodo JSON-RPC. `priority` menor = se prefiere."""

    url: str
    label: str = ""
    priority: int = 100

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def display_name(self) -> str:
        return self.label or safe_url(self.url)


@dataclass(slots=True)
class EndpointHealth:
    """Salud observada de un endpoint. Mutable: es estado de ejecución."""

    consecutive_failures: int = 0
    open_until: datetime | None = None
    latency_ms: float | None = None
    calls: int = 0
    failures: int = 0
    last_error: str = ""

    def is_open(self, now: datetime) -> bool:
        """Si el cortacircuitos está abierto (el endpoint está en penitencia)."""
        return self.open_until is not None and now < self.open_until

    def record_success(self, latency_ms: float) -> None:
        self.calls += 1
        self.consecutive_failures = 0
        self.open_until = None
        self.last_error = ""
        self.latency_ms = (
            latency_ms
            if self.latency_ms is None
            else (LATENCY_ALPHA * latency_ms) + ((1 - LATENCY_ALPHA) * self.latency_ms)
        )

    def record_failure(
        self,
        now: datetime,
        reason: str,
        *,
        threshold: int,
        cooldown_seconds: float,
    ) -> None:
        self.calls += 1
        self.failures += 1
        self.consecutive_failures += 1
        self.last_error = reason
        if self.consecutive_failures >= threshold:
            self.open_until = now + timedelta(seconds=cooldown_seconds)


@dataclass(frozen=True, slots=True)
class EndpointStatus:
    """Instantánea de salud para mostrar en la UI."""

    endpoint: RpcEndpoint
    is_open: bool
    latency_ms: float | None
    consecutive_failures: int
    calls: int
    failures: int
    last_error: str

    @property
    def success_rate(self) -> float:
        return 1.0 if self.calls == 0 else (self.calls - self.failures) / self.calls


class RpcPool:
    """Endpoints de una cadena, con selección por salud y latencia."""

    __slots__ = (
        "_client",
        "_clock",
        "_cooldown_seconds",
        "_endpoints",
        "_failure_threshold",
        "_health",
        "_next_id",
        "chain",
    )

    def __init__(
        self,
        chain: str,
        endpoints: Sequence[RpcEndpoint],
        client: httpx.AsyncClient,
        *,
        clock: Clock | None = None,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
    ) -> None:
        if not endpoints:
            raise AllEndpointsFailedError(
                f"la red «{chain}» no tiene endpoints configurados. "
                f"Añádelos en config.toml antes de usarla."
            )
        self.chain = chain
        self._endpoints = tuple(endpoints)
        self._client = client
        self._clock = clock or SystemClock()
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._health: dict[str, EndpointHealth] = {
            endpoint.url: EndpointHealth() for endpoint in self._endpoints
        }
        self._next_id = 0

    # ------------------------------------------------------------ selección #
    def _ordered(self) -> tuple[RpcEndpoint, ...]:
        """Endpoints a intentar, en orden: cerrados primero, por prioridad y latencia."""
        now = self._clock.now()
        closed: list[RpcEndpoint] = []
        opened: list[RpcEndpoint] = []
        for endpoint in self._endpoints:
            (opened if self._health[endpoint.url].is_open(now) else closed).append(endpoint)

        closed.sort(key=lambda e: (e.priority, self._health[e.url].latency_ms or 0.0))
        # Si todo está abierto, se sondea el que menos tiempo le quede de
        # penitencia en vez de devolver un apagón total.
        opened.sort(key=lambda e: (self._health[e.url].open_until or now, e.priority))
        return tuple(closed + opened)

    def status(self) -> tuple[EndpointStatus, ...]:
        now = self._clock.now()
        return tuple(
            EndpointStatus(
                endpoint=endpoint,
                is_open=self._health[endpoint.url].is_open(now),
                latency_ms=self._health[endpoint.url].latency_ms,
                consecutive_failures=self._health[endpoint.url].consecutive_failures,
                calls=self._health[endpoint.url].calls,
                failures=self._health[endpoint.url].failures,
                last_error=self._health[endpoint.url].last_error,
            )
            for endpoint in self._endpoints
        )

    @property
    def is_degraded(self) -> bool:
        now = self._clock.now()
        return any(health.is_open(now) for health in self._health.values())

    # ---------------------------------------------------------------- call  #
    async def call(self, method: str, params: Sequence[Any] = ()) -> Any:
        """Invoca `method` en el primer endpoint que responda.

        Lanza `RpcError` si el nodo rechaza la petición (sin failover), o
        `AllEndpointsFailedError` si ninguno responde.
        """
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": list(params),
        }

        attempts: list[str] = []
        for endpoint in self._ordered():
            started = self._clock.now()
            try:
                result = await self._call_one(endpoint, payload, method)
            except RpcTransportError as error:
                reason = str(error)
                attempts.append(f"{endpoint.display_name}: {reason}")
                self._health[endpoint.url].record_failure(
                    self._clock.now(),
                    reason,
                    threshold=self._failure_threshold,
                    cooldown_seconds=self._cooldown_seconds,
                )
                _log.warning(
                    "rpc.endpoint_failed",
                    chain=self.chain,
                    endpoint=endpoint.url,
                    method=method,
                    reason=reason,
                )
                continue

            elapsed_ms = (self._clock.now() - started).total_seconds() * 1000
            self._health[endpoint.url].record_success(elapsed_ms)
            return result

        raise AllEndpointsFailedError(
            f"ningún endpoint de la red «{self.chain}» respondió a «{method}». "
            f"Intentos: " + " | ".join(attempts)
        )

    async def _call_one(
        self,
        endpoint: RpcEndpoint,
        payload: dict[str, Any],
        method: str,
    ) -> Any:
        try:
            response = await self._client.post(endpoint.url, json=payload)
        except httpx.HTTPError as error:
            raise RpcTransportError(f"{type(error).__name__}: {error}") from error

        if response.status_code in ENDPOINT_FAULT_STATUSES:
            raise RpcTransportError(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            # Un 4xx distinto no es un nodo caído: es nuestra petición. No se
            # reintenta en otros endpoints.
            raise RpcError(response.status_code, response.text[:200], method)

        try:
            body = response.json()
        except ValueError as error:
            raise RpcTransportError(f"respuesta no es JSON: {error}") from error

        if not isinstance(body, dict):
            raise RpcTransportError(f"respuesta JSON-RPC inesperada: {type(body).__name__}")

        if (error_obj := body.get("error")) is not None:
            code = error_obj.get("code", -1) if isinstance(error_obj, dict) else -1
            message = (
                error_obj.get("message", str(error_obj))
                if isinstance(error_obj, dict)
                else str(error_obj)
            )
            # Error de aplicación: el nodo está sano. Falla rápido.
            raise RpcError(int(code), str(message), method)

        if "result" not in body:
            raise RpcTransportError("respuesta JSON-RPC sin «result» ni «error»")
        return body["result"]


def jittered_delay(base_seconds: float, *, spread: float = 0.3) -> float:
    """Retardo con jitter, para que varios reintentos no se sincronicen.

    No es criptografía: aquí sólo hace falta que dos clientes no golpeen el
    mismo endpoint en el mismo milisegundo.
    """
    return base_seconds * (1 + random.uniform(-spread, spread))  # noqa: S311


@dataclass(frozen=True, slots=True)
class RpcRegistry:
    """Los pools de todas las redes configuradas, indexados por clave de red."""

    pools: dict[str, RpcPool] = field(default_factory=dict)

    def pool(self, chain_key: str) -> RpcPool:
        try:
            return self.pools[chain_key]
        except KeyError:
            known = ", ".join(sorted(self.pools)) or "ninguna"
            raise AllEndpointsFailedError(
                f"la red «{chain_key}» no está configurada; configuradas: {known}"
            ) from None

    def __contains__(self, chain_key: str) -> bool:
        return chain_key in self.pools
