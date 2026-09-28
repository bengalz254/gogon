from decimal import Decimal

import pytest
import requests
from fakes import KEY, SECRET, FakeExchange, FakeResponse

from scalper.config import Settings
from scalper.exchange import (
    BinanceAPIError,
    BinanceBroker,
    BinanceFuturesClient,
    BrokerError,
    OrderStatusUnknown,
    PaperBroker,
    RateLimited,
    parse_symbol_rules,
)
from scalper.models import LONG, SHORT, Candle, PositionInfo
from scalper.trade import Trade


class ScriptSession:
    """Returns queued responses; records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, timeout=None):
        self.requests.append((method, url, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_client(session, **kw):
    sleeps = []
    client = BinanceFuturesClient("https://x", KEY, SECRET, session=session, clock=lambda: 1_000.0,
                                  sleep=sleeps.append, **kw)
    return client, sleeps


# -- client ---------------------------------------------------------------------------
def test_signature_matches_binance_documentation_example():
    client = BinanceFuturesClient("https://x", "k", "2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9")
    qs = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943"
    assert client.sign(qs) == "3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9"


def test_signed_request_layout():
    session = ScriptSession([FakeResponse(200, {"ok": True})])
    client, _ = make_client(session)
    client.request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT", "reduceOnly": True, "price": None}, signed=True)
    method, url, headers = session.requests[0]
    assert method == "POST" and headers["X-MBX-APIKEY"] == KEY
    query = url.split("?", 1)[1]
    assert query.startswith("symbol=BTCUSDT&reduceOnly=true&recvWindow=5000&timestamp=1000000&signature=")
    assert "price" not in query


def test_timestamp_error_resyncs_clock_and_retries_once():
    session = ScriptSession([
        FakeResponse(400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}),
        FakeResponse(200, {"serverTime": 1_003_000}),
        FakeResponse(200, [{"asset": "USDT"}]),
    ])
    client, _ = make_client(session)
    assert client.request("GET", "/fapi/v2/balance", signed=True) == [{"asset": "USDT"}]
    assert client.time_offset_ms == 3_000
    assert "timestamp=1003000" in session.requests[2][1]


def test_rate_limit_waits_retry_after_then_retries_reads():
    session = ScriptSession([FakeResponse(429, {"code": -1003, "msg": "Too many requests"}, headers={"Retry-After": "7"}),
                             FakeResponse(200, {"serverTime": 1})])
    client, sleeps = make_client(session)
    assert client.request("GET", "/fapi/v1/time") == {"serverTime": 1}
    assert sleeps == [7.0]


def test_ip_ban_raises():
    session = ScriptSession([FakeResponse(418, {"code": -1003, "msg": "banned"}, headers={"Retry-After": "120"})])
    client, _ = make_client(session)
    with pytest.raises(RateLimited) as e:
        client.request("GET", "/fapi/v1/time")
    assert e.value.retry_after == 120


def test_writes_are_never_blindly_retried():
    for failure in (FakeResponse(503, None, text="busy"), requests.Timeout("slow"),
                    requests.exceptions.ChunkedEncodingError("cut off"),
                    FakeResponse(200, {"code": -1007, "msg": "Timeout waiting for response from backend server."})):
        session = ScriptSession([failure])
        client, _ = make_client(session)
        with pytest.raises(OrderStatusUnknown):
            client.request("POST", "/fapi/v1/order", {"symbol": "X"}, signed=True)
        assert len(session.requests) == 1


def test_reads_are_retried_on_network_and_server_errors():
    session = ScriptSession([requests.ConnectionError("down"), FakeResponse(502, None, text="bad gateway"),
                             FakeResponse(200, {"serverTime": 5})])
    client, sleeps = make_client(session)
    assert client.request("GET", "/fapi/v1/time") == {"serverTime": 5}
    assert len(sleeps) == 2


def test_api_error_carries_code():
    session = ScriptSession([FakeResponse(400, {"code": -2019, "msg": "Margin is insufficient."})])
    client, _ = make_client(session)
    with pytest.raises(BinanceAPIError) as e:
        client.request("POST", "/fapi/v1/order", {}, signed=True)
    assert e.value.code == -2019 and not e.value.missing_endpoint


def test_versioned_endpoint_falls_back_and_caches():
    session = ScriptSession([FakeResponse(404, None, text="<html>"), FakeResponse(200, [{"symbol": "BTCUSDT"}]),
                             FakeResponse(200, [])])
    client, _ = make_client(session)
    assert client.position_risk() == [{"symbol": "BTCUSDT"}]
    assert "/fapi/v2/positionRisk" in session.requests[1][1]
    client.position_risk()
    assert "/fapi/v2/positionRisk" in session.requests[2][1]  # remembered


def test_weight_header_throttles():
    session = ScriptSession([FakeResponse(200, {}, headers={"X-MBX-USED-WEIGHT-1M": "2000"})])
    client, sleeps = make_client(session)
    client.request("GET", "/fapi/v1/ping")
    assert len(sleeps) == 1 and sleeps[0] > 0


# -- broker against the fake exchange ------------------------------------------------------
def make_broker(fake: FakeExchange, **exec_overrides):
    s = Settings()
    s.symbols = list(fake.prices)
    for k, v in exec_overrides.items():
        setattr(s.execution, k, v)
    client = BinanceFuturesClient("https://fake", KEY, SECRET, session=fake, clock=lambda: fake.now / 1000,
                                  sleep=lambda x: None)
    rules = parse_symbol_rules(client.exchange_info(), s.symbols)
    return BinanceBroker(client, s, rules, sleep=lambda x: None), s


def open_long(fake, broker, qty="1"):
    return broker.market_order("BTCUSDT", "BUY", Decimal(qty))


def test_prepare_sets_account_and_reads_fees():
    fake = FakeExchange()
    broker, _ = make_broker(fake)
    assert broker.prepare(["BTCUSDT"]) == []
    assert fake.margin["BTCUSDT"] == "ISOLATED" and fake.leverage["BTCUSDT"] == 5
    assert broker.fee_rates["BTCUSDT"] == (0.0002, 0.0005)
    broker.prepare(["BTCUSDT"])  # second time: -4046 "no need to change" is fine


def test_prepare_refuses_hedge_mode():
    fake = FakeExchange()
    fake.dual = True
    broker, _ = make_broker(fake)
    with pytest.raises(BrokerError, match="Hedge Mode"):
        broker.prepare(["BTCUSDT"])
    broker2, _ = make_broker(fake, auto_one_way_mode=True)
    assert "One-way" in broker2.prepare(["BTCUSDT"])[0] and fake.dual is False


def test_market_order_and_positions():
    fake = FakeExchange()
    broker, _ = make_broker(fake)
    fill = open_long(fake, broker, "1.5")
    assert fill.qty == 1.5 and fill.avg_price == pytest.approx(100.005)
    pos = broker.positions()["BTCUSDT"]
    assert pos.qty == 1.5 and pos.side == LONG


def test_stop_goes_to_algo_api_with_close_position_rounded_away():
    fake = FakeExchange()
    broker, _ = make_broker(fake)
    open_long(fake, broker)
    ref = broker.place_stop("BTCUSDT", LONG, 98.999)
    algo = fake.open_algos("BTCUSDT")[0]
    assert ref.kind == "algo" and algo["orderType"] == "STOP_MARKET" and algo["side"] == "SELL"
    assert algo["triggerPrice"] == "98.99" and algo["closePosition"] is True
    assert algo["workingType"] == "MARK_PRICE"
    fake.set_price("BTCUSDT", 98.5)
    assert fake.pos["BTCUSDT"][0] == 0  # stop closed the position


def test_stop_falls_back_to_classic_orders_on_older_servers():
    fake = FakeExchange(algo=False)
    broker, _ = make_broker(fake)
    open_long(fake, broker)
    ref = broker.place_stop("BTCUSDT", LONG, 99.0)
    assert ref.kind == "regular" and fake.open_regular("BTCUSDT")[0]["type"] == "STOP_MARKET"
    assert broker._cond_api == "legacy"
    assert [o.purpose for o in broker.open_orders("BTCUSDT")] == ["sl"]


def test_switches_to_algo_when_classic_endpoint_refuses_conditional_orders():
    fake = FakeExchange(algo=True)
    broker, _ = make_broker(fake)
    broker._cond_api = "legacy"
    open_long(fake, broker)
    ref = broker.place_stop("BTCUSDT", LONG, 99.0)
    assert ref.kind == "algo" and broker._cond_api == "algo"


def test_take_profit_is_reduce_only_limit_rounded_toward_entry():
    fake = FakeExchange()
    broker, _ = make_broker(fake)
    open_long(fake, broker, "2")
    ref = broker.place_take_profit("BTCUSDT", LONG, 101.519, Decimal("2"))
    order = fake.open_regular("BTCUSDT")[0]
    assert ref.purpose == "tp" and order["type"] == "LIMIT" and order["price"] == "101.51"
    assert order["reduceOnly"] is True and order["timeInForce"] == "GTC"


def test_cancel_all_clears_regular_and_algo_orders():
    fake = FakeExchange()
    broker, _ = make_broker(fake)
    open_long(fake, broker)
    broker.place_stop("BTCUSDT", LONG, 99.0)
    broker.place_take_profit("BTCUSDT", LONG, 101.5, Decimal("1"))
    assert len(broker.open_orders("BTCUSDT")) == 2
    assert broker.cancel_all("BTCUSDT") == []
    assert fake.open_regular("BTCUSDT") == [] and fake.open_algos("BTCUSDT") == []


def test_timed_out_market_order_is_looked_up_not_resent():
    fake = FakeExchange()
    broker, _ = make_broker(fake)
    state = {"done": False}

    def hook(method, path, params):
        if method == "POST" and path == "/fapi/v1/order" and not state["done"]:
            state["done"] = True
            fake.handle(method, path, params)  # the exchange DID execute it...
            return requests.Timeout("...but the response never arrived")
        return None

    fake.inject.append(hook)
    fill = open_long(fake, broker)
    assert fill.qty == 1.0
    assert fake.pos["BTCUSDT"][0] == 1.0  # exactly one position, no duplicate
    posts = [c for c in fake.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
    assert len(posts) == 1


def test_timed_out_order_that_never_arrived_counts_as_not_filled():
    fake = FakeExchange()
    broker, _ = make_broker(fake)
    fake.inject.append(lambda m, p, q: requests.Timeout("lost") if m == "POST" else None)
    fill = open_long(fake, broker)
    assert fill.qty == 0.0 and fake.pos["BTCUSDT"][0] == 0


def test_close_position_and_closed_trade_info():
    fake = FakeExchange()
    broker, _ = make_broker(fake)
    fill = open_long(fake, broker, "2")
    trade = Trade("t", "BTCUSDT", LONG, "x", 2.0, fill.avg_price, 99.0, 99.0, 101.5, fake.now,
                  entry_order_id=fill.order_id)
    fake.now += 60_000
    fake.set_price("BTCUSDT", 101.0)
    broker.close_position("BTCUSDT", broker.positions()["BTCUSDT"])
    info = broker.closed_trade_info("BTCUSDT", trade)
    exit_px = 101.0 * (1 - 0.00005)
    assert info.exit_price == pytest.approx(exit_px)
    assert info.gross_pnl == pytest.approx((exit_px - 100.005) * 2)
    assert info.fees == pytest.approx(2 * 100.005 * 0.0005 + 2 * exit_px * 0.0005)
    assert info.approximate is False


def test_closed_trade_info_waits_for_fills():
    fake = FakeExchange()
    broker, _ = make_broker(fake)
    trade = Trade("t", "BTCUSDT", LONG, "x", 1.0, 100.0, 99.0, 99.0, 101.5, fake.now)
    assert broker.closed_trade_info("BTCUSDT", trade) is None


# -- paper broker ------------------------------------------------------------------------------
def paper(quote=(99.99, 100.01)):
    s = Settings()
    s.costs.slippage_bps = 1
    return PaperBroker(s, None, quote_fn=lambda sym: quote, clock=lambda: 1_000.0), s


def test_paper_market_fill_and_stop():
    broker, s = paper()
    fill = broker.market_order("BTCUSDT", "BUY", Decimal("2"))
    assert fill.avg_price == pytest.approx(100.01 * 1.0001)
    assert broker.balance == pytest.approx(1000 - fill.fee)
    broker.place_stop("BTCUSDT", LONG, 99.0)
    broker.on_candle("BTCUSDT", Candle(1_000_000, 100, 100.2, 98.8, 99.1, 1, 1_299_999))
    assert broker.positions() == {}
    info = broker.closed_trade_info("BTCUSDT", None)
    assert info.exit_price == pytest.approx(99.0 * (1 - 0.0001))
    assert broker.balance == pytest.approx(1000 + info.gross_pnl - info.fees)


def test_paper_limit_target_uses_maker_fee_and_short_side():
    broker, s = paper()
    broker.market_order("BTCUSDT", "SELL", Decimal("1"))
    broker.place_take_profit("BTCUSDT", SHORT, 98.0, Decimal("1"))
    broker.on_candle("BTCUSDT", Candle(1_000_000, 99, 99.5, 97.9, 98.2, 1, 1_299_999))
    info = broker.closed_trade_info("BTCUSDT", None)
    assert info.exit_price == 98.0
    assert info.fees == pytest.approx(99.99 * (1 - 0.0001) * 0.0005 + 98.0 * 0.0002)


def test_paper_ignores_candles_before_entry_and_persists():
    broker, s = paper()
    broker.market_order("BTCUSDT", "BUY", Decimal("1"))
    broker.place_stop("BTCUSDT", LONG, 99.0)
    broker.on_candle("BTCUSDT", Candle(0, 100, 100, 50, 60, 1, 299_999))  # closed before the entry
    assert "BTCUSDT" in broker.positions()
    again, _ = paper()
    again.load_dict(broker.to_dict())
    assert again.positions()["BTCUSDT"].qty == 1 and again.open_orders("BTCUSDT")[0].purpose == "sl"
    again.close_position("BTCUSDT", PositionInfo("BTCUSDT", 1, 100))
    assert again.positions() == {}
