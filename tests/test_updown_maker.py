import pytest

from updown.broker import PaperBroker
from updown.config import MakerConfig, SizingConfig, UpDownSettings
from updown.engine import UpDownEngine
from updown.feeds import PriceHistory
from updown.maker import FastMoveGuard, MakerQuoter, PaperMakerBroker, Quote
from updown.markets import WindowMarket
from updown.strategy import DOWN, UP, Book, WindowPosition

T0 = 1_800_000_000


def books(up=(0.49, 0.51), down=(0.49, 0.51), size=100.0):
    return {UP: Book.from_raw([(up[0], size)], [(up[1], size)]), DOWN: Book.from_raw([(down[0], size)], [(down[1], size)])}


def quoter(**kw):
    return MakerQuoter(MakerConfig(**kw))


# -- quoting ---------------------------------------------------------------
def test_flat_book_quotes_both_sides_with_a_profitable_pair():
    q, why = quoter(half_spread=0.03).targets(books(), WindowPosition(), 0.5)
    assert q[UP].price == pytest.approx(0.47) and q[DOWN].price == pytest.approx(0.47)
    assert q[UP].price + q[DOWN].price <= 1 - 2 * 0.03 + 1e-9
    assert "pair 0.94" in why


def test_quotes_never_cross_and_never_overpay_for_top_of_book():
    # Wide book: best bid 0.30 -> no need to bid more than 0.31 to be first in line.
    q, _ = quoter().targets(books(up=(0.30, 0.70), down=(0.30, 0.70)), WindowPosition(), 0.5)
    assert q[UP].price == pytest.approx(0.31)
    # Tight book: bid must stay a tick under the ask (post-only).
    q, _ = quoter(half_spread=0.0).targets(books(up=(0.49, 0.50), down=(0.49, 0.50)), WindowPosition(), 0.5)
    assert q[UP].price <= 0.49 + 1e-9


def test_leans_toward_completing_pairs_and_caps_the_pair_price():
    pos = WindowPosition()
    pos.shares[UP], pos.cost_usd[UP] = 20, 20 * 0.47  # long 20 Up at 0.47
    q, _ = quoter(half_spread=0.03, skew_per_share=0.002, pair_margin=0.02).targets(books(), pos, 0.5)
    flat, _ = quoter(half_spread=0.03).targets(books(), WindowPosition(), 0.5)
    assert q[UP] is None or q[UP].price < flat[UP].price  # less keen on more Up
    assert q[DOWN].price > flat[DOWN].price or q[DOWN].price == pytest.approx(0.50)  # keener on Down (capped at top of book)
    assert 0.47 + q[DOWN].price <= 1 - 0.02 + 1e-9  # completed pair still profitable


def test_inventory_limits_stop_the_heavy_side():
    pos = WindowPosition()
    pos.shares[UP], pos.cost_usd[UP] = 20, 9.4
    q, why = quoter(max_imbalance_shares=20).targets(books(), pos, 0.5)
    assert q[UP] is None and q[DOWN] is not None and "inventory" in why


def test_stays_out_when_model_and_market_disagree():
    q, why = quoter(max_model_gap=0.25).targets(books(), WindowPosition(), 0.9)
    assert q[UP] is None and q[DOWN] is None and "disagree" in why


# -- paper broker ------------------------------------------------------------
def test_paper_fill_only_when_the_ask_reaches_our_bid():
    b = PaperMakerBroker(MakerConfig())
    b.sync("up", UP, Quote(0.47, 10), now=100)
    assert b.fills("up", books()[UP], now=101) == []  # ask 0.51: no one sold to us
    fills = b.fills("up", Book.from_raw([(0.44, 50)], [(0.46, 4), (0.47, 3), (0.50, 50)]), now=102)
    assert len(fills) == 1 and fills[0].shares == 7 and fills[0].price == 0.47  # we get our price
    fills = b.fills("up", Book.from_raw([(0.44, 50)], [(0.45, 50)]), now=103)
    assert fills[0].shares == 3 and "up" not in b.orders  # rest filled, order gone


