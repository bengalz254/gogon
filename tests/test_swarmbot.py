from datetime import datetime, timedelta, timezone

import pytest

from swarmbot import config as config_mod
from swarmbot.config import Config
from swarmbot.engine import Engine
from swarmbot.moods import classify, parse_token
from swarmbot.paper import Paper

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def raw(mint="M1", p5=0.0, p1=0.0, p24=0.0, traders=10, net=2, buys=20, sells=20,
        sus=False, liq=50_000, mcap=200_000, price=1.0, pool_age_h=48, grad_age_h=None):
    r = {
        "id": mint, "symbol": mint, "name": mint, "usdPrice": price, "liquidity": liq, "mcap": mcap,
        "stats5m": {"priceChange": p5},
        "stats1h": {"priceChange": p1, "numTraders": traders, "numNetBuyers": net,
                    "numBuys": buys, "numSells": sells},
        "stats24h": {"priceChange": p24},
        "audit": {"isSus": sus},
        "firstPool": {"createdAt": (NOW - timedelta(hours=pool_age_h)).isoformat().replace("+00:00", "Z")},
    }
    if grad_age_h is not None:
        r["graduatedAt"] = (NOW - timedelta(hours=grad_age_h)).isoformat()
    return r


def mood(**kw):
    return classify(parse_token(raw(**kw)), now=NOW)


def test_moods_follow_dotswarm_order():
    assert mood(p5=35, p1=50) == "shocked"
    assert mood(p5=10, p1=25) == "happy"
    assert mood(net=6, traders=10) == "focused"
    assert mood(grad_age_h=3) == "graduated"
    assert mood(pool_age_h=0.5) == "newborn"
    assert mood(p5=1, p1=-3) == "calm"
    assert mood(sus=True) == "suspicious"
    assert mood(p1=-25) == "stressed"
    assert mood(p5=0, p1=0, traders=0, net=0, buys=0, sells=0) == "asleep"
    assert mood(p5=12, p1=15) == "other"


def test_shocked_wins_over_suspicious():
    assert mood(p5=40, sus=True) == "shocked"


class FakeJup:
    def __init__(self, lists, prices=None):
        self.lists = lists
        self.live = prices or {}

    def token_list(self, name, limit=100):
        return self.lists.get(name, [])

    def tokens(self, mints):
        return [raw(mint=m, price=self.live[m]) for m in mints if m in self.live]

    def prices(self, mints):
        return {m: self.live[m] for m in mints if m in self.live}


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def make(tmp_path, rows, **cfg_kw):
    base = {"take_profit_pct": 15, "stop_loss_pct": 10, "stale_minutes": 0, "max_hold_minutes": 0,
            "buy_moods": ["shocked", "happy", "calm"], "min_liquidity_usd": 1000, "min_mcap_usd": 10_000,
            "max_mcap_usd": 20_000_000, "skip_suspicious": True,
            "starting_cash_usd": 100, "max_open_positions": 5, "max_buys_per_hour": 6, "position_usd": 10}
    cfg = Config(data_dir=tmp_path, jupiter_lists=["a"], **{**base, **cfg_kw})
    jup = FakeJup({"a": rows})
    clock = Clock()
    eng = Engine(cfg, jupiter=jup, paper=Paper(tmp_path, cfg.starting_cash_usd, cfg.fee_pct), clock=clock)
    return eng, jup, clock


def test_buys_only_wanted_moods_and_filters(tmp_path):
    rows = [
        raw("SHOCK", p5=40), raw("HAPPY", p1=25), raw("CALM", p5=1, p1=2),
        raw("FOCUS", net=8), raw("TINY", p5=40, liq=500), raw("SUS", p1=30, sus=True),
        raw("BIG", p5=1, p1=0.3, mcap=200_000_000),
    ]
    eng, _, _ = make(tmp_path, rows)
    eng.try_buys(eng.scan())
    assert set(eng.paper.positions) == {"SHOCK", "HAPPY", "CALM"}
    assert eng.paper.cash == pytest.approx(70)


def test_take_profit_and_stop_loss(tmp_path):
    eng, jup, clock = make(tmp_path, [raw("A", p5=40, price=1.0), raw("B", p1=25, price=2.0)])
    eng.try_buys(eng.scan())
    jup.live = {"A": 1.14, "B": 1.81}
    eng.refresh_positions()
    assert set(eng.paper.positions) == {"A", "B"}  # +14% and -9.5%: hold
    jup.live = {"A": 1.15, "B": 1.79}
    eng.refresh_positions()
    assert eng.paper.positions == {}
    lines = (tmp_path / "trades.csv").read_text().splitlines()
    assert any(",TP" in line for line in lines[1:]) and any(",SL" in line for line in lines[1:])
    # fees: 1% in and 1% out
    assert eng.paper.cash == pytest.approx(80 + 10 * 0.99 * 1.15 * 0.99 + 10 * 0.99 * 0.895 * 0.99)


