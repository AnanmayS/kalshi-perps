"""Backfill Kalshi perps candles (and funding history) into SQLite.

    python scripts/backfill_candles.py                  # last 7 days of 1m candles + funding
    python scripts/backfill_candles.py --days 30 --interval 60
    python scripts/backfill_candles.py --resume         # continue from the newest stored candle

Kalshi caps each candlestick request at 5,000 candles, so the range is fetched
in chunks. Re-running is safe: rows are upserted by (ticker, interval, end_ts).
Public endpoints only; no API key needed.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_perps import KalshiPerpsClient, load_settings  # noqa: E402
from kalshi_perps.client import parse_ts  # noqa: E402
from kalshi_perps.store import DEFAULT_DB, MarketStore  # noqa: E402

MAX_CANDLES_PER_REQUEST = 5000
CHUNK_CANDLES = 4000  # stay safely under the cap


def chunks(start_ts: int, end_ts: int, interval_min: int, per_chunk: int = CHUNK_CANDLES):
    """Split [start_ts, end_ts] into windows of at most `per_chunk` candles."""
    step = interval_min * 60 * per_chunk
    t = start_ts
    while t < end_ts:
        yield t, min(t + step, end_ts)
        t += step


def fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def backfill(client, store: MarketStore, ticker: str, start_ts: int, end_ts: int, interval_min: int,
             pause: float = 0.2, log=print) -> int:
    total = 0
    for a, b in chunks(start_ts, end_ts, interval_min):
        candles = client.candlesticks(a, b, period_interval=interval_min, ticker=ticker)
        n = store.upsert_candles(ticker, interval_min, candles)
        total += n
        log(f"  {fmt(a)} -> {fmt(b)}: {n} candles")
        time.sleep(pause)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=7, help="how far back to fetch (default 7)")
    ap.add_argument("--interval", type=int, default=1, choices=(1, 60, 1440), help="candle minutes (default 1)")
    ap.add_argument("--ticker", default=None, help="defaults to KXBTCPERP1 on demo, KXBTCPERP on prod")
    ap.add_argument("--resume", action="store_true", help="start from the newest stored candle instead of --days")
    ap.add_argument("--no-funding", action="store_true", help="skip funding history")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    settings = load_settings()
    client = KalshiPerpsClient(settings)
    ticker = args.ticker or settings.ticker
    store = MarketStore(args.db)

    end_ts = int(time.time()) // 60 * 60
    start_ts = end_ts - int(args.days * 86400)
    if args.resume:
        last = store.last_candle_ts(ticker, args.interval)
        if last:
            start_ts = last - args.interval * 60  # re-fetch the last bar in case it was partial
    print(f"Backfilling {ticker} {args.interval}m candles {fmt(start_ts)} -> {fmt(end_ts)} into {args.db}")
    n = backfill(client, store, ticker, start_ts, end_ts, args.interval)
    print(f"Candles written: {n}  (stored total {store.candle_count(ticker, args.interval)})")

    if not args.no_funding:
        events = client.funding_rates_history(ticker=ticker, start_ts=start_ts, end_ts=end_ts)
        print(f"Funding events written: {store.upsert_funding(ticker, events, parse_ts)}")

    rows = store.db.execute(
        "SELECT COUNT(*), SUM(close IS NULL), SUM(bid_close IS NULL OR ask_close IS NULL) "
        "FROM candles WHERE ticker=? AND interval_min=? AND end_ts BETWEEN ? AND ?",
        (ticker, args.interval, start_ts, end_ts)).fetchone()
    expected = (end_ts - start_ts) // (args.interval * 60) + 1  # both ends inclusive
    print(f"Coverage: {rows[0]}/{expected} bars; {rows[1] or 0} with no trades; "
          f"{rows[2] or 0} with an empty side of the book")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
