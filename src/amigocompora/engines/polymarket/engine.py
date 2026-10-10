"""Motor de mercados de predicción sobre la API pública de Polymarket.

Lee los mercados abiertos reales y sus precios reales. En un mercado de
predicción el precio de un resultado **es** su probabilidad implícita, así que
el análisis que pide el spec —probabilidades implícitas y discrepancias— se
calcula sobre el dato tal cual llega, sin transformarlo.

Dos cosas que conviene saber al leer la tabla que produce:

- La suma de probabilidades de un mercado binario de Polymarket sale casi
  siempre muy cerca de 100 %, porque la propia plataforma publica los dos
  precios como complementarios. Las desviaciones interesantes aparecen entre
  mercados del mismo evento (los `negRisk`, varios candidatos a lo mismo), no
  dentro de un mercado binario. El `overround_bps` del dominio mide lo primero;
  comparar entre mercados relacionados es trabajo del caso de uso.
- Se piden sólo mercados **abiertos** y ordenados por volumen. Un mercado
  cerrado tiene precios congelados en 0 y 1, y mezclarlos con los abiertos
  llenaría la tabla de probabilidades del 100 % que no son una predicción de
  nada. Cuando lo que se busca es **lo que está terminando**, el orden pasa a
  ser el del reloj y la ventana se filtra contra la fecha de cierre.
- El orden se puede pedir por nombre propio —`createdAt` para lo recién creado,
  `volume24hr` para lo que se mueve— y la lista se filtra por categoría
  (`tag_id`), pero las dos cosas se vuelven a aplicar aquí: pedirlas gasta el
  `limit` en lo que se quiere ver, y aplicarlas mantiene la lista correcta
  aunque la fuente ignore el parámetro. Las **etiquetas** —que la fuente publica
  en el evento, no en el mercado— se traen aparte, en una consulta de adorno que
  cuando falla cuesta la etiqueta, nunca la fila.

Sin API key: la API Gamma es pública y de sólo lectura.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from urllib.parse import quote

import structlog

from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import EngineNotFoundError, ExecutionError, InvalidAmountError
from amigocompora.domain.models import (
    DepthLevel,
    MarketDepth,
    MarketOutcome,
    MarketTag,
    PredictionMarket,
    PredictionOrder,
    PredictionPosition,
    PredictionSide,
    PredictionSort,
    SignedPredictionOrder,
    SubmittedPredictionOrder,
    Token,
    UnsignedTransaction,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import (
    EngineKind,
    EngineManifest,
    WalletChannelCredentials,
)
from amigocompora.engines.catalog import token_by_address
from amigocompora.engines.http_source import (
    JsonSource,
    as_mapping,
    as_sequence,
    optional_decimal,
)
from amigocompora.engines.polymarket.clob import ClobClient
from amigocompora.engines.polymarket.deposit_wallet import derive_beacon_deposit_wallet
from amigocompora.engines.polymarket.orders import (
    CHAIN_ID,
    CHAIN_KEY,
    COLLATERAL,
    CONDITIONAL_TOKENS,
    build_order,
    build_redeem_calldata,
    exchange_for,
    sign_order,
)
from amigocompora.engines.polymarket.relayer import (
    CONFIRMED_STATE,
    BuilderCredentials,
    Call,
    RelayerClient,
    RelayerKey,
    RelayerTransaction,
    sign_batch,
)
from amigocompora.infra.evm.broadcast import (
    build_approve_calldata,
    build_set_approval_for_all_calldata,
)

_log = structlog.get_logger(__name__)

SOURCE_NAME: Final = "Polymarket"
HOST: Final = "gamma-api.polymarket.com"
API_ROOT: Final = f"https://{HOST}"

#: El libro de órdenes vive en **otro** host: la API Gamma publica el catálogo de
#: mercados y la CLOB los libros y la negociación. Son dos servicios distintos y
#: se declaran los dos: el cliente HTTP del motor sólo puede hablar con los hosts
#: que su manifiesto nombra, y dejar éste fuera haría que leer un libro fallara en
#: el transporte con un mensaje que habla de la allowlist y no de la causa.
CLOB_HOST: Final = "clob.polymarket.com"
CLOB_ROOT: Final = f"https://{CLOB_HOST}"

#: Y un tercer servicio: el que publica **lo que tiene cada cartera**. No sale de
#: Gamma ni del CLOB, y no es una comodidad — es la única fuente de «cuántas
#: participaciones tengo». Deducirlo de las órdenes propias sería contar lo que se
#: pidió y no lo que se cruzó, y entre publicar y tener hay un cruce que ocurre en
#: el libro de otro. Va en el manifiesto por la misma razón que el CLOB: el
#: cliente HTTP sólo habla con los hosts declarados, y dejar éste fuera haría
#: fallar el transporte con un mensaje sobre la lista de permitidos en vez de
#: sobre la causa.
DATA_HOST: Final = "data-api.polymarket.com"
DATA_ROOT: Final = f"https://{DATA_HOST}"

#: Polymarket liquida en Polygon. La red va en el venue para que la UI pueda
#: decir en qué cadena vive lo que está mirando. Es la **clave** del registro de
#: redes, no un chain id numérico: ver `domain.chains`.
SETTLEMENT_CHAIN: Final = "polygon"

MANIFEST: Final = EngineManifest(
    engine_id="polymarket",
    name="Polymarket — mercados reales",
    version="1.0.0",
    kind=EngineKind.PREDICTION_MARKETS,
    summary=(
        "Mercados abiertos de Polymarket, con sus precios reales leídos de la API "
        "pública Gamma. El precio de cada resultado es su probabilidad implícita. "
        "Además de leerlos, construye y firma órdenes de compra y de venta contra "
        "el recinto de negociación."
    ),
    # `PREPARE_TX` es construir y firmar —lo que no toca el dinero—, y
    # `BROADCAST_TX` es publicar la orden en el recinto, que sí lo toca: una vez
    # publicada, otro puede cruzarla y no hay forma de desdecirse. Declarar la
    # segunda es lo que hace que `ModeGuard` exija modo EJECUCIÓN y que el
    # diálogo de confirmación marque la acción como irreversible. Un motor que
    # sólo leyera mercados no las declararía, y por eso siguen siendo la frontera
    # real entre mirar y operar.
    capabilities=frozenset(
        {Capability.READ_CHAIN, Capability.PREPARE_TX, Capability.BROADCAST_TX}
    ),
    required_config=(),
    allowed_hosts=(HOST, CLOB_HOST, DATA_HOST),
)

VENUE: Final = Venue(
    venue_id="polymarket",
    name="Polymarket",
    kind=VenueKind.PREDICTION_MARKET,
    chain=SETTLEMENT_CHAIN,
)

#: Liquidez mínima para que un mercado entre en la tabla. Un mercado sin
#: liquidez tiene un precio que no refleja ninguna opinión agregada: es el
#: último precio que alguien puso, y leerlo como probabilidad engaña.
MIN_LIQUIDITY_USD: Final = Decimal("1000")

#: Importe que el relayer de Polymarket **exige** al aprobar el colateral desde
#: la deposit wallet: medido contra el relayer, una aprobación por el importe
#: exacto se rechaza con «approve to exchange 0xE111… must be MaxUint256». Es
#: una política del canal, no una preferencia de este código, y por eso la capa
#: de aplicación no decide aquí: cumple, y lo dice al usuario con esas palabras.
_MAX_UINT256: Final = (1 << 256) - 1

#: Ventana de validez de un lote firmado para el relayer, en segundos. Un lote
#: caducado se rechaza, y renovarlo cuesta una firma: media hora es margen de
#: sobra para el viaje de una petición.
_WALLET_BATCH_DEADLINE_SECONDS: Final = 1800

#: Cuántos mercados pedir de más para compensar los que se descarten por
#: liquidez o por formato, y seguir devolviendo `limit` filas útiles.
_OVERFETCH: Final = 3
_MAX_FETCH: Final = 200

#: Cuántos eventos se piden por llamada al traer las etiquetas. Medido el
#: 2026-10-09: la fuente admite varios `id` repetidos en una consulta (la coma
#: no: «invalid integer»), y un evento entero con todos sus mercados llega a
#: pesar cientos de kilobytes —60 eventos trajeron 8,3 MB—, así que el lote se
#: queda corto a propósito: las etiquetas son adorno y no pueden costar más que
#: la lista que adornan.
_EVENT_BATCH: Final = 40


class PolymarketEngine:
    """Mercados de predicción reales, de sólo lectura."""

    __slots__ = ("_clock", "_relayer_poll_attempts", "_relayer_poll_seconds", "_source")

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        ttl_seconds: float = 20.0,
        timeout_seconds: float = 10.0,
        relayer_poll_seconds: float = 3.0,
        relayer_poll_attempts: int = 30,
    ) -> None:
        self._clock = clock or SystemClock()
        self._relayer_poll_seconds = relayer_poll_seconds
        self._relayer_poll_attempts = relayer_poll_attempts
        self._source = JsonSource(
            name=SOURCE_NAME,
            allowed_hosts=MANIFEST.allowed_hosts,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
            min_interval_seconds=0.3,
        )

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    async def aopen(self) -> None:
        await self._source.aopen()

    async def aclose(self) -> None:
        await self._source.aclose()

    # ----------------------------------------------------------------- #
    # Consultas
    # ----------------------------------------------------------------- #
    async def markets(
        self,
        *,
        limit: int = 20,
        search: str | None = None,
        closing_within: timedelta | None = None,
        category: MarketTag | None = None,
        sort: PredictionSort | None = None,
    ) -> Sequence[PredictionMarket]:
        """Mercados abiertos.

        Sin `closing_within` se ordenan por volumen: son los que tienen precios
        que significan algo. Con `closing_within` el orden pasa a ser el del
        reloj —lo que antes cierra, primero— y sólo entran los que cierran dentro
        de esa ventana, que es la vista de «lo que está terminando».

        `sort` pide el orden por nombre propio —`createdAt` para lo recién
        creado, `volume24hr` para lo que se mueve ahora— y `category` filtra por
        `tag_id`. Ese orden se pide a la fuente **y** se vuelve a aplicar aquí.
        Pedirlo hace que el `limit` se gaste en lo que se quiere ver; aplicarlo
        aquí hace que la lista sea correcta aunque la fuente ignore los
        parámetros, que es lo que se comprueba en las pruebas con una respuesta
        que no los respeta. Con la categoría vale lo mismo, con una salvedad:
        sólo se descarta en cliente un mercado del que **consta** que no es de
        la etiqueta —uno sin etiquetas se queda, porque no se puede afirmar que
        no la lleve—.

        El umbral de liquidez —el mismo con el que se descartan los mercados
        cuyo precio no significa nada— viaja además a la fuente
        (`liquidity_num_min`): sin él, la vista de lo recién creado se gasta el
        límite en mercados que todavía no tienen liquidez y se queda en cero
        filas, que es lo que se midió con datos reales el 2026-10-09.

        El filtro por texto se aplica en cliente sobre la pregunta del mercado:
        así el comportamiento del buscador no depende de parámetros de la API
        que no estén documentados, y es idéntico para cualquier fuente futura.
        """
        if limit <= 0:
            return ()

        needle = (search or "").strip().lower()
        # Con búsqueda hay que traer bastante más, porque el filtro cae después.
        requested = min(limit * (_OVERFETCH * 4 if needle else _OVERFETCH), _MAX_FETCH)

        now = self._clock.now()
        order, ascending = _order_params(closing_within, sort)
        params = {
            "closed": "false",
            "active": "true",
            "limit": str(requested),
            "order": order,
            "ascending": ascending,
            # El umbral de liquidez se pide **también** a la fuente. Medido el
            # 2026-10-09 con datos reales: los cien mercados más nuevos no
            # llegaban al umbral —recién creados, todavía sin liquidez— y la
            # vista «más nuevas» se quedaba en cero filas con el orden bien
            # pedido, porque el `limit` se gastaba en filas que se tiran aquí.
            # Pidiéndolo, llegan las más nuevas **entre las operables**.
            "liquidity_num_min": str(MIN_LIQUIDITY_USD),
        }
        if closing_within is not None:
            params["end_date_min"] = _iso(now)
            params["end_date_max"] = _iso(now + closing_within)
        if category is not None:
            # Medido el 2026-10-09: `/markets` filtra de verdad por `tag_id` (un
            # id inexistente devuelve lista vacía) y no entiende `tag_slug`.
            params["tag_id"] = category.tag_id

        payload = await self._source.get_json(f"{API_ROOT}/markets", params=params)

        selected: list[tuple[Mapping[str, Any], PredictionMarket]] = []
        for raw in as_sequence(payload, "markets", SOURCE_NAME):
            entry = raw if isinstance(raw, dict) else {}
            if needle and needle not in str(entry.get("question") or "").lower():
                continue
            market = self._to_market(entry, now)
            if market is None:
                continue
            if closing_within is not None:
                remaining = market.time_left(now)
                if remaining is None or remaining > closing_within:
                    continue
            selected.append((entry, market))
            if len(selected) >= limit:
                break

        tag_map = await self._event_tags(entry for entry, _ in selected)
        found = [
            replace(market, tags=_merge_event_tags(entry, tag_map))
            for entry, market in selected
        ]
        if category is not None:
            found = [
                market
                for market in found
                if not market.tags
                or any(tag.slug == category.slug for tag in market.tags)
            ]
        if sort is PredictionSort.NEWEST:
            found.sort(key=_created_key, reverse=True)
        elif sort is PredictionSort.TRENDING:
            found.sort(key=_volume24h_key, reverse=True)
        return tuple(found)

    async def market(self, market_id: str) -> PredictionMarket:
        payload = as_mapping(
            await self._source.get_json(f"{API_ROOT}/markets/{market_id}"),
            "market",
            SOURCE_NAME,
        )
        market = self._to_market(payload, self._clock.now())
        if market is None:
            raise EngineNotFoundError(
                f"el mercado «{market_id}» existe en {SOURCE_NAME} pero no se "
                f"puede leer como mercado de predicción: puede estar cerrado, "
                f"sin liquidez o sin precios publicados."
            )
        return await self._with_event_tags(payload, market)

    async def market_by_condition(self, condition_id: str) -> PredictionMarket:
        """El mercado de una posición, por el `conditionId` de su contrato.

        Es la vuelta atrás para operar sobre una posición: la posición publica
        el contrato y su resultado, no el número del mercado, y la tarjeta de
        orden necesita el mercado entero —su tick, su tamaño mínimo—. Gamma
        filtra por `condition_ids` y devuelve una lista (medido el 2026-10-09
        contra el mercado 665374: una fila, el mercado correcto).

        Si el mercado existe pero ya no se puede operar —resuelto, cerrado,
        sin precios—, `_to_market` lo descarta y se lanza el error con el
        motivo: quien llama esto necesita operar, no mirar.
        """
        payload = await self._source.get_json(
            f"{API_ROOT}/markets",
            params={"condition_ids": condition_id},
        )
        now = self._clock.now()
        for raw in as_sequence(payload, "markets", SOURCE_NAME):
            entry = raw if isinstance(raw, dict) else {}
            market = self._to_market(entry, now)
            if market is not None:
                return await self._with_event_tags(entry, market)
        raise EngineNotFoundError(
            f"el mercado con condición «{condition_id}» no está en {SOURCE_NAME} "
            f"como mercado operativo: puede estar cerrado, resuelto o sin "
            f"liquidez publicada."
        )

    async def _with_event_tags(
        self, entry: Mapping[str, Any], market: PredictionMarket
    ) -> PredictionMarket:
        """El mismo mercado con las etiquetas de sus eventos.

        Para un mercado suelto —el que se abre desde una posición, por ejemplo—
        es una llamada más; para la lista, `markets` pide todos los eventos de
        una vez. El modelo es inmutable, así que se reasigna con `replace`.
        """
        tag_map = await self._event_tags((entry,))
        return replace(market, tags=_merge_event_tags(entry, tag_map))

    async def _event_tags(
        self, entries: Iterable[Mapping[str, Any]]
    ) -> dict[str, tuple[MarketTag, ...]]:
        """Las etiquetas de los eventos de estos mercados, por id de evento.

        Hace falta esta llamada porque la fuente no publica las categorías en el
        mercado sino en el **evento** que lo contiene (medido el 2026-10-09), y
        el evento embebido en el mercado llega sin ellas. Es una lectura de
        adorno: si un lote falla, se devuelve lo que haya y se registra el aviso
        —una etiqueta que falta cuesta menos que una lista que no llega—.

        Se piden varios eventos por llamada con `id` repetido —la coma la
        rechaza la fuente— y el `limit` va explícito, porque el de por omisión no
        está medido. La consulta va cosida en la URL porque un diccionario de
        `params` no puede expresar una clave repetida.
        """
        ids: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            for event_id in _event_ids(entry):
                if event_id not in seen:
                    seen.add(event_id)
                    ids.append(event_id)

        tags: dict[str, tuple[MarketTag, ...]] = {}
        for start in range(0, len(ids), _EVENT_BATCH):
            batch = ids[start : start + _EVENT_BATCH]
            query = "&".join(f"id={quote(event_id, safe='')}" for event_id in batch)
            try:
                payload = await self._source.get_json(
                    f"{API_ROOT}/events?{query}&limit={len(batch)}"
                )
                events = as_sequence(payload, "events", SOURCE_NAME)
            except Exception as error:
                # Deliberadamente ancho: cualquier fallo de este adorno —red,
                # formato, un lote que la fuente rechaza— se queda en un aviso y
                # la lista sigue sin etiquetas. Lo que no puede pasar es que se
                # caiga la búsqueda entera por una consulta que sólo decora.
                _log.warning(
                    "polymarket.event_tags_failed", events=len(batch), reason=str(error)
                )
                continue
            for raw in events:
                if not isinstance(raw, dict):
                    continue
                # Nombre distinto del `event_id` de arriba: aquí es `str | None`
                # —la respuesta puede venir sin id— y allí siempre es `str`.
                read_id = _id_text(raw.get("id"))
                if read_id is not None:
                    tags[read_id] = _to_tags(raw.get("tags"))
        return tags

    # ----------------------------------------------------------------- #
    # Traducción
    # ----------------------------------------------------------------- #
    def _to_market(
        self,
        entry: Mapping[str, Any],
        observed_at: datetime,
    ) -> PredictionMarket | None:
        """Traduce un mercado, o `None` si no es utilizable.

        Un mercado descartado se registra en el log pero no interrumpe la
        lectura de los demás: con datos reales siempre hay filas raras, y la
        tabla debe mostrar las buenas en vez de caerse por una mala.
        """
        market_id = str(entry.get("id") or "").strip()
        question = str(entry.get("question") or "").strip()
        if not market_id or not question:
            return None
        if entry.get("closed") is True or entry.get("active") is False:
            return None

        liquidity = optional_decimal(entry.get("liquidityNum"))
        if liquidity is None or liquidity < MIN_LIQUIDITY_USD:
            return None

        outcomes = self._to_outcomes(entry, market_id)
        if outcomes is None:
            return None

        try:
            return PredictionMarket(
                market_id=market_id,
                venue=VENUE,
                question=question,
                outcomes=outcomes,
                observed_at=observed_at,
                closes_at=_parse_moment(entry.get("endDate")),
                condition_id=_optional_text(entry.get("conditionId")),
                neg_risk=_optional_bool(entry.get("negRisk")),
                tick_size=optional_decimal(entry.get("orderPriceMinTickSize")),
                min_order_size=optional_decimal(entry.get("orderMinSize")),
                created_at=_parse_moment(entry.get("createdAt")),
                volume=_non_negative(entry.get("volumeNum")),
                volume_24h=_non_negative(entry.get("volume24hr")),
                # La liquidez ya se leyó arriba para descartar el mercado; se
                # reutiliza la misma cifra en vez de volver a leerla.
                liquidity=liquidity,
                spread=_non_negative(entry.get("spread")),
                price_change_1h=_price_delta(entry.get("oneHourPriceChange")),
                price_change_24h=_price_delta(entry.get("oneDayPriceChange")),
                price_change_1w=_price_delta(entry.get("oneWeekPriceChange")),
            )
        except Exception as error:
            _log.debug("polymarket.market_skipped", market_id=market_id, reason=str(error))
            return None

    def _to_outcomes(
        self,
        entry: Mapping[str, Any],
        market_id: str,
    ) -> tuple[MarketOutcome, ...] | None:
        labels = _decode_json_array(entry.get("outcomes"))
        prices = _decode_json_array(entry.get("outcomePrices"))
        if labels is None or prices is None or len(labels) != len(prices):
            _log.debug("polymarket.outcomes_unreadable", market_id=market_id)
            return None
        if len(labels) < 2:
            return None

        # Los identificadores operables vienen en una lista **paralela** a la de
        # las etiquetas, con el mismo orden y la misma longitud. Se alinean por
        # índice, que es la única relación que las une —no hay ninguna clave
        # compartida—, así que si las longitudes no cuadran se descarta el
        # mercado entero: emparejar por posición dos listas de distinto tamaño
        # asignaría a un resultado el identificador de otro, y eso es comprar lo
        # contrario de lo que se cree.
        token_ids = _decode_json_array(entry.get("clobTokenIds"))
        if token_ids is not None and len(token_ids) != len(labels):
            _log.debug(
                "polymarket.token_ids_misaligned",
                market_id=market_id,
                labels=len(labels),
                token_ids=len(token_ids),
            )
            token_ids = None

        outcomes: list[MarketOutcome] = []
        seen: set[str] = set()
        for index, (label, raw_price) in enumerate(zip(labels, prices, strict=True)):
            text = str(label).strip()
            price = optional_decimal(raw_price)
            if not text or price is None or not (Decimal(0) <= price <= Decimal(1)):
                return None
            if text in seen:
                # Etiquetas repetidas: el dominio lo rechazaría, y aquí se sabe
                # de qué mercado viene, así que se descarta con contexto.
                _log.debug("polymarket.duplicate_outcome", market_id=market_id, label=text)
                return None
            seen.add(text)
            outcomes.append(
                MarketOutcome(label=text, price=price, token_id=_token_id(token_ids, index))
            )
        return tuple(outcomes)

    # ----------------------------------------------------------------- #
    # Operar: construir, firmar y publicar
    # ----------------------------------------------------------------- #
    def build_order(
        self,
        market: PredictionMarket,
        *,
        outcome_label: str,
        side: PredictionSide,
        size: Decimal,
        price: Decimal,
    ) -> PredictionOrder:
        """Construye la orden. Sin red y sin claves: se puede enseñar antes de firmar."""
        return build_order(
            market, outcome_label=outcome_label, side=side, size=size, price=price
        )

    def sign_order(
        self,
        order: PredictionOrder,
        *,
        private_key: str,
        wallet: str | None = None,
    ) -> SignedPredictionOrder:
        return sign_order(order, private_key=private_key, wallet=wallet)

    async def submit_order(
        self, signed: SignedPredictionOrder, *, private_key: str
    ) -> SubmittedPredictionOrder:
        """Publica la orden. Es el único paso que la saca de esta máquina.

        El cliente se construye y se cierra **dentro de la llamada**, con la clave
        prestada, en vez de guardarse en el motor. Un motor vive lo que vive la
        aplicación y lo comparten todas las peticiones: dejarle una clave dentro
        sería dejarla viva hasta que se cierre la ventana, y este objeto no tiene
        ninguna razón para conocerla entre orden y orden.
        """
        cliente = ClobClient(private_key)
        try:
            return await cliente.submit_order(signed)
        finally:
            await cliente.aclose()

    # ----------------------------------------------------------------- #
    # Deposit wallet: el canal con el que el recinto admite operar
    # ----------------------------------------------------------------- #
    def settlement_wallet(self, owner: str) -> str:
        """La deposit wallet de `owner`. Puro: se deriva, no se pregunta.

        La dirección sale del dueño, de la factoría y del beacon, así que se
        conoce **antes** de desplegarla y se puede enseñar sin tocar la red. El
        relayer es la fuente de verdad al desplegar: si confirma una dirección
        distinta de la derivada, `deploy_settlement_wallet` lo dice en vez de
        seguir con la calculada.
        """
        return derive_beacon_deposit_wallet(owner)

    async def deploy_settlement_wallet(
        self, *, owner: str, credentials: WalletChannelCredentials
    ) -> str:
        """Despliega la deposit wallet de `owner` por el relayer y espera a que confirme.

        El despliegue lo paga el relayer —no cuesta gas— y lo autentica la
        Builder key, que es la única que ese endpoint admite. Se espera a la
        confirmación en vez de devolver el identificador: la operación siguiente
        opera **contra esa wallet**, y devolver antes la dejaría apuntando a una
        dirección sin código. Y se contrasta lo que el relayer confirma con lo
        derivado: si no coinciden manda el relayer, y seguir con la calculada
        mandaría fondos a otro sitio.
        """
        derivada = self.settlement_wallet(owner)
        cliente = _relayer_client(credentials)
        try:
            envio = await cliente.submit_wallet_create(owner)
            _log.info("polymarket.wallet_deploying", transaction_id=envio.transaction_id)
            ficha = await self._wait_for_relayer(
                lambda: cliente.wallet_create_status(envio.transaction_id, owner=owner),
                what="el despliegue de la deposit wallet",
                transaction_id=envio.transaction_id,
            )
        finally:
            await cliente.aclose()
        proxy = ficha.proxy_address
        if proxy is None or proxy.lower() != derivada.lower():
            raise ExecutionError(
                f"el relayer confirmó la deposit wallet en "
                f"«{proxy or 'una dirección sin publicar'}» y la derivada para esta "
                f"cuenta es «{derivada}». El relayer es la fuente de verdad: no se "
                f"opera con una dirección que no coincide con la suya."
            )
        _log.info("polymarket.wallet_deployed", wallet=derivada)
        return derivada

    async def approve_wallet_collateral(
        self,
        *,
        owner: str,
        collateral: Token,
        spender: str,
        private_key: str,
        credentials: WalletChannelCredentials,
    ) -> str:
        """Aprueba el colateral al recinto desde la deposit wallet, por el relayer.

        El importe es `MaxUint256` porque el relayer lo exige (ver
        `_MAX_UINT256`): en este canal el permiso queda sin tope, y eso no es
        una elección de este código sino una condición del recinto. Quien se lo
        enseñe al usuario tiene que decirlo con esas palabras.
        """
        if collateral.address is None:
            raise ExecutionError(
                f"el colateral «{collateral.symbol}» no tiene dirección en "
                f"«{CHAIN_KEY}»: no hay contrato al que concederle el permiso."
            )
        return await self._submit_wallet_calls(
            owner=owner,
            calls=(
                Call(
                    target=collateral.address,
                    value=0,
                    data=build_approve_calldata(spender, _MAX_UINT256),
                ),
            ),
            private_key=private_key,
            credentials=credentials,
            what=f"el permiso de {collateral.symbol} al recinto",
        )

    async def approve_wallet_shares(
        self,
        *,
        owner: str,
        collection: str,
        spender: str,
        private_key: str,
        credentials: WalletChannelCredentials,
    ) -> str:
        """Autoriza la colección de participaciones desde la deposit wallet.

        Mismo canal que el permiso del colateral, con el permiso **sin importe**
        del estándar ERC-1155: `setApprovalForAll` alcanza a todas las
        participaciones de esa colección, no sólo a las de una orden.
        """
        return await self._submit_wallet_calls(
            owner=owner,
            calls=(
                Call(
                    target=collection,
                    value=0,
                    data=build_set_approval_for_all_calldata(spender, approved=True),
                ),
            ),
            private_key=private_key,
            credentials=credentials,
            what="la autorización de las participaciones al recinto",
        )

    async def _submit_wallet_calls(
        self,
        *,
        owner: str,
        calls: Sequence[Call],
        private_key: str,
        credentials: WalletChannelCredentials,
        what: str,
    ) -> str:
        """Firma un lote con la clave del dueño, lo envía y espera su confirmación.

        La clave entra por parámetro, se usa para firmar el lote y se suelta:
        el motor no la guarda entre llamadas. Devuelve el identificador del
        relayer, que **no** es un hash de cadena — con él se consulta el estado,
        y el asiento del registro no lo confunde con una transacción.
        """
        wallet = self.settlement_wallet(owner)
        cliente = _relayer_client(credentials)
        try:
            nonce = await cliente.wallet_nonce(owner)
            deadline = int(self._clock.now().timestamp()) + _WALLET_BATCH_DEADLINE_SECONDS
            firma = sign_batch(
                private_key,
                chain_id=CHAIN_ID,
                wallet=wallet,
                nonce=nonce,
                deadline=deadline,
                calls=calls,
            )
            envio = await cliente.submit_wallet_batch(
                owner=owner,
                wallet=wallet,
                nonce=nonce,
                deadline=deadline,
                calls=calls,
                signature=firma,
            )
            _log.info(
                "polymarket.wallet_batch_submitted",
                transaction_id=envio.transaction_id,
                what=what,
            )
            await self._wait_for_relayer(
                lambda: cliente.batch_status(envio.transaction_id, owner=owner),
                what=what,
                transaction_id=envio.transaction_id,
            )
        finally:
            await cliente.aclose()
        return envio.transaction_id

    async def _wait_for_relayer(
        self,
        consulta: Callable[[], Awaitable[RelayerTransaction]],
        *,
        what: str,
        transaction_id: str,
    ) -> RelayerTransaction:
        """Sondea la transacción del relayer hasta su estado terminal.

        Sólo devuelve cuando el estado es el confirmado. Agotar el tiempo sin
        confirmación se dice con un error y **no se sigue**: operar contra una
        wallet a medio desplegar o sin permiso produce una orden que el recinto
        rechaza, y el rechazo llegaría con un mensaje que habla de saldos.
        """
        ficha: RelayerTransaction | None = None
        for intento in range(self._relayer_poll_attempts):
            if intento:
                await asyncio.sleep(self._relayer_poll_seconds)
            ficha = await consulta()
            if ficha.is_terminal:
                break
        if ficha is None or ficha.state != CONFIRMED_STATE:
            estado = ficha.state if ficha is not None else "sin respuesta"
            raise ExecutionError(
                f"{what} no llegó a confirmar en el relayer ({estado}, "
                f"{transaction_id}). No se sigue: sin eso el recinto rechazaría "
                f"la orden."
            )
        return ficha

    def collateral_for(self, market: PredictionMarket) -> Token:
        """El USDC **puenteado** de Polygon, que es el colateral del recinto.

        No es el USDC nativo de la red, y los dos publican `symbol() == "USDC"`.
        Se distinguen por dirección, y por eso esto devuelve el token del catálogo
        construido sobre la dirección medida y no un token buscado por símbolo:
        `token_by_symbol("USDC", "polygon")` devuelve el **nativo**, y aprobarle
        el permiso al recinto sería no aprobar nada — la orden se rechazaría por
        saldo, con un mensaje que habla de fondos cuando el problema es otro.

        Delega en `collateral_on` porque el cobro necesita el mismo token y no
        tiene un mercado entre manos, sólo una posición. Duplicar la búsqueda
        sería tener dos sitios donde el colateral del recinto podría pasar a ser
        otro.
        """
        return self.collateral_on(market.venue.chain)

    def collateral_on(self, chain_key: str) -> Token:
        """El colateral del recinto en esa red, leído del catálogo por dirección."""
        colateral = token_by_address(COLLATERAL, chain_key)
        if colateral is None:
            raise ExecutionError(
                f"la dirección del colateral del recinto ({COLLATERAL}) no está en "
                f"el catálogo de «{chain_key}», así que no se sabe con qué "
                f"decimales medir el importe. Sin decimales, el importe que se "
                f"firma lleva la coma en el sitio equivocado."
            )
        return colateral

    def shares_collection_for(self, market: PredictionMarket) -> str:
        """El contrato condicional ERC-1155, donde viven las participaciones.

        Es el mismo contrato para todos los mercados de Polymarket, pero se
        declara por mercado y no como constante de la aplicación: quien lo
        necesite opera contra «un recinto de predicción», y ese dato es del
        recinto.

        Se comprueba además que el mercado esté en la red del recinto. Autorizar
        un contrato en una red donde no existe es una transacción que gasta gas
        para escribir en una dirección vacía, y el fallo no se vería hasta que la
        orden no se pudiera liquidar.
        """
        if market.venue.chain != CHAIN_KEY:
            raise ExecutionError(
                f"el mercado «{market.question}» dice estar en "
                f"«{market.venue.chain}», y las participaciones de este recinto "
                f"viven en «{CHAIN_KEY}». No se autoriza nada en una red donde el "
                f"contrato no existe."
            )
        return CONDITIONAL_TOKENS

    def exchange_for(self, market: PredictionMarket) -> str:
        """Contra qué contrato se firma esta orden. Medido, no deducido.

        El contrato lo elige el **tipo** de mercado —de resultados excluyentes o
        no—, y se lee del propio mercado. Un mercado que no declare si lo es
        devuelve aquí una excepción en vez de un contrato por omisión: firmar
        contra el exchange equivocado produce una orden que el recinto rechaza, y
        «por omisión» sería la forma de que ese fallo ocurriera sólo en los
        mercados raros, que es donde menos se mira.
        """
        if market.neg_risk is None:
            raise ExecutionError(
                f"el mercado «{market.question}» no dice si es de resultados "
                f"excluyentes, y eso decide contra qué contrato se firma. Sin ese "
                f"dato no se firma: elegir uno por omisión firmaría contra el "
                f"exchange equivocado en la mitad de los casos."
            )
        return exchange_for(market.neg_risk)

    async def book(self, token_id: str) -> MarketDepth:
        """El libro real de un resultado. Lectura pública, sin credenciales.

        Medido: `/book` responde sin autenticación y trae `tick_size` y
        `min_order_size` del mercado además de los niveles. Eso hace que este
        método sea la fuente preferida de esos dos datos frente a la lista de
        mercados, que los publica pero sólo para los mercados que devuelve.

        Los niveles vienen **de mejor a peor** ya desde la fuente en los dos
        lados, y se conserva ese orden: es el orden en que se consumirían.
        """
        payload = as_mapping(
            await self._source.get_json(
                f"{CLOB_ROOT}/book",
                params={"token_id": token_id},
            ),
            "book",
            SOURCE_NAME,
        )
        return MarketDepth(
            token_id=token_id,
            bids=_levels(payload.get("bids"), best_first=True),
            asks=_levels(payload.get("asks"), best_first=False),
            tick_size=optional_decimal(payload.get("tick_size")),
            min_order_size=optional_decimal(payload.get("min_order_size")),
        )

    # ----------------------------------------------------------------- #
    # Cobrar: lo que ya se tiene
    # ----------------------------------------------------------------- #
    async def positions(
        self, *, wallet: str, redeemable_only: bool = False
    ) -> tuple[PredictionPosition, ...]:
        """Lo que esa cartera tiene en el recinto. Lectura pública, sin claves.

        `redeemable_only` se traduce al filtro del servidor en vez de aplicarse
        aquí: una cartera con historial tiene cientos de posiciones cerradas o
        perdedoras, y traérselas todas para descartarlas sería traer la mayor
        parte de la respuesta para tirarla.

        Una fila que trae mercado pero le falta el resultado o el tamaño **no se
        descarta**: se lanza. Ver `_to_position`.
        """
        payload = await self._source.get_json(
            f"{DATA_ROOT}/positions",
            params={
                "user": wallet,
                "sizeThreshold": "0",
                # El servidor publica el booleano en `redeemable`; se pide el
                # filtro por su nombre y no por el nuestro porque es su API.
                **({"redeemable": "true"} if redeemable_only else {}),
            },
        )
        filas = as_sequence(payload, "positions", SOURCE_NAME)
        ahora = self._clock.now()
        posiciones: list[PredictionPosition] = []
        for fila in filas:
            posicion = _to_position(as_mapping(fila, "position", SOURCE_NAME), ahora)
            if posicion is not None:
                posiciones.append(posicion)
        return tuple(posiciones)

    def build_redeem(
        self, positions: Sequence[PredictionPosition], *, wallet: str
    ) -> UnsignedTransaction:
        """La transacción que cobra **un** mercado. Sin red y sin claves.

        Cobra las que se le pasan y no «todas las que haya»: el conjunto lo
        decide quien las enseñó, y volver a consultarlo aquí permitiría firmar
        algo distinto de lo que se confirmó.

        **Todas tienen que ser del mismo mercado**, y si no lo son se lanza. El
        contrato cobra un mercado por llamada, así que agrupar es inevitable —
        pero agrupar aquí, en silencio, devolvería una transacción de un mercado
        cuando se pidieron dos y dejaría la otra sin cobrar sin que nadie se
        entere. Que agrupe quien llama: es quien tiene que enseñarlo y quien
        escribe un asiento por cada cobro.

        El importe que se cobra no se calcula aquí. Va en la descripción, que es
        donde el usuario lo lee antes de firmar, y se saca de las posiciones —que
        ya lo traen— en vez de volver a consultarlo.
        """
        if not positions:
            raise ExecutionError("un cobro sin posiciones no es un cobro")
        mercados = {posicion.condition_id for posicion in positions}
        if len(mercados) > 1:
            raise ExecutionError(
                f"se pidió cobrar {len(mercados)} mercados distintos en una sola "
                f"transacción, y el contrato cobra uno por llamada. Se rechaza en "
                f"vez de agrupar por dentro: agrupar aquí devolvería la transacción "
                f"de uno y dejaría los otros sin cobrar en silencio."
            )
        primera = positions[0]
        if primera.venue.venue_id != VENUE.venue_id:
            raise ExecutionError(
                f"la posición es de «{primera.venue.venue_id}» y este motor es "
                f"«{VENUE.venue_id}»: no se construye un cobro contra otro recinto."
            )
        if primera.venue.chain != CHAIN_KEY:
            raise ExecutionError(
                f"la posición dice estar en «{primera.venue.chain}» y las "
                f"participaciones de este recinto viven en «{CHAIN_KEY}». No se "
                f"firma un cobro en una red donde el contrato no existe."
            )
        faltas = [
            falta
            for posicion in positions
            for falta in posicion.redeemability_blockers
        ]
        if faltas:
            raise ExecutionError(
                "no se puede cobrar esta posición todavía: " + "; ".join(faltas)
            )
        if not wallet:
            raise ExecutionError(
                "un cobro necesita saber a qué cartera pertenecen las "
                "participaciones: sin eso no se sabe de quién se queman."
            )

        total = sum((posicion.payout for posicion in positions), Decimal(0))
        colateral = self.collateral_on(primera.venue.chain)
        _log.info(
            "polymarket.redeem_built",
            condition_id=primera.condition_id,
            positions=len(positions),
            payout=str(total),
        )
        return UnsignedTransaction(
            chain_id=CHAIN_ID,
            # El destino es el contrato condicional, que es quien tiene las
            # participaciones: no se le manda a nadie una orden, se le pide al
            # contrato que queme las propias y devuelva el colateral.
            to_address=CONDITIONAL_TOKENS,
            calldata=build_redeem_calldata(primera.condition_id),
            # Cero: cobrar no manda valor consigo. El gas se paga aparte, y
            # ponerlo aquí lo cobraría dos veces.
            value=TokenAmount.zero(18, "POL"),
            description=(
                f"Cobrar {len(positions)} posición"
                f"{'es' if len(positions) > 1 else ''} de «{primera.question}»: "
                f"{total:f} {colateral.symbol} de colateral por "
                f"{sum((p.shares for p in positions), Decimal(0)):f} participaciones"
            ),
        )


# --------------------------------------------------------------------------- #
# El cliente del relayer
# --------------------------------------------------------------------------- #
def _relayer_client(credentials: WalletChannelCredentials) -> RelayerClient:
    """El cliente del relayer del canal de la deposit wallet.

    Se construye por llamada, como el del CLOB: las credenciales entran, se
    usan y se sueltan, y ningún objeto de larga vida se queda con ellas.
    """
    relayer = (
        RelayerKey(api_key=credentials.relayer_api_key, address=credentials.relayer_address)
        if credentials.relayer_api_key and credentials.relayer_address
        else None
    )
    return RelayerClient(
        builder=BuilderCredentials(
            api_key=credentials.builder_api_key,
            secret=credentials.builder_secret,
            passphrase=credentials.builder_passphrase,
        ),
        relayer=relayer,
    )


# --------------------------------------------------------------------------- #
# Lectura del formato de la fuente
# --------------------------------------------------------------------------- #
def _decode_json_array(value: Any) -> list[Any] | None:
    """Decodifica una lista que la fuente manda **como cadena JSON**.

    Gamma publica `outcomes` y `outcomePrices` así: `'["Yes", "No"]'`. Es un
    JSON dentro de otro JSON, no un descuido nuestro al leerlo.
    """
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, list) else None


def _token_id(token_ids: list[Any] | None, index: int) -> str | None:
    """El identificador operable de un resultado, o `None` si no se puede leer.

    La fuente lo manda como cadena de dígitos —es un `uint256`— y a veces como
    número. Los dos se aceptan y se normalizan a la forma decimal canónica; lo
    que **no** se acepta es cualquier otra cosa, porque esto acaba dentro de una
    firma y un identificador mal formado produce una orden que el recinto rechaza
    o, peor, que apunta a otro resultado.
    """
    if token_ids is None or index >= len(token_ids):
        return None
    raw = token_ids[index]
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw) if raw >= 0 else None
    if isinstance(raw, str):
        text = raw.strip()
        return text if text.isdigit() else None
    return None


def _optional_text(value: Any) -> str | None:
    """Un texto de la fuente, o `None` si viene vacío o no es texto."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _optional_bool(value: Any) -> bool | None:
    """Un booleano de la fuente, sin inventarse el que no viene.

    `bool | None` y no `bool`: devolver `False` para un campo ausente afirmaría
    «este mercado no es de resultados excluyentes», y esa afirmación decide
    contra qué contrato se firma. `None` significa «no consta», y el dominio ya
    sabe negarse a operar con eso.
    """
    return value if isinstance(value, bool) else None


