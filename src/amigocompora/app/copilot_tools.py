"""Las herramientas de sólo lectura que el copiloto puede usar.

El contrato original de la IA en este proyecto era «el modelo sólo ve lo que se
le pasa»: no sale a buscar nada. Eso protegía de las cifras inventadas, pero
dejaba al usuario tecleando a mano lo que la aplicación ya sabe — y una pregunta
tan natural como «¿esta dirección tiene saldo?» no se podía contestar.

Aquí está el cambio, y está acotado: el modelo **pide** datos, los datos los
trae la aplicación por los mismos casos de uso que usan las pantallas, y lo que
vuelve al modelo es el resultado ya calculado. El modelo no tiene red, no
inventa cifras y no puede pedir nada que no esté en esta lista. Las cuatro
herramientas son de sólo lectura: ninguna firma, ninguna emite, ninguna mueve
un céntimo. Proponer una operación es otra cosa, y vive en `copilot.py`.

### Por qué el resultado es texto en español y no JSON

Lo que se le devuelve al modelo se escribe para que lo **lea**: importes ya
formateados, comisiones dichas como se dicen («0,05 %» o «no la desglosa») y
errores con su motivo. Un JSON de estructuras internas obligaría al modelo a
saber interpretar los campos del proyecto —y a acertar— para poder responder lo
mismo, y cada campo nuevo rompería los prompts sin que nada lo dijera.

### Por qué las herramientas fallan con `CopilotToolError`

Un fallo no se convierte en una respuesta vacía: se le devuelve al modelo **con
el motivo**, porque un «no se pudo leer la cartera porque no hay motor de
cartera activo» es exactamente lo que la persona necesita oír, y el modelo sólo
puede decirlo si lo recibe. Un error genérico lo llevaría a inventarse la causa.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from amigocompora.app.usecases.compare_bridges import CompareBridges
from amigocompora.app.usecases.compare_prices import ComparePrices
from amigocompora.app.usecases.wallet import ReadWallet
from amigocompora.domain.chains import CHAINS
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.models import (
    BridgeComparison,
    BridgeQuote,
    BridgeRequest,
    PriceComparison,
    Quote,
    Token,
    TradingPair,
)
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.wallet import WalletKind, WalletProfile
from amigocompora.engines.catalog import token_by_symbol
from amigocompora.engines.token_lookup import TokenLookup
from amigocompora.engines.token_store import UserTokenStore


class CopilotToolError(Exception):
    """La herramienta no pudo hacer su trabajo, y el motivo se le dice al modelo."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Una herramienta tal como se le describe al modelo: nombre, args y qué da."""

    name: str
    args: str
    description: str


#: El catálogo de herramientas. Es la **única** fuente: el manual del prompt y el
#: despachador salen de aquí, así que una herramienta no puede existir para el
#: modelo sin existir de verdad, ni al revés.
SPECS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="saldos",
        args='{"direccion": "0x…", "redes": ["polygon"] (opcional)}',
        description=(
            "los saldos de una dirección, red por red. Sin «redes» se leen todas "
            "las que la aplicación sepa leer para esa familia. Devuelve el saldo "
            "de cada token con fondos y los motivos de las redes que fallaron."
        ),
    ),
    ToolSpec(
        name="cotizacion",
        args='{"red": "polygon", "entrada": "USDC", "salida": "POL", "importe": "50"}',
        description=(
            "cotiza vender un importe de un token por otro en una red, contra "
            "todos los motores activos, ordenado de mejor a peor. «entrada» y "
            "«salida» aceptan símbolo del catálogo o dirección de contrato."
        ),
    ),
    ToolSpec(
        name="token",
        args='{"red": "base", "direccion": "0x…"}',
        description=(
            "símbolo y decimales de un token, leídos de su propio contrato. Es la "
            "forma de resolver una dirección de token que no esté en el catálogo."
        ),
    ),
    ToolSpec(
        name="puente",
        args='{"origen": "base", "destino": "polygon", "token": "USDC", "importe": "20"}',
        description=(
            "las rutas para mover un token de una red a otra: cuánto llega, "
            "mínimo garantizado, comisión y duración, por cada motor de puentes."
        ),
    ),
)

#: Tope de rutas que se le enseñan al modelo por herramienta. La mejor y unas
#: pocas más bastan para responder; volcar una tabla de veinte filas sólo gasta
#: contexto y hace más probable que el modelo cite la equivocada.
MAX_FILAS: Final = 5


