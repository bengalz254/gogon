"""Turns strategy Signals into either simulated fills (paper mode) or real
orders on the Polymarket CLOB (live mode), and updates risk/position state.
"""
from __future__ import annotations

import logging
from typing import Callable

from bot.journal import TradeJournal
from bot.orderbook import OrderBook
from bot.paper import fok_fillable
from bot.risk import RiskManager
from bot.strategies.base import Signal

logger = logging.getLogger("polybot.execution")


def order_filled(response) -> bool:
    """Whether a CLOB V2 order response confirms a fill-or-kill order matched.

    Filled: success is true and the status is "matched" (or trades were
    created). Anything else — an error message, "live", "delayed" (a taker
    delay hides the outcome), "unmatched" or an unknown shape — counts as not
    filled; the hourly reconciliation catches the rare case where that's wrong.
    """
    if not isinstance(response, dict) or response.get("success") is not True:
        return False
    if response.get("errorMsg"):
        return False
    status = str(response.get("status", "")).lower()
    return status == "matched" or bool(response.get("tradeIDs") or response.get("transactionsHashes"))


class OrderExecutor:
    def __init__(
        self,
        client,
        risk: RiskManager,
        journal: TradeJournal,
        live: bool,
        store=None,
        notifier=None,
        notify_fills: bool = True,
        book_lookup: Callable[[str], OrderBook | None] | None = None,
    ):
        self.client = client
        self.risk = risk
        self.journal = journal
        self.live = live
        self.store = store  # bot.state.StateStore, saved after every fill
        self.notifier = notifier  # bot.notify.Notifier
        self.notify_fills = notify_fills
        # Paper mode checks fills against the latest book (with depth) when given.
        self.book_lookup = book_lookup

    @property
    def mode(self) -> str:
        return "live" if self.live else "paper"

    def execute_signals(self, signals: list[Signal]) -> None:
        """Execute signals in order, keeping multi-leg groups (same group_id) together."""
        units: dict[str, list[Signal]] = {}
        for i, sig in enumerate(signals):
            units.setdefault(sig.group_id or f"single-{i}", []).append(sig)
        for legs in units.values():
            if len(legs) == 1:
                self.execute(legs[0])
            else:
                self.execute_group(legs)

    def execute(self, signal: Signal) -> bool:
        if signal.side == "BUY":
            allowed, reason = self.risk.can_open(signal.risk_key, signal.size_usd + signal.fee_usd)
            if not allowed:
                logger.info("Skipping BUY %s/%s: %s", signal.market_id, signal.outcome, reason)
                return False

        filled = self._send([signal])[0]
        self._record(signal, filled)
        if filled:
            self._notify_fill([signal])
        return filled

    def execute_group(self, legs: list[Signal]) -> bool:
        """Execute a multi-leg trade (e.g. both arbitrage legs) as one unit.

        The whole group is risk-checked once, up front: checking legs one by
        one let the first leg's fill push the second over a cap (float noise
        in an order sized exactly to the budget was enough), leaving an
        unhedged position. Live, all legs go to the exchange in one request so
        they reach the matching engine together; in paper mode they're
        simulated in order and simulation stops at the first leg that
        wouldn't fill.
        """
        # One risk bucket per group: a market, or a multi-market set (set_id).
        risk_keys = {leg.risk_key for leg in legs}
        if len(risk_keys) != 1:
            logger.error("Refusing group %s spanning several markets: %s", legs[0].group_id, risk_keys)
            return False
        market_id = risk_keys.pop()

        # Every leg must be a valid order on its own, or it would be rejected
        # while the others fill.
        min_order = self.risk.cfg.min_order_size_usd
        too_small = [leg for leg in legs if leg.size_usd < min_order - 1e-9]
        if too_small:
            logger.info(
                "Skipping %d-leg group in %s: %s leg $%.2f is below the $%.2f minimum order",
                len(legs),
                market_id,
                too_small[0].outcome,
                too_small[0].size_usd,
                min_order,
            )
            return False

        buy_usd = sum(leg.size_usd + leg.fee_usd for leg in legs if leg.side == "BUY")
        if buy_usd > 0:
            allowed, reason = self.risk.can_open(market_id, buy_usd)
            if not allowed:
                logger.info("Skipping %d-leg group in %s: %s", len(legs), market_id, reason)
                return False

        results = self._send(legs)
        filled = []
        for leg, ok in zip(legs, results):
            self._record(leg, ok)
            if ok:
                filled.append(leg)

        if len(filled) == len(legs):
            self._notify_fill(legs)
            return True
        if filled:
            unfilled = next(leg for leg, ok in zip(legs, results) if not ok)
            self._alert_legged(filled, unfilled)
        return False

    # -- sending -------------------------------------------------------------
    def _send(self, signals: list[Signal]) -> list[bool]:
        """Fill result per signal. Paper mode stops at the first leg that
        wouldn't fill (later legs get no result and aren't recorded)."""
        if self.live:
            return self._execute_live(signals)
        results = []
        for signal in signals:
            ok = self._execute_paper(signal)
            results.append(ok)
            if not ok:
                break
        return results

    def _record(self, signal: Signal, filled: bool) -> None:
        if filled:
            # Track the position before anything that can fail (like the
            # journal write): the order has already happened on the exchange.
            self._apply_fill(signal)
        try:
            self.journal.record(signal, mode=self.mode, filled=filled)
        except Exception:
            logger.exception("Failed to write the trade journal for %s/%s", signal.market_id, signal.outcome)

    def _apply_fill(self, signal: Signal) -> None:
        """Update positions for a fill and persist them. Fees are part of the
        cost basis on buys and come out of the proceeds on sells."""
        if signal.side == "BUY":
            self.risk.record_open(
                signal.market_id,
                signal.token_id,
                signal.outcome,
                signal.size_shares,
                signal.size_usd + signal.fee_usd,
                outcome_count=signal.outcome_count,
                set_id=signal.set_id,
            )
        else:
            self.risk.record_close(signal.token_id, signal.size_shares, signal.size_usd - signal.fee_usd)

        if self.store is not None:
            try:
                self.store.save(self.risk.snapshot())
            except Exception:
                logger.exception("Failed to persist risk state after fill")

    # -- paper mode: fill if the latest book has the size at the limit price --
    def _execute_paper(self, signal: Signal) -> bool:
        book = self.book_lookup(signal.token_id) if self.book_lookup is not None else None
        if book is not None and not fok_fillable(book, signal.side, signal.limit_price, signal.size_shares):
            logger.info(
                "[PAPER] %s %.2f %s @ %.4f would not fill: not enough size in the book (market=%s)",
                signal.side,
                signal.size_shares,
                signal.outcome,
                signal.limit_price,
                signal.market_id,
            )
            return False
        logger.info(
            "[PAPER] %s %.4f %s shares @ %.4f + fee $%.4f (market=%s) — %s",
            signal.side,
            signal.size_shares,
            signal.outcome,
            signal.limit_price,
            signal.fee_usd,
            signal.market_id,
            signal.reason,
        )
        return True

    # -- live mode: sign and submit real fill-or-kill orders ----------------
    def _execute_live(self, signals: list[Signal]) -> list[bool]:
        """Sign and submit fill-or-kill orders (CLOB V2). FOK fills completely
        and immediately or not at all, so no resting order is left behind.
        Several orders go in one request, so the legs of a hedged trade reach
        the matching engine together."""
        from py_clob_client_v2 import OrderArgs, OrderType, PostOrdersV2Args

        try:
            signed = [
                self.client.create_order(
                    OrderArgs(
                        token_id=s.token_id,
                        price=round(s.limit_price, 4),
                        size=round(s.size_shares, 2),
                        side=s.side,
                    )
                )
                for s in signals
            ]
            if len(signed) == 1:
                responses = [self.client.post_order(signed[0], OrderType.FOK)]
            else:
                responses = self.client.post_orders(
                    [PostOrdersV2Args(order=order, orderType=OrderType.FOK) for order in signed]
                )
        except Exception:
            logger.exception(
                "Order submission failed for %s (%s)",
                signals[0].market_id,
                ", ".join(s.outcome for s in signals),
            )
            return [False] * len(signals)

        if not isinstance(responses, list):
            responses = [responses]
        results = []
        for i, signal in enumerate(signals):
            response = responses[i] if i < len(responses) else None
            filled = order_filled(response)
            logger.info(
                "[LIVE] %s %.4f %s shares @ %.4f (market=%s) -> filled=%s response=%s",
                signal.side,
                signal.size_shares,
                signal.outcome,
                signal.limit_price,
                signal.market_id,
                filled,
                response,
            )
            results.append(filled)
        return results

    # -- notifications -------------------------------------------------------
    def _notify_fill(self, legs: list[Signal]) -> None:
        if self.notifier is None or not self.notify_fills:
            return
        lines = [
            f"{leg.side} {leg.size_shares:.2f} {leg.outcome} @ {leg.limit_price:.3f} (fee ${leg.fee_usd:.2f})"
            for leg in legs
        ]
        cash_flow = sum(
            -(leg.size_usd + leg.fee_usd) if leg.side == "BUY" else (leg.size_usd - leg.fee_usd)
            for leg in legs
        )
        self.notifier.send(
            f"✅ [{self.mode.upper()}] {legs[0].strategy} terisi — market {legs[0].market_id[:12]}\n"
            + "\n".join(lines)
            + f"\nArus kas: {'-' if cash_flow < 0 else '+'}${abs(cash_flow):.2f}"
        )

    def _alert_legged(self, filled: list[Signal], failed: Signal) -> None:
        """One leg of a hedged trade filled and another didn't: the bot now
        holds a directional position it never intended to."""
        filled_desc = ", ".join(f"{s.side} {s.size_shares:.2f} {s.outcome}" for s in filled)
        logger.critical(
            "LEGGED TRADE in market %s (group %s): filled [%s] but %s %s did not fill — "
            "position is UNHEDGED, check it manually",
            failed.market_id,
            failed.group_id,
            filled_desc,
            failed.side,
            failed.outcome,
        )
        if self.notifier is not None:
            self.notifier.send(
                f"🚨 [{self.mode.upper()}] Arbitrase hanya terisi sebagian di market {failed.market_id}\n"
                f"Terisi: {filled_desc}\nGagal: {failed.side} {failed.size_shares:.2f} {failed.outcome}\n"
                "Posisi TIDAK terlindung — cek manual di Polymarket."
            )
