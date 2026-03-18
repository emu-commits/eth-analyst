# ── trader/signals.py ─────────────────────────────────────────────────────────
# Signal generation logic — single source of truth shared by the runner and
# (in future) the HTML tool.
#
# This is the Python equivalent of the indicator + analyze functions in
# eth-analyzer.html. Keep them in sync if you change the scoring logic.

import math
from . import config


# ── INDICATORS ────────────────────────────────────────────────────────────────

def calc_rsi(closes: list[float], period: int = None) -> float | None:
    period = period or config.RSI_PERIOD
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + (d if d > 0 else 0)) / period
        avg_loss = (avg_loss * (period - 1) + (-d if d < 0 else 0)) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def calc_bollinger(closes: list[float], period: int = None,
                   mult: float = None) -> dict | None:
    period = period or config.BB_PERIOD
    mult   = mult   or config.BB_MULT
    if len(closes) < period:
        return None
    sl   = closes[-period:]
    mean = sum(sl) / period
    std  = math.sqrt(sum((x - mean) ** 2 for x in sl) / period)
    return {'upper': mean + mult * std, 'middle': mean, 'lower': mean - mult * std}


def calc_vwap(candles: list[dict]) -> float:
    tpv, vol = 0.0, 0.0
    for c in candles:
        tp   = (c['high'] + c['low'] + c['close']) / 3
        tpv += tp * c['volume']
        vol += c['volume']
    return tpv / vol if vol > 0 else candles[-1]['close']


def calc_atr(candles: list[dict], period: int = None) -> float:
    period = period or config.ATR_PERIOD
    trs = []
    for i in range(1, len(candles)):
        c    = candles[i]
        prev = candles[i - 1]
        trs.append(max(
            c['high'] - c['low'],
            abs(c['high'] - prev['close']),
            abs(c['low']  - prev['close']),
        ))
    recent = trs[-period:]
    return sum(recent) / len(recent)


def _cluster_levels(levels: list[float],
                    threshold: float = None) -> list[float]:
    threshold = threshold or config.SR_CLUSTER_THR
    if not levels:
        return []
    sorted_lvls = sorted(levels)
    clusters    = []
    group       = [sorted_lvls[0]]
    for lvl in sorted_lvls[1:]:
        if group[0] > 0 and (lvl - group[0]) / group[0] < threshold:
            group.append(lvl)
        else:
            clusters.append(sum(group) / len(group))
            group = [lvl]
    clusters.append(sum(group) / len(group))
    return clusters


def find_support_resistance(candles: list[dict]) -> dict:
    w = config.SR_WINDOW
    supports, resistances = [], []
    for i in range(w, len(candles) - w):
        near = candles[i - w: i + w + 1]
        if candles[i]['low']  == min(c['low']  for c in near):
            supports.append(candles[i]['low'])
        if candles[i]['high'] == max(c['high'] for c in near):
            resistances.append(candles[i]['high'])
    return {
        'supports':    _cluster_levels(supports),
        'resistances': _cluster_levels(resistances),
    }


# ── SIGNAL SCORER ─────────────────────────────────────────────────────────────

