"""Configuration: config/migbot.yaml (settings) + .env (tokens and URLs).

Every key in the YAML file must be known: a typo such as `min_liqudity_usd`
stops the bot at startup instead of being silently ignored.
"""
from __future__ import annotations

import dataclasses
import os
import typing
from dataclasses import dataclass, field

import yaml

SOL_MINT = "So11111111111111111111111111111111111111112"
DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"

# Token accounts owned by these addresses are pool vaults or burn addresses,
# not holders, and are left out of the holder-concentration checks.
DEFAULT_AMM_OWNERS = [
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
    "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh",  # Raydium LaunchLab authority
    "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC",  # Meteora DAMM v2 pool authority
    "1nc1nerator11111111111111111111111111111111",  # incinerator (burned tokens)
]

# Token-2022 extensions that let someone else move, freeze or tax your tokens.
DEFAULT_BAD_EXTENSIONS = [
    "transferFeeConfig",
    "transferHook",
    "permanentDelegate",
    "nonTransferable",
    "defaultAccountState",
    "pausableConfig",
]


class ConfigError(ValueError):
    """Invalid configuration; the message is meant for the user (Indonesian)."""


@dataclass
class PumpPortalConfig:
    enabled: bool = True
    ws_url: str = "wss://pumpportal.fun/api/data"
    # Reconnect when the socket has been silent this long (a dead socket can stay "open").
    reconnect_after_silence_minutes: float = 30.0


@dataclass
class GeckoTerminalConfig:
    enabled: bool = True
    base_url: str = "https://api.geckoterminal.com/api/v2"
    poll_seconds: float = 30.0
    # GeckoTerminal DEX ids whose new pools count as migrations.
    dexes: list[str] = field(default_factory=lambda: ["pumpswap"])


@dataclass
class DiscoveryConfig:
    pumpportal: PumpPortalConfig = field(default_factory=PumpPortalConfig)
    geckoterminal: GeckoTerminalConfig = field(default_factory=GeckoTerminalConfig)
    # pump.fun mint addresses end in "pump". Empty list = accept any mint.
    mint_suffixes: list[str] = field(default_factory=lambda: ["pump"])
    # Ignore tokens that migrated longer ago than this when first found.
    max_age_on_discovery_seconds: float = 600.0


@dataclass
class MarketConfig:
    dexscreener_url: str = "https://api.dexscreener.com"
    refresh_seconds: float = 10.0


@dataclass
class TrackingConfig:
    # Every migrated token is followed this long so the report can measure
    # what happened to it (bought or not).
    track_minutes: float = 120.0
    checkpoints_minutes: list[float] = field(default_factory=lambda: [1, 3, 5, 10, 15, 30, 60, 120])


@dataclass
class EntryConfig:
    delay_seconds: float = 180.0
    window_seconds: float = 900.0


@dataclass
class FilterConfig:
    min_liquidity_usd: float = 10_000.0
    min_market_cap_usd: float = 40_000.0
    max_market_cap_usd: float = 1_500_000.0
    min_volume_5m_usd: float = 5_000.0
    min_txns_5m: int = 60
    min_buy_ratio_5m: float = 0.50
    max_drop_from_first_pct: float = 40.0
    max_rise_from_first_pct: float = 300.0
    require_socials: bool = False
    require_mint_renounced: bool = True
    require_freeze_renounced: bool = True
    bad_token2022_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_BAD_EXTENSIONS))
    max_top10_pct: float = 30.0
    max_top_holder_pct: float = 10.0
    max_dev_hold_pct: float = 5.0
    # GMGN-only checks: applied only when GMGN data is available. 0 = off.
    min_holders: int = 0
    min_smart_buys: int = 0
    max_sniper_count: int = 0
    # false: a check whose data is unavailable is skipped. true: it fails.
    strict_missing_data: bool = False


@dataclass
class RugcheckConfig:
    enabled: bool = True
    base_url: str = "https://api.rugcheck.xyz/v1"
    reject_danger: bool = True
    ignore_risks: list[str] = field(default_factory=list)
    # Maximum score_normalised (0-100, higher = riskier). 0 = off.
    max_score: float = 0.0


@dataclass
class GmgnConfig:
    enabled: bool = True
    base_url: str = "https://gmgn.ai"
    # After a block (403 / Cloudflare page), wait this long before trying again.
    blocked_retry_minutes: float = 30.0


