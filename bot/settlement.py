"""Settle positions in markets that have resolved.

When a market resolves, each winning share is worth exactly $1 and each
losing share $0. Until the bot notices, it keeps counting the position
against its exposure caps — and because state survives restarts, resolved
markets would otherwise stay "open" forever and eventually block every new
trade once the caps fill up.

Settling is bookkeeping: it realizes the P&L, frees the exposure, and logs a
SELL-at-payout row in the trade journal so the dashboard agrees. In live mode
the USDC still has to be redeemed on Polymarket (the "Redeem" button in the
UI); the bot doesn't do that on-chain step yet.
"""
from __future__ import annotations

import logging

from bot.journal import TradeJournal
from bot.risk import RiskManager
from bot.strategies.base import Signal

logger = logging.getLogger("polybot.settlement")


def resolved_payouts(raw_market) -> dict[str, float] | None:
    """Payout per share for each token of a resolved market, or None.

    None means "not resolved" or "can't tell": the market must be closed and
    have exactly one token marked as the winner. Anything else (still open,
    missing fields, a 50/50 or disputed outcome) leaves positions untouched.
    """
    if not isinstance(raw_market, dict) or not raw_market.get("closed"):
        return None
    payouts: dict[str, float] = {}
    for token in raw_market.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        token_id = token.get("token_id") or token.get("tokenId")
        if token_id:
            winner = token.get("winner") in (True, "true", "True")
            payouts[str(token_id)] = 1.0 if winner else 0.0
    if not payouts or sum(payouts.values()) != 1.0:
        return None
    return payouts


def settle_resolved_markets(
    client, risk: RiskManager, journal: TradeJournal, mode: str
) -> list[tuple[str, float]]:
    """Check every market the bot holds and settle the resolved ones.

    Returns (market_id, realized_pnl) for each market settled.
    """
    settled = []
    for market_id in sorted({p.market_id for p in risk.positions.values()}):
        try:
            payouts = resolved_payouts(client.get_market(market_id))
        except Exception as exc:
            logger.warning("Could not check whether market %s resolved (%s)", market_id, type(exc).__name__)
            continue
        if payouts is None:
            continue

        pnl = 0.0
        for pos in [p for p in list(risk.positions.values()) if p.market_id == market_id]:
            if pos.token_id not in payouts:
                continue
            payout = payouts[pos.token_id]
            size = pos.size
            pnl += risk.record_close(pos.token_id, size, size * payout)
            try:
                journal.record(
                    Signal(
                        strategy="settlement",
                        market_id=market_id,
                        token_id=pos.token_id,
                        outcome=pos.outcome,
                        side="SELL",
                        limit_price=payout,
                        size_shares=size,
                        size_usd=size * payout,
                        reason="market resolved; position settled at its payout",
                    ),
                    mode=mode,
                    filled=True,
                )
            except Exception:
                logger.exception("Failed to write the trade journal for settled token %s", pos.token_id)

        logger.info("Market %s resolved — settled positions, realized P&L $%.2f", market_id, pnl)
        settled.append((market_id, pnl))
    return settled
