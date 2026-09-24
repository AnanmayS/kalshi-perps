"""Paper broker: simulated taker orders against a real order book snapshot.

No fantasy fills:
  * every paper order is immediate-or-cancel and crosses the spread, so it
    consumes displayed liquidity level by level (buys lift asks, sells hit bids)
    and always pays the TAKER fee;
  * size beyond the displayed depth (or beyond an optional limit price) is
    cancelled, never filled;
  * resting/maker orders are not simulated, because queue position can't be
    known from snapshots.
Liquidation is not modelled; a margin check at order time (max leverage on
equity) stands in for it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from risk.engine import RiskConfig, RiskEngine

from .accounting import (DEFAULT_TAKER_FEE_RATE, ZERO, Position, balance_change_for_fee, sign,
                         taker_fee)

Level = tuple[Decimal, Decimal]  # (price, size)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Sweep:
    levels: list[Level]
    filled: Decimal
    notional: Decimal

    @property
    def vwap(self) -> Decimal | None:
        return self.notional / self.filled if self.filled else None


def sweep_book(side: str, count: Decimal, bids: Sequence[Level], asks: Sequence[Level],
               limit_price: Decimal | None = None) -> Sweep:
    """Walk the opposite side of the book. `bids` best-first (desc), `asks` best-first (asc)."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    book = sorted(asks, key=lambda l: l[0]) if side == "buy" else sorted(bids, key=lambda l: l[0], reverse=True)
    remaining, used, notional = Decimal(count), [], ZERO
    for price, size in book:
        if remaining <= 0:
            break
        if limit_price is not None and (price > limit_price if side == "buy" else price < limit_price):
            break
        take = min(remaining, size)
        if take <= 0:
            continue
        used.append((price, take))
        notional += take * price
        remaining -= take
    return Sweep(used, Decimal(count) - remaining, notional)


class OrderRejected(Exception):
    pass


