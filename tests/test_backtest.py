from decimal import Decimal as D

import pytest

from backtest.engine import BacktestConfig, run_backtest
from risk import RiskConfig
from strategies import Momentum
from strategies.base import Bar, Strategy


def bar(ts, bid, ask, bid_open=None, ask_open=None, close=None):
    bid, ask = D(bid), D(ask)
    bo = D(bid_open) if bid_open is not None else bid
    ao = D(ask_open) if ask_open is not None else ask
    c = D(close) if close is not None else (bid + ask) / 2
    return Bar(ts, c, c, c, c, bo, bid, ao, ask, D(1))


class Script(Strategy):
    """Returns pre-set targets by bar index and records what it was shown."""
    name = "script"

    def __init__(self, targets):
        self.targets = targets
        self.seen = []

    def on_bar(self, b, position):
        self.seen.append(b.ts)
        t = self.targets.get(len(self.seen) - 1)
        return None if t is None else D(t)


def flat_bars(n, bid="8.49", ask="8.51"):
    return [bar(60 * (i + 1), bid, ask) for i in range(n)]


CFG = BacktestConfig(taker_fee_rate=D("0.0012"), risk=RiskConfig(D("100000"), D("100000")))


def test_orders_fill_at_next_bar_open_quote_not_signal_bar():
    bars = [bar(60, "8.49", "8.51"), bar(120, "8.59", "8.61", bid_open="8.55", ask_open="8.57")]
    res = run_backtest(bars, [], Script({0: 10}), CFG)
    t = res.trades[0]
    assert t.ts == 120 and t.side == "buy"
    assert t.price == D("8.57")          # next bar's ask_open, not 8.51 (signal bar) or 8.60 (mid)


def test_sells_hit_the_bid():
    bars = [bar(60, "8.49", "8.51"), bar(120, "8.49", "8.51", bid_open="8.48", ask_open="8.52")]
    res = run_backtest(bars, [], Script({0: -5}), CFG)
    assert res.trades[0].price == D("8.48")


def test_round_trip_on_flat_market_costs_spread_plus_two_taker_fees():
    res = run_backtest(flat_bars(4), [], Script({0: 10, 1: 0}), CFG)
    # buy 10 @ 8.51, sell 10 @ 8.49
    assert res.price_pnl == D("-0.20")
    assert res.spread_cost == D("0.20")
    assert res.fees == D("0.1022") + D("0.1019")           # ceil(85.1*.0012), ceil(84.9*.0012)
    assert res.net_pnl == D("-0.20") - res.fees


def test_no_fill_when_quote_side_is_empty():
    bars = [bar(60, "8.49", "8.51"), Bar(120, None, None, None, None, D("8.49"), D("8.49"), None, None, D(0))]
    res = run_backtest(bars, [], Script({0: 10}), CFG)
    assert res.trades == [] and res.rejected[0]["reason"] == "no quote on that side"


def test_strategy_never_sees_future_bars():
    bars = flat_bars(5)
    s = Script({})
    run_backtest(bars, [], s, CFG)
    assert s.seen == [60, 120, 180, 240, 300]      # called once per bar, in order, after each closes


def test_short_pnl_positive_when_price_falls():
    bars = [bar(60, "8.49", "8.51"), bar(120, "8.49", "8.51"), bar(180, "8.29", "8.31")]
    res = run_backtest(bars, [], Script({0: -10}), CFG)
    assert res.price_pnl == D("10") * (D("8.49") - D("8.30"))   # short @ bid 8.49, marked at mid 8.30


def test_funding_charged_on_position_held_at_funding_time():
    bars = flat_bars(4)
    funding = [(90, D("0.001"), D("8.50")),     # inside bar 120, after its open-time fill -> long 10 pays
               (150, D("0.001"), D("8.50")),    # still long 10 -> pays
               (500, D("0.001"), D("8.50"))]    # after the last bar -> ignored
    res = run_backtest(bars, funding, Script({0: 10}), CFG)
    # order from bar 60 fills at the open of bar 120, i.e. t=60; funding at 90 and 150 both see long 10
    assert [e["ts"] for e in res.funding_events] == [90, 150]
    assert res.funding == 2 * D("0.001") * 10 * D("8.50")


