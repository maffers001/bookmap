"""NQ: session-count windows x sensitivity, with -1h clock shift."""
from __future__ import annotations

import bisect
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from research.irt_lvn._diagnose_grid import MarketData, partial_day, score
from research.irt_lvn._diagnose_grid2 import near3
from research.irt_lvn._diagnose_variants import extrema_units, to_prices
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
SNAPS = [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
         ("2026-10-13", "14:30"), ("2026-10-20", "14:30"),
         ("2026-09-30", "14:30"), ("2026-10-08", "14:30"),
         ("2026-10-14", "14:30"), ("2026-10-15", "14:30"),
         ("2026-10-16", "14:30"), ("2026-10-19", "14:30")]


def main():
    md = MarketData("NQ", "tick")
    allp = ROOT / "data" / "irt" / "@NQ#_ALL.csv"
    # trading days = dates present in daily VAP (md.days)
    print("### NQ tick, shift -1h, session windows")
    for nsess in (15, 20, 25, 30, 35, 40):
        for sens in (80, 100, 120):
            e0 = n = 0
            per = []
            for day, hhmm in SNAPS:
                actual = load_all_snapshot(allp, day, hhmm)
                if actual is None:
                    continue
                end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M") - timedelta(hours=1)
                part = partial_day("NQ", end, "tick")
                # start = nsess-th trading day <= end.date()
                cand = [d for d in md.days if d <= end.date()]
                start_day = cand[-nsess] if len(cand) >= nsess else cand[0]
                n_days = (end.date() - start_day).days
                arr = md.vap_window(end.date(), n_days, part)
                vi, pi = extrema_units(arr, sens)
                v, p = to_prices(vi, md.lo), to_prices(pi, md.lo)
                act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
                act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
                a = (score(act_l, near3(v, actual["Close"]), 0.0)
                     + score(act_h, near3(p, actual["Close"]), 0.0))
                e0 += a; n += 6
                per.append(f"{day[5:]}:{a}")
            print(f"  sess={nsess:3} S={sens:3}: tol0={e0}/{n}  {' '.join(per)}", flush=True)


if __name__ == "__main__":
    main()
