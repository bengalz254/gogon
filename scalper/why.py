"""Why the bot is (not) trading.

Every closed candle ends in exactly one outcome per symbol: an entry, or the
reason there was none (the strategy's own skip reason, or the risk layer's
rejection of its signal). This module keeps a rolling 24-hour tally of those
outcomes for the dashboard, `status` and the heartbeat, so "no trades for six
hours" can be answered with "the 1h trend was flat for five of them".

Read-only bookkeeping: nothing here changes a trading decision.
"""
from __future__ import annotations

import re
import time
from collections import Counter, deque

WINDOW_MS = 24 * 3_600_000

# First match wins. The patterns match the reasons produced by the
# strategies (Strategy._skip) and by risk.evaluate_entry / the engine.
_RULES: list[tuple[str, re.Pattern]] = [
    (code, re.compile(pattern))
    for code, pattern in (
        ("warmup", r"^warming up"),
        ("no_trend", r"no clear higher-timeframe trend"),
        ("weak_trend", r"^ADX "),
        ("trending", r"^trending \(ADX"),
        ("tf_disagree", r"base timeframe does not agree"),
        ("waiting_setup", r"setup incomplete|no band re-entry"),
        ("volatility", r"^ATR .*outside|^zero ATR"),
        ("spike", r"volatility spike"),
        ("bad_rr", r"max_sl_atr|reward:risk too small"),
        ("fee_filter", r"round-trip cost"),
        ("halted", r"^bot halted|daily loss limit|cooling down|max_trades_per_day"),
        ("busy", r"max_open_positions|symbol cooldown|already in a position"),
        ("order_failed", r"^entry |^no quote|old orders|stop-loss could not|bad fill"),
        ("late", r"^price already"),
        ("too_small", r"minimum|rounds to zero|liquidation|no equity|stop equals entry"),
    )
]

LABELS = {
    "warmup": "Pemanasan indikator",
    "no_trend": "Tren {htf} belum jelas naik/turun",
    "weak_trend": "Tren terlalu lemah (ADX rendah)",
    "trending": "Pasar sedang trending, strategi range menunggu",
    "tf_disagree": "Tren {tf} belum searah tren {htf}",
    "waiting_setup": "Menunggu setup lengkap (pullback & konfirmasi)",
    "volatility": "Volatilitas di luar batas strategi",
    "spike": "Candle lonjakan tajam, dilewati",
    "bad_rr": "Stop terlalu jauh untuk reward:risk",
    "fee_filter": "Ada sinyal, tapi stop terlalu kecil dibanding biaya",
    "halted": "Ada sinyal, ditahan batas risiko",
    "busy": "Ada sinyal, slot posisi penuh / jeda koin",
    "order_failed": "Ada sinyal, order gagal (lihat log)",
    "late": "Ada sinyal, harga sudah melewati stop",
    "too_small": "Ada sinyal, ukuran order tidak memenuhi syarat",
    "stale": "Sinyal kedaluwarsa (setelah jeda koneksi)",
    "signal": "Ada sinyal (sebelum bot menyala)",
    "entered": "Entry",
    "in_trade": "Sedang ada posisi",
    "other": "{reason}",
}

TREND_LABELS = {"LONG": "naik", "SHORT": "turun", "FLAT": "datar"}

# Outcomes in which the strategy did produce a signal.
SIGNAL_CODES = frozenset(
    {"entered", "signal", "fee_filter", "halted", "busy", "order_failed", "late", "too_small", "stale"}
)


def classify(reason: str) -> str:
    for code, pattern in _RULES:
        if pattern.search(reason or ""):
            return code
    return "other"


def label(code: str, tf: str = "5m", htf: str = "1h", reason: str = "") -> str:
    return LABELS.get(code, "{reason}").format(tf=tf, htf=htf, reason=reason or code)


class WhyTracker:
    """Rolling tally of candle outcomes per symbol."""

    def __init__(self, window_ms: int = WINDOW_MS):
        self.window_ms = window_ms
        self._events: dict[str, deque[tuple[int, str]]] = {}
        self.latest: dict[str, dict] = {}

    def record(self, symbol: str, time_ms: int, code: str, reason: str = "", extra: dict | None = None) -> None:
        events = self._events.setdefault(symbol, deque())
        events.append((time_ms, code))
        while events and events[0][0] <= time_ms - self.window_ms:
            events.popleft()
        self.latest[symbol] = {"code": code, "reason": reason, "time": time_ms, **(extra or {})}

    def counts(self, symbol: str) -> dict[str, int]:
        return dict(Counter(code for _, code in self._events.get(symbol, ())))

    def to_dict(self) -> dict:
        return {sym: {"latest": self.latest.get(sym, {}), "counts": self.counts(sym)} for sym in self._events}


def rows(why_state: dict | None) -> list[dict]:
    """Per-symbol summary of a saved tracker (the state file's "why"), in words."""
    w = why_state or {}
    tf, htf = w.get("tf") or "5m", w.get("htf") or "1h"
    fee_min = w.get("fee_min_pct")
    out = []
    for sym, info in sorted((w.get("symbols") or {}).items()):
        latest = info.get("latest") or {}
        counts = info.get("counts") or {}
        candles = sum(counts.values())
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
        max_stop = latest.get("max_stop_pct")
        at = latest.get("time") or 0
        out.append({
            "symbol": sym,
            "code": latest.get("code", ""),
            "now": label(latest["code"], tf, htf, latest.get("reason", "")) if latest.get("code") else "",
            "at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(at / 1000)) if at else "",
            "trend": TREND_LABELS.get(latest.get("trend", ""), ""),
            "adx": latest.get("adx"),
            "atr_pct": latest.get("atr_pct"),
            "max_stop_pct": max_stop,
            "calm": fee_min is not None and max_stop is not None and max_stop < fee_min,
            "candles": candles,
            "signals": sum(n for code, n in counts.items() if code in SIGNAL_CODES),
            "entries": counts.get("entered", 0),
            "breakdown": [
                {"code": code, "label": label(code, tf, htf), "n": n, "pct": round(n * 100 / candles)}
                for code, n in top
            ],
        })
    return out
