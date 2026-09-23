from dashboard.server import Dashboard, TTLCache


class StubClient:
    def candlesticks(self, start_ts, end_ts, period_interval):
        def c(t, price, bid, ask):
            return {"end_period_ts": t, "price": price, "bid": {"close": bid}, "ask": {"close": ask}, "volume": "1.00"}
        traded = {"open": "8.50", "high": "8.52", "low": "8.49", "close": "8.51", "previous": "8.50"}
        no_trade = {"open": None, "high": None, "low": None, "close": None, "previous": "8.51"}
        return [
            c(60, traded, "8.50", "8.52"),
            c(120, no_trade, "0.0000", "922337203685477.5807"),  # empty-book sentinels
        ]


def make():
    d = Dashboard.__new__(Dashboard)
    d.client, d.cache = StubClient(), TTLCache()
    return d


def test_candles_carry_previous_close_and_drop_sentinel_spreads():
    out = make().candles()["candles"]
    assert out[0]["c"] == "8.51" and out[0]["spread"] == "0.02"
    assert out[1]["o"] == out[1]["h"] == out[1]["l"] == out[1]["c"] == "8.51"
    assert out[1]["spread"] is None
