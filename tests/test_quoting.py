from bot.config import MarketMakerConfig, RiskConfig
from bot.fees import FeeModel
from bot.heartbeat import Heartbeat
from bot.market_data import MarketInfo, RewardInfo, TokenInfo
from bot.orderbook import OrderBook
from bot.quoting import QuoteManager
from bot.risk import RiskManager
from bot.strategies.market_maker import Quote
from bot.ws_market import Trade


class FakeJournal:
    def __init__(self):
        self.rows = []

    def record(self, signal, mode, filled):
        self.rows.append((signal, mode, filled))


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


MARKET = MarketInfo(
    condition_id="mkt1",
    question="Will it?",
    tokens=[TokenInfo("yes", "Yes"), TokenInfo("no", "No")],
    rewards=RewardInfo(min_size=10, max_spread=0.03, daily_rate=50),
    tick_size=0.01,
)
BOOKS = {
    "yes": OrderBook("yes", bids={0.48: 100.0}, asks={0.52: 100.0}, tick_size=0.01),
    "no": OrderBook("no", bids={0.48: 100.0}, asks={0.52: 100.0}, tick_size=0.01),
}
BID_YES = Quote("yes", "BUY", 0.48, 10)
BID_NO = Quote("no", "BUY", 0.48, 10)


def make_manager(live=False, client=None, max_position_usd=25.0, **cfg_overrides):
    risk = RiskManager(
        RiskConfig(max_position_usd=max_position_usd, max_total_exposure_usd=200, max_daily_loss_usd=50, min_order_size_usd=1)
    )
    cfg = MarketMakerConfig(enabled=True, order_ttl_seconds=120, requote_ticks=1, **cfg_overrides)
    journal, clock = FakeJournal(), Clock()
    manager = QuoteManager(client, risk, journal, FeeModel.zero(), cfg, live=live, clock=clock)
    return manager, risk, journal, clock


# -- paper -------------------------------------------------------------------------


def test_paper_quotes_rest_and_are_not_churned():
    manager, _, _, _ = make_manager()
    manager.sync(MARKET, [BID_YES, BID_NO], BOOKS.get)
    first = set(manager.orders)
    assert len(first) == 2
    manager.sync(MARKET, [BID_YES, BID_NO], BOOKS.get)
    assert set(manager.orders) == first  # unchanged quotes keep their queue position


def test_paper_quote_moving_a_tick_is_replaced_and_dropped_quotes_cancelled():
    manager, _, _, _ = make_manager()
    manager.sync(MARKET, [BID_YES, BID_NO], BOOKS.get)
    old_yes = next(o.order_id for o in manager.orders.values() if o.token_id == "yes")
    manager.sync(MARKET, [Quote("yes", "BUY", 0.47, 10)], BOOKS.get)
    assert [(o.token_id, o.price) for o in manager.orders.values()] == [("yes", 0.47)]
    assert old_yes not in manager.matcher.orders


def test_resting_buys_count_against_the_budget():
    manager, _, _, _ = make_manager(max_position_usd=6.0)  # one $4.80 bid fits, two don't
    manager.sync(MARKET, [BID_YES, BID_NO], BOOKS.get)
    assert [o.token_id for o in manager.orders.values()] == ["yes"]


def test_sells_need_the_shares_to_be_held():
    manager, risk, _, _ = make_manager()
    manager.sync(MARKET, [Quote("yes", "SELL", 0.52, 10)], BOOKS.get)
    assert manager.orders == {}
    risk.record_open("mkt1", "yes", "Yes", 10.0, 4.8, outcome_count=2)
    manager.sync(MARKET, [Quote("yes", "SELL", 0.52, 10)], BOOKS.get)
    assert len(manager.orders) == 1


def test_paper_fills_update_positions_journal_and_stats():
    manager, risk, journal, _ = make_manager()
    manager.sync(MARKET, [BID_YES], BOOKS.get)
    # a trade below our 0.48 bid would have filled us first
    assert manager.poll_fills([Trade("yes", 0.47, 4.0, "SELL", 0.0)], BOOKS.get) == 1
    assert risk.positions["yes"].size == 4.0
    assert abs(risk.positions["yes"].cost_usd - 4 * 0.48) < 1e-12
    signal, mode, filled = journal.rows[0]
    assert (signal.strategy, signal.side, signal.size_shares, mode, filled) == ("market_maker", "BUY", 4.0, "paper", True)
    assert (manager.fills_today, round(manager.volume_today, 2)) == (1, 1.92)
    # the book moving through our price fills the rest and retires the order
    crossed = {"yes": OrderBook("yes", bids={0.40: 5.0}, asks={0.47: 5.0}, tick_size=0.01)}
    manager.poll_fills([], crossed.get)
    assert risk.positions["yes"].size == 10.0 and manager.orders == {}


def test_cancel_market_and_cancel_all():
    manager, _, _, _ = make_manager()
    manager.sync(MARKET, [BID_YES, BID_NO], BOOKS.get)
    manager.cancel_market("mkt1", "test")
    assert manager.orders == {} and manager.matcher.orders == {}
    manager.sync(MARKET, [BID_YES], BOOKS.get)
    manager.cancel_all("test")
    assert manager.orders == {}


