"""Base común de los motores que leen de una API HTTP pública.

Los tres motores reales comparten el mismo esqueleto: abren un cliente
restringido a los hosts de su manifiesto, piden JSON, cachean la respuesta unos
segundos y traducen los fallos de red a errores del dominio. Eso vive aquí una
sola vez.

Tres decisiones que no son obvias:

- **Límite de ritmo propio.** Las APIs públicas gratuitas limitan las
  peticiones por minuto (GeckoTerminal, 30). Que el servidor nos corte con un
  429 y reintentemos es la forma más rápida de que nos bloquee: es más barato
  respetar un intervalo mínimo entre peticiones desde el cliente. El `Lock`
  además serializa las llamadas del motor, de modo que abrir tres pestañas a la
  vez no multiplica el ritmo.
- **La caché va antes del límite de ritmo.** Un acierto de caché no consume
  cuota ni espera. Con un TTL de pocos segundos, moverse por la interfaz es
  gratis y el dato sigue siendo fresco.
- **Dos clases de fallo.** `SourceUnavailableError` es reintentable (red, caída,
  429); `SourceResponseError` no lo es (la fuente cambió de formato). Mezclarlas
  haría que la UI ofreciera «reintentar» ante un fallo que nunca se va a
  arreglar reintentando.
- **Un 429 no tumba el barrido.** Medido de verdad: con 2,5 s entre peticiones,
  GeckoTerminal devolvió 429 en la séptima llamada seguida de un recorrido de 11
  redes, y ese único fallo abortaba las cuatro redes restantes. El intervalo
  mínimo reduce la probabilidad pero no la elimina —la cuota es del servidor y no
  la vemos—, así que el transporte reintenta un número acotado de veces
  respetando el `Retry-After` que pida la fuente. Lo que no hace es insistir
  indefinidamente: ver `MAX_RETRY_WAIT_SECONDS`.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Final

import httpx
import structlog

from amigocompora.domain.errors import (
    EngineNotReadyError,
    SourceResponseError,
    SourceUnavailableError,
)
from amigocompora.infra.cache import TtlCache
from amigocompora.infra.http import build_client

_log = structlog.get_logger(__name__)

#: Códigos en los que merece la pena reintentar más tarde (la fuente está
#: saturada o nos está limitando), frente a un 404 que es una respuesta válida.
RETRYABLE_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Espera máxima que se acepta antes de desistir y devolver el control.
#:
#: Si la fuente pide en `Retry-After` más que esto, no se duerme: se propaga el
#: error para que la interfaz pueda decir «la fuente nos está limitando» y
#: ofrecer reintentar. Una herramienta que se queda callada 60 segundos parece
#: colgada, y un dato de mercado que llega un minuto tarde ya no sirve.
MAX_RETRY_WAIT_SECONDS: Final = 8.0

#: Techo del intervalo adaptativo entre peticiones. Si hace falta más que esto,
#: la fuente no está limitando: está cerrada para nosotros, y conviene fallar y
#: decirlo antes que arrastrar la interfaz a quince segundos por petición.
MAX_INTERVAL_SECONDS: Final = 15.0

#: Factores del intervalo adaptativo: se duplica con cada 429 y baja un 20 % con
#: cada respuesta buena. La asimetría es el punto, no un descuido.
_INTERVAL_GROWTH: Final = 2.0
_INTERVAL_DECAY: Final = 0.8

_CacheKey = tuple[str, tuple[tuple[str, str], ...]]


class _Absent:
    """Centinela de «la fuente respondió que ahí no hay nada».

    Hace falta porque `TtlCache.get` ya devuelve `None` para un fallo de caché:
    guardar `None` como valor haría que una ausencia conocida se volviera a
    pedir en cada consulta, que es justo lo que la caché debe evitar. El
    centinela se queda dentro de este módulo; `get_json` lo traduce a `None`.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<absent>"


_ABSENT: Final = _Absent()


