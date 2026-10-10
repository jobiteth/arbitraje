"""El copiloto: un turno de conversación, con herramientas y con propuestas.

### Lo que cambia respecto al copiloto anterior

Antes era un formulario: se escribía una pregunta, se pegaban a mano las cifras
de otras pantallas, y el modelo analizaba eso. El modelo seguía sin salir a
buscar datos —que era el objetivo— pero el que los buscaba era la persona, y una
pregunta como «¿esta dirección tiene saldo?» no se podía contestar de ninguna
forma.

Ahora el modelo **pide** los datos que necesita, la aplicación los trae por los
mismos casos de uso que usan las pantallas, y el resultado vuelve al modelo.
Sigue sin tener red y sin poder inventarse una cifra: sólo puede pedir una de
las cuatro herramientas de `copilot_tools`, que son de sólo lectura.

### Cómo se habla con un motor que sólo sabe responder

El contrato de `AiAdvisorEngine` es una sola llamada: pregunta y respuesta. No
hay soporte de herramientas en el protocolo, y añadirlo obligaría a tocar los
cuatro proveedores —Claude, DeepSeek, ChatGPT, el de respaldo— y sus pruebas.
Así que el bucle vive **por encima**: en cada vuelta se le pide al motor que
responda con un JSON de tres formas posibles —pedir una herramienta, responder,
o proponer una operación—, se ejecuta lo que pida y se le devuelve el resultado.
Funciona con cualquier proveedor sin cambiarlo, y con un doble en las pruebas.

### La lectura del JSON es tolerante, y no por comodidad

Un modelo puede contestar con vallas de código, con una frase delante o con el
JSON partido. Nada de eso puede convertir el chat en un error: si el JSON no se
entiende, lo que dijo se trata como la respuesta final y se enseña tal cual. El
peor caso es un turno sin herramientas —lo que hacía el copiloto de antes—, no
un turno perdido.

### Proponer no es ejecutar

La tercera acción del contrato fabrica una **propuesta**: la mejor cotización
para un swap o la mejor ruta de un puente, con la cifra ya observada. El modelo
no puede firmar nada —no tiene forma de pedirlo— y la propuesta no se ejecuta
sola: la enseña la pantalla con su botón, y a partir de ahí manda el camino de
siempre, con sus diálogos de confirmación y sus topes. Con la autonomía armada y
dentro de los límites, ese mismo camino puede cerrarse sin preguntar, pero quien
lo decide es la política, no el modelo.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import structlog

from amigocompora.app.chat_store import ChatMessage, ChatRole
from amigocompora.app.copilot_tools import CopilotToolError, CopilotTools
from amigocompora.app.usecases.analyze_with_ai import AnalyzeWithAi
from amigocompora.domain.models import BridgeQuote, Quote
from amigocompora.domain.protocols import AnalysisRequest

_log = structlog.get_logger(__name__)


#: El protocolo que se le explica al modelo en cada vuelta. Viaja en el contexto
#: —bajo `instrucciones_de_respuesta`— y no dentro de la pregunta: para los
#: proveedores las dos cosas acaban en el mismo mensaje del usuario, así que el
#: modelo lo lee igual, pero la pregunta se queda con las palabras de la persona,
#: que son lo que la aplicación enseña cuando el modelo no contesta. Con el
#: protocolo delante de la pregunta, un fallo del modelo mostraba el protocolo en
#: JSON como si fuera su respuesta. Va en cada vuelta porque el prompt del
#: sistema de los proveedores dice que el rol es sólo analizar, y es aquí donde
#: se le cuenta que además puede pedir datos.
PROTOCOL: Final = """\
Responde EXCLUSIVAMENTE con un objeto JSON, sin texto alrededor y sin vallas de \
código. Las tres formas posibles son:

1. Pedir datos a una herramienta:
   {"accion": "herramienta", "herramienta": "<nombre>", "argumentos": {...}}
2. Responder al usuario:
   {"accion": "responder", "texto": "<respuesta>", "hallazgos": ["<dato o riesgo>"]}
3. Proponer una operación (sólo si el usuario la pidió; nunca la ejecutas tú):
   {"accion": "proponer", "texto": "<explicación>",
    "operacion": {"tipo": "swap", "red": "...", "entrada": "...", "salida": "...",
                  "importe": "..."}}
   o
   {"accion": "proponer", "texto": "<explicación>",
    "operacion": {"tipo": "puente", "origen": "...", "destino": "...",
                  "token": "...", "importe": "..."}}

