"""Entry point: python -m bot.main"""
from __future__ import annotations

import json
import logging
import os
import signal as signal_module
import time
from datetime import datetime, timezone

from bot.books import BookProvider
from bot.client import build_client
from bot.config import Settings, load_settings
from bot.execution import OrderExecutor
from bot.fees import FeeModel
from bot.gamma import fetch_negrisk_events
from bot.heartbeat import Heartbeat
from bot.journal import TradeJournal
from bot.logger import setup_logging
from bot.market_data import MarketInfo, iter_active_markets
from bot.merge import merge_market
from bot.notify import Notifier
from bot.quoting import QuoteManager
from bot.reconcile import reconcile
from bot.risk import RiskManager
from bot.settlement import settle_resolved_markets
from bot.state import StateStore, state_path
from bot.strategies import ArbitrageStrategy, ThresholdStrategy
from bot.strategies.market_maker import MidTracker, compute_quotes, select_markets, skip_reason
from bot.strategies.negrisk_arbitrage import NegRiskArbitrageStrategy
from bot.ws_market import MarketFeed

logger = logging.getLogger("polybot.main")

# Small JSON heartbeat, rewritten every cycle (read by the Docker healthcheck).
STATUS_PATH = "data/status.json"
# How often held markets are checked for resolution (and settled).
SETTLE_INTERVAL_SECONDS = 600
# How often, in live mode, positions are re-checked against Polymarket.
RECONCILE_INTERVAL_SECONDS = 3600
# The same recurring problem alerts at most this often.
ERROR_ALERT_COOLDOWN_SECONDS = 900
RECONCILE_ALERT_COOLDOWN_SECONDS = 6 * 3600
# Alert when this many cycles in a row found no markets at all (API down?).
EMPTY_SCAN_ALERT_CYCLES = 3

_stop = False


def _request_stop(signum, frame):
    global _stop
    _stop = True


def _usd(amount: float) -> str:
    return f"{'-' if amount < 0 else '+'}${abs(amount):.2f}"


def _pnl_summary(risk: RiskManager) -> str:
    return (
        f"posisi {len(risk.positions)} | eksposur ${risk.total_exposure_usd:.2f} | "
        f"P&L hari ini {_usd(risk.daily_pnl)} (realized {_usd(risk.realized_pnl_today)}, "
        f"unrealized {_usd(risk.unrealized_pnl)})"
    )


def _ws_assets(risk: RiskManager, priority: list[str], scan_tokens: list[str], limit: int) -> list[str]:
    """Tokens to stream: held positions and quoted markets first (they need
    live books), then the scan's."""
    return list(dict.fromkeys([*risk.positions, *priority, *scan_tokens]))[: max(0, limit)]


def _marks_for_positions(risk: RiskManager, get_book) -> dict[str, float]:
    """Best bid for every held token — what each position could be sold for now."""
    marks = {}
    for token_id in list(risk.positions):
        bid = get_book(token_id).best_bid
        if bid is not None:
            marks[token_id] = bid
    return marks


def _guarded(notifier: Notifier, step: str, fn, *args) -> None:
    """Run a housekeeping step; on failure log and alert instead of stopping the bot."""
    try:
        fn(*args)
    except Exception as exc:
        logger.exception("%s failed; continuing", step)
        notifier.send(
            f"⚠️ Langkah '{step}' gagal ({type(exc).__name__}) — bot tetap jalan, cek logs/bot.log",
            key=f"step_error:{step}",
            cooldown_s=ERROR_ALERT_COOLDOWN_SECONDS,
        )


def _settle_resolved(
    client, risk: RiskManager, journal: TradeJournal, store: StateStore, notifier: Notifier, mode: str
) -> None:
    settled = settle_resolved_markets(client, risk, journal, mode)
    if not settled:
        return
    try:
        store.save(risk.snapshot())
    except Exception:
        logger.exception("Failed to persist risk state after settlement")
    redeem_note = "\nKlaim (redeem) pUSD-nya di Polymarket." if mode == "live" else ""
    for market_id, pnl in settled:
        notifier.send(
            f"🏁 [{mode.upper()}] Market {market_id[:12]} sudah selesai — posisi ditutup, P&L {_usd(pnl)}{redeem_note}"
        )


