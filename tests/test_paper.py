from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from kalshi_perps.paper import OrderRejected, PaperBroker, sweep_book
from risk import RiskConfig, RiskEngine

T0 = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)
BIDS = [(D("8.49"), D("5")), (D("8.48"), D("10"))]
ASKS = [(D("8.51"), D("5")), (D("8.52"), D("10"))]


def broker(max_notional="500", limit="100", cash="10000"):
    risk = RiskEngine(RiskConfig(D(max_notional), D(limit)), D(cash), T0)
    return PaperBroker(risk, D(cash), taker_fee_rate=D("0.0012"))


def test_buy_walks_asks_and_cancels_excess():
    sw = sweep_book("buy", D("20"), BIDS, ASKS)
    assert sw.levels == [(D("8.51"), D("5")), (D("8.52"), D("10"))]
    assert sw.filled == 15 and sw.notional == D("127.75")


def test_limit_price_stops_sweep():
    sw = sweep_book("sell", D("12"), BIDS, ASKS, limit_price=D("8.49"))
    assert sw.filled == 5


def test_market_buy_pays_taker_fee_at_ask_not_mid():
    b = broker()
    f = b.place("buy", D("8"), BIDS, ASKS, mark=D("8.50"), now=T0)
    assert f["liquidity"] == "taker"
    assert f["notional"] == D("68.11")            # 5 @ 8.51 + 3 @ 8.52
    assert f["fee"] == D("0.0818")                # ceil(68.11 x 0.0012 = 0.081732)
    assert b.cash == D("10000") - D("0.0818")
    # Marked at 8.50 the fresh long is already down the spread it crossed.
    assert b.position.unrealized_pnl(D("8.50")) == D("8") * D("8.50") - D("68.11")


def test_round_trip_short_pnl_and_fees():
    b = broker()
    b.place("sell", D("5"), BIDS, ASKS, mark=D("8.50"), now=T0)          # short 5 @ 8.49
    down = [(D("8.29"), D("50"))], [(D("8.30"), D("50"))]
    f = b.place("buy", D("5"), down[0], down[1], mark=D("8.295"), now=T0)  # cover @ 8.30
    assert f["realized_pnl"] == D("0.95")                                # 5 x (8.49 - 8.30)
    fees = D("0.0510") + D("0.0498")                                      # ceil(42.45*.0012), ceil(41.5*.0012)
    assert b.position.qty == 0
    assert b.cash == D("10000") + D("0.95") - fees


def test_risk_cap_rejects_before_filling():
    b = broker(max_notional="50")
    with pytest.raises(OrderRejected, match="exceeds max"):
        b.place("buy", D("10"), BIDS, ASKS, mark=D("8.50"), now=T0)
    assert b.position.qty == 0 and b.fills == []


def test_margin_check():
    b = broker(max_notional="100000", cash="20")
    with pytest.raises(OrderRejected, match="insufficient paper margin"):
        b.place("buy", D("15"), BIDS, ASKS, mark=D("8.50"), now=T0)      # ~$128 notional > 5 x $20


def test_kill_switch_blocks_new_risk_but_allows_close():
    b = broker(max_notional="1000", limit="1")
    b.place("buy", D("15"), BIDS, ASKS, mark=D("8.50"), now=T0)
    b.mark_to_market(D("8.40"), T0)                                      # down ~$1.7 > $1 limit
    assert b.risk.killed
    with pytest.raises(OrderRejected, match="kill switch"):
        b.place("buy", D("1"), BIDS, ASKS, mark=D("8.40"), now=T0)
    b.close_position(BIDS, ASKS, mark=D("8.40"), now=T0)
    assert b.position.qty == 0


def test_reduce_only_caps_size_and_rejects_wrong_side():
    b = broker()
    b.place("buy", D("3"), BIDS, ASKS, mark=D("8.50"), now=T0)
    f = b.place("sell", D("10"), BIDS, ASKS, mark=D("8.50"), reduce_only=True, now=T0)
    assert f["filled"] == 3 and b.position.qty == 0
    with pytest.raises(OrderRejected, match="would not reduce"):
        b.place("sell", D("1"), BIDS, ASKS, mark=D("8.50"), reduce_only=True, now=T0)


def test_funding_short_receives_positive_rate_once():
    b = broker()
    b.place("sell", D("5"), BIDS, ASKS, mark=D("8.50"), now=T0)
    cash = b.cash
    paid = b.apply_funding("2026-09-23T20:00:00Z", D("0.001"), D("8.50"), T0)
    assert paid == D("-0.04250") and b.cash == cash + D("0.0425")
    assert b.apply_funding("2026-09-23T20:00:00Z", D("0.001"), D("8.50"), T0) is None  # idempotent


def test_state_round_trips_through_disk(tmp_path):
    path = tmp_path / "paper.json"
    b = PaperBroker.open(path, RiskConfig(D("500"), D("100")))
    b.place("sell", D("7"), BIDS, ASKS, mark=D("8.50"), now=T0)
    b2 = PaperBroker.open(path, RiskConfig(D("500"), D("100")))
    assert b2.position.qty == -7 and b2.cash == b.cash and b2.position.avg_entry == b.position.avg_entry
