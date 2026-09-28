import glob
import os
import re

import pytest

from scalper.config import Settings
from scalper.demo import synthetic_candles
from scalper.strategies import build_strategy
from scalper.why import WhyTracker, classify, label, rows

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "scalper")


@pytest.mark.parametrize("reason,code", [
    ("warming up", "warmup"),
    ("no clear higher-timeframe trend", "no_trend"),
    ("ADX 12.3 < 18.0", "weak_trend"),
    ("ADX None < 18.0", "weak_trend"),
    ("trending (ADX 25.1 > 20.0)", "trending"),
    ("base timeframe does not agree with higher-timeframe trend", "tf_disagree"),
    ("long setup incomplete", "waiting_setup"),
    ("no band re-entry", "waiting_setup"),
    ("ATR 0.012% outside [0.03, 2.0]", "volatility"),
    ("trigger bar is a volatility spike", "spike"),
    ("stop 2.71 ATR away > max_sl_atr", "bad_rr"),
    ("long reversion reward:risk too small", "bad_rr"),
    ("stop 0.200% < 3.0× round-trip cost (0.420%); fees would eat the edge", "fee_filter"),
    ("bot halted: drawdown 15.2% >= 15.0%", "halted"),
    ("daily loss limit reached (-30.00); no new trades until 00:00 UTC", "halted"),
    ("cooling down after 3 losses in a row (45 min left)", "halted"),
    ("max_open_positions (2) reached", "busy"),
    ("symbol cooldown after last exit", "busy"),
    ("entry rejected: Margin is insufficient.", "order_failed"),
    ("stop-loss could not be placed", "order_failed"),
    ("price already at/below the stop", "late"),
    ("notional 45.20 below exchange minimum 50 USDT (account too small for this symbol)", "too_small"),
    ("stop is 25.00% away but liquidation at 5x is only ~19.50% away", "too_small"),
    ("something new", "other"),
])
def test_known_reasons_get_a_plain_language_label(reason, code):
    assert classify(reason) == code
    assert label(code, reason=reason)


def test_every_strategy_skip_reason_is_recognised():
    """A reworded _skip() reason would silently show up as "other"."""
    found = 0
    for path in glob.glob(os.path.join(SRC, "strategies", "*.py")):
        with open(path, encoding="utf-8") as f:
            for literal in re.findall(r'_skip\(f?"([^"]+)"\)', f.read()):
                found += 1
                example = re.sub(r"\{[^}]*\}", "1", literal)
                assert classify(example) != "other", f"{os.path.basename(path)}: {literal!r}"
    assert found >= 15


def test_tracker_keeps_a_rolling_24h_window():
    t = WhyTracker()
    hour = 3_600_000
    t.record("BTCUSDT", 0, "no_trend")
    t.record("BTCUSDT", 23 * hour, "waiting_setup")
    assert t.counts("BTCUSDT") == {"no_trend": 1, "waiting_setup": 1}
    t.record("BTCUSDT", 25 * hour, "entered", extra={"atr_pct": 0.3})
    assert t.counts("BTCUSDT") == {"waiting_setup": 1, "entered": 1}
    assert t.latest["BTCUSDT"]["code"] == "entered" and t.latest["BTCUSDT"]["atr_pct"] == 0.3


def test_rows_flag_a_market_too_calm_to_pass_the_fee_filter():
    state = {"tf": "5m", "htf": "1h", "fee_min_pct": 0.42, "symbols": {
        "ETHUSDT": {"latest": {"code": "waiting_setup", "time": 1, "atr_pct": 0.2, "max_stop_pct": 0.5},
                    "counts": {"waiting_setup": 10}},
        "BTCUSDT": {"latest": {"code": "no_trend", "time": 1, "trend": "FLAT", "atr_pct": 0.14, "max_stop_pct": 0.35},
                    "counts": {"no_trend": 6, "fee_filter": 2, "entered": 1}},
    }}
    btc, eth = rows(state)  # sorted by symbol
    assert btc["symbol"] == "BTCUSDT" and btc["calm"] and not eth["calm"]
    assert btc["now"] == "Tren 1h belum jelas naik/turun" and btc["trend"] == "datar"
    assert (btc["candles"], btc["signals"], btc["entries"]) == (9, 3, 1)
    assert btc["breakdown"][0] == {"code": "no_trend", "label": "Tren 1h belum jelas naik/turun", "n": 6, "pct": 67}
    assert rows(None) == []


def test_trend_pullback_reports_what_it_sees():
    s = Settings()
    strat = build_strategy(s, "BTCUSDT")
    for c in synthetic_candles(100.0, 0.002, 7, 2000, 300_000, 2000 * 300_000):
        strat.on_candle(c)
    st = strat.status()
    assert st["trend"] in ("LONG", "SHORT", "FLAT") and st["adx"] > 0
    assert st["max_stop_pct"] == pytest.approx(st["atr_pct"] * s.strategy.trend_pullback.max_sl_atr, rel=1e-3)
    assert classify(strat.last_skip_reason) != "other" or strat.last_skip_reason == ""
