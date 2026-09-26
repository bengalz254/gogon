"""Turns strategy Signals into either simulated fills (paper mode) or real
orders on the Polymarket CLOB (live mode), and updates risk/position state.
"""
from __future__ import annotations

import logging

from bot.journal import TradeJournal
from bot.risk import RiskManager
from bot.strategies.base import Signal

logger = logging.getLogger("polybot.execution")


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
    ):
        self.client = client
        self.risk = risk
        self.journal = journal
        self.live = live
        self.store = store  # bot.state.StateStore, saved after every fill
        self.notifier = notifier  # bot.notify.Notifier
        self.notify_fills = notify_fills

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
            allowed, reason = self.risk.can_open(signal.market_id, signal.size_usd + signal.fee_usd)
            if not allowed:
                logger.info("Skipping BUY %s/%s: %s", signal.market_id, signal.outcome, reason)
                return False

        filled = self._fill(signal)
        if filled:
            self._notify_fill([signal])
        return filled

    def execute_group(self, legs: list[Signal]) -> bool:
        """Execute a multi-leg trade (e.g. both arbitrage legs) as one unit.

        The whole group is risk-checked once, up front: checking legs one by
        one let the first leg's fill push the second over a cap (float noise
        in an order sized exactly to the budget was enough), leaving an
        unhedged position. Legs are then sent in order, and sending stops at
        the first leg that doesn't fill.
        """
        market_ids = {leg.market_id for leg in legs}
        if len(market_ids) != 1:
            logger.error("Refusing group %s spanning several markets: %s", legs[0].group_id, market_ids)
            return False
        market_id = market_ids.pop()

        # Every leg must be a valid order on its own, or it would be rejected
        # after the legs before it had already filled.
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

        filled: list[Signal] = []
        for leg in legs:
            if not self._fill(leg):
                break
            filled.append(leg)

        if len(filled) == len(legs):
            self._notify_fill(legs)
            return True

        if filled:
            self._alert_legged(filled, legs[len(filled)])
        return False

    # -- shared fill path ---------------------------------------------------
    def _fill(self, signal: Signal) -> bool:
        filled = self._execute_live(signal) if self.live else self._execute_paper(signal)
        if filled:
            # Track the position before anything that can fail (like the
            # journal write): the order has already happened on the exchange.
            self._apply_fill(signal)
        try:
            self.journal.record(signal, mode=self.mode, filled=filled)
        except Exception:
            logger.exception("Failed to write the trade journal for %s/%s", signal.market_id, signal.outcome)
        return filled

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
            )
        else:
            self.risk.record_close(signal.token_id, signal.size_shares, signal.size_usd - signal.fee_usd)

        if self.store is not None:
            try:
                self.store.save(self.risk.snapshot())
            except Exception:
                logger.exception("Failed to persist risk state after fill")

    # -- paper mode: assume the observed best bid/ask fills immediately -----
    def _execute_paper(self, signal: Signal) -> bool:
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

    # -- live mode: sign and submit a real fill-or-kill order --------------
    def _execute_live(self, signal: Signal) -> bool:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        side = BUY if signal.side == "BUY" else SELL
        order_args = OrderArgs(
            token_id=signal.token_id,
            price=round(signal.limit_price, 4),
            size=round(signal.size_shares, 2),
            side=side,
        )

        try:
            signed_order = self.client.create_order(order_args)
            # FOK: fills completely and immediately, or not at all — no resting
            # order is left on the book, which minimizes one-leg-only arb risk.
            response = self.client.post_order(signed_order, OrderType.FOK)
        except Exception:
            logger.exception("Order submission failed for %s/%s", signal.market_id, signal.outcome)
            return False

        # NOTE: verify this against the actual API response shape before
        # relying on it — reconcile positions periodically via
        # client.get_trades()/get_orders() rather than trusting this alone.
        success = bool(response) and not (isinstance(response, dict) and response.get("error"))

        logger.info(
            "[LIVE] %s %.4f %s shares @ %.4f (market=%s) -> response=%s",
            signal.side,
            signal.size_shares,
            signal.outcome,
            signal.limit_price,
            signal.market_id,
            response,
        )

        if not success:
            logger.warning("Order for %s/%s did not confirm success: %s", signal.market_id, signal.outcome, response)

        return success

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
        """One leg of a hedged trade filled and a later one didn't: the bot now
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
