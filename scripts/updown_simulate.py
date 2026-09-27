"""Run the Up/Down engine against a synthetic market (no network needed).

    python scripts/updown_simulate.py --windows 48 --seed 1

Exercises the full pipeline and prints P&L per strategy plus a Brier-score
comparison of the model vs the simulated market maker.

The simulated market is inefficient ON PURPOSE (stale, over-cautious,
noisy market maker), so the P&L printed here says nothing about real
Polymarket profitability. Use it to test changes, not to size bets.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bot.updown.config import load_updown_config  # noqa: E402
from bot.updown.journal import UpDownJournal  # noqa: E402
from bot.updown.sim import SimConfig, run_simulation  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--windows", type=int, default=24, help="windows per asset (5 min each)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--config", default=None, help="updown.yaml to use (default: config/updown.yaml)")
    ap.add_argument("--mm-lag", type=int, default=5, help="market maker staleness, seconds")
    ap.add_argument("--mm-vol-mult", type=float, default=1.35, help="market maker vol overestimate")
    ap.add_argument("--mm-noise", type=float, default=0.015, help="market maker probability noise")
    ap.add_argument("--journal-dir", default=None, help="write journals here (e.g. data/sim) for updown_report.py")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    cfg = load_updown_config(args.config)
    cfg.feeds.cex_source = "none"
    sim = SimConfig(windows=args.windows, seed=args.seed, mm_lag_s=args.mm_lag,
                    mm_vol_mult=args.mm_vol_mult, mm_noise=args.mm_noise)
    journal = None
    if args.journal_dir:
        journal = UpDownJournal(args.journal_dir, os.path.join(args.journal_dir, "trades.csv"), mode="sim")
    result = run_simulation(sim, cfg, journal=journal, verbose=args.verbose)
    if journal is not None:
        journal.close()
    print(result.summary())
    if args.journal_dir:
        print(f"\nJournals written to {args.journal_dir}/ -- try: python scripts/updown_report.py --data-dir {args.journal_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
