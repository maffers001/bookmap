"""ES: sweep bar-distribution end-weight w and sensitivity (trailing 90d).

w=1 -> uniform inclusive; w=0.5 -> trapezoid; w=0 -> ends excluded (renorm).
Per-day VAP matrices built once per w from ESZ6 1-min bars.
"""
from __future__ import annotations

import bisect
import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from research.irt_lvn._diagnose_grid import score
from research.irt_lvn._diagnose_grid2 import near3
from research.irt_lvn._diagnose_variants import extrema_units, to_prices
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
SNAPS = [("2026-09-22", "14:30"), ("2026-09-29", "14:30"), ("2026-10-06", "14:30"),
         ("2026-10-07", "14:30"), ("2026-10-07", "15:00"), ("2026-10-07", "16:00"),
         ("2026-10-09", "14:30"), ("2026-10-13", "14:30"), ("2026-10-20", "14:30")]


def load_bars_by_day():
    by_day: dict[date, list] = defaultdict(list)
    with (ROOT / "data" / "irt" / "ESZ6_ALL.csv").open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                d = datetime.strptime(row["Date"].strip()[:10], "%Y-%m-%d").date()
                t = row["Date"].strip()[11:16]
                o, h, l, c, v = (float(row["Open"]), float(row["High"]), float(row["Low"]),
                                 float(row["Close"]), float(row["Volume"]))
            except (ValueError, KeyError):
                continue
            if v > 0 and h > 0:
                by_day[d].append((t, o, h, l, c, v))
    return by_day


def add_bar(vap, h, l, v, w_end):
    rh, rl = int(round(h / TICK)), int(round(l / TICK))
    if rh == rl:
        vap[rl] += v
        return
    n_interior = rh - rl - 1
    total_w = n_interior * 1.0 + 2 * w_end
    if total_w <= 0:
        share = v / (rh - rl + 1)
        for r in range(rl, rh + 1):
            vap[r] += share
        return
    share = v / total_w
    for r in range(rl, rh + 1):
        vap[r] += share * (w_end if r in (rl, rh) else 1.0)


def main():
    by_day = load_bars_by_day()
    days_sorted = sorted(by_day)
    allp = ROOT / "data" / "irt" / "ESZ6_ALL.csv"
    snaps = []
    for day, hhmm in SNAPS:
        actual = load_all_snapshot(allp, day, hhmm)
        if actual:
            snaps.append((day, hhmm, actual))
    for w_end in (0.0, 0.25, 0.5, 0.75, 1.0):
        # daily matrices
        per_day: dict[date, dict[int, float]] = {}
        for d, bars in by_day.items():
            vap: dict[int, float] = defaultdict(float)
            for t, o, h, l, c, v in bars:
                add_bar(vap, h, l, v, w_end)
            per_day[d] = dict(vap)
        all_rows = [r for dv in per_day.values() for r in dv]
        lo, hi = min(all_rows), max(all_rows)
        mat = np.zeros((len(days_sorted), hi - lo + 1))
        for i, d in enumerate(days_sorted):
            for r, v in per_day[d].items():
                mat[i, r - lo] = v
        cum = np.vstack([np.zeros((1, mat.shape[1])), np.cumsum(mat, axis=0)])
        for win in (75, 90):
            for sens in (13, 14, 15, 16, 17):
                e0 = e25 = n = 0
                for day, hhmm, actual in snaps:
                    end_day = datetime.strptime(day, "%Y-%m-%d").date()
                    part: dict[int, float] = defaultdict(float)
                    for t, o, h, l, c, v in by_day[end_day]:
                        if t <= hhmm:
                            add_bar(part, h, l, v, w_end)
                    i0 = bisect.bisect_left(days_sorted, end_day - timedelta(days=win))
                    i1 = bisect.bisect_left(days_sorted, end_day)
                    arr = (cum[i1] - cum[i0]).copy()
                    for r, v in part.items():
                        arr[r - lo] += v
                    vi, pi = extrema_units(arr, sens)
                    vv, pp = to_prices(vi, lo), to_prices(pi, lo)
                    act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
                    act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
                    e0 += (score(act_l, near3(vv, actual["Close"]), 0.0)
                           + score(act_h, near3(pp, actual["Close"]), 0.0))
                    e25 += (score(act_l, near3(vv, actual["Close"]), TICK)
                            + score(act_h, near3(pp, actual["Close"]), TICK))
                    n += 6
                print(f"w_end={w_end:.2f} win={win} S={sens}: tol0={e0}/{n} tol.25={e25}/{n}", flush=True)


if __name__ == "__main__":
    main()
