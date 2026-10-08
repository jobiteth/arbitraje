"""Leer los saldos de una dirección de Solana: tres llamadas, y una asimetría.

### La asimetría que cambia el diseño

En EVM una cartera **no puede** enumerar sus tokens: el saldo vive en el contrato
del token y no hay lista de tenedores, así que hay que preguntar por contratos
que alguien haya nombrado antes. En Solana sí puede: cada token account es una
cuenta propia con su dueño escrito dentro, y `getTokenAccountsByOwner` las
devuelve todas. Medido el 2026-10-07 contra una dirección pública: **2820
cuentas**.

Por eso aquí no hay catálogo de entrada. Se pregunta por lo que hay y se
descubre, en vez de comprobar una lista.

### Lo medido, porque decide el resto

- De ocho nodos públicos probados, **sólo `api.mainnet-beta.solana.com` contesta
  `getTokenAccountsByOwner`**. Los demás o no hablan Solana (400/401) o devuelven
  **403** justo en ese método —`solana-rpc.publicnode.com` da el saldo nativo y
  bloquea los tokens—. Un nodo así no sirve: daría el SOL y se callaría los USDC.
- Ese nodo **raciona por ventana**, no por ráfaga: 20 peticiones seguidas pasaron
  y la siguiente dio 429. De ahí que la lectura de Solana vaya **en serie** y no
  en paralelo, y que el motor cachee: es el único nodo que hay, y gastarlo en
  repetir lo mismo deja al usuario sin saldo.
- Hay **dos programas de token** y hay que preguntar por separado. Medido: el
  clásico devuelve 2820 cuentas y el Token-2022, 325. Preguntar sólo por el
  clásico esconde los tokens del programa nuevo; son la minoría, y «la minoría»
  sigue siendo dinero del usuario.

### El símbolo es presentación; el mint es la identidad

Un mint de SPL no lleva su símbolo dentro en el estándar base: vive en una cuenta
de metadatos aparte del programa de Metaplex. Se resuelve primero contra el
catálogo del proyecto —que ya trae direcciones medidas— y lo que no esté se
identifica por su mint acortado, que es lo que de verdad identifica a un token:
dos tokens distintos pueden llamarse «USDC» y un mint sólo hay uno.

### Varias cuentas para un mismo mint

Una cartera puede tener **más de una** token account del mismo mint. Es legal y
ocurre. Si se listaran tal cual, el mismo USDC saldría dos veces con la mitad
cada uno y el total por red —que suma holdings— daría el doble. Se agrupan por
mint y se suman los enteros, que es exacto por definición al compartir escala.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import structlog

from amigocompora.domain.addresses import shorten
from amigocompora.domain.chains import chain
from amigocompora.domain.errors import SourceResponseError, SourceUnavailableError
from amigocompora.domain.models import Token
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.wallet import ChainHoldings, TokenHolding
from amigocompora.engines.catalog import token_by_address
from amigocompora.engines.evm_rpc import ChainReader

_log = structlog.get_logger(__name__)

#: El programa de tokens clásico. Casi todo el valor de Solana está aquí.
#:
#: El `noqa` es porque S105 ve una cadena larga de base58 y la toma por una
#: credencial. No lo es: es la dirección de un programa **público** de Solana, la
#: misma para todo el mundo, y está escrita aquí justamente para poder
#: preguntarle. Una credencial de verdad no aparecería en un fichero del
#: repositorio, y menos con este comentario al lado.
TOKEN_PROGRAM: Final = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105

#: El programa Token-2022, el nuevo. Medido: 325 cuentas frente a 2820 del
#: clásico en la misma dirección, así que no es un caso raro que se pueda omitir
#: para simplificar. Misma aclaración sobre el `noqa` que el de arriba.
TOKEN_2022_PROGRAM: Final = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"  # noqa: S105

#: Los dos, en el orden en que se preguntan.
TOKEN_PROGRAMS: Final = (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)

#: Lamports por SOL. Un lamport es la unidad mínima y son mil millones.
LAMPORTS_PER_SOL: Final = 1_000_000_000

#: Tope de decimales que se acepta. Un mint puede declarar hasta 255, y un valor
#: así no es un token: es una forma de que `as_decimal()` produzca cantidades que
#: ninguna interfaz puede pintar. Se rechaza el holding en vez de propagarlo, y
#: se registra para que se vea que se descartó algo.
MAX_DECIMALS: Final = 36


@dataclass(frozen=True, slots=True)
class SolanaReader:
    """Lee saldos de Solana con el mismo lector de cadena que los motores EVM.

    `ChainReader` no tiene nada de EVM: expone `call(red, método, params)` y
    construye el pool desde la tabla de nodos medidos de esa red. Solana entra
    por ahí igual que las demás, y la lista blanca de hosts del motor sale de la
    misma tabla.
    """

    reader: ChainReader

    async def holdings(self, chain_key: str, address: str) -> ChainHoldings:
        """La cartera de esa dirección en Solana, o el motivo por el que no se pudo.

        Igual que el lector EVM, **no lanza**: devuelve el fallo dentro del
        resultado, porque una cartera multi-red donde Solana falla tiene que
        seguir pintándose con las demás redes.
        """
        spec = chain(chain_key)
        try:
            nativo = await self._native(chain_key, address)
            crudas, faltantes = await self._token_accounts(chain_key, address)
        except (SourceResponseError, SourceUnavailableError) as error:
            _log.warning("wallet.solana_read_failed", chain=chain_key, reason=str(error))
            return ChainHoldings(chain=chain_key, error=str(error))
        except Exception as error:
            _log.warning("wallet.solana_read_error", chain=chain_key, reason=repr(error))
            return ChainHoldings(chain=chain_key, error=f"lectura interrumpida: {error}")

        holdings: list[TokenHolding] = [
            TokenHolding(
                token=Token(
                    symbol=spec.native_symbol,
                    decimals=spec.native_decimals,
                    chain=chain_key,
                    address=None,
                ),
                amount=TokenAmount(
                    raw=nativo, decimals=spec.native_decimals, symbol=spec.native_symbol
                ),
            )
        ]
        holdings.extend(_agrupar(chain_key, crudas))
        return ChainHoldings(chain=chain_key, holdings=tuple(holdings), missing=faltantes)

    async def _native(self, chain_key: str, address: str) -> int:
        """El saldo en lamports. `finalized` y no `confirmed`: un saldo que puede
        desaparecer en una reorganización no es un saldo que se deba enseñar."""
        salida = await self.reader.call(
            chain_key, "getBalance", [address, {"commitment": "finalized"}]
        )
        return _lamports(salida)

    async def _token_accounts(
        self, chain_key: str, address: str
    ) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
        """Las cuentas SPL de los dos programas, y cuáles no contestaron.

        En serie a propósito: el único nodo público que contesta este método
        raciona por ventana, y lanzar los dos programas en paralelo es empezar a
        gastar la ventana en la primera petición de la cartera.

        Devuelve también los programas que fallaron, y **no** una lista vacía a
        secas. La diferencia importa: si los dos fallan y se devolviera vacío sin
        más, la red se pintaría con el SOL y sin un solo token, con el mismo
        aspecto que una cartera de sólo SOL. Nombrando lo que faltó, la red
        conserva lo que sí llegó y dice qué le falta.
        """
        encontradas: list[dict[str, Any]] = []
        faltantes: list[str] = []
        for programa in TOKEN_PROGRAMS:
            cuentas, fallo = await self._one_program(chain_key, address, programa)
            encontradas.extend(cuentas)
            if fallo is not None:
                faltantes.append(fallo)
        return encontradas, tuple(faltantes)

    async def _one_program(
        self, chain_key: str, address: str, program_id: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Las cuentas de un programa, o el motivo por el que no se pudieron leer.

        Un programa que falla no invalida el otro: se pierde lo que hubiera en él
        y se dice, en vez de tirar también lo del programa que sí contestó. Un 403
        del nodo en Token-2022 no debe ocultar el USDC del programa clásico.
        """
        etiqueta = "Token-2022" if program_id == TOKEN_2022_PROGRAM else "SPL clásico"
        try:
            salida = await self.reader.call(
                chain_key,
                "getTokenAccountsByOwner",
                [
                    address,
                    {"programId": program_id},
                    {"encoding": "jsonParsed", "commitment": "finalized"},
                ],
            )
        except (SourceResponseError, SourceUnavailableError) as error:
            _log.warning(
                "wallet.solana_program_failed",
                chain=chain_key,
                program=program_id,
                reason=str(error),
            )
            return [], f"token {etiqueta}: {error}"

        if not isinstance(salida, dict):
            return [], (
                f"token {etiqueta}: «{chain_key}» devolvió {type(salida).__name__} "
                f"donde se esperaba el objeto con las cuentas"
            )
        cuentas = salida.get("value")
        if not isinstance(cuentas, list):
            return [], f"token {etiqueta}: la respuesta no trae una lista de cuentas"
        return [c for c in cuentas if isinstance(c, dict)], None

