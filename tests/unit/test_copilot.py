"""El bucle del copiloto: pedir datos, leerlos, responder o proponer.

El motor de IA sabe responder a una pregunta, y nada más: no tiene herramientas
en su contrato. El bucle que las convierte en conversación vive aquí, y lo que
estas pruebas fijan es su contrato —que una vuelta de herramienta devuelva el
resultado al modelo, que un fallo no se convierta en silencio, que agotar el
presupuesto se diga, y que proponer no sea ejecutar—. Lo que el modelo responde
va doblado; lo que se comprueba es lo que la aplicación hace con ello.

El caso que más importa: cuando el JSON no se entiende, el turno **no** se
pierde. Lo que el modelo dijo se trata como su respuesta y se enseña —el peor
caso es un turno sin herramientas, que es lo que hacía el copiloto de antes—.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime

from amigocompora.app.chat_store import ChatMessage, ChatRole
from amigocompora.app.copilot import (
    MAX_STEPS,
    Copilot,
    CopilotEvent,
    CopilotEventKind,
    CopilotReply,
    ProposalKind,
)
from amigocompora.app.copilot_tools import CopilotToolError, CopilotTools
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
from amigocompora.domain.protocols import AnalysisRequest, AnalysisResult

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
_IN = Token("USDC", 6, "polygon", "0x" + "3" * 40)
_OUT = Token("WPOL", 18, "polygon", "0x" + "4" * 40)
_VENUE = Venue(
    venue_id="uniswap_v3@polygon", name="Uniswap v3", kind=VenueKind.DEX, chain="polygon"
)

_SWAP = (
    '{"accion": "proponer", "texto": "Te lo preparo", "operacion": {"tipo": "swap", '
    '"red": "polygon", "entrada": "USDC", "salida": "WPOL", "importe": "50"}}'
)
_PUENTE = (
    '{"accion": "proponer", "texto": "Te lo preparo", "operacion": {"tipo": "puente", '
    '"origen": "base", "destino": "polygon", "token": "USDC", "importe": "20"}}'
)
_COTIZAR = (
    '{"accion": "herramienta", "herramienta": "cotizacion", '
    '"argumentos": {"red": "polygon", "entrada": "USDC", "salida": "WPOL", "importe": "50"}}'
)


def _quote(amount_out: TokenAmount) -> Quote:
    return Quote(
        venue=_VENUE,
        pair=TradingPair(base=_IN, quote=_OUT),
        amount_in=TokenAmount.from_decimal(50, 6, "USDC"),
        amount_out=amount_out,
        engine_id="uniswap",
        fee_bps=BasisPoints(5),
        fee_basis=Measurement.REPORTED,
        price_impact_bps=BasisPoints(2),
        impact_basis=Measurement.DERIVED,
        observed_at=NOW,
    )


def _comparison() -> PriceComparison:
    return PriceComparison(
        pair=TradingPair(base=_IN, quote=_OUT),
        amount_in=TokenAmount.from_decimal(50, 6, "USDC"),
        quotes=(_quote(TokenAmount.from_decimal(100, 18, "WPOL")),),
    )


def _puentes() -> BridgeComparison:
    peticion = BridgeRequest(
        origin=Token("USDC", 6, "base", "0x" + "7" * 40),
        destination=_IN,
        amount_in=TokenAmount.from_decimal(20, 6, "USDC"),
    )
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
class FakeAnalyze:
    """El motor de IA doblado: contesta lo que se le diga, en orden.

    Se queda sin respuestas a propósito —si el bucle pide una vuelta más de las
    previstas, la prueba se cae con un motivo claro en vez de girar sin fin—.
    """

    def __init__(self, *respuestas: str) -> None:
        self.respuestas = list(respuestas)
        self.peticiones: list[AnalysisRequest] = []

    async def __call__(self, request: AnalysisRequest) -> AnalysisResult:
        self.peticiones.append(request)
        if not self.respuestas:
            raise AssertionError("el copiloto pidió más vueltas de las previstas")
        return AnalysisResult(summary=self.respuestas.pop(0))


class FakePrices:
    def __init__(self, respuesta: PriceComparison | Exception) -> None:
        self.respuesta = respuesta
        self.llamadas: list[tuple[TradingPair, TokenAmount]] = []

    async def __call__(self, pair: TradingPair, amount_in: TokenAmount) -> PriceComparison:
        self.llamadas.append((pair, amount_in))
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        return self.respuesta


class FakeBridges:
    def __init__(self, respuesta: BridgeComparison | Exception) -> None:
        self.respuesta = respuesta
        self.llamadas: list[BridgeRequest] = []

    async def __call__(self, request: BridgeRequest) -> BridgeComparison:
        self.llamadas.append(request)
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        return self.respuesta


class _NoUsado:
    """Un doble que se cae si alguien lo llama: la prueba dice qué NO se usó."""

    def __call__(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("esta herramienta no debía usarse en esta prueba")

    async def by_address(self, chain_key: str, address: str) -> Token:
        raise AssertionError("esta herramienta no debía usarse en esta prueba")


def _tools(
    *,
    precios: FakePrices | None = None,
    puentes: FakeBridges | None = None,
) -> CopilotTools:
    return CopilotTools(
        read_wallet=_NoUsado(),  # type: ignore[arg-type]
        compare_prices=precios or FakePrices(_comparison()),  # type: ignore[arg-type]
        compare_bridges=puentes or FakeBridges(_puentes()),  # type: ignore[arg-type]
        token_lookup=_NoUsado(),  # type: ignore[arg-type]
    )


def _copilot(fake: FakeAnalyze, tools: CopilotTools | None = None, **kwargs: int) -> Copilot:
    return Copilot(analyze=fake, tools=tools or _tools(), **kwargs)  # type: ignore[arg-type]


def _tipos(reply: CopilotReply) -> list[CopilotEventKind]:
    return [evento.kind for evento in reply.events]


# --------------------------------------------------------------------------- #
# Responder
# --------------------------------------------------------------------------- #
async def test_una_respuesta_directa_no_consulta_nada() -> None:
    fake = FakeAnalyze('{"accion": "responder", "texto": "Hola", "hallazgos": ["un dato"]}')

    reply = await _copilot(fake).ask("¿qué tal?")

    assert reply.text == "Hola"
    assert reply.findings == ("un dato",)
    assert reply.proposal is None
    assert _tipos(reply) == [CopilotEventKind.ANSWER]
    assert len(fake.peticiones) == 1


async def test_el_texto_sin_json_es_la_respuesta() -> None:
    """Un modelo que se sale del contrato no rompe el chat: se lee lo que dijo."""
    fake = FakeAnalyze("No puedo ayudarte con eso.")

    reply = await _copilot(fake).ask("¿qué tal?")

    assert reply.text == "No puedo ayudarte con eso."
    assert reply.proposal is None
    assert len(fake.peticiones) == 1, "y no se le vuelve a preguntar"


async def test_un_json_roto_no_se_toma_por_una_accion() -> None:
    """Una llave suelta no puede convertirse en una orden ni en un turno perdido."""
    fake = FakeAnalyze('{"accion": "herramienta", "herramienta":')

    reply = await _copilot(fake).ask("¿qué tal?")

    assert reply.text == '{"accion": "herramienta", "herramienta":'


async def test_al_modelo_se_le_describen_las_herramientas_y_el_contrato() -> None:
    fake = FakeAnalyze("vale")

    await _copilot(fake).ask("¿qué tal?")

    peticion = fake.peticiones[0]
    assert "accion" in peticion.context["instrucciones_de_respuesta"]
    assert "herramienta" in peticion.context["instrucciones_de_respuesta"]
    assert peticion.question == "¿qué tal?", "la pregunta viaja con las palabras del usuario"
    assert "saldos" in peticion.context["herramientas_disponibles"]
    assert "todavía ninguna" in peticion.context["resultados_de_herramientas"]


async def test_el_historial_que_viaja_son_las_dos_voces() -> None:
    """Los resultados viejos de herramientas no se repiten: ya pueden no ser ciertos."""
    fake = FakeAnalyze("vale")
    historial = (
        ChatMessage(role=ChatRole.USER, text="¿tiene saldo?", at=NOW),
        ChatMessage(role=ChatRole.TOOL, text="12 USDC", at=NOW),
        ChatMessage(role=ChatRole.COPILOT, text="Sí, 12 USDC.", at=NOW),
    )

    await _copilot(fake).ask("¿y puedes moverlo?", history=historial)

    conversacion = fake.peticiones[0].context["conversacion"]
    assert "usuario: ¿tiene saldo?" in conversacion
    assert "copiloto: Sí, 12 USDC." in conversacion
    assert "12 USDC\n" not in conversacion, "el resultado de la herramienta no vuelve"


# --------------------------------------------------------------------------- #
# Pedir herramientas
# --------------------------------------------------------------------------- #
async def test_pide_una_herramienta_y_el_resultado_vuelve_al_modelo() -> None:
    fake = FakeAnalyze(_COTIZAR, '{"accion": "responder", "texto": "Cien WPOL"}')
    precios = FakePrices(_comparison())

    reply = await _copilot(fake, _tools(precios=precios)).ask("¿cuánto me dan por 50 USDC?")

    assert reply.text == "Cien WPOL"
    assert _tipos(reply) == [
        CopilotEventKind.TOOL,
        CopilotEventKind.TOOL_RESULT,
        CopilotEventKind.ANSWER,
    ]
    assert len(fake.peticiones) == 2
    devuelto = fake.peticiones[1].context["resultados_de_herramientas"]
    assert "[cotizacion]" in devuelto
    assert "100 WPOL" in devuelto, "la cifra llega al modelo tal como se observó"
    assert precios.llamadas[0][1].raw == 50_000_000


async def test_un_fallo_de_herramienta_le_llega_al_modelo_con_el_motivo() -> None:
    """Sin el motivo, el modelo sólo puede inventarse la causa."""
    fake = FakeAnalyze(_COTIZAR, '{"accion": "responder", "texto": "No hay motores."}')
    precios = FakePrices(CopilotToolError("no hay ningún motor de precios activo"))

    reply = await _copilot(fake, _tools(precios=precios)).ask("¿cuánto me dan?")

    assert "no hay ningún motor de precios activo" in (
        fake.peticiones[1].context["resultados_de_herramientas"]
    )
    assert reply.text == "No hay motores."


async def test_un_motor_roto_tampoco_tumba_el_turno() -> None:
    fake = FakeAnalyze(_COTIZAR, '{"accion": "responder", "texto": "Voy a intentarlo."}')
    precios = FakePrices(RuntimeError("la red se cayó"))

    reply = await _copilot(fake, _tools(precios=precios)).ask("¿cuánto me dan?")

    assert "la red se cayó" in fake.peticiones[1].context["resultados_de_herramientas"]
    assert reply.text == "Voy a intentarlo."


async def test_una_herramienta_que_no_existe_se_le_dice_al_modelo() -> None:
    fake = FakeAnalyze(
        '{"accion": "herramienta", "herramienta": "ejecutar_swap", "argumentos": {}}',
        '{"accion": "responder", "texto": "No puedo hacer eso."}',
    )

    reply = await _copilot(fake).ask("mueve mis fondos")

    assert "ejecutar_swap" in fake.peticiones[1].context["resultados_de_herramientas"]
    assert reply.text == "No puedo hacer eso."


async def test_agotar_el_presupuesto_de_herramientas_se_dice() -> None:
    fake = FakeAnalyze(*([_COTIZAR] * (MAX_STEPS + 1)))

    reply = await _copilot(fake).ask("¿cuánto me dan?")

    assert str(MAX_STEPS) in reply.text
    assert _tipos(reply)[-1] is CopilotEventKind.ERROR
    assert len(fake.peticiones) == MAX_STEPS + 1


# --------------------------------------------------------------------------- #
# Proponer
# --------------------------------------------------------------------------- #
async def test_una_propuesta_de_swap_llega_con_la_cotizacion_observada() -> None:
    """Se ejecutará exactamente la cifra que se enseñó, no una nueva cotización."""
    fake = FakeAnalyze(_SWAP)
    comparacion = _comparison()
    precios = FakePrices(comparacion)

    reply = await _copilot(fake, _tools(precios=precios)).ask("cámbiame 50 USDC por WPOL")

    propuesta = reply.proposal
    assert propuesta is not None
    assert propuesta.kind is ProposalKind.SWAP
    assert propuesta.ready
    assert propuesta.quote is comparacion.quotes[0], "la misma que devolvió el motor"
    assert "50 USDC" in propuesta.summary
    assert "Uniswap v3" in propuesta.summary
    assert _tipos(reply) == [CopilotEventKind.PROPOSAL]
    assert precios.llamadas[0][0].symbol == "USDC/WPOL"


async def test_una_propuesta_de_puente_llega_con_su_ruta() -> None:
    fake = FakeAnalyze(_PUENTE)
    rutas = _puentes()
    puentes = FakeBridges(rutas)

    reply = await _copilot(fake, _tools(puentes=puentes)).ask("pasa 20 USDC a Polygon")

    propuesta = reply.proposal
    assert propuesta is not None
    assert propuesta.kind is ProposalKind.BRIDGE
    assert propuesta.ready
    assert propuesta.bridge is rutas.routes[0].quote
    assert "Across" in propuesta.summary
    assert "3 min" in propuesta.summary
    assert puentes.llamadas[0].amount_in.raw == 20_000_000


async def test_una_operacion_que_no_existe_no_se_convierte_en_propuesta() -> None:
    fake = FakeAnalyze(
        '{"accion": "proponer", "texto": "", "operacion": {"tipo": "lanzar_cohete"}}'
    )

    reply = await _copilot(fake).ask("haz algo raro")

    propuesta = reply.proposal
    assert propuesta is not None
    assert propuesta.kind is ProposalKind.NONE
    assert not propuesta.ready
    assert "swap" in propuesta.error
    assert "puente" in propuesta.error
    assert _tipos(reply) == [CopilotEventKind.ERROR]
    assert reply.text == propuesta.error, "y se dice aunque el modelo no lo dijera"


async def test_una_propuesta_que_no_se_pudo_resolver_no_lleva_cifras() -> None:
    fake = FakeAnalyze(_SWAP)
    precios = FakePrices(CopilotToolError("«USDC» no está en el catálogo de polygon"))

    reply = await _copilot(fake, _tools(precios=precios)).ask("cámbiame 50 USDC")

    propuesta = reply.proposal
    assert propuesta is not None
    assert not propuesta.ready
    assert propuesta.quote is None, "sin cotización no hay nada que ejecutar"
    assert "no está en el catálogo" in propuesta.error
    assert _tipos(reply) == [CopilotEventKind.ERROR]


async def test_proponer_no_puede_ejecutar_porque_no_tiene_con_quien() -> None:
    """La garantía no es una promesa en un comentario: es que el turno no tiene emisor.

    `Copilot` sólo sabe pedirle respuestas al modelo y datos a las herramientas de
    lectura. No hay firmante, ni cartera, ni pasarela de confirmación entre sus
    campos, así que ninguna respuesta del modelo —ni un JSON inventado— puede
    acabar en una transacción desde aquí.
    """
    assert {campo.name for campo in fields(Copilot)} == {"analyze", "tools", "max_steps"}


# --------------------------------------------------------------------------- #
# El observador
# --------------------------------------------------------------------------- #
async def test_un_observador_que_revienta_no_rompe_el_turno() -> None:
    """Un fallo al pintar no es un fallo del copiloto: la conversación ya va."""
    fake = FakeAnalyze('{"accion": "responder", "texto": "Hola"}')
    vistos: list[CopilotEvent] = []

    def observador(evento: CopilotEvent) -> None:
        vistos.append(evento)
        raise RuntimeError("la pantalla se cerró")

    reply = await _copilot(fake).ask("¿qué tal?", on_event=observador)

    assert reply.text == "Hola"
    assert [evento.kind for evento in vistos] == [CopilotEventKind.ANSWER]


async def test_los_pasos_llegan_al_observador_en_orden() -> None:
    fake = FakeAnalyze(_COTIZAR, '{"accion": "responder", "texto": "Cien WPOL"}')
    vistos: list[CopilotEvent] = []

    await _copilot(fake).ask("¿cuánto me dan?", on_event=vistos.append)

    assert [evento.kind for evento in vistos] == [
        CopilotEventKind.TOOL,
        CopilotEventKind.TOOL_RESULT,
        CopilotEventKind.ANSWER,
    ]
    assert "cotizacion" in vistos[0].title
    assert "50" in vistos[0].detail, "los argumentos se enseñan tal como se pidieron"


async def test_la_comision_se_le_devuelve_al_modelo_como_la_dice_la_aplicacion() -> None:
    """El modelo no traduce unidades: recibe «5 bps» y lo repite tal cual."""
    fake = FakeAnalyze(_COTIZAR, '{"accion": "responder", "texto": "vale"}')

    await _copilot(fake).ask("¿cuánto me dan?")

    devuelto = fake.peticiones[1].context["resultados_de_herramientas"]
    assert "comisión 5 bps" in devuelto
    assert "Cotización de 50 USDC" in devuelto
