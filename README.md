# ETH Trader

Hourly signal generation and automated trading for ETH token pairs on Uniswap V3.
Runs on GitHub Actions — free, no server needed.

## Architecture

```
trader/
  config.py     ← all tunable parameters (edit this)
  gt_client.py  ← GeckoTerminal API: pool resolution + OHLCV
  signals.py    ← analysis logic: RSI, BB, VWAP, S/R, scoring
  trader.py     ← hourly runner: signals → position checks → trades
  executor.py   ← Uniswap V3 swap execution (paper + live)

.github/workflows/
  trader.yml    ← cron schedule, runs trader.py, commits state, sends email

  costs.py      ← round-trip cost model (fees, gas, price impact)

signals.json    ← written every hour by trader.py (readable in repo)
positions.json  ← current open positions (written by trader.py)
trades.log      ← append-only trade history (one JSON line per trade)
```

## Setup

### 1. Fork / push this repo to GitHub

### 2. Set GitHub Actions Secrets
Go to **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Value |
|--------|-------|
| `ALCHEMY_RPC_URL` | `https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY` |
| `WALLET_PRIVATE_KEY` | `0x...` your trading wallet private key (live mode only) |
| `NOTIFY_EMAIL_FROM` | Gmail address to send notifications from |
| `NOTIFY_EMAIL_PASSWORD` | Gmail App Password (not your main password) |
| `NOTIFY_EMAIL_TO` | Your email address to receive notifications |

### 3. Set paper/live mode
Go to **Settings → Secrets and variables → Actions → Variables**:

| Variable | Value |
|----------|-------|
| `PAPER_TRADING` | `true` (default) or `false` for live trading |

**Start with `true`. Verify paper trades look correct for several days before going live.**

### 4. Get an Alchemy API key
- Sign up at https://alchemy.com (free tier is sufficient)
- Create an app on Ethereum Mainnet
- Copy the HTTPS URL

### 5. Create a dedicated trading wallet
- **Never use your main wallet.** Create a fresh wallet just for this bot.
- Fund it with only the WETH you're willing to risk.
- The private key goes in `WALLET_PRIVATE_KEY` (only read by the Actions runner,
  never printed or stored anywhere else).

### 6. Email notifications (optional)
Uses a Gmail account with an App Password:
- Enable 2FA on the Gmail account
- Go to Google Account → Security → App Passwords
- Create an app password for "Mail"
- Use that as `NOTIFY_EMAIL_PASSWORD`

### 7. Test a manual run
Go to **Actions → ETH Trader → Run workflow** → set `paper_override=true` → Run.
Check the output log. Should see signals generated and paper trades logged.

## Read this first

This strategy has a documented five-month losing record: 62 closed trades,
30.6% win rate, −20.65% gross. Read [docs/POSTMORTEM.md](docs/POSTMORTEM.md)
before running it, and especially before switching `PAPER_TRADING` to `false`.

The short version: paper mode modelled no fees, no gas and no price impact for
five months, and the omitted cost was larger than the entire signal. That is
now fixed — paper P&L is net, and trades that cannot pay for themselves are
rejected rather than taken.

## Trade logic

Entry fires when ALL of:
- Circuit breaker is not tripped
- Verdict = BUY
- Confidence ≥ 90% (`MIN_CONFIDENCE`)
- R:R ≥ 2.0 (`MIN_RR`)
- Pool liquidity ≥ $500k (`MIN_POOL_LIQUIDITY_USD`)
- Position ≤ 0.5% of pool reserve (`MAX_POOL_SHARE`)
- Gas ≤ 0.5% of notional round trip (`MAX_GAS_COST_PCT`) — this is a minimum
  position size in disguise, roughly $1,250 at 8 gwei
- Move to target ≥ 3× the round-trip cost, and ≥ 4% absolute
- 7-day change above −8% (crash veto)
- Symbol not in post-stop cooldown
- Current price ≤ entry zone + 1% tolerance
- No existing position for that symbol, and fewer than 4 open

Exit management:
- Reaching the target **arms a trailing stop** rather than selling. The stop
  ratchets to entry-plus-costs, and then trails the high-water mark by
  2× ATR (floor 2%).
- Stop fires when price ≤ stop loss.
- A 21-day time stop closes anything that has gone nowhere.

Circuit breaker: when net expectancy over the last 15 closed trades is negative,
no new positions are opened. Open positions are still managed to their exits.

## Configuration

All tunable parameters are in `trader/config.py`. Key ones:

```python
POSITION_SIZE_PCT  = 0.25   # 25% of WETH balance per trade
MAX_OPEN_POSITIONS = 4
PAPER_WETH_BALANCE = 5.0    # simulated book size — drives paper position size
MIN_CONFIDENCE     = 90
MIN_RR             = 2.0
ENTRY_TOLERANCE    = 0.01   # price must be within 1% of entry zone
ATR_STOP_MULT      = 2.5    # stop = current - 2.5 × ATR
MIN_STOP_PCT       = 0.025  # stop at least 2.5% away

MIN_POOL_LIQUIDITY_USD = 500_000
MIN_EDGE_COST_MULTIPLE = 3.0
MAX_GAS_COST_PCT       = 0.5
TRAIL_ATR_MULT         = 2.0
BREAKER_LOOKBACK       = 15
```

**`PAPER_WETH_BALANCE` is not cosmetic.** It sets the paper position size, which
sets the modelled gas cost, which decides whether any trade is viable. Set it to
what you would actually deploy. Below roughly 2 WETH the honest result on
mainnet is "no trades" — that is the correct answer, not a bug.

## Output files

After each run, the Actions workflow commits these back to your repo:

**signals.json** — full signal data for all pairs, readable by the HTML tool
**positions.json** — current open positions with entry prices and targets
**trades.log** — append-only JSONL history of every trade action

## Going live checklist

- [ ] Read [docs/POSTMORTEM.md](docs/POSTMORTEM.md) in full
- [ ] **Net** expectancy positive over ≥ 30 closed trades (not gross)
- [ ] Circuit breaker not tripped
- [ ] Paper traded for ≥ 7 days
- [ ] Reviewed signals.json manually — entries/exits look reasonable
- [ ] Dedicated wallet created (not your main wallet)
- [ ] Wallet funded with only what you can afford to lose
- [ ] `WALLET_PRIVATE_KEY` secret set
- [ ] `ALCHEMY_RPC_URL` secret set
- [ ] Set `PAPER_TRADING = false` in Actions variables
- [ ] Trigger a manual run and verify the first live tx on Etherscan