def _agrupar(chain_key: str, cuentas: list[dict[str, Any]]) -> list[TokenHolding]:
    """Suma por mint y construye los holdings, en el orden en que llegaron.

    Se agrupa porque una cartera puede tener varias cuentas del mismo mint, y
    listarlas sueltas haría que el token apareciera repetido y que la suma por red
    contase dos veces lo mismo. No es un caso teórico: 2820 cuentas medidas en una
    sola dirección, y nada impide que dos apunten al mismo mint.
    """
    crudos: dict[str, int] = {}
    decimales: dict[str, int] = {}
    orden: list[str] = []

    for cuenta in cuentas:
        leido = _leer_cuenta(cuenta, chain_key)
        if leido is None:
            continue
        mint, dec, cantidad = leido
        if dec > MAX_DECIMALS:
            # Se descarta el holding, no la lectura entera: el resto de la cartera
            # sigue siendo válido y el aviso deja rastro de qué se dejó fuera.
            _log.warning(
                "wallet.solana_decimals_out_of_range",
                chain=chain_key,
                mint=mint,
                decimals=dec,
            )
            continue
        if mint not in crudos:
            crudos[mint] = 0
            decimales[mint] = dec
            orden.append(mint)
        crudos[mint] += cantidad

    holdings: list[TokenHolding] = []
    for mint in orden:
        # El `Token` se construye una vez y su símbolo es el que se le pasa al
        # `TokenAmount`. Calcularlos por caminos distintos —catálogo por un lado,
        # mint acortado por otro— dejaría un token que no se puede sumar consigo
        # mismo, porque dos cantidades sólo se combinan si coinciden símbolo **y**
        # decimales.
        token = _token_de(chain_key, mint, decimales[mint])
        holdings.append(
            TokenHolding(
                token=token,
                amount=TokenAmount(
                    raw=crudos[mint], decimals=token.decimals, symbol=token.symbol
                ),
            )
        )
    return holdings


