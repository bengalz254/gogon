"""Order execution: simulated fills (paper) or fill-and-kill orders on the CLOB (live)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from updown.strategy import Decision

logger = logging.getLogger("polybot.updown.broker")


@dataclass
class Fill:
    shares: float
    usd: float  # price * shares, excluding fees
    fee_usd: float


class PaperBroker:
    """Assumes the levels the strategy saw are still there when the order lands.

    That's optimistic: in a real race, other bots take the cheap asks first.
    Treat paper P&L as an upper bound, and read the fill rate in live mode
    before trusting it.
    """

    live = False

    def execute(self, token_id: str, decision: Decision) -> Fill:
        shares = int(decision.shares * 100) / 100  # the CLOB trades in 0.01-share steps
        if shares <= 0:
            return Fill(0.0, 0.0, 0.0)
        scale = shares / decision.shares
        return Fill(shares, decision.usd * scale, decision.fee_usd * scale)


class LiveBroker:
    """Fill-and-kill (FAK) limit orders: take what's available up to the limit
    price right now, cancel the rest. Nothing is left resting on the book."""

    live = True

    def __init__(self, client):
        self.client = client

    def execute(self, token_id: str, decision: Decision) -> Fill:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        size = int(decision.shares * 100) / 100  # round down to 0.01 share
        if size <= 0:
            return Fill(0.0, 0.0, 0.0)
        args = OrderArgs(
            token_id=token_id,
            price=round(decision.limit_price, 4),
            size=size,
            side=BUY if decision.side == "BUY" else SELL,
        )
        try:
            signed = self.client.create_order(args)  # fee rate is looked up by the client
            resp = self.client.post_order(signed, OrderType.FAK)
        except Exception:
            logger.exception("Order failed: %s %s %.2f @ %.4f", decision.side, decision.outcome, size, decision.limit_price)
            return Fill(0.0, 0.0, 0.0)

        logger.info("[LIVE] %s %s %.2f @ <=%.4f -> %s", decision.side, decision.outcome, size, decision.limit_price, resp)
        return self._parse_fill(resp, decision, size)

    @staticmethod
    def _parse_fill(resp, decision: Decision, size: float) -> Fill:
        if not isinstance(resp, dict) or resp.get("error") or resp.get("errorMsg") or resp.get("success") is False:
            return Fill(0.0, 0.0, 0.0)
        if str(resp.get("status", "")).lower() not in ("matched", "", "live", "delayed"):
            return Fill(0.0, 0.0, 0.0)
        per_share_fee = decision.fee_usd / decision.shares if decision.shares else 0.0
        try:
            making = float(resp.get("makingAmount") or 0)
            taking = float(resp.get("takingAmount") or 0)
        except (TypeError, ValueError):
            making = taking = 0.0
        # BUY: we give USDC (making) and get shares (taking); SELL is the reverse.
        shares, usd = (taking, making) if decision.side == "BUY" else (making, taking)
        if shares > 0:
            return Fill(shares, usd, per_share_fee * shares)
        # Unknown response shape: pick the mistake that overstates exposure
        # rather than hiding it (a BUY counts as filled, a SELL as not).
        # Reconcile against the Polymarket UI / client.get_trades().
        if decision.side == "BUY":
            logger.warning("Could not read fill amounts from %s; assuming the BUY filled fully", resp)
            return Fill(size, size * decision.usd / decision.shares, per_share_fee * size)
        logger.warning("Could not read fill amounts from %s; assuming the SELL did not fill", resp)
        return Fill(0.0, 0.0, 0.0)
