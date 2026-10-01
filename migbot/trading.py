"""Paper positions, simulated fills, exit rules and risk limits.

Fills are priced from a Jupiter quote for the real size (pool fee and price
impact included) plus `extra_slippage_pct`, or, without Jupiter, from the
DexScreener price minus an estimated fee and slippage. A priority fee is
charged on every transaction. Nothing here touches a wallet.
"""
from __future__ import annotations

import dataclasses
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from migbot.config import SOL_MINT, CostConfig, ExitConfig, TradingConfig
from migbot.models import MarketSnapshot
from migbot.sources.jupiter import JupiterSource, NoRoute

LAMPORTS = 1_000_000_000
DUST_FRACTION = 0.01  # a remainder below 1% of the original position is sold with the last sale


class NoFill(RuntimeError):
    pass


@dataclass
class Fill:
    side: str  # "BUY" | "SELL"
    sol: float  # BUY: SOL spent incl. fee; SELL: SOL received after fee
    tokens_raw: int
    price_native: float  # effective SOL per whole token
    fee_sol: float
    method: str  # "jupiter" | "estimasi" | "tanpa harga"
    impact_pct: float | None = None
    route: str = ""
    decimals: int = 6


def infer_decimals(tokens_raw: int, sol_amount: float, price_native: float | None) -> int | None:
    """Token decimals implied by a quote and the market price (whole tokens = SOL / price)."""
    if not price_native or tokens_raw <= 0:
        return None
    whole = sol_amount / price_native
    decimals = round(math.log10(tokens_raw / whole))
    return decimals if 0 <= decimals <= 12 else None


@dataclass
class Position:
    mint: str
    symbol: str
    pair_address: str
    opened_at: float
    cost_sol: float
    tokens_raw_initial: int
    tokens_raw: int
    decimals: int
    entry_price_native: float
    entry_price_usd: float | None
    entry_mcap_usd: float | None
    liquidity_at_entry: float | None
    peak_price_native: float
    last_price_native: float | None
    last_price_at: float
    method: str
    tp_done: list[int] = field(default_factory=list)
    proceeds_sol: float = 0.0
    realized_sol: float = 0.0
    last_liquidity_usd: float | None = None
    last_mcap_usd: float | None = None
    low_price_native: float | None = None  # lowest price while held
    migrated_at: float | None = None

    @property
    def tokens_ui(self) -> float:
        return self.tokens_raw / 10**self.decimals

    def value_sol(self, price_native: float | None = None) -> float:
        price = price_native if price_native is not None else self.last_price_native
        return self.tokens_ui * price if price else 0.0

    def unrealized_sol(self) -> float:
        """Mark-to-market P&L of the part still held (before exit costs)."""
        held_cost = self.cost_sol * self.tokens_raw / self.tokens_raw_initial
        return self.value_sol() - held_cost

    def total_pnl_sol(self) -> float:
        return self.proceeds_sol + self.value_sol() - self.cost_sol

    def diagnostics(self, now: float) -> dict:
        """How the price moved while held: the best and worst point vs the entry, and the timing."""
        def pct(price):
            return "" if not price else f"{(price / self.entry_price_native - 1) * 100:.1f}"

        return {
            "peak_pct": pct(self.peak_price_native),
            "low_pct": pct(self.low_price_native),
            "held_min": f"{(now - self.opened_at) / 60:.1f}",
            "entry_age_min": "" if self.migrated_at is None else f"{(self.opened_at - self.migrated_at) / 60:.1f}",
        }

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Position":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})


class PaperBroker:
    def __init__(self, costs: CostConfig, jupiter: JupiterSource | None = None):
        self.costs = costs
        self.jupiter = jupiter if costs.use_jupiter else None

    def _jupiter_quote(self, input_mint: str, output_mint: str, amount: int):
        if self.jupiter is None:
            return None
        try:
            return self.jupiter.quote(input_mint, output_mint, amount)
        except NoRoute:
            raise
        except Exception:  # noqa: BLE001 - network trouble: fall back to the estimate
            return None

    def buy(self, mint: str, sol_amount: float, snap: MarketSnapshot | None, decimals: int | None) -> Fill:
        """`decimals=None` (unknown): taken from the quote vs the market price, else 6 (pump.fun)."""
        fee = self.costs.priority_fee_sol
        keep = 1 - self.costs.extra_slippage_pct / 100
        try:
            quote = self._jupiter_quote(SOL_MINT, mint, int(sol_amount * LAMPORTS))
        except NoRoute as exc:
            raise NoFill(f"Jupiter: tidak ada rute beli ({exc})") from exc
        if quote is not None:
            if decimals is None:
                decimals = infer_decimals(quote.out_amount, sol_amount, snap.price_native if snap else None)
            tokens_raw = int(quote.out_amount * keep)
            method, impact, route = "jupiter", quote.price_impact_pct, quote.route
        else:
            if snap is None or not snap.price_native:
                raise NoFill("tidak ada harga untuk simulasi beli")
            decimals = 6 if decimals is None else decimals
            keep_est = 1 - (self.costs.est_swap_fee_pct + self.costs.est_slippage_pct) / 100
            tokens_raw = int(sol_amount / snap.price_native * keep_est * 10**decimals)
            method, impact, route = "estimasi", None, ""
        decimals = 6 if decimals is None else decimals
        if tokens_raw <= 0:
            raise NoFill("jumlah token hasil beli 0")
        spent = sol_amount + fee
        return Fill("BUY", spent, tokens_raw, spent / (tokens_raw / 10**decimals), fee, method, impact, route, decimals)

    def sell(self, mint: str, tokens_raw: int, decimals: int, snap: MarketSnapshot | None) -> Fill:
        fee = self.costs.priority_fee_sol
        tokens_ui = tokens_raw / 10**decimals
        try:
            quote = self._jupiter_quote(mint, SOL_MINT, tokens_raw)
        except NoRoute:
            quote = None  # pool drained or not indexed: fall back to the estimate below
        if quote is not None:
            gross = quote.out_amount / LAMPORTS * (1 - self.costs.extra_slippage_pct / 100)
            method, impact, route = "jupiter", quote.price_impact_pct, quote.route
        elif snap is not None and snap.price_native:
            keep_est = 1 - (self.costs.est_swap_fee_pct + self.costs.est_slippage_pct) / 100
            gross = tokens_ui * snap.price_native * keep_est
            method, impact, route = "estimasi", None, ""
        else:
            return Fill("SELL", 0.0, tokens_raw, 0.0, 0.0, "tanpa harga")
        net = max(0.0, gross - fee)
        return Fill("SELL", net, tokens_raw, net / tokens_ui if tokens_ui else 0.0, fee, method, impact, route)


