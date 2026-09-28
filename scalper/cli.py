"""Command line interface: python -m scalper <command>

  run         start the bot (mode from .env SCALPER_MODE or --mode; paper by default)
  backtest    test the strategy on historical data (downloads + caches candles)
  optimize    small parameter grid on in-sample data, validated out-of-sample
  download    only download / refresh the candle cache
  check       pre-flight checks: config, connectivity, keys, account settings, sizing
  status      show open trades, risk state and journal stats from the state file
  reset-risk  clear a drawdown halt / cooldowns (after you've reviewed why it stopped)
  dashboard   local web dashboard of trades and equity
"""
from __future__ import annotations

import argparse
import itertools
import logging
import math
import os
import signal
import sys
import time

from scalper import __version__
from scalper.config import Settings, credentials_from_env, load_settings, validate
from scalper.logger import setup_logging

logger = logging.getLogger("scalper")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class InstanceLock:
    """OS-level lock so two copies of the bot can never trade the same account
    in the same mode at once (the lock dies with the process)."""

    def __init__(self, path: str):
        self.path = path
        self._f = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._f.close()
            self._f = None
            return False
        return True

    def release(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


def _paths(settings: Settings) -> dict[str, str]:
    d = settings.data_dir
    return {
        "journal": os.path.join(d, f"trades_{settings.mode}.csv"),
        "state": os.path.join(d, f"state_{settings.mode}.json"),
        "lock": os.path.join(d, f"bot_{settings.mode}.lock"),
        "klines": os.path.join(d, "klines"),
        "backtests": os.path.join(d, "backtests"),
    }


def _load(args, mode: str | None = None) -> Settings:
    settings = load_settings(config_path=args.config, mode_override=mode or getattr(args, "mode", None))
    for w in validate(settings):
        logger.warning(w)
    return settings


def _public_client(settings: Settings, live_data: bool = False):
    from scalper.exchange import BinanceFuturesClient

    url = settings.credentials.public_base_url
    if live_data:
        url = credentials_from_env("live").public_base_url
    return BinanceFuturesClient(url, recv_window=settings.execution.recv_window_ms)


def _apply_overrides(settings: Settings, args) -> None:
    if getattr(args, "timeframe", None):
        settings.timeframe = args.timeframe
    if getattr(args, "strategy", None):
        settings.strategy.name = args.strategy
    if getattr(args, "symbols", None):
        settings.symbols = [s.upper() for s in args.symbols]
    validate(settings)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def cmd_run(args) -> int:
    settings = _load(args)
    setup_logging(settings.log_dir)
    paths = _paths(settings)

    lock = InstanceLock(paths["lock"])
    if not lock.acquire():
        logger.error("Another scalper instance is already running in %s mode. Refusing to start.", settings.mode)
        return 1

    from scalper.engine import Engine, MarketData
    from scalper.exchange import (
        BinanceAPIError,
        BinanceBroker,
        BinanceFuturesClient,
        BrokerError,
        PaperBroker,
        parse_symbol_rules,
    )
    from scalper.journal import StateStore, TradeJournal
    from scalper.notifier import Notifier

    try:
        if settings.mode == "live":
            banner = (
                "\n" + "!" * 72 + "\n"
                "  LIVE TRADING: real orders with REAL money on Binance Futures.\n"
                f"  Symbols: {', '.join(settings.symbols)} | risk/trade {settings.risk.risk_per_trade_pct}% | "
                f"leverage {settings.execution.leverage}x | daily loss cap {settings.risk.max_daily_loss_pct}%\n"
                + "!" * 72
            )
            print(banner)
            if not args.yes:
                for i in range(10, 0, -1):
                    print(f"  Starting in {i}s... (Ctrl+C to abort)", end="\r", flush=True)
                    time.sleep(1)
                print()

        creds = settings.credentials
        if settings.mode == "paper":
            client = BinanceFuturesClient(creds.public_base_url, recv_window=settings.execution.recv_window_ms)
        else:
            client = BinanceFuturesClient(
                creds.base_url, creds.api_key, creds.api_secret, recv_window=settings.execution.recv_window_ms
            )
        client.sync_time()
        rules = parse_symbol_rules(client.exchange_info(), settings.symbols)
        market = MarketData(client, settings.timeframe)
        if settings.mode == "paper":
            broker = PaperBroker(settings, rules, quote_fn=market.quote, clock=lambda: client.now_ms() / 1000)
        else:
            broker = BinanceBroker(client, settings, rules)
        notifier = Notifier(
            creds.telegram_token, creds.telegram_chat_id,
            prefix="", enabled=settings.notify.telegram_enabled,
        )
        engine = Engine(
            settings, broker, market, rules,
            journal=TradeJournal(paths["journal"]),
            store=StateStore(paths["state"]),
            notifier=notifier,
        )

        def _stop(signum, frame):
            logger.info("Stop requested (signal %s); finishing the current step...", signum)
            engine.stop()

        signal.signal(signal.SIGINT, _stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _stop)

        engine.run()
        notifier.close()
        return 0
    except KeyboardInterrupt:
        logger.info("Aborted before start.")
        return 1
    except (BrokerError, BinanceAPIError) as e:
        logger.error("Cannot start: %s", e)
        if isinstance(e, BinanceAPIError) and e.code in (-2014, -2015, -1022):
            logger.error("Check the API key/secret, that Futures is enabled on the key, and the IP whitelist. "
                         "Testnet keys only work with SCALPER_MODE=testnet.")
        return 1
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# backtest / optimize / download
# ---------------------------------------------------------------------------
def _get_candles(settings: Settings, args, symbol: str):
    from scalper.data import load_csv, load_history

    if getattr(args, "csv", None):
        return load_csv(args.csv, settings.timeframe)
    client = _public_client(settings, live_data=True)
    now = client.server_time()
    days = args.days or settings.backtest.days
    return load_history(client, symbol, settings.timeframe, days, _paths(settings)["klines"], now)


def _get_rules(settings: Settings, symbols: list[str], offline: bool):
    if offline:
        return {}
    from scalper.exchange import parse_symbol_rules

    try:
        client = _public_client(settings, live_data=True)
        return parse_symbol_rules(client.exchange_info(), symbols)
    except Exception as e:  # noqa: BLE001 - backtests still work without exchange filters
        logger.warning("Could not load exchange rules (%s); backtesting without lot-size/min-notional checks", e)
        return {}


def cmd_backtest(args) -> int:
    from scalper.backtest import run_backtest
    from scalper.report import format_summary, write_outputs

    settings = _load(args, mode="paper")
    setup_logging(settings.log_dir)
    _apply_overrides(settings, args)
    if args.csv and len(settings.symbols) != 1:
        settings.symbols = settings.symbols[:1]
    balance = args.balance or settings.backtest.starting_balance
    oos = settings.backtest.oos_fraction if args.oos is None else args.oos
    rules = _get_rules(settings, settings.symbols, offline=bool(args.csv))

    results = []
    for sym in settings.symbols:
        candles = _get_candles(settings, args, sym)
        if not candles:
            logger.error("%s: no candles", sym)
            continue
        logger.info("%s: backtesting %d candles", sym, len(candles))
        results.append(run_backtest(candles, settings, sym, rules.get(sym), balance))
    if not results:
        return 1

    label = f"{settings.strategy.name} {settings.timeframe}"
    print(format_summary(label, results, oos))
    out_dir = args.out or os.path.join(_paths(settings)["backtests"], time.strftime("%Y%m%d-%H%M%S"))
    paths = write_outputs(out_dir, label, results, oos)
    print(f"\nSaved: {paths['html']}\n       {paths['trades']}")
    return 0


GRIDS = {
    "trend_pullback": {
        "tp_r": [1.2, 1.5, 2.0],
        "min_sl_atr": [0.8, 1.2],
        "adx_min": [0.0, 18.0, 25.0],
        "rsi_pullback": [(40.0, 60.0), (45.0, 55.0)],
    },
    "range_reversion": {
        "bb_std": [1.8, 2.0, 2.5],
        "rsi_band": [(20.0, 80.0), (25.0, 75.0), (30.0, 70.0)],
        "adx_max": [15.0, 20.0, 25.0],
        "min_rr": [0.8, 1.2],
    },
}


def _apply_combo(settings: Settings, combo: dict) -> None:
    name = settings.strategy.name
    p = getattr(settings.strategy, name)
    for k, v in combo.items():
        if k == "rsi_pullback":
            p.rsi_pullback_long, p.rsi_pullback_short = v
        elif k == "rsi_band":
            p.rsi_oversold, p.rsi_overbought = v
        else:
            setattr(p, k, v)


def cmd_optimize(args) -> int:
    import copy

    from scalper.backtest import compute_stats, run_backtest

    settings = _load(args, mode="paper")
    setup_logging(settings.log_dir)
    _apply_overrides(settings, args)
    if args.csv and len(settings.symbols) != 1:
        settings.symbols = settings.symbols[:1]
    grid = GRIDS[settings.strategy.name]
    oos = settings.backtest.oos_fraction if args.oos is None else args.oos
    balance = args.balance or settings.backtest.starting_balance
    rules = _get_rules(settings, settings.symbols, offline=bool(args.csv))

    data = {}
    for sym in settings.symbols:
        candles = _get_candles(settings, args, sym)
        if len(candles) < 500:
            logger.error("%s: not enough candles (%d)", sym, len(candles))
            return 1
        cut = int(len(candles) * (1 - oos))
        data[sym] = (candles[:cut], candles[cut:])

    keys = list(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]
    print(f"Testing {len(combos)} combinations on in-sample data ({(1 - oos):.0%}), "
          f"then checking the best on the unseen last {oos:.0%}...")

    def evaluate(s: Settings, part: int) -> dict:
        trades, start, net = [], 0.0, 0.0
        for sym, parts in data.items():
            r = run_backtest(parts[part], s, sym, rules.get(sym), balance)
            trades += r.trades
            start += r.start_equity
            net += r.end_equity - r.start_equity
        trades.sort(key=lambda t: t.entry_time)
        eq, curve = start, [(0, start)]
        for t in trades:
            eq += t.net_pnl
            curve.append((t.exit_time, eq))
        st = compute_stats(trades, curve, start)
        st["score"] = st["avg_r"] * math.sqrt(st["trades"]) if st["trades"] >= 20 else -math.inf
        return st

    rows = []
    for i, combo in enumerate(combos, 1):
        s = copy.deepcopy(settings)
        _apply_combo(s, combo)
        rows.append((combo, evaluate(s, 0)))
        if sys.stdout.isatty():
            print(f"  {i}/{len(combos)}", end="\r", flush=True)
    rows.sort(key=lambda r: r[1]["score"], reverse=True)
    top = rows[: args.top]

    def describe(combo: dict) -> str:
        return ", ".join(f"{k}={v}" for k, v in combo.items())

    def pf(v: float) -> str:
        return "inf" if math.isinf(v) else f"{v:.2f}"

    width = max([len(describe(c)) for c, _ in top] + [6])
    print("\nTop settings by in-sample score (avg R × √trades), with their OUT-OF-SAMPLE result:")
    print(f"  {'params':{width}} {'IS trades':>9} {'IS PF':>6} {'IS net':>9} | {'OOS trades':>10} {'OOS PF':>7} {'OOS net':>9}")
    for combo, st in top:
        s = copy.deepcopy(settings)
        _apply_combo(s, combo)
        o = evaluate(s, 1)
        print(f"  {describe(combo):{width}} {st['trades']:>9} {pf(st['profit_factor']):>6} {st['net_profit']:>+9.2f} | "
              f"{o['trades']:>10} {pf(o['profit_factor']):>7} {o['net_profit']:>+9.2f}")
    print(
        "\nHow to read this: only the OOS columns are an honest test. Prefer settings that are\n"
        "good on BOTH sides and whose neighbours are also good (a plateau), not a lone spike.\n"
        "Put your choice into config/scalper.yaml, then paper trade it."
    )
    return 0


def cmd_download(args) -> int:
    from scalper.data import load_history

    settings = _load(args, mode="paper")
    setup_logging(settings.log_dir)
    _apply_overrides(settings, args)
    client = _public_client(settings, live_data=True)
    now = client.server_time()
    for sym in settings.symbols:
        candles = load_history(client, sym, settings.timeframe, args.days or settings.backtest.days,
                               _paths(settings)["klines"], now)
        print(f"{sym} {settings.timeframe}: {len(candles)} candles cached")
    return 0


# ---------------------------------------------------------------------------
# check / status / reset-risk / dashboard
# ---------------------------------------------------------------------------
def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def cmd_check(args) -> int:
    from scalper.engine import MarketData
    from scalper.exchange import BinanceAPIError, BinanceFuturesClient, parse_symbol_rules
    from scalper.risk import size_position
    from scalper.strategies import build_strategy

    try:
        settings = load_settings(config_path=args.config, mode_override=args.mode)
    except Exception as e:  # noqa: BLE001
        _fail(f"configuration: {e}")
        return 1
    print(f"Scalper {__version__} pre-flight check — mode {settings.mode.upper()}")
    print(f"  strategy {settings.strategy.name} on {settings.timeframe} | symbols {', '.join(settings.symbols)}")
    print(f"  risk/trade {settings.risk.risk_per_trade_pct}% | max positions {settings.risk.max_open_positions} | "
          f"daily loss cap {settings.risk.max_daily_loss_pct}% | drawdown halt {settings.risk.max_drawdown_pct}% | "
          f"leverage {settings.execution.leverage}x {settings.execution.margin_type}")
    for w in validate(settings):
        _warn(w)
    failures = 0

    creds = settings.credentials
    client = BinanceFuturesClient(creds.base_url, creds.api_key, creds.api_secret,
                                  recv_window=settings.execution.recv_window_ms)
    try:
        t0 = time.time()
        client.ping()
        latency = (time.time() - t0) * 1000
        offset = client.sync_time()
        _ok(f"reached {creds.base_url} ({latency:.0f} ms, clock offset {offset} ms)")
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot reach {creds.base_url}: {e}")
        return 1

    try:
        rules = parse_symbol_rules(client.exchange_info(), settings.symbols)
        for sym, r in rules.items():
            _ok(f"{sym}: tick {r.tick_size} | step {r.market_step_size} | min notional {r.min_notional} {r.margin_asset}")
    except Exception as e:  # noqa: BLE001
        _fail(str(e))
        return 1

    balance = settings.paper.starting_balance
    if settings.mode != "paper":
        try:
            from scalper.exchange import BinanceBroker

            broker = BinanceBroker(client, settings, rules)
            wallet, available = broker.balances()
            balance = wallet
            _ok(f"API key works: wallet {wallet:.2f}, available {available:.2f}")
            if wallet <= 0:
                _fail("futures wallet balance is 0 — transfer funds to USDⓈ-M futures")
                failures += 1
            if client.position_mode():
                msg = "account is in Hedge Mode; the bot needs One-way Mode"
                if settings.execution.auto_one_way_mode:
                    _warn(msg + " (will be switched automatically at start)")
                else:
                    _fail(msg + " (Binance Futures → Preferences → Position Mode)")
                    failures += 1
            else:
                _ok("One-way position mode")
            positions = broker.positions()
            for sym in settings.symbols:
                pos = positions.get(sym)
                orders = broker.open_orders(sym)
                if pos or orders:
                    _warn(f"{sym}: existing position {pos.qty if pos else 0} and {len(orders)} open order(s); "
                          "the bot will adopt/clean these up on start")
            if settings.mode == "live":
                try:
                    perms = client.api_restrictions()
                    if perms.get("enableWithdrawals"):
                        _fail("this API key can WITHDRAW funds. Disable withdrawals on the key!")
                        failures += 1
                    else:
                        _ok("withdrawals disabled on this key")
                    if not perms.get("enableFutures"):
                        _fail("futures trading is not enabled on this API key")
                        failures += 1
                    if not perms.get("ipRestrict"):
                        _warn("key is not IP-restricted; restricting it to your server's IP is safer")
                except BinanceAPIError as e:
                    _warn(f"could not read key permissions: {e.msg}")
        except BinanceAPIError as e:
            _fail(f"signed request failed: {e} (wrong key/secret, testnet vs live mix-up, or IP not whitelisted?)")
            return 1

    print("  Sizing preview (with current volatility):")
    for sym in settings.symbols:
        try:
            strat = build_strategy(settings, sym)
            candles = MarketData(client, settings.timeframe).closed_candles(sym, 300)
            for c in candles:
                strat.on_candle(c)
            atr = strat.atr
            px = candles[-1].close
            p = getattr(settings.strategy, settings.strategy.name)
            stop_dist = getattr(p, "min_sl_atr", 1.0) * atr
            res = size_position(balance, balance, px, px - stop_dist, rules[sym], settings.risk,
                                settings.costs, settings.execution.leverage)
            needed = settings.risk.min_sl_cost_ratio * settings.costs.round_trip_cost
            vol_note = "passes the fee filter" if stop_dist / px >= needed else (
                f"under the {needed:.3%} fee filter now, the bot waits for more volatility"
            )
            if res.ok:
                _ok(f"{sym}: ATR {atr / px:.3%} | min stop {stop_dist / px:.3%}, {vol_note} | "
                    f"e.g. qty {res.qty:g} = {res.notional:.2f} notional, risk {res.risk_usd:.2f}")
            else:
                _warn(f"{sym}: {res.reason}")
        except Exception as e:  # noqa: BLE001
            _warn(f"{sym}: sizing preview failed: {e}")

    if settings.notify.telegram_enabled:
        _ok("Telegram notifications enabled")
    print("\nAll checks passed." if not failures else f"\n{failures} problem(s) found — fix them before running.")
    return 0 if not failures else 1


def cmd_status(args) -> int:
    from scalper.journal import StateStore, TradeJournal
    from scalper.risk import RiskGuard
    from scalper.trade import Trade

    settings = _load(args)
    paths = _paths(settings)
    state = StateStore(paths["state"]).peek()
    print(f"Mode {settings.mode.upper()} | state file {paths['state']}")
    if not state:
        print("  No state yet (bot has not run in this mode).")
    else:
        g = RiskGuard.from_dict(settings.risk, state.get("risk"))
        print(f"  saved at {state.get('saved_at')} UTC")
        print(f"  today: {g.s.realized_today:+.2f} in {g.s.trades_today} trade(s) | drawdown {g.drawdown_pct:.1f}%"
              f" | loss streak {g.s.loss_streak}")
        if g.s.halted:
            print(f"  HALTED: {g.s.halt_reason}")
        for sym, t in (state.get("trades") or {}).items():
            tr = Trade.from_dict(t)
            print(f"  OPEN {sym} {tr.side} {tr.qty:g} @ {tr.entry_price:.6g} | stop {tr.stop:.6g} ({tr.stop_kind}) "
                  f"| target {tr.take_profit:.6g} | {tr.bars_held} bars")
        if "paper" in state:
            print(f"  paper balance: {state['paper'].get('balance', 0):.2f}")
    rows = TradeJournal(paths["journal"]).read() if os.path.exists(paths["journal"]) else []
    if rows:
        nets = [float(r["net_pnl"]) for r in rows]
        wins = [n for n in nets if n > 0]
        losses = [-n for n in nets if n <= 0]
        pf = sum(wins) / sum(losses) if sum(losses) > 0 else math.inf
        print(f"  journal: {len(rows)} closed trades | net {sum(nets):+.2f} | win {len(wins) / len(rows):.0%} | PF {pf:.2f}")
    return 0


def cmd_reset_risk(args) -> int:
    from scalper.journal import StateStore
    from scalper.risk import RiskGuard

    settings = _load(args)
    paths = _paths(settings)
    lock = InstanceLock(paths["lock"])
    if not lock.acquire():
        print("Stop the running bot first.")
        return 1
    try:
        store = StateStore(paths["state"])
        state = store.load()
        g = RiskGuard.from_dict(settings.risk, state.get("risk"))
        g.reset_halt()
        g.s.loss_streak = 0
        g.s.cooldown_until = 0
        g.s.symbol_cooldown_until = {}
        state["risk"] = g.to_dict()
        store.save(state)
        print("Risk halt, cooldowns and loss streak cleared. Drawdown now measured from the current level.")
        return 0
    finally:
        lock.release()


def cmd_dashboard(args) -> int:
    from scalper.dashboard import serve

    settings = _load(args)
    serve(settings, port=args.port, open_browser=not args.no_browser)
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scalper", description="Binance USDⓈ-M futures scalping bot")
    p.add_argument("--config", help="path to YAML config (default config/scalper.yaml)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="start the bot")
    r.add_argument("--mode", choices=["paper", "testnet", "live"])
    r.add_argument("--yes", action="store_true", help="skip the 10s live-mode countdown")
    r.set_defaults(func=cmd_run)

    for name, func, helptext in (
        ("backtest", cmd_backtest, "backtest on historical candles"),
        ("optimize", cmd_optimize, "grid search with out-of-sample validation"),
        ("download", cmd_download, "download candles into the cache"),
    ):
        b = sub.add_parser(name, help=helptext)
        b.add_argument("--symbols", nargs="+")
        b.add_argument("--days", type=float)
        b.add_argument("--timeframe")
        b.add_argument("--strategy", choices=list(GRIDS))
        if name != "download":
            b.add_argument("--balance", type=float)
            b.add_argument("--oos", type=float, help="out-of-sample fraction, e.g. 0.3")
        if name in ("backtest", "optimize"):
            b.add_argument("--csv", help="use a local CSV (e.g. from data.binance.vision) instead of downloading")
        if name == "backtest":
            b.add_argument("--out", help="output folder for the report")
        if name == "optimize":
            b.add_argument("--top", type=int, default=10)
        b.set_defaults(func=func)

    c = sub.add_parser("check", help="pre-flight checks")
    c.add_argument("--mode", choices=["paper", "testnet", "live"])
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("status", help="show state and journal stats")
    s.add_argument("--mode", choices=["paper", "testnet", "live"])
    s.set_defaults(func=cmd_status)

    rr = sub.add_parser("reset-risk", help="clear a halt / cooldowns")
    rr.add_argument("--mode", choices=["paper", "testnet", "live"])
    rr.set_defaults(func=cmd_reset_risk)

    d = sub.add_parser("dashboard", help="local web dashboard")
    d.add_argument("--mode", choices=["paper", "testnet", "live"])
    d.add_argument("--port", type=int, default=8766)
    d.add_argument("--no-browser", action="store_true")
    d.set_defaults(func=cmd_dashboard)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # never crash on a console without UTF-8
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if not logging.getLogger("scalper").handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        return args.func(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
