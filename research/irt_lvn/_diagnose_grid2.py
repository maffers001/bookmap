"""Focused: sensitivity/rule sweep with node counts + nearest-3 operational score.

Constructions fixed by grid1: ES bar_uniform, NQ tick. Windows: ES 60/90/120,
NQ 25/30/35/45. Rules: all-in-window (strict) vs endpoint-only comparison.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np

from research.irt_lvn._diagnose_grid import MarketData, partial_day, score
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
SNAPS = [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
         ("2026-10-13", "14:30"), ("2026-10-20", "14:30")]


def extrema(arr, lo, sens, rule="all"):
    n = len(arr)
    if rule == "endpoint":
        is_v = np.ones(n, bool)
        is_p = np.ones(n, bool)
        for side in (-sens, sens):
            shifted = np.zeros(n)
            if side > 0:
                shifted[:-side] = arr[side:]
            else:
                shifted[-side:] = arr[:side]
            is_v &= arr < shifted
            is_p &= arr > shifted
        is_v[:sens] = False; is_v[n - sens:] = False
        is_p[:sens] = False; is_p[n - sens:] = False
        is_v[arr <= 0] = False; is_p[arr <= 0] = False
    else:
        is_v = np.ones(n, bool)
        is_p = np.ones(n, bool)
        for k in range(1, sens + 1):
            up = np.zeros(n); up[:-k] = arr[k:]
            dn = np.zeros(n); dn[k:] = arr[:-k]
            is_v &= (arr < up) & (arr < dn)
            is_p &= (arr > up) & (arr > dn)
            if not is_v.any() and not is_p.any():
                break
        is_v[:sens] = False; is_v[n - sens:] = False
        is_p[:sens] = False; is_p[n - sens:] = False
        is_v[arr <= 0] = False; is_p[arr <= 0] = False
    valleys = [(lo + i) * TICK for i in np.flatnonzero(is_v)]
    peaks = [(lo + i) * TICK for i in np.flatnonzero(is_p)]
    return valleys, peaks


def near3(nodes, ref):
    return sorted(nodes, key=lambda x: abs(x - ref))[:3]


def run(market, construction, windows, sens_list, rules=("all",)):
    md = MarketData(market, construction)
    allp = ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv")
    snaps = []
    for day, hhmm in SNAPS:
        actual = load_all_snapshot(allp, day, hhmm)
        end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
        part = partial_day(market, end, construction)
        snaps.append((day, end, actual, part))
    for rule in rules:
        print(f"\n### {market}/{construction} rule={rule}")
        print(f"{'win':>5} {'S':>5} {'allSetExact':>11} {'near3Exact':>10} {'nV':>5} {'nP':>5}  per-snap near3 (L+H /6)")
        for w in windows:
            vaps = [(day, actual, md.vap_window(end.date(), w, part))
                    for day, end, actual, part in snaps]
            for sens in sens_list:
                all_exact = near_exact = 0
                nv = npk = 0
                per = []
                for day, actual, arr in vaps:
                    v, p = extrema(arr, md.lo, sens, rule)
                    nv += len(v); npk += len(p)
                    act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
                    act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
                    all_exact += score(act_l, v, TICK) + score(act_h, p, TICK)
                    ne = (score(act_l, near3(v, actual["Close"]), TICK)
                          + score(act_h, near3(p, actual["Close"]), TICK))
                    near_exact += ne
                    per.append(f"{ne}")
                print(f"{w:>5} {sens:>5} {all_exact:>11} {near_exact:>10} "
                      f"{nv//len(vaps):>5} {npk//len(vaps):>5}  {' '.join(per)}")


if __name__ == "__main__":
    run("ES", "bar_uniform", [90], [3, 4, 5, 6, 7, 8, 10, 12, 15, 20], rules=("all", "endpoint"))
    run("ES", "tick", [90], [5, 7, 10, 15], rules=("all",))
    run("NQ", "tick", [30, 35], [10, 15, 20, 25, 30, 40, 50, 65, 80, 100, 130], rules=("all", "endpoint"))
    run("NQ", "bar_uniform", [30], [25, 50, 100], rules=("all",))