@dataclass
class SafetyConfig:
    refresh_seconds: float = 60.0
    amm_owners: list[str] = field(default_factory=lambda: list(DEFAULT_AMM_OWNERS))
    rugcheck: RugcheckConfig = field(default_factory=RugcheckConfig)
    gmgn: GmgnConfig = field(default_factory=GmgnConfig)


@dataclass
class TradingConfig:
    buy_sol: float = 0.1
    paper_balance_sol: float = 5.0
    max_open_positions: int = 3
    max_buys_per_day: int = 20
    # Daily loss (realized + open positions) that stops new buys until 00:00 UTC.
    max_daily_loss_sol: float = 0.5


@dataclass
class ExitConfig:
    stop_loss_pct: float = 35.0
    # [[rise %, fraction of the original position to sell], ...]
    take_profit: list[list[float]] = field(default_factory=lambda: [[100.0, 0.5]])
    trailing_start_pct: float = 50.0
    trailing_pct: float = 30.0
    max_hold_minutes: float = 60.0
    liquidity_drop_pct: float = 50.0
    no_data_exit_minutes: float = 10.0


@dataclass
class CostConfig:
    use_jupiter: bool = True
    jupiter_url: str = "https://lite-api.jup.ag/swap/v1"
    # Extra loss on top of a Jupiter quote (latency, MEV). Percent.
    extra_slippage_pct: float = 1.0
    # Used only when no Jupiter quote is available.
    est_swap_fee_pct: float = 1.25
    est_slippage_pct: float = 3.0
    # Priority fee + anti-MEV tip per transaction.
    priority_fee_sol: float = 0.001


@dataclass
class NotifyConfig:
    telegram_enabled: bool = True
    notify_migrations: bool = False
    notify_rejections: bool = False
    daily_summary: bool = True


@dataclass
class Settings:
    poll_seconds: float = 2.0
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    data_dir: str = "data/migbot"
    log_dir: str = "logs"
    # Filled from .env, not from the YAML file.
    rpc_url: str = DEFAULT_RPC_URL
    telegram_token: str = ""
    telegram_chat_id: str = ""
    pumpportal_api_key: str = ""
    jupiter_api_key: str = ""
    config_path: str = ""


ENV_FIELDS = {"rpc_url", "telegram_token", "telegram_chat_id", "pumpportal_api_key", "jupiter_api_key", "config_path"}


def _coerce(value, tp, path: str):
    origin = typing.get_origin(tp)
    if dataclasses.is_dataclass(tp):
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: harus berisi pengaturan (bagian), bukan {value!r}")
        return _build(tp, value, path)
    if origin is list:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ConfigError(f"{path}: harus berupa daftar, misalnya [a, b]")
        (item_tp,) = typing.get_args(tp) or (typing.Any,)
        return [_coerce(v, item_tp, f"{path}[{i}]") for i, v in enumerate(value)]
    if tp is bool:
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{path}: harus true atau false, bukan {value!r}")
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ConfigError(f"{path}: harus bilangan bulat, bukan {value!r}")
        return int(value)
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{path}: harus angka, bukan {value!r} (tulis 10000, bukan 10.000)")
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{path}: harus teks, bukan {value!r}")
        return value
    return value


def _build(cls, raw: dict, prefix: str = ""):
    hints = typing.get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)} - ENV_FIELDS
    unknown = sorted(set(raw) - known)
    if unknown:
        where = f"{prefix}." if prefix else ""
        raise ConfigError(
            "pengaturan tidak dikenal: "
            + ", ".join(f"{where}{k}" for k in unknown)
            + " (salah ketik? lihat config/migbot.yaml)"
        )
    kwargs = {}
    for name, value in raw.items():
        path = f"{prefix}.{name}" if prefix else name
        kwargs[name] = _coerce(value, hints[name], path)
    return cls(**kwargs)


