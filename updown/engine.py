"""Main loop for 5-minute Up/Down markets: one window at a time, per asset.

Each tick (default 1s) for every asset:
  1. make sure we know the current window's market and its strike price
  2. if inside the trading slice, read both order books and ask the strategy
  3. execute at most one decision, journal it
Finished windows are settled from Polymarket's resolution (falling back to
our own feed if that's slow), and their P&L feeds the risk limits.

Everything time-dependent takes `now` as an argument, so the simulator and
tests drive the exact same code as live trading.
"""
from __future__ import annotations

import copy
import logging
import threading
from collections import deque
from dataclasses import dataclass, field

from bot.journal import TradeJournal
from bot.strategies.base import Signal
from updown.config import UpDownSettings
from updown.feeds import PriceHistory
from updown.maker import FastMoveGuard, LeadGuard, MakerQuoter, PaperMakerBroker
from updown.markets import WindowMarket, window_start
from updown.model import VolEstimator, prob_vol_1s, twap_prob_vol_1s
from updown.risk import UpDownRisk
from updown.strategy import DOWN, UP, Snapshot, UpDownStrategy, WindowPosition

logger = logging.getLogger("polybot.updown.engine")

STRATEGY_NAME = "updown_5m"
DISCOVERY_RETRY_S = 5.0
STRIKE_TOLERANCE_S = 2.0
# If no tick landed just before the open, accept the first one this soon after.
STRIKE_LATE_S = 4.0
STATUS_LOG_EVERY_S = 30.0
TRADES_POLL_S = 2.0


@dataclass
class WindowState:
    asset: str
    start: int
    end: int
    market: WindowMarket | None = None
    strike: float | None = None
    skip_reason: str = ""
    last_discovery_try: float = -1e18
    warned_missing: bool = False
    pos: WindowPosition = field(default_factory=WindowPosition)
    cash_usd: float = 0.0  # sells - buys - fees
    fees_usd: float = 0.0
    settled: bool = False
    last_resolution_try: float = -1e18
    last_status_log: float = -1e18
    # For the dashboard: spot path, last books seen, last decision reason, fills.
    track: list[tuple[float, float]] = field(default_factory=list)
    books: dict | None = None
    why: str = ""
    fills: list[dict] = field(default_factory=list)
    last_entry_ts: float | None = None
    trades: list = field(default_factory=list)
    last_trades_fetch: float = -1e18

    @property
    def has_position(self) -> bool:
        return any(v > 1e-9 for v in self.pos.shares.values())


@dataclass
class Stats:
    windows_traded: int = 0
    wins: int = 0
    losses: int = 0
    pnl_usd: float = 0.0
    fees_usd: float = 0.0
    orders: int = 0


