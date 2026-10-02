from hlbot.data import HyperliquidInfo, load_csv, save_csv
from hlbot.strategy import Candle

from hl_helpers import SPAN_30M, T0


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    """Serves candleSnapshot pages of at most `page` candles, like the real API."""

    def __init__(self, n, page):
        self.rows = [
            {"t": T0 + i * SPAN_30M, "T": T0 + (i + 1) * SPAN_30M - 1, "s": "BTC", "i": "30m",
             "o": "1", "h": "2", "l": "0.5", "c": str(1 + i), "v": "3", "n": 1}
            for i in range(n)
        ]
        self.page = page
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append(json)
        if json["type"] == "allMids":
            return FakeResponse({"BTC": "65000.5"})
        req = json["req"]
        rows = [r for r in self.rows if req["startTime"] <= r["t"] <= req["endTime"]]
        return FakeResponse(rows[: self.page])


def test_candles_paginate_and_parse():
    session = FakeSession(n=12, page=5)
    info = HyperliquidInfo("https://example.invalid", session=session)
    candles = info.candles("BTC", "30m", T0, T0 + 20 * SPAN_30M)
    assert [c.t for c in candles] == [T0 + i * SPAN_30M for i in range(12)]
    assert candles[3].c == 4.0 and candles[0].h == 2.0
    assert len(session.calls) <= 4  # 3 pages + possibly one empty probe


def test_mid_price():
    info = HyperliquidInfo("https://example.invalid", session=FakeSession(1, 1))
    assert info.mid_price("BTC") == 65000.5


def test_csv_roundtrip_and_binance_format(tmp_path):
    candles = [Candle(t=T0 + i * SPAN_30M, o=1.0, h=2.0, l=0.5, c=1.5, v=10.0) for i in range(3)]
    p = tmp_path / "c.csv"
    save_csv(candles, str(p))
    assert load_csv(str(p)) == candles

    # headerless Binance export with microsecond timestamps (data.binance.vision, 2025+)
    b = tmp_path / "binance.csv"
    b.write_text(
        "\n".join(f"{(T0 + i * SPAN_30M) * 1000},1.0,2.0,0.5,1.5,10.0,0,0,0,0,0,0" for i in range(3)) + "\n"
    )
    assert load_csv(str(b)) == candles
