"""Lo que falta para poder firmar, según la pantalla. Un solo sitio, funciones puras.

### Por qué está sacado de la página

Esta lista de motivos **tiene que decir lo mismo** que las comprobaciones que hará
el caso de uso al firmar. Si la pantalla dijera que sí y `ExecuteSwap` dijera que
no, la interfaz estaría prometiendo una operación que luego rechaza —y ese es el
botón que menos puede permitirse mentir, porque es el que saca dinero.

Escribirla dentro de la página la ataba a sus widgets y a su ciclo de vida: se
probaba sólo construyendo una ventana. Aquí son funciones de `(container, …)` a
`tuple[str, ...]`, así que se pueden comprobar sin Qt y la página sólo tiene que
pintar el resultado.

### Por qué se devuelven **todos** los motivos y no el primero

Son condiciones distintas y se arreglan en sitios distintos: el modo se cambia en
la propia pantalla, `enabled` se cambia en el fichero, las listas blancas y la
cartera se configuran aparte. Descubrirlas de una en una —arreglar una para que
aparezca la siguiente— es lo que hace pensar que la aplicación está rota en vez de
a medio configurar.

### Qué no hay aquí

Ni una regla nueva. Cada comprobación llama al **mismo** código que corre al
firmar (`ExecutionLimits.check_amount`, `reference_notional`), y los textos salen
de ahí para que no puedan separarse de lo que dirá el caso de uso.
"""

from __future__ import annotations

from amigocompora.app.container import Container
from amigocompora.app.usecases.execute_swap import reference_notional
from amigocompora.domain.addresses import is_evm_address, is_solana_address
from amigocompora.domain.chains import AddressFormat, chain
from amigocompora.domain.errors import ExecutionLimitExceededError
from amigocompora.domain.models import Quote, TradingPair
from amigocompora.domain.modes import Capability
from amigocompora.infra.wallets import WalletSource


def execution_blockers(
    container: Container,
    *,
    pair: TradingPair | None = None,
    quote: Quote | None = None,
) -> tuple[str, ...]:
    """Todo lo que falta para poder firmar y emitir un swap. Vacío significa que sí.

    Se devuelve la **lista de motivos** y no un booleano porque un botón apagado
    sin explicación es lo que empuja a alguien a buscar la forma de saltárselo.

    Mira las mismas cosas que mira `ExecuteSwap` antes de firmar, y en el mismo
    orden: el modo, el interruptor, la lista blanca de tokens, que haya un motor
    que sepa construir el payload en esa red, que haya cartera, y por último los
    topes de importe.

    El orden va de la ruta a la custodia: primero si la operación puede existir
    —el modo, el interruptor, un motor que sepa construir— y después si hay con
    qué firmarla. Decirlo al revés llevaría a configurar una cartera para
    descubrir luego que no había por dónde.

    El par se pregunta a propósito, y no sólo la red: un motor puede construir en
    una red y no en otra —`uniswap` cubre Base pero no Solana—, así que «hay algún
    planificador» no es la misma pregunta que «hay planificador para el par que
    estoy mirando». Y con un token añadido por dirección la lista blanca deja de
    ser un detalle de configuración que se supone correcto: es la diferencia entre
    un botón que firma y uno que no.
    """
    motivos: list[str] = []
    if not container.guard.mode.grants(Capability.BROADCAST_TX):
        motivos.append(
            f"el modo {container.guard.mode.label} no permite emitir "
            "(cambia a EJECUCIÓN)"
        )
    if not container.policy.limits.enabled:
        motivos.append(
            "la ejecución está apagada (`enabled = true` bajo `[execution]` "
            "en config.toml)"
        )
    if pair is not None:
        motivos.extend(_token_blockers(container, pair))
    chain_key = pair.chain if pair is not None else None
    if not container.prepare_swap.is_available(chain_key):
        # Se midió: con `geckoterminal` activo —que cotiza pero no construye— no
        # hay ningún planificador, y sin este motivo el botón quedaba encendido y
        # fallaba al pulsarlo. Es justo el botón que enseña a desconfiar de los
        # botones, y el más caro de todos: el que firma.
        donde = f" en {chain_key}" if chain_key else ""
        motivos.append(
            f"no hay ningún motor activo capaz de construir el swap{donde} "
            "(activa `uniswap` o `zeroex` y dale su clave)"
        )
    cartera = _wallet_blocker(container)
    if cartera is not None:
        motivos.append(cartera)
    if quote is not None and not container.prepare_swap.can_build(quote):
        # La ruta la cotizó un motor que sólo cotiza: aparece en la tabla, pero el
        # swap no sale de él. Se dice cuál sirve, en vez de dejar un botón que falla.
        motivos.append(
            f"la ruta de «{quote.engine_id}» sólo cotiza y no construye el swap: "
            f"elige una ruta de uniswap_v3 para firmarla"
        )
    if quote is not None:
        motivos.extend(cap_blockers(container, quote))
    return tuple(motivos)