def _handle_complete_sets(
    settings, risk: RiskManager, journal: TradeJournal, store: StateStore, notifier: Notifier, mode: str
) -> None:
    """Paper: merge complete sets right away. Live: say when they're worth merging."""
    sets_by_market = risk.markets_with_complete_sets()
    if not sets_by_market:
        return
    if mode == "paper":
        if not settings.merge.paper_auto_merge:
            return
        for market_id in sets_by_market:
            merge_market(risk, journal, market_id, mode)
        try:
            store.save(risk.snapshot())
        except Exception:
            logger.exception("Failed to persist risk state after merging")
        return
    for market_id, sets in sets_by_market.items():
        if sets >= settings.merge.live_alert_min_sets:
            notifier.send(
                f"🔁 {sets:.2f} set lengkap (semua outcome) di market {market_id} bisa di-merge jadi "
                f"${sets:.2f} pUSD. Merge di Polymarket, lalu hentikan bot dan catat dengan:\n"
                f"python scripts/positions.py --live merge {market_id}",
                key=f"merge:{market_id}",
                cooldown_s=24 * 3600,
            )


def _reconcile_positions(address: str, risk: RiskManager, notifier: Notifier) -> None:
    local = {token_id: pos.size for token_id, pos in risk.positions.items()}
    mismatches = reconcile(address, local)
    if mismatches is None:
        notifier.send(
            "⚠️ Tidak bisa mengecek posisi ke Polymarket (Data API) — cek logs/bot.log",
            key="reconcile_failed",
            cooldown_s=RECONCILE_ALERT_COOLDOWN_SECONDS,
        )
    elif mismatches:
        logger.warning("Positions differ from Polymarket (%d): %s", len(mismatches), "; ".join(mismatches))
        shown = mismatches[:10]
        more = f"\n…dan {len(mismatches) - len(shown)} lainnya" if len(mismatches) > len(shown) else ""
        notifier.send(
            "⚠️ Posisi yang dicatat bot beda dengan Polymarket:\n" + "\n".join(shown) + more,
            key="reconcile_mismatch",
            cooldown_s=RECONCILE_ALERT_COOLDOWN_SECONDS,
        )
    else:
        logger.info("Positions match Polymarket (%d tokens checked).", len(local))


def _write_status(
    mode: str,
    risk: RiskManager,
    cycles: int,
    last_cycle_seconds: float,
    markets_scanned: int,
    feed: MarketFeed | None = None,
    market_maker: dict | None = None,
) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_ts": time.time(),
        "mode": mode,
        "cycles": cycles,
        "last_cycle_seconds": round(last_cycle_seconds, 2),
        "markets_scanned": markets_scanned,
        "open_positions": len(risk.positions),
        "exposure_usd": round(risk.total_exposure_usd, 4),
        "realized_pnl_today_usd": round(risk.realized_pnl_today, 4),
        "unrealized_pnl_usd": round(risk.unrealized_pnl, 4),
        "kill_switch": risk.daily_loss_limit_hit,
        "websocket_live": feed.is_live() if feed is not None else False,
        "market_maker": market_maker or {"enabled": False},
    }
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    tmp_path = STATUS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, STATUS_PATH)


