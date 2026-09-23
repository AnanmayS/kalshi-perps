"""Walk-forward evaluation of every strategy family on stored candles.

    python scripts/walk_forward.py
    python scripts/walk_forward.py --folds 2026-08-26 2026-09-02 2026-09-09 2026-09-16 --json data/wf.json

For each weekly fold, each family's parameters are chosen using ONLY the data
before that fold, then scored on the fold. The totals are therefore fully
out-of-sample and measure the whole research process, not one lucky setting.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.sweep import grid, walk_forward  # noqa: E402

FAMILIES = {
    "carry": grid("carry", {"threshold_bps": [10, 20, 40], "window": [1, 3, 6]}),
    "carry+hysteresis": grid("carry", {"threshold_bps": [20, 40], "window": [1, 3], "exit_bps": [0, 10]}),
    "carry+rally guard": grid("carry", {"threshold_bps": [20], "window": [3], "trend_minutes": [240, 720, 1440],
                                        "trend_bps": [50, 100, 200]}),
    "mean reversion": grid("meanrev", {"lookback": [240], "entry_z": [3.0, 4.0], "exit_z": [0.5],
                                       "min_edge_bps": [100, 150], "max_hold": [480]}),
    "momentum": grid("momentum", {"lookback": [60, 240], "threshold_bps": [40, 80], "exit_bps": [5],
                                  "min_hold": [30, 120]}),
    "buy & hold": [("hold", {})],
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-08-14", help="first day of training data")
    ap.add_argument("--folds", nargs="+", default=["2026-08-26", "2026-09-02", "2026-09-09", "2026-09-16"],
                    help="fold start dates; the last fold runs to the newest stored candle")
    ap.add_argument("--json", default=None, help="write results here")
    args = ap.parse_args()
    folds = [(a, b) for a, b in zip(args.folds, args.folds[1:] + [None])]

    results = {}
    for fam, cfgs in FAMILIES.items():
        wf = walk_forward(cfgs, folds, args.start)
        total = sum(f["test"]["net"] for f in wf)
        results[fam] = {"total": total, "folds": [{"fold": f["fold"], "net": f["test"]["net"],
                                                   "fills": f["test"]["fills"], "chosen": f["chosen"]["params"]}
                                                  for f in wf]}
        print(f"{fam:18} out-of-sample {total:+9.2f}   " + "  ".join(f"{f['test']['net']:+8.2f}" for f in wf))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
