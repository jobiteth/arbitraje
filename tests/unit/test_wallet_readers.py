"""Los dos lectores de saldos, contra redes de mentira que saben la verdad.

Lo que se fija aquí no es «devuelve números», sino los cuatro sitios donde estos
lectores **pueden mentir sin dar error**, que es la única clase de fallo que
importa en una cartera:

1. **Que el saldo de un token no acabe en la casilla de otro.** En EVM los N+1
   saldos viajan en una sola `aggregate3` y se emparejan **por posición**. Si ese
   emparejamiento se desordenara, los números seguirían siendo plausibles —todos
   son enteros positivos— y el error no se vería nunca. Por eso el nodo de
   mentira no responde «una lista»: responde mirando **a qué contrato y con qué
   selector** se preguntó, y la prueba compara cada holding con el saldo que ese
   contrato tenía apuntado.
2. **Que una llamada que revierte valga cero y no tumbe la red.** Es lo que
   compra `allowFailure`, y sin ello un token quemado dejaría al usuario sin ver
   los otros diecinueve.
3. **Que una respuesta que no cuadra con la petición se detecte.** Un nodo que
   devuelve tres resultados para cuatro preguntas está diciendo algo que no se
   pidió; aceptarlo es asignar saldos a ciegas.
4. **Que Solana no pierda el programa de tokens que sí contestó** cuando el otro
   falla, y que al perderse uno se **diga** en vez de devolver una lista corta
   con el mismo aspecto que una cartera que de verdad no tiene más tokens.

Sólo hay dobles: ni una petición de red. Las lecturas contra las redes de verdad
se hicieron aparte, en los guiones de medida.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from eth_abi.abi import decode, encode

from amigocompora.domain.errors import SourceResponseError
from amigocompora.domain.models import Token
from amigocompora.domain.wallet import ChainHoldings
from amigocompora.engines.wallet import solana as sol
from amigocompora.engines.wallet.evm import (
    MAX_CALLS_PER_BATCH,
    SELECTOR_BALANCE_OF,
    SELECTOR_GET_ETH_BALANCE,
    EvmReader,
)

#: Una dirección EVM cualquiera para el dueño de la cartera.
DUEÑO = "0x2c887da24C6E6c939B0Fe3adaE50814b938B44f9"


def _token(simbolo: str, direccion: str, decimales: int = 18) -> Token:
    return Token(symbol=simbolo, decimals=decimales, chain="base", address=direccion)


def _nativo() -> Token:
    return Token(symbol="ETH", decimals=18, chain="base", address=None)


def _respuesta(saldos: Sequence[int | None]) -> str:
    """Un `aggregate3` de mentira. `None` es una llamada que revirtió."""
    tuplas = [
        (False, b"") if saldo is None else (True, saldo.to_bytes(32, "big")) for saldo in saldos
    ]
    return "0x" + encode(["(bool,bytes)[]"], [tuplas]).hex()


class _NodoEvm:
    """Un nodo de mentira que sabe el saldo de cada contrato **y del nativo**.

    Despacha por contrato más selector, no por orden de llegada: es lo que
    permite distinguir «el lector preguntó lo correcto» de «el lector recibió una
    lista y la repartió como pudo». Con un doble que devolviera los saldos en
    orden, un desorden en el emparejamiento pasaría la prueba.
    """

    def __init__(
        self,
        *,
        saldos: Mapping[str, int] | None = None,
        nativo: int = 0,
        revierten: Sequence[str] = (),
        respuesta_cruda: str | None = None,
    ) -> None:
        self.saldos = {k.lower(): v for k, v in (saldos or {}).items()}
        self.nativo = nativo
        self.revierten = {d.lower() for d in revierten}
        self.respuesta_cruda = respuesta_cruda
        #: Una entrada por `eth_call`, con lo que se preguntó. Es lo que permite
        #: afirmar «una sola llamada por red», que es la promesa del diseño.
        self.llamadas: list[tuple[str, str, str]] = []
        #: Cuántas sub-llamadas llevaba cada `eth_call`: para probar el troceado.
        self.tamanos: list[int] = []

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def eth_call(self, chain_key: str, to: str, data: str, *, block: str = "latest") -> str:
        del block
        self.llamadas.append((chain_key, to, data))
        if self.respuesta_cruda is not None:
            return self.respuesta_cruda

        (peticiones,) = decode(["(address,bool,bytes)[]"], bytes.fromhex(data[10:]))
        self.tamanos.append(len(peticiones))

        salidas: list[int | None] = []
        for destino, _permitir_fallo, llamada in peticiones:
            salidas.append(self._contestar(destino, llamada))
        return _respuesta(salidas)

    def _contestar(self, destino: str, llamada: bytes) -> int | None:
        contrato = destino.lower()
        selector = "0x" + llamada[:4].hex()
        if selector == SELECTOR_GET_ETH_BALANCE:
            # El nativo se pide a Multicall3, no a la cartera, que no tiene
            # código. Si llegara una petición así a otro contrato, es un fallo.
            assert contrato == "0xca11bde05977b3631167028862be2a173976ca11", (
                f"el saldo nativo se pidió a {destino} y no a Multicall3"
            )
            return self.nativo
        assert selector == SELECTOR_BALANCE_OF, f"selector inesperado {selector}"
        if contrato in self.revierten:
            return None
        return self.saldos.get(contrato, 0)


def _lector(nodo: _NodoEvm) -> EvmReader:
    return EvmReader(reader=nodo)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# EVM: una llamada, y el orden es el de entrada
# --------------------------------------------------------------------------- #
async def test_el_nativo_y_todos_los_tokens_caben_en_una_sola_llamada() -> None:
    """La promesa entera del diseño: N+1 saldos, **un** viaje de ida y vuelta."""
    tokens = (
        _nativo(),
        _token("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
        _token("WETH", "0x4200000000000000000000000000000000000006"),
    )
    nodo = _NodoEvm(
        nativo=7,
        saldos={
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 37_192_124,
            "0x4200000000000000000000000000000000000006": 1_466_014,
        },
    )

    resultado = await _lector(nodo).holdings("base", DUEÑO, tokens)

    assert not resultado.failed
    assert len(nodo.llamadas) == 1, "se preguntó más de una vez por la misma red"
    assert len(nodo.tamanos) == 1
    assert nodo.tamanos[0] == 3, "no viajaron las tres lecturas en la agregación"
    assert [h.amount.raw for h in resultado.holdings] == [7, 37_192_124, 1_466_014]


async def test_cada_saldo_llega_a_su_token_y_no_al_de_al_lado() -> None:
    """El fallo silencioso: un emparejamiento desordenado da números plausibles.

    Se le dan al nodo saldos **distinguibles** por contrato, y se comprueba cada
    holding contra el suyo. Si el lector repartiera por orden de llegada en vez
    de por posición, los tres números seguirían siendo enteros razonables y sólo
    esta comparación lo notaría.
    """
    tokens = (
        _nativo(),
        _token("ALFA", "0x" + "aa" * 20, 6),
        _token("BETA", "0x" + "bb" * 20, 8),
        _token("GAMMA", "0x" + "cc" * 20, 18),
    )
    nodo = _NodoEvm(
        nativo=11,
        saldos={"0x" + "aa" * 20: 222, "0x" + "bb" * 20: 333, "0x" + "cc" * 20: 444},
    )

    resultado = await _lector(nodo).holdings("base", DUEÑO, tokens)

    crudo = {h.token.symbol: h.amount.raw for h in resultado.holdings}
    assert crudo == {"ETH": 11, "ALFA": 222, "BETA": 333, "GAMMA": 444}
    # Y la escala, que un decimal mal puesto convierte 0,000222 en 222.
    escala = {h.token.symbol: h.amount.decimals for h in resultado.holdings}
    assert escala == {"ETH": 18, "ALFA": 6, "BETA": 8, "GAMMA": 18}


async def test_el_orden_de_entrada_es_el_de_salida() -> None:
    """No hay convención que recordar: sale en el mismo orden en que se pidió."""
    tokens = (_token("Z", "0x" + "11" * 20), _nativo(), _token("A", "0x" + "22" * 20))
    nodo = _NodoEvm(nativo=5, saldos={"0x" + "11" * 20: 1, "0x" + "22" * 20: 2})

    resultado = await _lector(nodo).holdings("base", DUEÑO, tokens)

    assert [h.token.symbol for h in resultado.holdings] == ["Z", "ETH", "A"]


async def test_una_llamada_que_revierte_vale_cero_y_no_tumba_la_red() -> None:
    """`allowFailure`: un token quemado no puede esconder los otros diecinueve."""
    tokens = (
        _nativo(),
        _token("QUEMADO", "0x" + "de" * 20),
        _token("VIVO", "0x" + "ab" * 20),
    )
    nodo = _NodoEvm(nativo=9, saldos={"0x" + "ab" * 20: 100}, revierten=["0x" + "de" * 20])

    resultado = await _lector(nodo).holdings("base", DUEÑO, tokens)

    assert not resultado.failed
    assert [h.amount.raw for h in resultado.holdings] == [9, 0, 100]


async def test_una_respuesta_que_no_cuadra_no_se_reparte_a_ciegas() -> None:
    """Tres resultados para cuatro preguntas: no se puede saber cuál falta."""
    nodo = _NodoEvm(respuesta_cruda=_respuesta([1, 2, 3]))

    resultado = await _lector(nodo).holdings(
        "base", DUEÑO, (_nativo(), _token("A", "0x" + "aa" * 20), _token("B", "0x" + "bb" * 20),
                        _token("C", "0x" + "cc" * 20))
    )

    assert resultado.failed
    assert resultado.error is not None
    assert "3" in resultado.error
    assert "4" in resultado.error


async def test_una_red_que_falla_no_lanza_devuelve_el_motivo() -> None:
    """Sin esto, una red caída se llevaría por delante las otras siete."""
    nodo = _NodoEvm()
    nodo.respuesta_cruda = None

    class _Explota(_NodoEvm):
        async def eth_call(
            self, chain_key: str, to: str, data: str, *, block: str = "latest"
        ) -> str:
            del chain_key, to, data, block
            raise SourceResponseError("el nodo contestó con un error JSON-RPC")

    resultado = await _lector(_Explota()).holdings("base", DUEÑO, (_nativo(),))

    assert resultado.failed
    assert resultado.error is not None
    assert "JSON-RPC" in resultado.error
    assert resultado.holdings == ()


async def test_un_fallo_inesperado_tampoco_lanza() -> None:
    """`holdings` promete no lanzar nunca: la cartera tiene que seguir pintándose."""

    class _Raro(_NodoEvm):
        async def eth_call(
            self, chain_key: str, to: str, data: str, *, block: str = "latest"
        ) -> str:
            del chain_key, to, data, block
            raise ValueError("algo que nadie previó")

    resultado = await _lector(_Raro()).holdings("base", DUEÑO, (_nativo(),))

    assert resultado.failed
    assert resultado.error is not None
    assert "interrumpida" in resultado.error


async def test_una_agregacion_enorme_se_trocea() -> None:
    """Un `eth_call` con mil llamadas dentro lo rechazan algunos nodos."""
    total = MAX_CALLS_PER_BATCH + 1
    tokens = tuple(
        _token(f"T{i}", "0x" + f"{i:040x}") for i in range(total)
    )
    direcciones = {"0x" + f"{i:040x}": i for i in range(total)}
    nodo = _NodoEvm(saldos=direcciones)

    resultado = await _lector(nodo).holdings("base", DUEÑO, tokens)

    assert len(nodo.llamadas) == 2
    assert nodo.tamanos == [MAX_CALLS_PER_BATCH, 1]
    assert [h.amount.raw for h in resultado.holdings] == list(range(total))


# --------------------------------------------------------------------------- #
# Solana: dos programas, y ninguno puede esconder al otro
# --------------------------------------------------------------------------- #
def _cuenta(mint: str, cantidad: int, decimales: int = 6) -> dict[str, Any]:
    return {
        "account": {
            "data": {
                "parsed": {
                    "type": "account",
                    "info": {
                        "mint": mint,
                        "tokenAmount": {"decimals": decimales, "amount": str(cantidad)},
                    },
                }
            }
        }
    }


class _NodoSolana:
    """Un nodo de mentira con el saldo nativo y las cuentas de cada programa."""

    def __init__(
        self,
        *,
        lamports: int = 0,
        por_programa: Mapping[str, Any] | None = None,
        falla_nativo: Exception | None = None,
    ) -> None:
        self.lamports = lamports
        self.por_programa = por_programa or {}
        self.falla_nativo = falla_nativo
        self.metodos: list[str] = []

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...

    async def call(self, chain_key: str, method: str, params: Sequence[Any] = ()) -> Any:
        del chain_key
        self.metodos.append(method)
        if method == "getBalance":
            if self.falla_nativo is not None:
                raise self.falla_nativo
            return {"context": {"slot": 1}, "value": self.lamports}
        assert method == "getTokenAccountsByOwner", f"método inesperado {method}"
        programa = params[1]["programId"]
        respuesta = self.por_programa.get(programa, [])
        if isinstance(respuesta, Exception):
            raise respuesta
        return {"context": {"slot": 1}, "value": respuesta}

    async def eth_call(self, chain_key: str, to: str, data: str, *, block: str = "latest") -> str:
        raise AssertionError("Solana no usa eth_call")


def _lector_sol(nodo: _NodoSolana) -> sol.SolanaReader:
    return sol.SolanaReader(reader=nodo)  # type: ignore[arg-type]


async def test_solana_pregunta_por_los_dos_programas_de_token() -> None:
    """El Token-2022 traía 325 cuentas medidas: omitirlo esconde dinero."""
    nodo = _NodoSolana(lamports=1_500_000_000)

    await _lector_sol(nodo).holdings("solana", DUEÑO)

    programas = [p for p in ("getBalance", "getTokenAccountsByOwner") if p in nodo.metodos]
    assert programas == ["getBalance", "getTokenAccountsByOwner"]
    assert nodo.metodos.count("getTokenAccountsByOwner") == 2, "faltó un programa de token"


async def test_el_saldo_nativo_llega_en_lamports_convertido_a_sol() -> None:
    nodo = _NodoSolana(lamports=10_572_652_082_898)

    resultado = await _lector_sol(nodo).holdings("solana", DUEÑO)

    nativo = resultado.native
    assert nativo is not None
    assert nativo.token.symbol == "SOL"
    assert nativo.token.decimals == 9
    assert nativo.amount.raw == 10_572_652_082_898
    assert str(nativo.as_decimal()) == "10572.652082898"


async def test_un_programa_que_falla_no_se_lleva_lo_del_otro() -> None:
    """El fallo medido: 403 en Token-2022 mientras el clásico contesta."""
    nodo = _NodoSolana(
        lamports=1_000,
        por_programa={
            sol.TOKEN_PROGRAM: [_cuenta("MintUno", 5_000_000)],
            sol.TOKEN_2022_PROGRAM: SourceResponseError("HTTP 403"),
        },
    )

    resultado = await _lector_sol(nodo).holdings("solana", DUEÑO)

    assert not resultado.failed
    assert [h.token.address for h in resultado.holdings] == [None, "MintUno"]
    assert resultado.holdings[1].amount.raw == 5_000_000
    assert len(resultado.missing) == 1
    assert "Token-2022" in resultado.missing[0]


async def test_si_los_dos_programas_fallan_el_hueco_se_dice_y_no_se_calla() -> None:
    """El fallo que se arregló: una cartera de sólo SOL con el mismo aspecto.

    Si los dos programas fallaran y esto devolviera una lista vacía sin más, la
    red se pintaría con el SOL y sin un solo token, indistinguible de una cartera
    que de verdad no tiene tokens. `missing` es lo que las separa.
    """
    nodo = _NodoSolana(
        lamports=1_000,
        por_programa={
            sol.TOKEN_PROGRAM: SourceResponseError("HTTP 403"),
            sol.TOKEN_2022_PROGRAM: SourceResponseError("HTTP 403"),
        },
    )

    resultado = await _lector_sol(nodo).holdings("solana", DUEÑO)

    assert not resultado.failed, "el nativo sí se leyó: no es una red ilegible"
    assert [h.token.symbol for h in resultado.holdings] == ["SOL"]
    assert len(resultado.missing) == 2
    assert not resultado.is_complete
    assert resultado.total_in_reference is None


async def test_varias_cuentas_del_mismo_mint_se_suman_una_sola_vez() -> None:
    """Dos token accounts del mismo mint son legales, y sumarlas dos veces miente.

    Se listaran sueltas, el USDC saldría dos veces con la mitad cada una y el
    total por red —que suma holdings— daría el doble.
    """
    nodo = _NodoSolana(
        lamports=0,
        por_programa={
            sol.TOKEN_PROGRAM: [
                _cuenta("MintUno", 3_000_000),
                _cuenta("MintUno", 4_000_000),
                _cuenta("MintDos", 1_000),
            ]
        },
    )

    resultado = await _lector_sol(nodo).holdings("solana", DUEÑO)

    por_mint = {h.token.address: h.amount.raw for h in resultado.holdings if not h.token.is_native}
    assert len(por_mint) == 2, "el mismo mint salió más de una vez"
    assert por_mint["MintUno"] == 7_000_000
    assert por_mint["MintDos"] == 1_000


async def test_el_simbolo_del_holding_es_el_del_token_que_lo_lleva() -> None:
    """Si divergieran, la cantidad no se podría sumar consigo misma.

    `TokenAmount` exige símbolo **y** decimales iguales para combinar dos
    cantidades, así que un símbolo calculado por un camino y el del `Token` por
    otro dejaría un holding que no se puede agregar ni con su propio token.
    """
    nodo = _NodoSolana(lamports=0, por_programa={sol.TOKEN_PROGRAM: [_cuenta("MintUno", 7, 9)]})

    resultado = await _lector_sol(nodo).holdings("solana", DUEÑO)

    holding = resultado.holdings[0]
    assert holding.amount.symbol == holding.token.symbol
    assert holding.amount.decimals == holding.token.decimals


async def test_un_mint_con_decimales_absurdos_se_descarta_sin_tirar_el_resto() -> None:
    """255 decimales no es un token: es una cantidad que ninguna interfaz pinta."""
    nodo = _NodoSolana(
        lamports=0,
        por_programa={sol.TOKEN_PROGRAM: [_cuenta("Malo", 1, 255), _cuenta("Bueno", 42, 6)]},
    )

    resultado = await _lector_sol(nodo).holdings("solana", DUEÑO)

    assert [h.token.address for h in resultado.holdings] == [None, "Bueno"]


@pytest.mark.parametrize(
    "basura",
    [
        {"account": {"data": "no soy un objeto"}},
        {"account": {"data": {"parsed": {"type": "mint", "info": {}}}}},
        {"account": {"data": {"parsed": {"type": "account", "info": {}}}}},
        {
            "account": {
                "data": {
                    "parsed": {
                        "type": "account",
                        "info": {"mint": "M", "tokenAmount": {}},
                    }
                }
            }
        },
    ],
)
async def test_una_cuenta_con_forma_rara_no_invalida_a_las_demas(basura: dict[str, Any]) -> None:
    """El nodo puede colar objetos de otra forma; una entrada rara no es 2819."""
    nodo = _NodoSolana(
        lamports=0,
        por_programa={sol.TOKEN_PROGRAM: [basura, _cuenta("Buena", 9)]},
    )

    resultado = await _lector_sol(nodo).holdings("solana", DUEÑO)

    assert [h.token.address for h in resultado.holdings] == [None, "Buena"]


async def test_un_saldo_nativo_que_no_es_un_entero_es_un_fallo_de_la_red() -> None:
    """Un ``value`` con otra forma no se convierte a la fuerza: se dice."""
    nodo = _NodoSolana(lamports=0)
    nodo.lamports = "no soy un entero"  # type: ignore[assignment]

    resultado = await _lector_sol(nodo).holdings("solana", DUEÑO)

    assert resultado.failed
    assert resultado.error is not None
    assert "lamports" in resultado.error


async def test_una_red_de_solana_ilegible_no_lanza() -> None:
    nodo = _NodoSolana(falla_nativo=SourceResponseError("el nodo no contestó"))

    resultado = await _lector_sol(nodo).holdings("solana", DUEÑO)

    assert resultado.failed
    assert resultado.error is not None
    assert "no contestó" in resultado.error


async def test_el_total_de_una_red_solana_completa_no_falla() -> None:
    """El caso sano: dos programas contestan y la red puede afirmar su total."""
    nodo = _NodoSolana(lamports=1_000, por_programa={sol.TOKEN_PROGRAM: [_cuenta("Uno", 5)]})

    resultado: ChainHoldings = await _lector_sol(nodo).holdings("solana", DUEÑO)

    assert resultado.is_complete
    assert resultado.error is None
    assert resultado.missing == ()