@dataclass
class ExitDecision:
    tokens_raw: int
    reason: str
    tp_index: int | None = None


def evaluate_exit(
    pos: Position, price_native: float | None, liquidity_usd: float | None, now: float, cfg: ExitConfig
) -> ExitDecision | None:
    """At most one exit action per call; the most protective rule wins."""
    everything = pos.tokens_raw
    if now - pos.last_price_at > cfg.no_data_exit_minutes * 60:
        return ExitDecision(everything, "data harga hilang")
    if price_native is None:
        return None
    ratio = price_native / pos.entry_price_native
    if (
        cfg.liquidity_drop_pct > 0
        and pos.liquidity_at_entry
        and liquidity_usd is not None
        and liquidity_usd < pos.liquidity_at_entry * (1 - cfg.liquidity_drop_pct / 100)
    ):
        return ExitDecision(everything, "likuiditas anjlok")
    if ratio <= 1 - cfg.stop_loss_pct / 100:
        return ExitDecision(everything, "stop loss")
    peak_ratio = pos.peak_price_native / pos.entry_price_native
    if cfg.breakeven_after_pct > 0 and peak_ratio >= 1 + cfg.breakeven_after_pct / 100 and ratio <= 1.0:
        return ExitDecision(everything, "stop impas")
    if cfg.max_hold_minutes > 0 and now - pos.opened_at >= cfg.max_hold_minutes * 60:
        return ExitDecision(everything, "waktu habis")
    if cfg.trailing_pct > 0:
        if peak_ratio >= 1 + cfg.trailing_start_pct / 100 and price_native <= pos.peak_price_native * (
            1 - cfg.trailing_pct / 100
        ):
            return ExitDecision(everything, "trailing stop")
    for i, (gain, fraction) in enumerate(cfg.take_profit):
        if i in pos.tp_done or ratio < 1 + gain / 100:
            continue
        amount = min(everything, int(math.floor(pos.tokens_raw_initial * fraction)))
        if everything - amount < pos.tokens_raw_initial * DUST_FRACTION:
            amount = everything
        if amount <= 0:
            continue
        return ExitDecision(amount, f"take profit +{gain:.0f}%", i)
    return None


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, cfg: TradingConfig, saved: dict | None = None, now: float | None = None):
        self.cfg = cfg
        saved = saved or {}
        now = time.time() if now is None else now
        self.day = saved.get("day") or utc_day(now)
        self.realized_today = float(saved.get("realized_today", 0.0))
        self.buys_today = int(saved.get("buys_today", 0))
        self.roll(now)

    def roll(self, now: float) -> bool:
        """Start a new UTC day; returns True when the day changed."""
        day = utc_day(now)
        if day == self.day:
            return False
        self.day, self.realized_today, self.buys_today = day, 0.0, 0
        return True

    def loss_today(self, unrealized_open: float) -> float:
        return self.realized_today + min(0.0, unrealized_open)

    def halted(self, unrealized_open: float) -> bool:
        return self.loss_today(unrealized_open) <= -self.cfg.max_daily_loss_sol

    def check_buy(self, balance: float, open_count: int, unrealized_open: float, fee: float) -> str | None:
        if not self.cfg.enabled:
            return "pembelian dimatikan (mode riset)"
        if self.halted(unrealized_open):
            return f"batas rugi harian {self.cfg.max_daily_loss_sol:g} SOL tercapai (sampai 00:00 UTC)"
        if open_count >= self.cfg.max_open_positions:
            return f"sudah {open_count} posisi terbuka (maks {self.cfg.max_open_positions})"
        if self.buys_today >= self.cfg.max_buys_per_day:
            return f"sudah {self.buys_today} beli hari ini (maks {self.cfg.max_buys_per_day})"
        if balance < self.cfg.buy_sol + fee:
            return f"saldo paper {balance:.3f} SOL tidak cukup"
        return None

    def record_buy(self) -> None:
        self.buys_today += 1

    def record_realized(self, pnl_sol: float) -> None:
        self.realized_today += pnl_sol

    def to_dict(self) -> dict:
        return {"day": self.day, "realized_today": self.realized_today, "buys_today": self.buys_today}
