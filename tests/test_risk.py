from datetime import date

from bot.config import RiskConfig
from bot.risk import Position, RiskManager, RiskState


def make_risk(**overrides) -> RiskManager:
    cfg = RiskConfig(
        max_position_usd=25.0,
        max_total_exposure_usd=100.0,
        max_daily_loss_usd=50.0,
        min_order_size_usd=1.0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return RiskManager(cfg)


def test_can_open_allows_within_limits():
    risk = make_risk()
    allowed, reason = risk.can_open("mkt1", 10.0)
    assert allowed
    assert reason == ""


def test_can_open_rejects_below_min_order_size():
    risk = make_risk()
    allowed, reason = risk.can_open("mkt1", 0.5)
    assert not allowed
    assert "minimum" in reason


def test_can_open_rejects_over_per_market_cap():
    risk = make_risk()
    risk.record_open("mkt1", "tokA", "YES", size=20.0, cost_usd=20.0)
    allowed, reason = risk.can_open("mkt1", 10.0)  # 20 + 10 > 25
    assert not allowed
    assert "max_position_usd" in reason


def test_can_open_rejects_over_total_exposure_cap():
    risk = make_risk(max_position_usd=1000.0, max_total_exposure_usd=30.0)
    risk.record_open("mkt1", "tokA", "YES", size=20.0, cost_usd=20.0)
    allowed, reason = risk.can_open("mkt2", 15.0)  # 20 + 15 > 30
    assert not allowed
    assert "max_total_exposure_usd" in reason


def test_daily_loss_limit_blocks_new_positions():
    risk = make_risk(max_daily_loss_usd=10.0)
    risk.record_open("mkt1", "tokA", "YES", size=10.0, cost_usd=10.0)
    pnl = risk.record_close("tokA", size=10.0, proceeds_usd=0.0)  # lose $10
    assert pnl == -10.0
    assert risk.daily_loss_limit_hit
    allowed, reason = risk.can_open("mkt2", 5.0)
    assert not allowed
    assert "daily loss limit" in reason


def test_record_open_accumulates_and_avg_price():
    risk = make_risk()
    risk.record_open("mkt1", "tokA", "YES", size=10.0, cost_usd=5.0)  # 0.50 each
    risk.record_open("mkt1", "tokA", "YES", size=10.0, cost_usd=6.0)  # 0.60 each
    pos = risk.positions["tokA"]
    assert pos.size == 20.0
    assert pos.cost_usd == 11.0
    assert abs(pos.avg_price - 0.55) < 1e-9


def test_record_close_partial_realizes_correct_pnl():
    risk = make_risk()
    risk.record_open("mkt1", "tokA", "YES", size=10.0, cost_usd=5.0)  # avg 0.50
    pnl = risk.record_close("tokA", size=4.0, proceeds_usd=3.0)  # sold 4 @ 0.75, cost basis 2.0
    assert abs(pnl - 1.0) < 1e-9
    assert risk.positions["tokA"].size == 6.0
    assert abs(risk.positions["tokA"].cost_usd - 3.0) < 1e-9


def test_record_close_full_removes_position():
    risk = make_risk()
    risk.record_open("mkt1", "tokA", "YES", size=10.0, cost_usd=5.0)
    risk.record_close("tokA", size=10.0, proceeds_usd=8.0)
    assert "tokA" not in risk.positions


def test_max_affordable_usd_respects_both_caps():
    risk = make_risk(max_position_usd=25.0, max_total_exposure_usd=30.0)
    risk.record_open("mkt1", "tokA", "YES", size=20.0, cost_usd=20.0)
    # per-market room = 5, total room = 10 -> min is 5
    assert risk.max_affordable_usd("mkt1") == 5.0


def test_max_affordable_usd_zero_when_daily_loss_hit():
    risk = make_risk(max_daily_loss_usd=5.0)
    risk.record_open("mkt1", "tokA", "YES", size=10.0, cost_usd=10.0)
    risk.record_close("tokA", size=10.0, proceeds_usd=0.0)  # -$10 loss
    assert risk.max_affordable_usd("mkt2") == 0.0


# -- float tolerance, mark-to-market, persistence ----------------------------


def test_cap_check_tolerates_float_noise_but_not_real_excess():
    risk = make_risk(max_position_usd=25.0)
    assert risk.can_open("mkt1", 25.000000000000004)[0]
    assert not risk.can_open("mkt1", 25.01)[0]


def test_mark_to_market_values_complete_sets_at_one_dollar():
    risk = make_risk()
    risk.record_open("mkt1", "yes", "YES", size=10.0, cost_usd=4.9, outcome_count=2)
    risk.record_open("mkt1", "no", "NO", size=10.0, cost_usd=4.9, outcome_count=2)
    # bids are below what was paid, but 10 YES + 10 NO always pay out $10
    assert abs(risk.mark_to_market({"yes": 0.45, "no": 0.45}) - (10.0 - 9.8)) < 1e-9


def test_mark_to_market_values_leftover_shares_at_the_bid():
    risk = make_risk()
    risk.record_open("mkt1", "yes", "YES", size=12.0, cost_usd=6.0, outcome_count=2)
    risk.record_open("mkt1", "no", "NO", size=10.0, cost_usd=4.8, outcome_count=2)
    # 10 sets = $10.00, plus 2 extra YES at a 0.40 bid = $0.80; cost $10.80
    assert abs(risk.mark_to_market({"yes": 0.40, "no": 0.45})) < 1e-9


def test_mark_to_market_single_side_and_missing_marks():
    risk = make_risk()
    risk.record_open("mkt1", "yes", "YES", size=10.0, cost_usd=5.0, outcome_count=2)
    risk.record_open("mkt2", "x", "YES", size=10.0, cost_usd=3.0)  # no mark -> valued at cost
    assert abs(risk.mark_to_market({"yes": 0.40}) - (4.0 - 5.0)) < 1e-9


def test_holding_some_but_not_all_outcomes_is_not_a_complete_set():
    risk = make_risk()
    risk.record_open("mkt1", "a", "A", size=10.0, cost_usd=3.0, outcome_count=3)
    risk.record_open("mkt1", "b", "B", size=10.0, cost_usd=3.0, outcome_count=3)
    assert abs(risk.mark_to_market({"a": 0.2, "b": 0.2}) - (4.0 - 6.0)) < 1e-9


def test_unrealized_loss_trips_the_kill_switch_and_recovery_lifts_it():
    risk = make_risk(max_daily_loss_usd=10.0)
    risk.record_open("mkt1", "tokA", "YES", size=100.0, cost_usd=50.0)
    assert not risk.daily_loss_limit_hit

    risk.update_marks({"tokA": 0.38})  # worth $38 -> -$12 unrealized
    assert risk.daily_loss_limit_hit
    allowed, reason = risk.can_open("mkt2", 5.0)
    assert not allowed
    assert "daily loss limit" in reason

    risk.update_marks({"tokA": 0.45})  # back to -$5
    assert not risk.daily_loss_limit_hit


def test_snapshot_and_restore_round_trip():
    risk = make_risk()
    risk.record_open("mkt1", "tokA", "YES", size=10.0, cost_usd=5.0, outcome_count=2)
    risk.record_open("mkt2", "tokB", "NO", size=4.0, cost_usd=2.0)
    risk.record_close("tokB", size=4.0, proceeds_usd=3.0)  # +$1 realized

    restored = make_risk()
    restored.restore(risk.snapshot())
    assert set(restored.positions) == {"tokA"}
    assert restored.positions["tokA"].outcome_count == 2
    assert restored.realized_pnl_today == 1.0

    # restored positions are copies, not shared objects
    restored.positions["tokA"].size = 99.0
    assert risk.positions["tokA"].size == 10.0


def test_restore_from_an_earlier_day_keeps_positions_but_not_realized_pnl():
    risk = make_risk()
    state = RiskState(
        day=date(2000, 1, 1),
        realized_pnl_today=-40.0,
        positions=[Position("mkt1", "tokA", "YES", 10.0, 5.0)],
    )
    risk.restore(state)
    assert "tokA" in risk.positions
    assert risk.realized_pnl_today == 0.0


def test_closed_position_stops_counting_as_unrealized_right_away():
    risk = make_risk(max_daily_loss_usd=100.0)
    risk.record_open("mkt1", "tokA", "YES", size=100.0, cost_usd=50.0)
    risk.update_marks({"tokA": 0.45})  # -$5 unrealized
    risk.record_close("tokA", size=100.0, proceeds_usd=0.0)  # resolved against us
    assert risk.unrealized_pnl == 0.0
    assert risk.daily_pnl == -50.0  # the loss counts once, not -$55
