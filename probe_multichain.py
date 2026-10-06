"""Verificación con datos reales de las 11 redes y de las versiones de Uniswap.

No simula nada: construye el contenedor de la aplicación, activa cada motor y
cotiza contra las APIs públicas de verdad. Temporal — se borra al terminar.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

from amigocompora.app.container import build_container
from amigocompora.domain.chains import CHAINS, chain
from amigocompora.domain.errors import ModeNotPermittedError, NoQuotesError
from amigocompora.domain.models import TradingPair
from amigocompora.domain.modes import OperationMode
from amigocompora.infra.config import Settings
from amigocompora.engines import catalog
from amigocompora.engines.amm_protocols import parse_dex_id

#: Tamaños de orden por red, en unidades del token base. Pequeños a propósito:
#: lo que se comprueba es que la cotización sale, no la profundidad.
SIZES = {
    "ethereum": Decimal("1"),
    "base": Decimal("1"),
    "bsc": Decimal("1"),
    "arbitrum": Decimal("1"),
    "polygon": Decimal("100"),
    "unichain": Decimal("1"),
    "optimism": Decimal("1"),
    "avalanche": Decimal("10"),
    "arc": Decimal("0.01"),
    "robinhood": Decimal("1"),
    "solana": Decimal("10"),
}

#: dex ids medidos de verdad en GeckoTerminal, para comprobar el normalizador.
REAL_DEX_IDS = [
    "uniswap_v2",
    "uniswap_v3",
    "uniswap-v3-base",
    "uniswap_v3_arbitrum",
    "uniswap_v3_polygon_pos",
    "uniswap-v4-ethereum",
    "uniswap-v4-base",
    "uniswap_v4_unichain",
    "pancakeswap_v2",
    "pancakeswap-v3-bsc",
    "pancakeswap-infinity-cl",
    "sushiswap",
    "sushiswap_v2_arbitrum",
    "aerodrome-base",
    "aerodrome-slipstream",
    "velodrome-v2",
    "raydium",
    "orca",
    "meteora",
    "quickswap_v3",
    "traderjoe-v2-2",
    "curve",
]


def base_token_for(chain_key: str):
    """El token con el que cotizar en esa red: el nativo envuelto si lo hay."""
    native = catalog.wrapped_native(chain_key)
    if native is not None:
        return native
    # Arc no tiene nativo envuelto: se cotiza su BTC envuelto contra USDC.
    extras = [t for t in catalog.tokens_for(chain_key) if t.symbol != "USDC"]
    return extras[0] if extras else None


async def quote_chain(container, chain_key: str) -> str:
    spec = chain(chain_key)
    base = base_token_for(chain_key)
    quote = catalog.quote_token(chain_key)
    if base is None or quote is None:
        return f"  {chain_key:<10} sin par en el catálogo"

    pair = TradingPair(base=base, quote=quote)
    amount = base.amount(SIZES[chain_key])
    try:
        comparison = await container.compare_prices(pair, amount)
    except NoQuotesError as error:
        return f"  {chain_key:<10} {pair.symbol:<14} SIN COTIZACIÓN — {error}"

    lines = [
        f"  {chain_key:<10} {pair.symbol:<14} {len(comparison.quotes)} venue(s), "
        f"spread {comparison.spread_bps}, "
        f"{'con estimaciones' if comparison.has_estimates else 'todo medido'}"
        f"{', comisión sin desglosar' if comparison.has_unknown_fees else ''}"
        f"   [{spec.name}, eip155={spec.eip155_id}]"
    ]
    for q in comparison.ranked[:4]:
        fee = "desconocida" if q.fee_bps is None else f"{q.fee_bps} ({q.fee_basis.value})"
        lines.append(
            f"       {q.venue.name:<26} {q.amount_in} → {q.amount_out}  "
            f"fee {fee}, impacto {q.price_impact_bps} "
            f"({q.impact_basis.value})"
        )
    return "\n".join(lines)


async def main() -> int:
    print("=" * 78)
    print("1) NORMALIZADOR DE PROTOCOLOS — dex ids reales medidos en la fuente")
    print("=" * 78)
    for dex_id in sorted(REAL_DEX_IDS):
        ref = parse_dex_id(dex_id)
        fee = f" fee={ref.constant_fee}" if ref.constant_fee is not None else ""
        print(
            f"  {dex_id:<28} → {ref.key:<22} «{ref.label}»  "
            f"{ref.fee_source.value}{fee}  cotizable={ref.is_quotable}"
        )

    print()
    print("=" * 78)
    print("2) REGISTRO DE REDES")
    print("=" * 78)
    for key, spec in CHAINS.items():
        tokens = ", ".join(t.symbol for t in catalog.tokens_for(key))
        print(
            f"  {key:<10} {spec.name:<18} nativo {spec.native_symbol}/"
            f"{spec.native_decimals} eip155={spec.eip155_id} "
            f"{spec.address_format.value:<14} tokens: {tokens}"
        )

    container = await build_container(Settings(mode=OperationMode.SIMULATION))
    try:
        print()
        print("=" * 78)
        print("3) COTIZACIONES REALES — motor geckoterminal (comisión medida)")
        print("=" * 78)
        for chain_key in CHAINS:
            print(await quote_chain(container, chain_key))

        print()
        print("=" * 78)
        print("4) COTIZACIONES REALES — motor dexscreener (reservas medidas)")
        print("=" * 78)
        await container.registry.activate("dexscreener")
        for chain_key in ("ethereum", "base", "bsc", "polygon", "solana"):
            print(await quote_chain(container, chain_key))

        print()
        print("=" * 78)
        print("5) COTIZACIONES REALES — motor jupiter (Solana, ejecución agregada)")
        print("=" * 78)
        await container.registry.activate("jupiter")
        print(await quote_chain(container, "solana"))
        # Una red que no es Solana: el motor debe decir que no, no reventar.
        print(await quote_chain(container, "ethereum"))
        # Tamaño grande: el impacto reportado tiene que crecer de verdad.
        sol = catalog.wrapped_native("solana")
        usdc = catalog.quote_token("solana")
        if sol is not None and usdc is not None:
            big = TradingPair(base=sol, quote=usdc)
            for size in (Decimal("10"), Decimal("5000"), Decimal("50000")):
                try:
                    comparison = await container.compare_prices(big, sol.amount(size))
                except NoQuotesError as error:
                    print(f"       {size:>8} WSOL  SIN COTIZACIÓN — {error}")
                    continue
                q = comparison.best
                print(
                    f"       {size:>8} WSOL → {q.amount_out}  "
                    f"impacto {q.price_impact_bps} ({q.impact_basis.value})  "
                    f"fee_is_known={q.fee_is_known} is_exact={q.is_exact}"
                )
                print(f"                {q.source_note}")

        print()
        print("=" * 78)
        print("6) BARRERA DE MODOS")
        print("=" * 78)
        observation = await build_container(Settings(mode=OperationMode.OBSERVATION))
        try:
            pair = TradingPair(
                base=catalog.wrapped_native("ethereum"),
                quote=catalog.quote_token("ethereum"),
            )
            await observation.scan_opportunities(pair, pair.base.amount(Decimal("1")))
            print("  FALLO: OBSERVACIÓN permitió calcular rutas")
            return 1
        except ModeNotPermittedError as error:
            print(f"  OK, bloqueado: {error}")
        finally:
            await observation.aclose()
    finally:
        await container.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
