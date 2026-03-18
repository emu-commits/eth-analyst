# ── trader/gt_client.py ───────────────────────────────────────────────────────
# GeckoTerminal API client.
# Handles pool resolution (token address → best live pool) and OHLCV fetching.
# This is the Python equivalent of resolvePool() + fetchOHLCV() in the HTML tool.
#
# Pool resolution results are cached in pool_cache.json for 24 hours.
# This cuts GeckoTerminal requests from 16/run to 8/run after the first run,
# staying well within the free tier rate limit.

import sys
import time
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from . import config

# ── POOL CACHE ────────────────────────────────────────────────────────────────
# Stores resolved pool addresses so we don't re-query GeckoTerminal every hour.
# Format: { token_address: { pool_address, dex, currency, liquidity_usd, cached_at } }

POOL_CACHE_FILE = 'pool_cache.json'
POOL_CACHE_TTL  = timedelta(hours=24)  # re-resolve once per day

def _load_pool_cache() -> dict:
    try:
        return json.loads(Path(POOL_CACHE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_pool_cache(cache: dict):
    Path(POOL_CACHE_FILE).write_text(json.dumps(cache, indent=2))

def _cache_is_fresh(entry: dict) -> bool:
    try:
        cached_at = datetime.fromisoformat(entry['cached_at'])
        return datetime.now(timezone.utc) - cached_at < POOL_CACHE_TTL
    except (KeyError, ValueError):
        return False

# ── SESSION ───────────────────────────────────────────────────────────────────
_session = requests.Session()
_session.headers.update(config.GT_HEADERS)
_last_request_time = 0.0


def _gt_get(path: str) -> dict:
    """Rate-limited GET to GeckoTerminal. Raises on non-200."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < config.GT_REQUEST_DELAY:
        time.sleep(config.GT_REQUEST_DELAY - elapsed)

    url = config.GT_BASE + path
    resp = _session.get(url, timeout=15)
    _last_request_time = time.time()

    if resp.status_code == 429:
        # Back off and retry once — use stderr so it doesn't pollute
        # the run_output.txt summary captured for email
        print(f"  [GT] Rate limited on {path[:60]} — waiting 30s", file=sys.stderr)
        time.sleep(30)
        resp = _session.get(url, timeout=15)
        _last_request_time = time.time()
        if resp.status_code == 429:
            raise RuntimeError(f"Rate limited twice on {path[:60]} — aborting pair")

    resp.raise_for_status()
    return resp.json()


def resolve_pool(pair: dict, force_refresh: bool = False) -> dict:
    """
    Given a pair config (with token_address), find the highest-liquidity
    live pool on Ethereum mainnet paired against WETH (or USDC for ETH/USDC).

    Results are cached in pool_cache.json for 24 hours to minimise API calls.
    Pass force_refresh=True to bypass the cache (e.g. after a failed OHLCV fetch).

    Returns:
        {
            'pool_address': str,
            'dex':          str,
            'currency':     'usd' | 'token',
            'liquidity_usd': float,
        }
    """
    token_addr = pair['token_address'].lower()

    # ── Check cache first ─────────────────────────────────────────────────────
    if not force_refresh:
        cache = _load_pool_cache()
        entry = cache.get(token_addr)
        if entry and _cache_is_fresh(entry):
            return {
                'pool_address':  entry['pool_address'],
                'dex':           entry['dex'],
                'currency':      entry['currency'],
                'liquidity_usd': entry.get('liquidity_usd', 0),
                'from_cache':    True,
            }
    data  = _gt_get(f'/networks/eth/tokens/{token_addr}/pools?page=1')
    pools = data.get('data', [])

    if not pools:
        raise ValueError(f"No pools found for token {token_addr}")

    # Determine target pair token
    target = (config.USDC_ADDRESS if pair.get('quote_is_usd')
              else config.WETH_ADDRESS).lower()

    def has_target(pool):
        rel  = pool.get('relationships', {})
        ids  = [
            rel.get('base_token',  {}).get('data', {}).get('id', ''),
            rel.get('quote_token', {}).get('data', {}).get('id', ''),
        ]
        return any(target in i.lower() for i in ids)

    matched = [p for p in pools if has_target(p)]
    candidates = matched if matched else pools

    # Sort by liquidity descending — pick the deepest pool
    candidates.sort(
        key=lambda p: float(p.get('attributes', {}).get('reserve_in_usd') or 0),
        reverse=True,
    )

    best = candidates[0]
    attr = best.get('attributes', {})
    pool_address = attr.get('address')
    if not pool_address:
        raise ValueError(f"Could not determine pool address for {pair['symbol']}")

    # DEX name from relationship id (e.g. "uniswap_v3" → "Uniswap V3")
    dex_id  = (best.get('relationships', {})
                   .get('dex', {})
                   .get('data', {})
                   .get('id', ''))
    dex     = ' '.join(w.capitalize() for w in dex_id.split('_')) or 'Unknown DEX'
    liq_usd = float(attr.get('reserve_in_usd') or 0)

    # currency: 'usd' for WETH/USDC pools, 'token' for TOKEN/WETH pools
    currency = 'usd' if pair.get('quote_is_usd') else 'token'

    result = {
        'pool_address':  pool_address,
        'dex':           dex,
        'currency':      currency,
        'liquidity_usd': liq_usd,
        'from_cache':    False,
    }

    # ── Write to cache ────────────────────────────────────────────────────────
    cache = _load_pool_cache()
    cache[token_addr] = {
        'pool_address':  pool_address,
        'dex':           dex,
        'currency':      currency,
        'liquidity_usd': liq_usd,
        'cached_at':     datetime.now(timezone.utc).isoformat(),
    }
    _save_pool_cache(cache)

    return result


def fetch_ohlcv(pool_address: str, currency: str = 'token') -> list[dict]:
    """
    Fetch 7-day hourly OHLCV candles for a pool.

    Returns list of dicts: [{open, high, low, close, volume}, ...]
    Oldest candle first.
    """
    path = (f'/networks/eth/pools/{pool_address}/ohlcv/hour'
            f'?aggregate=1&limit={config.OHLCV_LIMIT}&currency={currency}')
    data = _gt_get(path)
    raw  = data.get('data', {}).get('attributes', {}).get('ohlcv_list', [])

    if len(raw) < 20:
        raise ValueError(
            f"Insufficient OHLCV data for pool {pool_address} "
            f"({len(raw)} candles, need ≥20)"
        )

    # GeckoTerminal returns newest-first → reverse to oldest-first
    candles = [
        {'open': float(c[1]), 'high': float(c[2]),
         'low':  float(c[3]), 'close': float(c[4]),
         'volume': float(c[5])}
        for c in reversed(raw)
    ]
    return candles


def fetch_current_price(pool_address: str, currency: str = 'token') -> float:
    """
    Fetch just the current price for a pool.
    Uses the most recent OHLCV candle close — lighter than a separate ticker call.
    """
    candles = fetch_ohlcv(pool_address, currency)
    return candles[-1]['close']
