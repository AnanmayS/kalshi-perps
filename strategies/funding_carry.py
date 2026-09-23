"""Funding carry: hold the side that GETS paid funding while the rate stays extreme.

Kalshi perps pay funding every 8h: positive rate -> longs pay shorts. When recent
realized rates (average of the last `window` events, known only after they happen)
exceed `threshold`, go short `size` to receive funding; below -threshold go long;
otherwise flat. Price risk is taken on in exchange for the carry, so a strong
trend against the position can outweigh it.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from .base import Bar, Strategy


class FundingCarry(Strategy):
    name = "carry"

    def __init__(self, threshold_bps: float = 10, window: int = 3, size: Decimal = Decimal("10")):
        self.threshold = Decimal(str(threshold_bps)) / 10_000
        self.window = window
        self.size = Decimal(size)
        self.rates: deque[Decimal] = deque(maxlen=window)
        self.last_signal_bps: float | None = None

    def params(self) -> dict:
        return {"threshold_bps": float(self.threshold * 10_000), "window": self.window, "size": str(self.size)}

    def on_funding(self, ts: int, rate: Decimal) -> None:
        self.rates.append(Decimal(rate))

    def on_bar(self, bar: Bar, position: Decimal) -> Decimal | None:
        if len(self.rates) < self.window:
            return None
        avg = sum(self.rates) / len(self.rates)
        self.last_signal_bps = float(avg * 10_000)
        if avg > self.threshold:
            return -self.size
        if avg < -self.threshold:
            return self.size
        return Decimal(0)
