from decimal import Decimal as D

import pytest

from kalshi_perps.accounting import Position, balance_change_for_fee, taker_fee


# ---- unrealized P&L sign ---------------------------------------------------

def test_short_gains_when_price_falls():
    p = Position()
    p.apply_fill(D(-10), D("8.50"))
    assert p.qty == -10 and p.side == "short"
    assert p.unrealized_pnl(D("8.40")) == D("1.00")    # 10 x 0.10 in our favour
    assert p.unrealized_pnl(D("8.60")) == D("-1.00")


def test_long_gains_when_price_rises():
    p = Position()
    p.apply_fill(D(10), D("8.50"))
    assert p.unrealized_pnl(D("8.60")) == D("1.00")
    assert p.unrealized_pnl(D("8.40")) == D("-1.00")


def test_short_realized_on_cover():
    p = Position()
    p.apply_fill(D(-4), D("8.50"))
    r = p.apply_fill(D(4), D("8.25"))
    assert r.realized_pnl == D("1.00") and p.qty == 0 and p.avg_entry == 0


# ---- average entry ---------------------------------------------------------

def test_adding_blends_entry():
    p = Position()
    p.apply_fill(D(10), D("8.00"))
    p.apply_fill(D(30), D("8.40"))
    assert p.avg_entry == D("8.30")


def test_adding_to_short_blends_entry():
    p = Position()
    p.apply_fill(D(-10), D("8.00"))
    p.apply_fill(D(-10), D("9.00"))
    assert p.qty == -20 and p.avg_entry == D("8.50")


def test_partial_reduce_keeps_entry():
    p = Position()
    p.apply_fill(D(10), D("8.00"))
    r = p.apply_fill(D(-4), D("9.00"))
    assert p.qty == 6 and p.avg_entry == D("8.00") and r.realized_pnl == D("4.00")


def test_flip_long_to_short_resets_entry_to_fill_price():
    p = Position()
    p.apply_fill(D(10), D("8.00"))
    r = p.apply_fill(D(-15), D("8.20"))
    assert r.flipped and r.closed == 10 and r.opened == 5
    assert r.realized_pnl == D("2.00")                 # 10 x (8.20 - 8.00)
    assert p.qty == -5 and p.avg_entry == D("8.20")    # not a blend of 8.00 and 8.20
    assert p.unrealized_pnl(D("8.10")) == D("0.50")    # short 5 from 8.20, mark 8.10


def test_flip_short_to_long_resets_entry_to_fill_price():
    p = Position()
    p.apply_fill(D(-10), D("8.00"))
    r = p.apply_fill(D(12), D("7.50"))
    assert r.realized_pnl == D("5.00")                 # short 10 covered 0.50 lower
    assert p.qty == 2 and p.avg_entry == D("7.50")


def test_realized_accumulates_through_round_trips():
    p = Position()
    p.apply_fill(D(5), D("8.00"))
    p.apply_fill(D(-10), D("8.10"))   # +0.50, now short 5 @ 8.10
    p.apply_fill(D(5), D("8.30"))     # -1.00, flat
    assert p.qty == 0 and p.realized_pnl == D("-0.50")


# ---- fees & funding --------------------------------------------------------

def test_taker_fee_rounds_up_to_micro_dollars():
    assert taker_fee(D("8.4997"), D("0.0012")) == D("0.010200")   # 0.01019964 -> ceil
    assert taker_fee(D("-100"), D("0.0012")) == D("0.120000")


def test_fee_cash_rounds_up_to_balance_grid():
    assert balance_change_for_fee(D("0.010200")) == D("0.0102")
    assert balance_change_for_fee(D("0.010201")) == D("0.0103")


@pytest.mark.parametrize("qty,rate,expected", [
    (D(10), D("0.001"), D("0.0850")),     # long pays when rate > 0
    (D(-10), D("0.001"), D("-0.0850")),   # short receives when rate > 0
    (D(10), D("-0.001"), D("-0.0850")),   # long receives when rate < 0
])
def test_funding_direction(qty, rate, expected):
    p = Position()
    p.apply_fill(qty, D("8.50"))
    assert p.funding_payment(rate, D("8.50")) == expected
