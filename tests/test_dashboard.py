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


def test_public_mode_is_read_only():
    import json
    from dashboard.server import make_handler

    d = Dashboard.__new__(Dashboard)
    d.public, d.broker = True, None
    d.settings = None
    for name in ("config", "snapshot", "candles", "account", "runner_status", "paper_state", "paper_preview",
                 "paper_order", "paper_close", "paper_kill", "paper_reset_kill", "paper_reset_account"):
        setattr(d, name, lambda *a, **k: {})
    handler = make_handler(d)

    class Req(handler):
        def __init__(self, method, path, body=b"{}"):
            import io
            self.command, self.path = method, path
            self.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
            self.rfile, self.wfile = io.BytesIO(body), io.BytesIO()
            self.sent = None

        def send_response(self, code, message=None):
            self.sent = code

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

    for path in ("/api/paper/order", "/api/paper/kill", "/api/paper/reset"):
        r = Req("POST", path)
        r.do_POST()
        assert r.sent == 403
    r = Req("GET", "/api/paper/state")
    r.do_GET()
    assert r.sent == 404
    r = Req("GET", "/api/runner")
    r.do_GET()
    assert r.sent == 200
