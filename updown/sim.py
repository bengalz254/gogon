"""Offline simulator: a synthetic price path plus a synthetic Polymarket order book.

It exists to exercise the real engine end to end (discovery, strike capture,
volatility, sizing, exits, settlement, risk limits) without network access.

What it can NOT tell you is whether the strategy makes money on Polymarket.
The simulated market maker prices off a price that's `lag_s` seconds old
plus noise; with lag > 0 there is edge by construction, with lag = 0 there
is none. Real edge has to be measured in paper mode against the real book.
"""
from __future__ import annotations

import math
import random

from updown.markets import WindowMarket, window_start
from updown.model import fair_prob_up
from updown.strategy import DOWN, UP, Book


class SimWorld:
    def __init__(
        self,
        seed: int = 7,
        start_price: float = 100_000.0,
        sigma: float = 9e-5,
        lag_s: float = 3.0,
        mm_noise: float = 0.01,
        half_spread: float = 0.01,
        feed_basis: float = 0.0004,
        window_seconds: int = 300,
    ):
        self.rng = random.Random(seed)
        self.sigma = sigma
        self.lag_s = lag_s
        self.mm_noise = mm_noise
        self.half_spread = half_spread
        self.feed_basis = feed_basis  # our feed = oracle * (1 + basis): a constant gap
        self.window_seconds = window_seconds
        self.oracle: dict[int, float] = {}
        self._price = start_price
        self._last_t: int | None = None
        self.now = 0.0
        self._mm_quote: dict[tuple[int, int], float] = {}

    def step(self, t: int) -> float:
        """Advance to second t; returns our feed's price at t."""
        if self._last_t is None:
            self._last_t = t - 1
        for s in range(self._last_t + 1, t + 1):
            # Student-t-ish shocks: mostly normal, occasionally a jump.
            z = self.rng.gauss(0, 1)
            if self.rng.random() < 0.002:
                z *= 6
            self._price *= math.exp(self.sigma * z)
            self.oracle[s] = self._price
        self._last_t = t
        self.now = t
        return self.oracle[t] * (1 + self.feed_basis)

    def market_prob_up(self, start: int) -> float:
        """Market maker's quote for Up: fair value computed from a stale price, plus noise.
        Cached per second so both books agree within a tick."""
        key = (start, int(self.now))
        if key not in self._mm_quote:
            stale_t = max(start, int(self.now - self.lag_s))
            p = fair_prob_up(self.oracle[stale_t], self.oracle[start], start + self.window_seconds - self.now, self.sigma)
            p += self.rng.gauss(0, self.mm_noise)
            self._mm_quote[key] = min(0.98, max(0.02, p))
        return self._mm_quote[key]


class SimGateway:
    def __init__(self, world: SimWorld):
        self.world = world
        self._markets: dict[str, WindowMarket] = {}

    def discover(self, asset: str, start: int) -> WindowMarket | None:
        wm = WindowMarket(
            asset=asset,
            start=start,
            end=start + self.world.window_seconds,
            slug=f"{asset}-updown-5m-{start}",
            condition_id=f"sim-{asset}-{start}",
            tokens={UP: f"{asset}-{start}-up", DOWN: f"{asset}-{start}-down"},
            price_to_beat=self.world.oracle.get(start),
        )
        self._markets[wm.tokens[UP]] = wm
        self._markets[wm.tokens[DOWN]] = wm
        return wm

    def book(self, token_id: str) -> Book:
        wm = self._markets[token_id]
        p_up = self.world.market_prob_up(wm.start)
        p = p_up if token_id.endswith("-up") else 1.0 - p_up
        hs = self.world.half_spread
        rng = self.world.rng
        bids, asks = [], []
        for i in range(3):
            bid = round(p - hs - 0.01 * i, 2)
            ask = round(p + hs + 0.01 * i, 2)
            if 0.01 <= bid <= 0.99:
                bids.append((bid, rng.uniform(20, 150)))
            if 0.01 <= ask <= 0.99:
                asks.append((ask, rng.uniform(20, 150)))
        return Book.from_raw(bids, asks)

    def resolution(self, wm: WindowMarket) -> str | None:
        end_price = self.world.oracle.get(wm.end)
        if end_price is None:
            return None
        return UP if end_price >= self.world.oracle[wm.start] else DOWN


def run_simulation(engine, world: SimWorld, history, windows: int, asset: str = "btc", t0: int = 1_800_000_000) -> None:
    """Drive the engine second by second through `windows` windows (no sleeping)."""
    start = window_start(t0, world.window_seconds) - 30
    end = start + 30 + windows * world.window_seconds + 30
    for t in range(start, end):
        price = world.step(t)
        history.add(asset, float(t), price)
        engine.on_price(asset, float(t), price)
        engine.tick(float(t))
