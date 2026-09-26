"""Merging complete sets (one share of every outcome) back into collateral.

A complete set always pays exactly $1, so merging it early frees the capital
(and the exposure it uses) without changing the profit. Polymarket's Merge is
an on-chain call (CTF mergePositions); since the April 2026 move to CLOB V2
and pUSD collateral the bot doesn't send it itself. So:

- paper mode merges complete sets automatically (bookkeeping only), as if
  you pressed Merge;
- live mode alerts you when sets are worth merging. Merge them on
  Polymarket, then record it with
  `python scripts/positions.py --live merge <market_id>` (with the bot stopped).
"""
from __future__ import annotations

import logging

from bot.journal import TradeJournal
from bot.risk import RiskManager
from bot.strategies.base import Signal

logger = logging.getLogger("polybot.merge")


def merge_market(
    risk: RiskManager, journal: TradeJournal | None, market_id: str, mode: str, sets: float | None = None
) -> tuple[float, float]:
    """Merge complete sets in one market (all of them by default).

    Returns (sets merged, realized P&L). Each outcome's share is logged in the
    trade journal as a SELL at its slice of the $1, so the dashboard agrees.
    """
    legs = risk.record_merge(market_id, sets)
    if not legs:
        return 0.0, 0.0
    if journal is not None:
        for leg in legs:
            try:
                journal.record(
                    Signal(
                        strategy="merge",
                        market_id=market_id,
                        token_id=leg.token_id,
                        outcome=leg.outcome,
                        side="SELL",
                        limit_price=leg.proceeds_usd / leg.size,
                        size_shares=leg.size,
                        size_usd=leg.proceeds_usd,
                        reason="complete sets merged back into collateral",
                    ),
                    mode=mode,
                    filled=True,
                )
            except Exception:
                logger.exception("Failed to write the trade journal for merged token %s", leg.token_id)
    merged, pnl = legs[0].size, sum(leg.pnl for leg in legs)
    logger.info("Merged %.2f complete sets in market %s — realized P&L $%.2f", merged, market_id, pnl)
    return merged, pnl
