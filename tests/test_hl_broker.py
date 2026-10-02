import pytest

import hyperliquid.exchange
import hyperliquid.info
from hlbot.broker import HyperliquidBroker, OrderError, _parse_order_response, round_price, round_size
from hlbot.strategy import LONG, SHORT

TEST_KEY = "0x" + "11" * 32  # throwaway key, never funded
ADDR = "0x000000000000000000000000000000000000dEaD"


def ok_filled(px, sz):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"filled": {"totalSz": str(sz), "avgPx": str(px), "oid": 1}}]}}}


class FakeInfo:
    def __init__(self, *a, **kw):
        self.position = None

    def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}

    def all_mids(self):
        return {"BTC": "65000"}

    def user_state(self, address):
        if self.position is None:
            return {"assetPositions": []}
        szi, entry = self.position
        return {"assetPositions": [{"position": {"coin": "BTC", "szi": str(szi), "entryPx": str(entry)}}]}


class FakeExchange:
    def __init__(self, wallet, base_url, account_address=None):
        self.calls = []

    def update_leverage(self, leverage, coin, is_cross=True):
        self.calls.append(("update_leverage", leverage, coin, is_cross))
        return {"status": "ok"}

    def market_open(self, coin, is_buy, sz, px, slippage):
        self.calls.append(("market_open", coin, is_buy, sz, slippage))
        return ok_filled(65010, sz)

    def market_close(self, coin, slippage):
        self.calls.append(("market_close", coin, slippage))
        return ok_filled(65500, 0.00308)

    def order(self, coin, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None):
        self.calls.append(("order", coin, is_buy, sz, limit_px, order_type, reduce_only, str(cloid)))
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 9}}]}}}

    def cancel_by_cloid(self, coin, cloid):
        self.calls.append(("cancel_by_cloid", coin, str(cloid)))
        return {"status": "ok"}


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setattr(hyperliquid.info, "Info", FakeInfo)
    monkeypatch.setattr(hyperliquid.exchange, "Exchange", FakeExchange)
    return HyperliquidBroker("BTC", TEST_KEY, ADDR, "https://example.invalid", max_slippage=0.01)


def test_rounding():
    assert round_price(65432.123, 5) == 65432.0
    assert round_price(1.234567, 2) == 1.2346
    assert round_size(0.0030769, 5) == 0.00308


def test_parse_order_response():
    assert _parse_order_response(ok_filled(100.5, 2)).price == 100.5
    assert _parse_order_response({"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}) is None
    with pytest.raises(OrderError):
        _parse_order_response({"status": "ok", "response": {"data": {"statuses": [{"error": "Insufficient margin"}]}}})
    with pytest.raises(OrderError):
        _parse_order_response({"status": "err", "response": "bad"})


def test_setup_rejects_leverage_above_max(broker):
    with pytest.raises(ValueError):
        broker.setup(50, is_cross=False)
    broker.setup(10, is_cross=False)
    assert broker.exchange.calls[-1] == ("update_leverage", 10, "BTC", False)


def test_open_sizes_from_notional(broker):
    fill = broker.open(LONG, 200.0)
    name, coin, is_buy, sz, slip = broker.exchange.calls[-1]
    assert (name, coin, is_buy, slip) == ("market_open", "BTC", True, 0.01)
    assert sz == round(200.0 / 65000, 5)
    assert fill.price == 65010


def test_position_parsing(broker):
    assert broker.get_position() is None
    broker.info.position = (-0.5, 64000)
    pos = broker.get_position()
    assert pos.side == SHORT and pos.size == 0.5 and pos.entry_price == 64000


def test_stop_order_placed_moved_and_cancelled_after_close(broker):
    broker.info.position = (0.00308, 65000)
    broker.sync_stop(LONG, 0.00308, 66000.0)
    first = broker.exchange.calls[-1]
    assert first[0] == "order" and first[2] is False and first[6] is True  # sell, reduce-only
    assert first[5] == {"trigger": {"triggerPx": 66000.0, "isMarket": True, "tpsl": "sl"}}
    first_cloid = broker.stop_cloid

    broker.sync_stop(LONG, 0.00308, 66010.0)  # < 0.1% move: no update
    assert broker.exchange.calls[-1] == first

    broker.sync_stop(LONG, 0.00308, 66300.0)  # new stop placed, THEN old one cancelled
    assert [c[0] for c in broker.exchange.calls[-2:]] == ["order", "cancel_by_cloid"]
    assert broker.exchange.calls[-1][2] == first_cloid

    broker.close()
    assert [c[0] for c in broker.exchange.calls[-2:]] == ["market_close", "cancel_by_cloid"]
    assert broker.stop_cloid is None
