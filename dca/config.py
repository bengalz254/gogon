"""Loads DCA bot configuration from .env (API keys) and config/dca.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

from dca.ladder import LONG, SHORT, Level, build_levels, build_plan


@dataclass
class LadderConfig:
    base_order_usdt: float = 40.0
    safety_order_usdt: float = 40.0
    max_safety_orders: int = 6
    price_deviation_pct: float = 1.5
    step_scale: float = 1.3
    volume_scale: float = 1.5
    take_profit_pct: float = 1.0
    # Measured from the base entry price; must lie beyond the last safety order.
    stop_loss_pct: float = 24.0

    def levels(self) -> list[Level]:
        return build_levels(
            self.base_order_usdt,
            self.safety_order_usdt,
            self.max_safety_orders,
            self.price_deviation_pct,
            self.step_scale,
            self.volume_scale,
        )


@dataclass
class EntryConfig:
    # "rsi": wait for oversold (long) / overbought (short); "always": re-enter right away.
    mode: str = "rsi"
    rsi_period: int = 14
    long_rsi_below: float = 30.0
    short_rsi_above: float = 70.0
    # Only long above the EMA and only short below it.
    trend_filter: bool = False
    ema_period: int = 200


@dataclass
class RiskConfig:
    # Stage-by-stage gap required between liquidation price and the next price
    # the deal can reach (next safety order or SL), in % of entry.
    liquidation_buffer_pct: float = 5.0
    max_daily_loss_usdt: float = 100.0
    cooldown_minutes_after_stop: int = 60
    # Max share of capital the worst case (every enabled side fully filled) may use as margin.
    max_capital_usage: float = 0.9
    # Also place a STOP_MARKET on Binance so SL works even if the bot is offline.
    exchange_stop_loss: bool = True


@dataclass
class DcaSettings:
    symbol: str = "SOL/USDT:USDT"
    timeframe: str = "15m"
    poll_interval_seconds: int = 10
    capital_usdt: float = 1000.0
    leverage: int = 3
    maintenance_margin_rate: float = 0.01
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    sides: list[str] = field(default_factory=lambda: [LONG, SHORT])
    ladder: LadderConfig = field(default_factory=LadderConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    live_trading: bool = False
    demo_trading: bool = True
    api_key: str | None = None
    api_secret: str | None = None

    def validate(self) -> None:
        errors = []
        for side in self.sides:
            if side not in (LONG, SHORT):
                errors.append(f"unknown side {side!r}")
        if not self.sides:
            errors.append("no sides enabled")
        if self.leverage < 1:
            errors.append("leverage must be >= 1")
        if self.entry.mode not in ("rsi", "always"):
            errors.append("entry.mode must be 'rsi' or 'always'")
        levels = self.ladder.levels()
        last_dev = levels[-1].deviation_pct
        if self.ladder.stop_loss_pct <= last_dev:
            errors.append(
                f"ladder.stop_loss_pct ({self.ladder.stop_loss_pct}%) must be beyond the "
                f"last safety order ({last_dev:.2f}%)"
            )
        if self.ladder.stop_loss_pct >= 100 and LONG in self.sides:
            errors.append("ladder.stop_loss_pct must be < 100 for longs")

        total_margin = 0.0
        for side in self.sides:
            # Percent-based ladder: the check is price independent, entry=1 is fine.
            plan = build_plan(
                1.0, side, levels, self.ladder.take_profit_pct, self.ladder.stop_loss_pct,
                self.leverage, self.maintenance_margin_rate,
            )
            total_margin += plan.margin_usdt
            if plan.min_liq_buffer_pct < self.risk.liquidation_buffer_pct:
                errors.append(
                    f"{side}: liquidation is only {plan.min_liq_buffer_pct:.2f}% beyond the next "
                    f"order/SL (need {self.risk.liquidation_buffer_pct}%) — lower leverage or "
                    "tighten the ladder"
                )
        budget = self.capital_usdt * self.risk.max_capital_usage
        if total_margin > budget:
            errors.append(
                f"worst-case margin ${total_margin:.2f} exceeds {self.risk.max_capital_usage:.0%} "
                f"of capital (${budget:.2f}) — shrink order sizes or raise leverage"
            )
        if self.live_trading and not (self.api_key and self.api_secret):
            errors.append("DCA_LIVE_TRADING=true but BINANCE_API_KEY / BINANCE_API_SECRET missing")
        if errors:
            raise ValueError("Invalid DCA config:\n  - " + "\n  - ".join(errors))


def _bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _merge(cls, raw: dict | None):
    raw = raw or {}
    known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"unknown keys in {cls.__name__}: {sorted(unknown)}")
    return cls(**known)


def load_dca_settings(config_path: str | None = None, env_path: str | None = None) -> DcaSettings:
    load_dotenv(dotenv_path=env_path, override=False)
    path = config_path or os.getenv("DCA_CONFIG_PATH", "config/dca.yaml")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    nested = {"ladder": LadderConfig, "entry": EntryConfig, "risk": RiskConfig}
    top = {k: v for k, v in raw.items() if k not in nested}
    settings = _merge(DcaSettings, top)
    for key, cls in nested.items():
        setattr(settings, key, _merge(cls, raw.get(key)))

    settings.live_trading = _bool_env("DCA_LIVE_TRADING", False)
    settings.demo_trading = _bool_env("BINANCE_DEMO", True)
    settings.api_key = os.getenv("BINANCE_API_KEY") or None
    settings.api_secret = os.getenv("BINANCE_API_SECRET") or None
    settings.validate()
    return settings
