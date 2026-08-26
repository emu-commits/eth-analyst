# ── trader/trader.py ──────────────────────────────────────────────────────────
# Main hourly runner.
# Called by GitHub Actions on a cron schedule.
#
# Flow:
#   1. For each pair: resolve best pool → fetch OHLCV → generate signal
#   2. Write signals.json to repo (committed back by Actions workflow)
#   3. Load positions.json (current open positions)
#   4. Check entry conditions for signals without open positions
#   5. Check exit/stop conditions for open positions
#   6. Execute trades (paper or live)
#   7. Update positions.json
#   8. Append to trades.log
#   9. Print summary (captured by Actions → email notification)

import os
import sys
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python -m trader.trader` from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from trader import config
from trader.gt_client import resolve_pool, fetch_ohlcv, fetch_current_price, _load_pool_cache_stale
from trader.signals import generate_signal, fmt_price
from trader.costs import round_trip_cost, min_viable_notional_usd
from trader.executor import Executor

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
logger = logging.getLogger(__name__)

# ── PAPER / LIVE MODE ─────────────────────────────────────────────────────────
# Set PAPER_TRADING=false in GitHub Actions secrets/env to go live.
PAPER = os.environ.get('PAPER_TRADING', 'true').lower() != 'false'


# ── STATE HELPERS ─────────────────────────────────────────────────────────────

def load_json(path: str, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data):
    Path(path).write_text(json.dumps(data, indent=2))


def append_trade_log(entry: dict):
    with open(config.TRADES_LOG, 'a') as f:
        f.write(json.dumps(entry) + '\n')


# ── POOL FEE DETECTION ────────────────────────────────────────────────────────
# GeckoTerminal pool names include the fee tier (e.g. "LINK / WETH 0.3%").
# We extract it so the swap router uses the correct pool.

def fee_from_dex_name(dex: str) -> int:
    """Parse fee tier from dex name string. Default to 3000 (0.3%)."""
    if '0.05' in dex or '0.05%' in dex:
        return 500
    if '1%' in dex or '1.0%' in dex:
        return 10000
    return 3000  # default: 0.3%


# ── STOP COOLDOWN ─────────────────────────────────────────────────────────────

def stop_cooldowns(now: datetime) -> dict:
    """
    Map of symbol → cooldown-expiry datetime, built from recent stop-losses
    in trades.log.

    A symbol that stopped out may not be re-entered for STOP_COOLDOWN_HOURS,
    multiplied by the number of stops on that symbol in the past 7 days
    (1 stop = 24h, 2 stops = 48h, ...). This breaks the stop→re-buy loop
    where the same falling market that triggered the stop immediately
    regenerates a BUY signal.
    """
    from datetime import timedelta

    week_ago = now - timedelta(days=7)
    stops: dict[str, list[datetime]] = {}
    try:
        with open(config.TRADES_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if t.get('action') != 'stop':
                    continue
                ts = datetime.fromisoformat(t['timestamp'])
                if week_ago <= ts <= now:
                    stops.setdefault(t['symbol'], []).append(ts)
    except FileNotFoundError:
        return {}

    cooldowns = {}
    for sym, times in stops.items():
        last = max(times)
        cooldowns[sym] = last + timedelta(hours=config.STOP_COOLDOWN_HOURS * len(times))
    return cooldowns


# ── TRADE LEDGER ──────────────────────────────────────────────────────────────

def closed_trades() -> list[dict]:
    """Every exit/stop record in trades.log, oldest first."""
    out = []
    try:
        with open(config.TRADES_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if t.get('action') in ('exit', 'stop'):
                    out.append(t)
    except FileNotFoundError:
        return []
    return out


def net_pnl(trade: dict) -> float:
    """
    Net P&L of a closed trade.

    Records written before the cost model existed carry only a gross pnl_pct.
    They are read at face value rather than retroactively charged — the point
    is to stop producing misleading numbers, not to rewrite history.
    """
    return float(trade.get('pnl_pct') or 0.0)


# ── CIRCUIT BREAKER ───────────────────────────────────────────────────────────

def check_breaker() -> str | None:
    """
    Returns a reason string when new entries should be suspended, else None.

    The system traded through a five-month, -20% gross drawdown without ever
    pausing, because nothing in it looked at its own results. This is the
    feedback loop: if the last BREAKER_LOOKBACK closed trades have negative
    net expectancy, stop opening new positions. Open positions are still
    managed to their exits.

    It is deliberately easy to clear — a single good run of trades re-arms it —
    because the goal is to stop compounding a losing configuration, not to
    permanently disable the bot.
    """
    if not config.BREAKER_ENABLED:
        return None

    closed = closed_trades()
    if len(closed) < config.BREAKER_MIN_TRADES:
        return None

    window = closed[-config.BREAKER_LOOKBACK:]
    pnls   = [net_pnl(t) for t in window]
    exp    = sum(pnls) / len(pnls)

    if exp < config.BREAKER_MIN_EXPECTANCY:
        wins = sum(1 for p in pnls if p > 0)
        return (f'net expectancy {exp:+.2f}%/trade over last {len(window)} '
                f'closed trades ({wins}/{len(window)} wins) — new entries '
                f'suspended until it turns positive')
    return None


# ── ENTRY CONDITION ───────────────────────────────────────────────────────────

def should_enter(signal: dict, open_positions: dict,
                 cooldowns: dict, now: datetime,
                 cost: dict | None = None,
                 breaker: str | None = None) -> tuple[bool, str]:
    """
    Returns (True, reason) if all entry conditions are met, (False, reason) if not.

    Conditions (all must be true):
      1. The circuit breaker is not tripped
      2. Verdict is BUY
      3. Confidence >= MIN_CONFIDENCE
      4. R:R >= MIN_RR
      5. Pool is liquid enough to trade, and our size is small against it
      6. The move to target clears the round-trip cost by MIN_EDGE_COST_MULTIPLE
      7. Symbol is not in post-stop cooldown
      8. Current price is within ENTRY_TOLERANCE above the entry zone
      9. No existing open position for this symbol
     10. Total open positions < MAX_OPEN_POSITIONS

    Conditions 1, 5 and 6 are the ones this system spent five months without.
    The indicator gates (2-4) were tightened three times and never addressed
    the actual problem, which was that trades were taken whose best case did
    not cover the cost of taking them.
    """
    sym = signal['symbol']

    if breaker:
        return False, f'circuit breaker: {breaker}'

    if signal['verdict'] != 'BUY':
        return False, f"verdict={signal['verdict']}"

    if signal['confidence'] < config.MIN_CONFIDENCE:
        return False, f"confidence {signal['confidence']} < {config.MIN_CONFIDENCE}"

    if signal['rr_ratio'] < config.MIN_RR:
        return False, f"R:R {signal['rr_ratio']:.2f} < {config.MIN_RR}"

    liq = float(signal.get('liquidity_usd') or 0)
    if liq < config.MIN_POOL_LIQUIDITY_USD:
        return False, (f'pool liquidity ${liq:,.0f} < '
                       f'${config.MIN_POOL_LIQUIDITY_USD:,.0f} floor')

    if cost:
        notional = cost['notional_usd']

        # Position too small to amortise gas. Reported as a sizing problem
        # with the number attached, because that is what it is — refusing
        # every trade forever without saying why is how a bot looks healthy
        # while doing nothing useful.
        if cost['gas_pct'] > config.MAX_GAS_COST_PCT:
            need = min_viable_notional_usd(cost['gas_gwei'], cost['eth_usd'])
            return False, (f'position ${notional:,.0f} too small — gas is '
                           f'{cost["gas_pct"]:.2f}% of it round trip '
                           f'(ceiling {config.MAX_GAS_COST_PCT}%); needs '
                           f'≥${need:,.0f} at {cost["gas_gwei"]:.1f} gwei')

        if liq > 0 and notional > liq * config.MAX_POOL_SHARE:
            return False, (f'position ${notional:,.0f} exceeds '
                           f'{config.MAX_POOL_SHARE:.2%} of ${liq:,.0f} pool')

        move     = float(signal.get('target_move_pct') or 0)
        required = max(cost['total_pct'] * config.MIN_EDGE_COST_MULTIPLE,
                       config.MIN_TARGET_MOVE_PCT)
        if move < required:
            return False, (f'target move {move:.2f}% < {required:.2f}% required '
                           f'(round-trip cost {cost["total_pct"]:.2f}%: '
                           f'fee {cost["lp_fee_pct"]:.2f} + gas {cost["gas_pct"]:.2f} '
                           f'+ impact {cost["impact_pct"]:.2f})')

    cd = cooldowns.get(sym)
    if cd and now < cd:
        return False, f"stop cooldown until {cd.strftime('%Y-%m-%d %H:%M')} UTC"

    price = float(signal['current_price'])
    entry = float(signal['entry'])
    # Price must be at or below entry + tolerance (we don't chase)
    if price > entry * (1 + config.ENTRY_TOLERANCE):
        return False, f"price {fmt_price(price)} too far above entry {fmt_price(entry)}"

    if sym in open_positions:
        return False, 'already have open position'

    if len(open_positions) >= config.MAX_OPEN_POSITIONS:
        return False, f'max positions ({config.MAX_OPEN_POSITIONS}) reached'

    return True, 'all conditions met'


# ── EXIT / STOP CONDITIONS ────────────────────────────────────────────────────

def check_exit_or_stop(position: dict, current_price: float,
                       now: datetime | None = None) -> tuple[str | None, str]:
    """
    Returns ('exit', reason), ('stop', reason), or (None, reason).

    MUTATES `position` — it ratchets the high-water mark and the trailing stop.
    The caller must persist positions.json after calling this.

    Reaching the target no longer closes the trade. It arms a trailing stop
    and ratchets the stop up to breakeven-after-costs, so a trade that got to
    its target can no longer end as a loser, and can still run.

    The reason for the change is in the record: over five months, 14 trades
    closed at their fixed target for an average of +5.9%, and 48 stopped out
    at an average of -2.15%. A 2.7:1 payoff needs a 27% win rate to break even
    gross; the realised rate was 30.6%, so the strategy was hovering at gross
    breakeven and losing badly net of costs. The only period with a positive
    gross expectancy earned all of it from three trades that ran well past
    where a fixed target would have sold — +14.7%, +9.8%, +7.5%. Capping the
    right tail is what made the arithmetic unwinnable.
    """
    now         = now or datetime.now(timezone.utc)
    exit_target = float(position['exit_target'])
    entry_f     = float(position['entry_price'])
    stop_loss   = float(position['stop_loss'])
    armed       = bool(position.get('trail_armed'))

    # ── Time stop ─────────────────────────────────────────────────────────────
    opened_at = position.get('opened_at')
    if opened_at:
        try:
            age_h = (now - datetime.fromisoformat(opened_at)).total_seconds() / 3600
            if age_h > config.MAX_HOLD_HOURS:
                pct = (current_price - entry_f) / entry_f * 100
                return 'exit', (f'time stop — held {age_h/24:.1f}d > '
                                f'{config.MAX_HOLD_HOURS/24:.0f}d ({pct:+.1f}%)')
        except ValueError:
            pass

    if not config.TRAIL_ENABLED:
        if current_price >= exit_target:
            pct = (current_price - entry_f) / entry_f * 100
            return 'exit', (f'price {fmt_price(current_price)} >= exit '
                            f'{fmt_price(exit_target)} (+{pct:.1f}%)')
        if current_price <= stop_loss:
            pct = (current_price - entry_f) / entry_f * 100
            return 'stop', (f'price {fmt_price(current_price)} <= stop '
                            f'{fmt_price(stop_loss)} ({pct:.1f}%)')
        return None, (f'price {fmt_price(current_price)} in range '
                      f'[{fmt_price(stop_loss)}, {fmt_price(exit_target)}]')

    # ── Arm the trail on first touch of the target ────────────────────────────
    if not armed and current_price >= exit_target:
        armed = True
        position['trail_armed'] = True
        position['high_water']  = fmt_price(current_price)
        logger.info(f'  {position["symbol"]}: target reached — trailing stop armed')

    if not armed:
        if current_price <= stop_loss:
            pct = (current_price - entry_f) / entry_f * 100
            return 'stop', (f'price {fmt_price(current_price)} <= stop '
                            f'{fmt_price(stop_loss)} ({pct:.1f}%)')
        return None, (f'price {fmt_price(current_price)} below target '
                      f'{fmt_price(exit_target)}, stop {fmt_price(stop_loss)}')

    # ── Armed: ratchet the high-water mark and the trailing stop ──────────────
    high_water = max(float(position.get('high_water') or current_price), current_price)
    position['high_water'] = fmt_price(high_water)

    atr        = float(position.get('atr') or 0)
    trail_dist = max(atr * config.TRAIL_ATR_MULT,
                     high_water * config.TRAIL_MIN_PCT)

    # Once armed, the floor is entry plus a cost buffer, so a trade that
    # reached its target should not end negative. "Should not" rather than
    # "cannot" — positions are only checked hourly, so price can gap straight
    # through the trail and fill well below it. That gap risk is inherent to
    # running on a cron schedule and is not something a stop level can fix.
    cost_pct   = float(position.get('cost_pct') or 0)
    breakeven  = entry_f * (1 + cost_pct * config.BREAKEVEN_COST_MULTIPLE / 100)
    trail_stop = max(high_water - trail_dist, breakeven, stop_loss)
    position['stop_loss'] = fmt_price(trail_stop)

    if current_price <= trail_stop:
        pct = (current_price - entry_f) / entry_f * 100
        return 'exit', (f'trailing stop — price {fmt_price(current_price)} <= '
                        f'{fmt_price(trail_stop)}, peak {fmt_price(high_water)} '
                        f'({pct:+.1f}%)')

    pct = (current_price - entry_f) / entry_f * 100
    return None, (f'trailing — price {fmt_price(current_price)} ({pct:+.1f}%), '
                  f'peak {fmt_price(high_water)}, trail {fmt_price(trail_stop)}')


# ── EMAIL BODY ────────────────────────────────────────────────────────────────

def write_email_body(actions: list, open_positions: dict,
                     mode_str: str, run_time: str) -> None:
    from datetime import datetime, timezone as _tz

    W = 52

    def bar(): return '─' * W

    def fmt_dt(iso):
        try:
            return datetime.fromisoformat(iso).strftime('%d %b %Y  %H:%M UTC')
        except Exception:
            return iso[:16]

    def fmt_pnl(pct):
        if pct is None:
            return ''
        return f'+{pct:.2f}%' if pct >= 0 else f'{pct:.2f}%'

    def held_str(opened_at):
        if not opened_at:
            return ''
        try:
            opened = datetime.fromisoformat(opened_at)
            now    = datetime.fromisoformat(run_time)
            delta  = now - opened
            d, h   = delta.days, delta.seconds // 3600
            return f'{d}d {h}h' if d else f'{h}h'
        except Exception:
            return ''

    def pct_diff(a, b):
        try:
            a, b = float(a), float(b)
            return f'{(b - a) / a * 100:+.1f}%'
        except Exception:
            return ''

    lines = []

    exits = [a for a in actions if a['action'] == 'exit']
    stops = [a for a in actions if a['action'] == 'stop']
    buys  = [a for a in actions if a['action'] == 'buy']

    # ── Header ────────────────────────────────────────────────────────────────
    n_ev   = len(exits) + len(stops)
    n_buys = len(buys)
    parts  = []
    if exits:  parts.append(f'{len(exits)} exit{"s" if len(exits)>1 else ""}')
    if stops:  parts.append(f'{len(stops)} stop{"s" if len(stops)>1 else ""}')
    if buys:   parts.append(f'{n_buys} new entr{"ies" if n_buys>1 else "y"}')
    headline = ' · '.join(parts)

    lines += [
        '=' * W,
        f'  ETH TRADER  [{mode_str}]',
        f'  {fmt_dt(run_time)}',
        f'  {headline}',
        '=' * W,
    ]

    def cost_lines(a: dict) -> list:
        """
        Gross → cost → net, shown on every closed trade.

        The whole reason this project spent five months tuning against
        meaningless numbers is that the cost was invisible. Putting it in the
        notification keeps it in front of whoever reads the result.
        """
        if a.get('pnl_pct_gross') is None:
            return []
        out = ['',
               f'  {"Gross":8}  {fmt_pnl(a["pnl_pct_gross"])}',
               f'  {"Cost":8}  −{a.get("cost_pct", 0):.2f}%',
               f'  {"Net":8}  {fmt_pnl(a.get("pnl_pct"))}']
        peak = a.get('peak_price')
        if a.get('trail_armed') and peak:
            out.append(f'  {"Peak":8}  {peak}  (trailing stop was armed)')
        return out

    # ── Exits ─────────────────────────────────────────────────────────────────
    for a in exits:
        pnl_str = fmt_pnl(a.get('pnl_pct'))
        held    = held_str(a.get('opened_at', ''))
        header  = f'  TARGET HIT  {a["symbol"]}'
        lines  += [
            '',
            header + pnl_str.rjust(W - len(header)),
            bar(),
            f'  {"Gained":8}  {pnl_str}' + (f'  (held {held})' if held else ''),
            '',
            f'  {"Opened at":8}  {a.get("entry_price", "?")}',
            f'  {"Closed at":8}  {a.get("close_price", "?")}',
            f'  {"Target":8}  {a.get("exit_target", "?")}',
        ] + cost_lines(a)
        reason = a.get('reason', '')
        if reason:
            lines += ['', f'  Why  {reason}']

    # ── Stops ─────────────────────────────────────────────────────────────────
    for a in stops:
        pnl_str = fmt_pnl(a.get('pnl_pct'))
        held    = held_str(a.get('opened_at', ''))
        header  = f'  STOP LOSS   {a["symbol"]}'
        lines  += [
            '',
            header + pnl_str.rjust(W - len(header)),
            bar(),
            f'  {"Lost":8}  {pnl_str}' + (f'  (held {held})' if held else ''),
            '',
            f'  {"Opened at":8}  {a.get("entry_price", "?")}',
            f'  {"Closed at":8}  {a.get("close_price", "?")}',
            f'  {"Stop":8}  {a.get("stop_loss", "?")}',
        ] + cost_lines(a)
        reason = a.get('reason', '')
        if reason:
            lines += ['', f'  Why  {reason}']

    # ── New entries ───────────────────────────────────────────────────────────
    for a in buys:
        ep   = a.get('entry_price', '?')
        et   = a.get('exit_target', '?')
        sl   = a.get('stop_loss',   '?')
        rr   = a.get('rr_ratio',    '?')
        conf = a.get('confidence',  '?')
        sigs = a.get('signals', [])
        lines += [
            '',
            f'  NEW ENTRY   {a["symbol"]}',
            bar(),
            f'  {"Entry":8}  {ep}',
            f'  {"Target":8}  {et}  ({pct_diff(ep, et)})',
            f'  {"Stop":8}  {sl}  ({pct_diff(ep, sl)})',
            f'  {"R:R":8}  {rr}  ·  confidence {conf}%',
        ]
        if sigs:
            lines += ['', f'  Why  {" · ".join(sigs)}']

    # ── Run summary ───────────────────────────────────────────────────────────
    closed = exits + stops
    if closed:
        net     = sum(a.get('pnl_pct', 0) for a in closed)
        net_str = fmt_pnl(net)
        lines  += ['', '=' * W, '  SUMMARY']
        if exits:
            win_strs = '  '.join(fmt_pnl(a.get('pnl_pct')) for a in exits)
            lines.append(f'  Wins   ({len(exits)})   {win_strs}')
        if stops:
            loss_strs = '  '.join(fmt_pnl(a.get('pnl_pct')) for a in stops)
            lines.append(f'  Losses ({len(stops)})   {loss_strs}')
        lines.append(f'  Net this run     {net_str}')

    # ── Open positions ────────────────────────────────────────────────────────
    if open_positions:
        lines += ['', '=' * W, f'  OPEN POSITIONS ({len(open_positions)})']
        for sym, p in open_positions.items():
            ep  = p.get('entry_price', '?')
            et  = p.get('exit_target', '?')
            sl  = p.get('stop_loss',   '?')
            age = held_str(p.get('opened_at', ''))
            age_str = f'  [{age}]' if age else ''
            lines.append(f'  {sym:10}  @ {ep}{age_str}')
            lines.append(f'  {"":10}  target {et}   stop {sl}')

    lines += ['', '=' * W]

    Path('email_body.txt').write_text('\n'.join(lines) + '\n')

    # Write subject line for the workflow to pick up
    subj_parts = []
    if exits:  subj_parts.append(f'{len(exits)} exit{"s" if len(exits)>1 else ""}')
    if stops:  subj_parts.append(f'{len(stops)} stop{"s" if len(stops)>1 else ""}')
    if buys:   subj_parts.append(f'{len(buys)} entr{"ies" if len(buys)>1 else "y"}')
    net_pct = sum(a.get('pnl_pct', 0) for a in exits + stops)
    net_str = f'{net_pct:+.2f}%' if exits or stops else ''
    subj = f'[ETH Trader] {", ".join(subj_parts)}'
    if net_str:
        subj += f' — net {net_str}'
    if mode_str == 'PAPER':
        subj += ' [PAPER]'
    Path('email_subject.txt').write_text(subj)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    run_time = datetime.now(timezone.utc).isoformat()
    mode_str = 'PAPER' if PAPER else 'LIVE'
    logger.info(f'=== ETH Trader — {mode_str} mode — {run_time} ===')

    executor        = Executor(paper=PAPER)
    open_positions  = load_json(config.POSITIONS_FILE, {})
    all_signals     = []
    actions_taken   = []
    errors          = []

    # ── PHASE 1: Generate fresh signals for all pairs ─────────────────────────
    logger.info(f'Generating signals for {len(config.PAIRS)} pairs…')

    for pair in config.PAIRS:
        sym = pair['symbol']
        try:
            # ── Step 1: resolve pool (with stale-cache fallback) ──────────────
            pool_info = None
            try:
                pool_info = resolve_pool(pair)
            except Exception as resolve_err:
                # Resolve failed (rate limited, network error, etc.)
                # Try falling back to a stale cached entry so we can still
                # fetch OHLCV for open position monitoring.
                stale = _load_pool_cache_stale(pair['token_address'].lower())
                if stale:
                    logger.warning(
                        f'  {sym}: resolve failed ({resolve_err}), '
                        f'using stale cache from {stale.get("cached_at","?")[:10]}'
                    )
                    pool_info = {
                        'pool_address':  stale['pool_address'],
                        'dex':           stale.get('dex', 'Unknown'),
                        'currency':      stale.get('currency', 'token'),
                        'liquidity_usd': stale.get('liquidity_usd', 0),
                        'from_cache':    True,
                        'stale':         True,
                    }
                else:
                    raise  # no cache at all — propagate original error

            pool_addr = pool_info['pool_address']
            dex       = pool_info['dex']
            currency  = pool_info['currency']
            liq       = pool_info['liquidity_usd']
            stale_tag = ' STALE' if pool_info.get('stale') else ''
            src       = ('cache' if pool_info.get('from_cache') else 'live') + stale_tag
            logger.info(f'  {sym}: {dex} pool={pool_addr[:10]}… liq=${liq:,.0f} [{src}]')

            # ── Step 2: fetch OHLCV ───────────────────────────────────────────
            logger.info(f'  {sym}: fetching OHLCV…')
            try:
                candles = fetch_ohlcv(pool_addr, currency)
            except ValueError:
                # OHLCV failed — pool may have migrated. Force re-resolve and retry once.
                logger.warning(f'  {sym}: OHLCV failed, forcing pool re-resolve…')
                pool_info = resolve_pool(pair, force_refresh=True)
                pool_addr = pool_info['pool_address']
                dex       = pool_info['dex']
                currency  = pool_info['currency']
                candles   = fetch_ohlcv(pool_addr, currency)

            signal  = generate_signal(candles, pair, pool_addr, dex, liq)
            signal['generated_at'] = run_time
            all_signals.append(signal)

            logger.info(
                f'  {sym}: {signal["verdict"]} '
                f'conf={signal["confidence"]}% '
                f'R:R={signal["rr_ratio"]:.2f} '
                f'price={signal["current_price"]} '
                f'entry={signal["entry"]}'
            )

        except Exception as e:
            err = f'{sym}: {e}'
            logger.error(f'  ERROR {err}')
            errors.append(err)

    # Write signals.json — committed back to repo by the Actions workflow
    signals_output = {
        'version':      '1.0',
        'generated_at': run_time,
        'mode':         mode_str,
        'signals':      all_signals,
    }
    save_json(config.SIGNALS_FILE, signals_output)
    logger.info(f'Wrote {config.SIGNALS_FILE} ({len(all_signals)} signals)')

    # pool_cache.json is written by gt_client.py automatically during resolve

    # ── PHASE 2: Check exit/stop for open positions ────────────────────────────
    logger.info(f'Checking {len(open_positions)} open position(s)…')

    # Build a quick price lookup from freshly generated signals
    price_map = {s['symbol']: float(s['current_price']) for s in all_signals}

    now_utc = datetime.now(timezone.utc)

    positions_to_close = []
    for sym, position in open_positions.items():
        current_price = price_map.get(sym)
        if current_price is None:
            logger.warning(f'  {sym}: no fresh price available — skipping exit check')
            continue

        # Mutates `position` (high-water mark, ratcheted trailing stop).
        # open_positions holds the same dict, so save_json below persists it.
        action, reason = check_exit_or_stop(position, current_price, now_utc)
        if action:
            positions_to_close.append((sym, position, action, reason))
            logger.info(f'  {sym}: {action.upper()} triggered — {reason}')
        else:
            logger.info(f'  {sym}: holding — {reason}')

    for sym, position, action, reason in positions_to_close:
        try:
            token_bal_wei, _ = executor.get_token_balance(position['token_address'])
            result = executor.swap_token_for_weth(
                token_address    = position['token_address'],
                pool_fee         = position['pool_fee'],
                token_amount_wei = token_bal_wei,
                reason           = action,
            )
            del open_positions[sym]
            close_px = price_map.get(sym, 0)
            entry_f  = float(position['entry_price'])
            gross    = round((close_px - entry_f) / entry_f * 100, 3) if entry_f else 0.0

            # Charge the round-trip cost estimated when the position was opened.
            # pnl_pct is the NET figure — it is what performance.html plots and
            # what the circuit breaker reads. Paper P&L that ignores fees, gas
            # and price impact is how this project spent five months tuning a
            # strategy against numbers that could not happen.
            cost_pct = float(position.get('cost_pct') or 0.0)
            pnl      = round(gross - cost_pct, 3)

            entry = {
                'timestamp':      run_time,
                'action':         action,
                'symbol':         sym,
                'reason':         reason,
                'entry_price':    str(position['entry_price']),
                'close_price':    fmt_price(close_px) if close_px else None,
                'exit_target':    str(position['exit_target']),
                'stop_loss':      str(position['stop_loss']),
                'opened_at':      position.get('opened_at', ''),
                'pnl_pct':        pnl,
                'pnl_pct_gross':  gross,
                'cost_pct':       round(cost_pct, 3),
                'cost_breakdown': position.get('cost_breakdown'),
                'peak_price':     position.get('high_water'),
                'trail_armed':    bool(position.get('trail_armed')),
                'tx_hash':        result['tx_hash'],
                'mode':           mode_str,
            }
            append_trade_log(entry)
            actions_taken.append(entry)
            logger.info(f'  {sym}: {action} executed — tx={result["tx_hash"]}')

        except Exception as e:
            err = f'{sym} {action} failed: {e}'
            logger.error(f'  ERROR {err}')
            errors.append(err)

    # ── PHASE 3: Check entry for new signals ──────────────────────────────────
    logger.info('Checking entry conditions…')

    # Computed after phase 2 so stops executed this run are included —
    # a symbol stopped out this hour must not be re-bought this hour.
    now       = datetime.now(timezone.utc)
    cooldowns = stop_cooldowns(now)

    breaker = check_breaker()
    if breaker:
        logger.warning(f'  CIRCUIT BREAKER TRIPPED — {breaker}')

    # Live inputs to the cost model. Both fall back to config assumptions so a
    # failed RPC call degrades the estimate rather than skipping the gate.
    eth_usd = price_map.get('ETH/USDC') or config.ASSUMED_ETH_USD
    try:
        gas_gwei = executor.w3.eth.gas_price / 1e9
    except Exception as gas_err:
        gas_gwei = config.ASSUMED_GAS_GWEI
        logger.warning(f'  gas price unavailable ({gas_err}) — '
                       f'assuming {gas_gwei} gwei')

    weth_amount_wei = executor.calc_position_size_wei()
    notional_usd    = weth_amount_wei / 1e18 * eth_usd
    logger.info(f'  position size {weth_amount_wei / 1e18:.4f} WETH '
                f'(≈${notional_usd:,.0f}) at {gas_gwei:.1f} gwei')

    for signal in all_signals:
        sym  = signal['symbol']
        cost = round_trip_cost(
            pool_fee      = fee_from_dex_name(signal['dex']),
            liquidity_usd = float(signal.get('liquidity_usd') or 0),
            notional_usd  = notional_usd,
            gas_price_gwei = gas_gwei,
            eth_usd        = eth_usd,
        )
        signal['round_trip_cost_pct'] = cost['total_pct']

        enter, reason = should_enter(signal, open_positions, cooldowns, now,
                                     cost=cost, breaker=breaker)
        logger.info(f'  {sym}: enter={enter} — {reason}')

        if not enter:
            continue

        try:
            result = executor.swap_weth_for_token(
                token_address   = signal['token_address'],
                pool_fee        = fee_from_dex_name(signal['dex']),
                weth_amount_wei = weth_amount_wei,
            )

            open_positions[sym] = {
                'symbol':        sym,
                'token_address': signal['token_address'],
                'pool_address':  signal['pool_address'],
                'pool_fee':      fee_from_dex_name(signal['dex']),
                'entry_price':   signal['current_price'],  # fmt_price string
                'exit_target':   signal['exit'],           # fmt_price string
                'stop_loss':     signal['stop_loss'],      # fmt_price string
                'atr':           signal['atr'],            # drives the trail
                'cost_pct':      cost['total_pct'],
                'cost_breakdown': cost,
                'trail_armed':   False,
                'high_water':    signal['current_price'],
                'weth_in_wei':   weth_amount_wei,
                'opened_at':     run_time,
                'tx_hash':       result['tx_hash'],
            }

            entry = {
                'timestamp':   run_time,
                'action':      'buy',
                'symbol':      sym,
                'cost_pct':    cost['total_pct'],
                'cost_breakdown': cost,
                'target_move_pct': signal.get('target_move_pct'),
                'liquidity_usd':   signal.get('liquidity_usd'),
                'entry_price': signal['current_price'],
                'exit_target': signal['exit'],
                'stop_loss':   signal['stop_loss'],
                'confidence':  signal['confidence'],
                'rr_ratio':    signal['rr_ratio'],
                'signals':     signal['signals'],
                'tx_hash':     result['tx_hash'],
                'mode':        mode_str,
            }
            append_trade_log(entry)
            actions_taken.append(entry)
            logger.info(f'  {sym}: BUY executed — tx={result["tx_hash"]}')

        except Exception as e:
            err = f'{sym} buy failed: {e}'
            logger.error(f'  ERROR {err}')
            errors.append(err)

    # ── SAVE STATE ────────────────────────────────────────────────────────────
    save_json(config.POSITIONS_FILE, open_positions)
    logger.info(f'Saved positions.json ({len(open_positions)} open)')

    # ── SUMMARY (printed to stdout, captured in run log artifact) ────────────
    print(f'\nETH Trader — {mode_str} — {run_time}')
    print(f'Pairs: {len(all_signals)}/{len(config.PAIRS)}  '
          f'Open: {len(open_positions)}  '
          f'Actions: {len(actions_taken)}  '
          f'Errors: {len(errors)}')
    for a in actions_taken:
        pnl = f'  pnl={a["pnl_pct"]:+.2f}%' if a.get('pnl_pct') is not None else ''
        print(f'  {a["action"].upper():6} {a["symbol"]:10}'
              f' @ {a.get("entry_price", a.get("close_price", "?"))}{pnl}')
    for s in all_signals:
        marker = '→' if s['symbol'] in open_positions else ' '
        print(f'  {marker} {s["symbol"]:10} {s["verdict"]:4} '
              f'conf={s["confidence"]:3d}% R:R={s["rr_ratio"]:.2f}')
    for e in errors:
        print(f'  ERROR {e}')

    # ── EMAIL (written to file, picked up by Actions workflow) ────────────────
    if actions_taken:
        write_email_body(actions_taken, open_positions, mode_str, run_time)

    # Exit with error code if there were errors (triggers Actions failure alert)
    if errors and not all_signals:
        sys.exit(1)


if __name__ == '__main__':
    main()
