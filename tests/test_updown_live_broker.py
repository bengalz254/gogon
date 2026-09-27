"""Live broker against a fake py-clob-client (no network, no keys)."""
import pytest

from bot.updown.broker_live import LiveBroker, parse_order_status, parse_post_response
from bot.updown.fees import FeeModel
from bot.updown.orders import OrderRequest, OrderState


def req(tif="FAK", price=0.62, shares=10.0, post_only=False):
    return OrderRequest(client_id="c1", window_id="w", strategy="s", token_id="111", outcome="Up",
                        price=price, shares=shares, tif=tif, post_only=post_only, tick_size=0.01)


class FakeClient:
    def __init__(self, post_response):
        self.post_response = post_response
        self.created = []
        self.posted = []
        self.cancelled = []

    def create_order(self, args, options=None):
        self.created.append((args, options))
        return {"signed": args}

    def post_order(self, order, order_type, post_only=False):
        self.posted.append((order, order_type, post_only))
        return self.post_response

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        return {"canceled": [order_id], "not_canceled": {}}

    def get_order(self, order_id):
        return {"id": order_id, "status": "LIVE", "size_matched": "4", "original_size": "10"}


def test_fak_matched_response_becomes_fills():
    u = parse_post_response(req(), {"success": True, "orderID": "0x1", "status": "matched",
                                    "makingAmount": "5.9", "takingAmount": "10"}, FeeModel(), 1.0)
    assert u.final and u.status == "filled"
    assert u.fills[0].shares == 10 and u.fills[0].price == pytest.approx(0.59)
    assert u.fills[0].fee > 0 and not u.fills[0].is_maker


def test_other_post_responses():
    fees = FeeModel()
    assert parse_post_response(req("GTC", post_only=True), {"success": True, "orderID": "0x2", "status": "live"},
                               fees, 1.0).status == "open"
    assert parse_post_response(req(), {"success": True, "orderID": "0x3", "status": "unmatched"}, fees, 1.0).final
    rej = parse_post_response(req(), {"success": False, "errorMsg": "not enough balance"}, fees, 1.0)
    assert rej.status == "rejected" and "balance" in rej.message


def test_order_status_polling_reports_incremental_fills():
    st = OrderState(req=req("GTC", price=0.95), filled=1.0)
    u = parse_order_status(st, {"status": "LIVE", "size_matched": "4"}, FeeModel(), 2.0)
    assert u.fills[0].shares == pytest.approx(3.0) and u.fills[0].is_maker and not u.final
    done = parse_order_status(st, {"status": "CANCELED", "size_matched": "1"}, FeeModel(), 3.0)
    assert done.final and done.status == "cancelled" and not done.fills


def test_live_broker_uses_fak_and_post_only_correctly():
    from py_clob_client.clob_types import OrderType

    client = FakeClient({"success": True, "orderID": "0xabc", "status": "live"})
    broker = LiveBroker(client, FeeModel())
    u = broker.submit(req("GTC", price=0.957, shares=5.129, post_only=True))
    args, opts = client.created[0]
    assert args.price == 0.957 and args.size == 5.12 and args.side == "BUY"
    assert opts.tick_size == "0.01"
    assert client.posted[0][1] == OrderType.GTC and client.posted[0][2] is True
    assert u.status == "open" and u.exchange_id == "0xabc"
    assert broker.cancel("c1", "0xabc").final
    assert client.cancelled == ["0xabc"]
