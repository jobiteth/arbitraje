"""La cartera: qué se tiene, en qué red y cuánto vale.

### Por qué esto es dominio y no motor

Un motor puede venir de un paquete de terceros y declara sus propios hosts; lo
que **no** puede es decidir qué es una cartera. Estos modelos son el contrato:
el motor los rellena, la interfaz los pinta y los casos de uso deciden. Si
mañana entra un motor de cartera por WalletConnect o por un Ledger, tiene que
producir exactamente esto.

### La decisión que gobierna el resto: un total parcial no es un total

Leer ocho redes es leer ocho nodos, y uno de ellos va a fallar de vez en cuando.
La tentación es saltarse el que falló y devolver la suma de los otros siete: es
una cifra con el mismo aspecto que la buena y que **miente sobre el patrimonio**
—dice que tienes menos de lo que tienes, sin avisar—. Aquí cada red lleva su
propio fallo pegado (`ChainHoldings.error`) y el conjunto dice si está completo
(`WalletSnapshot.is_complete`). La suma se devuelve igual, porque es útil, pero
nunca sola: quien la enseñe tiene delante el dato de que falta algo.

### Lo que NO se guarda aquí

Ni claves, ni frases, ni material de firma. `WalletProfile` lleva la **dirección**,
que es pública, y el nombre con el que se busca la clave en el llavero. La clave
no entra en este módulo ni en ningún objeto que lo use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from amigocompora.domain.chains import CHAINS, AddressFormat
from amigocompora.domain.errors import InvalidAmountError
from amigocompora.domain.models import Token
from amigocompora.domain.money import TokenAmount

#: Longitud máxima de una etiqueta de cartera. Es un nombre que acaba en la
#: interfaz y en el registro de ejecuciones; acotarlo evita que una etiqueta de
#: mil caracteres desplace media pantalla o falsee una línea del libro.
MAX_LABEL_LENGTH: Final = 48


class WalletKind(StrEnum):
    """La familia de una cartera, que es lo que decide en qué redes sirve.

    No se llama «red» a propósito: una cartera EVM vale en Base, en Polygon y en
    cualquier otra red EVM —es la misma clave y la misma dirección—, así que
    «una cartera, una red» sería una mentira que obligaría a duplicar la misma
    cartera ocho veces.
    """

    EVM = "evm"
    SOLANA = "solana"

    @property
    def label(self) -> str:
        return "EVM" if self is WalletKind.EVM else "Solana"

    @classmethod
    def of_chain(cls, chain_key: str) -> WalletKind:
        """La familia a la que pertenece una red del catálogo."""
        spec = CHAINS.get(chain_key)
        if spec is None:
            raise InvalidAmountError(f"«{chain_key}» no es una red conocida")
        if spec.address_format is AddressFormat.SOLANA_BASE58:
            return cls.SOLANA
        return cls.EVM


@dataclass(frozen=True, slots=True)
class WalletProfile:
    """Una cartera con nombre: a quién pertenece una dirección y de qué familia es.

    **No lleva la clave.** Lleva `wallet_id`, que es con lo que se busca en el
    llavero cuando hay que firmar, y la dirección, que es lo que se enseña y lo
    que se usa para leer saldos. Es la misma separación que hace
    `SpendingKeyProvider`: la dirección se puede pedir siempre, la clave sólo en
    el momento de firmar.
    """

    wallet_id: str
    label: str
    kind: WalletKind
    address: str

    def __post_init__(self) -> None:
        if not self.wallet_id.strip():
            raise InvalidAmountError("una cartera necesita un identificador")
        etiqueta = self.label.strip()
        if not etiqueta:
            raise InvalidAmountError("una cartera necesita una etiqueta")
        if len(etiqueta) > MAX_LABEL_LENGTH:
            raise InvalidAmountError(
                f"la etiqueta «{etiqueta[:20]}…» pasa de {MAX_LABEL_LENGTH} caracteres"
            )
        # La dirección se valida contra el formato de **su familia**, no contra
        # el de una red concreta: en EVM el mismo texto vale en las ocho redes,
        # así que atarlo a una obligaría a elegir una arbitraria.
        if self.kind is WalletKind.SOLANA:
            from amigocompora.domain.addresses import require_solana_address

            valida = require_solana_address(self.address, "cartera")
        else:
            from amigocompora.domain.addresses import require_evm_address

            valida = require_evm_address(self.address, "cartera")
        object.__setattr__(self, "address", valida)

    def covers(self, chain_key: str) -> bool:
        """Si esta cartera sirve para leer esa red.

        EVM cubre toda red EVM; Solana, sólo Solana. Es lo que permite tener una
        cartera por familia en vez de una por red.
        """
        try:
            return WalletKind.of_chain(chain_key) is self.kind
        except InvalidAmountError:
            return False

    def chains_of(self) -> tuple[str, ...]:
        """Las redes del catálogo donde esta cartera se puede leer."""
        return tuple(key for key in CHAINS if self.covers(key))


@dataclass(frozen=True, slots=True)
class TokenHolding:
    """Un token y cuánto se tiene, con su valor si se pudo medir.

    `value_in_reference` es el valor en la stablecoin de esa red, y es `None`
    cuando no se pudo valorar. `None` **no** es cero: un token sin cotización
    disponible no vale nada medible, que es distinto de valer cero, y colapsarlos
    haría que un token ilíquido desapareciera del total sin dejar rastro.
    """

    token: Token
    amount: TokenAmount
    value_in_reference: TokenAmount | None = None

    @property
    def is_empty(self) -> bool:
        return self.amount.raw == 0

    @property
    def is_valued(self) -> bool:
        return self.value_in_reference is not None

    @property
    def reference(self) -> TokenAmount | None:
        return self.value_in_reference

    def as_decimal(self) -> Decimal:
        return self.amount.as_decimal()


@dataclass(frozen=True, slots=True)
class ChainHoldings:
    """Lo que hay en una red, o el motivo por el que no se pudo leer.

    Los dos casos van en el mismo objeto a propósito. Si una red que falla
    desapareciera de la lista, quien suma no sabría que falta; teniéndola aquí
    con su `error`, la lista siempre tiene tantas entradas como redes se
    intentaron y el hueco se ve.

    ### `error` y `missing` no son lo mismo, y por eso son dos campos

    `error` es «de esta red no se pudo leer **nada**». `missing` es «se leyó, pero
    no está todo, y esto es lo que falta»: en Solana son dos programas de token y
    uno puede devolver 403 mientras el otro contesta; y en cualquier red puede
    haber posiciones sin valorar porque valorarlas cuesta una cotización por
    token y hay un presupuesto.

    La distinción no es cosmética. Si las dos cosas se colapsaran en `error`, una
    red leída a medias se marcaría como no leída y se **tirarían** los saldos que
    sí llegaron; y si se colapsaran en silencio, quedaría una cartera con el SOL y
    sin los USDC, con el mismo aspecto que una bien leída. Con `missing` aparte, la
    red conserva lo que se leyó y dice qué le falta, y el total deja de ser
    defendible — que es la verdad.
    """

    chain: str
    holdings: tuple[TokenHolding, ...] = ()
    #: Motivo por el que no se pudo leer **nada** de esta red.
    error: str | None = None
    #: Lo que falta en esta red y por qué: una fuente que no contestó —en Solana,
    #: el programa de tokens que devolvió 403— o posiciones que quedaron sin
    #: valorar porque la valoración tiene presupuesto. Se nombran para que la
    #: interfaz pueda decir qué falta en vez de dar a entender que no hay más.
    missing: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def is_complete(self) -> bool:
        """Si lo que hay es **todo** lo que hay en esa red."""
        return self.error is None and not self.missing

    @property
    def native(self) -> TokenHolding | None:
        """La moneda nativa de la red, que es siempre la primera de la lista."""
        for holding in self.holdings:
            if holding.token.address is None:
                return holding
        return None

    @property
    def tokens(self) -> tuple[TokenHolding, ...]:
        """Todo menos la moneda nativa."""
        return tuple(h for h in self.holdings if h.token.address is not None)

    @property
    def non_empty(self) -> tuple[TokenHolding, ...]:
        return tuple(h for h in self.holdings if not h.is_empty)

    @property
    def unvalued(self) -> tuple[TokenHolding, ...]:
        """Posiciones con fondos a las que no se les pudo poner precio.

        Se separa de `total_in_reference` porque son dos preguntas distintas:
        «¿cuál es el patrimonio?» —que esta red no puede contestar— y «¿qué se
        quedó fuera de la suma?», que es lo que la interfaz necesita para poder
        enseñar un importe **etiquetado** en vez de ninguno.
        """
        return tuple(h for h in self.holdings if not h.is_empty and not h.is_valued)

    @property
    def valued_total(self) -> Decimal:
        """Lo que suman las posiciones **valoradas**. NO es el patrimonio.

        Existe porque el caso real lo pide y negarlo no lo arregla: una cartera de
        Solana con tres mil cuentas de spam no se puede valorar entera —cada
        posición es una cotización— y devolver `None` deja al usuario sin ninguna
        cifra. Esto devuelve la que sí se puede sostener, siempre que quien la
        enseñe diga dos cosas con ella: que es lo **valorado** y cuántas
        posiciones quedaron fuera (`unvalued`). El nombre lo dice; el uso tiene
        que decirlo también.

        A diferencia de `total_in_reference`, no falla cuando falta una fuente:
        suma lo que hay. Por eso no sustituye al otro ni al revés.
        """
        total = Decimal(0)
        for holding in self.holdings:
            referencia = holding.value_in_reference
            if referencia is not None:
                total += referencia.as_decimal()
        return total

    @property
    def total_in_reference(self) -> Decimal | None:
        """La suma valorada, o `None` si no se puede afirmar.

        Es deliberadamente estricta, y falla por dos motivos: que algún saldo con
        fondos no se haya podido valorar —sumar lo que se pudo y callar el resto
        daría un total más bajo que el real con toda la apariencia de ser el
        bueno— o que falte una fuente por contestar, porque entonces puede haber
        saldos que no están en la lista y ninguna suma sobre ella es el patrimonio.
        """
        if not self.is_complete:
            return None
        total = Decimal(0)
        for holding in self.holdings:
            if holding.is_empty:
                continue
            referencia = holding.value_in_reference
            if referencia is None:
                return None
            total += referencia.as_decimal()
        return total


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    """La foto de una cartera en un instante, red por red.

    `read_at` no es decorativo: un saldo sin hora es un saldo que no se puede
    juzgar. Y `is_complete` es lo que separa «tienes esto» de «pude leer siete de
    ocho redes y esto es lo que vi».
    """

    profile: WalletProfile
    chains: tuple[ChainHoldings, ...]
    read_at: datetime

    @property
    def failed_chains(self) -> tuple[str, ...]:
        return tuple(c.chain for c in self.chains if c.failed)

    @property
    def partial_chains(self) -> tuple[str, ...]:
        """Redes que se leyeron a medias: hay datos, y no son todos."""
        return tuple(c.chain for c in self.chains if not c.failed and c.missing)

    @property
    def is_complete(self) -> bool:
        """Si la foto es de la cartera entera y no de la parte que se pudo leer."""
        return all(c.is_complete for c in self.chains)

    @property
    def read_chains(self) -> tuple[ChainHoldings, ...]:
        return tuple(c for c in self.chains if not c.failed)

    @property
    def unvalued_positions(self) -> tuple[TokenHolding, ...]:
        """Todas las posiciones con fondos que se quedaron sin precio, en toda la cartera."""
        return tuple(h for c in self.chains for h in c.unvalued)

    @property
    def valued_total(self) -> Decimal:
        """La suma de lo valorado en todas las redes. NO es el patrimonio.

        Misma advertencia que en `ChainHoldings.valued_total`: quien la enseñe
        tiene que enseñar con ella `unvalued_positions`, o estará dando un número
        más bajo que el real con el aspecto de ser el bueno.
        """
        return sum((c.valued_total for c in self.chains), Decimal(0))

    @property
    def total_in_reference(self) -> Decimal | None:
        """El total de todas las redes, o `None` si no se puede afirmar.

        Falla por dos motivos y los dos tienen que darse: que alguna red no se
        leyera —ahí el total es incompleto por definición— o que alguna red leída
        tenga una posición sin valorar. Cualquiera de los dos convierte la suma en
        una cifra que no se puede defender.
        """
        if not self.is_complete:
            return None
        total = Decimal(0)
        for cadena in self.chains:
            parcial = cadena.total_in_reference
            if parcial is None:
                return None
            total += parcial
        return total

    def by_chain(self) -> tuple[tuple[str, ChainHoldings], ...]:
        """Las redes en el orden en que se leyeron, para pintarlas."""
        return tuple((c.chain, c) for c in self.chains)
