"""Reunir el estado de red, firmar y emitir. El tramo que comparten los tres caminos.

### Por qué existe

Los tres caminos que emiten algo —un swap, el permiso de una orden de predicción
y el cobro de una posición— hacían esta misma secuencia, cada uno con su copia:

1. comprobar que el nodo está en la red que la transacción dice,
2. leer el nonce pendiente de quien firma,
3. resolver el límite de gas,
4. leer las comisiones de la red,
5. firmar EIP-1559 en local,
6. emitir.

Seis pasos, tres copias. La copia no es peligrosa por el código que repite sino
por el que **deja de repetir**: el día que a una de las tres se le añada una
comprobación —o que a una se le olvide una que las otras dos tienen—, las tres
seguirán pareciendo correctas por separado y sólo se distinguirán el día que
alguien emita por el camino corto. En un tramo que firma con dinero real, tener
un solo sitio donde se lee es la diferencia entre «las tres hacen lo mismo» y
«las tres deberían hacer lo mismo».

Los pasos 2 a 4 son además los que **cuestan red**, y van juntos a propósito: el
nonce y el gas se leen justo antes de firmar porque entre la lectura y la emisión
el estado de la cadena cambia, y una estimación de gas calculada antes de pedir
el nonce estaría midiendo otro estado.

### La clave entra, no se busca

La clave llega **por parámetro** y no se pide aquí dentro. Parece un detalle y es
la frontera de capas: quien la tiene —el caso de uso, que la pide a su almacén en
el último momento— es quien decide cuándo se pide, y este módulo no conoce ni el
almacén ni la frase que lo abre. Si la pidiera él, `infra` tendría que saber de
`app`, que es la dirección que la prueba de hermetismo prohíbe.

La clave se usa para derivar la dirección y para firmar, y sale de ámbito al
terminar la función: no se guarda en ningún objeto ni se pasa a nadie más.
"""

from __future__ import annotations

import structlog

from amigocompora.domain.models import BroadcastReceipt, UnsignedTransaction
from amigocompora.infra.evm.broadcast import EvmBroadcaster
from amigocompora.infra.evm.signer import address_from_key, sign_eip1559

_log = structlog.get_logger(__name__)


async def sign_and_send(
    unsigned: UnsignedTransaction,
    broadcaster: EvmBroadcaster,
    *,
    private_key: str,
    expected_to: str,
    event: str = "execution.signing",
) -> BroadcastReceipt:
    """Firma una transacción con la clave dada y la emite. Devuelve el recibo.

    `expected_to` es la dirección que el emisor **espera** que tenga la
    transacción, y se pasa hasta el final en vez de leerse de `unsigned`: el
    emisor compara las dos, y esa comparación es lo que detecta que se esté
    emitiendo contra otro contrato del que el caso de uso cree. Leerla de la
    misma transacción que se emite haría que la comprobación se diera siempre por
    buena.

    `event` permite que cada camino nombre su traza como venía haciéndolo. Es lo
    único que distinguía a las tres copias, y por eso es lo único que se
    parametriza: el resto tiene que ser idéntico o no sirve de nada tenerlo aquí.
    """
    owner = address_from_key(private_key)

    await broadcaster.require_chain(unsigned.chain_id)
    nonce = await broadcaster.pending_nonce(owner)
    gas_limit, gas_source = await broadcaster.resolve_gas_limit(
        unsigned, from_address=owner
    )
    fees = await broadcaster.read_fees()

    signed = sign_eip1559(
        unsigned,
        private_key=private_key,
        nonce=nonce,
        gas_limit=gas_limit,
        max_fee_per_gas=fees.max_fee_per_gas,
        max_priority_fee_per_gas=fees.max_priority_fee_per_gas,
    )
    # Se anota el hash y el gas, nunca la clave. `structlog` ya enmascara
    # `private_key`/`passphrase` por si algún día alguien la pasa, pero aquí no
    # se pasa.
    _log.info(
        event,
        chain=broadcaster.chain_key,
        to=unsigned.to_address,
        nonce=nonce,
        gas_limit=gas_limit,
        gas_source=gas_source,
        max_fee_per_gas=fees.max_fee_per_gas,
        tip=fees.max_priority_fee_per_gas,
        tx_hash=signed.tx_hash,
    )
    return await broadcaster.send(signed, expected_to=expected_to)
