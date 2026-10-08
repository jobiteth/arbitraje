"""Raíz de composición: construye el grafo de objetos de la aplicación.

Inyección de dependencias a mano, sin framework. El grafo es pequeño y
explícito se lee mejor: este fichero es el único sitio donde hay que mirar para
saber qué está conectado con qué.

Es también la única frontera donde `app` y `infra` se encuentran. Ni los casos
de uso ni el registro saben que existen `keyring`, `httpx` o ficheros TOML.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Self
from urllib.parse import urlsplit

import httpx
import structlog

from amigocompora.app.alerts import AlertCenter, AlertKind, severity_for_bps
from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.execution_policy import (
    AutonomyPolicy,
    ExecutionLedger,
    PolicyBypass,
    limits_from_config,
)
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry, sole_engine_for
from amigocompora.app.scheduler import Scheduler
from amigocompora.app.usecases.analyze_prediction_market import AnalyzePredictionMarkets
from amigocompora.app.usecases.analyze_with_ai import AnalyzeWithAi
from amigocompora.app.usecases.compare_bridges import CompareBridges
from amigocompora.app.usecases.compare_prices import ComparePrices
from amigocompora.app.usecases.execute_bridge import ExecuteBridge
from amigocompora.app.usecases.execute_swap import ExecuteSwap
from amigocompora.app.usecases.find_prediction_opportunities import (
    FindPredictionOpportunities,
)
from amigocompora.app.usecases.place_prediction_order import PlacePredictionOrder
from amigocompora.app.usecases.prepare_bridge import PrepareBridge
from amigocompora.app.usecases.prepare_swap import PrepareSwap
from amigocompora.app.usecases.redeem_prediction import RedeemPrediction
from amigocompora.app.usecases.scan_opportunities import ScanOpportunities
from amigocompora.app.usecases.value_in_reference import ValueInReference
from amigocompora.app.usecases.wallet import ReadWallet, ValueWallet
from amigocompora.app.usecases.watch_scan import WatchedPair, WatchScan
from amigocompora.app.usecases.withdraw import WithdrawFunds
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.models import Opportunity, TradingPair
from amigocompora.domain.protocols import EngineKind
from amigocompora.engines.token_lookup import TokenLookup
from amigocompora.engines.token_store import UserTokenStore
from amigocompora.infra import config as config_module
from amigocompora.infra.config import Settings, load_settings
from amigocompora.infra.evm.broadcast import broadcasters_by_chain
from amigocompora.infra.http import build_client
from amigocompora.infra.logging import configure_logging
from amigocompora.infra.rpc.pool import RpcEndpoint, RpcPool, RpcRegistry
from amigocompora.infra.secrets import (
    AutonomyPassphraseProvider,
    KeyringSecretStore,
    MissingSecretError,
    SecretStore,
    SpendingKeyProvider,
    build_config_resolver,
    resolve_placeholders,
)

_log = structlog.get_logger(__name__)

#: Motor que se activa en cada ranura cuando el usuario no ha elegido ninguno.
#:
#: Vive aquí, en la raíz de composición, y no en el registro: elegir un motor
#: concreto es una decisión de producto sobre qué debe ver el usuario al abrir
#: la aplicación por primera vez, no una regla del mecanismo de motores. El
#: registro sigue sin conocer ningún motor por su nombre.
#:
#: Se escogen los de mayor cobertura: `geckoterminal` cotiza todas las
#: versiones de protocolo, así que la primera tabla que ve el usuario sale
#: poblada. Puede cambiarlo en el panel de motores o fijarlo en `config.toml`.
DEFAULT_ENGINE_IDS: Final[Mapping[str, str]] = {
    EngineKind.DEX_QUOTES.value: "geckoterminal",
    EngineKind.PREDICTION_MARKETS.value: "polymarket",
    # Asistente offline por defecto: la ranura de IA queda utilizable sin
    # configurar ninguna clave. El usuario puede cambiar a Claude/DeepSeek/ChatGPT
    # desde el panel de motores o fijarlo en config.toml.
    EngineKind.AI_ADVISOR.value: "stub_advisor",
    # LI.FI y no Relay, y por una razón que no es de gusto: Relay declara su
    # clave en `required_config`, así que autoactivarlo sin credencial fallaría en
    # el arranque y dejaría la ranura igual de vacía. LI.FI cotiza y construye
    # sin clave —la suya es `optional_config`— y su manifiesto ya se declara
    # «antes que Relay» con `bridge_priority=50`.
    #
    # Sin esta línea la ranura se quedaba vacía: `sole_engine_for` sólo
    # autoactiva cuando hay **un** motor instalado de la ranura, y desde que hay
    # dos puentes no hay ninguno que sea el único. La pestaña «Entre redes» se
    # abría, dejaba elegir redes, tokens e importe, y al buscar contestaba que no
    # había a quién preguntar.
    EngineKind.CROSS_CHAIN.value: "lifi",
}


@dataclass(slots=True)
class Container:
    """Todo lo que la UI necesita, ya cableado."""

    settings: Settings
    clock: Clock
    guard: ModeGuard
    gateway: ConfirmationGateway
    registry: EngineRegistry
    compare_prices: ComparePrices
    #: Cotiza el mismo cruce en **todos** los motores de puentes activos y ordena
    #: las rutas por lo que entregan. Es a los puentes lo que `compare_prices` es a
    #: los swaps, y no tiene efectos: sólo pregunta.
    compare_bridges: CompareBridges
    scan_opportunities: ScanOpportunities
    analyze_markets: AnalyzePredictionMarkets
    find_prediction_opportunities: FindPredictionOpportunities
    prepare_swap: PrepareSwap
    #: Construye el payload sin firmar de un puente. Es al camino de puentes lo
    #: que `prepare_swap` es al de swaps, y por la misma razón: poder mirar qué se
    #: haría sin que nada ocurra.
    prepare_bridge: PrepareBridge
    #: Firma y emite un swap. Está detrás de dos puertas: el modo `EJECUCIÓN` y
    #: `execution.enabled`. Sin él la aplicación sólo prepara payloads.
    execute_swap: ExecuteSwap
    #: Firma y emite un **puente**: saca el dinero de una red y lo hace aparecer
    #: en otra. Está detrás de las mismas dos puertas que `execute_swap` —el modo
    #: `EJECUCIÓN` y `execution.enabled`— y por eso comparte emisores y política.
    #: Se separa del swap porque lo que se comprueba no es lo mismo: aquí la lista
    #: blanca de redes tiene que cubrir **las dos** redes, no sólo la de origen.
    execute_bridge: ExecuteBridge
    #: Construye, firma y **publica** una orden en un mercado de predicción. Es,
    #: junto con `execute_swap`, el otro camino por el que sale dinero: mismo
    #: modo y mismo interruptor, y por eso comparten emisores y política.
    place_prediction_order: PlacePredictionOrder
    #: Cobra posiciones de un mercado de predicción ya resuelto. Mismo modo y
    #: mismo interruptor que los otros dos, y el único de los tres por el que el
    #: dinero **entra**: por eso no le aplica el tope de gasto, sólo el resto de
    #: la comprobación.
    redeem_prediction: RedeemPrediction
    #: La política de autonomía, con sus límites y su registro. La interfaz la lee
    #: para saber si puede ofrecer el botón, y para poder explicar por qué no.
    policy: AutonomyPolicy
    #: De dónde sale la clave privada. Se expone para que la interfaz pueda pedir
    #: la **dirección** derivada —que es pública— sin tocar la clave.
    keys: SpendingKeyProvider
    #: De dónde sale la frase que habilita la ejecución desatendida. Se expone por
    #: el mismo motivo que `keys` y con el mismo límite —el valor no se pide
    #: nunca—: la pantalla de credenciales tiene que poder decir si está puesta y
    #: **de dónde**. Sin esto, una frase servida por el entorno se anunciaba como
    #: «el llavero no responde»: cierto, y llevaba a la conclusión contraria.
    passphrase: AutonomyPassphraseProvider
    analyze_with_ai: AnalyzeWithAi
    alert_center: AlertCenter
    scheduler: Scheduler
    watch_scan: WatchScan
    #: Los nodos JSON-RPC por red. Sin esto no hay forma de leer el estado que
    #: falta para firmar —nonce, gas, comisiones— ni de emitir nada.
    rpc: RpcRegistry
    #: Resuelve un token cualquiera por su dirección, leyendo su contrato. Es lo
    #: que hace que se pueda operar con un token que no está en el catálogo: sin
    #: esto, la lista de tokens sería el límite de lo que la aplicación sabe
    #: nombrar, y un token nuevo no tendría forma de entrar.
    token_lookup: TokenLookup
    #: Los tokens que el usuario añadió a mano, guardados entre sesiones. El
    #: catálogo del código no crece solo, y pegar una dirección en cada arranque
    #: es justo lo que hacía que un token nuevo no llegara a ser tradeable.
    token_store: UserTokenStore
    #: Lee los saldos de una cartera, red por red y sin valorarlos. Es una
    #: lectura pura: ni firma ni emite, y el motor que la sirve declara
    #: `READ_CHAIN` como única capacidad. Se separa de `value_wallet` porque leer
    #: cuesta una llamada por red y valorar una cotización por token con fondos.
    read_wallet: ReadWallet
    #: Pone precio a una foto ya leída, sin volver a tocar los nodos. Recibe el
    #: `WalletSnapshot` y no la cartera justamente para eso: releyendo, la
    #: interfaz que pinta saldos y luego importes haría el doble de peticiones, y
    #: en Solana —un solo nodo público, racionado por ventana— eso no se puede.
    value_wallet: ValueWallet
    #: Saca fondos de la cartera: construye la transferencia, la firma y la emite.
    #: Tercer camino por el que sale dinero, junto a `execute_swap` y
    #: `execute_bridge`, y el único que no pasa por ningún motor. Está detrás de
    #: las mismas dos puertas —el modo `EJECUCIÓN` y `execution.enabled`— y de la
    #: lista blanca de motores, que tiene que nombrar `wallet`.
    withdraw_funds: WithdrawFunds
    #: El almacén de credenciales del sistema. Se expone para que la interfaz
    #: pueda **escribir** en él: hasta ahora sólo se leía, y eso dejaba la clave
    #: privada y las API keys sin ningún sitio donde ponerlas.
    secrets: SecretStore
    #: El cliente HTTP compartido por todos los pools. Se guarda para poder
    #: cerrarlo: `RpcPool` no es dueño de su transporte.
    _rpc_client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        """Cierra scheduler, motores y el cliente de RPC. La UI lo llama al salir."""
        try:
            await self.scheduler.stop()
        finally:
            try:
                await self.registry.aclose()
            finally:
                if self._rpc_client is not None:
                    await self._rpc_client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


async def build_container(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    secret_store: SecretStore | None = None,
    discover: bool = True,
    configure_logs: bool = True,
) -> Container:
    """Construye y arranca la aplicación sin UI.

    Útil también para tests de integración y para un futuro modo CLI: todo lo
    que no es Qt se puede ejercitar desde aquí.
    """
    effective_settings = settings or load_settings()
    if configure_logs:
        configure_logging(
            level=effective_settings.log_level,
            as_json=effective_settings.log_json,
        )

    effective_clock = clock or SystemClock()
    guard = ModeGuard(effective_settings.mode)
    # El prompt real (el diálogo Qt) se conecta después, al construir la
    # ventana. Hasta entonces rige `DenyAllPrompt`.
    gateway = ConfirmationGateway(guard, clock=effective_clock)

    effective_store = secret_store or KeyringSecretStore()
    rpc_registry, rpc_client = build_rpc_registry(
        effective_settings, effective_store, effective_clock
    )

    resolver = build_config_resolver(effective_store)
    registry = EngineRegistry(guard, resolver)

    if discover:
        registry.discover()
        await _activate_configured_engines(registry, effective_settings)

    compare_prices = ComparePrices(registry=registry, gateway=gateway)
    compare_bridges = CompareBridges(registry=registry, gateway=gateway)
    scan_opportunities = ScanOpportunities(
        compare_prices=compare_prices,
        gateway=gateway,
        clock=effective_clock,
    )
    alert_center = AlertCenter(clock=effective_clock)
    scheduler = Scheduler(guard=guard, clock=effective_clock)
    watch_scan = WatchScan(scan=scan_opportunities, gateway=gateway)
    watch_scan.set_pairs(_watched_pairs(effective_settings))
    # Publicar oportunidades del barrido periódico como alertas.
    watch_scan.subscribe(lambda opps: _publish_opportunities(alert_center, opps))
    analyze_markets = AnalyzePredictionMarkets(registry=registry, gateway=gateway)

    # La cartera. Se construye **siempre**, aunque no haya motor de cartera
    # activo: lo que falta entonces lo dice el propio caso de uso con un mensaje
    # accionable, y construirlo bajo condición dejaría a la interfaz sin nada a
    # lo que preguntar para poder explicar que falta un motor.
    #
    # Los dos pasos van separados —leer saldos y valorarlos— porque cuestan cosas
    # muy distintas: leer es una llamada por red, valorar es una cotización por
    # token con fondos. Ver `usecases.wallet`.
    # La misma tienda que las pantallas de tokens: lo que el usuario añade aquí es
    # lo que la cartera tiene que leer, o un token añadido nunca tendría saldo.
    token_store = UserTokenStore(config_module.config_dir() / "tokens.json")
    read_wallet = ReadWallet(
        registry=registry,
        guard=guard,
        clock=effective_clock,
        added_tokens=lambda chain_key: tuple(
            token for token in token_store.load() if token.chain == chain_key
        ),
    )
    value_wallet = ValueWallet(valuation=ValueInReference(registry=registry))

    # El camino que firma y emite. Se construye **siempre**, incluso con la
    # ejecución apagada: lo que impide operar son las puertas —el modo y
    # `execution.enabled`—, no que falte el objeto. Construirlo bajo condición
    # haría que encenderlo exigiera reiniciar, y que un fallo de cableado sólo
    # apareciera el día que alguien se decide a operar, que es el peor día.
    prepare_swap = PrepareSwap(registry=registry, gateway=gateway)
    prepare_bridge = PrepareBridge(registry=registry, gateway=gateway)
    execution = _build_execution(
        settings=effective_settings,
        store=effective_store,
        clock=effective_clock,
        gateway=gateway,
        prepare=prepare_swap,
        prepare_bridge=prepare_bridge,
        rpc=rpc_registry,
        registry=registry,
    )

    return Container(
        settings=effective_settings,
        clock=effective_clock,
        guard=guard,
        gateway=gateway,
        registry=registry,
        compare_prices=compare_prices,
        compare_bridges=compare_bridges,
        scan_opportunities=scan_opportunities,
        analyze_markets=analyze_markets,
        find_prediction_opportunities=FindPredictionOpportunities(
            analyze=analyze_markets,
            gateway=gateway,
            clock=effective_clock,
        ),
        prepare_swap=prepare_swap,
        prepare_bridge=prepare_bridge,
        execute_swap=execution.execute_swap,
        execute_bridge=execution.execute_bridge,
        place_prediction_order=execution.place_prediction_order,
        redeem_prediction=execution.redeem_prediction,
        policy=execution.policy,
        keys=execution.keys,
        passphrase=execution.passphrase,
        analyze_with_ai=AnalyzeWithAi(registry=registry, gateway=gateway),
        alert_center=alert_center,
        scheduler=scheduler,
        watch_scan=watch_scan,
        rpc=rpc_registry,
        token_lookup=TokenLookup(rpc=rpc_registry),
        # La misma casa que `executions.jsonl`, y por el mismo motivo: el
        # directorio de configuración es el único sitio que existe cuando la
        # aplicación se lanza desde el menú de inicio y el directorio de trabajo
        # es cualquiera. Se lee por el **módulo** —ver la nota del registro de
        # ejecuciones— para que el aislamiento de la suite lo pueda desviar.
        token_store=token_store,
        secrets=effective_store,
        read_wallet=read_wallet,
        value_wallet=value_wallet,
        withdraw_funds=execution.withdraw_funds,
        _rpc_client=rpc_client,
    )


def build_rpc_registry(
    settings: Settings,
    store: SecretStore,
    clock: Clock,
) -> tuple[RpcRegistry, httpx.AsyncClient | None]:
    """Un `RpcPool` por red configurada, sobre un cliente HTTP compartido.

    Se construye aunque todavía no haya nada que firme, porque leer el estado de
    la red —balances, allowance, comisiones— es lo que hace falta *antes* de
    firmar, y tenerlo cableado desde el arranque evita que la primera ejecución
    real dependa de que alguien se acuerde de añadirlo.

    Los marcadores `${NOMBRE}` de las URLs se resuelven **aquí**, una sola vez y
    al construir: es lo que permite que una clave de RPC viva en el `.env` o en
    el llavero y no en `config.toml`, que se copia, se pega en un issue y se sube
    a un repositorio.

    Un cliente por proceso y no uno por red: comparten el grupo de conexiones, y
    la lista de hosts permitidos sigue siendo la unión de los nodos configurados,
    que es un conjunto cerrado y conocido. Si no hay ninguna red utilizable no se
    construye cliente ninguno — `build_client` con una lista vacía es un error, y
    con razón: un cliente sin hosts permitidos es un cliente que no puede hablar
    con nadie.
    """
    pools: dict[str, RpcPool] = {}
    urls: list[tuple[str, str, str, int]] = []
    for chain_settings in settings.chains:
        if not chain_settings.is_usable:
            _log.warning(
                "rpc.chain_without_endpoints",
                chain=chain_settings.chain,
                hint=(
                    "la red no tiene nodos declarados ni respaldos públicos medidos; "
                    "añade al menos un endpoint en config.toml para poder usarla"
                ),
            )
            continue
        for endpoint in chain_settings.effective_endpoints():
            nombre = endpoint.label or endpoint.url
            try:
                resolved = resolve_placeholders(
                    endpoint.url,
                    store,
                    place=f"el endpoint «{nombre}» de {chain_settings.chain}",
                )
            except MissingSecretError as error:
                # Un marcador sin resolver descarta **ese** endpoint, no el
                # arranque. Los nodos de una red son intercambiables por diseño
                # —es justo lo que hace la lista de respaldos—, así que perder el
                # nodo propio deja la red más lenta y con menos cuota, pero no
                # deja al usuario sin aplicación. Abortar aquí convertía «me
                # faltó pegar una clave» en «no puedo abrir el programa», que es
                # un precio desproporcionado por un endpoint de cinco.
                #
                # El aviso dice qué secreto falta y dónde configurarlo: el
                # mensaje de `MissingSecretError` ya nombra la credencial y la
                # variable, así que se registra tal cual.
                _log.warning(
                    "rpc.endpoint_without_secret",
                    chain=chain_settings.chain,
                    endpoint=nombre,
                    reason=str(error),
                )
                continue
            urls.append((chain_settings.chain, resolved, endpoint.label, endpoint.priority))

    if not urls:
        return RpcRegistry(), None

    client = build_client(
        allowed_hosts=frozenset(urlsplit(url).hostname or "" for _, url, _, _ in urls),
        timeout_seconds=settings.request_timeout_seconds,
    )

    by_chain: dict[str, list[RpcEndpoint]] = {}
    for chain_key, url, label, priority in urls:
        by_chain.setdefault(chain_key, []).append(
            RpcEndpoint(url=url, label=label, priority=priority)
        )
    for chain_key, endpoints in by_chain.items():
        pools[chain_key] = RpcPool(
            chain_key,
            endpoints,
            client,
            clock=clock,
            failure_threshold=settings.rpc_failure_threshold,
            cooldown_seconds=settings.rpc_cooldown_seconds,
        )
    _log.info(
        "rpc.registry_ready",
        chains=sorted(pools),
        endpoints={key: len(value) for key, value in by_chain.items()},
    )
    return RpcRegistry(pools), client


@dataclass(frozen=True, slots=True)
class _Execution:
    """Las piezas de la ejecución, agrupadas para no devolver una tupla de tres.

    Una tupla funcionaría y se leería peor: `execution[1]` no dice qué es, y este
    es el trozo del grafo donde confundir la política con el registro tiene
    consecuencias.
    """

    execute_swap: ExecuteSwap
    execute_bridge: ExecuteBridge
    #: La retirada. Va con los otros dos porque comparte lo que de verdad
    #: importa: los **mismos** emisores, la misma política y el mismo almacén de
    #: claves. Un mapa de emisores propio sería un segundo sitio donde el pool de
    #: una red puede quedar distinto del otro sin que nada lo diga.
    withdraw_funds: WithdrawFunds
    place_prediction_order: PlacePredictionOrder
    redeem_prediction: RedeemPrediction
    policy: AutonomyPolicy
    keys: SpendingKeyProvider
    #: Va con las dos anteriores y por el mismo motivo: la pantalla de credenciales
    #: necesita poder decir si una frase servida por el entorno está puesta, y eso
    #: sólo lo sabe el proveedor.
    passphrase: AutonomyPassphraseProvider


def _build_execution(
    settings: Settings,
    store: SecretStore,
    clock: Clock,
    gateway: ConfirmationGateway,
    prepare: PrepareSwap,
    prepare_bridge: PrepareBridge,
    rpc: RpcRegistry,
    registry: EngineRegistry,
) -> _Execution:
    """Cablea firmar y emitir: secretos, límites, registro y emisores.

    Se construye entero aunque la ejecución esté apagada, porque cada pieza de
    aquí es también una pieza de la **explicación** que la interfaz tiene que dar:
    sin cartera cargada, sin política que leer y sin límites en vigor, lo único
    que la aplicación podría decir es «no se puede», sin decir por qué. Y un
    «no se puede» sin motivo es lo que empuja a alguien a buscar la forma de
    saltárselo.

    El orden importa por una razón concreta: los emisores se construyen sobre los
    pools del registro de RPC, que ya existe cuando se llama a esto. Al revés no
    compilaría; y construirlos perezosamente haría que la primera operación real
    dependiera de que alguien se acordara de construirlos, que es justo el fallo
    que este fichero existe para no tener.

    La clave privada **no** se lee aquí. Esto sólo prepara de dónde saldría: se
    pide en el momento de firmar y se suelta. Lo que sí se puede calcular sin
    riesgo es la dirección derivada, y para eso el proveedor expone `address()`.
    """
    cfg = settings.execution

    # El mismo `allow_env_key` para los dos secretos a propósito: es **una**
    # decisión —«esta máquina lee secretos de ejecución del entorno»— y no dos.
    # La frase sola no firma nada; lo que habilita es la ejecución desatendida.
    keys = SpendingKeyProvider(store, allow_env_fallback=cfg.allow_env_key)
    passphrase = AutonomyPassphraseProvider(store, allow_env_fallback=cfg.allow_env_key)

    # El registro se abre por el **módulo** y no por la función importada, y eso
    # no es estilo: `from ... import config_dir` copia la referencia al objeto, así
    # que parchear el atributo del módulo —que es lo que hace el aislamiento de la
    # suite— no rebindea esa copia y el registro seguiría apuntando al directorio
    # real del usuario. Se midió: la suite pasaba en CI y fallaba en la máquina de
    # desarrollo en cuanto había ejecutado algo de verdad una vez.
    ledger = ExecutionLedger(config_module.config_dir() / "executions.jsonl", clock=clock)
    policy = AutonomyPolicy(
        keys=keys,
        passphrase=passphrase,
        limits=limits_from_config(
            enabled=cfg.enabled,
            max_quote_per_trade=cfg.max_quote_per_trade,
            max_quote_per_day=cfg.max_quote_per_day,
            allowed_tokens=cfg.allowed_tokens,
            allowed_chains=cfg.allowed_chains,
            allowed_engines=cfg.allowed_engines,
            max_executions_per_cycle=cfg.max_executions_per_cycle,
            slippage_bps=cfg.slippage_bps,
        ),
        ledger=ledger,
        clock=clock,
        trigger=cfg.trigger,
    )

    # El bypass se conecta **siempre**, no sólo con la ejecución encendida:
    # `PolicyBypass.reason_to_skip` ya devuelve `None` salvo que se trate de
    # emitir **y** la autonomía esté armada, y armar exige la frase de autonomía
    # configurada. Conectarlo bajo condición sería añadir una segunda cosa que
    # recordar —y que olvidar deja la ejecución desatendida sin efecto— a cambio
    # de ninguna seguridad.
    gateway.set_bypass(PolicyBypass(policy))

    # Los emisores se construyen **una vez** y se comparten: el camino de swaps y
    # el de órdenes de predicción firman en las mismas redes, y dos mapas con el
    # mismo contenido serían dos sitios donde el pool de una red puede quedar
    # distinto del otro sin que nada lo diga.
    broadcasters = broadcasters_by_chain(
        rpc.pools,
        gas_policy=cfg.gas.to_policy(),
        clock=clock,
    )

    return _Execution(
        execute_swap=ExecuteSwap(
            prepare=prepare,
            gateway=gateway,
            keys=keys,
            policy=policy,
            broadcasters=broadcasters,
            clock=clock,
            # Con esto, un par que no toque la stablecoin de referencia —ETH
            # contra un token, o dos tokens entre sí— sigue midiéndose contra los
            # topes: se valora lo entregado con los mismos motores que cotizaron
            # el par. Sin esto habría que rechazarlo, que es lo que se hacía.
            valuation=ValueInReference(registry=registry),
        ),
        execute_bridge=ExecuteBridge(
            prepare=prepare_bridge,
            gateway=gateway,
            # Los **mismos** emisores que el swap: el puente emite en la red de
            # origen, que es una de las que un swap ya usa, y dos mapas con el
            # mismo contenido serían dos sitios donde el pool de una red puede
            # quedar distinto del otro sin que nada lo diga.
            keys=keys,
            policy=policy,
            broadcasters=broadcasters,
            clock=clock,
            # Igual que en los swaps: un puente que sale en la stablecoin de su
            # red —el caso normal— ya está en la unidad del tope y no necesita
            # valoración. Esto cubre el otro caso, salir en ETH o en un token.
            valuation=ValueInReference(registry=registry),
        ),
        place_prediction_order=PlacePredictionOrder(
            registry=registry,
            gateway=gateway,
            keys=keys,
            policy=policy,
            broadcasters=broadcasters,
            clock=clock,
        ),        redeem_prediction=RedeemPrediction(
            registry=registry,
            gateway=gateway,
            keys=keys,
            policy=policy,
            broadcasters=broadcasters,
            clock=clock,
        ),
        # La retirada no cotiza nada —se manda lo que se manda—, así que no tiene
        # `prepare` del que tirar: construye su propia transferencia. Lo que sí
        # comparte con los otros dos caminos es la valoración, y por eso la
        # recibe: con el token nativo o con un token cualquiera, el importe hay
        # que traducirlo a la moneda del tope antes de compararlo, o el tope no
        # se está aplicando.
        withdraw_funds=WithdrawFunds(
            gateway=gateway,
            keys=keys,
            policy=policy,
            broadcasters=broadcasters,
            valuation=ValueInReference(registry=registry),
        ),
        policy=policy,
        keys=keys,
        passphrase=passphrase,
    )


def _watched_pairs(settings: Settings) -> list[WatchedPair]:
    """Resuelve los pares vigilados de la config contra el catálogo de tokens.

    Un par cuya base o quote no estén en el catálogo se omite con un aviso: la
    configuración puede quedar desactualizada y la app debe abrir igual, no
    morir por una entrada que no resuelve.
    """
    from amigocompora.domain.money import BasisPoints
    from amigocompora.engines import catalog

    resolved: list[WatchedPair] = []
    for entry in settings.watch_pairs:
        base = catalog.token_by_symbol(entry.base, entry.chain)
        quote = catalog.token_by_symbol(entry.quote, entry.chain)
        if base is None or quote is None:
            _log.warning(
                "watch_pair.unknown_token",
                chain=entry.chain,
                base=entry.base,
                quote=entry.quote,
            )
            continue
        try:
            pair = TradingPair(base=base, quote=quote)
            amount = base.amount(entry.amount)
        except Exception as error:
            _log.warning(
                "watch_pair.invalid",
                pair=f"{entry.base}/{entry.quote}",
                reason=str(error),
            )
            continue
        resolved.append(
            WatchedPair(
                pair=pair,
                amount_in=amount,
                min_net_bps=BasisPoints(entry.min_net_bps),
            )
        )
    return resolved


def _publish_opportunities(
    alert_center: AlertCenter,
    opportunities: tuple[Opportunity, ...],
) -> None:
    """Traduce oportunidades del barrido en alertas deduplicadas."""
    for opportunity in opportunities:
        alert_center.publish(
            kind=AlertKind.OPPORTUNITY,
            severity=severity_for_bps(opportunity.net_spread_bps.value),
            title=f"{opportunity.pair.symbol}: {opportunity.net_spread_bps} neto",
            detail=(
                f"{opportunity.best.venue.name} vs {opportunity.reference.venue.name} · "
                f"{opportunity.best.amount_out} vs {opportunity.reference.amount_out}"
            ),
            dedup_key=(
                f"opp:{opportunity.pair.symbol}:{opportunity.best.venue.venue_id}:"
                f"{opportunity.reference.venue.venue_id}"
            ),
        )


async def _activate_configured_engines(registry: EngineRegistry, settings: Settings) -> None:
    """Activa los motores de cada ranura: los configurados, o el de por omisión.

    El orden importa y es, de más a menos específico: lo que el usuario haya
    puesto en `config.toml` gana siempre; si no ha puesto nada, el preferido de
    la ranura, pero sólo si está instalado; y si tampoco, el único motor
    disponible de esa clase, cuando no haya ambigüedad.

    Una ranura puede llevar **varios** motores. El primero se activa —y con eso
    retira lo que hubiera— y los siguientes se **suman**. La diferencia no es un
    detalle de implementación: `activate` deja un motor solo, así que apilar con
    él borraría los precios de los demás, que es justo lo contrario de lo que se
    pide al escribir una lista. El orden de respuesta lo decide igualmente
    `swap_priority` del manifiesto, no el orden de la lista, de modo que añadir
    un respaldo detrás no cambia quién contesta primero.

    Un fallo al activar **no** aborta el arranque: la aplicación debe abrir y
    dejar que el usuario elija otro motor en el panel, no morir en el `main`. Y
    se registra por motor, para que un nombre mal escrito al final de una lista
    no se lleve por delante al que sí era correcto.

    Una ranura **declarada vacía** (`cross_chain = []`) se queda vacía, y no es lo
    mismo que no declararla: sin la clave rige el valor por omisión, con la clave
    puesta a cero el usuario está diciendo que no quiere ninguno. Es la misma
    lectura que `allowed_tokens`, donde una lista vacía significa «nada» y no «sin
    restricción»; sin esa distinción apagar una ranura no se podía ni escribir.
    """
    catalog = registry.available()
    installed = {manifest.engine_id for manifest in catalog}
    for kind in EngineKind:
        if kind.value in settings.active_engines:
            configured = _configured_engines(settings.active_engines[kind.value])
        else:
            preferred = DEFAULT_ENGINE_IDS.get(kind.value)
            fallback = preferred if preferred in installed else sole_engine_for(catalog, kind)
            configured = () if fallback is None else (fallback,)
        if not configured:
            _log.info("engine.slot_empty", kind=kind.value)
            continue

        for position, engine_id in enumerate(configured):
            try:
                if position == 0:
                    await registry.activate(engine_id)
                else:
                    await registry.add(engine_id)
            except Exception as error:
                _log.warning(
                    "engine.autostart_failed",
                    kind=kind.value,
                    engine_id=engine_id,
                    reason=str(error),
                )


def _configured_engines(value: str | tuple[str, ...] | None) -> tuple[str, ...]:
    """Los motores pedidos para una ranura, venga uno suelto o una lista."""
    if value is None:
        return ()
    return (value,) if isinstance(value, str) else value
