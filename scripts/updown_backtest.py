"""Replay recorded market data through the Up/Down engine (paper fills).

Record while paper trading:   python -m bot.updown --record
Replay later with any config: python scripts/updown_backtest.py data/recordings/*.jsonl.gz \
                                  --config config/updown.yaml --out data/backtest

The replay feeds the exact same event stream into a fresh engine, so you can
change thresholds/strategies and compare results on identical market data.
Caveat: paper fills can't know how the real market would have reacted to
your orders -- treat the result as optimistic for size.
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bot.updown.config import load_updown_config  # noqa: E402
from bot.updown.engine import Engine  # noqa: E402
from bot.updown.journal import UpDownJournal  # noqa: E402
from bot.updown.orders import PaperBroker  # noqa: E402
from bot.updown.recorder import read_events  # noqa: E402
from bot.updown.risk import UpDownRisk  # noqa: E402
from bot.updown.strategies import build_strategies  # noqa: E402


def replay(paths, cfg, journal=None):
    engine = None
    broker = None
    step = cfg.execution.step_interval_s
    next_step = None
    last = None
    n = 0
    for rt, ev in read_events(paths):
        if engine is None:
            risk = UpDownRisk(cfg.risk, now=rt)
            engine = Engine(cfg, build_strategies(cfg.strategies), risk, journal, mode="backtest")
            broker = PaperBroker(engine)
            next_step = rt
        while next_step <= rt:
            for u in broker.execute(engine.step(next_step), next_step):
                engine.on_order_update(u)
            next_step += step
        engine.handle(ev)
        for u in broker.on_event(ev, rt):
            engine.on_order_update(u)
        last = rt
        n += 1
    if engine is not None:
        end = last + cfg.settlement.settle_grace_s + 60
        while next_step <= end:
            for u in broker.execute(engine.step(next_step), next_step):
                engine.on_order_update(u)
            next_step += step
    return engine, n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="recording files or globs")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default="data/backtest", help="journal output directory")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)-7s %(name)s: %(message)s")

    paths = sorted({p for pattern in args.inputs for p in glob.glob(pattern)})
    if not paths:
        print("No recording files found.")
        return 1
    cfg = load_updown_config(args.config)
    cfg.settlement.use_official = True
    journal = UpDownJournal(args.out, os.path.join(args.out, "trades.csv"), mode="backtest")
    engine, n = replay(paths, cfg, journal)
    journal.close()
    if engine is None:
        print("Recordings contained no events.")
        return 1

    pnl, counts, wins = {}, {}, {}
    for r in engine.results.values():
        for s, p in r["pnl"].items():
            pnl[s] = pnl.get(s, 0.0) + p
            counts[s] = counts.get(s, 0) + 1
            wins[s] = wins.get(s, 0) + (p > 0)
    print(f"Replayed {n} events from {len(paths)} file(s); fills={engine.stats['fills']}")
    for s in sorted(pnl):
        print(f"  {s:<17} windows={counts[s]:<4} win_rate={wins[s] / counts[s]:>5.0%} pnl=${pnl[s]:+.2f}")
    if not pnl:
        print("  (no settled positions)")
    print(f"Journals in {args.out}/ -- python scripts/updown_report.py --data-dir {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
