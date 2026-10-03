"""`python -m migbot status`: is the running bot healthy, and is the research data still growing?

Reads only what the bot writes (status.json, tokens.csv, the multi-day samples and the log file), so it
runs next to the bot at any time and changes nothing.
"""
from __future__ import annotations

import os
import shutil
import time

from migbot.config import Settings
from migbot.filters import fmt_dur
from migbot.storage import read_csv, read_json

GOOD, WARN, BAD = "✅", "⚠️", "❌"
STALE_S = 60  # status.json is rewritten every few seconds; older means the bot stopped or hangs
SAMPLES_STALE_S = 30 * 60  # multi-day samples are written every 10 minutes
WARN_WINDOW_S = 24 * 3600
LOW_DISK = 2e9
LOW_MEMORY = 200e6
SHOW_WARNINGS = 3


def _feed(f: dict, now: float) -> tuple[str, str]:
    """(mark, text) for one data source in status.json."""
    name = f.get("name") or "?"
    if name == "PumpPortal":
        mark = GOOD if f.get("connected") else BAD
        text = f"{name}: {'tersambung' if f.get('connected') else 'terputus'}, {f.get('event_count') or 0} migrasi"
        if f.get("last_event_at"):
            text += f", terakhir {fmt_dur(now - f['last_event_at'])} lalu"
        return mark, text
    oks, fails = f.get("ok_count") or 0, f.get("error_count") or 0
    if not oks and not fails:
        return "•", f"{name}: belum dipakai"
    text = f"{name}: {oks} ok, {fails} gagal"
    if f.get("note"):
        return WARN, f"{text} ({f['note']})"
    if (f.get("consecutive_errors") or 0) >= 3:
        return BAD, f"{text}, gagal terus: {(f.get('last_error') or '')[:80]}"
    return GOOD, text


def _log_warnings(path: str, now: float) -> tuple[int, list[str]]:
    """Warnings and errors in the bot's log file from the last 24 hours: (count, the latest few)."""
    count, latest = 0, []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split(" ", 3)  # date, time, level, "name: message"
                if len(parts) < 4 or parts[2] not in ("WARNING", "ERROR", "CRITICAL"):
                    continue
                try:
                    ts = time.mktime(time.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
                if now - ts > WARN_WINDOW_S:
                    continue
                count += 1
                latest.append(f"{parts[1][:5]} {parts[3].split(': ', 1)[-1].strip()[:90]}")
    except OSError:
        pass
    return count, latest[-SHOW_WARNINGS:]


def _memory_available() -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _gb(value: float) -> str:
    return f"{value / 1e9:.1f} GB" if value >= 1e9 else f"{value / 1e6:.0f} MB"


def collect(s: Settings, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    data = s.data_dir
    lines: list[tuple[str, str]] = []
    problems: list[str] = []

    def add(mark: str, text: str, problem: str = "") -> None:
        lines.append((mark, text))
        if mark == BAD:
            problems.append(problem or text)

    status = read_json(os.path.join(data, "status.json"))
    if not isinstance(status, dict):
        add(BAD, "Bot belum pernah jalan dengan folder data ini", "bot belum jalan")
        status = {}
    else:
        age = now - float(status.get("updated_at") or 0)
        if status.get("running") and age < STALE_S:
            add(GOOD, f"Bot jalan: diperbarui {fmt_dur(age)} lalu, hidup {fmt_dur(now - float(status.get('started_at') or now))}")
        elif status.get("running"):
            add(BAD, f"Bot macet: status tidak diperbarui {fmt_dur(age)}", "bot macet")
        else:
            add(BAD, f"Bot berhenti {fmt_dur(age)} lalu", "bot berhenti")
        settings = status.get("settings") or {}
        pnl = float(status.get("equity_sol") or 0) - float(status.get("start_balance_sol") or 0)
        mode = "Mode riset: tidak membeli" if settings.get("trading_enabled") is False else f"{len(status.get('positions') or [])} posisi terbuka"
        lines.append(("", f"   {mode}. Saldo {float(status.get('balance_sol') or 0):.4f} SOL, P&L {pnl:+.4f} SOL"))
        for f in status.get("feeds") or []:
            mark, text = _feed(f, now)
            add(mark, text)

    lines.append(("", ""))
    lines.append(("", "Data riset"))
    settings = status.get("settings") or {}
    lines.append(("", f"   token baru dipantau (2 jam pertama): {len(status.get('tokens') or [])}"))
    lines.append(("", f"   token selesai dipantau: {len(read_csv(os.path.join(data, 'tokens.csv')))}"))
    if s.long_tracking.enabled:
        lines.append(("", f"   token lama diikuti: {settings.get('long_tracked', '?')} (sampai {s.long_tracking.max_days:g} hari)"))
        path = os.path.join(data, "long_samples.csv.gz")
        if not os.path.exists(path):
            add(BAD, "Catatan token lama belum ada", "catatan token lama tidak bertambah")
        else:
            written = now - os.path.getmtime(path)
            text = f"Catatan token lama: {_gb(os.path.getsize(path))}, ditulis {fmt_dur(written)} lalu"
            add(GOOD if written < SAMPLES_STALE_S else BAD, text, "catatan token lama tidak bertambah")

    lines.append(("", ""))
    count, latest = _log_warnings(os.path.join(s.log_dir, "migbot.log"), now)
    lines.append((GOOD if not count else WARN, f"Peringatan di log 24 jam terakhir: {count}"))
    lines.extend(("", f"   {w}") for w in latest)
    free = shutil.disk_usage(data).free if os.path.isdir(data) else None
    if free is not None:
        add(GOOD if free >= LOW_DISK else BAD, f"Disk kosong: {_gb(free)}", "disk hampir penuh")
    memory = _memory_available()
    if memory is not None:
        add(GOOD if memory >= LOW_MEMORY else BAD, f"RAM tersedia: {_gb(memory)}", "RAM hampir habis")
    return {"lines": lines, "problems": problems, "now": now}


def format_status(result: dict) -> str:
    out = ["=" * 46, f" Status migbot  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(result['now']))} UTC", "=" * 46]
    for mark, text in result["lines"]:
        out.append(f"{mark} {text}" if mark else text)
    out.append("")
    if result["problems"]:
        out.append(f"{BAD} Ada masalah: {', '.join(result['problems'])}.")
    else:
        out.append(f"{GOOD} Semua baik.")
    return "\n".join(out)
