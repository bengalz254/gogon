import csv

import pytest

from bot.journal import TradeJournal
from scripts.dashboard import build_summary
from updown.broker import PaperBroker
from updown.config import FeedConfig, RiskLimitsConfig, SizingConfig, UpDownSettings
from updown.engine import UpDownEngine
from updown.feeds import PriceHistory
from updown.markets import WindowMarket
from updown.risk import UpDownRisk
from updown.strategy import DOWN, UP, Book

T0 = 1_800_000_000  # a window boundary (divisible by 300)


class FakeGateway:
    """Fair 50/50 book for the first half of each window, then a stale
    Up 0.60/0.62 quote while the price has moved. Resolves to `winner`."""

    def __init__(self, winner=UP, resolved=True):
        self.winner = winner
        self.resolved = resolved
        self.book_calls = 0
        self.now = 0

    def discover(self, asset, start):
        return WindowMarket(asset, start, start + 300, f"{asset}-updown-5m-{start}", f"cond-{start}",
                            {UP: f"up-{start}", DOWN: f"down-{start}"}, price_to_beat=None)

    def book(self, token_id):
        self.book_calls += 1
        if (self.now - T0) % 300 < 150:
            return Book.from_raw([(0.49, 100)], [(0.51, 100)])
        if token_id.startswith("up"):
            return Book.from_raw([(0.60, 100)], [(0.62, 100)])
        return Book.from_raw([(0.38, 100)], [(0.40, 100)])

    def resolution(self, wm):
        return self.winner if self.resolved else None


def settings(**sizing):
    s = UpDownSettings(wallet=None, assets=["btc"])
    s.model.vol_multiplier = 1.0
    s.model.basis_sd = 0.0
    s.sizing = SizingConfig(bankroll_usd=100, kelly_fraction=0.25, max_bet_usd=5, max_window_exposure_usd=10, min_order_usd=1, **sizing)
    s.feed = FeedConfig(max_age_s=3)
    return s


def run(engine, history, prices, t_from, t_to):
    """Feed a constant-ish price path and tick every second."""
    for t in range(t_from, t_to):
        engine.gateway.now = t
        p = prices(t)
        history.add("btc", float(t), p)
        engine.on_price("btc", float(t), p)
        engine.tick(float(t))


def make(gateway, tmp_path=None, **sizing):
    history = PriceHistory()
    journal = TradeJournal(str(tmp_path / "trades.csv")) if tmp_path else None
    engine = UpDownEngine(settings(**sizing), gateway, PaperBroker(), history, journal, settle_fallback_s=30)
    engine.vol["btc"].seed(1e-4)
    return engine, history


def price_path(t):
    # Flat at 100 until 150s into the window, then +0.1%: Up becomes ~90% likely.
    return 100.0 if (t - T0) % 300 < 150 else 100.1


def test_skips_window_it_joined_midway_and_trades_the_next(tmp_path):
    gw = FakeGateway()
    engine, history = make(gw)
    run(engine, history, price_path, T0 + 100, T0 + 300 + 200)
    first = engine.windows[("btc", T0)]
    second = engine.windows[("btc", T0 + 300)]
    assert first.skip_reason and first.pos.entries == 0
    assert second.strike == 100.0 and second.pos.entries >= 1


def test_winning_window_pnl_and_journal_match_dashboard(tmp_path):
    engine, history = make(FakeGateway(winner=UP), tmp_path)
    run(engine, history, price_path, T0 - 5, T0 + 300 + 20)
    st = engine.stats
    assert st.windows_traded == 1 and st.wins == 1
    w = engine.windows[("btc", T0)]
    assert w.settled
    # P&L = shares paid out at $1 minus everything spent (incl. fees)
    assert st.pnl_usd > 0
    with open(tmp_path / "trades.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert {r["strategy"] for r in rows} == {"updown_5m"}
    summary = build_summary(rows)
    assert summary["realized_pnl_usd"] == pytest.approx(st.pnl_usd, abs=0.01)
    assert summary["open_positions_count"] == 0


def test_losing_window_and_daily_loss_stop(tmp_path):
    gw = FakeGateway(winner=DOWN)
    engine, history = make(gw)
    engine.risk = UpDownRisk(RiskLimitsConfig(max_daily_loss_usd=6, max_consecutive_losses=99))
    run(engine, history, price_path, T0 - 5, T0 + 3 * 300 + 20)
    assert engine.stats.losses >= 1
    assert engine.stats.pnl_usd < 0
    # Never lose more than the daily limit plus one window's max exposure.
    assert engine.stats.pnl_usd >= -(6 + 10)
    ok, why = engine.risk.can_trade(float(T0 + 3 * 300 + 20))
    if engine.risk.realized_pnl_today <= -6:
        assert not ok and "daily loss" in why


def test_settles_from_own_feed_when_polymarket_is_slow():
    engine, history = make(FakeGateway(resolved=False))
    run(engine, history, price_path, T0 - 5, T0 + 300 + 10)
    assert engine.stats.windows_traded == 0  # still waiting for Polymarket
    run(engine, history, price_path, T0 + 300 + 10, T0 + 300 + 45)
    assert engine.stats.windows_traded == 1 and engine.stats.wins == 1


def test_stale_feed_blocks_trading():
    gw = FakeGateway()
    engine, history = make(gw)
    history.add("btc", float(T0 - 1), 100.0)
    for t in range(T0, T0 + 300):
        gw.now = t
        engine.tick(float(t))  # no new prices arrive
    assert engine.windows[("btc", T0)].pos.entries == 0


def test_does_not_poll_books_before_trading_slice():
    gw = FakeGateway()
    engine, history = make(gw)
    run(engine, history, lambda t: 100.0, T0 - 5, T0 + 55)
    assert gw.book_calls == 0


def test_strike_survives_a_feed_hiccup_at_the_open():
    engine, history = make(FakeGateway())
    # last tick 10s before the open, then nothing until 3s after it
    history.add("btc", float(T0 - 10), 99.0)
    history.add("btc", float(T0 + 3), 100.0)
    engine.tick(float(T0 + 3))
    assert engine.windows[("btc", T0)].strike == 100.0

    engine2, history2 = make(FakeGateway())
    history2.add("btc", float(T0 - 10), 99.0)
    history2.add("btc", float(T0 + 6), 100.0)  # too late to trust as the open
    engine2.tick(float(T0 + 6))
    assert engine2.windows[("btc", T0)].skip_reason


def test_entries_in_a_window_are_spaced_out():
    engine, history = make(FakeGateway())
    run(engine, history, price_path, T0 - 5, T0 + 300 + 20)
    buys = [e["ts"] for e in engine.events if e["kind"] == "BUY" and e["window"] == T0]
    assert len(buys) == 2
    assert buys[1] - buys[0] >= engine.s.strategy.min_seconds_between_entries


def test_market_lookup_errors_do_not_stop_the_bot():
    class Flaky(FakeGateway):
        calls = 0

        def discover(self, asset, start):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("gamma-api.polymarket.com timed out")
            return super().discover(asset, start)

    engine, history = make(Flaky())
    run(engine, history, price_path, T0 - 5, T0 + 300 + 20)
    assert engine.windows[("btc", T0)].market is not None
    assert engine.stats.windows_traded == 1
