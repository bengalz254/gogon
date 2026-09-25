import pytest

from bot.updown.book import OrderBook
from bot.updown.config import ConfigError, FairValueConfig, RiskConfig, UpDownConfig, _apply, resolve_params, validate
from bot.updown.fees import FeeModel
from bot.updown.orders import OrderRequest, OrderTracker, OrderUpdate, PaperExchange
from bot.updown.risk import UpDownRisk, kelly_fraction
from updown_helpers import T0


def _roomy(**kw) -> RiskConfig:
    """Caps far away, so Kelly sizing is what's being tested."""
    base = dict(bankroll_usd=1000, max_usd_per_trade=1e6, max_usd_per_window=1e6,
                max_total_exposure_usd=1e6, max_slot_direction_usd=1e6)
    base.update(kw)
    return RiskConfig(**base)


def _size(risk, **kw):
    args = dict(
        strategy="fair_value", strategy_cfg=FairValueConfig(max_usd_per_trade=100, max_usd_per_window=100),
        p_win=0.75, price=0.60, fee_per_share=0.0, taker=True, elapsed=120.0, now=T0,
        window_exposure=0.0, strategy_window_exposure=0.0, total_exposure=0.0, slot_direction_exposure=0.0,
        open_windows=0, window_is_open=False, min_order_shares=5.0,
    )
    args.update(kw)
    return risk.size(**args)


# -- risk -----------------------------------------------------------------------------
def test_kelly():
    assert kelly_fraction(0.75, 0.60) == pytest.approx(0.375)
    assert kelly_fraction(0.5, 0.6) < 0


def test_sizing_is_fractional_kelly_capped():
    risk = UpDownRisk(_roomy(kelly_fraction=0.1), now=T0)
    d = _size(risk)
    assert d.ok and d.usd == pytest.approx(1000 * 0.1 * 0.375, abs=0.01)
    risk = UpDownRisk(_roomy(kelly_fraction=0.1, max_usd_per_window=20), now=T0)
    assert "max_usd_per_window" in _size(risk).reason  # clipped to the window cap
    d = _size(risk, window_exposure=19.0)  # only $1 of window room left -> too small for 5 shares
    assert not d.ok and "min" in d.reason


def test_no_edge_no_trade():
    risk = UpDownRisk(RiskConfig(), now=T0)
    assert not _size(risk, p_win=0.58, price=0.58, fee_per_share=0.008).ok


def test_early_taker_near_50c_is_banned():
    risk = UpDownRisk(RiskConfig(bankroll_usd=1000), now=T0)
    assert "banned" in _size(risk, elapsed=30.0, price=0.52, p_win=0.70).reason
    assert _size(risk, elapsed=30.0, price=0.52, p_win=0.70, taker=False).ok  # makers allowed
    assert _size(risk, elapsed=90.0, price=0.52, p_win=0.70).ok


def test_slot_direction_cap():
    risk = UpDownRisk(RiskConfig(bankroll_usd=10000, max_slot_direction_usd=40, max_usd_per_trade=100,
                                 max_usd_per_window=100, max_total_exposure_usd=1000), now=T0)
    d = _size(risk, slot_direction_exposure=35.0)
    assert d.ok and d.usd <= 5.0 + 1e-9


def test_daily_loss_kill_switch_counts_pending_losses():
    risk = UpDownRisk(RiskConfig(max_daily_loss_usd=30), now=T0)
    risk.on_settlement("fair_value", -20.0, T0)
    assert _size(risk).ok
    risk.pending_pnl = -15.0
    assert "daily loss" in _size(risk).reason


def test_anti_martingale_size_only_shrinks_after_losses():
    risk = UpDownRisk(_roomy(max_consecutive_losses=3, cooldown_s_after_streak=600, max_daily_loss_usd=1e9), now=T0)
    base = _size(risk).usd
    risk.on_settlement("fair_value", -1.0, T0)
    after_one = _size(risk).usd
    assert after_one < base
    risk.on_settlement("fair_value", -1.0, T0)
    risk.on_settlement("fair_value", -1.0, T0)
    assert "cooling down" in _size(risk).reason
    assert _size(risk, now=T0 + 601).usd < after_one  # still smaller after the cooldown
    risk.on_settlement("fair_value", +2.0, T0 + 700)
    # a win resets the streak; bankroll is now 1000 - 3 + 2
    assert _size(risk, now=T0 + 701).usd == pytest.approx(999 * 0.15 * 0.375, rel=0.01)


def test_martingale_config_rejected():
    cfg = UpDownConfig()
    cfg.risk.loss_streak_decay = 1.5
    with pytest.raises(ConfigError):
        validate(cfg)


