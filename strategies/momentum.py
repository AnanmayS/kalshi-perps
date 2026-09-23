"""Starter strategy: short-term time-series momentum on the mid price.

Signal: return of the mid over the last `lookback` minutes.
  * above +threshold_bps -> target long `size` contracts
  * below -threshold_bps -> target short `size` contracts
  * otherwise keep the current position
Positions are held at least `min_hold` minutes before they can change, and
flattened if the signal decays back inside +/- exit_bps, to limit churn.

The threshold matters: every round trip pays two taker fees (0.12% each on
the default tier) plus the spread, i.e. roughly 25-30 bps. A signal smaller
than that loses money by construction.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from .base import Bar, Strategy


class Momentum(Strategy):
    name = "momentum"

    def __init__(self, lookback: int = 60, threshold_bps: float = 40, exit_bps: float = 5,
                 size: Decimal = Decimal("10"), min_hold: int = 30):
        if lookback < 1 or threshold_bps <= exit_bps:
            raise ValueError("need lookback >= 1 and threshold_bps > exit_bps")
        self.lookback = lookback
        self.threshold = Decimal(str(threshold_bps)) / 10_000
        self.exit = Decimal(str(exit_bps)) / 10_000
        self.size = Decimal(size)
        self.min_hold = min_hold
        self.mids: deque[Decimal] = deque(maxlen=lookback + 1)
        self.held = 0
        self.last_signal_bps: float | None = None   # for status displays

    def params(self) -> dict:
        return {"lookback": self.lookback, "threshold_bps": float(self.threshold * 10_000),
                "exit_bps": float(self.exit * 10_000), "size": str(self.size), "min_hold": self.min_hold}

    def on_bar(self, bar: Bar, position: Decimal) -> Decimal | None:
        mid = bar.mid
        if mid is None:
            return None
        self.mids.append(mid)
        self.held = self.held + 1 if position != 0 else 0
        if len(self.mids) <= self.lookback:
            return None
        ret = self.mids[-1] / self.mids[0] - 1
        self.last_signal_bps = float(ret * 10_000)

        if position != 0 and self.held < self.min_hold:
            return None
        if ret > self.threshold:
            return self.size
        if ret < -self.threshold:
            return -self.size
        if position != 0 and abs(ret) < self.exit:
            return Decimal(0)
        return None


class BuyAndHold(Strategy):
    """Baseline: go long `size` on the first bar and hold (pays funding the whole time)."""

    name = "hold"

    def __init__(self, size: Decimal = Decimal("10")):
        self.size = Decimal(size)

    def params(self) -> dict:
        return {"size": str(self.size)}

    def on_bar(self, bar: Bar, position: Decimal) -> Decimal | None:
        return self.size if position == 0 else None
