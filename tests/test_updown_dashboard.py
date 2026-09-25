"""Dashboard: analysis helpers and the local HTTP server (no bot, no network)."""
import csv
import importlib.util
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from bot.updown.analysis import pnl_timeline, tail_csv
from bot.updown.journal import SETTLEMENT_FIELDS


def _load_dashboard():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "updown_dashboard.py")
    spec = importlib.util.spec_from_file_location("updown_dashboard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _settlements(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SETTLEMENT_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in SETTLEMENT_FIELDS})


def test_pnl_timeline_uses_latest_booked_row_per_window(tmp_path):
    rows = [
        {"window_id": "a", "start": "2026-09-25T08:00:00+00:00", "end": "2026-09-25T08:05:00+00:00",
         "event": "booked", "pnl_by_strategy": '{"fair_value": 2.0}'},
        {"window_id": "b", "end": "2026-09-25T08:05:00+00:00", "event": "booked", "pnl_by_strategy": '{"fair_value": -1.0}'},
        {"window_id": "a", "start": "2026-09-25T08:00:00+00:00", "end": "2026-09-25T08:05:00+00:00",
         "event": "official", "pnl_by_strategy": '{"fair_value": -3.0}'},
        {"window_id": "c", "end": "2026-09-25T08:10:00+00:00", "event": "provisional", "pnl_by_strategy": "{}"},
    ]
    tl = pnl_timeline(rows)
    assert [r["window_id"] for r in tl] == ["a", "b"]  # correction for "a" wins; unbooked "c" is left out
    assert tl[-1]["cum"] == -4.0
    assert tl[0]["start"] == tl[0]["end"] - 300  # the chart starts the line at $0 from here


def test_tail_csv_reads_only_the_end(tmp_path):
    path = tmp_path / "t.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["n", "text"])
        for i in range(5000):
            w.writerow([i, "x" * 50])
    rows = tail_csv(str(path), 3, max_bytes=2000)
    assert [r["n"] for r in rows] == ["4997", "4998", "4999"]


def test_dashboard_serves_page_and_json(tmp_path):
    status = {"ts": 1790000000.0, "mode": "paper", "windows": [], "feeds": [], "stats": {},
              "risk": {}, "exposure": {}, "results": []}
    (tmp_path / "updown_status.json").write_text(json.dumps(status), encoding="utf-8")
    _settlements(tmp_path / "updown_settlements.csv", [
        {"window_id": "btc-1", "asset": "btc", "end": "2026-09-25T08:05:00+00:00", "event": "booked",
         "pnl_by_strategy": '{"fair_value": 1.5}', "official_winner": "Up", "winner_twap_rule": "Up", "winner_last_rule": "Down"},
    ])
    dash = _load_dashboard()
    server = ThreadingHTTPServer(("127.0.0.1", 0), dash.make_handler(dash.DataSource(str(tmp_path))))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        html = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert "Pantau" in html and "/api/live" in html
        live = json.load(urllib.request.urlopen(base + "/api/live"))
        assert live["status"]["mode"] == "paper" and live["age"] > 0
        summary = json.load(urllib.request.urlopen(base + "/api/summary"))
        assert summary["by_strategy"]["fair_value"]["pnl"] == 1.5
        assert summary["rules"]["twap"] == {"ok": 1, "n": 1} and summary["rules"]["last"] == {"ok": 0, "n": 1}
    finally:
        server.shutdown()


def test_runner_writes_the_status_file(tmp_path):
    from bot.config import WalletConfig
    from bot.updown.config import UpDownConfig
    from bot.updown.runner import Runner

    cfg = UpDownConfig()
    cfg.journal.dir = str(tmp_path)
    cfg.journal.trades_csv = str(tmp_path / "trades.csv")
    runner = Runner(cfg, WalletConfig(None, 137, "http://127.0.0.1:1", 0, None, False))
    runner.write_status_file(runner.status_text())
    status = json.loads((tmp_path / "updown_status.json").read_text(encoding="utf-8"))
    assert status["mode"] == "paper" and status["loop_lag_ms"] == 0
    runner._note_loop_lag(3.0)
    assert json.loads(runner.status_text())["loop_lag_ms"] == 3000
    runner.journal.close()
