"""Grid trading logic (no I/O).

The price range [lower, upper] is cut into geometric levels L0 < L1 < ... so
every gap is the same percentage wide. Each gap is a slot that buys at its
lower level and sells at its upper one:

    idle    -> buy     a buy order rests at the lower level once that level
                       is below the market and money is available
    buy     -> holding the price traded below the buy level
    holding -> sell    (next poll) a sell order rests at the upper level
    sell    -> idle    the price traded above the sell level: one round trip,
                       one gap of profit minus fees and tax

The grid starts with money only, no coins: slots under the price wait with a
buy order, slots above it stay idle until the price comes back under them.
The risk is the price falling through the whole range, leaving every slot
holding coins worth less than they cost — hence the stop-loss in spot/bot.py.

Fills are simulated from 1-minute candles, pessimistically: an order fills
only if the price traded strictly beyond it (touching its level isn't
enough, since orders queued before it may have taken that volume), and a
counter-order placed after a fill can only fill from the next poll on.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from spot.config import CostConfig, GridConfig
from spot.market import Candle, MarketRules

IDLE, BUYING, HOLDING, SELLING = "idle", "buy", "holding", "sell"
_EPS = 1e-12


def _to_step(value: float, step: float, rounding) -> float:
    step_d = Decimal(str(step))
    units = (Decimal(str(value)) / step_d).to_integral_value(rounding=rounding)
    return float(units * step_d)


def floor_to(value: float, step: float) -> float:
    return _to_step(value, step, ROUND_FLOOR)


def ceil_to(value: float, step: float) -> float:
    return _to_step(value, step, ROUND_CEILING)


def round_to(value: float, step: float) -> float:
    return _to_step(value, step, ROUND_HALF_UP)


def auto_range(price: float, range_pct: float) -> tuple[float, float]:
    return price * (1 - range_pct / 100), price * (1 + range_pct / 100)


def build_levels(lower: float, upper: float, count: int, tick: float) -> list[float]:
    """`count` geometric price levels from lower to upper, on the tick grid."""
    ratio = (upper / lower) ** (1 / (count - 1))
    return [round_to(lower * ratio**i, tick) for i in range(count)]


@dataclass
class Slot:
    index: int
    buy_price: float
    sell_price: float
    amount: float
    state: str = IDLE
    order_price: float = 0.0  # the resting order's price (buy or sell)
    cost: float = 0.0  # quote paid for the held coins, fees and tax included

    @property
    def holds_coins(self) -> bool:
        return self.state in (HOLDING, SELLING)


@dataclass
class Fill:
    slot: int
    side: str  # "BUY" or "SELL"
    price: float
    amount: float
    value: float
    fee: float
    tax: float
    pnl: float = 0.0  # realized, on sells
    taker: bool = False


def make_slots(levels: list[float], order_value: float, step: float) -> list[Slot]:
    return [
        Slot(index=i, buy_price=levels[i], sell_price=levels[i + 1], amount=floor_to(order_value / levels[i], step))
        for i in range(len(levels) - 1)
    ]


def slot_net_profit_pct(slot: Slot, costs: CostConfig) -> float:
    """Profit of one buy→sell round trip after fees and tax, in % of what the buy cost."""
    paid = slot.amount * slot.buy_price * (1 + costs.cost_rate("BUY"))
    received = slot.amount * slot.sell_price * (1 - costs.cost_rate("SELL"))
    return (received - paid) / paid * 100 if paid > 0 else float("-inf")


@dataclass
class Plan:
    levels: list[float]
    slots: list[Slot]
    min_gap_pct: float  # narrowest gap between two levels
    round_trip_cost_pct: float  # fees + tax of one buy and one sell
    net_profit_pct: float  # the worst slot's profit per round trip
    capital: float  # quote needed if every buy order fills
    problems: list[str]


def plan_grid(
    lower: float, upper: float, grid: GridConfig, costs: CostConfig, rules: MarketRules, balance: float
) -> Plan:
    """Levels and slots for a range, with everything that makes it unworkable."""
    levels = build_levels(lower, upper, grid.levels, rules.tick)
    problems: list[str] = []
    if any(b <= a for a, b in zip(levels, levels[1:])) or levels[0] <= 0:
        problems.append(
            f"Range terlalu sempit untuk {grid.levels} level dengan tick {fmt_amount(rules.tick)}: "
            "kurangi grid.levels atau lebarkan range"
        )
        return Plan(levels, [], 0.0, 0.0, 0.0, 0.0, problems)

    slots = make_slots(levels, grid.order_value, rules.step)
    min_gap_pct = min((b / a - 1) * 100 for a, b in zip(levels, levels[1:]))
    round_trip = (costs.cost_rate("BUY") + costs.cost_rate("SELL")) * 100
    net = min(slot_net_profit_pct(s, costs) for s in slots)
    capital = sum(s.amount * s.buy_price * (1 + costs.cost_rate("BUY")) for s in slots)

    small = [s for s in slots if s.amount <= 0 or s.amount < rules.min_amount or s.amount * s.buy_price < rules.min_cost]
    if small:
        s = small[-1]
        need = []
        if rules.min_amount:
            need.append(f"{fmt_amount(rules.min_amount)} {rules.base}")
        if rules.min_cost:
            need.append(fmt_money(rules.min_cost, rules.quote))
        problems.append(
            f"grid.order_value terlalu kecil: order di level {fmt_money(s.buy_price, rules.quote)} hanya "
            f"{fmt_amount(s.amount)} {rules.base}" + (f"; minimal {' / '.join(need)}" if need else "")
        )
    if net < costs.min_net_profit_pct:
        problems.append(
            f"Jarak antar level {min_gap_pct:.2f}% terlalu rapat: biaya + pajak {round_trip:.2f}% per putaran, "
            f"sisa untung {net:.2f}% (minimal {costs.min_net_profit_pct:.2f}%). "
            "Kurangi grid.levels atau lebarkan range"
        )
    if capital > balance + 1e-9:
        problems.append(
            f"Modal kurang: kalau semua order beli terisi grid butuh {fmt_money(capital, rules.quote)}, "
            f"modal paper hanya {fmt_money(balance, rules.quote)}. "
            "Kecilkan grid.order_value atau grid.levels, atau naikkan risk.paper_balance"
        )
    return Plan(levels, slots, min_gap_pct, round_trip, net, capital, problems)


class Grid:
    """The slots and the paper account (quote money + coins) they trade from."""

    def __init__(self, slots: list[Slot], quote: float, base: float, costs: CostConfig):
        self.slots = slots
        self.quote = quote
        self.base = base
        self.costs = costs

    @property
    def lower(self) -> float:
        return self.slots[0].buy_price

    @property
    def upper(self) -> float:
        return self.slots[-1].sell_price

    @property
    def reserved_quote(self) -> float:
        return sum(s.amount * s.order_price * (1 + self.costs.cost_rate("BUY")) for s in self.slots if s.state == BUYING)

    @property
    def available_quote(self) -> float:
        return self.quote - self.reserved_quote

    def count(self, state: str) -> int:
        return sum(1 for s in self.slots if s.state == state)

    # -- fills ---------------------------------------------------------------------
    def apply_candle(self, candle: Candle) -> list[Fill]:
        """Fill the resting orders the candle traded through."""
        fills = []
        for slot in self.slots:
            if slot.state == BUYING and candle.low < slot.order_price - _EPS:
                fills.append(self._fill_buy(slot))
            elif slot.state == SELLING and candle.high > slot.order_price + _EPS:
                fills.append(self._fill_sell(slot, slot.order_price))
        return fills

    def _fill_buy(self, slot: Slot) -> Fill:
        value = slot.amount * slot.order_price
        fee = value * self.costs.fee_rate()
        tax = value * self.costs.tax_rate("BUY")
        self.quote -= value + fee + tax
        self.base += slot.amount
        slot.state, slot.cost = HOLDING, value + fee + tax
        return Fill(slot.index, "BUY", slot.order_price, slot.amount, value, fee, tax)

    def _fill_sell(self, slot: Slot, price: float, taker: bool = False) -> Fill:
        value = slot.amount * price
        fee = value * self.costs.fee_rate(taker)
        tax = value * self.costs.tax_rate("SELL")
        proceeds = value - fee - tax
        pnl = proceeds - slot.cost
        self.quote += proceeds
        self.base -= slot.amount
        slot.state, slot.cost, slot.order_price = IDLE, 0.0, 0.0
        return Fill(slot.index, "SELL", price, slot.amount, value, fee, tax, pnl, taker)

    # -- orders --------------------------------------------------------------------
    def place_orders(self, bid: float | None, ask: float | None, tick: float) -> tuple[int, int]:
        """Rest the orders slots are waiting to place; returns (buys, sells) placed.

        Orders stay on the maker side: a sell goes at its level, or one tick
        above the bid if the price has already run past it; a buy only goes
        in while its level is below the ask, nearest the price first so
        limited money funds the levels most likely to fill.
        """
        sells = 0
        for slot in self.slots:
            if slot.state == HOLDING:
                price = slot.sell_price
                if bid is not None and price <= bid + _EPS:
                    price = ceil_to(bid + tick, tick)
                slot.state, slot.order_price = SELLING, price
                sells += 1
        buys = 0
        if ask is None:
            return buys, sells
        for slot in sorted(self.slots, key=lambda s: -s.buy_price):
            if slot.state != IDLE or slot.buy_price >= ask - _EPS:
                continue
            need = slot.amount * slot.buy_price * (1 + self.costs.cost_rate("BUY"))
            if need > self.available_quote + 1e-9:
                continue
            slot.state, slot.order_price = BUYING, slot.buy_price
            buys += 1
        return buys, sells

    def liquidate(self, bid: float) -> list[Fill]:
        """Stop-loss: drop every buy order and sell all coins at the bid (as a taker)."""
        fills = []
        for slot in self.slots:
            if slot.state == BUYING:
                slot.state, slot.order_price = IDLE, 0.0
            elif slot.holds_coins:
                fills.append(self._fill_sell(slot, bid, taker=True))
        return fills

    # -- valuation -----------------------------------------------------------------
    def unrealized(self, bid: float | None) -> float:
        """What the held coins would fetch at the bid (after selling costs) minus what they cost."""
        if bid is None:
            return 0.0
        keep = 1 - self.costs.cost_rate("SELL")
        return sum(s.amount * bid * keep - s.cost for s in self.slots if s.holds_coins)

    def equity(self, bid: float | None) -> float:
        held_cost = sum(s.cost for s in self.slots if s.holds_coins)
        return self.quote + held_cost + self.unrealized(bid)

    # -- persistence ---------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"quote": self.quote, "base": self.base, "slots": [asdict(s) for s in self.slots]}

    @classmethod
    def from_dict(cls, data: dict, costs: CostConfig) -> "Grid":
        return cls([Slot(**s) for s in data["slots"]], float(data["quote"]), float(data["base"]), costs)


# -- formatting (Indonesian style for rupiah) -----------------------------------------
_RUPIAH = {"IDR", "BIDR", "IDRT"}


def fmt_money(value: float, quote: str) -> str:
    sign = "-" if value < 0 else ""
    if quote in _RUPIAH:
        return f"{sign}Rp {abs(value):,.0f}".replace(",", ".")
    return f"{sign}{abs(value):,.2f} {quote}"


def fmt_amount(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"
