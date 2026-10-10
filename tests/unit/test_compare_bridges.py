"""Pedirle la ruta a todos los motores de puentes y quedarse con el orden.

Lo que se fija aquí:

1. **Que se pregunte a todos**, no al preferido. Una lista con las rutas de un
   solo motor no compara nada, y comparar es lo único que hace falta para
   llamar a esto.
2. **Que el orden lo ponga el importe recibido** y no el orden de activación. La
   prueba activa los motores en un orden y espera el contrario: si el orden de
   llegada se colara, la «mejor» ruta cambiaría entre consultas sin que nada
   hubiera cambiado en el mercado.
3. **Que un motor caído no se lleve por delante a los demás**, y que su id quede
   dicho. Es lo que impide que una tabla a la que le falta una fuente parezca
   completa.
4. **Que no responder y no ofrecer nada se digan distinto.** No hay nadie a quien
   preguntar no es lo mismo que haber preguntado y recibido un no: lo primero se
   arregla activando un motor, lo segundo cambiando el importe.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from amigocompora.app.confirmation import ConfirmationGateway, RecordingPrompt
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.compare_bridges import CompareBridges
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.errors import (
    CurrencyMismatchError,
    InvalidAmountError,
    NoQuotesError,
    SourceResponseError,
)
from amigocompora.domain.models import (
    BridgeComparison,
    BridgeQuote,
    BridgeRequest,
    Measurement,
    Token,
    rank_bridges,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import EngineKind, EngineManifest

AHORA = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_POLYGON = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"


def _solicitud() -> BridgeRequest:
    return BridgeRequest(
        origin=Token("USDC", 6, "base", USDC_BASE),
        destination=Token("USDC", 6, "polygon", USDC_POLYGON),
        amount_in=TokenAmount(1_000_000, 6, "USDC"),
    )


def _ruta(
    *,
    engine_id: str,
    provider: str | None = None,
    out: int = 989_888,
    min_out: int | None = None,
    fee: int | None = 10_112,
    duration: int | None = 60,
    observed_at: datetime | None = AHORA,
    request: BridgeRequest | None = None,
) -> BridgeQuote:
    """Una cotización con lo que se quiera variar.

    El proveedor sale del motor por omisión, y no fijo: dos motores distintos
    cotizando el **mismo** puente es el caso que el comparador agrupa, así que
    ponerlo fijo aquí haría que casi todas las pruebas describieran sin querer ese
    caso en vez del normal.

    La comisión va en la moneda **entregada** y con su procedencia, porque el
    dominio no admite una sin la otra: una comisión sin decir cómo se obtuvo es
    una cifra que nadie puede comprobar.
    """
    peticion = request or _solicitud()
    destino = peticion.destination
    return BridgeQuote(
        engine_id=engine_id,
        provider=provider or f"Puente {engine_id}",
        request=peticion,
        amount_out=TokenAmount(out, destino.decimals, destino.symbol),
        amount_out_min=TokenAmount(
            min_out if min_out is not None else out, destino.decimals, destino.symbol
        ),
        fee=None if fee is None else TokenAmount(fee, 6, "USDC"),
        fee_basis=None if fee is None else Measurement.REPORTED,
        duration_seconds=duration,
        observed_at=observed_at,
    )


class _Planner:
    """Motor de puentes que devuelve lo que se le diga, o falla si se le pide."""

    __slots__ = ("_cotizaciones", "_falla", "_manifest", "_preguntas")

    def __init__(
        self,
        *,
        engine_id: str,
        cotizaciones: tuple[BridgeQuote, ...] = (),
        falla: Exception | None = None,
        chains: frozenset[str] = frozenset({"base"}),
        priority: int = 50,
    ) -> None:
        self._manifest = EngineManifest(
            engine_id=engine_id,
            name=f"Motor falso {engine_id}",
            version="1.0.0",
            kind=EngineKind.CROSS_CHAIN,
            summary="Doble de prueba.",
            capabilities=frozenset({Capability.READ_CHAIN, Capability.PREPARE_TX}),
            bridge_chains=chains,
            bridge_priority=priority,
        )
        self._cotizaciones = cotizaciones
        self._falla = falla
        self._preguntas = 0

    @property
    def preguntas(self) -> int:
        return self._preguntas

    @property
    def manifest(self) -> EngineManifest:
        return self._manifest

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def quote_bridge(self, request: BridgeRequest) -> Sequence[BridgeQuote]:
        self._preguntas += 1
        if self._falla is not None:
            raise self._falla
        return self._cotizaciones

    async def plan_bridge(self, quote: BridgeQuote, *, recipient: str) -> Any:
        raise NotImplementedError

    def expected_destination(self, chain_key: str) -> frozenset[str] | None:
        return None


class _Provider:
    def __init__(self, engine: _Planner) -> None:
        self.engine = engine

    @property
    def manifest(self) -> EngineManifest:
        return self.engine.manifest

    def create(self, config: object) -> _Planner:
        del config
        return self.engine


async def _compare(*engines: _Planner) -> CompareBridges:
    """Un comparador con esos motores en la ranura de puentes.

    El primero se **activa** y los demás se **suman**: `activate` deja al motor
    como único de su ranura, así que usarlo para todos dejaría al último solo y
    una prueba que creyera comparar dos motores estaría comparando uno.
    """
    registry = EngineRegistry(ModeGuard(OperationMode.ASSISTED))
    for position, engine in enumerate(engines):
        registry.register(_Provider(engine))
        if position == 0:
            await registry.activate(engine.manifest.engine_id)
        else:
            await registry.add(engine.manifest.engine_id)
    return CompareBridges(
        registry=registry,
        gateway=ConfirmationGateway(
            ModeGuard(OperationMode.ASSISTED),
            prompt=RecordingPrompt(answer=True),
            clock=FrozenClock(AHORA),
        ),
    )


# --------------------------------------------------------------------------- #
# La comparación
# --------------------------------------------------------------------------- #
async def test_se_pregunta_a_todos_los_motores() -> None:
    primero = _Planner(engine_id="uno", cotizaciones=(_ruta(engine_id="uno", out=990_000),))
    segundo = _Planner(engine_id="dos", cotizaciones=(_ruta(engine_id="dos", out=980_000),))
    compare = await _compare(primero, segundo)

    comp = await compare(_solicitud())

    assert primero.preguntas == 1
    assert segundo.preguntas == 1
    assert [route.quote.engine_id for route in comp.routes] == ["uno", "dos"]


async def test_manda_lo_que_entrega_y_no_quien_se_activo_primero() -> None:
    """El orden es por importe recibido, y la prueba lo fuerza al revés.

    Se activa primero el que **peor** paga: si el orden de activación se colara en
    la tabla, la mejor ruta que se enseña cambiaría según qué motor se activó
    antes, que no dice nada del precio.
    """
    peor = _Planner(engine_id="peor", cotizaciones=(_ruta(engine_id="peor", out=970_000),))
    mejor = _Planner(engine_id="mejor", cotizaciones=(_ruta(engine_id="mejor", out=995_000),))
    compare = await _compare(peor, mejor)

    comp = await compare(_solicitud())

    assert [route.quote.engine_id for route in comp.routes] == ["mejor", "peor"]
    assert comp.best.quote.engine_id == "mejor"
    # Las posiciones se numeran de 1 y en orden: la tabla las enseña tal cual, así
    # que una lista desordenada sería una tabla que numera mal la mejor ruta.
    assert [route.position for route in comp.routes] == [1, 2]
    assert comp.best.is_best


async def test_una_ruta_peor_no_se_descarta() -> None:
    """Enseñar sólo la mejor escondería que la segunda está a un 0,1 %."""
    buena = _Planner(engine_id="buena", cotizaciones=(_ruta(engine_id="buena", out=995_000),))
    casi = _Planner(engine_id="casi", cotizaciones=(_ruta(engine_id="casi", out=994_000),))
    compare = await _compare(buena, casi)

    comp = await compare(_solicitud())

    assert len(comp.routes) == 2


async def test_un_motor_caido_no_se_lleva_a_los_demas() -> None:
    """Y deja su id dicho: una tabla con dos filas donde había tres no es completa."""
    vivo = _Planner(engine_id="vivo", cotizaciones=(_ruta(engine_id="vivo"),))
    caido = _Planner(engine_id="caido", falla=SourceResponseError("502 del proveedor"))
    compare = await _compare(vivo, caido)

    comp = await compare(_solicitud())

    assert [route.quote.engine_id for route in comp.routes] == ["vivo"]
    assert comp.failed_engines == ("caido",)
    assert comp.has_partial_sources


async def test_si_todos_fallan_sale_el_motivo_de_verdad() -> None:
    """Se propaga el primer error en vez de envolverlo: envolverlo lo perdería.

    Y «el primero» es el del motor **preferido**, que es lo que la prioridad
    decide. Se le dan prioridades distintas a propósito: con la misma, el orden lo
    rompería el id por orden alfabético y la prueba estaría afirmando cuál de los
    dos motores se llama antes, que no es lo que quiere comprobar.
    """
    uno = _Planner(engine_id="uno", priority=10, falla=SourceResponseError("401 de la fuente"))
    dos = _Planner(engine_id="dos", priority=20, falla=SourceResponseError("429 de la fuente"))
    compare = await _compare(uno, dos)

    with pytest.raises(SourceResponseError, match="401"):
        await compare(_solicitud())


async def test_sin_motor_que_sepa_salir_de_esa_red_no_es_no_haber_ruta() -> None:
    """Se arregla activando un motor, no cambiando el importe."""
    otro = _Planner(
        engine_id="desde_solana",
        cotizaciones=(_ruta(engine_id="desde_solana"),),
        chains=frozenset({"solana"}),
    )
    compare = await _compare(otro)

    with pytest.raises(NoQuotesError, match="desde «base»"):
        await compare(_solicitud())


async def test_haber_preguntado_y_recibir_un_no_se_dice_con_los_motores() -> None:
    """El otro caso, y el mensaje tiene que ser distinto: aquí sí se miró."""
    mudo = _Planner(engine_id="mudo")
    compare = await _compare(mudo)

    with pytest.raises(NoQuotesError, match="Motores consultados: mudo"):
        await compare(_solicitud())


async def test_no_se_pregunta_a_quien_no_sale_de_esa_red() -> None:
    """Ni se le llama: el manifiesto ya dice por dónde puede cruzar."""
    desde_base = _Planner(engine_id="desde_base", cotizaciones=(_ruta(engine_id="desde_base"),))
    desde_solana = _Planner(
        engine_id="desde_solana", chains=frozenset({"solana"}), falla=AssertionError("no")
    )
    compare = await _compare(desde_base, desde_solana)

    comp = await compare(_solicitud())

    assert desde_solana.preguntas == 0
    assert comp.failed_engines == ()


# --------------------------------------------------------------------------- #
# Dos motores, el mismo puente
# --------------------------------------------------------------------------- #
async def test_el_mismo_proveedor_desde_dos_motores_es_una_fila() -> None:
    """LI.FI y Relay pueden acabar enrutando por el mismo puente.

    Dos filas del mismo camino no darían más información: darían dos cifras casi
    iguales y la ilusión de poder elegir entre ellas.
    """
    uno = _Planner(
        engine_id="lifi",
        cotizaciones=(_ruta(engine_id="lifi", provider="AcrossV4", out=990_000),),
    )
    dos = _Planner(
        engine_id="relay",
        cotizaciones=(_ruta(engine_id="relay", provider="AcrossV4", out=992_000),),
    )
    compare = await _compare(uno, dos)

    comp = await compare(_solicitud())

    assert len(comp.routes) == 1


async def test_de_dos_mediciones_del_mismo_puente_se_queda_la_primera() -> None:
    """Y no la mejor: quedarse con la mejor sería elegir la cifra que más conviene.

    El usuario acabaría construyendo contra un importe que puede no ser el que hay,
    así que gana el motor preferido —el primero de la pila— aunque publique menos.
    """
    preferido = _Planner(
        engine_id="lifi",
        cotizaciones=(_ruta(engine_id="lifi", provider="AcrossV4", out=985_000),),
    )
    respaldo = _Planner(
        engine_id="relay",
        cotizaciones=(_ruta(engine_id="relay", provider="AcrossV4", out=995_000),),
    )
    compare = await _compare(preferido, respaldo)

    comp = await compare(_solicitud())

    assert comp.best.quote.engine_id == "lifi"
    assert comp.best.quote.amount_out.raw == 985_000


async def test_proveedores_distintos_son_filas_distintas() -> None:
    uno = _Planner(
        engine_id="lifi",
        cotizaciones=(_ruta(engine_id="lifi", provider="AcrossV4", out=985_000),),
    )
    dos = _Planner(
        engine_id="relay",
        cotizaciones=(_ruta(engine_id="relay", provider="red de solvers", out=995_000),),
    )
    compare = await _compare(uno, dos)

    comp = await compare(_solicitud())

    assert {route.quote.provider for route in comp.routes} == {"AcrossV4", "red de solvers"}


async def test_un_motor_puede_devolver_varias_rutas() -> None:
    """Una fuente puede ofrecer caminos distintos, y todos valen."""
    uno = _Planner(
        engine_id="lifi",
        cotizaciones=(
            _ruta(engine_id="lifi", provider="AcrossV4", out=990_000),
            _ruta(engine_id="lifi", provider="Stargate", out=992_000),
        ),
    )
    compare = await _compare(uno)

    comp = await compare(_solicitud())

    assert [route.quote.provider for route in comp.routes] == ["Stargate", "AcrossV4"]


# --------------------------------------------------------------------------- #
# Lo que el valor comparado no admite
# --------------------------------------------------------------------------- #
def test_una_comision_sin_procedencia_no_se_puede_comparar() -> None:
    """Sin `fee_bps` la ruta parece gratis, y la bandera lo dice."""
    sin_comision = _ruta(engine_id="uno", fee=None)
    con_comision = _ruta(engine_id="dos")
    comp = BridgeComparison(
        request=_solicitud(),
        routes=rank_bridges((sin_comision, con_comision)),
    )
    assert comp.has_unknown_fees


def test_una_comparacion_ordenada_a_mano_no_vale() -> None:
    """El orden se decide una vez, y esta prueba es lo que lo sostiene.

    Si alguien construyera el objeto con las rutas al revés, la tabla numeraría
    mal la mejor ruta —y el botón de ejecutar usaría la que no es.
    """
    rutas = rank_bridges((_ruta(engine_id="uno", out=990_000), _ruta(engine_id="dos", out=980_000)))
    with pytest.raises(InvalidAmountError, match="ordenadas y numeradas"):
        BridgeComparison(request=_solicitud(), routes=tuple(reversed(rutas)))


def test_una_ruta_de_otro_cruce_no_vale() -> None:
    otra = BridgeRequest(
        origin=Token("USDC", 6, "base", USDC_BASE),
        destination=Token("WETH", 18, "polygon", "0x" + "7" * 40),
        amount_in=TokenAmount(1_000_000, 6, "USDC"),
    )
    with pytest.raises(CurrencyMismatchError, match="no USDC@base"):
        BridgeComparison(
            request=_solicitud(),
            routes=rank_bridges((_ruta(engine_id="uno", request=otra),)),
        )


def test_sin_rutas_no_hay_comparacion() -> None:
    with pytest.raises(InvalidAmountError, match="necesita una ruta"):
        BridgeComparison(request=_solicitud(), routes=())


def test_una_ruta_sin_hora_impide_fechar_la_comparacion() -> None:
    """La frescura del conjunto es la de su dato más viejo; sin él, no se sabe."""
    comp = BridgeComparison(
        request=_solicitud(),
        routes=rank_bridges((_ruta(engine_id="uno", observed_at=None),)),
    )
    with pytest.raises(InvalidAmountError, match="fechar la comparación"):
        _ = comp.observed_at


def test_la_fecha_de_la_comparacion_es_la_del_dato_mas_viejo() -> None:
    viejo = datetime(2026, 10, 7, 11, 0, tzinfo=UTC)
    comp = BridgeComparison(
        request=_solicitud(),
        routes=rank_bridges(
            (
                _ruta(engine_id="uno", out=990_000, observed_at=AHORA),
                _ruta(engine_id="dos", out=980_000, observed_at=viejo),
            )
        ),
    )
    assert comp.observed_at == viejo


def test_el_empate_lo_rompe_lo_que_se_garantiza() -> None:
    """A igualdad de importe recibido gana el que garantiza más."""
    garantiza = _ruta(engine_id="garantiza", provider="A", out=990_000, min_out=989_000)
    promete = _ruta(engine_id="promete", provider="B", out=990_000, min_out=960_000)
    comp = BridgeComparison(
        request=_solicitud(), routes=rank_bridges((promete, garantiza))
    )
    assert comp.best.quote.provider == "A"


# --------------------------------------------------------------------------- #
# La solicitud se le pasa entera
# --------------------------------------------------------------------------- #
async def test_la_solicitud_la_construye_quien_llama_y_no_el_comparador() -> None:
    """El comparador no arma la petición: la recibe.

    Es lo que hace que la ruta que se enseña y la que se ejecuta sean la misma
    petición —la que el usuario escribió en la pantalla— y no dos construcciones
    parecidas que podrían diferir en un decimal.
    """
    request = _solicitud()
    motor = _Planner(engine_id="uno", cotizaciones=(_ruta(engine_id="uno", request=request),))
    compare = await _compare(motor)

    comp = await compare(request)

    assert comp.request == request
    assert comp.best.quote.request == request


async def test_el_importe_del_tope_se_declara_en_la_solicitud() -> None:
    """El dominio no admite un importe de otro token: se comprueba al construir."""
    with pytest.raises(CurrencyMismatchError, match="no es del token de origen"):
        BridgeRequest(
            origin=Token("USDC", 6, "base", USDC_BASE),
            destination=Token("USDC", 6, "polygon", USDC_POLYGON),
            amount_in=TokenAmount(10**18, 18, "WETH"),
        )
