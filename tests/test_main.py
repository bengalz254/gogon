"""End-to-end run of the main loop in paper mode against a fake CLOB client:
one cycle, then a restart, checking that trades, state and status line up."""
import csv
import json

import bot.main as main_mod

SETTINGS = """
polling_interval_seconds: 1
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
    monkeypatch.setattr(main_mod, "_stop", False)
    sleeps = []

    def stop_after_enough_cycles(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= cycles:
            main_mod._stop = True

    monkeypatch.setattr(main_mod.time, "sleep", stop_after_enough_cycles)
    main_mod.run()


def run_one_cycle(monkeypatch):
    run_cycles(monkeypatch, cycles=1)


def read_trades(tmp_path):
    with open(tmp_path / "data" / "trades.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_status(tmp_path):
    return json.loads((tmp_path / "data" / "status.json").read_text(encoding="utf-8"))


def prepare(tmp_path, monkeypatch, client):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(SETTINGS, encoding="utf-8")
    monkeypatch.setenv("BOT_CONFIG_PATH", "config/settings.yaml")
    # Set explicitly so a developer's real .env can't switch on live trading
    # or Telegram during the test.
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)
    monkeypatch.setattr(main_mod, "build_client", lambda wallet: client)
    monkeypatch.setattr(main_mod.signal_module, "signal", lambda *args: None)


def test_paper_cycle_trades_persists_and_survives_a_restart(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch, FakeClient())

    run_one_cycle(monkeypatch)

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