def validate(s: Settings) -> None:
    errors = []
    if s.poll_seconds <= 0:
        errors.append("poll_seconds harus > 0")
    if not (s.discovery.pumpportal.enabled or s.discovery.geckoterminal.enabled):
        errors.append("minimal satu sumber migrasi harus aktif (discovery.pumpportal atau discovery.geckoterminal)")
    if s.discovery.geckoterminal.poll_seconds < 5:
        errors.append("discovery.geckoterminal.poll_seconds minimal 5 (batas API GeckoTerminal)")
    if s.market.refresh_seconds < 2:
        errors.append("market.refresh_seconds minimal 2")
    if s.entry.delay_seconds < 0 or s.entry.window_seconds <= s.entry.delay_seconds:
        errors.append("entry.window_seconds harus lebih besar dari entry.delay_seconds")
    if s.tracking.track_minutes * 60 < s.entry.window_seconds:
        errors.append("tracking.track_minutes harus mencakup entry.window_seconds")
    cps = s.tracking.checkpoints_minutes
    if any(c <= 0 for c in cps) or cps != sorted(cps) or len(set(cps)) != len(cps):
        errors.append("tracking.checkpoints_minutes harus angka > 0, urut naik, tanpa duplikat")
    f = s.filters
    if f.min_market_cap_usd > f.max_market_cap_usd > 0:
        errors.append("filters.min_market_cap_usd lebih besar dari max_market_cap_usd")
    if not 0 <= f.min_buy_ratio_5m <= 1:
        errors.append("filters.min_buy_ratio_5m harus antara 0 dan 1 (0.5 = 50%)")
    t = s.trading
    if t.buy_sol <= 0:
        errors.append("trading.buy_sol harus > 0")
    if t.paper_balance_sol < t.buy_sol:
        errors.append("trading.paper_balance_sol lebih kecil dari trading.buy_sol")
    if t.max_open_positions < 1:
        errors.append("trading.max_open_positions minimal 1")
    e = s.exits
    if not 0 < e.stop_loss_pct < 100:
        errors.append("exits.stop_loss_pct harus antara 0 dan 100")
    if not 0 <= e.trailing_pct < 100:
        errors.append("exits.trailing_pct harus antara 0 dan 100 (0 = mati)")
    last_gain = 0.0
    total_frac = 0.0
    for i, level in enumerate(e.take_profit):
        if len(level) != 2 or level[0] <= 0 or not 0 < level[1] <= 1:
            errors.append(f"exits.take_profit[{i}] harus [naik %, porsi 0-1], misalnya [100, 0.5]")
            continue
        if level[0] <= last_gain:
            errors.append("exits.take_profit harus urut dari kenaikan terkecil")
        last_gain = level[0]
        total_frac += level[1]
    if total_frac > 1.0 + 1e-9:
        errors.append("jumlah porsi exits.take_profit lebih dari 1 (100%)")
    if not e.take_profit and e.trailing_pct == 0 and e.max_hold_minutes <= 0:
        errors.append("tidak ada cara keluar untung: isi exits.take_profit, trailing_pct, atau max_hold_minutes")
    if s.costs.extra_slippage_pct < 0 or s.costs.est_slippage_pct < 0 or s.costs.est_swap_fee_pct < 0:
        errors.append("biaya (costs.*) tidak boleh negatif")
    if errors:
        raise ConfigError("config salah:\n  - " + "\n  - ".join(errors))


def load_settings(path: str | None = None, env_path: str | None = ".env") -> Settings:
    """Load settings. The YAML path defaults to $MIGBOT_CONFIG or config/migbot.yaml."""
    if env_path:
        try:
            from dotenv import load_dotenv
        except ImportError:  # python-dotenv is in requirements; tolerate its absence
            load_dotenv = None
        if load_dotenv is not None and os.path.exists(env_path):
            load_dotenv(env_path, override=False)

    path = path or os.getenv("MIGBOT_CONFIG") or "config/migbot.yaml"
    if not os.path.exists(path):
        raise ConfigError(f"file konfigurasi {path} tidak ditemukan")
    with open(path, encoding="utf-8") as fh:
        try:
            raw = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} bukan YAML yang valid: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} harus berisi pengaturan (key: value)")

    settings = _build(Settings, raw)
    settings.config_path = path
    settings.rpc_url = os.getenv("SOLANA_RPC_URL", "").strip() or DEFAULT_RPC_URL
    settings.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    settings.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    settings.pumpportal_api_key = os.getenv("PUMPPORTAL_API_KEY", "").strip()
    settings.jupiter_api_key = os.getenv("JUPITER_API_KEY", "").strip()
    from migbot.http import register_secret

    for secret in (settings.telegram_token, settings.pumpportal_api_key, settings.jupiter_api_key):
        register_secret(secret)
    if settings.rpc_url != DEFAULT_RPC_URL:
        register_secret(settings.rpc_url)
    validate(settings)
    return settings