def _levels(value: Any, *, best_first: bool) -> tuple[DepthLevel, ...]:
    """Traduce los niveles del libro, descartando los ilegibles y ordenando.

    La fuente ya los manda ordenados de mejor a peor, y aun así se ordena aquí:
    el dominio **exige** ese orden y comprobarlo es barato. Depender del orden de
    un tercero para un cálculo que decide cuánto se paga es la clase de confianza
    que se convierte en un error silencioso el día que cambian el serializador.
    """
    if not isinstance(value, list):
        return ()
    niveles: list[DepthLevel] = []
    for bruto in value:
        if not isinstance(bruto, dict):
            continue
        price = optional_decimal(bruto.get("price"))
        size = optional_decimal(bruto.get("size"))
        if price is None or size is None:
            continue
        try:
            niveles.append(DepthLevel(price=price, size=size))
        except InvalidAmountError:
            # Un nivel con el precio en 0 o negativo no es un nivel: es una fila
            # que la fuente publica para sus cuentas internas. Se salta, y saltar
            # es correcto —no cambia lo que costaría cruzar el resto—. No se
            # registra porque es lo esperado: registrar cada fila descartada
            # llenaría el log de ruido en cada lectura de libro.
            continue
    niveles.sort(key=lambda nivel: nivel.price, reverse=best_first)
    return tuple(niveles)


