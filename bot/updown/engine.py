"""The Up/Down engine core -- synchronous, deterministic, no IO.

Inputs:  engine.handle(event)            market data / discovery / resolutions
         engine.on_order_update(update)  fills and order status from a broker
         engine.step(now) -> [actions]   called every ~250ms
Outputs: PlaceOrder / CancelOrder actions for a broker to execute.

Because it never touches the network or the wall clock, the exact same
engine runs live (runner.py), on recorded data (scripts/updown_backtest.py)
and in the synthetic simulator (sim.py).
"""
from __future__ import annotations

import logging
import math
import re
from collections import deque
from dataclasses import dataclass

from bot.updown.book import OrderBook
from bot.updown.config import UpDownConfig, resolve_params, session_of
from bot.updown.fees import FeeModel
from bot.updown.journal import NullJournal
from bot.updown.mathutil import floor_to_tick
from bot.updown.model import (
    ModelSnapshot,
    Nowcaster,
    estimate_vol,
    evaluate_window,
    settlement_value,
)
from bot.updown.orders import CancelOrder, OrderRequest, OrderTracker, OrderUpdate, PlaceOrder, new_client_id
from bot.updown.risk import UpDownRisk
from bot.updown.series import PriceSeries
from bot.updown.strategies.base import Quote, QuoteView, StrategyContext, Take
from bot.updown.window import DOWN, OUTCOMES, UP, Phase, WindowSpec, WindowState

logger = logging.getLogger("polybot.updown.engine")

@dataclass
class Exposure:
    window: float = 0.0  # USD in this window (positions + resting/in-flight orders)
    strategy_window: float = 0.0  # ... for this strategy only
    total: float = 0.0  # across all unsettled windows
    slot_direction: float = 0.0  # same outcome, same time slot, all assets
    open_windows: int = 0
    window_open: bool = False


