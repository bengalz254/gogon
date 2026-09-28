"""Open-trade state plus the trade-management and fill-simulation rules.

These functions are shared by the backtester, the paper broker and the live
engine, so a trade is managed exactly the same way in all three. That is what
makes a backtest worth anything: if live trading used different exit rules,
the backtest would be measuring a different bot.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, fields

from scalper.config import ManagementConfig
from scalper.models import LONG, SHORT, Candle, OrderRef, Signal


def new_trade_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Trade:
    trade_id: str
    symbol: str
    side: str
    strategy: str
    qty: float
    entry_price: float
    stop: float
    initial_stop: float
    take_profit: float
    opened_at: int  # ms, time of the entry fill
    entry_fee: float = 0.0
    initial_qty: float = 0.0  # size at entry (qty can shrink after a partial take-profit)
    best_price: float = 0.0  # best high (long) / low (short) seen on closed candles
    stop_kind: str = "SL"  # SL | BE | TRAIL
    bars_held: int = 0
    last_bar_time: int = -1  # open_time of the last candle counted in bars_held
    reason: str = ""
    funding_paid: float = 0.0
    adopted: bool = False
    entry_order_id: str = ""
    exit_hint: str = ""  # set when the bot itself closes the trade (TIME, NO_STOP, ...)
    close_attempts: int = 0  # how many times we tried to settle a closed trade
    sl_order: OrderRef | None = None
    tp_order: OrderRef | None = None

    def __post_init__(self):
        if not self.best_price:
            self.best_price = self.entry_price
        if not self.initial_qty:
            self.initial_qty = self.qty

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_price - self.initial_stop)

    def r_multiple(self, price: float) -> float:
        risk = self.risk_per_unit
        if risk <= 0:
            return 0.0
        move = price - self.entry_price if self.side == LONG else self.entry_price - price
        return move / risk

    def unrealized_pnl(self, price: float) -> float:
        diff = price - self.entry_price if self.side == LONG else self.entry_price - price
        return diff * self.qty

    def observe(self, c: Candle) -> bool:
        """Count one more closed candle while the trade is open.

        Only candles that END after the entry count, and each candle counts
        once (safe to call again after a restart). Returns True if counted.
        """
        if c.close_time <= self.opened_at or c.open_time <= self.last_bar_time:
            return False
        self.last_bar_time = c.open_time
        self.bars_held += 1
        if self.side == LONG:
            self.best_price = max(self.best_price, c.high)
        else:
            self.best_price = min(self.best_price, c.low)
        return True

    def to_dict(self) -> dict:
        d = {}
        for f in fields(self):
            v = getattr(self, f.name)
            d[f.name] = v.to_dict() if isinstance(v, OrderRef) else v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Trade":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        kwargs["sl_order"] = OrderRef.from_dict(d.get("sl_order"))
        kwargs["tp_order"] = OrderRef.from_dict(d.get("tp_order"))
        return cls(**kwargs)


def targets_from_fill(signal: Signal, fill_price: float) -> tuple[float, float] | None:
    """Stop and take-profit for a trade actually filled at `fill_price`.

    The stop stays at the level the strategy chose (it is anchored to market
    structure). An R-multiple target is re-anchored to the real fill so the
    planned reward:risk holds even after slippage. Returns None if the fill
    already sits beyond the stop or the fixed target — the setup is gone.
    """
    stop = signal.stop
    if signal.side == LONG:
        if stop >= fill_price:
            return None
        tp = fill_price + signal.tp_r * (fill_price - stop) if signal.tp_r else signal.take_profit
        if tp <= fill_price:
            return None
    else:
        if stop <= fill_price:
            return None
        tp = fill_price - signal.tp_r * (stop - fill_price) if signal.tp_r else signal.take_profit
        if tp >= fill_price:
            return None
    return stop, tp


@dataclass
class ManageAction:
    new_stop: float | None = None
    new_stop_kind: str = ""
    exit_reason: str = ""


def manage_trade(
    trade: Trade,
    c: Candle,
    atr: float | None,
    cfg: ManagementConfig,
    round_trip_cost: float,
) -> ManageAction:
    """Decide, at a candle close, whether to tighten the stop or exit.

    Call `trade.observe(c)` first. Rules, in order:
      * time stop: exit after `max_bars_in_trade` bars (scalps that don't
        work quickly usually don't work);
      * breakeven: once the trade has been `breakeven_at_r` R in profit, move
        the stop to entry + round-trip costs, so a reversal exits at ~zero
        instead of a full loss;
      * trailing: once `trail_start_r` R in profit, trail the stop
        `trail_atr` ATR behind the best price.
    A stop only ever moves in the trade's favour, and never to within
    `min_stop_gap_atr` ATR of the close (that would trigger instantly).
    """
    if cfg.max_bars_in_trade > 0 and trade.bars_held >= cfg.max_bars_in_trade:
        return ManageAction(exit_reason="TIME")
    if trade.risk_per_unit <= 0:
        return ManageAction()

    long = trade.side == LONG
    sign = 1.0 if long else -1.0
    best_r = trade.r_multiple(trade.best_price)
    candidates: list[tuple[float, str]] = []
    if cfg.breakeven_at_r > 0 and best_r >= cfg.breakeven_at_r:
        candidates.append((trade.entry_price * (1.0 + sign * round_trip_cost), "BE"))
    if cfg.trail_start_r > 0 and atr and best_r >= cfg.trail_start_r:
        candidates.append((trade.best_price - sign * cfg.trail_atr * atr, "TRAIL"))

    gap = cfg.min_stop_gap_atr * (atr or 0.0)
    valid = []
    for level, kind in candidates:
        improves = level > trade.stop if long else level < trade.stop
        room = level < c.close - gap if long else level > c.close + gap
        if improves and room:
            valid.append((level, kind))
    if not valid:
        return ManageAction()
    level, kind = max(valid) if long else min(valid)
    return ManageAction(new_stop=level, new_stop_kind=kind)


@dataclass
class ExitFill:
    price: float
    reason: str  # TP | SL | BE | TRAIL
    maker: bool


def simulate_exit(trade: Trade, c: Candle, slippage: float, tp_is_limit: bool) -> ExitFill | None:
    """Did the resting stop / take-profit fill during candle `c`? (backtest & paper)

    Candles don't say whether the high or the low came first, so when a bar
    touches both the stop and the target this assumes the STOP filled — the
    pessimistic answer. A limit take-profit needs price to trade THROUGH the
    level (touching it isn't proof of a fill); a stop fills with slippage,
    and a bar that opens beyond the stop fills at the open.
    """
    stop, tp = trade.stop, trade.take_profit
    if trade.side == LONG:
        if c.open <= stop:
            return ExitFill(c.open * (1 - slippage), trade.stop_kind, False)
        if c.low <= stop:
            return ExitFill(stop * (1 - slippage), trade.stop_kind, False)
        if tp_is_limit:
            if c.open >= tp or c.high > tp:
                return ExitFill(tp, "TP", True)
        else:
            if c.open >= tp:
                return ExitFill(c.open * (1 - slippage), "TP", False)
            if c.high >= tp:
                return ExitFill(tp * (1 - slippage), "TP", False)
        return None

    if c.open >= stop:
        return ExitFill(c.open * (1 + slippage), trade.stop_kind, False)
    if c.high >= stop:
        return ExitFill(stop * (1 + slippage), trade.stop_kind, False)
    if tp_is_limit:
        if c.open <= tp or c.low < tp:
            return ExitFill(tp, "TP", True)
    else:
        if c.open <= tp:
            return ExitFill(c.open * (1 + slippage), "TP", False)
        if c.low <= tp:
            return ExitFill(tp * (1 + slippage), "TP", False)
    return None


def gross_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    return (exit_price - entry) * qty if side == LONG else (entry - exit_price) * qty


__all__ = [
    "Trade",
    "ManageAction",
    "ExitFill",
    "manage_trade",
    "simulate_exit",
    "targets_from_fill",
    "gross_pnl",
    "new_trade_id",
    "LONG",
    "SHORT",
]
