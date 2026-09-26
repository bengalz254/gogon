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
  ├─ strategies:  turn order-book data into buy/sell Signals
  │    ├─ arbitrage  (default, ON)  — buy YES+NO when combined price < $1
  │    └─ threshold  (default, OFF) — mean-reversion on price swings
  ├─ risk:        approves/rejects each Signal against position & loss caps
  └─ execution:   simulates the fill (paper) or signs & submits an order (live)
```

Every signal, filled or not, is appended to `data/trades.csv` as an audit
trail. Logs go to the console and to `logs/bot.log` (rotating).

### Why arbitrage is the default strategy

A binary Polymarket market always pays exactly $1 to the winning outcome's
shares and $0 to the losing side. Buying **one YES share and one NO share**
therefore always resolves to exactly $1 combined, no matter which side wins.
If the combined ask price of YES + NO is reliably below `$1 - fees`, the gap
is close to risk-free profit at resolution. The real risks are execution
risk (one leg fills, the other doesn't — mitigated here by using
fill-or-kill orders) and Polymarket's own fee/rule changes — which is why
`min_edge` and `fee_buffer` exist as safety margins in the config.

The threshold (mean-reversion) strategy is included as a second option but
ships **disabled**, because it's directional and can lose money in a
trending market — only turn it on if you understand that risk.

## Up/Down engine (crypto 5-minute markets)

`python -m bot.updown` is a second, separate engine for Polymarket's
short-horizon crypto markets ("Bitcoin Up or Down - 5 minute", ETH, SOL,
XRP, DOGE, ...). It doesn't try to guess direction. It prices **P(Up)
from the same Chainlink stream the market settles on** and trades only
when the share price disagrees with that probability by more than all costs:

```
edge = p_model - p_market - fee - half_spread - slippage   >= min_edge (e.g. 5c)
```

Without a settlement-aware probability model, a bot on these markets is
just gambling. The model is the core of the engine; the strategies are thin
rules around it.

### The model

- **Settlement-aware.** Since 14 Aug 2026 both ends are Chainlink TWAPs
  (per this repo's other Up/Down branch, which ran against the real
  markets): the price to beat is the mean of the per-second prices over the
  60s *before* the open, and the result is the mean over the last 60s before
  the close. `settlement.rule: last` switches back to single prices. Inside
  the closing TWAP window, the samples already observed are locked in and
  only the rest is random. The variance of the future average is computed
  exactly (`bot/updown/model.py`, checked against Monte Carlo in the tests).
- **Inputs:** Chainlink price (truth), the CEX price as a *leading
  indicator only* (a basis-corrected nowcast that nudges the spot price),
  realized volatility over 3 and 30 minutes with per-coin floors, time
  remaining, and the distance to `price_to_beat` (locked at t=0).
- **Robust:** fat-tailed Student-t instead of a normal distribution. The
  edge must hold across `sigma * (1 +/- vol_uncertainty)`, and the result
  is blended 50/50 with the market price (`market_blend`): other bots may
  see the settlement price sooner than we do. When the model and the market
  disagree by more than 0.30 (`max_model_market_gap`), the bot assumes its
  own data is wrong or late and stays out of that window.

### Architecture: 4 layers

```
1. data       feeds/rtds.py      Chainlink (settlement truth) + Binance relay via Polymarket RTDS
              feeds/clob_ws.py   CLOB websocket: full order books, trades, tick sizes
              feeds/cex.py       optional direct Binance/Bybit trades (fast signal only)
              discovery.py       Gamma API: slug -> condition/token ids, official results
2. state      window.py          per-window state machine: UPCOMING -> LIVE -> CLOSED -> SETTLED
              series.py/model.py per-second TWAP proxy, vol, p_model(t), nowcast
