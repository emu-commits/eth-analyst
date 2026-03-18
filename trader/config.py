# ── trader/config.py ──────────────────────────────────────────────────────────
# All tunable parameters. Edit this file to change behaviour.
# Never put secrets here — those go in GitHub Actions secrets.

# ── TRADING PARAMETERS ────────────────────────────────────────────────────────

# Fraction of WETH balance to allocate per new position (0.10 = 10%)
POSITION_SIZE_PCT = 0.10

# Maximum number of simultaneously open positions
MAX_OPEN_POSITIONS = 8

# Minimum confidence score (0-100) required to enter a trade
MIN_CONFIDENCE = 80

# Minimum R:R ratio required to enter a trade
MIN_RR = 1.5

# How close to the entry zone price must be to trigger entry (as a fraction).
# e.g. 0.02 means current price must be within 2% above the entry price.
# Prevents entering a trade when price is far above the computed entry.
ENTRY_TOLERANCE = 0.02

# ── SIGNAL ANALYSIS PARAMETERS ───────────────────────────────────────────────

# RSI period
RSI_PERIOD = 14

# Bollinger Band period and multiplier
BB_PERIOD = 20
BB_MULT   = 2.0

# ATR period (used for stop loss sizing)
ATR_PERIOD = 14

# ATR multiplier for stop loss (stop = entry - ATR * ATR_STOP_MULT)
ATR_STOP_MULT = 1.5

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
