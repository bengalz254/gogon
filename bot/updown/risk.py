"""Risk layer for the Up/Down engine: sizing, caps and hard bans.

- Fractional Kelly sizing: f* = (p - c) / (1 - c) for a binary share
  bought at all-in cost c with win probability p.
- Caps: per trade, per window, per strategy-window, total exposure (open
  positions + resting bids + in-flight orders), max concurrent windows.
- Daily loss kill switch (UTC day), counting provisional losses of closed
  windows that are still waiting for the official resolution.
- Anti-martingale: after each consecutive loss a strategy's size is
  multiplied by loss_streak_decay (<= 1, enforced by config validation) and
  a long losing streak triggers a cooldown. Size never goes up after a loss.
- Hard ban from the design notes: no taker entries in the first
  `no_early_taker_s` seconds while the price is near 50c.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from bot.updown.config import RiskConfig

logger = logging.getLogger("polybot.updown.risk")


@dataclass
class SizeDecision:
    shares: float
    usd: float
    kelly: float
    reason: str = ""  # why it was rejected / clipped

    @property
    def ok(self) -> bool:
        return self.shares > 0


def kelly_fraction(p_win: float, cost: float) -> float:
    if cost >= 1.0 or cost <= 0.0:
        return 0.0
    return (p_win - cost) / (1.0 - cost)


class UpDownRisk:
    def __init__(self, cfg: RiskConfig, now: float | None = None):
        self.cfg = cfg
        self.realized_pnl_today = 0.0
        self.realized_pnl_total = 0.0
        self.pending_pnl = 0.0  # provisional P&L of closed, not-yet-booked windows
        self.loss_streak: dict[str, int] = {}
        self.cooldown_until: dict[str, float] = {}
        self._day = self._utc_day(now) if now is not None else None

    @staticmethod
    def _utc_day(ts: float):
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()

    def roll_day(self, now: float) -> None:
        day = self._utc_day(now)
        if self._day is None:
            self._day = day
        elif day != self._day:
            self._day = day
            self.realized_pnl_today = 0.0

    # -- state ---------------------------------------------------------------
    @property
    def daily_loss_hit(self) -> bool:
        worst = self.realized_pnl_today + min(0.0, self.pending_pnl)
        return worst <= -abs(self.cfg.max_daily_loss_usd)

    @property
    def bankroll(self) -> float:
        return max(0.0, self.cfg.bankroll_usd + self.realized_pnl_total)

    def size_multiplier(self, strategy: str) -> float:
        streak = self.loss_streak.get(strategy, 0)
        return min(1.0, self.cfg.loss_streak_decay) ** streak

    def blocked_reason(self, strategy: str, now: float) -> str | None:
        if self.daily_loss_hit:
            return (
                f"daily loss limit hit (realized ${self.realized_pnl_today:.2f}, "
                f"pending ${self.pending_pnl:.2f}); no new risk until UTC midnight"
            )
        until = self.cooldown_until.get(strategy, 0.0)
        if now < until:
            return f"{strategy} cooling down after {self.loss_streak.get(strategy, 0)} straight losses ({until - now:.0f}s left)"
        return None

    # -- checks & sizing -------------------------------------------------------
    def guard(self, *, taker: bool, price: float, elapsed: float) -> str | None:
        if price > self.cfg.max_buy_price + 1e-9:
            return f"price {price:.3f} above max_buy_price {self.cfg.max_buy_price}"
        if price < self.cfg.min_buy_price - 1e-9:
            return f"price {price:.3f} below min_buy_price {self.cfg.min_buy_price}"
        lo, hi = self.cfg.early_taker_band
        if taker and elapsed < self.cfg.no_early_taker_s and lo <= price <= hi:
            return (
                f"banned: taker entry {elapsed:.0f}s into the window at {price:.3f} "
                f"(near 50c, first {self.cfg.no_early_taker_s:.0f}s)"
            )
        return None

    def size(
        self,
        *,
        strategy: str,
        strategy_cfg,
        p_win: float,
        price: float,
        fee_per_share: float,
        taker: bool,
        elapsed: float,
        now: float,
        window_exposure: float,
        strategy_window_exposure: float,
        total_exposure: float,
        slot_direction_exposure: float,
        open_windows: int,
        window_is_open: bool,
        min_order_shares: float,
        max_shares: float | None = None,
        max_usd: float | None = None,
        fixed_size: bool = False,
    ) -> SizeDecision:
        """How many shares to buy (0 = rejected, with reason).

        fixed_size: the caller asks for exactly `max_shares` (non-directional
        legs); Kelly is skipped but every cap, ban and kill switch still applies.
        """
        blocked = self.blocked_reason(strategy, now)
        if blocked:
            return SizeDecision(0.0, 0.0, 0.0, blocked)
        banned = self.guard(taker=taker, price=price, elapsed=elapsed)
        if banned:
            return SizeDecision(0.0, 0.0, 0.0, banned)
        if not window_is_open and open_windows >= self.cfg.max_open_windows:
            return SizeDecision(0.0, 0.0, 0.0, f"max_open_windows ({self.cfg.max_open_windows}) reached")

        cost = price + fee_per_share
        f = kelly_fraction(p_win, cost)
        mult = self.size_multiplier(strategy) * float(getattr(strategy_cfg, "size_mult", 1.0))
        if fixed_size:
            if max_shares is None:
                return SizeDecision(0.0, 0.0, f, "fixed_size requires a share count")
            stake = max_shares * cost * mult
        else:
            if f <= 0:
                return SizeDecision(0.0, 0.0, f, f"no edge after costs (p={p_win:.3f} <= cost {cost:.3f})")
            stake = self.bankroll * self.cfg.kelly_fraction * f * mult
        caps = {
            "risk.max_usd_per_trade": self.cfg.max_usd_per_trade,
            "strategy.max_usd_per_trade": float(getattr(strategy_cfg, "max_usd_per_trade", math.inf)),
            "risk.max_usd_per_window room": self.cfg.max_usd_per_window - window_exposure,
            "strategy.max_usd_per_window room": float(getattr(strategy_cfg, "max_usd_per_window", math.inf))
            - strategy_window_exposure,
            "risk.max_total_exposure_usd room": self.cfg.max_total_exposure_usd - total_exposure,
            "risk.max_slot_direction_usd room": self.cfg.max_slot_direction_usd - slot_direction_exposure,
        }
        if max_usd is not None:
            caps["intent max_usd"] = max_usd
        cap_name, cap = min(caps.items(), key=lambda kv: kv[1])
        room = max(0.0, cap)
        usd = min(stake, room)
        shares = usd / cost if cost > 0 else 0.0
        if max_shares is not None:
            shares = min(shares, max_shares)
        shares = math.floor(shares * 100.0) / 100.0

        if shares < min_order_shares:
            bump_cost = min_order_shares * cost
            fits = bump_cost <= room + 1e-9 and (max_shares is None or max_shares >= min_order_shares)
            if self.cfg.bump_to_min_size and fits:
                shares = min_order_shares
            else:
                why = cap_name if room < stake else "kelly stake"
                return SizeDecision(
                    0.0, 0.0, f,
                    f"size {shares:.2f} < market min {min_order_shares:g} shares (limited by {why}: ${min(stake, room):.2f})",
                )
        clipped = f"clipped by {cap_name}" if room < stake else ""
        return SizeDecision(shares, shares * price, f, clipped)

    # -- outcomes ------------------------------------------------------------------
    def on_settlement(self, strategy: str, pnl: float, now: float) -> None:
        self.roll_day(now)
        self.realized_pnl_today += pnl
        self.realized_pnl_total += pnl
        if pnl < -1e-9:
            streak = self.loss_streak.get(strategy, 0) + 1
            self.loss_streak[strategy] = streak
            if streak >= self.cfg.max_consecutive_losses:
                self.cooldown_until[strategy] = now + self.cfg.cooldown_s_after_streak
                logger.warning(
                    "%s: %d consecutive losses -> cooling down for %.0fs (size x%.2f afterwards)",
                    strategy, streak, self.cfg.cooldown_s_after_streak, self.size_multiplier(strategy),
                )
        elif pnl > 1e-9:
            self.loss_streak[strategy] = 0

    def on_correction(self, pnl_delta: float, now: float) -> None:
        self.roll_day(now)
        self.realized_pnl_today += pnl_delta
        self.realized_pnl_total += pnl_delta
