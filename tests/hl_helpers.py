"""Shared helpers for the hlbot tests (synthetic candles, no network)."""
from hlbot.config import BacktestConfig, StrategyConfig, TradeConfig
from hlbot.strategy import Candle

SPAN_30M = 1_800_000
T0 = 1_700_000_000_000 // SPAN_30M * SPAN_30M


def candles_from_closes(closes, start_t=T0, wick=0.0):
    """Each candle opens at the previous close; optional symmetric wicks."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = max(o, c) * (1 + wick)
        low = min(o, c) * (1 - wick)
        out.append(Candle(t=start_t + i * SPAN_30M, o=o, h=h, l=low, c=c))
        prev = c
    return out


def down_then_up(n_down=40, n_up=40, start=100.0, step=0.5):
    closes = [start - step * i for i in range(n_down)]
    bottom = closes[-1]
    closes += [bottom + step * (i + 1) for i in range(n_up)]
    return closes


def frictionless(intrabar="ohlc"):
    """No fees/slippage; OHLC intrabar path so expected exit prices are easy to compute."""
    return BacktestConfig(initial_equity_usd=1000.0, taker_fee=0.0, slippage=0.0, intrabar=intrabar)


def strategy_cfg():
    return StrategyConfig(coin="BTC", interval="30m", ema_fast=9, ema_slow=21)


def trade_cfg(**kw):
    """Price-based percentages by default (easy arithmetic); tests of the
    margin basis pass pct_basis="margin" explicitly."""
    base = dict(
        leverage=10, margin_usd=100.0, take_profit_pct=0.02, trailing_pct=0.005, exit_mode="trailing",
        pct_basis="price",
    )
    base.update(kw)
    return TradeConfig(**base)
