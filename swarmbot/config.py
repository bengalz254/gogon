"""Settings from config/swarmbot.yaml (and JUPITER_API_KEY from .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from swarmbot.moods import MOODS, MoodRules

DEFAULT_PATH = Path("config/swarmbot.yaml")


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    mode: str = "paper"
    poll_seconds: float = 20
    price_check_seconds: float = 3
    jupiter_base_url: str = "https://lite-api.jup.ag"
    jupiter_api_key: str = ""
    jupiter_lists: list[str] = field(default_factory=lambda: [
        "toptrending/5m", "toptraded/5m", "toporganicscore/5m",
        "toptrending/1h", "toptraded/1h", "toporganicscore/1h", "recent",
    ])
    jupiter_limit: int = 100
    buy_moods: list[str] = field(default_factory=lambda: ["shocked", "happy"])
    take_profit_pct: float = 50.0
    stop_loss_pct: float = 50.0
    position_usd: float = 10.0
    starting_cash_usd: float = 300.0
    max_open_positions: int = 25
    max_buys_per_hour: int = 0
    cooldown_minutes: float = 15
    max_hold_hours: float = 0
    max_hold_minutes: float = 10
    stale_minutes: float = 5
    stale_move_pct: float = 2.0
    min_liquidity_usd: float = 0
    min_mcap_usd: float = 0
    max_mcap_usd: float = 0
    skip_suspicious: bool = False
    fee_pct: float = 1.0
    data_dir: Path = Path("data/swarmbot")
    rules: MoodRules = field(default_factory=MoodRules)


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load(path: Path | str = DEFAULT_PATH) -> Config:
    _load_dotenv()
    path = Path(path)
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: isinya harus berupa pengaturan 'nama: nilai'")
    jup = raw.get("jupiter") or {}
    filters = raw.get("filters") or {}
    paper = raw.get("paper") or {}
    c = Config()
    try:
        c.mode = str(raw.get("mode", c.mode)).lower()
        c.poll_seconds = float(raw.get("poll_seconds", c.poll_seconds))
        c.price_check_seconds = float(raw.get("price_check_seconds", c.price_check_seconds))
        c.jupiter_base_url = str(jup.get("base_url", c.jupiter_base_url)).rstrip("/")
        c.jupiter_lists = [str(x) for x in jup.get("lists", c.jupiter_lists)]
        c.jupiter_limit = int(jup.get("limit", c.jupiter_limit))
        c.buy_moods = [str(m).lower() for m in raw.get("buy_moods", c.buy_moods)]
        c.take_profit_pct = float(raw.get("take_profit_pct", c.take_profit_pct))
        c.stop_loss_pct = float(raw.get("stop_loss_pct", c.stop_loss_pct))
        c.position_usd = float(raw.get("position_usd", c.position_usd))
        c.starting_cash_usd = float(raw.get("starting_cash_usd", c.starting_cash_usd))
        c.max_open_positions = int(raw.get("max_open_positions", c.max_open_positions))
        c.max_buys_per_hour = int(raw.get("max_buys_per_hour", c.max_buys_per_hour))
        c.cooldown_minutes = float(raw.get("cooldown_minutes", c.cooldown_minutes))
        c.max_hold_hours = float(raw.get("max_hold_hours", c.max_hold_hours))
        c.max_hold_minutes = float(raw.get("max_hold_minutes", c.max_hold_minutes))
        c.stale_minutes = float(raw.get("stale_minutes", c.stale_minutes))
        c.stale_move_pct = float(raw.get("stale_move_pct", c.stale_move_pct))
        c.min_liquidity_usd = float(filters.get("min_liquidity_usd", c.min_liquidity_usd))
        c.min_mcap_usd = float(filters.get("min_mcap_usd", c.min_mcap_usd))
        c.max_mcap_usd = float(filters.get("max_mcap_usd", c.max_mcap_usd))
        c.skip_suspicious = bool(filters.get("skip_suspicious", c.skip_suspicious))
        c.fee_pct = float(paper.get("fee_pct", c.fee_pct))
        c.data_dir = Path(raw.get("data_dir", c.data_dir))
        rules = raw.get("mood_rules") or {}
        for name in vars(c.rules):
            if name in rules:
                setattr(c.rules, name, type(getattr(c.rules, name))(rules[name]))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: ada angka yang tidak valid ({exc})") from exc

    c.jupiter_api_key = os.environ.get("JUPITER_API_KEY", "").strip()
    if c.jupiter_api_key and "lite-api.jup.ag" in c.jupiter_base_url:
        c.jupiter_base_url = "https://api.jup.ag"

    if c.mode != "paper":
        raise ConfigError("mode harus 'paper'. Trading dengan uang sungguhan belum dibuat di bot ini.")
    bad = [m for m in c.buy_moods if m not in MOODS]
    if bad:
        raise ConfigError(f"buy_moods tidak dikenal: {bad}. Pilihan: {', '.join(MOODS)}")
    if not 0 < c.take_profit_pct <= 1000 or not 0 < c.stop_loss_pct < 100:
        raise ConfigError("take_profit_pct harus > 0 dan stop_loss_pct antara 0 dan 100")
    if c.position_usd <= 0 or c.max_open_positions < 1:
        raise ConfigError("position_usd harus > 0 dan max_open_positions minimal 1")
    if c.poll_seconds < 5 or c.price_check_seconds < 2:
        raise ConfigError("poll_seconds minimal 5 dan price_check_seconds minimal 2")
    return c
