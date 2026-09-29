"""Scalper settings: strategy / risk parameters from YAML, secrets from .env.

Unknown keys in the YAML are rejected rather than silently ignored — a typo
like `risk_per_trade: 5` falling back to a default without telling you is
exactly the kind of mistake that costs money.
"""
from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

from scalper.models import INTERVAL_MS

MODES = ("paper", "testnet", "live")
STRATEGIES = ("trend_pullback", "range_reversion", "trend_follow")
LIVE_FAPI_URL = "https://fapi.binance.com"
TESTNET_FAPI_URL = "https://testnet.binancefuture.com"
DEFAULT_CONFIG_PATH = "config/scalper.yaml"


@dataclass
class TrendPullbackParams:
    htf_interval: str = "1h"
    htf_ema_fast: int = 20
    htf_ema_slow: int = 50
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14
    adx_min: float = 18.0
    pullback_lookback: int = 6
    pullback_atr_tolerance: float = 0.3
    rsi_pullback_long: float = 45.0
    rsi_pullback_short: float = 55.0
    rsi_trigger_long: float = 50.0
    rsi_trigger_short: float = 50.0
    swing_lookback: int = 6
    sl_buffer_atr: float = 0.2
    min_sl_atr: float = 0.8
    max_sl_atr: float = 2.5
    tp_r: float = 1.5
    min_atr_pct: float = 0.10
    max_atr_pct: float = 2.0
    max_bar_atr: float = 3.0


@dataclass
class RangeReversionParams:
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 7
    rsi_oversold: float = 25.0
    rsi_overbought: float = 75.0
    adx_period: int = 14
    adx_max: float = 20.0
    atr_period: int = 14
    swing_lookback: int = 3
    sl_buffer_atr: float = 0.3
    min_sl_atr: float = 1.0
    min_rr: float = 1.0
    min_atr_pct: float = 0.10
    max_atr_pct: float = 2.0
    max_bar_atr: float = 3.0


@dataclass
class TrendFollowParams:
    entry_bars: int = 20  # breakout of the highest high / lowest low of the previous N candles
    trend_ema: int = 100  # only trade in this EMA's direction (0 = no filter)
    atr_period: int = 20
    stop_atr: float = 3.0  # initial stop distance in ATR
    tp_r: float = 0.0  # fixed target in R; 0 = none, the trailing stop is the exit
    min_atr_pct: float = 0.2
    max_atr_pct: float = 15.0


@dataclass
class StrategyConfig:
    name: str = "trend_pullback"
    allow_long: bool = True
    allow_short: bool = True
    trend_pullback: TrendPullbackParams = field(default_factory=TrendPullbackParams)
    range_reversion: RangeReversionParams = field(default_factory=RangeReversionParams)
    trend_follow: TrendFollowParams = field(default_factory=TrendFollowParams)


@dataclass
class ManagementConfig:
    breakeven_at_r: float = 1.0
    trail_start_r: float = 0.0
    trail_atr: float = 1.5
    max_bars_in_trade: int = 24
    cooldown_bars_after_exit: int = 2
    min_stop_gap_atr: float = 0.1


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_open_positions: int = 2
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 15.0
    max_consecutive_losses: int = 3
    loss_cooldown_minutes: int = 60
    max_trades_per_day: int = 30
    max_notional_usd: float = 0.0
    max_margin_use_pct: float = 80.0
    min_sl_cost_ratio: float = 3.0
    liquidation_buffer: float = 0.5
    maintenance_margin_rate: float = 0.01


@dataclass
class ExecutionConfig:
    leverage: int = 5
    margin_type: str = "ISOLATED"
    take_profit_order: str = "limit"
    stop_working_type: str = "MARK_PRICE"
    conditional_order_api: str = "auto"
    orphan_policy: str = "adopt"
    orphan_sl_atr: float = 2.0
    auto_one_way_mode: bool = False
    poll_seconds: float = 2.0
    position_check_seconds: float = 5.0
    order_check_seconds: float = 60.0
    candle_close_delay_seconds: float = 2.0
    recv_window_ms: int = 5000
    warmup_bars: int = 1000
    flatten_on_exit: bool = False


