"""End-to-end run of the main loop in paper mode against a fake CLOB client:
one cycle, then a restart, checking that trades, state and status line up."""
import csv
import json

import bot.main as main_mod

SETTINGS = """
polling_interval_seconds: 1
market_data:
  websocket: false
markets:
  min_volume_usd: 0
  min_liquidity_usd: 0
  max_markets_per_cycle: 10
risk:
  max_position_usd: 25
  max_total_exposure_usd: 200
  max_daily_loss_usd: 50
  min_order_size_usd: 1
strategies:
  arbitrage:
    enabled: true
    min_edge: 0.015
    fee_buffer: 0.005
  threshold:
    enabled: false
"""


class Level:
    def __init__(self, price, size):
        self.price, self.size = str(price), str(size)


class Book:
    def __init__(self, bid, ask):
        self.bids, self.asks = [Level(bid, 100)], [Level(ask, 100)]


class FakeClient:
    """One fee-free market whose YES+NO asks sum to 0.96; it never resolves."""

    def get_sampling_markets(self, next_cursor="MA=="):
        market = {
            "condition_id": "mkt1",
            "question": "Will it happen?",
            "tags": ["Geopolitics"],
            "tokens": [{"token_id": "yes", "outcome": "Yes"}, {"token_id": "no", "outcome": "No"}],
        }
        return {"data": [market], "next_cursor": "LTE="}

    def get_order_book(self, token_id):
        ask = {"yes": 0.47, "no": 0.49}[token_id]
        return Book(round(ask - 0.02, 2), ask)

    def get_order_books(self, params):
        self.batch_requests = getattr(self, "batch_requests", 0) + 1
        books = []
        for p in params:
            ask = {"yes": 0.47, "no": 0.49}[p["token_id"]]
            books.append(
                {
                    "asset_id": p["token_id"],
                    "bids": [{"price": str(round(ask - 0.02, 2)), "size": "100"}],
                    "asks": [{"price": str(ask), "size": "100"}],
                }
            )
        return books

    def get_market(self, condition_id):
        return {"condition_id": condition_id, "closed": False, "tokens": []}


class EmptyClient(FakeClient):
    """As if the market listing were unreachable: every scan comes back empty."""

    def get_sampling_markets(self, next_cursor="MA=="):
        return {"data": []}


class RecordingNotifier:
    instances = []

    def __init__(self, bot_token, chat_id):
        self.enabled = True
        self.messages = []
        RecordingNotifier.instances.append(self)

    def send(self, text, key=None, cooldown_s=0.0):
        self.messages.append(text)
        return True

    def close(self, timeout=10.0):
        pass


def run_cycles(monkeypatch, cycles=1):
    """Run the bot on a fake clock that only moves when it sleeps, stopping
    after `cycles` polling intervals (1 s in these settings)."""
    monkeypatch.setattr(main_mod, "_stop", False)
    clock = {"now": 1_000_000.0}
    start = clock["now"]

    def fake_sleep(seconds):
        clock["now"] += seconds
        if clock["now"] - start >= cycles - 1e-6:
            main_mod._stop = True

    monkeypatch.setattr(main_mod.time, "time", lambda: clock["now"])
    monkeypatch.setattr(main_mod.time, "sleep", fake_sleep)
    main_mod.run()


def run_one_cycle(monkeypatch):
    run_cycles(monkeypatch, cycles=1)


def read_trades(tmp_path):
    with open(tmp_path / "data" / "trades.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_status(tmp_path):
    return json.loads((tmp_path / "data" / "status.json").read_text(encoding="utf-8"))


def prepare(tmp_path, monkeypatch, client, extra_settings=""):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(SETTINGS + extra_settings, encoding="utf-8")
    monkeypatch.setenv("BOT_CONFIG_PATH", "config/settings.yaml")
    # Set explicitly so a developer's real .env can't switch on live trading
    # or Telegram during the test.
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)
    monkeypatch.setattr(main_mod, "build_client", lambda wallet: client)
    monkeypatch.setattr(main_mod.signal_module, "signal", lambda *args: None)


NO_MERGE = """
merge:
  paper_auto_merge: false
"""


def test_paper_cycle_trades_persists_and_survives_a_restart(tmp_path, monkeypatch):
    client = FakeClient()
    prepare(tmp_path, monkeypatch, client, NO_MERGE)

    run_one_cycle(monkeypatch)
    assert client.batch_requests == 1  # both books came in one /books request

    trades = read_trades(tmp_path)
    assert [(t["side"], t["outcome"], t["filled"]) for t in trades] == [
        ("BUY", "Yes", "True"),
        ("BUY", "No", "True"),
    ]
    assert {t["size_shares"] for t in trades} == {"26.0400"}  # $25 / 0.96, rounded down

    status = read_status(tmp_path)
    assert status["mode"] == "paper"
    assert status["open_positions"] == 2
    assert abs(status["exposure_usd"] - 26.04 * 0.96) < 1e-6
    # 26.04 complete sets are worth $26.04 whatever the bids are
    assert abs(status["unrealized_pnl_usd"] - 26.04 * 0.04) < 1e-6
    assert (tmp_path / "data" / "state_paper.sqlite3").exists()

    # Restart: positions come back from SQLite, so the $25 per-market cap
    # still holds and the same opportunity isn't bought twice.
    run_one_cycle(monkeypatch)

    assert len(read_trades(tmp_path)) == 2
    assert read_status(tmp_path)["open_positions"] == 2


