"""Paper-trading runner: drives a strategy on live Kalshi data, fills on paper.

Every minute, once Kalshi publishes the just-closed 1m candle, the runner turns
it into the same Bar the backtester uses and asks the strategy for a target
position. A change in target becomes a paper taker order that walks the LIVE
order book (kalshi_perps.paper.PaperBroker), goes through the risk engine and
pays the taker fee. Nothing is ever sent to Kalshi: this module never calls
the client's order methods.

Parity with the backtester:
  * same Bar construction, same strategy code, same fees/position accounting;
  * the backtester fills at the next bar's opening quote; the runner fills a few
    seconds after the bar closes, against the full live book (so it also sees depth);
  * kill switch: targets are clipped so exposure can only shrink, and the position
    is flattened as soon as it trips (flatten_on_kill).
On start it replays recent candles to warm the strategy up but never trades on
them; only bars that close after start can trigger orders. Likewise, if the
runner falls behind (laptop asleep, network outage), missed bars are replayed
into the strategy WITHOUT trading: acting on a signal from minutes or hours ago
at today's price would be a fill no backtest would produce.
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from backtest.data import bar_from_candle
from kalshi_perps.accounting import ZERO, sign
from kalshi_perps.client import D, parse_ts
from kalshi_perps.paper import OrderRejected, PaperBroker
from strategies.base import Bar, Strategy

BAR_SECONDS = 60


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


class PaperRunner:
    def __init__(self, client, broker: PaperBroker, strategy: Strategy, *, ticker: str,
                 status_path: Path | None = None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, warmup_minutes: int = 360,
                 bar_grace: float = 3.0, bar_giveup: float = 45.0, mark_every: float = 5.0,
                 funding_every: float = 60.0, flatten_on_kill: bool = True, stale_after: float = 90.0,
                 max_slippage_bps: float = 25.0, retry_every: float = 5.0, retry_for: float = 300.0):
        self.client = client
        self.broker = broker
        self.strategy = strategy
        self.ticker = ticker
        self.status_path = status_path
        self.clock = clock
        self._log = log
        self.warmup_minutes = warmup_minutes
        self.bar_grace = bar_grace
        self.bar_giveup = bar_giveup
        self.mark_every = mark_every
        self.funding_every = funding_every
        self.flatten_on_kill = flatten_on_kill
        self.stale_after = stale_after  # bars older than this (seconds past close) never trade
        # Slippage guard: never fill more than this far from the mark. If the book is too
        # thin (the demo book sometimes empties for a moment right after the minute),
        # keep the unfilled remainder pending and retry instead of taking a bad price.
        self.max_slippage = Decimal(str(max_slippage_bps)) / 10_000
        self.retry_every = retry_every
        self.retry_for = retry_for
        self.pending: dict | None = None

        self.started_at = clock()
        self.last_bar_ts: int | None = None
        self.last_bar: Bar | None = None
        self.last_target: Decimal | None = None
        self.mark: Decimal | None = None
        self._last_mark_at = 0.0
        self._last_funding_at = 0.0
        self.errors = 0
        self.bars_seen = 0
        self.missing_bars = 0
        self.stale_bars = 0
        self.funding_seen_until: float = clock()
        self.recent: deque[str] = deque(maxlen=40)

    # ---- helpers ------------------------------------------------------------------

    def log(self, msg: str) -> None:
        line = f"{datetime.fromtimestamp(self.clock(), timezone.utc):%H:%M:%S} {msg}"
        self.recent.append(line)
        self._log(line)

    def _candles(self, start_ts: int, end_ts: int) -> list[dict]:
        return self.client.candlesticks(start_ts, end_ts, period_interval=1, ticker=self.ticker)

    # ---- lifecycle ----------------------------------------------------------------

    def warmup(self) -> int:
        """Feed recent closed bars to the strategy without trading on them."""
        now = self.clock()
        end = int(now) // BAR_SECONDS * BAR_SECONDS
        bars = [bar_from_candle(c) for c in self._candles(end - self.warmup_minutes * BAR_SECONDS, end)]
        bars = [b for b in bars if b.ts <= now - self.bar_grace]
        # Funding events that already happened, oldest first, so e.g. a carry strategy knows recent rates.
        fstart = end - max(self.warmup_minutes * BAR_SECONDS, 3 * 86400)
        events = sorted(self.client.funding_rates_history(ticker=self.ticker, start_ts=fstart, end_ts=int(now)),
                        key=lambda e: e["funding_time"])
        feed = [(parse_ts(e["funding_time"]).timestamp(), Decimal(str(e["funding_rate"]))) for e in events]
        fi = 0
        for b in bars:
            while fi < len(feed) and feed[fi][0] <= b.ts:
                self.strategy.on_funding(int(feed[fi][0]), feed[fi][1])
                fi += 1
            self.strategy.on_bar(b, self.broker.position.qty)  # signal ignored on purpose
        for t, r in feed[fi:]:
            self.strategy.on_funding(int(t), r)
        self.funding_seen_until = now
        if bars:
            self.last_bar_ts, self.last_bar = bars[-1].ts, bars[-1]
        else:
            self.last_bar_ts = end - BAR_SECONDS
        self.log(f"warmed up on {len(bars)} bars; trading starts with the bar closing after "
                 f"{_iso(self.last_bar_ts)}")
        self.write_status()
        return len(bars)

    def step(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        try:
            if now - self._last_mark_at >= self.mark_every:
                self._update_mark(now)
            if now - self._last_funding_at >= self.funding_every:
                self._settle_funding(now)
            if self.last_bar_ts is not None and now >= self.last_bar_ts + BAR_SECONDS + self.bar_grace:
                self._process_new_bars(now)
            if self.pending and now - self.pending["last_try"] >= self.retry_every:
                self._retry_pending(now)
        except Exception as e:  # keep running through network hiccups
            self.errors += 1
            self.log(f"error: {type(e).__name__}: {e}")
        self.write_status(now)

    def run(self, duration: float | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        if self.last_bar_ts is None:
            self.warmup()
        deadline = None if duration is None else self.clock() + duration
        while deadline is None or self.clock() < deadline:
            self.step()
            sleep(1.0)

    # ---- pieces -------------------------------------------------------------------

    def _update_mark(self, now: float) -> None:
        m = self.client.market(self.ticker)
        mark = (m.get("settlement_mark_price") or {}).get("price")
        self.mark = D(mark) if mark else None
        self._last_mark_at = now
        was_killed = self.broker.risk.killed
        self.broker.mark_to_market(self.mark)
        if self.broker.risk.killed and not was_killed:
            self.log(f"KILL SWITCH: {self.broker.risk.kill_reason}")
            if self.flatten_on_kill and self.broker.position.qty != 0:
                self._execute(ZERO, "kill switch flatten")

    def _settle_funding(self, now: float) -> None:
        since = int(self.broker.funding_checked_until)
        events = self.client.funding_rates_history(ticker=self.ticker, start_ts=since - 60, end_ts=int(now))
        for ev in sorted(events, key=lambda e: e["funding_time"]):
            t = parse_ts(ev["funding_time"]).timestamp()
            if self.funding_seen_until < t <= now:
                self.strategy.on_funding(int(t), Decimal(str(ev["funding_rate"])))
                self.log(f"funding event {ev['funding_time']}: rate {Decimal(str(ev['funding_rate'])) * 100:.4f}%")
        self.funding_seen_until = now
        for paid in self.broker.settle_funding(events, int(now), parse_ts):
            if paid:
                self.log(f"funding settled: {'paid' if paid > 0 else 'received'} ${abs(paid):.4f}")
        self._last_funding_at = now

    def _process_new_bars(self, now: float) -> None:
        start = self.last_bar_ts
        bars = [bar_from_candle(c) for c in self._candles(start, int(now))]
        bars = sorted((b for b in bars if b.ts > start and b.ts <= now - self.bar_grace), key=lambda b: b.ts)
        if not bars:
            expected = start + BAR_SECONDS
            if now >= expected + self.bar_giveup:
                # Kalshi publishes no candle for some minutes; the backtester skips them too.
                self.missing_bars += 1
                self.last_bar_ts = expected
                self.log(f"no candle for {_iso(expected)}; skipping it")
            return
        stale = [b for b in bars if now - b.ts > self.stale_after]
        if stale:
            self.stale_bars += len(stale)
            self.log(f"catching up on {len(stale)} missed bar(s) since {_iso(stale[0].ts)[11:16]} "
                     f"(asleep or offline?); updating the strategy without trading on them")
        for b in bars:
            self._on_bar(b, trade=b not in stale)

    def _on_bar(self, bar: Bar, trade: bool = True) -> None:
        self.bars_seen += 1
        self.last_bar_ts, self.last_bar = bar.ts, bar
        qty = self.broker.position.qty
        target = self.strategy.on_bar(bar, qty)
        if not trade:
            return
        sig = getattr(self.strategy, "last_signal_bps", None)
        sig_txt = "" if sig is None else f" signal {sig:+.1f} bps"
        if target is None or Decimal(target) == qty:
            self.log(f"bar {_iso(bar.ts)[11:16]} mid {bar.mid}{sig_txt} -> hold {qty}")
            return
        self.last_target = Decimal(target)
        self.log(f"bar {_iso(bar.ts)[11:16]} mid {bar.mid}{sig_txt} -> target {target} (from {qty})")
        self.pending = None  # a fresh signal supersedes any unfinished order
        self._execute(Decimal(target), "strategy")

    def _retry_pending(self, now: float) -> None:
        p = self.pending
        if p["expires"] is not None and now > p["expires"]:
            self.log(f"{p['why']}: gave up reaching target {p['target']} within {self.max_slippage * 10_000:.0f} bps "
                     f"of the mark; position stays {self.broker.position.qty}")
            self.pending = None
            return
        self._update_mark(now)
        self._execute(p["target"], p["why"] + " (retry)", retry=True)

    def _execute(self, target: Decimal, why: str, retry: bool = False) -> None:
        now = self.clock()
        base_why = why.replace(" (retry)", "")
        qty = self.broker.position.qty
        if self.broker.risk.killed:
            # Same clip as the backtester: exposure may only shrink toward zero.
            target = ZERO if sign(target) != sign(qty) else sign(qty) * min(abs(target), abs(qty))
        delta = target - qty
        if delta == 0:
            self.pending = None
            return
        side = "buy" if delta > 0 else "sell"
        book = self.client.orderbook(self.ticker)
        ref = self.mark or (self.last_bar.mid if self.last_bar else None) or book.mid
        limit = None
        if ref is not None:
            limit = ref * (1 + self.max_slippage) if side == "buy" else ref * (1 - self.max_slippage)
            limit = limit.quantize(Decimal("0.0001"))
        reduces = qty != 0 and sign(delta) == -sign(qty) and abs(delta) <= abs(qty)

        def keep_pending(reason: str) -> None:
            if not retry:
                self.log(f"{why}: {reason}; retrying every {self.retry_every:.0f}s")
            expires = None if base_why.startswith("kill") else now + self.retry_for
            prev = self.pending
            self.pending = {"target": target, "why": base_why, "last_try": now,
                            "expires": prev["expires"] if (retry and prev) else expires}

        try:
            f = self.broker.place(side, abs(delta), book.bids, book.asks, self.mark,
                                  limit_price=limit, reduce_only=reduces)
        except OrderRejected as e:
            if "no liquidity" in str(e):
                keep_pending(f"no {side} liquidity within {self.max_slippage * 10_000:.0f} bps of mark {ref} "
                             f"(best {'ask' if side == 'buy' else 'bid'} {book.best_ask if side == 'buy' else book.best_bid})")
            else:
                self.log(f"{why}: {side} {abs(delta)} REJECTED: {e}")
                self.pending = None
            return
        self.log(f"{why}: {side} {f['filled']} @ {f['vwap']:.4f} TAKER fee ${f['fee']}; position now {f['position_after']}")
        if f["cancelled"]:
            keep_pending(f"{f['cancelled']} left unfilled within {self.max_slippage * 10_000:.0f} bps of the mark")
        else:
            self.pending = None

    # ---- status -------------------------------------------------------------------

    def status(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        st = self.broker.state(self.mark)
        return {
            "heartbeat": _iso(now),
            "heartbeat_ts": now,
            "started_at": _iso(self.started_at),
            "ticker": self.ticker,
            "strategy": self.strategy.name,
            "params": self.strategy.params(),
            "signal_bps": getattr(self.strategy, "last_signal_bps", None),
            "last_bar": _iso(self.last_bar_ts),
            "last_mid": self.last_bar.mid if self.last_bar else None,
            "last_target": self.last_target,
            "bars_seen": self.bars_seen,
            "missing_bars": self.missing_bars,
            "stale_bars": self.stale_bars,
            "errors": self.errors,
            "flatten_on_kill": self.flatten_on_kill,
            "pending": self.pending,
            "paper": st,
            "log": list(self.recent)[-20:],
        }

    def write_status(self, now: float | None = None) -> None:
        if self.status_path is None:
            return
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.status(now), default=lambda o: str(o) if isinstance(o, Decimal) else o))
        tmp.replace(self.status_path)