class UpDownEngine:
    def __init__(self, settings: UpDownSettings, gateway, broker, history: PriceHistory, journal: TradeJournal | None, settle_fallback_s: float = 600.0, maker_broker=None,
                 lead_history: PriceHistory | None = None):
        self.s = settings
        self.gateway = gateway
        self.broker = broker
        self.history = history
        self.journal = journal
        self.settle_fallback_s = settle_fallback_s
        self.sizing = copy.deepcopy(settings.sizing)  # bankroll grows/shrinks with P&L
        self.strategy = UpDownStrategy(settings.strategy, settings.model, settings.fees, self.sizing)
        self.risk = UpDownRisk(settings.risk)
        self.vol = {a: VolEstimator(settings.model.vol_halflife_s, settings.model.vol_floor, settings.model.vol_cap) for a in settings.assets}
        self.windows: dict[tuple[str, int], WindowState] = {}
        self.stats = Stats()
        self.mode = "live" if broker.live else "paper"
        self.maker_mode = settings.mode == "maker"
        self.quoter = MakerQuoter(settings.maker)
        self.maker_broker = maker_broker or PaperMakerBroker(settings.maker)
        self.fast_guard = FastMoveGuard(settings.maker)
        # Early-warning prices (Binance / Hyperliquid), if running.
        self.lead_history = lead_history
        self.lead_guard = LeadGuard(settings.maker)
        # Set from the lead feed's thread to run a tick right away (live only).
        self.wake = threading.Event()
        self.started_at: float | None = None
        self.recent_windows: deque[dict] = deque(maxlen=64 * max(1, len(settings.assets)))  # newest last
        self.asset_stats: dict[str, Stats] = {a: Stats() for a in settings.assets}
        self.events: deque[dict] = deque(maxlen=60)  # fills and settlements, newest last

    # -- inputs ---------------------------------------------------------
    def on_price(self, asset: str, ts: float, price: float) -> None:
        self.vol[asset].update(ts, price)

    def on_lead_price(self, asset: str, ts: float, price: float) -> None:
        """Lead-feed thread: wake the main loop at once if a sharp move starts,
        so live bids get cancelled now instead of at the next 1s tick."""
        if not self.broker.live or self.lead_history is None or asset not in self.vol:
            return
        if ts < self.lead_guard.until.get(asset, 0.0):
            return  # already pulled
        z = self.lead_guard.move_z(self.lead_history, asset, ts, self.vol[asset].sigma)
        if z is not None and z > self.s.maker.lead_move_z:
            self.wake.set()

    def _lead_pulled(self, asset: str, now: float) -> bool:
        if self.lead_history is None or not self.s.maker.lead_guard:
            return False
        before = self.lead_guard.trips.get(asset, 0)
        pulled = self.lead_guard.check(self.lead_history, asset, now, self.vol[asset].sigma)
        if self.lead_guard.trips.get(asset, 0) > before:
            logger.info("[%s] early warning: sharp move on the lead venue; bids pulled for %.0fs", asset, self.s.maker.lead_cooldown_s)
        return pulled

    # -- main tick -------------------------------------------------------
    def tick(self, now: float) -> None:
        if self.started_at is None:
            self.started_at = now
        states = []
        for asset in self.s.assets:
            start = window_start(now, self.s.window_seconds)
            key = (asset, start)
            if key not in self.windows:
                self.windows[key] = WindowState(asset, start, start + self.s.window_seconds)
            st = self.windows[key]
            try:
                self._prepare(st, now)
                states.append(st)
            except Exception:
                logger.exception("[%s] error preparing window %d", asset, start)
        books = self._fetch_books([st for st in states if self._wants_books(st, now)])
        for st in states:
            try:
                if self.maker_mode:
                    self._make(st, now, books)
                else:
                    self._trade(st, now, books)
                self._record_track(st, now)
            except Exception:
                logger.exception("[%s] error while trading window %d", st.asset, st.start)
        self._settle(now)

    def _wants_books(self, st: WindowState, now: float) -> bool:
        if st.market is None or st.strike is None:
            return False
        seconds_left = st.end - now
        if self.maker_mode:
            # From quote start until the close: books are needed to quote and,
            # in paper mode, to see whether resting bids got hit.
            return 0 < seconds_left and now - st.start >= self.s.maker.quote_start_s
        # Before the trading slice there's nothing to do; don't spend API calls.
        return 0 < seconds_left and (seconds_left <= self.s.strategy.max_seconds_left or st.has_position)

    def _fetch_books(self, states: list[WindowState]) -> dict:
        tokens = [st.market.tokens[o] for st in states for o in (UP, DOWN)]
        if not tokens:
            return {}
        if hasattr(self.gateway, "books"):
            try:
                return self.gateway.books(tokens)
            except Exception as exc:
                logger.warning("Batch order-book fetch failed (%s); falling back to one request per book", exc)
        out = {}
        for t in tokens:
            try:
                out[t] = self.gateway.book(t)
            except Exception as exc:
                logger.warning("Order book fetch failed for %s: %s", t, exc)
        return out

    def _prepare(self, st: WindowState, now: float) -> None:
        if st.strike is None and not st.skip_reason:
            # Strike comes from OUR feed at the open, not Polymarket's
            # "price to beat": settlement compares oracle-end vs oracle-start,
            # so comparing feed-now vs feed-start cancels the constant gap
            # between the two sources. Mixing sources would bake it in.
            w = self.s.model.twap_window_s
            if w > 0:
                # The price to beat is the Chainlink TWAP at the open: the
                # average over the W seconds before it. Rebuild it from our feed.
                st.strike = self.history.twap(st.asset, st.start - w, st.start)
            else:
                st.strike = self.history.price_near(st.asset, st.start, STRIKE_TOLERANCE_S, STRIKE_LATE_S)
            if st.strike is None:
                st.skip_reason = (
                    f"not enough feed history for the opening {w:.0f}s TWAP (bot started recently?)" if w > 0
                    else "no feed price at window open (bot started mid-window?)"
                )
                logger.info("[%s] skipping window %d: %s", st.asset, st.start, st.skip_reason)
        if st.market is None and not st.skip_reason and now - st.last_discovery_try >= DISCOVERY_RETRY_S:
            st.last_discovery_try = now
            try:
                st.market = self.gateway.discover(st.asset, st.start)
            except Exception as exc:  # network trouble: one line, retried in a few seconds
                if now - st.last_status_log >= STATUS_LOG_EVERY_S:
                    st.last_status_log = now
                    logger.warning("[%s] market lookup failed (will retry): %s", st.asset, _short(exc))
                return
            if st.market is None and now - st.start >= 30 and not st.warned_missing:
                st.warned_missing = True
                logger.warning(
                    "[%s] no market found for window %d after 30s. Check slug_template in "
                    "config/updown.yaml against the URL of a live 5-minute market on polymarket.com.",
                    st.asset, st.start,
                )
            if st.market:
                ptb = st.market.price_to_beat
                logger.info(
                    "[%s] window %d market %s | strike(feed)=%.2f%s",
                    st.asset, st.start, st.market.slug, st.strike,
                    f" | polymarket price_to_beat={ptb:.2f} (gap {ptb - st.strike:+.2f})" if ptb else "",
                )

    def twap_so_far(self, st: WindowState, now: float) -> float | None:
        """Average price so far inside the closing TWAP window, once it has started."""
        w = self.s.model.twap_window_s
        opens = st.end - w
        if w <= 0 or now <= opens + 1:
            return None
        return self.history.twap(st.asset, opens, min(now, st.end), min_coverage=0.5)

    def snapshot(self, st: WindowState, now: float, spot: float, books: dict) -> Snapshot:
        return Snapshot(
            spot=spot, strike=st.strike, seconds_left=st.end - now, sigma=self.vol[st.asset].sigma,
            books=books, twap_so_far=self.twap_so_far(st, now),
        )

    def _record_track(self, st: WindowState, now: float) -> None:
        latest = self.history.latest(st.asset)
        if st.strike is None or latest is None or latest[0] < st.start:
            return
        if not st.track or latest[0] > st.track[-1][0]:
            st.track.append((latest[0], latest[1]))

    def _side_exposure(self) -> dict[str, float]:
        """Open cost on each side, summed across every coin's unsettled window."""
        out = {UP: 0.0, DOWN: 0.0}
        for w in self.windows.values():
            if not w.settled:
                for o in (UP, DOWN):
                    out[o] += w.pos.cost_usd[o]
        return out

    def _open_exposure(self) -> float:
        return sum(w.pos.total_cost_usd for w in self.windows.values() if not w.settled and w.has_position)

    def _trade(self, st: WindowState, now: float, all_books: dict) -> None:
        if not self._wants_books(st, now):
            return
        seconds_left = st.end - now
        latest = self.history.latest(st.asset)
        if latest is None or now - latest[0] > self.s.feed.max_age_s:
            st.why = "price feed stale; not trading"
            if now - st.last_status_log >= STATUS_LOG_EVERY_S:
                st.last_status_log = now
                logger.warning("[%s] price feed stale; not trading", st.asset)
            return

        allowed, why_not = self.risk.can_trade(now, st.asset)
        room = max(0.0, self.risk.room_usd(now, st.asset) - self._open_exposure()) if allowed else 0.0

        books = {o: all_books.get(st.market.tokens[o]) for o in (UP, DOWN)}
        if books[UP] is None or books[DOWN] is None:
            st.why = "order book unavailable this tick"
            return
        snap = self.snapshot(st, now, latest[1], books)
        side = self._side_exposure()
        cap = self.s.sizing.max_same_direction_usd
        side_room = {o: max(0.0, cap - side[o]) for o in (UP, DOWN)}
        since = now - st.last_entry_ts if st.last_entry_ts is not None else None
        decision, why = self.strategy.decide(snap, st.pos, room, side_room, since)
        st.books = books
        st.why = why_not or why

        if now - st.last_status_log >= STATUS_LOG_EVERY_S:
            st.last_status_log = now
            logger.info(
                "[%s] %3.0fs left | spot %.2f vs strike %.2f | P(up)=%.3f | Up %s/%s Down %s/%s | %s",
                st.asset, seconds_left, snap.spot, snap.strike, self.strategy.prob_up(snap),
                _fmt(books[UP].best_bid), _fmt(books[UP].best_ask), _fmt(books[DOWN].best_bid), _fmt(books[DOWN].best_ask),
                why_not or why,
            )
        if decision is None:
            return

        token = st.market.tokens[decision.outcome]
        fill = self.broker.execute(token, decision)
        filled = fill.shares > 0
        if filled:
            self.stats.orders += 1
            self.asset_stats[st.asset].orders += 1
            st.fees_usd += fill.fee_usd
            if decision.side == "BUY":
                st.pos.shares[decision.outcome] += fill.shares
                st.pos.cost_usd[decision.outcome] += fill.usd + fill.fee_usd
                st.pos.entries += 1
                st.last_entry_ts = now
                st.cash_usd -= fill.usd + fill.fee_usd
            else:
                held = st.pos.shares[decision.outcome]
                sold = min(fill.shares, held)
                st.pos.cost_usd[decision.outcome] *= (held - sold) / held if held else 0.0
                st.pos.shares[decision.outcome] = held - sold
                st.cash_usd += fill.usd - fill.fee_usd
        if filled:
            event = {
                "ts": now, "asset": st.asset, "window": st.start, "kind": decision.side,
                "outcome": decision.outcome, "shares": fill.shares, "price": fill.usd / fill.shares,
                "usd": fill.usd, "fee": fill.fee_usd, "fair": decision.fair_prob, "reason": decision.reason,
            }
            st.fills.append(event)
            self.events.append(event)
        logger.info(
            "[%s][%s] %s %.2f %s @<=%.3f ($%.2f + fee $%.2f) %s | %s",
            self.mode.upper(), st.asset, decision.side, fill.shares if filled else decision.shares, decision.outcome,
            decision.limit_price, fill.usd if filled else decision.usd, fill.fee_usd if filled else decision.fee_usd,
            "FILLED" if filled else "NOT FILLED", decision.reason,
        )
        # Journal USD includes fees (paid on a buy, deducted on a sell) so
        # the dashboard's P&L replay comes out net of fees.
        f = fill if filled else decision
        fee = f.fee_usd if decision.side == "BUY" else -f.fee_usd
        self._journal(st, decision.side, decision.outcome, decision.limit_price,
                      f.shares, f.usd + fee, filled, decision.reason)

    # -- market making ------------------------------------------------------
    def _make(self, st: WindowState, now: float, all_books: dict) -> None:
        mb = self.maker_broker
        tokens = {o: st.market.tokens[o] for o in (UP, DOWN)} if st.market else {}
        if not tokens:
            return
        seconds_left = st.end - now
        books = {o: all_books.get(tokens[o]) for o in (UP, DOWN)}

        # 0. Early warning: the leading venue is moving fast, so cancel before
        #    counting fills (the cancel went out when we saw the move).
        lead_pulled = self._lead_pulled(st.asset, now)
        if lead_pulled:
            mb.cancel_all(list(tokens.values()))

        # 1. Fills on the bids we already have out.
        resting = any(t in mb.orders for t in tokens.values())
        if resting and not mb.live and hasattr(self.gateway, "trades") and now - st.last_trades_fetch >= TRADES_POLL_S:
            st.last_trades_fetch = now
            try:
                st.trades = self.gateway.trades(st.market.condition_id)
            except Exception as exc:
                st.trades = []
                if now - st.last_status_log >= STATUS_LOG_EVERY_S:
                    logger.warning("[%s] trade feed unavailable (%s); paper fills from book crossings only", st.asset, _short(exc))
        for o in (UP, DOWN):
            for f in mb.fills(tokens[o], books[o], now, st.trades):
                self._apply_maker_fill(st, now, f, books)

        # 2. Where we want to be now.
        if not self._wants_books(st, now) or seconds_left < self.s.maker.stop_quoting_s:
            mb.cancel_all(list(tokens.values()))
            if 0 < seconds_left < self.s.maker.stop_quoting_s:
                st.why = f"quotes pulled for the last {self.s.maker.stop_quoting_s:.0f}s; holding to settlement"
            return
        latest = self.history.latest(st.asset)
        reason = ""
        if latest is None or now - latest[0] > self.s.feed.max_age_s:
            reason = "price feed stale"
        elif books[UP] is None or books[DOWN] is None:
            reason = "order book unavailable"
        else:
            allowed, why_not = self.risk.can_trade(now, st.asset)
            room = max(0.0, self.risk.room_usd(now, st.asset) - self._open_exposure()) if allowed else 0.0
            then = self.history.price_at(st.asset, latest[0] - self.s.maker.fast_move_window_s, 2.0)
            if not allowed:
                reason = why_not
            elif room < self.s.maker.quote_shares * 0.5:
                reason = f"no risk room (${room:.2f})"
            elif self.fast_guard.check(st.asset, now, latest[1], then, self.vol[st.asset].sigma):
                reason = "sharp move: quotes pulled for a moment"
            elif lead_pulled:
                reason = "early warning: Binance moving fast; bids pulled"
        if reason:
            mb.cancel_all(list(tokens.values()))
            st.why = reason
            return

        snap = self.snapshot(st, now, latest[1], books)
        m = self.s.model
        if m.twap_window_s > 0:
            pv = twap_prob_vol_1s(latest[1], st.strike, seconds_left, self.vol[st.asset].sigma, m.twap_window_s, snap.twap_so_far, m.basis_sd)
        else:
            pv = prob_vol_1s(latest[1], st.strike, seconds_left, self.vol[st.asset].sigma)
        targets, why = self.quoter.targets(books, st.pos, self.strategy.prob_up(snap), pv)
        for o in (UP, DOWN):
            mb.sync(tokens[o], o, targets[o], now)
        st.books = books
        st.why = why + (f" | holding Up {st.pos.shares[UP]:.0f} / Down {st.pos.shares[DOWN]:.0f}" if st.has_position else "")
        if now - st.last_status_log >= STATUS_LOG_EVERY_S:
            st.last_status_log = now
            logger.info("[%s] %3.0fs left | market P(up)=%.2f | %s", st.asset, seconds_left,
                        self.quoter.market_fair_up(books) or float("nan"), st.why)

    def _apply_maker_fill(self, st: WindowState, now: float, f, books: dict) -> None:
        usd = f.price * f.shares  # makers pay no fee
        st.pos.shares[f.outcome] += f.shares
        st.pos.cost_usd[f.outcome] += usd
        st.pos.entries += 1
        st.last_entry_ts = now
        st.cash_usd -= usd
        self.stats.orders += 1
        self.asset_stats[st.asset].orders += 1
        mid = books[f.outcome].mid if books.get(f.outcome) else None
        paired = min(st.pos.shares[UP], st.pos.shares[DOWN])
        reason = f"maker bid filled @ {f.price:.2f} (mid {mid:.2f})" if mid is not None else f"maker bid filled @ {f.price:.2f}"
        reason += f"; pairs {paired:.0f}, Up {st.pos.shares[UP]:.0f} / Down {st.pos.shares[DOWN]:.0f}"
        event = {
            "ts": now, "asset": st.asset, "window": st.start, "kind": "BUY", "outcome": f.outcome,
            "shares": f.shares, "price": f.price, "usd": usd, "fee": 0.0, "fair": mid if mid is not None else f.price,
            "reason": reason,
        }
        st.fills.append(event)
        self.events.append(event)
        logger.info("[%s][%s] MAKER BUY %.2f %s @ %.2f | %s", self.mode.upper(), st.asset, f.shares, f.outcome, f.price, reason)
        self._journal(st, "BUY", f.outcome, f.price, f.shares, usd, True, reason)

    # -- settlement -------------------------------------------------------
    def _settle(self, now: float) -> None:
        for key, st in list(self.windows.items()):
            if st.settled or now < st.end + 2:
                continue
            if not st.has_position:
                st.settled = True
                if st.pos.entries:  # traded, but fully exited before expiry
                    self._book(st, now, st.cash_usd, "exited early", None)
                else:
                    self.recent_windows.append({
                        "asset": st.asset, "start": st.start, "result": "skip" if st.skip_reason else "idle",
                        "pnl": 0.0, "note": st.skip_reason or st.why,
                    })
                continue
            if now - st.last_resolution_try < 10:
                continue
            st.last_resolution_try = now
            outcome = None
            try:
                outcome = self.gateway.resolution(st.market)
            except Exception as exc:
                logger.warning("[%s] resolution lookup failed for %s: %s", st.asset, st.market.slug, exc)
            source = "polymarket"
            if outcome is None and now - st.end >= self.settle_fallback_s:
                w = self.s.model.twap_window_s
                end_price = (self.history.twap(st.asset, st.end - w, st.end) if w > 0
                             else self.history.price_near(st.asset, st.end, STRIKE_TOLERANCE_S, STRIKE_LATE_S))
                if end_price is None:
                    continue
                outcome = UP if end_price >= st.strike else DOWN
                source = "own feed (Polymarket not resolved yet; verify later)"
            if outcome is None:
                continue

            payout = st.pos.shares[outcome]
            for o in (UP, DOWN):
                if st.pos.shares[o] > 0:
                    price = 1.0 if o == outcome else 0.0
                    self._journal(st, "SELL", o, price, st.pos.shares[o], st.pos.shares[o] * price, True,
                                  f"settled: {outcome} won (source: {source})")
            pnl = st.cash_usd + payout
            st.pos = WindowPosition(entries=st.pos.entries)
            st.settled = True
            self._book(st, now, pnl, f"{outcome} won via {source}", outcome)
        self._forget_old(now)

    def _book(self, st: WindowState, now: float, pnl: float, note: str, winner: str | None) -> None:
        self.risk.record_window(now, pnl, st.asset)
        for stats in (self.stats, self.asset_stats[st.asset]):
            stats.windows_traded += 1
            stats.pnl_usd += pnl
            stats.fees_usd += st.fees_usd
            if pnl > 0:
                stats.wins += 1
            elif pnl < 0:
                stats.losses += 1
        self.sizing.bankroll_usd = max(0.0, self.s.sizing.bankroll_usd + self.stats.pnl_usd)
        sides = sorted({f["outcome"] for f in st.fills if f["kind"] == "BUY"})
        result = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
        self.recent_windows.append({
            "asset": st.asset, "start": st.start, "result": result, "pnl": pnl,
            "sides": sides, "winner": winner, "note": note, "cum_pnl": self.stats.pnl_usd,
        })
        self.events.append({
            "ts": now, "asset": st.asset, "window": st.start, "kind": "SETTLE", "outcome": winner,
            "pnl": pnl, "reason": note,
        })
        logger.info(
            "[%s] window %d settled: %s | P&L $%+.2f | total $%+.2f over %d windows (%dW/%dL) | today $%+.2f",
            st.asset, st.start, note, pnl, self.stats.pnl_usd, self.stats.windows_traded,
            self.stats.wins, self.stats.losses, self.risk.realized_pnl_today,
        )

    def _forget_old(self, now: float) -> None:
        cutoff = now - 3600
        for key in [k for k, w in self.windows.items() if w.settled and w.end < cutoff]:
            del self.windows[key]

    def _journal(self, st: WindowState, side: str, outcome: str, price: float, shares: float, usd: float, filled: bool, reason: str) -> None:
        if self.journal is None or st.market is None:
            return
        self.journal.record(
            Signal(
                strategy=STRATEGY_NAME,
                market_id=st.market.condition_id,
                token_id=st.market.tokens[outcome],
                outcome=outcome,
                side=side,
                limit_price=price,
                size_shares=shares,
                size_usd=usd,
                reason=reason,
                group_id=st.market.slug,
            ),
            mode=self.mode,
            filled=filled,
        )


def _short(exc: Exception) -> str:
    """First line of an exception, without urllib3's nested retry noise."""
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return f"{type(exc).__name__}: {text[:160]}"


def _fmt(x: float | None) -> str:
    return f"{x:.2f}" if x is not None else "--"