@dataclass(frozen=True, slots=True)
class CopilotTools:
    """Las herramientas, con los casos de uso de la aplicación detrás.

    Recibe los casos de uso y no el contenedor: es la misma regla que el resto de
    `app` —quien construye cablea— y además evita el ciclo obvio, porque el
    contenedor construye a este objeto.
    """

    read_wallet: ReadWallet
    compare_prices: ComparePrices
    compare_bridges: CompareBridges
    token_lookup: TokenLookup
    #: Los tokens que el usuario añadió a mano. Sin esto, un token agregado por
    #: su dirección no se encontraría por símbolo, que es justo como lo va a
    #: escribir quien pregunta por él.
    tokens: UserTokenStore | None = None
    clock: Clock | None = None

    async def run(self, name: str, args: Mapping[str, object]) -> str:
        """Ejecuta la herramienta pedida. Sólo estos cuatro nombres existen."""
        limpio = name.strip().lower()
        if limpio == "saldos":
            return await self.balances(args)
        if limpio == "cotizacion":
            return await self.quote(args)
        if limpio == "token":
            return await self.token_info(args)
        if limpio == "puente":
            return await self.bridge(args)
        raise CopilotToolError(
            f"«{name}» no es una herramienta disponible. Las que hay son: "
            f"{', '.join(spec.name for spec in SPECS)}."
        )

    def manual(self) -> str:
        """El manual que viaja en el prompt: qué puede pedir y cómo."""
        lineas = ["Herramientas disponibles (sólo lectura, ninguna ejecuta nada):"]
        for spec in SPECS:
            lineas.append(f"- {spec.name}: {spec.description} Argumentos: {spec.args}")
        return "\n".join(lineas)

    # ------------------------------------------------------------------ #
    # saldos
    # ------------------------------------------------------------------ #
    async def balances(self, args: Mapping[str, object]) -> str:
        direccion = _texto(args, "direccion")
        redes = _lista(args, "redes")
        for red in redes:
            if red not in CHAINS:
                raise CopilotToolError(
                    f"«{red}» no es una red conocida. Las que hay: {', '.join(sorted(CHAINS))}."
                )
        # La familia se deduce de la primera red pedida, y sin redes se supone
        # EVM —que es el caso normal—: es lo único que decide cómo se valida la
        # dirección, y una dirección de Solana en la familia EVM se rechaza con
        # un mensaje que lo dice, no con una lectura vacía.
        familia = WalletKind.of_chain(redes[0] if redes else "ethereum")
        try:
            perfil = WalletProfile(
                wallet_id="consulta-copiloto",
                label="Consulta del copiloto",
                kind=familia,
                address=direccion,
            )
        except Exception as error:
            # Una dirección con el formato de otra familia se rechaza **aquí**,
            # con el mensaje de la validación: dejarla pasar daría una lectura de
            # red vacía que parecería «esta cartera no tiene nada».
            raise CopilotToolError(str(error)) from error
        foto = await self.read_wallet(perfil, chains=tuple(redes) if redes else None)

        lineas = [f"Saldos de {perfil.address} (leído {_momento(self._now())}):"]
        if not foto.chains:
            lineas.append(
                "- ninguna red legible: no hay motor de cartera activo, o no cubre "
                "ninguna de las redes pedidas."
            )
            return "\n".join(lineas)
        for cadena in foto.chains:
            if cadena.error:
                lineas.append(f"- {cadena.chain}: no se pudo leer: {cadena.error}")
                continue
            with_funds = [h for h in cadena.holdings if h.amount.is_positive]
            if not with_funds:
                lineas.append(f"- {cadena.chain}: sin saldo en los tokens mirados.")
                continue
            detalle = ", ".join(_cantidad(h.amount) for h in with_funds[:MAX_FILAS])
            sobran = len(with_funds) - MAX_FILAS
            extra = f" (y {sobran} tokens más)" if sobran > 0 else ""
            lineas.append(f"- {cadena.chain}: {detalle}{extra}")
            if cadena.missing:
                lineas.append(
                    f"  sin dato en {cadena.chain}: {', '.join(cadena.missing)}"
                )
        return "\n".join(lineas)

    # ------------------------------------------------------------------ #
    # cotizacion
    # ------------------------------------------------------------------ #
    async def quote(self, args: Mapping[str, object]) -> str:
        comparacion = await self.quote_comparison(args)
        lineas = [
            f"Cotización de {_cantidad(comparacion.amount_in)} → "
            f"{comparacion.pair.quote.symbol} en {comparacion.pair.chain} "
            f"({len(comparacion.quotes)} rutas, mejor primero):"
        ]
        lineas.extend(
            _fila_cotizacion(indice, cita)
            for indice, cita in enumerate(comparacion.ranked[:MAX_FILAS], 1)
        )
        if len(comparacion.quotes) > MAX_FILAS:
            lineas.append(f"…y {len(comparacion.quotes) - MAX_FILAS} rutas más.")
        if comparacion.failed_engines:
            lineas.append(
                f"Motores que no respondieron: {', '.join(comparacion.failed_engines)} "
                f"(la comparación puede estar incompleta)."
            )
        if comparacion.has_unknown_fees:
            lineas.append("Hay rutas que no desglosan su comisión: no se puede restar.")
        return "\n".join(lineas)

    async def quote_comparison(self, args: Mapping[str, object]) -> PriceComparison:
        """La comparación de verdad, no su texto.

        Existe separada de `quote` porque quien propone una operación necesita la
        mejor `Quote` **entera** —con su `engine_id` y su par— para poder
        construir el swap; quedarse con el texto obligaría a volver a cotizar
        después, y la cifra que se enseñó y la que se ejecutaría podrían no ser
        la misma.
        """
        red = _red(args, "red")
        entrada = await self._token(red, _texto(args, "entrada"))
        salida = await self._token(red, _texto(args, "salida"))
        importe = _importe(args, entrada)
        return await self.compare_prices(TradingPair(base=entrada, quote=salida), importe)

    # ------------------------------------------------------------------ #
    # token
    # ------------------------------------------------------------------ #
    async def token_info(self, args: Mapping[str, object]) -> str:
        red = _red(args, "red")
        direccion = _texto(args, "direccion")
        token = await self.token_lookup.by_address(red, direccion)
        return (
            f"{token.symbol} en {red}: {token.decimals} decimales, contrato "
            f"{token.address}."
        )

    # ------------------------------------------------------------------ #
    # puente
    # ------------------------------------------------------------------ #
    async def bridge(self, args: Mapping[str, object]) -> str:
        comparacion = await self.bridge_comparison(args)
        lineas = [
            f"Puente de {_cantidad(comparacion.request.amount_in)} de "
            f"{comparacion.request.origin.chain} a "
            f"{comparacion.request.destination.chain} "
            f"({len(comparacion.routes)} rutas, mejor primero):"
        ]
        lineas.extend(
            _fila_puente(indice, ruta.quote)
            for indice, ruta in enumerate(comparacion.routes[:MAX_FILAS], 1)
        )
        if len(comparacion.routes) > MAX_FILAS:
            lineas.append(f"…y {len(comparacion.routes) - MAX_FILAS} rutas más.")
        if comparacion.failed_engines:
            lineas.append(
                f"Motores que no respondieron: {', '.join(comparacion.failed_engines)}."
            )
        if comparacion.has_unknown_fees:
            lineas.append("Hay rutas que no desglosan su comisión: no se puede restar.")
        return "\n".join(lineas)

    async def bridge_comparison(self, args: Mapping[str, object]) -> BridgeComparison:
        """Las rutas de verdad, para poder ofrecer la mejor como propuesta."""
        origen = _red(args, "origen")
        destino = _red(args, "destino")
        if origen == destino:
            raise CopilotToolError(
                f"«{origen}» y «{destino}» son la misma red: un puente cruza dos "
                f"redes, y para cambiar dos tokens de la misma red se usa un swap."
            )
        token_origen = await self._token(origen, _texto(args, "token"))
        # El destino puede ser otro token —un puente puede entregar USDC donde
        # salió USDT—, pero lo normal es el mismo: si no se nombra, se usa el del
        # origen resuelto en la red de destino.
        nombre_destino = _texto(args, "token_destino", obligatorio=False)
        token_destino = (
            await self._token(destino, nombre_destino)
            if nombre_destino
            else await self._token(destino, token_origen.symbol)
        )
        importe = _importe(args, token_origen)
        return await self.compare_bridges(
            BridgeRequest(origin=token_origen, destination=token_destino, amount_in=importe)
        )

    # ------------------------------------------------------------------ #
    # apoyo
    # ------------------------------------------------------------------ #
    def _now(self) -> datetime:
        return (self.clock or SystemClock()).now()

    async def _token(self, chain_key: str, texto: str) -> Token:
        """Un token por símbolo del catálogo, por los añadidos, o por dirección.

        El orden no es casual: primero lo que no cuesta red —catálogo y tokens
        añadidos—, y sólo si no está, se lee el contrato. Una dirección escrita
        como símbolo no existe, así que ahí se va directo a la cadena.
        """
        limpio = texto.strip()
        if not limpio:
            raise CopilotToolError("hace falta un token (símbolo o dirección).")
        if limpio.startswith("0x") or _parece_base58(limpio):
            return await self.token_lookup.by_address(chain_key, limpio)
        for candidato in (self.tokens or ()):
            if candidato.chain == chain_key and candidato.symbol.lower() == limpio.lower():
                return candidato
        encontrado = token_by_symbol(limpio, chain_key)
        if encontrado is None:
            raise CopilotToolError(
                f"«{limpio}» no está en el catálogo de {chain_key}. Si tienes su "
                f"dirección de contrato, pásala y se resuelve leyéndola."
            )
        return encontrado


