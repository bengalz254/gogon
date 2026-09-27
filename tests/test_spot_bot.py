"""The grid bot end to end, on a fake market and a fake clock."""
import csv
import json

import pytest

import spot.main as spot_main
from spot.bot import GridBot, StartupError, last_closed_candle_ts
from spot.config import CostConfig, GridConfig, NotificationConfig, RiskConfig, SpotSettings
from spot.grid import BUYING, IDLE, SELLING
from spot.market import CANDLE_MS, CLOSE_GRACE_MS, Candle, DataError, MarketRules, SymbolError

RULES = MarketRules("BTC/IDR", "BTC", "IDR", tick=0.01, step=0.001, min_amount=0.001, min_cost=1.0, native=True)
T0 = 1_700_000_000.0


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeData:
    def __init__(self, bid=99.9, ask=100.1):
        self.bid, self.ask = bid, ask
        self.candles = []
        self.fail_next = 0

    def rules(self):
        return RULES

    def top_of_book(self):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise DataError("Gagal mengambil order book: RequestTimeout")
        return self.bid, self.ask

    def closed_candles(self, since_ms, now_ms):
        return [c for c in self.candles if c.ts >= since_ms and c.ts + CANDLE_MS + CLOSE_GRACE_MS <= now_ms]

    def add(self, low, high, close=None):
        """Append the next minute's candle and return its opening time."""
        ts = (self.candles[-1].ts if self.candles else last_closed_candle_ts(T0)) + CANDLE_MS
        self.candles.append(Candle(ts, (low + high) / 2, high, low, (low + high) / 2 if close is None else close))
        return ts


class Recorder:
    def __init__(self):
        self.enabled = True
        self.messages = []
        self.closed = False

    def send(self, text, key=None, cooldown_s=0.0):
        self.messages.append(text)
        return True

    def close(self, timeout=10.0):
        self.closed = True


def settings(tmp_path, **grid):
    grid_cfg = dict(lower_price=90, upper_price=110, levels=5, order_value=100)
    grid_cfg.update(grid)
    return SpotSettings(
        symbol="BTC/IDR",
        poll_seconds=15,
        grid=GridConfig(**grid_cfg),
        costs=CostConfig(),
        risk=RiskConfig(paper_balance=1000, stop_loss_pct=10),
        notifications=NotificationConfig(fills=True, summary_hours=0),
        data_dir=str(tmp_path / "data" / "spot"),
    )


def make_bot(tmp_path, data, clock=None, **grid):
    clock = clock or Clock()
    return GridBot(settings(tmp_path, **grid), data, Recorder(), clock=clock, sleep=clock.sleep), clock


def close_minute(clock, ts):
    """Move the clock to just after the candle opened at `ts` has closed."""
    clock.now = max(clock.now, (ts + CANDLE_MS + CLOSE_GRACE_MS) / 1000)


