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
from trader.gt_client import resolve_pool, fetch_ohlcv, fetch_current_price
from trader.signals import generate_signal
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


# ── ENTRY CONDITION ───────────────────────────────────────────────────────────

def should_enter(signal: dict, open_positions: dict) -> tuple[bool, str]:
    """
    Returns (True, reason) if all entry conditions are met, (False, reason) if not.

    Conditions (all must be true):
      1. Verdict is BUY
      2. Confidence >= MIN_CONFIDENCE
      3. R:R >= MIN_RR
      4. Current price is within ENTRY_TOLERANCE above the entry zone
      5. No existing open position for this symbol
      6. Total open positions < MAX_OPEN_POSITIONS
    """
    sym = signal['symbol']

    if signal['verdict'] != 'BUY':
        return False, f"verdict={signal['verdict']}"

    if signal['confidence'] < config.MIN_CONFIDENCE:
        return False, f"confidence {signal['confidence']} < {config.MIN_CONFIDENCE}"

    if signal['rr_ratio'] < config.MIN_RR:
        return False, f"R:R {signal['rr_ratio']:.2f} < {config.MIN_RR}"

    price = signal['current_price']
    entry = signal['entry']
    # Price must be at or below entry + tolerance (we don't chase)
    if price > entry * (1 + config.ENTRY_TOLERANCE):
        return False, f"price {price:.6f} too far above entry {entry:.6f}"

    if sym in open_positions:
        return False, 'already have open position'

    if len(open_positions) >= config.MAX_OPEN_POSITIONS:
        return False, f'max positions ({config.MAX_OPEN_POSITIONS}) reached'

    return True, 'all conditions met'


# ── EXIT / STOP CONDITIONS ────────────────────────────────────────────────────

def check_exit_or_stop(position: dict, current_price: float) -> tuple[str | None, str]:
    """
    Returns ('exit', reason), ('stop', reason), or (None, reason).

    Checks current price against the exit target and stop loss stored
    when the position was opened.
    """
    exit_target = position['exit_target']
    stop_loss   = position['stop_loss']

    if current_price >= exit_target:
        pct = (current_price - position['entry_price']) / position['entry_price'] * 100
        return 'exit', f'price {current_price:.6f} >= exit {exit_target:.6f} (+{pct:.1f}%)'

    if current_price <= stop_loss:
        pct = (current_price - position['entry_price']) / position['entry_price'] * 100
        return 'stop', f'price {current_price:.6f} <= stop {stop_loss:.6f} ({pct:.1f}%)'

    return None, f'price {current_price:.6f} in range [{stop_loss:.6f}, {exit_target:.6f}]'


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
            pool_info = resolve_pool(pair)
            pool_addr = pool_info['pool_address']
            dex       = pool_info['dex']
            currency  = pool_info['currency']
            liq       = pool_info['liquidity_usd']
            src       = 'cache' if pool_info.get('from_cache') else 'live'
            logger.info(f'  {sym}: {dex} pool={pool_addr[:10]}… liq=${liq:,.0f} [{src}]')

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

            signal  = generate_signal(candles, pair, pool_addr, dex)
            signal['generated_at'] = run_time
            all_signals.append(signal)

            logger.info(
                f'  {sym}: {signal["verdict"]} '
                f'conf={signal["confidence"]}% '
                f'R:R={signal["rr_ratio"]:.2f} '
                f'price={signal["current_price"]:.6f} '
                f'entry={signal["entry"]:.6f}'
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
    price_map = {s['symbol']: s['current_price'] for s in all_signals}

    positions_to_close = []
    for sym, position in open_positions.items():
        current_price = price_map.get(sym)
        if current_price is None:
            logger.warning(f'  {sym}: no fresh price available — skipping exit check')
            continue

        action, reason = check_exit_or_stop(position, current_price)
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
            entry = {
                'timestamp':     run_time,
                'action':        action,
                'symbol':        sym,
                'reason':        reason,
                'entry_price':   position['entry_price'],
                'close_price':   price_map.get(sym),
                'pnl_pct':       round((price_map.get(sym, 0) - position['entry_price'])
                                       / position['entry_price'] * 100, 2),
                'tx_hash':       result['tx_hash'],
                'mode':          mode_str,
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

    for signal in all_signals:
        sym    = signal['symbol']
        enter, reason = should_enter(signal, open_positions)
        logger.info(f'  {sym}: enter={enter} — {reason}')

        if not enter:
            continue

        try:
            weth_amount_wei = executor.calc_position_size_wei()
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
                'entry_price':   signal['current_price'],
                'exit_target':   signal['exit'],
                'stop_loss':     signal['stop_loss'],
                'weth_in_wei':   weth_amount_wei,
                'opened_at':     run_time,
                'tx_hash':       result['tx_hash'],
            }

            entry = {
                'timestamp':   run_time,
                'action':      'buy',
                'symbol':      sym,
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

    # ── SUMMARY (printed to stdout — captured by Actions for email) ───────────
    print('\n' + '═' * 60)
    print(f'ETH Trader Run — {mode_str} — {run_time}')
    print('═' * 60)
    print(f'Pairs analyzed:   {len(all_signals)}/{len(config.PAIRS)}')
    print(f'Open positions:   {len(open_positions)}')
    print(f'Actions taken:    {len(actions_taken)}')
    if errors:
        print(f'Errors:           {len(errors)}')
        for e in errors:
            print(f'  ▸ {e}')

    if actions_taken:
        print('\nActions:')
        for a in actions_taken:
            print(f'  {a["action"].upper():6} {a["symbol"]:10} '
                  f'@ {a.get("entry_price", a.get("close_price", "?")):.6f}'
                  f'  tx={a["tx_hash"]}')

    print('\nSignals:')
    for s in all_signals:
        marker = '→' if s['symbol'] in open_positions else ' '
        print(f'  {marker} {s["symbol"]:10} {s["verdict"]:4} '
              f'conf={s["confidence"]:3d}% '
              f'R:R={s["rr_ratio"]:.2f} '
              f'price={s["current_price"]:.6f}')

    print('═' * 60 + '\n')

    # Exit with error code if there were errors (triggers Actions failure alert)
    if errors and not all_signals:
        sys.exit(1)


if __name__ == '__main__':
    main()
