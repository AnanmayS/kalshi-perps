"""SQLite market-data store shared by the backfill script, the WebSocket
collector and the backtester (default path: data/market.sqlite, git-ignored).

Prices are stored as TEXT decimal strings exactly as Kalshi sends them (dollars
per contract), so nothing is lost to float rounding. Candle bid/ask fields that
Kalshi fills with empty-book sentinels (bid 0, ask ~int64 max) are stored NULL.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Iterable

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "market.sqlite"
SENTINEL_MAX = Decimal("1000000")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    ticker TEXT NOT NULL, interval_min INTEGER NOT NULL, end_ts INTEGER NOT NULL,
    open TEXT, high TEXT, low TEXT, close TEXT, mean TEXT, previous TEXT,
    bid_open TEXT, bid_high TEXT, bid_low TEXT, bid_close TEXT,
    ask_open TEXT, ask_high TEXT, ask_low TEXT, ask_close TEXT,
    volume TEXT, open_interest TEXT,
    PRIMARY KEY (ticker, interval_min, end_ts)
);
CREATE TABLE IF NOT EXISTS funding (
    ticker TEXT NOT NULL, funding_ts INTEGER NOT NULL, rate TEXT NOT NULL, mark_price TEXT,
    PRIMARY KEY (ticker, funding_ts)
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, ts_ms INTEGER NOT NULL,
    price TEXT NOT NULL, count TEXT NOT NULL, taker_side TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_ticker_ts ON trades (ticker, ts_ms);
CREATE TABLE IF NOT EXISTS quotes (
    ticker TEXT NOT NULL, ts_ms INTEGER NOT NULL,
    bid TEXT, bid_size TEXT, ask TEXT, ask_size TEXT
);
CREATE INDEX IF NOT EXISTS quotes_ticker_ts ON quotes (ticker, ts_ms);
CREATE TABLE IF NOT EXISTS tickers (
    ticker TEXT NOT NULL, ts_ms INTEGER NOT NULL, price TEXT, bid TEXT, ask TEXT,
    mark_price TEXT, reference_price TEXT, funding_rate REAL, next_funding_ms INTEGER,
    volume_24h TEXT, open_interest TEXT
);
CREATE INDEX IF NOT EXISTS tickers_ticker_ts ON tickers (ticker, ts_ms);
CREATE TABLE IF NOT EXISTS book_snapshots (
    ticker TEXT NOT NULL, ts_ms INTEGER NOT NULL, bids TEXT NOT NULL, asks TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS book_ticker_ts ON book_snapshots (ticker, ts_ms);
"""


def _clean_bid(x):
    return None if x is None or Decimal(x) <= 0 else x


def _clean_ask(x):
    return None if x is None or Decimal(x) >= SENTINEL_MAX else x


class MarketStore:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def commit(self) -> None:
        self.db.commit()

    # ---- candles / funding (REST backfill) -----------------------------------

    def upsert_candles(self, ticker: str, interval_min: int, candles: Iterable[dict]) -> int:
        rows = []
        for c in candles:
            p, b, a = c["price"], c["bid"], c["ask"]
            rows.append((
                ticker, interval_min, c["end_period_ts"],
                p.get("open"), p.get("high"), p.get("low"), p.get("close"), p.get("mean"), p.get("previous"),
                _clean_bid(b.get("open")), _clean_bid(b.get("high")), _clean_bid(b.get("low")), _clean_bid(b.get("close")),
                _clean_ask(a.get("open")), _clean_ask(a.get("high")), _clean_ask(a.get("low")), _clean_ask(a.get("close")),
                c.get("volume"), c.get("open_interest"),
            ))
        self.db.executemany("INSERT OR REPLACE INTO candles VALUES (" + ",".join("?" * 19) + ")", rows)
        self.db.commit()
        return len(rows)

    def last_candle_ts(self, ticker: str, interval_min: int) -> int | None:
        r = self.db.execute("SELECT MAX(end_ts) FROM candles WHERE ticker=? AND interval_min=?",
                            (ticker, interval_min)).fetchone()
        return r[0]

    def candle_count(self, ticker: str, interval_min: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM candles WHERE ticker=? AND interval_min=?",
                               (ticker, interval_min)).fetchone()[0]

    def upsert_funding(self, ticker: str, events: Iterable[dict], parse_ts) -> int:
        rows = [(ticker, int(parse_ts(e["funding_time"]).timestamp()), repr(e["funding_rate"]), e.get("mark_price"))
                for e in events]
        self.db.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?,?)", rows)
        self.db.commit()
        return len(rows)

    # ---- live data (WebSocket collector) --------------------------------------

    def add_trade(self, m: dict) -> None:
        self.db.execute("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?)",
                        (m["trade_id"], m["market_ticker"], m["ts_ms"], m["price"], m["count"], m["taker_side"]))

    def add_quote(self, ticker: str, ts_ms: int, bid, bid_size, ask, ask_size) -> None:
        self.db.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?)",
                        (ticker, ts_ms, _s(bid), _s(bid_size), _s(ask), _s(ask_size)))

    def add_ticker(self, m: dict) -> None:
        fr = m.get("funding_rate") or {}
        self.db.execute(
            "INSERT INTO tickers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (m["market_ticker"], m["ts_ms"], m.get("price"), m.get("bid"), m.get("ask"),
             (m.get("settlement_mark_price") or {}).get("price"), (m.get("reference_price") or {}).get("price"),
             fr.get("rate"), fr.get("next_funding_time_ms"), m.get("volume_24h"), m.get("open_interest")))

    def add_book_snapshot(self, ticker: str, ts_ms: int, bids, asks) -> None:
        self.db.execute("INSERT INTO book_snapshots VALUES (?,?,?,?)",
                        (ticker, ts_ms, json.dumps([[str(p), str(q)] for p, q in bids]),
                         json.dumps([[str(p), str(q)] for p, q in asks])))

    def counts(self) -> dict:
        return {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("candles", "funding", "trades", "quotes", "tickers", "book_snapshots")}


def _s(x):
    return None if x is None else str(x)
