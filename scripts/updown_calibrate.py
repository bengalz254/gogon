"""Check the Up/Down pricing model against real price history.

Downloads Binance 1-minute candles (public, no key), replays every 5-minute
window, and at 4, 3, 2 and 1 minutes before close compares the model's
P(Up) with what actually happened. A well-calibrated model says "70%" on
windows that go Up about 70% of the time.

    python scripts/updown_calibrate.py --asset btc --days 7

This validates the model only. Whether Polymarket's prices are *worse*
than the model is a separate question, answered by paper trading.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from updown.config import BINANCE_SYMBOLS, ModelConfig  # noqa: E402
from updown.model import fair_prob_up, sigma_from_closes  # noqa: E402


def fetch_klines(host: str, symbol: str, days: int) -> list[tuple[int, float, float]]:
    """(open_time_s, open, close) for each 1m candle, oldest first."""
    end_ms = int(time.time() // 60 * 60 * 1000)
    start_ms = end_ms - days * 86_400_000
    out: list[tuple[int, float, float]] = []
    while start_ms < end_ms:
        resp = requests.get(
            f"{host}/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "startTime": start_ms, "limit": 1000},
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        out.extend((int(r[0]) // 1000, float(r[1]), float(r[4])) for r in rows)
        start_ms = int(rows[-1][0]) + 60_000
    return out


def evaluate(candles: list[tuple[int, float, float]], model: ModelConfig, vol_lookback: int = 60) -> list[tuple[int, float, int]]:
    """Return (seconds_left, predicted P(Up), outcome 1/0) samples."""
    by_time = {t: (o, c) for t, o, c in candles}
    closes = [c for _, _, c in candles]
    index = {t: i for i, (t, _, _) in enumerate(candles)}
    samples = []
    for t, o, _ in candles:
        if t % 300 != 0:
            continue
        minutes = [t + 60 * k for k in range(5)]
        if not all(m in by_time for m in minutes) or index[t] < vol_lookback:
            continue
        strike = o
        final = by_time[minutes[4]][1]
        outcome = 1 if final >= strike else 0
        i0 = index[t]
        sigma = sigma_from_closes(closes[i0 - vol_lookback : i0], 60.0) or model.vol_floor
        sigma = min(model.vol_cap, max(model.vol_floor, sigma))
        for k in range(1, 5):  # after minute k closes, 300 - 60k seconds remain
            spot = by_time[minutes[k - 1]][1]
            p = fair_prob_up(spot, strike, 300 - 60 * k, sigma, model.basis_sd, model.vol_multiplier)
            samples.append((300 - 60 * k, p, outcome))
    return samples


def report(samples: list[tuple[int, float, int]]) -> str:
    lines = []
    for left in sorted({s[0] for s in samples}, reverse=True):
        rows = [(p, y) for sl, p, y in samples if sl == left]
        n = len(rows)
        brier = sum((p - y) ** 2 for p, y in rows) / n
        base = sum((0.5 - y) ** 2 for _, y in rows) / n
        eps = 1e-6
        logloss = -sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for p, y in rows) / n
        lines.append(f"{left:3d}s left | n={n:5d} | Brier {brier:.4f} (coin flip {base:.4f}, skill {1 - brier / base:+.1%}) | log loss {logloss:.4f}")
    lines.append("\nCalibration (all checkpoints): predicted vs actual Up rate")
    for lo in [i / 10 for i in range(10)]:
        rows = [(p, y) for _, p, y in samples if lo <= p < lo + 0.1 or (lo == 0.9 and p == 1.0)]
        if rows:
            avg_p = sum(p for p, _ in rows) / len(rows)
            rate = sum(y for _, y in rows) / len(rows)
            lines.append(f"  {lo:.1f}-{lo + 0.1:.1f}: predicted {avg_p:.3f}  actual {rate:.3f}  (n={len(rows)})")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", default="btc", choices=sorted(BINANCE_SYMBOLS))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--host", default="https://api.binance.com")
    args = ap.parse_args()

    print(f"Downloading {args.days} days of 1m {BINANCE_SYMBOLS[args.asset]} candles...")
    candles = fetch_klines(args.host, BINANCE_SYMBOLS[args.asset], args.days)
    samples = evaluate(candles, ModelConfig())
    print(f"{len(samples) // 4} windows evaluated\n")
    print(report(samples))


if __name__ == "__main__":
    main()
