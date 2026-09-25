import pytest

from updown.broker import PaperBroker
from updown.config import FeedConfig, UpDownSettings, load_updown_settings
from updown.engine import UpDownEngine
from updown.feeds import BinanceFeed, HyperliquidFeed, PriceHistory, build_feeds
from updown.sim import MultiSimGateway, step_multi
from updown.markets import window_start

ALL = ["btc", "eth", "sol", "xrp", "bnb", "doge", "hype"]


def test_default_config_trades_all_seven_coins():
    s = load_updown_settings("config/updown.yaml")
    assert s.assets == ALL


def test_unknown_or_duplicate_coin_is_rejected(tmp_path):
    for assets in ("[btc, pepe]", "[btc, btc]"):
        p = tmp_path / "c.yaml"
        p.write_text(f"assets: {assets}\n")
        with pytest.raises(ValueError):
            load_updown_settings(str(p))


def test_feeds_route_hype_to_hyperliquid():
    feeds = build_feeds(ALL, FeedConfig(), PriceHistory())
    assert isinstance(feeds["hype"], HyperliquidFeed)
    assert all(isinstance(feeds[a], BinanceFeed) for a in ALL if a != "hype")
    assert len({id(f) for f in feeds.values()}) == 2  # one poller per venue


class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_hyperliquid_parsing():
    f = HyperliquidFeed(["hype"], FeedConfig(), PriceHistory())
    f._session.post = lambda url, json, timeout: _Resp({"HYPE": "41.5", "BTC": "1"} if json["type"] == "allMids" else [{"c": "40"}, {"c": "41"}, {"c": "40.5"}])
    assert f._fetch() == {"hype": 41.5}
    assert f.seed_sigma("hype") > 0


class CountingSim(MultiSimGateway):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.batch_calls = 0
        self.single_calls = 0

    def books(self, token_ids):
        self.batch_calls += 1
        return {t: MultiSimGateway.book(self, t) for t in token_ids}

    def book(self, token_id):
        self.single_calls += 1
        return super().book(token_id)


def test_seven_coins_trade_independently_with_one_book_request_per_tick():
    s = UpDownSettings(wallet=None, assets=ALL, mode="taker")
    sim = CountingSim(ALL, seed=3, lag_s=2.0)
    history = PriceHistory(maxlen=4000)
    engine = UpDownEngine(s, sim, PaperBroker(), history, journal=None)
    for a in ALL:
        engine.vol[a].seed(sim.worlds[a].sigma)
    t0 = window_start(1_800_000_000, 300)
    ticks_in_zone = 0
    for t in range(t0 - 65, t0 + 3 * 300 + 30):
        step_multi(engine, sim, history, t)
        if 60 <= (t - t0) % 300 < 300 and t >= t0:
            ticks_in_zone += 1
    # books come in one batched request per tick, never 14 single ones
    assert sim.single_calls == 0
    assert 0 < sim.batch_calls <= ticks_in_zone + 5
    traded = [a for a, st in engine.asset_stats.items() if st.windows_traded]
    assert len(traded) >= 5
    assert engine.stats.windows_traded == sum(st.windows_traded for st in engine.asset_stats.values())
    assert engine.stats.pnl_usd == pytest.approx(sum(st.pnl_usd for st in engine.asset_stats.values()))
