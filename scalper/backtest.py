"""Event-driven backtester that reuses the live decision code.

Per closed candle, in this order (the same order the live engine uses):
  1. a time-stop exit decided at the previous close fills at this open;
  2. an entry decided at the previous close fills at this open (+ slippage),
     and its targets are re-anchored to that fill;
  3. funding is charged if a funding timestamp falls at this open;
  4. the resting stop / take-profit are checked against this candle
     (pessimistic when both are touched: the stop wins);
  5. the strategy sees the closed candle; open trades are managed
     (breakeven / trailing / time stop); flat symbols may get a new entry
     decision via the exact same `evaluate_entry` gate live trading uses.

Signals are only ever acted on at the NEXT bar's open, so there is no
look-ahead, and every fill pays fees (taker for market orders and stops,
maker for limit take-profits) plus slippage.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from scalper.config import Settings
from scalper.models import LONG, Candle, Signal, SymbolRules
from scalper.risk import RiskGuard, SizeResult, evaluate_entry
from scalper.strategies import Strategy, build_strategy
from scalper.trade import Trade, gross_pnl, manage_trade, new_trade_id, simulate_exit, targets_from_fill

FUNDING_INTERVAL_MS = 8 * 3_600_000


@dataclass
class ClosedTrade:
    trade_id: str
    symbol: str
    side: str
    strategy: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    qty: float
    initial_stop: float
    take_profit: float
    exit_reason: str
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float
    r_multiple: float
    bars_held: int
    equity_after: float
    reason: str = ""


@dataclass
class BacktestResult:
    symbol: str
    start_equity: float
    end_equity: float
    start_time: int
    end_time: int
    trades: list[ClosedTrade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    signals: int = 0
    skipped: Counter = field(default_factory=Counter)


def _bucket_reason(reason: str) -> str:
    """Collapse numeric detail so skip reasons can be counted."""
    for key in (
        "daily loss limit", "max_open_positions", "max_trades_per_day", "cooling down",
        "symbol cooldown", "fees would eat the edge", "liquidation", "below exchange minimum",
        "halted", "price already",
    ):
        if key in reason:
            return key
    return reason[:60]


def run_backtest(
    candles: list[Candle],
    settings: Settings,
    symbol: str,
    rules: SymbolRules | None = None,
    starting_equity: float | None = None,
    strategy: Strategy | None = None,
) -> BacktestResult:
    costs = settings.costs
    mgmt = settings.management
    risk_cfg = settings.risk
    leverage = settings.execution.leverage
    tp_is_limit = settings.execution.take_profit_order == "limit"
    slip = costs.slippage
    interval = settings.interval_ms
    cooldown_ms = mgmt.cooldown_bars_after_exit * interval

    strat = strategy or build_strategy(settings, symbol)
    equity = float(starting_equity if starting_equity is not None else settings.backtest.starting_balance)
    result = BacktestResult(
        symbol=symbol,
        start_equity=equity,
        end_equity=equity,
        start_time=candles[0].open_time if candles else 0,
        end_time=candles[-1].close_time if candles else 0,
    )
    if not candles:
        return result

    guard = RiskGuard(risk_cfg)
    guard.start(equity, candles[0].open_time)

    trade: Trade | None = None
    pending: Signal | None = None
    pending_size: SizeResult | None = None
    pending_exit = ""

    def close_trade(price: float, reason: str, maker: bool, t: int) -> None:
        nonlocal trade, equity
        assert trade is not None
        fee = trade.qty * price * (costs.maker_fee if maker else costs.taker_fee)
        g = gross_pnl(trade.side, trade.entry_price, price, trade.qty)
        equity += g - fee
        net = g - fee - trade.entry_fee - trade.funding_paid
        risk_usd = trade.qty * trade.risk_per_unit
        guard.on_trade_closed(symbol, net, t, cooldown_ms)
        result.trades.append(
            ClosedTrade(
                trade_id=trade.trade_id,
                symbol=symbol,
                side=trade.side,
                strategy=trade.strategy,
                entry_time=trade.opened_at,
                exit_time=t,
                entry_price=trade.entry_price,
                exit_price=price,
                qty=trade.qty,
                initial_stop=trade.initial_stop,
                take_profit=trade.take_profit,
                exit_reason=reason,
                gross_pnl=g,
                fees=trade.entry_fee + fee,
                funding=trade.funding_paid,
                net_pnl=net,
                r_multiple=net / risk_usd if risk_usd > 0 else 0.0,
                bars_held=trade.bars_held,
                equity_after=equity,
                reason=trade.reason,
            )
        )
        trade = None

    for c in candles:
        guard.on_time(c.open_time)

        # 1) market exit decided at the previous close
        if trade is not None and pending_exit:
            px = c.open * (1 - slip) if trade.side == LONG else c.open * (1 + slip)
            close_trade(px, pending_exit, False, c.open_time)
        pending_exit = ""

        # 2) entry decided at the previous close
        if pending is not None and trade is None:
            fill = c.open * (1 + slip) if pending.side == LONG else c.open * (1 - slip)
            targets = targets_from_fill(pending, fill)
            if targets is None:
                result.skipped["opened past stop/target"] += 1
            else:
                stop, tp = targets
                qty = pending_size.qty
                fee = qty * fill * costs.taker_fee
                equity -= fee
                trade = Trade(
                    trade_id=new_trade_id(),
                    symbol=symbol,
                    side=pending.side,
                    strategy=pending.strategy,
                    qty=qty,
                    entry_price=fill,
                    stop=stop,
                    initial_stop=stop,
                    take_profit=tp,
                    opened_at=c.open_time,
                    entry_fee=fee,
                    reason=pending.reason,
                )
                guard.on_trade_opened(symbol, c.open_time)
        pending, pending_size = None, None

        # 3) funding at this open (8h boundaries), always charged as a cost
        if trade is not None and c.open_time % FUNDING_INTERVAL_MS == 0 and trade.opened_at < c.open_time:
            cost = trade.qty * c.open * costs.funding_bps_per_8h / 10_000.0
            trade.funding_paid += cost
            equity -= cost

        # 4) resting stop / take-profit
        if trade is not None:
            ex = simulate_exit(trade, c, slip, tp_is_limit)
            if ex is not None:
                close_trade(ex.price, ex.reason, ex.maker, c.close_time)

        # 5) strategy + management / new entries at the close
        sig = strat.on_candle(c)
        if trade is not None:
            trade.observe(c)
            act = manage_trade(trade, c, strat.atr, mgmt, costs.round_trip_cost)
            if act.exit_reason:
                pending_exit = act.exit_reason
            elif act.new_stop is not None:
                trade.stop = act.new_stop
                trade.stop_kind = act.new_stop_kind
        elif sig is not None:
            result.signals += 1
            decision = evaluate_entry(
                sig, c.close, equity, equity, 0, c.close_time + 1,
                guard, rules, risk_cfg, costs, leverage,
            )
            if decision.ok:
                pending, pending_size = sig, decision.size
            else:
                result.skipped[_bucket_reason(decision.reason)] += 1

        mtm = equity + (trade.unrealized_pnl(c.close) if trade is not None else 0.0)
        result.equity_curve.append((c.close_time, mtm))

    if trade is not None:
        last = candles[-1]
        px = last.close * (1 - slip) if trade.side == LONG else last.close * (1 + slip)
        close_trade(px, "END", False, last.close_time)
        result.equity_curve[-1] = (last.close_time, equity)

    result.end_equity = equity
    return result


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def _max_drawdown(curve: list[tuple[int, float]]) -> float:
    peak = -math.inf
    mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak)
    return mdd * 100.0


def _daily_returns(curve: list[tuple[int, float]]) -> list[float]:
    by_day: dict[str, float] = {}
    for t, eq in curve:
        by_day[datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")] = eq
    values = list(by_day.values())
    return [(b - a) / a for a, b in zip(values, values[1:]) if a > 0]


def compute_stats(
    trades: list[ClosedTrade],
    curve: list[tuple[int, float]],
    start_equity: float,
) -> dict:
    n = len(trades)
    end_equity = curve[-1][1] if curve else start_equity
    days = (curve[-1][0] - curve[0][0]) / 86_400_000 if len(curve) > 1 else 0.0
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)

    streak = worst_streak = 0
    for t in trades:
        streak = streak + 1 if t.net_pnl <= 0 else 0
        worst_streak = max(worst_streak, streak)

    rets = _daily_returns(curve)
    sharpe = 0.0
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = mean / sd * math.sqrt(365) if sd > 0 else 0.0

    return {
        "trades": n,
        "win_rate_pct": len(wins) / n * 100 if n else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
        "net_profit": end_equity - start_equity,
        "return_pct": (end_equity - start_equity) / start_equity * 100 if start_equity else 0.0,
        "max_drawdown_pct": _max_drawdown(curve),
        "expectancy": sum(t.net_pnl for t in trades) / n if n else 0.0,
        "avg_r": sum(t.r_multiple for t in trades) / n if n else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "fees": sum(t.fees for t in trades),
        "funding": sum(t.funding for t in trades),
        "gross_pnl": sum(t.gross_pnl for t in trades),
        "sharpe": sharpe,
        "max_losing_streak": worst_streak,
        "trades_per_day": n / days if days > 0 else 0.0,
        "avg_bars_held": sum(t.bars_held for t in trades) / n if n else 0.0,
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "longs": sum(1 for t in trades if t.side == LONG),
        "shorts": sum(1 for t in trades if t.side != LONG),
        "days": days,
        "start_equity": start_equity,
        "end_equity": end_equity,
    }


def split_stats(result: BacktestResult, oos_fraction: float) -> dict[str, dict]:
    """Stats for the whole run, the in-sample part and the out-of-sample tail."""
    out = {"all": compute_stats(result.trades, result.equity_curve, result.start_equity)}
    if not (0 < oos_fraction < 1) or len(result.equity_curve) < 2:
        return out
    t0, t1 = result.equity_curve[0][0], result.equity_curve[-1][0]
    split_t = t0 + (t1 - t0) * (1 - oos_fraction)
    is_curve = [p for p in result.equity_curve if p[0] < split_t]
    oos_curve = [p for p in result.equity_curve if p[0] >= split_t]
    if is_curve:
        out["in_sample"] = compute_stats(
            [t for t in result.trades if t.entry_time < split_t], is_curve, is_curve[0][1]
        )
    if oos_curve:
        out["out_of_sample"] = compute_stats(
            [t for t in result.trades if t.entry_time >= split_t], oos_curve, oos_curve[0][1]
        )
    return out
