"""The life of one migrated token, and the research row written when it ends.

Every migrated token is followed for `tracking.track_minutes`, bought or
not. Its price is recorded at fixed minutes after the migration, so the
report can compare the tokens the filters picked with the ones they
rejected: the only honest way to tell whether the filters help.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone

WATCHING, BOUGHT, REJECTED, NO_DATA = "dipantau", "dibeli", "ditolak", "tanpa data"
PASSED = "lolos, tak dibeli"  # passed every filter but a risk limit (or no route) kept the bot out


@dataclass
class TrackedToken:
    mint: str
    source: str
    migrated_at: float
    detected_at: float
    symbol: str = ""
    name: str = ""
    pool: str = ""
    dex: str = ""
    signature: str = ""
    sources: list[str] = field(default_factory=list)
    status: str = WATCHING
    pair_address: str = ""
    decimals: int | None = None
    first_price_usd: float | None = None
    first_price_at: float | None = None
    first_mcap_usd: float | None = None
    ref_price_usd: float | None = None
    ref_at: float | None = None
    max_price_usd: float | None = None
    min_price_usd: float | None = None
    checkpoints: dict[str, float] = field(default_factory=dict)
    last: dict = field(default_factory=dict)  # latest MarketSnapshot as a dict
    last_eval_ts: float = 0.0
    evaluations: int = 0
    passed_filters: bool = False
    checks: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    decided_at: float | None = None
    decision_snapshot: dict = field(default_factory=dict)
    safety: dict | None = None
    rugcheck: dict | None = None
    gmgn: dict | None = None
    safety_at: float = 0.0
    rugcheck_at: float = 0.0
    gmgn_at: float = 0.0
    trade_pnl_sol: float | None = None
    trade_pnl_pct: float | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "TrackedToken":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})

    @property
    def label(self) -> str:
        return f"${self.symbol}" if self.symbol else self.mint[:6] + "…"

    def observe_price(self, price_usd: float | None, now: float, delay_s: float, checkpoints: list[float], tolerance_s: float) -> None:
        """Record first/reference/checkpoint/extreme prices from a fresh snapshot."""
        if not price_usd or price_usd <= 0:
            return
        if self.first_price_usd is None:
            self.first_price_usd, self.first_price_at = price_usd, now
        if self.ref_price_usd is None and now >= self.migrated_at + delay_s:
            self.ref_price_usd, self.ref_at = price_usd, now
        for minute in checkpoints:
            key = f"{minute:g}"
            due = self.migrated_at + minute * 60
            if key not in self.checkpoints and due <= now <= due + tolerance_s:
                self.checkpoints[key] = price_usd
        if self.ref_price_usd is not None:
            self.max_price_usd = max(self.max_price_usd or price_usd, price_usd)
            self.min_price_usd = min(self.min_price_usd or price_usd, price_usd)


def _iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def _ret(price: float | None, ref: float | None) -> str:
    if not price or not ref:
        return ""
    return f"{(price / ref - 1) * 100:.2f}"


def _r(value, digits: int = 2) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def token_fields(checkpoints: list[float]) -> list[str]:
    return (
        [
            "migrated_at_utc", "mint", "symbol", "source", "detect_delay_s", "status", "reasons",
            "decided_after_s", "first_price_usd", "ref_price_usd", "first_mcap_usd",
            "mcap_usd", "liquidity_usd", "volume_5m_usd", "txns_5m", "buy_ratio_5m",
            "top10_pct", "top_holder_pct", "dev_pct", "rugcheck_score", "rugcheck_danger",
            "gmgn_holders", "gmgn_smart_buys",
        ]
        + [f"ret_{m:g}m" for m in checkpoints]
        + ["max_ret_pct", "min_ret_pct", "trade_pnl_sol", "trade_pnl_pct"]
    )


def research_row(tok: TrackedToken, checkpoints: list[float]) -> dict:
    snap = tok.decision_snapshot or tok.last or {}
    buys, sells = snap.get("buys_m5"), snap.get("sells_m5")
    txns = buys + sells if buys is not None and sells is not None else None
    safety = tok.safety or {}
    rug = tok.rugcheck or {}
    danger = [r.get("name", "") for r in rug.get("risks", []) if r.get("level") == "danger"]
    gmgn = tok.gmgn or {}
    row = {
        "migrated_at_utc": _iso(tok.migrated_at),
        "mint": tok.mint,
        "symbol": tok.symbol,
        "source": "+".join(tok.sources) or tok.source,
        "detect_delay_s": f"{tok.detected_at - tok.migrated_at:.0f}",
        "status": tok.status,
        "reasons": " | ".join(tok.reasons),
        "decided_after_s": "" if tok.decided_at is None else f"{tok.decided_at - tok.migrated_at:.0f}",
        "first_price_usd": _r(tok.first_price_usd, 12),
        "ref_price_usd": _r(tok.ref_price_usd, 12),
        "first_mcap_usd": _r(tok.first_mcap_usd, 0),
        "mcap_usd": _r(snap.get("market_cap_usd"), 0),
        "liquidity_usd": _r(snap.get("liquidity_usd"), 0),
        "volume_5m_usd": _r(snap.get("volume_m5"), 0),
        "txns_5m": "" if txns is None else str(txns),
        "buy_ratio_5m": "" if not txns else f"{buys / txns:.3f}",
        "top10_pct": _r(safety.get("top10_pct"), 1),
        "top_holder_pct": _r(safety.get("top_holder_pct"), 1),
        "dev_pct": _r(safety.get("dev_pct"), 2),
        "rugcheck_score": _r(rug.get("score_normalised"), 0),
        "rugcheck_danger": "; ".join(danger),
        "gmgn_holders": "" if gmgn.get("holders") is None else str(gmgn["holders"]),
        "gmgn_smart_buys": "" if gmgn.get("smart_buys") is None else str(gmgn["smart_buys"]),
        "max_ret_pct": _ret(tok.max_price_usd, tok.ref_price_usd),
        "min_ret_pct": _ret(tok.min_price_usd, tok.ref_price_usd),
        "trade_pnl_sol": _r(tok.trade_pnl_sol, 4),
        "trade_pnl_pct": _r(tok.trade_pnl_pct, 1),
    }
    for minute in checkpoints:
        key = f"{minute:g}"
        # A return is only meaningful after the reference point (the first moment the bot could buy).
        before_ref = tok.ref_at is None or tok.migrated_at + minute * 60 <= tok.ref_at
        row[f"ret_{key}m"] = "" if before_ref else _ret(tok.checkpoints.get(key), tok.ref_price_usd)
    return row
