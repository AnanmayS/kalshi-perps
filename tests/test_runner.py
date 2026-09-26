from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from kalshi_perps.client import Orderbook
from kalshi_perps.paper import PaperBroker
from risk import RiskConfig, RiskEngine
from runner.paper import PaperRunner
from strategies.base import Strategy

T0 = 1_790_000_000 // 60 * 60  # a minute boundary


def candle(ts, bid, ask, close=None):
    c = close or str((D(bid) + D(ask)) / 2)
    return {"end_period_ts": ts, "price": {"open": c, "high": c, "low": c, "close": c, "previous": c},
            "bid": {"open": bid, "high": bid, "low": bid, "close": bid},
            "ask": {"open": ask, "high": ask, "low": ask, "close": ask}, "volume": "1.00"}


class FakeClient:
    def __init__(self):
        self.candles = {}
        self.book = Orderbook(bids=[(D("8.49"), D("100"))], asks=[(D("8.51"), D("100"))])
        self.mark = "8.50"
        self.funding = []
        self.order_calls = 0

    def candlesticks(self, a, b, period_interval, ticker):
        return [c for t, c in sorted(self.candles.items()) if a <= t <= b]

    def orderbook(self, ticker=None, depth=None):
        return self.book

    def market(self, ticker=None):
        # Like the real API: last trade and top of book alongside the mark.
        bid, ask = self.book.best_bid, self.book.best_ask
        return {"price": self.mark, "bid": str(bid) if bid else None, "ask": str(ask) if ask else None,
                "settlement_mark_price": {"price": self.mark, "ts_ms": 0}}

    def funding_rates_history(self, ticker=None, start_ts=None, end_ts=None):
        return self.funding

    def create_order(self, *a, **k):  # must never be called
        self.order_calls += 1
        raise AssertionError("runner tried to send a real order")


class Script(Strategy):
    name = "script"

    def __init__(self, targets):
        self.targets, self.seen = targets, []

    def on_bar(self, bar, position):
        self.seen.append(bar.ts)
        t = self.targets.get(bar.ts)
        return None if t is None else D(t)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(targets, loss_limit="100", flatten=True):
    client, clock = FakeClient(), Clock(T0 + 10)
    for i in range(5):
        client.candles[T0 - 60 * i] = candle(T0 - 60 * i, "8.49", "8.51")
    risk = RiskEngine(RiskConfig(D("1000"), D(loss_limit)), D("10000"), datetime.fromtimestamp(T0, timezone.utc))
    broker = PaperBroker(risk, D("10000"), taker_fee_rate=D("0.0012"))
    broker.funding_checked_until = T0
    strat = Script(targets)
    r = PaperRunner(client, broker, strat, ticker="KXBTCPERP1", clock=clock, log=lambda m: None,
                    warmup_minutes=10, flatten_on_kill=flatten)
    return r, client, clock, strat


def test_warmup_never_trades():
    r, client, clock, strat = make({T0: 10, T0 - 60: 10})
    r.warmup()
    assert strat.seen[-1] == T0 and r.broker.fills == [] and r.last_bar_ts == T0


def test_trades_on_new_bar_against_live_book_as_taker():
    r, client, clock, strat = make({T0 + 60: 10})
    r.warmup()
    client.candles[T0 + 60] = candle(T0 + 60, "8.49", "8.51")
    clock.t = T0 + 60 + 1           # candle not yet past the grace period
    r.step()
    assert r.broker.fills == []
    clock.t = T0 + 60 + 4
    r.step()
    f = r.broker.fills[-1]
    assert f["side"] == "buy" and f["vwap"] == D("8.51") and f["liquidity"] == "taker"
    assert r.broker.position.qty == 10 and client.order_calls == 0


def test_each_bar_processed_once():
    r, client, clock, strat = make({})
    r.warmup()
    client.candles[T0 + 60] = candle(T0 + 60, "8.49", "8.51")
    for dt in (4, 5, 30, 50):
        clock.t = T0 + 60 + dt
        r.step()
    assert strat.seen.count(T0 + 60) == 1


