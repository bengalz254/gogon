"""End-to-end engine scenarios: real Engine + BinanceBroker/PaperBroker + real
REST client, talking to the in-memory FakeExchange."""
import pytest
import requests
from fakes import KEY, SECRET, STEP, FakeExchange, FakeResponse, ScriptedStrategy, flat, ohlc

from scalper.config import Credentials, Settings
from scalper.engine import Engine, MarketData
from scalper.exchange import BinanceBroker, BinanceFuturesClient, BrokerError, PaperBroker, parse_symbol_rules
from scalper.journal import StateStore, TradeJournal
from scalper.models import LONG, SHORT

SYM = "BTCUSDT"
HISTORY = 120


class Harness:
    def __init__(self, tmp_path, mode="testnet", algo=True, plan=None, settings=None, fake=None):
        self.tmp = tmp_path
        self.fake = fake or FakeExchange(algo=algo)
        if fake is None:
            self.fake.candles[SYM] = flat(HISTORY)
            self.fake.now = self.fake.candles[SYM][-1].close_time + 1 + 2_500
        self.mode = mode
        self.plan = plan if plan is not None else {}
        self.settings = settings or self.default_settings(mode)
        self.engine = self.build()

    @staticmethod
    def default_settings(mode):
        s = Settings()
        s.mode = mode
        s.symbols = [SYM]
        s.execution.warmup_bars = 100
        s.risk.min_sl_cost_ratio = 0
        s.notify.heartbeat_minutes = 0
        s.credentials = Credentials(api_key=KEY, api_secret=SECRET, base_url="https://fake", public_base_url="https://fake")
        return s

    def build(self):
        fake, s = self.fake, self.settings
        client = BinanceFuturesClient("https://fake", KEY, SECRET, session=fake,
                                      clock=lambda: fake.now / 1000, sleep=lambda x: None)
        client.sync_time()
        rules = parse_symbol_rules(client.exchange_info(), s.symbols)
        market = MarketData(client, s.timeframe)
        if self.mode == "paper":
            broker = PaperBroker(s, rules, quote_fn=market.quote, clock=lambda: fake.now / 1000)
        else:
            broker = BinanceBroker(client, s, rules, sleep=lambda x: None)
        engine = Engine(
            s, broker, market, rules,
            journal=TradeJournal(str(self.tmp / f"trades_{self.mode}.csv")),
            store=StateStore(str(self.tmp / f"state_{self.mode}.json")),
            sleep=lambda x: None,
        )
        engine.strategies[SYM] = ScriptedStrategy(SYM, plan=self.plan)
        return engine

    def next_index(self):
        return len(self.fake.candles[SYM])

    def signal_next(self, side=LONG, stop=99.0, tp_r=1.5):
        """Schedule a signal on the next candle to close."""
        t = self.fake.candles[SYM][-1].open_time + STEP
        self.plan[t] = (side, stop, tp_r)

    def play(self, o, h, l, c, order="ohlc", tick=True):
        candle = ohlc(self.next_index(), o, h, l, c)
        self.fake.play(SYM, candle, order)
        if tick:
            self.engine.tick()
        return candle

    def journal(self):
        return self.engine.journal.read()


def open_long(h: Harness):
    h.engine.start()
    h.signal_next(LONG, stop=99.0)
    h.play(100, 100.1, 99.9, 100)
    return h.engine.trades[SYM]


def test_entry_places_exchange_side_stop_and_limit_target(tmp_path):
    h = Harness(tmp_path)
    trade = open_long(h)
    fake = h.fake
    assert fake.pos[SYM][0] == pytest.approx(trade.qty) and trade.side == LONG
    assert trade.entry_price == pytest.approx(100.005)
    assert trade.take_profit == pytest.approx(100.005 + 1.5 * (100.005 - 99.0))
    algo = fake.open_algos(SYM)
    assert len(algo) == 1 and algo[0]["orderType"] == "STOP_MARKET"
    assert algo[0]["reduceOnly"] is True and float(algo[0]["quantity"]) == pytest.approx(trade.qty)
    tp = fake.open_regular(SYM)
    assert len(tp) == 1 and tp[0]["type"] == "LIMIT" and tp[0]["reduceOnly"] is True
    # risk: 0.5% of 1000 = 5 USDT including costs
    assert trade.qty * (trade.entry_price - 99.0) < 5.0
    assert h.engine.guard.s.trades_today == 1
    # state was persisted
    assert SYM in StateStore(str(tmp_path / "state_testnet.json")).peek()["trades"]


