import csv

import pytest

from hlbot.broker import PaperBroker, PositionInfo
from hlbot.config import ConnectionConfig, HLSettings
from hlbot.main import LiveBot, TradeLog
from hlbot.strategy import LONG, SHORT

from hl_helpers import SPAN_30M, candles_from_closes, down_then_up, frictionless, strategy_cfg, trade_cfg
from test_hl_backtest import signal_indices


class FakeInfo:
    def __init__(self, candles, price=100.0):
        self.candles = candles
        self.price = price
        self.available_until = None  # simulate API lag: hide candles with t > this

    def recent_candles(self, coin, interval, count, now_ms=None):
        out = [c for c in self.candles if c.t <= now_ms]
        if self.available_until is not None:
            out = [c for c in out if c.t <= self.available_until]
        return out[-count:]

    def mid_price(self, coin):
        return self.price


class Clock:
    def __init__(self, t_ms):
        self.t_ms = t_ms

    def __call__(self):
        return self.t_ms / 1000.0


def make_bot(tmp_path, info, clock, broker=None):
    settings = HLSettings(
        strategy=strategy_cfg(),
        trade=trade_cfg(candle_close_delay_seconds=5),
        backtest=frictionless(),
        connection=ConnectionConfig(live_trading=False, network="mainnet", secret_key=None, account_address=None),
    )
    broker = broker or PaperBroker(info, "BTC", taker_fee=0.0, slippage=0.0)
    journal = TradeLog(str(tmp_path / "trades.csv"))
    return LiveBot(settings, info, broker, journal, str(tmp_path / "state.json"), live=False, clock=clock)


def journal_rows(tmp_path):
    with open(tmp_path / "trades.csv") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def scenario():
    candles = candles_from_closes(down_then_up() + [100.0 - 0.5 * i for i in range(1, 41)])
    k = signal_indices(candles)[0][0]  # index of the candle whose close is a LONG cross
    return candles, k


def test_ignores_forming_candle_then_opens_long_after_close(tmp_path, scenario):
    candles, k = scenario
    info = FakeInfo(candles, price=candles[k].c)
    clock = Clock(candles[k].t + 60_000)  # 1 min into the cross candle (still forming)
    bot = make_bot(tmp_path, info, clock)
    bot.start()
    bot.tick()
    assert bot.broker.get_position() is None  # cross only exists in the forming candle

    clock.t_ms = candles[k].t + SPAN_30M + 2_000  # closed, but within the 5s API delay
    bot.tick()
    assert bot.broker.get_position() is None

    clock.t_ms = candles[k].t + SPAN_30M + 6_000
    bot.tick()
    pos = bot.broker.get_position()
    assert pos is not None and pos.side == LONG
    assert bot.tracker.side == LONG
    assert bot.last_candle_t == candles[k].t
    assert [r["action"] for r in journal_rows(tmp_path)] == ["OPEN"]

    bot.tick()  # same candle is never processed twice
    assert [r["action"] for r in journal_rows(tmp_path)] == ["OPEN"]


def test_cross_closes_opposite_side_first(tmp_path, scenario):
    candles, k = scenario
    info = FakeInfo(candles, price=candles[k].c)
    clock = Clock(candles[k].t + 60_000)
    broker = PaperBroker(info, "BTC", taker_fee=0.0, slippage=0.0)
    broker.position = PositionInfo(SHORT, 1.0, candles[k].c * 1.01)
    bot = make_bot(tmp_path, info, clock, broker)
    bot.start()
    assert bot.tracker.side == SHORT  # adopted existing position

    clock.t_ms = candles[k].t + SPAN_30M + 6_000
    bot.tick()
    rows = journal_rows(tmp_path)
    assert [(r["action"], r["side"], r["reason"]) for r in rows] == [
        ("CLOSE", SHORT, "REVERSE_SIGNAL"),
        ("OPEN", LONG, "EMA9/21 cross"),
    ]
    assert broker.get_position().side == LONG