@dataclass
class CostConfig:
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    slippage_bps: float = 2.0
    funding_bps_per_8h: float = 1.0
    use_exchange_fee_rates: bool = True

    @property
    def slippage(self) -> float:
        return self.slippage_bps / 10_000.0

    @property
    def round_trip_cost(self) -> float:
        """Worst-case cost of a full round trip as a fraction of price.

        Taker in, taker out (a stop-loss is always a market order), plus
        slippage on both sides.
        """
        return 2 * self.taker_fee + 2 * self.slippage


@dataclass
class PaperConfig:
    starting_balance: float = 1000.0


@dataclass
class BacktestConfig:
    starting_balance: float = 1000.0
    days: int = 60
    oos_fraction: float = 0.3


@dataclass
class NotifyConfig:
    telegram_enabled: bool = False
    heartbeat_minutes: int = 60


@dataclass
class Credentials:
    api_key: str | None = None
    api_secret: str | None = None
    base_url: str = LIVE_FAPI_URL
    public_base_url: str = LIVE_FAPI_URL
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


@dataclass
class Settings:
    mode: str = "paper"
    symbols: list = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    timeframe: str = "5m"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    management: ManagementConfig = field(default_factory=ManagementConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    data_dir: str = "data/scalper"
    log_dir: str = "logs"
    credentials: Credentials = field(default_factory=Credentials)

    @property
    def interval_ms(self) -> int:
        return INTERVAL_MS[self.timeframe]


_SECRET_KEYS = {"api_key", "api_secret", "secret", "apikey", "credentials", "telegram_token"}


def _default_of(f: dataclasses.Field):
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return None


def _coerce(value, default, path: str):
    if value is None:
        return default
    try:
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
                return True
            if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off"):
                return False
            raise ValueError("expected true/false")
        if isinstance(default, int):
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("expected a whole number")
            return int(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, list):
            if isinstance(value, str):
                value = [v for v in re.split(r"[,\s]+", value) if v]
            if not isinstance(value, list):
                raise ValueError("expected a list")
            return [str(v) for v in value]
        if isinstance(default, str):
            return str(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid value for '{path}': {value!r} ({e})") from None
    return value


def _build(cls, raw, path: str):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"'{path}' must be a mapping of settings, got {type(raw).__name__}")
    known = {f.name: f for f in dataclasses.fields(cls)}
    kwargs = {}
    for key, value in raw.items():
        if str(key).lower() in _SECRET_KEYS:
            raise ValueError(
                f"'{path}.{key}' looks like a secret. Put API keys / tokens in .env, "
                "never in the YAML config (it is easy to commit by accident)."
            )
        if key not in known or key == "credentials":
            valid = ", ".join(k for k in known if k != "credentials")
            raise ValueError(f"Unknown setting '{path}.{key}'. Valid keys here: {valid}")
        default = _default_of(known[key])
        if dataclasses.is_dataclass(default):
            kwargs[key] = _build(type(default), value, f"{path}.{key}")
        else:
            kwargs[key] = _coerce(value, default, f"{path}.{key}")
    return cls(**kwargs)


def settings_from_dict(raw: dict | None) -> Settings:
    return _build(Settings, raw or {}, "config")


def load_settings(
    config_path: str | None = None,
    env_path: str | None = None,
    mode_override: str | None = None,
) -> Settings:
    """Load YAML + .env, apply environment overrides and validate."""
    load_dotenv(dotenv_path=env_path, override=False)
    path = config_path or os.getenv("SCALPER_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    raw: dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    elif config_path:
        raise FileNotFoundError(f"Config file not found: {path}")

    settings = settings_from_dict(raw)
    mode = mode_override or os.getenv("SCALPER_MODE") or settings.mode
    settings.mode = mode.strip().lower()
    settings.symbols = [s.strip().upper() for s in settings.symbols if s.strip()]
    settings.credentials = credentials_from_env(settings.mode)
    validate(settings)
    return settings


def credentials_from_env(mode: str) -> Credentials:
    live_url = os.getenv("BINANCE_FAPI_URL", LIVE_FAPI_URL).rstrip("/")
    testnet_url = os.getenv("BINANCE_TESTNET_FAPI_URL", TESTNET_FAPI_URL).rstrip("/")
    creds = Credentials(
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
    )
    if mode == "testnet":
        creds.api_key = os.getenv("BINANCE_TESTNET_API_KEY") or None
        creds.api_secret = os.getenv("BINANCE_TESTNET_API_SECRET") or None
        creds.base_url = testnet_url
        # The testnet is a separate market with its own prices, so signals
        # must come from the same place the orders go.
        creds.public_base_url = testnet_url
    else:
        creds.api_key = os.getenv("BINANCE_API_KEY") or None
        creds.api_secret = os.getenv("BINANCE_API_SECRET") or None
        creds.base_url = live_url
        creds.public_base_url = live_url
    return creds


def validate(s: Settings) -> list[str]:
    """Raise ValueError on invalid settings; return a list of warnings."""
    errors: list[str] = []
    warnings: list[str] = []

    if s.mode not in MODES:
        errors.append(f"mode must be one of {MODES}, got {s.mode!r}")
    if s.timeframe not in INTERVAL_MS:
        errors.append(f"timeframe {s.timeframe!r} not supported ({', '.join(INTERVAL_MS)})")
    if not s.symbols:
        errors.append("symbols list is empty")
    for sym in s.symbols:
        if not re.fullmatch(r"[A-Z0-9]{5,30}", sym):
            errors.append(f"symbol {sym!r} does not look like a Binance futures symbol (e.g. BTCUSDT)")
    if len(set(s.symbols)) != len(s.symbols):
        errors.append("symbols list contains duplicates")

    st = s.strategy
    if st.name not in STRATEGIES:
        errors.append(f"strategy.name must be one of {STRATEGIES}, got {st.name!r}")
    if not st.allow_long and not st.allow_short:
        errors.append("strategy.allow_long and allow_short are both false — nothing to trade")
    tp = st.trend_pullback
    if st.name == "trend_pullback" and s.timeframe in INTERVAL_MS:
        if tp.htf_interval not in INTERVAL_MS:
            errors.append(f"trend_pullback.htf_interval {tp.htf_interval!r} not supported")
        else:
            base, htf = INTERVAL_MS[s.timeframe], INTERVAL_MS[tp.htf_interval]
            if htf < base or htf % base:
                errors.append("trend_pullback.htf_interval must be a whole multiple of timeframe")
        if tp.ema_fast >= tp.ema_slow or tp.htf_ema_fast >= tp.htf_ema_slow:
            errors.append("trend_pullback EMA fast periods must be shorter than slow periods")
        if not (0 < tp.min_sl_atr <= tp.max_sl_atr):
            errors.append("trend_pullback requires 0 < min_sl_atr <= max_sl_atr")
        if tp.tp_r <= 0:
            errors.append("trend_pullback.tp_r must be > 0")
    tf = st.trend_follow
    if st.name == "trend_follow":
        if tf.entry_bars < 2 or tf.atr_period < 2 or tf.trend_ema < 0:
            errors.append("trend_follow needs entry_bars >= 2, atr_period >= 2 and trend_ema >= 0")
        if tf.stop_atr <= 0 or tf.tp_r < 0:
            errors.append("trend_follow needs stop_atr > 0 and tp_r >= 0")
        if not (0 <= tf.min_atr_pct < tf.max_atr_pct):
            errors.append("trend_follow requires 0 <= min_atr_pct < max_atr_pct")
        if tf.tp_r == 0 and s.management.trail_start_r <= 0:
            errors.append(
                "trend_follow with tp_r = 0 exits only through the trailing stop: "
                "set management.trail_start_r > 0 (e.g. 1.0)"
            )
        if s.timeframe in INTERVAL_MS and INTERVAL_MS[s.timeframe] < INTERVAL_MS["1h"]:
            warnings.append(
                f"trend_follow on {s.timeframe}: it is built for 1h and above, where fees are a "
                "small part of each trade. Backtest before using it on small timeframes."
            )

    r = s.risk
    if not (0 < r.risk_per_trade_pct <= 5):
        errors.append("risk.risk_per_trade_pct must be in (0, 5]")
    elif r.risk_per_trade_pct > 2:
        warnings.append(
            f"risk.risk_per_trade_pct={r.risk_per_trade_pct}% is aggressive; "
            "a normal losing streak can cut the account hard. 0.25–1% is typical."
        )
    if r.max_open_positions < 1:
        errors.append("risk.max_open_positions must be >= 1")
    if r.max_daily_loss_pct <= 0:
        errors.append("risk.max_daily_loss_pct must be > 0")
    if r.max_drawdown_pct <= 0:
        errors.append("risk.max_drawdown_pct must be > 0")
    if not (0 < r.max_margin_use_pct <= 100):
        errors.append("risk.max_margin_use_pct must be in (0, 100]")
    if not (0 < r.liquidation_buffer < 1):
        errors.append("risk.liquidation_buffer must be in (0, 1)")

    ex = s.execution
    if not (1 <= ex.leverage <= 125):
        errors.append("execution.leverage must be between 1 and 125")
    elif ex.leverage > 20:
        warnings.append(
            f"execution.leverage={ex.leverage}x is high. Position size is set by "
            "risk_per_trade_pct, so extra leverage mostly just moves liquidation closer."
        )
    if ex.margin_type not in ("ISOLATED", "CROSSED"):
        errors.append("execution.margin_type must be ISOLATED or CROSSED")
    if ex.take_profit_order not in ("limit", "market"):
        errors.append("execution.take_profit_order must be 'limit' or 'market'")
    if ex.stop_working_type not in ("MARK_PRICE", "CONTRACT_PRICE"):
        errors.append("execution.stop_working_type must be MARK_PRICE or CONTRACT_PRICE")
    if ex.conditional_order_api not in ("auto", "algo", "legacy"):
        errors.append("execution.conditional_order_api must be auto, algo or legacy")
    if ex.orphan_policy not in ("adopt", "ignore"):
        errors.append("execution.orphan_policy must be 'adopt' or 'ignore'")
    if ex.warmup_bars < 100:
        errors.append("execution.warmup_bars must be >= 100")

    c = s.costs
    for name in ("taker_fee", "maker_fee"):
        v = getattr(c, name)
        if not (0 <= v < 0.01):
            errors.append(f"costs.{name} must be a fraction like 0.0005 (=0.05%), got {v}")
    if c.slippage_bps < 0:
        errors.append("costs.slippage_bps must be >= 0")

    if s.timeframe == "1m":
        warnings.append(
            "timeframe=1m: typical 1-minute moves are only a few times larger than "
            "round-trip fees, so most 1m scalping loses to costs. Backtest first."
        )

    if s.mode in ("testnet", "live"):
        if not s.credentials.api_key or not s.credentials.api_secret:
            prefix = "BINANCE_TESTNET_" if s.mode == "testnet" else "BINANCE_"
            errors.append(
                f"mode={s.mode} needs {prefix}API_KEY and {prefix}API_SECRET in .env"
            )
    if s.notify.telegram_enabled and not (
        s.credentials.telegram_token and s.credentials.telegram_chat_id
    ):
        errors.append("notify.telegram_enabled=true needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    if errors:
        raise ValueError("Invalid scalper configuration:\n  - " + "\n  - ".join(errors))
    return warnings