def test_stop_hit_is_settled_from_real_fills_and_leftovers_cancelled(tmp_path):
    h = Harness(tmp_path)
    trade = open_long(h)
    qty = trade.qty
    h.play(100, 100.1, 98.9, 99.2)  # sweeps through the 99.0 stop
    assert SYM not in h.engine.trades
    assert h.fake.open_regular(SYM) == [] and h.fake.open_algos(SYM) == []  # take-profit cancelled
    [row] = h.journal()
    assert row["exit_reason"] == "SL" and float(row["exit_price"]) == pytest.approx(99.0)
    expected = (99.0 - 100.005) * qty - qty * 100.005 * 0.0005 - qty * 99.0 * 0.0005
    assert float(row["net_pnl"]) == pytest.approx(expected, abs=1e-4)
    assert float(row["net_pnl"]) > -5.0  # never worse than the planned risk here
    assert h.fake.wallet == pytest.approx(1000 + expected, abs=1e-4)
    assert h.engine.guard.s.loss_streak == 1


def test_target_hit_pays_maker_fee_and_cancels_stop(tmp_path):
    h = Harness(tmp_path)
    trade = open_long(h)
    h.play(100.1, 101.7, 100.0, 101.6)
    [row] = h.journal()
    assert row["exit_reason"] == "TP"
    assert float(row["exit_price"]) == pytest.approx(101.51)  # target rounded toward entry
    assert trade.take_profit > 101.51
    tp = float(h.fake.fills[-1]["price"])
    assert tp == pytest.approx(101.51) and h.fake.fills[-1]["maker"] is True
    assert h.fake.open_algos(SYM) == []
    assert float(row["net_pnl"]) > 0


def test_breakeven_places_new_stop_before_cancelling_old(tmp_path):
    h = Harness(tmp_path)
    trade = open_long(h)
    old_id = trade.sl_order.order_id
    h.fake.calls.clear()
    h.play(100.0, 101.2, 99.9, 101.1)  # +1.2R, closes well above breakeven
    t = h.engine.trades[SYM]
    assert t.stop_kind == "BE"
    assert t.stop == pytest.approx(100.005 * (1 + h.settings.costs.round_trip_cost))
    live = h.fake.open_algos(SYM)
    assert len(live) == 1 and str(live[0]["algoId"]) != old_id
    posts = [i for i, c in enumerate(h.fake.calls) if c[:2] == ("POST", "/fapi/v1/algoOrder")]
    deletes = [i for i, c in enumerate(h.fake.calls) if c[:2] == ("DELETE", "/fapi/v1/algoOrder")]
    assert posts and deletes and posts[0] < deletes[0]  # never without a stop
    h.play(101.1, 101.2, 100.0, 100.5)  # reverses through breakeven
    [row] = h.journal()
    assert row["exit_reason"] == "BE"
    # Breakeven covers fees + a slippage allowance. The fake fills stops with
    # no slippage, so the unused allowance is left over as a tiny profit.
    net = float(row["net_pnl"])
    allowance = t.qty * t.entry_price * 2 * h.settings.costs.slippage
    assert 0 <= net <= allowance


def test_time_stop_closes_at_market(tmp_path):
    s = Harness.default_settings("testnet")
    s.management.max_bars_in_trade = 2
    s.management.breakeven_at_r = 0
    h = Harness(tmp_path, settings=s)
    open_long(h)
    h.play(100, 100.2, 99.8, 100.1)
    assert SYM in h.engine.trades
    h.play(100.1, 100.3, 99.9, 100.2)
    assert SYM not in h.engine.trades
    [row] = h.journal()
    assert row["exit_reason"] == "TIME" and h.fake.pos[SYM][0] == 0
    assert h.fake.open_regular(SYM) == [] and h.fake.open_algos(SYM) == []


