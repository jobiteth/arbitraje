"""Leer la cartera: qué hay, en qué red, y cuánto vale.

### Dos pasos y no uno, porque la velocidad y el precio no cuestan lo mismo

`ReadWallet` lee **saldos**: una llamada por red como mucho en EVM, tres en
Solana, y ninguna cotización. Devuelve una foto con las cantidades y sin valores.

`ValueWallet` coge esa foto y le añade el equivalente en la stablecoin de cada
red, que es **una cotización por token con fondos**: en una cartera de ocho redes
con cuatro tokens cada una son treinta y pico peticiones a los motores de precios.

Están separados a propósito. Si fueran uno, la interfaz no podría pintar nada
hasta que terminara la cotización número treinta, y el usuario miraría una
pantalla vacía mientras se resuelve una pregunta —«¿cuánto vale?»— que no es la
que hizo. Llamando a los dos seguidos, los saldos aparecen en cuanto llegan y los
importes se rellenan después. Y quien sólo quiera saber si tiene fondos en una
red llama al primero y no paga el segundo.

### Qué redes se intentan, y por qué eso es una decisión

No se intentan todas las de la familia de la cartera: se intentan **las que el
motor declara que sabe leer** (`manifest.wallet_chains`), cruzadas con las que le
corresponden a esa cartera. La diferencia no es teórica y está medida: `CHAINS`
tiene once redes, el motor declara nueve —las que tienen nodos medidos— y una
cartera EVM «cubre» diez por familia. Leyendo las once, `arc` y `robinhood`
fallarían **siempre**, la foto saldría incompleta siempre y el patrimonio no se
podría afirmar nunca. Una red que se sabe que no se puede leer no se intenta: se
declara fuera, y el motor es quien dice cuáles son.

### La barrera de modo

Se exige `READ_CHAIN` al `ModeGuard` como cualquier otro caso de uso. Es una
lectura y todos los modos la conceden, así que en la práctica no bloquea nada —
pero pasa por la misma puerta que todo lo demás, y eso es lo que hace que la
tabla de política siga siendo la única fuente de verdad sobre qué se puede hacer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Final

import structlog

from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.value_in_reference import ReferenceValuation
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import EngineConfigError, NoActiveEngineError
from amigocompora.domain.models import Token
from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import EngineKind, WalletEngine
from amigocompora.domain.wallet import (
    ChainHoldings,
    TokenHolding,
    WalletProfile,
    WalletSnapshot,
)
from amigocompora.engines.catalog import quote_token, token_by_address

_log = structlog.get_logger(__name__)

#: Cuántas redes se leen a la vez. No es prudencia decorativa: los nodos
#: públicos limitan por IP, y lanzar once redes de golpe contra el mismo host es
#: la forma más rápida de ganarse un 429 que dejaría **todas** sin leer. Con seis
#: a la vez y el failover del pool detrás, una red lenta no bloquea a las demás.
DEFAULT_CONCURRENCY: Final = 6

#: Cuántas valoraciones a la vez. Más alto que el de redes porque quien cotiza
#: son los motores de precios, que van contra APIs con su propia cuota y ya
#: tienen su failover; y porque cada valoración es independiente de las otras.
DEFAULT_VALUATION_CONCURRENCY: Final = 4

#: Cuántas posiciones se valoran como mucho **por red y por foto**.
#:
#: No es prudencia decorativa, es una medida. Una cartera de Solana leída en vivo
#: el 2026-10-07 traía **3102 mints con fondos** —casi todos regalos de spam— y
#: valorarlos todos son 3102 cotizaciones contra los motores de precios: minutos
#: de espera, cuota agotada y ningún dato útil, porque esos mints no tienen
#: mercado. Con presupuesto, la red contesta en segundos y dice cuántas
#: posiciones dejó sin valorar, que es lo que hace defendible el número que da.
#:
#: Cincuenta es holgado para una cartera de verdad —una persona tiene unos pocos
#: tokens, no cincuenta— y corta en seco el caso patológico.
DEFAULT_VALUATION_BUDGET: Final = 50

#: Lo que se apunta en `missing` cuando la red no tiene moneda de referencia.
SIN_REFERENCIA: Final = "no hay moneda de referencia para valorar esta red"


@dataclass(frozen=True, slots=True)
class ReadWallet:
    """Los saldos de una cartera, red por red, sin valorarlos."""

    registry: EngineRegistry
    guard: ModeGuard
    clock: Clock | None = None

    async def __call__(
        self,
        profile: WalletProfile,
        *,
        chains: tuple[str, ...] | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> WalletSnapshot:
        """La foto de la cartera. `chains` limita a esas redes si se pasa.

        El motor **no lanza** cuando una red falla: devuelve el `ChainHoldings`
        con su motivo. Eso es lo que permite que un reparto en paralelo no pierda
        las otras diez redes porque una no contestara, y por eso aquí se recogen
        resultados en vez de excepciones.
        """
        self.guard.require(Capability.READ_CHAIN)
        engine = self._engine()
        reloj = self.clock or SystemClock()

        objetivo = self._chains_for(engine, profile, chains)
        if not objetivo:
            _log.info("wallet.sin_redes_que_leer", cartera=profile.wallet_id)
            return WalletSnapshot(profile=profile, chains=(), read_at=reloj.now())

        semaforo = asyncio.Semaphore(max(1, concurrency))

        async def una(chain_key: str) -> ChainHoldings:
            async with semaforo:
                try:
                    return await engine.holdings(profile, chain_key)
                except Exception as error:
                    # El protocolo dice que el motor no lanza, y el nuestro no lo
                    # hace; pero un motor puede venir de un paquete de terceros, y
                    # una excepción suya no puede dejar al usuario sin ver sus
                    # otras diez redes. Se contiene **por red**, que es la misma
                    # decisión que ya toma el motor consigo mismo.
                    _log.warning(
                        "wallet.motor_lanzo",
                        cartera=profile.wallet_id,
                        chain=chain_key,
                        reason=repr(error),
                    )
                    return ChainHoldings(
                        chain=chain_key, error=f"el motor de cartera falló: {error}"
                    )

        leidas = await asyncio.gather(*(una(c) for c in objetivo))
        foto = WalletSnapshot(profile=profile, chains=tuple(leidas), read_at=reloj.now())
        _log.info(
            "wallet.leida",
            cartera=profile.wallet_id,
            redes=len(foto.chains),
            ilegibles=len(foto.failed_chains),
            parciales=len(foto.partial_chains),
        )
        return foto

    def _engine(self) -> WalletEngine:
        engine = self.registry.active_or_none(EngineKind.WALLET)
        if engine is None:
            raise NoActiveEngineError(
                "no hay ningún motor de cartera activo, así que no se pueden leer "
                "saldos. Activa uno en la configuración de motores."
            )
        return engine  # type: ignore[return-value]

    def _chains_for(
        self,
        engine: WalletEngine,
        profile: WalletProfile,
        chains: tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        """Las redes a intentar: las que el motor sabe leer y la cartera cubre.

        El orden es el del registro de redes, que es estable, y no el de un
        `set`: la foto tiene que salir en el mismo orden en cada lectura para que
        la interfaz no reordene las filas cada vez que se refresca.
        """
        declaradas = engine.manifest.wallet_chains
        if not declaradas:
            raise EngineConfigError(
                f"el motor «{engine.manifest.engine_id}» está activo como cartera "
                f"pero no declara ninguna red en `wallet_chains`"
            )
        pedidas = set(chains) if chains is not None else None
        return tuple(
            key
            for key in profile.chains_of()
            if key in declaradas and (pedidas is None or key in pedidas)
        )


@dataclass(frozen=True, slots=True)
class ValueWallet:
    """Pone precio a una foto de la cartera, sin volver a leer los saldos.

    Recibe la foto en vez de la cartera porque valorar y leer son preguntas
    distintas y cuestan cosas distintas: si esto releyera, la interfaz que pinta
    saldos y luego precios haría **el doble** de peticiones a los nodos, y en
    Solana —un solo nodo público, racionado por ventana— eso es justo lo que no
    se puede permitir.

    ### El presupuesto, y por qué el orden en que se gasta no es indiferente

    Cada valoración es una cotización, y una cartera puede traer miles de mints.
    Se valoran a lo sumo `budget` posiciones por red, pero **cuáles** se eligen
    no es arbitrario: primero la moneda nativa —que siempre vale algo y es lo que
    el usuario mira primero—, después los tokens que el catálogo conoce por su
    dirección, que son los que alguien midió como reales, y sólo entonces el
    resto en el orden en que se leyó. Gastar el presupuesto en tres mil mints de
    spam antes de llegar al USDC sería absurdo.

    Lo que queda fuera **no se calla**: se apunta en `missing`, la red deja de
    poder afirmar su total y `valued_total` sigue dando la cifra que sí se
    sostiene. Las dos cosas juntas, nunca una sola.
    """

    valuation: ReferenceValuation
    concurrency: int = DEFAULT_VALUATION_CONCURRENCY
    budget: int = DEFAULT_VALUATION_BUDGET

    async def __call__(self, snapshot: WalletSnapshot) -> WalletSnapshot:
        """La misma foto con `value_in_reference` relleno donde se pudo medir.

        Un token que no se puede valorar se queda con `None`, que **no** es cero:
        un token sin cotización no vale nada medible, y colapsarlo con cero lo
        haría desaparecer del total sin dejar rastro. Que el total de la red pase
        a ser `None` es la consecuencia correcta y está dicha en el modelo.
        """
        cadenas = await asyncio.gather(*(self._una(c) for c in snapshot.chains))
        return WalletSnapshot(
            profile=snapshot.profile, chains=tuple(cadenas), read_at=snapshot.read_at
        )

    async def _una(self, cadena: ChainHoldings) -> ChainHoldings:
        """Una red valorada. El orden de los holdings **no cambia**.

        Importa que no cambie: `ChainHoldings.native` documenta que la moneda
        nativa es la primera de la lista, y la interfaz pinta las filas en ese
        orden. Reagrupar —vacíos primero, valorados después, que es lo que hacía
        antes— dejaba el nativo en medio y obligaba a reordenar en cada refresco.
        """
        if cadena.failed or not cadena.non_empty:
            return cadena

        referencia = quote_token(cadena.chain)
        if referencia is None:
            # Una red sin moneda de referencia no se puede valorar, y no se le
            # inventa una. Antes esto se quedaba en un log: los holdings salían
            # sin valor y la red sin total, sin decir por qué. Ahora se dice.
            _log.info("wallet.sin_referencia", chain=cadena.chain)
            return replace(cadena, missing=(*cadena.missing, SIN_REFERENCIA))

        a_valorar, fuera = self._reparto(cadena)
        semaforo = asyncio.Semaphore(max(1, self.concurrency))

        async def una(holding: TokenHolding) -> TokenHolding:
            async with semaforo:
                return await self._uno(holding, referencia)

        valorados = await asyncio.gather(*(una(h) for h in a_valorar))
        # Se recompone por posición en vez de concatenar bloques: así el holding
        # de la fila i sigue siendo el de la fila i, se haya valorado o no.
        por_token = {id(h): v for h, v in zip(a_valorar, valorados, strict=True)}
        holdings = tuple(por_token.get(id(h), h) for h in cadena.holdings)

        faltantes = cadena.missing
        if fuera:
            faltantes = (
                *faltantes,
                f"sin valorar {len(fuera)} de {len(cadena.non_empty)} posiciones "
                f"(se valoran hasta {max(0, self.budget)} por red)",
            )
        return replace(cadena, holdings=holdings, missing=faltantes)

    def _reparto(
        self, cadena: ChainHoldings
    ) -> tuple[tuple[TokenHolding, ...], tuple[TokenHolding, ...]]:
        """`(lo que se valora, lo que no)`, ya ordenado por prioridad.

        El orden es estable —la clave lleva un índice de desempate— para que dos
        lecturas de la misma cartera valoren exactamente lo mismo: si el corte del
        presupuesto cayera en un sitio distinto cada vez, el total cambiaría entre
        refrescos sin que se hubiera movido un solo saldo.
        """
        presupuesto = max(0, self.budget)

        def prioridad(indice_holding: int) -> tuple[int, int]:
            holding = cadena.holdings[indice_holding]
            if holding.token.address is None:
                return (0, indice_holding)
            conocido = token_by_address(holding.token.address, holding.token.chain)
            return (1 if conocido is not None else 2, indice_holding)

        indices = sorted(
            (i for i, h in enumerate(cadena.holdings) if not h.is_empty), key=prioridad
        )
        elegidos = indices[:presupuesto]
        return (
            tuple(cadena.holdings[i] for i in elegidos),
            tuple(cadena.holdings[i] for i in indices[presupuesto:]),
        )

    async def _uno(self, holding: TokenHolding, referencia: Token) -> TokenHolding:
        """El holding con su valor, o el mismo holding si no se pudo valorar.

        Cualquier fallo de un motor al cotizar se traduce a «sin valor»: la
        cartera no puede quedarse sin pintar porque un motor de precios tenga un
        mal día, y un valor ausente ya está previsto en el modelo.
        """
        try:
            valor = await self.valuation(holding.token, holding.amount, referencia)
        except Exception as error:
            _log.warning(
                "wallet.valoracion_fallida",
                token=holding.token.symbol,
                chain=holding.token.chain,
                reason=str(error),
            )
            return holding
        if valor is None:
            return holding
        return TokenHolding(
            token=holding.token, amount=holding.amount, value_in_reference=valor
        )
