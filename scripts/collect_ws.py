"""Run the authenticated WebSocket collector.

    python scripts/collect_ws.py                  # run until Ctrl-C
    python scripts/collect_ws.py --duration 60    # run for 60 seconds

Records live trades, ticker updates, top-of-book quotes and periodic order book
snapshots for the BTC perp into data/market.sqlite. Needs demo API keys in .env.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_perps import load_settings  # noqa: E402
from kalshi_perps.store import DEFAULT_DB, MarketStore  # noqa: E402
from kalshi_perps.ws import Collector  # noqa: E402


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=None, help="seconds to run (default: forever)")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--status-every", type=float, default=10.0)
    ap.add_argument("--snapshot-every", type=float, default=5.0, help="seconds between order book snapshots")
    args = ap.parse_args()

    settings = load_settings()
    store = MarketStore(args.db)
    before = store.counts()
    collector = Collector(settings, store, ticker=args.ticker, snapshot_every=args.snapshot_every, log=log)
    log(f"env={settings.env} ticker={collector.ticker} db={args.db}")
    try:
        asyncio.run(collector.run(duration=args.duration, status_every=args.status_every))
    except KeyboardInterrupt:
        log("stopping")
    finally:
        after = store.counts()
        store.close()
    log(collector.status_line())
    log("rows added: " + ", ".join(f"{k} +{after[k] - before[k]}" for k in after if k not in ("candles", "funding")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