def test_short_trade_cycle(tmp_path):
    h = Harness(tmp_path)
    h.engine.start()
    h.signal_next(SHORT, stop=101.0)
    h.play(100, 100.1, 99.9, 100)
    t = h.engine.trades[SYM]
    assert t.side == SHORT and h.fake.pos[SYM][0] < 0
    assert h.fake.open_algos(SYM)[0]["side"] == "BUY"
    h.play(100, 101.2, 99.9, 101.1)
    assert h.journal()[0]["exit_reason"] == "SL"


def test_restart_restores_trade_and_replaces_missing_stop(tmp_path):
    h = Harness(tmp_path)
    open_long(h)
    # someone cancels the stop by hand while the bot is down
    for a in h.fake.open_algos(SYM):
        a["algoStatus"] = "CANCELED"
    h2 = Harness(tmp_path, fake=h.fake, plan={})
    h2.engine.start()
    assert SYM in h2.engine.trades
    assert len(h2.fake.open_algos(SYM)) == 1  # re-protected
    assert len(h2.fake.open_regular(SYM)) == 1  # target still there, not duplicated


def test_trade_closed_while_bot_was_down_is_booked_on_restart(tmp_path):
    h = Harness(tmp_path)
    open_long(h)
    h.fake.play(SYM, ohlc(h.next_index(), 100, 100.1, 98.5, 98.8))  # stop fills, no bot running
    h2 = Harness(tmp_path, fake=h.fake, plan={})
    h2.engine.start()
    assert SYM not in h2.engine.trades
    [row] = h2.journal()
    assert row["exit_reason"] == "SL"
    assert h2.fake.open_regular(SYM) == []  # stale take-profit removed


def test_orphan_position_is_adopted_and_protected(tmp_path):
    h = Harness(tmp_path)
    h.fake.pos[SYM] = [0.5, 100.0]
    h.engine.start()
    t = h.engine.trades[SYM]
    assert t.adopted and t.side == LONG and t.qty == 0.5
    assert t.stop == pytest.approx(100.0 - 2.0 * 0.5)  # orphan_sl_atr × ATR
    assert len(h.fake.open_algos(SYM)) == 1


def test_orphan_ignored_when_policy_says_so(tmp_path):
    s = Harness.default_settings("testnet")
    s.execution.orphan_policy = "ignore"
    h = Harness(tmp_path, settings=s)
    h.fake.pos[SYM] = [0.5, 100.0]
    h.engine.start()
    assert h.engine.trades == {} and h.fake.open_algos(SYM) == []


def test_crash_between_order_and_bookkeeping_is_recovered(tmp_path):
    s = Harness.default_settings("testnet")
    s.execution.orphan_policy = "ignore"  # even then: a pending entry is ours
    h = Harness(tmp_path, settings=s)
    h.fake.pos[SYM] = [0.5, 100.0]
    h.engine.pending_entry[SYM] = {"client_id": "scenX", "time": h.fake.now, "side": LONG}
    h.engine.start()
    assert SYM in h.engine.trades and len(h.fake.open_algos(SYM)) == 1


def test_position_is_closed_if_stop_cannot_be_placed(tmp_path):
    h = Harness(tmp_path)

    def reject_stops(method, path, params):
        if (method, path) == ("POST", "/fapi/v1/algoOrder"):
            return FakeResponse(400, {"code": -4001, "msg": "Price less than 0."})
        return None

    h.fake.inject.append(reject_stops)
    h.engine.start()
    h.signal_next(LONG, stop=99.0)
    h.play(100, 100.1, 99.9, 100)
    assert h.fake.pos[SYM][0] == 0 and SYM not in h.engine.trades
    [row] = h.journal()
    assert row["exit_reason"] == "NO_STOP"


def test_entry_timeout_does_not_double_the_position(tmp_path):
    h = Harness(tmp_path)
    state = {"done": False}

    def hook(method, path, params):
        if (method, path) == ("POST", "/fapi/v1/order") and params.get("type") == "MARKET" and not state["done"]:
            state["done"] = True
            h.fake.handle(method, path, params)
            return requests.Timeout("response lost")
        return None

    h.fake.inject.append(hook)
    trade = open_long(h)
    assert h.fake.pos[SYM][0] == pytest.approx(trade.qty)
    assert len([c for c in h.fake.calls if c[:2] == ("POST", "/fapi/v1/order") and c[2].get("type") == "MARKET"]) == 1
    assert len(h.fake.open_algos(SYM)) == 1


