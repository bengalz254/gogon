"""Backtest the DCA config on historical candles.

    python scripts/dca_backtest.py --days 180            # download SOL candles from Binance
    python scripts/dca_backtest.py --csv data/sol_15m.csv # or use a CSV: timestamp_ms,open,high,low,close[,volume]

Fills use each candle's high/low with conservative ordering (see dca/paper.py).
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dca.config import DcaSettings, load_dca_settings  # noqa: E402
from dca.engine import DcaEngine  # noqa: E402
from dca.journal import DcaJournal  # noqa: E402
from dca.paper import PaperBroker  # noqa: E402
from dca.signals import candles_needed  # noqa: E402
from dca.state import StateStore  # noqa: E402



def load_csv(path: str) -> list[list[float]]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            try:
                rows.append([float(x) for x in row[:5]])
            except ValueError:
                continue  # header
    rows.sort(key=lambda r: r[0])
    return rows


def download(symbol: str, timeframe: str, days: int, ex=None) -> list[list[float]]:
    """Page through Binance klines from `days` ago until now.

    Binance caps how many candles one request returns (currently 1000), so keep
    requesting from the last timestamp until the present is reached.
    """
    if ex is None:
        import ccxt

        ex = ccxt.binanceusdm({"enableRateLimit": True})
    now = ex.milliseconds()
    since = now - days * 86_400_000
    by_ts: dict[float, list[float]] = {}
    while since < now:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        for row in batch:
            by_ts[row[0]] = row
        next_since = batch[-1][0] + 1
        if next_since <= since:
            break  # no progress; avoid looping forever
        since = next_since
        print(f"\rdownloaded {len(by_ts)} candles...", end="", flush=True)
        time.sleep(getattr(ex, "rateLimit", 0) / 1000)
    print()
    rows = [by_ts[t] for t in sorted(by_ts)]
    return rows[:-1]  # drop the still-forming candle


def save_csv(path: str, candles: list[list[float]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_ms", "open", "high", "low", "close", "volume"])
        w.writerows(candles)


def run_backtest(settings: DcaSettings, candles: list[list[float]], journal_path: str | None = None) -> dict:
    engine = DcaEngine(settings, PaperBroker(settings), StateStore(None), DcaJournal(journal_path))
    closes = [c[4] for c in candles]
    window = candles_needed(settings.entry)
    equity_peak = 0.0
    max_dd = 0.0
    for i, (ts, _o, high, low, close) in enumerate(c[:5] for c in candles):
        now = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        engine.tick(now, closes[max(0, i - window + 1): i + 1], low, high, close)
        equity = engine.state.total_pnl + sum(d.unrealized_pnl(close) for d in engine.state.deals.values())
        equity_peak = max(equity_peak, equity)
        max_dd = max(max_dd, equity_peak - equity)

    deals = engine.closed_deals
    by_reason: dict[str, int] = {}
    for d in deals:
        by_reason[d.close_reason] = by_reason.get(d.close_reason, 0) + 1
    last = candles[-1][4]
    return {
        "candles": len(candles),
        "from": datetime.fromtimestamp(candles[0][0] / 1000, tz=timezone.utc).date().isoformat(),
        "to": datetime.fromtimestamp(candles[-1][0] / 1000, tz=timezone.utc).date().isoformat(),
        "price_change_pct": (last / candles[0][4] - 1) * 100,
        "closed_deals": len(deals),
        "by_reason": by_reason,
        "by_side": {s: sum(1 for d in deals if d.side == s) for s in settings.sides},
        "realized_pnl": engine.state.total_pnl,
        "open_deals": len(engine.state.deals),
        "unrealized_pnl": sum(d.unrealized_pnl(last) for d in engine.state.deals.values()),
        "max_drawdown": max_dd,
        "max_safety_orders_used": max((d.safety_orders_filled for d in deals), default=0),
        "fees": sum(d.fees_usdt for d in deals),
        "net_pnl": engine.state.total_pnl + sum(d.unrealized_pnl(last) for d in engine.state.deals.values()),
        "per_side": {
            side: {
                "deals": sum(1 for d in deals if d.side == side),
                "stops": sum(1 for d in deals if d.side == side and d.close_reason != "take_profit"),
                "realized": sum(d.realized_pnl for d in deals if d.side == side),
                "unrealized": sum(d.unrealized_pnl(last) for d in engine.state.deals.values() if d.side == side),
            }
            for side in settings.sides
        },
        "stops": [d for d in deals if d.close_reason != "take_profit"],
        "open": list(engine.state.deals.values()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--config")
    ap.add_argument("--journal", default=None, help="optional CSV path for every simulated event")
    args = ap.parse_args()

    logging.getLogger("dcabot").setLevel(logging.WARNING)
    s = load_dca_settings(args.config)
    if args.csv:
        candles = load_csv(args.csv)
    else:
        candles = download(s.symbol, s.timeframe, args.days)
        cache = os.path.join(_ROOT, "data", f"candles_{s.symbol.split(':')[0].replace('/', '')}_{s.timeframe}_{args.days}d.csv")
        save_csv(cache, candles)
        print(f"saved candles to {cache} (re-run with --csv to skip downloading)")
    if len(candles) < 50:
        sys.exit("not enough candles")
    r = run_backtest(s, candles, args.journal)

    print(f"\nBacktest {s.symbol} {s.timeframe} | {r['from']} -> {r['to']} ({r['candles']} candles) "
          f"| price {r['price_change_pct']:+.1f}%")
    print(f"closed deals     : {r['closed_deals']}  {r['by_reason']}  per side {r['by_side']}")
    print(f"realized PnL     : ${r['realized_pnl']:.2f}  ({r['realized_pnl'] / s.capital_usdt:+.1%} of capital)")
    print(f"fees paid        : ${r['fees']:.2f}")
    print(f"open at end      : {r['open_deals']} deal(s), unrealized ${r['unrealized_pnl']:.2f}")
    print(f"max drawdown     : ${r['max_drawdown']:.2f}")
    print(f"max SO used      : {r['max_safety_orders_used']}/{s.ladder.max_safety_orders}")
    print(f"NET PnL          : ${r['net_pnl']:.2f}  (realized + unrealized)")
    for side, v in r["per_side"].items():
        print(f"  {side:<5} deals {v['deals']:>4} | stops {v['stops']} | realized ${v['realized']:>8.2f} "
              f"| unrealized ${v['unrealized']:>8.2f}")
    for d in r["stops"]:
        print(f"  STOP  {d.side:<5} opened {d.opened_at[:16]} closed {d.closed_at[:16]} "
              f"entry {d.entry_price:.3f} -> {d.close_price:.3f} | PnL ${d.realized_pnl:.2f}")
    for d in r["open"]:
        print(f"  OPEN  {d.side:<5} opened {d.opened_at[:16]} entry {d.entry_price:.3f} avg {d.avg_price:.3f} "
              f"SO {d.safety_orders_filled}/{len(d.orders) - 1}")


if __name__ == "__main__":
    main()
