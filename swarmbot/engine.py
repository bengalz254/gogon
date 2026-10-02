"""Main loop: scan tokens, label their mood, buy shocked/happy/calm, sell at TP or SL."""
from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter

from swarmbot.config import Config
from swarmbot.jupiter import Jupiter, JupiterError
from swarmbot.moods import Token, classify, parse_token
from swarmbot.paper import Paper

log = logging.getLogger("swarmbot")


class Engine:
    def __init__(self, cfg: Config, jupiter: Jupiter | None = None, paper: Paper | None = None, clock=time.time):
        self.cfg = cfg
        self.jup = jupiter or Jupiter(cfg.jupiter_base_url, cfg.jupiter_api_key)
        self.paper = paper or Paper(cfg.data_dir, cfg.starting_cash_usd, cfg.fee_pct)
        self.clock = clock
        self.stopping = False
        self.last_scan_at = 0.0
        self.last_scan_ok = False
        self.mood_counts: dict[str, int] = {}
        self.candidates: list[dict] = []
        self.buy_block = ""

    # -- scanning -----------------------------------------------------------
    def scan(self) -> list[tuple[Token, str]]:
        seen: dict[str, Token] = {}
        failures = 0
        for name in self.cfg.jupiter_lists:
            try:
                rows = self.jup.token_list(name, self.cfg.jupiter_limit)
            except JupiterError as exc:
                failures += 1
                log.warning("%s", exc)
                continue
            for raw in rows:
                t = parse_token(raw)
                if t and t.mint not in seen:
                    seen[t.mint] = t
        if failures == len(self.cfg.jupiter_lists):
            raise JupiterError("semua daftar token Jupiter gagal dibaca")
        labelled = [(t, classify(t, self.cfg.rules)) for t in seen.values()]
        counts = Counter(m for _, m in labelled)
        self.last_scan_at = self.clock()
        self.mood_counts = dict(counts)
        log.info("scan: %d token | %s", len(labelled), " ".join(f"{m}={n}" for m, n in counts.most_common()))
        return labelled

    def eligible(self, t: Token, mood: str) -> str:
        """Why this token is skipped, or '' when it may be bought."""
        now = self.clock()
        c = self.cfg
        if mood not in c.buy_moods:
            return "mood"
        if t.mint in self.paper.positions:
            return "sudah dipegang"
        if self.paper.cooldowns.get(t.mint, 0) > now:
            return "cooldown"
        if c.skip_suspicious and t.is_sus:
            return "ditandai mencurigakan oleh Jupiter"
        if t.price <= 0:
            return "tanpa harga"
        if c.min_liquidity_usd > 0 and t.liquidity < c.min_liquidity_usd:
            return "likuiditas kecil"
        if c.min_mcap_usd > 0 and t.mcap < c.min_mcap_usd:
            return "market cap kecil"
        if c.max_mcap_usd > 0 and t.mcap > c.max_mcap_usd:
            return "market cap besar (bukan token launch)"
        return ""

    def try_buys(self, labelled: list[tuple[Token, str]]) -> int:
        c = self.cfg
        order = {m: i for i, m in enumerate(c.buy_moods)}
        picks = [(t, m) for t, m in labelled if not self.eligible(t, m)]
        picks.sort(key=lambda tm: (order[tm[1]], -tm[0].liquidity))
        bought = 0
        self.buy_block = ""
        for t, mood in picks:
            now = self.clock()
            recent = [x for x in self.paper.buy_times if x > now - 3600]
            if len(self.paper.positions) >= c.max_open_positions:
                self.buy_block = f"slot penuh ({len(self.paper.positions)}/{c.max_open_positions})"
            elif c.max_buys_per_hour > 0 and len(recent) >= c.max_buys_per_hour:
                self.buy_block = f"batas {c.max_buys_per_hour} beli per jam tercapai"
            elif self.paper.cash < c.position_usd * 0.999:
                self.buy_block = f"saldo simulasi habis ({self.paper.cash:.2f} USD)"
            if self.buy_block:
                log.info("tidak beli %d kandidat: %s", len(picks) - bought, self.buy_block)
                break
            self.paper.buy(t.mint, t.symbol, mood, t.price, c.position_usd, now)
            bought += 1
            log.info("BELI (paper) %s mood=%s harga=%.10g liq=%.0f 5m=%+.1f%% 1h=%+.1f%% %s",
                     t.symbol, mood, t.price, t.liquidity, t.change_5m, t.change_1h, t.mint)
        return bought

    def remember_candidates(self, labelled: list[tuple[Token, str]], limit: int = 30) -> None:
        order = {m: i for i, m in enumerate(self.cfg.buy_moods)}
        rows = [(t, m) for t, m in labelled if m in order]
        rows.sort(key=lambda tm: (order[tm[1]], -tm[0].liquidity))
        self.candidates = [{
            "mint": t.mint, "symbol": t.symbol, "mood": m, "price": t.price,
            "change_5m": t.change_5m, "change_1h": t.change_1h,
            "liquidity": t.liquidity, "mcap": t.mcap,
            "skip": self.eligible(t, m),
        } for t, m in rows[:limit]]

    def write_status(self) -> None:
        """data_dir/status.json, read by the dashboard."""
        c = self.cfg
        now = self.clock()
        positions = []
        for p in self.paper.positions.values():
            price = p.last_price or p.entry_price
            positions.append({
                "mint": p.mint, "symbol": p.symbol, "mood": p.mood,
                "entry_price": p.entry_price, "price": price,
                "move_pct": (price / p.entry_price - 1) * 100,
                "cost_usd": p.cost_usd, "value_usd": p.qty * price * (1 - self.paper.fee),
                "opened_at": p.opened_at, "price_at": p.last_price_at,
                "price_age_s": now - p.last_price_at if p.last_price_at else None,
            })
        data = {
            "updated_at": now, "last_scan_at": self.last_scan_at, "last_scan_ok": self.last_scan_ok,
            "settings": {
                "buy_moods": c.buy_moods, "take_profit_pct": c.take_profit_pct,
                "stop_loss_pct": c.stop_loss_pct, "position_usd": c.position_usd,
                "starting_cash_usd": c.starting_cash_usd, "max_open_positions": c.max_open_positions,
            },
            "cash": self.paper.cash, "equity": self.paper.equity(),
            "buy_block": self.buy_block,
            "mood_counts": self.mood_counts, "candidates": self.candidates, "positions": positions,
        }
        path = self.paper.dir / "status.json"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("status.json gagal ditulis: %s", exc)

    # -- exits --------------------------------------------------------------
    def update_prices(self, prices: dict[str, float], fresher_than: float = 0) -> None:
        """fresher_than: skip positions whose price was updated less than this many seconds ago."""
        now = self.clock()
        for mint, pos in self.paper.positions.items():
            if fresher_than and now - pos.last_price_at < fresher_than:
                continue
            if prices.get(mint, 0) > 0:
                pos.last_price = prices[mint]
                pos.last_price_at = now
                if pos.anchor_price <= 0:
                    pos.anchor_price, pos.anchor_at = pos.entry_price, pos.opened_at
                if abs(pos.last_price / pos.anchor_price - 1) * 100 >= self.cfg.stale_move_pct:
                    pos.anchor_price, pos.anchor_at = pos.last_price, now

    def check_exits(self) -> int:
        c = self.cfg
        now = self.clock()
        sold = 0
        for mint, pos in list(self.paper.positions.items()):
            if pos.last_price <= 0:
                continue
            move = (pos.last_price / pos.entry_price - 1) * 100
            reason = ""
            if move >= c.take_profit_pct - 1e-9:
                reason = "TP"
            elif move <= -c.stop_loss_pct + 1e-9:
                reason = "SL"
            elif c.stale_minutes > 0 and now - (pos.anchor_at or pos.opened_at) >= c.stale_minutes * 60:
                reason = "diam"
            elif c.max_hold_minutes > 0 and now - pos.opened_at >= c.max_hold_minutes * 60:
                reason = f"{c.max_hold_minutes:g} menit"
            elif c.max_hold_hours > 0 and now - pos.opened_at >= c.max_hold_hours * 3600:
                reason = "waktu habis"
            if reason:
                pnl, pct = self.paper.sell(mint, pos.last_price, reason, c.cooldown_minutes * 60, now)
                sold += 1
                log.info("JUAL (paper) %s %s harga %+.1f%% | hasil %+.2f USD (%+.1f%% setelah fee) | saldo %.2f",
                         pos.symbol, reason, move, pnl, pct, self.paper.cash)
        return sold

    def refresh_positions(self) -> None:
        if not self.paper.positions:
            return
        mints = list(self.paper.positions)
        prices: dict[str, float] = {}
        try:
            prices = self.jup.prices(mints)
        except (JupiterError, AttributeError) as exc:
            log.warning("Price API gagal (%s); pakai data token", exc)
        missing = [m for m in mints if m not in prices]
        if missing:
            try:
                for raw in self.jup.tokens(missing):
                    t = parse_token(raw)
                    if t and t.price > 0:
                        prices[t.mint] = t.price
            except JupiterError as exc:
                log.warning("harga posisi gagal dibaca: %s", exc)
        self.update_prices(prices)
        self.check_exits()

    # -- loop ---------------------------------------------------------------
    def run(self) -> None:
        c = self.cfg
        log.info("swarmbot started | mode PAPER | beli mood %s | TP +%g%% SL -%g%% | %g USD per posisi | saldo %.2f USD | %d posisi terbuka",
                 ",".join(c.buy_moods), c.take_profit_pct, c.stop_loss_pct, c.position_usd,
                 self.paper.cash, len(self.paper.positions))
        next_scan = 0.0
        while not self.stopping:
            now = time.monotonic()
            if now >= next_scan:
                next_scan = now + c.poll_seconds
                try:
                    labelled = self.scan()
                except JupiterError as exc:
                    self.last_scan_ok = False
                    log.warning("scan gagal: %s (coba lagi nanti)", exc)
                else:
                    self.last_scan_ok = True
                    self.update_prices({t.mint: t.price for t, _ in labelled}, fresher_than=30)
                    self.check_exits()
                    self.try_buys(labelled)
                    self.remember_candidates(labelled)
            self.refresh_positions()
            self.write_status()
            end = time.monotonic() + c.price_check_seconds
            while not self.stopping and time.monotonic() < end:
                time.sleep(0.5)
        self.paper.save()
        log.info("swarmbot berhenti; state disimpan (%d posisi terbuka)", len(self.paper.positions))