def test_classic_conditional_orders_on_servers_without_algo_api(tmp_path):
    h = Harness(tmp_path, algo=False)
    open_long(h)
    kinds = sorted(o["type"] for o in h.fake.open_regular(SYM))
    assert kinds == ["LIMIT", "STOP_MARKET"]
    h.play(100, 100.1, 98.9, 99.1)
    assert h.journal()[0]["exit_reason"] == "SL" and h.fake.open_regular(SYM) == []


def test_hedge_mode_refused_at_start(tmp_path):
    h = Harness(tmp_path)
    h.fake.dual = True
    with pytest.raises(BrokerError):
        h.engine.start()


def test_daily_loss_limit_blocks_new_entries(tmp_path):
    h = Harness(tmp_path)
    h.engine.start()
    h.engine.guard.s.realized_today = -100.0
    h.signal_next(LONG)
    h.play(100, 100.1, 99.9, 100)
    assert h.engine.trades == {} and h.fake.pos[SYM][0] == 0


def test_stale_signals_after_a_gap_are_not_traded(tmp_path):
    h = Harness(tmp_path)
    h.engine.start()
    h.signal_next(LONG)
    for _ in range(4):  # four candles pass while the bot is not ticking
        h.play(100, 100.1, 99.9, 100, tick=False)
    h.engine.tick()
    assert h.engine.trades == {} and h.fake.pos[SYM][0] == 0
    assert h.engine.last_candle[SYM] == h.fake.candles[SYM][-1].open_time


def test_stale_orders_cleared_before_new_entry(tmp_path):
    h = Harness(tmp_path)
    h.engine.start()
    # a leftover reduce-only order from an old trade
    h.fake.pos[SYM] = [0.3, 100.0]
    h.fake.handle("POST", "/fapi/v1/order", {"symbol": SYM, "side": "SELL", "type": "LIMIT", "quantity": "0.3",
                                            "price": "103", "reduceOnly": "true", "newClientOrderId": "sctpold"})
    h.fake.pos[SYM] = [0.0, 0.0]
    h.signal_next(LONG)
    h.play(100, 100.1, 99.9, 100)
    ids = [o["clientOrderId"] for o in h.fake.open_regular(SYM)]
    assert "sctpold" not in ids and SYM in h.engine.trades


def test_paper_mode_full_cycle(tmp_path):
    h = Harness(tmp_path, mode="paper")
    h.engine.start()
    h.signal_next(LONG, stop=99.0)
    h.play(100, 100.1, 99.9, 100)
    assert h.engine.trades[SYM].side == LONG
    assert h.fake.pos[SYM][0] == 0  # paper never touches the exchange
    assert not any(c[0] in ("POST", "DELETE") for c in h.fake.calls)
    h.play(100, 101.8, 99.95, 101.7)
    [row] = h.journal()
    assert row["exit_reason"] == "TP" and float(row["net_pnl"]) > 0
    state = StateStore(str(tmp_path / "state_paper.json")).peek()
    assert state["paper"]["balance"] == pytest.approx(1000 + float(row["net_pnl"]), abs=1e-6)


def test_paper_restart_fills_orders_from_candles_missed_offline(tmp_path):
    h = Harness(tmp_path, mode="paper")
    h.engine.start()
    h.signal_next(LONG, stop=99.0)
    h.play(100, 100.1, 99.9, 100)
    assert SYM in h.engine.trades
    h.fake.play(SYM, ohlc(h.next_index(), 100, 100.1, 98.7, 98.9))  # bot offline
    h.fake.play(SYM, ohlc(h.next_index(), 98.9, 99.0, 98.8, 98.9))
    h2 = Harness(tmp_path, mode="paper", fake=h.fake, plan={})
    h2.engine.start()
    assert SYM not in h2.engine.trades
    [row] = h2.journal()
    assert row["exit_reason"] == "SL"


def test_shutdown_can_flatten(tmp_path):
    s = Harness.default_settings("testnet")
    s.execution.flatten_on_exit = True
    h = Harness(tmp_path, settings=s)
    open_long(h)
    h.engine.shutdown()
    assert h.fake.pos[SYM][0] == 0 and h.engine.trades == {}