# -- live --------------------------------------------------------------------------


class FakeLiveClient:
    def __init__(self):
        self.created = []
        self.posted = []
        self.cancelled = []
        self.cancel_all_calls = 0
        self.open_orders = []
        self.final = {}
        self.reject = False
        self._next = 0

    def create_order(self, args):
        self.created.append(args)
        return args

    def post_order(self, order, order_type, post_only=False):
        self.posted.append((order, order_type, post_only))
        if self.reject:
            return {"success": False, "errorMsg": "invalid post-only order: order crosses book"}
        self._next += 1
        return {"success": True, "orderID": f"0x{self._next}", "status": "live"}

    def cancel_orders(self, ids):
        self.cancelled.extend(ids)

    def cancel_all(self):
        self.cancel_all_calls += 1

    def get_open_orders(self):
        return self.open_orders

    def get_order(self, order_id):
        return self.final[order_id]


def test_live_quotes_are_post_only_gtd_orders_that_expire_on_their_own():
    client = FakeLiveClient()
    manager, _, _, clock = make_manager(live=True, client=client)
    manager.sync(MARKET, [BID_YES], BOOKS.get)
    order, order_type, post_only = client.posted[0]
    assert (order_type, post_only) == ("GTD", True)
    assert order.expiration == int(clock.now) + 60 + 120
    assert list(manager.orders) == ["0x1"]


def test_live_rejected_quote_is_not_tracked():
    client = FakeLiveClient()
    client.reject = True
    manager, _, _, _ = make_manager(live=True, client=client)
    manager.sync(MARKET, [BID_YES], BOOKS.get)
    assert manager.orders == {}


def test_live_partial_then_final_fills_are_recorded_once():
    client = FakeLiveClient()
    manager, risk, journal, _ = make_manager(live=True, client=client)
    manager.sync(MARKET, [BID_YES], BOOKS.get)
    client.open_orders = [{"id": "0x1", "size_matched": "3"}]
    manager.poll_fills([], BOOKS.get)
    assert risk.positions["yes"].size == 3.0
    manager.poll_fills([], BOOKS.get)  # nothing new
    assert len(journal.rows) == 1
    client.open_orders = []  # gone: fully filled
    client.final["0x1"] = {"id": "0x1", "size_matched": "10", "status": "MATCHED"}
    manager.poll_fills([], BOOKS.get)
    assert risk.positions["yes"].size == 10.0
    assert manager.orders == {} and len(journal.rows) == 2


def test_live_cancelled_order_still_gets_its_last_fill():
    client = FakeLiveClient()
    manager, risk, _, _ = make_manager(live=True, client=client)
    manager.sync(MARKET, [BID_YES], BOOKS.get)
    manager.sync(MARKET, [], BOOKS.get)  # quote no longer wanted
    assert client.cancelled == ["0x1"] and manager.orders == {}
    client.final["0x1"] = {"order": {"id": "0x1", "size_matched": "2"}}  # filled 2 before the cancel landed
    manager.poll_fills([], BOOKS.get)
    assert risk.positions["yes"].size == 2.0


def test_live_orders_are_refreshed_before_they_expire():
    client = FakeLiveClient()
    manager, _, _, clock = make_manager(live=True, client=client)
    manager.sync(MARKET, [BID_YES], BOOKS.get)
    clock.now += 60
    manager.sync(MARKET, [BID_YES], BOOKS.get)
    assert client.cancelled == []
    clock.now += 61  # older than order_ttl_seconds
    manager.sync(MARKET, [BID_YES], BOOKS.get)
    assert client.cancelled == ["0x1"] and list(manager.orders) == ["0x2"]


def test_live_cancel_all_cancels_the_whole_account():
    client = FakeLiveClient()
    manager, _, _, _ = make_manager(live=True, client=client)
    manager.cancel_all("startup cleanup")
    assert client.cancel_all_calls == 1


# -- heartbeat ------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status_code, error_msg):
        self.status_code, self.error_msg = status_code, error_msg


class HeartbeatClient:
    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def post_heartbeat(self, heartbeat_id):
        self.sent.append(heartbeat_id)
        result = self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_heartbeat_adopts_ids_from_responses_and_corrections():
    client = HeartbeatClient(
        [
            ApiError(400, {"error": "invalid heartbeat id", "heartbeat_id": "hb-2"}),
            {"heartbeat_id": "hb-2"},
            {"heartbeat_id": "hb-3"},
        ]
    )
    hb = Heartbeat(client)
    assert not hb.healthy
    assert hb.beat() and hb.healthy
    assert hb.beat()
    assert client.sent == ["", "hb-2", "hb-2"]
    assert hb._id == "hb-3"


def test_heartbeat_failure_is_reported():
    hb = Heartbeat(HeartbeatClient([ApiError(500, "server error")]))
    assert not hb.beat() and not hb.healthy