def test_no_cross_stays_idle(tmp_path, scenario):
    candles, k = scenario
    info = FakeInfo(candles, price=candles[k].c)
    clock = Clock(candles[k - 5].t + 60_000)
    bot = make_bot(tmp_path, info, clock)
    bot.start()
    for j in range(k - 5, k):  # candles that close without a cross
        clock.t_ms = candles[j].t + SPAN_30M + 6_000
        bot.tick()
    assert bot.broker.get_position() is None
    assert journal_rows(tmp_path) == []


def test_waits_for_lagging_candle_data(tmp_path, scenario):
    candles, k = scenario
    info = FakeInfo(candles, price=candles[k].c)
    clock = Clock(candles[k].t + 60_000)
    bot = make_bot(tmp_path, info, clock)
    bot.start()
    info.available_until = candles[k - 1].t  # API hasn't published candle k yet
    clock.t_ms = candles[k].t + SPAN_30M + 6_000
    bot.tick()
    assert bot.broker.get_position() is None
    assert bot.last_candle_t == candles[k - 1].t

    info.available_until = None
    bot.tick()
    assert bot.broker.get_position().side == LONG


def test_trailing_exit_then_idle_until_next_cross(tmp_path, scenario):
    candles, k = scenario
    info = FakeInfo(candles, price=100.0)
    clock = Clock(candles[k].t + 60_000)
    bot = make_bot(tmp_path, info, clock)
    bot.start()
    clock.t_ms = candles[k].t + SPAN_30M + 6_000
    bot.tick()
    entry = bot.tracker.entry_price
    assert entry == pytest.approx(100.0)

    info.price = 103.0
    bot.tick()
    assert bot.tracker.trailing_active
    info.price = 102.6
    bot.tick()
    assert bot.tracker is not None
    info.price = 102.4  # below 103 * 0.995 = 102.485
    bot.tick()
    assert bot.tracker is None and bot.broker.get_position() is None
    assert journal_rows(tmp_path)[-1]["reason"] == "TRAILING_STOP"
    assert bot.broker.realized_pnl == pytest.approx(0.024 * bot.s.trade.notional_usd, rel=1e-6)

    clock.t_ms += SPAN_30M  # next candle closes without a cross: still flat
    bot.tick()
    assert bot.broker.get_position() is None


def test_state_survives_restart(tmp_path, scenario):
    candles, k = scenario
    info = FakeInfo(candles, price=100.0)
    clock = Clock(candles[k].t + 60_000)
    bot = make_bot(tmp_path, info, clock)
    bot.start()
    clock.t_ms = candles[k].t + SPAN_30M + 6_000
    bot.tick()
    info.price = 103.0
    bot.tick()

    bot2 = make_bot(tmp_path, info, clock, PaperBroker(info, "BTC", taker_fee=0.0, slippage=0.0))
    bot2.start()
    assert bot2.broker.get_position().side == LONG
    assert bot2.tracker.trailing_active
    assert bot2.tracker.best_price == pytest.approx(103.0)
    assert bot2.last_candle_t == candles[k].t


def test_margin_basis_live_tp_and_trailing(tmp_path, scenario):
    candles, k = scenario
    info = FakeInfo(candles, price=100.0)
    clock = Clock(candles[k].t + 60_000)
    bot = make_bot(tmp_path, info, clock)
    bot.s.trade.pct_basis = "margin"  # TP 2% / trailing 0.5% of margin at 10x
    bot.start()
    clock.t_ms = candles[k].t + SPAN_30M + 6_000
    bot.tick()
    assert bot.tracker.tp_price == pytest.approx(100.2)

    info.price = 100.19
    bot.tick()
    assert not bot.tracker.trailing_active
    info.price = 100.30
    bot.tick()
    assert bot.tracker.trailing_active
    info.price = 100.26  # stop = 100.30 * 0.9995 = 100.24985
    bot.tick()
    assert bot.tracker is not None
    info.price = 100.24
    bot.tick()
    assert bot.tracker is None
    assert journal_rows(tmp_path)[-1]["reason"] == "TRAILING_STOP"
