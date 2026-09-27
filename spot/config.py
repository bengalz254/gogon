"""Settings for the Tokocrypto grid bot: config/spot.yaml, plus Telegram from .env.

Messages in ConfigError are shown to the user as they are, so they're in
Indonesian like the rest of the bot's user-facing text.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG_PATH = "config/spot.yaml"
DATA_DIR = "data/spot"


class ConfigError(ValueError):
    """A setting is missing or invalid."""


@dataclass
class GridConfig:
    # Fixed price range. Leave both at 0 to center the grid on the price at
    # the first start (range_pct below and above it); the grid is then saved
    # and kept across restarts.
    lower_price: float = 0.0
    upper_price: float = 0.0
    range_pct: float = 10.0
    # Price lines. The bot trades the gaps between neighbouring lines, so
    # there are levels - 1 slots, each buying at its lower line and selling
    # at its upper one.
    levels: int = 15
    # Quote currency (IDR for an IDR pair) spent on each buy order.
    order_value: float = 100_000.0


@dataclass
class CostConfig:
    """Trading costs, in percent of each trade's value."""

    # The grid's orders rest in the book, so they pay the maker fee. The
    # taker fee only applies to the stop-loss sale.
    maker_fee_pct: float = 0.10
    taker_fee_pct: float = 0.20
    # Exchange and clearing levy (ICEx/CFX), charged on every trade.
    exchange_fee_pct: float = 0.03
    # PPh 22 final (PMK 50/2025): 0.21% of every sale on a licensed domestic
    # exchange. Buying with rupiah isn't taxed; for crypto-to-crypto pairs
    # (e.g. BTC/USDT) both sides are sales, so set buy_tax_pct too.
    buy_tax_pct: float = 0.0
    sell_tax_pct: float = 0.21
    # The bot refuses a grid whose gaps earn less than this per buy→sell
    # cycle after all costs.
    min_net_profit_pct: float = 0.30

    def fee_rate(self, taker: bool = False) -> float:
        return ((self.taker_fee_pct if taker else self.maker_fee_pct) + self.exchange_fee_pct) / 100

    def tax_rate(self, side: str) -> float:
        return (self.buy_tax_pct if side == "BUY" else self.sell_tax_pct) / 100

    def cost_rate(self, side: str, taker: bool = False) -> float:
        """Fees plus tax, as a fraction of the trade's value."""
        return self.fee_rate(taker) + self.tax_rate(side)


@dataclass
class RiskConfig:
    # Paper money the grid starts with (quote currency).
    paper_balance: float = 2_000_000.0
    # Sell everything and stop once the price closes this far below the
    # grid's lowest line (0 = never).
    stop_loss_pct: float = 10.0


@dataclass
class NotificationConfig:
    # A Telegram message for every filled order.
    fills: bool = True
    # A P&L summary every this many hours (0 = off).
    summary_hours: float = 24.0
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


@dataclass
class SpotSettings:
    symbol: str = "BTC/IDR"
    poll_seconds: float = 15.0
    grid: GridConfig = field(default_factory=GridConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    data_dir: str = DATA_DIR

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0]

    @property
    def quote(self) -> str:
        return self.symbol.split("/")[1]


_NUMBER_HINT = " (tulis angka tanpa titik ribuan, misalnya 1000000)"


def _cast(value, default, key: str):
    """`value` converted to the type of `default`, or ConfigError."""
    try:
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("true", "yes", "on", "1"):
                return True
            if text in ("false", "no", "off", "0"):
                return False
            raise ValueError
        if isinstance(default, (int, float)):
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
            if number != number or number in (float("inf"), float("-inf")):
                raise ValueError
            if isinstance(default, int):
                if number != int(number):
                    raise ValueError
                return int(number)
            return number
        return str(value).strip()
    except (TypeError, ValueError):
        hint = _NUMBER_HINT if isinstance(default, (int, float)) and not isinstance(default, bool) else ""
        raise ConfigError(f"{key}: nilai '{value}' tidak valid{hint}") from None


def _section(cls, raw, name: str, skip: tuple[str, ...] = ()):
    """A config dataclass from one YAML section; unknown keys are an error
    (a typo would otherwise be silently ignored)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"'{name}' harus berisi pengaturan (key: value)")
    names = {f.name for f in fields(cls)} - set(skip)
    unknown = sorted(set(raw) - names)
    if unknown:
        raise ConfigError(f"Pengaturan tidak dikenal di '{name}': {', '.join(unknown)}")
    defaults = cls()
    values = {
        key: _cast(value, getattr(defaults, key), f"{name}.{key}") for key, value in raw.items() if value is not None
    }
    return cls(**values)


def _validate(s: SpotSettings) -> None:
    parts = s.symbol.split("/")
    if len(parts) != 2 or not all(p and p == p.strip() for p in parts):
        raise ConfigError(f"symbol harus berbentuk BASE/QUOTE, misalnya BTC/IDR (sekarang: '{s.symbol}')")
    if s.poll_seconds < 5:
        raise ConfigError("poll_seconds minimal 5 detik")
    g = s.grid
    if not 3 <= g.levels <= 200:
        raise ConfigError("grid.levels harus antara 3 dan 200")
    if g.order_value <= 0:
        raise ConfigError("grid.order_value harus lebih dari 0")
    if (g.lower_price, g.upper_price) != (0, 0):
        if not 0 < g.lower_price < g.upper_price:
            raise ConfigError(
                "grid.lower_price dan grid.upper_price: isi keduanya dengan lower < upper, "
                "atau biarkan keduanya 0 supaya range dipasang otomatis"
            )
    elif not 1 <= g.range_pct <= 50:
        raise ConfigError("grid.range_pct harus antara 1 dan 50")
    for f in fields(CostConfig):
        value = getattr(s.costs, f.name)
        if not 0 <= value <= 5:
            raise ConfigError(f"costs.{f.name} harus antara 0 dan 5 (persen)")
    if s.risk.paper_balance <= 0:
        raise ConfigError("risk.paper_balance harus lebih dari 0")
    if not 0 <= s.risk.stop_loss_pct <= 50:
        raise ConfigError("risk.stop_loss_pct harus antara 0 (mati) dan 50")
    if s.notifications.summary_hours < 0:
        raise ConfigError("notifications.summary_hours tidak boleh negatif")


def load_settings(config_path: str | None = None, env_path: str | None = None) -> SpotSettings:
    """Read config/spot.yaml (or SPOT_CONFIG_PATH) and the Telegram settings from .env."""
    load_dotenv(dotenv_path=env_path, override=False)
    path = config_path or os.getenv("SPOT_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise ConfigError(f"File config {path} tidak ditemukan") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} tidak bisa dibaca (format YAML salah): {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} harus berisi pengaturan (key: value)")

    sections = {"grid", "costs", "risk", "notifications"}
    top = _section(SpotSettings, {k: v for k, v in raw.items() if k not in sections}, "config", skip=(*sections, "data_dir"))
    notifications = _section(
        NotificationConfig, raw.get("notifications"), "notifications", skip=("telegram_bot_token", "telegram_chat_id")
    )
    notifications.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    notifications.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    settings = SpotSettings(
        symbol=top.symbol.upper(),
        poll_seconds=top.poll_seconds,
        grid=_section(GridConfig, raw.get("grid"), "grid"),
        costs=_section(CostConfig, raw.get("costs"), "costs"),
        risk=_section(RiskConfig, raw.get("risk"), "risk"),
        notifications=notifications,
    )
    _validate(settings)
    return settings
