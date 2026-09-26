# gogon — Polymarket Auto-Trading Bot

An automated trading bot for [Polymarket](https://polymarket.com) built on
Polymarket's official CLOB (Central Limit Order Book) API. It scans active
markets, applies pluggable strategies, and executes trades through a risk
manager with hard position/exposure/loss caps.

> ⚠️ **This is trading software. It can lose real money.** Read the whole
> README, run in paper mode first, and never risk more than you can afford
> to lose. Nothing here is financial advice.

## How it works

```
main loop
  ├─ market_data: scans active markets from the CLOB API
  ├─ strategies:  turn order-book data into buy/sell Signals (net of taker fees)
  │    ├─ arbitrage  (default, ON)  — buy YES+NO when combined cost incl. fees < $1
  │    └─ threshold  (default, OFF) — mean-reversion on price swings
  ├─ risk:        approves/rejects each Signal (or whole multi-leg group)
  │               against position, exposure & daily-loss caps
  ├─ execution:   simulates the fill (paper) or signs & submits an order (live)
  ├─ state:       saves positions + today's P&L to SQLite after every fill
  └─ notify:      Telegram alerts (fills, errors, kill switch, heartbeat)
```

Every order the bot sends (or simulates), filled or not, is appended to
`data/trades.csv` as an audit trail. Logs go to the console and to
`logs/bot.log` (rotating). `data/status.json` is rewritten every cycle with
the bot's current exposure and P&L.

### Why arbitrage is the default strategy

A binary Polymarket market always pays exactly $1 to the winning outcome's
shares and $0 to the losing side. Buying **one YES share and one NO share**
therefore always resolves to exactly $1 combined, no matter which side wins.
If the combined ask price of YES + NO is reliably below `$1 - fees`, the gap
is close to risk-free profit at resolution. The real risks are execution
risk (one leg fills, the other doesn't — mitigated here by using
fill-or-kill orders) and Polymarket's own fee/rule changes — which is why
`min_edge` and `fee_buffer` exist as safety margins in the config.

### Fees decide whether an arbitrage is real

Polymarket charges **takers** a fee of `shares × rate × price × (1 − price)`,
with the rate set per market category (e.g. crypto 0.07, sports 0.05,
politics 0.04, geopolitics 0 as of Sept 2026). The bot's
fill-or-kill orders are always the taker, and near 50¢ the fee on one
YES+NO set is about `rate × 0.5` — e.g. 3.5¢ in a crypto market, more than a
typical 2¢ discount. So the arbitrage strategy only trades when the profit
is still at least `min_edge` **after** fees and `fee_buffer`, and paper mode
charges the same fees so its P&L isn't flattering.

Rates live in `config/settings.yaml` under `fees:`. A market's rate is
picked from its tags; unrecognized markets get `default_taker_rate` (the
highest known rate), so costs are never under-estimated. **Check the rates
against [Polymarket's fee page](https://docs.polymarket.com/trading/fees)
before going live** — Polymarket changes them.

The threshold (mean-reversion) strategy is included as a second option but
ships **disabled**, because it's directional and can lose money in a
trending market — only turn it on if you understand that risk.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`:
- Leave `LIVE_TRADING=false` to run in **paper trading** (fully simulated,
  no funds at risk, no wallet required). This is the default and the
  recommended starting point.
- To go live later, you'll need:
  - `POLY_PRIVATE_KEY` — the private key of the wallet that signs orders.
    **Never commit this or paste it anywhere outside your local `.env`.**
  - `POLY_FUNDER_ADDRESS` — the address holding your USDC. If you trade
    through the polymarket.com website, this is your **proxy wallet**
    address (shown on your Polymarket profile), and `POLY_SIGNATURE_TYPE`
    should be `2`. If you trade with a plain EOA wallet directly, use your
    wallet address and `POLY_SIGNATURE_TYPE=0`.
  - Then set `LIVE_TRADING=true`.
- Optional but recommended for 24/7 use: `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` for alerts on your phone (see [Alerts](#alerts-telegram)).

Tune strategy and risk parameters in `config/settings.yaml` — in
particular `risk.max_position_usd`, `risk.max_total_exposure_usd`, and
`risk.max_daily_loss_usd`. Start small.

## Running

```bash
# Sanity-check config, wallet, and connectivity before running for real:
python scripts/check_setup.py

# Run the bot:
python -m bot.main
```

Stop any time with `Ctrl+C` — it finishes the current market and exits
cleanly.

## Running 24/7

A bot that runs around the clock will be restarted — by crashes, deploys,
or server reboots — so it is built to survive that:

- **Positions persist.** Open positions and today's realized P&L are saved
  to SQLite after every fill (`data/state_paper.sqlite3` or
  `data/state_live.sqlite3` — paper and live never mix) and restored on
  start, so exposure caps and the kill switch keep working across restarts.
- **Resolved markets are settled.** Every 10 minutes (and on start) the bot
  checks whether the markets it holds have resolved and closes those
  positions in its books at the payout, so exposure doesn't pile up forever.
- **Positions are reconciled (live).** On start and every hour, the bot
  compares its positions with what Polymarket's Data API reports for your
  funder address and alerts you about mismatches. It never "fixes" them
  on its own — you decide.
- **The kill switch counts open losses.** `risk.max_daily_loss_usd`
  compares today's realized P&L **plus** the unrealized P&L of open
  positions (marked at the best bid each cycle; complete YES+NO sets count
  as the $1 they are worth).

Run it on a small always-on Linux VPS, not a laptop. Two ways:

**Docker** (restarts automatically, including after a reboot):

```bash
cp .env.example .env            # then fill it in
mkdir -p data logs && sudo chown -R 1000:1000 data logs   # the container runs as uid 1000
docker compose up -d --build
docker compose logs -f          # follow the logs
docker compose ps               # shows (healthy) while data/status.json keeps updating
```

**systemd** (no Docker): clone to `/opt/gogon`, create the venv there as in
[Setup](#setup), create a `gogon` user that owns the folder, then:

```bash
sudo cp deploy/gogon-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gogon-bot
journalctl -u gogon-bot -f
```

The dashboard only listens on `127.0.0.1`. To see it from your own
computer, run `python3 scripts/dashboard.py` on the server and open an SSH
tunnel (`ssh -L 8765:127.0.0.1:8765 you@your-server`) — never expose it
publicly.

## Alerts (Telegram)

With `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set in `.env`, the bot
messages you when it starts or stops, fills a trade (turn off with
`notifications.fills: false`), settles a resolved market, hits or clears
the daily loss limit, fills only one leg of an arbitrage, sees positions
that don't match Polymarket, finds no markets to scan, or keeps hitting
errors. It also sends a status message every
`notifications.heartbeat_hours` (default 6) — **if those stop arriving, the
bot or its server is down.**

To set it up: message [@BotFather](https://t.me/BotFather) in Telegram,
send `/newbot`, and put the token in `.env`. Send your new bot any message,
open `https://api.telegram.org/bot<TOKEN>/getUpdates`, and copy the
`"chat":{"id":...}` number into `TELEGRAM_CHAT_ID`. Then check it works:

```bash
python scripts/check_setup.py --telegram-test
```

Alerts are best-effort: they never block or crash trading, and the token is
never written to the logs.

## Dashboard

A local, read-only dashboard shows live trading activity — KPIs, cumulative
volume chart, strategy breakdown, open positions, and recent trades — read
straight from `data/trades.csv`. No extra dependencies, nothing leaves your
machine.

```bash
# in a second terminal, alongside `python -m bot.main`:
python scripts/dashboard.py
```

It opens `http://127.0.0.1:8765` in your browser automatically and
refreshes every 5 seconds.

## Running tests

```bash
python -m pytest
```

Tests cover the pure logic (fees, risk limits and mark-to-market,
arbitrage sizing/edge detection, grouped execution, state persistence,
reconciliation parsing, alert throttling, threshold signal generation) with
no network calls, so they're safe and fast to run anytime.

## Safety notes

- **Start in paper mode** and watch `data/trades.csv` / `logs/bot.log` for
  at least a few days before considering live trading.
- **Start with small caps** in `config/settings.yaml` when you do go live.
- The bot enforces a **daily loss kill-switch** (`risk.max_daily_loss_usd`,
  realized + unrealized): once hit, it stops opening new positions until
  P&L recovers or realized P&L resets at UTC midnight. It does not
  automatically close existing positions for you.
- Both legs of an arbitrage are risk-checked together and sent one at a
  time, thinner book first; if a leg doesn't fill, the remaining legs aren't
  sent. If a later leg fails after an earlier one filled, you hold an
  **unhedged position**: the bot logs it as CRITICAL and alerts you, but
  does not unwind it for you.
- Arbitrage positions are held until the market resolves. Every 10
  minutes the bot checks the markets it holds and settles resolved ones in
  its own books at the payout ($1 winner / $0 loser), which realizes the
  P&L and frees the exposure. In live mode you still redeem the USDC on
  Polymarket yourself, and merging YES+NO sets back into USDC before
  resolution (to recycle capital sooner) isn't implemented yet.
- In live mode, order fills are tracked based on the CLOB API's response to
  each order submission, and fees are the bot's estimate. The hourly
  reconciliation flags drift, but still check the Polymarket UI — don't rely
  solely on the bot's own tracking for anything you haven't verified.
- The arbitrage strategy currently only handles simple **binary
  (two-outcome)** markets.
- This code has not been run against the live Polymarket API from this
  environment (no network access here) — including the market tags used
  for fee rates and the Data API used for reconciliation. Treat
  `scripts/check_setup.py` and a few days of paper trading as your first
  real-world check, and review the code yourself before trusting it with
  funds.

## Project layout

```
bot/
  config.py          # loads .env + config/settings.yaml
  client.py           # wraps py-clob-client's ClobClient
  market_data.py       # market discovery + order book parsing
  fees.py               # Polymarket taker-fee model
  risk.py               # position/exposure/loss limits, mark-to-market
  execution.py           # paper vs. live order execution, grouped legs
  journal.py              # CSV trade log
  state.py                 # SQLite persistence of positions + daily P&L
  settlement.py            # settles positions in resolved markets
  reconcile.py             # compares positions with Polymarket's Data API
  notify.py                # Telegram alerts
  main.py                  # the scan-evaluate-execute loop
  strategies/
    base.py                # Signal + Strategy interface
    arbitrage.py            # complete-set arbitrage (default, on)
    threshold.py             # mean-reversion (default, off)
config/settings.yaml    # strategy, fee, risk & alert parameters (no secrets)
.env.example             # secrets template (copy to .env)
scripts/check_setup.py    # pre-flight sanity check (+ Telegram test)
scripts/dashboard.py       # local trading-activity dashboard
Dockerfile, docker-compose.yml  # run 24/7 with Docker
deploy/gogon-bot.service        # run 24/7 with systemd
tests/                      # pytest unit tests, no network required
```
