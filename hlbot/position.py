"""Take-profit / trailing-stop logic for one open position.

The same `ExitTracker` drives both the backtester (fed OHLC candles) and the
live bot (fed real-time prices), so both apply identical exit rules.

Exit modes (`tp_pct` / `trailing_pct` here are PRICE-move fractions; the
config converts percentages of margin into price moves by dividing by the
leverage, see TradeConfig.pct_basis):

* ``trailing`` (default) — "TP lalu trailing". Nothing happens until
  price has moved `tp_pct` in our favour. From then on a trailing stop sits
  `trailing_pct` behind the best price seen and the position closes when
  price pulls back that far. Profit is therefore at least ~(tp - trailing)
  and can run further while the trend continues.
* ``fixed`` — hard take-profit exactly at `tp_pct`, plus a trailing stop
  `trailing_pct` behind the best price seen since entry (which also works as
  a tight stop-loss right after entry).

With `tp_pct=None` there is no take-profit and no trailing stop at all: the
position is only closed by the strategy (opposite EMA cross), the optional
stop-loss or liquidation.

Optional `stop_loss_pct` and `liquidation_price` are honoured in all modes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from hlbot.strategy import LONG, SHORT

EXIT_MODES = ("trailing", "fixed")


@dataclass(frozen=True)
class ExitEvent:
    price: float
    reason: str  # TAKE_PROFIT | TRAILING_STOP | STOP_LOSS | LIQUIDATION


@dataclass
class ExitTracker:
    side: str
    entry_price: float
    tp_pct: Optional[float]
    trailing_pct: Optional[float]
    mode: str = "trailing"
    stop_loss_pct: Optional[float] = None
    liquidation_price: Optional[float] = None
    best_price: float = field(default=0.0)
    worst_price: float = field(default=0.0)
    trailing_active: bool = field(default=False)
    last_price: float = field(default=0.0)

    def __post_init__(self) -> None:
        if self.side not in (LONG, SHORT):
            raise ValueError(f"side must be LONG or SHORT, got {self.side!r}")
        if self.mode not in EXIT_MODES:
            raise ValueError(f"exit mode must be one of {EXIT_MODES}, got {self.mode!r}")
        if self.mode == "fixed" and self.tp_pct is None:
            raise ValueError("exit mode 'fixed' needs a take-profit")
        if not self.best_price:
            self.best_price = self.entry_price
        if not self.worst_price:
            self.worst_price = self.entry_price
        if not self.last_price:
            self.last_price = self.entry_price
        if self.mode == "fixed":
            self.trailing_active = True

    # -- derived prices --------------------------------------------------
    @property
    def direction(self) -> int:
        return 1 if self.side == LONG else -1

    @property
    def tp_price(self) -> Optional[float]:
        if self.tp_pct is None:
            return None
        return self.entry_price * (1 + self.direction * self.tp_pct)

    @property
    def trailing_stop_price(self) -> Optional[float]:
        if not self.trailing_active:
            return None
        return self.best_price * (1 - self.direction * self.trailing_pct)

    def protective_stop(self) -> Optional[ExitEvent]:
        """The closest stop below (long) / above (short) the market, if any."""
        d = self.direction
        candidates: list[ExitEvent] = []
        trail = self.trailing_stop_price
        if trail is not None:
            candidates.append(ExitEvent(trail, "TRAILING_STOP"))
        if self.stop_loss_pct:
            candidates.append(ExitEvent(self.entry_price * (1 - d * self.stop_loss_pct), "STOP_LOSS"))
        if self.liquidation_price:
            candidates.append(ExitEvent(self.liquidation_price, "LIQUIDATION"))
        if not candidates:
            return None
        # Long: the highest stop is hit first. Short: the lowest.
        return max(candidates, key=lambda e: e.price * d)

    def adverse_excursion_pct(self) -> float:
        """Worst move against the position since entry, as a positive price %."""
        return max(0.0, -self.direction * (self.worst_price - self.entry_price) / self.entry_price)

    # -- price feed --------------------------------------------------------
    def on_price(self, price: float) -> Optional[ExitEvent]:
        """Feed the next observed price. Returns an ExitEvent if the position
        should be closed (the price is the stop/TP level that was crossed)."""
        event = self._move(self.last_price, price)
        self.last_price = price
        return event

    def on_candle(self, o: float, h: float, low: float, c: float) -> Optional[ExitEvent]:
        """Feed a whole OHLC candle (backtest). The intra-candle path is
        assumed to be open->low->high->close for a green candle and
        open->high->low->close for a red one."""
        path = (o, low, h, c) if c >= o else (o, h, low, c)
        for p in path:
            event = self.on_price(p)
            if event:
                return event
        return None

    def _move(self, a: float, b: float) -> Optional[ExitEvent]:
        d = self.direction
        if (b - a) * d > 0:  # favourable move
            if self.mode == "fixed" and (b - self.tp_price) * d >= 0:
                return ExitEvent(self.tp_price, "TAKE_PROFIT")
            if (b - self.best_price) * d > 0:
                self.best_price = b
            tp = self.tp_price
            if not self.trailing_active and tp is not None and (self.best_price - tp) * d >= 0:
                self.trailing_active = True
            return None

        if (b - self.worst_price) * d < 0:
            self.worst_price = b
        stop = self.protective_stop()
        if stop is not None and (b - stop.price) * d <= 0:
            return stop
        return None

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ExitTracker":
        return cls(**data)
