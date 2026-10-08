"""Retirar fondos de la cartera: sacar dinero de verdad.

Capacidad requerida: `BROADCAST_TX`, que **sólo** concede el modo `EJECUCIÓN`.
Es, junto con `ExecuteSwap` y los puentes, uno de los tres caminos por los que
sale dinero, y es el más desnudo de los tres: no hay router, no hay cotización,
no hay motor que construya nada. Se manda lo que se manda, y no vuelve.

### El orden, y por qué cada paso está donde está

1. **El modo, antes que nada.** En `OBSERVACIÓN`, `SIMULACIÓN` o `ASISTIDO` esto
   lanza en la primera línea y no se gasta ni una petición de red.
2. **La red.** Fuera de EVM no se firma: ver `require_evm`. Una retirada en
   Solana es otra implementación —otro formato de transacción, otra unidad de
   comisión— y decir que no es mejor que hacerla a medias.
3. **El destinatario**, validado **antes** de construir nada. Ésta es la
   comprobación que más importa en este fichero: una dirección mal escrita en una
   transferencia no revierte, se ejecuta y el dinero se va a una dirección que
   nadie controla. No hay vuelta atrás ni contrato que lo deshaga.
4. **Los límites**, contra el registro de ejecuciones: red, token, motor, importe
   por operación y acumulado del día.
5. **El saldo.** Una transferencia por más de lo que hay revierte —en el mejor
   caso, pagando el gas— y en el nativo el fallo es más sutil: hay que dejar
   dinero para pagar el gas, así que «todo mi saldo» **nunca** cabe. Se comprueba
   aquí, antes de firmar, y no se deja que lo descubra la red.
6. **El payload.** Nativo: `value` y sin calldata. ERC-20: `transfer(to, amount)`
   al contrato del token, y `value` en cero.
7. **El permiso**, con la operación entera delante: a dónde va y cuánto sale.
8. Firmar, emitir y anotar.

### La clave se pide para saber de dónde sale el dinero

Un paso de este camino necesita saber **qué dirección firma**, y no por comodidad:
es la dirección cuyo saldo hay que comprobar y la que el usuario tiene que ver
antes de confirmar. Se deriva pidiendo la clave y soltándola, exactamente igual
que hace `ExecuteSwap.address()`. Y se vuelve a comprobar al firmar, comparando
la dirección que se dedujo de la clave **que se va a usar** con la del intento:
si entre la comprobación del saldo y la firma alguien cambió la credencial
configurada, se firmaría desde una cartera cuyo saldo nadie miró.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.execution_policy import AutonomyPolicy, PrivateKeySource
from amigocompora.app.usecases.value_in_reference import ReferenceValuation
from amigocompora.domain.addresses import require_evm_address, shorten
from amigocompora.domain.chains import chain
from amigocompora.domain.errors import ExecutionError, UnsupportedOperationError
from amigocompora.domain.models import (
    BroadcastReceipt,
    Token,
    UnsignedTransaction,
    WithdrawIntent,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.engines.catalog import quote_token, wrapped_native
from amigocompora.infra.evm.broadcast import (
    EvmBroadcaster,
    build_transfer_calldata,
    require_evm,
)
from amigocompora.infra.evm.dispatch import sign_and_send
from amigocompora.infra.evm.signer import address_from_key

_log = structlog.get_logger(__name__)

#: El gas de una transferencia nativa sin calldata. Es fijo por definición del
#: protocolo: 21 000, ni uno más ni uno menos. Se usa para saber **cuánto hay que
#: dejar** en la cartera para pagar la comisión, porque de eso depende que una
#: retirada de «todo el saldo» quepa o no — y no cabe.
NATIVE_TRANSFER_GAS = 21_000

#: Con qué motor se anota una retirada en el registro de ejecuciones.
#:
#: Una retirada no pasa por ningún motor —no hay par, no hay ruta, no hay
#: cotización—, pero el registro tiene una columna de motor y la política la
#: comprueba contra una lista blanca (`check_engine`), así que hay que decir algo.
#: Se dice `wallet`, que es el motor que **sí** existe y del que sale el dinero: la
#: cartera. Y tiene una consecuencia que hay que saber: `allowed_engines` no es una
#: lista de exclusiones sino de inclusiones —vacía significa «nada permitido»—, así
#: que si no incluye `wallet`, las retiradas se rechazan. Es a propósito: retirar
#: es sacar dinero, y que haya que nombrarlo en la lista blanca es exactamente el
#: grado de fricción que merece.
WALLET_ENGINE_ID: Final = "wallet"


@dataclass(frozen=True, slots=True)
class WithdrawFunds:
    """Saca un token de la cartera y lo manda a otra dirección."""

    gateway: ConfirmationGateway
    keys: PrivateKeySource
    policy: AutonomyPolicy
    #: Un emisor por red, para leer el saldo y para emitir. Se recibe como mapa y
    #: no como registro para que este caso de uso no dependa de `infra.rpc`.
    broadcasters: Mapping[str, EvmBroadcaster] = field(default_factory=dict)
    #: Con qué se valora lo retirado cuando el token **no** es la stablecoin de
    #: su red. Sin esto, retirar ETH o cualquier token contra un tope escrito en
    #: dólares se rechaza en vez de medirse mal.
    valuation: ReferenceValuation | None = None

    async def __call__(
        self, token: Token, amount: TokenAmount, *, recipient: str
    ) -> BroadcastReceipt:
        # 1. El modo primero: antes de gastar red, y antes de pedir la clave.
        self.gateway.precheck(Capability.PREPARE_TX)
        self.gateway.precheck(Capability.BROADCAST_TX)

        spec = chain(token.chain)

        # 2. Esta entrega firma en EVM y sólo en EVM.
        try:
            require_evm(spec)
        except UnsupportedOperationError as error:
            raise UnsupportedOperationError(
                f"retirar desde «{token.chain}» todavía no está implementado: esta "
                f"entrega firma retiradas EVM. En Solana la transacción se construye "
                f"de otra forma y la comisión se paga en otra unidad, así que no se "
                f"reutiliza este camino. ({error})"
            ) from error

        # 3. El destinatario, antes de construir nada. Es la comprobación que más
        #    importa aquí: una dirección mal escrita no revierte, se ejecuta.
        destino = require_evm_address(recipient, "el destinatario de la retirada")

        source = self._source()

        # 4. El intento y los topes.
        intent = await self._intent(token, amount, destino, source)
        self.policy.check_intent(intent)

        broadcaster = self._broadcaster(token.chain)

        # 5. El saldo, antes de firmar.
        await self._require_balance(broadcaster, token, amount, source)

        # 6. El payload.
        unsigned = self._build(token, amount, destino, spec.require_eip155_id())

        # 7. El permiso, con todo delante.
        await self._authorize(token, amount, destino, source, unsigned, intent)

        # 8. Firmar, emitir, anotar.
        receipt = await self._sign_and_send(unsigned, broadcaster, source=source)
        self.policy.ledger.record_receipt(receipt, intent)
        _log.info(
            "withdrawal.recorded",
            chain=token.chain,
            token=token.symbol,
            amount=f"{amount.as_decimal():f}",
            to=destino,
            tx_hash=receipt.tx_hash,
            status=receipt.status.value,
        )
        return receipt

    # --------------------------------------------------------------- pasos  #
    async def _intent(
        self, token: Token, amount: TokenAmount, recipient: str, source: str
    ) -> WithdrawIntent:
        """El intento que la política evalúa, con el importe en la unidad del tope.

        Igual que en un swap, hay dos casos: si el token **es** la stablecoin con
        la que se miden los topes, el importe ya está en esa unidad y no hay nada
        que valorar; si no lo es —ETH, un token cualquiera— se valora con los
        mismos motores que cotizan los pares, y **si no se puede valorar no se
        retira**. Un tope que se salta cuando no se puede medir no es un tope, y
        una retirada es justo donde más caro sale saltárselo.
        """
        reference = quote_token(token.chain)
        if reference is not None and token.symbol == reference.symbol:
            return WithdrawIntent(
                chain=token.chain,
                token=token,
                amount=amount,
                recipient=recipient,
                source=source,
                engine_id=WALLET_ENGINE_ID,
            )

        if self.valuation is None or reference is None:
            raise ExecutionError(self._describe_unmeasurable(token, reference))

        cotizable, importe = self._quotable(token, amount)
        value = await self.valuation(cotizable, importe, reference)
        if value is None:
            raise ExecutionError(self._describe_unmeasurable(token, reference))
        _log.info(
            "withdrawal.valued_in_reference",
            chain=token.chain,
            token=token.symbol,
            amount=f"{amount.as_decimal():f}",
            quoted_as=cotizable.symbol,
            reference=reference.symbol,
            value=f"{value.as_decimal():f}",
        )
        return WithdrawIntent(
            chain=token.chain,
            token=token,
            amount=amount,
            recipient=recipient,
            source=source,
            engine_id=WALLET_ENGINE_ID,
            reference_value=value,
        )

    def _quotable(self, token: Token, amount: TokenAmount) -> tuple[Token, TokenAmount]:
        """El token con el que se puede **preguntar** por el precio de éste.

        Para un ERC-20 es él mismo. Para el nativo no hay nada que preguntar: los
        AMM de producto constante no operan con ETH, operan con su envoltorio
        ERC-20, así que preguntar por «ETH» no encuentra ninguna piscina y la
        retirada se rechazaría por no poder medirse —que es lo correcto cuando de
        verdad no se puede medir, y un error cuando lo único que pasa es que se
        preguntó por la forma equivocada del mismo activo.

        Envolver no es estimar: ETH y WETH son el mismo activo, el envoltorio es
        un depósito uno a uno —no un intercambio— y no hay comisión ni
        deslizamiento. Lo único que cambia es la escala, y por eso la sustitución
        sólo se hace cuando las dos coinciden: si algún día una red tuviera un
        nativo de 18 decimales y un envoltorio de 8, reutilizar el importe daría
        un número equivocado en silencio, y entonces es mejor no sustituir nada y
        rechazar la retirada por no poder medirla.
        """
        if token.address is not None:
            return token, amount
        envoltorio = wrapped_native(token.chain)
        if envoltorio is None or envoltorio.decimals != token.decimals:
            return token, amount
        return envoltorio, TokenAmount(amount.raw, envoltorio.decimals, envoltorio.symbol)

    def _describe_unmeasurable(self, token: Token, reference: Token | None) -> str:
        """Por qué no se puede medir esta retirada.

        El caso «hay referencia pero no se pudo cotizar» lo explica el valorador,
        no este fichero: el mensaje vive junto a la búsqueda que falló, y así no
        puede desfasarse de lo que de verdad se intentó. Aquí sólo se escribe el
        caso que el valorador no conoce —una red sin moneda de referencia— y el
        de no tener valorador siquiera.
        """
        if reference is None:
            return (
                f"«{token.chain}» no tiene una moneda de referencia con la que medir "
                f"los topes, así que una retirada de {token.symbol} no se puede "
                f"comparar con ninguno. Retirar sin poder aplicar el tope sería "
                f"operar sin límite."
            )
        if self.valuation is None:
            return (
                f"retirar {token.symbol} en «{token.chain}» no se puede medir contra "
                f"los topes, que están escritos en {reference.symbol}, y esta "
                f"instalación no tiene con qué valorarlo. Se rechaza en vez de retirar "
                f"sin límite: se arregla activando un motor que cotice "
                f"{token.symbol} contra {reference.symbol}."
            )
        return self.valuation.describe_failure(token, reference)

    async def _require_balance(
        self, broadcaster: EvmBroadcaster, token: Token, amount: TokenAmount, owner: str
    ) -> None:
        """Corta si la cartera no tiene para cubrir la retirada.

        En ERC-20 el fallo es claro: `transfer` revierte por saldo insuficiente y
        se paga el gas igual.

        En el nativo el fallo es más sutil y por eso se calcula aquí: el importe
        sale de la cartera **y** el gas también, así que mandar el saldo entero no
        cabe nunca — falta justo lo que cuesta mover. Se reserva el gas de una
        transferencia sin calldata (21 000, fijo) al precio máximo que la política
        está dispuesta a pagar, que es el peor caso: con eso, si pasa la
        comprobación, pasa de verdad.
        """
        if token.address is None:
            disponible = await broadcaster.native_balance(owner)
            reserva = NATIVE_TRANSFER_GAS * (await broadcaster.read_fees()).max_fee_per_gas
            if amount.raw + reserva > disponible:
                raise ExecutionError(
                    f"la cartera tiene {_human(disponible, token.decimals)} {token.symbol} y "
                    f"la retirada es de {amount.as_decimal():f}. Hay que dejar además "
                    f"{_human(reserva, token.decimals)} {token.symbol} para pagar el gas "
                    f"de la transferencia, así que ni siquiera el saldo entero cabe: en "
                    f"el nativo la comisión sale de la misma cuenta que el importe."
                )
            return

        disponible = await broadcaster.token_balance(token.address, owner)
        if amount.raw > disponible:
            raise ExecutionError(
                f"la cartera tiene {_human(disponible, token.decimals)} {token.symbol} y "
                f"la retirada es de {amount.as_decimal():f}. No se firma: la "
                f"transferencia revertiría y el gas se pagaría igual."
            )

    def _build(
        self, token: Token, amount: TokenAmount, recipient: str, chain_id: int
    ) -> UnsignedTransaction:
        """El payload de la retirada.

        Los dos casos se ven mejor juntos, porque son casi opuestos:

        - **El nativo** viaja en el `value` de la transacción, que va a la
          dirección de destino, y **no lleva calldata**: no hay contrato al que
          llamar. El destinatario de la llamada y el de los fondos son el mismo.
        - **Un ERC-20** se mueve llamando a `transfer` en **su** contrato, con
          `value` en cero. Aquí el destinatario de la llamada es el token y el de
          los fondos va escrito dentro de la calldata: son dos direcciones
          distintas, y confundirlas manda el dinero a la dirección del contrato.
        """
        if token.address is None:
            return UnsignedTransaction(
                chain_id=chain_id,
                to_address=recipient,
                calldata="",
                value=amount,
                description=f"Retirada de {amount} a {shorten(recipient)}",
            )
        return UnsignedTransaction(
            chain_id=chain_id,
            to_address=token.address,
            calldata=build_transfer_calldata(recipient, amount.raw),
            value=TokenAmount(raw=0, decimals=token.decimals, symbol=token.symbol),
            description=(
                f"Retirada de {amount} a {shorten(recipient)} "
                f"(transfer en el contrato de {token.symbol})"
            ),
        )

    async def _authorize(
        self,
        token: Token,
        amount: TokenAmount,
        recipient: str,
        source: str,
        transaction: UnsignedTransaction,
        intent: WithdrawIntent,
    ) -> None:
        """El permiso para emitir, con todo delante.

        Se enseña **a dónde va** la primera, y con la dirección completa, no
        acortada: en una retirada la dirección de destino es el único dato que
        decide si el dinero llega o se pierde, y acortarla escondería justo los
        caracteres donde vive un error de copia. El resto de la pantalla puede
        permitirse abreviar; esto no.
        """
        await self.gateway.authorize(
            Capability.BROADCAST_TX,
            f"Retirar {amount} de {token.chain}",
            details=(
                f"Sale de la cartera: {source}",
                f"Va a esta dirección: {recipient}",
                f"Red: {token.chain}",
                f"Importe: {amount}",
                f"Importe de referencia: {intent.measured}",
                f"Contrato que recibe la llamada: {transaction.to_address}",
                f"Motor que lo anota: {intent.engine_id}",
                "Comprueba la dirección de destino carácter a carácter: una "
                "transferencia no se puede deshacer.",
                "Es una operación real e irreversible: se firma y se emite.",
            ),
            transaction=transaction,
        )

    async def _sign_and_send(
        self, unsigned: UnsignedTransaction, broadcaster: EvmBroadcaster, *, source: str
    ) -> BroadcastReceipt:
        """Firma con la clave del momento y comprueba que es la cartera esperada.

        La comprobación no es ceremonia: el saldo se miró hace unas líneas y la
        clave se pide ahora. Si entre las dos cosas cambió la credencial
        configurada, se firmaría desde una cartera **cuyo saldo nadie comprobó**, y
        el registro diría que salió de la primera. Se corta aquí, que todavía no
        se ha emitido nada.
        """
        key = self.keys.require()
        firmante = address_from_key(key)
        if firmante.lower() != source.lower():
            raise ExecutionError(
                f"la clave configurada ahora firma desde {shorten(firmante)} y el saldo "
                f"se comprobó en {shorten(source)}: la credencial cambió entre la "
                f"comprobación y la firma. No se emite nada."
            )
        # El destino esperado es el de la **llamada**, que es el mismo objeto que
        # se acaba de construir y enseñar: no se recalcula, para que no pueda
        # haber dos respuestas que se separen entre lo confirmado y lo firmado.
        return await sign_and_send(
            unsigned,
            broadcaster,
            private_key=key,
            expected_to=unsigned.to_address,
            event="withdrawal.signing",
        )

    # --------------------------------------------------------------- apoyo  #
    def _source(self) -> str:
        """De qué cartera sale el dinero, según la clave configurada.

        Sólo sale la dirección, que es pública. La clave se pide y se suelta aquí
        mismo: lo único que se queda es a quién pertenece.
        """
        if not self.keys.available():
            raise ExecutionError(
                "no hay ninguna clave de cartera configurada, así que no hay desde "
                "dónde retirar. Configúrala en la pestaña de motores, en Credenciales."
            )
        return address_from_key(self.keys.require())

    def _broadcaster(self, chain_key: str) -> EvmBroadcaster:
        broadcaster = self.broadcasters.get(chain_key)
        if broadcaster is None:
            raise ExecutionError(
                f"no hay ningún nodo configurado para «{chain_key}»: sin él no se "
                f"puede leer el saldo ni emitir nada. Declara al menos un endpoint "
                f"para esa red."
            )
        return broadcaster


def _human(raw: int, decimals: int) -> str:
    """Un entero en unidad mínima, escrito en unidades humanas.

    Existe para que el mensaje de error se pueda leer: «tiene 19491666140000000000»
    no dice nada, «tiene 19,49166614» sí.
    """
    return f"{TokenAmount(raw=raw, decimals=decimals, symbol='x').as_decimal():f}"
