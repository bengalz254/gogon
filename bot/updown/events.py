"""Engine input events.

Every piece of outside information reaches the engine as one of these small
immutable records. That single choke point is what makes the engine
replayable: the live runner, the recorder, the backtester and the synthetic
simulator all speak this same vocabulary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import ClassVar

from bot.updown.window import WindowSpec


@dataclass(frozen=True)
class OracleTick:
    """Settlement-source price (Chainlink, via Polymarket RTDS)."""

    kind: ClassVar[str] = "oracle"
    asset: str
    ts: float
    price: float


@dataclass(frozen=True)
class CexTick:
    """Fast exchange price -- a leading indicator only, never settlement."""

    kind: ClassVar[str] = "cex"
    asset: str
    ts: float
    price: float


@dataclass(frozen=True)
class BookSnapshot:
    kind: ClassVar[str] = "book"
    token_id: str
    ts: float
    bids: tuple = ()  # ((price, size), ...)
    asks: tuple = ()


@dataclass(frozen=True)
class BookLevel:
    kind: ClassVar[str] = "level"
    token_id: str
    ts: float
    side: str  # BUY (bid) / SELL (ask)
    price: float
    size: float  # new total size at this price; 0 removes the level


@dataclass(frozen=True)
class TradePrint:
    kind: ClassVar[str] = "trade"
    token_id: str
    ts: float
    price: float
    size: float
    side: str = ""  # taker side if known


@dataclass(frozen=True)
class TickSizeChange:
    kind: ClassVar[str] = "tick"
    token_id: str
    ts: float
    tick_size: float


@dataclass(frozen=True)
class WindowListed:
    kind: ClassVar[str] = "window"
    spec: WindowSpec


@dataclass(frozen=True)
class OfficialPriceToBeat:
    kind: ClassVar[str] = "official_ptb"
    window_id: str
    price: float


@dataclass(frozen=True)
class OfficialResolution:
    kind: ClassVar[str] = "resolution"
    window_id: str
    winner: str  # "Up" / "Down"


@dataclass(frozen=True)
class FeedStatus:
    kind: ClassVar[str] = "feed"
    feed: str  # oracle | cex | clob
    ts: float
    connected: bool
    detail: str = field(default="", compare=False)


EVENT_TYPES = {
    cls.kind: cls
    for cls in (
        OracleTick, CexTick, BookSnapshot, BookLevel, TradePrint, TickSizeChange,
        WindowListed, OfficialPriceToBeat, OfficialResolution, FeedStatus,
    )
}


def event_to_dict(ev) -> dict:
    d = asdict(ev)
    d["k"] = ev.kind
    return d


def event_from_dict(d: dict):
    d = dict(d)
    cls = EVENT_TYPES[d.pop("k")]
    if cls is WindowListed:
        return WindowListed(spec=WindowSpec(**d["spec"]))
    if cls is BookSnapshot:
        d["bids"] = tuple(tuple(x) for x in d.get("bids", ()))
        d["asks"] = tuple(tuple(x) for x in d.get("asks", ()))
    return cls(**d)
