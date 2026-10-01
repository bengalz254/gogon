"""The main loop: discover migrations, watch, filter, paper trade, record.

Single-threaded apart from the WebSocket and Telegram threads. `tick()` is
one pass; `run()` repeats it until stopped. All time comes from `clock`, so
tests can drive whole token lifecycles in simulated time.
"""
from __future__ import annotations

import collections
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from migbot import __version__
from migbot import notifier as texts
from migbot.config import Settings
from migbot.filters import FilterResult, dip_check, market_checks, safety_checks
from migbot.http import HttpError
from migbot.models import GmgnInfo, MarketSnapshot, MigrationEvent, RugcheckReport, SafetyReport
from migbot.notifier import Notifier
from migbot.sources.dexscreener import DexScreenerSource
from migbot.sources.geckoterminal import GeckoTerminalSource
from migbot.sources.gmgn import GmgnSource
from migbot.sources.jupiter import JupiterSource
from migbot.sources.pumpportal import PumpPortalFeed
from migbot.sources.rugcheck import RugcheckSource
from migbot.sources.solana_rpc import SolanaRpc
from migbot.storage import TRADE_FIELDS, CsvJournal, LongSampleLog, PathLog, read_csv, read_json, write_json_atomic
from migbot.tracker import BOUGHT, NO_DATA, PASSED, REJECTED, WATCHING, TrackedToken, research_row, token_fields
from migbot.trading import DUST_FRACTION, NoFill, PaperBroker, Position, RiskManager, evaluate_exit, utc_day

logger = logging.getLogger("migbot.engine")

STATE_VERSION = 1
DONE_MEMORY = 2000
EXTRA_CACHE_S = 300  # RugCheck / GMGN results are reused this long
MERGE_WINDOW_S = 120  # two sources' times for one migration are this close
PATH_MAX_POINTS = 1500  # price points kept per token (2 hours of 10 s refreshes is 720)
# A buy is cancelled when Jupiter's price (costs included, normally about +5%) is this far from
# DexScreener's: the price is moving faster than the data, and the exits would fire on stale prices.
QUOTE_GAP_MIN, QUOTE_GAP_MAX = -0.15, 0.25
LONG_BATCH = 150  # tokens per tick in the multi-day tracking (5 DexScreener calls), so a tick stays short
JUNK_SOURCE = "geckoterminal"  # seen only by GeckoTerminal: a new pool for an old token, not a migration