class _Throttled(Exception):  # noqa: N818
    """Interno: la fuente respondió con un código reintentable.

    No sale de este módulo. Existe para poder decidir si reintentar **con** el
    retardo que la propia fuente pide, antes de convertir el fallo en el error
    de dominio que verá la interfaz.
    """

    __slots__ = ("delay", "status")

    def __init__(self, status: int, delay: float | None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.delay = delay


def _retry_after(header: str | None) -> float | None:
    """Traduce la cabecera `Retry-After` a segundos, o `None` si no es legible.

    RFC 9110 admite dos formatos y las fuentes reales usan los dos: un número de
    segundos (`"30"`) o una fecha HTTP (`"Wed, 21 Oct 2026 07:28:00 GMT"`). La
    segunda forma se convierte a espera restante respecto a *ahora*, que es lo
    único accionable.

    Una cabecera ilegible devuelve `None` y no un error: que una fuente mande
    basura en una cabecera opcional no debe cambiar cómo se trata el fallo que la
    acompaña. Quien llama tiene su propio retardo por omisión.
    """
    if header is None:
        return None
    text = header.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    # `parsedate_to_datetime` devuelve naíf cuando la fecha trae `-0000`.
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class JsonSource:
    """Lector de JSON de una API pública, con caché y ritmo acotado.

    No es un motor: es la pieza que los motores usan por composición. Se
    mantiene ignorante del dominio a propósito —devuelve el JSON ya parseado—
    para que la traducción a entidades viva en cada motor, que es quien conoce
    el formato de su fuente.
    """

    __slots__ = (
        "_backoff",
        "_cache",
        "_client",
        "_floor_interval",
        "_headers",
        "_interval",
        "_last_request",
        "_lock",
        "_max_attempts",
        "_name",
        "_params",
        "_timeout",
    )

    def __init__(
        self,
        *,
        name: str,
        allowed_hosts: tuple[str, ...],
        ttl_seconds: float,
        timeout_seconds: float = 10.0,
        min_interval_seconds: float = 0.0,
        cache_maxsize: int = 128,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._name = name
        self._params = (allowed_hosts, timeout_seconds)
        self._timeout = timeout_seconds
        # Puede contener una credencial: no se expone ni se loguea.
        self._headers = dict(headers or {})
        self._client: httpx.AsyncClient | None = None
        self._cache: TtlCache[_CacheKey, Any] = TtlCache(ttl_seconds, maxsize=cache_maxsize)
        self._lock = asyncio.Lock()
        # El intervalo configurado es el **suelo**: nunca se va por debajo, pero
        # sube solo si la fuente nos limita. Ver `_fetch_with_retry`.
        self._floor_interval = max(0.0, min_interval_seconds)
        self._interval = self._floor_interval
        self._max_attempts = max(1, max_attempts)
        self._backoff = max(0.0, retry_backoff_seconds)
        # Reloj monótono: aquí se mide un intervalo, no un instante, y la hora
        # del sistema puede saltar hacia atrás.
        self._last_request = 0.0

    async def aopen(self) -> None:
        """Abre el cliente HTTP. Idempotente."""
        if self._client is None:
            allowed_hosts, timeout = self._params
            self._client = build_client(
                allowed_hosts=allowed_hosts,
                timeout_seconds=timeout,
                headers=self._headers,
            )

    async def aclose(self) -> None:
        """Cierra el cliente y vacía la caché. Idempotente y no lanza."""
        client, self._client = self._client, None
        self._cache.clear()
        if client is not None:
            try:
                await client.aclose()
            except Exception as error:
                _log.debug("source.close_failed", source=self._name, reason=str(error))

    @property
    def is_open(self) -> bool:
        return self._client is not None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise EngineNotReadyError(
                f"la fuente «{self._name}» no está abierta: falta llamar a aopen()"
            )
        return self._client

    async def _respect_rate_limit(self) -> None:
        if self._interval <= 0:
            return
        waited = time.monotonic() - self._last_request
        if waited < self._interval:
            await asyncio.sleep(self._interval - waited)

    def _widen_interval(self) -> None:
        """Sube el intervalo tras un 429: la fuente acaba de decir que sobramos."""
        grown = max(self._floor_interval, self._interval) * _INTERVAL_GROWTH
        self._interval = min(MAX_INTERVAL_SECONDS, grown or _INTERVAL_GROWTH)

    def _narrow_interval(self) -> None:
        """Baja el intervalo tras una respuesta buena, sin pasar del suelo.

        El `+ 0.1` del corte evita quedarse acercándose al suelo para siempre con
        una multiplicación que nunca llega: cuando ya está a menos de una décima,
        se fija en el suelo y se deja de pagar una espera que no sirve.
        """
        if self._interval <= self._floor_interval:
            return
        narrowed = self._interval * _INTERVAL_DECAY
        self._interval = (
            self._floor_interval if narrowed <= self._floor_interval + 0.1 else narrowed
        )

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        absent_statuses: frozenset[int] = frozenset(),
    ) -> Any:
        """Pide JSON, sirviéndolo de caché si sigue fresco.

        Devuelve `Any` porque cada fuente tiene su propia forma: validarla es
        trabajo del motor, que es quien sabe qué campos espera.

        `absent_statuses` declara los códigos que en **esta** fuente significan
        «no hay dato», y para los que se devuelve `None` en vez de lanzar. No es
        un parámetro de comodidad: hay APIs que contestan con un código de error
        a una petición perfectamente válida. El caso medido es Jupiter, que
        responde `400` con `errorCode: TOKEN_NOT_TRADABLE` cuando el par no tiene
        ruta — una respuesta informativa, no un formato roto. Sin esto, «ese par
        no se puede cotizar aquí» abortaría el barrido de la red entera.

        Sólo se consultan los códigos que el motor declara, y **después** de
        `RETRYABLE_STATUSES`: un 429 se reintenta aunque alguien lo declare
        ausente, porque ahí la fuente no está diciendo que no haya dato, está
        diciendo que pregunte luego.
        """
        key: _CacheKey = (url, tuple(sorted((params or {}).items())))
        cached = self._cache.get(key)
        if cached is not None:
            return None if cached is _ABSENT else cached

        client = self._require_client()
        async with self._lock:
            # Se vuelve a mirar dentro del lock: si varias corrutinas pidieron
            # lo mismo a la vez, sólo la primera gasta una petición.
            cached = self._cache.get(key)
            if cached is not None:
                return None if cached is _ABSENT else cached
            payload = await self._fetch_with_retry(client, url, params, absent_statuses)
            self._cache.set(key, payload)
            return None if payload is _ABSENT else payload

    async def post_json(
        self,
        url: str,
        *,
        json_body: Any,
        absent_statuses: frozenset[int] = frozenset(),
        cache_key: str | None = None,
    ) -> Any:
        """Envía un cuerpo JSON y devuelve la respuesta ya parseada.

        **Por omisión no usa la caché, a propósito.** Una respuesta a un POST
        depende de lo que se envió, y en el caso que motivó esto —el endpoint de
        swap de Jupiter— describe algo que además **caduca**: una transacción sin
        firmar vale hasta cierta altura de bloque. Servirla desde una caché con
        TTL sería devolver algo que ya no se puede emitir con la apariencia de
        estar recién pedido, que es la peor forma de fallar.

        `cache_key` abre la puerta a lo contrario para las fuentes que sí lo
        necesitan: hay APIs que sólo admiten POST para **leer** —la de Uniswap
        entre ellas, que cotiza con `POST /quote`—, y ahí no cachear significa
        gastar una petición de cuota cada vez que la interfaz repinta. Pasar una
        clave es afirmar dos cosas: que la respuesta depende sólo de esa clave, y
        que no caduca dentro del TTL. Quien no pueda afirmar las dos, no la pasa.

        Comparte con `get_json` todo lo demás: el intervalo mínimo, el lock que
        serializa las llamadas del motor, los reintentos acotados y la
        traducción de los fallos a errores del dominio.
        """
        client = self._require_client()

        if cache_key is None:
            async with self._lock:
                return await self._fetch_with_retry(client, url, None, absent_statuses, json_body)

        key: _CacheKey = (url, (("cache-key", cache_key),))
        cached = self._cache.get(key)
        if cached is not None:
            return None if cached is _ABSENT else cached

        async with self._lock:
            # Se vuelve a mirar dentro del lock: dos corrutinas que pidieron lo
            # mismo a la vez sólo deben gastar una petición.
            cached = self._cache.get(key)
            if cached is not None:
                return None if cached is _ABSENT else cached
            payload = await self._fetch_with_retry(client, url, None, absent_statuses, json_body)
            self._cache.set(key, payload)
            return None if payload is _ABSENT else payload

    async def _fetch_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, str] | None,
        absent_statuses: frozenset[int] = frozenset(),
        json_body: Any = None,
    ) -> Any:
        """Pide el JSON reintentando un número acotado de veces si la fuente limita.

        Cuatro reglas, todas salidas de medir contra la API real:

        1. **El `Retry-After` es un suelo, no la última palabra.** GeckoTerminal
           manda literalmente `retry-after: 0` con sus 429 —comprobado, con
           Cloudflare delante—, y obedecerlo al pie de la letra significa
           reintentar al instante y volver a fallar. Se respeta cuando pide más
           que nuestro backoff y se ignora cuando pide menos.
        2. **El intervalo aprende.** Lo que de verdad arregla un 429 es espaciar
           las peticiones siguientes, no reintentar mejor la que falló, porque
           estos límites son de ventana: medido, GeckoTerminal deja pasar 6
           peticiones y rechaza la séptima **sin importar el espaciado**. Así que
           cada 429 duplica el intervalo y cada respuesta buena lo baja un 20 %.
           Sube rápido y baja despacio a propósito: pasarse de lento cuesta
           segundos, pasarse de rápido cuesta el bloqueo.
        3. **La espera está acotada.** Más de `MAX_RETRY_WAIT_SECONDS` no se
           duerme: se devuelve el control con un error reintentable para que la
           interfaz lo diga y el usuario decida. Una ventana congelada medio
           minuto parece rota, y una cotización que llega tarde ya no vale.
        4. **El reloj del límite de ritmo se toca también al fallar.** Un 429
           consumió petición igual que un 200; si no se registrase, el reintento
           saldría inmediatamente y empeoraría exactamente el problema que
           estamos tratando.

        Corre dentro del lock de `get_json`, así que mientras uno espera no hay
        otra corrutina de este motor metiendo peticiones por debajo.
        """
        attempt = 0
        while True:
            attempt += 1
            await self._respect_rate_limit()
            try:
                payload = await self._fetch(client, url, params, absent_statuses, json_body)
            except _Throttled as throttled:
                self._last_request = time.monotonic()
                self._widen_interval()
                delay = max(throttled.delay or 0.0, self._backoff * attempt)
                if attempt >= self._max_attempts or delay > MAX_RETRY_WAIT_SECONDS:
                    raise SourceUnavailableError(
                        self._throttle_message(throttled, attempt)
                    ) from None
                _log.info(
                    "source.throttled",
                    source=self._name,
                    status=throttled.status,
                    attempt=attempt,
                    delay_seconds=round(delay, 2),
                    interval_seconds=round(self._interval, 2),
                )
                await asyncio.sleep(delay)
                continue
            self._last_request = time.monotonic()
            self._narrow_interval()
            return payload

    def _throttle_message(self, throttled: _Throttled, attempts: int) -> str:
        detail = (
            " La fuente está limitando el ritmo de peticiones; espera unos segundos."
            if throttled.status == 429
            else ""
        )
        tries = "" if attempts == 1 else f" tras {attempts} intentos"
        return f"«{self._name}» respondió {throttled.status}{tries}.{detail}"

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, str] | None,
        absent_statuses: frozenset[int] = frozenset(),
        json_body: Any = None,
    ) -> Any:
        try:
            response = (
                await client.post(url, json=json_body)
                if json_body is not None
                else await client.get(url, params=params)
            )
        except httpx.HTTPError as error:
            raise SourceUnavailableError(
                f"no se pudo consultar «{self._name}»: {type(error).__name__}. "
                f"Comprueba la conexión y vuelve a intentarlo."
            ) from error

        if response.status_code in RETRYABLE_STATUSES:
            raise _Throttled(
                response.status_code,
                _retry_after(response.headers.get("Retry-After")),
            )
        if response.status_code in absent_statuses:
            _log.debug(
                "source.absent",
                source=self._name,
                status=response.status_code,
            )
            return _ABSENT
        if response.status_code >= 400:
            raise SourceResponseError(
                f"«{self._name}» respondió {response.status_code} a una petición "
                f"que debería ser válida{self._rejection_hint(response.status_code)}."
                f"{_body_hint(response)}"
            )

        try:
            return response.json()
        except ValueError as error:
            raise SourceResponseError(f"«{self._name}» no devolvió JSON válido: {error}") from error

    @property
    def cache_hit_rate(self) -> float:
        return self._cache.stats.hit_rate

    def _rejection_hint(self, status: int) -> str:
        """La coletilla del rechazo, sin afirmar lo que no consta.

        Un 401 o un 403 cuando la petición llevaba credencial es casi siempre la
        credencial —rechazada, caducada o de otra cuenta—, y el mensaje de
        siempre («puede que su API haya cambiado») manda a buscar donde no está.
        Medido el 2026-10-10 con LI.FI: con una clave guardada inválida responde
        `401 Invalid API key`, y el motivo estaba en la credencial, no en la API.
        Sin credencial configurada no se puede afirmar nada de ninguna, así que se
        deja el mensaje de siempre.
        """
        if status in (401, 403) and self._credentials_configured:
            return (
                ", y llevaba la credencial del motor: comprueba que siga siendo "
                "válida —puede estar caducada, revocada o ser de otra cuenta— en "
                "Motores → Credenciales; puede que su API haya cambiado"
            )
        return "; puede que su API haya cambiado"

    @property
    def _credentials_configured(self) -> bool:
        """Si alguna cabecera propia tiene forma de credencial.

        Se mira el **nombre** y no el valor: el valor no se expone ni se registra
        nunca, y una cabecera con nombre de clave es lo único que hace falta para
        saber que un 401 puede ir por ahí.
        """
        return any(
            "api-key" in name.lower() or name.lower() == "authorization"
            for name in self._headers
        )


