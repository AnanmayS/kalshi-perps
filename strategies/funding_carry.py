"""Funding carry: hold the side that GETS paid funding while the rate stays extreme.

Kalshi perps pay funding every 8h: positive rate -> longs pay shorts. When recent
realized rates (average of the last `window` events, known only after they happen)
exceed `threshold_bps`, go short `size` to receive funding; below -threshold go long.

Optional refinements (off by default):
  * exit_bps: hysteresis. Once in, stay until the average falls below exit_bps
    instead of threshold_bps, so the position doesn't flip in and out near the line.
  * trend_minutes / trend_bps: rally guard. Stand aside while the mid has moved
    more than trend_bps AGAINST the carry position over the last trend_minutes
    (e.g. a sharp rally while short), since that price move can outweigh the funding.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from .base import Bar, Strategy


class FundingCarry(Strategy):
    name = "carry"

    def __init__(self, threshold_bps: float = 20, window: int = 3, size: Decimal = Decimal("10"),
                 exit_bps: float | None = None, trend_minutes: int = 0, trend_bps: float = 0):
        self.threshold = Decimal(str(threshold_bps)) / 10_000
        self.exit = self.threshold if exit_bps is None else Decimal(str(exit_bps)) / 10_000
        if self.exit > self.threshold:
            raise ValueError("exit_bps must not exceed threshold_bps")
        self.window = window
        self.size = Decimal(size)
        self.trend_minutes = trend_minutes
        self.trend = Decimal(str(trend_bps)) / 10_000
        self.rates: deque[Decimal] = deque(maxlen=window)
        self.mids: deque[Decimal] = deque(maxlen=trend_minutes + 1 if trend_minutes else 1)
        self.last_signal_bps: float | None = None

    def params(self) -> dict:
        p = {"threshold_bps": float(self.threshold * 10_000), "window": self.window, "size": str(self.size)}
        if self.exit != self.threshold:
            p["exit_bps"] = float(self.exit * 10_000)
        if self.trend_minutes:
            p["trend_minutes"], p["trend_bps"] = self.trend_minutes, float(self.trend * 10_000)
        return p

    def on_funding(self, ts: int, rate: Decimal) -> None:
        self.rates.append(Decimal(rate))

    def _against(self, direction: int) -> bool:
        """True if price moved more than trend_bps against a position in `direction` (+1 long, -1 short)."""
        if not self.trend_minutes or len(self.mids) <= self.trend_minutes:
            return False
        ret = self.mids[-1] / self.mids[0] - 1
        return ret * -direction > self.trend

    def on_bar(self, bar: Bar, position: Decimal) -> Decimal | None:
        if bar.mid is not None:
            self.mids.append(bar.mid)
        if len(self.rates) < self.window:
            return None
        avg = sum(self.rates) / len(self.rates)
        self.last_signal_bps = float(avg * 10_000)

        if avg > self.threshold or (position < 0 and avg > self.exit):
            want = -1
        elif avg < -self.threshold or (position > 0 and avg < -self.exit):
            want = 1
        else:
            return Decimal(0)
        if self._against(want):
            return Decimal(0)
        return want * self.size
