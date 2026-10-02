"""LiveBroker against an in-memory fake of BinanceFutures (no network)."""
import itertools

import pytest

from dca.config import DcaSettings
from dca.ladder import LONG, SHORT
from dca.live import LiveBroker


class FakeExchange:
    def __init__(self):
        self.ids = itertools.count(1)
        self.orders: dict[str, dict] = {}
        self.positions = {LONG: 0.0, SHORT: 0.0}
        self.cancelled: list[str] = []

    def _new(self, kind, side, qty, price, **extra):
        oid = str(next(self.ids))
        self.orders[oid] = {"id": oid, "kind": kind, "side": side, "amount": qty, "price": price,
                            "status": "open", "filled": 0.0, "average": None, **extra}
        return self.orders[oid]

    def fill(self, oid, qty=None):
        o = self.orders[oid]
        qty = o["amount"] if qty is None else qty
        o["filled"] = qty
        o["average"] = o["price"]
        o["status"] = "closed" if qty >= o["amount"] else "open"
        sign = 1 if o["kind"] == "open" else -1
        self.positions[o["side"]] += sign * qty

    # BinanceFutures interface
    def position_qty(self, side): return round(self.positions[side], 8)
    def free_usdt(self): return 1000.0
    def amount(self, q): return round(q, 2)
    def price(self, p): return round(p, 3)
    def min_notional(self): return 5.0

    def market_open(self, side, qty):
        self.positions[side] += qty
        return {"id": str(next(self.ids)), "filled": qty, "average": 100.0}

    def market_close(self, side, qty):
        self.positions[side] -= qty
        return {"id": str(next(self.ids)), "filled": qty, "average": None}

    def limit_open(self, side, qty, price): return self._new("open", side, qty, price)
    def limit_close(self, side, qty, price): return self._new("close", side, qty, price)
    def stop_close(self, side, qty, stop): return self._new("stop", side, qty, stop)
    def fetch_order(self, oid, trigger=False): return dict(self.orders[oid])

    def cancel(self, oid, trigger=False):
        if oid and self.orders.get(oid, {}).get("status") == "open":
            self.orders[oid]["status"] = "canceled"
            self.cancelled.append(oid)


def make():
    s = DcaSettings(sides=[LONG])
    s.validate()
    ex = FakeExchange()
    return LiveBroker(s, ex), ex


def test_open_places_ladder_tp_and_sl():
    broker, ex = make()
    deal, events = broker.open_deal(LONG, 100.0, "t0")
    assert events[0].kind == "open"
    kinds = [o["kind"] for o in ex.orders.values()]
    assert kinds.count("open") == 6 and kinds.count("close") == 1 and kinds.count("stop") == 1
    assert ex.orders[deal.tp_order_id]["price"] == pytest.approx(101.0)
    assert ex.orders[deal.tp_order_id]["amount"] == pytest.approx(0.4)


def test_refuses_untracked_position():
    broker, ex = make()
    ex.positions[LONG] = 1.0
    deal, _ = broker.open_deal(LONG, 100.0, "t0")
    assert deal is None and ex.orders == {}


def test_safety_fill_moves_tp_then_tp_closes_and_cancels_rest():
    broker, ex = make()
    deal, _ = broker.open_deal(LONG, 100.0, "t0")
    old_tp = deal.tp_order_id
    ex.fill(deal.orders[1].order_id)
    events = broker.sync(deal, 98.5, 98.5, 98.5, "t1")
    assert [e.kind for e in events] == ["safety_order"]
    assert old_tp in ex.cancelled and deal.tp_order_id != old_tp
    new_tp = ex.orders[deal.tp_order_id]
    assert new_tp["amount"] == pytest.approx(ex.positions[LONG])
    assert new_tp["price"] < 101.0

    ex.fill(deal.tp_order_id)
    events = broker.sync(deal, 100, 100, 100, "t2")
    assert events[0].kind == "take_profit" and deal.status == "closed"
    assert deal.realized_pnl > 0
    assert all(o["status"] != "open" for o in ex.orders.values())


def test_bot_side_stop_loss_closes_position():
    broker, ex = make()
    deal, _ = broker.open_deal(LONG, 100.0, "t0")
    events = broker.sync(deal, 70, 70, 70, "t1")
    assert events[-1].kind == "stop_loss"
    assert ex.positions[LONG] == pytest.approx(0)
    assert all(o["status"] != "open" for o in ex.orders.values())


def test_external_close_detected():
    broker, ex = make()
    deal, _ = broker.open_deal(LONG, 100.0, "t0")
    ex.positions[LONG] = 0.0  # e.g. exchange-side SL or manual close
    events = broker.sync(deal, 99, 99, 99, "t1")
    assert events[-1].kind == "closed_external" and deal.status == "closed"