def _iso(moment: datetime) -> str:
    """Marca ISO-8601 en UTC, que es el formato que acepta la fuente.

    Se fija el sufijo `Z` en vez de dejar el `+00:00` de `isoformat` porque es lo
    que se midió que la API devuelve y acepta; y se recortan los microsegundos,
    que no aportan nada a una ventana medida en días.
    """
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _to_position(fila: Mapping[str, Any], ahora: datetime) -> PredictionPosition | None:
    """Traduce una posición de la fuente, o `None` si la fila no es una posición.

    Devuelve `None` sólo cuando la fila **no describe una posición**, que es
    cuando no trae mercado: no hay nada que cobrar y no hay nada que decir. En
    cuanto trae un mercado, la fila habla de algo que la cartera tiene, y
    entonces un dato que falte **no se puede tirar**. Descartarla en silencio
    dejaría la pantalla diciendo «no tienes nada que cobrar» mientras la cartera
    sí tiene algo, y el usuario no tendría forma de saber que el problema es que
    la fuente cambió de formato. Por eso lo que falta ahí se lanza.

    `negativeRisk` se copia con `_optional_bool`, así que un campo ausente queda
    en `None` y no en `False`. La diferencia decide contra qué contrato se cobra,
    y afirmar «no comparte colateral» porque la fuente no lo dijo es exactamente
    la suposición que el dominio se niega a hacer.
    """
    condition_id = _optional_text(fila.get("conditionId"))
    if condition_id is None:
        return None
    token_id = _optional_text(fila.get("asset"))
    size = optional_decimal(fila.get("size"))
    if token_id is None or size is None:
        falta = "el identificador del resultado" if token_id is None else "el tamaño"
        raise ExecutionError(
            f"la fuente publicó una posición en el mercado {condition_id} sin "
            f"{falta}, y sin ese dato no se puede saber qué participaciones se "
            f"tienen ni cuántas. Se dice en vez de descartarla: descartarla dejaría "
            f"la pantalla diciendo que no hay nada que cobrar cuando sí lo hay."
        )
    question = _optional_text(fila.get("title")) or _optional_text(fila.get("slug"))
    outcome = _optional_text(fila.get("outcome"))
    return PredictionPosition(
        venue=VENUE,
        condition_id=condition_id,
        question=question or "mercado sin nombre",
        outcome_label=outcome or "resultado sin nombre",
        token_id=token_id,
        shares=size,
        # La fuente no publica **cuándo** se leyó la posición —es una foto, no un
        # histórico—, así que el instante es el de esta lectura. Poner la fecha
        # de cierre del mercado o cualquier otra del payload sería fechar el dato
        # con algo que no es su fecha.
        observed_at=ahora,
        redeemable=bool(_optional_bool(fila.get("redeemable"))),
        neg_risk=_optional_bool(fila.get("negativeRisk")),
        cur_price=optional_decimal(fila.get("curPrice")),
        # El precio medio y el resultado no hacen falta para cobrar —eso sólo
        # necesita `redeemable`—, pero sí para poder decir «cuánto se ha ganado»
        # al operar sobre una posición viva. Se copian tal como los publica la
        # fuente y se quedan en `None` si no están: no se calculan aquí, porque
        # recalcularlos exigiría el histórico de compras, que esta lectura no
        # trae.
        avg_price=optional_decimal(fila.get("avgPrice")),
        cash_pnl=optional_decimal(fila.get("cashPnl")),
        percent_pnl=optional_decimal(fila.get("percentPnl")),
    )


