"""Backtester for the EMA-cross strategy — mandatory before running the bot.

Simulation rules (mirror the live bot):
  * A signal is read at the CLOSE of a candle and executed at the OPEN of the
    next candle (the live bot can only act once the candle has closed).
  * LONG signal: close a SHORT if one is open, then open LONG. SHORT signal:
    close a LONG if one is open, then open SHORT. Already on the signalled
    side -> keep the position. No cross -> do nothing.
  * TP / trailing / optional stop-loss are checked inside every candle using
    `ExitTracker.on_candle`. Only OHLC is known, so with
    backtest.intrabar = "conservative" (default) a trailing stop that arms
    inside a candle is assumed to be hit straight away at the minimum
    locked-in profit (TP - trailing); "ohlc" follows the assumed
    open->low->high->close / open->high->low->close path instead.
  * Isolated-margin liquidation is modelled; a liquidation loses the whole
    margin of that position.
  * Taker fees and slippage are charged on every entry and exit. Funding
    payments are NOT modelled.

Usage:
    python -m hlbot.backtest                    # coin/params from config, data from Hyperliquid
    python -m hlbot.backtest --coin ETH --days 60
    python -m hlbot.backtest --csv data/BTCUSDT-30m.csv
    python -m hlbot.backtest --exit-mode fixed
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hlbot.config import BacktestConfig, StrategyConfig, TradeConfig
from hlbot.indicators import ema
from hlbot.position import ExitEvent, ExitTracker
from hlbot.strategy import LONG, Candle, closed_candles, cross_at, interval_ms


def liquidation_price(side: str, entry: float, leverage: int, mmr: float) -> float:
    """Approximate Hyperliquid isolated-margin liquidation price."""
    move_long = (1.0 / leverage - mmr) / (1.0 - mmr)
    move_short = (1.0 / leverage - mmr) / (1.0 + mmr)
    return entry * (1 - move_long) if side == LONG else entry * (1 + move_short)


def resolve_maintenance_margin(settings, info) -> None:
    """Fill backtest.maintenance_margin_rate when it is "auto" (None):
    Hyperliquid's maintenance margin is half the initial margin at the coin's
    max leverage. Also rejects a leverage above the coin's maximum. If the
    exchange can't be reached, falls back to assuming max leverage == the
    configured leverage (the closest, most conservative liquidation)."""
    bt, trade, coin = settings.backtest, settings.trade, settings.strategy.coin
    try:
        max_lev = info.max_leverage(coin)
    except KeyError:
        raise
    except Exception as e:
        if bt.maintenance_margin_rate is None:
            bt.maintenance_margin_rate = 1.0 / (2 * trade.leverage)
            print(f"[WARN] Could not read {coin} max leverage ({e}); assuming maintenance margin "
                  f"{bt.maintenance_margin_rate:.2%}")
        return
    if trade.leverage > max_lev:
        raise ValueError(f"{coin} max leverage on Hyperliquid is {max_lev}x, config asks for {trade.leverage}x")
    if bt.maintenance_margin_rate is None:
        bt.maintenance_margin_rate = 1.0 / (2 * max_lev)


def _mmr(trade: TradeConfig, bt: BacktestConfig) -> float:
    return bt.maintenance_margin_rate if bt.maintenance_margin_rate is not None else 1.0 / (2 * trade.leverage)


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


@dataclass
class Trade:
    side: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    exit_reason: str
    size: float
    margin_usd: float
    fees_usd: float
    pnl_usd: float
    roe_pct: float  # pnl as % of margin
    max_adverse_pct: float  # worst price move against the position, %


@dataclass
class BacktestResult:
    coin: str
    interval: str
    start: str
    end: str
    candles: int
    params: dict
    initial_equity_usd: float
    trades: list[Trade] = field(default_factory=list)
    max_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0

    @property
    def net_pnl_usd(self) -> float:
        return sum(t.pnl_usd for t in self.trades)

    @property
    def final_equity_usd(self) -> float:
        return self.initial_equity_usd + self.net_pnl_usd

    def summary(self) -> dict:
        wins = [t for t in self.trades if t.pnl_usd > 0]
        losses = [t for t in self.trades if t.pnl_usd <= 0]
        gross_win = sum(t.pnl_usd for t in wins)
        gross_loss = -sum(t.pnl_usd for t in losses)
        reasons: dict[str, int] = {}
        for t in self.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        n = len(self.trades)
        return {
            "coin": self.coin,
            "interval": self.interval,
            "period": f"{self.start} -> {self.end} UTC",
            "candles": self.candles,
            "trades": n,
            "long_trades": sum(1 for t in self.trades if t.side == LONG),
            "short_trades": sum(1 for t in self.trades if t.side != LONG),
            "win_rate_pct": round(100.0 * len(wins) / n, 2) if n else 0.0,
            "net_pnl_usd": round(self.net_pnl_usd, 2),
            "return_pct": round(100.0 * self.net_pnl_usd / self.initial_equity_usd, 2),
            "initial_equity_usd": round(self.initial_equity_usd, 2),
            "final_equity_usd": round(self.final_equity_usd, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "avg_roe_pct": round(sum(t.roe_pct for t in self.trades) / n, 2) if n else 0.0,
            "best_roe_pct": round(max((t.roe_pct for t in self.trades), default=0.0), 2),
            "worst_roe_pct": round(min((t.roe_pct for t in self.trades), default=0.0), 2),
            "max_adverse_move_pct": round(max((t.max_adverse_pct for t in self.trades), default=0.0), 2),
            "fees_usd": round(sum(t.fees_usd for t in self.trades), 2),
            "max_drawdown_usd": round(self.max_drawdown_usd, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "liquidations": reasons.get("LIQUIDATION", 0),
            "exit_reasons": reasons,
            "params": self.params,
        }


@dataclass
class _OpenPosition:
    tracker: ExitTracker
    size: float
    entry_fill: float
    entry_time: int
    entry_fee: float


def run_backtest(
    candles: Sequence[Candle],
    strategy: StrategyConfig,
    trade: TradeConfig,
    bt: BacktestConfig,
) -> BacktestResult:
    if len(candles) < strategy.ema_slow + 2:
        raise ValueError(
            f"Need at least {strategy.ema_slow + 2} closed candles to backtest, got {len(candles)}"
        )

    closes = [c.c for c in candles]
    fast = ema(closes, strategy.ema_fast)
    slow = ema(closes, strategy.ema_slow)
    span = interval_ms(strategy.interval)

    result = BacktestResult(
        coin=strategy.coin,
        interval=strategy.interval,
        start=_fmt_ts(candles[0].t),
        end=_fmt_ts(candles[-1].t + span),
        candles=len(candles),
        params={
            "ema_fast": strategy.ema_fast,
            "ema_slow": strategy.ema_slow,
            "leverage": trade.leverage,
            "margin_usd": trade.margin_usd,
            "take_profit_pct": trade.take_profit_pct,
            "trailing_pct": trade.trailing_pct,
            "pct_basis": trade.pct_basis,
            "tp_price_pct": trade.tp_price_pct,
            "trailing_price_pct": trade.trailing_price_pct,
            "exit_mode": trade.exit_mode,
            "stop_loss_pct": trade.stop_loss_pct,
            "intrabar": bt.intrabar,
            "taker_fee": bt.taker_fee,
            "slippage": bt.slippage,
            "maintenance_margin_rate": _mmr(trade, bt),
            "liquidation_move_pct": round(
                100.0 * (1 - liquidation_price(LONG, 1.0, trade.leverage, _mmr(trade, bt))), 2
            ),
        },
        initial_equity_usd=bt.initial_equity_usd,
    )

    pos: Optional[_OpenPosition] = None
    pending: Optional[str] = None
    realized = 0.0
    peak_equity = bt.initial_equity_usd

    def open_position(side: str, price: float, ts: int) -> _OpenPosition:
        d = 1 if side == LONG else -1
        fill = price * (1 + d * bt.slippage)
        size = trade.notional_usd / fill
        tracker = ExitTracker(
            side=side,
            entry_price=fill,
            tp_pct=trade.tp_price_pct,
            trailing_pct=trade.trailing_price_pct,
            mode=trade.exit_mode,
            stop_loss_pct=trade.stop_loss_price_pct,
            liquidation_price=liquidation_price(side, fill, trade.leverage, _mmr(trade, bt)),
        )
        return _OpenPosition(tracker, size, fill, ts, trade.notional_usd * bt.taker_fee)

    def close_position(p: _OpenPosition, price: float, ts: int, reason: str) -> float:
        tr = p.tracker
        d = tr.direction
        if reason == "LIQUIDATION":
            exit_fill = price
            exit_fee = 0.0
            pnl = -trade.margin_usd - p.entry_fee
        else:
            exit_fill = price * (1 - d * bt.slippage)
            exit_fee = exit_fill * p.size * bt.taker_fee
            pnl = d * (exit_fill - p.entry_fill) * p.size - p.entry_fee - exit_fee
        result.trades.append(
            Trade(
                side=tr.side,
                entry_time=_fmt_ts(p.entry_time),
                entry_price=round(p.entry_fill, 6),
                exit_time=_fmt_ts(ts),
                exit_price=round(exit_fill, 6),
                exit_reason=reason,
                size=round(p.size, 8),
                margin_usd=round(trade.margin_usd, 2),
                fees_usd=round(p.entry_fee + exit_fee, 4),
                pnl_usd=round(pnl, 4),
                roe_pct=round(100.0 * pnl / trade.margin_usd, 2),
                max_adverse_pct=round(100.0 * tr.adverse_excursion_pct(), 2),
            )
        )
        return pnl

    for i, candle in enumerate(candles):
        # 1) act on the signal produced by the previous candle's close
        if pending is not None:
            if pos is not None and pos.tracker.side != pending:
                realized += close_position(pos, candle.o, candle.t, "REVERSE_SIGNAL")
                pos = None
            if pos is None:
                pos = open_position(pending, candle.o, candle.t)
            pending = None

        # 2) TP / trailing / stops inside this candle
        if pos is not None:
            tr = pos.tracker
            event = tr.on_candle(candle.o, candle.h, candle.l, candle.c)
            if (
                bt.intrabar == "conservative"
                and tr.mode == "trailing"
                and tr.trailing_active
                and (event is None or event.reason == "TRAILING_STOP")
            ):
                lock = tr.tp_price * (1 - tr.direction * tr.trailing_pct)
                event = ExitEvent(lock, "TRAILING_STOP")
            if event is not None:
                realized += close_position(pos, event.price, candle.t + span, event.reason)
                pos = None

        # 3) mark-to-market equity for drawdown
        unrealized = 0.0
        if pos is not None:
            unrealized = pos.tracker.direction * (candle.c - pos.entry_fill) * pos.size - pos.entry_fee
            unrealized = max(unrealized, -trade.margin_usd - pos.entry_fee)
        equity = bt.initial_equity_usd + realized + unrealized
        peak_equity = max(peak_equity, equity)
        dd = peak_equity - equity
        if dd > result.max_drawdown_usd:
            result.max_drawdown_usd = dd
            result.max_drawdown_pct = 100.0 * dd / peak_equity if peak_equity > 0 else 0.0

        # 4) the candle has closed: check for an EMA cross
        signal = cross_at(fast, slow, i)
        if signal is not None:
            pending = signal

    if pos is not None:
        last = candles[-1]
        close_position(pos, last.c, last.t + span, "END_OF_DATA")

    return result


def print_summary(summary: dict, file=sys.stdout) -> None:
    p = summary["params"]
    lines = [
        "=" * 64,
        f" BACKTEST {summary['coin']} {summary['interval']} | {summary['period']}",
        "=" * 64,
        f" Strategi      : EMA{p['ema_fast']}/EMA{p['ema_slow']} cross, leverage {p['leverage']}x, "
        f"margin ${p['margin_usd']}/posisi",
        f" Exit          : mode={p['exit_mode']} TP={p['take_profit_pct'] * 100:.2f}% "
        f"trailing={p['trailing_pct'] * 100:.2f}% SL="
        + (f"{p['stop_loss_pct'] * 100:.2f}%" if p["stop_loss_pct"] else "off")
        + f" (dari {p['pct_basis']})",
        f" = gerak harga : TP {p['tp_price_pct'] * 100:.3f}% | trailing {p['trailing_price_pct'] * 100:.3f}% "
        f"| intrabar={p['intrabar']}",
        f" Candle        : {summary['candles']}",
        f" Jumlah trade  : {summary['trades']} (long {summary['long_trades']}, short {summary['short_trades']})",
        f" Win rate      : {summary['win_rate_pct']}%",
        f" Net PnL       : ${summary['net_pnl_usd']} ({summary['return_pct']}% dari modal "
        f"${summary['initial_equity_usd']})",
        f" Profit factor : {summary['profit_factor']}",
        f" ROE/trade     : rata2 {summary['avg_roe_pct']}% | terbaik {summary['best_roe_pct']}% | "
        f"terburuk {summary['worst_roe_pct']}%",
        f" Max drawdown  : ${summary['max_drawdown_usd']} ({summary['max_drawdown_pct']}%)",
        f" Max adverse   : {summary['max_adverse_move_pct']}% gerak harga melawan posisi",
        f" Likuidasi     : {summary['liquidations']} (terjadi bila harga ~{p['liquidation_move_pct']}% "
        f"melawan posisi)",
        f" Biaya (fee)   : ${summary['fees_usd']}",
        f" Exit reasons  : {summary['exit_reasons']}",
        "=" * 64,
    ]
    print("\n".join(lines), file=file)


def save_report(result: BacktestResult, out_dir: str = "data") -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    base = f"hl_backtest_{result.coin}_{result.interval}"
    trades_path = os.path.join(out_dir, base + "_trades.csv")
    report_path = os.path.join(out_dir, base + "_report.json")
    with open(trades_path, "w", newline="", encoding="utf-8") as f:
        fields = list(Trade.__dataclass_fields__)
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in result.trades:
            w.writerow(asdict(t))
    report = result.summary()
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return trades_path, report_path


# Grid for --sweep, in % of margin (divided by leverage for the price move).
SWEEP_TP = (0.05, 0.10, 0.20, 0.30, 0.50)
SWEEP_TRAILING = (0.005, 0.02, 0.05, 0.10)
SWEEP_STOP_LOSS = (None, 0.3)


def sweep(
    candles: Sequence[Candle],
    strategy: StrategyConfig,
    trade: TradeConfig,
    bt: BacktestConfig,
    tps: Sequence[float] = SWEEP_TP,
    trails: Sequence[float] = SWEEP_TRAILING,
    stop_losses: Sequence[Optional[float]] = SWEEP_STOP_LOSS,
) -> list[dict]:
    """Backtest every TP / trailing / stop-loss combination (as % of margin,
    trailing exit mode) under both intrabar models. Sorted by the
    conservative net PnL, best first."""
    rows = []
    for tp in tps:
        for trail in trails:
            if trail >= tp:
                continue
            for sl in stop_losses:
                t = replace(trade, take_profit_pct=tp, trailing_pct=trail, stop_loss_pct=sl,
                            pct_basis="margin", exit_mode="trailing")
                cons = run_backtest(candles, strategy, t, replace(bt, intrabar="conservative")).summary()
                ohlc = run_backtest(candles, strategy, t, replace(bt, intrabar="ohlc")).summary()
                rows.append({
                    "tp_pct": tp, "trailing_pct": trail, "stop_loss_pct": sl,
                    "trades": cons["trades"], "win_rate_pct": cons["win_rate_pct"],
                    "net_pnl_conservative": cons["net_pnl_usd"], "net_pnl_ohlc": ohlc["net_pnl_usd"],
                    "profit_factor": cons["profit_factor"], "max_drawdown_pct": cons["max_drawdown_pct"],
                    "liquidations": cons["liquidations"],
                })
    rows.sort(key=lambda r: r["net_pnl_conservative"], reverse=True)
    return rows


def print_sweep(rows: list[dict], trade: TradeConfig, file=sys.stdout) -> None:
    def pct(v):
        return "off" if v is None else f"{v * 100:g}%"

    print("=" * 92, file=file)
    print(f" SWEEP | leverage {trade.leverage}x, margin ${trade.margin_usd:g} | persen = dari margin | "
          f"urut dari Net konservatif", file=file)
    print("=" * 92, file=file)
    print(f" {'TP':>5} {'Trail':>6} {'SL':>5} {'Trades':>7} {'Win%':>6} {'Net kons.':>11} {'Net ohlc':>11} "
          f"{'PF':>5} {'MaxDD%':>7} {'Likuid':>6}", file=file)
    for r in rows:
        pf = "-" if r["profit_factor"] is None else f"{r['profit_factor']:.2f}"
        print(f" {pct(r['tp_pct']):>5} {pct(r['trailing_pct']):>6} {pct(r['stop_loss_pct']):>5} "
              f"{r['trades']:>7} {r['win_rate_pct']:>6.1f} {r['net_pnl_conservative']:>11.2f} "
              f"{r['net_pnl_ohlc']:>11.2f} {pf:>5} {r['max_drawdown_pct']:>7.1f} {r['liquidations']:>6}", file=file)
    print("=" * 92, file=file)
    print(" Net kons. = trailing dianggap langsung kena setelah aktif (pesimis);"
          " Net ohlc = ikut jalur OHLC (optimis).", file=file)
    print(" Hasil nyata biasanya di antara keduanya. Hati-hati overfitting: pilih setelan yang", file=file)
    print(" tetangganya juga bagus, bukan satu angka terbaik saja.", file=file)


def save_sweep(rows: list[dict], coin: str, interval: str, leverage: int, out_dir: str = "data") -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"hl_sweep_{coin}_{interval}_{leverage}x.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["tp_pct"])
        w.writeheader()
        w.writerows(rows)
    return path


def fetch_history(info, coin: str, interval: str, days: int, now_ms: Optional[int] = None) -> list[Candle]:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    start = now_ms - days * 86_400_000
    return closed_candles(info.candles(coin, interval, start, now_ms), interval, now_ms)


def main(argv: Optional[list[str]] = None) -> int:
    from hlbot.config import load_hl_settings
    from hlbot.data import HyperliquidInfo, load_csv, save_csv

    ap = argparse.ArgumentParser(description="Backtest the Hyperliquid EMA-cross bot")
    ap.add_argument("--config", help="path to hyperliquid.yaml (default: config/hyperliquid.yaml)")
    ap.add_argument("--coin", help="override strategy.coin, e.g. BTC, ETH, SOL")
    ap.add_argument("--days", type=int, help="override backtest.days (Hyperliquid serves max 5000 candles)")
    ap.add_argument("--csv", help="backtest on candles from a CSV file instead of the Hyperliquid API")
    ap.add_argument("--save-candles", help="also save the downloaded candles to this CSV path")
    ap.add_argument("--exit-mode", choices=["trailing", "fixed"], help="override trade.exit_mode")
    ap.add_argument("--stop-loss", type=float, help="override trade.stop_loss_pct, e.g. 0.3 (0 = off)")
    ap.add_argument("--pct-basis", choices=["margin", "price"], help="override trade.pct_basis")
    ap.add_argument("--intrabar", choices=["conservative", "ohlc"], help="override backtest.intrabar")
    ap.add_argument("--tp", type=float, help="override trade.take_profit_pct, e.g. 0.1")
    ap.add_argument("--trailing", type=float, help="override trade.trailing_pct, e.g. 0.02")
    ap.add_argument("--leverage", type=int, help="override trade.leverage, e.g. 5")
    ap.add_argument("--sweep", action="store_true",
                    help="compare many TP / trailing / stop-loss combinations (%% of margin) in one run")
    ap.add_argument("--out-dir", default="data", help="where to write the report (default: data/)")
    args = ap.parse_args(argv)

    settings = load_hl_settings(config_path=args.config)
    if args.coin:
        settings.strategy.coin = args.coin.upper()
    if args.days:
        settings.backtest.days = args.days
    if args.exit_mode:
        settings.trade.exit_mode = args.exit_mode
    if args.stop_loss is not None:
        settings.trade.stop_loss_pct = args.stop_loss or None
    if args.pct_basis:
        settings.trade.pct_basis = args.pct_basis
    if args.intrabar:
        settings.backtest.intrabar = args.intrabar
    if args.tp:
        settings.trade.take_profit_pct = args.tp
    if args.trailing:
        settings.trade.trailing_pct = args.trailing
    if args.leverage:
        settings.trade.leverage = args.leverage

    info = HyperliquidInfo(settings.connection.base_url)
    try:
        resolve_maintenance_margin(settings, info)
    except (KeyError, ValueError) as e:
        print(f"[FAIL] {e}")
        return 1

    if args.csv:
        candles = load_csv(args.csv)
        print(f"Loaded {len(candles)} candles from {args.csv}")
        span = interval_ms(settings.strategy.interval)
        if len(candles) > 1 and candles[1].t - candles[0].t != span:
            print(
                f"[FAIL] CSV candles are {(candles[1].t - candles[0].t) // 60000} min apart but "
                f"strategy.interval is {settings.strategy.interval}"
            )
            return 1
    else:
        print(
            f"Downloading {settings.backtest.days} days of {settings.strategy.coin} "
            f"{settings.strategy.interval} candles from {settings.connection.base_url} ..."
        )
        candles = fetch_history(info, settings.strategy.coin, settings.strategy.interval, settings.backtest.days)
        print(f"Got {len(candles)} closed candles")
        if args.save_candles:
            save_csv(candles, args.save_candles)

    if args.sweep:
        rows = sweep(candles, settings.strategy, settings.trade, settings.backtest)
        print_sweep(rows, settings.trade)
        path = save_sweep(rows, settings.strategy.coin, settings.strategy.interval, settings.trade.leverage,
                          args.out_dir)
        print(f"Saved : {path}")
        return 0

    result = run_backtest(candles, settings.strategy, settings.trade, settings.backtest)
    print_summary(result.summary())
    trades_path, report_path = save_report(result, args.out_dir)
    print(f"Trades : {trades_path}\nReport : {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
