"""Session-level risk limits: daily loss stop and a cooldown after a losing streak.

Pure logic; time is passed in so tests (and the simulator) control the clock.
"""
from __future__ import annotations

from datetime import datetime, timezone

from updown.config import RiskLimitsConfig


class UpDownRisk:
    def __init__(self, cfg: RiskLimitsConfig):
        self.cfg = cfg
        self.realized_pnl_today = 0.0
        self.consecutive_losses = 0
        self.cooldown_until = 0.0
        self._day = None

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, tz=timezone.utc).date()
        if day != self._day:
            self._day = day
            self.realized_pnl_today = 0.0

    def can_trade(self, now: float) -> tuple[bool, str]:
        self._roll_day(now)
        if self.realized_pnl_today <= -abs(self.cfg.max_daily_loss_usd):
            return False, f"daily loss limit hit (${self.realized_pnl_today:.2f}); waiting for UTC midnight"
        if now < self.cooldown_until:
            return False, f"cooling down after {self.cfg.max_consecutive_losses} losing windows ({self.cooldown_until - now:.0f}s left)"
        return True, ""

    def room_usd(self, now: float) -> float:
        """Max new risk allowed right now: never bet more than we could still lose today."""
        ok, _ = self.can_trade(now)
        if not ok:
            return 0.0
        return max(0.0, abs(self.cfg.max_daily_loss_usd) + self.realized_pnl_today)

    def record_window(self, now: float, pnl: float) -> None:
        """Book the realized P&L of one finished window (0 if we didn't trade it)."""
        self._roll_day(now)
        self.realized_pnl_today += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.cfg.max_consecutive_losses:
                self.cooldown_until = now + self.cfg.cooldown_minutes * 60
                self.consecutive_losses = 0
        elif pnl > 0:
            self.consecutive_losses = 0
