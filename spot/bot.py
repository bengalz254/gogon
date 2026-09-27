"""The grid bot's loop: prices in, simulated fills, new orders, state and alerts."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from spot.config import SpotSettings
from spot.grid import BUYING, HOLDING, SELLING, Fill, Grid, auto_range, fmt_amount, fmt_money, plan_grid, slot_net_profit_pct
from spot.market import CANDLE_MS, CLOSE_GRACE_MS, DataError, MarketRules
from spot.records import JsonStore, TradeLog

logger = logging.getLogger("spot.bot")

MODE = "paper"
STATE_VERSION = 1
# Failed polls in a row before Telegram hears about it, and how often it's repeated.
DATA_ALERT_AFTER = 3
ALERT_COOLDOWN_SECONDS = 30 * 60
RANGE_ALERT_COOLDOWN_SECONDS = 12 * 3600
# Candle closes older than this don't trip the stop-loss (they're catch-up after downtime).
STOP_LOOKBACK_MS = 5 * CANDLE_MS


class StartupError(Exception):
    """The grid can't run as configured; the message says what to change."""


def last_closed_candle_ts(now: float) -> int:
    """Opening time (ms) of the newest 1-minute candle that has closed by `now`."""
    return int((now * 1000 - CLOSE_GRACE_MS) // CANDLE_MS) * CANDLE_MS - CANDLE_MS


def write_status(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def write_error_status(path: str, error: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    write_status(
        path,
        {
            "updated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            "updated_ts": now,
            "running": False,
            "mode": MODE,
            "error": error,
        },
    )


class GridBot:
    def __init__(self, settings: SpotSettings, data, notifier, clock=time.time, sleep=time.sleep):
        self.settings = settings
        self.data = data
        self.notifier = notifier
        self.clock = clock
        self.sleep = sleep
        self.store = JsonStore(os.path.join(settings.data_dir, f"state_{MODE}.json"))
        self.trades = TradeLog(os.path.join(settings.data_dir, "trades.csv"))
        self.status_path = os.path.join(settings.data_dir, "status.json")
        self.stop_requested = False
        self.rules: MarketRules | None = None
        self.grid: Grid | None = None
        self.state: dict = {}
        self.bid: float | None = None
        self.ask: float | None = None
        self.failures = 0
        self.last_error = ""

    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    # -- helpers ---------------------------------------------------------------------
    def _money(self, value: float) -> str:
        return fmt_money(value, self.settings.quote)

    def _price(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.bid if self.bid is not None else self.ask

    def _fingerprint(self) -> dict:
        g = self.settings.grid
        return {
            "symbol": self.settings.symbol,
            "lower_price": g.lower_price,
            "upper_price": g.upper_price,
            "range_pct": g.range_pct,
            "levels": g.levels,
            "order_value": g.order_value,
        }

    def _stop_price(self) -> float | None:
        pct = self.settings.risk.stop_loss_pct
        return self.grid.lower * (1 - pct / 100) if pct > 0 else None

    def _hodl_pnl(self) -> float | None:
        """P&L had the starting money bought the coin at the start price and held it."""
        start_price, bid = self.state.get("start_price"), self.bid
        if not start_price or bid is None:
            return None
        costs, balance = self.settings.costs, self.state["start_balance"]
        coins = balance * (1 - costs.cost_rate("BUY")) / start_price
        return coins * bid * (1 - costs.cost_rate("SELL")) - balance

    # -- startup ---------------------------------------------------------------------
    def start(self) -> None:
        """Load the pair's trading rules and the saved grid, or build a new one.

        DataError means try again later; SymbolError and StartupError mean
        the config has to change first.
        """
        self.rules = self.data.rules()
        self.bid, self.ask = self.data.top_of_book()
        price = self._price()
        if price is None:
            raise DataError(f"order book {self.settings.symbol} kosong, belum ada harga")
        unreadable = StartupError(
            f"{self.store.path} rusak atau formatnya tidak dikenali. Reset grid paper: python -m spot.main --reset"
        )
        try:
            saved = self.store.load()
            if saved is not None and (not isinstance(saved, dict) or saved.get("version") != STATE_VERSION):
                raise unreadable
            if saved is not None and saved.get("fingerprint") == self._fingerprint():
                self._restore(saved)
            else:
                self._new_grid(saved, price)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise unreadable from exc
        self._save()

    def _restore(self, saved: dict) -> None:
        self.state = saved
        self.grid = Grid.from_dict(saved["grid"], self.settings.costs)
        net = min(slot_net_profit_pct(s, self.settings.costs) for s in self.grid.slots)
        if net < self.settings.costs.min_net_profit_pct:
            self.notifier.send(
                f"⚠️ Dengan biaya di config sekarang, grid ini hanya untung {net:.2f}% per putaran "
                f"(minimal {self.settings.costs.min_net_profit_pct:.2f}%). Grid tetap jalan; "
                "reset dengan level lebih sedikit kalau ingin memperbaikinya."
            )
        logger.info("Restored the saved grid: %s", self._grid_line())

    def _new_grid(self, saved: dict | None, price: float) -> None:
        costs, g = self.settings.costs, self.settings.grid
        if saved is not None:
            old = Grid.from_dict(saved["grid"], costs)
            if any(s.holds_coins for s in old.slots):
                raise StartupError(
                    "Pengaturan grid di config/spot.yaml berubah, tapi grid lama masih memegang koin. "
                    "Kembalikan pengaturan lama, atau reset grid paper (posisinya dihapus): "
                    "python -m spot.main --reset"
                )
            balance = old.quote  # carries the realized P&L over
        else:
            balance = self.settings.risk.paper_balance
        lower, upper = (g.lower_price, g.upper_price) if g.lower_price > 0 else auto_range(price, g.range_pct)
        plan = plan_grid(lower, upper, g, costs, self.rules, balance)
        if plan.problems:
            raise StartupError("\n".join(plan.problems))
        self.grid = Grid(plan.slots, balance, 0.0, costs)
        now = self.clock()
        if saved is None:
            self.state = {
                "version": STATE_VERSION,
                "symbol": self.settings.symbol,
                "started_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                "start_price": price,
                "start_balance": balance,
                "realized_pnl": 0.0,
                "round_trips": 0,
                "fees": 0.0,
                "taxes": 0.0,
                "volume": 0.0,
                "last_summary_ts": now,
            }
        else:
            self.state = saved
            logger.info("Grid settings changed and no coins were held: rebuilding the grid")
        self.state.update(
            fingerprint=self._fingerprint(),
            stopped=False,
            stop_reason="",
            last_candle_ts=last_closed_candle_ts(now),
        )
        logger.info(
            "New grid: %s | gap %.2f%%, net %.2f%% per round trip after %.2f%% costs, capital %s",
            self._grid_line(orders=False),
            plan.min_gap_pct,
            plan.net_profit_pct,
            plan.round_trip_cost_pct,
            self._money(plan.capital),
        )

    def _grid_line(self, orders: bool = True) -> str:
        g = self.grid
        line = f"{self.settings.symbol} {self._money(g.lower)}–{self._money(g.upper)}, {len(g.slots)} slot"
        if orders:
            line += f", {g.count(BUYING)} buy / {g.count(SELLING) + g.count(HOLDING)} sell orders"
        return line

    # -- one poll ----------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        now_ms = int(now * 1000)
        self.bid, self.ask = self.data.top_of_book()
        candles = self.data.closed_candles(self.state["last_candle_ts"] + CANDLE_MS, now_ms)
        for candle in candles:
            if not self.state["stopped"]:
                for fill in self.grid.apply_candle(candle):
                    self._record(fill)
                # Only recent closes trip the stop-loss: after downtime the bot
                # can only sell at today's price, which the bid check below covers.
                recent = now_ms - candle.ts <= STOP_LOOKBACK_MS
                if recent and self._below_stop(candle.close):
                    self._stop_loss(f"harga penutupan {self._money(candle.close)}", candle.close)
            self.state["last_candle_ts"] = candle.ts
        if not self.state["stopped"] and self.bid is not None and self._below_stop(self.bid):
            self._stop_loss(f"harga bid {self._money(self.bid)}", self.bid)
        if not self.state["stopped"]:
            self.grid.place_orders(self.bid, self.ask, self.rules.tick)
            self._range_alerts()
        self._save()
        self._maybe_summary(now)

    def _below_stop(self, price: float) -> bool:
        stop = self._stop_price()
        return stop is not None and price < stop

    def _stop_loss(self, reason: str, fallback_price: float) -> None:
        price = self.bid if self.bid is not None else fallback_price
        fills = self.grid.liquidate(price)
        for fill in fills:
            self._record(fill, note="stop-loss")
        self.state["stopped"] = True
        self.state["stop_reason"] = f"stop-loss: {reason} di bawah {self._money(self._stop_price())}"
        loss = sum(f.pnl for f in fills)
        logger.warning("STOP-LOSS: %s — sold %d slots, P&L %s", reason, len(fills), self._money(loss))
        self.notifier.send(
            f"⛔ [PAPER] Stop-loss {self.settings.symbol}: {reason} di bawah {self._money(self._stop_price())}.\n"
            f"Semua koin dijual ({len(fills)} slot, P&L {self._money(loss)}) dan grid berhenti.\n"
            "Untuk mulai lagi: hentikan bot, jalankan python -m spot.main --reset, lalu nyalakan lagi."
        )

    def _record(self, fill: Fill, note: str = "") -> None:
        s = self.state
        if fill.side == "SELL":
            s["realized_pnl"] += fill.pnl
            if not fill.taker:
                s["round_trips"] += 1
        s["fees"] += fill.fee
        s["taxes"] += fill.tax
        s["volume"] += fill.value
        self.trades.record(fill, self.settings.symbol, MODE, note)
        base = self.settings.base
        logger.info(
            "[PAPER] %s %s %s @ %s (slot %d)%s",
            fill.side,
            fmt_amount(fill.amount),
            base,
            self._money(fill.price),
            fill.slot + 1,
            f" P&L {self._money(fill.pnl)}" if fill.side == "SELL" else "",
        )
        if not self.settings.notifications.fills or note == "stop-loss":
            return
        if fill.side == "BUY":
            text = (
                f"🟢 [PAPER] Beli {fmt_amount(fill.amount)} {base} @ {self._money(fill.price)} "
                f"(slot {fill.slot + 1}/{len(self.grid.slots)})"
            )
        else:
            text = (
                f"💰 [PAPER] Jual {fmt_amount(fill.amount)} {base} @ {self._money(fill.price)} — "
                f"untung bersih {self._money(fill.pnl)} (biaya+pajak {self._money(fill.fee + fill.tax)})"
            )
        self.notifier.send(text)

    def _range_alerts(self) -> None:
        price = self._price()
        if price is None:
            return
        g, symbol = self.grid, self.settings.symbol
        if price > g.upper:
            self.notifier.send(
                f"⬆️ [PAPER] Harga {symbol} {self._money(price)} di atas range grid "
                f"({self._money(g.lower)}–{self._money(g.upper)}). Bot menunggu harga turun lagi; "
                "kalau lama, reset supaya range mengikuti harga.",
                key="above_range",
                cooldown_s=RANGE_ALERT_COOLDOWN_SECONDS,
            )
        elif price < g.lower:
            stop = self._stop_price()
            stop_text = f"Stop-loss di {self._money(stop)}." if stop else "Stop-loss mati."
            self.notifier.send(
                f"⬇️ [PAPER] Harga {symbol} {self._money(price)} di bawah range grid. Bot memegang "
                f"{fmt_amount(g.base)} {self.settings.base} (unrealized {self._money(g.unrealized(self.bid))}) "
                f"dan menunggu harga naik. {stop_text}",
                key="below_range",
                cooldown_s=RANGE_ALERT_COOLDOWN_SECONDS,
            )

    def _maybe_summary(self, now: float) -> None:
        hours = self.settings.notifications.summary_hours
        if hours <= 0 or now - self.state.get("last_summary_ts", 0.0) < hours * 3600:
            return
        self.state["last_summary_ts"] = now
        self.notifier.send(self.summary_text())

    def summary_text(self) -> str:
        g, s, base = self.grid, self.state, self.settings.base
        equity = g.equity(self.bid)
        total = equity - s["start_balance"]
        lines = [
            f"📊 Ringkasan grid {self.settings.symbol} (PAPER)",
            f"Harga {self._money(self._price() or 0.0)} | range {self._money(g.lower)}–{self._money(g.upper)}",
            f"Putaran selesai: {s['round_trips']} | untung terealisasi {self._money(s['realized_pnl'])}",
            f"Memegang {fmt_amount(g.base)} {base} | unrealized {self._money(g.unrealized(self.bid))}",
            f"Nilai akun {self._money(equity)} ({self._money(total)}, {total / s['start_balance'] * 100:+.2f}% dari modal "
            f"{self._money(s['start_balance'])})",
            f"Sudah bayar biaya {self._money(s['fees'])} + pajak {self._money(s['taxes'])}",
        ]
        hodl = self._hodl_pnl()
        if hodl is not None:
            lines.append(f"Pembanding — kalau modal dibelikan {base} semua sejak awal: {self._money(hodl)}")
        if s.get("stopped"):
            lines.append(f"⛔ Grid berhenti ({s.get('stop_reason')})")
        return "\n".join(lines)

    # -- persistence -------------------------------------------------------------------
    def _save(self, running: bool = True) -> None:
        self.state["grid"] = self.grid.to_dict()
        self.store.save(self.state)
        self._write_status(running)

    def _write_status(self, running: bool = True) -> None:
        now = self.clock()
        payload = {
            "updated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            "updated_ts": now,
            "running": running,
            "mode": MODE,
            "symbol": self.settings.symbol,
            "price": self._price(),
            "bid": self.bid,
            "ask": self.ask,
            "last_error": self.last_error,
            "data_failures": self.failures,
        }
        if self.grid is not None and self.state:
            g, s = self.grid, self.state
            equity = g.equity(self.bid)
            payload.update(
                grid={
                    "lower": g.lower,
                    "upper": g.upper,
                    "slots": len(g.slots),
                    "buy_orders": g.count(BUYING),
                    "sell_orders": g.count(SELLING) + g.count(HOLDING),
                },
                holding=g.base,
                quote_balance=g.quote,
                equity=equity,
                start_balance=s["start_balance"],
                started_at=s.get("started_at"),
                total_pnl=equity - s["start_balance"],
                realized_pnl=s["realized_pnl"],
                unrealized_pnl=g.unrealized(self.bid),
                round_trips=s["round_trips"],
                fees=s["fees"],
                taxes=s["taxes"],
                hodl_pnl=self._hodl_pnl(),
                stop_price=self._stop_price(),
                stopped=s.get("stopped", False),
                stop_reason=s.get("stop_reason", ""),
            )
        write_status(self.status_path, payload)

    # -- lifecycle ---------------------------------------------------------------------
    def _sleep_until(self, deadline: float) -> None:
        while not self.stop_requested:
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self.sleep(min(1.0, remaining))

    def idle_until_stopped(self) -> None:
        """Wait for Ctrl+C / `docker compose stop` without doing anything."""
        while not self.stop_requested:
            self.sleep(1.0)

    def _start_with_retries(self) -> bool:
        attempt = 0
        while not self.stop_requested:
            try:
                self.start()
                return True
            except DataError as exc:
                attempt += 1
                self.last_error = str(exc)
                logger.warning("Could not start (attempt %d): %s", attempt, exc)
                if attempt == DATA_ALERT_AFTER:
                    self.notifier.send(f"⚠️ Bot grid belum bisa mulai: {exc}\nDicoba lagi tiap menit.")
                write_error_status(self.status_path, str(exc), self.clock())
                self._sleep_until(self.clock() + min(60, 10 * attempt))
        return False

    def run(self) -> int:
        """Start (retrying while Tokocrypto can't be reached), then poll until stopped.

        Raises SymbolError / StartupError when the config has to change first.
        """
        if not self._start_with_retries():
            self.notifier.close()
            return 0
        self._announce_start()
        next_poll = self.clock()
        while not self.stop_requested:
            self._poll_once()
            next_poll = max(next_poll + self.settings.poll_seconds, self.clock())
            self._sleep_until(next_poll)
        self._shutdown()
        return 0

    def _poll_once(self) -> None:
        try:
            self.tick()
        except DataError as exc:
            self.failures += 1
            self.last_error = str(exc)
            logger.warning("Poll failed (%d in a row): %s", self.failures, exc)
            if self.failures >= DATA_ALERT_AFTER:
                self.notifier.send(
                    f"⚠️ {self.failures}x berturut-turut gagal mengambil data Tokocrypto: {exc}",
                    key="data_error",
                    cooldown_s=ALERT_COOLDOWN_SECONDS,
                )
            self._write_status_quietly()
        except Exception as exc:
            logger.exception("Unexpected error during a poll; continuing")
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.notifier.send(
                f"⚠️ Error tak terduga di bot grid ({type(exc).__name__}) — bot tetap jalan, cek logs/spot.log",
                key="tick_error",
                cooldown_s=ALERT_COOLDOWN_SECONDS,
            )
            self._write_status_quietly()
        else:
            if self.failures or self.last_error:
                alerted = self.failures >= DATA_ALERT_AFTER
                self.failures, self.last_error = 0, ""
                self._write_status_quietly()
                if alerted:
                    self.notifier.send("✅ Data Tokocrypto tersambung lagi.")

    def _write_status_quietly(self) -> None:
        try:
            self._write_status()
        except OSError:
            logger.exception("Failed to write %s", self.status_path)

    def _announce_start(self) -> None:
        g, costs = self.grid, self.settings.costs
        gap = min(s.sell_price / s.buy_price - 1 for s in g.slots) * 100
        net = min(slot_net_profit_pct(s, costs) for s in g.slots)
        stop = self._stop_price()
        lines = [
            f"🟢 Bot grid mulai (PAPER) — {self.settings.symbol}",
            f"Harga {self._money(self._price())} | range {self._money(g.lower)}–{self._money(g.upper)}",
            f"{len(g.slots)} slot × {self._money(self.settings.grid.order_value)}, jarak {gap:.2f}% per level, "
            f"untung bersih ±{net:.2f}% per putaran setelah biaya + pajak",
            f"Uang paper {self._money(g.quote)}" + (f" | stop-loss di {self._money(stop)}" if stop else " | stop-loss mati"),
        ]
        if self.state.get("stopped"):
            lines.append(f"⛔ Grid ini sudah berhenti ({self.state.get('stop_reason')}). Reset: python -m spot.main --reset")
        logger.info("Grid bot started in PAPER mode: %s", self._grid_line(orders=False))
        self.notifier.send("\n".join(lines))

    def _shutdown(self) -> None:
        try:
            self._save(running=False)
        except OSError:
            logger.exception("Failed to save state on shutdown")
        logger.info("Grid bot stopped.")
        equity = self.grid.equity(self.bid)
        self.notifier.send(
            f"🔴 Bot grid berhenti (PAPER) — {self.settings.symbol}, nilai akun {self._money(equity)} "
            f"({self._money(equity - self.state['start_balance'])} dari modal)"
        )
        self.notifier.close()
