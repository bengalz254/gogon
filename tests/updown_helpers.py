"""Shared builders for Up/Down engine tests (no network)."""
from __future__ import annotations

from bot.updown.book import OrderBook
from bot.updown.config import UpDownConfig
from bot.updown.fees import FeeModel
from bot.updown.model import VolEstimate, annual_to_per_second, evaluate_window
from bot.updown.strategies.base import StrategyContext
from bot.updown.window import WindowSpec, WindowState

T0 = 1_760_000_100.0  # window start used throughout the tests


def spec(asset="btc", start=T0, interval=300, tick=0.01, min_size=5.0) -> WindowSpec:
    return WindowSpec(
        window_id=f"{asset}-updown-5m-{int(start)}", asset=asset, interval_s=interval,
        start=start, end=start + interval, condition_id=f"c-{asset}", up_token=f"{asset}-up",
        down_token=f"{asset}-down", tick_size=tick, min_order_size=min_size,
    )


def book(token, bid=None, ask=None, size=100.0, tick=0.01, levels=1) -> OrderBook:
    b = OrderBook(token, tick_size=tick)
    bids = [(round(bid - i * tick, 4), size) for i in range(levels)] if bid is not None else []
    asks = [(round(ask + i * tick, 4), size) for i in range(levels)] if ask is not None else []
    b.apply_snapshot(bids, asks, T0)
    return b


def vol(sigma_annual=0.5, short=None, long=None) -> VolEstimate:
    s = annual_to_per_second(sigma_annual)
    return VolEstimate(
        sigma=s, sigma_short=short if short is not None else s, sigma_long=long if long is not None else s,
        floor=0.0, n_short=36, n_long=360, source="oracle",
    )


def snapshot(*, delta=0.0, remaining=150.0, sigma_annual=0.5, ptb=100.0, realized_mean=None,
             cfg: UpDownConfig | None = None, start=T0, interval=300, v: VolEstimate | None = None, drift=0.0):
    cfg = cfg or UpDownConfig()
    end = start + interval
    now = end - remaining
    spot = ptb * (1 + delta)
    samples = cfg.settle_samples
    first = int(end) - samples + 1
    if realized_mean is None and int(now) >= first:
        realized_mean = spot
    return evaluate_window(
        now=now, start=start, end=end, samples=samples, ptb=ptb, spot=spot, oracle_price=spot,
        oracle_age=0.2, cex_price=None, vol=v or vol(sigma_annual), realized_mean=realized_mean,
        cfg=cfg.model, drift=drift,
    )


def context(strategy_cfg, snap, *, up=(0.49, 0.51), down=(0.49, 0.51), name="s", asset="btc",
            cfg: UpDownConfig | None = None, slot_deltas=None, asset_return=None, spot_change=None,
            remodel=None, window: WindowState | None = None, size=100.0) -> StrategyContext:
    cfg = cfg or UpDownConfig()
    sp = spec(asset)
    w = window or WindowState(spec=sp, ptb=snap.ptb)
    return StrategyContext(
        now=snap.ts, spec=sp, window=w, model=snap,
        up_book=book(sp.up_token, *up, size=size), down_book=book(sp.down_token, *down, size=size),
        fees=FeeModel.from_config(cfg.fees), params=strategy_cfg, session="us", strategy=name,
        slippage=cfg.risk.slippage_buffer, market_blend=cfg.model.market_blend,
        slot_deltas=slot_deltas or {}, asset_return=asset_return or (lambda a, lb: 0.0),
        spot_change=spot_change or (lambda lb: 0.0), remodel=remodel or (lambda **kw: snap),
    )
