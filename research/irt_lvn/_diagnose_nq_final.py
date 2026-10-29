"""NQ: VPOC fingerprint by construction x window, then node score of best."""
from __future__ import annotations

import bisect
import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

from research.irt_lvn._diagnose_grid import score
from research.irt_lvn._diagnose_grid2 import near3
from research.irt_lvn._diagnose_variants import extrema_units, to_prices
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
CON = duckdb.connect()
DAYS = ["2026-09-29", "2026-10-06", "2026-10-07", "2026-10-09",
        "2026-10-13", "2026-10-16", "2026-10-20"]


def load_dvpoc2(day: str) -> float:
    path = ROOT / "data" / "irt" / "@NQ#_ALL.csv"
    with path.open(encoding="utf-8", newline="") as f:
        rdr = csv.reader(f)
        hdr = [h.strip() for h in next(rdr)]
        idx: dict[str, list[int]] = defaultdict(list)
        for i, h in enumerate(hdr):
            idx[h].append(i)
        di, dvi = idx["Date"][0], idx["dVPOC"][1]
        for row in rdr:
            if row[di].strip().startswith(f"{day} 14:30"):
                return float(row[dvi])
    return 0.0


def daily_matrix(source: str):
    per_day: dict[date, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    if source == "tick":
        pq = str(ROOT / "data" / "irt" / "vap_daily_NQ.parquet").replace("\\", "/")
        for d, r, v in CON.execute(f"SELECT day, row_tick, vol FROM read_parquet('{pq}')").fetchall():
            per_day[d][int(r)] = float(v)
    else:
        mode = source  # bar_trap / bar_uniform
        with (ROOT / "data" / "irt" / "@NQ#_ALL.csv").open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    d = datetime.strptime(row["Date"].strip()[:10], "%Y-%m-%d").date()
                    h, l, v = float(row["High"]), float(row["Low"]), float(row["Volume"])
                except (ValueError, KeyError):
                    continue
                if v <= 0 or h <= 0:
                    continue
                rh, rl = int(round(h / TICK)), int(round(l / TICK))
                if mode == "bar_trap":
                    if rh == rl:
                        per_day[d][rl] += v
                    else:
                        share = v / (rh - rl)
                        for r in range(rl, rh + 1):
                            per_day[d][r] += share * (0.5 if r in (rl, rh) else 1.0)
                else:
                    share = v / (rh - rl + 1)
                    for r in range(rl, rh + 1):
                        per_day[d][r] += share
    days = sorted(per_day)
    all_rows = [r for dv in per_day.values() for r in dv]
    lo, hi = min(all_rows), max(all_rows)
    mat = np.zeros((len(days), hi - lo + 1))
    for i, d in enumerate(days):
        for r, v in per_day[d].items():
            mat[i, r - lo] = v
    cum = np.vstack([np.zeros((1, mat.shape[1])), np.cumsum(mat, axis=0)])
    return days, cum, lo


def partial_tick(end: datetime):
    pq = str(ROOT / "data" / "irt" / "vap_minute_NQ.parquet").replace("\\", "/")
    rows = CON.execute(
        f"""SELECT row_tick, sum(vol) FROM read_parquet('{pq}')
            WHERE minute >= TIMESTAMP '{end:%Y-%m-%d} 00:00:00'
              AND minute <  TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}' GROUP BY 1""").fetchall()
    return {int(r): float(v) for r, v in rows}


def partial_bar(end: datetime, mode: str):
    out: dict[int, float] = defaultdict(float)
    end_s = end.strftime("%Y-%m-%d %H:%M")
    with (ROOT / "data" / "irt" / "@NQ#_ALL.csv").open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = row["Date"].strip()
            if not d.startswith(end.strftime("%Y-%m-%d")) or d[:16] > end_s:
                continue
            try:
                h, l, v = float(row["High"]), float(row["Low"]), float(row["Volume"])
            except ValueError:
                continue
            if v <= 0 or h <= 0:
                continue
            rh, rl = int(round(h / TICK)), int(round(l / TICK))
            if mode == "bar_trap":
                if rh == rl:
                    out[rl] += v
                else:
                    share = v / (rh - rl)
                    for r in range(rl, rh + 1):
                        out[r] += share * (0.5 if r in (rl, rh) else 1.0)
            else:
                share = v / (rh - rl + 1)
                for r in range(rl, rh + 1):
                    out[r] += share
    return dict(out)


def main():
    mats = {src: daily_matrix(src) for src in ("tick", "bar_trap", "bar_uniform")}
    print("NQ VPOC match vs dVPOC#2 (exact tick) over", len(DAYS), "days")
    for src, (days, cum, lo) in mats.items():
        for w in (10, 15, 20, 25, 30, 40, 50, 60, 90):
            hits = 0
            errs = []
            for day in DAYS:
                end = datetime.strptime(f"{day} 14:30", "%Y-%m-%d %H:%M")
                actual = load_all_snapshot(ROOT / "data" / "irt" / "@NQ#_ALL.csv", day, "14:30")
                part = partial_tick(end) if src == "tick" else partial_bar(end, src)
                i0 = bisect.bisect_left(days, end.date() - timedelta(days=w))
                i1 = bisect.bisect_left(days, end.date())
                arr = (cum[i1] - cum[i0]).copy()
                for r, v in part.items():
                    arr[r - lo] += v
                vpoc = (lo + int(np.argmax(arr))) * TICK
                dv = load_dvpoc2(day)
                errs.append(vpoc - dv)
                if abs(vpoc - dv) <= TICK:
                    hits += 1
            print(f"  {src:12} win={w:3} VPOC hits={hits}/{len(DAYS)} errs={[round(e,1) for e in errs]}")

    # node score for tick windows (best known) + bar_trap windows at S=100
    print("\nNQ node near3 tol0 (S=100):")
    for src in ("tick", "bar_trap"):
        days, cum, lo = mats[src]
        for w in (20, 30, 45):
            e0 = e25 = n = 0
            per = []
            for day in DAYS:
                actual = load_all_snapshot(ROOT / "data" / "irt" / "@NQ#_ALL.csv", day, "14:30")
                if actual is None:
                    continue
                end = datetime.strptime(f"{day} 14:30", "%Y-%m-%d %H:%M")
                part = partial_tick(end) if src == "tick" else partial_bar(end, src)
                i0 = bisect.bisect_left(days, end.date() - timedelta(days=w))
                i1 = bisect.bisect_left(days, end.date())
                arr = (cum[i1] - cum[i0]).copy()
                for r, v in part.items():
                    arr[r - lo] += v
                vi, pi = extrema_units(arr, 100)
                v, p = to_prices(vi, lo), to_prices(pi, lo)
                act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
                act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
                a = (score(act_l, near3(v, actual["Close"]), 0.0)
                     + score(act_h, near3(p, actual["Close"]), 0.0))
                b = (score(act_l, near3(v, actual["Close"]), TICK)
                     + score(act_h, near3(p, actual["Close"]), TICK))
                e0 += a; e25 += b; n += 6
                per.append(f"{day[5:]}:{a}")
            print(f"  {src:10} win={w:3} tol0={e0}/{n} tol.25={e25}/{n} per={' '.join(per)}")


if __name__ == "__main__":
    main()
