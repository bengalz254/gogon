"""Historical candles: download from Binance, cache as CSV, import CSV files.

The cache lives in data/scalper/klines/<SYMBOL>_<interval>.csv and only the
missing head/tail is downloaded on later runs. Files downloaded by hand from
https://data.binance.vision (futures/um/.../klines) can be passed to the
backtester directly with --csv; both their header and header-less layouts
(and microsecond timestamps) are understood.
"""
from __future__ import annotations

import csv
import logging
import os
import time
from typing import Callable

from scalper.models import Candle, interval_ms

logger = logging.getLogger("scalper.data")

CSV_HEADER = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
DAY_MS = 86_400_000


def parse_kline(row) -> Candle:
    """Binance kline array -> Candle."""
    return Candle(
        open_time=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        close_time=int(row[6]),
    )


def closed_only(candles: list[Candle], now_ms: int) -> list[Candle]:
    """Drop the still-forming candle (Binance returns it as the last kline)."""
    return [c for c in candles if c.close_time < now_ms]


def dedupe_sorted(candles: list[Candle]) -> list[Candle]:
    by_time = {c.open_time: c for c in candles}
    return [by_time[t] for t in sorted(by_time)]


def find_gaps(candles: list[Candle], step_ms: int) -> list[tuple[int, int]]:
    """(from_open_time, to_open_time) pairs where candles are missing."""
    gaps = []
    for a, b in zip(candles, candles[1:]):
        if b.open_time - a.open_time > step_ms:
            gaps.append((a.open_time, b.open_time))
    return gaps


def fetch_klines(
    client,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    now_ms: int | None = None,
    pause_s: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[int], None] | None = None,
) -> list[Candle]:
    """Page through /fapi/v1/klines (1500 per request) from start_ms to end_ms."""
    step = interval_ms(interval)
    out: list[Candle] = []
    cursor = start_ms
    while cursor <= end_ms:
        rows = client.klines(symbol, interval, limit=1500, start_time=cursor, end_time=end_ms)
        if not rows:
            break
        batch = [parse_kline(r) for r in rows]
        out.extend(batch)
        nxt = batch[-1].open_time + step
        if nxt <= cursor:
            break
        cursor = nxt
        if progress:
            progress(len(out))
        if len(rows) < 1500:
            break
        sleep(pause_s)
    if now_ms is not None:
        out = closed_only(out, now_ms)
    return dedupe_sorted(out)


def _to_ms(v: int) -> int:
    # data.binance.vision switched some datasets to microseconds.
    return v // 1000 if v > 10**14 else v


def load_csv(path: str, interval: str | None = None) -> list[Candle]:
    step = interval_ms(interval) if interval else None
    out: list[Candle] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 6:
                continue
            try:
                open_time = _to_ms(int(float(row[0])))
            except ValueError:
                continue  # header row
            if len(row) >= 7 and row[6].strip():
                close_time = _to_ms(int(float(row[6])))
            elif step:
                close_time = open_time + step - 1
            else:
                close_time = 0
            out.append(
                Candle(
                    open_time=open_time,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    close_time=close_time,
                )
            )
    out = dedupe_sorted(out)
    if out and not out[0].close_time and len(out) > 1:
        # No close_time column and no interval given: infer the interval.
        inferred = min(b.open_time - a.open_time for a, b in zip(out, out[1:]))
        for c in out:
            c.close_time = c.open_time + inferred - 1
    return out


def save_csv(path: str, candles: list[Candle]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for c in candles:
            w.writerow([c.open_time, repr(c.open), repr(c.high), repr(c.low), repr(c.close), repr(c.volume), c.close_time])
    os.replace(tmp, path)


def cache_path(cache_dir: str, symbol: str, interval: str) -> str:
    return os.path.join(cache_dir, f"{symbol}_{interval}.csv")


def load_history(
    client,
    symbol: str,
    interval: str,
    days: float,
    cache_dir: str,
    now_ms: int,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Candle]:
    """Return `days` of closed candles, downloading only what the cache lacks."""
    step = interval_ms(interval)
    path = cache_path(cache_dir, symbol, interval)
    cached = load_csv(path, interval) if os.path.exists(path) else []
    start = (now_ms - int(days * DAY_MS)) // step * step

    fetched: list[Candle] = []
    if not cached or cached[0].open_time > start:
        head_end = cached[0].open_time - 1 if cached else now_ms
        logger.info("%s %s: downloading %s -> %s", symbol, interval, _fmt(start), _fmt(head_end))
        fetched += fetch_klines(client, symbol, interval, start, head_end, now_ms, sleep=sleep)
    if cached and cached[-1].open_time + 2 * step <= now_ms:
        tail_start = cached[-1].open_time + step
        logger.info("%s %s: updating cache from %s", symbol, interval, _fmt(tail_start))
        fetched += fetch_klines(client, symbol, interval, tail_start, now_ms, now_ms, sleep=sleep)

    merged = dedupe_sorted(cached + fetched)
    if fetched:
        save_csv(path, merged)
    window = [c for c in merged if c.open_time >= start]
    gaps = find_gaps(window, step)
    if gaps:
        logger.info("%s %s: %d gap(s) in the data (exchange maintenance?)", symbol, interval, len(gaps))
    return window


def _fmt(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))
