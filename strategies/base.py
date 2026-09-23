"""Strategy interface.

A strategy sees one completed bar at a time and returns a TARGET position in
signed contracts (+ long, - short, 0 flat), or None to keep the current one.
It never sees future bars and never chooses fill prices: the backtester (or a
live runner) turns target changes into taker orders at the next observed quote.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Bar:
    ts: int                      # end of the 1-minute period (unix seconds, inclusive)
    open: Decimal | None         # trade prices; None when no trades that minute
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    bid_open: Decimal | None     # observed quotes; None when that side of the book was empty
    bid_close: Decimal | None
    ask_open: Decimal | None
    ask_close: Decimal | None
    volume: Decimal

    @property
    def mid(self) -> Decimal | None:
        if self.bid_close is not None and self.ask_close is not None:
            return (self.bid_close + self.ask_close) / 2
        return self.close


class Strategy:
    name = "base"

    def on_bar(self, bar: Bar, position: Decimal) -> Decimal | None:
        raise NotImplementedError

    def params(self) -> dict:
        return {}
