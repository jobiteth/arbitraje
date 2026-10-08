"""Leer los saldos de una dirección EVM: la red entera en **una** llamada.

### Lo que se midió, porque es la razón de que esto sea así

Medido el 2026-10-07 contra las ocho redes configuradas:

- `0xcA11bde05977b3631167028862bE2a173976CA11` tiene contrato en todas, y su
  `aggregate3` contesta.
- `aggregate3((address,bool,bytes)[])` = `0x82ad56cb`, calculado con keccak.
- `getEthBalance(address)` = `0x4d2301cc`: **Multicall3 también devuelve el saldo
  nativo**, no sólo los de token.
- Con las dos cosas juntas, el nativo y N tokens caben en **una** `eth_call`.
  Medido: `ethereum -> nativo=5,753522 ETH, USDC=37,19` en una sola petición.

Sin esto serían N+1 viajes de ida y vuelta por red. Con ocho redes y veinte
tokens cada una son 168 llamadas; así son 8.

### Lo que este lector NO puede hacer, y hay que decirlo

**No descubre tokens.** Una dirección EVM no tiene forma de enumerar los ERC-20
que le pertenecen: no hay en el contrato ninguna lista de tenedores, y el saldo
vive en el contrato **del token**, no en el de la cartera. Enumerarlos exige un
indexador que haya recorrido los eventos `Transfer` de toda la historia de la
red — eso es lo que hace un explorador, y no se puede deducir de la cadena.

Así que se leen los tokens que se sabe que hay que mirar: el catálogo de la red,
lo que el usuario haya pegado en su lista, y lo que devuelva un indexador cuando
lo haya. Un token que nadie nombró no aparece, y eso no es un fallo del lector:
es cómo funciona EVM. La interfaz tiene que decir «añade el contrato para verlo»
en vez de dar a entender que la lista está completa.

### Falla por token, no por red

`allowFailure` va en `true` para cada llamada: un token que revierta —uno que se
quemó, uno congelado— devuelve cero y **no** tumba la red entera. Un solo
contrato hostil no puede dejar al usuario sin ver los otros diecinueve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import structlog

# Se importa del submódulo y no de la raíz del paquete, que es lo que hace el
# resto del proyecto: `eth_abi` y `eth_utils` reexportan por conveniencia y sus
# tipos no lo declaran, así que la forma corta no pasa el comprobador de tipos.
from eth_abi.abi import decode, encode
from eth_utils.crypto import keccak

from amigocompora.domain.errors import SourceResponseError, SourceUnavailableError
from amigocompora.domain.models import Token
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.wallet import ChainHoldings, TokenHolding
from amigocompora.engines.evm_rpc import ChainReader

_log = structlog.get_logger(__name__)

#: El contrato que junta las lecturas. La misma dirección en todas las redes
#: —comprobado, no supuesto: se verificó que hay código en las ocho—.
MULTICALL3: Final = "0xcA11bde05977b3631167028862bE2a173976CA11"

#: Los selectores, calculados con keccak en vez de copiados de un blog. Un
#: selector mal escrito no da error de red: llama a otra función, o a ninguna.
SELECTOR_AGGREGATE3: Final = "0x" + keccak(text="aggregate3((address,bool,bytes)[])")[:4].hex()
SELECTOR_BALANCE_OF: Final = "0x" + keccak(text="balanceOf(address)")[:4].hex()
SELECTOR_GET_ETH_BALANCE: Final = "0x" + keccak(text="getEthBalance(address)")[:4].hex()

#: Cuántos tokens como mucho en una sola agregación. No es un límite del
#: contrato: es que un `eth_call` con mil llamadas dentro devuelve una respuesta
#: que algunos nodos rechazan por tamaño, y partirlo en trozos es más predecible
#: que confiar en que el nodo la acepte.
MAX_CALLS_PER_BATCH: Final = 120


@dataclass(frozen=True, slots=True)
class EvmReader:
    """Lee saldos EVM por Multicall3, sobre el lector de cadena de los motores.

    Se le pasa el `ChainReader` que ya usan los motores directos en vez de montar
    uno propio: es el mismo failover, la misma salud acumulada por nodo y el
    mismo cliente con la lista blanca del manifiesto. Un segundo camino daría dos
    ideas distintas de «qué nodos funcionan» dentro del mismo proceso.
    """

    reader: ChainReader

    async def holdings(
        self, chain_key: str, address: str, tokens: tuple[Token, ...]
    ) -> ChainHoldings:
        """Lo que hay en esa red, o el motivo por el que no se pudo leer.

        No lanza: **devuelve** el fallo dentro del resultado. Una cartera de ocho
        redes donde una falla tiene que seguir pintándose, y quien suma necesita
        saber cuál faltó — que es justo lo que se pierde si esto lanza.
        """
        try:
            crudos = await self._aggregate(chain_key, address, tokens)
        except (SourceResponseError, SourceUnavailableError) as error:
            _log.warning("wallet.evm_read_failed", chain=chain_key, reason=str(error))
            return ChainHoldings(chain=chain_key, error=str(error))
        except Exception as error:
            _log.warning("wallet.evm_read_error", chain=chain_key, reason=repr(error))
            return ChainHoldings(chain=chain_key, error=f"lectura interrumpida: {error}")

        leidos: list[TokenHolding] = []
        for token, valor in zip(tokens, crudos, strict=True):
            leidos.append(
                TokenHolding(
                    token=token,
                    amount=TokenAmount(raw=valor, decimals=token.decimals, symbol=token.symbol),
                )
            )
        return ChainHoldings(chain=chain_key, holdings=tuple(leidos))

    async def _aggregate(
        self, chain_key: str, address: str, tokens: tuple[Token, ...]
    ) -> list[int]:
        """Los N+1 saldos —nativo y tokens—, en las llamadas que hagan falta.

        La lista de llamadas se construye **una por token, en el mismo orden**, y
        no metiendo el nativo aparte y saltándose su hueco. Esa segunda forma
        funciona mientras el nativo vaya el primero, y el día que alguien pase la
        lista en otro orden el primer token recibiría el saldo nativo: un número
        plausible en la casilla equivocada, que es el peor fallo posible aquí
        porque no se nota. Emparejando por posición desde el principio, el orden
        de entrada es el orden de salida y no hay convención que recordar.
        """
        argumento = bytes.fromhex(address[2:].lower().rjust(64, "0"))
        sel_nativo = bytes.fromhex(SELECTOR_GET_ETH_BALANCE[2:]) + argumento
        sel_token = bytes.fromhex(SELECTOR_BALANCE_OF[2:]) + argumento

        objetivo: list[tuple[str, bytes]] = [
            # El nativo se pide a **Multicall3** por `getEthBalance`, y no a la
            # dirección de la cartera: una cartera no tiene código, así que una
            # llamada directa a ella devolvería vacío. Es lo que permite que el
            # nativo viaje en la misma petición que los tokens.
            (MULTICALL3, sel_nativo) if token.address is None else (token.address, sel_token)
            for token in tokens
        ]

        saldos: list[int] = []
        for trozo in _trozos(objetivo, MAX_CALLS_PER_BATCH):
            saldos.extend(await self._one_call(chain_key, trozo))
        return saldos

    async def _one_call(self, chain_key: str, objetivo: list[tuple[str, bytes]]) -> list[int]:
        tuplas = [(destino, True, llamada) for destino, llamada in objetivo]
        datos = "0x" + (
            bytes.fromhex(SELECTOR_AGGREGATE3[2:]) + encode(["(address,bool,bytes)[]"], [tuplas])
        ).hex()
        salida = await self.reader.eth_call(chain_key, MULTICALL3, datos)
        return _decode_balances(salida, len(objetivo))


def _trozos[T](objetivo: list[T], tamano: int) -> list[list[T]]:
    return [objetivo[i : i + tamano] for i in range(0, len(objetivo), tamano)]


def _decode_balances(salida: str, esperados: int) -> list[int]:
    """Los enteros de un `aggregate3`, en orden, con los fallidos a cero.

    Un `success = false` se traduce a **cero** y no a un error: es una llamada
    que revirtió —un token quemado, uno congelado— y el resto de la red sigue
    siendo válido. Que un contrato hostil no pueda dejar al usuario sin ver sus
    otros diecinueve saldos es la razón de que `allowFailure` vaya en `true`.
    """
    cuerpo = salida[2:] if salida[:2].lower() == "0x" else salida
    if len(cuerpo) % 2 != 0:
        raise SourceResponseError("la lectura agregada no es hexadecimal de longitud par")
    try:
        (resultados,) = decode(["(bool,bytes)[]"], bytes.fromhex(cuerpo))
    except Exception as error:
        # Se traduce porque un fallo de decodificación con la traza de eth_abi
        # delante no le dice nada a quien lo lea en la interfaz.
        raise SourceResponseError(
            f"no se pudo interpretar la lectura agregada de saldos: {error}"
        ) from error

    if len(resultados) != esperados:
        raise SourceResponseError(
            f"se pidieron {esperados} saldos y llegaron {len(resultados)}: la "
            f"respuesta no se corresponde con la petición"
        )

    saldos: list[int] = []
    for exito, devuelto in resultados:
        if not exito or len(devuelto) < 32:
            saldos.append(0)
            continue
        # `>u256` y no un `int(hex, 16)`: el entero es una palabra de 32 bytes sin
        # signo, y esta es la conversión que lo dice sin depender de que el
        # hexadecimal llegue con o sin prefijo.
        saldos.append(int.from_bytes(devuelto[:32], "big"))
    return saldos
