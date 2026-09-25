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
from updown.model import fair_prob_up, fair_prob_up_twap
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
        noise_flow: float = 0.3,
        twap_window: int = 60,
    ):
        self.twap_window = twap_window
        self.noise_flow = noise_flow  # chance per second per token of an uninformed trade
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

    def twap(self, t: int) -> float | None:
        """The oracle's TWAP at second t: average over the last twap_window seconds
        (the settlement rule since Aug 2026). With twap_window 0, the price at t."""
        w = self.twap_window
        if w <= 0:
            return self.oracle.get(t)
        pts = [self.oracle[s] for s in range(t - w + 1, t + 1) if s in self.oracle]
        return sum(pts) / len(pts) if pts else None

    def market_prob_up(self, start: int) -> float:
        """Market maker's quote for Up: fair value computed from a stale price, plus noise.
        Cached per second so both books agree within a tick."""
        key = (start, int(self.now))
        if key not in self._mm_quote:
            stale_t = max(start, int(self.now - self.lag_s))
            end = start + self.window_seconds
            left = end - self.now
            w = self.twap_window
            seen = [self.oracle[s] for s in range(end - w + 1, stale_t + 1) if s in self.oracle] if w > 0 else []
            p = fair_prob_up_twap(self.oracle[stale_t], self.twap(start), left, self.sigma, w,
                                  observed_avg=sum(seen) / len(seen) if seen else None)
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
            price_to_beat=self.world.twap(start) if start in self.world.oracle else None,
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

    def trades(self, condition_id: str) -> list:
        """Uninformed takers: now and then someone sells into the best bid or
        buys the best ask, for no reason related to the price. This is the
        flow a market maker earns on. How much of it the real market has is
        exactly what paper trading has to measure; here it's a guess."""
        from updown.markets import TradePrint

        wm = next((m for m in self._markets.values() if m.condition_id == condition_id), None)
        if wm is None:
            return []
        rng, out = self.world.rng, []
        for token in wm.tokens.values():
            if rng.random() < self.world.noise_flow:
                book = self.book(token)
                side = "SELL" if rng.random() < 0.5 else "BUY"
                px = book.best_bid if side == "SELL" else book.best_ask
                if px is not None:
                    out.append(TradePrint(token, side, px, rng.uniform(5, 40), self.world.now, f"{token}:{self.world.now}:{side}"))
        return out

    def resolution(self, wm: WindowMarket) -> str | None:
        if wm.end not in self.world.oracle:
            return None
        return UP if self.world.twap(wm.end) >= self.world.twap(wm.start) else DOWN


def run_simulation(engine, world: SimWorld, history, windows: int, asset: str = "btc", t0: int = 1_800_000_000) -> None:
    """Drive the engine second by second through `windows` windows (no sleeping)."""
    start = window_start(t0, world.window_seconds) - 90  # room for the opening 60s TWAP
    end = start + 90 + windows * world.window_seconds + 30
    for t in range(start, end):
        price = world.step(t)
        history.add(asset, float(t), price)
        engine.on_price(asset, float(t), price)
        engine.tick(float(t))


# Rough starting prices and per-second vols, so the demo looks like each coin.
SIM_COINS = {
    "btc": (100_000.0, 9e-5),
    "eth": (3_500.0, 1.2e-4),
    "sol": (180.0, 1.6e-4),
    "xrp": (2.5, 1.5e-4),
    "bnb": (650.0, 1.0e-4),
    "doge": (0.25, 1.8e-4),
    "hype": (40.0, 2.0e-4),
}


class MultiSimGateway:
    """One SimWorld per coin behind a single gateway, like Polymarket's one API."""

    def __init__(self, assets: list[str], seed: int = 7, lag_s: float = 1.5, mm_noise: float = 0.01):
        self.worlds = {}
        self.gateways = {}
        for i, a in enumerate(assets):
            price, sigma = SIM_COINS.get(a, (100.0, 1.2e-4))
            self.worlds[a] = SimWorld(seed=seed + i * 101, start_price=price, sigma=sigma, lag_s=lag_s, mm_noise=mm_noise)
            self.gateways[a] = SimGateway(self.worlds[a])

    def step(self, t: int) -> dict[str, float]:
        return {a: w.step(t) for a, w in self.worlds.items()}

    def discover(self, asset: str, start: int):
        return self.gateways[asset].discover(asset, start)

    def book(self, token_id: str) -> Book:
        return self.gateways[token_id.split("-", 1)[0]].book(token_id)

    def books(self, token_ids: list[str]) -> dict[str, Book]:
        return {t: self.book(t) for t in token_ids}

    def trades(self, condition_id: str) -> list:
        asset = condition_id.split("-")[1] if condition_id.startswith("sim-") else ""
        gw = self.gateways.get(asset)
        return gw.trades(condition_id) if gw else []

    def resolution(self, wm: WindowMarket) -> str | None:
        return self.gateways[wm.asset].resolution(wm)


def step_multi(engine, sim: MultiSimGateway, history, t: int) -> None:
    for asset, price in sim.step(t).items():
        history.add(asset, float(t), price)
        engine.on_price(asset, float(t), price)
    engine.tick(float(t))