def test_alerts_when_scans_keep_coming_back_empty(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch, EmptyClient())
    RecordingNotifier.instances.clear()
    monkeypatch.setattr(main_mod, "Notifier", RecordingNotifier)

    run_cycles(monkeypatch, cycles=3)

    messages = RecordingNotifier.instances[0].messages
    assert "Bot mulai" in messages[0]
    assert "Bot berhenti" in messages[-1]
    empty_alerts = [m for m in messages if "tidak ada market" in m]
    assert len(empty_alerts) == 1  # only once the 3rd empty cycle is reached
    assert read_status(tmp_path)["markets_scanned"] == 0


def test_failing_housekeeping_step_does_not_stop_the_bot(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch, FakeClient())
    RecordingNotifier.instances.clear()
    monkeypatch.setattr(main_mod, "Notifier", RecordingNotifier)

    def disk_full(*args):
        raise OSError("No space left on device")

    monkeypatch.setattr(main_mod, "settle_resolved_markets", disk_full)

    run_one_cycle(monkeypatch)

    messages = RecordingNotifier.instances[0].messages
    assert any("'settlement' gagal (OSError)" in m for m in messages)
    assert read_status(tmp_path)["cycles"] == 1  # the cycle still ran


def test_paper_merges_complete_sets_and_books_the_profit(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch, FakeClient())

    run_one_cycle(monkeypatch)

    trades = read_trades(tmp_path)
    assert [(t["strategy"], t["side"]) for t in trades] == [
        ("arbitrage", "BUY"),
        ("arbitrage", "BUY"),
        ("merge", "SELL"),
        ("merge", "SELL"),
    ]
    # journal amounts are rounded to 4 decimals per row
    assert abs(sum(float(t["size_usd"]) for t in trades if t["strategy"] == "merge") - 26.04) < 1e-3
    status = read_status(tmp_path)
    assert status["open_positions"] == 0
    assert abs(status["realized_pnl_today_usd"] - 26.04 * 0.04) < 1e-6


# -- market making through the main loop ---------------------------------------------

from bot.orderbook import OrderBook  # noqa: E402
from bot.ws_market import Trade  # noqa: E402

MM_SETTINGS = """
strategies:
  arbitrage:
    enabled: false
  market_maker:
    enabled: true
    order_size: 10
    refresh_seconds: 1
    min_days_to_end: 0
"""


class RewardClient(FakeClient):
    """One reward market, quoted 0.48 / 0.52 on both tokens."""

    def get_sampling_markets(self, next_cursor="MA=="):
        market = {
            "condition_id": "mkt1",
            "question": "Will it happen?",
            "tokens": [{"token_id": "yes", "outcome": "Yes"}, {"token_id": "no", "outcome": "No"}],
            "rewards": {"rates": [{"rewards_daily_rate": 40}], "min_size": 10, "max_spread": 3},
            "minimum_tick_size": 0.01,
        }
        return {"data": [market], "next_cursor": "LTE="}


class FakeFeed:
    """Stands in for the market WebSocket: fixed books, trades handed out per refresh."""

    instances = []

    def __init__(self, url=None, stale_after=None):
        self.books = {
            t: OrderBook(t, bids={0.48: 100.0}, asks={0.52: 100.0}, tick_size=0.01) for t in ("yes", "no")
        }
        self.trade_batches = [[], [Trade("yes", 0.47, 4.0, "SELL", 0.0)]]
        self.assets = []
        FakeFeed.instances.append(self)

    def start(self):
        pass

    def stop(self):
        pass

    def set_assets(self, assets):
        self.assets = list(assets)

    def is_live(self, now=None):
        return True

    def book(self, token_id, now=None):
        book = self.books.get(token_id)
        return book.copy() if book else None

    def drain_trades(self):
        return self.trade_batches.pop(0) if self.trade_batches else []


def test_paper_market_making_quotes_and_fills(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch, RewardClient(), MM_SETTINGS)
    # prepare() switched the socket off; market making needs it (faked here)
    settings_file = tmp_path / "config" / "settings.yaml"
    settings_file.write_text(settings_file.read_text().replace("websocket: false", "websocket: true"))
    FakeFeed.instances.clear()
    RecordingNotifier.instances.clear()
    monkeypatch.setattr(main_mod, "MarketFeed", FakeFeed)
    monkeypatch.setattr(main_mod, "Notifier", RecordingNotifier)

    run_cycles(monkeypatch, cycles=3)

    feed = FakeFeed.instances[0]
    assert {"yes", "no"} <= set(feed.assets)  # quoted tokens are streamed
    trades = read_trades(tmp_path)
    assert [(t["strategy"], t["side"], t["outcome"], t["price"], t["size_shares"]) for t in trades] == [
        ("market_maker", "BUY", "Yes", "0.4800", "4.0000")
    ]
    mm = read_status(tmp_path)["market_maker"]
    assert mm["enabled"] and mm["markets"] == ["mkt1"] and mm["fills_today"] == 1
    assert mm["open_quotes"] == 2
    messages = RecordingNotifier.instances[0].messages
    assert any("Market making di 1 market" in m for m in messages)


def test_market_making_is_refused_without_the_websocket(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch, RewardClient(), MM_SETTINGS)
    run_one_cycle(monkeypatch)
    assert read_status(tmp_path)["market_maker"] == {"enabled": False}