def test_missing_candle_is_skipped_after_giveup():
    r, client, clock, strat = make({})
    r.warmup()
    clock.t = T0 + 60 + 50           # nothing published for T0+60
    r.step()
    assert r.last_bar_ts == T0 + 60 and r.missing_bars == 1
    client.candles[T0 + 120] = candle(T0 + 120, "8.49", "8.51")
    clock.t = T0 + 120 + 4
    r.step()
    assert strat.seen[-1] == T0 + 120


def test_kill_switch_flattens_and_blocks_new_risk():
    r, client, clock, strat = make({T0 + 60: 10, T0 + 120: -10}, loss_limit="1")
    r.warmup()
    client.candles[T0 + 60] = candle(T0 + 60, "8.49", "8.51")
    clock.t = T0 + 64
    r.step()
    assert r.broker.position.qty == 10
    client.mark = "8.30"             # long 10 now down ~$2 > $1 limit
    client.book = Orderbook(bids=[(D("8.29"), D("100"))], asks=[(D("8.31"), D("100"))])
    clock.t = T0 + 75
    r.step()
    assert r.broker.risk.killed and r.broker.position.qty == 0      # flattened
    client.candles[T0 + 120] = candle(T0 + 120, "8.29", "8.31")
    clock.t = T0 + 124
    r.step()                                                        # strategy wants short 10
    assert r.broker.position.qty == 0


def test_funding_settled_on_runner_position():
    r, client, clock, strat = make({T0 + 60: -10})
    r.warmup()
    client.candles[T0 + 60] = candle(T0 + 60, "8.49", "8.51")
    clock.t = T0 + 64
    r.step()
    client.funding = [{"funding_time": datetime.fromtimestamp(T0 + 90, timezone.utc).isoformat(),
                       "funding_rate": 0.001, "mark_price": "8.50"}]
    clock.t = T0 + 64 + 61
    r.step()
    assert r.broker.position.funding_paid == D("-0.085")          # short receives


def test_status_file(tmp_path):
    r, client, clock, strat = make({})
    r.status_path = tmp_path / "status.json"
    r.warmup()
    import json
    st = json.loads(r.status_path.read_text())
    assert st["strategy"] == "script" and st["paper"]["position"]["qty"] == "0"


def test_after_sleep_missed_bars_update_strategy_but_do_not_trade():
    r, client, clock, strat = make({T0 + 60: 10, T0 + 120: -10, T0 + 3600: 5})
    r.warmup()
    for k in range(1, 61):                       # an hour of candles published while "asleep"
        client.candles[T0 + 60 * k] = candle(T0 + 60 * k, "8.49", "8.51")
    clock.t = T0 + 3600 + 4                      # wake up just after the latest bar closed
    r.step()
    assert T0 + 60 in strat.seen and T0 + 120 in strat.seen   # strategy state kept current
    assert [f["position_after"] for f in r.broker.fills] == [D(5)]  # only the fresh bar traded
    assert r.stale_bars == 58                     # the bar closed 64s before wake is still fresh (< 90s)


def test_carry_strategy_gets_funding_in_warmup_and_live():
    from strategies import FundingCarry
    r, client, clock, _ = make({})
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    client.funding = [{"funding_time": iso(T0 - 3600 * k), "funding_rate": 0.005, "mark_price": "8.50"} for k in (1, 9, 17)]
    carry = FundingCarry(threshold_bps=20, window=3, size=D(10))
    r.strategy = carry
    r.warmup()
    assert len(carry.rates) == 3                     # warm from history, no trade yet
    assert r.broker.fills == []
    client.candles[T0 + 60] = candle(T0 + 60, "8.49", "8.51")
    clock.t = T0 + 64
    r.step()
    assert r.broker.position.qty == -10              # positive funding -> short to receive it
    client.funding.append({"funding_time": iso(T0 + 100), "funding_rate": -0.02, "mark_price": "8.50"})
    clock.t = T0 + 64 + 61
    r.step()
    assert carry.rates[-1] == D("-0.02")             # live event delivered after it happened


