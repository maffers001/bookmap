"""Per-node detail for best configs + NQ Oct-20 anomaly checks."""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

from research.irt_lvn._diagnose_grid import MarketData, partial_day
from research.irt_lvn._diagnose_grid2 import extrema, near3
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
CON = duckdb.connect()
SNAPS = [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
         ("2026-10-13", "14:30"), ("2026-10-20", "14:30")]


def detail(market, construction, win, sens):
    md = MarketData(market, construction)
    allp = ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv")
    print(f"\n### {market}/{construction} win={win} S={sens}")
    for day, hhmm in SNAPS:
        actual = load_all_snapshot(allp, day, hhmm)
        end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
        part = partial_day(market, end, construction)
        arr = md.vap_window(end.date(), win, part)
        v, p = extrema(arr, md.lo, sens)
        act_l = sorted(actual[k] for k in ("LVN1", "LVN2", "LVN3"))
        act_h = sorted(actual[k] for k in ("HVN1", "HVN2", "HVN3"))
        nlv = sorted(near3(v, actual["Close"]))
        nhv = sorted(near3(p, actual["Close"]))
        def fmt(act, pred, allpred):
            out = []
            for a in act:
                d = min((abs(a - x), x) for x in allpred) if allpred else (float("nan"), float("nan"))
                tag = "OK " if d[0] <= TICK + 1e-9 else "MISS"
                out.append(f"{a:9.2f}[{tag} nearestPred={d[1]:9.2f} d={d[0]:6.2f}]")
            return " ".join(out)
        print(f" {day} close={actual['Close']:.2f}")
        print(f"   LVN act: {fmt(act_l, nlv, v)}")
        print(f"   LVN our near3: {nlv}")
        print(f"   HVN act: {fmt(act_h, nhv, p)}")
        print(f"   HVN our near3: {nhv}")


def nq_oct20_timeline():
    path = ROOT / "data" / "irt" / "@NQ#_ALL.csv"
    print("\n### NQ Oct-19/20 ALL timeline (hourly samples)")
    with path.open(encoding="utf-8", newline="") as f:
        rdr = csv.reader(f)
        hdr = [h.strip() for h in next(rdr)]
        idx: dict[str, list[int]] = defaultdict(list)
        for i, h in enumerate(hdr):
            idx[h].append(i)
        rows = list(rdr)
    def g(row, name, occ=0):
        try:
            return float(row[idx[name][occ]])
        except ValueError:
            return 0.0
    for row in rows:
        d = row[idx["Date"][0]].strip()
        if d.startswith(("2026-10-19", "2026-10-20")) and d[14:16] == "00" and d[11:13] in (
                "08", "10", "12", "14", "16", "18", "20"):
            print(f"  {d} C={g(row,'Close'):9.2f} dVPOC2={g(row,'dVPOC',1):9.2f} "
                  f"LVN={[g(row,f'LVN{i}') for i in (1,2,3)]} HVN={[g(row,f'HVN{i}') for i in (1,2,3)]}")
    # tick vs ALL daily volumes around then
    pq = str(ROOT / "data" / "irt" / "vap_minute_NQ.parquet").replace("\\", "/")
    print("### NQ daily volume: ALL bars vs tick file")
    allvol: dict[str, float] = defaultdict(float)
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            allvol[row["Date"].strip()[:10]] += float(row["Volume"] or 0)
    for day in ("2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21"):
        tv = CON.execute(
            f"SELECT cast(sum(vol) AS DOUBLE) FROM read_parquet('{pq}') "
            f"WHERE cast(minute AS DATE) = DATE '{day}'").fetchone()[0]
        print(f"  {day}: ALL={allvol[day]:12.0f} tick={tv:12.0f} ratio={tv/allvol[day] if allvol[day] else 0:.4f}")


if __name__ == "__main__":
    detail("ES", "bar_uniform", 75, 15)
    detail("NQ", "tick", 30, 100)
    nq_oct20_timeline()
