"""Fingerprint IRT profile window via dVPOC: which lookback's argmax matches?

Also tracks dVPOC#k constant segments in the export.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25


def load_all(market: str):
    fn = "ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv"
    path = ROOT / "data" / "irt" / fn
    with path.open(encoding="utf-8", newline="") as f:
        r = csv.reader(f)
        hdr = [h.strip() for h in next(r)]
        idx: dict[str, list[int]] = defaultdict(list)
        for i, h in enumerate(hdr):
            idx[h].append(i)
        rows = [row for row in r if len(row) > idx["Close"][0]]
    return idx, rows


def f(row, idx, name, occ=0):
    try:
        return float(row[idx[name][occ]])
    except (ValueError, IndexError):
        return 0.0


def segments(market: str) -> None:
    idx, rows = load_all(market)
    print(f"\n== {market} dVPOC segments (value runs >= 200 bars) ==")
    for occ in range(3):
        vals = [f(r, idx, "dVPOC", occ) for r in rows]
        dates = [r[idx["Date"][0]].strip()[:16] for r in rows]
        runs = []
        start = 0
        for i in range(1, len(vals) + 1):
            if i == len(vals) or vals[i] != vals[start]:
                runs.append((dates[start], dates[i - 1], vals[start], i - start))
                start = i
        long_runs = [x for x in runs if x[3] >= 200]
        print(f" dVPOC#{occ+1}: {len(runs)} runs, {len(long_runs)} long")
        for x in long_runs[-14:]:
            print(f"    {x[0]} -> {x[1]}  VPOC={x[2]:.2f} bars={x[3]}")


def vpoc_at(con, parquet: str, start: datetime, end: datetime,
            mode: str = "vol") -> float:
    if mode == "vol":
        agg = "sum(vol)"
    else:  # tpo: number of minutes touching the row
        agg = "count(*)"
    q = f"""SELECT row_tick, {agg} AS a FROM read_parquet('{parquet}')
            WHERE minute >= TIMESTAMP '{start:%Y-%m-%d %H:%M:%S}'
              AND minute <  TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}'
            GROUP BY 1 ORDER BY a DESC LIMIT 1"""
    r = con.execute(q).fetchone()
    return r[0] * TICK if r else 0.0


def fingerprint(market: str, days: list[str]) -> None:
    idx, rows = load_all(market)
    by_day = {}
    for r in rows:
        d = r[idx["Date"][0]].strip()
        if d.startswith(tuple(f"{x} 14:30" for x in days)) and ":30" in d[11:]:
            # first 14:30 row
            key = d[:10]
            if key not in by_day:
                by_day[key] = r
    con = duckdb.connect()
    pq = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    windows = [7, 14, 20, 30, 45, 60, 90, 120, 150, 9999]
    print(f"\n== {market} VPOC fingerprint at 14:30 (vol) ==")
    print("day        dVPOC#2  dVPOC#3 | " + " ".join(f"w{w}" for w in windows))
    hits2 = defaultdict(int)
    hits3 = defaultdict(int)
    for day in days:
        r = by_day.get(day)
        if r is None:
            print(day, "no row")
            continue
        end = datetime.strptime(f"{day} 14:30", "%Y-%m-%d %H:%M")
        dv2, dv3 = f(r, idx, "dVPOC", 1), f(r, idx, "dVPOC", 2)
        outs = []
        for w in windows:
            start = end - timedelta(days=w) if w < 9999 else datetime(2020, 1, 1)
            v = vpoc_at(con, pq, start, end, "vol")
            outs.append(v)
            if abs(v - dv2) <= TICK:
                hits2[w] += 1
            if abs(v - dv3) <= TICK:
                hits3[w] += 1
        print(f"{day}  {dv2:9.2f} {dv3:9.2f} | " + " ".join(f"{v:.0f}" for v in outs))
    print(" hits vs #2:", dict(hits2))
    print(" hits vs #3:", dict(hits3))


if __name__ == "__main__":
    days = ["2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22", "2026-09-29",
            "2026-10-06", "2026-10-07", "2026-10-09", "2026-10-13", "2026-10-20"]
    for m in ("ES", "NQ"):
        segments(m)
        fingerprint(m, days)
