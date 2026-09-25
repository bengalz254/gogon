"""Session-level risk limits: daily loss stop and a cooldown after a losing streak.

Pure logic; time is passed in so tests (and the simulator) control the clock.
"""
from __future__ import annotations

from datetime import datetime, timezone

from updown.config import RiskLimitsConfig


class UpDownRisk:
    """Daily loss stop (all coins together) and a losing-streak cooldown
    (per coin: one coin trending against us shouldn't freeze the other six)."""

    def __init__(self, cfg: RiskLimitsConfig):
        self.cfg = cfg
        self.realized_pnl_today = 0.0
        self.streaks: dict[str, int] = {}
        self.cooldowns: dict[str, float] = {}
        self._day = None

    # Summaries for the dashboard: the worst coin.
    @property
    def consecutive_losses(self) -> int:
        return max(self.streaks.values(), default=0)

    @property
    def cooldown_until(self) -> float:
        return max(self.cooldowns.values(), default=0.0)

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, tz=timezone.utc).date()
        if day != self._day:
            self._day = day
            self.realized_pnl_today = 0.0

    def can_trade(self, now: float, asset: str = "") -> tuple[bool, str]:
        self._roll_day(now)
        if self.realized_pnl_today <= -abs(self.cfg.max_daily_loss_usd):
            return False, f"daily loss limit hit (${self.realized_pnl_today:.2f}); waiting for UTC midnight"
        until = self.cooldowns.get(asset, 0.0)
        if now < until:
            return False, f"cooling down after {self.cfg.max_consecutive_losses} losing windows ({until - now:.0f}s left)"
        return True, ""

    def room_usd(self, now: float, asset: str = "") -> float:
        """Max new risk allowed right now: never bet more than we could still lose today."""
        ok, _ = self.can_trade(now, asset)
        if not ok:
            return 0.0
        return max(0.0, abs(self.cfg.max_daily_loss_usd) + self.realized_pnl_today)

    def record_window(self, now: float, pnl: float, asset: str = "") -> None:
        """Book the realized P&L of one finished window (0 if we didn't trade it)."""
        self._roll_day(now)
        self.realized_pnl_today += pnl
        if pnl < 0:
            self.streaks[asset] = self.streaks.get(asset, 0) + 1
            if self.streaks[asset] >= self.cfg.max_consecutive_losses:
                self.cooldowns[asset] = now + self.cfg.cooldown_minutes * 60
                self.streaks[asset] = 0
        elif pnl > 0:
            self.streaks[asset] = 0
