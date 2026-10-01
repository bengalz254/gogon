"""`migbot backtest`: entry rules and the exit simulation on recorded price paths."""
import os

import pytest

from migbot.backtest import LOOSE_EXITS, RULES, backtest, format_backtest, simulate
from migbot.cli import main
from migbot.config import ExitConfig
from migbot.storage import CsvJournal, PathLog
from migbot.tracker import token_fields
from migbot_fakes import settings

COST = 0.03


def path(prices, start=30, step=10, liq=30_000, mcap=80_000, vol=9_000, buys=70, sells=40):
    """Points every `step` s from `start`; prices may be a list or a function of seconds."""
    if callable(prices):
        times = range(start, 7200, step)
        return [[t, prices(t), liq, mcap, vol, buys, sells] for t in times if prices(t)]
    return [[start + k * step, price, liq, mcap, vol, buys, sells] for k, price in enumerate(prices)]


def test_simulate_take_profit_then_trailing_stop():
    exits = ExitConfig()  # SL 25%, break-even +20%, +40% sell half, trailing 25% from +40%
    points = path([1.0, 1.5, 2.0, 1.4])
    eff = 1.0 * (1 + COST)
    expected = 0.5 * 1.5 / eff * (1 - COST) + 0.5 * 1.4 / eff * (1 - COST) - 1
    assert simulate(points, 0, exits, COST) == pytest.approx(expected)


def test_simulate_crash_between_points_is_sold_at_the_next_point():
    points = path([1.0, 0.05, 0.04])
    assert simulate(points, 0, ExitConfig(), COST) == pytest.approx(0.05 / (1 + COST) * (1 - COST) - 1)
    # nothing triggered before the path ends: sold at the last price
    flat = path([1.0, 1.05, 1.02])
    assert simulate(flat, 0, LOOSE_EXITS, COST) == pytest.approx(1.02 / (1 + COST) * (1 - COST) - 1)


def test_breakeven_stop_in_the_simulation():
    points = path([1.0, 1.3, 1.0, 0.5])
    # +30% arms the break-even stop; back at 1.0 (below the 1.03 paid) it sells there, not at 0.5
    assert simulate(points, 0, ExitConfig(), COST) == pytest.approx(1.0 / (1 + COST) * (1 - COST) - 1)


def rules(points, feat=None, s=None):
    s = s or settings_for_rules
    return {name: rule(points, feat or {}, s) for name, rule in RULES}


settings_for_rules = None


@pytest.fixture(autouse=True)
def _settings(tmp_path):
    global settings_for_rules
    settings_for_rules = settings(tmp_path)


def test_rules_pick_their_points():
    busy = path(lambda t: 1.0, buys=900, sells=400)
    picked = rules(busy)
    assert picked["ramai menit 5"] is not None and busy[picked["ramai menit 5"]][0] >= 300
    assert picked["sepi menit 5"] is None and picked["acak menit 5"] == picked["ramai menit 5"]

    calm = path(lambda t: 1.0, buys=60, sells=40)
    spread = {"top50_pct": "18.5", "wallets_1pct": "1"}
    assert rules(calm, spread)["sepi+tersebar"] == rules(calm)["sepi menit 5"]
    assert rules(calm, {"top50_pct": "40", "wallets_1pct": "1"})["sepi+tersebar"] is None
    assert rules(path(lambda t: 1.0, liq=300))["acak menit 5"] is None  # not a real pool

    survivor = path(lambda t: 1.0 if t < 1800 else 0.6)
    assert survivor[rules(survivor)["bertahan 30m"]][0] >= 1800
    assert rules(path(lambda t: 1.0 if t < 1800 else 0.4))["bertahan 30m"] is None


def test_trending_late_waits_for_minute_30_and_a_price_near_its_high():
    points = path(lambda t: 1.0 if t < 2400 else 0.8, buys=200, sells=150)
    i = rules(points)["trending 30-60m"]
    assert i is not None and 1800 <= points[i][0] < 2400
    falling = path(lambda t: 1.0 if t < 1200 else 0.5, buys=200, sells=150)  # half its high: not trending up
    assert rules(falling)["trending 30-60m"] is None


def test_bot_rule_buys_the_dip_bounce_with_the_filters():
    def dip(t):
        if t < 120:
            return 0.0001
        if t < 240:
            return 0.00015
        if t < 600:
            return 0.00009
        return 0.0001

    points = path(dip)
    i = rules(points)["aturan bot"]
    assert 600 <= points[i][0] <= 610
    assert rules(points, {"holders": "120"})["aturan bot"] is None  # holder check from the snapshot
    assert rules(points, {"rugcheck_danger": "Freeze Authority still enabled"})["aturan bot"] is None


def write_data(data_dir, tokens):
    journal = CsvJournal(os.path.join(data_dir, "tokens.csv"), token_fields([5, 15, 60]))
    log = PathLog(os.path.join(data_dir, "paths.jsonl.gz"))
    for i, (source, points) in enumerate(tokens):
        journal.append({"mint": f"M{i}", "source": source, "status": "ditolak"})
        log.append({"mint": f"M{i}", "symbol": f"T{i}", "migrated_at": 0, "status": "ditolak", "points": points})


def test_backtest_end_to_end_and_cli(tmp_path, capsys):
    winners = [("pumpportal", path(lambda t: 1.0 if t < 400 else 2.5, buys=60, sells=40)) for _ in range(40)]
    losers = [("pumpportal", path(lambda t: 1.0 if t < 400 else 0.05, buys=900, sells=400)) for _ in range(40)]
    junk = [("geckoterminal", path(lambda t: 1.0, liq=20, buys=0, sells=0)) for _ in range(25)]
    write_data(str(tmp_path / "data"), winners + losers + junk)
    s = settings(tmp_path)
    result = backtest(str(tmp_path / "data"), s)
    assert result["tokens"] == 80 and result["junk"] == 25
    rows = {r["rule"]: r for r in result["rows"]}
    assert rows["sepi menit 5"]["ketat"]["n"] == 40 and rows["sepi menit 5"]["ketat"]["win_pct"] == 100
    assert rows["ramai menit 5"]["ketat"]["n"] == 40 and rows["ramai menit 5"]["ketat"]["avg_pct"] < -90
    assert result["best"]["rule"] in ("sepi menit 5", "bertahan 30m") and result["best"]["avg_pct"] > 0
    text = format_backtest(result)
    assert "Backtest: aturan beli pada 80 migrasi" in text and "Tidak dihitung: 25 pool sampah." in text
    assert "Terbaik:" in text and max(len(line) for line in text.splitlines()) <= 60

    assert main(["backtest", "--dir", str(tmp_path / "data")]) == 0
    assert "Backtest" in capsys.readouterr().out
    assert main(["backtest", "--dir", str(tmp_path / "missing")]) == 1


def test_backtest_without_paths(tmp_path):
    result = backtest(str(tmp_path), settings(tmp_path))
    assert result["tokens"] == 0 and "Belum ada catatan harga" in format_backtest(result)