# --------------------------------------------------------------------------- #
# Lectura defensiva del JSON
# --------------------------------------------------------------------------- #
def _body_hint(response: httpx.Response, *, limit: int = 200) -> str:
    """Fragmento del cuerpo de un error, para que el mensaje sea accionable.

    Una API que rechaza una petición por un campo mal formado lo dice en el
    cuerpo —Jupiter responde `missing field 'userPublicKey'`—, y sin él quien
    depura se queda con un número y una suposición. Se acota y se colapsa el
    espacio en blanco porque esto acaba en una línea de log y en un `QLabel`.
    """
    try:
        text = " ".join(response.text.split())
    except Exception:
        # El cuerpo puede no ser texto legible; el mensaje no vale perder el
        # error original por intentar enriquecerlo.
        return ""
    if not text:
        return ""
    return f" Respuesta: {text[:limit]}{'…' if len(text) > limit else ''}"


def as_mapping(value: Any, field: str, source: str) -> dict[str, Any]:
    """Exige que `value` sea un objeto JSON, con un error que diga dónde falló."""
    if not isinstance(value, dict):
        raise SourceResponseError(
            f"«{source}»: se esperaba un objeto en «{field}», llegó {type(value).__name__}"
        )
    return value


def as_sequence(value: Any, field: str, source: str) -> list[Any]:
    """Exige que `value` sea una lista JSON."""
    if not isinstance(value, list):
        raise SourceResponseError(
            f"«{source}»: se esperaba una lista en «{field}», llegó {type(value).__name__}"
        )
    return value


def optional_decimal(value: Any) -> Decimal | None:
    """Convierte a `Decimal` desde texto o entero, o `None` si no se puede.

    Las APIs de este tipo devuelven los números **como cadenas** para no perder
    precisión en JSON, y a veces los omiten o los mandan vacíos. Devolver `None`
    en vez de lanzar permite al motor descartar ese pool y seguir con los demás:
    un campo ausente en un pool no debe tumbar la comparación completa.

    Nunca se pasa por `float`: `Decimal(str)` conserva los dígitos que la fuente
    publicó, mientras que `Decimal(float(...))` arrastraría el error binario.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Una fuente que manda un número JSON en coma flotante ya ha perdido
        # precisión; se reconstruye desde su repr más corta, que es lo máximo
        # recuperable.
        return Decimal(repr(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None
