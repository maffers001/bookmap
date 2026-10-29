"""Broader near-3 grid: window x sensitivity, per-snapshot breakdown."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np

from research.irt_lvn._diagnose_grid import MarketData, partial_day, score
from research.irt_lvn._diagnose_grid2 import extrema, near3
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
SNAPS = [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
         ("2026-10-13", "14:30"), ("2026-10-20", "14:30")]


def run(market, construction, windows, sens_list):
    md = MarketData(market, construction)
    allp = ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv")
    snaps = []
    for day, hhmm in SNAPS:
        actual = load_all_snapshot(allp, day, hhmm)
        end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
        part = partial_day(market, end, construction)
        snaps.append((day, end, actual, part))
    print(f"\n### {market}/{construction}  (near3 exact / near3 within1pt, per-snap Oct7,9,13,20)")
    best = []
    for w in windows:
        vaps = [(day, actual, md.vap_window(end.date(), w, part))
                for day, end, actual, part in snaps]
        for sens in sens_list:
            tot_e = tot_1 = 0
            per = []
            for day, actual, arr in vaps:
                v, p = extrema(arr, md.lo, sens)
                act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
                act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
                nlv, nhv = near3(v, actual["Close"]), near3(p, actual["Close"])
                e = score(act_l, nlv, TICK) + score(act_h, nhv, TICK)
                o = score(act_l, nlv, 1.0) + score(act_h, nhv, 1.0)
                tot_e += e; tot_1 += o
                per.append(f"{e}/{o}")
            best.append((tot_e, tot_1, w, sens, per))
    best.sort(reverse=True)
    for tot_e, tot_1, w, sens, per in best[:18]:
        print(f"  win={w:4} S={sens:4} near3exact={tot_e:2}/24 near3@1pt={tot_1:2}/24  per={' '.join(per)}")


if __name__ == "__main__":
    run("ES", "bar_uniform", [30, 45, 60, 75, 90, 120, 150], [10, 12, 15, 18, 22])
    run("NQ", "tick", [20, 25, 30, 35, 45, 60, 75, 90], [50, 65, 80, 100, 120])
    run("NQ", "tick", [30], [100])  # baseline ref
    run("ES", "tick", [90], [15])
