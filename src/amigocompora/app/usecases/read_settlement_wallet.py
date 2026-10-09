"""Leer la wallet de depósito: dirección, saldo y posiciones, sin firmar nada.

Sólo lectura. No declara ninguna capacidad, no pasa por el
`ConfirmationGateway` y no pide la clave privada: todo lo que sale de aquí es
público —una dirección, un saldo, unas posiciones— y nada tiene efectos. Por eso
el proveedor de claves se tipa contra `AddressSource` y no contra
`PrivateKeySource`: enseñar un saldo no debe poder leer la clave ni de refilón.

La dirección es una **derivación**, no una lectura: `settlement_wallet` la
calcula desde la EOA por CREATE2, sin red y sin credenciales del relayer. Por
eso se puede enseñar antes de haber leído nada —es lo que permite recibir un
depósito en una instalación recién configurada, cuando la wallet todavía no
existe en la cadena—. El saldo y las posiciones sí salen a la red, y sólo cuando
se piden.

Esto no es lo mismo que el **canal** de la wallet de `PlacePredictionOrder`: allí
unos fondos se mueven y hace falta el relayer; aquí sólo se mira, y por eso
funciona igual de bien sin credenciales configuradas.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

import structlog

from amigocompora.app.execution_policy import AddressSource
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.models import PredictionPosition, Token
from amigocompora.domain.money import TokenAmount
from amigocompora.infra.evm.broadcast import EvmBroadcaster

_log = structlog.get_logger(__name__)

#: La red donde vive la deposit wallet de Polymarket. El colateral del recinto y
#: las participaciones están en Polygon; no es una preferencia de esta lectura,
#: es un hecho de la cuenta.
CHAIN_KEY: Final = "polygon"


@dataclass(frozen=True, slots=True)
class SettlementWalletView:
    """La foto de la wallet tras leerla. Todo público y nada firmable."""

    address: str
    collateral: Token
    balance: TokenAmount
    positions: tuple[PredictionPosition, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ReadSettlementWallet:
    """Lee la deposit wallet del usuario. Devuelve la foto; no guarda nada."""

    registry: EngineRegistry
    keys: AddressSource
    broadcasters: Mapping[str, EvmBroadcaster] = field(default_factory=dict)
    clock: Clock | None = None

    def address(self) -> str:
        """La dirección de la wallet, derivada de la EOA. Pura: sin red.

        Lanza si no hay cartera o si el motor activo no sabe planificar órdenes
        —de quien sale `settlement_wallet`—, con el mensaje de cada uno: son dos
        negativas distintas y confundirlas mandaría al usuario a arreglar lo que
        no está roto.
        """
        planner = self.registry.prediction_planner()
        return planner.settlement_wallet(self._owner())

    def collateral(self) -> Token:
        """El colateral del recinto en Polygon. Pura: sale del catálogo."""
        return self.registry.prediction_redeemer().collateral_on(CHAIN_KEY)

    async def __call__(self) -> SettlementWalletView:
        wallet = self.address()
        redeemer = self.registry.prediction_redeemer()
        collateral = redeemer.collateral_on(CHAIN_KEY)
        contrato = _address_of(collateral)
        balance = await self._broadcaster().token_balance(contrato, wallet)
        positions = await redeemer.positions(wallet=wallet, redeemable_only=False)
        _log.info(
            "prediction.wallet_read",
            wallet=wallet,
            raw_balance=balance,
            positions=len(positions),
        )
        return SettlementWalletView(
            address=wallet,
            collateral=collateral,
            balance=TokenAmount(
                raw=balance,
                decimals=collateral.decimals,
                symbol=collateral.symbol,
            ),
            positions=positions,
            observed_at=(self.clock or SystemClock()).now(),
        )

    # --------------------------------------------------------------- apoyo  #
    def _owner(self) -> str:
        owner = self.keys.address() if self.keys.available() else None
        if owner is None:
            raise ExecutionError(
                "no hay ninguna cartera configurada, así que no se puede derivar "
                "la dirección de la wallet de depósito."
            )
        return owner

    def _broadcaster(self) -> EvmBroadcaster:
        broadcaster = self.broadcasters.get(CHAIN_KEY)
        if broadcaster is None:
            raise ExecutionError(
                f"no hay ningún nodo configurado para «{CHAIN_KEY}»: sin él no se "
                f"puede leer el saldo de la wallet."
            )
        return broadcaster


def _address_of(token: Token) -> str:
    """La dirección del colateral, o un error que dice qué falta.

    Un `Token` puede no tener dirección —el nativo de una red no la tiene—, y el
    colateral de este recinto sí la tiene siempre. Cuando no la tuviera, el
    error tiene que decir eso y no un `None` colado en una llamada de red.
    """
    if token.address is None:
        raise ExecutionError(
            f"el colateral «{token.symbol}» del recinto no tiene dirección, así "
            f"que no hay contrato contra el que leer el saldo de la wallet."
        )
    return token.address