def read_trades(tmp_path):
    with open(tmp_path / "data" / "spot" / "trades.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_status(tmp_path):
    return json.loads((tmp_path / "data" / "spot" / "status.json").read_text(encoding="utf-8"))


def states(bot):
    return [s.state for s in bot.grid.slots]


def test_paper_grid_buys_then_sells_and_survives_a_restart(tmp_path):
    data = FakeData()
    bot, clock = make_bot(tmp_path, data)
    bot.start()
    bot.tick()
    assert states(bot) == [BUYING, BUYING, BUYING, IDLE]
    slot = bot.grid.slots[2]

    # The price dips through slot 3's buy level...
    close_minute(clock, data.add(low=slot.buy_price - 0.1, high=100.5))
    bot.tick()
    assert slot.state == SELLING and slot.order_price == slot.sell_price
    # ...and comes back up through its sell level.
    data.bid, data.ask = 104.9, 105.1
    close_minute(clock, data.add(low=100.0, high=slot.sell_price + 0.1))
    bot.tick()

    trades = read_trades(tmp_path)
    assert [(t["side"], t["slot"], t["mode"]) for t in trades] == [("BUY", "2", "paper"), ("SELL", "2", "paper")]
    pnl = float(trades[1]["pnl"])
    assert pnl == pytest.approx(slot.amount * (slot.sell_price * 0.9966 - slot.buy_price * 1.0013), abs=1e-4)
    status = read_status(tmp_path)
    assert status["round_trips"] == 1
    assert status["realized_pnl"] == pytest.approx(pnl, abs=1e-4)
    assert status["running"] is True and status["mode"] == "paper"
    texts = bot.notifier.messages
    assert any(t.startswith("🟢 [PAPER] Beli") for t in texts)
    assert any(t.startswith("💰 [PAPER] Jual") and "untung bersih" in t for t in texts)

    # A restart picks the same grid, balance and counters back up.
    again, _ = make_bot(tmp_path, data, clock)
    again.start()
    assert again.state["round_trips"] == 1
    assert again.grid.quote == pytest.approx(bot.grid.quote)
    assert states(again) == states(bot)


def test_auto_range_is_centered_on_the_first_price_and_kept(tmp_path):
    data = FakeData(bid=199.9, ask=200.1)
    bot, clock = make_bot(tmp_path, data, lower_price=0, upper_price=0, range_pct=10)
    bot.start()
    assert (bot.grid.lower, bot.grid.upper) == (180.0, 220.0)
    data.bid, data.ask = 149.9, 150.1  # a restart at another price keeps the saved range
    again, _ = make_bot(tmp_path, data, clock, lower_price=0, upper_price=0, range_pct=10)
    again.start()
    assert (again.grid.lower, again.grid.upper) == (180.0, 220.0)


def test_orders_resting_during_downtime_fill_but_counter_orders_wait(tmp_path):
    data = FakeData()
    bot, clock = make_bot(tmp_path, data)
    bot.start()
    bot.tick()
    # While the bot is down the price dips through two buy levels, then rallies.
    data.add(low=94.0, high=99.0)
    data.bid, data.ask = 105.9, 106.1
    last = data.add(low=100.0, high=106.0)
    close_minute(clock, last)
    again, _ = make_bot(tmp_path, data, clock)
    again.start()
    again.tick()
    # The buys filled, but their sells didn't exist yet during the rally.
    assert [t["side"] for t in read_trades(tmp_path)] == ["BUY", "BUY"]
    assert states(again)[1:3] == [SELLING, SELLING]
    # The next rally fills them.
    close_minute(clock, data.add(low=105.0, high=111.0))
    again.tick()
    assert [t["side"] for t in read_trades(tmp_path)] == ["BUY", "BUY", "SELL", "SELL"]
    assert again.state["round_trips"] == 2


def test_stop_loss_sells_everything_and_halts(tmp_path):
    data = FakeData()
    bot, clock = make_bot(tmp_path, data)
    bot.start()
    bot.tick()
    data.bid, data.ask = 85.0, 85.2
    close_minute(clock, data.add(low=84.0, high=99.0))  # all three buys fill
    bot.tick()
    assert bot.grid.count(SELLING) == 3 and not bot.state["stopped"]

    data.bid, data.ask = 79.0, 79.2
    close_minute(clock, data.add(low=78.5, high=85.0, close=79.5))  # closes under 90 - 10% = 81
    bot.tick()
    assert bot.state["stopped"] is True
    assert all(s.state == IDLE for s in bot.grid.slots)
    assert bot.grid.base == pytest.approx(0)
    sells = [t for t in read_trades(tmp_path) if t["side"] == "SELL"]
    assert len(sells) == 3 and all(t["note"] == "stop-loss" and float(t["price"]) == 79.0 for t in sells)
    assert any(m.startswith("⛔ [PAPER] Stop-loss") for m in bot.notifier.messages)

    # Stopped: no new orders, even when the price recovers.
    data.bid, data.ask = 99.9, 100.1
    close_minute(clock, data.add(low=95.0, high=101.0))
    bot.tick()
    assert all(s.state == IDLE for s in bot.grid.slots)
    assert read_status(tmp_path)["stopped"] is True


def test_old_closes_after_downtime_do_not_trip_the_stop_loss(tmp_path):
    data = FakeData()
    bot, clock = make_bot(tmp_path, data)
    bot.start()
    bot.tick()
    # While the bot is down the price crashes under the stop (81) and recovers.
    data.add(low=78.0, high=99.0, close=79.0)
    for _ in range(9):
        data.add(low=95.0, high=99.0, close=98.0)
    data.bid, data.ask = 97.9, 98.1
    close_minute(clock, data.candles[-1].ts)
    again, _ = make_bot(tmp_path, data, clock)
    again.start()
    again.tick()
    # The resting buys filled in the crash, but the bot only sells at today's price.
    assert again.state["stopped"] is False
    assert [t["side"] for t in read_trades(tmp_path)] == ["BUY", "BUY", "BUY"]
    assert again.grid.count(SELLING) == 3


def test_changed_settings_rebuild_the_grid_only_without_coins(tmp_path):
    data = FakeData()
    bot, clock = make_bot(tmp_path, data)
    bot.start()
    bot.tick()
    close_minute(clock, data.add(low=99.0, high=100.5))  # slot 3 buys
    bot.tick()

    changed, _ = make_bot(tmp_path, data, clock, levels=4)
    with pytest.raises(StartupError, match="masih memegang koin"):
        changed.start()

    # Once the coins are sold, a settings change rebuilds the grid and keeps the money.
    close_minute(clock, data.add(low=100.0, high=106.0))
    bot.tick()
    money = bot.grid.quote
    rebuilt, _ = make_bot(tmp_path, data, clock, levels=4)
    rebuilt.start()
    assert len(rebuilt.grid.slots) == 3
    assert rebuilt.grid.quote == pytest.approx(money)
    assert rebuilt.state["round_trips"] == 1


@pytest.mark.parametrize("content", ["{not json", "[]", '{"version": 1}', '{"version": 1, "fingerprint": null, "grid": {}}'])
def test_a_damaged_state_file_asks_for_a_reset(tmp_path, content):
    data = FakeData()
    bot, _ = make_bot(tmp_path, data)
    state = tmp_path / "data" / "spot" / "state_paper.json"
    state.parent.mkdir(parents=True)
    state.write_text(content, encoding="utf-8")
    with pytest.raises(StartupError, match="--reset"):
        bot.start()


def test_unworkable_grid_is_refused_with_the_reason(tmp_path):
    bot, _ = make_bot(tmp_path, FakeData(), lower_price=99, upper_price=101, levels=10)
    with pytest.raises(StartupError, match="terlalu rapat"):
        bot.start()


def test_run_retries_until_the_market_answers_then_polls_and_stops(tmp_path):
    data = FakeData()
    data.fail_next = 3  # the first three starts can't reach Tokocrypto
    bot, clock = make_bot(tmp_path, data)

    polls = []
    original_tick = bot.tick

    def tick_and_stop_after_three():
        original_tick()
        polls.append(clock.now)
        if len(polls) == 3:
            bot.request_stop()

    bot.tick = tick_and_stop_after_three
    assert bot.run() == 0
    messages = bot.notifier.messages
    assert any("belum bisa mulai" in m for m in messages)
    assert any(m.startswith("🟢 Bot grid mulai (PAPER)") for m in messages)
    assert messages[-1].startswith("🔴 Bot grid berhenti")
    assert polls[1] - polls[0] == pytest.approx(15) and polls[2] - polls[1] == pytest.approx(15)
    assert read_status(tmp_path)["running"] is False
    assert bot.notifier.closed


def test_data_errors_alert_after_three_failures_and_on_recovery(tmp_path):
    data = FakeData()
    bot, clock = make_bot(tmp_path, data)
    bot.start()
    data.fail_next = 3
    for _ in range(4):
        bot._poll_once()
    alerts = [m for m in bot.notifier.messages if "gagal mengambil data" in m]
    assert len(alerts) == 1 and alerts[0].startswith("⚠️ 3x")
    assert bot.notifier.messages[-1] == "✅ Data Tokocrypto tersambung lagi."
    assert read_status(tmp_path)["data_failures"] == 0


def test_summary_compares_with_buy_and_hold(tmp_path):
    data = FakeData()
    bot, clock = make_bot(tmp_path, data)
    bot.start()
    bot.tick()
    data.bid, data.ask = 109.9, 110.1
    bot.tick()
    text = bot.summary_text()
    assert "Putaran selesai: 0" in text
    assert "kalau modal dibelikan BTC semua sejak awal" in text
    # 1000 bought at 100 is worth ~1100 at 110, minus the costs of both sides
    assert bot._hodl_pnl() == pytest.approx(1000 * 0.9987 / 100 * 109.9 * 0.9966 - 1000)


def test_symbol_errors_propagate_from_run(tmp_path):
    class NoPair(FakeData):
        def rules(self):
            raise SymbolError("Pasangan BTC/IDR tidak ada di Tokocrypto.")

    bot, _ = make_bot(tmp_path, NoPair())
    with pytest.raises(SymbolError):
        bot.run()


# -- the command line ------------------------------------------------------------------


def test_config_errors_exit_with_a_readable_message(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "spot.yaml").write_text("grid:\n  levels: 1\n", encoding="utf-8")
    monkeypatch.delenv("SPOT_WAIT_ON_ERROR", raising=False)
    assert spot_main.main(["--config", "spot.yaml"]) == 2
    assert "grid.levels" in capsys.readouterr().err


def test_reset_refuses_while_the_bot_runs_then_archives(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    (tmp_path / "spot.yaml").write_text("symbol: BTC/IDR\n", encoding="utf-8")
    spot_dir = tmp_path / "data" / "spot"
    spot_dir.mkdir(parents=True)
    (spot_dir / "state_paper.json").write_text("{}", encoding="utf-8")
    status = {"running": True, "updated_ts": __import__("time").time()}
    (spot_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")

    assert spot_main.main(["--config", "spot.yaml", "--reset"]) == 1
    assert (spot_dir / "state_paper.json").exists()

    status["running"] = False
    (spot_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    assert spot_main.main(["--config", "spot.yaml", "--reset"]) == 0
    assert not (spot_dir / "state_paper.json").exists()
    assert list(spot_dir.glob("state_paper.json.bak.*"))
    assert "disimpan sebagai" in capsys.readouterr().out