def _parse_moment(value: Any) -> datetime | None:
    """Convierte una marca ISO-8601 a `datetime` con zona horaria.

    El dominio exige instantes con zona: una fecha de cierre sin zona es
    ambigua en varias horas, y de esas horas puede depender si un mercado está
    abierto. Si no se puede determinar, se devuelve `None` en vez de suponer UTC.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _non_negative(value: Any) -> Decimal | None:
    """Una cifra de la fuente que no puede ser negativa, o `None`.

    Un volumen o una liquidez negativos no existen: si llegan así es una fila
    mal publicada, y se descarta **el dato**, no el mercado —estas cifras son
    para mirar, y el mercado sigue sirviendo—.
    """
    cifra = optional_decimal(value)
    if cifra is None or cifra < 0:
        return None
    return cifra


def _price_delta(value: Any) -> Decimal | None:
    """Un cambio de precio, o `None` si no es un delta creíble.

    Medido el 2026-10-09: la fuente publica el cambio en **unidades de precio**
    —un mercado cuyo «sí» pasó de 0.445 a 0.235 trae `oneDayPriceChange` de
    -0.21, exactamente el delta—, así que un valor fuera de [-1, 1] no puede
    venir de dos precios de [0, 1]. Se descarta el dato y no el mercado: la
    tendencia es adorno.
    """
    delta = optional_decimal(value)
    if delta is None or abs(delta) > 1:
        return None
    return delta


def _order_params(
    closing_within: timedelta | None, sort: PredictionSort | None
) -> tuple[str, str]:
    """El campo y la dirección con los que la fuente ordena la lista.

    Devuelve `(order, ascending)` listos para el parámetro. `closing_within`
    manda sobre `sort` —la vista de la ventana es la de lo que está
    terminando— y un `sort` ausente o de los que ordena este lado cae en el
    volumen de siempre.
    """
    if closing_within is not None:
        return "endDate", "true"
    if sort is PredictionSort.NEWEST:
        return "createdAt", "false"
    if sort is PredictionSort.TRENDING:
        return "volume24hr", "false"
    return "volumeNum", "false"


def _created_key(market: PredictionMarket) -> datetime:
    """La fecha de creación para ordenar: un mercado sin fecha va al final.

    Se ordena con `reverse=True`, así que la clave vacía tiene que ser la
    **mínima** para caer al final: no consta cuándo se creó, y ponerlo primero
    sería afirmar que es el más nuevo de todos.
    """
    return market.created_at or datetime.min.replace(tzinfo=UTC)


def _volume24h_key(market: PredictionMarket) -> Decimal:
    """El volumen de 24 h para ordenar: un mercado sin dato va al final, no primero."""
    return market.volume_24h if market.volume_24h is not None else Decimal(-1)


def _event_ids(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """Los identificadores de los eventos que contienen este mercado.

    Medido: el mercado trae sus eventos embebidos en `events`, y las etiquetas
    sólo viven en el evento, no en el mercado.
    """
    events = entry.get("events")
    if not isinstance(events, list):
        return ()
    ids: list[str] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        event_id = _id_text(raw.get("id"))
        if event_id is not None:
            ids.append(event_id)
    return tuple(ids)


def _merge_event_tags(
    entry: Mapping[str, Any], tags_by_event: Mapping[str, Sequence[MarketTag]]
) -> tuple[MarketTag, ...]:
    """Las etiquetas de los eventos del mercado, sin slug repetido.

    Un mercado puede colgar de varios eventos y compartir etiqueta con más de
    uno; se queda la primera aparición. Un evento sin etiquetas —o cuya consulta
    falló— simplemente no aporta: no hay nada que inventar.
    """
    merged: list[MarketTag] = []
    seen: set[str] = set()
    for event_id in _event_ids(entry):
        for tag in tags_by_event.get(event_id, ()):
            if tag.slug not in seen:
                seen.add(tag.slug)
                merged.append(tag)
    return tuple(merged)


def _id_text(value: Any) -> str | None:
    """Un identificador de la fuente, venga como cadena o como número.

    Gamma publica el id del evento como número y el de la etiqueta como cadena
    (medido), así que leerlo con `_optional_text` perdería la mitad.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    return _optional_text(value)


def _to_tags(value: Any) -> tuple[MarketTag, ...]:
    """Traduce las etiquetas de un evento, descartando lo ilegible.

    Se enseñan **tal cual las publica la fuente**: junto a categorías de verdad
    (`Politics`, `Crypto`) publica etiquetas suyas de organización (`Recurring`,
    `Hide From New`), y elegir aquí cuáles son «categorías» sería decidir por el
    usuario qué puede filtrar. El selector de la interfaz se ordena por número
    de mercados, y con eso las que organizan la lista quedan arriba sin borrar
    ninguna.
    """
    if not isinstance(value, list):
        return ()
    tags: list[MarketTag] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        tag_id = _id_text(raw.get("id"))
        label = _optional_text(raw.get("label"))
        slug = _optional_text(raw.get("slug"))
        if tag_id is None or label is None or slug is None or slug in seen:
            continue
        seen.add(slug)
        tags.append(MarketTag(tag_id=tag_id, label=label, slug=slug))
    return tuple(tags)


@dataclass(frozen=True, slots=True)
class PolymarketProvider:
    manifest: EngineManifest = MANIFEST

    def create(self, config: Mapping[str, str]) -> PolymarketEngine:
        return PolymarketEngine()


PROVIDER: Final = PolymarketProvider()
