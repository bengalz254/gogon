"""Synthetic market simulator for the Up/Down engine (offline, no network).

It generates correlated random-walk prices for several coins, an oracle
feed that lags the "exchange" price, and a deliberately imperfect market
maker that quotes each window's Up/Down book from a stale spot price, an
overestimated volatility and some persistent noise. The real engine,
strategies, risk layer and paper exchange then trade against it.

What this is for: exercising the whole pipeline end to end (discovery ->
state machine -> model -> strategies -> risk -> orders -> fills ->
settlement -> journals) and comparing model calibration against the
market maker. What it is NOT: evidence that any strategy makes money on
Polymarket. P&L here comes from inefficiencies the simulator injects on
purpose. Only paper trading on real data (and then tiny live size) can
tell you whether an edge exists.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from bot.updown.config import UpDownConfig
from bot.updown.engine import Engine
from bot.updown.events import BookSnapshot, CexTick, OfficialResolution, OracleTick, TradePrint, WindowListed
from bot.updown.journal import NullJournal
from bot.updown.mathutil import clamp
from bot.updown.model import annual_to_per_second, prob_at_least, settlement_distribution
from bot.updown.orders import PaperBroker
from bot.updown.risk import UpDownRisk
from bot.updown.series import PriceSeries
from bot.updown.strategies import build_strategies
from bot.updown.window import DOWN, UP, WindowSpec


@dataclass
class SimConfig:
    assets: list = field(default_factory=lambda: ["btc", "eth", "sol", "xrp", "doge"])
    windows: int = 24
    interval_s: int = 300
    warmup_s: int = 1800
    start_ts: int = 1_760_000_100
    annual_vol: dict = field(default_factory=lambda: {"btc": 0.55, "eth": 0.70, "sol": 0.90, "xrp": 0.90, "doge": 1.00})
    start_price: dict = field(default_factory=lambda: {"btc": 60000.0, "eth": 2500.0, "sol": 150.0, "xrp": 0.6, "doge": 0.12})
    common_factor: float = 0.85  # loading on the shared market shock
    oracle_lag_s: int = 1
    mm_lag_s: int = 5  # market maker quotes off a spot this many seconds old
    mm_vol_mult: float = 1.35  # ... with this much too much volatility
    mm_noise: float = 0.015  # ... plus persistent noise in its probability
    mm_spread: float = 0.02
    mm_levels: int = 5
    mm_depth: float = 150.0
    trade_rate: float = 0.4  # prints per second per token
    steps_per_second: int = 2
    seed: int = 7


@dataclass
class SimResult:
    windows: int
    trades: int
    pnl_by_strategy: dict
    wins_by_strategy: dict
    count_by_strategy: dict
    brier_model: float | None
    brier_market: float | None
    calibration_points: int
    settlement_mismatches: int
    engine: Engine = field(repr=False, default=None)

    def summary(self) -> str:
        lines = [
            f"windows simulated: {self.windows}   fills: {self.trades}",
            f"Brier score (lower is better): model={_f(self.brier_model)}  market maker={_f(self.brier_market)}  "
            f"(n={self.calibration_points})",
            f"engine-computed vs simulated official settlement mismatches: {self.settlement_mismatches}",
            "P&L by strategy (synthetic market! not evidence of real edge):",
        ]
        if not self.pnl_by_strategy:
            lines.append("  (no positions taken)")
        for name, pnl in sorted(self.pnl_by_strategy.items()):
            n = self.count_by_strategy.get(name, 0)
            wins = self.wins_by_strategy.get(name, 0)
            lines.append(f"  {name:<17} windows={n:<4} win_rate={wins / n if n else 0:>5.0%}  pnl=${pnl:+.2f}")
        return "\n".join(lines)


def _f(x):
    return "n/a" if x is None else f"{x:.4f}"


def default_engine_config() -> UpDownConfig:
    cfg = UpDownConfig()
    cfg.settlement.use_official = True
    cfg.feeds.cex_source = "none"
    return cfg


def run_simulation(sim: SimConfig | None = None, cfg: UpDownConfig | None = None, journal=None,
                   verbose: bool = False, recorder=None) -> SimResult:
    sim = sim or SimConfig()
    cfg = cfg or default_engine_config()
    rng = random.Random(sim.seed)
    interval = sim.interval_s
    cfg.markets.assets = list(sim.assets)
    cfg.markets.interval = f"{interval}s"

    t0 = (sim.start_ts // interval) * interval + interval  # first window start
    t_begin = t0 - sim.warmup_s
    t_end = t0 + sim.windows * interval + 30

    risk = UpDownRisk(cfg.risk, now=t_begin)
    engine = Engine(cfg, build_strategies(cfg.strategies), risk, journal or NullJournal(), mode="sim")
    broker = PaperBroker(engine)

    def feed(ev, now):
        if recorder is not None:
            recorder.record(ev, now)
        engine.handle(ev, now)
        for u in broker.on_event(ev, now):
            engine.on_order_update(u)

    sig = {a: annual_to_per_second(sim.annual_vol.get(a, 0.8)) for a in sim.assets}
    price = {a: sim.start_price.get(a, 100.0) for a in sim.assets}
    history = {a: [] for a in sim.assets}  # true price per second
    oracle_series = {a: PriceSeries() for a in sim.assets}
    mm_noise = {a: 0.0 for a in sim.assets}
    samples = cfg.settle_samples
    load = sim.common_factor
    idio = math.sqrt(max(0.0, 1 - load * load))

    specs: dict[tuple[str, int], WindowSpec] = {}
    for k in range(sim.windows):
        start = t0 + k * interval
        for a in sim.assets:
            specs[(a, k)] = WindowSpec(
                window_id=f"{a}-updown-sim-{start}", asset=a, interval_s=interval, start=start,
                end=start + interval, condition_id=f"cond-{a}-{start}", up_token=f"{a}-{start}-up",
                down_token=f"{a}-{start}-down", tick_size=0.01, min_order_size=5.0,
                question=f"{a.upper()} Up or Down (sim) {start}",
            )
    listed: set = set()
    resolved: set = set()
    official: dict[str, str] = {}
    calib: list[tuple[float, float, str]] = []  # (p_model, p_market, window_id)

    t = t_begin
    while t <= t_end:
        # 1) prices
        common = rng.gauss(0, 1)
        for a in sim.assets:
            shock = load * common + idio * rng.gauss(0, 1)
            price[a] *= math.exp(sig[a] * shock - 0.5 * sig[a] ** 2)
            history[a].append(price[a])
            feed(CexTick(a, t + 0.05, price[a]), t + 0.05)
            lagged = history[a][-1 - sim.oracle_lag_s] if len(history[a]) > sim.oracle_lag_s else history[a][0]
            oracle_series[a].update(t + 0.2, lagged)
            feed(OracleTick(a, t + 0.2, lagged), t + 0.2)

        # 2) windows: listing, market-maker books, trades, resolution
        for (a, k), spec in specs.items():
            if spec.window_id not in listed and t >= spec.start - 90:
                listed.add(spec.window_id)
                feed(WindowListed(spec), t)
            if spec.start <= t < spec.end:
                p_mm = _market_maker_prob(sim, a, spec, t, history[a], oracle_series[a], sig[a], samples, rng, mm_noise)
                for ev in _books(spec, p_mm, sim, t + 0.3):
                    feed(ev, t + 0.3)
                for ev in _trades(spec, engine, sim, rng, t + 0.4):
                    feed(ev, t + 0.4)
                w = engine.windows.get(spec.window_id)
                if w is not None and w.last_model is not None and int(t) % 10 == 0:
                    calib.append((w.last_model.p_up, p_mm, spec.window_id))
            if spec.window_id not in resolved and t >= spec.end + 20:
                resolved.add(spec.window_id)
                settle = oracle_series[a].sample_mean(int(spec.end) - samples + 1, int(spec.end))
                ptb = oracle_series[a].price_at(spec.start)
                if settle is not None and ptb is not None:
                    winner = UP if settle >= ptb else DOWN
                    official[spec.window_id] = winner
                    feed(OfficialResolution(spec.window_id, winner), t)

        # 3) engine steps
        for i in range(sim.steps_per_second):
            now = t + 0.5 + i / sim.steps_per_second * 0.49
            actions = engine.step(now)
            for u in broker.execute(actions, now):
                engine.on_order_update(u)
        if verbose and int(t - t0) % 3600 == 0 and t >= t0:
            print(engine.status_line())
        t += 1

    for _ in range(3):  # flush settlements
        engine.step(t_end + cfg.settlement.settle_grace_s + 60)

    pnl, wins, counts = {}, {}, {}
    mismatches = 0
    for r in engine.results.values():
        if r["official"] and r["provisional"] and r["official"] != r["provisional"]:
            mismatches += 1
        for strat, p in r["pnl"].items():
            pnl[strat] = pnl.get(strat, 0.0) + p
            counts[strat] = counts.get(strat, 0) + 1
            wins[strat] = wins.get(strat, 0) + (1 if p > 0 else 0)

    brier_model = brier_market = None
    pts = [(pm, pk, official[wid]) for pm, pk, wid in calib if wid in official]
    if pts:
        brier_model = sum((pm - (1.0 if o == UP else 0.0)) ** 2 for pm, _, o in pts) / len(pts)
        brier_market = sum((pk - (1.0 if o == UP else 0.0)) ** 2 for _, pk, o in pts) / len(pts)

    return SimResult(
        windows=len(specs), trades=engine.stats["fills"], pnl_by_strategy=pnl, wins_by_strategy=wins,
        count_by_strategy=counts, brier_model=brier_model, brier_market=brier_market,
        calibration_points=len(pts), settlement_mismatches=mismatches, engine=engine,
    )


def _market_maker_prob(sim, asset, spec, t, hist, oracle, sigma, samples, rng, noise_state) -> float:
    lag = min(len(hist) - 1, sim.mm_lag_s + sim.oracle_lag_s)
    stale_spot = hist[-1 - lag]
    ptb = oracle.price_at(spec.start) or stale_spot
    first = int(spec.end) - samples + 1
    realized = oracle.sample_mean(first, int(t)) if int(t) >= first else None
    dist = settlement_distribution(
        now=t, end=spec.end, samples=samples, spot=stale_spot, sigma=sigma * sim.mm_vol_mult,
        realized_mean=realized,
    )
    p = prob_at_least(dist, ptb, "normal", 0)
    noise_state[asset] = 0.9 * noise_state[asset] + rng.gauss(0, sim.mm_noise * math.sqrt(1 - 0.81))
    return clamp(p + noise_state[asset], 0.02, 0.98)


def _books(spec: WindowSpec, p_up: float, sim: SimConfig, ts: float):
    tick = spec.tick_size
    half = sim.mm_spread / 2.0
    bid0 = math.floor((p_up - half) / tick + 1e-9) * tick
    ask0 = math.ceil((p_up + half) / tick - 1e-9) * tick
    ask0 = max(ask0, bid0 + tick)
    up_bids = tuple((round(bid0 - i * tick, 4), sim.mm_depth) for i in range(sim.mm_levels) if bid0 - i * tick >= tick)
    up_asks = tuple((round(ask0 + i * tick, 4), sim.mm_depth) for i in range(sim.mm_levels) if ask0 + i * tick <= 1 - tick)
    down_bids = tuple((round(1 - p, 4), s) for p, s in up_asks)
    down_asks = tuple((round(1 - p, 4), s) for p, s in up_bids)
    return [
        BookSnapshot(spec.up_token, ts, up_bids, up_asks),
        BookSnapshot(spec.down_token, ts, down_bids, down_asks),
    ]


def _trades(spec: WindowSpec, engine: Engine, sim: SimConfig, rng: random.Random, ts: float):
    out = []
    for tok in (spec.up_token, spec.down_token):
        book = engine.books.get(tok)
        if book is None or not book.has_snapshot:
            continue
        n = _poisson(rng, sim.trade_rate)
        for _ in range(n):
            if rng.random() < 0.5 and book.best_bid is not None:
                out.append(TradePrint(tok, ts, book.best_bid, rng.uniform(5, 60), "SELL"))
            elif book.best_ask is not None:
                out.append(TradePrint(tok, ts, book.best_ask, rng.uniform(5, 60), "BUY"))
    return out


def _poisson(rng: random.Random, lam: float) -> int:
    k, p, threshold = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1
