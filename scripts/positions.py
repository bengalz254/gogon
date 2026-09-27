"""Inspect and adjust the bot's saved positions (data/state_*.sqlite3).

    python scripts/positions.py list                          # paper positions
    python scripts/positions.py --live list                   # live positions
    python scripts/positions.py --live merge <market_id> [--sets N]
    python scripts/positions.py --live close <token_id> --price P [--size N]

Use `merge` after merging complete YES+NO sets on Polymarket, and `close`
after selling or redeeming a position outside the bot, so the bot's
exposure and P&L match reality. Both are also logged in data/trades.csv.

Stop the bot first: it keeps positions in memory and would overwrite
changes made here the next time it saves.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bot.config import RiskConfig  # noqa: E402
from bot.journal import TradeJournal  # noqa: E402
from bot.merge import merge_market  # noqa: E402
from bot.risk import RiskManager  # noqa: E402
from bot.state import StateStore, state_path  # noqa: E402
from bot.strategies.base import Signal  # noqa: E402

STATUS_PATH = os.path.join(_ROOT, "data", "status.json")
# The bot rewrites status.json every cycle; a fresher file means it's running.
RUNNING_IF_UPDATED_WITHIN_SECONDS = 180


def bot_seems_running() -> bool:
    """True if data/status.json says the bot is up: it wasn't marked stopped
    on a clean shutdown and was updated recently (a crashed bot goes stale)."""
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            status = json.load(f)
        if status.get("running") is False:
            return False
        return time.time() - float(status["updated_ts"]) < RUNNING_IF_UPDATED_WITHIN_SECONDS
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def load(live: bool) -> tuple[StateStore, RiskManager]:
    store = StateStore(os.path.join(_ROOT, state_path(live)))
    risk = RiskManager(RiskConfig())
    saved = store.load()
    if saved is not None:
        risk.restore(saved)
    return store, risk


def cmd_list(risk: RiskManager) -> int:
    if not risk.positions:
        print("No open positions.")
        return 0
    by_market = defaultdict(list)
    for pos in risk.positions.values():
        by_market[pos.market_id].append(pos)
    for market_id, positions in by_market.items():
        sets = risk.complete_sets(market_id)
        print(f"market {market_id}" + (f"  — {sets:.2f} complete sets (mergeable)" if sets > 0 else ""))
        for p in positions:
            print(f"  {p.outcome:<10} {p.size:>10.2f} shares  cost ${p.cost_usd:>9.2f}  avg {p.avg_price:.4f}  token {p.token_id}")
    print(f"\nTotal exposure ${risk.total_exposure_usd:.2f} | realized P&L today ${risk.realized_pnl_today:+.2f}")
    return 0


def cmd_merge(risk: RiskManager, journal: TradeJournal, mode: str, market_id: str, sets: float | None) -> int:
    available = risk.complete_sets(market_id)
    if available <= 0:
        print(f"No complete sets in market {market_id}.")
        return 1
    merged, pnl = merge_market(risk, journal, market_id, mode, sets)
    print(f"Recorded a merge of {merged:.2f} sets in {market_id}: realized P&L ${pnl:+.2f}")
    return 0


def cmd_close(risk: RiskManager, journal: TradeJournal, mode: str, token_id: str, price: float, size: float | None) -> int:
    pos = risk.positions.get(token_id)
    if pos is None:
        print(f"No position in token {token_id}.")
        return 1
    size = pos.size if size is None else min(size, pos.size)
    market_id, outcome = pos.market_id, pos.outcome
    pnl = risk.record_close(token_id, size, size * price)
    journal.record(
        Signal(
            strategy="manual",
            market_id=market_id,
            token_id=token_id,
            outcome=outcome,
            side="SELL",
            limit_price=price,
            size_shares=size,
            size_usd=size * price,
            reason="closed outside the bot (recorded with scripts/positions.py)",
        ),
        mode=mode,
        filled=True,
    )
    print(f"Recorded closing {size:.2f} {outcome} at {price:.4f}: realized P&L ${pnl:+.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # --live/--force work before or after the command (SUPPRESS keeps a
    # subcommand from resetting a flag given before it).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--live", action="store_true", default=argparse.SUPPRESS, help="use the live state instead of paper")
    common.add_argument(
        "--force", action="store_true", default=argparse.SUPPRESS, help="edit even if the bot looks like it's running"
    )
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show saved positions", parents=[common])
    merge = sub.add_parser("merge", help="record merged complete sets", parents=[common])
    merge.add_argument("market_id")
    merge.add_argument("--sets", type=float, default=None, help="how many sets (default: all)")
    close = sub.add_parser("close", help="record a position closed outside the bot", parents=[common])
    close.add_argument("token_id")
    close.add_argument("--price", type=float, required=True, help="price received per share (0-1)")
    close.add_argument("--size", type=float, default=None, help="shares (default: the whole position)")
    args = parser.parse_args(argv)

    # Read with getattr: a flag given nowhere is absent (SUPPRESS). Don't use
    # parser.set_defaults for these — it rewrites the actions shared with the
    # subcommands and makes them reset flags given before the command.
    live, force = getattr(args, "live", False), getattr(args, "force", False)
    mode = "live" if live else "paper"
    store, risk = load(live)
    try:
        if args.command == "list":
            return cmd_list(risk)
        if bot_seems_running() and not force:
            print("The bot looks like it's running (data/status.json was updated recently).")
            print("Stop it first, or it will overwrite this change on its next save (--force to proceed anyway).")
            return 2
        journal = TradeJournal(os.path.join(_ROOT, "data", "trades.csv"))
        if args.command == "merge":
            code = cmd_merge(risk, journal, mode, args.market_id, args.sets)
        else:
            if not 0.0 <= args.price <= 1.0:
                print("--price must be between 0 and 1.")
                return 1
            code = cmd_close(risk, journal, mode, args.token_id, args.price, args.size)
        if code == 0:
            store.save(risk.snapshot())
        return code
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
