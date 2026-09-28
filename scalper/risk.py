"""Position sizing and account-level risk guards.

Sizing is RISK-based, not leverage-based: every trade risks about
`risk_per_trade_pct` of equity if the stop is hit (fees and slippage
included). Leverage only decides how much margin that position ties up; it
does not make the bet bigger. That is the single most important difference
between a bot that survives a losing streak and one that doesn't.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from scalper.config import CostConfig, RiskConfig
from scalper.models import LONG, Signal, SymbolRules


@dataclass
class SizeResult:
    qty: float
    notional: float
    risk_usd: float  # expected loss if the stop fills, fees + slippage included
    reason: str = ""  # non-empty = rejected
    qty_dec: Decimal | None = None

    @property
    def ok(self) -> bool:
        return not self.reason and self.qty > 0


def size_position(
    equity: float,
    available_balance: float,
    entry: float,
    stop: float,
    rules: SymbolRules | None,
    risk: RiskConfig,
    costs: CostConfig,
    leverage: int,
) -> SizeResult:
    if equity <= 0 or entry <= 0:
        return SizeResult(0.0, 0.0, 0.0, "no equity")
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return SizeResult(0.0, 0.0, 0.0, "stop equals entry")

    stop_frac = stop_dist / entry
    liq_frac = 1.0 / leverage - risk.maintenance_margin_rate
    if liq_frac <= 0 or stop_frac >= risk.liquidation_buffer * liq_frac:
        return SizeResult(
            0.0, 0.0, 0.0,
            f"stop is {stop_frac:.2%} away but liquidation at {leverage}x is only "
            f"~{max(liq_frac, 0):.2%} away — lower execution.leverage",
        )

    budget = equity * risk.risk_per_trade_pct / 100.0
    per_unit_cost = entry * (costs.taker_fee + costs.slippage) + stop * (costs.taker_fee + costs.slippage)
    qty = budget / (stop_dist + per_unit_cost)

    cap = max(available_balance, 0.0) * leverage * risk.max_margin_use_pct / 100.0
    if risk.max_notional_usd > 0:
        cap = min(cap, risk.max_notional_usd)
    capped = False
    if qty * entry > cap:
        qty = cap / entry
        capped = True

    qty_dec = None
    if rules is not None:
        qty_dec = rules.qty(qty, market=True)
        problem = rules.check_order(qty_dec, entry, market=True)
        if problem:
            hint = " (margin cap hit: add balance or raise leverage)" if capped else (
                " (account too small for this symbol at this risk_per_trade_pct)"
            )
            return SizeResult(0.0, 0.0, 0.0, problem + hint)
        qty = float(qty_dec)
    if qty <= 0:
        return SizeResult(0.0, 0.0, 0.0, "size rounds to zero")
    return SizeResult(
        qty=qty,
        notional=qty * entry,
        risk_usd=qty * (stop_dist + per_unit_cost),
        qty_dec=qty_dec,
    )


def fee_filter(signal: Signal, costs: CostConfig, risk: RiskConfig) -> str:
    """Reject setups whose stop is too tight to beat trading costs.

    If the stop is 0.2% away and a round trip costs 0.12%, every loser costs
    1.6R and every winner loses 0.6R to fees — no entry signal survives that.
    Requiring the stop to be `min_sl_cost_ratio` × round-trip cost away keeps
    costs a small slice of each trade. Returns "" when OK.
    """
    if risk.min_sl_cost_ratio <= 0 or signal.entry_ref <= 0:
        return ""
    stop_frac = signal.risk_per_unit / signal.entry_ref
    needed = risk.min_sl_cost_ratio * costs.round_trip_cost
    if stop_frac < needed:
        return (
            f"stop {stop_frac:.3%} < {risk.min_sl_cost_ratio}× round-trip cost "
            f"({needed:.3%}); fees would eat the edge"
        )
    return ""


def _utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class RiskState:
    day: str = ""
    realized_today: float = 0.0
    trades_today: int = 0
    loss_streak: int = 0
    cooldown_until: int = 0
    baseline_equity: float = 0.0  # equity when tracking started
    cum_realized: float = 0.0  # realized P&L since tracking started
    peak_value: float = 0.0  # peak of baseline + cum_realized
    day_start_value: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    symbol_cooldown_until: dict = field(default_factory=dict)


class RiskGuard:
    """Account-level circuit breakers.

    Drawdown and the daily loss limit are measured on REALIZED P&L from a
    baseline, not on wallet balance, so deposits and withdrawals don't
    trigger (or mask) them.
    """

    def __init__(self, cfg: RiskConfig, state: RiskState | None = None):
        self.cfg = cfg
        self.s = state or RiskState()

    # -- persistence ------------------------------------------------------
    def to_dict(self) -> dict:
        return dict(self.s.__dict__)

    @classmethod
    def from_dict(cls, cfg: RiskConfig, d: dict | None) -> "RiskGuard":
        st = RiskState()
        for k, v in (d or {}).items():
            if hasattr(st, k):
                setattr(st, k, v)
        return cls(cfg, st)

    # -- bookkeeping --------------------------------------------------------
    @property
    def value(self) -> float:
        return self.s.baseline_equity + self.s.cum_realized

    @property
    def drawdown_pct(self) -> float:
        if self.s.peak_value <= 0:
            return 0.0
        return max(0.0, (self.s.peak_value - self.value) / self.s.peak_value * 100.0)

    def start(self, equity: float, now_ms: int) -> None:
        if self.s.baseline_equity <= 0:
            self.s.baseline_equity = equity
            self.s.peak_value = equity
        self.on_time(now_ms)

    def on_time(self, now_ms: int) -> bool:
        """Roll the UTC day if needed. Returns True when a new day started."""
        day = _utc_day(now_ms)
        if day == self.s.day:
            return False
        self.s.day = day
        self.s.realized_today = 0.0
        self.s.trades_today = 0
        self.s.day_start_value = self.value
        return True

    @property
    def daily_loss_hit(self) -> bool:
        base = self.s.day_start_value or self.s.baseline_equity
        if base <= 0:
            return False
        return self.s.realized_today <= -base * self.cfg.max_daily_loss_pct / 100.0

    def halt(self, reason: str) -> None:
        self.s.halted = True
        self.s.halt_reason = reason

    def reset_halt(self) -> None:
        self.s.halted = False
        self.s.halt_reason = ""
        self.s.peak_value = self.value

    def can_open(self, symbol: str, now_ms: int, open_positions: int) -> tuple[bool, str]:
        self.on_time(now_ms)
        if self.s.halted:
            return False, f"bot halted: {self.s.halt_reason}"
        if self.drawdown_pct >= self.cfg.max_drawdown_pct:
            self.halt(
                f"max drawdown {self.drawdown_pct:.1f}% >= {self.cfg.max_drawdown_pct}% "
                "(review the strategy, then run `python -m scalper reset-risk`)"
            )
            return False, f"bot halted: {self.s.halt_reason}"
        if self.daily_loss_hit:
            return False, (
                f"daily loss limit reached ({self.s.realized_today:.2f}); "
                "no new trades until 00:00 UTC"
            )
        if self.s.trades_today >= self.cfg.max_trades_per_day:
            return False, f"max_trades_per_day ({self.cfg.max_trades_per_day}) reached"
        if now_ms < self.s.cooldown_until:
            mins = (self.s.cooldown_until - now_ms) / 60_000
            return False, f"cooling down after {self.cfg.max_consecutive_losses} losses in a row ({mins:.0f} min left)"
        until = self.s.symbol_cooldown_until.get(symbol, 0)
        if now_ms < until:
            return False, "symbol cooldown after last exit"
        if open_positions >= self.cfg.max_open_positions:
            return False, f"max_open_positions ({self.cfg.max_open_positions}) reached"
        return True, ""

    def on_trade_opened(self, symbol: str, now_ms: int) -> None:
        self.on_time(now_ms)
        self.s.trades_today += 1

    def on_trade_closed(
        self, symbol: str, pnl_net: float, now_ms: int, cooldown_ms: int = 0
    ) -> None:
        self.on_time(now_ms)
        self.s.realized_today += pnl_net
        self.s.cum_realized += pnl_net
        self.s.peak_value = max(self.s.peak_value, self.value)
        if pnl_net < 0:
            self.s.loss_streak += 1
            if self.cfg.max_consecutive_losses > 0 and self.s.loss_streak >= self.cfg.max_consecutive_losses:
                self.s.cooldown_until = now_ms + self.cfg.loss_cooldown_minutes * 60_000
                self.s.loss_streak = 0
        else:
            self.s.loss_streak = 0
        if cooldown_ms > 0:
            self.s.symbol_cooldown_until[symbol] = now_ms + cooldown_ms


@dataclass
class EntryDecision:
    ok: bool
    reason: str = ""
    size: SizeResult | None = None


def evaluate_entry(
    signal: Signal,
    price: float,
    equity: float,
    available_balance: float,
    open_positions: int,
    now_ms: int,
    guard: RiskGuard,
    rules: SymbolRules | None,
    risk: RiskConfig,
    costs: CostConfig,
    leverage: int,
) -> EntryDecision:
    """The single gate every entry passes through, in every mode."""
    allowed, reason = guard.can_open(signal.symbol, now_ms, open_positions)
    if not allowed:
        return EntryDecision(False, reason)
    reason = fee_filter(signal, costs, risk)
    if reason:
        return EntryDecision(False, reason)
    if signal.side == LONG and signal.stop >= price:
        return EntryDecision(False, "price already at/below the stop")
    if signal.side != LONG and signal.stop <= price:
        return EntryDecision(False, "price already at/above the stop")
    size = size_position(equity, available_balance, price, signal.stop, rules, risk, costs, leverage)
    if not size.ok:
        return EntryDecision(False, size.reason, size)
    return EntryDecision(True, "", size)
