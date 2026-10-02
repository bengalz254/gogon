import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("dca_backtest", os.path.join(ROOT, "scripts", "dca_backtest.py"))
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

STEP = 900_000  # 15m


class FakeBinance:
    """Returns at most 1000 candles per call, like Binance futures."""

    rateLimit = 0

    def __init__(self, now):
        self.now = now
        self.calls = 0

    def milliseconds(self):
        return self.now

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.calls += 1
        start = since + (-since) % STEP
        out = []
        t = start
        while t <= self.now and len(out) < min(limit, 1000):
            out.append([t, 1, 2, 0.5, 1.5, 10])
            t += STEP
        return out


def test_download_pages_past_the_per_request_cap():
    now = 1_800_000_000_000 - (1_800_000_000_000 % STEP)
    ex = FakeBinance(now)
    rows = bt.download("SOL/USDT:USDT", "15m", 180, ex=ex)
    expected = 180 * 96  # 96 fifteen-minute candles per day
    assert abs(len(rows) - expected) <= 1
    assert ex.calls >= 18
    ts = [r[0] for r in rows]
    assert ts == sorted(set(ts))
    assert all(b - a == STEP for a, b in zip(ts, ts[1:]))
