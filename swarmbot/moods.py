"""The ten dotswarm.fun moods, computed the way dotswarm.fun/how describes them.

dotswarm reads token data from the Jupiter Tokens API and checks the moods in a
fixed order; the first rule that fits wins:

  1 shocked    up 30%+ in 5 minutes
  2 happy      up 20%+ this hour
  3 focused    accumulation: most of this hour's traders are net buyers
  4 graduated  left the launch curve for a real pool in the last day
  5 newborn    first pool opened less than an hour ago
  6 calm       steady trading, nothing dramatic
  7 suspicious flagged by Jupiter
  8 stressed   down 20%+ this hour
  9 asleep     no trades in the last hour
 10 rekt       down 60% in the hour or 80% in the day

The site has no public API, so the bot applies the same rules to the same data.
"Calm" is not given in numbers on the site; its limits live in the config, and a
token that trades but fits no rule is labelled "other" (never bought).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

MOODS = (
    "shocked", "happy", "focused", "graduated", "newborn",
    "calm", "suspicious", "stressed", "asleep", "rekt",
)


@dataclass
class MoodRules:
    shocked_5m_pct: float = 30.0
    happy_1h_pct: float = 20.0
    stressed_1h_pct: float = -20.0
    rekt_1h_pct: float = -60.0
    rekt_24h_pct: float = -80.0
    calm_max_abs_5m_pct: float = 5.0
    calm_max_abs_1h_pct: float = 10.0
    calm_min_traders_1h: int = 2


@dataclass
class Token:
    mint: str
    symbol: str
    name: str
    price: float
    liquidity: float
    mcap: float
    change_5m: float
    change_1h: float
    change_24h: float
    trades_1h: int
    traders_1h: int
    net_buyers_1h: int
    is_sus: bool
    first_pool_at: datetime | None
    graduated_at: datetime | None
    launchpad: str = ""


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value) -> int:
    return int(_f(value))


def _ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e12 else value
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_token(raw: dict) -> Token | None:
    """One entry of a Jupiter Tokens API v2 answer, or None when it is unusable."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    s5 = raw.get("stats5m") or {}
    s1 = raw.get("stats1h") or {}
    s24 = raw.get("stats24h") or {}
    audit = raw.get("audit") or {}
    first_pool = raw.get("firstPool") or {}
    return Token(
        mint=str(raw["id"]),
        symbol=str(raw.get("symbol") or "?"),
        name=str(raw.get("name") or ""),
        price=_f(raw.get("usdPrice")),
        liquidity=_f(raw.get("liquidity")),
        mcap=_f(raw.get("mcap")),
        change_5m=_f(s5.get("priceChange")),
        change_1h=_f(s1.get("priceChange")),
        change_24h=_f(s24.get("priceChange")),
        trades_1h=_i(s1.get("numBuys")) + _i(s1.get("numSells")),
        traders_1h=_i(s1.get("numTraders")),
        net_buyers_1h=_i(s1.get("numNetBuyers")),
        is_sus=bool(audit.get("isSus")),
        first_pool_at=_ts(first_pool.get("createdAt")),
        graduated_at=_ts(raw.get("graduatedAt")),
        launchpad=str(raw.get("launchpad") or ""),
    )


def classify(t: Token, rules: MoodRules | None = None, now: datetime | None = None) -> str:
    r = rules or MoodRules()
    now = now or datetime.now(timezone.utc)
    if t.change_5m >= r.shocked_5m_pct:
        return "shocked"
    if t.change_1h >= r.happy_1h_pct:
        return "happy"
    if t.traders_1h > 0 and t.net_buyers_1h * 2 > t.traders_1h:
        return "focused"
    if t.graduated_at and (now - t.graduated_at).total_seconds() < 24 * 3600:
        return "graduated"
    if t.first_pool_at and (now - t.first_pool_at).total_seconds() < 3600:
        return "newborn"
    if (
        t.traders_1h >= r.calm_min_traders_1h
        and abs(t.change_5m) <= r.calm_max_abs_5m_pct
        and abs(t.change_1h) <= r.calm_max_abs_1h_pct
        and not t.is_sus
    ):
        return "calm"
    if t.is_sus:
        return "suspicious"
    if t.change_1h <= r.stressed_1h_pct:
        return "stressed"
    if t.trades_1h == 0:
        return "asleep"
    if t.change_1h <= r.rekt_1h_pct or t.change_24h <= r.rekt_24h_pct:
        return "rekt"
    # Trading, but too jumpy for "calm" and not big enough for any other mood.
    # The bot never buys these.
    return "other"
