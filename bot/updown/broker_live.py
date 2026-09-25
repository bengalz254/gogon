"""Live order execution on the Polymarket CLOB via py-clob-client.

All calls here are blocking HTTP requests; the runner calls them from a
worker thread (asyncio.to_thread) and feeds the resulting OrderUpdate back
into the engine.

UNVERIFIED against the live API from the environment this was written in
(no network access there). Response shapes are parsed defensively; start
with the smallest sizes and reconcile against the Polymarket UI.
"""
from __future__ import annotations

import logging
import time

from bot.updown.fees import FeeModel
from bot.updown.orders import Fill, OrderRequest, OrderState, OrderUpdate, floor_shares

logger = logging.getLogger("polybot.updown.live")

_TICK_STR = {0.1: "0.1", 0.01: "0.01", 0.001: "0.001", 0.0001: "0.0001"}


def _tick_str(tick: float) -> str | None:
    for value, label in _TICK_STR.items():
        if abs(tick - value) < 1e-12:
            return label
    return None


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_post_response(req: OrderRequest, resp, fees: FeeModel, now: float) -> OrderUpdate:
    """Translate a POST /order response into an OrderUpdate."""
    if not isinstance(resp, dict):
        return OrderUpdate(req.client_id, "rejected", final=True, message=f"unexpected response: {resp!r}")
    error = resp.get("errorMsg") or resp.get("error")
    order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id")
    if resp.get("success") is False or (error and not order_id):
        return OrderUpdate(req.client_id, "rejected", final=True, message=str(error or resp))
    status = str(resp.get("status", "")).lower()
    fills = []
    if status == "matched":
        # For a BUY: makingAmount = USDC paid, takingAmount = shares received.
        shares = _num(resp.get("takingAmount"))
        usdc = _num(resp.get("makingAmount"))
        if shares and shares > req.shares * 50:  # base units (6 decimals)
            shares /= 1e6
            usdc = usdc / 1e6 if usdc else usdc
        if shares and shares > 0:
            price = (usdc / shares) if usdc else req.price
            price = min(price, req.price) if price > 0 else req.price
            fills.append(Fill(shares, price, shares * fees.per_share(price, req.is_maker), now, req.is_maker))
        final = req.tif in ("FAK", "FOK") or (shares or 0) >= req.shares - 1e-6
        return OrderUpdate(req.client_id, "filled" if final else "partial", fills, order_id, final=final)
    if status == "live":
        return OrderUpdate(req.client_id, "open", exchange_id=order_id)
    if status == "unmatched" or (req.tif in ("FAK", "FOK") and status not in ("delayed", "")):
        return OrderUpdate(req.client_id, "cancelled", exchange_id=order_id, final=True, message=status)
    # "delayed" or unknown: keep it open and let polling resolve it
    return OrderUpdate(req.client_id, "open", exchange_id=order_id, message=status)


def parse_order_status(st: OrderState, resp, fees: FeeModel, now: float) -> OrderUpdate | None:
    """Translate GET /data/order/<id> into an incremental OrderUpdate."""
    if not isinstance(resp, dict) or not resp:
        return None
    req = st.req
    matched = _num(resp.get("size_matched")) or 0.0
    new = matched - st.filled
    fills = []
    if new > 1e-6:
        price = req.price  # makers fill at their own price; for takers this is the worst case
        fills.append(Fill(new, price, new * fees.per_share(price, req.is_maker), now, req.is_maker))
    status = str(resp.get("status", "")).upper()
    if status.startswith("CANCEL") or status == "INVALID":
        return OrderUpdate(req.client_id, "cancelled", fills, final=True, message=status)
    if status == "MATCHED" or matched >= req.shares - 1e-6:
        return OrderUpdate(req.client_id, "filled", fills, final=True, message=status)
    if fills:
        return OrderUpdate(req.client_id, "partial", fills, message=status)
    return None


class LiveBroker:
    def __init__(self, client, fees: FeeModel):
        self.client = client
        self.fees = fees
        self._heartbeat_id = None
        self._hb_warned = 0.0

    def submit(self, req: OrderRequest) -> OrderUpdate:
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY

        now = time.time()
        args = OrderArgs(token_id=req.token_id, price=round(req.price, 4), size=floor_shares(req.shares), side=BUY)
        opts = PartialCreateOrderOptions(tick_size=_tick_str(req.tick_size), neg_risk=req.neg_risk or None)
        try:
            try:
                signed = self.client.create_order(args, opts)
            except Exception as e:
                if "tick size" not in str(e).lower():
                    raise
                signed = self.client.create_order(args)  # let the client resolve the tick size
            resp = self.client.post_order(signed, getattr(OrderType, req.tif), post_only=req.post_only)
        except Exception as e:
            logger.exception("Order submission failed (%s %s %s)", req.strategy, req.outcome, req.window_id)
            return OrderUpdate(req.client_id, "rejected", final=True, message=str(e))
        update = parse_post_response(req, resp, self.fees, now)
        logger.info("[LIVE] %s %s %s %.2f @ %.3f %s -> %s %s", req.tif, req.strategy, req.outcome,
                    req.shares, req.price, req.window_id, update.status, update.message)
        return update

    def cancel(self, client_id: str, exchange_id: str) -> OrderUpdate:
        try:
            resp = self.client.cancel(exchange_id)
        except Exception as e:
            logger.warning("Cancel failed for %s: %s", exchange_id, e)
            return OrderUpdate(client_id, "open", exchange_id=exchange_id, message=f"cancel failed: {e}")
        not_canceled = resp.get("not_canceled") if isinstance(resp, dict) else None
        if not_canceled and exchange_id in not_canceled:
            # Usually means it already filled or was already gone; polling settles it.
            return OrderUpdate(client_id, "open", exchange_id=exchange_id, message=f"not canceled: {not_canceled}")
        return OrderUpdate(client_id, "cancelled", exchange_id=exchange_id, final=True)

    def poll(self, st: OrderState) -> OrderUpdate | None:
        if not st.exchange_id:
            return None
        try:
            resp = self.client.get_order(st.exchange_id)
        except Exception as e:
            logger.debug("get_order failed for %s: %s", st.exchange_id, e)
            return None
        return parse_order_status(st, resp, self.fees, time.time())

    def cancel_many(self, exchange_ids: list) -> None:
        if not exchange_ids:
            return
        try:
            self.client.cancel_orders(exchange_ids)
        except Exception:
            logger.exception("Bulk cancel failed; cancel these manually in the UI: %s", exchange_ids)

    def heartbeat(self) -> None:
        """Dead-man switch: if the bot stops sending these, Polymarket cancels
        all of the account's resting orders after ~10s."""
        try:
            resp = self.client.post_heartbeat(self._heartbeat_id)
            if isinstance(resp, dict) and resp.get("heartbeat_id"):
                self._heartbeat_id = resp["heartbeat_id"]
        except Exception as e:
            if time.time() - self._hb_warned > 300:
                self._hb_warned = time.time()
                logger.warning("Heartbeat failed (%s); resting orders are still cancelled at window close", e)

    def prewarm(self, token_ids) -> None:
        """Cache tick size / neg-risk / fee rate so the first order isn't slow."""
        for tok in token_ids:
            for fn in ("get_tick_size", "get_neg_risk", "get_fee_rate_bps"):
                try:
                    getattr(self.client, fn)(tok)
                except Exception:
                    pass

    def server_time_offset(self) -> float | None:
        try:
            server = float(self.client.get_server_time())
        except Exception:
            return None
        return server - time.time()