def _lamports(salida: Any) -> int:
    """El entero dentro de `{"context":…, "value": N}`, o el número suelto."""
    valor = salida.get("value") if isinstance(salida, dict) else salida
    if type(valor) is not int:
        raise SourceResponseError(
            f"el saldo nativo llegó como {type(valor).__name__} y no como un entero "
            f"de lamports"
        )
    return valor


def _leer_cuenta(cuenta: dict[str, Any], chain_key: str) -> tuple[str, int, int] | None:
    """`(mint, decimales, cantidad)` de una cuenta, o `None` si no es una cuenta.

    Devuelve `None` en vez de lanzar para las entradas que no son lo que se pidió:
    el nodo puede colar objetos de otra forma en la lista, y una entrada rara no
    debe invalidar las otras 2819.
    """
    datos = (cuenta.get("account") or {}).get("data")
    if not isinstance(datos, dict):
        return None
    parsed = datos.get("parsed")
    if not isinstance(parsed, dict) or parsed.get("type") != "account":
        # El filtro por programa puede devolver una entrada cuyo `parsed.type` no
        # sea «account» si el nodo cambia de forma; se ignora en vez de leerla mal.
        return None
    info = parsed.get("info")
    if not isinstance(info, dict):
        return None

    mint = info.get("mint")
    token_amount = info.get("tokenAmount")
    if not isinstance(mint, str) or not mint or not isinstance(token_amount, dict):
        return None

    dec = token_amount.get("decimals")
    crudo = token_amount.get("amount")
    if type(dec) is not int or dec < 0 or not isinstance(crudo, str):
        return None
    try:
        cantidad = int(crudo)
    except ValueError:
        _log.warning("wallet.solana_amount_not_numeric", chain=chain_key, mint=mint)
        return None
    return mint, dec, cantidad


def _token_de(chain_key: str, mint: str, decimals: int) -> Token:
    """El token del catálogo si el mint está en él; si no, uno identificado por mint."""
    conocido = token_by_address(mint, chain_key)
    if conocido is not None:
        return conocido
    return Token(symbol=shorten(mint), decimals=decimals, chain=chain_key, address=mint)
