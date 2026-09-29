"""Plain data types shared by the sources, the filters and the engine."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass
class MigrationEvent:
    mint: str
    source: str  # "pumpportal" | "geckoterminal"
    migrated_at: float
    received_at: float
    pool: str = ""  # pool type (pumpportal) or pool address (geckoterminal)
    dex: str = ""
    signature: str = ""
    symbol: str = ""
    name: str = ""
    decimals: int | None = None


@dataclass
class MarketSnapshot:
    ts: float
    pair_address: str
    dex_id: str
    price_usd: float | None
    price_native: float | None  # SOL per whole token
    liquidity_usd: float | None
    market_cap_usd: float | None
    volume_m5: float | None = None
    volume_h1: float | None = None
    buys_m5: int | None = None
    sells_m5: int | None = None
    buys_h1: int | None = None
    sells_h1: int | None = None
    change_m5: float | None = None
    change_h1: float | None = None
    pair_created_at: float | None = None
    symbol: str = ""
    name: str = ""
    socials: list[str] = field(default_factory=list)
    url: str = ""

    @property
    def txns_m5(self) -> int | None:
        if self.buys_m5 is None or self.sells_m5 is None:
            return None
        return self.buys_m5 + self.sells_m5

    @property
    def buy_ratio_m5(self) -> float | None:
        total = self.txns_m5
        if not total:
            return None
        return self.buys_m5 / total

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "MarketSnapshot":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})


@dataclass
class SafetyReport:
    ts: float
    decimals: int | None = None
    supply_ui: float | None = None
    token_program: str = ""
    mint_authority: str | None = None
    freeze_authority: str | None = None
    authorities_known: bool = False
    extensions: list[str] = field(default_factory=list)
    top10_pct: float | None = None
    top_holder_pct: float | None = None
    holders_seen: int = 0
    excluded_accounts: int = 0
    creator: str | None = None
    dev_pct: float | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "SafetyReport":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})


@dataclass
class RugcheckReport:
    ts: float
    score: float | None = None
    score_normalised: float | None = None
    risks: list[dict] = field(default_factory=list)  # {"name", "level", "description"}

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "RugcheckReport":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})


@dataclass
class GmgnInfo:
    ts: float
    holders: int | None = None
    top10_pct: float | None = None
    smart_buys: int | None = None
    smart_sells: int | None = None
    sniper_count: int | None = None
    dev_status: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "GmgnInfo":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})


@dataclass
class Quote:
    in_amount: int
    out_amount: int
    price_impact_pct: float = 0.0
    route: str = ""
