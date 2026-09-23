from datetime import datetime

from kalshi_perps.store import MarketStore
from scripts.backfill_candles import backfill, chunks


def test_chunks_stay_under_candle_cap():
    wins = list(chunks(0, 10 * 86400, 1, per_chunk=4000))
    assert wins[0] == (0, 240000) and wins[-1][1] == 10 * 86400
    assert all((b - a) // 60 <= 4000 for a, b in wins)
    assert all(wins[i][1] == wins[i + 1][0] for i in range(len(wins) - 1))


class FakeClient:
    def __init__(self):
        self.calls = []

    def candlesticks(self, a, b, period_interval, ticker):
        self.calls.append((a, b))
        return [{"end_period_ts": a + 60,
                 "price": {"open": "8.5", "high": "8.6", "low": "8.4", "close": "8.55", "mean": "8.5", "previous": "8.5"},
                 "bid": {"open": "8.49", "high": "8.5", "low": "0.0000", "close": "8.54"},
                 "ask": {"open": "8.51", "high": "922337203685477.5807", "low": "8.5", "close": "8.56"},
                 "volume": "3.00", "open_interest": "10.00"}]


def test_backfill_upserts_and_nulls_sentinels():
    store = MarketStore(":memory:")
    fc = FakeClient()
    n = backfill(fc, store, "KXBTCPERP1", 0, 9 * 86400, 1, pause=0, log=lambda m: None)
    assert n == len(fc.calls) == 4
    backfill(fc, store, "KXBTCPERP1", 0, 9 * 86400, 1, pause=0, log=lambda m: None)  # idempotent
    assert store.candle_count("KXBTCPERP1", 1) == 4
    row = store.db.execute("SELECT bid_low, ask_high, bid_close, ask_close FROM candles LIMIT 1").fetchone()
    assert row == (None, None, "8.54", "8.56")


def test_funding_upsert():
    store = MarketStore(":memory:")
    ev = [{"funding_time": "2026-09-23T12:00:00Z", "funding_rate": 0.0068, "mark_price": "8.6285"}]
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    store.upsert_funding("KXBTCPERP1", ev, parse)
    store.upsert_funding("KXBTCPERP1", ev, parse)
    assert store.db.execute("SELECT funding_ts, rate FROM funding").fetchall() == [(1790164800, "0.0068")]
