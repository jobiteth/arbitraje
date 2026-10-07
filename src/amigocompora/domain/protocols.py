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
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from amigocompora.domain.errors import EngineConfigError
from amigocompora.domain.models import (
    PlannedTransaction,
    PredictionMarket,
    Quote,
    TradingPair,
    Venue,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount

#: Grupo de entry points donde se publican los motores.
ENTRY_POINT_GROUP: Final = "amigocompora.engines"


class EngineKind(StrEnum):
    """Ranura que ocupa un motor. Hay un motor activo por ranura."""

    DEX_QUOTES = "dex_quotes"
    PREDICTION_MARKETS = "prediction_markets"
    AI_ADVISOR = "ai_advisor"

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS: Final[Mapping[EngineKind, str]] = {
    EngineKind.DEX_QUOTES: "Cotizaciones DEX",
    EngineKind.PREDICTION_MARKETS: "Mercados de predicción",
    EngineKind.AI_ADVISOR: "Asistente de IA",
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
