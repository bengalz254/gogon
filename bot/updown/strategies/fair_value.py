"""Strategy 1 -- fair value vs Polymarket price (the core "intelligence").

No direction guessing: the model prices P(Up) from the same oracle stream
the market settles on (Chainlink, TWAP-aware), time remaining, realized vol
and the distance to price_to_beat. Buy a side only when

    edge = p_model - p_market - fee - half_spread - slippage >= min_edge

e.g. TWAP +0.12% above the open with 90s left in a calm tape: the model says
p_up ~ 0.72; if Up is still offered at 0.58 that's a trade, at 0.74 it isn't.
"""
from __future__ import annotations

from bot.updown.strategies.base import StrategyContext, Take, UpDownStrategy
from bot.updown.window import OUTCOMES, opposite


class FairValueStrategy(UpDownStrategy):
    name = "fair_value"

    def evaluate(self, ctx: StrategyContext) -> list:
        p = ctx.params
        m = ctx.model
        if not (p.min_remaining_s <= m.remaining <= p.max_remaining_s):
            return []
        if ctx.entries >= p.max_entries_per_window:
            return []
        if ctx.last_entry_ts is not None and ctx.now - ctx.last_entry_ts < p.min_entry_spacing_s:
            return []

        best: Take | None = None
        for outcome in OUTCOMES:
            if ctx.held(opposite(outcome)) > 0:
                continue  # never flip sides inside one window
            ask = ctx.book(outcome).best_ask
            if ask is None or not (p.min_price <= ask <= p.max_price):
                continue
            pw = ctx.p_win(outcome)
            edge = ctx.taker_edge(outcome, pw)
            if edge is None or edge < p.min_edge:
                continue
            limit = ctx.max_taker_price(pw, p.min_edge)
            if limit is None or limit < ask:
                continue
            take = Take(
                outcome=outcome,
                limit_price=min(limit, p.max_price),
                p_win=pw,
                edge=edge,
                tif=p.tif,
                reason=f"{outcome}: {ctx.edge_breakdown(outcome, pw)} | {ctx.model_brief()}",
            )
            if best is None or take.edge > best.edge:
                best = take
        return [best] if best else []