class PaperBroker:
    def __init__(self, risk: RiskEngine, starting_cash: Decimal = Decimal("10000"),
                 taker_fee_rate: Decimal = DEFAULT_TAKER_FEE_RATE, max_leverage: Decimal = Decimal("5"),
                 state_path: Path | None = None):
        self.risk = risk
        self.starting_cash = Decimal(starting_cash)
        self.cash = Decimal(starting_cash)
        self.taker_fee_rate = Decimal(taker_fee_rate)
        self.max_leverage = Decimal(max_leverage)
        self.position = Position()
        self.fills: list[dict] = []
        self.events: list[dict] = []
        self.funding_applied: set[str] = set()
        # Funding events after this epoch second are still to be settled.
        self.funding_checked_until: float = _now().timestamp()
        self.state_path = state_path

    # -- valuation -------------------------------------------------------------

    def equity(self, mark: Decimal | None) -> Decimal:
        return self.cash + self.position.unrealized_pnl(mark)

    def mark_to_market(self, mark: Decimal | None, now: datetime | None = None) -> None:
        if mark is None:
            return
        was_killed = self.risk.killed
        self.risk.update_equity(self.equity(mark), now)
        if self.risk.killed and not was_killed:
            self._event("kill_switch", now or _now(), reason=self.risk.kill_reason)
            self._save()

    # -- orders ------------------------------------------------------------------

    def _reduces_only(self, qty_delta: Decimal) -> bool:
        q = self.position.qty
        return q != 0 and sign(qty_delta) == -sign(q) and abs(qty_delta) <= abs(q)

    def preview(self, side: str, count: Decimal, bids, asks, mark: Decimal | None,
                limit_price: Decimal | None = None, reduce_only: bool = False,
                now: datetime | None = None) -> dict:
        count = Decimal(count)
        if count <= 0:
            raise OrderRejected("count must be positive")
        if count != count.quantize(Decimal("0.01")):
            raise OrderRejected("count has at most 2 decimals")
        if reduce_only:
            q = self.position.qty
            want = -1 if side == "sell" else 1
            if q == 0 or sign(q) == want:
                raise OrderRejected("reduce-only order would not reduce the position")
            count = min(count, abs(q))

        sw = sweep_book(side, count, bids, asks, limit_price)
        qty_delta = sw.filled if side == "buy" else -sw.filled
        reduces = self._reduces_only(qty_delta) if sw.filled else self._reduces_only(count if side == "buy" else -count)
        fee = taker_fee(sw.notional, self.taker_fee_rate)
        fee_cash = balance_change_for_fee(fee)

        # Hypothetical position after the fill, for the margin check.
        after = Position(self.position.qty, self.position.avg_entry)
        fr = after.apply_fill(qty_delta, sw.vwap) if sw.filled else None
        ref = mark if mark is not None else sw.vwap
        equity_after = self.cash + (fr.realized_pnl if fr else ZERO) - fee_cash + after.unrealized_pnl(ref)
        new_notional = after.notional(ref) if ref is not None else ZERO

        decision = self.risk.check_order(sw.notional, reduces_only=reduces, now=now)
        reason = decision.reason
        if decision.allowed and sw.filled == 0:
            reason = "no liquidity at or better than the limit price" if limit_price is not None else "book is empty"
        elif decision.allowed and not reduces and new_notional > equity_after * self.max_leverage:
            reason = (f"insufficient paper margin: position notional ${new_notional:.2f} > "
                      f"{self.max_leverage}x equity ${equity_after:.2f}")

        return {
            "side": side,
            "requested": count,
            "filled": sw.filled,
            "unfilled": count - sw.filled,
            "vwap": sw.vwap,
            "levels": sw.levels,
            "notional": sw.notional,
            "liquidity": "taker",
            "fee_rate": self.taker_fee_rate,
            "fee": fee,
            "fee_cash": fee_cash,
            "reduces_only": reduces,
            "position_after": {"qty": after.qty, "avg_entry": after.avg_entry},
            "realized_pnl": fr.realized_pnl if fr else ZERO,
            "allowed": reason == "",
            "reason": reason,
        }

    def place(self, side: str, count: Decimal, bids, asks, mark: Decimal | None,
              limit_price: Decimal | None = None, reduce_only: bool = False,
              now: datetime | None = None) -> dict:
        now = now or _now()
        p = self.preview(side, count, bids, asks, mark, limit_price, reduce_only, now)
        order_id = str(uuid.uuid4())
        if not p["allowed"]:
            self._event("rejected", now, order_id=order_id, side=side, count=count, reason=p["reason"])
            self._save()
            raise OrderRejected(p["reason"])

        qty_delta = p["filled"] if side == "buy" else -p["filled"]
        fr = self.position.apply_fill(qty_delta, p["vwap"])
        self.cash += fr.realized_pnl - p["fee_cash"]
        self.position.fees_paid += p["fee_cash"]
        fill = {
            "order_id": order_id,
            "ts": now.isoformat(),
            "side": side,
            "requested": p["requested"],
            "filled": p["filled"],
            "cancelled": p["unfilled"],
            "vwap": p["vwap"],
            "notional": p["notional"],
            "liquidity": "taker",
            "fee_rate": self.taker_fee_rate,
            "fee": p["fee_cash"],
            "realized_pnl": fr.realized_pnl,
            "flipped": fr.flipped,
            "position_after": self.position.qty,
            "levels": p["levels"],
        }
        self.fills.append(fill)
        self.mark_to_market(mark if mark is not None else p["vwap"], now)
        self._save()
        return fill

    def close_position(self, bids, asks, mark: Decimal | None, now: datetime | None = None) -> dict:
        q = self.position.qty
        if q == 0:
            raise OrderRejected("no open position")
        return self.place("sell" if q > 0 else "buy", abs(q), bids, asks, mark, reduce_only=True, now=now)

    # -- funding -----------------------------------------------------------------

    def position_at(self, ts: float) -> Decimal:
        """Signed position held at unix time `ts`, reconstructed from the fill history."""
        if not self.fills:
            return self.position.qty
        qty = ZERO  # every paper account starts flat
        for f in self.fills:
            if datetime.fromisoformat(f["ts"]).timestamp() > ts:
                break
            qty = Decimal(str(f["position_after"]))
        return qty

    def apply_funding(self, funding_time: str, rate: Decimal, mark: Decimal, now: datetime | None = None,
                      qty: Decimal | None = None) -> Decimal | None:
        """Settle one funding event (idempotent per funding_time). Returns amount paid (neg = received).

        `qty` is the position held at the funding time (defaults to the current one).
        Positive rate: longs pay shorts, payment = rate x qty x mark.
        """
        if funding_time in self.funding_applied:
            return None
        self.funding_applied.add(funding_time)
        qty = self.position.qty if qty is None else Decimal(qty)
        pay = Decimal(rate) * qty * Decimal(mark)
        if qty != 0:
            self.cash -= pay
            self.position.funding_paid += pay
            self._event("funding", now or _now(), funding_time=funding_time, rate=Decimal(rate),
                        mark=Decimal(mark), qty=qty, paid=pay)
            self.mark_to_market(Decimal(mark), now)
        self._save()
        return pay

    def settle_funding(self, events: list[dict], until_ts: float, parse_ts) -> list[Decimal]:
        """Settle API funding events newer than the checkpoint, on the position held at each event.

        Kalshi publishes a funding event a few minutes AFTER its funding_time, so the
        checkpoint only advances to the newest event actually seen, never to "now";
        otherwise an event published late would fall behind the checkpoint and be skipped.
        """
        since = self.funding_checked_until
        newest = since
        paid = []
        for ev in sorted(events, key=lambda e: e["funding_time"]):
            t = parse_ts(ev["funding_time"]).timestamp()
            if since < t <= until_ts:
                r = self.apply_funding(ev["funding_time"], Decimal(str(ev["funding_rate"])),
                                       Decimal(ev["mark_price"]), qty=self.position_at(t))
                if r is not None:
                    paid.append(r)
                newest = max(newest, t)
        self.funding_checked_until = newest
        self._save()
        return paid

    # -- misc --------------------------------------------------------------------

    def _event(self, kind: str, now: datetime, **data) -> None:
        self.events.append({"kind": kind, "ts": now.isoformat(), **data})
        del self.events[:-200]

    def state(self, mark: Decimal | None) -> dict:
        pos = self.position
        return {
            "cash": self.cash,
            "starting_cash": self.starting_cash,
            "equity": self.equity(mark),
            "mark": mark,
            "position": {
                "qty": pos.qty, "side": pos.side, "avg_entry": pos.avg_entry,
                "notional": pos.notional(mark), "unrealized_pnl": pos.unrealized_pnl(mark),
                "realized_pnl": pos.realized_pnl, "fees_paid": pos.fees_paid, "funding_paid": pos.funding_paid,
            },
            "taker_fee_rate": self.taker_fee_rate,
            "max_leverage": self.max_leverage,
            "risk": self.risk.snapshot(),
            "fills": self.fills[-200:][::-1],
            "events": self.events[-200:][::-1],
        }

    def to_dict(self) -> dict:
        p = self.position
        r = self.risk
        return {
            "cash": str(self.cash), "starting_cash": str(self.starting_cash),
            "position": {"qty": str(p.qty), "avg_entry": str(p.avg_entry), "realized_pnl": str(p.realized_pnl),
                         "fees_paid": str(p.fees_paid), "funding_paid": str(p.funding_paid)},
            "fills": self.fills[-500:], "events": self.events[-200:],
            "funding_applied": sorted(self.funding_applied)[-100:],
            "funding_checked_until": self.funding_checked_until,
            "risk": {"day": r.day.isoformat(), "day_start_equity": str(r.day_start_equity),
                     "last_equity": str(r.last_equity), "killed": r.killed, "kill_reason": r.kill_reason,
                     "killed_at": r.killed_at.isoformat() if r.killed_at else None},
        }

    def load_dict(self, d: dict) -> None:
        from datetime import date
        self.cash = Decimal(d["cash"])
        self.starting_cash = Decimal(d["starting_cash"])
        p = d["position"]
        self.position = Position(Decimal(p["qty"]), Decimal(p["avg_entry"]), Decimal(p["realized_pnl"]),
                                 Decimal(p["fees_paid"]), Decimal(p["funding_paid"]))
        self.fills, self.events = d.get("fills", []), d.get("events", [])
        self.funding_applied = set(d.get("funding_applied", []))
        self.funding_checked_until = d.get("funding_checked_until", self.funding_checked_until)
        r = d["risk"]
        self.risk.day = date.fromisoformat(r["day"])
        self.risk.day_start_equity = Decimal(r["day_start_equity"])
        self.risk.last_equity = Decimal(r["last_equity"])
        self.risk.killed, self.risk.kill_reason = r["killed"], r["kill_reason"]
        self.risk.killed_at = datetime.fromisoformat(r["killed_at"]) if r["killed_at"] else None

    def _save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), default=str, indent=1))
        tmp.replace(self.state_path)

    @classmethod
    def open(cls, path: Path, config: RiskConfig, starting_cash: Decimal = Decimal("10000"), **kw) -> "PaperBroker":
        broker = cls(RiskEngine(config, starting_cash), starting_cash, state_path=path, **kw)
        if path.is_file():
            broker.load_dict(json.loads(path.read_text()))
        return broker
