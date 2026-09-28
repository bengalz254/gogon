import csv
import io
import os
import zipfile

import pytest
from fakes import STEP, ScriptedStrategy, flat, ohlc, trend_pullback_sequence

from scalper.backtest import run_backtest, split_stats
from scalper.config import Settings
from scalper.data import dedupe_sorted, fetch_klines, find_gaps, load_csv, load_history, parse_kline, save_csv
from scalper.models import LONG, SHORT
from scalper.report import format_summary, verdict, write_outputs


def settings(**risk):
    s = Settings()
    s.symbols = ["BTCUSDT"]
    s.risk.min_sl_cost_ratio = 0
    s.risk.risk_per_trade_pct = 1.0
    s.costs.slippage_bps = 0
    s.costs.funding_bps_per_8h = 0
    s.management.breakeven_at_r = 0
    for k, v in risk.items():
        setattr(s.risk, k, v)
    return s


def test_long_trade_hits_limit_target_with_exact_accounting():
    s = settings()
    candles = flat(20)
    candles.append(ohlc(20, 100, 100.2, 99.8, 100))  # signal bar
    candles.append(ohlc(21, 100, 100.5, 99.5, 100.2))  # entry at open 100
    candles.append(ohlc(22, 100.2, 101.6, 100.1, 101.4))  # trades through TP 101.5
    candles += flat(3, 101.4, start=23)
    strat = ScriptedStrategy("BTCUSDT", plan={candles[20].open_time: (LONG, 99.0, 1.5)})
    r = run_backtest(candles, s, "BTCUSDT", strategy=strat, starting_equity=1000)
    assert len(r.trades) == 1
    t = r.trades[0]
    assert t.entry_time == candles[21].open_time  # next bar's open: no look-ahead
    assert t.entry_price == 100.0 and t.exit_price == 101.5 and t.exit_reason == "TP"
    qty = t.qty
    expected_fees = qty * 100.0 * s.costs.taker_fee + qty * 101.5 * s.costs.maker_fee
    assert t.fees == pytest.approx(expected_fees)
    assert t.net_pnl == pytest.approx(qty * 1.5 - expected_fees)
    assert r.end_equity == pytest.approx(1000 + t.net_pnl)
    assert t.r_multiple == pytest.approx(t.net_pnl / (qty * 1.0))


def test_stop_first_when_bar_hits_both():
    s = settings()
    candles = flat(20) + [ohlc(20, 100, 100, 100, 100), ohlc(21, 100, 100, 100, 100),
                          ohlc(22, 100, 102, 98, 100)] + flat(2, start=23)
    strat = ScriptedStrategy("BTCUSDT", plan={candles[20].open_time: (LONG, 99.0, 1.5)})
    r = run_backtest(candles, s, "BTCUSDT", strategy=strat)
    assert [t.exit_reason for t in r.trades] == ["SL"]
    assert r.trades[0].exit_price == 99.0


def test_short_and_time_stop_exit_at_next_open():
    s = settings()
    s.management.max_bars_in_trade = 2
    candles = flat(20) + [ohlc(20, 100, 100, 100, 100)] + flat(3, 99.9, start=21) + [ohlc(24, 99.7, 99.8, 99.6, 99.7)]
    strat = ScriptedStrategy("BTCUSDT", plan={candles[20].open_time: (SHORT, 101.0, 1.5)})
    r = run_backtest(candles, s, "BTCUSDT", strategy=strat)
    t = r.trades[0]
    assert t.side == SHORT and t.exit_reason == "TIME" and t.bars_held == 2
    assert t.exit_time == candles[23].open_time and t.exit_price == candles[23].open


def test_funding_charged_when_holding_through_8h_boundary():
    s = settings()
    s.costs.funding_bps_per_8h = 1.0
    t0 = 8 * 3_600_000 * 1000  # an 8h boundary
    candles = [ohlc(i, 100, 100.1, 99.9, 100, t0=t0 - 30 * STEP) for i in range(40)]
    strat = ScriptedStrategy("BTCUSDT", plan={candles[20].open_time: (LONG, 99.0, 50)})
    r = run_backtest(candles, s, "BTCUSDT", strategy=strat)
    t = r.trades[0]
    assert t.exit_reason == "END"
    assert t.funding == pytest.approx(t.qty * 100 * 0.0001)


def test_accounting_identity_on_real_strategy():
    s = Settings()
    candles = trend_pullback_sequence(1) + flat(50, 110.0, start=726)
    r = run_backtest(candles, s, "BTCUSDT")
    assert r.end_equity == pytest.approx(r.start_equity + sum(t.net_pnl for t in r.trades))


def test_daily_loss_limit_stops_backtest_trading():
    s = settings(max_daily_loss_pct=0.5, max_consecutive_losses=0)
    plan, candles = {}, flat(20)
    i = 20
    for _ in range(4):  # four identical losing setups on the same day
        candles += [ohlc(i, 100, 100, 100, 100), ohlc(i + 1, 100, 100, 100, 100),
                    ohlc(i + 2, 100, 100, 98.5, 99)] + flat(3, 100, start=i + 3)
        plan[candles[i - 0].open_time] = (LONG, 99.0, 1.5)
        i += 6
    strat = ScriptedStrategy("BTCUSDT", plan=plan)
    r = run_backtest(candles, s, "BTCUSDT", strategy=strat)
    assert len(r.trades) == 1  # 1% loss > 0.5% daily cap -> no more trades that day
    assert r.skipped["daily loss limit"] == 3


