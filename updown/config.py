"""Configuration for the 5-minute Up/Down bot (config/updown.yaml + .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

import yaml
from dotenv import load_dotenv

from bot.config import WalletConfig, _bool_env


@dataclass
class ModelConfig:
    # How fast the live volatility estimate reacts (seconds).
    vol_halflife_s: float = 180.0
    # Clamp on volatility per sqrt(second). BTC at ~50% annual vol is ~9e-5.
    vol_floor: float = 4e-5
    vol_cap: float = 6e-4
    # Scale sigma up to cover fat tails; 1.0 = plain normal model.
    vol_multiplier: float = 1.0
    # Entries must still show an edge if true volatility is this much lower
    # or higher than estimated (0.3 = +-30%). Without it the bot mostly ends
    # up buying longshots whenever its vol guess differs from the market's.
    vol_uncertainty: float = 0.3
    # Uncertainty (as a log return) between our feed and the settling oracle.
    basis_sd: float = 0.00015


@dataclass
class FeeConfig:
    # fee per share = fee_rate * (p * (1 - p)) ** fee_exponent (taker only).
    fee_rate: float = 0.25
    fee_exponent: float = 2.0


@dataclass
class StrategyConfig:
    # Only trade inside this slice of the window (seconds left before close).
    # The first minute is skipped: the model has no information yet (~50/50)
    # and that's exactly where the fee is highest.
    max_seconds_left: float = 240.0
    # Last few seconds are skipped: our order may land after the oracle ticks.
    min_seconds_left: float = 8.0
    # Required expected profit per share after fees (0.04 = 4c on a $1 payout).
    min_edge: float = 0.04
    # Don't buy near-certain or near-worthless shares: tiny upside, oracle risk.
    min_price: float = 0.08
    max_price: float = 0.92
    # If the market disagrees with the model by more than this, assume our
    # data is wrong (stale feed, wrong strike) rather than the market's.
    max_model_market_gap: float = 0.30
    # Sell an open position early when the bid (after fees) beats the model by this much.
    exit_margin: float = 0.06
    max_entries_per_window: int = 2
    # Wait this long before adding to a position: a second entry should come
    # from new information, not re-hit the same asks a second later.
    min_seconds_between_entries: float = 20.0


@dataclass
class SizingConfig:
    bankroll_usd: float = 100.0
    # Fraction of full Kelly to bet. Full Kelly is too aggressive for a model
    # with estimation error; 0.1-0.25 is the usual range.
    kelly_fraction: float = 0.15
    max_bet_usd: float = 5.0
    max_window_exposure_usd: float = 10.0
    # Coins move together: seven "Down" bets in the same minute are really one
    # big bet on the market falling. Cap open cost on each side across coins.
    max_same_direction_usd: float = 20.0
    min_order_usd: float = 1.0


@dataclass
class RiskLimitsConfig:
    max_daily_loss_usd: float = 20.0
    # Pause after this many losing windows in a row...
    max_consecutive_losses: int = 4
    # ...for this many minutes.
    cooldown_minutes: float = 30.0


@dataclass
class FeedConfig:
    # Binance public REST host. US users: https://api.binance.us
    binance_host: str = "https://api.binance.com"
    hyperliquid_host: str = "https://api.hyperliquid.xyz"
    poll_seconds: float = 1.0
    # Refuse to trade on a price older than this.
    max_age_s: float = 3.0


@dataclass
class UpDownSettings:
    wallet: WalletConfig
    assets: list[str] = field(default_factory=lambda: ["btc"])
    # Gamma slug for a window starting at unix time {start}.
    slug_template: str = "{asset}-updown-5m-{start}"
    window_seconds: int = 300
    tick_seconds: float = 1.0
    gamma_host: str = "https://gamma-api.polymarket.com"
    journal_path: str = "data/trades.csv"
    # Live snapshot for the radar dashboard (scripts/updown_dashboard.py).
    state_path: str = "data/updown_state.json"
    model: ModelConfig = field(default_factory=ModelConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    risk: RiskLimitsConfig = field(default_factory=RiskLimitsConfig)
    feed: FeedConfig = field(default_factory=FeedConfig)


# Where each asset's live price comes from. Binance spot where it's listed;
# Hyperliquid (its home venue) for HYPE, which Binance spot doesn't list.
BINANCE_SYMBOLS = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT",
    "bnb": "BNBUSDT", "doge": "DOGEUSDT",
}
HYPERLIQUID_COINS = {"hype": "HYPE"}
SUPPORTED_ASSETS = {**BINANCE_SYMBOLS, **HYPERLIQUID_COINS}


def _section(cls, raw: dict | None):
    """Build a dataclass from a YAML dict, rejecting unknown keys (catches typos)."""
    raw = raw or {}
    known = {f.name: f for f in fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys in config: {sorted(unknown)}")
    kwargs = {}
    for name, value in raw.items():
        default = getattr(cls(), name)
        kwargs[name] = type(default)(value) if isinstance(default, (int, float, str)) else value
    return cls(**kwargs)


def load_wallet_from_env() -> WalletConfig:
    wallet = WalletConfig(
        private_key=os.getenv("POLY_PRIVATE_KEY") or None,
        chain_id=int(os.getenv("POLY_CHAIN_ID", "137")),
        clob_host=os.getenv("POLY_CLOB_HOST", "https://clob.polymarket.com"),
        signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")),
        funder_address=os.getenv("POLY_FUNDER_ADDRESS") or None,
        live_trading=_bool_env("LIVE_TRADING", False),
    )
    if wallet.live_trading:
        missing = [n for n, v in (("POLY_PRIVATE_KEY", wallet.private_key), ("POLY_FUNDER_ADDRESS", wallet.funder_address)) if not v]
        if missing:
            raise ValueError(
                "LIVE_TRADING=true but missing required env vars: " + ", ".join(missing)
                + ". Refusing to start in live mode without full wallet config."
            )
    return wallet


def load_updown_settings(config_path: str | None = None, env_path: str | None = None) -> UpDownSettings:
    load_dotenv(dotenv_path=env_path, override=False)
    path = config_path or os.getenv("UPDOWN_CONFIG_PATH", "config/updown.yaml")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    assets = [str(a).lower() for a in raw.get("assets", ["btc"])]
    unsupported = [a for a in assets if a not in SUPPORTED_ASSETS]
    if unsupported:
        raise ValueError(f"Unsupported assets {unsupported}; supported: {sorted(SUPPORTED_ASSETS)}")
    if len(set(assets)) != len(assets):
        raise ValueError("assets: each coin may appear only once")

    settings = UpDownSettings(
        wallet=load_wallet_from_env(),
        assets=assets,
        slug_template=str(raw.get("slug_template", "{asset}-updown-5m-{start}")),
        window_seconds=int(raw.get("window_seconds", 300)),
        tick_seconds=float(raw.get("tick_seconds", 1.0)),
        gamma_host=str(raw.get("gamma_host", "https://gamma-api.polymarket.com")),
        journal_path=str(raw.get("journal_path", "data/trades.csv")),
        state_path=str(raw.get("state_path", "data/updown_state.json")),
        model=_section(ModelConfig, raw.get("model")),
        fees=_section(FeeConfig, raw.get("fees")),
        strategy=_section(StrategyConfig, raw.get("strategy")),
        sizing=_section(SizingConfig, raw.get("sizing")),
        risk=_section(RiskLimitsConfig, raw.get("risk")),
        feed=_section(FeedConfig, raw.get("feed")),
    )
    s = settings.strategy
    if not (0 <= s.min_seconds_left < s.max_seconds_left <= settings.window_seconds):
        raise ValueError("strategy: need 0 <= min_seconds_left < max_seconds_left <= window_seconds")
    if not (0 < s.min_price < s.max_price < 1):
        raise ValueError("strategy: need 0 < min_price < max_price < 1")
    return settings
