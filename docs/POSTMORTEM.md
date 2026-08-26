# ETH Trader — post-mortem, March–August 2026

Five months of hourly paper trading, 62 entries, 62 closed trades, and three
rounds of "tighten the strategy" fixes. This documents what the record actually
shows, why the previous three interventions did not work, and what changes in
response.

## The record

| | |
|---|---|
| Period | 2026-03-18 → 2026-08-26 |
| Entries | 62 |
| Closed trades | 62 |
| Win rate | 30.6% (19W / 43L) |
| Average winner | +4.40% |
| Average loser | −2.43% |
| **Gross P&L, sum** | **−20.65%** |
| **Gross expectancy** | **−0.33% per trade** |

Every one of those figures is **gross**. Paper mode modelled no LP fee, no gas
and no price impact, so the P&L recorded was the raw price delta between entry
and exit. That single omission is the reason five months produced no usable
information.

By exit type:

| Exit | n | Avg | Sum |
|---|---|---|---|
| Target hit | 14 | +5.90% | +82.64% |
| Stopped out | 48 | −2.15% | −103.29% |

By symbol, worst to best: AAVE −24.98%, UNI −8.93%, ETH/USDC −7.14%,
POL −6.73%, AMP −4.79%, ARB +5.93%, LINK +9.90%, LDO +16.09%.

## The three previous interventions

**1. 2026-05-10 — `fix: cast entry_price to float`.** A `TypeError` in
`check_exit_or_stop` had been aborting position monitoring mid-run since
mid-March, masked by a `tee` pipe in the workflow. Five positions sat open for
up to 60 days with their stops and targets doing nothing. This was a real bug
and the fix was correct.

**2. 2026-05-26 — `improve: tighten signal quality`.** Stops were being anchored
to the computed entry zone rather than the actual fill, so logged R:R had no
relationship to the trade taken: median real stop distance was 0.59%, and 13 of
45 entries had a stop *at or above* the entry price — guaranteed to trigger on
the next candle. The fix re-anchored the stop, added a 2.5% minimum stop
distance, and raised `MIN_CONFIDENCE` 80→90 and `MIN_RR` 1.5→2.0.

The geometry fix was right. The threshold raises were not: expectancy went from
−0.33%/trade to **−1.33%/trade** over the next 9 trades.

**3. 2026-06-11 — `strategy: add crash veto and post-stop cooldown`.** The
ETH/USDC sequence had stopped out 15 times, repeatedly re-buying within the same
run at the same price — a stop and a re-entry sharing a timestamp is a wash
trade that in live mode pays a full round trip for no change in position. The
cooldown and the crash veto both work and both remain.

That period (post-June-11, 12 closed trades) is the only one with positive gross
expectancy: **+0.41%/trade**. But all of its profit came from three trades that
ran past their target — +14.66%, +9.83%, +7.48% — and 9 of its 12 trades still
stopped out.

## Why it kept failing

**The costs were never in the numbers.** Two Uniswap V3 swaps cost roughly $6.30
in gas at 8 gwei with ETH near $2,400, regardless of trade size. At the
configured position size — 10% of a 1 WETH book, about $245 — that is **2.56%
round trip in gas alone**, plus 0.60% in LP fees, plus price impact. Call it
3.2% on the deepest pool in the list and 4.2% on the thinnest.

Set that against the strategy's own numbers: a +4.40% average winner arriving
30.6% of the time. The cost of participating was consuming roughly three
quarters of the average winning trade. No amount of indicator tuning reaches
across that gap, and all three previous interventions were indicator tuning.

**Three of the eight pairs were untradeable at any size.** ARB/WETH holds about
$24k of liquidity, POL/WETH $35k, AMP/WETH $67k on Sushiswap. A $245 order is
1% of the ARB pool's entire reserve. Those three symbols contributed −5.6% gross
and considerably worse net.

**Winners were capped, losers were not.** The fixed take-profit sold at the
nearest resistance, averaging +5.90%, while stops cut at −2.15%. That 2.7:1
payoff requires a 27% win rate to break even gross, against a realised 30.6% —
so the strategy was hovering at gross breakeven by construction, with no margin
for cost. Meanwhile the only real profit in the whole record came from the
handful of trades that ran well past where a fixed target would have sold them.

**Nothing ever looked at the results.** The bot traded continuously through a
five-month, −20% gross drawdown. There was no mechanism by which a losing
configuration could stop itself.

## What changed

- **`trader/costs.py`** — an explicit round-trip cost model: LP fee, gas, and a
  pessimistic price-impact estimate, each charged on both legs.
- **Paper P&L is now net.** `trades.log` records `pnl_pct` (net), plus
  `pnl_pct_gross` and `cost_pct`. `performance.html` shows gross, costs and net
  side by side so the gap stays visible.
- **Viability gates.** A trade is rejected unless the move to target clears the
  round-trip cost by 3×, the pool holds at least $500k, the position is under
  0.5% of pool reserve, and gas is under 0.5% of notional. That last gate names
  the required position size in its rejection message rather than silently
  refusing forever.
- **Position size raised** 10% → 25% of book, with a configurable paper balance,
  because at the old size the gas gate can never be satisfied.
- **Trailing stop replaces the fixed take-profit.** Reaching the target arms a
  trail and ratchets the stop to breakeven-plus-costs instead of selling.
- **Circuit breaker.** New entries suspend when net expectancy over the last 15
  closed trades is negative. Against the current log it trips immediately:
  −0.47%/trade over the last 15 trades, 3 wins of 15.
- **Live-mode `amountOutMinimum` fixed.** It was computed in the input token's
  units. Live entries would have had no slippage protection at all, and live
  exits would have reverted every time — positions would have been unsellable
  while their stops sat there doing nothing. Now quoted through QuoterV2.

## Honest assessment

The changes above make the system measure itself correctly and refuse trades it
cannot win. They do not manufacture an edge, and none of them has been validated
out of sample — `ohlcv_cache.json` is empty, so there is no candle history in
this repo to backtest against. The trailing-stop change in particular is a
judgment call motivated by three trades.

What the arithmetic does say clearly:

**On Ethereum mainnet, this strategy needs a position of at least ~$1,250 to
keep gas under 0.5% round trip, which means roughly $10k of working capital at
four concurrent positions.** Below that, the correct paper result is "no trades",
and the circuit breaker plus the sizing gate will now produce exactly that
instead of quietly losing money.

Three ways forward, in order of how much they address the actual problem:

1. **Move to an L2** (Base, Arbitrum). Gas per swap drops from ~$3 to well under
   a cent, which removes the dominant cost term and the minimum-size constraint
   with it. The same code, indicators and GeckoTerminal integration work
   unchanged — only `PAIRS`, the router address and the network slug change.
   This is the highest-leverage change available and it is mostly configuration.
2. **Fund it properly on mainnet** at ~$10k+ and trade only ETH/USDC, LINK, UNI
   and AAVE. Viable arithmetically, but it stakes real capital on an edge that
   has never been demonstrated net of costs in any period.
3. **Drop execution, keep the analysis.** The signal generation, pool
   resolution, dashboard and notification pipeline all work well. As a
   monitoring and alerting tool it has value today; as an automated trader it
   has produced 62 losing trades and no evidence of edge.

Recommendation: **(1), and stay in paper mode until net expectancy is positive
over at least 30 closed trades.** The circuit breaker now enforces the second
half of that automatically.
