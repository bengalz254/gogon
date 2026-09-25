"""Strategy 3 -- constellation / cross-coin laggard.

BTC, ETH, SOL, XRP (and sometimes DOGE/BNB/HYPE) move together inside a
5-minute window. Between T+60s and T+120s: if at least `min_consensus`
peers already moved the same way (> move_threshold) and this coin is still
flat (< flat_threshold) with its consensus-side share still near 50c, buy
the consensus side on the laggard.

This is intra-window correlation catch-up, not a macro call. The catch-up
view enters the fair-value model as a drift (catchup_factor * gap) and the
trade still has to clear the normal edge test.

Regime filter: skip when the reference coin (BTC) already moved >= 2% in the
last hour -- in extreme tape the catch-up often fails.
"""
from __future__ import annotations

from bot.updown.strategies.base import StrategyContext, Take, UpDownStrategy
from bot.updown.window import DOWN, UP, opposite


class ConstellationStrategy(UpDownStrategy):
    name = "constellation"

    def evaluate(self, ctx: StrategyContext) -> list:
        p = ctx.params
        m = ctx.model
        if not (p.eval_start_s <= m.elapsed <= p.eval_end_s):
            return []
        if ctx.entries >= p.max_entries_per_window:
            return []
        me = ctx.spec.asset
        my_delta = ctx.slot_deltas.get(me)
        if my_delta is None or abs(my_delta) >= p.flat_threshold:
            return []

        peers = {a: d for a, d in ctx.slot_deltas.items() if a != me and d is not None}
        ups = [d for d in peers.values() if d >= p.move_threshold]
        downs = [d for d in peers.values() if d <= -p.move_threshold]
        if len(ups) >= p.min_consensus and len(downs) <= p.max_opposing:
            direction, movers = UP, ups
        elif len(downs) >= p.min_consensus and len(ups) <= p.max_opposing:
            direction, movers = DOWN, downs
        else:
            return []

        ref = ctx.asset_return(p.reference_asset, 3600.0)
        if ref is None:
            if p.require_ref_1h:
                return []
        elif abs(ref) >= p.max_ref_1h_abs_return:
            return []

        if ctx.held(opposite(direction)) > 0:
            return []
        ask = ctx.book(direction).best_ask
        if ask is None or not (p.ask_min <= ask <= p.ask_max):
            return []

        consensus = sum(movers) / len(movers)
        gap = consensus - my_delta
        drift = p.catchup_factor * gap / max(m.remaining, 1.0)
        m2 = ctx.remodel(drift=drift)
        if m2 is None:
            return []
        pw = ctx.p_win(direction, m2)
        edge = ctx.taker_edge(direction, pw)
        if edge is None or edge < p.min_edge:
            return []
        limit = ctx.max_taker_price(pw, p.min_edge)
        if limit is None:
            return []
        limit = min(limit, p.ask_max)
        if limit < ask:
            return []
        return [
            Take(
                outcome=direction,
                limit_price=limit,
                p_win=pw,
                edge=edge,
                reason=(
                    f"laggard {me} {my_delta * 100:+.3f}% vs {len(movers)} peers "
                    f"{consensus * 100:+.3f}% ({direction}); ref1h={'n/a' if ref is None else f'{ref * 100:+.2f}%'} "
                    f"| {ctx.edge_breakdown(direction, pw)} | {ctx.model_brief(m2)}"
                ),
                meta={"consensus": consensus, "movers": len(movers)},
            )
        ]
