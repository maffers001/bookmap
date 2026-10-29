"""Print VAP slices around IRT nodes; effective sensitivity of each IRT node."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
CON = duckdb.connect()


def get_vap(market: str, start: datetime, end: datetime) -> dict[int, float]:
    pq = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    rows = CON.execute(
        f"""SELECT row_tick, sum(vol) FROM read_parquet('{pq}')
            WHERE minute >= TIMESTAMP '{start:%Y-%m-%d %H:%M:%S}'
              AND minute <  TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}'
            GROUP BY 1"""
    ).fetchall()
    return {int(r): float(v) for r, v in rows}


def eff_sens(vap: dict[int, float], price: float, kind: str, smax: int = 300) -> int:
    """Largest S for which price is a strict local extremum on this VAP."""
    r = int(round(price / TICK))
    v = vap.get(r, 0.0)
    s_eff = 0
    for s in range(1, smax + 1):
        a, b = vap.get(r + s, 0.0), vap.get(r - s, 0.0)
        if kind == "LVN":
            if not (v < a and v < b):
                break
        else:
            if not (v > a and v > b):
                break
        s_eff = s
    return s_eff


def slice_around(vap, centre, half_ticks, act_marks):
    r0 = int(round(centre / TICK))
    vmax = max(vap.get(r, 0.0) for r in range(r0 - half_ticks, r0 + half_ticks + 1))
    for r in range(r0 - half_ticks, r0 + half_ticks + 1):
        v = vap.get(r, 0.0)
        bar = "#" * int(50 * v / vmax) if vmax else ""
        mark = act_marks.get(round(r * TICK, 2), "")
        print(f"    {r*TICK:10.2f} {v:12.0f} {bar} {mark}")


def report(market, day, hhmm, win_days, sens):
    allp = ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv")
    actual = load_all_snapshot(allp, day, hhmm)
    end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
    vap = get_vap(market, end - timedelta(days=win_days), end)
    print(f"\n===== {market} {day} {hhmm} win=cal{win_days} S={sens} =====")
    print("actual:", actual)
    marks = {}
    for k in ("LVN1", "LVN2", "LVN3"):
        marks[actual[k]] = f"<- IRT {k}"
    for k in ("HVN1", "HVN2", "HVN3"):
        marks[actual[k]] = marks.get(actual[k], "") + f"<- IRT {k}"
    for k in ("LVN1", "LVN2", "LVN3", "HVN1", "HVN2", "HVN3"):
        kind = "LVN" if k.startswith("L") else "HVN"
        p = actual[k]
        es = eff_sens(vap, p, kind)
        r = int(round(p / TICK))
        print(f"  {k} {p:10.2f} vol={vap.get(r,0):12.0f} effective-S on our VAP = {es}"
              f" (need {sens})")
    print(f"  -- slice {actual['Close']-18:.0f}..{actual['Close']+18:.0f} --")
    slice_around(vap, actual["Close"], 72 if market == "ES" else 200, marks)


if __name__ == "__main__":
    report("ES", "2026-10-07", "14:30", 90, 15)
    report("NQ", "2026-10-07", "14:30", 30, 100)
