# ── trader/config.py ──────────────────────────────────────────────────────────
# All tunable parameters. Edit this file to change behaviour.
# Never put secrets here — those go in GitHub Actions secrets.

# ── TRADING PARAMETERS ────────────────────────────────────────────────────────

# Fraction of WETH balance to allocate per new position.
#
# Raised from 0.10 because 10% of a 1 WETH book is a ~$245 position, and two
# mainnet swaps cost ~$6 in gas regardless of size — 2.6% round trip, against
# an average winner of ~6% arriving 30% of the time. The strategy was paying
# a third of its best case to the network on every trade. See docs/POSTMORTEM.md.
POSITION_SIZE_PCT = 0.25

# Maximum number of simultaneously open positions.
# 4 x 25% = fully deployed at four concurrent positions.
MAX_OPEN_POSITIONS = 4

# Simulated WETH balance used for position sizing in paper mode.
#
# This is not a cosmetic number. It sets the paper position size, which sets
# the modelled gas cost, which decides whether trades pass the viability gate.
# Set it to the amount you would actually deploy — if that is below roughly
# 2 WETH, the honest paper result on mainnet is "no trades", and that is the
# correct answer rather than a bug.
PAPER_WETH_BALANCE = 5.0

# Minimum confidence score (0-100) required to enter a trade
MIN_CONFIDENCE = 90

# Minimum R:R ratio required to enter a trade
MIN_RR = 2.0

# How close to the entry zone price must be to trigger entry (as a fraction).
# e.g. 0.01 means current price must be within 1% above the entry price.
# Prevents entering a trade when price is far above the computed entry.
ENTRY_TOLERANCE = 0.01

# Hours to wait before re-entering a symbol after a stop-loss fires.
# Scales linearly with the number of stops on that symbol in the past
# 7 days (1 stop = 24h, 2 stops = 48h, ...). Prevents repeatedly
# re-buying into the same falling market that triggered the stop.
STOP_COOLDOWN_HOURS = 24

# Block BUY entries entirely when the 7-day change is below this (%).
# Mean-reversion longs during a steep decline were the dominant
# historical loss source (5 consecutive ETH/USDC stops, May-Jun 2026).
MAX_7D_DECLINE_FOR_ENTRY = -8.0

# ── COST MODEL ────────────────────────────────────────────────────────────────
# Paper trading ran for five months with zero modelled costs. Every P&L figure
# it produced was gross, and the strategy's gross edge was smaller than the
# cost it was ignoring. These parameters make the cost explicit so both the
# entry gate and the paper ledger can account for it.

# Gas units for one Uniswap V3 exactInputSingle swap (observed range 130k-180k).
SWAP_GAS_UNITS = 160_000

# Fallback assumptions when live chain data is unavailable (paper mode, or an
# RPC failure). Overridden at runtime by the real gas price and ETH price.
ASSUMED_GAS_GWEI = 8.0
ASSUMED_ETH_USD  = 2400.0

# Price impact model: impact% ≈ IMPACT_COEFF × (notional / pool liquidity) × 100
# Crude by necessity — GeckoTerminal reports total reserve, not the tick
# distribution that actually determines V3 fill quality. Calibrated pessimistic.
IMPACT_COEFF   = 0.5
MAX_IMPACT_PCT = 5.0

# ── VIABILITY GATES ───────────────────────────────────────────────────────────
# A trade must be able to pay for itself. These are the gates that the previous
# three rounds of tuning never had: they reject a setup on economics rather
# than on indicator quality.

# Gas may not exceed this share of notional over the round trip (%). Because
# gas is a fixed dollar amount per swap, this is really a minimum position size
# in disguise — see costs.min_viable_notional_usd(). At 8 gwei and ETH ~$2,400
# a 0.5% ceiling implies roughly a $1,250 position.
MAX_GAS_COST_PCT = 0.5

# The move to target must be at least this multiple of the round-trip cost.
# At 3x, a 2% round trip demands a 6% target — which is roughly where this
# strategy's winners actually landed.
MIN_EDGE_COST_MULTIPLE = 3.0

# Absolute floor on the target move, regardless of modelled cost (%).
MIN_TARGET_MOVE_PCT = 4.0

# Pools thinner than this are untradeable at any size we would use. The three
# worst-performing symbols by cost-adjusted return (ARB, POL, AMP) all sat in
# pools between $24k and $67k.
MIN_POOL_LIQUIDITY_USD = 500_000

# Position may not exceed this share of pool reserve. Paired with the
# liquidity floor above this allows up to a $2,500 position in the thinnest
# tradeable pool, which sits above the gas-viability floor of ~$1,250.
MAX_POOL_SHARE = 0.005

# ── EXIT MANAGEMENT ───────────────────────────────────────────────────────────
# Historically the fixed take-profit at the nearest resistance capped winners
# at ~+6% while stops cut at -3%. All of the profit in the only marginally
# positive period came from three trades that ran past their target. Capping
# them is what made the win-rate arithmetic impossible.

