"""Local dashboard server.

    python dashboard/server.py            # http://localhost:8765

Serves a single page plus a small JSON API that proxies Kalshi's perps REST
endpoints (with short caches so the browser can poll without hammering Kalshi).
This server never sends orders to Kalshi. The paper trading panel fills
simulated taker orders against the live order book (kalshi_perps.paper), with
state persisted to data/paper_state.json.
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
from kalshi_perps.accounting import DEFAULT_TAKER_FEE_RATE, Position  # noqa: E402
from kalshi_perps.client import D, parse_ts, sane_mark  # noqa: E402
from kalshi_perps.paper import OrderRejected, PaperBroker  # noqa: E402
from risk import RiskConfig  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
PAPER_STATE = ROOT / "data" / "paper_state.json"
RUNNER_STATUS = ROOT / "data" / "runner" / "status.json"
RUNNER_STATE = ROOT / "data" / "runner" / "paper_state.json"
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


REPO_URL = "https://github.com/AnanmayS/kalshi-perps"


class Dashboard:
    def __init__(self, public: bool = False):
        # public=True is the read-only internet-facing view: no manual paper account,
        # no POST endpoints, slower caches. The private instance keeps full control.
        self.public = public
        self.settings = load_settings()
        self.client = KalshiPerpsClient(self.settings)
        self.cache = TTLCache()
        self.lock = threading.RLock()
        self.fee_rate, self.fee_source = self._taker_fee_rate()
        self.last_funding_check = 0.0
        self.broker = None
        if public:
            return
        self.broker = PaperBroker.open(
            PAPER_STATE,
            RiskConfig(self.settings.max_notional_per_trade, self.settings.daily_loss_limit),
            taker_fee_rate=self.fee_rate,
        )
        self.broker.taker_fee_rate = self.fee_rate  # always use the current rate, not a persisted one

    def _taker_fee_rate(self) -> tuple[Decimal, str]:
        if self.settings.has_credentials:
            try:
                rate = self.client.fee_tiers()["taker_fee_rates"].get(self.settings.ticker)
                if rate is not None:
                    return Decimal(str(rate)), "your Kalshi fee tier"
            except Exception:
                pass
        return DEFAULT_TAKER_FEE_RATE, "default (no API key)"

    def config(self) -> dict:
        s = self.settings
        return {"env": s.env, "ticker": s.ticker, "live_trading_enabled": s.live_trading_enabled,
                "has_credentials": s.has_credentials, "contract_btc": str(Decimal(1) / CONTRACT_BTC),
                "taker_fee_rate": self.fee_rate, "fee_source": self.fee_source,
                "public": self.public, "repo_url": REPO_URL}

    def snapshot(self) -> dict:
        def build():
            c = self.client
            m = c.market()
            ob = c.orderbook(depth=15)
            # These change slowly; cache them separately so the fast path is just market + book.
            fund = self.cache.get("funding_estimate", 15.0, c.funding_estimate)
            status = self.cache.get("exchange_status", 15.0, c.exchange_status)
            return {
                "server_ts_ms": int(time.time() * 1000),
                "status": status,
                "market": {k: m.get(k) for k in (
                    "ticker", "title", "status", "price", "bid", "ask", "tick_size", "open_interest",
                    "open_interest_notional_value_dollars", "volume_24h", "volume_24h_notional_value_dollars",
                    "settlement_mark_price", "liquidation_mark_price", "reference_price", "leverage_estimate")},
                "book": {"bids": ob.bids, "asks": ob.asks, "mid": ob.mid, "spread": ob.spread},
                "mark": sane_mark(m, ob.mid),
                "funding": fund,
            }
        snap = self.cache.get("snapshot", 1.0, build)  # at most one Kalshi fetch/s however many viewers
        if self.broker is not None:
            self._paper_tick(snap)
        return snap

    # ---- paper trading ------------------------------------------------------

    @staticmethod
    def mark_of(snap: dict) -> Decimal | None:
        return sane_mark(snap["market"], D(snap["book"]["mid"]))

    def full_book(self):
        return self.cache.get("full_book", 1.0, lambda: self.client.orderbook())

    def _paper_tick(self, snap: dict) -> None:
        with self.lock:
            self.broker.mark_to_market(self.mark_of(snap))
        # Settle funding events that happened while the paper position was held.
        if time.time() - self.last_funding_check < 30:
            return
        self.last_funding_check = time.time()
        now = int(time.time())
        since = int(min(self.broker.funding_checked_until, now - 2 * 86400))  # events publish late; look back
        try:
            events = self.client.funding_rates_history(start_ts=since, end_ts=now)
        except Exception:
            return
        with self.lock:
            self.broker.settle_funding(events, now, parse_ts)

    def runner_status(self) -> dict:
        """Read-only view of scripts/paper_run.py (a separate process) via its status file."""
        if not RUNNER_STATUS.is_file():
            return {"exists": False, "running": False}
        try:
            st = json.loads(RUNNER_STATUS.read_text())
        except (ValueError, OSError):
            return {"exists": True, "running": False, "error": "status file unreadable"}
        age = time.time() - float(st.get("heartbeat_ts") or 0)
        st.update(exists=True, running=age < 15, age_s=round(age, 1))
        return st

    def runner_history(self) -> dict:
        """Equity curve of the strategy runner's paper account, rebuilt from its saved fills and
        funding payments, marked at each candle's bid/ask mid. Nothing is stored by the runner,
        so this also covers the time before the chart existed."""
        def build():
            if not RUNNER_STATE.is_file():
                return {"points": []}
            st = json.loads(RUNNER_STATE.read_text())
            fills = sorted(st.get("fills", []), key=lambda f: f["ts"])
            if not fills:
                return {"points": [], "starting_cash": st.get("starting_cash")}
            funding = sorted((parse_ts(e["funding_time"]).timestamp(), D(e["paid"]))
                             for e in st.get("events", []) if e.get("kind") == "funding")
            start = int(parse_ts(fills[0]["ts"]).timestamp()) - 1800
            now = int(time.time())
            span_min = (now - start) // 60
            interval = 1 if span_min <= 4500 else 60 if span_min <= 60 * 4500 else 1440
            candles = self.client.candlesticks(start, now, period_interval=interval)
            points = []
            cash = D(st["starting_cash"])
            pos = Position()
            fees = funding_recv = Decimal(0)
            fi = ei = 0
            last_mid = None
            for c in candles:
                t = c["end_period_ts"]
                while fi < len(fills) and parse_ts(fills[fi]["ts"]).timestamp() <= t:
                    f = fills[fi]
                    qty = D(f["filled"]) * (1 if f["side"] == "buy" else -1)
                    r = pos.apply_fill(qty, D(f["vwap"]))
                    cash += r.realized_pnl - D(f["fee"])
                    fees += D(f["fee"])
                    fi += 1
                while ei < len(funding) and funding[ei][0] <= t:
                    cash -= funding[ei][1]
                    funding_recv -= funding[ei][1]
                    ei += 1
                # Mark at the bid/ask mid only when the quote is sane: the thin demo book sometimes
                # empties for a moment at the minute boundary, which would draw fake spikes.
                bid, ask = c["bid"].get("close"), c["ask"].get("close")
                if bid and ask and 0 < D(bid) < D(ask) < 1000 and (D(ask) - D(bid)) / D(bid) < Decimal("0.005"):
                    last_mid = (D(bid) + D(ask)) / 2
                if last_mid is None:
                    continue
                mid = last_mid
                equity = cash + pos.unrealized_pnl(mid)
                points.append([t, round(float(equity), 2), round(float(funding_recv), 2), round(float(fees), 2)])
            return {"starting_cash": float(D(st["starting_cash"])), "interval_min": interval, "points": points}
        return self.cache.get("runner_history", 60.0, build)

    def paper_state(self) -> dict:
        snap = self.snapshot()
        with self.lock:
            st = self.broker.state(self.mark_of(snap))
        st["fee_source"] = self.fee_source
        return st

    def _order_args(self, body: dict) -> dict:
        side = body.get("side")
        if side not in ("buy", "sell"):
            raise OrderRejected("side must be buy or sell")
        try:
            count = Decimal(str(body.get("count")))
            limit = body.get("limit_price")
            limit = Decimal(str(limit)) if limit not in (None, "") else None
        except Exception:
            raise OrderRejected("invalid number")
        return {"side": side, "count": count, "limit_price": limit, "reduce_only": bool(body.get("reduce_only"))}

    def paper_preview(self, body: dict) -> dict:
        args = self._order_args(body)
        ob, mark = self.full_book(), self.mark_of(self.snapshot())
        with self.lock:
            return self.broker.preview(bids=ob.bids, asks=ob.asks, mark=mark, **args)

    def paper_order(self, body: dict) -> dict:
        args = self._order_args(body)
        ob = self.client.orderbook()  # fresh book for the actual fill
        mark = self.mark_of(self.snapshot())
        with self.lock:
            return {"fill": self.broker.place(bids=ob.bids, asks=ob.asks, mark=mark, **args)}

    def paper_close(self, body: dict) -> dict:
        ob = self.client.orderbook()
        mark = self.mark_of(self.snapshot())
        with self.lock:
            return {"fill": self.broker.close_position(ob.bids, ob.asks, mark)}

    def paper_kill(self, body: dict) -> dict:
        with self.lock:
            self.broker.risk.kill("manual kill switch")
            self.broker._save()
        return self.paper_state()

    def paper_reset_kill(self, body: dict) -> dict:
        with self.lock:
            self.broker.risk.reset()
            self.broker._save()
        return self.paper_state()

    def paper_reset_account(self, body: dict) -> dict:
        with self.lock:
            if PAPER_STATE.exists():
                PAPER_STATE.unlink()
            self.broker = PaperBroker.open(
                PAPER_STATE, RiskConfig(self.settings.max_notional_per_trade, self.settings.daily_loss_limit),
                taker_fee_rate=self.fee_rate)
            self.broker._save()
        return self.paper_state()

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
              "/api/candles": app.candles, "/api/account": app.account, "/api/paper/state": app.paper_state,
              "/api/runner": app.runner_status, "/api/runner/history": app.runner_history}
    post_routes = {"/api/paper/preview": app.paper_preview, "/api/paper/order": app.paper_order,
                   "/api/paper/close": app.paper_close, "/api/paper/kill": app.paper_kill,
                   "/api/paper/reset_kill": app.paper_reset_kill, "/api/paper/reset": app.paper_reset_account}
    if app.public:
        routes.pop("/api/paper/state")
        post_routes = {}  # read-only: nothing on the public site can change state
    content_types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if not self.path.startswith("/api/"):
                super().log_message(fmt, *args)

        def _send(self, status: int, body: bytes, ctype: str):
            if status >= 500:
                sys.stderr.write(f"[api] {self.command} {self.path} -> {status}: {body[:300].decode(errors='replace')}\n")
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
            self._serve_static(path)

        def do_POST(self):
            path = urlparse(self.path).path
            if app.public:
                self._send(403, b'{"error": "read-only public dashboard"}', "application/json")
                return
            if path not in post_routes:
                self._send(404, b"not found", "text/plain")
                return
            # Requiring a JSON content type forces a CORS preflight for cross-site
            # requests, which this server never answers, so other sites can't post here.
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._send(415, b'{"error": "expected application/json"}', "application/json")
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 10_000)
                body = json.loads(self.rfile.read(length) or b"{}")
                self._send(200, json.dumps(post_routes[path](body), default=_jsonable).encode(), "application/json")
            except OrderRejected as e:
                self._send(422, json.dumps({"error": str(e)}).encode(), "application/json")
            except Exception as e:
                self._send(502, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")

        def _serve_static(self, path):
            rel = "index.html" if path == "/" else path.lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC not in f.parents or not f.is_file():
                self._send(404, b"not found", "text/plain")
                return
            body = f.read_bytes()
            if app.public and f.name == "index.html":
                # Mark the page public before any script runs, so the simplified layout renders first.
                body = body.replace(b"<body>", b'<body class="public">', 1)
            self._send(200, body, content_types.get(f.suffix, "application/octet-stream"))

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--public", action="store_true",
                    help="read-only mode for exposing behind a reverse proxy: no controls, no manual paper account")
    args = ap.parse_args()
    app = Dashboard(public=args.public)
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
