from datetime import datetime, timedelta, timezone

import pytest

from dca.config import DcaSettings, EntryConfig, LadderConfig
from dca.engine import DcaEngine
from dca.indicators import ema, rsi
from dca.journal import DcaJournal
from dca.ladder import LONG, SHORT, build_levels, build_plan, liquidation_price
from dca.paper import PaperBroker
from dca.state import EngineState, StateStore

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def settings(**kw) -> DcaSettings:
    s = DcaSettings(entry=EntryConfig(mode="always"), **kw)
    s.validate()
    return s


def engine(s: DcaSettings, tmp_path=None) -> DcaEngine:
    store = StateStore(str(tmp_path / "state.json") if tmp_path else None)
    return DcaEngine(s, PaperBroker(s), store, DcaJournal(None))


def test_levels_scale_deviation_and_volume():
    lv = build_levels(40, 40, 3, 1.5, 1.3, 1.5)
    assert [round(l.deviation_pct, 4) for l in lv] == [0, 1.5, 3.45, 5.985]
    assert [l.notional_usdt for l in lv] == [40, 40, 60, 90]


def test_plan_long_and_short_mirror():
    lv = build_levels(40, 40, 6, 1.5, 1.3, 1.5)
    long_ = build_plan(100, LONG, lv, 1.0, 24, 3, 0.01)
    short = build_plan(100, SHORT, lv, 1.0, 24, 3, 0.01)
    assert long_.rows[1].price == pytest.approx(98.5)
    assert short.rows[1].price == pytest.approx(101.5)
    assert long_.sl_price == pytest.approx(76) and short.sl_price == pytest.approx(124)
    assert long_.rows[-1].avg_price < 100 < short.rows[-1].avg_price
    assert long_.total_notional_usdt == pytest.approx(871.25)
    assert long_.margin_usdt == pytest.approx(871.25 / 3)
    assert long_.min_liq_buffer_pct > 5


def test_liquidation_price_formula():
    assert liquidation_price(100, LONG, 1, 0.0) == pytest.approx(0)
    assert liquidation_price(100, LONG, 2, 0.0) == pytest.approx(50)
    assert liquidation_price(100, SHORT, 2, 0.0) == pytest.approx(150)


def test_validate_rejects_high_leverage():
    s = DcaSettings(leverage=20)
    with pytest.raises(ValueError, match="liquidation"):
        s.validate()


def test_validate_rejects_sl_inside_ladder():
    s = DcaSettings(ladder=LadderConfig(stop_loss_pct=10))
    with pytest.raises(ValueError, match="stop_loss_pct"):
        s.validate()


def test_validate_rejects_oversized_ladder():
    s = DcaSettings(capital_usdt=300)
    with pytest.raises(ValueError, match="worst-case margin"):
        s.validate()


def test_indicators():
    assert ema([1, 2, 3], 3) == pytest.approx(2)
    assert rsi(list(range(1, 30)), 14) == 100.0
    assert rsi(list(range(30, 1, -1)), 14) == pytest.approx(0.0)


def test_long_deal_safety_orders_then_take_profit():
    s = settings(sides=[LONG])
    e = engine(s)
    e.tick(T0, [100] * 20, 100, 100, 100)
    deal = e.state.deals[LONG]
    assert deal.filled_qty == pytest.approx(0.4)

    # Drop through SO1 and SO2 (98.5, 96.55)
    e.tick(T0 + timedelta(minutes=15), [100] * 20, 96.0, 99.0, 96.5)
    assert deal.safety_orders_filled == 2
    assert deal.avg_price < 100
    tp = deal.tp_price
    assert tp == pytest.approx(deal.avg_price * 1.01)

    # Bounce to TP — deal closes, a new deal opens immediately (mode=always)
    e.tick(T0 + timedelta(minutes=30), [100] * 20, 96.5, tp + 0.1, tp)
    assert len(e.closed_deals) == 1
    closed = e.closed_deals[0]
    assert closed.close_reason == "take_profit"
    assert closed.realized_pnl > 0
    assert e.state.wins == 1
    assert e.state.deals[LONG].entry_price == pytest.approx(tp)


def test_no_take_profit_on_same_bar_as_safety_fill():
    s = settings(sides=[LONG])
    e = engine(s)
    e.tick(T0, [100] * 20, 100, 100, 100)
    e.tick(T0 + timedelta(minutes=15), [100] * 20, 98.0, 110.0, 99)
    assert e.closed_deals == []
    assert e.state.deals[LONG].safety_orders_filled == 1


def test_short_stop_loss_fills_all_orders_and_sets_cooldown():
    s = settings(sides=[SHORT])
    e = engine(s)
    e.tick(T0, [100] * 20, 100, 100, 100)
    e.tick(T0 + timedelta(minutes=15), [100] * 20, 100, 130, 130)
    closed = e.closed_deals[0]
    assert closed.close_reason == "stop_loss"
    assert closed.safety_orders_filled == 6
    assert closed.close_price == pytest.approx(124)
    assert closed.realized_pnl < 0
    # Cooldown: no new short right away
    assert SHORT not in e.state.deals
    e.tick(T0 + timedelta(minutes=30), [100] * 20, 129, 129, 129)
    assert SHORT not in e.state.deals
    e.tick(T0 + timedelta(minutes=80), [100] * 20, 129, 129, 129)
    assert SHORT in e.state.deals


def test_daily_loss_limit_blocks_new_deals():
    s = settings(sides=[LONG])
    e = engine(s)
    e.state.daily_pnl[T0.date().isoformat()] = -150
    e.tick(T0, [100] * 20, 100, 100, 100)
    assert e.state.deals == {}
    e.tick(T0 + timedelta(days=1), [100] * 20, 100, 100, 100)
    assert LONG in e.state.deals


def test_rsi_entry_filter():
    s = DcaSettings(sides=[LONG, SHORT])
    e = engine(s)
    falling = [100 - i for i in range(30)]
    e.tick(T0, falling, falling[-1], falling[-1], falling[-1])
    assert set(e.state.deals) == {LONG}


def test_state_round_trip(tmp_path):
    s = settings()
    e = engine(s, tmp_path)
    e.tick(T0, [100] * 20, 100, 100, 100)
    e.tick(T0 + timedelta(minutes=15), [100] * 20, 98, 102, 100)
    restored = StateStore(str(tmp_path / "state.json")).load()
    assert isinstance(restored, EngineState)
    for side in (LONG, SHORT):
        assert restored.deals[side].to_dict() == e.state.deals[side].to_dict()
