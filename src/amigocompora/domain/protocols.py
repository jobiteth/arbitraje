"""Contratos de los motores intercambiables (*hot-swappable engines*).

Son `typing.Protocol`, no clases base abstractas. La diferencia importa:

- El núcleo **nunca importa una clase concreta de motor**. Acoplamiento cero y
  arranque barato: el registro lee manifiestos sin instanciar nada.
- Un motor de terceros no hereda de nada nuestro; basta con que su firma encaje.
- Los tests usan dobles que implementan el protocolo sin heredar.

Un motor se publica a través de un `EngineProvider` registrado en el grupo de
entry points `amigocompora.engines`, lo que permite añadir motores instalando
un paquete, sin tocar el núcleo.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from amigocompora.domain.errors import EngineConfigError
from amigocompora.domain.models import (
    BridgeQuote,
    BridgeRequest,
    MarketDepth,
    PlannedTransaction,
    PredictionMarket,
    PredictionOrder,
    PredictionPosition,
    PredictionSide,
    Quote,
    SignedPredictionOrder,
    SubmittedPredictionOrder,
    Token,
    TradingPair,
    UnsignedTransaction,
    Venue,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.wallet import ChainHoldings, WalletProfile

#: Grupo de entry points donde se publican los motores.
ENTRY_POINT_GROUP: Final = "amigocompora.engines"


class EngineKind(StrEnum):
    """Ranura que ocupa un motor. Hay un motor activo por ranura."""

    DEX_QUOTES = "dex_quotes"
    PREDICTION_MARKETS = "prediction_markets"
    AI_ADVISOR = "ai_advisor"
    #: Puentes entre redes. Es una ranura y no una capacidad de `DEX_QUOTES`
    #: porque **no** comparte el modelo: un par de trading vive en una sola red
    #: —`TradingPair` lo exige— y un puente existe precisamente para cruzarlas, y
    #: puede entregar un token distinto del que recibe. Meterlo en la ranura de
    #: cotizaciones obligaría a relajar esa invariante para todo el mundo.
    #:
    #: Añadir la ranura no necesitó tocar la configuración: `active_engines` está
    #: indexado por el valor de la ranura, y `build_container` recorre el enum.
    CROSS_CHAIN = "cross_chain"
    #: Cartera: leer qué tiene una dirección, red por red.
    #:
    #: Es una ranura propia y no una capacidad de `DEX_QUOTES` por la misma razón
    #: que los puentes: no comparte el modelo. Una cotización es una **oferta de
    #: cambio** —hay un precio, hay un par, hay un venue que se compromete— y un
    #: saldo es un **hecho** sobre una dirección, que no cotiza nada ni se
    #: construye contra nadie. Además las direcciones de una cartera no son las de
    #: un par: una cartera EVM vale en ocho redes a la vez, y un par vive en una.
    #:
    #: Que sea ranura y no motor suelto es lo que permite sustituirlo: mañana un
    #: motor de cartera por WalletConnect o por un Ledger tiene que producir
    #: exactamente los mismos `WalletSnapshot` y entrar por aquí sin tocar el
    #: núcleo.
    WALLET = "wallet"

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS: Final[Mapping[EngineKind, str]] = {
    EngineKind.DEX_QUOTES: "Cotizaciones DEX",
    EngineKind.PREDICTION_MARKETS: "Mercados de predicción",
    EngineKind.AI_ADVISOR: "Asistente de IA",
    EngineKind.CROSS_CHAIN: "Puentes entre redes",
    EngineKind.WALLET: "Cartera",
}


@dataclass(frozen=True, slots=True)
class EngineManifest:
    """Metadatos declarados por un motor, legibles sin instanciarlo."""

    engine_id: str
    name: str
    version: str
    kind: EngineKind
    summary: str
    #: Capacidades que el motor puede ejercer. Son informativas: la barrera
    #: real está en el caso de uso, que exige la capacidad al `ModeGuard` antes
    #: de llamar al motor. El registro las publica para que la UI pueda avisar
    #: de qué parte de un motor está inerte en el modo activo.
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    #: Redes en las que el motor sabe **construir** un swap, no sólo leer
    #: precios. Vacío = el motor no construye nada.
    #:
    #: Va en el manifiesto y no se deduce instanciando porque la decisión de a
    #: qué motor pedirle el payload hay que tomarla **antes** de abrirlo: abrir
    #: varios motores para preguntarles si saben hacer algo gastaría conexiones
    #: y cuota en una pregunta que el manifiesto ya responde.
    #:
    #: Es lo que hace que cubrir una red nueva no toque el núcleo. Un motor que
    #: añada una red la declara aquí y el caso de uso lo encuentra solo; y como
    #: varios motores pueden declarar la misma red, la preferencia entre ellos
    #: —el agregado o el directo— es una decisión de configuración, no de código.
    swap_chains: frozenset[str] = field(default_factory=frozenset)
    #: Redes de **origen** desde las que el motor sabe cruzar a otra. Mismo papel
    #: que `swap_chains` y misma razón para estar en el manifiesto —saber quién
    #: puede antes de abrirlo—, pero en una lista aparte porque son preguntas
    #: distintas: un motor puede saber construir un swap en una red y no cruzar
    #: desde ella, y al revés. Mezclarlas haría que un puente apareciera como
    #: candidato a construir swaps de DEX, y que la pestaña de swap ofreciera una
    #: ruta entre redes donde no la hay.
    #:
    #: Se indexa por el origen y no por el destino porque el contrato que se
    #: contrasta antes de firmar es el del payload, y el payload vive en el
    #: origen: es ahí donde el motor tiene que declarar a dónde manda el dinero.
    bridge_chains: frozenset[str] = field(default_factory=frozenset)
    #: Orden de preferencia entre los motores que cruzan desde la **misma** red.
    #: Menor = se le pregunta primero. Sólo decide a quién se le pide la ruta,
    #: nunca cuál se le enseña al usuario como mejor: eso lo decide el importe
    #: que cada uno entrega. Ver `domain.models.rank_bridges`.
    bridge_priority: int = 100
    #: Orden de preferencia entre los motores que declaran la **misma** red en
    #: `swap_chains`. Menor = se le pide primero el payload.
    #:
    #: Existe porque «qué motor construye este swap» no puede decidirse por el
    #: orden en que el usuario activó los motores: eso cambiaría la ruta de
    #: ejecución cada vez que alguien reordena la lista en la interfaz, y un
    #: cambio de preferencia debe ser una decisión, no un efecto secundario. Con
    #: la prioridad en el manifiesto, la preferencia es explícita y comprobable.
    #:
    #: El valor por omisión es el mismo para todos, así que en caso de empate
    #: desempata el `engine_id`: el orden tiene que ser determinista y no
    #: depender del orden de inserción de un diccionario.
    swap_priority: int = 100
    #: Redes cuyos saldos este motor sabe leer. Mismo papel que `swap_chains` y
    #: `bridge_chains` —saber quién puede antes de abrirlo— y en una lista aparte
    #: por la misma razón que aquellas: un motor puede saber leer saldos en una
    #: red y no construir swaps en ella, y al revés. Mezclarlas haría que un
    #: lector de cartera apareciera como candidato a construir swaps.
    #:
    #: Se declara **por red** y no «todas» porque lo que el motor cubre de
    #: verdad depende de que haya nodos medidos para esa red: una red sin
    #: endpoints no se puede leer, y prometerla en el manifiesto daría una
    #: cartera con una red que siempre falla.
    wallet_chains: frozenset[str] = field(default_factory=frozenset)
    #: Claves de configuración obligatorias (API keys, URLs de RPC). Se
    #: resuelven desde el keyring del SO, nunca desde disco.
    required_config: tuple[str, ...] = ()
    #: Claves que mejoran el motor pero no le hacen falta para funcionar. El
    #: caso real: una API key de plan gratuito que sube el límite de peticiones.
    #: Sin ella el motor funciona más despacio; con ella, más rápido. Separarlas
    #: de las obligatorias es lo que permite que un motor así **arranque** sin
    #: configurar nada y que el panel de motores pueda ofrecer la mejora sin
    #: presentarla como un error.
    optional_config: tuple[str, ...] = ()
    #: Allowlist de hosts. Mínimo privilegio: el cliente HTTP que recibe el
    #: motor rechaza cualquier host fuera de esta lista. `()` = no usa red.
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.engine_id:
            raise EngineConfigError("el motor necesita un engine_id")
        if not self.name or not self.version:
            raise EngineConfigError(f"el motor {self.engine_id} necesita name y version")
        overlap = set(self.required_config) & set(self.optional_config)
        if overlap:
            raise EngineConfigError(
                f"el motor {self.engine_id} declara {', '.join(sorted(overlap))} "
                f"como obligatoria y opcional a la vez"
            )
        # Un manifiesto que promete construir swaps en alguna red y no declara
        # la capacidad se contradice. Se detecta al construirlo —que es cuando
        # se escribe— en vez de cuando alguien pide un swap y no aparece.
        if self.swap_chains and Capability.PREPARE_TX not in self.capabilities:
            raise EngineConfigError(
                f"el motor {self.engine_id} declara redes de swap "
                f"({', '.join(sorted(self.swap_chains))}) pero no declara la "
                f"capacidad {Capability.PREPARE_TX.value}"
            )
        # Una prioridad negativa no significa nada —no hay un «antes del
        # primero»— y casi siempre sería un cero de más al teclear. Se detecta
        # al construir el manifiesto, que es cuando se escribe.
        if self.swap_priority < 0:
            raise EngineConfigError(
                f"el motor {self.engine_id} declara swap_priority="
                f"{self.swap_priority}: la prioridad no puede ser negativa"
            )
        # La misma coherencia para los puentes: declarar redes de cruce sin la
        # capacidad de preparar transacciones sería prometer un payload que el
        # motor no puede dar.
        if self.bridge_chains and Capability.PREPARE_TX not in self.capabilities:
            raise EngineConfigError(
                f"el motor {self.engine_id} declara redes de puente "
                f"({', '.join(sorted(self.bridge_chains))}) pero no declara la "
                f"capacidad {Capability.PREPARE_TX.value}"
            )
        if self.bridge_priority < 0:
            raise EngineConfigError(
                f"el motor {self.engine_id} declara bridge_priority="
                f"{self.bridge_priority}: la prioridad no puede ser negativa"
            )
        # Leer saldos no es preparar transacciones: un motor de cartera **no**
        # necesita PREPARE_TX, y exigírselo sería pedirle la capacidad de mover
        # dinero para poder mirarlo. Lo que sí tiene que declarar es que lee la
        # cadena, porque es exactamente lo que hace.
        if self.wallet_chains and Capability.READ_CHAIN not in self.capabilities:
            raise EngineConfigError(
                f"el motor {self.engine_id} declara redes de cartera "
                f"({', '.join(sorted(self.wallet_chains))}) pero no declara la "
                f"capacidad {Capability.READ_CHAIN.value}"
            )

    @property
    def needs_network(self) -> bool:
        return bool(self.allowed_hosts)

    @property
    def needs_secrets(self) -> bool:
        return bool(self.required_config)

    @property
    def config_options(self) -> tuple[str, ...]:
        """Todas las claves que el motor sabe usar, obligatorias primero."""
        return (*self.required_config, *self.optional_config)


#: Resuelve la configuración de un motor (API keys, URLs de RPC) a partir de su
#: manifiesto. El contrato vive aquí, en el dominio, para que `app` lo consuma y
#: `infra` lo implemente sin que ninguna de las dos dependa de la otra.
ConfigResolver = Callable[[EngineManifest], Mapping[str, str]]


def validate_config(manifest: EngineManifest, config: Mapping[str, str]) -> None:
    """Comprueba que están todas las claves obligatorias del motor.

    Se llama antes de instanciar, para que un motor mal configurado falle con un
    mensaje accionable en vez de a mitad de una petición.
    """
    missing = [key for key in manifest.required_config if not config.get(key)]
    if missing:
        raise EngineConfigError(
            f"al motor «{manifest.name}» le falta configuración: {', '.join(missing)}"
        )


# --------------------------------------------------------------------------- #
# Ciclo de vida común
# --------------------------------------------------------------------------- #
@runtime_checkable
class Engine(Protocol):
    """Base de todos los motores: manifiesto y ciclo de vida explícito.

    `aopen()`/`aclose()` existen para que el registro pueda intercambiar un
    motor en caliente liberando de forma determinista sus conexiones, en vez de
    depender del recolector de basura.
    """

    @property
    def manifest(self) -> EngineManifest: ...

    async def aopen(self) -> None:
        """Reserva recursos. Idempotente."""
        ...

    async def aclose(self) -> None:
        """Libera recursos. Idempotente, y no debe lanzar."""
        ...


# --------------------------------------------------------------------------- #
# Ranuras
# --------------------------------------------------------------------------- #
@runtime_checkable
class DexQuoteEngine(Engine, Protocol):
    """Lectura de precios y liquidez en exchanges descentralizados."""

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        """Venues que este motor puede cotizar en la red dada.

        `chain_key` es la clave del registro de redes (`"ethereum"`, `"solana"`),
        no un chain id numérico: ver `domain.chains`. Una red que el motor no
        cubra devuelve una secuencia vacía, no un error: que una fuente no
        llegue a una red es normal, no excepcional.
        """
        ...

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        """Cotiza vender `amount_in` de la base del par, en cada venue.

        Devuelve una cotización por venue con liquidez suficiente. Un venue sin
        liquidez se omite en lugar de devolver una cotización de cero.
        """
        ...


@runtime_checkable
class SwapPlanner(Engine, Protocol):
    """Capacidad **opcional** de un motor DEX: construir el payload de un swap.

    Deliberadamente separada de `DexQuoteEngine`. Un motor de sólo lectura no
    tiene por qué implementarla, y el caso de uso comprueba con `isinstance` si
    el motor activo la soporta antes de ofrecer la acción al usuario. Así la
    superficie de código capaz de generar transacciones se mantiene mínima y
    localizable.

    Extiende `Engine` —y no es un protocolo suelto— porque un planificador **es**
    un motor: tiene manifiesto y ciclo de vida, y quien lo elige necesita poder
    leer su `engine_id` para comprobar que es el mismo que observó la cotización.
    Sin eso habría que adivinar qué motor construye cada cotización.

    Construir no es firmar ni emitir: devuelve una `PlannedTransaction` para
    que el usuario la inspeccione o la exporte. La aplicación no tiene claves.

    ### Dos clases de backend, el mismo contrato

    La ranura no distingue cómo se obtuvo el payload, y por eso caben las dos
    formas de llegar a un swap:

    - **Agregado.** Un motor que reparte la orden entre varios venues y
      devuelve el resultado ya neto (caso medido: Jupiter en Solana). Se
      activa cuando hay que ejecutar a cualquier precio disponible.
    - **Directo.** Un motor que habla con un venue concreto —el pool que la
      comparación señaló como mejor— y construye contra él. Se activa cuando
      se sabe dónde se quiere ejecutar y no se quiere ceder la ruta a un
      tercero.

    Añadir el segundo no toca el núcleo: se publica otro entry point y se
    activa en la ranura, igual que cualquier motor. Lo que **no** cambia entre
    backends es lo que este contrato garantiza: la implementación decide cómo
    llega al payload, y responde de que el payload corresponde a la cotización
    que se le pasó.
    """

    async def plan_swap(self, quote: Quote, *, recipient: str) -> PlannedTransaction: ...

    def expected_destination(self, chain_key: str) -> str | None:
        """El contrato al que este motor dirige los swaps en esa red, o `None`.

        Existe para que el camino de ejecución pueda contrastar, **antes de
        firmar**, que el `to` del payload está entre los destinos que el motor
        declara para esa red. Un payload cuyo destino no sea uno de ellos es
        exactamente lo que hay que no firmar: ni un motor mal configurado ni una
        respuesta manipulada deberían poder desviar los fondos.

        Lo declara el motor y no el núcleo porque la tabla es suya y está medida
        contra el proveedor: `infra.evm` no puede importar `engines` —la
        dependencia va `infra → domain`—, así que si el dato viviera allí, o se
        duplicaría aquí o el contraste no podría existir.

        `None` significa «este motor no declara destino para esa red», y quien
        lo consulte tiene que decidir qué hacer con eso; en el camino de
        ejecución, no firmar.

        ### Añadir un método aquí no es aditivo, y conviene saberlo

        `SwapPlanner` es `runtime_checkable`, y esa comprobación mira **qué
        métodos existen**, no sus firmas. Así que un método nuevo aquí invalida
        de golpe a todo motor que no lo tenga, y no sólo para ejecutar: el
        registro deja de reconocerlo y tampoco podrá preparar swaps. Se midió al
        añadir este método: tres pruebas de Solana y tres de la pila de motores
        empezaron a fallar porque a sus dobles les faltaba.

        Es el precio de que la comprobación sea estructural, y se paga a
        conciencia: un motor que construye payloads **tiene** que poder decir a
        dónde van. Lo que hay que recordar es que el próximo método que se añada
        aquí obliga a tocar todos los motores y todos los dobles, y que el
        olvido no se ve en el tipo —se ve en las pruebas, si existen.
        """
        ...


@runtime_checkable
class CrossChainPlanner(Engine, Protocol):
    """Cotizar y construir el cruce de un token entre dos redes.

    ### Por qué no reutiliza `SwapPlanner`

    Un swap tiene **un** payload, en la red donde ocurre, y se firma una vez. Un
    puente tiene también **un** payload que se firma —el del origen, que es donde
    están los fondos—, pero con dos diferencias que cambian decisiones de dinero:

    - El destino del payload es un **contrato puente**, no un router de un DEX, y
      lo que se envía no vuelve a esta red. Un destino equivocado no pierde
      dinero en una operación de mercado: lo manda a otra parte.
    - El importe que se recibe **no** se conoce al firmar. Se estima, y lo
      garantizado es `amount_out_min`. Durante los minutos que el puente tarda,
      el mercado se mueve, y quien firma tiene que haber visto las dos cifras.

    Por eso el modelo es `BridgeQuote` y no `Quote`, y por eso la comparación es
    entre rutas de proveedores distintos y no entre venues de una red.

    ### El contrato

    `quote_bridge` devuelve **una ruta por proveedor subyacente** con liquidez
    suficiente —un agregado puede ofrecer varios puentes para el mismo par, y
    elegir el mejor es del usuario, no del motor—. Una ruta imposible se omite en
    lugar de devolver un cero.

    `plan_bridge` vuelve a cotizar contra el proveedor con el destinatario real y
    devuelve el payload del origen. Que la cotización se repita y no se guarde es
    deliberado: el destinatario forma parte de lo que se firma, y el payload de
    una cotización pedida para otro destinatario no sirve. La implementación
    **responde de que el importe no se haya ido** —el mismo contraste que hace
    `SwapPlanner`, con su tolerancia— y falla en vez de firmar algo peor de lo
    que se enseñó.

    `expected_destination` es el mismo contraste que en `SwapPlanner` y por la
    misma razón, con una diferencia: aquí la tabla se indexa por la red de
    **origen**, que es donde vive el contrato al que se manda el dinero. Un
    puente cuyo `to` no sea uno de los declarados para esa red no se firma.
    """

    async def quote_bridge(self, request: BridgeRequest) -> Sequence[BridgeQuote]:
        """Rutas disponibles para cruzar `request.amount_in`, una por puente."""
        ...

    async def plan_bridge(
        self, quote: BridgeQuote, *, recipient: str
    ) -> UnsignedTransaction:
        """El payload de origen que hay que firmar para ejecutar `quote`.

        `recipient` es quien recibe en la red de destino, y se pasa aparte porque
        puede **no** ser quien firma: firmar desde una dirección y recibir en
        otra es legítimo y es como se comprueba que el puente llega.
        """
        ...

    def expected_destination(self, chain_key: str) -> str | None:
        """El contrato al que este motor manda los fondos **desde** esa red.

        `None` significa «no declaro destino para esa red», y el camino de
        ejecución lo trata como motivo para no firmar. Indexado por la red de
        origen y no por la de destino porque el contrato que se contrasta es el
        del payload, y el payload vive en el origen.
        """
        ...


@runtime_checkable
class WalletEngine(Engine, Protocol):
    """Leer qué tiene una dirección, red por red.

    ### Lo que este contrato **no** puede hacer, y por qué está escrito aquí

    No firma, no emite y no acepta una clave. Ni siquiera mueve dinero «para
    probar»: mover dinero vive en el camino de ejecución, que pasa por la barrera
    de modo y por el diálogo de confirmación. Un motor —que puede venir de un
    paquete de terceros y declara sus propios hosts— que pudiera firmar sería un
    motor capaz de vaciar la cartera que acaba de leer. La capacidad declarada es
    `READ_CHAIN` y no hay ninguna otra en el manifiesto.

    ### Una red por llamada, y el reparto lo hace quien llama

    `holdings` lee **una** red. Barrer ocho es una decisión de producto —cuántas
    a la vez, con qué tope, qué hacer si una tarda— y no una propiedad del motor:
    ponerla aquí obligaría a cada motor a reimplementar el reparto, y los topes
    de concurrencia tienen que ser visibles y configurables en un solo sitio, no
    escondidos dentro de cada implementación.

    Tampoco lanza cuando una red falla: devuelve el `ChainHoldings` con su
    `error`. Una cartera de ocho redes donde una no contesta tiene que seguir
    pintándose, y el reparto que llama a esto en paralelo no puede quedarse con
    las otras siete porque una lanzara.
    """

    async def holdings(self, profile: WalletProfile, chain_key: str) -> ChainHoldings:
        """Lo que hay en esa red para esa cartera, o el motivo por el que no se pudo.

        Recibe el **perfil** y no una dirección suelta porque el perfil lleva la
        familia —EVM o Solana—, y es eso lo que decide cómo se lee. Una dirección
        sin su familia obligaría al motor a adivinarla por el formato, que es
        justo la comprobación que el perfil ya hizo al construirse.
        """
        ...


@runtime_checkable
class PredictionMarketEngine(Engine, Protocol):
    """Lectura de mercados de predicción y sus probabilidades implícitas."""
    async def markets(
        self,
        *,
        limit: int = 20,
        search: str | None = None,
        closing_within: timedelta | None = None,
    ) -> Sequence[PredictionMarket]:
        """Mercados abiertos, por volumen; o los que cierran dentro de una ventana.

        `closing_within` no es un filtro más: cambia el **orden**. Sin él se pide
        lo de más volumen, que es donde los precios significan algo. Con él se
        pide lo que antes cierra, que es la vista de «lo que está terminando», y
        un mercado sin fecha de cierre legible se queda fuera: no se puede
        afirmar que cierre pronto quien no dice cuándo cierra.
        """
        ...

    async def market(self, market_id: str) -> PredictionMarket: ...

    async def book(self, token_id: str) -> MarketDepth:
        """El libro real de un resultado: qué hay que pagar por cada participación.

        Es lo que convierte «el precio es 0,16» en «5 participaciones cuestan
        0,80 y hay 57.268 en venta a ese precio». Sin esto, el coste de una orden
        es una multiplicación sobre un precio que sólo es cierto para la primera
        participación.
        """
        ...


@runtime_checkable
class PredictionOrderPlanner(PredictionMarketEngine, Protocol):
    """Capacidad **opcional** de un motor de predicción: construir, firmar y publicar.

    Hermano de `SwapPlanner`, y separado de `PredictionMarketEngine` por la misma
    razón: leer mercados y operarlos son dos superficies distintas, y un motor de
    sólo lectura no debería verse obligado a declarar la segunda.

    ### Por qué esto no se parece a un swap

    Un swap termina en una transacción: se construye, se firma, se emite y la red
    dice si valió. Una orden de predicción **no toca la cadena al publicarse**. Es
    un mensaje firmado que se le entrega al recinto, y es el recinto quien la cruza
    contra su libro y, más tarde, quien liquida. Eso parte el trabajo en tres pasos
    que aquí se declaran por separado y a propósito:

    1. `build_order` — decide **qué** se firma. Puro: sin red, sin claves. Se puede
       enseñar al usuario y probarlo entero sin gastar una petición.
    2. `sign_order` — lo firma. Es donde hace falta la clave, y sólo aquí.
    3. `submit_order` — lo publica y devuelve el identificador del **recinto**, que
       es el único que puede darlo.

    Separarlos es lo que permite que el diálogo de confirmación muestre la orden
    exacta antes de que exista una firma, y que la firma se pueda verificar
    —recuperando la dirección— sin publicar nada.

    ### Dónde entra la clave, y por qué en dos sitios

    `build_order` no la ve: decide **qué** se firma, y eso se puede enseñar al
    usuario antes de que exista ningún secreto en juego.

    `sign_order` y `submit_order` sí la piden, prestada y por llamada, y la razón
    del segundo no es evidente: publicar exige credenciales de nivel 2, y el
    recinto sólo las entrega a quien firma un mensaje de nivel 1 con la clave. No
    hay forma de publicar sin ella. Lo que sí se evita es **guardarla**: llega como
    argumento, se usa para firmar y para derivar, y se suelta. Ni el motor ni el
    cliente del recinto la retienen más allá de la llamada, y ninguno la escribe en
    un log ni en la orden que construyen.
    """

    def build_order(
        self,
        market: PredictionMarket,
        *,
        outcome_label: str,
        side: PredictionSide,
        size: Decimal,
        price: Decimal,
    ) -> PredictionOrder:
        """Construye la orden. Puro: sin red y sin claves.

        Lanza si el mercado no es operable —sin identificadores, sin salto de
        precio, sin mínimo— en vez de devolver una orden a medias: una orden
        construida sobre un mercado al que le falta un dato se firma igual y la
        rechaza el recinto, que es el peor sitio para enterarse.
        """
        ...

    def sign_order(
        self, order: PredictionOrder, *, private_key: str
    ) -> SignedPredictionOrder:
        """Firma la orden y **comprueba su propia firma** antes de devolverla.

        La comprobación no es ceremonia: recupera la dirección desde la firma y
        falla si no es la de la clave. Un fallo aquí se detecta sin red y sin
        fondos, mientras que el mismo fallo descubierto al publicar cuesta una
        orden rechazada y una tarde de duda entre «la firma» y «el saldo».
        """
        ...

    async def submit_order(
        self, signed: SignedPredictionOrder, *, private_key: str
    ) -> SubmittedPredictionOrder:
        """Publica la orden y devuelve lo que el recinto contestó.

        El identificador lo da el recinto, no se calcula: el resumen EIP-712 que
        se firmó es un dato distinto y darlo por equivalente sería afirmar algo no
        medido en el asiento del registro.

        Y devuelve además **el estado en su vocabulario**, sin traducir: es lo
        que permite anotar si la orden quedó viva en el libro o ya cruzada, que
        son dos cosas muy distintas para quien tiene que ir a buscarla después.
        """
        ...

    def exchange_for(self, market: PredictionMarket) -> str:
        """El contrato contra el que se firma, para poder contrastarlo antes.

        Igual que `expected_destination` en `SwapPlanner`: deja que quien va a
        firmar compruebe **contra qué contrato** se firma, en vez de enterarse por
        el `verifyingContract` de un mensaje ya construido. La tabla es del motor
        porque está medida contra el proveedor, y `domain` no puede tenerla: la
        dependencia va `engines → domain`, no al revés.
        """
        ...

    def collateral_for(self, market: PredictionMarket) -> Token:
        """El token con el que liquida ese mercado, que es el que se mueve.

        Lo declara el motor por la misma razón que `exchange_for`: es un hecho de
        su recinto, no una preferencia de la aplicación. La capa de aplicación
        necesita saberlo para dos cosas que no puede deducir —en qué unidad se
        mide el importe contra los topes, y sobre qué contrato hay que pedir el
        permiso antes de firmar—, y las dos serían una suposición si las
        escribiera ella.
        """
        ...

    def shares_collection_for(self, market: PredictionMarket) -> str:
        """El contrato donde viven las participaciones de ese mercado.

        Hace falta para **vender**: entregar participaciones exige autorizar al
        recinto sobre la colección que las contiene, y esa colección es un hecho
        del recinto —una dirección medida— que la capa de aplicación no tiene por
        qué conocer. Escribirla allí ataría el caso de uso a un recinto concreto.
        """
        ...


@runtime_checkable
class PredictionRedeemer(PredictionMarketEngine, Protocol):
    """Capacidad **opcional**: leer lo que se tiene y construir el cobro.

    Tercer hermano de `SwapPlanner` y `PredictionOrderPlanner`, y el más corto de
    los tres, porque el cobro es lo más simple que se puede hacer con dinero en
    una cadena: no hay precio que negociar, ni libro, ni contraparte. Se llama a
    un contrato, se le entregan unas participaciones y devuelve el colateral.

    ### Por qué no reutiliza `PredictionOrderPlanner`

    Porque no comparten **nada** de lo que hacen. Aquél construye un mensaje
    firmado que se publica en un libro y liquida más tarde, con un precio, un
    lado y un salto que respetar; éste construye una transacción que se emite y
    termina en el mismo bloque. Meterlos en el mismo protocolo obligaría a un
    motor de sólo órdenes a declarar métodos que no sabe implementar, que es
    justo lo que la separación de `PredictionMarketEngine` ya evita.

    ### Lo que sí comparte con el planificador

    `collateral_for`, y no por casualidad: es **el mismo** colateral, y el cobro
    lo devuelve en la misma unidad en la que la orden lo cobró. Declararlo dos
    veces sería tener dos sitios donde podría decirse una cosa distinta.
    """

    async def positions(
        self, *, wallet: str, redeemable_only: bool = False
    ) -> tuple[PredictionPosition, ...]:
        """Lo que esa cartera tiene en este recinto, leído del recinto.

        `redeemable_only` filtra en el servidor a lo que ya se puede cobrar. No es
        una comodidad: una cartera con historial tiene cientos de posiciones
        cerradas o perdedoras, y traérselas todas para descartarlas aquí sería
        traer la mayor parte de la respuesta para tirarla.

        Devuelve la tupla vacía —y no un error— cuando la cartera no tiene nada:
        «no tengo posiciones» es una respuesta, no un fallo, y confundirlas
        dejaría la pantalla diciendo que algo se rompió cuando lo que pasa es que
        no hay nada que cobrar.
        """
        ...

    def collateral_on(self, chain_key: str) -> Token:
        """El colateral del recinto en esa red, sin necesitar un mercado delante.

        `collateral_for` lo pide a partir de un mercado, y el cobro no tiene uno:
        tiene posiciones. Es el **mismo** token y la **misma** búsqueda —por
        dirección y no por símbolo, porque el colateral del recinto y el USDC
        nativo de la red publican los dos `symbol() == "USDC"`—, así que
        declararlo aquí evita que el caso de uso tenga que fabricarse un mercado
        falso sólo para preguntar por el colateral, y evita algo peor: que lo
        buscara por su cuenta y diera con el otro USDC.
        """
        ...

    def build_redeem(
        self, positions: Sequence[PredictionPosition], *, wallet: str
    ) -> UnsignedTransaction:
        """Construye la transacción que cobra **un** mercado. Puro, sin claves.

        Puro como `build_order`: entra lo que se tiene, sale lo que se firmaría,
        y se puede enseñar entero antes de que exista ninguna firma.

        Cobra **las que se le pasan** y no «todas las que haya»: el conjunto lo
        decide quien las enseñó, y volver a consultarlo aquí permitiría que se
        firmara algo distinto de lo que el usuario confirmó.

        Las posiciones tienen que ser **todas del mismo mercado**, y si no lo son
        el motor lanza. El contrato cobra un mercado por llamada, así que agrupar
        es inevitable — pero agrupar por dentro devolvería la transacción de un
        mercado cuando se pidieron dos, y el segundo quedaría sin cobrar sin que
        nadie se entere. Agrupa quien llama, que es quien tiene que enseñarlo
        antes de firmar y quien anota un asiento por cada cobro.
        """
        ...


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    """Petición al asistente de IA.

    `context` lleva los datos **ya obtenidos** por la aplicación. El motor de IA
    no sale a la red a buscar información por su cuenta: así lo que analiza es
    exactamente lo que el usuario está viendo en pantalla, y es reproducible.
    """

    question: str
    context: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Respuesta del asistente. Siempre acompañada de su advertencia."""

    summary: str
    findings: tuple[str, ...] = ()
    disclaimer: str = (
        "Análisis generado por IA sobre los datos mostrados. No es asesoramiento "
        "financiero: verifica las cifras antes de decidir."
    )


@runtime_checkable
class AiAdvisorEngine(Engine, Protocol):
    """Asistente analítico. Propone lecturas; no ejecuta nada."""

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult: ...


# --------------------------------------------------------------------------- #
# Publicación
# --------------------------------------------------------------------------- #
@runtime_checkable
class EngineProvider(Protocol):
    """Fábrica de un motor, publicada en el entry point.

    Separar la fábrica del motor permite al registro listar todo lo instalado
    —con su manifiesto, requisitos y hosts— sin instanciar ni abrir conexiones.
    """

    @property
    def manifest(self) -> EngineManifest: ...

    def create(self, config: Mapping[str, str]) -> Engine:
        """Instancia el motor. No debe hacer I/O: eso es trabajo de `aopen()`."""
        ...