# On reaching the target, arm a trailing stop instead of selling.
TRAIL_ENABLED = True

# Trailing stop distance below the high-water mark, in ATRs.
TRAIL_ATR_MULT = 2.0

# Floor on the trailing distance as a fraction of price, so the trail is not
# placed inside normal hourly noise.
TRAIL_MIN_PCT = 0.02

# Once the trail is armed, the stop never sits below entry plus this multiple
# of the round-trip cost — a trade that reached its target cannot end a loser.
BREAKEVEN_COST_MULTIPLE = 1.5

# Hard time stop. Generous by design: the single best trade in the record took
# ten days to mature. This is a safety valve against a position being forgotten
# (which happened for 60 days in March-May 2026), not an active exit tool.
MAX_HOLD_HOURS = 504  # 21 days

# ── CIRCUIT BREAKER ───────────────────────────────────────────────────────────
# The system traded continuously through a -20% drawdown across five months
# without ever pausing. It had no mechanism to notice it was losing.

# Stop opening new positions when net expectancy over the last N closed trades
# is negative. Open positions continue to be managed normally.
BREAKER_ENABLED       = True
BREAKER_LOOKBACK      = 15
BREAKER_MIN_TRADES    = 10
BREAKER_MIN_EXPECTANCY = 0.0

# ── SIGNAL ANALYSIS PARAMETERS ───────────────────────────────────────────────

# RSI period
RSI_PERIOD = 14

# Bollinger Band period and multiplier
BB_PERIOD = 20
BB_MULT   = 2.0

# ATR period (used for stop loss sizing)
ATR_PERIOD = 14

# ATR multiplier for stop loss (stop = current_price - ATR * ATR_STOP_MULT)
ATR_STOP_MULT = 2.5

# Minimum stop distance as a fraction of current price.
# Prevents stops being placed inside normal market noise.
MIN_STOP_PCT = 0.025

# Support/resistance local window (candles either side)
SR_WINDOW = 4

# Support/resistance cluster threshold (2 levels within X% are merged)
SR_CLUSTER_THR = 0.015

# Minimum entry/exit spread as multiple of ATR (prevents entry==exit collapse)
MIN_SPREAD_ATR = 1.0

# Number of hourly candles to fetch (168 = 7 days)
OHLCV_LIMIT = 168

# ── PAIRS ─────────────────────────────────────────────────────────────────────
# Token contract addresses on Ethereum mainnet (permanent — never change).
# Pool addresses are resolved at runtime by querying GeckoTerminal for the
# highest-liquidity WETH-paired pool.

WETH_ADDRESS = '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'
USDC_ADDRESS = '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48'

PAIRS = [
    {'symbol': 'ETH/USDC', 'category': 'Base',       'token_address': WETH_ADDRESS,                                    'pair_token': USDC_ADDRESS, 'quote_is_usd': True},
    {'symbol': 'ARB/ETH',  'category': 'L2',          'token_address': '0xB50721BCf8d664c30412Cfbc6cf7a15145234ad1',   'quote_is_usd': False},
    {'symbol': 'LINK/ETH', 'category': 'Oracle',      'token_address': '0x514910771AF9Ca656af840dff83E8264EcF986CA',   'quote_is_usd': False},
    {'symbol': 'UNI/ETH',  'category': 'DeFi',        'token_address': '0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984',   'quote_is_usd': False},
    {'symbol': 'AAVE/ETH', 'category': 'DeFi',        'token_address': '0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9',   'quote_is_usd': False},
    {'symbol': 'POL/ETH',  'category': 'L2',          'token_address': '0x455e53CBB86018Ac2B8092FdCd39d8444aFFC3F6',   'quote_is_usd': False},
    {'symbol': 'LDO/ETH',  'category': 'LST',         'token_address': '0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32',   'quote_is_usd': False},
    {'symbol': 'AMP/ETH',  'category': 'Collateral',  'token_address': '0xfF20817765cB7f73d4bde2e66e067E58d11095C2',   'quote_is_usd': False},
]

# ── UNISWAP V3 ────────────────────────────────────────────────────────────────

# Uniswap V3 UniversalRouter on Ethereum mainnet
UNISWAP_UNIVERSAL_ROUTER = '0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD'

# Slippage tolerance for swaps (0.005 = 0.5%)
SLIPPAGE_TOLERANCE = 0.005

# Maximum time a submitted transaction can stay pending (seconds)
TX_DEADLINE_SECONDS = 180

# ── GECKOTERMINAL ─────────────────────────────────────────────────────────────

GT_BASE    = 'https://api.geckoterminal.com/api/v2'
GT_HEADERS = {'Accept': 'application/json;version=20230302'}

# Seconds to wait between GeckoTerminal requests (respects ~30 req/min free tier)
GT_REQUEST_DELAY = 2.5

# ── STATE FILE ────────────────────────────────────────────────────────────────

# Written to repo root — readable by anyone inspecting the Actions run
SIGNALS_FILE   = 'signals.json'
POSITIONS_FILE = 'positions.json'
TRADES_LOG     = 'trades.log'
