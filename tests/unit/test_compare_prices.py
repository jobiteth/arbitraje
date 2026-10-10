"""Comparar es la operación que quiere **todas** las fuentes, no la preferida.

Antes de esto, `ComparePrices` le preguntaba sólo al titular de la ranura: con
dos motores activos el segundo no aparecía en la tabla, así que tener un respaldo
no servía para comparar, sólo para cuando el primero se cayera. Estas pruebas
fijan las cuatro propiedades que hacen que la fusión sea correcta y no un
`sum` de listas:

- **Gana el mejor precio, no el motor preferido.** Es lo único que justifica
  fusionar: si el orden decidiera, el respaldo sería decorativo.
- **Una fuente caída no detiene la comparación.** Es el requisito de no
  detenerse nunca, y la razón de tener respaldos.
- **Pero tampoco desaparece en silencio.** Una tabla con dos filas donde había
  tres fuentes se lee como «aquí no hay nada mejor», y puede que la mejor fuera
  justo la que no respondió.
- **El mismo venue medido por dos motores no se duplica.** El `venue_id`
  identifica el protocolo, no la fuente: sin deduplicar, el `spread` sería la
  diferencia entre dos mediciones del mismo pool, o sea ruido con aspecto de
  oportunidad.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from amigocompora.app.confirmation import ConfirmationGateway, RecordingPrompt
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.compare_prices import ComparePrices
from amigocompora.domain.errors import (
    EngineError,
    NoActiveEngineError,
    NoQuotesError,
    SourceUnavailableError,
)
from amigocompora.domain.models import (
    Measurement,
    PlannedTransaction,
    Quote,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.domain.protocols import Engine, EngineKind, EngineManifest

NOW = datetime(2026, 1, 1, tzinfo=UTC)

_WETH = Token("WETH", 18, "ethereum", "0x" + "4" * 40)
_USDC = Token("USDC", 6, "ethereum", "0x" + "3" * 40)
PAIR = TradingPair(base=_WETH, quote=_USDC)
AMOUNT = TokenAmount(10**18, 18, "WETH")

#: Dos precios reales del par medido, en unidades mínimas de USDC: 2 690 y
#: 2 700 por WETH. La diferencia es de ~37 bps, que es lo que tiene que
#: sobrevivir a la fusión.
_OUT_2690 = 2_690_000_000
_OUT_2700 = 2_700_000_000
_OUT_2600 = 2_600_000_000

#: El pool que dos motores distintos pueden estar midiendo a la vez.
_POOL = "uniswap_v3@ethereum"


def _manifest(engine_id: str, *, priority: int = 100) -> EngineManifest:
    return EngineManifest(
        engine_id=engine_id,
        name=f"Motor {engine_id}",
        version="1.0.0",
        kind=EngineKind.DEX_QUOTES,
        summary="Doble de prueba.",
        capabilities=frozenset({Capability.READ_CHAIN}),
        swap_priority=priority,
    )


class _Engine:
    """Motor de doble: devuelve lo que se le diga, o falla si se le pide.

    `barrier` sirve para medir la concurrencia: dos dobles que la compartan no
    pueden responder hasta que los dos hayan empezado. `delay` retrasa la
    respuesta para poder invertir el orden en que llegan.
    """

    __slots__ = ("_barrier", "_delay", "_error", "_manifest", "_quotes", "asked")

    def __init__(
        self,
        manifest: EngineManifest,
        *,
        quotes: Sequence[Quote] = (),
        error: Exception | None = None,
        barrier: asyncio.Barrier | None = None,
        delay: float = 0.0,
    ) -> None:
        self._manifest = manifest
        self._quotes = tuple(quotes)
        self._error = error
        self._barrier = barrier
        self._delay = delay
        self.asked: list[TradingPair] = []

    @property
    def manifest(self) -> EngineManifest:
        return self._manifest

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def venues(self, chain_key: str) -> Sequence[Venue]:
        return ()

    async def quote(self, pair: TradingPair, amount_in: TokenAmount) -> Sequence[Quote]:
        self.asked.append(pair)
        if self._barrier is not None:
            # Si las consultas fueran secuenciales, la primera se quedaría aquí
            # esperando a una segunda que no ha llegado a empezar, y el plazo lo
            # delataría en vez de pasar.
            await asyncio.wait_for(self._barrier.wait(), timeout=1.0)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._quotes


class _NotAQuoter:
    """Está en la ranura DEX pero no cotiza: no cumple `DexQuoteEngine`."""

    __slots__ = ("_manifest",)

    def __init__(self, manifest: EngineManifest) -> None:
        self._manifest = manifest

    @property
    def manifest(self) -> EngineManifest:
        return self._manifest

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...


class _Builder(_Engine):
    """Motor que además de cotizar sabe construir el swap de lo que cotiza.

    Es la diferencia que decide el desempate cuando dos motores miden el mismo
    pool: el payload sale del motor que observó la cotización, así que quedarse
    con la del que sólo cotiza deja la fila sin poder firmarse.
    """

    async def plan_swap(self, quote: Quote, *, recipient: str) -> PlannedTransaction:
        raise AssertionError("comparar no construye nada")

    def expected_destination(self, chain_key: str) -> str | None:
        return None


def _builder_manifest(engine_id: str, *, priority: int = 100) -> EngineManifest:
    """Manifiesto de un motor que declara construir swaps en la red del par."""
    return EngineManifest(
        engine_id=engine_id,
        name=f"Motor {engine_id}",
        version="1.0.0",
        kind=EngineKind.DEX_QUOTES,
        summary="Doble de prueba que construye.",
        capabilities=frozenset({Capability.READ_CHAIN, Capability.PREPARE_TX}),
        swap_chains=frozenset({"ethereum"}),
        swap_priority=priority,
    )


@dataclass(frozen=True, slots=True)
class _Provider:
    engine: Engine
    manifest: EngineManifest

    def create(self, config: Mapping[str, str]) -> Engine:
        return self.engine


def _pool_quote(engine_id: str, out_raw: int) -> Quote:
    """El mismo pool de Uniswap V3 —el mismo `venue_id`— medido por un motor."""
    return _quote(engine_id=engine_id, venue_id=_POOL, out_raw=out_raw)


def _quote(
    *,
    engine_id: str,
    venue_id: str,
    out_raw: int,
    fee_bps: BasisPoints | None = None,
    venue_name: str | None = None,
) -> Quote:
    return Quote(
        venue=Venue(
            venue_id=venue_id,
            name=venue_name or venue_id,
            kind=VenueKind.DEX,
            chain="ethereum",
        ),
        engine_id=engine_id,
        pair=PAIR,
        amount_in=AMOUNT,
        amount_out=TokenAmount(out_raw, 6, "USDC"),
        fee_bps=fee_bps,
        # Los dos campos van juntos o no va ninguno; ver `Quote.__post_init__`.
        fee_basis=Measurement.REPORTED if fee_bps is not None else None,
        price_impact_bps=BasisPoints(1),
        impact_basis=Measurement.REPORTED,
        observed_at=NOW,
    )


async def _registry(*engines: _Engine | _NotAQuoter) -> EngineRegistry:
    """Registro con esos motores activos **en el orden dado**, que es la pila."""
    registry = EngineRegistry(ModeGuard(OperationMode.ASSISTED))
    for engine in engines:
        registry.register(_Provider(engine=engine, manifest=engine.manifest))
    for engine in engines:
        await registry.add(engine.manifest.engine_id)
    return registry


def _usecase(registry: EngineRegistry) -> ComparePrices:
    guard = ModeGuard(OperationMode.ASSISTED)
    return ComparePrices(
        registry=registry,
        gateway=ConfirmationGateway(guard, RecordingPrompt()),
    )


# --------------------------------------------------------------------------- #
# La fusión
# --------------------------------------------------------------------------- #
async def test_cotiza_todos_los_motores_de_la_ranura() -> None:
    """Con dos motores activos, la tabla tiene las filas de los dos.

    Es el cambio de fondo: antes el segundo motor sólo existía como Plan B.
    """
    first = _Engine(
        _manifest("agregado"),
        quotes=(_quote(engine_id="agregado", venue_id="agg@ethereum", out_raw=_OUT_2690),),
    )
    second = _Engine(
        _manifest("directo"),
        quotes=(_quote(engine_id="directo", venue_id="dir@ethereum", out_raw=_OUT_2700),),
    )
    comparison = await _usecase(await _registry(first, second))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 2
    assert {quote.engine_id for quote in comparison.quotes} == {"agregado", "directo"}
    assert len(first.asked) == 1
    assert len(second.asked) == 1


async def test_gana_el_mejor_precio_aunque_venga_del_respaldo() -> None:
    """El orden de la pila no decide el resultado; decide el precio.

    Si decidiera el orden, fusionar no aportaría nada: la tabla mostraría
    siempre al titular y el respaldo sólo serviría para cuando aquél fallara.
    """
    first = _Engine(
        _manifest("agregado"),
        quotes=(_quote(engine_id="agregado", venue_id="agg@ethereum", out_raw=_OUT_2690),),
    )
    second = _Engine(
        _manifest("directo"),
        quotes=(_quote(engine_id="directo", venue_id="dir@ethereum", out_raw=_OUT_2700),),
    )
    comparison = await _usecase(await _registry(first, second))(PAIR, AMOUNT)

    assert comparison.best.engine_id == "directo"
    assert comparison.best.amount_out.raw == _OUT_2700
    assert comparison.worst.engine_id == "agregado"


async def test_el_spread_sale_de_las_dos_mediciones_reales() -> None:
    """El diferencial que se muestra es el de las dos fuentes, no el de una."""
    first = _Engine(
        _manifest("agregado"),
        quotes=(_quote(engine_id="agregado", venue_id="agg@ethereum", out_raw=_OUT_2700),),
    )
    second = _Engine(
        _manifest("directo"),
        quotes=(_quote(engine_id="directo", venue_id="dir@ethereum", out_raw=_OUT_2600),),
    )
    comparison = await _usecase(await _registry(first, second))(PAIR, AMOUNT)

    assert comparison.best.amount_out.raw == _OUT_2700
    assert comparison.worst.amount_out.raw == _OUT_2600
    assert comparison.spread_bps.value > 0


# --------------------------------------------------------------------------- #
# El mismo venue visto por dos motores
# --------------------------------------------------------------------------- #
async def test_el_mismo_venue_medido_por_dos_motores_no_se_duplica() -> None:
    """El `venue_id` identifica el protocolo, no la fuente.

    GeckoTerminal y DexScreener mirando el mismo pool de Uniswap V3 producen el
    mismo identificador. Sin deduplicar, la tabla mostraría el mismo sitio dos
    veces y el `spread` sería la diferencia entre dos mediciones del mismo
    contrato: ruido con aspecto de oportunidad.
    """
    first = _Engine(
        _manifest("geckoterminal"),
        quotes=(_pool_quote("geckoterminal", _OUT_2690),),
    )
    second = _Engine(
        _manifest("dexscreener"),
        quotes=(_pool_quote("dexscreener", _OUT_2700),),
    )
    comparison = await _usecase(await _registry(first, second))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 1
    assert comparison.quotes[0].engine_id == "geckoterminal"


async def test_no_se_elige_la_mejor_de_dos_mediciones_del_mismo_pool() -> None:
    """Entre dos medidas del mismo contrato se queda la primera, no la mejor.

    Quedarse con la mejor sería escoger, de dos observaciones del mismo pool, la
    cifra que más conviene — y el usuario construiría contra un precio que no es
    el que hay.
    """
    first = _Engine(
        _manifest("geckoterminal"),
        quotes=(_pool_quote("geckoterminal", _OUT_2690),),
    )
    second = _Engine(
        _manifest("dexscreener"),
        quotes=(_pool_quote("dexscreener", _OUT_2700),),
    )
    comparison = await _usecase(await _registry(first, second))(PAIR, AMOUNT)

    assert comparison.best.amount_out.raw == _OUT_2690
    assert all(quote.amount_out.raw != _OUT_2700 for quote in comparison.quotes)


async def test_dos_mediciones_del_mismo_pool_no_inflan_el_spread() -> None:
    """La diferencia entre dos medidas del mismo pool no es una oportunidad.

    Con el mismo pool duplicado, el `spread` sería el del pool contra sí mismo
    más el ruido entre las dos observaciones. Aquí se fija que los dos extremos
    del diferencial son los dos **venues** de verdad.
    """
    first = _Engine(
        _manifest("geckoterminal"),
        quotes=(
            _pool_quote("geckoterminal", _OUT_2690),
            _quote(engine_id="geckoterminal", venue_id="otro@ethereum", out_raw=_OUT_2600),
        ),
    )
    second = _Engine(
        _manifest("dexscreener"),
        quotes=(_pool_quote("dexscreener", _OUT_2700),),
    )
    comparison = await _usecase(await _registry(first, second))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 2
    assert comparison.best.venue.venue_id == "uniswap_v3@ethereum"
    assert comparison.best.amount_out.raw == _OUT_2690
    assert comparison.worst.venue.venue_id == "otro@ethereum"


async def test_dos_venues_distintos_no_se_tocan() -> None:
    """Deduplicar es por venue, no por motor: dos sitios distintos son dos filas."""
    first = _Engine(
        _manifest("agregado"),
        quotes=(_pool_quote("agregado", _OUT_2690),),
    )
    second = _Engine(
        _manifest("directo"),
        quotes=(_quote(engine_id="directo", venue_id="zeroex@ethereum", out_raw=_OUT_2700),),
    )
    comparison = await _usecase(await _registry(first, second))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 2
    assert {quote.venue.venue_id for quote in comparison.quotes} == {
        "uniswap_v3@ethereum",
        "zeroex@ethereum",
    }


async def test_del_mismo_pool_se_queda_la_medicion_que_se_puede_firmar() -> None:
    """Entre dos medidas del mismo pool gana la del motor que construye el swap.

    Medido en Polygon con POL/USDC: GeckoTerminal observa `uniswap-v3@5` y el
    motor `uniswap_v3` observa ese mismo pool. Quedándose con la primera de la
    pila —GeckoTerminal— la fila seguía en la tabla pero dejaba de poder
    firmarse, porque el payload lo construye el motor que la observó. Con dos
    rutas directas colisionando a la vez no quedaba ninguna ruta ejecutable: la
    tabla llena y el botón de firmar apagado.

    No es quedarse con la mejor cifra: el criterio es la capacidad. Aquí el que
    construye mide **peor** (2 690 contra 2 700) y gana igual.
    """
    solo_cotiza = _Engine(
        _manifest("geckoterminal"),
        quotes=(_pool_quote("geckoterminal", _OUT_2700),),
    )
    construye = _Builder(
        _builder_manifest("uniswap_v3"),
        quotes=(_pool_quote("uniswap_v3", _OUT_2690),),
    )
    comparison = await _usecase(await _registry(solo_cotiza, construye))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 1
    assert comparison.quotes[0].engine_id == "uniswap_v3"
    assert comparison.quotes[0].amount_out.raw == _OUT_2690


async def test_el_que_construye_tampoco_se_pierde_si_cotiza_primero() -> None:
    """El desempate no depende del orden en que lleguen las dos mediciones."""
    construye = _Builder(
        _builder_manifest("uniswap_v3"),
        quotes=(_pool_quote("uniswap_v3", _OUT_2690),),
    )
    solo_cotiza = _Engine(
        _manifest("geckoterminal"),
        quotes=(_pool_quote("geckoterminal", _OUT_2700),),
    )
    comparison = await _usecase(await _registry(construye, solo_cotiza))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 1
    assert comparison.quotes[0].engine_id == "uniswap_v3"


async def test_entre_dos_que_construyen_sigue_mandando_la_pila() -> None:
    """Con la capacidad empatada vuelve a decidir la preferencia del usuario.

    Y no la cifra: elegir aquí la mejor de dos observaciones del mismo contrato
    sería quedarse con el número que más conviene.
    """
    primera = _Builder(
        _builder_manifest("uniswap_v3"),
        quotes=(_pool_quote("uniswap_v3", _OUT_2690),),
    )
    segunda = _Builder(
        _builder_manifest("zeroex"),
        quotes=(_pool_quote("zeroex", _OUT_2700),),
    )
    comparison = await _usecase(await _registry(primera, segunda))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 1
    assert comparison.quotes[0].engine_id == "uniswap_v3"


async def test_el_que_construye_en_otra_red_no_gana_el_desempate() -> None:
    """Construir «en general» no es construir **esta** red.

    El desempate se resuelve con `planners_for(red del par)`: un motor que
    declara swaps sólo en Base no puede firmar una ruta de Ethereum, y dejarle
    ganar la fila volvería a dejarla sin poder construirse.
    """
    solo_cotiza = _Engine(
        _manifest("geckoterminal"),
        quotes=(_pool_quote("geckoterminal", _OUT_2700),),
    )
    otra_red = _Builder(
        EngineManifest(
            engine_id="de_base",
            name="Motor de Base",
            version="1.0.0",
            kind=EngineKind.DEX_QUOTES,
            summary="Construye, pero en otra red.",
            capabilities=frozenset({Capability.READ_CHAIN, Capability.PREPARE_TX}),
            swap_chains=frozenset({"base"}),
        ),
        quotes=(_pool_quote("de_base", _OUT_2690),),
    )
    comparison = await _usecase(await _registry(solo_cotiza, otra_red))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 1
    assert comparison.quotes[0].engine_id == "geckoterminal"


# --------------------------------------------------------------------------- #
# No detenerse, pero no callarse
# --------------------------------------------------------------------------- #
async def test_un_motor_caido_no_detiene_la_comparacion() -> None:
    """Es el requisito de no detenerse nunca, y la razón de tener respaldos."""
    caido = _Engine(_manifest("caido"), error=SourceUnavailableError("la fuente no responde"))
    vivo = _Engine(
        _manifest("vivo"),
        quotes=(_quote(engine_id="vivo", venue_id="dir@ethereum", out_raw=_OUT_2700),),
    )
    comparison = await _usecase(await _registry(caido, vivo))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 1
    assert comparison.best.engine_id == "vivo"


async def test_una_fuente_caida_queda_dicha() -> None:
    """Una tabla con menos filas de las que fuentes había no puede parecer completa."""
    caido = _Engine(_manifest("caido"), error=SourceUnavailableError("la fuente no responde"))
    vivo = _Engine(
        _manifest("vivo"),
        quotes=(_quote(engine_id="vivo", venue_id="dir@ethereum", out_raw=_OUT_2700),),
    )
    comparison = await _usecase(await _registry(caido, vivo))(PAIR, AMOUNT)

    assert comparison.failed_engines == ("caido",)
    assert comparison.has_partial_sources


async def test_una_comparacion_completa_no_se_marca_como_parcial() -> None:
    """La marca tiene que significar algo: sin fallos, no se enciende."""
    engine = _Engine(
        _manifest("unico"),
        quotes=(_quote(engine_id="unico", venue_id="dir@ethereum", out_raw=_OUT_2700),),
    )
    comparison = await _usecase(await _registry(engine))(PAIR, AMOUNT)

    assert comparison.failed_engines == ()
    assert not comparison.has_partial_sources


async def test_todas_las_fuentes_caidas_propagan_el_fallo_de_la_preferida() -> None:
    """Decir «no hay cotizaciones» aquí sería afirmar algo del mercado.

    No se pudo mirar, que es otra cosa. Se propaga el fallo real de la
    preferida —el que mejor lo explica— en vez de envolverlo en un error que
    perdería el motivo.
    """
    primera = _Engine(_manifest("preferida"), error=SourceUnavailableError("cuota agotada"))
    segunda = _Engine(_manifest("respaldo"), error=RuntimeError("otra cosa"))

    with pytest.raises(SourceUnavailableError, match="cuota agotada"):
        await _usecase(await _registry(primera, segunda))(PAIR, AMOUNT)


async def test_sin_liquidez_en_ninguna_fuente_viva_da_no_quotes() -> None:
    """Aquí sí se miró y no había: el mensaje lo puede afirmar."""
    una = _Engine(_manifest("una"))
    otra = _Engine(_manifest("otra"))

    with pytest.raises(NoQuotesError, match="probablemente no hay liquidez"):
        await _usecase(await _registry(una, otra))(PAIR, AMOUNT)


async def test_sin_liquidez_se_nombra_a_quien_no_respondio() -> None:
    """Faltar liquidez y faltar una fuente son cosas distintas, y se ven juntas."""
    caido = _Engine(_manifest("caido"), error=SourceUnavailableError("la fuente no responde"))
    vacio = _Engine(_manifest("vacio"))

    with pytest.raises(NoQuotesError, match="No respondieron: caido"):
        await _usecase(await _registry(caido, vacio))(PAIR, AMOUNT)


# --------------------------------------------------------------------------- #
# El tiempo: se pregunta a todos a la vez
# --------------------------------------------------------------------------- #
async def test_los_motores_se_preguntan_a_la_vez() -> None:
    """El tiempo de la comparación es el del motor más lento, no la suma.

    En secuencia —como eran— cada motor esperaba a que el anterior acabara:
    con cinco activos la primera cifra tardaba varios segundos, y el más lento
    retrasaba también a los que ya habían respondido. Los dos dobles se esperan
    en una barrera: si las consultas no fueran concurrentes, la primera se
    quedaría esperando a una segunda que no llegaría a empezar y el plazo la
    haría fallar en vez de pasar.
    """
    barrera = asyncio.Barrier(2)
    una = _Engine(
        _manifest("una"),
        quotes=(_quote(engine_id="una", venue_id="una@ethereum", out_raw=_OUT_2690),),
        barrier=barrera,
    )
    otra = _Engine(
        _manifest("otra"),
        quotes=(_quote(engine_id="otra", venue_id="otra@ethereum", out_raw=_OUT_2700),),
        barrier=barrera,
    )

    comparison = await _usecase(await _registry(una, otra))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 2
    assert comparison.failed_engines == ()


async def test_el_desempate_no_depende_de_quien_responda_primero() -> None:
    """El pliegue va en el orden de la pila aunque la red conteste al revés.

    Las dos mediciones son del mismo pool, así que se queda una: la primera de
    la pila. Da igual que sea la que más tarda en llegar —en paralelo, quien
    responde antes es quien tiene menos latencia, no quien manda—.
    """
    lenta = _Engine(
        _manifest("preferida"),
        quotes=(_pool_quote("preferida", _OUT_2690),),
        delay=0.05,
    )
    rapida = _Engine(
        _manifest("respaldo"),
        quotes=(_pool_quote("respaldo", _OUT_2700),),
    )

    comparison = await _usecase(await _registry(lenta, rapida))(PAIR, AMOUNT)

    assert len(comparison.quotes) == 1
    assert comparison.quotes[0].engine_id == "preferida"
    assert comparison.quotes[0].amount_out.raw == _OUT_2690


# --------------------------------------------------------------------------- #
# La ranura
# --------------------------------------------------------------------------- #
async def test_sin_motor_activo_no_se_compara() -> None:
    registry = EngineRegistry(ModeGuard(OperationMode.ASSISTED))

    with pytest.raises(NoActiveEngineError, match="panel de motores"):
        await _usecase(registry)(PAIR, AMOUNT)


async def test_una_ranura_con_un_motor_que_no_cotiza_se_detecta_al_pedirla() -> None:
    """Se valida la ranura entera antes de devolver nada.

    Si se devolviera la tupla a medias, el fallo aparecería al llamar al motor
    que no cotiza, lejos de la causa y con un `AttributeError` en vez de una
    frase que diga qué pasa.
    """
    registry = await _registry(_Engine(_manifest("bueno")), _NotAQuoter(_manifest("roto")))

    with pytest.raises(EngineError, match="DexQuoteEngine"):
        registry.active_dex_stack()
