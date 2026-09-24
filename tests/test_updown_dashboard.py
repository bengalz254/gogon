import pytest
import json

from scripts.updown_dashboard import StateSource
from tests.test_updown_engine import T0, FakeGateway, make, price_path, run
from updown.state import build_state, write_state


def test_state_snapshot_is_complete_and_serializable(tmp_path):
    engine, history = make(FakeGateway())
    run(engine, history, price_path, T0 - 5, T0 + 300 + 200)  # one full window + into the next
    state = build_state(engine, float(T0 + 300 + 200))
    json.dumps(state)  # must be plain JSON

    a = state["assets"][0]
    assert a["window_start"] == T0 + 300 and a["market_found"]
    assert a["strike"] == 100.0 and a["spot"] == 100.1
    assert a["p_up_low"] <= a["p_up"] <= a["p_up_high"]
    assert set(a["edges"]) == {"Up", "Down"} and a["books"]["Up"]["ask"] == 0.62
    assert a["track"] and a["track"][-1][1] == 100.1
    assert state["stats"]["windows_traded"] == 1
    assert state["recent_windows"][-1]["result"] == "win"
    assert state["recent_windows"][-1]["cum_pnl"] == state["stats"]["pnl_usd"]
    kinds = {e["kind"] for e in state["events"]}
    assert "BUY" in kinds and "SETTLE" in kinds


def test_skipped_window_shows_why(tmp_path):
    engine, history = make(FakeGateway())
    run(engine, history, price_path, T0 + 100, T0 + 110)
    a = build_state(engine, float(T0 + 110))["assets"][0]
    assert a["skip_reason"] and a["p_up"] is None and a["books"] is None


def test_dashboard_reads_state_file(tmp_path):
    path = str(tmp_path / "state.json")
    src = StateSource(path)
    assert src.payload()["status"] == "no_bot"

    engine, history = make(FakeGateway())
    run(engine, history, price_path, T0 - 5, T0 + 100)
    write_state(path, build_state(engine, float(T0 + 100)))
    p = src.payload()
    assert p["status"] == "online" and p["state"]["assets"][0]["strike"] == 100.0


def test_report_summarises_journal(tmp_path, capsys, monkeypatch):
    import scripts.updown_report as report
    from bot.journal import TradeJournal

    engine, history = make(FakeGateway())
    engine.journal = TradeJournal(str(tmp_path / "trades.csv"))
    run(engine, history, price_path, T0 - 5, T0 + 300 + 20)
    monkeypatch.setattr(report, "TRADES", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(report, "LOG", str(tmp_path / "none.log"))
    monkeypatch.setattr("sys.argv", ["updown_report.py"])
    report.main()
    out = capsys.readouterr().out
    total = [l for l in out.splitlines() if l.startswith("TOTAL")][0].split()
    assert total[1:4] == ["1", "1", "0"]
    assert float(total[5]) == pytest.approx(engine.stats.pnl_usd, abs=0.01)
    assert float(total[6]) > 0  # model expected a profit on these entries
