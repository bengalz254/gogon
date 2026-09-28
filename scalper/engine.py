"""The trading loop. Paper, testnet and live mode all run this exact code;
only the broker behind it changes.

Every closed candle, per symbol:
    paper fills -> strategy update -> position sync -> manage open trade
    (breakeven / trailing / time stop) or evaluate a new entry.
Every few seconds: position sync (detects stop/target fills on the exchange)
and a check that every open position still has its stop-loss.

Safety rules the engine never breaks:
  * an open position ALWAYS has an exchange-side stop-loss; if one cannot be
    placed, the position is closed at market immediately;
  * stale orders are cancelled before every new entry, so a leftover
    take-profit from an old trade can't hit a new position;
  * state is saved atomically after every change, and on restart the bot
    reconciles its memory with what the exchange actually shows.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Callable

from scalper.config import Settings
from scalper.data import closed_only, fetch_klines, parse_kline
from scalper.exchange.broker import is_bot_order, new_client_id
from scalper.exchange.client import BinanceAPIError
from scalper.journal import StateStore, TradeJournal, utc
from scalper.models import LONG, SHORT, Candle, ClosedTradeInfo, PositionInfo, Signal, interval_ms, order_side
from scalper.notifier import Notifier
from scalper.risk import RiskGuard, evaluate_entry, fee_filter
from scalper.strategies import Strategy, build_strategy
from scalper.trade import Trade, gross_pnl, manage_trade, new_trade_id, targets_from_fill
from scalper.why import WhyTracker, classify

logger = logging.getLogger("scalper.engine")

STATE_VERSION = 1
BALANCE_REFRESH_SECONDS = 300


class MarketData:
    """Candles and quotes from Binance's public endpoints."""

    def __init__(self, client, interval: str):
        self.client = client
        self.interval = interval

    def now_ms(self) -> int:
        return self.client.now_ms()

    def closed_candles(self, symbol: str, limit: int) -> list[Candle]:
        rows = self.client.klines(symbol, self.interval, limit=min(max(limit, 2), 1500))
        return closed_only([parse_kline(r) for r in rows], self.now_ms())

    def history(self, symbol: str, bars: int) -> list[Candle]:
        if bars < 1500:
            return self.closed_candles(symbol, bars + 1)
        now = self.now_ms()
        start = now - (bars + 2) * interval_ms(self.interval)
        return fetch_klines(self.client, symbol, self.interval, start, now, now)

    def quote(self, symbol: str) -> tuple[float, float]:
        bt = self.client.book_ticker(symbol)
        return float(bt["bidPrice"]), float(bt["askPrice"])