def generate_signal(candles: list[dict], pair: dict,
                    pool_address: str, dex: str) -> dict:
    """
    Run all indicators on candle data and return a complete signal dict.

    The returned dict is what gets written to signals.json and what the
    trader reads to decide whether to enter/exit/stop a position.
    """
    closes  = [c['close'] for c in candles]
    current = closes[-1]
    oldest  = closes[0]
    change_7d = (current - oldest) / oldest * 100

    rsi  = calc_rsi(closes)
    boll = calc_bollinger(closes)
    vwap = calc_vwap(candles)
    atr  = calc_atr(candles)
    sr   = find_support_resistance(candles)

    # ── Entry / exit / stop ───────────────────────────────────────────────────
    sup_below = [s for s in sr['supports']    if s <= current * 1.02]
    res_above = [r for r in sr['resistances'] if r >= current * 0.98]

    entry = (max(sup_below) if sup_below
             else (boll['lower'] if boll else current * 0.95))
    exit_ = (min(res_above) if res_above
             else (boll['upper'] if boll else current * 1.08))

    if boll:
        if boll['lower'] < current and boll['lower'] > entry * 0.98:
            entry = (entry + boll['lower']) / 2
        if boll['upper'] > current and boll['upper'] < exit_ * 1.02:
            exit_ = (exit_ + boll['upper']) / 2

    if rsi and rsi > 50:
        entry *= 0.99

    # Enforce minimum spread of 1× ATR between entry and exit
    if (exit_ - entry) < atr * config.MIN_SPREAD_ATR:
        entry -= atr * 0.5
        exit_ += atr * 0.5
    if exit_ <= entry:
        exit_ = entry * 1.03

    stop  = entry - atr * config.ATR_STOP_MULT
    risk  = max(entry - stop, 1e-18)
    rr    = (exit_ - entry) / risk

    # ── Scoring ───────────────────────────────────────────────────────────────
    score    = 0
    signals  = []
    rsi_sig  = 'neutral'
    boll_sig = 'neutral'
    vwap_sig = 'neutral'

    if rsi is not None:
        if   rsi < 30: score += 2; rsi_sig = 'bullish'; signals.append('RSI oversold')
        elif rsi < 45: score += 1; rsi_sig = 'bullish'
        elif rsi > 70: score -= 2; rsi_sig = 'bearish'; signals.append('RSI overbought')
        elif rsi > 60: score -= 1; rsi_sig = 'bearish'

    if boll:
        pct = (current - boll['lower']) / (boll['upper'] - boll['lower'])
        if   pct < 0.20: score += 2; boll_sig = 'bullish'; signals.append('Near BB lower')
        elif pct < 0.35: score += 1; boll_sig = 'bullish'
        elif pct > 0.80: score -= 2; boll_sig = 'bearish'; signals.append('Near BB upper')
        elif pct > 0.65: score -= 1; boll_sig = 'bearish'

    if   current < vwap * 0.99: score += 1; vwap_sig = 'bullish'; signals.append('Below VWAP')
    elif current > vwap * 1.01: score -= 1; vwap_sig = 'bearish'

    if   change_7d < -8:  score += 1; signals.append('7D dip')
    elif change_7d > 15:  score -= 1

    # Determine verdict
    verdict = 'BUY' if score >= 3 else 'SELL' if score <= -2 else 'HOLD'

    # Downgrade if R:R too thin
    if rr < 0.5 and verdict != 'HOLD':
        verdict = 'HOLD'
        signals.append('R:R too thin — downgraded')

    confidence = min(100, max(0, 50 + score * 10))

    return {
        'symbol':        pair['symbol'],
        'category':      pair['category'],
        'pool_address':  pool_address,
        'token_address': pair['token_address'],
        'dex':           dex,
        'quote_is_usd':  pair.get('quote_is_usd', False),
        'current_price': round(current, 8),
        'change_7d_pct': round(change_7d, 3),
        'entry':         round(entry, 8),
        'exit':          round(exit_, 8),
        'stop_loss':     round(stop, 8),
        'rr_ratio':      round(rr, 3),
        'verdict':       verdict,
        'confidence':    confidence,
        'rsi14':         round(rsi, 2) if rsi is not None else None,
        'bb_pct':        round((current - boll['lower']) / (boll['upper'] - boll['lower']) * 100, 1) if boll else None,
        'vwap_delta_pct': round((current - vwap) / vwap * 100, 3) if vwap else None,
        'rsi_signal':    rsi_sig,
        'boll_signal':   boll_sig,
        'vwap_signal':   vwap_sig,
        'signals':       signals,
        'atr':           round(atr, 8),
    }
