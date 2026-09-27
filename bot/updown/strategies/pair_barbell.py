"""Strategy 4 -- pair / inventory barbell (not a prediction).

At the open, with both sides near 50c, rest small maker bids on Up AND Down
(combined cost <= pair_max_cost, e.g. 0.99). Later, once the model /
TWAP confirms a side, add more shares on that side, then hold to expiry --
no emotional take-profit.

The edge is not the spread (that usually loses to fees and adverse
selection); it is ending the window with extra shares on the winning side.
Needs capital, fast execution and discipline -- with poor latency this is a
machine for losing money, which is why it ships disabled.
"""
from __future__ import annotations

from bot.updown.mathutil import floor_to_tick
from bot.updown.strategies.base import Quote, StrategyContext, Take, UpDownStrategy
from bot.updown.window import DOWN, OUTCOMES, UP, opposite


class PairBarbellStrategy(UpDownStrategy):
    name = "pair_barbell"

    def evaluate(self, ctx: StrategyContext) -> list:
        p = ctx.params
        m = ctx.model
        if m.elapsed < p.open_phase_s:
            return self._base_quotes(ctx)
        notes = ctx.notes
        if "base_filled" not in notes:
            notes["base_filled"] = {o: ctx.held(o) for o in OUTCOMES}
        if m.elapsed >= p.tilt_start_s and m.remaining >= p.tilt_min_remaining_s:
            return self._tilt(ctx, notes["base_filled"])
        return []

    def _base_quotes(self, ctx: StrategyContext) -> list:
        p = ctx.params
        tick = ctx.tick
        prices = {}
        for o in OUTCOMES:
            book = ctx.book(o)
            bid, ask = book.best_bid, book.best_ask
            if ask is None or not (p.side_ask_min <= ask <= p.side_ask_max):
                return []
            price = bid + tick if bid is not None else ask - tick
            prices[o] = min(price, ask - tick)
        excess = prices[UP] + prices[DOWN] - p.pair_max_cost
        if excess > 0:
            for o in OUTCOMES:
                prices[o] -= excess / 2.0
        quotes = []
        for o in OUTCOMES:
            price = floor_to_tick(prices[o], tick)
            want = p.base_shares - ctx.held(o)
            if price <= 0 or want <= 0:
                continue
            quotes.append(
                Quote(
                    key=f"base_{o.lower()}",
                    outcome=o,
                    price=price,
                    limit=p.pair_max_cost - prices[opposite(o)],
                    shares=want,
                    fixed_size=True,  # the pair's edge is cost < $1, not direction
                    p_win=ctx.p_win(o),
                    reason=f"barbell base {o} @ {price:.3f} (pair <= {p.pair_max_cost}) | {ctx.model_brief()}",
                )
            )
        return quotes

    def _tilt(self, ctx: StrategyContext, base_filled: dict) -> list:
        p = ctx.params
        if base_filled.get(UP, 0) <= 0 and base_filled.get(DOWN, 0) <= 0:
            return []  # no inventory to tilt: a pure directional bet is fair_value's job
        for o in OUTCOMES:
            pw = ctx.p_win(o)
            if pw < p.confirm_prob:
                continue
            target = base_filled.get(o, 0.0) + p.tilt_ratio * p.base_shares
            want = target - ctx.held(o)
            if want <= 0:
                return []
            book = ctx.book(o)
            edge = ctx.taker_edge(o, pw)
            if edge is not None and edge >= p.tilt_take_min_edge:
                limit = ctx.max_taker_price(pw, p.tilt_take_min_edge)
                if limit is not None and book.best_ask is not None and limit >= book.best_ask:
                    return [
                        Take(
                            outcome=o, limit_price=limit, p_win=pw, edge=edge, max_shares=want,
                            reason=f"barbell tilt take {o} | {ctx.edge_breakdown(o, pw)} | {ctx.model_brief()}",
                        )
                    ]
            bid, ask = book.best_bid, book.best_ask
            if ask is None:
                return []
            tick = ctx.tick
            price = bid + tick if bid is not None else ask - tick
            price = floor_to_tick(min(price, ask - tick, pw - p.tilt_min_edge), tick)
            if price <= 0:
                return []
            other = opposite(o)
            return [
                Quote(
                    key="tilt", outcome=o, price=price, shares=want, p_win=pw, limit=pw - p.tilt_min_edge,
                    reason=(
                        f"barbell tilt bid {o} @ {price:.3f} (base {base_filled.get(o, 0):.1f}/"
                        f"{base_filled.get(other, 0):.1f}) p_win={pw:.3f} | {ctx.model_brief()}"
                    ),
                )
            ]
        return []