Reglas:
- Pide una herramienta cuando te falte un dato. No inventes cifras ni saldos: \
si no lo has consultado, no lo sabes.
- Una herramienta por respuesta, y espera su resultado antes de pedir otra.
- Cuando ya tengas lo necesario, responde citando las cifras tal como te \
llegaron. Si algo falló, dilo con el motivo.
- La aplicación es la que confirma y ejecuta: tú propones, el usuario decide.
"""

#: Cuántas herramientas puede pedir el modelo en un mismo turno. Seis es holgado
#: para las preguntas que estas herramientas contestan —saldos, una cotización,
#: un token—, y acota lo que puede tardar un turno cuando el modelo se atasca
#: pidiendo lo mismo en bucle.
MAX_STEPS: Final = 6

#: Cuántos mensajes del historial viajan al modelo. Una conversación larga no
#: cabe en la ventana del modelo, y lo que importa para seguir el hilo son los
#: últimos; el historial completo se queda en pantalla.
MAX_HISTORY_MESSAGES: Final = 12

#: Tope de caracteres por resultado de herramienta, para que una tabla enorme no
#: se coma el contexto de las siguientes vueltas.
MAX_TOOL_CHARS: Final = 4000


class CopilotEventKind(StrEnum):
    """Lo que va pasando en un turno, para poder contarlo en la pantalla.

    La pantalla no recibe el resultado final y nada más: recibe cada paso. Un
    turno que consulta saldos y cotiza tarda segundos, y durante esos segundos
    lo único honesto es decir qué se está haciendo — y al terminar, poder
    enseñar qué se consultó, que es la prueba de dónde salió cada cifra.
    """

    TOOL = "herramienta"  # el modelo pidió una herramienta
    TOOL_RESULT = "resultado"  # y esto fue lo que devolvió
    ANSWER = "respuesta"
    PROPOSAL = "propuesta"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CopilotEvent:
    kind: CopilotEventKind
    title: str
    detail: str = ""


class ProposalKind(StrEnum):
    SWAP = "swap"
    BRIDGE = "puente"
    #: Una operación que no se pudo resolver —o que no es de las que existen—.
    #: Tiene miembro propio para que una propuesta rota no tenga que fingir que
    #: era un swap: la pantalla la enseña como lo que es, un error con su motivo.
    NONE = "ninguna"


@dataclass(frozen=True, slots=True)
class CopilotProposal:
    """Una operación que el chat propone, ya resuelta contra el mercado.

    `quote`/`bridge` llevan la cifra **observada** —la misma que se enseñó—, no
    una intención: cuando el usuario pulse «Revisar y ejecutar», lo que se
    ejecuta es exactamente lo que se leyó. Si la propuesta no se pudo resolver,
    `error` lo dice y no viaja ninguna cotización.
    """

    kind: ProposalKind
    summary: str
    quote: Quote | None = None
    bridge: BridgeQuote | None = None
    error: str = ""

    @property
    def ready(self) -> bool:
        return self.error == "" and (self.quote is not None or self.bridge is not None)


@dataclass(frozen=True, slots=True)
class CopilotReply:
    """Lo que devuelve un turno: el texto, sus pasos y —si la hay— la propuesta."""

    text: str
    findings: tuple[str, ...] = ()
    disclaimer: str = ""
    proposal: CopilotProposal | None = None
    events: tuple[CopilotEvent, ...] = ()

    @property
    def tool_events(self) -> tuple[CopilotEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.kind in (CopilotEventKind.TOOL, CopilotEventKind.TOOL_RESULT)
        )


# --------------------------------------------------------------------------- #
# El JSON del modelo
# --------------------------------------------------------------------------- #
class _Action(StrEnum):
    TOOL = "herramienta"
    ANSWER = "responder"
    PROPOSE = "proponer"


@dataclass(frozen=True, slots=True)
class _Parsed:
    action: _Action
    text: str = ""
    findings: tuple[str, ...] = ()
    tool: str = ""
    args: Mapping[str, object] = field(default_factory=dict)
    operation: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Copilot:
    """Un turno de conversación con herramientas.

    Es `frozen` como los demás casos de uso: lo que cambia entre turnos es el
    argumento, no el objeto. El estado de la conversación vive en el
    `ChatStore` —que es del usuario, y sobrevive al cierre— y no aquí.
    """

    analyze: AnalyzeWithAi
    tools: CopilotTools
    max_steps: int = MAX_STEPS

    async def ask(
        self,
        question: str,
        *,
        history: Sequence[ChatMessage] = (),
        on_event: Callable[[CopilotEvent], None] | None = None,
    ) -> CopilotReply:
        """Responde a una pregunta, consultando lo que haga falta por el camino."""
        eventos: list[CopilotEvent] = []

        def anunciar(kind: CopilotEventKind, title: str, detail: str = "") -> None:
            # El observador **no puede romper el turno**: un fallo al pintar no
            # es un fallo del copiloto, y la conversación ya está en marcha.
            evento = CopilotEvent(kind=kind, title=title, detail=detail)
            eventos.append(evento)
            if on_event is None:
                return
            try:
                on_event(evento)
            except Exception:
                _log.warning("copilot.listener_failed", kind=str(kind))

        contexto: dict[str, str] = {
            "herramientas_disponibles": self.tools.manual(),
            "conversacion": _historial(history),
            "instrucciones_de_respuesta": PROTOCOL,
        }
        resultados: list[str] = []

        for paso in range(self.max_steps + 1):
            contexto["resultados_de_herramientas"] = (
                "\n\n".join(resultados) if resultados else "(todavía ninguna)"
            )
            result = await self.analyze(
                AnalysisRequest(
                    question=question,
                    # Una copia por vuelta, y no el diccionario vivo: cada
                    # petición tiene que describir lo que se sabía **entonces**,
                    # no lo que se sepa cuando la última vuelta lo haya llenado.
                    context=dict(contexto),
                )
            )
            accion = _parse(result.summary)

            if accion is None or accion.action is _Action.ANSWER:
                # Lo que no se entiende como JSON se trata como la respuesta: es
                # lo que el modelo dijo, y enseñarlo es mejor que esconderlo. En
                # el peor caso el turno no consultó nada, que es justo lo que
                # hacía el copiloto de antes.
                texto = (accion.text if accion is not None else "") or result.summary
                hallazgos = accion.findings if accion is not None else result.findings
                anunciar(CopilotEventKind.ANSWER, "Respuesta del copiloto", texto)
                return CopilotReply(
                    text=texto,
                    findings=hallazgos,
                    disclaimer=result.disclaimer,
                    events=tuple(eventos),
                )

            if accion.action is _Action.TOOL:
                if paso >= self.max_steps:
                    break
                salida = await self._run_tool(accion, anunciar)
                resultados.append(f"[{accion.tool}] {salida[:MAX_TOOL_CHARS]}")
                continue

            # Proponer: resolver la operación contra el mercado y devolverla. La
            # propuesta **no** se ejecuta aquí; eso es de la pantalla, con sus
            # diálogos y sus topes.
            propuesta = await self._resolve_proposal(accion.operation)
            if not propuesta.ready:
                anunciar(
                    CopilotEventKind.ERROR,
                    "No se pudo preparar la operación",
                    propuesta.error,
                )
            else:
                anunciar(CopilotEventKind.PROPOSAL, "Operación propuesta", propuesta.summary)
            return CopilotReply(
                text=accion.text or propuesta.summary or propuesta.error,
                findings=accion.findings,
                disclaimer=result.disclaimer,
                proposal=propuesta,
                events=tuple(eventos),
            )

        # Se agotó el presupuesto de herramientas sin llegar a una respuesta. Se
        # dice tal cual, con lo último que devolvió el modelo a la vista: parar
        # en silencio dejaría al usuario esperando algo que ya no va a llegar.
        anunciar(
            CopilotEventKind.ERROR,
            "El copiloto no llegó a una respuesta",
            f"pidió más de {self.max_steps} herramientas en un mismo turno",
        )
        return CopilotReply(
            text=(
                f"No llegué a una respuesta: pedí más de {self.max_steps} "
                f"herramientas en un mismo turno. Prueba a preguntar algo más "
                f"concreto —una cotización, unos saldos— y lo intento otra vez."
            ),
            events=tuple(eventos),
        )

    async def _run_tool(
        self,
        accion: _Parsed,
        anunciar: Callable[[CopilotEventKind, str, str], None],
    ) -> str:
        """Ejecuta una herramienta y devuelve su resultado, o el motivo del fallo.

        Un fallo **no** aborta el turno: se le devuelve al modelo con el motivo,
        que es lo único que le permite explicarle a la persona qué pasó. Convertir
        el error en una excepción dejaría el chat sin respuesta por no poder leer
        una cartera.
        """
        anunciar(
            CopilotEventKind.TOOL,
            f"Consultando «{accion.tool}»",
            _argumentos_legibles(accion.args),
        )
        try:
            salida = await self.tools.run(accion.tool, accion.args)
        except CopilotToolError as error:
            salida = f"la herramienta no pudo hacer su trabajo: {error}"
        except Exception as error:
            # Un motor roto —o una fuente caída— no puede tumbar el chat: el
            # turno sigue y el modelo recibe el motivo para poder explicarlo.
            _log.warning("copilot.tool_failed", tool=accion.tool, reason=repr(error))
            salida = f"la herramienta falló: {error}"
        anunciar(CopilotEventKind.TOOL_RESULT, f"Resultado de «{accion.tool}»", salida)
        return salida

    async def _resolve_proposal(self, operacion: Mapping[str, object]) -> CopilotProposal:
        """Convierte lo que pidió el modelo en una operación con su cifra real."""
        tipo = str(operacion.get("tipo") or "").strip().lower()
        if tipo == ProposalKind.SWAP:
            try:
                precios = await self.tools.quote_comparison(operacion)
            except CopilotToolError as error:
                return CopilotProposal(
                    kind=ProposalKind.SWAP, summary="", error=str(error)
                )
            mejor = precios.best
            return CopilotProposal(
                kind=ProposalKind.SWAP,
                summary=(
                    f"Vender {mejor.amount_in} por {mejor.amount_out} en "
                    f"{precios.pair.chain} · mejor ruta: {mejor.venue.name} "
                    f"(motor {mejor.engine_id})"
                ),
                quote=mejor,
            )
        if tipo == ProposalKind.BRIDGE:
            try:
                rutas = await self.tools.bridge_comparison(operacion)
            except CopilotToolError as error:
                return CopilotProposal(
                    kind=ProposalKind.BRIDGE, summary="", error=str(error)
                )
            mejor_ruta = rutas.best.quote
            return CopilotProposal(
                kind=ProposalKind.BRIDGE,
                summary=(
                    f"Mover {mejor_ruta.request.amount_in} de "
                    f"{mejor_ruta.request.origin.chain} a "
                    f"{mejor_ruta.request.destination.chain} · entrega "
                    f"{mejor_ruta.amount_out} (mínimo {mejor_ruta.amount_out_min}) "
                    f"por {mejor_ruta.provider}, en {mejor_ruta.duration_label}"
                ),
                bridge=mejor_ruta,
            )
        return CopilotProposal(
            kind=ProposalKind.NONE,
            summary="",
            error=(
                f"«{tipo or 'sin tipo'}» no es una operación que se pueda proponer. "
                f"Sólo hay swap y puente."
            ),
        )


# --------------------------------------------------------------------------- #
# Lectura del JSON del modelo
# --------------------------------------------------------------------------- #
def _parse(texto: str) -> _Parsed | None:
    """El JSON del modelo, o `None` si lo que dijo no es una acción.

    Se busca el primer `{` y se decodifica desde ahí con `raw_decode`, que
    tolera lo que venga detrás —una frase de cierre, una segunda llave—. Las
    vallas de código se aceptan porque los tres proveedores las ponen de vez en
    cuando a pesar de pedir lo contrario, y rechazar el turno por eso sería
    castigar al usuario por una manía del modelo.
    """
    crudo = texto.strip()
    inicio = crudo.find("{")
    if inicio < 0:
        return None
    try:
        datos, _ = json.JSONDecoder().raw_decode(crudo[inicio:])
    except ValueError:
        return None
    if not isinstance(datos, dict):
        return None

    accion_cruda = str(datos.get("accion") or "").strip().lower()
    if accion_cruda == _Action.ANSWER:
        return _Parsed(
            action=_Action.ANSWER,
            text=str(datos.get("texto") or "").strip(),
            findings=_hallazgos(datos.get("hallazgos")),
        )
    if accion_cruda == _Action.TOOL:
        herramienta = str(datos.get("herramienta") or "").strip()
        if not herramienta:
            return None
        argumentos = datos.get("argumentos")
        return _Parsed(
            action=_Action.TOOL,
            tool=herramienta,
            args=argumentos if isinstance(argumentos, dict) else {},
        )
    if accion_cruda == _Action.PROPOSE:
        operacion = datos.get("operacion")
        if not isinstance(operacion, dict):
            return None
        return _Parsed(
            action=_Action.PROPOSE,
            text=str(datos.get("texto") or "").strip(),
            operation=operacion,
        )
    return None


def _hallazgos(valor: object) -> tuple[str, ...]:
    if not isinstance(valor, list):
        return ()
    return tuple(str(entry).strip() for entry in valor if str(entry).strip())


def _historial(history: Sequence[ChatMessage]) -> str:
    """La conversación anterior, en texto, para que el modelo siga el hilo.

    Sólo viajan las voces del usuario y del copiloto: los resultados de las
    herramientas del turno se mandan aparte —y caducan al terminar—, y repetir
    los viejos llenaría el contexto con cifras que ya pueden no ser ciertas.
    """
    relevantes = [
        mensaje
        for mensaje in history
        if mensaje.role in (ChatRole.USER, ChatRole.COPILOT) and mensaje.text.strip()
    ][-MAX_HISTORY_MESSAGES:]
    if not relevantes:
        return "(es el primer mensaje de la conversación)"
    etiquetas = {ChatRole.USER: "usuario", ChatRole.COPILOT: "copiloto"}
    return "\n".join(
        f"{etiquetas[mensaje.role]}: {mensaje.text.strip()}" for mensaje in relevantes
    )


def _argumentos_legibles(args: Mapping[str, object]) -> str:
    """Los argumentos tal como se enseñan en la pantalla, en una línea."""
    return ", ".join(f"{clave}: {valor}" for clave, valor in args.items())
