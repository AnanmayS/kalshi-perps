"""Regenerate the charts in docs/img/ from stored candles (README + docs/RESULTS.md).

    pip install -r requirements-dev.txt
    python scripts/walk_forward.py --json data/wf.json
    python scripts/make_report.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from backtest.data import load_bars, load_funding  # noqa: E402
from backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from kalshi_perps import load_settings  # noqa: E402
from kalshi_perps.store import MarketStore  # noqa: E402
from risk import RiskConfig  # noqa: E402
from strategies import BuyAndHold, FundingCarry, MeanReversion, Momentum  # noqa: E402

OUT = ROOT / "docs" / "img"
INK, INK2, MUTED, GRID, BASE, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
BLUE, ORANGE, AQUA, YELLOW, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#b4b2a9"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": BASE, "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.spines.top": False,
    "axes.spines.right": False, "axes.spines.left": False, "font.size": 10.5, "axes.titlesize": 13,
    "axes.titleweight": "medium", "axes.titlecolor": INK, "axes.titlelocation": "left", "legend.frameon": False,
})


def money(v: float) -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}"


def dollars(ax):
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{'-' if v < 0 else ''}${abs(v):,.0f}"))
    ax.xaxis.grid(False)
    ax.axhline(0, color=BASE, linewidth=1, zorder=1)


def curve(res):
    xs = [datetime.fromtimestamp(t, timezone.utc) for t, _ in res.equity_curve]
    ys = [float(e - res.starting_cash) for _, e in res.equity_curve]
    return xs, ys


def strategies_chart(bars, funding, cfg):
    runs = {
        "Funding carry": (FundingCarry(20, 3, Decimal(10)), BLUE, "-", 2.2),
        "Buy & hold": (BuyAndHold(Decimal(10)), ORANGE, "--", 1.6),
        "Mean reversion": (MeanReversion(240, 4.0, 0.5, 150, 480, Decimal(10)), GRAY, "-", 1.4),
        "Momentum (4h)": (Momentum(240, 80, 5, Decimal(10), 120), MUTED, "-", 1.4),
    }
    fig, ax = plt.subplots(figsize=(10, 4.6))
    results = {}
    for name, (strat, color, ls, lw) in runs.items():
        res = run_backtest(bars, funding, strat, cfg)
        results[name] = res
        xs, ys = curve(res)
        ax.plot(xs, ys, color=color, linestyle=ls, linewidth=lw, solid_capstyle="round", zorder=3)
        ax.annotate(f"{name}  {money(ys[-1])}", (xs[-1], ys[-1]), xytext=(8, 0), textcoords="offset points",
                    va="center", color=INK2, fontsize=9.5)
    dollars(ax)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.set_title("Cumulative net P&L, 10 contracts, Aug 14 – Sep 23 2026 (demo)", pad=12)
    fig.subplots_adjust(right=0.78)
    fig.savefig(OUT / "strategies_40d.png", dpi=160)
    plt.close(fig)
    return results


def carry_breakdown(res):
    xs, ys = curve(res)
    fev = sorted((e["ts"], -float(e["paid"])) for e in res.funding_events)
    fund, i, cum = [], 0, 0.0
    for t, _ in res.equity_curve:
        while i < len(fev) and fev[i][0] <= t:
            cum += fev[i][1]
            i += 1
        fund.append(cum)
    price = [n - f for n, f in zip(ys, fund)]
    fig, ax = plt.subplots(figsize=(10, 3.8))
    ax.plot(xs, fund, color=AQUA, linewidth=2, label=f"Funding received  {money(fund[-1])}")
    ax.plot(xs, price, color=YELLOW, linewidth=2, linestyle=(0, (2, 2)), label=f"Price move + fees  {money(price[-1])}")
    ax.plot(xs, ys, color=BLUE, linewidth=2.2, label=f"Net  {money(ys[-1])}")
    dollars(ax)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.legend(loc="upper left", labelcolor=INK2)
    ax.set_title("Funding carry: the profit is the funding, not the price", pad=12)
    fig.tight_layout()
    fig.savefig(OUT / "carry_breakdown.png", dpi=160)
    plt.close(fig)


def walk_forward_chart(wf_path: Path):
    wf = json.loads(wf_path.read_text())
    names = sorted(wf, key=lambda k: wf[k]["total"])
    vals = [wf[k]["total"] for k in names]
    fig, ax = plt.subplots(figsize=(10, 3.6))
    colors = [BLUE if n == "carry" else GRAY for n in names]
    ax.barh(names, vals, color=colors, height=0.55, zorder=3)
    for y, v in enumerate(vals):
        ax.annotate(money(v), (v, y), xytext=(6 if v >= 0 else -6, 0), textcoords="offset points",
                    ha="left" if v >= 0 else "right", va="center", color=INK2, fontsize=9.5)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{'-' if v < 0 else ''}${abs(v):,.0f}"))
    ax.yaxis.grid(False)
    ax.axvline(0, color=BASE, linewidth=1)
    ax.tick_params(axis="y", colors=INK2, length=0)
    lo = min(vals)
    ax.set_xlim(lo * 1.35, max(max(vals) * 3, 40))
    folds = next(iter(wf.values()))["folds"]
    ax.set_title(f"Walk-forward, out-of-sample P&L over {len(folds)} weekly folds "
                 f"({folds[0]['fold'][:10]} → {folds[-1]['fold'][-3:]})", pad=12)
    fig.tight_layout()
    fig.savefig(OUT / "walk_forward.png", dpi=160)
    plt.close(fig)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    store = MarketStore()
    bars = load_bars(store, s.ticker)
    funding = load_funding(store, s.ticker)
    cfg = BacktestConfig(taker_fee_rate=Decimal("0.0012"), risk=RiskConfig(s.max_notional_per_trade, s.daily_loss_limit))
    results = strategies_chart(bars, funding, cfg)
    carry_breakdown(results["Funding carry"])
    wf = ROOT / "data" / "wf.json"
    if wf.is_file():
        walk_forward_chart(wf)
    else:
        print("data/wf.json missing: run scripts/walk_forward.py --json data/wf.json first")
    for name, r in results.items():
        print(f"{name:16} net {money(float(r.net_pnl)):>10}  fills {len(r.trades):4}  max dd {float(r.max_drawdown[0]):8.2f}")
    print("wrote", ", ".join(p.name for p in sorted(OUT.glob("*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
