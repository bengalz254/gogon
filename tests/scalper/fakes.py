"""Test doubles for the scalper: candle builders, a scripted strategy and an
in-memory Binance USDⓈ-M futures exchange that speaks the REST protocol.

FakeExchange plugs in as the `requests.Session` of the REAL client, so tests
exercise URL building, HMAC signing (every signed request is verified), the
broker's order logic and the engine together. Moving the price with
`set_price` triggers stops and fills resting limit orders like the exchange.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import random
from urllib.parse import parse_qsl, urlsplit

from scalper.models import LONG, Candle, Signal
from scalper.strategies.base import Strategy

STEP = 300_000  # 5m
T0 = 1_700_006_400_000  # an exact hour boundary (UTC)
KEY = "test-key"
SECRET = "test-secret"


# ---------------------------------------------------------------------------
# candle builders
# ---------------------------------------------------------------------------
def bar(i: int, o: float, c: float, wick: float = 0.0006, t0: int = T0, step: int = STEP) -> Candle:
    hi = max(o, c) * (1 + wick)
    lo = min(o, c) * (1 - wick)
    t = t0 + i * step
    return Candle(t, o, hi, lo, c, 100.0, t + step - 1)


def ohlc(i: int, o: float, h: float, l: float, c: float, t0: int = T0, step: int = STEP) -> Candle:
    t = t0 + i * step
    return Candle(t, o, h, l, c, 100.0, t + step - 1)


def trend_pullback_sequence(sign: int = 1) -> list[Candle]:
    """Steady trend, a 5-bar pullback, then one strong resumption bar.

    With default trend_pullback settings this fires exactly one signal: on
    the last bar (LONG for sign=+1, SHORT for sign=-1).
    """
    out, p, i = [], 100.0, 0
    for k in range(720):
        c = p * (1 + sign * 0.0012 * (1.6 if k % 3 else -0.2))
        out.append(bar(i, p, c))
        p, i = c, i + 1
    for _ in range(5):
        c = p * (1 - sign * 0.004)
        out.append(bar(i, p, c))
        p, i = c, i + 1
    out.append(bar(i, p, p * (1 + sign * 0.008)))
    return out


def ranging_sequence(sign: int = 1) -> list[Candle]:
    """Noisy range, a 2-bar flush outside the band, then a snap-back bar.

    With default range_reversion settings this fires exactly one signal on
    the last bar (LONG for sign=+1, SHORT for sign=-1).
    """
    rnd = random.Random(1)
    out, p = [], 100.0
    for i in range(200):
        c = 100.0 * (1 + 0.002 * rnd.uniform(-1, 1))
        out.append(bar(i, p, c))
        p = c
    i = 200
    for _ in range(2):
        c = p * (1 - sign * 0.006)
        out.append(bar(i, p, c))
        p, i = c, i + 1
    out.append(bar(i, p, p * (1 + sign * 0.004)))
    return out


def flat(n: int, price: float = 100.0, start: int = 0) -> list[Candle]:
    return [ohlc(start + i, price, price * 1.001, price * 0.999, price) for i in range(n)]


class ScriptedStrategy(Strategy):
    """Emits pre-planned signals at given candle open times."""

    name = "scripted"

    def __init__(self, symbol: str, base_ms: int = STEP, plan: dict | None = None, atr: float = 0.5):
        super().__init__(symbol, base_ms)
        self.plan = plan if plan is not None else {}  # open_time -> (side, stop, tp_r)
        self._atr = atr

    @property
    def atr(self) -> float:
        return self._atr

    @property
    def warmup_bars(self) -> int:
        return 10

    def _on_candle(self, c: Candle):
        spec = self.plan.get(c.open_time)
        if spec is None:
            return None
        side, stop, tp_r = spec
        risk = abs(c.close - stop)
        tp = c.close + tp_r * risk if side == LONG else c.close - tp_r * risk
        return Signal(self.symbol, side, "scripted", c.close, stop, tp, self._atr, c.close_time, "test signal", tp_r)


# ---------------------------------------------------------------------------
# fake exchange
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, payload=None, headers: dict | None = None, text: str | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class ApiErr(Exception):
    def __init__(self, code: int, msg: str):
        self.code, self.msg = code, msg


CONDITIONAL = ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT", "TRAILING_STOP_MARKET")


class FakeExchange:
    def __init__(
        self,
        symbols=("BTCUSDT",),
        price: float = 100.0,
        balance: float = 1000.0,
        algo: bool = True,
        spread: float = 0.0001,
        taker: float = 0.0005,
        maker: float = 0.0002,
        min_notional: str = "5",
    ):
        self.now = T0
        # Testnet quirks seen in the wild: a MARKET order reports its filled
        # qty but an average price of 0 (market_zero_price), or comes back as
        # NEW and only shows as FILLED on a later query (market_async).
        self.market_zero_price = False
        self.market_async = False
        self.algo_supported = algo
        self.spread = spread
        self.taker, self.maker = taker, maker
        self.wallet = balance
        self.dual = False
        self.min_notional = min_notional
        self.prices = {s: price for s in symbols}
        self.pos = {s: [0.0, 0.0] for s in symbols}  # [amount, entry]
        self.candles: dict[str, list[Candle]] = {s: [] for s in symbols}
        self.orders: dict[int, dict] = {}
        self.algos: dict[int, dict] = {}
        self.fills: list[dict] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.inject: list = []  # hooks(method, path, params) -> FakeResponse | Exception | None
        self.margin: dict[str, str] = {}
        self.leverage: dict[str, int] = {}
        self._oid, self._aid, self._tid = 1000, 5000, 1
        self.routes = {
            ("GET", "/fapi/v1/time"): lambda p: {"serverTime": self.now},
            ("GET", "/fapi/v1/ping"): lambda p: {},
            ("GET", "/fapi/v1/exchangeInfo"): self._exchange_info,
            ("GET", "/fapi/v1/klines"): self._klines,
            ("GET", "/fapi/v1/ticker/bookTicker"): lambda p: {
                "symbol": p["symbol"], "bidPrice": str(self._bid(p["symbol"])), "askPrice": str(self._ask(p["symbol"]))
            },
            ("GET", "/fapi/v1/premiumIndex"): lambda p: {"symbol": p["symbol"], "markPrice": str(self.prices.get(p["symbol"], 300.0))},
            ("GET", "/fapi/v1/positionSide/dual"): lambda p: {"dualSidePosition": self.dual},
            ("POST", "/fapi/v1/positionSide/dual"): self._set_dual,
            ("GET", "/fapi/v1/multiAssetsMargin"): lambda p: {"multiAssetsMargin": False},
            ("POST", "/fapi/v1/marginType"): self._margin_type,
            ("POST", "/fapi/v1/leverage"): self._set_leverage,
            ("GET", "/fapi/v1/commissionRate"): lambda p: {
                "symbol": p["symbol"], "makerCommissionRate": str(self.maker), "takerCommissionRate": str(self.taker)
            },
            ("GET", "/fapi/v3/balance"): self._balance,
            ("GET", "/fapi/v3/positionRisk"): self._position_risk,
            ("POST", "/fapi/v1/order"): self._new_order,
            ("GET", "/fapi/v1/order"): self._get_order,
            ("DELETE", "/fapi/v1/order"): self._cancel_order,
            ("DELETE", "/fapi/v1/allOpenOrders"): self._cancel_all,
            ("GET", "/fapi/v1/openOrders"): lambda p: [
                dict(o) for o in self.orders.values() if o["status"] == "NEW" and o["symbol"] == p.get("symbol", o["symbol"])
            ],
            ("GET", "/fapi/v1/userTrades"): lambda p: [
                f for f in self.fills
                if f["symbol"] == p["symbol"] and f["time"] >= int(p.get("startTime", 0))
                and ("orderId" not in p or str(f["orderId"]) == p["orderId"])
            ],
            ("GET", "/fapi/v1/income"): lambda p: [],
        }
        if algo:
            self.routes.update({
                ("POST", "/fapi/v1/algoOrder"): self._new_algo,
                ("DELETE", "/fapi/v1/algoOrder"): self._cancel_algo,
                ("DELETE", "/fapi/v1/algoOpenOrders"): self._cancel_all_algo,
                ("GET", "/fapi/v1/openAlgoOrders"): lambda p: [
                    dict(a) for a in self.algos.values()
                    if a["algoStatus"] == "NEW" and a["symbol"] == p.get("symbol", a["symbol"])
                ],
            })

    # -- requests.Session interface ------------------------------------------
    def request(self, method, url, headers=None, timeout=None):
        parts = urlsplit(url)
        path, raw = parts.path, parts.query
        params = dict(parse_qsl(raw, keep_blank_values=True))
        self.calls.append((method, path, params))
        for hook in list(self.inject):
            r = hook(method, path, params)
            if r is not None:
                if isinstance(r, Exception):
                    raise r
                return r
        if "signature" in params:
            unsigned = raw.rsplit("&signature=", 1)[0] if "&signature=" in raw else ""
            expected = hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
            if params["signature"] != expected or (headers or {}).get("X-MBX-APIKEY") != KEY:
                return FakeResponse(400, {"code": -1022, "msg": "Signature for this request is not valid."})
        return self.handle(method, path, params)

    def handle(self, method, path, params):
        handler = self.routes.get((method, path))
        if handler is None:
            return FakeResponse(404, None, text="<html>Not Found</html>")
        try:
            return FakeResponse(200, handler(params), headers={"X-MBX-USED-WEIGHT-1M": "10"})
        except ApiErr as e:
            return FakeResponse(400, {"code": e.code, "msg": e.msg})

    # -- helpers ------------------------------------------------------------------
    def _bid(self, sym):
        return round(self.prices[sym] * (1 - self.spread / 2), 8)

    def _ask(self, sym):
        return round(self.prices[sym] * (1 + self.spread / 2), 8)

    def _next_oid(self):
        self._oid += 1
        return self._oid

    def open_regular(self, sym):
        return [o for o in self.orders.values() if o["symbol"] == sym and o["status"] == "NEW"]

    def open_algos(self, sym):
        return [a for a in self.algos.values() if a["symbol"] == sym and a["algoStatus"] == "NEW"]

    def _exchange_info(self, p):
        return {
            "symbols": [
                {
                    "symbol": s,
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "marginAsset": "USDT",
                    "quoteAsset": "USDT",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000000"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "120"},
                        {"filterType": "MIN_NOTIONAL", "notional": self.min_notional},
                    ],
                }
                for s in self.prices
            ]
        }

    def _klines(self, p):
        sym = p["symbol"]
        limit = int(p.get("limit", 500))
        start = int(p["startTime"]) if "startTime" in p else None
        end = int(p["endTime"]) if "endTime" in p else None
        rows = [c for c in self.candles[sym] if c.open_time <= self.now]
        if start is not None:
            rows = [c for c in rows if c.open_time >= start]
        if end is not None:
            rows = [c for c in rows if c.open_time <= end]
        rows = rows[:limit] if start is not None else rows[-limit:]
        return [[c.open_time, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume), c.close_time,
                 "0", 0, "0", "0", "0"] for c in rows]

    def _set_dual(self, p):
        want = p["dualSidePosition"] == "true"
        if want == self.dual:
            raise ApiErr(-4059, "No need to change position side.")
        self.dual = want
        return {"code": 200, "msg": "success"}

    def _margin_type(self, p):
        if self.margin.get(p["symbol"]) == p["marginType"]:
            raise ApiErr(-4046, "No need to change margin type.")
        self.margin[p["symbol"]] = p["marginType"]
        return {"code": 200, "msg": "success"}

    def _set_leverage(self, p):
        self.leverage[p["symbol"]] = int(p["leverage"])
        return {"symbol": p["symbol"], "leverage": int(p["leverage"]), "maxNotionalValue": "1000000"}

    def _balance(self, p):
        margin = sum(abs(a) * e / max(1, self.leverage.get(s, 1)) for s, (a, e) in self.pos.items())
        return [{"asset": "USDT", "balance": str(self.wallet), "availableBalance": str(self.wallet - margin)}]

    def _position_risk(self, p):
        out = []
        for s, (amt, entry) in self.pos.items():
            if amt == 0:
                continue
            mark = self.prices[s]
            upnl = (mark - entry) * amt
            out.append({"symbol": s, "positionAmt": str(amt), "entryPrice": str(entry), "markPrice": str(mark),
                        "unRealizedProfit": str(upnl), "liquidationPrice": "0", "positionSide": "BOTH"})
        return out

    # -- trading ------------------------------------------------------------------
    def _fill(self, sym, side, qty, price, maker, order):
        amt, entry = self.pos[sym]
        signed = qty if side == "BUY" else -qty
        realized = 0.0
        if amt == 0 or (amt > 0) == (signed > 0):
            new_amt = amt + signed
            entry = (abs(amt) * entry + qty * price) / abs(new_amt)
            amt = new_amt
        else:
            close_qty = min(qty, abs(amt))
            realized = (price - entry) * close_qty if amt > 0 else (entry - price) * close_qty
            amt = amt - close_qty if amt > 0 else amt + close_qty
            if abs(amt) < 1e-12:
                amt, entry = 0.0, 0.0
        fee = qty * price * (self.maker if maker else self.taker)
        self.wallet += realized - fee
        self.pos[sym] = [round(amt, 12), entry]
        self.fills.append({
            "symbol": sym, "id": self._tid, "orderId": order["orderId"], "side": side, "price": str(price),
            "qty": str(qty), "realizedPnl": str(realized), "commission": str(fee), "commissionAsset": "USDT",
            "time": self.now, "maker": maker, "buyer": side == "BUY", "positionSide": "BOTH",
        })
        self._tid += 1

    def _market_fill(self, sym, side, qty, order, price=None):
        px = price if price is not None else (self._ask(sym) if side == "BUY" else self._bid(sym))
        self._fill(sym, side, qty, px, False, order)
        order.update(status="FILLED", executedQty=str(qty), avgPrice=str(px), cumQuote=str(qty * px))

    def _new_order(self, p):
        sym, side, typ = p["symbol"], p["side"], p["type"]
        cid = p.get("newClientOrderId") or f"auto{self._oid}"
        if typ in CONDITIONAL:
            if self.algo_supported:
                raise ApiErr(-4120, "Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.")
            return self._conditional(p, sym, side, typ, float(p["stopPrice"]), cid, legacy=True)
        qty = float(p["quantity"])
        reduce = p.get("reduceOnly") == "true"
        amt = self.pos[sym][0]
        if reduce and (amt == 0 or (amt > 0) == (side == "BUY")):
            raise ApiErr(-2022, "ReduceOnly Order is rejected.")
        oid = self._next_oid()
        order = {
            "orderId": oid, "clientOrderId": cid, "symbol": sym, "side": side, "type": typ,
            "origQty": p["quantity"], "executedQty": "0", "avgPrice": "0", "cumQuote": "0",
            "price": p.get("price", "0"), "stopPrice": "0", "reduceOnly": reduce, "closePosition": False,
            "status": "NEW", "timeInForce": p.get("timeInForce", "GTC"), "updateTime": self.now,
        }
        self.orders[oid] = order
        if typ == "MARKET":
            self._market_fill(sym, side, min(qty, abs(amt)) if reduce else qty, order)
            if self.market_zero_price:
                order.update(avgPrice="0.00", cumQuote="0")
            if self.market_async:
                return dict(order, status="NEW", executedQty="0", avgPrice="0.00", cumQuote="0")
        return dict(order)

    def _conditional(self, p, sym, side, typ, trigger, cid, legacy):
        price = self.prices[sym]
        if typ == "STOP_MARKET" and ((side == "SELL" and price <= trigger) or (side == "BUY" and price >= trigger)):
            raise ApiErr(-2021, "Order would immediately trigger.")
        if typ == "TAKE_PROFIT_MARKET" and ((side == "SELL" and price >= trigger) or (side == "BUY" and price <= trigger)):
            raise ApiErr(-2021, "Order would immediately trigger.")
        close_pos = p.get("closePosition") == "true"
        if legacy:
            oid = self._next_oid()
            order = {"orderId": oid, "clientOrderId": cid, "symbol": sym, "side": side, "type": typ,
                     "stopPrice": str(trigger), "closePosition": close_pos, "status": "NEW", "reduceOnly": True,
                     "price": "0", "origQty": "0", "executedQty": "0", "avgPrice": "0"}
            self.orders[oid] = order
            return dict(order)
        self._aid += 1
        algo = {"algoId": self._aid, "clientAlgoId": cid, "algoType": "CONDITIONAL", "orderType": typ,
                "symbol": sym, "side": side, "positionSide": "BOTH", "quantity": p.get("quantity", "0"),
                "algoStatus": "NEW", "triggerPrice": str(trigger), "closePosition": close_pos,
                "workingType": p.get("workingType", "CONTRACT_PRICE")}
        self.algos[self._aid] = algo
        return dict(algo)

    def _new_algo(self, p):
        if p.get("algoType") != "CONDITIONAL":
            raise ApiErr(-1102, "Mandatory parameter 'algoType' was not sent.")
        return self._conditional(p, p["symbol"], p["side"], p["type"], float(p["triggerPrice"]), p.get("clientAlgoId"), False)

    def _find(self, p):
        if "orderId" in p:
            return self.orders.get(int(p["orderId"]))
        cid = p.get("origClientOrderId")
        for o in self.orders.values():
            if o["clientOrderId"] == cid:
                return o
        return None

    def _get_order(self, p):
        o = self._find(p)
        if o is None:
            raise ApiErr(-2013, "Order does not exist.")
        return dict(o)

    def _cancel_order(self, p):
        o = self._find(p)
        if o is None or o["status"] != "NEW":
            raise ApiErr(-2011, "Unknown order sent.")
        o["status"] = "CANCELED"
        return dict(o)

    def _cancel_all(self, p):
        for o in self.open_regular(p["symbol"]):
            o["status"] = "CANCELED"
        return {"code": 200, "msg": "The operation of cancel all open order is done."}

    def _cancel_algo(self, p):
        a = None
        if "algoId" in p:
            a = self.algos.get(int(p["algoId"]))
        else:
            a = next((x for x in self.algos.values() if x["clientAlgoId"] == p.get("clientAlgoId")), None)
        if a is None or a["algoStatus"] != "NEW":
            raise ApiErr(-2011, "Unknown order sent.")
        a["algoStatus"] = "CANCELED"
        return {"algoId": a["algoId"], "clientAlgoId": a["clientAlgoId"], "code": "200", "msg": "success"}

    def _cancel_all_algo(self, p):
        for a in self.open_algos(p["symbol"]):
            a["algoStatus"] = "CANCELED"
        return {"code": 200, "msg": "The operation of cancel all open order is done."}

    # -- market simulation -----------------------------------------------------------
    def set_price(self, sym: str, price: float) -> None:
        """Move the market to `price`, sweeping every level in between."""
        old = self.prices[sym]
        lo, hi = min(old, price), max(old, price)
        self.prices[sym] = price
        for kind, book, status_key, type_key, trig_key in (
            ("algo", self.algos, "algoStatus", "orderType", "triggerPrice"),
            ("legacy", self.orders, "status", "type", "stopPrice"),
        ):
            for o in list(book.values()):
                if o["symbol"] != sym or o[status_key] != "NEW" or o[type_key] not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                    continue
                trig = float(o[trig_key])
                if not (lo <= trig <= hi):
                    continue
                o[status_key] = "FINISHED" if kind == "algo" else "FILLED"
                amt = self.pos[sym][0]
                if amt == 0 or (amt > 0) == (o["side"] == "BUY"):
                    continue  # nothing to close
                mo = {"orderId": self._next_oid(), "clientOrderId": f"trig{o.get('clientAlgoId') or o.get('clientOrderId')}",
                      "symbol": sym, "side": o["side"], "type": "MARKET", "origQty": str(abs(amt)), "status": "NEW",
                      "reduceOnly": True, "price": "0", "stopPrice": "0", "closePosition": True}
                self.orders[mo["orderId"]] = mo
                self._market_fill(sym, o["side"], abs(amt), mo, price=trig)
        for o in list(self.orders.values()):
            if o["symbol"] != sym or o["status"] != "NEW" or o["type"] != "LIMIT":
                continue
            lp = float(o["price"])
            if not ((o["side"] == "SELL" and hi >= lp) or (o["side"] == "BUY" and lo <= lp)):
                continue
            amt = self.pos[sym][0]
            if o["reduceOnly"] and (amt == 0 or (amt > 0) == (o["side"] == "BUY")):
                o["status"] = "EXPIRED"
                continue
            qty = float(o["origQty"])
            if o["reduceOnly"]:
                qty = min(qty, abs(amt))
            self._fill(sym, o["side"], qty, lp, True, o)
            o.update(status="FILLED", executedQty=str(qty), avgPrice=str(lp))

    def play(self, sym: str, c: Candle, order: str = "ohlc", delay_ms: int = 2_500) -> None:
        """Let candle `c` form (price walks its path) and close; time moves past the close."""
        if not self.candles[sym] or self.candles[sym][-1].open_time < c.open_time:
            self.candles[sym].append(c)
        path = {"o": c.open, "h": c.high, "l": c.low, "c": c.close}
        self.now = max(self.now, c.open_time)
        for key in order:
            self.now += 1_000  # time only moves forward
            self.set_price(sym, path[key])
        self.now = c.close_time + 1 + delay_ms
