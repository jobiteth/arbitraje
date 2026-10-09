"""La cartera: el contrato, el motor y los dos casos de uso.

Aquí no se prueba que «devuelva datos». Se prueban las decisiones que hacen que
una cifra de patrimonio **se pueda enseñar**, que son las que se rompen callando:

1. **Que un total parcial no salga nunca con la cara de un total.** Cuando una
   red no se leyó, cuando un token no se pudo valorar o cuando la valoración se
   quedó sin presupuesto, `total_in_reference` es `None`. Y a la vez existe
   `valued_total`, que sí da una cifra —etiquetada— para que el caso real
   —tres mil mints de spam en Solana— no deje al usuario sin ningún número.
2. **Que sólo se intenten las redes que el motor declara saber leer.** Medido:
   `CHAINS` tiene once redes, una cartera EVM «cubre» diez por familia y el motor
   declara nueve. Leyendo las once, `arc` y `robinhood` fallarían **siempre** y
   el patrimonio no se podría afirmar jamás.
3. **Que el motor no pueda tumbar la lectura entera.** Una red que falla viaja
   dentro de su resultado; la foto sale con las demás.
4. **Que el presupuesto de valoración se gaste en lo que importa** —el nativo,
   luego lo que el catálogo conoce— y que lo que quede fuera se diga.
5. **Que el orden de los holdings no cambie al valorarlos.** La interfaz pinta en
   ese orden y el nativo tiene que seguir siendo el primero, como documenta el
   modelo.

Todo con dobles: ni una petición de red, ni una firma, ni una escritura.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import pytest

from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.usecases.wallet import (
    SIN_REFERENCIA,
    ReadWallet,
    ValueWallet,
)
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    InvalidAmountError,
    NoActiveEngineError,
)
from amigocompora.domain.models import Token
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import EngineKind
from amigocompora.domain.wallet import (
    MAX_LABEL_LENGTH,
    ChainHoldings,
    TokenHolding,
    WalletKind,
    WalletProfile,
    WalletSnapshot,
)
from amigocompora.engines.catalog import quote_token, tokens_for
from amigocompora.engines.wallet.engine import MANIFEST, WALLET_CHAINS, tokens_to_read

AHORA = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
EVM = "0x2c887da24C6E6c939B0Fe3adaE50814b938B44f9"
SOL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"

#: Las redes declaradas por el motor que le corresponden a una cartera EVM. Es el
#: número que el fallo «10 de 9» tenía mal: la familia cubre diez, el motor sabe
#: leer ocho de ellas, y sólo esas ocho se intentan.
REDES_EVM: Final = tuple(k for k in WALLET_CHAINS if WalletKind.of_chain(k) is WalletKind.EVM)


def _perfil(chain_kind: WalletKind = WalletKind.EVM, address: str = EVM) -> WalletProfile:
    return WalletProfile(wallet_id="prueba", label="Prueba", kind=chain_kind, address=address)


def _holding(
    simbolo: str, crudo: int, chain: str, valor: str | None, decimales: int = 6
) -> TokenHolding:
    return TokenHolding(
        token=Token(symbol=simbolo, decimals=decimales, chain=chain, address="0x" + simbolo * 4),
        amount=TokenAmount(raw=crudo, decimals=decimales, symbol=simbolo),
        value_in_reference=(
            None
            if valor is None
            else TokenAmount.from_decimal(valor, 6, "USDC", rounding="ROUND_DOWN")
        ),
    )


def _nativo_holding(
    crudo: int, chain: str, valor: str | None, simbolo: str = "POL"
) -> TokenHolding:
    return TokenHolding(
        token=Token(symbol=simbolo, decimals=18, chain=chain, address=None),
        amount=TokenAmount(raw=crudo, decimals=18, symbol=simbolo),
        value_in_reference=(
            None
            if valor is None
            else TokenAmount.from_decimal(valor, 6, "USDC", rounding="ROUND_DOWN")
        ),
    )


# --------------------------------------------------------------------------- #
# Dominio: la cartera y sus dos familias
# --------------------------------------------------------------------------- #
def test_una_cartera_evm_no_admite_una_direccion_de_solana() -> None:
    """La dirección se valida contra su **familia**, no contra una red concreta."""
    with pytest.raises(InvalidAmountError):
        WalletProfile(wallet_id="x", label="x", kind=WalletKind.EVM, address=SOL)


def test_una_cartera_solana_no_admite_una_direccion_evm() -> None:
    with pytest.raises(InvalidAmountError):
        WalletProfile(wallet_id="x", label="x", kind=WalletKind.SOLANA, address=EVM)


def test_una_etiqueta_kilometrica_se_rechaza() -> None:
    """La etiqueta acaba en pantalla y en el libro de ejecuciones."""
    with pytest.raises(InvalidAmountError):
        WalletProfile(
            wallet_id="x", label="a" * (MAX_LABEL_LENGTH + 1), kind=WalletKind.EVM, address=EVM
        )


def test_una_cartera_sin_identificador_se_rechaza() -> None:
    with pytest.raises(InvalidAmountError):
        WalletProfile(wallet_id="   ", label="x", kind=WalletKind.EVM, address=EVM)


def test_una_cartera_evm_cubre_todas_las_redes_evm_y_ninguna_de_solana() -> None:
    """Es lo que permite una cartera por familia en vez de una por red."""
    perfil = _perfil()
    assert perfil.covers("base")
    assert perfil.covers("polygon")
    assert perfil.covers("ethereum")
    assert not perfil.covers("solana")
    assert "solana" not in perfil.chains_of()
    assert len(perfil.chains_of()) > 8


def test_una_cartera_solana_cubre_solo_solana() -> None:
    perfil = _perfil(WalletKind.SOLANA, SOL)
    assert perfil.chains_of() == ("solana",)
    assert not perfil.covers("base")


def test_una_red_que_no_existe_no_la_cubre_ninguna_cartera() -> None:
    assert not _perfil().covers("no-existe")
    with pytest.raises(InvalidAmountError):
        WalletKind.of_chain("no-existe")


def test_la_familia_de_una_red_sale_del_registro_y_no_de_una_lista() -> None:
    assert WalletKind.of_chain("solana") is WalletKind.SOLANA
    assert WalletKind.of_chain("base") is WalletKind.EVM


# --------------------------------------------------------------------------- #
# Dominio: un total parcial no es un total
# --------------------------------------------------------------------------- #
def test_una_red_completa_y_valorada_si_afirma_su_total() -> None:
    cadena = ChainHoldings(
        chain="polygon",
        holdings=(
            _nativo_holding(19_491_666_140_000_000_000, "polygon", "1.99"),
            _holding("USDC", 1_000_000, "polygon", "1.00"),
        ),
    )
    assert cadena.is_complete
    assert cadena.total_in_reference == Decimal("2.99")


def test_una_red_a_la_que_le_falta_una_fuente_no_afirma_su_total() -> None:
    """El caso medido: 403 en Token-2022 mientras el clásico contesta."""
    cadena = ChainHoldings(
        chain="solana",
        holdings=(_nativo_holding(1_000_000_000, "solana", "150.00", "SOL"),),
        missing=("token Token-2022: HTTP 403",),
    )
    assert not cadena.is_complete
    assert cadena.total_in_reference is None
    # Pero la cifra que sí se sostiene sigue estando, para poder etiquetarla.
    assert cadena.valued_total == Decimal("150.00")


def test_una_posicion_sin_valorar_impide_afirmar_el_total_pero_no_borra_lo_demas() -> None:
    """`None` no es cero: un token ilíquido no puede desaparecer del patrimonio."""
    cadena = ChainHoldings(
        chain="polygon",
        holdings=(
            _nativo_holding(1_000_000_000_000_000_000, "polygon", "1.00"),
            _holding("RARO", 5, "polygon", None),
        ),
    )
    assert cadena.is_complete
    assert cadena.total_in_reference is None
    assert cadena.valued_total == Decimal("1.00")
    assert [h.token.symbol for h in cadena.unvalued] == ["RARO"]


def test_un_saldo_a_cero_no_impide_el_total_aunque_no_tenga_precio() -> None:
    """Preguntar por veinte tokens vacíos no puede dejar la red sin total."""
    cadena = ChainHoldings(
        chain="polygon",
        holdings=(
            _holding("VACIO", 0, "polygon", None),
            _nativo_holding(1_000_000_000_000_000_000, "polygon", "1.00"),
        ),
    )
    assert cadena.total_in_reference == Decimal("1.00")
    assert cadena.unvalued == ()


def test_la_moneda_nativa_se_encuentra_esté_donde_esté() -> None:
    """`native` busca por `address is None`, no por posición."""
    cadena = ChainHoldings(
        chain="polygon",
        holdings=(_holding("USDC", 1, "polygon", "1"), _nativo_holding(2, "polygon", "1")),
    )
    nativo = cadena.native
    assert nativo is not None
    assert nativo.token.symbol == "POL"
    assert [h.token.symbol for h in cadena.tokens] == ["USDC"]


def test_la_foto_marca_las_redes_parciales_y_las_ilegibles_por_separado() -> None:
    foto = WalletSnapshot(
        profile=_perfil(),
        chains=(
            ChainHoldings(chain="base", holdings=(_nativo_holding(1, "base", "1.00", "ETH"),)),
            ChainHoldings(chain="bsc", error="el nodo no contestó"),
            ChainHoldings(chain="solana", holdings=(), missing=("token SPL: HTTP 403",)),
        ),
        read_at=AHORA,
    )
    assert foto.failed_chains == ("bsc",)
    assert foto.partial_chains == ("solana",)
    assert not foto.is_complete
    assert foto.total_in_reference is None
    # Y aun así da la cifra etiquetable de lo que sí se sostiene.
    assert foto.valued_total == Decimal("1.00")


def test_el_patrimonio_de_varias_redes_suma_solo_si_todas_afirman() -> None:
    completa = WalletSnapshot(
        profile=_perfil(),
        chains=(
            ChainHoldings(chain="base", holdings=(_nativo_holding(1, "base", "3.00", "ETH"),)),
            ChainHoldings(chain="polygon", holdings=(_nativo_holding(1, "polygon", "4.00"),)),
        ),
        read_at=AHORA,
    )
    assert completa.total_in_reference == Decimal("7.00")
    assert completa.valued_total == Decimal("7.00")


def test_las_posiciones_sin_valorar_de_toda_la_cartera_se_pueden_listar() -> None:
    foto = WalletSnapshot(
        profile=_perfil(),
        chains=(
            ChainHoldings(chain="base", holdings=(_holding("A", 1, "base", None),)),
            ChainHoldings(chain="polygon", holdings=(_holding("B", 1, "polygon", None),)),
        ),
        read_at=AHORA,
    )
    assert [h.token.symbol for h in foto.unvalued_positions] == ["A", "B"]


# --------------------------------------------------------------------------- #
# El motor: qué declara y qué se niega a hacer
# --------------------------------------------------------------------------- #
def test_el_manifiesto_ocupa_la_ranura_de_cartera_y_solo_lee() -> None:
    assert MANIFEST.kind is EngineKind.WALLET
    assert {c.value for c in MANIFEST.capabilities} == {"read_chain"}
    assert not MANIFEST.needs_secrets, "leer saldos no puede exigir una credencial"


def test_el_motor_declara_las_redes_con_nodo_de_la_familia() -> None:
    """Una red de la familia sólo se declara si tiene un nodo con el que leerla.

    `arc` y `robinhood` tienen respaldo público, así que se leen igual que el
    resto; una red sin ningún nodo quedaría fuera y no fallaría en silencio.
    """
    cubiertas = set(_perfil().chains_of())
    assert {"arc", "robinhood"} <= set(WALLET_CHAINS)
    # Y la familia que no es EVM entra por su propio lado, no por el de esta.
    assert "solana" in WALLET_CHAINS
    assert "solana" not in cubiertas
    assert MANIFEST.wallet_chains == WALLET_CHAINS


def test_el_manifiesto_declara_los_hosts_de_las_redes_que_lee() -> None:
    assert MANIFEST.allowed_hosts, "un motor que lee la cadena tiene que declarar sus hosts"


def test_los_tokens_que_se_leen_son_el_nativo_primero_y_luego_el_catalogo() -> None:
    tokens = tokens_to_read("polygon")
    assert tokens[0].address is None, "el nativo no va primero"
    assert tokens[0].symbol == "POL"
    assert tokens[0].decimals == 18
    assert list(tokens[1:]) == list(tokens_for("polygon"))


def test_un_token_anadido_por_el_usuario_se_lee_una_sola_vez() -> None:
    uni = Token(symbol="UNI", decimals=18, chain="polygon", address="0x" + "b" * 40)
    en_otra_red = Token(symbol="UNI", decimals=18, chain="base", address="0x" + "b" * 40)
    tokens = tokens_to_read("polygon", (uni, en_otra_red, *tokens_for("polygon")))
    assert tokens[-1] == uni
    assert sum(t.is_same_asset(uni) for t in tokens) == 1
    assert all(t.chain == "polygon" for t in tokens)


class _Motor:
    """Doble del motor de cartera: devuelve lo que se le diga, sin red."""

    def __init__(
        self,
        *,
        por_red: dict[str, ChainHoldings] | None = None,
        falla: Exception | None = None,
        retraso: float = 0.0,
        manifest: Any = MANIFEST,
    ) -> None:
        self.manifest = manifest
        self.por_red = por_red or {}
        self.falla = falla
        self.retraso = retraso
        self.pedidas: list[str] = []
        self.en_vuelo = 0
        self.maximo_en_vuelo = 0

    async def holdings(
        self, profile: WalletProfile, chain_key: str, *, tokens: tuple[Token, ...] = ()
    ) -> ChainHoldings:
        del profile
        self.pedidas.append(chain_key)
        self.en_vuelo += 1
        self.maximo_en_vuelo = max(self.maximo_en_vuelo, self.en_vuelo)
        try:
            if self.retraso:
                await asyncio.sleep(self.retraso)
            if self.falla is not None:
                raise self.falla
            return self.por_red.get(chain_key, ChainHoldings(chain=chain_key))
        finally:
            self.en_vuelo -= 1


class _Registro:
    def __init__(self, engine: object | None) -> None:
        self.engine = engine
        self.consultas: list[EngineKind] = []

    def active_or_none(self, kind: EngineKind) -> object | None:
        self.consultas.append(kind)
        return self.engine


def _leer(engine: object | None, guard: ModeGuard | None = None) -> ReadWallet:
    return ReadWallet(
        registry=_Registro(engine),  # type: ignore[arg-type]
        guard=guard or ModeGuard(OperationMode.OBSERVATION),
        clock=FrozenClock(AHORA),
    )


def test_leer_la_cartera_pasa_por_la_barrera_de_modo() -> None:
    """Como todo lo demás: la tabla de política sigue siendo la única verdad.

    Se comprueba que **pregunta**, no que le concedan: que todos los modos
    concedan `READ_CHAIN` es lo que hace que en la práctica no bloquee nada, y
    eso es una consecuencia de la tabla, no una excepción a ella.
    """

    class _Guard:
        def __init__(self) -> None:
            self.pedidas: list[Capability] = []

        def require(self, capability: Capability) -> None:
            self.pedidas.append(capability)

    guard = _Guard()
    motor = _Motor()
    asyncio.run(_leer(motor, guard)(_perfil()))  # type: ignore[arg-type]

    assert guard.pedidas == [Capability.READ_CHAIN]
    assert motor.pedidas, "no se llegó a leer ninguna red"


def test_todos_los_modos_pueden_leer_saldos() -> None:
    """Una cartera que no se puede mirar en `OBSERVACIÓN` no sirve de nada."""
    for mode in OperationMode:
        assert mode.grants(Capability.READ_CHAIN), f"{mode.label} no deja leer saldos"


def test_sin_motor_de_cartera_activo_se_dice_en_vez_de_devolver_una_foto_vacia() -> None:
    """Una foto vacía se leería como «no tienes nada», que es una mentira útil."""
    with pytest.raises(NoActiveEngineError):
        asyncio.run(_leer(None)(_perfil()))


async def test_solo_se_intentan_las_redes_que_el_motor_declara_saber_leer() -> None:
    motor = _Motor()
    foto = await _leer(motor)(_perfil())

    assert set(motor.pedidas) == set(REDES_EVM)
    assert len(motor.pedidas) == len(foto.chains)
    assert {"arc", "robinhood"} <= set(motor.pedidas)
    assert "solana" not in motor.pedidas, "una cartera EVM no puede leer Solana"


async def test_una_cartera_solana_solo_intenta_solana() -> None:
    motor = _Motor()
    await _leer(motor)(_perfil(WalletKind.SOLANA, SOL))
    assert motor.pedidas == ["solana"]


async def test_se_puede_pedir_un_subconjunto_de_redes() -> None:
    motor = _Motor()
    await _leer(motor)(_perfil(), chains=("polygon", "base"))
    assert sorted(motor.pedidas) == ["base", "polygon"]


async def test_pedir_una_red_que_el_motor_no_declara_no_la_activa() -> None:
    """Limitar no puede ensanchar: `chains` filtra, nunca añade."""
    motor = _Motor()
    foto = await _leer(motor)(_perfil(), chains=("solana",))
    assert motor.pedidas == []
    assert foto.chains == ()


async def test_el_orden_de_las_redes_es_estable_entre_lecturas() -> None:
    """Un `set` reordenaría las filas en cada refresco."""
    primera = await _leer(_Motor())(_perfil())
    segunda = await _leer(_Motor())(_perfil())
    assert [c.chain for c in primera.chains] == [c.chain for c in segunda.chains]


async def test_una_red_ilegible_no_se_lleva_por_delante_a_las_demas() -> None:
    """El motor devuelve el fallo dentro del resultado; el caso de uso lo conserva."""
    motor = _Motor(
        por_red={
            "polygon": ChainHoldings(chain="polygon", error="el nodo no contestó"),
            "base": ChainHoldings(
                chain="base", holdings=(_nativo_holding(1, "base", "1.00", "ETH"),)
            ),
        }
    )
    foto = await _leer(motor)(_perfil())

    assert len(foto.chains) == len(REDES_EVM)
    assert foto.failed_chains == ("polygon",)
    assert foto.total_in_reference is None


async def test_un_motor_que_lanza_se_convierte_en_red_ilegible_y_no_tumba_la_foto() -> None:
    """El protocolo dice que no lanza; si un motor de terceros lo hace, se contiene.

    Se envuelve **por red**: lo contrario —dejar que la excepción suba— haría que
    un motor defectuoso dejara al usuario sin ver ninguna de sus otras diez redes.
    """
    motor = _Motor(falla=RuntimeError("motor roto"))
    foto = await _leer(motor)(_perfil())

    assert len(foto.chains) == len(REDES_EVM)
    assert set(foto.failed_chains) == set(REDES_EVM)
    assert foto.total_in_reference is None
    assert all(c.error is not None and "motor roto" in c.error for c in foto.chains)


async def test_la_foto_lleva_la_hora_del_reloj_inyectado() -> None:
    foto = await _leer(_Motor())(_perfil())
    assert foto.read_at == AHORA


async def test_no_se_lanzan_mas_redes_a_la_vez_que_las_permitidas() -> None:
    """Los nodos públicos limitan por IP: lanzar once de golpe es ganarse un 429."""
    motor = _Motor(retraso=0.01)
    await _leer(motor)(_perfil(), concurrency=3)
    assert motor.maximo_en_vuelo <= 3


# --------------------------------------------------------------------------- #
# Valorar: el presupuesto, el orden y lo que se queda fuera
# --------------------------------------------------------------------------- #
class _Valorador:
    """Doble de `ReferenceValuation`: precio fijo por símbolo, y dice qué le pidieron."""

    def __init__(
        self, precios: dict[str, str] | None = None, falla: Sequence[str] = (), retraso: float = 0.0
    ) -> None:
        self.precios = precios or {}
        self.falla = set(falla)
        self.retraso = retraso
        self.pedidos: list[str] = []
        self.en_vuelo = 0
        self.maximo_en_vuelo = 0

    async def __call__(
        self, spent: Token, amount: TokenAmount, reference: Token
    ) -> TokenAmount | None:
        del reference
        self.pedidos.append(spent.symbol)
        self.en_vuelo += 1
        self.maximo_en_vuelo = max(self.maximo_en_vuelo, self.en_vuelo)
        try:
            if self.retraso:
                await asyncio.sleep(self.retraso)
            if spent.symbol in self.falla:
                raise RuntimeError("el motor de precios se cayó")
            precio = self.precios.get(spent.symbol)
            if precio is None:
                return None
            return TokenAmount.from_decimal(
                Decimal(precio) * amount.as_decimal(), 6, "USDC", rounding="ROUND_DOWN"
            )
        finally:
            self.en_vuelo -= 1

    def describe_failure(self, spent: Token, reference: Token) -> str:
        del spent, reference
        return "no se pudo valorar"


def _foto(chain: str, holdings: tuple[TokenHolding, ...]) -> WalletSnapshot:
    return WalletSnapshot(
        profile=_perfil(), chains=(ChainHoldings(chain=chain, holdings=holdings),), read_at=AHORA
    )


async def test_la_valoracion_no_vuelve_a_leer_los_saldos() -> None:
    """La foto entra hecha: releerla duplicaría las peticiones al nodo."""
    motor = _Motor()
    foto = await _leer(motor)(_perfil())
    antes = list(motor.pedidas)

    await ValueWallet(valuation=_Valorador())(foto)

    assert motor.pedidas == antes, "la valoración volvió a pedir saldos a la red"


async def test_se_valora_y_el_total_de_la_red_queda_afirmado() -> None:
    valorada = await ValueWallet(valuation=_Valorador({"POL": "0.102"}))(
        _foto("polygon", (_nativo_holding(19_491_666_140_000_000_000, "polygon", None),))
    )
    cadena = valorada.chains[0]
    assert cadena.is_complete
    # 19,49166614 POL a 0,102 USDC = 1,988149…
    assert cadena.total_in_reference == Decimal("1.988149")
    assert valorada.total_in_reference == Decimal("1.988149")


async def test_el_nativo_se_valora_antes_que_nada() -> None:
    """Es lo que el usuario mira primero y siempre vale algo."""
    valorador = _Valorador({"POL": "1", "RARO": "1"})
    await ValueWallet(valuation=valorador, budget=1)(
        _foto(
            "polygon",
            (
                _holding("RARO", 1, "polygon", None),
                _nativo_holding(1, "polygon", None),
            ),
        )
    )
    assert valorador.pedidos == ["POL"]


async def test_lo_que_el_catalogo_conoce_se_valora_antes_que_lo_desconocido() -> None:
    """Gastar el presupuesto en mints anónimos antes de llegar al USDC sería absurdo."""
    del_catalogo = tokens_for("polygon")[0]
    conocida = TokenHolding(
        token=del_catalogo,
        amount=TokenAmount(
            raw=5_000_000, decimals=del_catalogo.decimals, symbol=del_catalogo.symbol
        ),
    )
    desconocida = _holding("RARO", 1, "polygon", None)
    valorador = _Valorador({"POL": "1", del_catalogo.symbol: "1", "RARO": "1"})

    await ValueWallet(valuation=valorador, budget=2)(
        _foto(
            "polygon",
            (desconocida, _nativo_holding(1, "polygon", None), conocida),
        )
    )

    assert valorador.pedidos == ["POL", del_catalogo.symbol]


async def test_lo_que_queda_fuera_del_presupuesto_se_dice_y_no_se_calla() -> None:
    """El caso medido: 3102 mints en una sola cartera de Solana."""
    holdings = tuple(_holding(f"T{i}", 1, "polygon", None) for i in range(10))
    valorador = _Valorador(dict.fromkeys([f"T{i}" for i in range(10)], "1"))

    valorada = await ValueWallet(valuation=valorador, budget=4)(
        _foto("polygon", holdings)
    )
    cadena = valorada.chains[0]

    assert len(valorador.pedidos) == 4, "el presupuesto no se respetó"
    assert not cadena.is_complete, "una red con posiciones sin valorar no está completa"
    assert cadena.total_in_reference is None
    assert len(cadena.missing) == 1
    assert "sin valorar" in cadena.missing[0]
    # Y la cifra que sí se sostiene sigue ahí, para poder enseñarla etiquetada:
    # cuatro posiciones de 0,000001 T a 1 USDC cada una.
    assert cadena.valued_total == Decimal("0.000004")
    assert len(cadena.unvalued) == 6


async def test_con_presupuesto_cero_no_se_valora_nada_y_la_red_lo_dice() -> None:
    valorador = _Valorador({"POL": "1"})
    valorada = await ValueWallet(valuation=valorador, budget=0)(
        _foto("polygon", (_nativo_holding(1, "polygon", None),))
    )
    assert valorador.pedidos == []
    assert not valorada.chains[0].is_complete
    assert valorada.chains[0].valued_total == Decimal(0)


async def test_el_tope_se_aplica_por_red_y_no_a_la_cartera_entera() -> None:
    """Ocho redes con cuatro tokens cada una son ocho presupuestos, no uno."""
    valorador = _Valorador(dict.fromkeys([f"T{i}" for i in range(6)], "1"))
    foto = WalletSnapshot(
        profile=_perfil(),
        chains=tuple(
            ChainHoldings(
                chain="polygon",
                holdings=tuple(_holding(f"T{i}", 1, "polygon", None) for i in range(6)),
            )
            for _ in range(3)
        ),
        read_at=AHORA,
    )

    await ValueWallet(valuation=valorador, budget=2)(foto)

    assert len(valorador.pedidos) == 6, "el presupuesto se gastó en la cartera, no por red"


async def test_el_orden_de_los_holdings_no_cambia_al_valorarlos() -> None:
    """La interfaz pinta en ese orden y `native` documenta que va primero."""
    simbolos = ["POL", "AAA", "BBB", "CCC"]
    holdings = tuple(
        _nativo_holding(1, "polygon", None)
        if s == "POL"
        else _holding(s, 0 if s == "BBB" else 1, "polygon", None)
        for s in simbolos
    )
    valorador = _Valorador(dict.fromkeys(simbolos, "1"))

    valorada = await ValueWallet(valuation=valorador)(_foto("polygon", holdings))

    assert [h.token.symbol for h in valorada.chains[0].holdings] == simbolos
    assert valorada.chains[0].holdings[0].token.is_native
    assert len(valorada.chains[0].holdings) == len(simbolos)


async def test_un_token_que_no_se_puede_valorar_se_queda_sin_valor_y_la_red_sin_total() -> None:
    valorada = await ValueWallet(valuation=_Valorador({"POL": "1"}))(
        _foto(
            "polygon",
            (_nativo_holding(1, "polygon", None), _holding("SINMERCADO", 1, "polygon", None)),
        )
    )
    assert valorada.chains[0].holdings[1].value_in_reference is None
    assert valorada.chains[0].total_in_reference is None


async def test_que_un_motor_de_precios_se_caiga_no_deja_la_cartera_sin_pintar() -> None:
    """Un motor con un mal día no puede tumbar la lectura de saldos."""
    valorada = await ValueWallet(valuation=_Valorador({"POL": "1"}, falla=["POL"]))(
        _foto("polygon", (_nativo_holding(1, "polygon", None),))
    )
    cadena = valorada.chains[0]
    assert len(cadena.holdings) == 1
    assert cadena.holdings[0].value_in_reference is None
    assert cadena.total_in_reference is None


async def test_una_red_sin_moneda_de_referencia_lo_dice_en_vez_de_dejarlo_en_un_log() -> None:
    """Antes esto salía como total `None` sin explicación. Ahora se explica."""
    valorada = await ValueWallet(valuation=_Valorador())(
        WalletSnapshot(
            profile=_perfil(),
            chains=(
                ChainHoldings(
                    chain="red-inventada",
                    holdings=(_holding("X", 1, "red-inventada", None),),
                ),
            ),
            read_at=AHORA,
        )
    )
    assert valorada.chains[0].missing == (SIN_REFERENCIA,)
    assert valorada.chains[0].total_in_reference is None


async def test_una_red_ilegible_no_se_valora_ni_se_toca() -> None:
    original = ChainHoldings(chain="bsc", error="el nodo no contestó")
    valorada = await ValueWallet(valuation=_Valorador())(
        WalletSnapshot(profile=_perfil(), chains=(original,), read_at=AHORA)
    )
    assert valorada.chains[0] == original


async def test_una_red_vacia_no_gasta_ni_una_cotizacion() -> None:
    valorador = _Valorador({"POL": "1"})
    valorada = await ValueWallet(valuation=valorador)(
        _foto("polygon", (_nativo_holding(0, "polygon", None),))
    )
    assert valorador.pedidos == []
    assert valorada.chains[0].is_complete, "no tener nada no es un dato incompleto"


async def test_las_valoraciones_de_una_red_respetan_la_concurrencia() -> None:
    valorador = _Valorador(dict.fromkeys([f"T{i}" for i in range(8)], "1"), retraso=0.01)
    await ValueWallet(valuation=valorador, concurrency=2)(
        _foto("polygon", tuple(_holding(f"T{i}", 1, "polygon", None) for i in range(8)))
    )
    assert valorador.maximo_en_vuelo <= 2


async def test_la_valoracion_conserva_las_redes_que_ya_estaban_valoradas() -> None:
    """Valorar dos veces no puede perder lo que ya se sabía."""
    foto = _foto(
        "polygon",
        (_nativo_holding(19_491_666_140_000_000_000, "polygon", "1.99"),),
    )
    valorador = _Valorador()
    valorada = await ValueWallet(valuation=valorador)(foto)
    assert valorada.chains[0].total_in_reference == Decimal("1.99")
    assert valorador.pedidos == ["POL"], "se volvió a cotizar algo ya valorado"


def test_la_referencia_de_cada_red_del_catalogo_existe() -> None:
    """Si a una red le faltara la referencia, su cartera no se podría valorar."""
    sin = [key for key in WALLET_CHAINS if quote_token(key) is None]
    assert sin == [], f"redes declaradas sin moneda de referencia: {sin}"