3. strategy   strategies/*.py    intents only (Take / Quote), never orders
4. risk+exec  risk.py            fractional Kelly, caps, kill switch, hard bans
              orders.py          order tracker + conservative paper exchange
              broker_live.py     py-clob-client (FAK takers, post-only GTC makers, heartbeat)
              engine.py          pure, deterministic core (same code live, backtest, sim)
              runner.py          asyncio shell
```

### Strategies (in priority order)

| # | Strategy | Default | Idea |
|---|----------|---------|------|
| 1 | `fair_value` | off* | Buy the side the model says is underpriced by >= `min_edge` after fee, spread and slippage. Never flips sides inside a window. |
| 2 | `late_certainty` | on (small) | 20-70s left, the TWAP is clearly on one side and not drifting back (projected z-score), vol isn't spiking: rest a **maker** bid on the winner at 0.92-0.985. High win rate, but each loss is big, so size is tiny. |
| 3 | `constellation` | off* | Between T+60s and T+120s, >= 4 coins have moved the same way and one coin is still flat with its share near 50c: buy the laggard in the consensus direction. Skipped when BTC moved >= 2% in the last hour. |
| 4 | `pair_barbell` | off | Maker bids on both Up and Down near 50c at the open (pair cost <= 0.99), then add to the side the model confirms and hold to expiry. Needs low latency. |
| 5 | `cheap_asymmetric` | off | Buy the cheap side (0.18-0.42) only with >= 90s left, when the model says it's still alive and the market overprices the expensive side. Small fixed size. |

\* Off in `config/updown.yaml` since the first real paper run (1010 windows):
the market mid out-predicted the model at every time to close, so
`fair_value`, which trades exactly where the two disagree, lost $207 and won
32% of its windows; `constellation` lost $26 over 16 windows. The 15-minute
engine below keeps `fair_value` on as an experiment.

Every strategy can be tuned per coin and per UTC session with `overrides`
in `config/updown.yaml`. Asia and US hours have different microstructure,
so one threshold set shouldn't fit all of them.

### Strategies that are deliberately *not* here (and how that's enforced)

- Indicator-based direction guessing (RSI/MACD/...): not implemented.
  Every entry has to pass the settlement model's edge test.
- **Taker entries in the first 60s near 50c** are a hard ban in the risk
  layer (`risk.no_early_taker_s`, `risk.early_taker_band`).
- **Martingale:** after a loss, size can only shrink
  (`loss_streak_decay <= 1`, checked at startup). Losing streaks trigger
  a per-strategy cooldown.
- Last-tick sniping of Binance vs a stale oracle: the CEX price never
  decides settlement, it only nudges the model. The engine pauses when
  the CEX and oracle disagree too much.
- Whale copy-trading: not implemented.
- One model for every coin and hour: per-coin vol floors plus
  per-coin/per-session overrides.
- Correlation: coins move together inside a window, so
  `risk.max_slot_direction_usd` caps the combined same-direction bet across
  coins in one time slot.

### Quick start

```bash
pip install -r requirements.txt

# 1) Offline: run the whole pipeline against a synthetic market (no network)
python scripts/updown_simulate.py --windows 48 --journal-dir data/sim
python scripts/updown_report.py --data-dir data/sim

# 2) Paper trade on the real markets (simulated fills, no orders sent).
#    --record saves every input event for later backtests.
python -m bot.updown --record

# 3) Check whether the model is actually smart
python scripts/updown_report.py          # P&L, Brier model-vs-market, settlement-rule check

# 4) Re-run recorded data with different settings
python scripts/updown_backtest.py "data/recordings/*.jsonl.gz" --config config/updown.yaml
```

### Live dashboard

While `python -m bot.updown` runs, start the monitor in a second terminal:

```bash
python scripts/updown_dashboard.py                  # opens http://127.0.0.1:8766
python scripts/updown_dashboard.py --host 0.0.0.0   # also reachable from a phone on the same Wi-Fi
python scripts/updown_dashboard.py --source "5 menit=data" --source "15 menit=data/15m"   # two engines, one switch
```

Every second it shows each coin's live window: price vs price to beat, model
vs market probability (with the model's uncertainty band and history), what
the bot holds and has resting, and why a coin isn't being traded. It also
shows today's P&L and exposure, the risk guards (kill switch, cooldowns) and
feed health. Every 15 seconds it adds the cumulative P&L, P&L per strategy,
model-vs-market calibration, the settlement-rule check and the latest fills.
The page's labels are in Indonesian.

The engine writes `data/updown_status.json` every second for it. The
dashboard only reads files: it can't place or cancel orders, and stopping it
never affects the bot. There's no login, so only use `--host 0.0.0.0` on a
network you trust.

The older `python scripts/dashboard.py` still shows the engine's fills and
settlements too.

### A second engine: 15-minute markets (paper experiment)

The first paper run lost because the market out-predicted the model, most of
all near the end of a window. That points at our oracle prices arriving a
little later than other traders'. Over a 15-minute window a few seconds
matter less, so a second engine paper trades the 15-minute markets next to
the 5-minute one:

```bash
python -m bot.updown --paper --config config/updown-15m.yaml --log-file logs/bot-15m.log
python scripts/updown_report.py --data-dir data/15m
```

- `config/updown-15m.yaml` starts with `extends: updown.yaml`: the base file
  is applied first and this one on top, so it only lists what differs
  (interval, its own `data/15m` journals, `fair_value` on). A `--config` that
  doesn't exist is an error, not a silent fallback to the defaults.
- `--paper` keeps an engine on paper even with `LIVE_TRADING=true`, so the
  experiment can never trade the real wallet next to a live 5-minute engine.
- `--log-file` gives it its own log; the report's calibration table adds
  180-300s, 300-600s and >=600s rows for the longer windows.

If the model still doesn't beat the market mid there, the edge isn't there
either. Check the 15-minute markets' rules and fees on polymarket.com; the
report's settlement-rule check shows which rule matches.

### Windows: double-click instead of typing

The repo root has four launchers. Each opens its own window and uses the
`venv` folder when it exists:

| File | What it does |
|---|---|
| `update.bat` | `git pull`, then shows the current version |
| `start_bot.bat` | runs `python -m bot.updown --record` |
| `start_dashboard.bat` | runs the live dashboard (http://127.0.0.1:8766) |
| `check_log.bat` | shows the recent connection events from `logs/bot.log` and copies them to the clipboard |

Stop the bot with `Ctrl+C` in its window (answer `Y` if Windows asks
"Terminate batch job?").

### Running on a Linux VPS

A VPS keeps the bot online around the clock: no Wi-Fi drops, no sleeping
laptop, no console window to freeze. On Ubuntu 22.04+ or Debian 12+:

```bash
git clone https://github.com/bengalz254/gogon.git && cd gogon
git checkout claude/epic-brahmagupta-gxpbv2
bash scripts/vps_setup.sh
```

The script installs Python, creates the venv, installs the requirements and
runs the tests. It also prints the latency to Polymarket and Polymarket's
region check for the server. Then it installs three systemd services that
start on boot and restart after a crash:
- `updown-bot`: the 5-minute markets, paper mode unless live trading is
  switched on in both `.env` and `config/updown.yaml`.
- `updown-bot-15m`: the 15-minute markets, always paper (see above).
- `updown-dashboard`: listens on the server's 127.0.0.1 only, with a switch
  between the two engines.

It also adds four commands:

| Command | What it does |
|---|---|
| `updown-status` | are the bots running, which version, last log lines |
| `updown-log` | recent connection events and the last stall report, per engine |
| `updown-report` | results so far for both engines: P&L per strategy, model vs market, settlement rule |
| `updown-update` | `git pull`, then re-run the setup (tests, services restart) |

To see the dashboard from Windows, double-click `vps_dashboard.bat`. It asks
for the server address once (e.g. `root@203.0.113.5`), opens an SSH tunnel
and shows the dashboard at http://127.0.0.1:8767. From any other machine:
`ssh -N -L 8767:127.0.0.1:8766 root@<server-ip>`.

Recordings (`--record`) are large, so files older than
`journal.record_keep_days` (7) are deleted. Before going live from a VPS,
make sure Polymarket allows trading from the server's region. The setup
script prints that check.

**Going live needs two opt-ins:** `LIVE_TRADING=true` in `.env` *and*
`execution.allow_live: true` in `config/updown.yaml`. Don't do it until
`updown_report.py` shows, over at least a few hundred paper windows, that
the model beats the market price (Brier score) and the paper P&L after fees
is positive. Then start with the smallest sizes.

### Verify these before trusting the engine

This engine was built and tested offline: unit tests, a synthetic
simulator, and an asyncio run against local mock servers. It has **never
connected to the real Polymarket APIs**. Check the following in paper mode:

- **Settlement rule.** The design assumes a 60s Chainlink TWAP. Read the
  rules text of a live market. Then look at section 4 of
  `updown_report.py`: it compares the TWAP rule and the last-price rule
  against official results. If `last` matches better, set
  `settlement.rule: last`.
- **Fees.** `fees:` uses Polymarket's crypto fee curve as the author
  understood it (~1.56% at 50c). Check the current fee schedule.
- **Slugs and symbols.** Check `markets.slug_template` (default
  `{asset}-updown-{interval}-{start}`), the RTDS symbols (`btc/usd`), and
  which coins actually have windows.
- **Live order responses.** Response parsing in `broker_live.py` is
  defensive but unverified. Start tiny and reconcile against the UI.
- **Winnings are not auto-redeemed.** Claim resolved positions in the
  Polymarket UI (or with your own redeem script) so the USDC is freed.
- **Clock.** Keep the machine NTP-synced. The runner warns if the local
  clock is more than 2s off the CLOB server clock.

The simulator's P&L is **not evidence of real edge**: its market maker is
made stale and noisy on purpose. Only calibration on real data counts.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate          # Windows PowerShell: .\venv\Scripts\Activate.ps1
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

Stop any time with `Ctrl+C` — it finishes the current cycle and exits
cleanly.

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

Tests cover the pure logic with no network calls: risk limits, arbitrage
and threshold signals, and for the Up/Down engine the probability model
(with a Monte Carlo check), every strategy, the risk bans, paper fills,
the engine lifecycle (t=0 lock -> fills -> settlement -> P&L), feed and
Gamma parsing, record/replay, and the live dashboard's data. They're safe and
fast to run anytime.

## Safety notes

- **Start in paper mode** and watch `data/trades.csv` / `logs/bot.log` for
  at least a few days before considering live trading.
- **Start with small caps** in `config/settings.yaml` when you do go live.
- The bot enforces a **daily loss kill-switch** (`risk.max_daily_loss_usd`):
  once hit, it stops opening new positions until UTC midnight. It does not
  automatically close existing positions for you.
- In live mode, order fills are tracked based on the CLOB API's response to
  each order submission. Periodically reconcile against
  `client.get_trades()` / the Polymarket UI — don't rely solely on the
  bot's in-memory position tracking for anything you haven't verified.
- The arbitrage strategy currently only handles simple **binary
  (two-outcome)** markets.
- This code has not been run against the live Polymarket API from this
  environment (no network access here) — treat `scripts/check_setup.py`
  as your first real-world check, and review the code yourself before
  trusting it with funds.

## Project layout

```
bot/
  config.py          # loads .env + config/settings.yaml
  client.py           # wraps py-clob-client's ClobClient
  market_data.py       # market discovery + order book parsing
  risk.py               # position/exposure/loss limits
  execution.py           # paper vs. live order execution
  journal.py              # CSV trade log
  main.py                  # the scan-evaluate-execute loop
  strategies/
    base.py                # Signal + Strategy interface
    arbitrage.py            # complete-set arbitrage (default, on)
    threshold.py             # mean-reversion (default, off)
config/settings.yaml    # strategy & risk parameters (no secrets)
.env.example             # secrets template (copy to .env)
scripts/check_setup.py    # pre-flight sanity check
scripts/dashboard.py       # local trading-activity dashboard
scripts/updown_simulate.py # Up/Down engine vs a synthetic market (offline)
scripts/updown_backtest.py # replay recorded events through the engine
scripts/updown_report.py   # P&L, calibration and settlement-rule report
scripts/updown_dashboard.py # live Up/Down monitor (per-coin model vs market, P&L, feeds)
config/updown.yaml         # Up/Down engine config (strategies, risk, model)
config/updown-15m.yaml     # second engine on the 15-minute markets (extends updown.yaml)
bot/updown/                # Up/Down engine (see "Up/Down engine" above)
tests/                      # pytest unit tests, no network required
```