def long_candidates(tokens_csv: str, now: float, max_age_s: float) -> dict[str, dict]:
    """The real migrations in tokens.csv younger than max_age_s, as multi-day tracking entries."""
    found: dict[str, dict] = {}
    for row in read_csv(tokens_csv):
        mint = row.get("mint") or ""
        if not mint or mint in found or (row.get("source") or JUNK_SOURCE) == JUNK_SOURCE:
            continue
        try:
            migrated_at = datetime.strptime(row["migrated_at_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except (KeyError, ValueError):
            continue
        if 0 <= now - migrated_at < max_age_s:
            found[mint] = {"migrated_at": migrated_at, "misses": 0}
    return found


def dashboard_reset_state(raw, tokens_csv: str, s: Settings, now: float) -> dict:
    """state.json for `migbot reset --simpan-lama`: the paper balance, positions, counters and events
    start over, but the tokens being watched and the multi-day tracking list carry on."""
    raw = raw if isinstance(raw, dict) and raw.get("version") == STATE_VERSION else {}
    tokens = []
    for item in raw.get("tokens", []):
        tok = TrackedToken.from_dict(item)
        tok.trade_pnl_sol = tok.trade_pnl_pct = None
        if tok.status == BOUGHT:  # its trade stays in the archived trades.csv
            tok.status = PASSED
        tokens.append(tok.to_dict())
    long = {m: dict(v) for m, v in (raw.get("long") or {}).items() if isinstance(v, dict)}
    backfilled = bool(raw.get("long_backfilled"))
    if s.long_tracking.enabled and not backfilled:  # fill it now: tokens.csv is about to be archived
        watching = {t["mint"] for t in tokens}
        for mint, item in long_candidates(tokens_csv, now, s.long_tracking.max_days * 86_400).items():
            if mint not in long and mint not in watching:
                long[mint] = item
        backfilled = True
    return {
        "version": STATE_VERSION,
        "saved_at": now,
        "balance_sol": s.trading.paper_balance_sol,
        "tokens": tokens,
        "positions": [],
        "done": raw.get("done", []),
        "recent": [],
        "long": long,
        "long_backfilled": backfilled,
    }


@dataclass
class Sources:
    pumpportal: PumpPortalFeed | None = None
    gecko: GeckoTerminalSource | None = None
    dexscreener: DexScreenerSource | None = None
    rpc: SolanaRpc | None = None
    rugcheck: RugcheckSource | None = None
    gmgn: GmgnSource | None = None
    jupiter: JupiterSource | None = None

    @classmethod
    def build(cls, s: Settings) -> "Sources":
        d, sf = s.discovery, s.safety
        return cls(
            pumpportal=PumpPortalFeed(d.pumpportal.ws_url, s.pumpportal_api_key, d.pumpportal.reconnect_after_silence_minutes)
            if d.pumpportal.enabled
            else None,
            gecko=GeckoTerminalSource(d.geckoterminal.base_url, d.geckoterminal.dexes, d.mint_suffixes)
            if d.geckoterminal.enabled
            else None,
            dexscreener=DexScreenerSource(s.market.dexscreener_url),
            rpc=SolanaRpc(s.rpc_url),
            rugcheck=RugcheckSource(sf.rugcheck.base_url) if sf.rugcheck.enabled else None,
            gmgn=GmgnSource(sf.gmgn.base_url, sf.gmgn.blocked_retry_minutes) if sf.gmgn.enabled else None,
            jupiter=JupiterSource(s.costs.jupiter_url, s.jupiter_api_key) if s.costs.use_jupiter else None,
        )

    def health(self) -> list[dict]:
        out = []
        if self.pumpportal is not None:
            out.append(self.pumpportal.snapshot())
        for src in (self.gecko, self.dexscreener, self.rpc, self.rugcheck, self.gmgn, self.jupiter):
            if src is not None:
                out.append(src.http.health.snapshot())
        return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class DayStats:
    day: str
    migrations: int = 0
    bought: int = 0
    rejected: int = 0
    closed: int = 0
    wins: int = 0
    realized_sol: float = 0.0
    late: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Engine:
    def __init__(self, settings: Settings, sources: Sources, notifier: Notifier | None = None, clock=time.time):
        self.s = settings
        self.src = sources
        self.notify = notifier or Notifier(enabled=False)
        self.clock = clock
        self.broker = PaperBroker(settings.costs, sources.jupiter)
        os.makedirs(settings.data_dir, exist_ok=True)
        self.state_path = os.path.join(settings.data_dir, "state.json")
        self.status_path = os.path.join(settings.data_dir, "status.json")
        self.tokens_journal = CsvJournal(
            os.path.join(settings.data_dir, "tokens.csv"), token_fields(settings.tracking.checkpoints_minutes)
        )
        self.trades_journal = CsvJournal(os.path.join(settings.data_dir, "trades.csv"), TRADE_FIELDS)
        self.path_log = PathLog(os.path.join(settings.data_dir, "paths.jsonl.gz"))
        self.paths: dict[str, list[list]] = {}  # in memory only; written when a token's tracking ends
        self.long_log = LongSampleLog(os.path.join(settings.data_dir, "long_samples.csv.gz"))
        self.long: dict[str, dict] = {}  # mint -> {"migrated_at", "misses"}: followed for days after the first 2 h
        self.long_backfilled = False
        self._long_queue: list[str] = []
        self._next_long_round = 0.0
        self.tokens: dict[str, TrackedToken] = {}
        self.positions: dict[str, Position] = {}
        self.done: collections.OrderedDict[str, float] = collections.OrderedDict()
        self.balance = settings.trading.paper_balance_sol
        self.recent: collections.deque[dict] = collections.deque(maxlen=60)
        now = clock()
        self.started_at = now
        self.stats = DayStats(utc_day(now))
        self.risk = RiskManager(settings.trading, now=now)
        self._next_gecko = 0.0
        self._next_market = 0.0
        self._next_save = 0.0
        self._dirty = False
        self._stop = False
        self._warned: dict[str, float] = {}
        self._load(now)

    # ------------------------------------------------------------------ state
    def _load(self, now: float) -> None:
        raw = read_json(self.state_path)
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            return
        self.balance = float(raw.get("balance_sol", self.balance))
        for item in raw.get("tokens", []):
            tok = TrackedToken.from_dict(item)
            self.tokens[tok.mint] = tok
        for item in raw.get("positions", []):
            pos = Position.from_dict(item)
            self.positions[pos.mint] = pos
        for mint in raw.get("done", [])[-DONE_MEMORY:]:
            self.done[mint] = 0.0
        self.risk = RiskManager(self.s.trading, raw.get("risk"), now=now)
        saved_stats = raw.get("stats") or {}
        if saved_stats.get("day") == utc_day(now):
            self.stats = DayStats(**{k: v for k, v in saved_stats.items() if k in DayStats.__dataclass_fields__})
        for item in raw.get("recent", [])[-60:]:
            self.recent.append(item)
        self.long = {m: dict(v) for m, v in (raw.get("long") or {}).items() if isinstance(v, dict)}
        self.long_backfilled = bool(raw.get("long_backfilled"))
        logger.info(
            "Restored state: %d tokens, %d open positions, balance %.4f SOL",
            len(self.tokens), len(self.positions), self.balance,
        )

    def save(self) -> None:
        write_json_atomic(
            self.state_path,
            {
                "version": STATE_VERSION,
                "saved_at": self.clock(),
                "balance_sol": self.balance,
                "tokens": [t.to_dict() for t in self.tokens.values()],
                "positions": [p.to_dict() for p in self.positions.values()],
                "done": list(self.done.keys())[-DONE_MEMORY:],
                "risk": self.risk.to_dict(),
                "stats": self.stats.to_dict(),
                "recent": list(self.recent),
                "long": self.long,
                "long_backfilled": self.long_backfilled,
            },
        )
        self._dirty = False

    def _event(self, now: float, kind: str, text: str, mint: str = "") -> None:
        self.recent.append({"t": now, "kind": kind, "text": text, "mint": mint})
        self._dirty = True

    def _warn_once(self, key: str, message: str, now: float, every_s: float = 3600) -> None:
        if now - self._warned.get(key, -1e12) >= every_s:
            self._warned[key] = now
            logger.warning(message)
            self.notify.send(f"⚠️ {message}")

    # ------------------------------------------------------------------ discovery
    def register(self, ev: MigrationEvent, now: float) -> bool:
        # A migration time in the future (clock skew between us and a source) would keep
        # the token waiting forever; it cannot have happened later than now.
        migrated_at = min(ev.migrated_at, now)
        tok = self.tokens.get(ev.mint)
        if tok is not None:
            if ev.source not in tok.sources:
                tok.sources.append(ev.source)
                # Same migration seen by a second source: take its (earlier, on-chain) time, but
                # only when close; a much older pool for this mint is a different event.
                if 0 < tok.migrated_at - migrated_at <= MERGE_WINDOW_S:
                    tok.migrated_at = migrated_at
                self._dirty = True
            return False
        if ev.mint in self.done or ev.mint in self.positions:
            return False
        suffixes = self.s.discovery.mint_suffixes
        if suffixes and not any(ev.mint.endswith(sfx) for sfx in suffixes):
            return False
        if now - migrated_at > self.s.discovery.max_age_on_discovery_seconds:
            self.stats.late += 1
            self._remember_done(ev.mint, now)
            return False
        tok = TrackedToken(
            mint=ev.mint,
            source=ev.source,
            sources=[ev.source],
            migrated_at=migrated_at,
            detected_at=now,
            symbol=ev.symbol,
            name=ev.name,
            pool=ev.pool,
            dex=ev.dex,
            signature=ev.signature,
            decimals=ev.decimals,
        )
        self.tokens[ev.mint] = tok
        self.stats.migrations += 1
        self._event(now, "migrasi", f"Migrasi {tok.label} ({ev.source})", ev.mint)
        logger.info("Migration %s via %s (%.0fs after migration)", ev.mint, ev.source, now - migrated_at)
        if self.s.notify.notify_migrations:
            self.notify.send(texts.migration_text(tok))
        return True

    def _discover(self, now: float) -> None:
        events: list[MigrationEvent] = []
        if self.src.pumpportal is not None:
            events.extend(self.src.pumpportal.drain())
        if self.src.gecko is not None and now >= self._next_gecko:
            self._next_gecko = now + self.s.discovery.geckoterminal.poll_seconds
            try:
                events.extend(self.src.gecko.poll())
            except Exception as exc:  # noqa: BLE001 - one bad poll must not stop the bot
                logger.warning("GeckoTerminal poll failed: %s", exc)
        for ev in sorted(events, key=lambda e: e.migrated_at):
            self.register(ev, now)

    # ------------------------------------------------------------------ market data
    def _checkpoint_tolerance(self) -> float:
        """How late a checkpoint price may be recorded (a refresh or two)."""
        return max(30.0, 3 * self.s.market.refresh_seconds)

    def _record_point(self, tok: TrackedToken, snap: MarketSnapshot, now: float) -> None:
        if not snap.price_usd:
            return
        points = self.paths.setdefault(tok.mint, [])
        if len(points) < PATH_MAX_POINTS:
            whole = [None if v is None else int(v) for v in (snap.liquidity_usd, snap.market_cap_usd, snap.volume_m5)]
            points.append([round(now - tok.migrated_at), float(f"{snap.price_usd:.6g}"), *whole, snap.buys_m5, snap.sells_m5])

    def _write_path(self, tok: TrackedToken) -> None:
        points = self.paths.pop(tok.mint, None)
        if not points:
            return
        record = {"mint": tok.mint, "symbol": tok.symbol, "migrated_at": tok.migrated_at, "status": tok.status, "points": points}
        try:
            self.path_log.append(record)
        except OSError as exc:
            logger.warning("Could not write the price path of %s: %s", tok.mint, exc)

    def _refresh_market(self, now: float) -> None:
        mints = [m for m in self.tokens] + [m for m in self.positions if m not in self.tokens]
        if not mints or self.src.dexscreener is None:
            return
        known = {m: t.pair_address for m, t in self.tokens.items() if t.pair_address}
        known.update({m: p.pair_address for m, p in self.positions.items()})
        try:
            snaps = self.src.dexscreener.fetch(mints, known)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DexScreener refresh failed: %s", exc)
            return
        tolerance = self._checkpoint_tolerance()
        for mint, snap in snaps.items():
            tok = self.tokens.get(mint)
            if tok is not None:
                if not tok.pair_address:
                    tok.pair_address = snap.pair_address
                tok.symbol = tok.symbol or snap.symbol
                tok.name = tok.name or snap.name
                if tok.first_mcap_usd is None and snap.market_cap_usd:
                    tok.first_mcap_usd = snap.market_cap_usd
                tok.last = snap.to_dict()
                self._record_point(tok, snap, now)
                tok.observe_price(
                    snap.price_usd, now, self.s.entry.delay_seconds, self.s.tracking.checkpoints_minutes, tolerance
                )
                if tok.ref_at == now and not tok.ref_snapshot:
                    tok.ref_snapshot = dict(tok.last)
            pos = self.positions.get(mint)
            if pos is not None and snap.price_native:
                pos.last_price_native = snap.price_native
                pos.last_price_at = now
                pos.peak_price_native = max(pos.peak_price_native, snap.price_native)
                pos.low_price_native = min(pos.low_price_native or snap.price_native, snap.price_native)
                pos.last_liquidity_usd = snap.liquidity_usd
                pos.last_mcap_usd = snap.market_cap_usd
        self._dirty = True

    # ------------------------------------------------------------------ entries
    def _safety(
        self, tok: TrackedToken, now: float, research: bool = False, refresh: bool = True
    ) -> tuple[SafetyReport | None, RugcheckReport | None, GmgnInfo | None]:
        sf = self.s.safety
        if refresh and self.src.rpc is not None and (tok.safety is None or now - tok.safety_at >= sf.refresh_seconds):
            creator = (tok.safety or {}).get("creator")
            count_holders = research or self.s.filters.min_holders > 0
            try:
                report = self.src.rpc.safety_report(
                    tok.mint, tok.pair_address, sf.amm_owners, creator, count_holders=count_holders
                )
                tok.safety, tok.safety_at = report.to_dict(), now
                if count_holders and self.src.rpc.holders_supported is False:
                    self._warn_once(
                        "holders-unsupported",
                        "RPC ini tidak bisa menghitung holder (butuh Helius di SOLANA_RPC_URL); filter jumlah holder dilewati",
                        now, 6 * 3600,
                    )
                if report.decimals is not None:
                    tok.decimals = report.decimals
                if report.errors:
                    logger.info("Safety data incomplete for %s: %s", tok.mint, "; ".join(report.errors))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Safety check failed for %s: %s", tok.mint, exc)
        if refresh and self.src.rugcheck is not None and (tok.rugcheck is None or now - tok.rugcheck_at >= EXTRA_CACHE_S):
            try:
                tok.rugcheck, tok.rugcheck_at = self.src.rugcheck.fetch(tok.mint).to_dict(), now
            except Exception as exc:  # noqa: BLE001
                tok.rugcheck_at = now - EXTRA_CACHE_S + 60  # retry in a minute
                logger.info("RugCheck unavailable for %s: %s", tok.mint, exc)
        gm = self.src.gmgn
        if refresh and gm is not None and gm.available and (tok.gmgn is None or now - tok.gmgn_at >= EXTRA_CACHE_S):
            try:
                tok.gmgn, tok.gmgn_at = gm.fetch(tok.mint).to_dict(), now
            except HttpError as exc:
                tok.gmgn_at = now
                if exc.blocked:
                    self._warn_once("gmgn-blocked", "GMGN memblokir request dari server ini; filter GMGN dilewati sementara", now, 6 * 3600)
            except Exception as exc:  # noqa: BLE001
                tok.gmgn_at = now
                logger.info("GMGN unavailable for %s: %s", tok.mint, exc)
        safety = SafetyReport.from_dict(tok.safety) if tok.safety else None
        rug = RugcheckReport.from_dict(tok.rugcheck) if tok.rugcheck else None
        gmgn = GmgnInfo.from_dict(tok.gmgn) if tok.gmgn else None
        return safety, rug, gmgn

    def _reject(self, tok: TrackedToken, now: float) -> None:
        tok.status = PASSED if tok.passed_filters else REJECTED if tok.last else NO_DATA
        tok.decided_at = now
        tok.decision_snapshot = dict(tok.last)
        if not tok.last:
            tok.reasons = ["tidak ada data pasar (DexScreener) selama jendela beli"]
        self.stats.rejected += 1
        verb = "lolos filter tapi tidak dibeli" if tok.passed_filters else "tidak dibeli"
        self._event(now, "tolak", f"{tok.label} {verb}: {'; '.join(tok.reasons[:3])}", tok.mint)
        if self.s.notify.notify_rejections:
            self.notify.send(texts.rejection_text(tok))

    def _unrealized(self) -> float:
        return sum(p.unrealized_sol() for p in self.positions.values())

    def _evaluate_entries(self, now: float) -> None:
        e = self.s.entry
        fresh_s = 2.5 * self.s.market.refresh_seconds
        for tok in list(self.tokens.values()):
            if tok.status != WATCHING:
                continue
            age = now - tok.migrated_at
            if age < e.delay_seconds:
                continue
            if age > e.window_seconds:
                self._reject(tok, now)
                continue
            snap_ts = tok.last.get("ts", 0.0) if tok.last else 0.0
            if not tok.last or now - snap_ts > fresh_s or snap_ts <= tok.last_eval_ts:
                continue  # wait for a new snapshot
            tok.last_eval_ts = snap_ts
            tok.evaluations += 1
            if tok.safety_first is None and self.src.rpc is not None:
                # Research: holder data for EVERY token at the start of the buy window, not only for
                # the few that pass the market checks, so `migbot analyze` can compare them all.
                self._safety(tok, now, research=True)
                if tok.safety:
                    tok.safety_first = dict(tok.safety)
            snap = MarketSnapshot.from_dict(tok.last)
            checks = []
            if e.dip_pct > 0:
                checks.append(dip_check(snap.price_usd, tok.peak_price_usd, tok.dip_low_usd, e.dip_pct, e.dip_bounce_pct))
            checks.extend(market_checks(snap, tok.first_price_usd, self.s.filters))
            result = FilterResult(checks)
            if result.passed:
                # Research mode buys nothing: judge on the data already there instead of paying for new checks.
                safety, rug, gmgn = self._safety(tok, now, refresh=self.s.trading.enabled)
                result.checks.extend(safety_checks(safety, rug, gmgn, self.s.filters, self.s.safety.rugcheck))
            tok.checks = [c.to_dict() for c in result.checks]
            tok.reasons = result.failures
            self._dirty = True
            if not result.passed:
                continue
            tok.passed_filters = True
            fee = self.s.costs.priority_fee_sol
            blocked = self.risk.check_buy(self.balance, len(self.positions), self._unrealized(), fee)
            if blocked:
                tok.reasons = [f"risiko: {blocked}"]
                continue
            self._buy(tok, snap, now)

    def _buy(self, tok: TrackedToken, snap: MarketSnapshot, now: float) -> None:
        try:
            fill = self.broker.buy(tok.mint, self.s.trading.buy_sol, snap, tok.decimals)
        except NoFill as exc:
            tok.reasons = [f"beli gagal: {exc}"]
            return
        if fill.method == "jupiter" and snap.price_native:
            gap = fill.price_native / snap.price_native - 1
            if not QUOTE_GAP_MIN <= gap <= QUOTE_GAP_MAX:
                tok.reasons = [f"beli batal: harga Jupiter {gap * 100:+.0f}% dari DexScreener (harga bergerak terlalu cepat)"]
                return
        decimals = fill.decimals
        self.balance -= fill.sol
        pos = Position(
            mint=tok.mint,
            symbol=tok.symbol or snap.symbol or tok.mint[:6],
            pair_address=tok.pair_address,
            opened_at=now,
            cost_sol=fill.sol,
            tokens_raw_initial=fill.tokens_raw,
            tokens_raw=fill.tokens_raw,
            decimals=decimals,
            entry_price_native=fill.price_native,
            entry_price_usd=snap.price_usd,
            entry_mcap_usd=snap.market_cap_usd,
            liquidity_at_entry=snap.liquidity_usd,
            peak_price_native=snap.price_native or fill.price_native,
            last_price_native=snap.price_native,
            last_price_at=now,
            method=fill.method,
            last_liquidity_usd=snap.liquidity_usd,
            last_mcap_usd=snap.market_cap_usd,
            low_price_native=snap.price_native or fill.price_native,
            migrated_at=tok.migrated_at,
        )
        self.positions[tok.mint] = pos
        tok.status = BOUGHT
        tok.decided_at = now
        tok.decision_snapshot = dict(tok.last)
        tok.reasons = []
        self.risk.record_buy()
        self.stats.bought += 1
        self.trades_journal.append(
            {
                "time_utc": _iso(now), "mint": tok.mint, "symbol": pos.symbol, "side": "BUY",
                "reason": "beli saat dip" if self.s.entry.dip_pct > 0 else "lolos filter",
                "sol": f"{fill.sol:.6f}", "tokens": f"{fill.tokens_raw / 10**decimals:.4f}",
                "price_native": f"{fill.price_native:.12f}", "price_usd": snap.price_usd, "mcap_usd": snap.market_cap_usd,
                "method": fill.method, "impact_pct": "" if fill.impact_pct is None else f"{fill.impact_pct:.2f}",
                "fee_sol": f"{fill.fee_sol:.6f}", "balance_sol": f"{self.balance:.6f}",
                "entry_age_min": pos.diagnostics(now)["entry_age_min"],
            }
        )
        self._event(now, "beli", f"BELI {tok.label} {fill.sol:.4f} SOL ({fill.method})", tok.mint)
        logger.info("PAPER BUY %s %.4f SOL -> %d raw tokens (%s)", tok.mint, fill.sol, fill.tokens_raw, fill.method)
        self.notify.send(texts.buy_text(tok, pos, fill, snap))
        self.save()

    # ------------------------------------------------------------------ exits
    def _manage_positions(self, now: float) -> None:
        for mint, pos in list(self.positions.items()):
            decision = evaluate_exit(pos, pos.last_price_native, pos.last_liquidity_usd, now, self.s.exits)
            if decision is None:
                continue
            snap = None
            tok = self.tokens.get(mint)
            if tok is not None and tok.last:
                snap = MarketSnapshot.from_dict(tok.last)
            self._sell(pos, decision.tokens_raw, decision.reason, decision.tp_index, snap, now)

    def _sell(self, pos: Position, tokens_raw: int, reason: str, tp_index, snap, now: float) -> None:
        tokens_raw = min(tokens_raw, pos.tokens_raw)
        if pos.tokens_raw - tokens_raw < pos.tokens_raw_initial * DUST_FRACTION:
            tokens_raw = pos.tokens_raw
        fill = self.broker.sell(pos.mint, tokens_raw, pos.decimals, snap)
        cost_part = pos.cost_sol * tokens_raw / pos.tokens_raw_initial
        pnl = fill.sol - cost_part
        pnl_pct = pnl / cost_part * 100 if cost_part else 0.0
        pos.tokens_raw -= tokens_raw
        pos.proceeds_sol += fill.sol
        pos.realized_sol += pnl
        if tp_index is not None:
            pos.tp_done.append(tp_index)
        self.balance += fill.sol
        self.risk.record_realized(pnl)
        self.stats.realized_sol += pnl
        closed = pos.tokens_raw <= 0
        position_pnl = pos.proceeds_sol - pos.cost_sol
        self.trades_journal.append(
            {
                "time_utc": _iso(now), "mint": pos.mint, "symbol": pos.symbol, "side": "SELL", "reason": reason,
                "sol": f"{fill.sol:.6f}", "tokens": f"{tokens_raw / 10**pos.decimals:.4f}",
                "price_native": f"{fill.price_native:.12f}", "price_usd": snap.price_usd if snap else "",
                "mcap_usd": snap.market_cap_usd if snap else "", "method": fill.method,
                "impact_pct": "" if fill.impact_pct is None else f"{fill.impact_pct:.2f}",
                "fee_sol": f"{fill.fee_sol:.6f}", "pnl_sol": f"{pnl:.6f}", "pnl_pct": f"{pnl_pct:.2f}",
                "position_pnl_sol": f"{position_pnl:.6f}" if closed else "", "balance_sol": f"{self.balance:.6f}",
                **(pos.diagnostics(now) if closed else {}),
            }
        )
        label = f"${pos.symbol}"
        self._event(now, "jual", f"{'TUTUP' if closed else 'JUAL'} {label} {reason}: {pnl:+.4f} SOL", pos.mint)
        logger.info("PAPER SELL %s %s: %.4f SOL (pnl %+.4f)", pos.mint, reason, fill.sol, pnl)
        self.notify.send(texts.sell_text(pos, fill, reason, pnl, pnl_pct, closed))
        if closed:
            del self.positions[pos.mint]
            self.stats.closed += 1
            if position_pnl > 0:
                self.stats.wins += 1
            tok = self.tokens.get(pos.mint)
            if tok is not None:
                tok.trade_pnl_sol = position_pnl
                tok.trade_pnl_pct = position_pnl / pos.cost_sol * 100 if pos.cost_sol else None
        self.save()

    # ------------------------------------------------------------------ bookkeeping
    def _finalize(self, now: float) -> None:
        # The grace period lets the last checkpoint (due exactly at the horizon) be recorded first.
        horizon = self.s.tracking.track_minutes * 60 + self._checkpoint_tolerance()
        for mint, tok in list(self.tokens.items()):
            if now - tok.migrated_at < horizon or mint in self.positions:
                continue
            if tok.status == WATCHING:
                self._reject(tok, now)
            self.tokens_journal.append(research_row(tok, self.s.tracking.checkpoints_minutes))
            self._write_path(tok)
            self._start_long(tok)
            del self.tokens[mint]
            self._remember_done(mint, now)
            self._dirty = True

    # ------------------------------------------------------------------ multi-day tracking
    def _start_long(self, tok: TrackedToken) -> None:
        lt = self.s.long_tracking
        if not lt.enabled or (tok.sources or [tok.source]) == [JUNK_SOURCE]:
            return
        if (tok.last.get("liquidity_usd") or 0) >= lt.alive_liquidity_usd:
            self.long[tok.mint] = {"migrated_at": tok.migrated_at, "misses": 0}

    def _backfill_long(self, now: float) -> None:
        """Once: follow the real migrations of the last days already in tokens.csv (from before this version)."""
        self.long_backfilled = True
        self._dirty = True
        found = long_candidates(self.tokens_journal.path, now, self.s.long_tracking.max_days * 86_400)
        for mint, item in found.items():
            if mint not in self.long and mint not in self.tokens:
                self.long[mint] = item
        logger.info("Multi-day tracking: %d tokens", len(self.long))

    def _long_tick(self, now: float) -> None:
        """Every sample_minutes, sample every followed token, a batch per tick."""
        lt = self.s.long_tracking
        if not lt.enabled or self.src.dexscreener is None:
            return
        if not self.long_backfilled:
            self._backfill_long(now)
        if not self._long_queue:
            if now < self._next_long_round or not self.long:
                return
            self._next_long_round = now + lt.sample_minutes * 60
            self._long_queue = list(self.long)
        batch, self._long_queue = self._long_queue[:LONG_BATCH], self._long_queue[LONG_BATCH:]
        try:
            snaps = self.src.dexscreener.fetch(batch)
        except Exception as exc:  # noqa: BLE001 - try again next round
            logger.warning("Multi-day tracking fetch failed: %s", exc)
            return
        rows = []
        for mint in batch:
            item = self.long.get(mint)
            if item is None:
                continue
            age_min = round((now - item["migrated_at"]) / 60)
            snap = snaps.get(mint)
            if snap is not None and snap.price_usd:
                whole = [None if v is None else int(v) for v in (snap.liquidity_usd, snap.market_cap_usd, snap.volume_h1)]
                rows.append([int(now), mint, age_min, float(f"{snap.price_usd:.6g}"), *whole, snap.buys_h1, snap.sells_h1])
            alive = snap is not None and snap.price_usd and (snap.liquidity_usd or 0) >= lt.alive_liquidity_usd
            item["misses"] = 0 if alive else item["misses"] + 1
            if item["misses"] >= lt.dead_after:
                rows.append([int(now), mint, age_min, 0, 0, 0, 0, 0, 0])  # dropped as dead
                del self.long[mint]
            elif now - item["migrated_at"] >= lt.max_days * 86_400:
                del self.long[mint]
        if rows:
            try:
                self.long_log.append(rows)
            except OSError as exc:
                logger.warning("Could not write multi-day samples: %s", exc)
        self._dirty = True

    def _remember_done(self, mint: str, now: float) -> None:
        self.done[mint] = now
        while len(self.done) > DONE_MEMORY:
            self.done.popitem(last=False)

    def _roll_day(self, now: float) -> None:
        if not self.risk.roll(now):
            return
        old = self.stats
        if self.s.notify.daily_summary:
            self.notify.send(texts.summary_text(old.day, old.to_dict(), self.balance, len(self.positions), old.realized_sol))
        self.stats = DayStats(utc_day(now))
        self._dirty = True

    def _check_feeds(self, now: float) -> None:
        pp = self.src.pumpportal
        if pp is not None and not pp.connected and now - self.started_at > 120:
            backup = "deteksi migrasi hanya dari GeckoTerminal" if self.src.gecko is not None else "migrasi baru tidak terdeteksi"
            self._warn_once("pumpportal-down", f"PumpPortal tidak tersambung; {backup}", now)
        ds = self.src.dexscreener
        if ds is not None and ds.http.health.consecutive_errors >= 10:
            self._warn_once("dexscreener-down", f"DexScreener gagal terus: {ds.http.health.last_error}", now)
        rpc = self.src.rpc
        if rpc is not None and rpc.http.health.consecutive_errors >= 10:
            self._warn_once("rpc-down", f"Solana RPC gagal terus: {rpc.http.health.last_error} (isi SOLANA_RPC_URL)", now)

    def status(self, now: float) -> dict:
        watching = sorted(self.tokens.values(), key=lambda t: t.migrated_at, reverse=True)
        return {
            "version": __version__,
            "mode": "PAPER",
            "running": not self._stop,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "updated_at": now,
            "balance_sol": self.balance,
            "start_balance_sol": self.s.trading.paper_balance_sol,
            "equity_sol": self.balance + sum(p.value_sol() for p in self.positions.values()),
            "unrealized_sol": self._unrealized(),
            "risk": {
                **self.risk.to_dict(),
                "halted": self.risk.halted(self._unrealized()),
                "max_daily_loss_sol": self.s.trading.max_daily_loss_sol,
                "max_open_positions": self.s.trading.max_open_positions,
                "max_buys_per_day": self.s.trading.max_buys_per_day,
            },
            "today": self.stats.to_dict(),
            "positions": [
                {
                    **p.to_dict(),
                    "value_sol": p.value_sol(),
                    "pnl_sol": p.total_pnl_sol(),
                    "pnl_pct": p.total_pnl_sol() / p.cost_sol * 100 if p.cost_sol else None,
                    "change_pct": (p.last_price_native / p.entry_price_native - 1) * 100 if p.last_price_native else None,
                }
                for p in self.positions.values()
            ],
            "tokens": [
                {
                    "mint": t.mint,
                    "symbol": t.symbol,
                    "name": t.name,
                    "label": t.label,
                    "source": "+".join(t.sources),
                    "status": t.status,
                    "migrated_at": t.migrated_at,
                    "age_s": now - t.migrated_at,
                    "pair_address": t.pair_address,
                    "last": t.last,
                    "reasons": t.reasons,
                    "checks": t.checks,
                    # distance from the high since the migration (how close to a dip buy)
                    "change_pct": (t.last.get("price_usd") / t.peak_price_usd - 1) * 100
                    if t.last.get("price_usd") and t.peak_price_usd
                    else None,
                    "trade_pnl_sol": t.trade_pnl_sol,
                }
                for t in watching
            ],
            "recent": list(self.recent)[::-1],
            "feeds": self.src.health(),
            "telegram": {"enabled": self.notify.enabled, "sent": self.notify.sent, "last_error": self.notify.last_error},
            "settings": {
                "entry_delay_s": self.s.entry.delay_seconds,
                "entry_window_s": self.s.entry.window_seconds,
                "track_minutes": self.s.tracking.track_minutes,
                "buy_sol": self.s.trading.buy_sol,
                "trading_enabled": self.s.trading.enabled,
                "long_tracked": len(self.long),
                "long_max_days": self.s.long_tracking.max_days if self.s.long_tracking.enabled else 0,
                "config_path": self.s.config_path,
            },
        }

    # ------------------------------------------------------------------ loop
    def tick(self) -> None:
        now = self.clock()
        self._roll_day(now)
        self._discover(now)
        if now >= self._next_market:
            self._next_market = now + self.s.market.refresh_seconds
            self._refresh_market(now)
        self._manage_positions(now)  # exits before entries: protecting open positions comes first
        self._evaluate_entries(now)
        self._finalize(now)
        self._long_tick(now)
        self._check_feeds(now)
        if self._dirty and now >= self._next_save:
            self.save()
            self._next_save = now + 15
        write_json_atomic(self.status_path, self.status(now))

    def stop(self, *_args) -> None:
        self._stop = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        if self.src.pumpportal is not None:
            self.src.pumpportal.start()
        logger.info("migbot %s started (PAPER), data in %s", __version__, self.s.data_dir)
        self.notify.send(texts.start_text("PAPER", self.balance, self.s))
        while not self._stop:
            started = time.monotonic()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep running, the log has the traceback
                logger.exception("Error in main loop")
            remaining = self.s.poll_seconds - (time.monotonic() - started)
            while remaining > 0 and not self._stop:
                step = min(0.5, remaining)
                time.sleep(step)
                remaining -= step
        logger.info("Stopping")
        if self.src.pumpportal is not None:
            self.src.pumpportal.stop()
        self.save()
        final = self.status(self.clock())
        final["running"] = False
        write_json_atomic(self.status_path, final)
        self.notify.send("🔴 Bot migrated berhenti. Posisi paper yang terbuka disimpan dan dilanjutkan saat start lagi.")
        self.notify.close()
