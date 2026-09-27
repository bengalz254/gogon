"""The ccxt adapter for Tokocrypto market data, against a fake ccxt client."""
import pytest

from spot.market import CANDLE_MS, CLOSE_GRACE_MS, DataError, SymbolError, TokocryptoData, describe_error

MARKETS = {
    "BTC/IDR": {
        "symbol": "BTC/IDR",
        "base": "BTC",
        "quote": "IDR",
        "active": True,
        "spot": True,
        # ccxt reports Tokocrypto's tick size as a string
        "precision": {"price": "1000", "amount": 1e-05},
        "limits": {"amount": {"min": 1e-05}, "cost": {"min": 10000.0}},
        "info": {"type": 3},
    },
    "BTC/USDT": {
        "symbol": "BTC/USDT",
        "base": "BTC",
        "quote": "USDT",
        "active": True,
        "spot": True,
        "precision": {"price": 0.01, "amount": 1e-05},
        "limits": {"amount": {"min": 1e-05}, "cost": {"min": 5.0}},
        "info": {"type": 1},
    },
    "OLD/IDR": {"symbol": "OLD/IDR", "base": "OLD", "quote": "IDR", "active": False, "info": {"type": 3}},
}


class FakeCcxt:
    def __init__(self, candles=None):
        self.candles = candles or []
        self.ohlcv_calls = []

    def load_markets(self):
        return MARKETS

    def is_native_market(self, market):
        return market["info"]["type"] != 1

    def fetch_order_book(self, symbol, limit):
        assert limit == 5
        return {"bids": [[1_599_000_000, 0.1]], "asks": [[1_601_000_000, 0.2]]}

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        assert timeframe == "1m"
        self.ohlcv_calls.append(since)
        rows = [c for c in self.candles if c[0] >= since]
        return rows[:limit]


def test_rules_parse_ticks_limits_and_host():
    rules = TokocryptoData("BTC/IDR", FakeCcxt()).rules()
    assert (rules.tick, rules.step, rules.min_amount, rules.min_cost) == (1000.0, 1e-05, 1e-05, 10000.0)
    assert rules.native is True
    assert TokocryptoData("BTC/USDT", FakeCcxt()).rules().native is False


def test_unknown_or_inactive_pairs_are_symbol_errors_with_suggestions():
    with pytest.raises(SymbolError, match="BTC/IDR, BTC/USDT"):
        TokocryptoData("BTC/EUR", FakeCcxt()).rules()
    with pytest.raises(SymbolError, match="tidak aktif"):
        TokocryptoData("OLD/IDR", FakeCcxt()).rules()


def test_symbols_lists_active_pairs_by_quote():
    data = TokocryptoData("BTC/IDR", FakeCcxt())
    assert data.symbols("IDR") == [("BTC/IDR", True)]
    assert [s for s, _ in data.symbols()] == ["BTC/IDR", "BTC/USDT"]


def test_top_of_book():
    assert TokocryptoData("BTC/IDR", FakeCcxt()).top_of_book() == (1_599_000_000, 1_601_000_000)


def row(minute):
    ts = minute * CANDLE_MS
    return [ts, 100.0, 101.0 + minute, 99.0, 100.5]


def test_closed_candles_skip_the_forming_minute():
    data = TokocryptoData("BTC/IDR", FakeCcxt([row(m) for m in range(10)]))
    now_ms = 9 * CANDLE_MS + 30_000  # minute 9 is still forming
    candles = data.closed_candles(3 * CANDLE_MS, now_ms)
    assert [c.ts // CANDLE_MS for c in candles] == [3, 4, 5, 6, 7, 8]
    assert candles[0].high == 104.0


def test_a_candle_just_after_its_minute_waits_for_the_grace_period():
    data = TokocryptoData("BTC/IDR", FakeCcxt([row(0), row(1)]))
    assert [c.ts for c in data.closed_candles(0, CANDLE_MS + CLOSE_GRACE_MS - 1)] == []
    assert [c.ts for c in data.closed_candles(0, CANDLE_MS + CLOSE_GRACE_MS)] == [0]


def test_long_gaps_are_fetched_page_by_page(monkeypatch):
    import spot.market as market

    monkeypatch.setattr(market, "CANDLE_PAGE", 4)
    fake = FakeCcxt([row(m) for m in range(11)])
    candles = TokocryptoData("BTC/IDR", fake).closed_candles(0, 100 * CANDLE_MS)
    assert len(candles) == 11
    assert fake.ohlcv_calls == [0, 4 * CANDLE_MS, 8 * CANDLE_MS]


class Broken(FakeCcxt):
    def fetch_order_book(self, symbol, limit):
        raise RuntimeError("451 Service unavailable from a restricted location")


def test_request_failures_become_data_errors_with_a_location_hint():
    with pytest.raises(DataError, match="lokasi server"):
        TokocryptoData("BTC/IDR", Broken()).top_of_book()
    assert describe_error(TimeoutError()) == "TimeoutError"