# --------------------------------------------------------------------------- #
# Presentación al modelo
# --------------------------------------------------------------------------- #
def _fila_cotizacion(indice: int, cita: Quote) -> str:
    partes = [f"{indice}. {cita.venue.name}: {_cantidad(cita.amount_out)}"]
    partes.append(
        f"comisión {cita.fee_bps.value} bps" if cita.fee_bps else "comisión sin desglosar"
    )
    if cita.price_impact_bps is not None:
        partes.append(f"impacto {cita.price_impact_bps.value} bps")
    if not cita.is_exact:
        partes.append("cifra estimada")
    partes.append(f"construye: {cita.engine_id}")
    return " · ".join(partes)


def _fila_puente(indice: int, ruta: BridgeQuote) -> str:
    partes = [
        f"{indice}. {ruta.provider}: entrega {_cantidad(ruta.amount_out)} "
        f"(mínimo garantizado {_cantidad(ruta.amount_out_min)})"
    ]
    partes.append(
        f"comisión {ruta.fee_bps.value} bps" if ruta.fee_bps else "comisión sin desglosar"
    )
    partes.append(f"duración {ruta.duration_label}")
    partes.append(f"construye: {ruta.engine_id}")
    return " · ".join(partes)


def _cantidad(amount: TokenAmount) -> str:
    """Un importe para leerlo: sin notación científica y sin ceros de relleno."""
    texto = format(amount.as_decimal(), "f")
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return f"{texto} {amount.symbol}"


