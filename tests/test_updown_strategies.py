import pytest

from bot.updown.config import (
    CheapAsymmetricConfig,
    ConstellationConfig,
    FairValueConfig,
    LateCertaintyConfig,
    PairBarbellConfig,
    UpDownConfig,
)
from bot.updown.strategies import (
    CheapAsymmetricStrategy,
    ConstellationStrategy,
    FairValueStrategy,
    LateCertaintyMaker,
    PairBarbellStrategy,
    Quote,
    Take,
)
from bot.updown.window import Holding
from updown_helpers import context, snapshot, vol


def _no_blend():
    cfg = UpDownConfig()
    cfg.model.market_blend = 0.0
    cfg.model.vol_uncertainty = 0.0
    return cfg


def _p72_snapshot(cfg):
    # TWAP lead with 90s left in a calm tape; sigma picked so the model says ~0.72.
    s = snapshot(delta=0.0012, remaining=90, sigma_annual=1.9, cfg=cfg)
    assert 0.70 <= s.p_up <= 0.76
    return s


# -- 1. fair value ------------------------------------------------------------------
def test_fair_value_buys_when_market_lags_the_model():
    cfg = _no_blend()
    snap = _p72_snapshot(cfg)
    ctx = context(FairValueConfig(), snap, up=(0.56, 0.58), down=(0.42, 0.44), cfg=cfg)
    out = FairValueStrategy(FairValueConfig()).evaluate(ctx)
    assert len(out) == 1 and isinstance(out[0], Take)
    take = out[0]
    assert take.outcome == "Up"
    # edge = p - ask - fee - slippage, and the limit keeps >= min_edge
    assert take.edge == pytest.approx(snap.p_up - 0.58 - ctx.fees.taker_per_share(0.58) - 0.005)
    assert 0.58 <= take.limit_price <= snap.p_up - 0.05


def test_fair_value_skips_when_market_already_priced_it():
    cfg = _no_blend()
    snap = _p72_snapshot(cfg)
    ctx = context(FairValueConfig(), snap, up=(0.72, 0.74), down=(0.26, 0.28), cfg=cfg)
    assert FairValueStrategy(FairValueConfig()).evaluate(ctx) == []


def test_fair_value_never_flips_inside_a_window():
    cfg = _no_blend()
    snap = snapshot(delta=-0.0012, remaining=90, sigma_annual=2.2, cfg=cfg)  # Down is the value side
    ctx = context(FairValueConfig(), snap, up=(0.56, 0.58), down=(0.42, 0.44), cfg=cfg, name="fair_value")
    ctx.window.holdings[("fair_value", "Up")] = Holding(shares=10, cost=5)
    assert FairValueStrategy(FairValueConfig()).evaluate(ctx) == []


def test_fair_value_respects_entry_limits():
    cfg = _no_blend()
    snap = _p72_snapshot(cfg)
    ctx = context(FairValueConfig(max_entries_per_window=1), snap, up=(0.56, 0.58), cfg=cfg, name="fair_value")
    ctx.window.entries["fair_value"] = 1
    assert FairValueStrategy(FairValueConfig()).evaluate(ctx) == []


# -- 2. late certainty ------------------------------------------------------------------
def _locked(cfg, remaining=45.0):
    return snapshot(delta=0.004, remaining=remaining, sigma_annual=0.4, cfg=cfg)


def test_late_certainty_rests_a_maker_bid_on_the_locked_side():
    cfg = _no_blend()
    snap = _locked(cfg)
    ctx = context(LateCertaintyConfig(), snap, up=(0.95, 0.97), down=(0.03, 0.05), cfg=cfg)
    out = LateCertaintyMaker(LateCertaintyConfig()).evaluate(ctx)
    assert len(out) == 1 and isinstance(out[0], Quote)
    q = out[0]
    assert q.outcome == "Up"
    assert q.price == pytest.approx(0.96)  # improved one tick, still below the ask
    assert q.price < 0.97


def test_late_certainty_never_crosses_the_spread():
    cfg = _no_blend()
    ctx = context(LateCertaintyConfig(), _locked(cfg), up=(0.95, 0.96), down=(0.04, 0.05), cfg=cfg)
    q = LateCertaintyMaker(LateCertaintyConfig()).evaluate(ctx)[0]
    assert q.price == pytest.approx(0.95)


def test_late_certainty_filters():
    cfg = _no_blend()
    strat = LateCertaintyMaker(LateCertaintyConfig())
    # outside the 20-70s band
    assert strat.evaluate(context(LateCertaintyConfig(), _locked(cfg, 120), up=(0.95, 0.97), cfg=cfg)) == []
    # bid outside 0.92-0.985
    assert strat.evaluate(context(LateCertaintyConfig(), _locked(cfg), up=(0.88, 0.90), cfg=cfg)) == []
    # price racing back toward the line: projected to cross -> no quote
    snap = _locked(cfg)
    falling = lambda lb: -(snap.spot - snap.ptb) * 1.5  # noqa: E731 - lost 150% of the lead in 15s
    ctx = context(LateCertaintyConfig(), snap, up=(0.95, 0.97), cfg=cfg, spot_change=falling)
    assert strat.evaluate(ctx) == []
    # volatility spike
    spiky = snapshot(delta=0.004, remaining=45, cfg=cfg, v=vol(0.4, short=0.0002, long=0.00005))
    assert strat.evaluate(context(LateCertaintyConfig(), spiky, up=(0.95, 0.97), cfg=cfg)) == []


