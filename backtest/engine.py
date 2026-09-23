"""Event-driven backtester over 1-minute Kalshi perps candles.

Execution model (no fantasy fills):
  * The strategy sees bar t only after it closes and returns a target position.
  * The resulting order executes at the OPEN of the next bar, at the quote that
    was actually observed then: buys pay ask_open, sells receive bid_open.
    Never the mid, never the close of the signal bar.
  * If the needed side of the book was empty (NULL quote), the order does not
    fill and is recorded as rejected.
  * Every fill crosses the spread and pays the TAKER fee (same fee and rounding
    code as the paper broker).
  * Order size is not checked against depth (candles don't carry it); keep
    sizes small relative to the book (the demo book is often a few hundred
    contracts deep at the touch), or add slippage_bps.
Funding: each funding event is settled on the position held at that instant
(positive rate: longs pay shorts), using the event's mark price.
Risk: the same RiskEngine as paper trading (max notional per trade, daily loss
limit, kill switch). While killed, targets are clipped so exposure can only shrink.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from kalshi_perps.accounting import (DEFAULT_TAKER_FEE_RATE, ZERO, Position, balance_change_for_fee, sign,
                                     taker_fee)
from risk.engine import RiskConfig, RiskEngine
from strategies.base import Bar, Strategy

BAR_SECONDS = 60


@dataclass
class BacktestConfig:
    starting_cash: Decimal = Decimal("10000")
    taker_fee_rate: Decimal = DEFAULT_TAKER_FEE_RATE
    slippage_bps: Decimal = Decimal("0")      # extra cost on top of the observed spread, per fill
    risk: RiskConfig = field(default_factory=RiskConfig)


@dataclass
class Trade:
    ts: int
    side: str
    qty: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    spread_cost: Decimal       # |fill - mid at open| x qty: what crossing the spread cost
    realized_pnl: Decimal
    position_after: Decimal


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    start_ts: int
    end_ts: int
    bars: int
    starting_cash: Decimal
    final_equity: Decimal
    price_pnl: Decimal          # realized + unrealized price P&L, before costs
    fees: Decimal
    funding: Decimal            # positive = paid
    spread_cost: Decimal        # already inside price_pnl; reported for attribution
    trades: list[Trade]
    rejected: list[dict]
    equity_curve: list[tuple[int, Decimal]]
    funding_events: list[dict]
    killed_at: int | None

    @property
    def net_pnl(self) -> Decimal:
        return self.final_equity - self.starting_cash

    @property
    def total_return(self) -> float:
        return float(self.net_pnl / self.starting_cash)

    @property
    def max_drawdown(self) -> tuple[Decimal, float]:
        peak, worst, worst_pct = None, ZERO, 0.0
        for _, eq in self.equity_curve:
            peak = eq if peak is None or eq > peak else peak
            dd = eq - peak
            if dd < worst:
                worst, worst_pct = dd, float(dd / peak)
        return worst, worst_pct

    @property
    def sharpe(self) -> float | None:
        """Annualized Sharpe of per-bar equity returns (crypto trades 24/7: 525,600 min/yr)."""
        rets = [float(b / a - 1) for (_, a), (_, b) in zip(self.equity_curve, self.equity_curve[1:]) if a > 0]
        if len(rets) < 2:
            return None
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        return None if sd == 0 else mu / sd * math.sqrt(525_600)

    @property
    def exposure(self) -> float:
        """Fraction of bars with a non-zero position."""
        if not self.equity_curve:
            return 0.0
        held = sum(1 for t in self._pos_by_bar if t != 0)
        return held / len(self._pos_by_bar)

    _pos_by_bar: list = field(default_factory=list, repr=False)

    def summary(self) -> str:
        def ts(t):
            return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")
        dd, ddp = self.max_drawdown
        sh = self.sharpe
        buys = sum(1 for t in self.trades if t.side == "buy")
        lines = [
            f"Strategy      {self.strategy} {self.params}",
            f"Period        {ts(self.start_ts)} -> {ts(self.end_ts)} UTC ({self.bars} bars)",
            f"Start equity  ${self.starting_cash:,.2f}",
            f"End equity    ${self.final_equity:,.2f}   net {self.net_pnl:+,.2f} ({self.total_return:+.3%})",
            "",
            "P&L attribution",
            f"  price P&L          {self.price_pnl:+,.4f}   (includes {-self.spread_cost:+,.4f} of spread crossed)",
            f"  taker fees         {-self.fees:+,.4f}",
            f"  funding            {-self.funding:+,.4f}   ({len(self.funding_events)} events while in a position)",
            f"  = net              {self.price_pnl - self.fees - self.funding:+,.4f}",
            "",
            f"Fills         {len(self.trades)} ({buys} buys / {len(self.trades) - buys} sells), "
            f"{len(self.rejected)} rejected",
            f"Turnover      ${sum(t.notional for t in self.trades):,.2f} notional",
            f"Exposure      {self.exposure:.1%} of bars in a position",
            f"Max drawdown  {dd:,.2f} ({ddp:.3%})",
            f"Sharpe (ann.) {'n/a' if sh is None else f'{sh:.2f}'}",
        ]
        if self.killed_at:
            lines.append(f"Kill switch   tripped at {ts(self.killed_at)} UTC")
        if self.rejected:
            reasons: dict[str, int] = {}
            for r in self.rejected:
                reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
            lines.append("Rejections    " + "; ".join(f"{k} x{v}" for k, v in reasons.items()))
        return "\n".join(lines)


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def run_backtest(bars: Sequence[Bar], funding: Sequence[tuple[int, Decimal, Decimal | None]],
                 strategy: Strategy, config: BacktestConfig | None = None) -> BacktestResult:
    cfg = config or BacktestConfig()
    if not bars:
        raise ValueError("no bars to backtest (run scripts/backfill_candles.py first)")

    pos = Position()
    cash = cfg.starting_cash
    risk = RiskEngine(cfg.risk, cfg.starting_cash, _dt(bars[0].ts))
    slip = cfg.slippage_bps / Decimal(10_000)

    trades: list[Trade] = []
    rejected: list[dict] = []
    curve: list[tuple[int, Decimal]] = []
    pos_by_bar: list[Decimal] = []
    fund_log: list[dict] = []
    fees = funding_paid = spread_cost = ZERO
    killed_at = None
    pending: Decimal | None = None
    f_idx = 0
    last_mid: Decimal | None = None

    def settle_funding_until(t: int, fallback_mark: Decimal | None):
        nonlocal f_idx, cash, funding_paid
        while f_idx < len(funding) and funding[f_idx][0] <= t:
            f_ts, rate, mark = funding[f_idx]
            f_idx += 1
            if f_ts < bars[0].ts - BAR_SECONDS or pos.qty == 0:
                continue
            m = mark if mark is not None else fallback_mark
            if m is None:
                continue
            pay = pos.funding_payment(rate, m)
            cash -= pay
            funding_paid += pay
            pos.funding_paid += pay
            fund_log.append({"ts": f_ts, "rate": rate, "mark": m, "qty": pos.qty, "paid": pay})

    for bar in bars:
        open_ts = bar.ts - BAR_SECONDS

        # 1) funding that happened before this bar opened, on the position held then
        settle_funding_until(open_ts, last_mid)

        # 2) execute the order decided on the previous bar at this bar's opening quote
        if pending is not None:
            target = pending
            pending = None
            if risk.killed:
                # reduce-only: clip toward zero, never past it
                target = ZERO if sign(target) != sign(pos.qty) else sign(pos.qty) * min(abs(target), abs(pos.qty))
            delta = target - pos.qty
            if delta != 0:
                side = "buy" if delta > 0 else "sell"
                quote = bar.ask_open if side == "buy" else bar.bid_open
                reduces = pos.qty != 0 and sign(delta) == -sign(pos.qty) and abs(delta) <= abs(pos.qty)
                if quote is None:
                    rejected.append({"ts": bar.ts, "side": side, "qty": abs(delta), "reason": "no quote on that side"})
                else:
                    price = quote * (1 + slip) if side == "buy" else quote * (1 - slip)
                    notional = abs(delta) * price
                    decision = risk.check_order(notional, reduces_only=reduces, now=_dt(bar.ts))
                    if not decision.allowed:
                        # Group by category (the raw reason includes the dollar amount).
                        r = decision.reason
                        reason = ("notional exceeds max per trade" if "exceeds max" in r
                                  else "kill switch active" if "kill switch" in r else r)
                        rejected.append({"ts": bar.ts, "side": side, "qty": abs(delta), "reason": reason})
                    else:
                        fr = pos.apply_fill(delta, price)
                        fee = balance_change_for_fee(taker_fee(notional, cfg.taker_fee_rate))
                        cash += fr.realized_pnl - fee
                        fees += fee
                        pos.fees_paid += fee
                        sc = ZERO
                        if bar.bid_open is not None and bar.ask_open is not None:
                            sc = abs(price - (bar.bid_open + bar.ask_open) / 2) * abs(delta)
                        spread_cost += sc
                        trades.append(Trade(bar.ts, side, abs(delta), price, notional, fee, sc,
                                            fr.realized_pnl, pos.qty))

        # 3) funding inside this bar, on the (possibly new) position
        mid = bar.mid if bar.mid is not None else last_mid
        settle_funding_until(bar.ts, mid)

        # 4) mark to market at the close; risk engine sees equity incl. fees & funding
        if mid is not None:
            last_mid = mid
        equity = cash + pos.unrealized_pnl(last_mid)
        was_killed = risk.killed
        risk.update_equity(equity, _dt(bar.ts))
        if risk.killed and not was_killed:
            killed_at = bar.ts
        curve.append((bar.ts, equity))
        pos_by_bar.append(pos.qty)

        # 5) strategy decides using data up to and including this bar only
        target = strategy.on_bar(bar, pos.qty)
        if target is not None and target != pos.qty:
            pending = Decimal(target)

    final_equity = curve[-1][1]
    price_pnl = pos.realized_pnl + pos.unrealized_pnl(last_mid)
    res = BacktestResult(
        strategy=strategy.name, params=strategy.params(), start_ts=bars[0].ts, end_ts=bars[-1].ts,
        bars=len(bars), starting_cash=cfg.starting_cash, final_equity=final_equity, price_pnl=price_pnl,
        fees=fees, funding=funding_paid, spread_cost=spread_cost, trades=trades, rejected=rejected,
        equity_curve=curve, funding_events=fund_log, killed_at=killed_at,
    )
    res._pos_by_bar = pos_by_bar
    return res