class Bot:
    """Wires the pieces together and runs two cadences: a scan every
    polling_interval_seconds (arbitrage/threshold, settlement, merging,
    reconciliation, status) and, when market making is on, a quote refresh
    every market_maker.refresh_seconds."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.live = settings.wallet.live_trading
        self.mode = "live" if self.live else "paper"

        self.notifier = Notifier(settings.notifications.telegram_bot_token, settings.notifications.telegram_chat_id)
        if self.notifier.enabled:
            logger.info("Telegram notifications enabled.")

        self.client = build_client(settings.wallet)
        self.risk = RiskManager(settings.risk)
        self.store = StateStore(state_path(self.live))
        saved = self.store.load()
        if saved is not None:
            self.risk.restore(saved)
            logger.info(
                "Restored state from %s: %d open positions | exposure=$%.2f | realized_pnl_today=$%.2f",
                self.store.path,
                len(self.risk.positions),
                self.risk.total_exposure_usd,
                self.risk.realized_pnl_today,
            )
        # Exact per-market fee terms from Polymarket when available, else config rates.
        self.fees = FeeModel(settings.fees, market_info=getattr(self.client, "get_clob_market_info", None))
        self.feed = None
        if settings.market_data.websocket:
            self.feed = MarketFeed(url=settings.market_data.ws_url, stale_after=settings.market_data.stale_after_seconds)
            self.feed.start()
        self.books = BookProvider(self.client, self.feed, batch_size=settings.market_data.rest_batch_size)
        self.journal = TradeJournal()
        self.executor = OrderExecutor(
            self.client,
            self.risk,
            self.journal,
            live=self.live,
            store=self.store,
            notifier=self.notifier,
            notify_fills=settings.notifications.fills,
            book_lookup=self.books.book,
        )

        self.strategies = []
        if settings.arbitrage.enabled:
            self.strategies.append(ArbitrageStrategy(settings.arbitrage, self.risk, self.fees))
        if settings.threshold.enabled:
            self.strategies.append(ThresholdStrategy(settings.threshold, self.risk, self.fees))
        self.negrisk = (
            NegRiskArbitrageStrategy(settings.negrisk_arbitrage, self.risk, self.fees)
            if settings.negrisk_arbitrage.enabled
            else None
        )
        self.negrisk_events = []
        self.last_negrisk_fetch = 0.0

        self.mm_cfg = settings.market_maker
        self.mm_enabled = self.mm_cfg.enabled
        if self.mm_enabled and self.feed is None:
            logger.error("Market making needs market_data.websocket: true (live books) — market maker disabled.")
            self.mm_enabled = False
        self.quotes = (
            QuoteManager(self.client, self.risk, self.journal, self.fees, self.mm_cfg, live=self.live, store=self.store)
            if self.mm_enabled
            else None
        )
        self.heartbeat = Heartbeat(self.client) if self.mm_enabled and self.live else None
        self.mm_markets: list[MarketInfo] = []
        self.mids = MidTracker(self.mm_cfg.volatility_window_seconds)
        self.cooldown_until: dict[str, float] = {}
        self.last_mm_select = 0.0

        if not self.strategies and not self.mm_enabled and self.negrisk is None:
            logger.warning("No strategies enabled in config/settings.yaml — bot will idle.")

        self.cycles = 0
        self.empty_scans = 0
        self.kill_switch_on = False
        self.last_settle = self.last_reconcile = self.last_heartbeat_msg = time.time()

    # -- lifecycle ---------------------------------------------------------------
    def run(self) -> None:
        self.notifier.send(f"🟢 Bot mulai ({self.mode.upper()}) — {_pnl_summary(self.risk)}")
        # Before trading, settle markets that resolved while the bot was down and
        # check positions against the exchange; both then repeat periodically.
        _guarded(self.notifier, "settlement", _settle_resolved, *self._settle_args())
        if self.live:
            _guarded(self.notifier, "reconcile", _reconcile_positions, *self._reconcile_args())
        if self.mm_enabled:
            self._start_market_making()

        # `now` never runs behind the deadline the loop last slept to, so an
        # early wake-up can't skip a due scan or refresh.
        now = time.time()
        next_scan = next_mm = now
        while not _stop:
            now = max(time.time(), now)
            if now >= next_scan:
                self.scan_cycle()
                now = max(time.time(), now)
                next_scan = now + self.settings.polling_interval_seconds
            if self.mm_enabled and now >= next_mm and not _stop:
                _guarded(self.notifier, "market maker", self.mm_tick)
                now = max(time.time(), now)
                next_mm = now + self.mm_cfg.refresh_seconds
            deadline = min(next_scan, next_mm) if self.mm_enabled else next_scan
            remaining = deadline - now
            # Sleep in small steps so Ctrl+C / SIGTERM is responsive.
            while not _stop and remaining > 0:
                step = min(1.0, remaining)
                time.sleep(step)
                remaining -= step
            now = max(now, deadline)
        self.shutdown()

    def shutdown(self) -> None:
        if self.quotes is not None:
            _guarded(self.notifier, "market maker", self.quotes.cancel_all, "bot stopping")
            # Record any fills that landed before the cancels did.
            _guarded(self.notifier, "market maker", self.quotes.poll_fills, [], self.feed.book)
        if self.heartbeat is not None:
            self.heartbeat.stop()
        if self.feed is not None:
            self.feed.stop()
        logger.info("Bot stopped.")
        self.notifier.send(f"🔴 Bot berhenti ({self.mode.upper()}) — {_pnl_summary(self.risk)}")
        self.notifier.close()
        self.store.close()

    def _settle_args(self):
        return self.client, self.risk, self.journal, self.store, self.notifier, self.mode

    def _reconcile_args(self):
        return self.settings.wallet.funder_address, self.risk, self.notifier

    # -- scan cycle ----------------------------------------------------------------
    def scan_cycle(self) -> None:
        cycle_start = time.time()
        self.books.new_cycle()
        market_count = 0
        try:
            markets = []
            for market in iter_active_markets(self.client, self.settings.markets):
                if _stop:
                    break
                markets.append(market)
            market_count = len(markets)
            scan_tokens = [t.token_id for m in markets for t in m.tokens]
            if self.feed is not None:
                self.feed.set_assets(
                    _ws_assets(self.risk, self._mm_tokens(), scan_tokens, self.settings.market_data.max_ws_assets)
                )
            self.books.prefetch(scan_tokens)
            if self.mm_enabled and time.time() - self.last_mm_select >= self.mm_cfg.reselect_minutes * 60:
                self._select_mm_markets(markets)
            for market in markets:
                if _stop:
                    break
                for strategy in self.strategies:
                    self.executor.execute_signals(strategy.generate_signals(market, self.books.top))
            if self.negrisk is not None and not _stop:
                self._scan_negrisk_events()
            self.risk.update_marks(_marks_for_positions(self.risk, self.books.top))
            logger.info(
                "Cycle complete: scanned %d markets | open_positions=%d | "
                "exposure=$%.2f | realized_pnl_today=$%.2f | unrealized_pnl=$%.2f",
                market_count,
                len(self.risk.positions),
                self.risk.total_exposure_usd,
                self.risk.realized_pnl_today,
                self.risk.unrealized_pnl,
            )
        except Exception as exc:
            logger.exception("Unhandled error during scan cycle; continuing")
            self.notifier.send(
                f"⚠️ Error di siklus scan ({type(exc).__name__}) — bot tetap jalan, cek logs/bot.log",
                key="cycle_error",
                cooldown_s=ERROR_ALERT_COOLDOWN_SECONDS,
            )

        self.empty_scans = self.empty_scans + 1 if market_count == 0 and not _stop else 0
        if self.empty_scans >= EMPTY_SCAN_ALERT_CYCLES:
            self.notifier.send(
                f"⚠️ {self.empty_scans} siklus berturut-turut tidak ada market yang bisa dipindai — "
                "API Polymarket tidak bisa diakses, atau filter `markets:` terlalu ketat. Cek logs/bot.log",
                key="empty_scan",
                cooldown_s=3600,
            )

        self._check_kill_switch()
        _guarded(
            self.notifier, "merge", _handle_complete_sets, self.settings, self.risk, self.journal, self.store, self.notifier, self.mode
        )
        if time.time() - self.last_settle >= SETTLE_INTERVAL_SECONDS:
            self.last_settle = time.time()
            _guarded(self.notifier, "settlement", _settle_resolved, *self._settle_args())
        if self.live and time.time() - self.last_reconcile >= RECONCILE_INTERVAL_SECONDS:
            self.last_reconcile = time.time()
            _guarded(self.notifier, "reconcile", _reconcile_positions, *self._reconcile_args())
        heartbeat_seconds = self.settings.notifications.heartbeat_hours * 3600
        if heartbeat_seconds > 0 and time.time() - self.last_heartbeat_msg >= heartbeat_seconds:
            self.last_heartbeat_msg = time.time()
            self.notifier.send(f"💓 Bot masih jalan ({self.mode.upper()}) — {_pnl_summary(self.risk)}{self._mm_summary()}")

        self.cycles += 1
        try:
            _write_status(
                self.mode,
                self.risk,
                self.cycles,
                time.time() - cycle_start,
                market_count,
                self.feed,
                self._mm_status(),
            )
        except OSError:
            logger.exception("Failed to write %s", STATUS_PATH)

    def _scan_negrisk_events(self) -> None:
        cfg = self.settings.negrisk_arbitrage
        if time.time() - self.last_negrisk_fetch >= cfg.refresh_minutes * 60:
            self.last_negrisk_fetch = time.time()
            try:
                self.negrisk_events = fetch_negrisk_events(max_events=cfg.max_events, max_outcomes=cfg.max_outcomes)
            except Exception as exc:
                logger.warning("Fetching multi-outcome events failed (%s); keeping the previous list", type(exc).__name__)
        self.books.prefetch([m.tokens[0].token_id for event in self.negrisk_events for m in event.outcomes])
        for event in self.negrisk_events:
            if _stop:
                break
            self.executor.execute_signals(self.negrisk.generate_signals(event, self.books.top))

    def _check_kill_switch(self) -> None:
        if self.risk.daily_loss_limit_hit:
            logger.warning(
                "Daily loss limit hit (realized_pnl_today=$%.2f, unrealized_pnl=$%.2f) — "
                "no new positions until P&L recovers or UTC midnight.",
                self.risk.realized_pnl_today,
                self.risk.unrealized_pnl,
            )
            if not self.kill_switch_on:
                self.notifier.send(
                    f"⛔ Batas rugi harian tercapai — bot berhenti membuka posisi baru.\n{_pnl_summary(self.risk)}"
                )
            self.kill_switch_on = True
        elif self.kill_switch_on:
            self.kill_switch_on = False
            self.notifier.send(f"✅ Batas rugi harian tidak aktif lagi — bot boleh membuka posisi.\n{_pnl_summary(self.risk)}")

    # -- market making ---------------------------------------------------------------
    def _start_market_making(self) -> None:
        if self.live:
            # Orders left by an earlier run aren't tracked; start from a clean book.
            logger.info("Market making: cancelling any open orders left on the account")
            self.quotes.cancel_all("startup cleanup")
            self.heartbeat.start()
        logger.info("Market making enabled (%s)", self.mode)

    def _mm_tokens(self) -> list[str]:
        return [t.token_id for m in self.mm_markets for t in m.tokens]

    def _select_mm_markets(self, markets: list[MarketInfo]) -> None:
        self.last_mm_select = time.time()
        # Markets holding legs of a multi-outcome set stay out: quoting there
        # would mix inventory into the set's positions.
        in_sets = {p.market_id for p in self.risk.positions.values() if p.set_id is not None}
        chosen = select_markets(
            [m for m in markets if m.condition_id not in in_sets],
            self.books.book,
            self.mm_cfg,
            datetime.now(timezone.utc),
            self.risk.cfg.max_position_usd,
        )
        chosen_ids = {m.condition_id for m in chosen}
        for market in self.mm_markets:
            if market.condition_id not in chosen_ids:
                self.quotes.cancel_market(market.condition_id, "no longer selected")
        if [m.condition_id for m in chosen] != [m.condition_id for m in self.mm_markets]:
            names = "\n".join(f"• {m.question[:80]} (${m.rewards.daily_rate:.0f}/hari)" for m in chosen)
            logger.info("Market making in %d markets: %s", len(chosen), ", ".join(m.condition_id for m in chosen))
            self.notifier.send(
                f"📊 [{self.mode.upper()}] Market making di {len(chosen)} market:\n{names}"
                if chosen
                else f"📊 [{self.mode.upper()}] Tidak ada market reward yang cocok untuk market making saat ini."
            )
        self.mm_markets = chosen
        if self.feed is not None:
            self.feed.set_assets(
                _ws_assets(
                    self.risk,
                    self._mm_tokens(),
                    [t.token_id for m in markets for t in m.tokens],
                    self.settings.market_data.max_ws_assets,
                )
            )

    def mm_tick(self) -> None:
        """Refresh fills and quotes for the selected markets."""
        if self.risk.daily_loss_limit_hit:
            if self.quotes.orders:
                self.quotes.cancel_all("daily loss limit")
            return
        if not self.feed.is_live():
            if self.quotes.orders:
                self.quotes.cancel_all("market data feed is down")
            return
        if self.heartbeat is not None and not self.heartbeat.healthy:
            # The exchange cancels everything when heartbeats lapse; don't quote blind.
            self.quotes.poll_fills([], self.feed.book)
            return

        self.quotes.poll_fills(self.feed.drain_trades(), self.feed.book)
        now = time.time()
        for market in list(self.mm_markets):
            market_id = market.condition_id
            reason = skip_reason(market, self.mm_cfg, datetime.now(timezone.utc))
            if reason is not None:
                self.quotes.cancel_market(market_id, reason)
                self.mm_markets.remove(market)
                continue
            if now < self.cooldown_until.get(market_id, 0.0):
                continue
            book = self.feed.book(market.tokens[0].token_id)
            if book is None or book.mid is None:
                self.quotes.cancel_market(market_id, "no live book")
                continue
            move = self.mids.add(market_id, book.mid, now)
            if move > self.mm_cfg.max_mid_move:
                self.quotes.cancel_market(market_id, f"midpoint moved {move:.3f} — cooling down")
                self.cooldown_until[market_id] = now + self.mm_cfg.cooldown_minutes * 60
                self.mids.reset(market_id)
                continue
            inventory = {
                t.token_id: self.risk.positions[t.token_id].size for t in market.tokens if t.token_id in self.risk.positions
            }
            self.quotes.sync(market, compute_quotes(market, book, inventory, self.mm_cfg), self.feed.book)

    def _mm_status(self) -> dict:
        if not self.mm_enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "markets": [m.condition_id for m in self.mm_markets],
            "open_quotes": len(self.quotes.orders),
            "fills_today": self.quotes.fills_today,
            "maker_volume_today_usd": round(self.quotes.volume_today, 4),
            "heartbeat_ok": self.heartbeat.healthy if self.heartbeat is not None else None,
        }

    def _mm_summary(self) -> str:
        if not self.mm_enabled:
            return ""
        return (
            f"\nMarket making: {len(self.mm_markets)} market, {len(self.quotes.orders)} quote aktif, "
            f"{self.quotes.fills_today} fill hari ini (${self.quotes.volume_today:.2f})"
        )


def run() -> None:
    settings = load_settings()
    setup_logging()
    live = settings.wallet.live_trading

    logger.info("Starting Polymarket bot | live_trading=%s", live)
    if live:
        logger.warning(
            "*** LIVE TRADING ENABLED *** Real orders will be placed with real funds. "
            "Ctrl+C to stop between cycles."
        )
    else:
        logger.info("Running in PAPER TRADING mode — no real orders will be sent.")

    bot = Bot(settings)
    signal_module.signal(signal_module.SIGINT, _request_stop)
    signal_module.signal(signal_module.SIGTERM, _request_stop)
    bot.run()


if __name__ == "__main__":
    run()
