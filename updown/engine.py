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
from dataclasses import dataclass, field

from bot.journal import TradeJournal
from bot.strategies.base import Signal
from updown.config import UpDownSettings
from updown.feeds import PriceHistory
from updown.markets import WindowMarket, window_start
from updown.model import VolEstimator
from updown.risk import UpDownRisk
from updown.strategy import DOWN, UP, Snapshot, UpDownStrategy, WindowPosition

logger = logging.getLogger("polybot.updown.engine")

STRATEGY_NAME = "updown_5m"
DISCOVERY_RETRY_S = 5.0
STRIKE_TOLERANCE_S = 2.0
STATUS_LOG_EVERY_S = 30.0


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
    def __init__(self, settings: UpDownSettings, gateway, broker, history: PriceHistory, journal: TradeJournal | None, settle_fallback_s: float = 600.0):
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

    # -- inputs ---------------------------------------------------------
    def on_price(self, asset: str, ts: float, price: float) -> None:
        self.vol[asset].update(ts, price)

    # -- main tick -------------------------------------------------------
    def tick(self, now: float) -> None:
        for asset in self.s.assets:
            start = window_start(now, self.s.window_seconds)
            key = (asset, start)
            if key not in self.windows:
                self.windows[key] = WindowState(asset, start, start + self.s.window_seconds)
            state = self.windows[key]
            try:
                self._prepare(state, now)
                self._trade(state, now)
            except Exception:
                logger.exception("[%s] error while trading window %d", asset, start)
        self._settle(now)

    def _prepare(self, st: WindowState, now: float) -> None:
        if st.strike is None and not st.skip_reason:
            # Strike comes from OUR feed at the open, not Polymarket's
            # "price to beat": settlement compares oracle-end vs oracle-start,
            # so comparing feed-now vs feed-start cancels the constant gap
            # between the two sources. Mixing sources would bake it in.
            st.strike = self.history.price_at(st.asset, st.start, STRIKE_TOLERANCE_S)
            if st.strike is None:
                st.skip_reason = "no feed price at window open (bot started mid-window?)"
                logger.info("[%s] skipping window %d: %s", st.asset, st.start, st.skip_reason)
        if st.market is None and not st.skip_reason and now - st.last_discovery_try >= DISCOVERY_RETRY_S:
            st.last_discovery_try = now
            st.market = self.gateway.discover(st.asset, st.start)
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

    def _open_exposure(self) -> float:
        return sum(w.pos.total_cost_usd for w in self.windows.values() if not w.settled and w.has_position)

    def _trade(self, st: WindowState, now: float) -> None:
        if st.market is None or st.strike is None:
            return
        seconds_left = st.end - now
        if seconds_left <= 0:
            return
        if seconds_left > self.s.strategy.max_seconds_left and not st.has_position:
            return  # nothing to do yet; don't spend API calls
        latest = self.history.latest(st.asset)
        if latest is None or now - latest[0] > self.s.feed.max_age_s:
            if now - st.last_status_log >= STATUS_LOG_EVERY_S:
                st.last_status_log = now
                logger.warning("[%s] price feed stale; not trading", st.asset)
            return

        allowed, why_not = self.risk.can_trade(now)
        room = max(0.0, self.risk.room_usd(now) - self._open_exposure()) if allowed else 0.0

        books = {o: self.gateway.book(st.market.tokens[o]) for o in (UP, DOWN)}
        snap = Snapshot(spot=latest[1], strike=st.strike, seconds_left=seconds_left, sigma=self.vol[st.asset].sigma, books=books)
        decision, why = self.strategy.decide(snap, st.pos, room)

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
            st.fees_usd += fill.fee_usd
            if decision.side == "BUY":
                st.pos.shares[decision.outcome] += fill.shares
                st.pos.cost_usd[decision.outcome] += fill.usd + fill.fee_usd
                st.pos.entries += 1
                st.cash_usd -= fill.usd + fill.fee_usd
            else:
                held = st.pos.shares[decision.outcome]
                sold = min(fill.shares, held)
                st.pos.cost_usd[decision.outcome] *= (held - sold) / held if held else 0.0
                st.pos.shares[decision.outcome] = held - sold
                st.cash_usd += fill.usd - fill.fee_usd
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

    # -- settlement -------------------------------------------------------
    def _settle(self, now: float) -> None:
        for key, st in list(self.windows.items()):
            if st.settled or now < st.end + 2:
                continue
            if not st.has_position:
                st.settled = True
                if st.pos.entries:  # traded, but fully exited before expiry
                    self._book(st, now, st.cash_usd, "exited early")
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
                end_price = self.history.price_at(st.asset, st.end, STRIKE_TOLERANCE_S)
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
            self._book(st, now, pnl, f"{outcome} won via {source}")
        self._forget_old(now)

    def _book(self, st: WindowState, now: float, pnl: float, note: str) -> None:
        self.risk.record_window(now, pnl)
        self.stats.windows_traded += 1
        self.stats.pnl_usd += pnl
        self.stats.fees_usd += st.fees_usd
        if pnl > 0:
            self.stats.wins += 1
        elif pnl < 0:
            self.stats.losses += 1
        self.sizing.bankroll_usd = max(0.0, self.s.sizing.bankroll_usd + self.stats.pnl_usd)
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


def _fmt(x: float | None) -> str:
    return f"{x:.2f}" if x is not None else "--"
