"""Configuration for the Up/Down engine (config/updown.yaml).

Every section is a dataclass with safe defaults. The loader rejects unknown
keys, because a typo in a trading config ("min_edgee: 0.2") silently falling
back to a default is exactly the kind of bug that loses money.

Strategy sections support `overrides`: parameter patches applied only for
some assets and/or trading sessions, so one model/threshold set doesn't
have to fit every coin and every hour (Asia vs US microstructure differs).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timezone

import yaml


class ConfigError(ValueError):
    pass


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
@dataclass
class MarketsConfig:
    # Assets to watch. Constellation uses all of them as "peers"; markets are
    # traded only where Gamma lists a window for the asset.
    assets: list = field(default_factory=lambda: ["btc", "eth", "sol", "xrp", "doge"])
    interval: str = "5m"
    # Gamma slug of one window; {start} is the window's unix start time.
    slug_template: str = "{asset}-updown-{interval}-{start}"
    discover_ahead: int = 2  # also discover this many upcoming windows
    discovery_interval_s: float = 20.0
    # asset -> RTDS Chainlink symbol / CEX symbol; defaults "btc/usd", "btcusdt".
    chainlink_symbols: dict = field(default_factory=dict)
    cex_symbols: dict = field(default_factory=dict)

    @property
    def interval_seconds(self) -> int:
        return parse_interval(self.interval)

    def chainlink_symbol(self, asset: str) -> str:
        return self.chainlink_symbols.get(asset, f"{asset}/usd")

    def cex_symbol(self, asset: str) -> str:
        return self.cex_symbols.get(asset, f"{asset}usdt")


@dataclass
class FeedsConfig:
    rtds_url: str = "wss://ws-live-data.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_url: str = "https://gamma-api.polymarket.com"
    # Fast leading indicator (never used for settlement): rtds | binance | bybit | none
    cex_source: str = "rtds"
    binance_ws_url: str = "wss://stream.binance.com:9443/stream"
    bybit_ws_url: str = "wss://stream.bybit.com/v5/public/spot"
    binance_rest_url: str = "https://api.binance.com"
    bybit_rest_url: str = "https://api.bybit.com"
    bootstrap_history: bool = True  # seed ~1h of 1m klines at startup
    # Staleness is measured from when prices *arrive* (immune to local clock
    # skew): no new oracle / CEX price for this long -> data counts as stale.
    oracle_max_age_s: float = 5.0
    cex_max_age_s: float = 3.0
    rtds_ping_s: float = 5.0
    rtds_stale_s: float = 10.0  # reconnect an RTDS socket after this long without a new price
    clob_ping_s: float = 10.0
    cex_throttle_ms: float = 100.0
    dynamic_subscribe: bool = True


@dataclass
class SettlementConfig:
    # How the oracle settles a window: "twap" = mean of per-second Chainlink
    # samples over the last `twap_seconds`; "last" = the single price at close.
    # VERIFY against the market's rules text; the bot logs both computed
    # outcomes next to the official one so you can check which rule matches.
    rule: str = "twap"
    twap_seconds: int = 60
    ptb_tolerance_s: float = 2.0  # max distance from t=0 for locking price_to_beat
    prefer_official_ptb: bool = True
    settle_grace_s: float = 3.0
    use_official: bool = True  # wait for Gamma's resolution before booking P&L
    official_poll_s: float = 15.0
    official_timeout_s: float = 1800.0


@dataclass
class ModelConfig:
    distribution: str = "student_t"  # student_t | normal
    tail_dof: float = 5.0
    vol_source: str = "auto"  # auto | oracle | cex
    vol_sample_s: float = 5.0
    vol_short_s: float = 180.0
    vol_long_s: float = 1800.0
    short_weight: float = 0.5
    min_vol_samples: int = 12
    vol_floor_annual: dict = field(
        default_factory=lambda: {
            "btc": 0.25, "eth": 0.35, "sol": 0.45, "xrp": 0.45,
            "doge": 0.55, "bnb": 0.30, "hype": 0.60,
        }
    )
    default_vol_floor_annual: float = 0.40
    # Robustness band: trade only if the edge survives sigma*(1 +/- this).
    vol_uncertainty: float = 0.20
    cex_lead_weight: float = 0.5
    basis_halflife_s: float = 60.0
    max_cex_oracle_divergence: float = 0.003
    # Shrink toward the market price: p = (1-w)*p_model + w*p_market.
    market_blend: float = 0.2

    def vol_floor(self, asset: str) -> float:
        return float(self.vol_floor_annual.get(asset, self.default_vol_floor_annual))


@dataclass
class FeesConfig:
    # Taker fee per share = p * taker_rate * (p*(1-p))**taker_exponent.
    # Defaults follow Polymarket's crypto up/down fee curve (~1.56% of notional
    # at 50c, near zero at the extremes). VERIFY against current docs.
    taker_rate: float = 0.25
    taker_exponent: float = 2.0
    maker_rate: float = 0.0


@dataclass
class RiskConfig:
    bankroll_usd: float = 200.0
    kelly_fraction: float = 0.15
    max_usd_per_trade: float = 10.0
    max_usd_per_window: float = 20.0
    max_total_exposure_usd: float = 80.0
    # Coins are highly correlated inside a window: cap the USD bet on the same
    # direction (all Up or all Down) across assets in one time slot.
    max_slot_direction_usd: float = 40.0
    max_open_windows: int = 8
    max_daily_loss_usd: float = 30.0
    max_consecutive_losses: int = 4
    cooldown_s_after_streak: float = 900.0
    loss_streak_decay: float = 0.75  # size multiplier per consecutive loss (<= 1: never martingale)
    max_buy_price: float = 0.985
    min_buy_price: float = 0.03
    slippage_buffer: float = 0.005
    # Hard ban: taker entries in the first N seconds while price is near 50c.
    no_early_taker_s: float = 60.0
    early_taker_band: list = field(default_factory=lambda: [0.40, 0.60])
    bump_to_min_size: bool = True


@dataclass
class ExecutionConfig:
    # Second opt-in for real money on top of LIVE_TRADING=true in .env: this
    # engine is new and unverified against the live API, so it refuses to
    # trade live unless this is also true.
    allow_live: bool = False
    step_interval_s: float = 0.25
    requote_min_interval_s: float = 1.0
    requote_min_ticks: int = 2  # keep a live quote unless the target price moved this many ticks

    cancel_quotes_before_end_s: float = 2.0
    live_order_poll_s: float = 1.5
    heartbeat: bool = True
    prewarm: bool = True
    max_orders_per_minute: int = 120  # runaway guard


@dataclass
class JournalConfig:
    dir: str = "data"
    trades_csv: str = "data/trades.csv"
    snapshot_every_s: float = 5.0
    record_events: bool = False
    record_dir: str = "data/recordings"


# -- strategies --------------------------------------------------------------
@dataclass
class Override:
    assets: list = field(default_factory=list)  # empty = every asset
    sessions: list = field(default_factory=list)  # empty = every session
    set: dict = field(default_factory=dict)


@dataclass
class StrategyConfig:
    enabled: bool = False
    max_usd_per_window: float = 10.0
    max_usd_per_trade: float = 10.0
    size_mult: float = 1.0
    overrides: list = field(default_factory=list)


@dataclass
class FairValueConfig(StrategyConfig):
    enabled: bool = True
    min_edge: float = 0.05
    min_remaining_s: float = 10.0
    max_remaining_s: float = 290.0
    max_entries_per_window: int = 2
    min_entry_spacing_s: float = 15.0
    min_price: float = 0.05
    max_price: float = 0.95
    tif: str = "FAK"


@dataclass
class LateCertaintyConfig(StrategyConfig):
    enabled: bool = True
    max_usd_per_window: float = 5.0
    max_usd_per_trade: float = 5.0
    min_remaining_s: float = 20.0
    max_remaining_s: float = 70.0
    min_model_prob: float = 0.95
    bid_min: float = 0.92
    bid_max: float = 0.985
    max_price: float = 0.985
    min_edge: float = 0.01
    max_vol_ratio: float = 1.6  # sigma_short / sigma_long: skip vol spikes
    max_sigma_annual: float = 0.0  # 0 = off
    approach_lookback_s: float = 15.0
    min_projected_z: float = 1.5  # settlement must stay this many sd away if the recent drift continues
    improve_by_tick: bool = True


@dataclass
class ConstellationConfig(StrategyConfig):
    enabled: bool = True
    max_usd_per_window: float = 8.0
    max_usd_per_trade: float = 8.0
    eval_start_s: float = 60.0  # elapsed seconds since window open
    eval_end_s: float = 120.0
    move_threshold: float = 0.0003  # 0.03%
    flat_threshold: float = 0.0002  # 0.02%
    min_consensus: int = 4
    max_opposing: int = 0
    ask_min: float = 0.49
    ask_max: float = 0.56
    reference_asset: str = "btc"
    max_ref_1h_abs_return: float = 0.02
    require_ref_1h: bool = True
    catchup_factor: float = 0.5
    min_edge: float = 0.03
    max_entries_per_window: int = 1


@dataclass
class PairBarbellConfig(StrategyConfig):
    enabled: bool = False
    max_usd_per_window: float = 20.0
    max_usd_per_trade: float = 10.0
    open_phase_s: float = 45.0
    side_ask_min: float = 0.44
    side_ask_max: float = 0.56
    pair_max_cost: float = 0.99
    base_shares: float = 10.0
    tilt_start_s: float = 60.0
    tilt_min_remaining_s: float = 45.0
    confirm_prob: float = 0.62
    tilt_ratio: float = 1.5
    tilt_min_edge: float = 0.03  # maker tilt bid: p - price >= this
    tilt_take_min_edge: float = 0.05  # taker tilt: p - ask - fees >= this


@dataclass
class CheapAsymmetricConfig(StrategyConfig):
    enabled: bool = False
    max_usd_per_window: float = 3.0
    max_usd_per_trade: float = 3.0
    min_remaining_s: float = 90.0
    ask_min: float = 0.18
    ask_max: float = 0.42
    min_edge: float = 0.03
    min_model_prob: float = 0.20
    min_overpricing: float = 0.03
    max_entries_per_window: int = 1


@dataclass
class StrategiesConfig:
    fair_value: FairValueConfig = field(default_factory=FairValueConfig)
    late_certainty: LateCertaintyConfig = field(default_factory=LateCertaintyConfig)
    constellation: ConstellationConfig = field(default_factory=ConstellationConfig)
    pair_barbell: PairBarbellConfig = field(default_factory=PairBarbellConfig)
    cheap_asymmetric: CheapAsymmetricConfig = field(default_factory=CheapAsymmetricConfig)


@dataclass
class UpDownConfig:
    markets: MarketsConfig = field(default_factory=MarketsConfig)
    feeds: FeedsConfig = field(default_factory=FeedsConfig)
    settlement: SettlementConfig = field(default_factory=SettlementConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    fees: FeesConfig = field(default_factory=FeesConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    strategies: StrategiesConfig = field(default_factory=StrategiesConfig)
    # UTC hour ranges [start, end); a range may wrap past midnight.
    sessions: dict = field(
        default_factory=lambda: {"asia": [0, 8], "europe": [8, 13], "us": [13, 21], "late_us": [21, 24]}
    )
    journal: JournalConfig = field(default_factory=JournalConfig)

    @property
    def settle_samples(self) -> int:
        """Number of per-second oracle samples averaged at settlement."""
        return max(1, int(self.settlement.twap_seconds)) if self.settlement.rule == "twap" else 1


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def parse_interval(label: str) -> int:
    label = label.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not label or label[-1] not in units:
        raise ConfigError(f"bad interval {label!r} (use e.g. 5m, 15m, 1h)")
    return int(float(label[:-1]) * units[label[-1]])


def session_of(ts: float, sessions: dict) -> str:
    hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
    for name, (start, end) in sessions.items():
        start, end = int(start), int(end)
        if start <= end:
            if start <= hour < end:
                return name
        elif hour >= start or hour < end:
            return name
    return "default"


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _coerce(current, value, path: str):
    try:
        if isinstance(current, bool):
            return _to_bool(value)
        if isinstance(current, int):
            return int(value)
        if isinstance(current, float):
            return float(value)
        if isinstance(current, str):
            return str(value)
        if isinstance(current, list):
            return list(value)
        if isinstance(current, dict):
            return dict(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{path}: cannot use {value!r} ({e})") from e
    return value


_REPLACE_DICTS = {"sessions"}  # dict keys replaced wholesale instead of merged


def _apply(obj, raw, path: str) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping, got {type(raw).__name__}")
    names = {f.name for f in fields(obj)}
    for key, value in raw.items():
        if key not in names:
            raise ConfigError(f"unknown config key '{path}.{key}'")
        current = getattr(obj, key)
        sub = f"{path}.{key}"
        if is_dataclass(current):
            _apply(current, value, sub)
        elif key == "overrides":
            setattr(obj, key, [_build_override(obj, o, f"{sub}[{i}]") for i, o in enumerate(value or [])])
        elif isinstance(current, dict) and key not in _REPLACE_DICTS:
            merged = dict(current)
            merged.update(_coerce(current, value or {}, sub))
            setattr(obj, key, merged)
        else:
            setattr(obj, key, _coerce(current, value, sub))


def _build_override(strategy_cfg, raw, path: str) -> Override:
    ov = Override()
    _apply(ov, raw, path)
    ov.assets = [str(a).lower() for a in ov.assets]
    allowed = {f.name for f in fields(strategy_cfg)} - {"enabled", "overrides"}
    for key, value in ov.set.items():
        if key not in allowed:
            raise ConfigError(f"{path}.set: unknown parameter '{key}'")
        ov.set[key] = _coerce(getattr(strategy_cfg, key), value, f"{path}.set.{key}")
    return ov


def resolve_params(cfg: StrategyConfig, asset: str, session: str):
    """Strategy params with matching overrides applied, in listed order."""
    out = cfg
    for ov in cfg.overrides:
        if ov.assets and asset not in ov.assets:
            continue
        if ov.sessions and session not in ov.sessions:
            continue
        out = replace(out, **ov.set)
    return out


def validate(cfg: UpDownConfig) -> None:
    if cfg.settlement.rule not in ("twap", "last"):
        raise ConfigError("settlement.rule must be 'twap' or 'last'")
    if cfg.model.distribution not in ("student_t", "normal"):
        raise ConfigError("model.distribution must be 'student_t' or 'normal'")
    if cfg.model.distribution == "student_t" and cfg.model.tail_dof <= 2:
        raise ConfigError("model.tail_dof must be > 2")
    if cfg.feeds.cex_source not in ("rtds", "binance", "bybit", "none"):
        raise ConfigError("feeds.cex_source must be rtds, binance, bybit or none")
    if cfg.model.vol_source not in ("auto", "oracle", "cex"):
        raise ConfigError("model.vol_source must be auto, oracle or cex")
    if not 0 <= cfg.model.market_blend < 1:
        raise ConfigError("model.market_blend must be in [0, 1)")
    if cfg.risk.loss_streak_decay > 1:
        raise ConfigError("risk.loss_streak_decay must be <= 1 (sizing up after losses is martingale)")
    if not 0 < cfg.risk.kelly_fraction <= 1:
        raise ConfigError("risk.kelly_fraction must be in (0, 1]")
    for name in ("fair_value", "constellation"):
        tif = getattr(cfg.strategies, name, None)
        if tif is not None and getattr(tif, "tif", "FAK") not in ("FAK", "FOK"):
            raise ConfigError(f"strategies.{name}.tif must be FAK or FOK")
    parse_interval(cfg.markets.interval)
    cfg.markets.assets = [str(a).lower() for a in cfg.markets.assets]


def load_updown_config(path: str | None = None) -> UpDownConfig:
    path = path or os.getenv("UPDOWN_CONFIG_PATH", "config/updown.yaml")
    cfg = UpDownConfig()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _apply(cfg, raw, "updown")
    validate(cfg)
    return cfg
