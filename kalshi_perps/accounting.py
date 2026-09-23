"""Position accounting shared by the paper broker and the backtester.

Conventions:
  * quantity is signed contracts: > 0 long, < 0 short;
  * prices are dollars per contract (Kalshi perps quote per 0.0001 BTC contract),
    so P&L = contracts x price change, in dollars, with no extra multiplier;
  * all math is Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

ZERO = Decimal(0)
FEE_QUANTUM = Decimal("0.000001")      # Kalshi rounds trade fees UP to $0.000001
BALANCE_QUANTUM = Decimal("0.0001")    # direct-member balance precision

# Fallback only: the spec's example taker rate. Live rates come from GET /margin/fee_tiers.
DEFAULT_TAKER_FEE_RATE = Decimal("0.0012")
DEFAULT_MAKER_FEE_RATE = Decimal("0.0002")


def sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


def taker_fee(notional: Decimal, rate: Decimal) -> Decimal:
    """Fee for a spread-crossing (taker) fill: rate x notional, rounded up to $0.000001."""
    return (abs(notional) * rate).quantize(FEE_QUANTUM, rounding=ROUND_CEILING)


def balance_change_for_fee(fee: Decimal) -> Decimal:
    """Cash actually debited for a fee once balance rounding to $0.0001 is applied.

    Kalshi floors (revenue - fee) to the balance grid, charging the remainder as a
    rounding fee; for a pure fee debit that is ceil(fee) to $0.0001.
    """
    return fee.quantize(BALANCE_QUANTUM, rounding=ROUND_CEILING)


@dataclass
class FillResult:
    realized_pnl: Decimal
    opened: Decimal       # contracts that increased exposure (incl. the new side of a flip)
    closed: Decimal       # contracts that reduced exposure
    flipped: bool


@dataclass
class Position:
    qty: Decimal = ZERO
    avg_entry: Decimal = ZERO          # 0 when flat
    realized_pnl: Decimal = ZERO       # price P&L only (fees/funding tracked separately)
    fees_paid: Decimal = ZERO
    funding_paid: Decimal = ZERO       # positive = paid out, negative = received
    history: list = field(default_factory=list, repr=False)

    @property
    def side(self) -> str:
        return "long" if self.qty > 0 else "short" if self.qty < 0 else "flat"

    def apply_fill(self, qty_delta: Decimal, price: Decimal) -> FillResult:
        """Apply a signed fill (+ buys, - sells) at `price` (dollars per contract)."""
        qty_delta, price = Decimal(qty_delta), Decimal(price)
        if qty_delta == 0:
            return FillResult(ZERO, ZERO, ZERO, False)
        if price <= 0:
            raise ValueError("price must be positive")

        realized = ZERO
        closed = opened = ZERO
        flipped = False

        if self.qty == 0 or sign(self.qty) == sign(qty_delta):
            # Opening or adding: size-weighted average entry.
            new_qty = self.qty + qty_delta
            self.avg_entry = (abs(self.qty) * self.avg_entry + abs(qty_delta) * price) / abs(new_qty)
            self.qty = new_qty
            opened = abs(qty_delta)
        else:
            # Reducing, closing, or flipping.
            closed = min(abs(qty_delta), abs(self.qty))
            # Long closes gain when price > entry; short closes gain when price < entry.
            realized = closed * (price - self.avg_entry) * sign(self.qty)
            remainder = abs(qty_delta) - closed
            if remainder > 0:
                # Flip: the old position is fully closed at `price`, and the leftover
                # opens a fresh position on the other side whose entry is `price` —
                # NOT a blend with the old entry.
                self.qty = remainder * sign(qty_delta)
                self.avg_entry = price
                opened = remainder
                flipped = True
            else:
                self.qty += qty_delta
                if self.qty == 0:
                    self.avg_entry = ZERO
                # Partial reduce: average entry of the remaining contracts is unchanged.
        self.realized_pnl += realized
        return FillResult(realized, opened, closed, flipped)

    def unrealized_pnl(self, mark: Decimal | None) -> Decimal:
        """Signed qty x (mark - entry): a short (qty < 0) gains when mark falls below entry."""
        if self.qty == 0 or mark is None:
            return ZERO
        return self.qty * (Decimal(mark) - self.avg_entry)

    def notional(self, mark: Decimal | None) -> Decimal:
        if mark is None:
            return abs(self.qty) * self.avg_entry
        return abs(self.qty) * Decimal(mark)

    def funding_payment(self, rate: Decimal, mark: Decimal) -> Decimal:
        """Amount this position PAYS at a funding event (negative = receives).

        Positive rate: longs pay shorts. Payment = rate x signed qty x mark.
        """
        return Decimal(rate) * self.qty * Decimal(mark)
