"""Anchor sweep v2: cumulative-from-start VAP, near-3 score at tol 0 / 0.25.

Constructions: ES bar_uniform S15, NQ tick S100 (winners so far).
For each snapshot find best start dates; check chart-start + k*period pattern.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from research.irt_lvn._diagnose_grid import MarketData, partial_day, score
from research.irt_lvn._diagnose_grid2 import extrema, near3
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
SNAPS = [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
         ("2026-10-13", "14:30"), ("2026-10-20", "14:30")]


def run(market, construction, sens):
    md = MarketData(market, construction)
    allp = ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv")
    print(f"\n### {market}/{construction} S={sens} anchor sweep (near3 tol0 / tol.25)")
    joint: dict[date, list[int]] = {}
    for day, hhmm in SNAPS:
        actual = load_all_snapshot(allp, day, hhmm)
        end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
        part = partial_day(market, end, construction)
        act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
        act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
        scored = []
        d = date(2026, 2, 1) if market == "ES" else date(2026, 4, 13)
        while d < end.date():
            n = (end.date() - d).days
            arr = md.vap_window(end.date(), n, part)
            v, p = extrema(arr, md.lo, sens)
            e0 = (score(act_l, near3(v, actual["Close"]), 0.0)
                  + score(act_h, near3(p, actual["Close"]), 0.0))
            e25 = (score(act_l, near3(v, actual["Close"]), 0.25)
                   + score(act_h, near3(p, actual["Close"]), 0.25))
            scored.append((e25, e0, d))
            joint.setdefault(d, []).append(e25)
            d += timedelta(days=1)
        scored.sort(reverse=True)
        print(f" {day}: best anchors: " +
              ", ".join(f"{d}({e0}/{e25})" for e25, e0, d in scored[:6]))
    print(" joint (sum tol.25 over snaps) best:")
    js = sorted(((sum(v), d) for d, v in joint.items() if len(v) == len(SNAPS)), reverse=True)
    for s, d in js[:8]:
        print(f"   start {d}: total {s}/24")


if __name__ == "__main__":
    run("ES", "bar_uniform", 15)
    run("NQ", "tick", 100)
