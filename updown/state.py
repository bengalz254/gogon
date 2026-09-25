"""A JSON snapshot of what the bot sees and is doing, for the radar dashboard.

The engine stays the single source of truth; this module only reads it.
The bot writes the snapshot to data/updown_state.json every tick and
scripts/updown_dashboard.py serves it to the browser.
"""
from __future__ import annotations

import json
import math
import os

from updown.strategy import DOWN, UP, Snapshot

MAX_TRACK_POINTS = 320
SECONDS_PER_YEAR = 365 * 86400


def _book_view(book) -> dict | None:
    if book is None:
        return None
    return {
        "bid": book.best_bid,
        "ask": book.best_ask,
        "bid_size": book.bids[0].size if book.bids else 0.0,
        "ask_size": book.asks[0].size if book.asks else 0.0,
    }


def _asset_view(engine, asset: str, now: float) -> dict:
    from updown.markets import window_start

    start = window_start(now, engine.s.window_seconds)
    st = engine.windows.get((asset, start))
    latest = engine.history.latest(asset)
    sigma = engine.vol[asset].sigma
    view = {
        "asset": asset,
        "window_start": start,
        "window_end": start + engine.s.window_seconds,
        "seconds_left": max(0.0, start + engine.s.window_seconds - now),
        "slug": st.market.slug if st and st.market else None,
        "market_found": bool(st and st.market),
        "strike": st.strike if st else None,
        "spot": latest[1] if latest else None,
        "feed_age": (now - latest[0]) if latest else None,
        "sigma": sigma,
        "vol_annual": sigma * math.sqrt(SECONDS_PER_YEAR),
        "skip_reason": st.skip_reason if st else "",
        "why": st.why if st else "",
        "p_up": None,
        "p_up_low": None,
        "p_up_high": None,
        "books": None,
        "edges": None,
        "position": None,
        "track": [],
        "fills": [],
    }
    if not st:
        return view

    mb = getattr(engine, "maker_broker", None)
    if mb is not None and st.market:
        view["quotes"] = {o: (mb.orders[st.market.tokens[o]].price if st.market.tokens[o] in mb.orders else None) for o in (UP, DOWN)}
    held = {o: st.pos.shares[o] for o in (UP, DOWN)}
    view["position"] = {"shares": held, "cost_usd": st.pos.total_cost_usd, "entries": st.pos.entries}
    view["fills"] = [{k: f[k] for k in ("ts", "kind", "outcome", "shares", "price", "usd", "fee", "fair")} for f in st.fills]
    track = st.track
    if len(track) > MAX_TRACK_POINTS:
        step = len(track) / MAX_TRACK_POINTS
        track = [track[int(i * step)] for i in range(MAX_TRACK_POINTS)] + [track[-1]]
    view["track"] = [[round(t, 2), p] for t, p in track]

    if st.strike and latest and view["seconds_left"] > 0:
        snap = Snapshot(spot=latest[1], strike=st.strike, seconds_left=view["seconds_left"], sigma=sigma, books=st.books or {})
        strat = engine.strategy
        view["p_up"] = strat.prob_up(snap)
        cons = strat.conservative_probs(snap)
        view["p_up_low"] = cons[UP]
        view["p_up_high"] = 1.0 - cons[DOWN]
        # 1-sigma price move over the time left: how far the line is, in "noise" units.
        view["sigma_move"] = latest[1] * sigma * math.sqrt(max(view["seconds_left"], 1.0))
        if st.books:
            view["books"] = {o: _book_view(st.books.get(o)) for o in (UP, DOWN)}
            edges = {}
            for o in (UP, DOWN):
                ask = st.books[o].best_ask if st.books.get(o) else None
                edges[o] = None if ask is None else cons[o] - ask - strat.fee(ask)
            view["edges"] = edges
    return view


def build_state(engine, now: float) -> dict:
    s = engine.s
    allowed, why_not = engine.risk.can_trade(now)
    open_exposure = engine._open_exposure()
    st = engine.stats
    return {
        "v": 1,
        "now": now,
        "mode": engine.mode,
        "strategy_mode": engine.s.mode,
        "started_at": engine.started_at,
        "window_seconds": s.window_seconds,
        "config": {
            "min_edge": s.strategy.min_edge,
            "max_seconds_left": s.strategy.max_seconds_left,
            "min_seconds_left": s.strategy.min_seconds_left,
            "min_price": s.strategy.min_price,
            "max_price": s.strategy.max_price,
            "vol_uncertainty": s.model.vol_uncertainty,
            "bankroll_start": s.sizing.bankroll_usd,
            "max_bet_usd": s.sizing.max_bet_usd,
            "quote_start_s": s.maker.quote_start_s,
            "stop_quoting_s": s.maker.stop_quoting_s,
        },
        "assets": [_asset_view(engine, a, now) for a in s.assets],
        "risk": {
            "can_trade": allowed,
            "reason": why_not,
            "today_pnl": engine.risk.realized_pnl_today,
            "daily_limit": abs(s.risk.max_daily_loss_usd),
            "consecutive_losses": engine.risk.consecutive_losses,
            "max_consecutive_losses": s.risk.max_consecutive_losses,
            "cooldown_left": max(0.0, engine.risk.cooldown_until - now),
            "open_exposure": open_exposure,
        },
        "stats": {
            "windows_traded": st.windows_traded,
            "wins": st.wins,
            "losses": st.losses,
            "pnl_usd": st.pnl_usd,
            "fees_usd": st.fees_usd,
            "orders": st.orders,
        },
        "asset_stats": {
            a: {"windows_traded": x.windows_traded, "wins": x.wins, "losses": x.losses, "pnl_usd": x.pnl_usd, "orders": x.orders}
            for a, x in engine.asset_stats.items()
        },
        "bankroll": engine.sizing.bankroll_usd,
        "recent_windows": list(engine.recent_windows),
        "events": list(engine.events)[-30:],
    }


def write_state(path: str, state: dict) -> None:
    """Atomic write, so the dashboard never reads half a file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    try:
        os.replace(tmp, path)
    except PermissionError:
        pass  # Windows: the dashboard has the file open this instant; next tick wins
