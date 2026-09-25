"""Strategy 5 -- cheap asymmetric (value, not momentum).

Buy the cheap side (ask 0.18-0.42) only when >= 90s remain, the model says
that side is not already dead, and the market is overpricing the expensive
side versus the model. Payoff is asymmetric (pay 25c, win 75c) so one win
covers several losses -- but without the overpricing filter it's just an
expensive coin flip. Small fixed size; never combined with martingale
(the risk layer enforces that globally).
"""
from __future__ import annotations

from bot.updown.strategies.base import StrategyContext, Take, UpDownStrategy
from bot.updown.window import OUTCOMES, opposite


class CheapAsymmetricStrategy(UpDownStrategy):
    name = "cheap_asymmetric"

    def evaluate(self, ctx: StrategyContext) -> list:
        p = ctx.params
        m = ctx.model
        if m.remaining < p.min_remaining_s or ctx.entries >= p.max_entries_per_window:
            return []
        for outcome in OUTCOMES:
            ask = ctx.book(outcome).best_ask
            if ask is None or not (p.ask_min <= ask <= p.ask_max):
                continue
            if ctx.held(opposite(outcome)) > 0:
                continue
            pw = ctx.p_win(outcome)  # conservative: the low end of the vol band
            if pw < p.min_model_prob:
                continue  # model says this side is already dead
            rich_mid = ctx.p_market(opposite(outcome))
            if rich_mid is None:
                continue
            overpricing = rich_mid - (1.0 - pw)
            if overpricing < p.min_overpricing:
                continue
            edge = ctx.taker_edge(outcome, pw)
            if edge is None or edge < p.min_edge:
                continue
            limit = ctx.max_taker_price(pw, p.min_edge)
            if limit is None:
                continue
            limit = min(limit, p.ask_max)
            if limit < ask:
                continue
            return [
                Take(
                    outcome=outcome,
                    limit_price=limit,
                    p_win=pw,
                    edge=edge,
                    reason=(
                        f"cheap {outcome} @ {ask:.3f}, rich side overpriced by {overpricing:+.3f} "
                        f"| {ctx.edge_breakdown(outcome, pw)} | {ctx.model_brief()}"
                    ),
                )
            ]
        return []
