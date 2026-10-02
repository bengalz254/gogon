"""Pure DCA ladder math — no I/O, so it is fully unit-testable.

A "ladder" is the base order plus N safety orders. Sizes are USDT notional
(position value, not margin); margin per order = notional / leverage.
"""
from __future__ import annotations

from dataclasses import dataclass

LONG = "long"
SHORT = "short"


def direction(side: str) -> int:
    if side == LONG:
        return 1
    if side == SHORT:
        return -1
    raise ValueError(f"unknown side {side!r}")


@dataclass(frozen=True)
class Level:
    index: int  # 0 = base order, 1..N = safety orders
    deviation_pct: float  # distance from the base entry price, in %
    notional_usdt: float


def build_levels(
    base_order_usdt: float,
    safety_order_usdt: float,
    max_safety_orders: int,
    price_deviation_pct: float,
    step_scale: float,
    volume_scale: float,
) -> list[Level]:
    levels = [Level(0, 0.0, base_order_usdt)]
    deviation = 0.0
    step = price_deviation_pct
    volume = safety_order_usdt
    for i in range(1, max_safety_orders + 1):
        deviation += step
        levels.append(Level(i, deviation, volume))
        step *= step_scale
        volume *= volume_scale
    return levels


def level_price(entry: float, deviation_pct: float, side: str) -> float:
    """Safety orders sit below entry for a long, above entry for a short."""
    return entry * (1 - direction(side) * deviation_pct / 100)


def average_price(fills: list[tuple[float, float]]) -> float:
    """fills: (price, qty) pairs."""
    qty = sum(q for _, q in fills)
    if qty <= 0:
        return 0.0
    return sum(p * q for p, q in fills) / qty


def take_profit_price(avg: float, side: str, take_profit_pct: float) -> float:
    return avg * (1 + direction(side) * take_profit_pct / 100)


def stop_loss_price(entry: float, side: str, stop_loss_pct: float) -> float:
    return entry * (1 - direction(side) * stop_loss_pct / 100)


def liquidation_price(avg: float, side: str, leverage: float, mmr: float) -> float:
    """Isolated-margin liquidation price for a single position whose margin is
    exactly notional / leverage (Binance formula with cum = 0)."""
    if side == LONG:
        return avg * (1 - 1 / leverage) / (1 - mmr)
    return avg * (1 + 1 / leverage) / (1 + mmr)


def pnl(side: str, avg: float, exit_price: float, qty: float) -> float:
    return direction(side) * (exit_price - avg) * qty


@dataclass(frozen=True)
class PlanRow:
    index: int
    deviation_pct: float
    price: float
    qty: float
    notional_usdt: float
    cum_notional_usdt: float
    avg_price: float
    tp_price: float
    liq_price: float


@dataclass(frozen=True)
class Plan:
    side: str
    entry: float
    rows: list[PlanRow]
    sl_price: float
    total_notional_usdt: float
    margin_usdt: float
    # Smallest gap (in % of entry) between the liquidation price at any stage
    # and the next price that stage can reach (next safety order, or SL).
    min_liq_buffer_pct: float


def build_plan(
    entry: float,
    side: str,
    levels: list[Level],
    take_profit_pct: float,
    stop_loss_pct: float,
    leverage: float,
    mmr: float,
) -> Plan:
    d = direction(side)
    sl = stop_loss_price(entry, side, stop_loss_pct)
    rows: list[PlanRow] = []
    fills: list[tuple[float, float]] = []
    cum = 0.0
    for lv in levels:
        price = level_price(entry, lv.deviation_pct, side)
        qty = lv.notional_usdt / price
        fills.append((price, qty))
        cum += lv.notional_usdt
        avg = average_price(fills)
        rows.append(
            PlanRow(
                index=lv.index,
                deviation_pct=lv.deviation_pct,
                price=price,
                qty=qty,
                notional_usdt=lv.notional_usdt,
                cum_notional_usdt=cum,
                avg_price=avg,
                tp_price=take_profit_price(avg, side, take_profit_pct),
                liq_price=liquidation_price(avg, side, leverage, mmr),
            )
        )

    buffers = []
    for i, row in enumerate(rows):
        reach = rows[i + 1].price if i + 1 < len(rows) else sl
        buffers.append(d * (reach - row.liq_price) / entry * 100)

    return Plan(
        side=side,
        entry=entry,
        rows=rows,
        sl_price=sl,
        total_notional_usdt=cum,
        margin_usdt=cum / leverage,
        min_liq_buffer_pct=min(buffers),
    )
