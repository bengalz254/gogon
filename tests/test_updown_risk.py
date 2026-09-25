from updown.config import RiskLimitsConfig
from updown.risk import UpDownRisk


def test_losing_streak_cools_down_only_that_coin():
    r = UpDownRisk(RiskLimitsConfig(max_daily_loss_usd=100, max_consecutive_losses=3, cooldown_minutes=30))
    for t in (0, 300, 600):
        r.record_window(t, -1.0, "btc")
    ok, why = r.can_trade(700, "btc")
    assert not ok and "cooling down" in why
    assert r.can_trade(700, "eth")[0]  # other coins keep going
    assert r.can_trade(600 + 30 * 60 + 1, "btc")[0]  # and btc comes back


def test_daily_loss_stops_every_coin():
    r = UpDownRisk(RiskLimitsConfig(max_daily_loss_usd=5, max_consecutive_losses=99))
    r.record_window(0, -3.0, "btc")
    r.record_window(10, -2.5, "eth")
    assert not r.can_trade(20, "sol")[0] and r.room_usd(20, "sol") == 0


def test_a_win_resets_the_streak():
    r = UpDownRisk(RiskLimitsConfig(max_daily_loss_usd=100, max_consecutive_losses=3))
    r.record_window(0, -1.0, "btc")
    r.record_window(300, -1.0, "btc")
    r.record_window(600, +1.0, "btc")
    r.record_window(900, -1.0, "btc")
    assert r.can_trade(1000, "btc")[0] and r.consecutive_losses == 1
