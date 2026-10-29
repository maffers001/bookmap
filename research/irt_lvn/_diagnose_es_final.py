"""ES trapezoid bar-distribution: window/S validation across many snapshots."""
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


def trap_bar(vap, o, h, l, c, v):
    rh, rl = int(round(h / TICK)), int(round(l / TICK))
    if rh == rl:
        vap[rl] += v
        return
    share = v / (rh - rl)
    for r in range(rl, rh + 1):
        vap[r] += share * (0.5 if r in (rl, rh) else 1.0)


def build_daily():
    per_day: dict[date, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    partial_bars: dict[date, list] = defaultdict(list)
    with (ROOT / "data" / "irt" / "ESZ6_ALL.csv").open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                dstr = row["Date"].strip()
                d = datetime.strptime(dstr[:10], "%Y-%m-%d").date()
                o, h, l, c, v = (float(row["Open"]), float(row["High"]), float(row["Low"]),
                                 float(row["Close"]), float(row["Volume"]))
            except (ValueError, KeyError):
                continue
            if v <= 0 or h <= 0:
                continue
            trap_bar(per_day[d], o, h, l, c, v)
            partial_bars[d].append((dstr[11:16], o, h, l, c, v))
    days = sorted(per_day)
    all_rows = [r for dv in per_day.values() for r in dv]
    lo, hi = min(all_rows), max(all_rows)
    mat = np.zeros((len(days), hi - lo + 1))
    for i, d in enumerate(days):
        for r, v in per_day[d].items():
            mat[i, r - lo] = v
    cum = np.vstack([np.zeros((1, mat.shape[1])), np.cumsum(mat, axis=0)])
    return days, cum, lo, partial_bars


def main():
    days, cum, lo, partial_bars = build_daily()
    allp = ROOT / "data" / "irt" / "ESZ6_ALL.csv"
    configs = []
    for anchor in ("2026-08-07", "2026-08-10"):
        configs.append((f"anchor{anchor}", anchor))
    for w in (60, 75, 90):
        configs.append((f"trailing{w}", w))
    totals = defaultdict(lambda: [0, 0, 0])
    vpoc_info = {}
    for day, hhmm in SNAPS:
        actual = load_all_snapshot(allp, day, hhmm)
        if actual is None:
            print(f"skip {day} {hhmm}")
            continue
        end_day = datetime.strptime(day, "%Y-%m-%d").date()
        act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
        act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
        # partial day: bars of end_day with bar time <= hhmm, minus full-day cum handled below
        part: dict[int, float] = defaultdict(float)
        for t, o, h, l, c, v in partial_bars[end_day]:
            if t <= hhmm:
                trap_bar(part, o, h, l, c, v)
        part_arr = np.zeros(cum.shape[1])
        for r, v in part.items():
            part_arr[r - lo] = v
        for name, cfg in configs:
            if isinstance(cfg, str):
                start_day = datetime.strptime(cfg, "%Y-%m-%d").date()
            else:
                start_day = end_day - timedelta(days=cfg)
            i0 = bisect.bisect_left(days, start_day)
            i1 = bisect.bisect_left(days, end_day)
            arr = cum[i1] - cum[i0] + part_arr
            if day == "2026-10-07" and hhmm == "14:30":
                vpoc_info[name] = (lo + int(np.argmax(arr))) * TICK
            for sens in (12, 15, 18):
                vi, pi = extrema_units(arr, sens)
                v = to_prices(vi, lo); p = to_prices(pi, lo)
                e0 = (score(act_l, near3(v, actual["Close"]), 0.0)
                      + score(act_h, near3(p, actual["Close"]), 0.0))
                e25 = (score(act_l, near3(v, actual["Close"]), TICK)
                       + score(act_h, near3(p, actual["Close"]), TICK))
                t = totals[(name, sens)]
                t[0] += e0; t[1] += e25; t[2] += 6
    print("ES trapezoid results (near3):")
    for (name, sens), (e0, e25, n) in sorted(totals.items(), key=lambda kv: -kv[1][0]):
        print(f"  {name:16} S={sens:3} tol0={e0:2d}/{n} tol.25={e25:2d}/{n}")
    print("VPOC Oct7 14:30 by config (IRT dVPOC#2=7764.00):",
          {k: round(v, 2) for k, v in vpoc_info.items()})


if __name__ == "__main__":
    main()
