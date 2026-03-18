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

## Trade logic

Entry fires when ALL of:
- Verdict = BUY
- Confidence ≥ 80% (set in config.py → `MIN_CONFIDENCE`)
- R:R ≥ 1.5 (set in config.py → `MIN_RR`)
- Current price ≤ entry zone + 2% tolerance
- No existing position for that symbol
- Total open positions < 8

Exit fires when:
- Current price ≥ exit target (take profit)

Stop fires when:
- Current price ≤ stop loss (cut loss)

## Configuration

All tunable parameters are in `trader/config.py`. Key ones:

```python
POSITION_SIZE_PCT = 0.10   # 10% of WETH balance per trade
MAX_OPEN_POSITIONS = 8
MIN_CONFIDENCE = 80
MIN_RR = 1.5
ENTRY_TOLERANCE = 0.02     # price must be within 2% of entry zone
ATR_STOP_MULT = 1.5        # stop = entry - 1.5 × ATR
```

## Output files

After each run, the Actions workflow commits these back to your repo:

**signals.json** — full signal data for all pairs, readable by the HTML tool
**positions.json** — current open positions with entry prices and targets
**trades.log** — append-only JSONL history of every trade action

## Going live checklist

- [ ] Paper traded for ≥ 7 days
- [ ] Reviewed signals.json manually — entries/exits look reasonable
- [ ] Dedicated wallet created (not your main wallet)
- [ ] Wallet funded with only what you can afford to lose
- [ ] `WALLET_PRIVATE_KEY` secret set
- [ ] `ALCHEMY_RPC_URL` secret set
- [ ] Set `PAPER_TRADING = false` in Actions variables
- [ ] Trigger a manual run and verify the first live tx on Etherscan
