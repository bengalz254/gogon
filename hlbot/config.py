"""Loads hlbot configuration from .env (secrets) and config/hyperliquid.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import yaml
from dotenv import load_dotenv

from hlbot.position import EXIT_MODES
from hlbot.strategy import interval_ms

MAINNET_API_URL = "https://api.hyperliquid.xyz"
TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"

# Hyperliquid rejects orders below $10 notional.
MIN_ORDER_NOTIONAL_USD = 10.0


@dataclass
class StrategyConfig:
    coin: str = "BTC"
    interval: str = "30m"
    ema_fast: int = 9
    ema_slow: int = 21


PCT_BASES = ("margin", "price")
INTRABAR_MODES = ("conservative", "ohlc")


@dataclass
class TradeConfig:
    leverage: int = 10
    margin_mode: str = "isolated"  # isolated | cross
    margin_usd: float = 20.0  # margin per position; notional = margin_usd * leverage
    take_profit_pct: float = 0.05
    trailing_pct: float = 0.005
    # What take_profit_pct / trailing_pct / stop_loss_pct are measured against:
    #   margin -> % of margin (ROE); at 10x, 2% of margin = 0.2% price move
    #   price  -> % price move
    pct_basis: str = "margin"
    exit_mode: str = "trailing"  # trailing | fixed  (see hlbot/position.py)
    stop_loss_pct: Optional[float] = None
    max_slippage: float = 0.01
    poll_seconds: float = 2.0
    candle_close_delay_seconds: float = 5.0
    exchange_stop_order: bool = True

    @property
    def notional_usd(self) -> float:
        return self.margin_usd * self.leverage

    def to_price_pct(self, pct: Optional[float]) -> Optional[float]:
        """Convert a configured percentage into a price-move fraction."""
        if pct is None:
            return None
        return pct / self.leverage if self.pct_basis == "margin" else pct

    @property
    def tp_price_pct(self) -> float:
        return self.to_price_pct(self.take_profit_pct)

    @property
    def trailing_price_pct(self) -> float:
        return self.to_price_pct(self.trailing_pct)

    @property
    def stop_loss_price_pct(self) -> Optional[float]:
        return self.to_price_pct(self.stop_loss_pct)


@dataclass
class BacktestConfig:
    days: int = 100
    initial_equity_usd: float = 200.0
    taker_fee: float = 0.00045
    slippage: float = 0.0002
    maintenance_margin_rate: float = 0.0125
    # How TP/trailing are resolved inside a candle (only OHLC is known):
    #   conservative -> once the trailing stop arms, assume price turns right
    #                   away: exit at the minimum locked-in profit (TP - trailing)
    #   ohlc         -> assume open->low->high->close (green) / open->high->low->close
    #                   (red); optimistic for tight trailing stops
    intrabar: str = "conservative"
    block_live_if_unprofitable: bool = True


@dataclass
class ConnectionConfig:
    live_trading: bool
    network: str
    secret_key: Optional[str]
    account_address: Optional[str]

    @property
    def base_url(self) -> str:
        return TESTNET_API_URL if self.network == "testnet" else MAINNET_API_URL


@dataclass
class HLSettings:
    strategy: StrategyConfig
    trade: TradeConfig
    backtest: BacktestConfig
    connection: ConnectionConfig


def _bool_env(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _opt_float(value) -> Optional[float]:
    if value is None or value == "" or value is False:
        return None
    value = float(value)
    return value if value > 0 else None


def load_hl_settings(config_path: Optional[str] = None, env_path: Optional[str] = None) -> HLSettings:
    load_dotenv(dotenv_path=env_path, override=False)

    path = config_path or os.getenv("HL_CONFIG_PATH", "config/hyperliquid.yaml")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    s_raw = raw.get("strategy", {}) or {}
    t_raw = raw.get("trade", {}) or {}
    b_raw = raw.get("backtest", {}) or {}

    strategy = StrategyConfig(
        coin=str(s_raw.get("coin", "BTC")),
        interval=str(s_raw.get("interval", "30m")),
        ema_fast=int(s_raw.get("ema_fast", 9)),
        ema_slow=int(s_raw.get("ema_slow", 21)),
    )
    trade = TradeConfig(
        leverage=int(t_raw.get("leverage", 10)),
        margin_mode=str(t_raw.get("margin_mode", "isolated")).lower(),
        margin_usd=float(t_raw.get("margin_usd", 20.0)),
        take_profit_pct=float(t_raw.get("take_profit_pct", 0.05)),
        trailing_pct=float(t_raw.get("trailing_pct", 0.005)),
        pct_basis=str(t_raw.get("pct_basis", "margin")).lower(),
        exit_mode=str(t_raw.get("exit_mode", "trailing")).lower(),
        stop_loss_pct=_opt_float(t_raw.get("stop_loss_pct")),
        max_slippage=float(t_raw.get("max_slippage", 0.01)),
        poll_seconds=float(t_raw.get("poll_seconds", 2.0)),
        candle_close_delay_seconds=float(t_raw.get("candle_close_delay_seconds", 5.0)),
        exchange_stop_order=bool(t_raw.get("exchange_stop_order", True)),
    )
    backtest = BacktestConfig(
        days=int(b_raw.get("days", 100)),
        initial_equity_usd=float(b_raw.get("initial_equity_usd", 200.0)),
        taker_fee=float(b_raw.get("taker_fee", 0.00045)),
        slippage=float(b_raw.get("slippage", 0.0002)),
        maintenance_margin_rate=float(b_raw.get("maintenance_margin_rate", 0.0125)),
        intrabar=str(b_raw.get("intrabar", "conservative")).lower(),
        block_live_if_unprofitable=bool(b_raw.get("block_live_if_unprofitable", True)),
    )
    connection = ConnectionConfig(
        live_trading=_bool_env("HL_LIVE_TRADING", False),
        network=os.getenv("HL_NETWORK", "mainnet").strip().lower(),
        secret_key=os.getenv("HL_SECRET_KEY") or None,
        account_address=os.getenv("HL_ACCOUNT_ADDRESS") or None,
    )

    settings = HLSettings(strategy=strategy, trade=trade, backtest=backtest, connection=connection)
    validate(settings)
    return settings


def validate(settings: HLSettings) -> None:
    s, t, c = settings.strategy, settings.trade, settings.connection
    interval_ms(s.interval)  # raises on unknown interval
    if not 1 <= s.ema_fast < s.ema_slow:
        raise ValueError("strategy.ema_fast must be >= 1 and smaller than strategy.ema_slow")
    if t.leverage < 1:
        raise ValueError("trade.leverage must be >= 1")
    if t.margin_mode not in ("isolated", "cross"):
        raise ValueError("trade.margin_mode must be 'isolated' or 'cross'")
    if t.exit_mode not in EXIT_MODES:
        raise ValueError(f"trade.exit_mode must be one of {EXIT_MODES}")
    if t.pct_basis not in PCT_BASES:
        raise ValueError(f"trade.pct_basis must be one of {PCT_BASES}")
    if t.take_profit_pct <= 0 or t.trailing_pct <= 0:
        raise ValueError("trade.take_profit_pct and trade.trailing_pct must be > 0")
    if t.trailing_pct >= t.take_profit_pct and t.exit_mode == "trailing":
        raise ValueError("trade.trailing_pct must be smaller than trade.take_profit_pct")
    if settings.backtest.intrabar not in INTRABAR_MODES:
        raise ValueError(f"backtest.intrabar must be one of {INTRABAR_MODES}")
    if t.notional_usd < MIN_ORDER_NOTIONAL_USD:
        raise ValueError(
            f"margin_usd x leverage = ${t.notional_usd:.2f} is below Hyperliquid's "
            f"${MIN_ORDER_NOTIONAL_USD:.0f} minimum order value"
        )
    if c.network not in ("mainnet", "testnet"):
        raise ValueError("HL_NETWORK must be 'mainnet' or 'testnet'")
    if c.live_trading:
        missing = [n for n, v in (("HL_SECRET_KEY", c.secret_key), ("HL_ACCOUNT_ADDRESS", c.account_address)) if not v]
        if missing:
            raise ValueError(
                "HL_LIVE_TRADING=true but missing required env vars: "
                + ", ".join(missing)
                + ". Refusing to start in live mode without full wallet config."
            )