def test_no_fill_on_the_tick_the_order_was_placed_and_requotes_are_throttled():
    b = PaperMakerBroker(MakerConfig(requote_s=3))
    b.sync("up", UP, Quote(0.47, 10), now=100)
    assert b.fills("up", Book.from_raw([], [(0.40, 50)]), now=100) == []
    b.sync("up", UP, Quote(0.49, 10), now=101)  # raising the bid: too soon
    assert b.orders["up"].price == 0.47
    b.sync("up", UP, Quote(0.45, 10), now=101.5)  # backing off: immediate
    assert b.orders["up"].price == 0.45
    b.sync("up", UP, Quote(0.47, 10), now=102)  # raising again: throttled
    assert b.orders["up"].price == 0.45
    b.sync("up", UP, Quote(0.47, 10), now=105)
    assert b.orders["up"].price == 0.47
    b.sync("up", UP, None, now=104.5)  # cancels are never throttled
    assert "up" not in b.orders


def test_fast_move_guard():
    g = FastMoveGuard(MakerConfig(fast_move_z=3, fast_move_window_s=5, fast_move_cooldown_s=15))
    assert not g.check("btc", 100, 100.0, 100.0, 1e-4)
    assert g.check("btc", 101, 100.2, 100.0, 1e-4)  # 0.2% in 5s at 1e-4 vol = ~9 sigma
    assert g.check("btc", 110, 100.2, 100.2, 1e-4)  # still cooling down
    assert not g.check("btc", 117, 100.2, 100.2, 1e-4)


# -- engine, end to end ------------------------------------------------------
class ScriptedGateway:
    """Flat 0.49/0.51 books; at 60s into the window the Up ask dips to 0.45,
    at 120s the Down ask dips to 0.45: both our bids get hit once."""

    def __init__(self, winner):
        self.winner = winner
        self.now = 0

    def discover(self, asset, start):
        return WindowMarket(asset, start, start + 300, f"{asset}-updown-5m-{start}", f"c{start}", {UP: f"up-{start}", DOWN: f"down-{start}"})

    def books(self, tokens):
        el = (self.now - T0) % 300
        out = {}
        for t in tokens:
            dip = (t.startswith("up") and 60 <= el < 62) or (t.startswith("down") and 120 <= el < 122)
            out[t] = Book.from_raw([(0.44 if dip else 0.49, 100)], [(0.45 if dip else 0.51, 100)])
        return out

    def book(self, t):
        return self.books([t])[t]

    def resolution(self, wm):
        return self.winner


def run_maker(winner):
    s = UpDownSettings(wallet=None, assets=["btc"], mode="maker")
    s.sizing = SizingConfig(bankroll_usd=1000)
    s.maker = MakerConfig(half_spread=0.03, quote_shares=10, fast_move_z=99, vol_spread_mult=0)
    gw = ScriptedGateway(winner)
    history = PriceHistory()
    engine = UpDownEngine(s, gw, PaperBroker(), history, journal=None, settle_fallback_s=30)
    engine.vol["btc"].seed(1e-4)
    for t in range(T0 - 65, T0 + 300 + 20):
        gw.now = t
        history.add("btc", float(t), 100.0)
        engine.tick(float(t))
    return engine


@pytest.mark.parametrize("winner", [UP, DOWN])
def test_completed_pair_profits_whoever_wins(winner):
    engine = run_maker(winner)
    buys = [e for e in engine.events if e["kind"] == "BUY"]
    assert {e["outcome"] for e in buys} == {UP, DOWN}
    first, second = buys
    assert first["price"] == pytest.approx(0.47)
    # Holding Up, the quoter leaned in on Down to complete the pair...
    assert second["price"] > 0.47
    pair_cost = first["price"] + second["price"]
    # ...but never paid so much that the pair stops being profitable.
    assert pair_cost <= 1 - engine.s.maker.pair_margin + 1e-9
    assert engine.stats.windows_traded == 1
    # 10 complete pairs: $10 payout whoever wins, no fees for makers
    assert engine.stats.pnl_usd == pytest.approx(10 * (1 - pair_cost), abs=1e-6) and engine.stats.pnl_usd > 0
    assert engine.stats.fees_usd == 0