def test_slippage_guard_waits_for_book_to_refill():
    r, client, clock, strat = make({T0 + 60: -10})
    r.warmup()
    # Hollow book right after the minute: 1 contract near the mark, the rest 2% lower.
    client.book = Orderbook(bids=[(D("8.4900"), D("1")), (D("8.33"), D("100"))], asks=[(D("8.51"), D("100"))])
    client.candles[T0 + 60] = candle(T0 + 60, "8.49", "8.51")
    clock.t = T0 + 64
    r.step()
    assert r.broker.position.qty == -1 and r.pending["target"] == D(-10)     # took only the good part
    assert all(D(f["vwap"]) >= D("8.4787") for f in r.broker.fills)          # never beyond 25 bps of 8.50
    client.book = Orderbook(bids=[(D("8.49"), D("100"))], asks=[(D("8.51"), D("100"))])
    clock.t = T0 + 70
    r.step()
    assert r.broker.position.qty == -10 and r.pending is None


def test_slippage_guard_gives_up_after_retry_window():
    r, client, clock, strat = make({T0 + 60: 10})
    r.warmup()
    client.book = Orderbook(bids=[(D("8.49"), D("100"))], asks=[(D("8.70"), D("100"))])   # ask 2% away
    client.candles[T0 + 60] = candle(T0 + 60, "8.49", "8.51")
    clock.t = T0 + 64
    r.step()
    assert r.broker.fills == [] and r.pending is not None
    clock.t = T0 + 64 + 301
    r.step()
    assert r.pending is None and r.broker.position.qty == 0


def test_funding_published_late_is_still_settled_on_position_held_then():
    r, client, clock, _ = make({T0 + 60: -10, T0 + 180: 0})
    r.warmup()
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    for k in (1, 2, 3):
        client.candles[T0 + 60 * k] = candle(T0 + 60 * k, "8.49", "8.51")
    clock.t = T0 + 64
    r.step()                                             # short 10 at T0+60
    clock.t = T0 + 125
    r.step()                                             # funding due at T0+120: not published yet
    clock.t = T0 + 184
    r.step()                                             # strategy goes flat at T0+180
    assert r.broker.position.qty == 0
    # Kalshi publishes the T0+120 event only now, after the checkpoint time and after the position closed.
    client.funding = [{"funding_time": iso(T0 + 120), "funding_rate": 0.001, "mark_price": "8.50"}]
    clock.t = T0 + 250
    r.step()
    assert r.broker.position.funding_paid == D("-0.085")  # short 10 at T0+120 received it
    clock.t = T0 + 320
    r.step()
    assert r.broker.position.funding_paid == D("-0.085")  # and only once


def test_zero_settlement_mark_does_not_fake_pnl_or_trip_kill_switch():
    r, client, clock, _ = make({T0 + 60: -10}, loss_limit="1")
    r.warmup()
    client.candles[T0 + 60] = candle(T0 + 60, "8.49", "8.51")
    clock.t = T0 + 64
    r.step()                                            # short 10 @ 8.49
    client.market = lambda ticker=None: {"price": "8.50", "bid": "8.49", "ask": "8.51",
                                         "settlement_mark_price": {"price": "0.0000"}}
    clock.t = T0 + 75
    r.step()
    assert r.mark == D("8.50")                           # book mid, not the broken 0 mark
    assert r.broker.equity(r.mark) < D("10000")          # no phantom +$85 gain
    assert not r.broker.risk.killed


def test_implausible_mark_jump_is_ignored_unless_it_persists():
    r, client, clock, _ = make({})
    r.warmup()
    clock.t = T0 + 10
    r.step()
    assert r.mark == D("8.50")
    client.mark = "12.00"                                 # +41% in 5 seconds: a bad print
    client.book = Orderbook(bids=[(D("11.99"), D("100"))], asks=[(D("12.01"), D("100"))])
    for i in range(3):
        clock.t = T0 + 20 + 5 * i
        r.step()
    assert r.mark == D("8.50") and not r.broker.risk.killed
    for i in range(12):                                   # the new level holds for a minute: accept
        clock.t = T0 + 40 + 5 * i
        r.step()
    assert r.mark == D("12.00")