def _wallet_blocker(container: Container) -> str | None:
    """El motivo de cartera para no poder firmar, o `None` si la activa puede.

    Se distinguen los tres «no» —ninguna cartera, la activa en observación, la
    activa cifrada sin desbloquear— porque se arreglan en sitios distintos:
    añadir una cartera, elegir la que tiene clave, desbloquear la sesión. Un
    motivo genérico mandaría a buscar la solución donde no está.

    Las preguntas se le hacen al proveedor —`is_locked`, `available`— y no se
    deducen del libro: es la misma pieza que decide al firmar, y una respuesta
    copiada aquí podría desviarse de la que da el caso de uso.
    """
    keys = container.keys
    active = keys.active()
    if active is None:
        # Sin entradas en el libro manda el proveedor heredado —la clave del
        # llavero o la del entorno—, y su respuesta es la de siempre.
        if not keys.available():
            return "no hay cartera configurada, así que no hay con qué firmar"
        return None
    if active.source is WalletSource.WATCH:
        return (
            f"la cartera «{active.label}» está en modo observación: se leen sus "
            f"saldos, pero no puede firmar (elige una cartera con clave en la "
            f"Cartera, pulsando su nombre)"
        )
    if keys.is_locked():
        return (
            f"la cartera «{active.label}» está bloqueada: desbloquéala en la "
            f"Cartera (menú de la cabecera → «Desbloquear cartera»)"
        )
    if not keys.available():
        return "no hay cartera configurada, así que no hay con qué firmar"
    return None


def _token_blockers(container: Container, pair: TradingPair) -> tuple[str, ...]:
    """Las dos patas del par contra `allowed_tokens`, nombrando lo que sí hay.

    Se dice también **qué hay declarado**: el error más común no es olvidar la
    lista, es escribir en ella un nombre que ningún token tiene. `ETH` no es el
    símbolo de ningún token de la aplicación —el nativo va envuelto y se llama
    `WETH`—, así que una lista con `ETH` dentro deja fuera justo lo que se quería
    permitir, y sin ver la lista al lado eso no se nota.
    """
    limites = container.policy.limits
    # Quién pasa lo decide `allows_token` —mayúsculas en los dos lados, como
    # `check_token` al firmar, y comodín «*» incluido—: la pantalla y el caso de
    # uso tienen que dar la misma respuesta, o el botón diría que no cuando la
    # política sí deja. El catálogo tiene símbolos en caja mixta (`pUSD`), así
    # que comparar en crudo bloquea tokens que la lista sí permite.
    fuera = sorted(
        symbol
        for symbol in (pair.base.symbol, pair.quote.symbol)
        if not limites.allows_token(symbol)
    )
    if not fuera:
        return ()
    return (
        f"{', '.join(fuera)} no está en `allowed_tokens`, así que la política no "
        f"deja operar con ese par (ahora declara: "
        f"{', '.join(sorted(limites.allowed_tokens)) or 'nada'}; añádelo en config.toml)",
    )


def cap_blockers(container: Container, quote: Quote) -> tuple[str, ...]:
    """Lo que los topes dirían de **esta** operación, con el importe ya medido.

    Faltaba, y se notó midiendo: con el campo de cantidad en el valor que trae al
    abrir —1,0—, la compra de 1 WETH valía unos 2 605 USDC contra un tope de 1 por
    operación. El botón se encendía igual, porque la lista miraba el modo, el
    interruptor, la lista blanca, el planificador y la cartera, pero no el
    importe. El caso de uso sí lo mira, y lo mira **antes** del diálogo, así que no
    se firmaba nada; lo que pasaba es que el botón prometía una operación que la
    política iba a rechazar en cuanto se pulsara.

    El importe se saca con `reference_notional`, que es la **misma** función que
    usa `ExecuteSwap._intent`: es la única forma de que el veredicto de aquí y el
    de allí no puedan separarse. Se llama a `check_amount`, que es el mismo código
    que corre al firmar, así que el texto del motivo es el que el usuario vería
    igualmente, sólo que antes de pulsar.

    Con un par que no toca la moneda de referencia, `reference_notional` devuelve
    `None` y aquí no se dice nada: valorarlo exige red, y el gasto de las últimas
    24 h se mueve solo, así que un «sí» tampoco sería una promesa.
    """
    notional = reference_notional(quote)
    if notional is None:
        return ()
    try:
        container.policy.limits.check_amount(
            notional.as_decimal(),
            spent_today=container.policy.spent_in_window(notional.symbol),
        )
    except ExecutionLimitExceededError as error:
        return (str(error),)
    return ()


def recipient_candidates(container: Container, chain_key: str) -> list[str]:
    """Las direcciones ya conocidas que son válidas en esa red, la propia primero.

    Ofrecer las propias antes que un campo vacío no es comodidad: teclear una
    dirección a mano es la forma más común de perder fondos, y la lista sale de lo
    que el usuario ya declaró en su configuración.

    La **cartera de la aplicación va primera**, porque es la dirección desde la que
    sale el dinero —de ahí sale y ahí vuelve— y es la única de la lista que la
    aplicación conoce sin que nadie la haya escrito en la configuración. Se deriva
    de la clave, así que nunca es la clave. Después van las demás carteras del
    libro —a qué otra dirección mandaría el usuario si no a la suya de reserva— y
    por último las observadas de la configuración.
    """
    spec = chain(chain_key)
    solana = spec.address_format is AddressFormat.SOLANA_BASE58
    check = is_solana_address if solana else is_evm_address
    candidates: list[str] = []
    propia = container.keys.address()
    if propia is not None and check(propia):
        candidates.append(propia)
    candidates.extend(
        wallet.address for wallet in container.keys.wallets() if check(wallet.address)
    )
    candidates.extend(
        address for address in container.settings.watch_addresses if check(address)
    )
    # Sin repetidos y conservando el orden: la cartera activa está también en el
    # libro, y verla dos veces en la lista parece un fallo.
    return list(dict.fromkeys(candidates))