def test_unknown_config_keys_rejected_and_overrides_resolve():
    cfg = UpDownConfig()
    with pytest.raises(ConfigError):
        _apply(cfg, {"strategies": {"fair_value": {"min_edgee": 0.1}}}, "updown")
    _apply(cfg, {"strategies": {"fair_value": {"overrides": [{"assets": ["SOL"], "sessions": ["asia"],
                                                              "set": {"min_edge": "0.08"}}]}}}, "updown")
    fv = cfg.strategies.fair_value
    assert resolve_params(fv, "sol", "asia").min_edge == 0.08
    assert resolve_params(fv, "sol", "us").min_edge == fv.min_edge
    with pytest.raises(ConfigError):
        _apply(cfg, {"strategies": {"fair_value": {"overrides": [{"set": {"nope": 1}}]}}}, "updown")


# -- paper exchange -----------------------------------------------------------------------------
def _exchange():
    up = OrderBook("up")
    up.apply_snapshot([(0.94, 100)], [(0.96, 10), (0.97, 20)], T0)
    down = OrderBook("down")
    down.apply_snapshot([(0.03, 100)], [(0.06, 100)], T0)
    books = {"up": up, "down": down}
    return PaperExchange(books, FeeModel(), {"up": "down", "down": "up"}), books


def _req(tif="FAK", price=0.97, shares=25.0, post_only=False):
    return OrderRequest(client_id=f"c-{tif}-{price}", window_id="w", strategy="s", token_id="up", outcome="Up",
                        price=price, shares=shares, tif=tif, post_only=post_only)


def test_paper_taker_walks_book_and_remembers_consumed_liquidity():
    ex, _ = _exchange()
    u = ex.submit(_req(), T0)
    assert u.status == "filled" and u.final
    assert [(f.price, f.shares) for f in u.fills] == [(0.96, 10), (0.97, 15)]
    assert all(f.fee > 0 for f in u.fills)
    again = ex.submit(_req(shares=10), T0)  # only 5 left at 0.97
    assert sum(f.shares for f in again.fills) == pytest.approx(5)
    assert ex.submit(_req(tif="FOK", shares=50), T0).status == "cancelled"


def test_paper_maker_queue_and_trade_through():
    ex, books = _exchange()
    u = ex.submit(_req(tif="GTC", price=0.94, shares=10, post_only=True), T0)
    assert u.status == "open"  # queued behind 100 shares at 0.94
    assert ex.on_trade("up", 0.94, 60, T0) == []
    fills = ex.on_trade("up", 0.94, 45, T0)  # 105 traded: 5 of ours
    assert fills[0].fills[0].shares == pytest.approx(5) and not fills[0].final
    done = ex.on_trade("up", 0.93, 1, T0)  # traded through our level
    assert done[0].final and done[0].fills[0].price == 0.94


def test_paper_post_only_rejects_crossing_and_complement_cross_fills():
    ex, books = _exchange()
    assert ex.submit(_req(tif="GTC", price=0.96, shares=10, post_only=True), T0).status == "rejected"
    ex.submit(_req(tif="GTC", price=0.95, shares=10, post_only=True), T0)
    books["down"].apply_level("BUY", 0.05, 50, T0 + 1)  # Up 0.95 + Down 0.05 = 1.00 -> mint
    out = ex.on_book("down", T0 + 1, level_price=0.05, level_side="BUY")
    assert out and out[0].final and out[0].fills[0].is_maker


def test_order_tracker_reserved_and_partial_fills():
    tr = OrderTracker()
    req = _req(tif="GTC", price=0.5, shares=10)
    tr.add(req)
    assert tr.reserved() == pytest.approx(5.0)
    from bot.updown.orders import Fill
    tr.apply(OrderUpdate(req.client_id, "partial", [Fill(4, 0.5, 0.0, T0, True)]), T0)
    assert tr.reserved() == pytest.approx(3.0)
    tr.apply(OrderUpdate(req.client_id, "cancelled", final=True), T0)
    assert tr.reserved() == 0.0


def test_shipped_yaml_config_loads():
    import os

    from bot.updown.config import load_updown_config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_updown_config(os.path.join(root, "config", "updown.yaml"))
    assert cfg.execution.allow_live is False  # live trading must stay an explicit opt-in
    assert cfg.risk.loss_streak_decay <= 1


def test_fixed_size_skips_kelly_but_keeps_caps_and_bans():
    risk = UpDownRisk(_roomy(max_usd_per_window=20), now=T0)
    d = _size(risk, p_win=0.45, price=0.49, max_shares=10, fixed_size=True, taker=False)
    assert d.ok and d.shares == 10  # no directional edge needed for a barbell leg
    assert not _size(risk, p_win=0.45, price=0.49, max_shares=100, fixed_size=True, taker=False,
                     window_exposure=19.0).ok  # caps still apply
    assert "banned" in _size(risk, p_win=0.45, price=0.50, max_shares=10, fixed_size=True,
                             elapsed=10.0).reason  # and so do the hard bans
