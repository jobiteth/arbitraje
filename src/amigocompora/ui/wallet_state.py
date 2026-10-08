"""Los saldos de la cartera, leídos una sola vez y compartidos por las vistas.

### Por qué existe

Antes había **dos** lecturas de saldo que no se conocían entre sí:

- la tarjeta de conversión, que leía la red que se estaba entregando, y
- la pestaña de cartera, que releía todas las redes con su propio contador de
  lectura para descartar lo que llegaba tarde.

Mientras las dos vivieron en pestañas distintas, la duplicidad no se notaba. En
cuanto la cartera pasa a estar **al lado** del swap —las dos mirando la misma red,
la misma dirección y el mismo instante— deja de ser un detalle: elegir un token
dispararía dos peticiones al mismo nodo por el mismo dato. En Solana eso no es una
molestia, es un nodo público racionado por ventana.

### Lo que NO hace

No decide qué es un saldo ni cuándo un total se puede afirmar: eso vive en
`domain/wallet.py` y se respeta tal cual. Esto es caché de presentación y por eso
está en `ui/`: no hay ninguna regla de negocio aquí que merezca viajar a `app`.

### La caché se tira entera cuando cambia el dueño

Un saldo de otra cartera es la peor clase de dato viejo: **el mismo número,
atribuido a quien no es**. No es un desliz de pintado — el usuario decide si tiene
dinero mirando eso. Por eso la clave lleva la dirección, y cuando la que firma
cambia, lo anterior deja de ser de quien dice ser y se tira.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

import structlog

from amigocompora.domain.models import Token
from amigocompora.domain.wallet import (
    ChainHoldings,
    TokenHolding,
    WalletKind,
    WalletProfile,
    WalletSnapshot,
)
from amigocompora.ui.widgets import spawn

_log = structlog.get_logger(__name__)

#: Observador de «esta red acaba de llegar». Se le pasa la red para que cada
#: vista repinte lo suyo sin tener que adivinar qué cambió.
BalancesListener = Callable[[str], None]


@runtime_checkable
class WalletReader(Protocol):
    """Lo que hace falta del contenedor: leer una cartera acotada a unas redes.

    Se declara aquí, en la capa que lo consume, en vez de recibir el `Container`
    entero: así esto no depende de la raíz de composición —con sus veinte
    campos— y una prueba puede pasar un doble de tres líneas. `ReadWallet` lo
    satisface sin cambiar nada, porque su `__call__` ya acepta `chains=` y no
    lanza cuando una red falla: devuelve el `ChainHoldings` con su motivo.
    """

    async def __call__(
        self,
        profile: WalletProfile,
        *,
        chains: tuple[str, ...] | None = None,
    ) -> WalletSnapshot: ...


class WalletBalances:
    """Lo que se tiene, por red, leído a lo sumo una vez por `(dueño, red)`.

    La unidad de caché y de relectura es **la red**, no la cartera entera. Leer
    una red devuelve todos sus saldos de golpe, así que moverse entre tokens de la
    misma red no cuesta una petición más; y la cartera entera son nueve lecturas
    que sólo se piden cuando alguien pulsa «Actualizar», no cada vez que se elige
    un token.
    """

    def __init__(self, read_wallet: WalletReader) -> None:
        self._read_wallet = read_wallet
        #: Lecturas llegadas, por red. Sólo valen para `self._owner`.
        self._holdings: dict[str, ChainHoldings] = {}
        #: Redes con una lectura **en vuelo**.
        #:
        #: La caché de arriba sólo evita la segunda lectura *después* de que la
        #: primera llegue, y el hueco entre las dos es justo donde se repite:
        #: elegir la red repinta las dos patas, y cada repintado pide el saldo de
        #: la pata que se entrega en ese instante. Con la caché todavía vacía, las
        #: tres peticiones salen a la red y la misma lectura se hace tres veces.
        self._reading: set[str] = set()
        #: A quién pertenecen los saldos de la caché. Cadena vacía = nadie.
        self._owner: str = ""
        self._listeners: list[BalancesListener] = []

    # ------------------------------------------------------------------ #
    # Observación
    # ------------------------------------------------------------------ #
    def subscribe(self, listener: BalancesListener) -> Callable[[], None]:
        """Registra un observador y devuelve la función para darse de baja.

        Existe porque hay **dos** vistas pintando el mismo dato y ninguna tiene
        por qué conocer a la otra: la tarjeta de conversión enseña el saldo de la
        pata que se entrega y el panel de cartera enseña la lista entera. Sin
        esto, la lectura que hizo una no repintaría la otra, y un saldo recién
        leído seguiría diciendo «Leyendo…» hasta el siguiente clic.
        """
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    # ------------------------------------------------------------------ #
    # Consulta
    # ------------------------------------------------------------------ #
    @property
    def owner(self) -> str:
        """La dirección a la que pertenecen los saldos de la caché."""
        return self._owner

    def holdings(self, chain_key: str) -> ChainHoldings | None:
        """Lo que ya se leyó de esa red, o `None`. No lee y no lanza.

        Es lo que usan las vistas mientras pintan: quien quiera provocar la
        lectura llama a `ensure`.
        """
        return self._holdings.get(chain_key)

    def reading(self, chain_key: str) -> bool:
        """Si hay una lectura de esa red en camino."""
        return chain_key in self._reading

    def find(self, chain_key: str, token: Token) -> TokenHolding | None:
        """El `TokenHolding` de ese token en esa red, si se leyó.

        Se busca por `is_same_asset` y no por símbolo: en Polygon el USDC nativo y
        el puenteado publican el mismo `symbol()`, y devolver el saldo del que no
        es sería el error que este método existe para no cometer.
        """
        leido = self._holdings.get(chain_key)
        if leido is None or leido.failed:
            return None
        return next((h for h in leido.holdings if h.token.is_same_asset(token)), None)

    def publish(self, chain_key: str, holdings: ChainHoldings, *, owner: str) -> None:
        """Anota una lectura que hizo **otra** vista, sin volver a preguntar al nodo.

        Existe porque la lista de la cartera lee la cartera entera —las nueve
        redes— para poder sumar un patrimonio, y esa lectura ya trae dentro, red
        por red, exactamente el mismo `ChainHoldings` que la tarjeta de swap pide
        una a una. Sin esto, arrancar la aplicación leería cada red dos veces: una
        para el total y otra para el saldo de la pata. En Solana eso no es una
        molestia, es agotar la cuota de un nodo público.

        La lectura **no se publica si es de otro dueño**: quien mira otra cartera
        no puede dejar sus saldos en la caché de la que firma, o la siguiente
        pantalla enseñaría el dinero de una bajo la dirección de la otra.
        """
        if owner != self._owner or not owner:
            return
        if self._holdings.get(chain_key) == holdings:
            return
        self._holdings[chain_key] = holdings
        self._notify(chain_key)

    # ------------------------------------------------------------------ #
    # Lectura
    # ------------------------------------------------------------------ #
    def ensure(self, chain_key: str, *, owner: str) -> None:
        """Lee esa red si hace falta. No espera: la vista se repinta al llegar.

        El dueño se comprueba **antes** que la caché, y en ese orden: si la
        dirección que firma cambió —se guardó otra credencial, se borró—, los
        saldos guardados dejan de ser de quien dice la pantalla y se tiran antes
        de decidir nada.
        """
        if not self._forget_other_owner(owner):
            return
        if not owner or chain_key in self._holdings or chain_key in self._reading:
            return
        self._reading.add(chain_key)
        spawn(self._read(chain_key, owner))

    def refresh(self, chain_key: str, *, owner: str) -> None:
        """Relee esa red aunque ya esté en la caché. Para el botón de actualizar."""
        if not self._forget_other_owner(owner):
            return
        if not owner or chain_key in self._reading:
            return
        self._holdings.pop(chain_key, None)
        self._reading.add(chain_key)
        spawn(self._read(chain_key, owner))

    def refresh_all(self, chain_keys: tuple[str, ...], *, owner: str) -> None:
        """Relee varias redes. «La cartera entera» es esto con nueve claves.

        Se refresca red a red y no de golpe a propósito: el caso de uso ya lee en
        paralelo con su propio semáforo, y pedir nueve redes de una vez aquí
        escondería la primera que llegue en lugar de enseñarla.
        """
        for chain_key in chain_keys:
            self.refresh(chain_key, owner=owner)

    def _forget_other_owner(self, owner: str) -> bool:
        """Tira la caché si el dueño cambió. Devuelve si hay dueño con el que leer."""
        if owner != self._owner:
            if self._holdings:
                _log.debug("balances.dueno_cambiado", antes=self._owner, ahora=owner)
            self._holdings.clear()
            self._owner = owner
        return bool(owner)

    async def _read(self, chain_key: str, owner: str) -> None:
        """Lee una red y la publica. Suelta la marca de «en vuelo», pase lo que pase.

        La marca se suelta en un `finally` y no al final del camino bueno: si se
        quedara puesta al fallar, un corte de red convertiría ese saldo en un dato
        que ya no se puede volver a pedir hasta reiniciar la aplicación — que es
        exactamente lo contrario de lo que hace falta justo después de un fallo.
        """
        try:
            perfil = WalletProfile(
                wallet_id="principal",
                label="Mi cartera",
                kind=WalletKind.of_chain(chain_key),
                address=owner,
            )
            snapshot = await self._read_wallet(perfil, chains=(chain_key,))
        except Exception as error:
            # Se contiene y se sigue: un saldo que no llega no puede tumbar la
            # pantalla que lo estaba pidiendo. El motivo acaba dentro del propio
            # `ChainHoldings` cuando el motor lo devuelve, y en el registro aquí.
            _log.warning("balances.lectura_fallida", chain=chain_key, reason=str(error))
            return
        finally:
            self._reading.discard(chain_key)

        # Se vuelve a preguntar por el dueño: la lectura tardó, y en ese rato la
        # credencial pudo cambiar. Publicar esto entonces sería pintar los saldos
        # de una cartera bajo la dirección de otra.
        if owner != self._owner:
            _log.debug("balances.descartado_por_dueno", chain=chain_key)
            return
        leido = next((c for c in snapshot.chains if c.chain == chain_key), None)
        if leido is None:
            return
        self._holdings[chain_key] = leido
        self._notify(chain_key)

    def _notify(self, chain_key: str) -> None:
        """Avisa a los observadores sin dejar que el fallo de uno rompa el resto.

        Un observador que lanza —una vista que se está cerrando, un widget ya
        destruido— no puede llevarse por delante el dato que acaba de llegar ni el
        aviso a los demás.
        """
        for listener in tuple(self._listeners):
            try:
                listener(chain_key)
            except Exception:  # un observador no decide nada
                _log.warning("balances.observador_fallido", chain=chain_key, exc_info=True)
