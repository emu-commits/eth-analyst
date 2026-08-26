# ── trader/costs.py ───────────────────────────────────────────────────────────
# Transaction cost model.
#
# Why this file exists
# --------------------
# For the first five months of this project, paper trading recorded raw price
# deltas as P&L: no LP fee, no gas, no price impact. Every performance number
# — trades.log, performance.html, and the three rounds of "tighten the strategy"
# commits that were justified by them — was a gross number.
#
# The strategy's measured gross edge over that period was -0.33%/trade. The
# round trip on a $250 mainnet swap costs 1.5-4%. Nothing could be learned from
# a paper log that omitted a cost larger than the entire signal.
#
# Everything here is an estimate. The point is not precision, it is that the
# number is no longer zero and no longer invisible.

from . import config


# ── COMPONENTS ────────────────────────────────────────────────────────────────

def lp_fee_pct(pool_fee: int) -> float:
    """
    Uniswap V3 pool fee for one leg, as a percentage.
    pool_fee is in hundredths of a bip: 3000 → 0.3%.
    """
    return pool_fee / 10_000.0


def gas_cost_pct(notional_usd: float, gas_price_gwei: float,
                 eth_usd: float) -> float:
    """
    Gas cost of one swap as a percentage of the position notional.

    This is the term that makes small positions unviable. A swap costs the
    same in gas whether it moves $250 or $250,000, so the percentage cost
    scales inversely with position size — a $250 position at 10 gwei pays
    roughly 1.5% in gas per leg, which is more than the strategy's entire
    average winner divided over its trade count.
    """
    if notional_usd <= 0:
        return 0.0
    gas_eth = config.SWAP_GAS_UNITS * gas_price_gwei * 1e-9
    gas_usd = gas_eth * eth_usd
    return gas_usd / notional_usd * 100.0


def price_impact_pct(notional_usd: float, liquidity_usd: float) -> float:
    """
    Rough price impact of one leg, as a percentage.

    Modelled as notional/liquidity scaled by IMPACT_COEFF. This is deliberately
    crude — real V3 impact depends on how liquidity is distributed across ticks
    around the current price, which GeckoTerminal's reserve figure does not
    tell us. It is calibrated to be pessimistic on thin pools, which is where
    it matters: a $250 trade against the $24k ARB/WETH pool is 1% of the whole
    reserve and will not fill anywhere near the quoted mid price.
    """
    if liquidity_usd <= 0:
        return config.MAX_IMPACT_PCT
    raw = notional_usd / liquidity_usd * 100.0 * config.IMPACT_COEFF
    return min(raw, config.MAX_IMPACT_PCT)


# ── ROUND TRIP ────────────────────────────────────────────────────────────────

def round_trip_cost(pool_fee: int, liquidity_usd: float,
                    notional_usd: float,
                    gas_price_gwei: float = None,
                    eth_usd: float = None) -> dict:
    """
    Total estimated cost of opening and closing one position, as a percentage
    of notional, broken down by source.

    Both legs are charged: two LP fees, two lots of gas, two lots of impact.
    Returns a dict so the breakdown can be logged and stored on the trade —
    when a future run asks "why did nothing trade this week", the answer is
    in these numbers rather than in a shrug.
    """
    gas_price_gwei = config.ASSUMED_GAS_GWEI if gas_price_gwei is None else gas_price_gwei
    eth_usd        = config.ASSUMED_ETH_USD  if eth_usd        is None else eth_usd

    fee    = lp_fee_pct(pool_fee) * 2
    gas    = gas_cost_pct(notional_usd, gas_price_gwei, eth_usd) * 2
    impact = price_impact_pct(notional_usd, liquidity_usd) * 2
    total  = fee + gas + impact

    return {
        'lp_fee_pct':   round(fee, 4),
        'gas_pct':      round(gas, 4),
        'impact_pct':   round(impact, 4),
        'total_pct':    round(total, 4),
        'notional_usd': round(notional_usd, 2),
        'liquidity_usd': round(liquidity_usd, 2),
        'gas_gwei':     gas_price_gwei,
        'eth_usd':      eth_usd,
    }


# ── VIABILITY ─────────────────────────────────────────────────────────────────

def min_viable_notional_usd(gas_price_gwei: float = None,
                            eth_usd: float = None,
                            max_gas_pct: float = None) -> float:
    """
    Smallest position (in USD) at which gas stays under max_gas_pct of notional
    for the round trip.

    Gas is a fixed dollar amount per swap, so its percentage cost is inversely
    proportional to position size. This is the single hardest constraint on the
    strategy and it is pure arithmetic, not market opinion: at 8 gwei with ETH
    near $2,400, two swaps cost roughly $6.30, which is 2.6% of a $245 position
    and 0.25% of a $2,500 one. The strategy's average winner is around +6%.
    A 2.6% round-trip gas bill against a 6% winner that arrives 30% of the time
    cannot produce a positive expectancy at any win rate.

    There are exactly two ways out: trade a bigger position, or trade somewhere
    gas is not denominated in dollars. See docs/POSTMORTEM.md.
    """
    gas_price_gwei = config.ASSUMED_GAS_GWEI if gas_price_gwei is None else gas_price_gwei
    eth_usd        = config.ASSUMED_ETH_USD  if eth_usd        is None else eth_usd
    max_gas_pct    = config.MAX_GAS_COST_PCT if max_gas_pct    is None else max_gas_pct

    if max_gas_pct <= 0:
        return float('inf')
    gas_usd_round_trip = config.SWAP_GAS_UNITS * gas_price_gwei * 1e-9 * eth_usd * 2
    return gas_usd_round_trip / (max_gas_pct / 100.0)