def test_split_stats_and_report(tmp_path):
    s = Settings()
    r = run_backtest(trend_pullback_sequence(1) + flat(30, 110.0, start=726), s, "BTCUSDT")
    parts = split_stats(r, 0.3)
    assert set(parts) == {"all", "in_sample", "out_of_sample"}
    text = format_summary("test", [r], 0.3)
    assert "OUT-OF-SAMPLE" in text and "Verdict" in text
    paths = write_outputs(str(tmp_path), "test", [r], 0.3)
    for p in paths.values():
        assert os.path.getsize(p) > 0


def test_verdict_is_conservative():
    base = dict(trades=100, net_profit=50.0, profit_factor=1.5)
    assert "too few" in verdict({**base, "trades": 10}, None)
    assert "Do NOT" in verdict({**base, "net_profit": -1, "profit_factor": 0.9}, None)
    assert "out-of-sample" in verdict(base, {"trades": 30, "net_profit": -5, "profit_factor": 0.8})
    assert "Marginal" in verdict({**base, "profit_factor": 1.1}, None)
    assert "Promising" in verdict(base, {"trades": 30, "net_profit": 20, "profit_factor": 1.4})


# -- data ---------------------------------------------------------------------------
def kline_row(t, price=100.0):
    return [t, str(price), str(price + 1), str(price - 1), str(price), "10", t + STEP - 1, "0", 5, "0", "0", "0"]


class PagingClient:
    def __init__(self, candles_open_times):
        self.times = candles_open_times
        self.calls = []

    def klines(self, symbol, interval, limit=500, start_time=None, end_time=None):
        self.calls.append((start_time, end_time, limit))
        rows = [kline_row(t) for t in self.times if start_time <= t <= end_time]
        return rows[:limit]


def test_fetch_klines_pages_and_drops_unclosed():
    times = [i * STEP for i in range(4000)]
    client = PagingClient(times)
    now = times[-1] + 10  # the last candle is still forming
    out = fetch_klines(client, "BTCUSDT", "5m", 0, now, now_ms=now, sleep=lambda s: None)
    assert len(out) == 3999 and out[-1].open_time == times[-2]
    assert len(client.calls) == 3


def test_load_history_uses_and_extends_cache(tmp_path):
    day = 86_400_000
    times = [i * STEP for i in range(3 * day // STEP)]
    client = PagingClient(times)
    now = times[-1] + STEP  # everything closed
    first = load_history(client, "BTCUSDT", "5m", 1, str(tmp_path), now, sleep=lambda s: None)
    assert first[0].open_time == now - day and first[-1].open_time == times[-1]
    calls_before = len(client.calls)
    again = load_history(client, "BTCUSDT", "5m", 1, str(tmp_path), now, sleep=lambda s: None)
    assert len(client.calls) == calls_before and len(again) == len(first)  # served from cache
    longer = load_history(client, "BTCUSDT", "5m", 2, str(tmp_path), now, sleep=lambda s: None)
    assert longer[0].open_time == now - 2 * day


def test_load_csv_formats(tmp_path):
    # data.binance.vision layout, header-less, microsecond timestamps
    p = tmp_path / "vision.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        for i in range(3):
            t = (1_700_000_000_000 + i * STEP) * 1000
            w.writerow([t, 1, 2, 0.5, 1.5, 10, t + STEP * 1000 - 1000, 0, 0, 0, 0, 0])
    rows = load_csv(str(p))
    assert rows[0].open_time == 1_700_000_000_000 and rows[1].open_time - rows[0].open_time == STEP

    # with a header and no close_time column
    q = tmp_path / "simple.csv"
    q.write_text("open_time,open,high,low,close,volume\n0,1,2,0.5,1.5,1\n300000,1,2,0.5,1.5,1\n")
    rows = load_csv(str(q))
    assert rows[0].close_time == STEP - 1

    # round trip through our own format
    out = tmp_path / "ours.csv"
    save_csv(str(out), rows)
    assert load_csv(str(out)) == rows


def test_parse_dedupe_and_gaps():
    c = parse_kline(kline_row(0))
    assert c.close == 100.0 and c.close_time == STEP - 1
    items = dedupe_sorted([parse_kline(kline_row(STEP)), c, parse_kline(kline_row(STEP))])
    assert [x.open_time for x in items] == [0, STEP]
    assert find_gaps([parse_kline(kline_row(0)), parse_kline(kline_row(3 * STEP))], STEP) == [(0, 3 * STEP)]


def test_zip_from_binance_vision_can_be_read_after_extracting(tmp_path):
    buf = io.StringIO()
    csv.writer(buf).writerows([kline_row(i * STEP) for i in range(5)])
    zpath = tmp_path / "BTCUSDT-5m-2024-01.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("BTCUSDT-5m-2024-01.csv", buf.getvalue())
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmp_path)
    assert len(load_csv(str(tmp_path / "BTCUSDT-5m-2024-01.csv"))) == 5
