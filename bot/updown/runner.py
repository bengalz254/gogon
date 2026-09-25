"""Asyncio shell around the engine: feeds in, orders out.

    RTDS (Chainlink [+ CEX relay]) ─┐
    Binance/Bybit (optional) ───────┤
    CLOB market websocket ──────────┼──> emit(event) ──> Engine.handle ──┐
    Gamma discovery / resolution ───┘        │                          │
                                        recorder (opt.)                 │
    step loop (every ~250ms): Engine.step(now) -> actions ─> broker ─> OrderUpdate ─> Engine
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time

from bot.config import WalletConfig
from bot.updown.config import UpDownConfig
from bot.updown.discovery import GammaClient, build_slug, discover_window, parse_price_to_beat, parse_resolution, slot_starts
from bot.updown.engine import Engine
from bot.updown.events import FeedStatus, OfficialPriceToBeat, OfficialResolution, WindowListed
from bot.updown.feeds.cex import BinanceFeed, BybitFeed, bootstrap_klines
from bot.updown.feeds.clob_ws import ClobMarketFeed
from bot.updown.feeds.rtds import build_rtds_feeds
from bot.updown.journal import UpDownJournal
from bot.updown.orders import CancelOrder, OrderUpdate, PaperBroker, PlaceOrder
from bot.updown.recorder import EventRecorder
from bot.updown.risk import UpDownRisk
from bot.updown.strategies import build_strategies

logger = logging.getLogger("polybot.updown.runner")


def server_clock_offset(clob_host: str) -> float | None:
    """CLOB server time minus local time, in seconds (None if unreachable)."""
    import requests

    try:
        resp = requests.get(f"{clob_host.rstrip('/')}/time", timeout=5)
        resp.raise_for_status()
        return float(resp.text.strip().strip('"')) - time.time()
    except Exception:
        return None


class Runner:
    def __init__(self, cfg: UpDownConfig, wallet: WalletConfig, record: bool = False):
        self.cfg = cfg
        self.wallet = wallet
        self.live = wallet.live_trading
        self.mode = "live" if self.live else "paper"
        self.stop = asyncio.Event()
        self.risk = UpDownRisk(cfg.risk, now=time.time())
        self.journal = UpDownJournal(cfg.journal.dir, cfg.journal.trades_csv, mode=self.mode)
        strategies = build_strategies(cfg.strategies)
        self.engine = Engine(cfg, strategies, self.risk, self.journal, mode=self.mode)
        self.recorder = EventRecorder(cfg.journal.record_dir) if (record or cfg.journal.record_events) else None
        self.gamma = GammaClient(cfg.feeds.gamma_url)
        self.paper = None
        self.broker = None
        self._exec_queue: asyncio.Queue = asyncio.Queue()
        self._known_slugs: set = set()
        self._missed: dict = {}
        self._ptb_polled: dict = {}
        self._res_polled: dict = {}
        self.clob: ClobMarketFeed | None = None
        self.feeds: list = []
        self.started_at = time.time()
        self.status_path = os.path.join(cfg.journal.dir, "updown_status.json")
        logger.info("Strategies enabled: %s", ", ".join(s.name for s in strategies) or "(none)")

    # -- events in ------------------------------------------------------------------
    def emit(self, ev, arrived: float | None = None) -> None:
        """Feed one event to the engine. `arrived` overrides the arrival time the
        engine uses for freshness (historical backfill must not look live)."""
        now = time.time()
        if self.recorder is not None:
            self.recorder.record(ev, now)
        self.engine.handle(ev, now if arrived is None else arrived)
        if self.paper is not None:
            for u in self.paper.on_event(ev, now):
                self.engine.on_order_update(u)

    def _feed_status(self, feed: str, connected: bool, detail: str) -> None:
        self.emit(FeedStatus(feed, time.time(), connected, detail))

    # -- lifecycle --------------------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:  # Windows
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self.stop.set))

        if self.live:
            from bot.client import build_client
            from bot.updown.broker_live import LiveBroker

            client = await asyncio.to_thread(build_client, self.wallet)
            self.broker = LiveBroker(client, self.engine.fees)
            offset = await asyncio.to_thread(self.broker.server_time_offset)
        else:
            self.paper = PaperBroker(self.engine)
            offset = await asyncio.to_thread(server_clock_offset, self.wallet.clob_host)
        if offset is not None and abs(offset) > 2:
            logger.warning(
                "Local clock is %.0fs %s the CLOB server clock. Window timing depends on it: sync the "
                "clock (Windows: Settings > Time & language > Date & time > Sync now).",
                abs(offset), "behind" if offset > 0 else "ahead of",
            )

        assets = self.cfg.markets.assets
        chainlink_map = {self.cfg.markets.chainlink_symbol(a): a for a in assets}
        cex_map = {self.cfg.markets.cex_symbol(a): a for a in assets}
        source = self.cfg.feeds.cex_source

        if self.cfg.feeds.bootstrap_history and source != "none":
            ticks = await asyncio.to_thread(
                bootstrap_klines, cex_map, self.cfg.feeds.binance_rest_url, self.cfg.feeds.bybit_rest_url
            )
            for ev in ticks:
                self.emit(ev, arrived=ev.ts)
            logger.info("Bootstrapped %d kline closes for %d assets", len(ticks), len({e.asset for e in ticks}))

        feeds = build_rtds_feeds(
            self.cfg.feeds.rtds_url, chainlink_map, cex_map if source == "rtds" else {}, self.emit,
            ping_s=self.cfg.feeds.rtds_ping_s, stale_s=self.cfg.feeds.rtds_stale_s, on_status=self._feed_status,
        )
        if source == "binance":
            feeds.append(BinanceFeed(self.cfg.feeds.binance_ws_url, cex_map, self.emit,
                                     self.cfg.feeds.cex_throttle_ms, on_status=self._feed_status))
        elif source == "bybit":
            feeds.append(BybitFeed(self.cfg.feeds.bybit_ws_url, cex_map, self.emit,
                                   self.cfg.feeds.cex_throttle_ms, on_status=self._feed_status))
        self.clob = ClobMarketFeed(self.cfg.feeds.clob_ws_url, self.emit, self.cfg.feeds.clob_ping_s,
                                   self.cfg.feeds.dynamic_subscribe, on_status=self._feed_status)
        feeds.append(self.clob)

        self.feeds = feeds
        tasks = [asyncio.create_task(f.run(), name=f"feed-{f.name}") for f in feeds]
        tasks += [
            asyncio.create_task(self._step_loop(), name="step"),
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._resolution_loop(), name="resolution"),
            asyncio.create_task(self._status_loop(), name="status"),
            asyncio.create_task(self._status_file_loop(), name="status-file"),
        ]
        if self.live:
            tasks += [
                asyncio.create_task(self._executor(), name="executor"),
                asyncio.create_task(self._poll_orders_loop(), name="order-poll"),
            ]
            if self.cfg.execution.heartbeat:
                tasks.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))

        logger.info("Up/Down engine running in %s mode (Ctrl+C to stop)", self.mode.upper())
        await self.stop.wait()
        logger.info("Stopping: cancelling resting orders...")
        await self._shutdown(tasks)

    async def _shutdown(self, tasks) -> None:
        if self.live and self.broker is not None:
            ids = [st.exchange_id for st in self.engine.orders.live() if st.exchange_id]
            await asyncio.to_thread(self.broker.cancel_many, ids)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.recorder is not None:
            self.recorder.close()
        self.journal.close()
        logger.info("Up/Down engine stopped. %s", self.engine.status_line())

    # -- loops --------------------------------------------------------------------------
    async def _step_loop(self) -> None:
        interval = self.cfg.execution.step_interval_s
        while True:
            now = time.time()
            try:
                actions = self.engine.step(now)
                if actions:
                    if self.paper is not None:
                        for u in self.paper.execute(actions, now):
                            self.engine.on_order_update(u)
                    else:
                        self._exec_queue.put_nowait(actions)
                if self.clob is not None:
                    await self.clob.set_tokens(self.engine.active_tokens())
            except Exception:
                logger.exception("Engine step failed; continuing")
            await asyncio.sleep(max(0.0, interval - (time.time() - now)))

    async def _executor(self) -> None:
        """Live mode: execute action batches strictly in order (a requote's
        cancel lands before its replacement is placed)."""
        while True:
            actions = await self._exec_queue.get()
            for a in actions:
                try:
                    if isinstance(a, PlaceOrder):
                        update = await asyncio.to_thread(self.broker.submit, a.req)
                        self.engine.on_order_update(update)
                    elif isinstance(a, CancelOrder):
                        st = self.engine.orders.get(a.client_id)
                        if st is None or st.final:
                            continue
                        if not st.exchange_id:
                            self.engine.on_order_update(OrderUpdate(a.client_id, "cancelled", final=True, message="never acknowledged"))
                            continue
                        update = await asyncio.to_thread(self.broker.cancel, a.client_id, st.exchange_id)
                        self.engine.on_order_update(update)
                except Exception:
                    logger.exception("Failed to execute %s", a)

    async def _poll_orders_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.execution.live_order_poll_s)
            for st in list(self.engine.orders.live()):
                if not st.exchange_id:
                    continue
                update = await asyncio.to_thread(self.broker.poll, st)
                if update is not None:
                    self.engine.on_order_update(update)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.broker.heartbeat)
            await asyncio.sleep(5.0)

    async def _discovery_loop(self) -> None:
        m = self.cfg.markets
        interval = m.interval_seconds
        while True:
            now = time.time()
            for start in slot_starts(now, interval, m.discover_ahead):
                if start + interval <= now + 5:
                    continue
                for asset in m.assets:
                    slug = build_slug(m.slug_template, asset, m.interval, start)
                    if slug in self._known_slugs or now - self._missed.get(slug, -1e9) < 60:
                        continue
                    try:
                        spec = await asyncio.to_thread(
                            discover_window, self.gamma, slug=slug, asset=asset, interval_s=interval, start=start
                        )
                    except Exception as e:
                        logger.warning("Gamma lookup failed for %s: %s", slug, e)
                        spec = None
                    if spec is None:
                        self._missed[slug] = now
                        continue
                    self._known_slugs.add(slug)
                    self.emit(WindowListed(spec))
                    if self.live and self.cfg.execution.prewarm:
                        asyncio.create_task(asyncio.to_thread(self.broker.prewarm, [spec.up_token, spec.down_token]))
            self._missed = {s: t for s, t in self._missed.items() if now - t < 3600}
            await asyncio.sleep(self.cfg.markets.discovery_interval_s)

    async def _resolution_loop(self) -> None:
        """Official price_to_beat (early in a window) and official outcomes."""
        while True:
            await asyncio.sleep(5.0)
            now = time.time()
            for w in self.engine.windows_needing_ptb(now):
                if now - self._ptb_polled.get(w.window_id, 0) < 10:
                    continue
                self._ptb_polled[w.window_id] = now
                try:
                    payload = await asyncio.to_thread(self.gamma.lookup, w.window_id)
                except Exception:
                    continue
                ptb = parse_price_to_beat(payload or {})
                if ptb:
                    self.emit(OfficialPriceToBeat(w.window_id, ptb))
            for w in self.engine.windows_needing_resolution(now)[:10]:
                if now - self._res_polled.get(w.window_id, 0) < self.cfg.settlement.official_poll_s:
                    continue
                self._res_polled[w.window_id] = now
                try:
                    payload = await asyncio.to_thread(self.gamma.market_by_slug, w.window_id)
                    payload = payload or await asyncio.to_thread(self.gamma.event_by_slug, w.window_id)
                except Exception:
                    continue
                winner = parse_resolution(payload or {})
                if winner:
                    self.emit(OfficialResolution(w.window_id, winner))

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(30.0)
            logger.info("STATUS %s", self.engine.status_line())

    def write_status(self) -> None:
        """Snapshot for scripts/updown_dashboard.py (atomic replace)."""
        now = time.time()
        snap = self.engine.status_snapshot(now)
        snap.update(mode=self.mode, started_at=self.started_at, feeds=[f.status(now) for f in self.feeds])
        tmp = self.status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, separators=(",", ":"))
        try:
            os.replace(tmp, self.status_path)
        except PermissionError:
            pass  # Windows: the dashboard is reading it right now; next second will do

    async def _status_file_loop(self) -> None:
        warned = False
        while True:
            await asyncio.sleep(1.0)
            try:
                self.write_status()
            except Exception:
                if not warned:
                    warned = True
                    logger.exception("Could not write %s (dashboard data)", self.status_path)
