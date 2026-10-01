"""`migbot backtest --lama`: tokens followed for days after migration."""
import os

import pytest

from migbot.backtest_long import DAY, HOUR, RULES, backtest_long, format_backtest_long, load_series
from migbot.cli import main
from migbot.storage import LongSampleLog
from migbot_fakes import T0, settings


def samples(mint, price_fn, start_h=2.0, end_h=96.0, liq=40_000, mcap=200_000, vol=12_000, dead_at_h=None):
    """Rows like the bot writes: every 10 minutes from start_h to end_h hours after migration."""
    rows, minute = [], int(start_h * 60)
    while minute <= end_h * 60:
        age = minute * 60
        if dead_at_h is not None and age >= dead_at_h * HOUR:
            rows.append([int(T0 + age), mint, minute, 0, 0, 0, 0, 0, 0])
            break
        rows.append([int(T0 + age), mint, minute, price_fn(age), liq, mcap, vol, 300, 200])
        minute += 10
    return rows


def write(data_dir, *row_lists):
    log = LongSampleLog(os.path.join(data_dir, "long_samples.csv.gz"))
    for rows in row_lists:
        log.append(rows)


def test_load_series_sorts_and_marks_dead_tokens(tmp_path):
    write(str(tmp_path), samples("A", lambda a: 1.0, end_h=3), samples("B", lambda a: 2.0, end_h=5, dead_at_h=4))
    series, newest = load_series(os.path.join(str(tmp_path), "long_samples.csv.gz"))
    assert [p[0] for p in series["A"]] == sorted(p[0] for p in series["A"]) and series["A"][0][0] == 2 * HOUR
    assert series["B"][-1][1] < 1e-9 and series["B"][-1][2] == 0  # the dead marker
    assert newest == T0 + 4 * HOUR


def rule(name):
    return dict(RULES)[name]


def test_rules_pick_their_points():
    flat = [(age, 1.0, 40_000, 200_000, 12_000, 300, 200) for age in range(2 * HOUR, 4 * DAY, 600)]
    i = rule("hidup hari 1")(flat)
    assert DAY <= flat[i][0] <= DAY + 600
    assert flat[rule("hidup hari 3")(flat)][0] >= 3 * DAY
    thin = [(a, p, 5_000, m, v, b, s) for a, p, _, m, v, b, s in flat]
    assert rule("hidup hari 1")(thin) is None  # not tradeable
    late = [p for p in flat if p[0] >= 30 * HOUR]  # picked up at 30 h (backfill): no day-1 sample
    assert rule("hidup hari 1")(late) is None and rule("hidup hari 2")(late) is not None
    falling = [(a, 2.0 if a < 18 * HOUR else 1.0, *rest) for a, _, *rest in flat]
    assert rule("naik di hari 1")(falling) is None and rule("naik di hari 1")(flat) is not None
    quiet = [(a, p, l, m, 500, b, s) for a, p, l, m, _, b, s in flat]
    assert rule("ramai di hari 1")(quiet) is None and rule("ramai di hari 1")(flat) is not None


def test_dip_rule_waits_for_the_bounce():
    def price(age):
        if age < 20 * HOUR:
            return 1.0
        if age < 30 * HOUR:
            return 0.5  # half its high
        return 0.6  # +20% off the low, 40% under the high
    points = [(a, price(a), 40_000, 200_000, 12_000, 300, 200) for a in range(12 * HOUR, 4 * DAY, 600)]
    i = rule("dip hari 1-3")(points)
    assert points[i][0] >= 30 * HOUR and points[i][1] == 0.6


def test_backtest_long_end_to_end(tmp_path, capsys):
    winners = [samples(f"W{i}", lambda a: 1.0 if a < DAY + HOUR else 2.0) for i in range(25)]
    losers = [samples(f"L{i}", lambda a: 1.0, dead_at_h=30) for i in range(25)]
    early_dead = [samples(f"D{i}", lambda a: 1.0, dead_at_h=5) for i in range(10)]
    young = [samples(f"Y{i}", lambda a: 1.0, end_h=26) for i in range(5)]  # bought on day 1, not finished yet
    write(str(tmp_path), *winners, *losers, *early_dead, *young)
    result = backtest_long(str(tmp_path), settings(tmp_path))
    assert result["tokens"] == 65
    survival = {s["label"]: s for s in result["survival"]}
    # 6 h and 1 day: all 65 are old enough to tell (the young ones reached 26 h); the 10 early dead are not alive
    assert survival["6 jam"]["n"] == 65 and survival["6 jam"]["pct"] == pytest.approx(55 / 65 * 100)
    assert survival["1 hari"]["n"] == 65 and survival["1 hari"]["pct"] == pytest.approx(55 / 65 * 100)
    # 2 days: the young ones are left out (too young); of the rest only the 25 winners are alive
    assert survival["2 hari"]["n"] == 60 and survival["2 hari"]["pct"] == pytest.approx(25 / 60 * 100)
    rows = {r["rule"]: r for r in result["rows"]}
    day1 = rows["hidup hari 1"]["1 hari"]
    assert day1["n"] == 50 and day1["win_pct"] == 50.0  # winners doubled, losers died
    assert result["unfinished"] >= 5
    text = format_backtest_long(result)
    assert "Backtest meme coin lama" in text and "Belum selesai (tidak dihitung)" in text
    assert max(len(line) for line in text.splitlines()) <= 60

    assert main(["backtest", "--lama", "--dir", str(tmp_path)]) == 0
    assert "Backtest meme coin lama" in capsys.readouterr().out


def test_backtest_long_without_data(tmp_path):
    result = backtest_long(str(tmp_path), settings(tmp_path))
    assert result["tokens"] == 0 and "Belum ada data token lama" in format_backtest_long(result)
