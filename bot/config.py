"""Loads and validates bot configuration from .env (secrets) and YAML (strategy/risk)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv


@dataclass
class MarketFilterConfig:
    whitelist: list[str] = field(default_factory=list)
    min_volume_usd: float = 5000.0
    min_liquidity_usd: float = 200.0
    max_markets_per_cycle: int = 200


@dataclass
class RiskConfig:
    max_position_usd: float = 25.0
    max_total_exposure_usd: float = 200.0
    max_daily_loss_usd: float = 50.0
    min_order_size_usd: float = 1.0


@dataclass
class ArbitrageConfig:
    enabled: bool = True
    min_edge: float = 0.015
    fee_buffer: float = 0.005


@dataclass
class ThresholdConfig:
    enabled: bool = False
    lookback_ticks: int = 20
    buy_drop_pct: float = 0.08
    sell_rise_pct: float = 0.08


# Polymarket taker-fee rates per market category (Sept 2026 schedule). Keep in
# sync with https://docs.polymarket.com/trading/fees — Polymarket does change
# them (sports went from 0.03 to 0.05 in July 2026).
DEFAULT_CATEGORY_TAKER_RATES: dict[str, float] = {
    "crypto": 0.07,
    "sports": 0.05,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "politics": 0.04,
    "finance": 0.04,
    "tech": 0.04,
    "mentions": 0.04,
    "geopolitics": 0.0,
}


@dataclass
class FeeConfig:
    # Used for markets whose tags match no category. It is the highest known
    # rate on purpose, so an unrecognized market is never under-costed.
    default_taker_rate: float = 0.07
    category_taker_rates: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_CATEGORY_TAKER_RATES)
    )


@dataclass
class NotificationConfig:
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    # Send a message for every filled trade (in addition to alerts).
    fills: bool = True
    # Send an "I'm alive" status message this often; 0 disables it.
    heartbeat_hours: float = 6.0


@dataclass
class WalletConfig:
    private_key: str | None
    chain_id: int
    clob_host: str
    signature_type: int
    funder_address: str | None
    live_trading: bool


@dataclass
class Settings:
    wallet: WalletConfig
    markets: MarketFilterConfig
    risk: RiskConfig
    arbitrage: ArbitrageConfig
    threshold: ThresholdConfig
    polling_interval_seconds: int = 15
    fees: FeeConfig = field(default_factory=FeeConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)


def _bool_env(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_settings(config_path: str | None = None, env_path: str | None = None) -> Settings:
    """Load configuration. Call once at startup.

    env_path defaults to a `.env` file in the current working directory (if present).
    config_path defaults to the BOT_CONFIG_PATH env var, or config/settings.yaml.
    """
    load_dotenv(dotenv_path=env_path, override=False)

    path = config_path or os.getenv("BOT_CONFIG_PATH", "config/settings.yaml")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    markets_raw = raw.get("markets", {}) or {}
    risk_raw = raw.get("risk", {}) or {}
    strategies_raw = raw.get("strategies", {}) or {}
    arb_raw = strategies_raw.get("arbitrage", {}) or {}
    thr_raw = strategies_raw.get("threshold", {}) or {}
    fees_raw = raw.get("fees", {}) or {}
    notify_raw = raw.get("notifications", {}) or {}

    # A category_taker_rates mapping in the YAML replaces the built-in one
    # entirely, so removing a category there really removes it.
    category_rates_raw = fees_raw.get("category_taker_rates")
    if category_rates_raw is None:
        category_rates_raw = DEFAULT_CATEGORY_TAKER_RATES
    fees = FeeConfig(
        default_taker_rate=float(fees_raw.get("default_taker_rate", 0.07)),
        category_taker_rates={str(k): float(v) for k, v in category_rates_raw.items()},
    )
    for name, rate in [("default_taker_rate", fees.default_taker_rate), *fees.category_taker_rates.items()]:
        if not 0.0 <= rate < 1.0:
            raise ValueError(f"fees: taker rate for {name!r} must be in [0, 1), got {rate}")

    wallet = WalletConfig(
        private_key=os.getenv("POLY_PRIVATE_KEY") or None,
        chain_id=int(os.getenv("POLY_CHAIN_ID", "137")),
        clob_host=os.getenv("POLY_CLOB_HOST", "https://clob.polymarket.com"),
        signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")),
        funder_address=os.getenv("POLY_FUNDER_ADDRESS") or None,
        live_trading=_bool_env("LIVE_TRADING", False),
    )

    settings = Settings(
        wallet=wallet,
        markets=MarketFilterConfig(
            whitelist=list(markets_raw.get("whitelist", []) or []),
            min_volume_usd=float(markets_raw.get("min_volume_usd", 5000.0)),
            min_liquidity_usd=float(markets_raw.get("min_liquidity_usd", 200.0)),
            max_markets_per_cycle=int(markets_raw.get("max_markets_per_cycle", 200)),
        ),
        risk=RiskConfig(
            max_position_usd=float(risk_raw.get("max_position_usd", 25.0)),
            max_total_exposure_usd=float(risk_raw.get("max_total_exposure_usd", 200.0)),
            max_daily_loss_usd=float(risk_raw.get("max_daily_loss_usd", 50.0)),
            min_order_size_usd=float(risk_raw.get("min_order_size_usd", 1.0)),
        ),
        arbitrage=ArbitrageConfig(
            enabled=bool(arb_raw.get("enabled", True)),
            min_edge=float(arb_raw.get("min_edge", 0.015)),
            fee_buffer=float(arb_raw.get("fee_buffer", 0.005)),
        ),
        threshold=ThresholdConfig(
            enabled=bool(thr_raw.get("enabled", False)),
            lookback_ticks=int(thr_raw.get("lookback_ticks", 20)),
            buy_drop_pct=float(thr_raw.get("buy_drop_pct", 0.08)),
            sell_rise_pct=float(thr_raw.get("sell_rise_pct", 0.08)),
        ),
        polling_interval_seconds=int(raw.get("polling_interval_seconds", 15)),
        fees=fees,
        notifications=NotificationConfig(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
            fills=bool(notify_raw.get("fills", True)),
            heartbeat_hours=float(notify_raw.get("heartbeat_hours", 6.0)),
        ),
    )

    if wallet.live_trading:
        missing = []
        if not wallet.private_key:
            missing.append("POLY_PRIVATE_KEY")
        if not wallet.funder_address:
            missing.append("POLY_FUNDER_ADDRESS")
        if missing:
            raise ValueError(
                "LIVE_TRADING=true but missing required env vars: "
                + ", ".join(missing)
                + ". Refusing to start in live mode without full wallet config."
            )

    return settings
