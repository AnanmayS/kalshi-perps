"""Mean reversion: fade moves that stretch too far from the recent average.

z = (mid - rolling mean) / rolling std over `lookback` minutes.
  * z above +entry_z -> short `size`; below -entry_z -> long `size`
  * exit when |z| falls back under exit_z, or after max_hold minutes
  * only enter if the stretch |mid - mean| is at least min_edge_bps of the mid,
    so the expected snap-back can cover the ~25-35 bps round-trip cost
"""

from __future__ import annotations

import math
from collections import deque
from decimal import Decimal

from .base import Bar, Strategy


class MeanReversion(Strategy):
    name = "meanrev"

    def __init__(self, lookback: int = 60, entry_z: float = 2.5, exit_z: float = 0.5,
                 min_edge_bps: float = 30, max_hold: int = 120, size: Decimal = Decimal("10")):
        if entry_z <= exit_z:
            raise ValueError("entry_z must exceed exit_z")
        self.lookback, self.entry_z, self.exit_z = lookback, entry_z, exit_z
        self.min_edge = min_edge_bps / 10_000
        self.max_hold, self.size = max_hold, Decimal(size)
        self.mids: deque[float] = deque(maxlen=lookback)
        self.held = 0
        self.last_signal_bps: float | None = None
        self.last_z: float | None = None

    def params(self) -> dict:
        return {"lookback": self.lookback, "entry_z": self.entry_z, "exit_z": self.exit_z,
                "min_edge_bps": self.min_edge * 10_000, "max_hold": self.max_hold, "size": str(self.size)}

    def on_bar(self, bar: Bar, position: Decimal) -> Decimal | None:
        if bar.mid is None:
            return None
        mid = float(bar.mid)
        self.held = self.held + 1 if position != 0 else 0
        ready = len(self.mids) == self.lookback
        if ready:
            mean = sum(self.mids) / len(self.mids)
            sd = math.sqrt(sum((m - mean) ** 2 for m in self.mids) / (len(self.mids) - 1))
        self.mids.append(mid)            # stats above use only bars before this one
        if not ready or sd == 0:
            return None
        z = (mid - mean) / sd
        stretch = (mid - mean) / mean
        self.last_z, self.last_signal_bps = z, stretch * 10_000

        if position != 0:
            if abs(z) < self.exit_z or self.held >= self.max_hold or (position > 0 and z > 0) or (position < 0 and z < 0):
                return Decimal(0)
            return None
        if abs(stretch) < self.min_edge:
            return None
        if z > self.entry_z:
            return -self.size
        if z < -self.entry_z:
            return self.size
        return None
