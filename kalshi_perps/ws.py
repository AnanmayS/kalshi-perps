"""Authenticated WebSocket collector for Kalshi perps market data.

Connects to the margin WebSocket with signed handshake headers (same scheme as
REST, signing GET + /trade-api/ws/v2/margin), subscribes to `orderbook_delta`,
`ticker` and `trade`, keeps a local order book, and writes to MarketStore:

  * trades           every public trade
  * tickers          ticker updates (<= 1/s per market: last, bid/ask, mark, index, funding)
  * quotes           a row each time top-of-book (best bid/ask price or size) changes
  * book_snapshots   top N levels every few seconds

Order book integrity: messages carry a per-subscription `seq`. A gap means a
delta was missed, so the local book is marked invalid (nothing is recorded from
it) and a fresh snapshot is requested via `update_subscription` / `get_snapshot`.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Callable

from .auth import KalshiSigner
from .config import Settings
from .store import MarketStore

WS_PATH = "/trade-api/ws/v2/margin"


class LocalBook:
    def __init__(self):
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.valid = False

    def load_snapshot(self, msg: dict) -> None:
        self.bids = {Decimal(p): Decimal(q) for p, q in msg.get("bid") or [] if Decimal(q) > 0}
        self.asks = {Decimal(p): Decimal(q) for p, q in msg.get("ask") or [] if Decimal(q) > 0}
        self.valid = True

    def apply_delta(self, side: str, price: str, delta: str) -> None:
        book = self.bids if side == "bid" else self.asks
        p = Decimal(price)
        size = book.get(p, Decimal(0)) + Decimal(delta)
        if size <= 0:
            book.pop(p, None)
        else:
            book[p] = size

    def top(self, n: int = 25) -> tuple[list, list]:
        bids = sorted(self.bids.items(), key=lambda l: l[0], reverse=True)[:n]
        asks = sorted(self.asks.items(), key=lambda l: l[0])[:n]
        return bids, asks

    def best(self) -> tuple:
        bids, asks = self.top(1)
        b = bids[0] if bids else (None, None)
        a = asks[0] if asks else (None, None)
        return b[0], b[1], a[0], a[1]

    @property
    def crossed(self) -> bool:
        bb, _, ba, _ = self.best()
        return bb is not None and ba is not None and bb >= ba


class Collector:
    def __init__(self, settings: Settings, store: MarketStore, ticker: str | None = None,
                 snapshot_every: float = 5.0, book_levels: int = 25, log: Callable[[str], None] = print):
        if not settings.has_credentials:
            raise RuntimeError("The WebSocket requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        self.settings = settings
        self.signer = KalshiSigner.from_file(settings.api_key_id, settings.private_key_path)
        self.store = store
        self.ticker = ticker or settings.ticker
        self.snapshot_every = snapshot_every
        self.book_levels = book_levels
        self.log = log
        self.book = LocalBook()
        self.sid_channel: dict[int, str] = {}
        self.last_seq: dict[int, int] = {}
        self.outbox: list[dict] = []
        self._next_id = 1
        self._last_top = None
        self._last_book_write = 0.0
        self.stats = {"messages": 0, "trades": 0, "tickers": 0, "quotes": 0, "deltas": 0,
                      "snapshots": 0, "book_writes": 0, "gaps": 0, "errors": 0, "reconnects": 0}

    # ---- commands ---------------------------------------------------------------

    def _cmd(self, cmd: str, params: dict) -> dict:
        c = {"id": self._next_id, "cmd": cmd, "params": params}
        self._next_id += 1
        return c

    def subscribe_commands(self) -> list[dict]:
        return [
            self._cmd("subscribe", {"channels": ["orderbook_delta"], "market_ticker": self.ticker}),
            self._cmd("subscribe", {"channels": ["ticker"], "market_ticker": self.ticker}),
            self._cmd("subscribe", {"channels": ["trade"], "market_ticker": self.ticker}),
        ]

    # ---- message handling (pure, testable) ---------------------------------------

    def _check_seq(self, sid: int, seq: int | None) -> bool:
        """True if `seq` follows the previous one for this subscription."""
        if seq is None:
            return True
        prev = self.last_seq.get(sid)
        self.last_seq[sid] = seq
        return prev is None or seq == prev + 1

    def handle(self, m: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.stats["messages"] += 1
        t, sid, seq, msg = m.get("type"), m.get("sid"), m.get("seq"), m.get("msg") or {}

        if t == "subscribed":
            self.sid_channel[msg["sid"]] = msg["channel"]
            self.log(f"subscribed {msg['channel']} (sid {msg['sid']})")
        elif t == "error":
            self.stats["errors"] += 1
            self.log(f"server error: {msg}")
        elif t == "orderbook_snapshot":
            self.last_seq[sid] = seq
            self.book.load_snapshot(msg)
            self.stats["snapshots"] += 1
            self._record_top(now)
            self._write_book(now)
        elif t == "orderbook_delta":
            self.stats["deltas"] += 1
            if not self._check_seq(sid, seq):
                self._on_gap(sid, "orderbook_delta")
                return
            if not self.book.valid:
                return  # waiting for a fresh snapshot
            self.book.apply_delta(msg["side"], msg["price"], msg["delta"])
            self._record_top(msg.get("ts_ms") and msg["ts_ms"] / 1000 or now)
        elif t == "ticker":
            self.stats["tickers"] += 1
            self.store.add_ticker(msg)
        elif t == "trade":
            if not self._check_seq(sid, seq):
                self.stats["gaps"] += 1
                self.log(f"trade seq gap on sid {sid}; some trades may be missing")
            self.stats["trades"] += 1
            self.store.add_trade(msg)

        if self.book.valid and now - self._last_book_write >= self.snapshot_every:
            self._write_book(now)

    def _on_gap(self, sid: int, channel: str) -> None:
        self.stats["gaps"] += 1
        self.book.valid = False
        self._last_top = None
        self.log(f"{channel} seq gap on sid {sid}; book invalidated, requesting fresh snapshot")
        self.outbox.append(self._cmd("update_subscription",
                                     {"sids": [sid], "market_tickers": [self.ticker], "action": "get_snapshot"}))

    def _record_top(self, ts: float) -> None:
        if not self.book.valid:
            return
        top = self.book.best()
        if top != self._last_top:
            self._last_top = top
            self.store.add_quote(self.ticker, int(ts * 1000), *top)
            self.stats["quotes"] += 1

    def _write_book(self, now: float) -> None:
        bids, asks = self.book.top(self.book_levels)
        self.store.add_book_snapshot(self.ticker, int(now * 1000), bids, asks)
        self._last_book_write = now
        self.stats["book_writes"] += 1

    def status_line(self) -> str:
        bb, bs, ba, as_ = self.book.best()
        spread = f"{ba - bb:.4f}" if bb is not None and ba is not None else "—"
        s = self.stats
        return (f"book {'OK' if self.book.valid else 'INVALID'} bid {bb} x {bs} / ask {ba} x {as_} "
                f"spread {spread} | msgs {s['messages']} deltas {s['deltas']} trades {s['trades']} "
                f"tickers {s['tickers']} quotes {s['quotes']} gaps {s['gaps']} reconnects {s['reconnects']}")

    # ---- network -------------------------------------------------------------------

    def handshake_headers(self) -> dict:
        return self.signer.headers("GET", WS_PATH)

    async def _session(self, deadline: float | None, status_every: float) -> None:
        import websockets

        self.book.valid = False
        self.last_seq.clear()
        self.sid_channel.clear()
        async with websockets.connect(self.settings.ws_url, additional_headers=self.handshake_headers(),
                                      ping_interval=20, ping_timeout=20, max_size=2 ** 22) as ws:
            self.log(f"connected {self.settings.ws_url}")
            for c in self.subscribe_commands():
                await ws.send(json.dumps(c))
            last_status = last_commit = time.time()
            while True:
                timeout = 1.0 if deadline is None else max(0.0, min(1.0, deadline - time.time()))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    self.handle(json.loads(raw))
                except asyncio.TimeoutError:
                    pass
                while self.outbox:
                    await ws.send(json.dumps(self.outbox.pop(0)))
                now = time.time()
                if now - last_commit >= 1:
                    self.store.commit()
                    last_commit = now
                if now - last_status >= status_every:
                    self.log(self.status_line())
                    last_status = now
                if deadline is not None and now >= deadline:
                    return

    async def run(self, duration: float | None = None, status_every: float = 10.0) -> None:
        deadline = time.time() + duration if duration else None
        backoff = 1.0
        while deadline is None or time.time() < deadline:
            started = time.time()
            try:
                await self._session(deadline, status_every)
                if deadline is not None and time.time() >= deadline:
                    break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network drop, handshake failure, etc.
                self.stats["reconnects"] += 1
                self.log(f"connection error: {type(e).__name__}: {e}; reconnecting in {backoff:.0f}s")
            finally:
                self.store.commit()
            if time.time() - started > 60:
                backoff = 1.0  # the session was healthy; reset
            await asyncio.sleep(backoff if deadline is None else min(backoff, max(0.0, deadline - time.time())))
            backoff = min(backoff * 2, 30.0)
