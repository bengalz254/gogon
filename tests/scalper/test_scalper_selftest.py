import pytest
from fakes import KEY, SECRET, FakeExchange, FakeResponse

from scalper.config import Settings
from scalper.engine import MarketData
from scalper.exchange import BinanceBroker, BinanceFuturesClient, parse_symbol_rules
from scalper.selftest import run_selftest

SYM = "BTCUSDT"


def setup(fake: FakeExchange):
    s = Settings()
    s.symbols = [SYM]
    client = BinanceFuturesClient("https://fake", KEY, SECRET, session=fake,
                                  clock=lambda: fake.now / 1000, sleep=lambda x: None)
    rules = parse_symbol_rules(client.exchange_info(), [SYM])
    broker = BinanceBroker(client, s, rules, sleep=lambda x: None)
    broker.prepare([SYM])
    return broker, MarketData(client, "5m"), rules


def run(fake):
    broker, market, rules = setup(fake)
    lines = []
    steps = run_selftest(broker, market, rules, SYM, log=lines.append, sleep=lambda x: None)
    return steps, lines


@pytest.mark.parametrize("algo", [True, False])
def test_selftest_passes_and_leaves_nothing_open(algo):
    fake = FakeExchange(algo=algo, min_notional="100")
    steps, lines = run(fake)
    assert [s.name for s in steps if not s.ok] == []
    assert len(steps) == 8
    assert ("Algo Order API" if algo else "classic order endpoint") in "\n".join(lines)
    assert fake.pos[SYM][0] == 0
    assert fake.open_regular(SYM) == [] and fake.open_algos(SYM) == []
    entry = fake.fills[0]
    assert float(entry["qty"]) * float(entry["price"]) >= 100  # smallest size that meets min notional
    assert float(entry["qty"]) * float(entry["price"]) < 115


def test_selftest_reports_rejected_stop_and_still_cleans_up():
    fake = FakeExchange(algo=True)
    fake.inject.append(lambda m, p, q: FakeResponse(400, {"code": -1116, "msg": "Invalid orderType."})
                       if (m, p) == ("POST", "/fapi/v1/algoOrder") else None)
    steps, _ = run(fake)
    failed = [s.name for s in steps if not s.ok]
    assert failed == ["unexpected error"] and "Invalid orderType" in steps[-1].detail
    assert fake.pos[SYM][0] == 0  # the test position was closed anyway
    assert fake.open_regular(SYM) == [] and fake.open_algos(SYM) == []


def test_selftest_refuses_to_touch_an_existing_position():
    fake = FakeExchange()
    fake.pos[SYM] = [0.5, 100.0]
    steps, _ = run(fake)
    assert [s.name for s in steps] == ["clean start"] and not steps[0].ok
    assert fake.pos[SYM][0] == 0.5  # untouched
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in fake.calls)
