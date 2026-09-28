import os
import socket

import pytest

from scalper.cli import build_parser
from scalper.config import Settings
from scalper.dashboard import PAGE, build_summary, serve
from scalper.demo import synthetic_candles, write_demo_data
from scalper.journal import StateStore, TradeJournal

NOW = 1_760_000_000_000


def test_demo_data_is_separate_labelled_and_consistent(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path)
    paths = write_demo_data(s, NOW)
    rows = TradeJournal(paths["journal"]).read()
    assert len(rows) >= 20
    assert all(r["mode"] == "demo" and r["note"].startswith("DEMO") for r in rows)
    assert not (tmp_path / "trades_paper.csv").exists() and not (tmp_path / "state_paper.json").exists()
    state = StateStore(paths["state"]).peek()
    assert state["mode"] == "demo"
    total = sum(float(r["net_pnl"]) for r in rows)
    assert state["paper"]["balance"] == pytest.approx(s.paper.starting_balance + total, abs=1e-3)
    summary = build_summary(rows, state, "demo")
    assert summary["trades"] == len(rows) and summary["mode"] == "demo"


def test_demo_is_deterministic_and_regenerated_fresh(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path)
    first = TradeJournal(write_demo_data(s, NOW)["journal"]).read()
    second = TradeJournal(write_demo_data(s, NOW)["journal"]).read()  # overwrites, never appends
    assert [r["net_pnl"] for r in first] == [r["net_pnl"] for r in second]


def test_synthetic_market_has_no_built_in_trend():
    candles = synthetic_candles(100.0, 0.002, 1, 20_000, 300_000, 20_000 * 300_000)
    assert candles[-1].close_time == 20_000 * 300_000 - 1
    assert all(c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close) for c in candles)
    moves = [b.close / a.close - 1 for a, b in zip(candles, candles[1:])]
    mean = sum(moves) / len(moves)
    assert abs(mean) < 0.0001  # driftless: no hidden edge for a trend follower


def test_dashboard_shows_live_balance_before_first_trade():
    state = {"mode": "testnet", "account": {"balance": 4987.25, "available": 4990.0, "at": "2026-09-28 10:00:00"}}
    summary = build_summary([], state, "testnet")
    assert summary["balance"] == 4987.25
    assert summary["trades"] == 0 and summary["win_rate"] is None  # shown as "-", not "0.0%"


@pytest.mark.skipif(os.name == "nt", reason="Windows lets a second socket share the port")
def test_dashboard_explains_a_port_taken_by_another_program(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path)
    with socket.socket() as other:  # e.g. another bot's dashboard
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        port = other.getsockname()[1]
        with pytest.raises(SystemExit, match=f"port {port}.*--port {port + 1}"):
            serve(s, port=port, open_browser=False)


def test_dashboard_flags_demo_mode():
    assert "--demo" in build_parser().format_help() or build_parser().parse_args(["dashboard", "--demo"]).demo
    assert 'id="demo"' in PAGE and "bukan hasil trading" in PAGE
