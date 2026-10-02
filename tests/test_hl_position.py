import pytest

from hlbot.backtest import liquidation_price
from hlbot.position import ExitTracker
from hlbot.strategy import LONG, SHORT


def tracker(side=LONG, entry=100.0, **kw):
    base = dict(tp_pct=0.02, trailing_pct=0.005, mode="trailing")
    base.update(kw)
    return ExitTracker(side=side, entry_price=entry, **base)


def test_long_trailing_activates_at_tp_then_trails():
    t = tracker()
    assert t.on_price(101.9) is None
    assert not t.trailing_active
    assert t.on_price(101.0) is None  # no stop before activation, no SL configured
    assert t.on_price(102.0) is None
    assert t.trailing_active
    assert t.trailing_stop_price == pytest.approx(101.49)
    assert t.on_price(103.0) is None
    assert t.trailing_stop_price == pytest.approx(102.485)
    assert t.on_price(102.6) is None
    ev = t.on_price(102.4)
    assert ev is not None and ev.reason == "TRAILING_STOP"
    assert ev.price == pytest.approx(102.485)


def test_short_trailing_mirror():
    t = tracker(side=SHORT)
    assert t.on_price(98.5) is None
    assert not t.trailing_active
    assert t.on_price(98.0) is None
    assert t.trailing_active
    assert t.on_price(97.0) is None
    assert t.trailing_stop_price == pytest.approx(97.485)
    ev = t.on_price(97.6)
    assert ev.reason == "TRAILING_STOP"
    assert ev.price == pytest.approx(97.485)


def test_no_exit_without_activation_or_stop_loss():
    t = tracker()
    for p in (99.0, 95.0, 92.0, 99.0):
        assert t.on_price(p) is None
    assert t.adverse_excursion_pct() == pytest.approx(0.08)


def test_optional_stop_loss():
    t = tracker(stop_loss_pct=0.03)
    assert t.on_price(97.5) is None
    ev = t.on_price(96.0)
    assert ev.reason == "STOP_LOSS" and ev.price == pytest.approx(97.0)


def test_liquidation_before_activation():
    liq = liquidation_price(LONG, 100.0, 10, 0.0125)
    t = tracker(liquidation_price=liq)
    ev = t.on_price(85.0)
    assert ev.reason == "LIQUIDATION" and ev.price == pytest.approx(liq)


def test_fixed_mode_takes_profit_exactly_at_tp():
    t = tracker(mode="fixed")
    assert t.trailing_active  # trailing stop runs from entry in fixed mode
    assert t.on_price(101.0) is None
    ev = t.on_price(102.5)
    assert ev.reason == "TAKE_PROFIT" and ev.price == pytest.approx(102.0)


def test_fixed_mode_trailing_stop_from_entry():
    t = tracker(mode="fixed")
    ev = t.on_price(99.4)
    assert ev.reason == "TRAILING_STOP" and ev.price == pytest.approx(99.5)


def test_candle_path_green_candle_hits_trailing_after_new_high():
    t = tracker()
    # green candle: open -> low -> high -> close
    ev = t.on_candle(100.0, 103.0, 99.0, 102.4)
    assert ev.reason == "TRAILING_STOP"
    assert ev.price == pytest.approx(103.0 * 0.995)


def test_candle_path_red_candle_wick_down_before_activation_does_nothing():
    t = tracker()
    # red candle: open -> high (101) -> low (98) -> close; never reached TP
    assert t.on_candle(100.0, 101.0, 98.0, 99.0) is None


def test_liquidation_price_formula():
    long_liq = liquidation_price(LONG, 100.0, 10, 0.0125)
    short_liq = liquidation_price(SHORT, 100.0, 10, 0.0125)
    assert 91.0 < long_liq < 92.0
    assert 108.0 < short_liq < 109.0


def test_roundtrip_serialisation():
    t = tracker()
    t.on_price(103.0)
    t2 = ExitTracker.from_dict(t.to_dict())
    assert t2 == t
    assert t2.on_price(102.4).reason == "TRAILING_STOP"


def test_rejects_bad_side_and_mode():
    with pytest.raises(ValueError):
        tracker(side="UP")
    with pytest.raises(ValueError):
        tracker(mode="moon")