class Engine:
    def __init__(
        self,
        settings: Settings,
        broker,
        market,
        rules: dict,
        journal: TradeJournal | None = None,
        store: StateStore | None = None,
        notifier: Notifier | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.s = settings
        self.broker = broker
        self.market = market
        self.rules = rules
        self.journal = journal
        self.store = store
        self.notify = notifier or Notifier(enabled=False)
        self.sleep = sleep
        self.mode = settings.mode
        self.symbols = list(settings.symbols)
        self.interval = settings.interval_ms
        self.strategies: dict[str, Strategy] = {s: build_strategy(settings, s) for s in self.symbols}
        self.guard = RiskGuard(settings.risk)
        self.trades: dict[str, Trade] = {}
        self.last_candle: dict[str, int] = {}
        self.last_close: dict[str, float] = {}
        self.pending_entry: dict[str, dict] = {}
        self._orphan_warned: set[str] = set()
        self.account: dict = {}
        self.why = WhyTracker()
        self._last_pos_check = 0
        self._last_order_check = 0
        self._last_balance_check = 0
        self._last_heartbeat = 0
        self._halt_notified = ""
        self._stop = False
        self.started = False

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------
    def save(self) -> None:
        if self.store is None:
            return
        data = {
            "version": STATE_VERSION,
            "mode": self.mode,
            "saved_at": utc(self.now()),
            "risk": self.guard.to_dict(),
            "trades": {s: t.to_dict() for s, t in self.trades.items()},
            "last_candle": self.last_candle,
            "pending_entry": self.pending_entry,
            "account": self.account,
            "why": self._why_state(),
        }
        if hasattr(self.broker, "to_dict"):
            data["paper"] = self.broker.to_dict()
        self.store.save(data)

    def load(self) -> dict:
        if self.store is None:
            return {}
        data = self.store.load()
        if not data:
            return {}
        if data.get("mode") != self.mode:
            logger.warning("State file belongs to mode %s, not %s; ignoring it", data.get("mode"), self.mode)
            return {}
        self.guard = RiskGuard.from_dict(self.s.risk, data.get("risk"))
        self.trades = {
            s: Trade.from_dict(t) for s, t in (data.get("trades") or {}).items() if s in self.symbols
        }
        self.last_candle = {k: int(v) for k, v in (data.get("last_candle") or {}).items()}
        self.pending_entry = dict(data.get("pending_entry") or {})
        if hasattr(self.broker, "load_dict"):
            self.broker.load_dict(data.get("paper"))
        return data

    def now(self) -> int:
        return self.market.now_ms()

    def _why_state(self) -> dict:
        params = getattr(self.s.strategy, self.s.strategy.name, None)
        return {
            "window_hours": self.why.window_ms // 3_600_000,
            "tf": self.s.timeframe,
            "htf": getattr(params, "htf_interval", ""),
            "fee_min_pct": round(self.s.risk.min_sl_cost_ratio * self.s.costs.round_trip_cost * 100.0, 4),
            "symbols": self.why.to_dict(),
        }

    def _note(self, sym: str, c: Candle, code: str, reason: str = "") -> None:
        """Remember what this candle led to (dashboard "why no entry")."""
        try:
            extra = self.strategies[sym].status()
        except Exception:  # noqa: BLE001 - diagnostics must never disturb trading
            extra = {}
        self.why.record(sym, c.close_time, code, reason, extra)

    def _refresh_balance(self) -> tuple[float, float]:
        """Read the wallet and keep it for the state file, so the dashboard
        shows the balance before the first trade has closed."""
        wallet, available = self.broker.balances()
        self._last_balance_check = self.now()
        self.account = {"balance": wallet, "available": available, "at": utc(self._last_balance_check)}
        return wallet, available

    # ------------------------------------------------------------------
    # startup / shutdown
    # ------------------------------------------------------------------
    def start(self) -> None:
        for w in self.broker.prepare(self.symbols):
            logger.warning(w)
        self._apply_exchange_fees()
        saved = self.load()
        if saved:
            logger.info(
                "Restored state: %d open trade(s), realized today %.2f",
                len(self.trades), self.guard.s.realized_today,
            )
        self._warmup()
        wallet, _ = self._refresh_balance()
        self.guard.start(wallet, self.now())
        self.reconcile()
        self.save()
        self.started = True
        self._last_heartbeat = self.now()
        msg = (
            f"Scalper started [{self.mode.upper()}] {self.s.strategy.name} {self.s.timeframe} "
            f"on {', '.join(self.symbols)} | balance {wallet:.2f} | risk/trade "
            f"{self.s.risk.risk_per_trade_pct}% | leverage {self.s.execution.leverage}x"
        )
        logger.info(msg)
        self.notify.send("🟢 " + msg)

    def _apply_exchange_fees(self) -> None:
        rates = getattr(self.broker, "fee_rates", {}) or {}
        if not rates:
            return
        maker = max(r[0] for r in rates.values())
        taker = max(r[1] for r in rates.values())
        c = self.s.costs
        if (maker, taker) != (c.maker_fee, c.taker_fee):
            logger.info(
                "Using your account's fee rates: maker %.4f%% taker %.4f%% (config had %.4f%% / %.4f%%)",
                maker * 100, taker * 100, c.maker_fee * 100, c.taker_fee * 100,
            )
            c.maker_fee, c.taker_fee = maker, taker

    def _warmup(self) -> None:
        # History inside the "why" window is replayed into it too, so the
        # dashboard can explain the last 24h right after a (re)start.
        why_since = self.now() - self.why.window_ms
        for sym in self.symbols:
            strat = self.strategies[sym]
            need = min(max(self.s.execution.warmup_bars, strat.warmup_bars + 50), 5000)
            candles = self.market.history(sym, need)
            if len(candles) < strat.warmup_bars:
                logger.warning(
                    "%s: only %d candles of history, strategy needs %d — it will stay idle until warmed up",
                    sym, len(candles), strat.warmup_bars,
                )
            saved_last = self.last_candle.get(sym, 0)
            trade = self.trades.get(sym)
            for c in candles:
                if saved_last and c.open_time > saved_last:
                    # candles we missed while offline: let paper orders fill
                    # and count them toward the open trade's age
                    self.broker.on_candle(sym, c)
                    if trade is not None:
                        trade.observe(c)
                sig = strat.on_candle(c)
                if c.close_time > why_since:
                    self._note_history(sym, c, sig, trade)
            if candles:
                self.last_candle[sym] = candles[-1].open_time
                self.last_close[sym] = candles[-1].close
            logger.info("%s: warmed up on %d candles", sym, len(candles))

    def _note_history(self, sym: str, c: Candle, sig: Signal | None, trade: Trade | None) -> None:
        if trade is not None and c.close_time >= trade.opened_at:
            self._note(sym, c, "in_trade")
        elif sig is not None:
            # Not traded (history); tell apart signals the fee filter would
            # have rejected from ones that were valid.
            reason = fee_filter(sig, self.s.costs, self.s.risk)
            self._note(sym, c, classify(reason) if reason else "signal", reason)
        else:
            reason = self.strategies[sym].last_skip_reason
            self._note(sym, c, classify(reason), reason)

    def reconcile(self) -> None:
        """Make memory match the exchange: settle closed trades, adopt
        orphans, re-protect naked positions, cancel stale orders."""
        positions = self.broker.positions()
        for sym in self.symbols:
            self._sync_symbol(sym, positions)
        for sym in self.symbols:
            if sym not in self.trades and sym not in positions:
                self.pending_entry.pop(sym, None)
                self._cancel_stale(sym)
        self._check_protection(self.broker.positions())

    def stop(self) -> None:
        self._stop = True

    def shutdown(self) -> None:
        if self.s.execution.flatten_on_exit:
            for sym, trade in list(self.trades.items()):
                logger.warning("%s: flatten_on_exit -> closing position", sym)
                self._exit_trade(sym, trade, "SHUTDOWN")
        self.save()
        open_txt = ", ".join(f"{s} {t.side}" for s, t in self.trades.items()) or "none"
        msg = f"Scalper stopped [{self.mode.upper()}]. Open positions (protected by exchange stops): {open_txt}"
        logger.info(msg)
        self.notify.send("🔴 " + msg)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run(self) -> None:
        if not self.started:
            self.start()
        poll = max(0.5, self.s.execution.poll_seconds)
        try:
            while not self._stop:
                try:
                    self.tick()
                except BinanceAPIError as e:
                    logger.error("Exchange error in main loop: %s", e)
                    self._nap(getattr(e, "retry_after", 5.0))
                except Exception:
                    logger.exception("Unexpected error in main loop; continuing")
                    self._nap(5.0)
                self._nap(poll)
        finally:
            self.shutdown()

    def _nap(self, seconds: float) -> None:
        """Sleep in small steps so Ctrl+C / SIGTERM is honoured quickly."""
        slept = 0.0
        while slept < seconds and not self._stop:
            step = min(0.5, seconds - slept)
            self.sleep(step)
            slept += step

    def tick(self) -> None:
        now = self.now()
        if self.guard.on_time(now):
            self._daily_summary()
        for sym in self.symbols:
            try:
                self._poll_candles(sym, now)
            except BinanceAPIError as e:
                logger.warning("%s: candle update failed: %s", sym, e)
        ex = self.s.execution
        if now - self._last_pos_check >= ex.position_check_seconds * 1000:
            self._last_pos_check = now
            positions = self.broker.positions()
            for sym in self.symbols:
                self._sync_symbol(sym, positions)
            if now - self._last_order_check >= ex.order_check_seconds * 1000:
                self._last_order_check = now
                self._check_protection(positions)
        if now - self._last_balance_check >= BALANCE_REFRESH_SECONDS * 1000:
            self._last_balance_check = now
            try:
                self._refresh_balance()
                self.save()
            except BinanceAPIError as e:
                logger.warning("Balance update failed: %s", e)
        self._maybe_notify_halt()
        if self.s.notify.heartbeat_minutes > 0 and now - self._last_heartbeat >= self.s.notify.heartbeat_minutes * 60_000:
            self._last_heartbeat = now
            self._heartbeat()

    def _poll_candles(self, sym: str, now: int) -> None:
        last = self.last_candle.get(sym)
        delay = int(self.s.execution.candle_close_delay_seconds * 1000)
        if last is not None and now < last + 2 * self.interval + delay:
            return  # the next candle hasn't closed yet
        missed = 5 if last is None else int((now - last) // self.interval) + 2
        candles = self.market.closed_candles(sym, min(missed, 1500))
        new = [c for c in candles if last is None or c.open_time > last]
        if last is not None and new and new[0].open_time > last + self.interval:
            logger.warning("%s: %d candle(s) were missed (sleep / network?)", sym,
                           (new[0].open_time - last) // self.interval - 1)
        if missed > 1500:
            logger.warning("%s: offline longer than 1500 candles; indicators are stale. "
                           "Restart the bot to re-warm them.", sym)
        for c in new:
            self._on_candle(sym, c)

    # ------------------------------------------------------------------
    # per-candle logic
    # ------------------------------------------------------------------
    def _on_candle(self, sym: str, c: Candle) -> None:
        strat = self.strategies[sym]
        self.broker.on_candle(sym, c)
        sig = strat.on_candle(c)
        self.last_candle[sym] = c.open_time
        self.last_close[sym] = c.close
        # Only the candle that JUST closed may open a trade. Candles replayed
        # after a network gap update indicators and manage open trades, but
        # an hour-old signal is not a reason to buy now.
        fresh = c.close_time >= self.now() - self.interval

        positions = self.broker.positions()
        self._sync_symbol(sym, positions)
        trade = self.trades.get(sym)
        if trade is not None:
            self._note(sym, c, "in_trade")
            pos = positions.get(sym)
            if pos is None or not pos.is_open:
                self.save()  # position already gone; waiting for its fills to settle
                return
            trade.observe(c)
            act = manage_trade(trade, c, strat.atr, self.s.management, self.s.costs.round_trip_cost)
            if act.exit_reason:
                logger.info("%s: %s exit after %d bars", sym, act.exit_reason, trade.bars_held)
                self._exit_trade(sym, trade, act.exit_reason)
            elif act.new_stop is not None:
                self._move_stop(sym, trade, act.new_stop, act.new_stop_kind)
        elif sig is not None and fresh:
            reason = self._try_entry(sym, sig)
            self._note(sym, c, classify(reason) if reason else "entered", reason)
        elif sig is not None:
            logger.info("%s: ignoring stale %s signal from %s", sym, sig.side, utc(c.close_time))
            self._note(sym, c, "stale")
        else:
            self._note(sym, c, classify(strat.last_skip_reason), strat.last_skip_reason)
        self.save()

    def _sync_symbol(self, sym: str, positions: dict[str, PositionInfo]) -> None:
        pos = positions.get(sym)
        trade = self.trades.get(sym)
        if trade is not None:
            if pos is None or not pos.is_open:
                self._settle(sym, trade)
            elif pos.side != trade.side:
                logger.error("%s: exchange shows a %s position but the bot holds %s; re-syncing", sym, pos.side, trade.side)
                self._settle(sym, trade, force=True)
                self._adopt(sym, pos)
            elif abs(abs(pos.qty) - trade.qty) > 1e-12:
                logger.info("%s: position size changed %.6f -> %.6f (partial fill / manual change)",
                            sym, trade.qty, abs(pos.qty))
                trade.qty = abs(pos.qty)
                self.save()
        elif pos is not None and pos.is_open:
            self._adopt(sym, pos)

    # ------------------------------------------------------------------
    # entries
    # ------------------------------------------------------------------
    def _try_entry(self, sym: str, sig: Signal) -> str:
        """Act on a fresh signal. Returns "" if a protected position was
        opened, otherwise why not (for the dashboard's "why no entry")."""
        now = self.now()
        positions = self.broker.positions()
        if sym in positions:
            return "already in a position"
        try:
            bid, ask = self.market.quote(sym)
        except BinanceAPIError as e:
            logger.warning("%s: no quote, skipping signal: %s", sym, e)
            return f"no quote: {e}"
        price = ask if sig.side == LONG else bid
        wallet, available = self._refresh_balance()
        decision = evaluate_entry(
            sig, price, wallet, available, len(positions), now, self.guard,
            self.rules.get(sym), self.s.risk, self.s.costs, self.s.execution.leverage,
        )
        if not decision.ok:
            logger.info("%s: %s signal skipped: %s", sym, sig.side, decision.reason)
            return decision.reason
        size = decision.size
        leftover = self.broker.cancel_all(sym)
        if leftover:
            logger.error("%s: could not clear old orders %s; not entering", sym, [o.client_id for o in leftover])
            return "old orders could not be cleared"

        cid = new_client_id("en")
        self.pending_entry[sym] = {"client_id": cid, "time": now, "side": sig.side}
        self.save()
        qty = size.qty_dec if size.qty_dec is not None else Decimal(str(size.qty))
        logger.info(
            "%s: %s entry %s @ ~%s | stop %.6g | %s", sym, sig.side, qty, price, sig.stop, sig.reason
        )
        try:
            fill = self.broker.market_order(sym, order_side(sig.side), qty, reduce_only=False, client_id=cid)
        except BinanceAPIError as e:
            logger.error("%s: entry order rejected: %s", sym, e)
            self.notify.send(f"⚠️ {sym} entry rejected: {e.msg}")
            self.pending_entry.pop(sym, None)
            self.save()
            return f"entry rejected: {e.msg}"
        self.pending_entry.pop(sym, None)
        if fill.qty <= 0:
            logger.warning("%s: entry not filled", sym)
            self.save()
            return "entry not filled"

        targets = targets_from_fill(sig, fill.avg_price)
        stop, tp = targets if targets else (sig.stop, sig.take_profit)
        trade = Trade(
            trade_id=new_trade_id(),
            symbol=sym,
            side=sig.side,
            strategy=sig.strategy,
            qty=fill.qty,
            entry_price=fill.avg_price,
            stop=stop,
            initial_stop=stop,
            take_profit=tp,
            opened_at=now,
            entry_fee=fill.fee or fill.qty * fill.avg_price * self.s.costs.taker_fee,
            reason=sig.reason,
            entry_order_id=fill.order_id,
        )
        self.trades[sym] = trade
        self.guard.on_trade_opened(sym, now)
        self.save()
        if targets is None:
            logger.warning("%s: filled at %.6g, already past the stop/target; exiting", sym, fill.avg_price)
            self._exit_trade(sym, trade, "BAD_FILL")
            return "bad fill: already past the stop/target"
        if not self._place_stop(sym, trade):
            logger.error("%s: STOP-LOSS COULD NOT BE PLACED - closing the position now", sym)
            self.notify.send(f"🚨 {sym}: stop-loss could not be placed, position closed immediately")
            self._exit_trade(sym, trade, "NO_STOP")
            return "stop-loss could not be placed"
        self._place_tp(sym, trade)
        self.save()
        risk_usd = trade.qty * trade.risk_per_unit
        msg = (
            f"{sym} {trade.side} {trade.qty:g} @ {trade.entry_price:.6g} | SL {trade.stop:.6g} | "
            f"TP {trade.take_profit:.6g} | risk ~{risk_usd:.2f} | {sig.reason}"
        )
        logger.info("OPENED %s", msg)
        self.notify.send(("📈 " if trade.side == LONG else "📉 ") + f"[{self.mode}] OPEN {msg}")
        return ""

    def _place_stop(self, sym: str, trade: Trade) -> bool:
        for attempt in range(3):
            try:
                trade.sl_order = self.broker.place_stop(
                    sym, trade.side, trade.stop, Decimal(str(trade.qty)), new_client_id("sl")
                )
                return True
            except BinanceAPIError as e:
                if e.code == -2021:  # "Order would immediately trigger": price is already through it
                    logger.warning("%s: stop %.6g would trigger immediately", sym, trade.stop)
                    return False
                logger.error("%s: stop-loss placement failed (attempt %d): %s", sym, attempt + 1, e)
            except Exception as e:  # noqa: BLE001 - never let this path crash silently
                logger.error("%s: stop-loss placement error (attempt %d): %s", sym, attempt + 1, e)
            self.sleep(0.5 * (attempt + 1))
        return False

    def _place_tp(self, sym: str, trade: Trade) -> None:
        try:
            trade.tp_order = self.broker.place_take_profit(
                sym, trade.side, trade.take_profit, Decimal(str(trade.qty)), new_client_id("tp")
            )
        except Exception as e:  # noqa: BLE001 - the stop protects the position; retried later
            trade.tp_order = None
            logger.warning("%s: take-profit not placed (%s); will retry", sym, e)

    # ------------------------------------------------------------------
    # management / exits
    # ------------------------------------------------------------------
    def _move_stop(self, sym: str, trade: Trade, new_stop: float, kind: str) -> None:
        old_ref = trade.sl_order
        try:
            new_ref = self.broker.place_stop(sym, trade.side, new_stop, Decimal(str(trade.qty)), new_client_id("sl"))
        except BinanceAPIError as e:
            logger.warning("%s: could not move stop to %.6g (%s); keeping %.6g", sym, new_stop, e, trade.stop)
            return
        # New stop first, THEN cancel the old one: the position is never
        # without a stop, even for a moment.
        trade.sl_order = new_ref
        trade.stop = new_stop
        trade.stop_kind = kind
        if old_ref is not None:
            try:
                self.broker.cancel(sym, old_ref)
            except BinanceAPIError as e:
                logger.warning("%s: old stop not cancelled (%s); it is reduce-only, so it can never open a "
                               "position, and it is cleaned up when the trade closes", sym, e)
        logger.info("%s: stop moved to %.6g (%s)", sym, new_stop, kind)
        self.save()

    def _exit_trade(self, sym: str, trade: Trade, reason: str) -> None:
        trade.exit_hint = reason
        try:
            pos = self.broker.positions().get(sym)
            if pos is not None and pos.is_open:
                self.broker.close_position(sym, pos)
        except BinanceAPIError as e:
            logger.error("%s: market exit (%s) failed: %s — the exchange stop still protects it", sym, reason, e)
            self.notify.send(f"⚠️ {sym}: exit ({reason}) failed: {e.msg}")
            return
        self._cancel_stale(sym)
        self._sync_symbol(sym, self.broker.positions())

    def _cancel_stale(self, sym: str) -> None:
        try:
            orders = self.broker.open_orders(sym)
        except BinanceAPIError as e:
            logger.warning("%s: could not list open orders: %s", sym, e)
            return
        bot_orders = [o for o in orders if is_bot_order(o) or o.kind == "paper"]
        if not bot_orders:
            return
        if len(bot_orders) == len(orders):
            self.broker.cancel_all(sym)
        else:
            for o in bot_orders:
                try:
                    self.broker.cancel(sym, o)
                except BinanceAPIError as e:
                    logger.warning("%s: could not cancel %s: %s", sym, o.client_id, e)
        logger.info("%s: cancelled %d leftover bot order(s)", sym, len(bot_orders))

    def _label_exit(self, trade: Trade, price: float) -> str:
        if trade.exit_hint:
            return trade.exit_hint
        if abs(price - trade.take_profit) <= abs(price - trade.stop):
            return "TP"
        return trade.stop_kind

    def _settle(self, sym: str, trade: Trade, force: bool = False) -> None:
        """Book a trade whose position is gone from the exchange."""
        trade.close_attempts += 1
        final = force or trade.close_attempts >= 6
        info: ClosedTradeInfo | None = None
        try:
            info = self.broker.closed_trade_info(sym, trade, final=final)
        except BinanceAPIError as e:
            logger.warning("%s: could not fetch closing fills: %s", sym, e)
        if info is None and not final:
            self.save()
            return  # fills not visible yet; retry on the next sync
        if info is None:
            px = self.last_close.get(sym, trade.entry_price)
            info = ClosedTradeInfo(
                exit_price=px,
                qty=trade.qty,
                gross_pnl=gross_pnl(trade.side, trade.entry_price, px, trade.qty),
                fees=trade.entry_fee + trade.qty * px * self.s.costs.taker_fee,
                exit_time=self.now(),
                approximate=True,
            )
        self._cancel_stale(sym)
        now = self.now()
        net = info.gross_pnl - info.fees - info.funding
        risk_usd = trade.initial_qty * trade.risk_per_unit
        r_mult = net / risk_usd if risk_usd > 0 else 0.0
        reason = self._label_exit(trade, info.exit_price)
        self.guard.on_trade_closed(sym, net, now, self.s.management.cooldown_bars_after_exit * self.interval)
        del self.trades[sym]
        try:
            wallet, _ = self._refresh_balance()
        except BinanceAPIError:
            wallet = float("nan")
        if self.journal is not None:
            self.journal.record(
                {
                    "closed_at_utc": utc(now),
                    "mode": self.mode,
                    "trade_id": trade.trade_id,
                    "symbol": sym,
                    "side": trade.side,
                    "strategy": trade.strategy,
                    "entry_time_utc": utc(trade.opened_at),
                    "exit_time_utc": utc(info.exit_time or now),
                    "entry_price": f"{trade.entry_price:.8g}",
                    "exit_price": f"{info.exit_price:.8g}",
                    "qty": f"{trade.initial_qty:.8g}",
                    "initial_stop": f"{trade.initial_stop:.8g}",
                    "take_profit": f"{trade.take_profit:.8g}",
                    "exit_reason": reason,
                    "gross_pnl": f"{info.gross_pnl:.6f}",
                    "fees": f"{info.fees:.6f}",
                    "funding": f"{info.funding:.6f}",
                    "net_pnl": f"{net:.6f}",
                    "r_multiple": f"{r_mult:.3f}",
                    "bars_held": trade.bars_held,
                    "equity_after": f"{wallet:.4f}",
                    "approximate": info.approximate,
                    "note": "adopted" if trade.adopted else trade.reason[:120],
                }
            )
        self.save()
        icon = "✅" if net > 0 else "❌"
        msg = (
            f"{sym} {trade.side} closed by {reason} @ {info.exit_price:.6g} | net {net:+.2f} "
            f"({r_mult:+.2f}R, fees {info.fees:.2f}) | today {self.guard.s.realized_today:+.2f}"
        )
        logger.info("CLOSED %s%s", msg, " (approximate)" if info.approximate else "")
        self.notify.send(f"{icon} [{self.mode}] {msg}")

    def _adopt(self, sym: str, pos: PositionInfo) -> None:
        """A position the bot isn't tracking (crash mid-entry, or manual)."""
        ours = self.pending_entry.pop(sym, None) is not None
        if self.s.execution.orphan_policy == "ignore" and not ours:
            if sym not in self._orphan_warned:
                self._orphan_warned.add(sym)
                logger.warning("%s: untracked %s position found; orphan_policy=ignore, leaving it alone", sym, pos.side)
                self.notify.send(f"⚠️ {sym}: untracked {pos.side} position found and left alone (orphan_policy=ignore)")
            return
        strat = self.strategies[sym]
        atr = strat.atr or pos.entry_price * 0.005
        dist = self.s.execution.orphan_sl_atr * atr
        side = pos.side
        stop = pos.entry_price - dist if side == LONG else pos.entry_price + dist
        tp_r = self.s.strategy.trend_pullback.tp_r
        tp = pos.entry_price + tp_r * dist if side == LONG else pos.entry_price - tp_r * dist
        trade = Trade(
            trade_id=new_trade_id(),
            symbol=sym,
            side=side,
            strategy="adopted",
            qty=abs(pos.qty),
            entry_price=pos.entry_price,
            stop=stop,
            initial_stop=stop,
            take_profit=tp,
            opened_at=self.now(),
            entry_fee=abs(pos.qty) * pos.entry_price * self.s.costs.taker_fee,
            reason="adopted untracked position",
            adopted=True,
        )
        self.trades[sym] = trade
        logger.warning("%s: adopting untracked %s position %g @ %.6g (stop %.6g, target %.6g)",
                       sym, side, trade.qty, pos.entry_price, stop, tp)
        self.notify.send(f"⚠️ {sym}: adopted untracked {side} position; stop {stop:.6g}, target {tp:.6g}")
        self._cancel_stale(sym)
        last = self.last_close.get(sym)
        if last is not None and ((side == LONG and last <= stop) or (side == SHORT and last >= stop)):
            self._exit_trade(sym, trade, "ADOPT_PAST_STOP")
            return
        if not self._place_stop(sym, trade):
            self._exit_trade(sym, trade, "NO_STOP")
            return
        self._place_tp(sym, trade)
        self.save()

    def _check_protection(self, positions: dict[str, PositionInfo]) -> None:
        for sym, trade in list(self.trades.items()):
            pos = positions.get(sym)
            if pos is None or not pos.is_open:
                continue  # settles on the next sync
            try:
                orders = self.broker.open_orders(sym)
            except BinanceAPIError as e:
                logger.warning("%s: protection check skipped: %s", sym, e)
                continue
            if not any(o.purpose == "sl" for o in orders):
                logger.error("%s: STOP-LOSS MISSING on the exchange - re-placing it", sym)
                self.notify.send(f"🚨 {sym}: stop-loss was missing, re-placing it")
                if not self._place_stop(sym, trade):
                    self._exit_trade(sym, trade, "NO_STOP")
                    continue
            if not any(o.purpose == "tp" for o in orders):
                self._place_tp(sym, trade)
        for sym in self.symbols:
            if sym not in self.trades and sym not in positions:
                self._cancel_stale(sym)
        self.save()

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    def status_line(self) -> str:
        parts = []
        for sym, t in self.trades.items():
            px = self.last_close.get(sym, t.entry_price)
            parts.append(f"{sym} {t.side} {t.r_multiple(px):+.2f}R ({t.stop_kind})")
        g = self.guard.s
        return (
            f"today {g.realized_today:+.2f} in {g.trades_today} trade(s) | "
            f"drawdown {self.guard.drawdown_pct:.1f}% | open: {', '.join(parts) or 'none'}"
        )

    def _heartbeat(self) -> None:
        try:
            wallet, _ = self._refresh_balance()
        except BinanceAPIError:
            wallet = float("nan")
        msg = f"[{self.mode}] balance {wallet:.2f} | {self.status_line()}"
        waiting = " ".join(f"{s}={self.why.latest[s]['code']}" for s in self.symbols if s in self.why.latest)
        if waiting:
            msg += f" | now: {waiting}"
        logger.info("Heartbeat: %s", msg)
        self.notify.send("💓 " + msg)

    def _daily_summary(self) -> None:
        msg = f"[{self.mode}] New UTC day. {self.status_line()}"
        logger.info(msg)
        self.notify.send("📅 " + msg)
        self._halt_notified = ""

    def _maybe_notify_halt(self) -> None:
        reason = ""
        if self.guard.s.halted:
            reason = f"HALTED: {self.guard.s.halt_reason}"
        elif self.guard.daily_loss_hit:
            reason = "Daily loss limit reached; no new trades until 00:00 UTC"
        if reason and reason != self._halt_notified:
            self._halt_notified = reason
            logger.warning(reason)
            self.notify.send("🛑 " + reason)