def _momento(momento: datetime) -> str:
    return momento.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- #
# Lectura de argumentos
# --------------------------------------------------------------------------- #
def _texto(
    args: Mapping[str, object], clave: str, *, obligatorio: bool = True
) -> str:
    valor = args.get(clave)
    if isinstance(valor, str) and valor.strip():
        return valor.strip()
    if obligatorio:
        raise CopilotToolError(f"falta el argumento «{clave}».")
    return ""


def _lista(args: Mapping[str, object], clave: str) -> tuple[str, ...]:
    valor = args.get(clave)
    if valor is None:
        return ()
    if isinstance(valor, str):
        return tuple(parte.strip() for parte in valor.split(",") if parte.strip())
    if isinstance(valor, Sequence):
        return tuple(str(parte).strip() for parte in valor if str(parte).strip())
    raise CopilotToolError(f"«{clave}» tiene que ser una lista de redes.")


def _red(args: Mapping[str, object], clave: str) -> str:
    red = _texto(args, clave).lower()
    if red not in CHAINS:
        raise CopilotToolError(
            f"«{red}» no es una red conocida. Las que hay: {', '.join(sorted(CHAINS))}."
        )
    return red


def _importe(args: Mapping[str, object], token: Token) -> TokenAmount:
    """El importe en unidades humanas, con la precisión del token.

    No se redondea: un importe que no cabe en los decimales del token se
    rechaza diciendo cuáles son. Redondear en silencio convertiría un «vende
    0,1234567891» en una operación por otra cifra sin que nadie lo supiera —la
    misma regla que aplica `TokenAmount.from_decimal` en todo el proyecto—, y el
    modelo puede volver a pedirlo con la precisión correcta, que es una vuelta
    y no un error.
    """
    crudo = _texto(args, "importe").replace(",", ".")
    try:
        valor = Decimal(crudo)
    except InvalidOperation as error:
        raise CopilotToolError(f"«{crudo}» no es un importe.") from error
    if valor <= 0:
        raise CopilotToolError(f"el importe tiene que ser positivo, llegó {crudo}.")
    try:
        return TokenAmount.from_decimal(valor, token.decimals, token.symbol)
    except Exception as error:
        raise CopilotToolError(
            f"{crudo} {token.symbol} no es representable con los {token.decimals} "
            f"decimales de {token.symbol}: pide el importe con esa precisión o "
            f"menos."
        ) from error


def _parece_base58(texto: str) -> bool:
    """Si el texto parece una dirección de Solana —32-44 caracteres base58—.

    Se comprueba aquí para decidir si hay que resolverlo por contrato, no para
    validarlo: la validación la hace `TokenLookup`, que es quien sabe de redes.
    """
    if not 32 <= len(texto) <= 44:
        return False
    alfabeto = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return set(texto) <= alfabeto
