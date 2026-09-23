"""Run a strategy against live Kalshi data with paper fills.

    python scripts/paper_run.py                                   # momentum, default params
    python scripts/paper_run.py --lookback 240 --threshold-bps 80 --min-hold 120 --size 5
    python scripts/paper_run.py --duration 600                    # stop after 10 minutes
    python scripts/paper_run.py --reset                           # start a fresh paper account

Paper only: orders fill in the local PaperBroker against the live order book and
are NEVER sent to Kalshi, whatever KALSHI_LIVE_TRADING says. State persists in
data/runner/ (position, fills, P&L), so stopping and restarting keeps the account.
The dashboard shows the runner under "Strategy runner" while it's running.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kalshi_perps import KalshiPerpsClient, load_settings  # noqa: E402
from kalshi_perps.accounting import DEFAULT_TAKER_FEE_RATE  # noqa: E402
from kalshi_perps.paper import PaperBroker  # noqa: E402
from risk import RiskConfig  # noqa: E402
from runner.paper import PaperRunner  # noqa: E402
from strategies import Momentum  # noqa: E402

RUNNER_DIR = ROOT / "data" / "runner"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", default="10")
    ap.add_argument("--lookback", type=int, default=60)
    ap.add_argument("--threshold-bps", type=float, default=40)
    ap.add_argument("--exit-bps", type=float, default=5)
    ap.add_argument("--min-hold", type=int, default=30)
    ap.add_argument("--duration", type=float, default=None, help="seconds to run (default: until Ctrl-C)")
    ap.add_argument("--starting-cash", default="10000")
    ap.add_argument("--reset", action="store_true", help="discard the saved runner paper account first")
    ap.add_argument("--no-flatten-on-kill", action="store_true")
    ap.add_argument("--flatten-on-exit", action="store_true", help="close the paper position when stopping")
    args = ap.parse_args()

    settings = load_settings()
    client = KalshiPerpsClient(settings)
    state_path = RUNNER_DIR / "paper_state.json"
    if args.reset and state_path.exists():
        state_path.unlink()

    rate = DEFAULT_TAKER_FEE_RATE
    if settings.has_credentials:
        try:
            r = client.fee_tiers()["taker_fee_rates"].get(settings.ticker)
            rate = Decimal(str(r)) if r is not None else rate
        except Exception:
            pass

    broker = PaperBroker.open(state_path, RiskConfig(settings.max_notional_per_trade, settings.daily_loss_limit),
                              starting_cash=Decimal(args.starting_cash), taker_fee_rate=rate)
    broker.taker_fee_rate = rate
    strategy = Momentum(args.lookback, args.threshold_bps, args.exit_bps, Decimal(args.size), args.min_hold)
    runner = PaperRunner(client, broker, strategy, ticker=settings.ticker,
                         status_path=RUNNER_DIR / "status.json",
                         warmup_minutes=max(360, args.lookback + 30),
                         flatten_on_kill=not args.no_flatten_on_kill,
                         log=lambda m: print(m, flush=True))

    runner.log(f"PAPER runner | env={settings.env} ticker={settings.ticker} strategy={strategy.name} "
               f"{strategy.params()} | taker fee {rate:.4%} | nothing is sent to Kalshi")
    runner.log(f"account: cash ${broker.cash:.2f}, position {broker.position.qty}, "
               f"kill switch {'ON' if broker.risk.killed else 'armed'}")
    try:
        runner.warmup()
        runner.run(duration=args.duration)
    except KeyboardInterrupt:
        runner.log("stopping (Ctrl-C)")
    finally:
        if args.flatten_on_exit and broker.position.qty != 0:
            runner._update_mark(runner.clock())
            runner._execute(Decimal(0), "flatten on exit")
        st = runner.status()
        p = st["paper"]
        runner.log(f"final: position {p['position']['qty']}, equity ${Decimal(p['equity']):.2f}, "
                   f"realized {p['position']['realized_pnl']}, fees {p['position']['fees_paid']}, "
                   f"bars {st['bars_seen']}, errors {st['errors']}")
        runner.write_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
