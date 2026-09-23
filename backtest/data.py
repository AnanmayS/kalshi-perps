"""Load bars and funding events from the market store."""

from __future__ import annotations

from decimal import Decimal

from kalshi_perps.store import MarketStore, _clean_ask, _clean_bid
from strategies.base import Bar


def _d(x):
    return None if x is None else Decimal(x)


def bar_from_candle(c: dict) -> Bar:
    """Build a Bar from a raw API candle, exactly as the backfill stores it (sentinels -> None)."""
    p, b, a = c["price"], c["bid"], c["ask"]
    return Bar(c["end_period_ts"], _d(p.get("open")), _d(p.get("high")), _d(p.get("low")), _d(p.get("close")),
               _d(_clean_bid(b.get("open"))), _d(_clean_bid(b.get("close"))),
               _d(_clean_ask(a.get("open"))), _d(_clean_ask(a.get("close"))), _d(c.get("volume")) or Decimal(0))


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
