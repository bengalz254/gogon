"""Live / paper loop for the Hyperliquid EMA-cross bot.

    python -m hlbot.main

Startup:
  1. Runs the MANDATORY backtest on recent Hyperliquid candles with the exact
     same parameters. If the backtest cannot run, the bot does not start. In
     live mode it also refuses to start if the backtest lost money (unless
     backtest.block_live_if_unprofitable is false).
  2. Sets leverage / margin mode, restores saved state, reconciles with the
     exchange position.

Loop (every trade.poll_seconds):
  * After each 30m candle has CLOSED (+ a few seconds for the API to catch
    up), fetch candles, drop the still-forming one and check EMA fast/slow:
      cross up   -> close SHORT if open, open LONG
      cross down -> close LONG if open, open SHORT
      no cross   -> do nothing
  * While a position is open, feed the current price to the ExitTracker
    (TP 5% -> trailing 0.5% of margin) and close the position when it fires.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import signal as signal_module
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.logger import setup_logging
from hlbot.backtest import fetch_history, print_summary, run_backtest, save_report
from hlbot.config import HLSettings, load_hl_settings
from hlbot.data import HyperliquidInfo
from hlbot.position import ExitTracker
from hlbot.strategy import closed_candles, interval_ms, latest_signal, trend

logger = logging.getLogger("hlbot")

# Candles fetched each time a candle closes; plenty for EMA21 to converge.
LOOKBACK_CANDLES = 300

JOURNAL_FIELDS = ["timestamp", "mode", "coin", "action", "side", "price", "size", "reason", "pnl_usd"]


class TradeLog:
    def __init__(self, path: str = "data/hl_trades.csv"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(JOURNAL_FIELDS)

    def record(self, mode: str, coin: str, action: str, side: str, price: float, size: float, reason: str,
               pnl_usd: Optional[float] = None) -> None:
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                [datetime.now(timezone.utc).isoformat(), mode, coin, action, side, f"{price:.6f}", f"{size:.8f}",
                 reason, "" if pnl_usd is None else f"{pnl_usd:.4f}"]
            )


class LiveBot:
    def __init__(
        self,
        settings: HLSettings,
        info,
        broker,
        journal: TradeLog,
        state_path: str,
        live: bool = False,
        clock: Callable[[], float] = time.time,
    ):
        self.s = settings
        self.info = info
        self.broker = broker
        self.journal = journal
        self.state_path = state_path
        self.live = live
        self.clock = clock
        self.mode = "live" if live else "paper"
        self.span = interval_ms(settings.strategy.interval)
        self.tracker: Optional[ExitTracker] = None
        self.position_size = 0.0
        self.last_candle_t: Optional[int] = None

    # -- state -------------------------------------------------------------
    def load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        with open(self.state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.last_candle_t = data.get("last_candle_t")
        self.position_size = float(data.get("position_size", 0.0))
        tr = data.get("tracker")
        self.tracker = ExitTracker.from_dict(tr) if tr else None
        if hasattr(self.broker, "load_dict"):
            self.broker.load_dict(data.get("broker", {}))
        logger.info("Restored state from %s", self.state_path)

    def save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        data = {
            "last_candle_t": self.last_candle_t,
            "position_size": self.position_size,
            "tracker": self.tracker.to_dict() if self.tracker else None,
            "broker": self.broker.to_dict() if hasattr(self.broker, "to_dict") else {},
        }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.state_path)

    def _new_tracker(self, side: str, entry_price: float) -> ExitTracker:
        t = self.s.trade
        return ExitTracker(
            side=side,
            entry_price=entry_price,
            tp_pct=t.tp_price_pct,
            trailing_pct=t.trailing_price_pct,
            mode=t.exit_mode,
            stop_loss_pct=t.stop_loss_price_pct,
        )

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.load_state()
        now_ms = int(self.clock() * 1000)
        if self.last_candle_t is None:
            # Fresh start: only act on crosses of candles that close from now on.
            self.last_candle_t = now_ms // self.span * self.span - self.span
            logger.info("Fresh start — waiting for the next %s candle to close.", self.s.strategy.interval)
        self.reconcile()
        self.save_state()

    def reconcile(self):
        """Make the in-memory tracker match the real position."""
        pos = self.broker.get_position()
        if pos is None and self.tracker is not None:
            logger.warning(
                "%s position no longer open (exchange stop, liquidation or manual close) — clearing tracker.",
                self.tracker.side,
            )
            self.broker.cancel_stop()
            self.tracker = None
            self.position_size = 0.0
            self.save_state()
        elif pos is not None and (self.tracker is None or self.tracker.side != pos.side):
            logger.warning("Adopting existing %s position: size=%s entry=%s", pos.side, pos.size, pos.entry_price)
            self.tracker = self._new_tracker(pos.side, pos.entry_price)
            self.position_size = pos.size
            self.save_state()
        elif pos is not None:
            self.position_size = pos.size
        return pos

    def due_candle(self, now_ms: int) -> Optional[int]:
        """Open time of the latest closed candle, if it is ready and unprocessed."""
        delay_ms = int(self.s.trade.candle_close_delay_seconds * 1000)
        latest_closed_t = (now_ms - delay_ms) // self.span * self.span - self.span
        if self.last_candle_t is not None and latest_closed_t <= self.last_candle_t:
            return None
        return latest_closed_t

    def on_candle_close(self, expected_t: int, now_ms: int) -> bool:
        """Evaluate the EMA cross on the just-closed candle. Returns False if
        the candle data is not available yet (retry on the next tick)."""
        st = self.s.strategy
        candles = closed_candles(
            self.info.recent_candles(st.coin, st.interval, LOOKBACK_CANDLES, now_ms), st.interval, now_ms
        )
        if not candles or candles[-1].t < expected_t:
            logger.debug("Candle %s not available yet, retrying", expected_t)
            return False
        if len(candles) < st.ema_slow + 2:
            logger.warning("Only %d closed candles available — not enough for EMA%d", len(candles), st.ema_slow)
            return False

        closes = [c.c for c in candles]
        sig = latest_signal(closes, st.ema_fast, st.ema_slow)
        self.last_candle_t = candles[-1].t
        candle_time = datetime.fromtimestamp(candles[-1].t / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

        if sig is None:
            logger.info(
                "Candle %s closed @ %.6g — no EMA%d/EMA%d cross, idle (trend=%s, position=%s)",
                candle_time, closes[-1], st.ema_fast, st.ema_slow,
                trend(closes, st.ema_fast, st.ema_slow), self.tracker.side if self.tracker else "none",
            )
            self.save_state()
            return True

        logger.info("Candle %s closed @ %.6g — EMA%d crossed %s EMA%d -> %s",
                    candle_time, closes[-1], st.ema_fast, "above" if sig == "LONG" else "below", st.ema_slow, sig)
        pos = self.reconcile()
        if pos is not None and pos.side == sig:
            logger.info("Already %s — holding.", sig)
        else:
            if pos is not None:
                self._close("REVERSE_SIGNAL")
            if self.broker.get_position() is None:
                self._open(sig)
            else:
                logger.error("Could not close the opposite position — NOT opening %s.", sig)
        self.save_state()
        return True

    def _open(self, side: str) -> None:
        try:
            fill = self.broker.open(side, self.s.trade.notional_usd)
        except Exception:
            logger.exception("Failed to open %s", side)
            return
        self.tracker = self._new_tracker(side, fill.price)
        self.position_size = fill.size
        t = self.tracker
        logger.info(
            "[%s] OPEN %s %s size=%s @ %.6g | TP at %.6g, then trailing %.3f%% of price",
            self.mode.upper(), side, self.s.strategy.coin, fill.size, fill.price, t.tp_price,
            t.trailing_pct * 100,
        )
        self.journal.record(self.mode, self.s.strategy.coin, "OPEN", side, fill.price, fill.size,
                            f"EMA{self.s.strategy.ema_fast}/{self.s.strategy.ema_slow} cross")
        self.save_state()

    def _close(self, reason: str) -> None:
        tracker = self.tracker
        try:
            fill = self.broker.close()
        except Exception:
            logger.exception("Failed to close position (%s)", reason)
            return
        if fill is not None and tracker is not None:
            pnl = tracker.direction * (fill.price - tracker.entry_price) * fill.size
            logger.info("[%s] CLOSE %s %s @ %.6g (%s) | pnl before fees ~$%.2f",
                        self.mode.upper(), tracker.side, self.s.strategy.coin, fill.price, reason, pnl)
            self.journal.record(self.mode, self.s.strategy.coin, "CLOSE", tracker.side, fill.price, fill.size,
                                reason, pnl)
        self.tracker = None
        self.position_size = 0.0
        self.save_state()

    def check_exits(self) -> None:
        if self.tracker is None:
            return
        if self.live and self.reconcile() is None:
            return
        price = self.broker.mid_price()
        event = self.tracker.on_price(price)
        if event is not None:
            logger.info("%s hit (level %.6g, price %.6g)", event.reason, event.price, price)
            self._close(event.reason)
            return
        if self.live and self.s.trade.exchange_stop_order:
            stop = self.tracker.protective_stop()
            if stop is not None and stop.reason != "LIQUIDATION":
                self.broker.sync_stop(self.tracker.side, self.position_size, stop.price)
        self.save_state()

    def tick(self) -> None:
        now_ms = int(self.clock() * 1000)
        due = self.due_candle(now_ms)
        if due is not None:
            self.on_candle_close(due, now_ms)
        self.check_exits()


# --------------------------------------------------------------------------
_stop = False


def _request_stop(signum, frame):
    global _stop
    _stop = True


def mandatory_backtest(settings: HLSettings, info) -> Optional[dict]:
    st = settings.strategy
    logger.info("Running mandatory backtest: %s %s, last %d days ...", st.coin, st.interval, settings.backtest.days)
    candles = fetch_history(info, st.coin, st.interval, settings.backtest.days)
    result = run_backtest(candles, st, settings.trade, settings.backtest)
    summary = result.summary()
    print_summary(summary)
    _, report_path = save_report(result)
    logger.info("Backtest report saved to %s", report_path)
    return summary


def run() -> int:
    settings = load_hl_settings()
    setup_logging(name="hlbot", filename="hlbot.log")
    conn = settings.connection
    live = conn.live_trading

    logger.info("Hyperliquid EMA bot | network=%s | live_trading=%s", conn.network, live)
    info = HyperliquidInfo(conn.base_url)

    try:
        summary = mandatory_backtest(settings, info)
    except Exception:
        logger.exception("Mandatory backtest failed — the bot will not start without a backtest.")
        return 1
    if summary["trades"] == 0:
        logger.warning("Backtest produced no trades — check the coin / interval / history length.")
    if live and settings.backtest.block_live_if_unprofitable and summary["net_pnl_usd"] <= 0:
        logger.error(
            "Backtest net PnL is $%.2f (<= 0). Refusing to trade LIVE with these parameters. "
            "Adjust the strategy, or set backtest.block_live_if_unprofitable: false to override.",
            summary["net_pnl_usd"],
        )
        return 1
    if not live and summary["net_pnl_usd"] <= 0:
        logger.warning("Backtest net PnL is $%.2f (<= 0) — continuing in PAPER mode only.", summary["net_pnl_usd"])

    st = settings.strategy
    if live:
        from hlbot.broker import HyperliquidBroker

        logger.warning("*** LIVE TRADING ENABLED *** Real orders with real funds on %s.", conn.network)
        broker = HyperliquidBroker(
            st.coin, conn.secret_key, conn.account_address, conn.base_url, settings.trade.max_slippage,
            # move the exchange stop in steps well below the trailing distance
            stop_order_min_move=min(0.001, settings.trade.trailing_price_pct / 5),
        )
    else:
        from hlbot.broker import PaperBroker

        logger.info("PAPER TRADING mode — no real orders will be sent.")
        broker = PaperBroker(info, st.coin, settings.backtest.taker_fee, settings.backtest.slippage)
    broker.setup(settings.trade.leverage, is_cross=settings.trade.margin_mode == "cross")

    state_path = f"data/hl_state_{st.coin}_{'live' if live else 'paper'}.json"
    bot = LiveBot(settings, info, broker, TradeLog(), state_path, live=live)
    bot.start()

    signal_module.signal(signal_module.SIGINT, _request_stop)
    signal_module.signal(signal_module.SIGTERM, _request_stop)
    tr = settings.trade
    logger.info(
        "Running: %s %s EMA%d/%d, %dx %s, margin $%.2f (notional $%.2f), TP %.2f%% trailing %.2f%% of %s "
        "(= %.3f%% / %.3f%% price move, %s mode)",
        st.coin, st.interval, st.ema_fast, st.ema_slow, tr.leverage, tr.margin_mode, tr.margin_usd,
        tr.notional_usd, tr.take_profit_pct * 100, tr.trailing_pct * 100, tr.pct_basis,
        tr.tp_price_pct * 100, tr.trailing_price_pct * 100, tr.exit_mode,
    )

    while not _stop:
        try:
            bot.tick()
        except Exception:
            logger.exception("Error in main loop; continuing")
        remaining = settings.trade.poll_seconds
        while remaining > 0 and not _stop:
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step

    bot.save_state()
    if bot.tracker is not None:
        logger.warning("Bot stopped with an OPEN %s position — it is NOT closed automatically.", bot.tracker.side)
    logger.info("Bot stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