def test_short_receives_positive_funding():
    res = run_backtest(flat_bars(3), [(150, D("0.002"), D("8.50"))], Script({0: -10}), CFG)
    assert res.funding == -D("0.170")


def test_funding_before_entry_is_not_charged():
    res = run_backtest(flat_bars(3), [(30, D("0.01"), D("8.50"))], Script({1: 10}), CFG)
    assert res.funding_events == []


def test_risk_cap_rejects_oversized_order():
    cfg = BacktestConfig(taker_fee_rate=D("0.0012"), risk=RiskConfig(D("50"), D("100")))
    res = run_backtest(flat_bars(3), [], Script({0: 10}), cfg)
    assert res.trades == [] and "exceeds max" in res.rejected[0]["reason"]


def test_kill_switch_only_allows_reducing():
    cfg = BacktestConfig(taker_fee_rate=D("0.0012"), risk=RiskConfig(D("100000"), D("1")))
    bars = [bar(60, "8.49", "8.51"), bar(120, "8.49", "8.51"), bar(180, "8.30", "8.32"),
            bar(240, "8.30", "8.32"), bar(300, "8.30", "8.32")]
    # long 10, price drops ~$1.9 -> killed; then strategy tries to flip short 20: only the close happens
    res = run_backtest(bars, [], Script({0: 10, 2: -20}), cfg)
    assert res.killed_at == 180
    assert [t.position_after for t in res.trades] == [D(10), D(0)]


def test_equity_identity():
    res = run_backtest(flat_bars(10), [(200, D("0.001"), D("8.5"))], Script({0: 10, 5: -10, 8: 0}), CFG)
    assert res.net_pnl == res.price_pnl - res.fees - res.funding


def test_momentum_goes_long_on_uptrend_and_respects_threshold():
    m = Momentum(lookback=3, threshold_bps=40, exit_bps=5, size=D(2), min_hold=1)
    mids = ["8.50", "8.50", "8.50", "8.52", "8.60"]   # +0.24% then +1.2% over 3 bars
    out = [m.on_bar(bar(60 * i, D(p) - D("0.01"), D(p) + D("0.01")), D(0)) for i, p in enumerate(mids)]
    assert out[3] is None and out[4] == D(2)


def test_strategy_sees_funding_only_after_it_happens():
    seen = []

    class Spy(Strategy):
        name = "spy"

        def on_funding(self, ts, rate):
            seen.append(("f", ts))

        def on_bar(self, b, position):
            seen.append(("b", b.ts))
            return None

    run_backtest(flat_bars(3), [(150, D("0.001"), D("8.5"))], Spy(), CFG)
    assert seen.index(("f", 150)) > seen.index(("b", 120))    # not visible at the 120 bar
    assert seen.index(("f", 150)) < seen.index(("b", 180))    # visible by the 180 bar


def test_carry_shorts_when_funding_positive_and_collects_it():
    from strategies import FundingCarry
    funding = [(30, D("0.005"), D("8.5")), (200, D("0.005"), D("8.5"))]
    res = run_backtest(flat_bars(5), funding, FundingCarry(threshold_bps=20, window=1, size=D(10)), CFG)
    assert res.trades[0].side == "sell" and res.funding < 0   # negative = received


def test_carry_hysteresis_and_rally_guard():
    from strategies import FundingCarry
    c = FundingCarry(threshold_bps=20, window=1, size=D(10), exit_bps=5)
    c.on_funding(0, D("0.003"))
    b = bar(60, "8.49", "8.51")
    assert c.on_bar(b, D(0)) == D(-10)
    c.on_funding(1, D("0.001"))                      # 10 bps: below entry, above exit
    assert c.on_bar(b, D(-10)) == D(-10) and c.on_bar(b, D(0)) == D(0)
    g = FundingCarry(threshold_bps=20, window=1, size=D(10), trend_minutes=2, trend_bps=50)
    g.on_funding(0, D("0.003"))
    for p in ("8.00", "8.00", "8.10"):               # +1.25% rally over 2 minutes
        out = g.on_bar(bar(60, str(D(p) - D("0.01")), str(D(p) + D("0.01"))), D(-10))
    assert out == D(0)                                # stand aside instead of staying short
