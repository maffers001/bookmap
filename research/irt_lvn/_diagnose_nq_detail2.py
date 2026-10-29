"""NQ bad-day detail under shift -1h, win35cal, S=100: distances of misses."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from research.irt_lvn._diagnose_grid import MarketData, partial_day
from research.irt_lvn._diagnose_grid2 import near3
from research.irt_lvn._diagnose_variants import extrema_units, to_prices
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
SNAPS = [("2026-10-15", "14:30"), ("2026-10-16", "14:30"),
         ("2026-10-19", "14:30"), ("2026-10-20", "14:30"),
         ("2026-10-07", "14:30")]


def main():
    md = MarketData("NQ", "tick")
    allp = ROOT / "data" / "irt" / "@NQ#_ALL.csv"
    for day, hhmm in SNAPS:
        actual = load_all_snapshot(allp, day, hhmm)
        end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M") - timedelta(hours=1)
        part = partial_day("NQ", end, "tick")
        arr = md.vap_window(end.date(), 35, part)
        vi, pi = extrema_units(arr, 100)
        v, p = to_prices(vi, md.lo), to_prices(pi, md.lo)
        print(f"\n{day} close={actual['Close']}")
        for kind, act, pred in (
            ("LVN", sorted(actual[k] for k in ("LVN1", "LVN2", "LVN3")), v),
            ("HVN", sorted(actual[k] for k in ("HVN1", "HVN2", "HVN3")), p),
        ):
            n3 = sorted(near3(pred, actual["Close"]))
            cells = []
            for a in act:
                d, x = min((abs(a - y), y) for y in pred)
                cells.append(f"{a:.2f}(nearest {x:.2f} d={d:.2f})")
            print(f"  {kind} act: {' '.join(cells)}")
            print(f"      our near3: {n3}")


if __name__ == "__main__":
    main()