def test_cooldown_and_limits(tmp_path):
    rows = [raw(f"T{i}", p5=40) for i in range(10)]
    eng, jup, clock = make(tmp_path, rows, max_open_positions=3)
    eng.try_buys(eng.scan())
    assert len(eng.paper.positions) == 3
    jup.live = {"T0": 2.0}
    eng.refresh_positions()
    assert "T0" not in eng.paper.positions
    eng.try_buys(eng.scan())
    assert "T0" not in eng.paper.positions  # cooldown
    assert len(eng.paper.positions) == 3


def test_state_survives_restart(tmp_path):
    eng, _, _ = make(tmp_path, [raw("A", p5=40)])
    eng.try_buys(eng.scan())
    again = Paper(tmp_path, 100, 1.0)
    assert set(again.positions) == {"A"} and again.cash == pytest.approx(90)


def test_config_file_loads_and_refuses_live(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = config_mod.load(config_mod.Path(__file__).resolve().parents[1] / "config/swarmbot.yaml")
    assert cfg.buy_moods == ["shocked", "happy"]
    assert (cfg.take_profit_pct, cfg.stop_loss_pct) == (12, 7)
    assert (cfg.currency, cfg.starting_cash_usd, cfg.position_usd) == ("SOL", 10, 0.1)
    assert cfg.max_open_positions == 25 and cfg.stale_minutes == 5
    bad = tmp_path / "live.yaml"
    bad.write_text("mode: live\n")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(bad)


def test_real_jupiter_answer_shape(tmp_path):
    # Trimmed copy of a real lite-api.jup.ag/tokens/v2/toptrending/1h entry (priceChange is in percent).
    sample = {
        "id": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", "symbol": "cbBTC", "name": "Coinbase Wrapped BTC",
        "mcap": 202575437.5, "usdPrice": 64139.0, "liquidity": 21492243.3,
        "stats5m": {"priceChange": -0.166, "numBuys": 127, "numSells": 180, "numTraders": 46, "numNetBuyers": 3},
        "stats1h": {"priceChange": 0.304, "numBuys": 2586, "numSells": 2279, "numTraders": 258, "numNetBuyers": 43},
        "stats24h": {"priceChange": 0.34},
        "firstPool": {"id": "x", "createdAt": "2024-11-07T14:34:00Z"},
        "audit": {"topHoldersPercentage": 35.5, "devMints": 1},
    }
    t = parse_token(sample)
    assert t.trades_1h == 4865 and t.traders_1h == 258
    assert classify(t, now=NOW) == "calm"
    eng = Engine(Config(data_dir=tmp_path, buy_moods=["calm"], max_mcap_usd=20_000_000), jupiter=FakeJup({}))
    assert "besar" in eng.eligible(t, "calm")


def test_status_file_and_dashboard_api(tmp_path):
    import json
    import threading
    import urllib.request

    from swarmbot.dashboard import make_server

    eng, jup, _ = make(tmp_path, [raw("A", p5=40), raw("B", p1=25, price=2.0), raw("C", p5=1)])
    labelled = eng.scan()
    eng.try_buys(labelled)
    eng.remember_candidates(labelled)
    jup.live = {"A": 1.2}
    eng.refresh_positions()
    eng.write_status()
    status = json.loads((tmp_path / "status.json").read_text())
    assert {p["symbol"] for p in status["positions"]} == {"B", "C"}
    assert status["mood_counts"]["shocked"] == 1

    server = make_server(tmp_path, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert b"swarmbot" in urllib.request.urlopen(base + "/").read()
        data = json.loads(urllib.request.urlopen(base + "/api/data").read())
        assert data["trades"]["closed"] == 1 and data["trades"]["tp"] == 1
        assert data["trades"]["by_mood"]["shocked"]["wins"] == 1
    finally:
        server.shutdown()


def test_raising_starting_cash_tops_up(tmp_path):
    p = Paper(tmp_path, 100, 1.0)
    p.buy("A", "A", "calm", 1.0, 10)
    p2 = Paper(tmp_path, 200, 1.0)
    assert p2.cash == pytest.approx(190) and p2.starting_cash == 200
    p2.save()
    assert Paper(tmp_path, 200, 1.0).cash == pytest.approx(190)


def test_max_hold_sells_after_time(tmp_path):
    eng, jup, clock = make(tmp_path, [raw("A", p5=1)], max_hold_hours=6)
    eng.try_buys(eng.scan())
    jup.live = {"A": 1.02}
    clock.t += 5 * 3600
    eng.refresh_positions()
    assert "A" in eng.paper.positions
    clock.t += 3600
    eng.refresh_positions()
    assert "A" not in eng.paper.positions


def test_fresh_pump_token_is_bought(tmp_path):
    # Like "Cassie": 4 minutes old, +881% in 5m, $35.7K market cap.
    eng, _, _ = make(tmp_path, [raw("CASSIE", p5=881, p1=881, mcap=35_700, liq=9_000, pool_age_h=0.07)])
    eng.try_buys(eng.scan())
    assert eng.paper.positions["CASSIE"].mood == "shocked"


def test_price_api_parsing_and_use(tmp_path):
    from swarmbot.jupiter import Jupiter

    class Resp:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    class Session:
        headers = {}
        urls = []

        def get(self, url, params=None, timeout=None):
            self.urls.append((url, params))
            return Resp({"A": {"usdPrice": 1.5, "blockId": 1}, "B": None, "C": {"usdPrice": "x"}})

    jup = Jupiter("https://lite-api.jup.ag", session=Session(), min_interval=0)
    assert jup.prices(["A", "B", "C"]) == {"A": 1.5}
    assert Session.urls[0][0] == "https://lite-api.jup.ag/price/v3"

    eng, fake, clock = make(tmp_path, [raw("A", p5=40, price=1.0)])
    eng.try_buys(eng.scan())
    fake.live = {"A": 1.16}
    fake.tokens = lambda mints: []  # only the Price API answers
    eng.refresh_positions()
    assert "A" not in eng.paper.positions  # TP from the live price


def test_coin_that_stands_still_for_5_minutes_is_sold(tmp_path):
    eng, jup, clock = make(tmp_path, [raw("A", p5=40), raw("B", p5=40)], stale_minutes=5, stale_move_pct=2)
    eng.try_buys(eng.scan())
    for minute in range(1, 6):
        clock.t += 60
        jup.live = {"A": 1.0 + 0.003 * minute, "B": 1.0 + (0.03 if minute % 2 else 0.0)}
        eng.refresh_positions()
    # A crept up 1.5% in 5 minutes: sold as "diam". B kept moving 3%: still held.
    assert "A" not in eng.paper.positions and "B" in eng.paper.positions
    assert ",diam" in (tmp_path / "trades.csv").read_text()


def test_fills_all_25_slots_without_hourly_cap(tmp_path):
    rows = [raw(f"T{i}", p5=40) for i in range(40)]
    eng, _, _ = make(tmp_path, rows, max_open_positions=25, max_buys_per_hour=0, starting_cash_usd=300)
    eng.try_buys(eng.scan())
    assert len(eng.paper.positions) == 25
    assert eng.buy_block.startswith("slot penuh")


def test_hourly_cap_is_reported(tmp_path):
    eng, _, _ = make(tmp_path, [raw(f"T{i}", p5=40) for i in range(10)], max_buys_per_hour=3)
    eng.try_buys(eng.scan())
    assert len(eng.paper.positions) == 3 and "per jam" in eng.buy_block


def test_every_position_is_sold_after_10_minutes(tmp_path):
    eng, jup, clock = make(tmp_path, [raw("A", p5=40)], max_hold_minutes=10)
    eng.try_buys(eng.scan())
    jup.live = {"A": 1.05}
    clock.t += 9 * 60
    eng.refresh_positions()
    assert "A" in eng.paper.positions
    clock.t += 60
    eng.refresh_positions()
    assert "A" not in eng.paper.positions
    assert ",10 menit" in (tmp_path / "trades.csv").read_text()


def test_default_config_buys_every_shocked_and_happy_but_not_calm(tmp_path):
    rows = [raw("S", p5=40, liq=300, mcap=2_000), raw("H", p1=25, sus=True, mcap=900_000_000),
            raw("C", p5=1, p1=2)]
    cfg = Config(data_dir=tmp_path, jupiter_lists=["a"])
    eng = Engine(cfg, jupiter=FakeJup({"a": rows}), paper=Paper(tmp_path, 300, 1.0), clock=Clock())
    eng.try_buys(eng.scan())
    assert set(eng.paper.positions) == {"S", "H"}


def test_switching_wallet_to_sol_starts_fresh_and_keeps_history(tmp_path):
    old = Paper(tmp_path, 300, 1.0, currency="USD")
    old.buy("A", "A", "happy", 1.0, 10)
    (tmp_path / "state.json").write_text(
        (tmp_path / "state.json").read_text().replace('"currency": "USD"', '"currency": "USD"'))
    new = Paper(tmp_path, 10, 1.0, currency="SOL")
    assert new.cash == 10 and new.positions == {}
    assert list(tmp_path.glob("state-USD-*.json")) and list(tmp_path.glob("trades-USD-*.csv"))
    new.buy("B", "B", "shocked", 2.0, 0.1)
    pnl, pct = new.sell("B", 2.24, "TP", 0)
    assert pnl == pytest.approx(0.1 * 0.99 * 1.12 * 0.99 - 0.1)


def test_status_carries_sol_price_for_the_dashboard(tmp_path):
    import json

    from swarmbot.engine import SOL_MINT

    eng, jup, _ = make(tmp_path, [raw("A", p5=40)])
    jup.live = {SOL_MINT: 150.0}
    eng.refresh_sol_price()
    eng.write_status()
    assert json.loads((tmp_path / "status.json").read_text())["sol_usd"] == 150.0
