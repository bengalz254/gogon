"""Report numbers, the dashboard's HTTP API and the reset command."""
import json
import os
import threading
import time
import urllib.request

import pytest

from migbot.cli import main
from migbot.dashboard import make_server
from migbot.report import format_report, summarize
from migbot.storage import TRADE_FIELDS, CsvJournal, write_json_atomic
from migbot.tracker import token_fields

CPS = [5, 15, 60]


def fill(data_dir):
    tokens = CsvJournal(os.path.join(data_dir, "tokens.csv"), token_fields(CPS))
    rows = [
        ("dibeli", "", [50, 120, 80], 150, -10),
        ("dibeli", "", [-20, -40, -60], 5, -70),
        ("lolos, tak dibeli", "risiko: sudah 3 posisi terbuka (maks 3)", [10, 20, 30], 40, -5),
        ("ditolak", "likuiditas: $8.0k < $10.0k | top10 holder: 45% > 30%", [-10, -30, -50], 20, -60),
        ("ditolak", "likuiditas: $5.0k < $10.0k", [5, 10, -5], 30, -20),
        ("ditolak", "RugCheck: bahaya: Freeze Authority still enabled", [0, -5, -90], 1, -95),
        ("tanpa data", "tidak ada data pasar (DexScreener) selama jendela beli", [None, None, None], None, None),
    ]
    for i, (status, reasons, rets, mx, mn) in enumerate(rows):
        row = {"mint": f"M{i}", "symbol": f"T{i}", "status": status, "reasons": reasons,
               "max_ret_pct": "" if mx is None else mx, "min_ret_pct": "" if mn is None else mn,
               "rugcheck_danger": "Freeze Authority still enabled" if "Freeze" in reasons else ""}
        for cp, r in zip(CPS, rets):
            row[f"ret_{cp}m"] = "" if r is None else r
        tokens.append(row)
    trades = CsvJournal(os.path.join(data_dir, "trades.csv"), TRADE_FIELDS)
    trades.append({"time_utc": "2026-09-29 01:00:00", "mint": "M0", "symbol": "T0", "side": "BUY", "sol": "0.1005", "fee_sol": "0.0005", "method": "jupiter"})
    trades.append({"time_utc": "2026-09-29 01:10:00", "mint": "M0", "symbol": "T0", "side": "SELL", "reason": "take profit +100%",
                   "sol": "0.1", "pnl_sol": "0.05", "fee_sol": "0.0005", "method": "jupiter"})
    trades.append({"time_utc": "2026-09-29 01:20:00", "mint": "M0", "symbol": "T0", "side": "SELL", "reason": "trailing stop",
                   "sol": "0.08", "pnl_sol": "0.03", "position_pnl_sol": "0.08", "fee_sol": "0.0005", "method": "jupiter"})
    trades.append({"time_utc": "2026-09-29 02:00:00", "mint": "M1", "symbol": "T1", "side": "BUY", "sol": "0.1005", "fee_sol": "0.0005", "method": "estimasi"})
    trades.append({"time_utc": "2026-09-29 02:10:00", "mint": "M1", "symbol": "T1", "side": "SELL", "reason": "stop loss",
                   "sol": "0.06", "pnl_sol": "-0.0405", "position_pnl_sol": "-0.0405", "fee_sol": "0.0005", "method": "estimasi"})


def test_summary_numbers(tmp_path):
    fill(str(tmp_path))
    rep = summarize(str(tmp_path))
    assert rep["tokens_total"] == 7 and rep["no_data"] == 1
    assert rep["ret_columns"] == ["ret_5m", "ret_15m", "ret_60m"]
    bought, rejected, everything = rep["research"]["dibeli"], rep["research"]["ditolak"], rep["research"]["semua"]
    assert bought["n"] == 2 and rejected["n"] == 3 and everything["n"] == 6
    passed = rep["research"]["lolos filter"]
    assert passed["n"] == 3 and passed["columns"]["ret_60m"]["median"] == pytest.approx(30.0)
    assert bought["columns"]["ret_60m"]["median"] == pytest.approx(10.0)
    assert rejected["columns"]["ret_60m"]["median"] == pytest.approx(-50.0)
    assert rejected["columns"]["ret_5m"]["pct_up"] == pytest.approx(100 / 3)
    assert bought["pct_2x"] == 50.0 and rejected["pct_halved"] == pytest.approx(200 / 3)
    assert rep["reject_reasons"] == {"likuiditas": 2, "top10 holder": 1, "RugCheck": 1}
    assert rep["rugcheck_dangers"] == {"Freeze Authority still enabled": 1}
    p = rep["pnl"]
    assert p["buys"] == 2 and p["closed"] == 2 and p["wins"] == 1 and p["win_rate"] == 50.0
    assert p["realized_sol"] == pytest.approx(0.0395) and p["fees_sol"] == pytest.approx(0.0025)
    assert p["exit_reasons"] == {"take profit +100%": 1, "trailing stop": 1, "stop loss": 1}
    assert [pt["pnl"] for pt in p["timeline"]] == pytest.approx([0.05, 0.08, 0.0395])
    text = format_report(rep)
    assert "Filter memilih token yang lebih baik" in text and "Baru 2 posisi selesai" in text
    assert "likuiditas" in text and "take profit +100% 1x" in text


def test_empty_report(tmp_path):
    rep = summarize(str(tmp_path))
    assert rep["tokens_total"] == 0 and rep["pnl"]["closed"] == 0
    assert "Laporan" in format_report(rep)


@pytest.fixture
def dashboard(tmp_path):
    server = make_server(str(tmp_path), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield tmp_path, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read().decode()


def test_dashboard_serves_page_and_api(dashboard):
    data_dir, base = dashboard
    status, ctype, body = get(base + "/")
    assert status == 200 and "text/html" in ctype and "<title>Migrated Meme Bot</title>" in body
    assert "cdn" not in body.lower()  # fully offline page

    _, _, body = get(base + "/api/status")
    assert json.loads(body) == {"missing": True}

    write_json_atomic(os.path.join(data_dir, "status.json"), {"running": True, "updated_at": time.time(), "balance_sol": float("nan")})
    _, _, body = get(base + "/api/status")
    status_json = json.loads(body, parse_constant=lambda c: pytest.fail(c))
    assert status_json["running"] and status_json["stale"] is False and status_json["balance_sol"] is None

    fill(str(data_dir))
    _, ctype, body = get(base + "/api/report")
    rep = json.loads(body)
    assert "application/json" in ctype and rep["pnl"]["closed"] == 2
    assert rep["recent_trades"][0]["reason"] == "stop loss"  # newest first

    with pytest.raises(urllib.error.HTTPError):
        get(base + "/nope")


def test_reset_archives_data(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"data_dir: {tmp_path / 'data'}\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "trades.csv").write_text("x\n", encoding="utf-8")
    write_json_atomic(str(data / "status.json"), {"running": True, "updated_at": time.time()})
    assert main(["--config", str(cfg), "reset", "--yes"]) == 1  # refuses while the bot runs
    write_json_atomic(str(data / "status.json"), {"running": False, "updated_at": time.time()})
    assert main(["reset", "--config", str(cfg), "--yes"]) == 0
    assert not (data / "trades.csv").exists()
    archived = list((data / "archive").iterdir())
    assert len(archived) == 1 and (archived[0] / "trades.csv").exists()


def test_config_error_exits_with_code_2(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("filters:\n  min_liqudity_usd: 1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(cfg), "report"])
    assert exc.value.code == 2
