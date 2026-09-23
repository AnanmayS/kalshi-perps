"""Risk engine: per-trade notional cap, daily loss limit, kill switch.

Daily P&L is equity now minus equity at the start of the UTC day, where equity is
cash + unrealized P&L at mark. It therefore includes fees and funding.

Kill switch: trips automatically when daily P&L <= -daily_loss_limit, or manually.
While tripped, orders that would open or add exposure are rejected; reduce-only
orders (closing) are still allowed so a position can always be exited. It clears
at the next UTC day, or via reset() — which re-trips immediately if the day's
loss is still at or past the limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

ZERO = Decimal(0)


@dataclass(frozen=True)
class RiskConfig:
    max_notional_per_trade: Decimal = Decimal("500")
    daily_loss_limit: Decimal = Decimal("100")


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""


def _utc_today(now: datetime | None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


class RiskEngine:
    def __init__(self, config: RiskConfig, start_equity: Decimal, now: datetime | None = None):
        if config.daily_loss_limit <= 0 or config.max_notional_per_trade <= 0:
            raise ValueError("risk limits must be positive")
        self.config = config
        self.day = _utc_today(now)
        self.day_start_equity = Decimal(start_equity)
        self.last_equity = Decimal(start_equity)
        self.killed = False
        self.kill_reason = ""
        self.killed_at: datetime | None = None

    # -- state ---------------------------------------------------------------

    @property
    def daily_pnl(self) -> Decimal:
        return self.last_equity - self.day_start_equity

    @property
    def loss_remaining(self) -> Decimal:
        return self.config.daily_loss_limit + self.daily_pnl

    def _roll_day(self, now: datetime | None) -> None:
        today = _utc_today(now)
        if today != self.day:
            self.day = today
            self.day_start_equity = self.last_equity
            if self.killed and self.kill_reason.startswith("daily loss"):
                self._clear()

    def _clear(self) -> None:
        self.killed, self.kill_reason, self.killed_at = False, "", None

    def kill(self, reason: str, now: datetime | None = None) -> None:
        if not self.killed:
            self.killed = True
            self.kill_reason = reason
            self.killed_at = now or datetime.now(timezone.utc)

    def reset(self, now: datetime | None = None) -> None:
        """Manual reset. Re-trips right away if today's loss is still over the limit."""
        self._clear()
        self.update_equity(self.last_equity, now)

    def update_equity(self, equity: Decimal, now: datetime | None = None) -> None:
        self._roll_day(now)
        self.last_equity = Decimal(equity)
        if self.daily_pnl <= -self.config.daily_loss_limit:
            self.kill(f"daily loss limit hit: {self.daily_pnl:.2f} <= -{self.config.daily_loss_limit}", now)

    # -- pre-trade -----------------------------------------------------------

    def check_order(self, notional: Decimal, reduces_only: bool, now: datetime | None = None) -> RiskDecision:
        self._roll_day(now)
        notional = abs(Decimal(notional))
        if self.killed and not reduces_only:
            return RiskDecision(False, f"kill switch active ({self.kill_reason}); only closing orders allowed")
        if notional > self.config.max_notional_per_trade and not reduces_only:
            return RiskDecision(False, f"notional ${notional:.2f} exceeds max per trade ${self.config.max_notional_per_trade}")
        return RiskDecision(True)

    def snapshot(self) -> dict:
        return {
            "max_notional_per_trade": self.config.max_notional_per_trade,
            "daily_loss_limit": self.config.daily_loss_limit,
            "day": self.day.isoformat(),
            "day_start_equity": self.day_start_equity,
            "daily_pnl": self.daily_pnl,
            "loss_remaining": self.loss_remaining,
            "killed": self.killed,
            "kill_reason": self.kill_reason,
            "killed_at": self.killed_at.isoformat() if self.killed_at else None,
        }