# -- 3. constellation ------------------------------------------------------------------------
def _constellation_ctx(cfg, deltas, ref_return=0.004, ask=0.52):
    snap = snapshot(delta=0.0, remaining=210, sigma_annual=0.5, cfg=cfg)  # 90s elapsed
    catchup = snapshot(delta=0.0015, remaining=210, sigma_annual=0.5, cfg=cfg)
    return context(
        ConstellationConfig(), snap, asset="btc", up=(ask - 0.02, ask), down=(1 - ask, 1 - ask + 0.02), cfg=cfg,
        slot_deltas=deltas, asset_return=lambda a, lb: ref_return, remodel=lambda **kw: catchup,
    )


def test_constellation_buys_the_laggard():
    cfg = _no_blend()
    deltas = {"btc": 0.00005, "eth": 0.0008, "sol": 0.0011, "xrp": 0.0009, "doge": 0.0012}
    out = ConstellationStrategy(ConstellationConfig()).evaluate(_constellation_ctx(cfg, deltas))
    assert len(out) == 1 and out[0].outcome == "Up"
    assert out[0].limit_price <= 0.56


def test_constellation_filters():
    cfg = _no_blend()
    strat = ConstellationStrategy(ConstellationConfig())
    movers = {"eth": 0.0008, "sol": 0.0011, "xrp": 0.0009, "doge": 0.0012}
    # laggard not flat
    assert strat.evaluate(_constellation_ctx(cfg, {"btc": 0.0005, **movers})) == []
    # not enough consensus
    assert strat.evaluate(_constellation_ctx(cfg, {"btc": 0.0, "eth": 0.0008, "sol": 0.0011, "xrp": 0.0, "doge": 0.0})) == []
    # reference coin already moved >= 2% in the last hour
    assert strat.evaluate(_constellation_ctx(cfg, {"btc": 0.0, **movers}, ref_return=0.025)) == []
    # share already repriced above 0.56
    assert strat.evaluate(_constellation_ctx(cfg, {"btc": 0.0, **movers}, ask=0.60)) == []


# -- 4. pair barbell -----------------------------------------------------------------------------
def test_pair_barbell_base_quotes_keep_pair_cost_under_cap():
    cfg = _no_blend()
    snap = snapshot(delta=0.0, remaining=280, cfg=cfg)  # 20s elapsed
    ctx = context(PairBarbellConfig(), snap, up=(0.49, 0.51), down=(0.49, 0.51), cfg=cfg)
    out = PairBarbellStrategy(PairBarbellConfig()).evaluate(ctx)
    assert {q.outcome for q in out} == {"Up", "Down"}
    assert sum(q.price for q in out) <= PairBarbellConfig().pair_max_cost + 1e-9


def test_pair_barbell_tilts_toward_confirmed_side_and_never_sells():
    cfg = _no_blend()
    snap = snapshot(delta=0.002, remaining=150, sigma_annual=0.6, cfg=cfg)
    ctx = context(PairBarbellConfig(), snap, up=(0.70, 0.72), down=(0.28, 0.30), cfg=cfg, name="pair_barbell")
    ctx.window.holdings[("pair_barbell", "Up")] = Holding(10, 4.9)
    ctx.window.holdings[("pair_barbell", "Down")] = Holding(10, 4.9)
    out = PairBarbellStrategy(PairBarbellConfig()).evaluate(ctx)
    assert len(out) == 1 and out[0].outcome == "Up"
    wanted = out[0].max_shares if isinstance(out[0], Take) else out[0].shares
    assert wanted == pytest.approx(15.0)  # tilt_ratio 1.5 * base 10


# -- 5. cheap asymmetric -----------------------------------------------------------------------------
def test_cheap_asymmetric_needs_the_rich_side_overpriced():
    cfg = _no_blend()
    snap = snapshot(delta=-0.0003, remaining=200, sigma_annual=0.9, cfg=cfg)  # p_up ~ 0.4
    strat = CheapAsymmetricStrategy(CheapAsymmetricConfig())
    cheap = context(CheapAsymmetricConfig(), snap, up=(0.28, 0.30), down=(0.70, 0.72), cfg=cfg)
    out = strat.evaluate(cheap)
    assert len(out) == 1 and out[0].outcome == "Up"
    fair = context(CheapAsymmetricConfig(), snap, up=(snap.p_up - 0.01, snap.p_up + 0.01),
                   down=(1 - snap.p_up - 0.01, 1 - snap.p_up + 0.01), cfg=cfg)
    assert strat.evaluate(fair) == []


def test_cheap_asymmetric_skips_dead_side():
    cfg = _no_blend()
    snap = snapshot(delta=-0.004, remaining=100, sigma_annual=0.4, cfg=cfg)  # Up nearly dead
    ctx = context(CheapAsymmetricConfig(), snap, up=(0.18, 0.20), down=(0.80, 0.82), cfg=cfg)
    assert CheapAsymmetricStrategy(CheapAsymmetricConfig()).evaluate(ctx) == []