def test_partial_take_profit_keeps_r_multiple_honest(tmp_path):
    h = Harness(tmp_path)
    trade = open_long(h)
    full = trade.qty
    half = round(full / 2, 3)
    # simulate a partially filled take-profit: half the position is sold
    h.fake.handle("POST", "/fapi/v1/order", {"symbol": SYM, "side": "SELL", "type": "MARKET",
                                            "quantity": str(half), "reduceOnly": "true"})
    h.fake.now += 6_000
    h.engine.tick()
    assert h.engine.trades[SYM].qty == pytest.approx(full - half)
    h.play(100, 100.1, 98.9, 99.2)  # stop takes the rest
    [row] = h.journal()
    assert float(row["qty"]) == pytest.approx(full)
    risk = full * (trade.entry_price - trade.initial_stop)
    assert float(row["r_multiple"]) == pytest.approx(float(row["net_pnl"]) / risk, abs=1e-3)


def test_nap_returns_immediately_after_stop(tmp_path):
    h = Harness(tmp_path)
    slept = []
    h.engine.sleep = slept.append
    h.engine.stop()
    h.engine._nap(3600)
    assert slept == []


def test_account_balance_is_saved_for_the_dashboard(tmp_path):
    h = Harness(tmp_path)
    h.engine.start()
    path = str(tmp_path / "state_testnet.json")
    assert StateStore(path).peek()["account"]["balance"] == pytest.approx(1000.0)
    h.fake.wallet = 1234.5  # e.g. a testnet top-up, picked up by the periodic refresh
    h.fake.now += 301_000
    h.engine.tick()
    account = StateStore(path).peek()["account"]
    assert account["balance"] == pytest.approx(1234.5) and account["at"]


def test_open_trade_on_a_symbol_removed_from_the_config_is_reported(tmp_path):
    sent = []
    h = Harness(tmp_path)
    h.engine.notify = type("N", (), {"send": staticmethod(sent.append)})()
    StateStore(str(tmp_path / "state_testnet.json")).save(
        {"version": 1, "mode": "testnet", "trades": {"XRPUSDT": {"side": LONG}}}
    )
    h.engine.load()
    assert h.engine.trades == {}
    assert any("XRPUSDT" in m and "removed" in m for m in sent)


def test_every_candle_is_explained_for_the_dashboard(tmp_path):
    s = Harness.default_settings("testnet")
    s.risk.min_sl_cost_ratio = 3.0  # fee filter on: the stop must be >= 3x the round-trip cost
    h = Harness(tmp_path, settings=s)
    h.engine.start()
    history = h.engine.market.history(SYM, s.execution.warmup_bars)
    assert sum(h.engine.why.counts(SYM).values()) == len(history)  # last 24h explained right after start
    h.signal_next(LONG, stop=99.99)  # 0.01% stop: fees would eat it
    h.play(100, 100.1, 99.9, 100)
    assert h.engine.why.latest[SYM]["code"] == "fee_filter"
    assert SYM not in h.engine.trades
    h.signal_next(LONG, stop=99.0)
    h.play(100, 100.1, 99.9, 100)
    assert h.engine.why.latest[SYM]["code"] == "entered" and SYM in h.engine.trades
    h.play(100, 100.2, 99.95, 100.1)
    assert h.engine.why.latest[SYM]["code"] == "in_trade"
    why = StateStore(str(tmp_path / "state_testnet.json")).peek()["why"]
    assert why["fee_min_pct"] == pytest.approx(3.0 * s.costs.round_trip_cost * 100, abs=1e-4)
    assert why["symbols"][SYM]["counts"]["fee_filter"] == 1
    assert why["symbols"][SYM]["counts"]["entered"] == 1


def test_entry_works_when_exchange_reports_zero_fill_price(tmp_path):
    h = Harness(tmp_path)
    h.fake.market_zero_price = True
    h.fake.market_async = True
    trade = open_long(h)
    assert trade.entry_price == pytest.approx(100.005)
    assert trade.stop == 99.0 and trade.take_profit == pytest.approx(100.005 + 1.5 * 1.005)
    assert len(h.fake.open_algos(SYM)) == 1 and len(h.fake.open_regular(SYM)) == 1
    assert h.journal() == []  # not closed as a bad fill
