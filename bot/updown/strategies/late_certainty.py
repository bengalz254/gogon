"""Strategy 2 -- late-certainty maker.

Don't chase 50/50. With 20-70s left, if the settlement TWAP is already
clearly on one side and not drifting back toward the line, rest a *maker*
bid on the winning side while it still trades at 0.92-0.985.

Wins are small and frequent; a loss (a late mean-reversion through the
line) is large -- hence tiny size and tight vol filters. This is the modern
version of the old "30-second scalper": it asks whether the 60s average is
locked in, not what the last print was.
"""
from __future__ import annotations

from bot.updown.mathutil import floor_to_tick
from bot.updown.model import per_second_to_annual
from bot.updown.strategies.base import Quote, StrategyContext, UpDownStrategy
from bot.updown.window import DOWN, UP, opposite


class LateCertaintyMaker(UpDownStrategy):
    name = "late_certainty"

    def evaluate(self, ctx: StrategyContext) -> list:
        p = ctx.params
        m = ctx.model
        if not (p.min_remaining_s <= m.remaining <= p.max_remaining_s):
            return []
        winner = UP if m.p_up >= 0.5 else DOWN
        if ctx.held(opposite(winner)) > 0:
            return []
        pw = ctx.p_win(winner)
        if pw < p.min_model_prob:
            return []

        ratio = m.vol.spike_ratio
        if ratio is not None and ratio > p.max_vol_ratio:
            return []
        if p.max_sigma_annual > 0 and per_second_to_annual(m.sigma) > p.max_sigma_annual:
            return []
        projected_z = self._projected_z(ctx, winner)
        if projected_z is None or projected_z < p.min_projected_z:
            return []

        book = ctx.book(winner)
        bid, ask = book.best_bid, book.best_ask
        if bid is None or not (p.bid_min <= bid <= p.bid_max):
            return []
        tick = ctx.tick
        price = bid + tick if p.improve_by_tick else bid
        if ask is not None and price > ask - tick + 1e-9:
            price = ask - tick  # stay a maker: never cross the spread
        limit = min(p.max_price, pw - p.min_edge)
        price = floor_to_tick(min(price, limit), tick)
        if price < p.bid_min:
            return []
        return [
            Quote(
                key="late_bid",
                outcome=winner,
                price=price,
                limit=limit,
                p_win=pw,
                reason=(
                    f"late-certainty {winner} bid {price:.3f} (book {bid:.3f}/{ask if ask else float('nan'):.3f}) "
                    f"p_win={pw:.3f} proj_z={projected_z:.2f} | {ctx.model_brief()}"
                ),
            )
        ]

    @staticmethod
    def _projected_z(ctx: StrategyContext, winner: str) -> float | None:
        """Distance (in settlement sd) that would remain if the recent drift
        continued to the close. Positive = still on the winning side."""
        p = ctx.params
        m = ctx.model
        sign = 1.0 if winner == UP else -1.0
        change = ctx.spot_change(p.approach_lookback_s)
        if change is None:
            return None  # no recent history: don't trust the setup
        velocity = change / p.approach_lookback_s  # price units per second
        total = m.realized_n + m.future_n
        mean_t = max(0.0, m.remaining - (m.future_n - 1) / 2.0)
        shift = velocity * mean_t * (m.future_n / total if total else 1.0)
        if velocity * sign > 0:
            shift = 0.0  # moving away from the line: no penalty, no bonus
        projected = m.settle_mean + shift
        if m.settle_sd <= 0:
            return float("inf") if (projected - m.ptb) * sign >= 0 else float("-inf")
        return sign * (projected - m.ptb) / m.settle_sd
