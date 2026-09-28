"""Paper broker: real-time Binance prices, simulated fills, zero risk.

Market orders fill at the current best bid/ask plus slippage and pay the
taker fee. Resting stops and take-profits are checked against every closed
candle with the SAME pessimistic rules the backtester uses
(`trade.simulate_exit`), so paper results and backtest results are directly
comparable. No API key is needed.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Callable

from scalper.config import Settings
from scalper.models import LONG, SHORT, ClosedTradeInfo, Fill, OrderRef, PositionInfo, SymbolRules
from scalper.trade import Trade, simulate_exit

FUNDING_INTERVAL_MS = 8 * 3_600_000


class PaperBrokerError(Exception):
    pass


@dataclass
class PaperPosition:
    symbol: str
    qty: float  # signed
    entry_price: float
    opened_at: int
    entry_fee: float
    stop: float | None = None
    stop_id: str = ""
    stop_cid: str = ""
    tp: float | None = None
    tp_id: str = ""
    tp_cid: str = ""
    funding: float = 0.0
    last_bar_time: int = 0


class PaperBroker:
    kind = "paper"

    def __init__(
        self,
        settings: Settings,
        rules: dict[str, SymbolRules] | None,
        quote_fn: Callable[[str], tuple[float, float]],
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.rules = rules or {}
        self.quote_fn = quote_fn
        self.clock = clock
        self.balance = float(settings.paper.starting_balance)
        self.pos: dict[str, PaperPosition] = {}
        self._closed: dict[str, ClosedTradeInfo] = {}
        self._seq = 0
        self.fee_rates: dict[str, tuple[float, float]] = {}

    # -- persistence ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "balance": self.balance,
            "positions": {k: asdict(v) for k, v in self.pos.items()},
            "closed": {k: asdict(v) for k, v in self._closed.items()},
            "seq": self._seq,
        }

    def load_dict(self, d: dict | None) -> None:
        if not d:
            return
        self.balance = float(d.get("balance", self.balance))
        self.pos = {k: PaperPosition(**v) for k, v in (d.get("positions") or {}).items()}
        self._closed = {k: ClosedTradeInfo(**v) for k, v in (d.get("closed") or {}).items()}
        self._seq = int(d.get("seq", 0))

    # -- helpers -----------------------------------------------------------------
    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _next_id(self) -> str:
        self._seq += 1
        return f"P{self._seq}"

    @property
    def _slip(self) -> float:
        return self.settings.costs.slippage

    def prepare(self, symbols: list[str]) -> list[str]:
        return []

    def balances(self) -> tuple[float, float]:
        lev = max(1, self.settings.execution.leverage)
        margin_used = sum(abs(p.qty) * p.entry_price / lev for p in self.pos.values())
        return self.balance, max(0.0, self.balance - margin_used)

    def positions(self) -> dict[str, PositionInfo]:
        return {
            s: PositionInfo(symbol=s, qty=p.qty, entry_price=p.entry_price) for s, p in self.pos.items()
        }

    def _close(self, symbol: str, price: float, fee: float, t: int) -> None:
        p = self.pos.pop(symbol)
        qty = abs(p.qty)
        gross = (price - p.entry_price) * qty if p.qty > 0 else (p.entry_price - price) * qty
        self.balance += gross - fee
        self._closed[symbol] = ClosedTradeInfo(
            exit_price=price,
            qty=qty,
            gross_pnl=gross,
            fees=p.entry_fee + fee,
            funding=p.funding,
            exit_time=t,
        )

    # -- orders --------------------------------------------------------------
    def market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        reduce_only: bool = False,
        client_id: str | None = None,
    ) -> Fill:
        q = float(qty)
        bid, ask = self.quote_fn(symbol)
        price = ask * (1 + self._slip) if side == "BUY" else bid * (1 - self._slip)
        fee = q * price * self.settings.costs.taker_fee
        now = self._now_ms()
        existing = self.pos.get(symbol)
        oid = self._next_id()
        if reduce_only:
            if existing is None or (existing.qty > 0) == (side == "BUY"):
                return Fill(qty=0.0, avg_price=0.0, order_id=oid, client_id=client_id or "")
            q = abs(existing.qty)
            fee = q * price * self.settings.costs.taker_fee
            self._close(symbol, price, fee, now)
            return Fill(qty=q, avg_price=price, fee=fee, order_id=oid, client_id=client_id or "")
        if existing is not None:
            raise PaperBrokerError(f"{symbol}: paper position already open")
        self.balance -= fee
        self.pos[symbol] = PaperPosition(
            symbol=symbol,
            qty=q if side == "BUY" else -q,
            entry_price=price,
            opened_at=now,
            entry_fee=fee,
        )
        return Fill(qty=q, avg_price=price, fee=fee, order_id=oid, client_id=client_id or "")

    def place_stop(self, symbol: str, trade_side: str, stop_price: float, client_id: str | None = None) -> OrderRef:
        p = self.pos.get(symbol)
        if p is None:
            raise PaperBrokerError(f"{symbol}: no paper position to protect")
        rules = self.rules.get(symbol)
        price = float(rules.stop_price(stop_price, trade_side)) if rules else stop_price
        p.stop, p.stop_id, p.stop_cid = price, self._next_id(), client_id or ""
        return OrderRef("paper", p.stop_id, p.stop_cid, "sl", "STOP_MARKET", price)

    def place_take_profit(
        self, symbol: str, trade_side: str, price: float, qty: Decimal, client_id: str | None = None
    ) -> OrderRef:
        p = self.pos.get(symbol)
        if p is None:
            raise PaperBrokerError(f"{symbol}: no paper position for a take-profit")
        rules = self.rules.get(symbol)
        tp = float(rules.target_price(price, trade_side)) if rules else price
        p.tp, p.tp_id, p.tp_cid = tp, self._next_id(), client_id or ""
        otype = "LIMIT" if self.settings.execution.take_profit_order == "limit" else "TAKE_PROFIT_MARKET"
        return OrderRef("paper", p.tp_id, p.tp_cid, "tp", otype, tp)

    def cancel(self, symbol: str, ref: OrderRef) -> None:
        p = self.pos.get(symbol)
        if p is None:
            return
        if p.stop_id and ref.order_id == p.stop_id:
            p.stop, p.stop_id, p.stop_cid = None, "", ""
        if p.tp_id and ref.order_id == p.tp_id:
            p.tp, p.tp_id, p.tp_cid = None, "", ""

    def cancel_all(self, symbol: str) -> list[OrderRef]:
        p = self.pos.get(symbol)
        if p is not None:
            p.stop, p.stop_id, p.stop_cid = None, "", ""
            p.tp, p.tp_id, p.tp_cid = None, "", ""
        return []

    def open_orders(self, symbol: str) -> list[OrderRef]:
        p = self.pos.get(symbol)
        refs: list[OrderRef] = []
        if p is None:
            return refs
        if p.stop is not None:
            refs.append(OrderRef("paper", p.stop_id, p.stop_cid, "sl", "STOP_MARKET", p.stop))
        if p.tp is not None:
            otype = "LIMIT" if self.settings.execution.take_profit_order == "limit" else "TAKE_PROFIT_MARKET"
            refs.append(OrderRef("paper", p.tp_id, p.tp_cid, "tp", otype, p.tp))
        return refs

    def close_position(self, symbol: str, pos: PositionInfo) -> Fill:
        side = "SELL" if pos.qty > 0 else "BUY"
        return self.market_order(symbol, side, Decimal(str(abs(pos.qty))), reduce_only=True)

    def closed_trade_info(self, symbol: str, trade, final: bool = False) -> ClosedTradeInfo | None:
        return self._closed.pop(symbol, None)

    # -- simulation --------------------------------------------------------------
    def on_candle(self, symbol: str, c) -> None:
        """Fill resting stop / take-profit orders that this closed candle hit."""
        p = self.pos.get(symbol)
        if p is None or c.close_time <= p.opened_at or c.open_time <= p.last_bar_time:
            return
        p.last_bar_time = c.open_time
        if c.open_time % FUNDING_INTERVAL_MS == 0 and p.opened_at < c.open_time:
            cost = abs(p.qty) * c.open * self.settings.costs.funding_bps_per_8h / 10_000.0
            p.funding += cost
            self.balance -= cost
        if p.stop is None and p.tp is None:
            return
        long = p.qty > 0
        inf = float("inf")
        probe = Trade(
            trade_id="paper",
            symbol=symbol,
            side=LONG if long else SHORT,
            strategy="",
            qty=abs(p.qty),
            entry_price=p.entry_price,
            stop=p.stop if p.stop is not None else (-inf if long else inf),
            initial_stop=p.stop if p.stop is not None else p.entry_price,
            take_profit=p.tp if p.tp is not None else (inf if long else -inf),
            opened_at=p.opened_at,
        )
        tp_is_limit = self.settings.execution.take_profit_order == "limit"
        ex = simulate_exit(probe, c, self._slip, tp_is_limit)
        if ex is None:
            return
        costs = self.settings.costs
        fee = abs(p.qty) * ex.price * (costs.maker_fee if ex.maker else costs.taker_fee)
        self._close(symbol, ex.price, fee, c.close_time)
