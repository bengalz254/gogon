# gogon — Polymarket Auto-Trading Bot

An automated trading bot for [Polymarket](https://polymarket.com) built on
Polymarket's official CLOB (Central Limit Order Book) V2 API. It scans active
markets, applies pluggable strategies — fee-aware arbitrage, market making
for liquidity rewards, multi-outcome arbitrage — and executes trades through
a risk manager with hard position/exposure/loss caps.

> Polymarket moved to **CLOB V2** on April 28, 2026 (new exchange contracts,
> new order format, **pUSD** collateral). This bot uses the V2 SDK
> (`py-clob-client-v2`); orders signed by the old `py-clob-client` are
> rejected by the exchange.

> ⚠️ **This is trading software. It can lose real money.** Read the whole
> README, run in paper mode first, and never risk more than you can afford
> to lose. Nothing here is financial advice.

🇮🇩 **Panduan langkah demi langkah dalam Bahasa Indonesia:** [PANDUAN.md](PANDUAN.md)
(dari menyiapkan VPS, mode paper, sampai live).

## How it works

```
main loop — a scan every polling_interval_seconds, quote refreshes in between
  ├─ market data: market list from the CLOB API; order books from the
  │               market WebSocket (live) or batched REST /books requests
  ├─ strategies:  turn order books into Signals / quotes, net of fees
  │    ├─ arbitrage          (ON)  — buy YES+NO when the pair costs < $1 after fees
  │    ├─ market_maker       (OFF) — quote both sides of reward markets
  │    ├─ negrisk_arbitrage  (OFF) — buy YES of every outcome of an event < $1
  │    └─ threshold          (OFF) — mean-reversion on price swings
  ├─ risk:        approves/rejects each Signal (or whole multi-leg group)
  │               against position, exposure & daily-loss caps
  ├─ execution:   simulates fills against the latest book (paper) or signs &
  │               submits orders (live); quoting.py manages resting quotes
  ├─ state:       saves positions + today's P&L to SQLite after every fill
  ├─ housekeeping: merge complete sets (paper), settle resolved markets,
  │               reconcile with Polymarket (live)
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

Polymarket charges **takers** a fee of
`shares × rate × (price × (1 − price))^exponent`, paid in collateral on top
of the price, with the rate set per market (by category: e.g. crypto 0.07,
sports 0.05, politics 0.04, geopolitics 0 as of Sept 2026). The bot's
fill-or-kill orders are always the taker, and near 50¢ the fee on one
YES+NO set is about `rate × 0.5` — e.g. 3.5¢ in a crypto market, more than a
typical 2¢ discount. So the arbitrage strategy only trades when the profit
is still at least `min_edge` **after** fees and `fee_buffer`, and paper mode
charges the same fees so its P&L isn't flattering.

Each market's own fee terms (rate, exponent, whether makers pay) are read
from Polymarket and cached. If that lookup fails, the rates in
`config/settings.yaml` under `fees:` are used, picked by the market's tags;
unrecognized markets get `default_taker_rate` (the highest known rate), so
costs are never under-estimated. **Check the fallback rates against
[Polymarket's fee page](https://docs.polymarket.com/trading/fees) before
going live** — Polymarket changes them.

Paper fills are checked against the latest order book (with depth): a
fill-or-kill order only "fills" if the book still holds the whole size at
the limit price.

The threshold (mean-reversion) strategy is included as a second option but
ships **disabled**, because it's directional and can lose money in a
trending market — only turn it on if you understand that risk.

## Market making (liquidity rewards)

With fees on takers, the structural edge on Polymarket sits with **makers**:
they pay no trading fee (unless a market says otherwise), get part of the
takers' fees back as rebates, and Polymarket pays daily **liquidity
rewards** to resting orders close to the midpoint. `strategies.market_maker`
(off by default) quotes both sides of reward markets to earn all three.

How it quotes:
- **Market choice** — reward markets ranked by daily reward, skipping ones
  that end within `min_days_to_end`, sports games starting within
  `avoid_game_start_hours` (or live), markets that delay taker orders, a
  midpoint outside `min_mid`–`max_mid`, or a reward minimum size your
  `risk.max_position_usd` can't cover. Re-picked every `reselect_minutes`.
- **Prices** — `spread_fraction × max_spread` either side of the midpoint,
  leaning against inventory (`inventory_skew`), rounded to the tick and
  never crossing the book (post-only).
- **Inventory** — held shares are sold before the complement is bought, so
  capital doesn't pile up in YES+NO sets; a side stops at `max_inventory`.
  Resting buys count against the exposure caps as if filled.
- **Protection** — quotes are pulled when the daily loss limit hits, the
  WebSocket feed goes quiet, the midpoint jumps more than `max_mid_move`
  (then the market cools down), or a market stops qualifying.

Live specifics:
- Orders are **post-only GTD** orders that expire about
  `order_ttl_seconds` + 60 s after the bot stops refreshing them.
- A **heartbeat** every 5 s arms Polymarket's dead-man switch: if the bot,
  server or network dies, the exchange cancels all its orders within ~10 s.
- The bot **cancels all open orders on the account** when it starts and
  stops. Use a **dedicated Polymarket account** for the bot.
- It requires `market_data.websocket: true` (it won't quote blind).

Be realistic: the risk is being **picked off** — an informed trader fills a
stale quote right before the price moves — and reward pools are shared
with professional market makers. Paper mode fills quotes from real trades
with a pessimistic queue model, but can't estimate rewards or rebates. Run
it in paper mode for a while, start live with small caps, and compare with
your reward earnings on Polymarket.

## Multi-outcome arbitrage

`strategies.negrisk_arbitrage` (off by default) buys the YES of **every**
outcome of a multi-outcome ("negative risk") event when the whole set costs
under $1 after each leg's fee — exactly one outcome resolves YES, so a full
set pays $1. It only considers events where "every outcome" can be
established: known not to be *augmented* (augmented events can add
outcomes later), no "Other" placeholder, and every outcome open and
trading. Any doubt skips the event. The legs go out in one request, but
with several legs a partial fill (a directional bet) is more likely than
in binary arbitrage, and the sets can't be merged early — they're held
until the event resolves. Each leg must also clear the $1 minimum order,
which small caps often can't.

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
  - `POLY_FUNDER_ADDRESS` — the address holding your funds (pUSD). If you trade
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
- **Books stream live.** Order books come over Polymarket's market
  WebSocket (reconnecting on its own); whenever it's down the bot falls
  back to REST, fetching books in batches.
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

## Merging complete sets and fixing positions

One share of every outcome of a market (a "complete set", e.g. from binary
arbitrage) is always worth $1, and Polymarket's **Merge** turns it back
into $1 of pUSD right away, freeing the capital. In paper mode the bot does
this automatically. In live mode it alerts you when sets are worth merging
(`merge.live_alert_min_sets`); it doesn't send the on-chain merge itself
yet. Merge on Polymarket, then — with the bot stopped — record it:

```bash
python scripts/positions.py --live list                  # saved positions, mergeable sets
python scripts/positions.py --live merge <market_id>     # after merging on Polymarket
python scripts/positions.py --live close <token_id> --price 0.93   # sold/redeemed outside the bot
```

(Drop `--live` for the paper state.) The script refuses to edit while the
bot is running, since the bot would overwrite the change on its next save.

## Alerts (Telegram)

With `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set in `.env`, the bot
messages you when it starts or stops, fills a trade (turn off with
`notifications.fills: false`), settles a resolved market, hits or clears
the daily loss limit, fills only one leg of an arbitrage, sees positions
that don't match Polymarket, finds no markets to scan, has complete sets
worth merging, changes the markets it quotes, or keeps hitting errors. It also sends a status message every
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

Tests cover the logic with no network calls (fees, risk limits and
mark-to-market, arbitrage and multi-outcome sizing, grouped execution, V2
order responses, order books and the WebSocket feed, paper fill
simulation, market-making quotes and order management, heartbeats,
merging, state persistence, reconciliation, alerts), plus main-loop runs
against fake exchanges, so they're safe and fast to run anytime.

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
- Market making keeps orders resting on the book: read
  [Market making](#market-making-liquidity-rewards) before enabling it, and
  give the bot its own Polymarket account (it cancels all open orders on
  the account at start and stop).
- Arbitrage positions are held until the market resolves. Every 10
  minutes the bot checks the markets it holds and settles resolved ones in
  its own books at the payout ($1 winner / $0 loser), which realizes the
  P&L and frees the exposure. In live mode you still redeem the pUSD on
  Polymarket yourself; merging complete sets earlier is manual (see
  [Merging complete sets](#merging-complete-sets-and-fixing-positions)).
- In live mode, order fills are tracked based on the CLOB API's response to
  each order submission, and fees are the bot's estimate. The hourly
  reconciliation flags drift, but still check the Polymarket UI — don't rely
  solely on the bot's own tracking for anything you haven't verified.
- The arbitrage strategy currently only handles simple **binary
  (two-outcome)** markets.
- This code has not been run against the live Polymarket API from this
  environment (no network access here) — including CLOB V2 order
  placement, the market WebSocket, the heartbeat, per-market fee terms,
  the Gamma events used for multi-outcome arbitrage and the Data API used
  for reconciliation. Treat
  `scripts/check_setup.py` and a few days of paper trading as your first
  real-world check, and review the code yourself before trusting it with
  funds.

## Project layout

```
bot/
  config.py          # loads .env + config/settings.yaml
  client.py           # wraps py-clob-client's ClobClient
  market_data.py       # market discovery + parsing (rewards, dates, ...)
  orderbook.py          # depth order books from snapshots and deltas
  ws_market.py          # market WebSocket feed (live books, trades)
  books.py              # book source: WebSocket, else batched REST
  fees.py               # Polymarket fee model (per-market terms)
  risk.py               # position/exposure/loss limits, mark-to-market
  execution.py           # paper vs. live order execution, grouped legs
  paper.py               # paper fill simulation (depth, queue model)
  quoting.py             # market maker's resting orders and fills
  heartbeat.py           # Polymarket dead-man switch for resting orders
  merge.py               # merging complete sets
  gamma.py               # multi-outcome events (Gamma API)
  journal.py              # CSV trade log
  state.py                 # SQLite persistence of positions + daily P&L
  settlement.py            # settles positions in resolved markets
  reconcile.py             # compares positions with Polymarket's Data API
  notify.py                # Telegram alerts
  main.py                  # the scan-evaluate-execute loop
  strategies/
    base.py                # Signal + Strategy interface
    arbitrage.py            # complete-set arbitrage (default, on)
    market_maker.py          # liquidity-reward quoting (default, off)
    negrisk_arbitrage.py     # multi-outcome arbitrage (default, off)
    threshold.py             # mean-reversion (default, off)
config/settings.yaml    # strategy, fee, risk & alert parameters (no secrets)
.env.example             # secrets template (copy to .env)
scripts/check_setup.py    # pre-flight sanity check (+ Telegram test)
scripts/positions.py      # list saved positions, record merges/closes
scripts/dashboard.py       # local trading-activity dashboard
Dockerfile, docker-compose.yml  # run 24/7 with Docker
deploy/gogon-bot.service        # run 24/7 with systemd
tests/                      # pytest unit tests, no network required
```
