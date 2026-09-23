"""Load bars and funding events from the market store."""

from __future__ import annotations

from decimal import Decimal

from kalshi_perps.store import MarketStore
from strategies.base import Bar


def _d(x):
    return None if x is None else Decimal(x)


def load_bars(store: MarketStore, ticker: str, start_ts: int | None = None, end_ts: int | None = None) -> list[Bar]:
    q = ("SELECT end_ts, open, high, low, close, bid_open, bid_close, ask_open, ask_close, volume "
         "FROM candles WHERE ticker=? AND interval_min=1")
    args: list = [ticker]
    if start_ts is not None:
        q += " AND end_ts >= ?"
        args.append(start_ts)
    if end_ts is not None:
        q += " AND end_ts <= ?"
        args.append(end_ts)
    rows = store.db.execute(q + " ORDER BY end_ts", args).fetchall()
    return [Bar(r[0], _d(r[1]), _d(r[2]), _d(r[3]), _d(r[4]), _d(r[5]), _d(r[6]), _d(r[7]), _d(r[8]),
                _d(r[9]) or Decimal(0)) for r in rows]


def load_funding(store: MarketStore, ticker: str, start_ts: int | None = None,
                 end_ts: int | None = None) -> list[tuple[int, Decimal, Decimal | None]]:
    q = "SELECT funding_ts, rate, mark_price FROM funding WHERE ticker=?"
    args: list = [ticker]
    if start_ts is not None:
        q += " AND funding_ts >= ?"
        args.append(start_ts)
    if end_ts is not None:
        q += " AND funding_ts <= ?"
        args.append(end_ts)
    return [(r[0], Decimal(r[1]), _d(r[2])) for r in store.db.execute(q + " ORDER BY funding_ts", args)]
