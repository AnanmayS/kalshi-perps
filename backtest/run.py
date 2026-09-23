"""Run a backtest over candles stored by scripts/backfill_candles.py.

    python -m backtest.run                                # momentum, all stored 1m candles
    python -m backtest.run --days 3 --size 5 --threshold-bps 60
    python -m backtest.run --strategy hold                # buy-and-hold baseline
    python -m backtest.run --out data/bt                  # also write trades/equity CSVs

The taker fee rate comes from your Kalshi fee tier when API keys are configured,
otherwise the 0.12% default. Nothing here touches the exchange except that lookup.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.data import load_bars, load_funding  # noqa: E402
from backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from kalshi_perps import KalshiPerpsClient, load_settings  # noqa: E402
from kalshi_perps.accounting import DEFAULT_TAKER_FEE_RATE  # noqa: E402
from kalshi_perps.store import DEFAULT_DB, MarketStore  # noqa: E402
from risk import RiskConfig  # noqa: E402
from strategies import BuyAndHold, Momentum  # noqa: E402


def fee_rate(settings, override: str | None) -> tuple[Decimal, str]:
    if override:
        return Decimal(override), "--fee-rate"
    if settings.has_credentials:
        try:
            r = KalshiPerpsClient(settings).fee_tiers()["taker_fee_rates"].get(settings.ticker)
            if r is not None:
                return Decimal(str(r)), "your Kalshi fee tier"
        except Exception:
            pass
    return DEFAULT_TAKER_FEE_RATE, "default"


def write_csvs(res, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "side", "qty", "price", "notional", "liquidity", "fee", "spread_cost", "realized_pnl", "position_after"])
        for t in res.trades:
            w.writerow([t.ts, t.side, t.qty, t.price, t.notional, "taker", t.fee, t.spread_cost, t.realized_pnl, t.position_after])
    with open(out / "equity.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "equity"])
        w.writerows(res.equity_curve)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", choices=("momentum", "hold"), default="momentum")
    ap.add_argument("--days", type=float, default=None, help="only the most recent N days (default: all stored)")
    ap.add_argument("--size", default="10", help="contracts per position (default 10 = 0.001 BTC)")
    ap.add_argument("--lookback", type=int, default=60)
    ap.add_argument("--threshold-bps", type=float, default=40)
    ap.add_argument("--exit-bps", type=float, default=5)
    ap.add_argument("--min-hold", type=int, default=30)
    ap.add_argument("--fee-rate", default=None, help="override taker fee rate, e.g. 0.0012")
    ap.add_argument("--slippage-bps", default="0", help="extra cost per fill beyond the observed spread")
    ap.add_argument("--starting-cash", default="10000")
    ap.add_argument("--no-baseline", action="store_true", help="skip the buy-and-hold comparison")
    ap.add_argument("--out", default=None, help="directory for trades.csv / equity.csv")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    settings = load_settings()
    store = MarketStore(args.db)
    start = int(time.time() - args.days * 86400) if args.days else None
    bars = load_bars(store, settings.ticker, start_ts=start)
    if not bars:
        print("No candles stored. Run: python scripts/backfill_candles.py")
        return 1
    funding = load_funding(store, settings.ticker)
    rate, rate_src = fee_rate(settings, args.fee_rate)
    cfg = BacktestConfig(
        starting_cash=Decimal(args.starting_cash), taker_fee_rate=rate, slippage_bps=Decimal(args.slippage_bps),
        risk=RiskConfig(settings.max_notional_per_trade, settings.daily_loss_limit),
    )
    size = Decimal(args.size)
    if args.strategy == "momentum":
        strat = Momentum(args.lookback, args.threshold_bps, args.exit_bps, size, args.min_hold)
    else:
        strat = BuyAndHold(size)

    print(f"{settings.ticker}: {len(bars)} bars, {len(funding)} funding events stored; "
          f"taker fee {rate:.4%} ({rate_src}); fills at next bar's observed bid/ask\n")
    res = run_backtest(bars, funding, strat, cfg)
    print(res.summary())

    if args.strategy != "hold" and not args.no_baseline:
        base = run_backtest(bars, funding, BuyAndHold(size), cfg)
        print(f"\nBaseline (buy & hold {size}): net {base.net_pnl:+,.4f} "
              f"(price {base.price_pnl:+,.4f}, fees {-base.fees:+,.4f}, funding {-base.funding:+,.4f})")

    if args.out:
        write_csvs(res, Path(args.out))
        print(f"\nWrote {args.out}/trades.csv and {args.out}/equity.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
