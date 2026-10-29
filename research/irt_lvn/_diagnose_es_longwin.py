"""ES trapezoid w0.25: longer windows (90 sessions ~126d?) on fit+holdout."""
from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from research.irt_lvn._diagnose_es_endweight import add_bar, load_bars_by_day
from research.irt_lvn._diagnose_grid import score
from research.irt_lvn._diagnose_grid2 import near3
from research.irt_lvn._diagnose_variants import extrema_units, to_prices
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
SNAPS = [("2026-09-22", "14:30"), ("2026-09-29", "14:30"), ("2026-10-06", "14:30"),
         ("2026-10-07", "14:30"), ("2026-10-09", "14:30"), ("2026-10-13", "14:30"),
         ("2026-10-20", "14:30"), ("2026-09-30", "14:30"), ("2026-10-15", "14:30"),
         ("2026-10-16", "14:30"), ("2026-10-19", "14:30"), ("2026-10-08", "14:30"),
         ("2026-10-14", "14:30")]


def main():
    by_day = load_bars_by_day()
    days_sorted = sorted(by_day)
    per_day = {}
    for d, bars in by_day.items():
        vap: dict[int, float] = defaultdict(float)
        for t, o, h, l, c, v in bars:
            add_bar(vap, h, l, v, 0.25)
        per_day[d] = dict(vap)
    all_rows = [r for dv in per_day.values() for r in dv]
    lo, hi = min(all_rows), max(all_rows)
    mat = np.zeros((len(days_sorted), hi - lo + 1))
    for i, d in enumerate(days_sorted):
        for r, v in per_day[d].items():
            mat[i, r - lo] = v
    cum = np.vstack([np.zeros((1, mat.shape[1])), np.cumsum(mat, axis=0)])
    allp = ROOT / "data" / "irt" / "ESZ6_ALL.csv"
    for win in (90, 105, 120, 135, 150):
        e0 = e25 = n = 0
        per = []
        for day, hhmm in SNAPS:
            actual = load_all_snapshot(allp, day, hhmm)
            if actual is None:
                continue
            end_day = datetime.strptime(day, "%Y-%m-%d").date()
            part: dict[int, float] = defaultdict(float)
            for t, o, h, l, c, v in by_day[end_day]:
                if t <= hhmm:
                    add_bar(part, h, l, v, 0.25)
            i0 = bisect.bisect_left(days_sorted, end_day - timedelta(days=win))
            i1 = bisect.bisect_left(days_sorted, end_day)
            arr = (cum[i1] - cum[i0]).copy()
            for r, v in part.items():
                arr[r - lo] += v
            vi, pi = extrema_units(arr, 15)
            vv, pp = to_prices(vi, lo), to_prices(pi, lo)
            act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
            act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
            a = (score(act_l, near3(vv, actual["Close"]), 0.0)
                 + score(act_h, near3(pp, actual["Close"]), 0.0))
            b = (score(act_l, near3(vv, actual["Close"]), TICK)
                 + score(act_h, near3(pp, actual["Close"]), TICK))
            e0 += a; e25 += b; n += 6
            per.append(f"{day[5:]}:{a}")
        print(f"win={win}: tol0={e0}/{n} tol.25={e25}/{n}  {' '.join(per)}", flush=True)


if __name__ == "__main__":
    main()