def test_quotes_are_pulled_before_the_close():
    engine = run_maker(UP)
    assert not [t for t in engine.maker_broker.orders if t.endswith(str(T0))]  # nothing left resting in that window
    last_fill = max(e["ts"] for e in engine.events if e["kind"] == "BUY")
    assert last_fill < T0 + 300 - engine.s.maker.stop_quoting_s


def test_paper_fill_from_a_taker_selling_into_our_bid():
    from updown.markets import TradePrint, parse_trades

    rows = [
        {"asset": "up", "side": "SELL", "price": "0.47", "size": "4", "timestamp": 1_000_000_105, "transactionHash": "0xa"},
        {"asset": "up", "side": "SELL", "price": "0.49", "size": "50", "timestamp": 1_000_000_105, "transactionHash": "0xb"},  # above our bid
        {"asset": "up", "side": "BUY", "price": "0.51", "size": "50", "timestamp": 1_000_000_105, "transactionHash": "0xc"},  # a buyer, not a seller
        {"asset": "down", "side": "SELL", "price": "0.40", "size": "50", "timestamp": 1_000_000_105, "transactionHash": "0xd"},  # other token
        {"asset": "up", "side": "SELL", "price": "0.40", "size": "50", "timestamp": 1_000_000_099, "transactionHash": "0xe"},  # before we bid
    ]
    trades = parse_trades(rows)
    assert all(isinstance(t, TradePrint) for t in trades) and len(trades) == 5
    b = PaperMakerBroker(MakerConfig())
    b.sync("up", UP, Quote(0.47, 10), now=1_000_000_100)
    fills = b.fills("up", books()[UP], now=1_000_000_106, trades=trades)
    assert len(fills) == 1 and fills[0].shares == 4
    # the same prints are never counted twice
    assert b.fills("up", books()[UP], now=1_000_000_107, trades=trades) == []


def test_spread_widens_when_the_probability_moves_fast():
    from updown.model import prob_vol_1s

    near_open_atm = prob_vol_1s(100.0, 100.0, 240, 1e-4)
    near_close_atm = prob_vol_1s(100.0, 100.0, 70, 1e-4)
    far_from_strike = prob_vol_1s(100.5, 100.0, 240, 1e-4)
    assert near_close_atm > near_open_atm > far_from_strike
    assert near_open_atm == pytest.approx(0.3989 / 240 ** 0.5, rel=1e-3)  # ~2.6c per second

    q = quoter(half_spread=0.03, vol_spread_mult=1.5, reaction_s=2.0)
    assert q.half_spread(None) == 0.03
    assert q.half_spread(0.001) == 0.03  # calm: floor applies
    assert q.half_spread(near_open_atm) == pytest.approx(1.5 * near_open_atm * 2 ** 0.5)
    wide, _ = q.targets(books(), WindowPosition(), 0.5, near_open_atm)
    narrow, _ = q.targets(books(), WindowPosition(), 0.5, 0.001)
    assert wide[UP].price < narrow[UP].price


def test_report_splits_maker_pnl_into_pairs_and_unpaired(tmp_path, capsys, monkeypatch):
    import scripts.updown_report as report
    from bot.journal import TradeJournal

    s = UpDownSettings(wallet=None, assets=["btc"], mode="maker")
    s.maker = MakerConfig(half_spread=0.03, quote_shares=10, fast_move_z=99, vol_spread_mult=0)
    gw = ScriptedGateway(UP)
    history = PriceHistory()
    engine = UpDownEngine(s, gw, PaperBroker(), history, TradeJournal(str(tmp_path / "t.csv")), settle_fallback_s=30)
    engine.vol["btc"].seed(1e-4)
    for t in range(T0 - 65, T0 + 300 + 20):
        gw.now = t
        history.add("btc", float(t), 100.0)
        engine.tick(float(t))
    monkeypatch.setattr(report, "TRADES", str(tmp_path / "t.csv"))
    monkeypatch.setattr(report, "LOG", str(tmp_path / "none.log"))
    monkeypatch.setattr("sys.argv", ["updown_report.py"])
    report.main()
    out = capsys.readouterr().out
    pairs_line = [l for l in out.splitlines() if "complete pairs" in l][0]
    assert "10 shares" in pairs_line
    assert float(pairs_line.split("P&L")[1].split()[0]) == pytest.approx(engine.stats.pnl_usd, abs=0.01)
