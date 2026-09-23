"""Local dashboard server.

    python dashboard/server.py            # http://localhost:8765

Serves a single page plus a small JSON API that proxies Kalshi's perps REST
endpoints (with short caches so the browser can poll without hammering Kalshi).
Read-only: this server never places orders.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kalshi_perps import KalshiAPIError, KalshiPerpsClient, load_settings  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
CONTRACT_BTC = 10_000  # 1 contract = 0.0001 BTC
# Kalshi fills empty-book candle fields with sentinels (int64-max ask, 0 bid).
SENTINEL_MAX = Decimal("1000000")


class TTLCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, object]] = {}

    def get(self, key: str, ttl: float, fn):
        with self._lock:
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        value = fn()
        with self._lock:
            self._data[key] = (time.monotonic(), value)
        return value


def _jsonable(o):
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(type(o).__name__)


class Dashboard:
    def __init__(self):
        self.settings = load_settings()
        self.client = KalshiPerpsClient(self.settings)
        self.cache = TTLCache()

    def config(self) -> dict:
        s = self.settings
        return {"env": s.env, "ticker": s.ticker, "live_trading_enabled": s.live_trading_enabled,
                "has_credentials": s.has_credentials, "contract_btc": str(Decimal(1) / CONTRACT_BTC)}

    def snapshot(self) -> dict:
        def build():
            c = self.client
            m = c.market()
            ob = c.orderbook(depth=15)
            fund = c.funding_estimate()
            status = c.exchange_status()
            return {
                "server_ts_ms": int(time.time() * 1000),
                "status": status,
                "market": {k: m.get(k) for k in (
                    "ticker", "title", "status", "price", "bid", "ask", "tick_size", "open_interest",
                    "open_interest_notional_value_dollars", "volume_24h", "volume_24h_notional_value_dollars",
                    "settlement_mark_price", "liquidation_mark_price", "reference_price", "leverage_estimate")},
                "book": {"bids": ob.bids, "asks": ob.asks, "mid": ob.mid, "spread": ob.spread},
                "funding": fund,
            }
        return self.cache.get("snapshot", 1.0, build)

    def candles(self) -> dict:
        def build():
            now = int(time.time())
            raw = self.client.candlesticks(now - 86400, now, period_interval=1)
            out, prev_close = [], None
            for c in raw:
                p = c["price"]
                close = p.get("close")
                if close is None:
                    # No trades this minute: carry the previous close as a flat bar.
                    close = p.get("previous") or prev_close
                    if close is None:
                        continue
                    o = h = l = close
                else:
                    o, h, l = p["open"], p["high"], p["low"]
                bid_c, ask_c = c["bid"]["close"], c["ask"]["close"]
                spread = None
                if bid_c and ask_c and Decimal(bid_c) > 0 and Decimal(ask_c) < SENTINEL_MAX:
                    spread = str(Decimal(ask_c) - Decimal(bid_c))
                out.append({"t": c["end_period_ts"], "o": o, "h": h, "l": l, "c": close,
                            "v": c["volume"], "spread": spread})
                prev_close = close
            return {"interval_s": 60, "candles": out}
        return self.cache.get("candles", 30.0, build)

    def account(self) -> dict:
        def build():
            if not self.settings.has_credentials:
                return {"available": False, "reason": "no credentials configured"}
            bal = self.client.balance()
            res = {"available": bal.available, "balance_status": bal.status,
                   "balance": bal.data, "balance_reason": bal.reason}
            try:
                res["positions"] = self.client.positions(ticker=self.settings.ticker)
                fees = self.client.fee_tiers()
                res["fees"] = {"maker": fees["maker_fee_rates"].get(self.settings.ticker),
                               "taker": fees["taker_fee_rates"].get(self.settings.ticker)}
            except KalshiAPIError as e:
                res["error"] = str(e)
            return res
        return self.cache.get("account", 5.0, build)


def make_handler(app: Dashboard):
    routes = {"/api/config": app.config, "/api/snapshot": app.snapshot,
              "/api/candles": app.candles, "/api/account": app.account}
    content_types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if not self.path.startswith("/api/"):
                super().log_message(fmt, *args)

        def _send(self, status: int, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in routes:
                try:
                    body = json.dumps(routes[path](), default=_jsonable).encode()
                    self._send(200, body, "application/json")
                except KalshiAPIError as e:
                    self._send(502, json.dumps({"error": str(e)}).encode(), "application/json")
                except Exception as e:  # network hiccups etc. — keep serving
                    self._send(502, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")
                return
            rel = "index.html" if path == "/" else path.lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC not in f.parents or not f.is_file():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, f.read_bytes(), content_types.get(f.suffix, "application/octet-stream"))

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    app = Dashboard()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    s = app.settings
    print(f"Dashboard: http://localhost:{args.port}  (env={s.env}, ticker={s.ticker}, "
          f"live_trading={'ON' if s.live_trading_enabled else 'off'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
