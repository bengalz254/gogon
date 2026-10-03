"""`migbot status`: a health check of the running bot from the files it writes."""
import gzip
import os
import time

from migbot.cli import main
from migbot.status import collect, format_status
from migbot.storage import write_json_atomic
from migbot_fakes import settings

NOW = time.time()


def stamp(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def setup(tmp_path, updated_ago=5.0, written_ago=120.0, pumpportal=True):
    s = settings(tmp_path)
    os.makedirs(s.data_dir)
    os.makedirs(s.log_dir)
    write_json_atomic(os.path.join(s.data_dir, "status.json"), {
        "running": True, "started_at": NOW - 7200, "updated_at": NOW - updated_ago,
        "balance_sol": 10.0, "equity_sol": 10.0, "start_balance_sol": 10.0, "positions": [],
        "tokens": [{"mint": "A"}, {"mint": "B"}],
        "settings": {"trading_enabled": False, "long_tracked": 995},
        "feeds": [
            {"name": "PumpPortal", "connected": pumpportal, "event_count": 40, "last_event_at": NOW - 60},
            {"name": "DexScreener", "ok_count": 340, "error_count": 0, "consecutive_errors": 0},
            {"name": "GMGN", "ok_count": 0, "error_count": 1, "note": "diblokir oleh GMGN"},
            {"name": "Jupiter", "ok_count": 0, "error_count": 0},
        ],
    })
    with open(os.path.join(s.data_dir, "tokens.csv"), "w", encoding="utf-8") as fh:
        fh.write("mint,status\nA,ditolak\nB,ditolak\nC,ditolak\n")
    samples = os.path.join(s.data_dir, "long_samples.csv.gz")
    with gzip.open(samples, "wt", encoding="utf-8") as fh:
        fh.write("ts,mint\n")
    os.utime(samples, (NOW - written_ago, NOW - written_ago))
    with open(os.path.join(s.log_dir, "migbot.log"), "w", encoding="utf-8") as fh:
        fh.write(f"{stamp(NOW - 30 * 3600)} WARNING migbot.engine: too old to count\n")
        fh.write(f"{stamp(NOW - 3600)} INFO    migbot.engine: Multi-day tracking: 1715 tokens\n")
        fh.write(f"{stamp(NOW - 1800)} WARNING migbot.engine: Multi-day tracking: 1 DexScreener requests failed: x\n")
        fh.write("Traceback (most recent call last):\n")
        fh.write(f"{stamp(NOW - 600)} ERROR   migbot.engine: tick failed\n")
    return s


def test_healthy_bot(tmp_path, capsys):
    s = setup(tmp_path)
    result = collect(s, NOW)
    text = format_status(result)
    assert not result["problems"] and "✅ Semua baik." in text
    assert "✅ Bot jalan: diperbarui 5 dtk lalu, hidup 2.0 jam" in text
    assert "Mode riset: tidak membeli. Saldo 10.0000 SOL, P&L +0.0000 SOL" in text
    assert "✅ PumpPortal: tersambung, 40 migrasi, terakhir 60 dtk lalu" in text
    assert "⚠️ GMGN: 0 ok, 1 gagal (diblokir oleh GMGN)" in text and "• Jupiter: belum dipakai" in text
    assert "token baru dipantau (2 jam pertama): 2" in text and "token selesai dipantau: 3" in text
    assert "token lama diikuti: 995 (sampai 7 hari)" in text and "ditulis 2 mnt lalu" in text
    assert "⚠️ Peringatan di log 24 jam terakhir: 2" in text and "tick failed" in text and "too old" not in text
    s.config_path = None
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"data_dir: {s.data_dir}\nlog_dir: {s.log_dir}\n", encoding="utf-8")
    assert main(["--config", str(cfg), "status"]) == 0
    assert "Status migbot" in capsys.readouterr().out


def test_problems_are_listed(tmp_path):
    s = setup(tmp_path, updated_ago=300, written_ago=2 * 3600, pumpportal=False)
    text = format_status(collect(s, NOW))
    assert "❌ Bot macet: status tidak diperbarui 5 mnt" in text
    assert "❌ PumpPortal: terputus" in text and "❌ Catatan token lama" in text
    assert "❌ Ada masalah: bot macet, PumpPortal: terputus, 40 migrasi, terakhir 60 dtk lalu, catatan token lama tidak bertambah." in text


def test_bot_never_ran(tmp_path):
    s = settings(tmp_path)
    result = collect(s, NOW)
    assert "bot belum jalan" in result["problems"]
    assert "Bot belum pernah jalan" in format_status(result)
