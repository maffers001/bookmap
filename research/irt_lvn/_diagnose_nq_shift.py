"""NQ re-test with VAP end = ALL timestamp - 1h (chart clock offset)."""
from __future__ import annotations

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


def run(shift_h: float):
    md = MarketData("NQ", "tick")
    allp = ROOT / "data" / "irt" / "@NQ#_ALL.csv"
    print(f"\n### NQ tick shift={shift_h:+.0f}h (near3 S=100)")
    for win in (25, 30, 35, 45):
        e0 = e25 = n = 0
        per = []
        for day, hhmm in SNAPS:
            actual = load_all_snapshot(allp, day, hhmm)
            if actual is None:
                continue
            end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M") + timedelta(hours=shift_h)
            part = partial_day("NQ", end, "tick")
            arr = md.vap_window(end.date(), win, part)
            vi, pi = extrema_units(arr, 100)
            v, p = to_prices(vi, md.lo), to_prices(pi, md.lo)
            act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
            act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
            a = (score(act_l, near3(v, actual["Close"]), 0.0)
                 + score(act_h, near3(p, actual["Close"]), 0.0))
            b = (score(act_l, near3(v, actual["Close"]), TICK)
                 + score(act_h, near3(p, actual["Close"]), TICK))
            e0 += a; e25 += b; n += 6
            per.append(f"{day[5:]}:{a}")
        print(f"  win={win}: tol0={e0}/{n} tol.25={e25}/{n}  {' '.join(per)}")


if __name__ == "__main__":
    run(0)
    run(-1)
