"""Parameter sweeps with a held-out test period.

Tune on TRAIN only; the TEST period is scored once for the chosen candidates, so
a result that only looks good because it was fitted to the data gets caught.
"""

from __future__ import annotations

import itertools
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.data import load_bars, load_funding  # noqa: E402
from backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from kalshi_perps import load_settings  # noqa: E402
from kalshi_perps.store import MarketStore  # noqa: E402
from risk import RiskConfig  # noqa: E402
from strategies import STRATEGIES  # noqa: E402

TAKER = Decimal("0.0012")


def ts(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


_cache: dict = {}


def _data(start: str, end: str | None):
    key = (start, end)
    if key not in _cache:
        s = load_settings()
        st = MarketStore()
        bars = load_bars(st, s.ticker, ts(start), ts(end) - 1 if end else None)
        _cache[key] = (bars, load_funding(st, s.ticker), s)
    return _cache[key]


def evaluate(job) -> dict:
    name, params, start, end = job
    bars, funding, s = _data(start, end)
    strat = STRATEGIES[name](**params)
    cfg = BacktestConfig(taker_fee_rate=TAKER, risk=RiskConfig(s.max_notional_per_trade, s.daily_loss_limit))
    r = run_backtest(bars, funding, strat, cfg)
    days = (r.end_ts - r.start_ts) / 86400
    return {"name": name, "params": params, "period": f"{start}..{end or 'now'}", "net": float(r.net_pnl),
            "price": float(r.price_pnl), "fees": float(r.fees), "funding_recv": float(-r.funding),
            "fills": len(r.trades), "dd": float(r.max_drawdown[0]), "per_day": float(r.net_pnl) / days,
            "exposure": r.exposure, "killed": r.killed_at is not None}


def grid(name: str, space: dict) -> list[tuple[str, dict]]:
    keys = list(space)
    return [(name, dict(zip(keys, vals))) for vals in itertools.product(*(space[k] for k in keys))]


def run_jobs(configs, start, end, workers=8) -> list[dict]:
    jobs = [(n, p, start, end) for n, p in configs]
    with ProcessPoolExecutor(workers) as ex:
        return list(ex.map(evaluate, jobs))


def fmt(r: dict) -> str:
    p = ", ".join(f"{k}={v}" for k, v in r["params"].items() if k != "size")
    return (f"{r['name']:8} {p:62} net {r['net']:+8.2f}  price {r['price']:+8.2f}  fees {-r['fees']:+7.2f}  "
            f"funding {r['funding_recv']:+7.2f}  fills {r['fills']:4}  dd {r['dd']:8.2f}{'  KILL' if r['killed'] else ''}")
