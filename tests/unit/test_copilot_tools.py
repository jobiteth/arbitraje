"""Las cuatro herramientas del copiloto: qué resuelven y cómo lo cuentan.

La herramienta es la frontera entre el modelo y los datos: lo que devuelve es
**todo** lo que el modelo va a saber, así que estas pruebas miran el texto que
sale —que lleve las cifras con sus límites, que diga lo que no se pudo leer y
por qué— y no sólo que no reviente. Un resultado que se coma la comisión
desconocida o el motor caído hace que la respuesta del modelo suene completa sin
serlo, que es la única forma de mentir que tiene este camino.

Ninguna prueba sale a la red: los cuatro casos de uso van doblados.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

import pytest

from amigocompora.app.copilot_tools import SPECS, CopilotToolError, CopilotTools
from amigocompora.domain.models import (
    BridgeComparison,
    BridgeQuote,
    BridgeRequest,
    BridgeRoute,
    Measurement,
    PriceComparison,
    Quote,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.domain.wallet import (
    ChainHoldings,
    TokenHolding,
    WalletKind,
    WalletProfile,
    WalletSnapshot,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
_DIRECCION = "0x" + "a" * 40

_USDC = Token("USDC", 6, "polygon", "0x" + "3" * 40)
#: El envoltorio nativo de Polygon, que es el que el catálogo conoce por símbolo:
#: `POL` a secas es la moneda nativa y no se cotiza contra una piscina, así que
#: resolver «POL» por símbolo no funcionaría —y eso es lo correcto—.
_WPOL = Token("WPOL", 18, "polygon", "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270")
_WETH = Token("WETH", 18, "polygon", "0x" + "5" * 40)
_USDC_BASE = Token("USDC", 6, "base", "0x" + "7" * 40)
_VENUE = Venue(
    venue_id="uniswap_v3@polygon", name="Uniswap v3", kind=VenueKind.DEX, chain="polygon"
)
#: Valores por defecto de las pruebas. Van en constantes y no escritos en los
#: argumentos porque `BasisPoints(...)` es una llamada, y una llamada en un valor
#: por defecto se evalúa una sola vez —compartida por todas las llamadas— y
#: ruff lo señala con razón.
_FEE = BasisPoints(5)
_IMPACT = BasisPoints(2)


def _perfil() -> WalletProfile:
    return WalletProfile(
        wallet_id="consulta-copiloto",
        label="Consulta del copiloto",
        kind=WalletKind.EVM,
        address=_DIRECCION,
    )


def _foto(*cadenas: ChainHoldings) -> WalletSnapshot:
    return WalletSnapshot(profile=_perfil(), chains=tuple(cadenas), read_at=NOW)


def _quote(
    amount_out: TokenAmount,
    *,
    fee_bps: BasisPoints | None = _FEE,
    impact: BasisPoints | None = _IMPACT,
    engine_id: str = "uniswap",
) -> Quote:
    return Quote(
        venue=_VENUE,
        pair=TradingPair(base=_USDC, quote=_WPOL),
        amount_in=TokenAmount.from_decimal(50, 6, "USDC"),
        amount_out=amount_out,
        engine_id=engine_id,
        fee_bps=fee_bps,
        fee_basis=Measurement.REPORTED if fee_bps is not None else None,
        price_impact_bps=impact,
        impact_basis=Measurement.DERIVED if impact is not None else None,
        observed_at=NOW,
    )


def _comparison(
    *out: TokenAmount,
    fee_bps: BasisPoints | None = _FEE,
    failed: tuple[str, ...] = (),
) -> PriceComparison:
    return PriceComparison(
        pair=TradingPair(base=_USDC, quote=_WPOL),
        amount_in=TokenAmount.from_decimal(50, 6, "USDC"),
        quotes=tuple(_quote(amount, fee_bps=fee_bps) for amount in out),
        failed_engines=failed,
    )


def _bridge_comparison() -> BridgeComparison:
    peticion = BridgeRequest(
        origin=_USDC_BASE,
        destination=_USDC,
        amount_in=TokenAmount.from_decimal(20, 6, "USDC"),
    )
    # Los importes van en texto y no en coma flotante: `from_decimal` es exacto
    # —rechaza lo que no cabe— y un `19.8` binario no lo es.
    cita = BridgeQuote(
        engine_id="lifi",
        provider="Across",
        request=peticion,
        amount_out=TokenAmount.from_decimal("19.8", 6, "USDC"),
        amount_out_min=TokenAmount.from_decimal("19.5", 6, "USDC"),
        fee=TokenAmount.from_decimal("0.2", 6, "USDC"),
        fee_basis=Measurement.REPORTED,
        duration_seconds=180,
    )
    return BridgeComparison(request=peticion, routes=(BridgeRoute(position=1, quote=cita),))


# --------------------------------------------------------------------------- #
# Dobles
# --------------------------------------------------------------------------- #
class FakeReadWallet:
    """Una lectura de cartera doblada: devuelve la foto que se le diga."""

    def __init__(self, foto: WalletSnapshot) -> None:
        self.foto = foto
        self.llamadas: list[tuple[str, tuple[str, ...] | None]] = []

    async def __call__(
        self, profile: WalletProfile, *, chains: tuple[str, ...] | None = None, concurrency: int = 6
    ) -> WalletSnapshot:
        self.llamadas.append((profile.address, chains))
        return self.foto


class FakeComparePrices:
    def __init__(self, comparacion: PriceComparison) -> None:
        self.comparacion = comparacion
        self.llamadas: list[tuple[TradingPair, TokenAmount]] = []

    async def __call__(self, pair: TradingPair, amount_in: TokenAmount) -> PriceComparison:
        self.llamadas.append((pair, amount_in))
        return self.comparacion


class FakeCompareBridges:
    def __init__(self, comparacion: BridgeComparison) -> None:
        self.comparacion = comparacion
        self.llamadas: list[BridgeRequest] = []

    async def __call__(self, request: BridgeRequest) -> BridgeComparison:
        self.llamadas.append(request)
        return self.comparacion


class FakeTokenLookup:
    def __init__(self, token: Token) -> None:
        self.token = token
        self.llamadas: list[tuple[str, str]] = []

    async def by_address(self, chain_key: str, address: str) -> Token:
        self.llamadas.append((chain_key, address))
        return self.token


class FakeTokens:
    """Los tokens añadidos a mano, reducidos a lo que la herramienta les pide: recorrerlos."""

    def __init__(self, tokens: tuple[Token, ...] = ()) -> None:
        self._tokens = tokens

    def __iter__(self) -> Iterator[Token]:
        return iter(self._tokens)


#: Una salida de cotización de las pruebas: cien WPOL, con la comisión puesta.
_UN_WPOL = TokenAmount.from_decimal(100, 18, "WPOL")


def _tools(
    *,
    wallet: FakeReadWallet | None = None,
    precios: FakeComparePrices | None = None,
    puentes: FakeCompareBridges | None = None,
    lookup: FakeTokenLookup | None = None,
    tokens: tuple[Token, ...] = (),
) -> CopilotTools:
    """Las herramientas con dobles de fábrica, para que cada prueba cambie la suya."""
    return CopilotTools(
        read_wallet=wallet or FakeReadWallet(_foto()),  # type: ignore[arg-type]
        compare_prices=precios or FakeComparePrices(_comparison(_UN_WPOL)),  # type: ignore[arg-type]
        compare_bridges=puentes or FakeCompareBridges(_bridge_comparison()),  # type: ignore[arg-type]
        token_lookup=lookup or FakeTokenLookup(_WPOL),  # type: ignore[arg-type]
        tokens=FakeTokens(tokens),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# El manual y el despacho
# --------------------------------------------------------------------------- #
def test_el_manual_nombra_las_cuatro_herramientas() -> None:
    manual = _tools().manual()
    for spec in SPECS:
        assert spec.name in manual
        assert spec.args in manual
    assert len(SPECS) == 4, "saldos, cotización, token y puente"


async def test_una_herramienta_que_no_existe_lo_dice_con_la_lista() -> None:
    with pytest.raises(CopilotToolError) as fallo:
        await _tools().run("ejecutar_swap", {})
    assert "ejecutar_swap" in str(fallo.value)
    assert "saldos" in str(fallo.value)


# --------------------------------------------------------------------------- #
# saldos
# --------------------------------------------------------------------------- #
async def test_saldos_ensena_solo_lo_que_tiene_fondos() -> None:
    foto = _foto(
        ChainHoldings(
            chain="polygon",
            holdings=(
                TokenHolding(token=_USDC, amount=TokenAmount.from_decimal("12.5", 6, "USDC")),
                TokenHolding(token=_WPOL, amount=TokenAmount(0, 18, "WPOL")),
            ),
        )
    )
    wallet = FakeReadWallet(foto)

    salida = await _tools(wallet=wallet).balances({"direccion": _DIRECCION})

    assert "12.5 USDC" in salida
    _, en_polygon = salida.split("polygon:", 1)
    assert "WPOL" not in en_polygon, "un saldo de cero no es un saldo"
    assert wallet.llamadas == [(_DIRECCION, None)], "sin `redes` se leen todas"


async def test_saldos_cuenta_la_red_que_no_se_pudo_leer_con_su_motivo() -> None:
    """Una red ilegible es un dato, no un hueco: el modelo tiene que poder decirlo."""
    foto = _foto(ChainHoldings(chain="polygon", error="no hay motor de cartera activo"))

    salida = await _tools(wallet=FakeReadWallet(foto)).balances({"direccion": _DIRECCION})

    assert "no se pudo leer" in salida
    assert "no hay motor de cartera activo" in salida


async def test_las_redes_pedidas_se_pasan_a_la_lectura() -> None:
    wallet = FakeReadWallet(_foto())

    await _tools(wallet=wallet).balances({"direccion": _DIRECCION, "redes": ["polygon"]})

    assert wallet.llamadas == [(_DIRECCION, ("polygon",))]


async def test_saldos_rechaza_una_red_que_no_existe() -> None:
    with pytest.raises(CopilotToolError) as fallo:
        await _tools().balances({"direccion": _DIRECCION, "redes": ["marte"]})
    assert "marte" in str(fallo.value)
    assert "ethereum" in str(fallo.value), "y dice cuáles hay"


async def test_saldos_rechaza_una_direccion_mal_formada() -> None:
    """No es una lectura vacía: es un error que dice que la dirección no vale."""
    with pytest.raises(CopilotToolError) as fallo:
        await _tools().balances({"direccion": "no-es-una-direccion"})
    assert "no-es-una-direccion" in str(fallo.value)


# --------------------------------------------------------------------------- #
# cotizacion
# --------------------------------------------------------------------------- #
async def test_la_cotizacion_ordena_de_mejor_a_peor_y_marca_lo_que_falta() -> None:
    precios = FakeComparePrices(
        _comparison(
            TokenAmount.from_decimal(9_000, 18, "WPOL"),
            TokenAmount.from_decimal(10_000, 18, "WPOL"),
            failed=("zeroex",),
        )
    )

    salida = await _tools(precios=precios).quote(
        {"red": "polygon", "entrada": "USDC", "salida": "WPOL", "importe": "50"}
    )

    assert salida.index("10000 WPOL") < salida.index("9000 WPOL"), "la mejor primero"
    assert "zeroex" in salida
    assert "no respondieron" in salida
    assert "construye: uniswap" in salida
    par, importe = precios.llamadas[0]
    assert par.symbol == "USDC/WPOL"
    assert importe.raw == 50_000_000


async def test_una_comision_sin_desglosar_se_dice_y_no_se_resta() -> None:
    precios = FakeComparePrices(_comparison(_UN_WPOL, fee_bps=None))

    salida = await _tools(precios=precios).quote(
        {"red": "polygon", "entrada": "USDC", "salida": "WPOL", "importe": "50"}
    )

    assert "sin desglosar" in salida


async def test_un_token_se_resuelve_por_su_direccion_leyendo_el_contrato() -> None:
    lookup = FakeTokenLookup(_WETH)
    precios = FakeComparePrices(_comparison(_UN_WPOL))

    await _tools(lookup=lookup, precios=precios).quote(
        {"red": "polygon", "entrada": "0x" + "5" * 40, "salida": "WPOL", "importe": "1"}
    )

    assert lookup.llamadas == [("polygon", "0x" + "5" * 40)]
    par, _ = precios.llamadas[0]
    assert par.base.symbol == "WETH", "la dirección se convirtió en el token que se cotiza"


async def test_un_token_anadido_a_mano_se_encuentra_por_su_simbolo() -> None:
    """Lo que el usuario añadió en la pantalla de tokens es lo que él va a nombrar."""
    lookup = FakeTokenLookup(_WETH)
    precios = FakeComparePrices(_comparison(_UN_WPOL))

    await _tools(lookup=lookup, precios=precios, tokens=(_WETH,)).quote(
        {"red": "polygon", "entrada": "WETH", "salida": "WPOL", "importe": "1"}
    )

    par, _ = precios.llamadas[0]
    assert par.base.symbol == "WETH"
    assert lookup.llamadas == [], "un símbolo conocido no cuesta una lectura de contrato"


async def test_un_simbolo_que_no_esta_en_el_catalogo_se_dice_con_salida() -> None:
    with pytest.raises(CopilotToolError) as fallo:
        await _tools().quote(
            {"red": "polygon", "entrada": "INVENTADO", "salida": "WPOL", "importe": "1"}
        )
    assert "INVENTADO" in str(fallo.value)
    assert "dirección" in str(fallo.value), "y ofrece la vía que sí funciona"


async def test_un_importe_que_no_cabe_en_los_decimales_se_rechaza_sin_redondear() -> None:
    with pytest.raises(CopilotToolError) as fallo:
        await _tools().quote(
            {"red": "polygon", "entrada": "USDC", "salida": "WPOL", "importe": "0.1234567891"}
        )
    assert "6 decimales" in str(fallo.value)


async def test_una_coma_decimal_se_entiende() -> None:
    """Quien escribe en español escribe «1,5», y el modelo lo copia tal cual."""
    precios = FakeComparePrices(_comparison(_UN_WPOL))

    await _tools(precios=precios).quote(
        {"red": "polygon", "entrada": "USDC", "salida": "WPOL", "importe": "1,5"}
    )

    assert precios.llamadas[0][1].raw == 1_500_000


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #
async def test_la_info_de_un_token_son_su_simbolo_sus_decimales_y_su_contrato() -> None:
    salida = await _tools(lookup=FakeTokenLookup(_WETH)).token_info(
        {"red": "polygon", "direccion": "0x" + "5" * 40}
    )

    assert "WETH" in salida
    assert "18 decimales" in salida
    assert "0x" + "5" * 40 in salida


# --------------------------------------------------------------------------- #
# puente
# --------------------------------------------------------------------------- #
async def test_un_puente_ensena_lo_que_llega_y_lo_que_esta_garantizado() -> None:
    salida = await _tools().bridge(
        {"origen": "base", "destino": "polygon", "token": "USDC", "importe": "20"}
    )

    assert "entrega 19.8 USDC" in salida
    assert "mínimo garantizado 19.5 USDC" in salida
    assert "3 min" in salida
    assert "Across" in salida


async def test_un_puente_que_no_cruza_se_rechaza() -> None:
    with pytest.raises(CopilotToolError) as fallo:
        await _tools().bridge(
            {"origen": "polygon", "destino": "polygon", "token": "USDC", "importe": "20"}
        )
    assert "misma red" in str(fallo.value)
    assert "swap" in str(fallo.value), "y dice cuál es la herramienta que sí es"


@pytest.mark.parametrize("clave", ["importe", "red", "entrada"])
async def test_falta_un_argumento_obligatorio_y_se_dice_cual(clave: str) -> None:
    args: Mapping[str, object] = {
        "red": "polygon",
        "entrada": "USDC",
        "salida": "WPOL",
        "importe": "1",
    }
    incompleto = {k: v for k, v in args.items() if k != clave}

    with pytest.raises(CopilotToolError) as fallo:
        await _tools().quote(incompleto)
    assert clave in str(fallo.value)