class Engine:
    def __init__(self, cfg: UpDownConfig, strategies: list, risk: UpDownRisk, journal=None, mode: str = "paper"):
        self.cfg = cfg
        self.samples = cfg.settle_samples
        self.fees = FeeModel.from_config(cfg.fees)
        self.strategies = strategies
        self.risk = risk
        self.journal = journal or NullJournal()
        self.mode = mode
        self.now = 0.0

        self.windows: dict[str, WindowState] = {}
        self.token_index: dict[str, tuple[str, str]] = {}  # token -> (window_id, outcome)
        self.complement: dict[str, str] = {}
        self.books: dict[str, OrderBook] = {}
        self.oracle: dict[str, PriceSeries] = {}
        self.cex: dict[str, PriceSeries] = {}
        self.nowcasters: dict[str, Nowcaster] = {}
        self.feed_down: dict[str, str] = {}
        self.orders = OrderTracker()
        self._slot_open: dict[tuple[float, str], float] = {}
        self._quote_changed: dict[tuple[str, str, str], float] = {}
        self._sent: deque = deque()
        self._warned: set = set()
        self._decision_log_ts: dict = {}
        self._vol_cache: dict = {}
        # (store, asset) -> local time the latest new price arrived. Freshness
        # is judged on arrival, not on the source timestamp, so a skewed local
        # clock can't make live data look stale (or stale data look live).
        self._arrival: dict = {}
        self.stats = {"takes": 0, "quotes": 0, "cancels": 0, "fills": 0, "rejected": 0}
        # Compact per-window outcome record that survives window GC (summaries, tests).
        self.results: dict[str, dict] = {}

    # ======================================================================
    # Inputs
    # ======================================================================
    def handle(self, ev, recv_ts: float | None = None) -> None:
        """Apply one input event. recv_ts: local time it arrived (default: last step time)."""
        k = ev.kind
        arrived = self.now if recv_ts is None else recv_ts
        if k == "oracle":
            series = self._series(self.oracle, ev.asset)
            if series.update(ev.ts, ev.price):
                self._arrival[("oracle", ev.asset)] = arrived
                cex = self.cex.get(ev.asset)
                cex_px = cex.price_at(ev.ts) if cex is not None and cex.age(ev.ts) <= self.cfg.feeds.cex_max_age_s else None
                self._nowcaster(ev.asset).observe(ev.ts, ev.price, cex_px)
        elif k == "cex":
            if self._series(self.cex, ev.asset).update(ev.ts, ev.price):
                self._arrival[("cex", ev.asset)] = arrived
        elif k == "book":
            book = self.books.get(ev.token_id)
            if book is not None:
                book.apply_snapshot(ev.bids, ev.asks, ev.ts)
        elif k == "level":
            book = self.books.get(ev.token_id)
            if book is not None:
                book.apply_level(ev.side, ev.price, ev.size, ev.ts)
        elif k == "tick":
            book = self.books.get(ev.token_id)
            if book is not None and ev.tick_size > 0:
                book.tick_size = ev.tick_size
        elif k == "window":
            self.add_window(ev.spec)
        elif k == "official_ptb":
            self._on_official_ptb(ev.window_id, ev.price)
        elif k == "resolution":
            self._on_official_resolution(ev.window_id, ev.winner)
        elif k == "feed":
            if ev.connected:
                self.feed_down.pop(ev.feed, None)
            else:
                self.feed_down[ev.feed] = ev.detail or "disconnected"
                if ev.feed == "clob":
                    for book in self.books.values():
                        book.has_snapshot = False  # force a fresh snapshot after reconnect
        # "trade" prints only matter to the paper exchange

    def add_window(self, spec: WindowSpec) -> bool:
        if spec.window_id in self.windows:
            return False
        if self.now and spec.end < self.now - 60:
            return False
        w = WindowState(spec=spec)
        self.windows[spec.window_id] = w
        for outcome in OUTCOMES:
            tok = spec.token(outcome)
            self.token_index[tok] = (spec.window_id, outcome)
            self.books.setdefault(tok, OrderBook(tok, tick_size=spec.tick_size))
        self.complement[spec.up_token] = spec.down_token
        self.complement[spec.down_token] = spec.up_token
        logger.info("Window listed: %s (%s) %s", spec.window_id, spec.asset, spec.question)
        return True

    def on_order_update(self, update: OrderUpdate) -> None:
        st = self.orders.get(update.client_id)
        if st is None:
            return
        new_fills = self.orders.apply(update, self.now)
        req = st.req
        w = self.windows.get(req.window_id)
        if update.status == "rejected":
            self.stats["rejected"] += 1
            logger.warning("Order %s rejected: %s", req.client_id, update.message)
        for f in new_fills:
            self.stats["fills"] += 1
            if w is None:
                logger.error("Fill for unknown/finished window %s: %s", req.window_id, f)
                continue
            h = w.holding(req.strategy, req.outcome)
            h.shares += f.shares
            h.cost += f.shares * f.price + f.fee
            self.journal.fill(
                ts=f.ts, strategy=req.strategy, window_id=req.window_id, token_id=req.token_id,
                outcome=req.outcome, price=f.price, shares=f.shares, fee=f.fee, reason=req.reason,
            )
            logger.info(
                "[%s] FILL %s %s %.2f @ %.3f (%s, fee $%.4f) %s",
                self.mode.upper(), req.strategy, req.outcome, f.shares, f.price,
                "maker" if f.is_maker else "taker", f.fee, req.window_id,
            )
        if st.final and st.filled <= 1e-9 and req.quote_key is None and w is not None:
            # A take that filled nothing doesn't count against max entries.
            w.entries[req.strategy] = max(0, w.entries.get(req.strategy, 0) - 1)

    # ======================================================================
    # Main step
    # ======================================================================
    def step(self, now: float) -> list:
        self.now = now
        self.risk.roll_day(now)
        actions: list = []
        for w in sorted(self.windows.values(), key=lambda w: (w.spec.start, w.asset)):
            self._advance(w, now, actions)
            if w.phase == Phase.LIVE:
                self._trade_window(w, now, actions)
        self._book_settlements(now)
        self.risk.pending_pnl = self._pending_pnl()
        self._gc(now)
        return actions

    # -- phases ----------------------------------------------------------------
    def _advance(self, w: WindowState, now: float, actions: list) -> None:
        spec = w.spec
        if w.phase == Phase.UPCOMING and now >= spec.start:
            w.phase = Phase.LIVE
        if w.phase == Phase.LIVE:
            if w.ptb is None:
                self._lock_ptb(w)
            if now >= spec.end:
                w.phase = Phase.CLOSED
                w.closed_at = now
                self._cancel_window_orders(w, actions, "window closed")
        if w.phase == Phase.CLOSED and w.provisional_winner is None and now >= spec.end + self.cfg.settlement.settle_grace_s:
            inflight = any(True for _ in self.orders.live(w.window_id))
            if not inflight or now >= spec.end + 30:
                self._provisional_settle(w, now)

    def _strikes(self, series: PriceSeries, start: float) -> tuple[float | None, float | None]:
        """Both candidate strikes from one feed: (TWAP over the `twap_seconds`
        before the open, price at the open). None where the data is missing."""
        cfg = self.cfg.settlement
        last = None
        b = series.last_bucket_at_or_before(start)
        if b is not None and start - b[0] <= cfg.ptb_tolerance_s:
            last = b[1]
        else:
            a = series.first_price_at_or_after(start, cfg.ptb_tolerance_s)
            if a is not None:
                last = a[1]
        first = int(start) - max(1, int(cfg.twap_seconds)) + 1
        twap = None
        if series.last_bucket_at_or_before(first) is not None and series.max_gap(first, start) <= cfg.max_gap_s:
            twap = series.sample_mean(first, int(start))
        return twap, last

    def _lock_ptb(self, w: WindowState) -> None:
        cfg = self.cfg.settlement
        series = self.oracle.get(w.asset)
        # Wait until the oracle has reported the opening second itself.
        if series is not None and series.last_ts is not None and series.last_ts >= w.spec.start \
                and w.ptb_twap is None and w.ptb_last is None:
            w.ptb_twap, w.ptb_last = self._strikes(series, w.spec.start)
        if w.official_ptb is not None and cfg.prefer_official_ptb:
            w.ptb, w.ptb_source = w.official_ptb, "official"
        elif cfg.rule == "twap" and w.ptb_twap is not None:
            w.ptb, w.ptb_source = w.ptb_twap, f"oracle {cfg.twap_seconds}s TWAP before the open"
        elif cfg.rule == "last" and w.ptb_last is not None:
            w.ptb, w.ptb_source = w.ptb_last, "oracle at the open"
        if w.ptb is not None:
            logger.info("%s price_to_beat locked at %.6g (%s)", w.window_id, w.ptb, w.ptb_source)

    def _on_official_ptb(self, window_id: str, price: float) -> None:
        w = self.windows.get(window_id)
        if w is None or price <= 0:
            return
        w.official_ptb = price
        if w.ptb is not None and abs(price / w.ptb - 1) > 1e-4:
            logger.warning(
                "%s official price_to_beat %.6g differs from oracle-locked %.6g (%.4f%%)",
                window_id, price, w.ptb, (price / w.ptb - 1) * 100,
            )
        if self.cfg.settlement.prefer_official_ptb and w.phase in (Phase.UPCOMING, Phase.LIVE):
            w.ptb, w.ptb_source = price, "official"

    # -- trading -----------------------------------------------------------------
    def _not_tradable(self, w: WindowState, reason: str, actions: list) -> None:
        # Reasons carry live numbers ("no new price for 7.3s"): log a change of
        # kind, not every step. Right after the open the opening second simply
        # hasn't arrived yet; only complain if the strike is still missing later.
        opening = w.ptb is None and self.now - w.spec.start < 5.0
        if _reason_key(reason) != _reason_key(w.tradable_reason) and not opening:
            logger.info("%s not tradable: %s", w.window_id, reason)
        w.tradable_reason = reason
        self._cancel_window_orders(w, actions, reason, quotes_only=True)

    def market_p_up(self, w: WindowState) -> float | None:
        up, down = self.books[w.spec.up_token], self.books[w.spec.down_token]
        if up.mid is not None:
            return up.mid
        return None if down.mid is None else 1.0 - down.mid

    def _trade_window(self, w: WindowState, now: float, actions: list) -> None:
        snap, reason = self._model(w, now)
        if snap is None:
            self._not_tradable(w, reason, actions)
            return
        w.last_model = snap
        self._maybe_snapshot(w, snap, now)
        limit = self.cfg.model.max_model_market_gap
        mkt = self.market_p_up(w)
        if limit > 0 and mkt is not None and abs(snap.p_up - mkt) > limit:
            self._not_tradable(w, (
                f"model p_up {snap.p_up:.2f} vs market {mkt:.2f}: gap > {limit:.2f}, "
                "assuming our data (feed or price_to_beat) is wrong or late"
            ), actions)
            return
        w.tradable_reason = ""
        if w.spec.end - now <= self.cfg.execution.cancel_quotes_before_end_s:
            self._cancel_window_orders(w, actions, "close approaching", quotes_only=True)
            return

        session = session_of(now, self.cfg.sessions)
        for strat in self.strategies:
            params = resolve_params(strat.cfg, w.asset, session)
            intents: list = []
            if self.risk.blocked_reason(strat.name, now) is None:
                ctx = self._context(w, snap, strat.name, params, session, now)
                try:
                    intents = strat.evaluate(ctx) or []
                except Exception:
                    logger.exception("Strategy %s crashed on %s; skipping this step", strat.name, w.window_id)
                    intents = []
            wanted = set()
            for it in intents:
                if isinstance(it, Take):
                    self._take(w, strat.name, params, it, snap, now, actions)
                elif isinstance(it, Quote):
                    wanted.add(it.key)
                    self._quote(w, strat.name, params, it, snap, now, actions)
            for st in list(self.orders.live(w.window_id, strat.name)):
                if st.req.quote_key is not None and st.req.quote_key not in wanted and not st.cancel_requested:
                    self._cancel(st, actions, "quote withdrawn")

    def _context(self, w: WindowState, snap: ModelSnapshot, name: str, params, session: str, now: float) -> StrategyContext:
        spec = w.spec
        quotes = {
            st.req.quote_key: QuoteView(st.req.quote_key, st.req.outcome, st.req.price, st.req.shares, st.filled)
            for st in self.orders.live(w.window_id, name)
            if st.req.quote_key is not None and not st.cancel_requested
        }
        return StrategyContext(
            now=now,
            spec=spec,
            window=w,
            model=snap,
            up_book=self.books[spec.up_token],
            down_book=self.books[spec.down_token],
            fees=self.fees,
            params=params,
            session=session,
            strategy=name,
            slippage=self.cfg.risk.slippage_buffer,
            market_blend=self.cfg.model.market_blend,
            slot_deltas=self._slot_deltas(spec, now),
            my_quotes=quotes,
            asset_return=lambda asset, lookback: self.asset_return(asset, lookback, now),
            spot_change=lambda lookback: self._spot_change(w.asset, lookback, now),
            remodel=lambda **kw: self._model(w, now, **kw)[0],
        )

    def _exposure(self, w: WindowState, strategy: str, outcome: str, exclude: str | None = None) -> "Exposure":
        e = Exposure()
        slot = w.spec.start
        for other in self.windows.values():
            if other.booked:
                continue
            live = [st for st in self.orders.live(other.window_id) if st.req.client_id != exclude]
            cost = other.cost()
            reserved = sum(st.reserved_usd for st in live)
            e.total += cost + reserved
            if cost > 0 or live:
                e.open_windows += 1
            if other.spec.start == slot:
                e.slot_direction += sum(h.cost for (_, o), h in other.holdings.items() if o == outcome)
                e.slot_direction += sum(st.reserved_usd for st in live if st.req.outcome == outcome)
            if other is w:
                e.window = cost + reserved
                e.strategy_window = other.cost(strategy) + sum(
                    st.reserved_usd for st in live if st.req.strategy == strategy
                )
                e.window_open = cost > 0 or bool(live)
        return e

    def _rate_ok(self, now: float) -> bool:
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        if len(self._sent) >= self.cfg.execution.max_orders_per_minute:
            if "rate" not in self._warned:
                logger.warning("Order rate limit (%d/min) reached; holding new orders", self.cfg.execution.max_orders_per_minute)
                self._warned.add("rate")
            return False
        self._warned.discard("rate")
        return True

    def _take(self, w: WindowState, name: str, params, it: Take, snap: ModelSnapshot, now: float, actions: list) -> None:
        if self.orders.has_inflight_take(w.window_id, name) or not self._rate_ok(now):
            return
        spec = w.spec
        token = spec.token(it.outcome)
        book = self.books[token]
        tick = book.tick_size  # starts at the market's tick; tick_size_change events update it
        limit = floor_to_tick(it.limit_price, tick)
        avail, vwap = book.vwap_buy(math.inf, limit)
        if avail <= 0:
            return
        e = self._exposure(w, name, it.outcome)
        max_shares = min(avail, it.max_shares) if it.max_shares is not None else avail
        dec = self.risk.size(
            strategy=name, strategy_cfg=params, p_win=it.p_win, price=vwap,
            fee_per_share=self.fees.taker_per_share(vwap), taker=True, elapsed=snap.elapsed, now=now,
            window_exposure=e.window, strategy_window_exposure=e.strategy_window, total_exposure=e.total,
            slot_direction_exposure=e.slot_direction, open_windows=e.open_windows,
            window_is_open=e.window_open, min_order_shares=spec.min_order_size,
            max_shares=max_shares, max_usd=it.max_usd,
        )
        self._log_decision(w, name, "take", it.outcome, limit, it.p_win, it.edge, dec, it.reason, snap, now)
        if not dec.ok:
            return
        req = OrderRequest(
            client_id=new_client_id(), window_id=w.window_id, strategy=name, token_id=token,
            outcome=it.outcome, price=limit, shares=dec.shares, tif=it.tif, tick_size=tick,
            neg_risk=spec.neg_risk, reason=it.reason[:300], created_ts=now,
            meta={"p_win": it.p_win, "edge": it.edge, "kelly": dec.kelly},
        )
        self.orders.add(req)
        self._sent.append(now)
        actions.append(PlaceOrder(req))
        w.entries[name] = w.entries.get(name, 0) + 1
        w.last_entry_ts[name] = now
        self.stats["takes"] += 1
        logger.info("[%s] TAKE %s %s %.2f sh <= %.3f | %s", self.mode.upper(), name, it.outcome, dec.shares, limit, it.reason)

    def _quote(self, w: WindowState, name: str, params, it: Quote, snap: ModelSnapshot, now: float, actions: list) -> None:
        spec = w.spec
        token = spec.token(it.outcome)
        book = self.books[token]
        tick = book.tick_size  # starts at the market's tick; tick_size_change events update it
        price = floor_to_tick(it.price, tick)
        if book.best_ask is not None and price > book.best_ask - tick + 1e-9:
            price = floor_to_tick(book.best_ask - tick, tick)
        comp = self.books.get(self.complement.get(token, ""))
        if comp is not None and comp.best_bid is not None and price >= 1.0 - comp.best_bid - 1e-9:
            price = floor_to_tick(1.0 - comp.best_bid - tick, tick)
        existing = self.orders.quote(w.window_id, name, it.key)
        if price < tick:
            if existing:
                self._cancel(existing, actions, "quote price invalid")
            return
        exclude = existing.req.client_id if existing else None
        e = self._exposure(w, name, it.outcome, exclude=exclude)
        dec = self.risk.size(
            strategy=name, strategy_cfg=params, p_win=it.p_win, price=price,
            fee_per_share=self.fees.maker_per_share(price), taker=False, elapsed=snap.elapsed, now=now,
            window_exposure=e.window, strategy_window_exposure=e.strategy_window, total_exposure=e.total,
            slot_direction_exposure=e.slot_direction, open_windows=e.open_windows,
            window_is_open=e.window_open, min_order_shares=spec.min_order_size,
            max_shares=it.shares, max_usd=it.max_usd, fixed_size=it.fixed_size,
        )
        key = (w.window_id, name, it.key)
        if not dec.ok:
            if existing:
                self._cancel(existing, actions, dec.reason)
            return
        if existing is not None:
            must_move = it.limit is not None and existing.req.price > it.limit + 1e-9  # no longer +EV
            small_move = abs(existing.req.price - price) < self.cfg.execution.requote_min_ticks * tick - 1e-9
            same_size = abs(existing.remaining - dec.shares) <= max(1.0, 0.2 * dec.shares)
            throttled = now - self._quote_changed.get(key, -math.inf) < self.cfg.execution.requote_min_interval_s
            if not must_move and (throttled or (small_move and same_size)):
                return
            self._cancel(existing, actions, f"requote {existing.req.price:.3f}->{price:.3f}")
        elif now - self._quote_changed.get(key, -math.inf) < self.cfg.execution.requote_min_interval_s:
            return
        if not self._rate_ok(now):
            return
        req = OrderRequest(
            client_id=new_client_id(), window_id=w.window_id, strategy=name, token_id=token,
            outcome=it.outcome, price=price, shares=dec.shares, tif="GTC", post_only=True,
            quote_key=it.key, tick_size=tick, neg_risk=spec.neg_risk, reason=it.reason[:300],
            created_ts=now, meta={"p_win": it.p_win, "kelly": dec.kelly},
        )
        self._log_decision(w, name, "quote", it.outcome, price, it.p_win, it.p_win - price, dec, it.reason, snap, now)
        self.orders.add(req)
        self._sent.append(now)
        self._quote_changed[key] = now
        actions.append(PlaceOrder(req))
        self.stats["quotes"] += 1
        logger.info("[%s] QUOTE %s %s %.2f sh @ %.3f | %s", self.mode.upper(), name, it.outcome, dec.shares, price, it.reason)

    def _cancel(self, st, actions: list, reason: str) -> None:
        if st.cancel_requested or st.final:
            return
        st.cancel_requested = True
        actions.append(CancelOrder(st.req.client_id, reason))
        self.stats["cancels"] += 1

    def _cancel_window_orders(self, w: WindowState, actions: list, reason: str, quotes_only: bool = False) -> None:
        for st in list(self.orders.live(w.window_id)):
            if quotes_only and st.req.quote_key is None:
                continue
            self._cancel(st, actions, reason)

    # -- model & market context ------------------------------------------------------
    def _model(self, w: WindowState, now: float, drift: float = 0.0) -> tuple[ModelSnapshot | None, str]:
        spec = w.spec
        if w.ptb is None:
            return None, "no price_to_beat yet (needs oracle data for the minute before the open)"
        for feed in ("oracle", "clob"):
            if feed in self.feed_down:
                return None, f"{feed} feed down: {self.feed_down[feed]}"
        up_book, down_book = self.books[spec.up_token], self.books[spec.down_token]
        if not (up_book.has_snapshot and down_book.has_snapshot):
            return None, "waiting for order books"
        series = self.oracle.get(w.asset)
        if series is None or series.last_price is None:
            return None, "no oracle data"
        silence = self._silence("oracle", w.asset, now)
        if silence > self.cfg.feeds.oracle_max_age_s:
            return None, f"oracle stale (no new price for {silence:.1f}s)"
        age = series.age(now)
        vol = self._vol(w.asset, series, now)
        if vol is None:
            return None, "insufficient history for volatility"
        spot, cex_px, why = self._effective_spot(w.asset, now)
        if spot is None:
            return None, why
        realized_mean = None
        first_sample = int(spec.end) - self.samples + 1
        if int(math.floor(now)) >= first_sample:
            last = min(int(math.floor(now)), int(spec.end))
            realized_mean = series.sample_mean(first_sample, last)
            if realized_mean is None:
                return None, "no oracle data at settlement window start"
            gap = series.max_gap(first_sample, min(now, spec.end))
            if gap > self.cfg.settlement.max_gap_s:
                return None, f"oracle gap {gap:.0f}s inside the settlement window"
        snap = evaluate_window(
            now=now, start=spec.start, end=spec.end, samples=self.samples, ptb=w.ptb, spot=spot,
            oracle_price=series.last_price, oracle_age=age, cex_price=cex_px, vol=vol,
            realized_mean=realized_mean, cfg=self.cfg.model, drift=drift,
        )
        return snap, ""

    def _effective_spot(self, asset: str, now: float) -> tuple[float | None, float | None, str]:
        oracle_px = self.oracle[asset].last_price
        cex = self.cex.get(asset)
        weight = self.cfg.model.cex_lead_weight
        stale = self._silence("cex", asset, now) > self.cfg.feeds.cex_max_age_s
        if cex is None or cex.last_price is None or stale or weight <= 0:
            return oracle_px, None, ""
        nc = self._nowcaster(asset).nowcast(cex.last_price)
        if nc is None:
            return oracle_px, cex.last_price, ""
        if abs(nc / oracle_px - 1.0) > self.cfg.model.max_cex_oracle_divergence:
            return None, cex.last_price, f"CEX nowcast diverges from oracle by {(nc / oracle_px - 1) * 100:+.3f}%"
        return oracle_px + weight * (nc - oracle_px), cex.last_price, ""

    def _silence(self, store: str, asset: str, now: float) -> float:
        """Seconds since a new price for `asset` last arrived from `store`."""
        return now - self._arrival.get((store, asset), -math.inf)

    def current_price(self, asset: str, now: float) -> float | None:
        o = self.oracle.get(asset)
        if o is not None and self._silence("oracle", asset, now) <= self.cfg.feeds.oracle_max_age_s:
            return o.last_price
        c = self.cex.get(asset)
        if c is not None and self._silence("cex", asset, now) <= self.cfg.feeds.cex_max_age_s:
            return c.last_price
        return None

    def asset_return(self, asset: str, lookback: float, now: float) -> float | None:
        for series in (self.oracle.get(asset), self.cex.get(asset)):
            if series is not None and series.covers(now - lookback) and series.age(now) <= 60:
                r = series.log_return(now, lookback)
                if r is not None:
                    return math.expm1(r)
        return None

    def _spot_change(self, asset: str, lookback: float, now: float) -> float | None:
        o = self.oracle.get(asset)
        if o is None or o.last_price is None or not o.covers(now - lookback):
            return None
        past = o.price_at(now - lookback)
        return None if past is None else o.last_price - past

    def _slot_open_price(self, start: float, asset: str) -> float | None:
        key = (start, asset)
        if key in self._slot_open:
            return self._slot_open[key]
        for w in self.windows.values():
            if w.asset == asset and w.spec.start == start and w.ptb is not None:
                self._slot_open[key] = w.ptb
                return w.ptb
        for series in (self.oracle.get(asset), self.cex.get(asset)):
            if series is None or series.last_ts is None or series.last_ts < start:
                continue
            twap, last = self._strikes(series, start)
            value = twap if self.cfg.settlement.rule == "twap" else last
            if value is not None:
                self._slot_open[key] = value
                return value
        return None

    def _slot_deltas(self, spec: WindowSpec, now: float) -> dict:
        out = {}
        for asset in self.cfg.markets.assets:
            open_px = self._slot_open_price(spec.start, asset)
            cur = self.current_price(asset, now)
            out[asset] = None if not open_px or cur is None else cur / open_px - 1.0
        return out

    # -- settlement ------------------------------------------------------------------------
    def _provisional_settle(self, w: WindowState, now: float) -> None:
        spec = w.spec
        series = self.oracle.get(w.asset)
        if series is not None:
            twap_n = max(1, int(self.cfg.settlement.twap_seconds))
            w.settle_twap = settlement_value(series, spec.end, twap_n)
            w.settle_last = settlement_value(series, spec.end, 1)
            gap = series.max_gap(int(spec.end) - self.samples + 1, spec.end)
            rule_value = w.settle_twap if self.samples > 1 else w.settle_last
            if w.ptb is not None and rule_value is not None and gap <= self.cfg.settlement.max_gap_s:
                w.provisional_winner = UP if rule_value >= w.ptb else DOWN
        if w.provisional_winner is None:
            w.provisional_winner = ""  # unknown; rely on the official result
        self.journal.settlement(self._settlement_row(w, "provisional"))
        if w.holdings:
            logger.info(
                "%s closed: ptb=%s twap=%s last=%s -> provisional %s",
                w.window_id, _fmt(w.ptb), _fmt(w.settle_twap), _fmt(w.settle_last), w.provisional_winner or "unknown",
            )

    def _on_official_resolution(self, window_id: str, winner: str) -> None:
        w = self.windows.get(window_id)
        if w is None or winner not in OUTCOMES or w.official_winner == winner:
            return
        w.official_winner = winner
        if w.provisional_winner and w.provisional_winner != winner:
            logger.warning(
                "%s: official winner %s != computed %s (check settlement.rule / price_to_beat)",
                window_id, winner, w.provisional_winner,
            )
        if w.booked and w.booked_winner != winner:
            if w.pnl:
                self._correct(w, winner)
            else:
                w.booked_winner = winner
        self.journal.settlement(self._settlement_row(w, "official"))

    def _book_settlements(self, now: float) -> None:
        cfg = self.cfg.settlement
        for w in self.windows.values():
            if w.booked or w.phase != Phase.CLOSED or w.provisional_winner is None:
                continue
            if not any(h.shares > 1e-9 for h in w.holdings.values()):
                w.booked, w.phase = True, Phase.SETTLED  # nothing to book
                w.booked_winner = w.official_winner or w.provisional_winner or None
                continue
            winner = w.official_winner
            if winner is None and w.provisional_winner:
                if not cfg.use_official:
                    winner = w.provisional_winner
                elif now >= w.spec.end + cfg.official_timeout_s:
                    logger.warning("%s: no official resolution after %.0fs; booking computed result", w.window_id, cfg.official_timeout_s)
                    winner = w.provisional_winner
            if winner is None:
                continue
            self._book(w, winner, now)

    def _book(self, w: WindowState, winner: str, now: float) -> None:
        per_strategy: dict[str, float] = {}
        for (strategy, outcome), h in w.holdings.items():
            if h.shares <= 1e-9:
                continue
            won = outcome == winner
            pnl = (h.shares if won else 0.0) - h.cost
            per_strategy[strategy] = per_strategy.get(strategy, 0.0) + pnl
            self.journal.settle(
                ts=now, strategy=strategy, window_id=w.window_id, token_id=w.spec.token(outcome),
                outcome=outcome, shares=h.shares, won=won, winner=winner,
            )
        for strategy, pnl in per_strategy.items():
            w.pnl[strategy] = pnl
            self.risk.on_settlement(strategy, pnl, now)
        w.booked, w.booked_winner, w.phase = True, winner, Phase.SETTLED
        if per_strategy:
            total = sum(per_strategy.values())
            logger.info(
                "[%s] SETTLED %s winner=%s pnl=$%+.2f %s | day=$%+.2f",
                self.mode.upper(), w.window_id, winner, total,
                {k: round(v, 2) for k, v in per_strategy.items()}, self.risk.realized_pnl_today,
            )
        self.journal.settlement(self._settlement_row(w, "booked"))

    def _correct(self, w: WindowState, winner: str) -> None:
        total_delta = 0.0
        for (strategy, outcome), h in w.holdings.items():
            if h.shares <= 1e-9:
                continue
            old = h.shares if outcome == w.booked_winner else 0.0
            new = h.shares if outcome == winner else 0.0
            delta = new - old
            if abs(delta) > 1e-9:
                total_delta += delta
                w.pnl[strategy] = w.pnl.get(strategy, 0.0) + delta
                self.journal.adjust(
                    ts=self.now, strategy=strategy, window_id=w.window_id, token_id=w.spec.token(outcome),
                    outcome=outcome, pnl_delta=delta,
                    reason=f"official winner {winner} != booked {w.booked_winner}",
                )
        self.risk.on_correction(total_delta, self.now)
        w.booked_winner = winner
        logger.warning("%s: corrected P&L by $%+.2f after official resolution", w.window_id, total_delta)

    def _pending_pnl(self) -> float:
        pending = 0.0
        for w in self.windows.values():
            if w.booked or not w.provisional_winner:
                continue
            for (_, outcome), h in w.holdings.items():
                pending += (h.shares if outcome == w.provisional_winner else 0.0) - h.cost
        return pending

    def _record_result(self, w: WindowState) -> None:
        self.results[w.window_id] = {
            "asset": w.asset, "start": w.spec.start, "ptb": w.ptb,
            "provisional": w.provisional_winner or None, "official": w.official_winner,
            "booked": w.booked_winner, "pnl": dict(w.pnl),
        }

    def _settlement_row(self, w: WindowState, event: str) -> dict:
        self._record_result(w)

        def rule_winner(settle, strike):
            if settle is None or strike is None:
                return ""
            return UP if settle >= strike else DOWN

        return {
            "window_id": w.window_id, "asset": w.asset, "start": w.spec.start, "end": w.spec.end,
            "ptb": w.ptb, "ptb_source": w.ptb_source, "settle_twap": w.settle_twap, "settle_last": w.settle_last,
            # Each rule with its own strike: TWAP-vs-TWAP, and price-at-close vs price-at-open.
            "winner_twap_rule": rule_winner(w.settle_twap, w.ptb_twap),
            "winner_last_rule": rule_winner(w.settle_last, w.ptb_last),
            "provisional_winner": w.provisional_winner or "", "official_winner": w.official_winner or "",
            "booked_winner": w.booked_winner or "", "event": event,
            "pnl_total": round(sum(w.pnl.values()), 6), "pnl_by_strategy": {k: round(v, 6) for k, v in w.pnl.items()},
        }

    # -- journaling helpers ------------------------------------------------------------------
    def _maybe_snapshot(self, w: WindowState, snap: ModelSnapshot, now: float) -> None:
        if now - w.last_snapshot_log < self.cfg.journal.snapshot_every_s:
            return
        w.last_snapshot_log = now
        up, down = self.books[w.spec.up_token], self.books[w.spec.down_token]
        w.history.append((round(now, 1), snap.p_up, snap.p_up_lo, snap.p_up_hi, self.market_p_up(w)))
        self.journal.snapshot({
            "ts": round(now, 3), "window_id": w.window_id, "asset": w.asset,
            "elapsed": round(snap.elapsed, 2), "remaining": round(snap.remaining, 2),
            "ptb": w.ptb, "spot": snap.spot, "oracle": snap.oracle_price, "cex": snap.cex_price,
            "delta": snap.delta, "sigma": snap.sigma, "z": _finite(snap.z),
            "p_up": snap.p_up, "p_up_lo": snap.p_up_lo, "p_up_hi": snap.p_up_hi,
            "up_bid": up.best_bid, "up_ask": up.best_ask, "down_bid": down.best_bid, "down_ask": down.best_ask,
            "realized_n": snap.realized_n, "realized_mean": snap.realized_mean,
        })

    def _log_decision(self, w, name, kind, outcome, price, p_win, edge, dec, reason, snap, now) -> None:
        if not dec.ok:
            # Strategies re-evaluate every step; log a repeated rejection at most every 10s.
            key = (w.window_id, name, kind, outcome, dec.reason[:40])
            last = self._decision_log_ts.get(key)
            if last is not None and now - last < 10.0:
                return
            self._decision_log_ts[key] = now
        self.journal.decision({
            "ts": round(now, 3), "window_id": w.window_id, "asset": w.asset, "strategy": name,
            "kind": kind, "outcome": outcome, "price": price, "p_win": p_win, "edge": edge,
            "accepted": dec.ok, "shares": dec.shares, "usd": dec.usd, "kelly": dec.kelly,
            "risk_note": dec.reason, "reason": reason, "remaining": round(snap.remaining, 2),
            "p_up": snap.p_up, "p_up_lo": snap.p_up_lo, "p_up_hi": snap.p_up_hi, "delta": snap.delta,
            "sigma": snap.sigma, "ptb": w.ptb, "spot": snap.spot,
        })
        if not dec.ok:
            logger.debug("%s %s %s rejected by risk: %s", name, kind, w.window_id, dec.reason)

    # -- housekeeping ------------------------------------------------------------------------
    def _gc(self, now: float) -> None:
        for wid in [wid for wid, w in self.windows.items() if w.booked and now > w.spec.end + 1800]:
            w = self.windows.pop(wid)
            for tok in (w.spec.up_token, w.spec.down_token):
                self.token_index.pop(tok, None)
                self.complement.pop(tok, None)
                self.books.pop(tok, None)
        for key in [k for k in self._slot_open if now - k[0] > 7200]:
            del self._slot_open[key]
        for key in [k for k, ts in self._decision_log_ts.items() if now - ts > 600]:
            del self._decision_log_ts[key]
        self.orders.prune(now)

    # -- queries for the runner -------------------------------------------------------------
    def active_tokens(self) -> set:
        toks = set()
        for w in self.windows.values():
            if w.phase in (Phase.UPCOMING, Phase.LIVE) or (w.phase == Phase.CLOSED and self.now < w.spec.end + 30):
                toks.update((w.spec.up_token, w.spec.down_token))
        return toks

    def windows_needing_resolution(self, now: float) -> list[WindowState]:
        return [
            w for w in self.windows.values()
            if w.official_winner is None and w.phase in (Phase.CLOSED, Phase.SETTLED)
            and w.spec.end + 5 <= now <= w.spec.end + self.cfg.settlement.official_timeout_s
        ]

    def windows_needing_ptb(self, now: float) -> list[WindowState]:
        return [
            w for w in self.windows.values()
            if w.official_ptb is None and w.phase in (Phase.UPCOMING, Phase.LIVE) and w.spec.start <= now <= w.spec.start + 120
        ]

    def status_snapshot(self, now: float) -> dict:
        """Plain-JSON view of the live state for the monitoring dashboard.
        Built in the event loop; non-finite numbers become None."""
        risk = self.risk
        windows = []
        for w in sorted(self.windows.values(), key=lambda w: (w.spec.start, w.asset)):
            if w.phase == Phase.SETTLED and now - w.spec.end > 180:
                continue
            spec, m = w.spec, w.last_model
            up, down = self.books.get(spec.up_token), self.books.get(spec.down_token)
            windows.append({
                "id": w.window_id, "asset": w.asset, "phase": w.phase.value,
                "start": spec.start, "end": spec.end,
                "elapsed": now - spec.start, "remaining": max(0.0, spec.end - now),
                "ptb": w.ptb, "ptb_source": w.ptb_source,
                "tradable": w.phase == Phase.LIVE and not w.tradable_reason and m is not None,
                "reason": w.tradable_reason,
                "model": None if m is None else {
                    "ts": m.ts, "p_up": m.p_up, "p_up_lo": m.p_up_lo, "p_up_hi": m.p_up_hi,
                    "delta": m.delta, "spot": m.spot, "z": m.z,
                    "sigma_annual": m.sigma * math.sqrt(365.0 * 24 * 3600),
                    "realized_n": m.realized_n, "samples": self.samples,
                },
                "market": {
                    "p_up": self.market_p_up(w) if up is not None and down is not None else None,
                    "up_bid": up.best_bid if up else None, "up_ask": up.best_ask if up else None,
                    "down_bid": down.best_bid if down else None, "down_ask": down.best_ask if down else None,
                },
                "holdings": [
                    {"strategy": st, "outcome": o, "shares": h.shares, "cost": h.cost}
                    for (st, o), h in sorted(w.holdings.items()) if h.shares > 1e-9
                ],
                "orders": [
                    {"strategy": st.req.strategy, "outcome": st.req.outcome, "price": st.req.price,
                     "shares": st.req.shares, "filled": st.filled, "tif": st.req.tif}
                    for st in self.orders.live(w.window_id)
                ],
                "history": [list(row) for row in w.history],
                "provisional": w.provisional_winner or None, "official": w.official_winner,
                "booked": w.booked, "pnl": dict(w.pnl),
            })
        results = sorted(self.results.items(), key=lambda kv: kv[1]["start"], reverse=True)[:40]
        exposure = sum(w.cost() for w in self.windows.values() if not w.booked) + self.orders.reserved()
        return _json_safe({
            "ts": now,
            "risk": {
                "realized_today": risk.realized_pnl_today, "realized_total": risk.realized_pnl_total,
                "pending": risk.pending_pnl, "bankroll": risk.bankroll,
                "max_daily_loss": risk.cfg.max_daily_loss_usd, "kill_switch": risk.daily_loss_hit,
                "streaks": dict(risk.loss_streak),
                "cooldowns": {k: v - now for k, v in risk.cooldown_until.items() if v > now},
            },
            "exposure": {"total": exposure, "cap": risk.cfg.max_total_exposure_usd},
            "stats": dict(self.stats),
            "feed_down": dict(self.feed_down),
            "windows": windows,
            "results": [{"id": wid, **r} for wid, r in results],
        })

    def status_line(self) -> str:
        live = [w for w in self.windows.values() if w.phase == Phase.LIVE]
        parts = []
        for w in sorted(live, key=lambda w: w.asset):
            m = w.last_model
            if m is not None and not w.tradable_reason:
                up = self.books[w.spec.up_token]
                parts.append(
                    f"{w.asset}:p_up={m.p_up:.2f}/mkt={_fmt(up.mid, 2)} d={m.delta * 100:+.2f}% {m.remaining:.0f}s"
                )
            else:
                parts.append(f"{w.asset}:({w.tradable_reason or 'warming up'})")
        exposure = sum(w.cost() for w in self.windows.values() if not w.booked) + self.orders.reserved()
        return (
            f"live={len(live)} exposure=${exposure:.2f} day=${self.risk.realized_pnl_today:+.2f} "
            f"pending=${self.risk.pending_pnl:+.2f} | " + " | ".join(parts)
        )

    # -- internals -------------------------------------------------------------------------
    def _vol(self, asset: str, series: PriceSeries, now: float):
        """Realized-vol estimate, cached per asset per second (it moves slowly)."""
        key = int(now)
        hit = self._vol_cache.get(asset)
        if hit is not None and hit[0] == key:
            return hit[1]
        vol = estimate_vol(series, self.cex.get(asset), now, asset, self.cfg.model)
        self._vol_cache[asset] = (key, vol)
        return vol

    def _series(self, store: dict, asset: str) -> PriceSeries:
        s = store.get(asset)
        if s is None:
            s = store[asset] = PriceSeries(max_age_s=max(7200.0, self.cfg.model.vol_long_s + 600))
        return s

    def _nowcaster(self, asset: str) -> Nowcaster:
        n = self.nowcasters.get(asset)
        if n is None:
            n = self.nowcasters[asset] = Nowcaster(self.cfg.model.basis_halflife_s)
        return n


def _fmt(x, digits: int = 6) -> str:
    return "n/a" if x is None else f"{x:.{digits}g}" if digits > 2 else f"{x:.{digits}f}"


def _finite(x: float):
    return x if math.isfinite(x) else (1e9 if x > 0 else -1e9)


def _reason_key(reason: str) -> str:
    return re.sub(r"\d+(\.\d+)?", "#", reason or "")


def _json_safe(obj):
    """Copy of obj with inf/nan replaced by None (strict JSON for the browser)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj
