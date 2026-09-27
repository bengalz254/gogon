"""scripts/spot_check.py and scripts/spot_status.py, without network access."""
import csv
import importlib.util
import json
import os
import time

import pytest

from spot.market import TokocryptoData
from spot.records import TRADE_FIELDS
from test_spot_market import FakeCcxt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    module = load_script("spot_check")
    monkeypatch.setattr(module, "TokocryptoData", lambda symbol: TokocryptoData(symbol, FakeCcxt()))
    return module


def write_config(tmp_path, symbol="BTC/IDR"):
    path = tmp_path / "spot.yaml"
    path.write_text(f"symbol: {symbol}\n", encoding="utf-8")
    return str(path)


def test_check_passes_and_previews_the_grid(tmp_path, monkeypatch, capsys, check):
    codes = {"tokocrypto": 200, "binance": 451}
    monkeypatch.setattr(check.requests, "get", lambda url, timeout: Response(codes["binance" if "binance" in url else "tokocrypto"]))
    assert check.main(["--config", write_config(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "OK     Tokocrypto" in out
    assert "HTTP 451 — lokasi server (VPS) diblokir" in out  # reported, but BTC/IDR doesn't need Binance
    assert "Data harga dari: Tokocrypto" in out
    assert "Harga sekarang: bid Rp 1.599.000.000 | ask Rp 1.601.000.000" in out
    assert "otomatis ±10% dari harga sekarang" in out
    assert "untung bersih per putaran" in out and "Stop-loss: jual semua" in out
    assert "✅ Siap" in out


def test_check_fails_when_the_pairs_data_host_is_blocked(tmp_path, monkeypatch, capsys, check):
    monkeypatch.setattr(check.requests, "get", lambda url, timeout: Response(451 if "binance" in url else 200))
    (tmp_path / "spot.yaml").write_text(
        "symbol: BTC/USDT\ngrid:\n  order_value: 100\nrisk:\n  paper_balance: 5000\n", encoding="utf-8"
    )
    assert check.main(["--config", str(tmp_path / "spot.yaml")]) == 1
    out = capsys.readouterr().out
    assert "data pasangan ini lewat Binance" in out
    assert "❌ Perbaiki masalah" in out


def test_check_lists_pairs(check, capsys):
    assert check.main(["--symbols", "idr"]) == 0
    out = capsys.readouterr().out
    assert "BTC/IDR" in out and "BTC/USDT" not in out and "1 pasangan dengan IDR" in out


def write_status(data_dir, **extra):
    status = {
        "updated_ts": time.time() - 5,
        "running": True,
        "symbol": "BTC/IDR",
        "price": 1_600_000_000.0,
        "last_error": "",
        "grid": {"lower": 1.44e9, "upper": 1.76e9, "slots": 14, "buy_orders": 6, "sell_orders": 1},
        "holding": 0.00006,
        "quote_balance": 1_950_000.0,
        "equity": 2_004_000.0,
        "start_balance": 2_000_000.0,
        "started_at": "2026-09-27T00:00:00+00:00",
        "total_pnl": 4_000.0,
        "realized_pnl": 3_500.0,
        "unrealized_pnl": 500.0,
        "round_trips": 4,
        "fees": 900.0,
        "taxes": 1_300.0,
        "hodl_pnl": 25_000.0,
        "stopped": False,
    }
    status.update(extra)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")


def test_status_shows_pnl_the_buy_and_hold_comparison_and_trades(tmp_path, capsys):
    data_dir = tmp_path / "spot"
    write_status(data_dir)
    with open(data_dir / "trades.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        writer.writerow(dict(timestamp="t1", side="BUY", price="1587000000", amount="0.00006000", pnl="", note=""))
        writer.writerow(dict(timestamp="t2", side="SELL", price="1610000000", amount="0.00006000", pnl="850.4", note=""))
    status = load_script("spot_status")
    assert status.main(["--data-dir", str(data_dir)]) == 0
    out = capsys.readouterr().out
    assert "Bot grid BTC/IDR (PAPER) — jalan" in out
    assert "Nilai akun Rp 2.004.000 (Rp 4.000, +0.20% dari modal Rp 2.000.000)" in out
    assert "Putaran selesai: 4" in out
    assert "kalau modal dibelikan BTC semua sejak awal: Rp 25.000" in out
    assert "JUAL 0.00006 BTC @ Rp 1.610.000.000  untung Rp 850" in out


def test_status_explains_why_the_bot_could_not_start(tmp_path, capsys):
    data_dir = tmp_path / "spot"
    data_dir.mkdir()
    (data_dir / "status.json").write_text(
        json.dumps({"updated_ts": time.time(), "running": False, "error": "Modal kurang: ..."}), encoding="utf-8"
    )
    assert load_script("spot_status").main(["--data-dir", str(data_dir)]) == 1
    assert "Tidak bisa mulai: Modal kurang" in capsys.readouterr().out


def test_status_without_a_run_yet(tmp_path, capsys):
    assert load_script("spot_status").main(["--data-dir", str(tmp_path)]) == 1
    assert "belum pernah jalan" in capsys.readouterr().out
