"""Holdout: production config vs corrected config on unused snapshots.

ES corrected: ESZ6 1-min bars, trapezoid-ish end-weight 0.25, trailing 90d, S=15.
ES production: @ES# tick VAP, trailing 90d, S=15.
NQ both: @NQ# tick VAP trailing 30d S=100 (production) — holdout just measures it.
Also NQ 30s tick/bar identity check.
"""
from __future__ import annotations

import bisect
import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

from research.irt_lvn._diagnose_grid import MarketData, partial_day, score
from research.irt_lvn._diagnose_grid2 import near3
from research.irt_lvn._diagnose_variants import extrema_units, to_prices
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
CON = duckdb.connect()
HOLDOUT = [("2026-09-30", "14:30"), ("2026-10-15", "14:30"),
           ("2026-10-16", "14:30"), ("2026-10-19", "14:30"),
           ("2026-10-08", "14:30"), ("2026-10-14", "14:30")]


def nq_identity():
    print("### NQ 30s identity: ALL bar vs tick-file 30s bucket")
    path = ROOT / "data" / "irt" / "@NQ#_ALL.csv"
    bars = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["Date"].strip().startswith("2026-10-07 14:3"):
                bars.append(row)
    src = str(ROOT / "data" / "irt" / "@NQ#_Export_1tick.csv").replace("\\", "/")
    bars = [b for b in bars if len(b["Date"].strip()) > 16][:4]
    for b in bars:
        d = b["Date"].strip()
        end = datetime.strptime(d, "%Y-%m-%d %H:%M:%S")
        start = end - timedelta(seconds=30)
        r = CON.execute(
            f"""SELECT cast(min(PRICE) AS DOUBLE), cast(max(PRICE) AS DOUBLE),
                       cast(sum(TICKVOL) AS DOUBLE), count(*)
                FROM read_csv('{src}', delim='\\t', header=true, sample_size=20000)
                WHERE DATE > TIMESTAMP '{start:%Y-%m-%d %H:%M:%S}'
                  AND DATE <= TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}'"""
        ).fetchone()
        print(f"  bar {d} H={b['High']} L={b['Low']} V={b['Volume']} | "
              f"tick30s min={r[0]} max={r[1]} vol={r[2]} n={r[3]}")


def es_bar_matrix(w_end=0.25):
    by_day: dict[date, list] = defaultdict(list)
    with (ROOT / "data" / "irt" / "ESZ6_ALL.csv").open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                d = datetime.strptime(row["Date"].strip()[:10], "%Y-%m-%d").date()
                t = row["Date"].strip()[11:16]
                h, l, v = float(row["High"]), float(row["Low"]), float(row["Volume"])
            except (ValueError, KeyError):
                continue
            if v > 0 and h > 0:
                by_day[d].append((t, h, l, v))

    def add(vap, h, l, v):
        rh, rl = int(round(h / TICK)), int(round(l / TICK))
        if rh == rl:
            vap[rl] += v
            return
        total_w = (rh - rl - 1) + 2 * w_end
        share = v / total_w
        for r in range(rl, rh + 1):
            vap[r] += share * (w_end if r in (rl, rh) else 1.0)

    days_sorted = sorted(by_day)
    per_day = {}
    for d, bars in by_day.items():
        vap: dict[int, float] = defaultdict(float)
        for t, h, l, v in bars:
            add(vap, h, l, v)
        per_day[d] = dict(vap)
    all_rows = [r for dv in per_day.values() for r in dv]
    lo, hi = min(all_rows), max(all_rows)
    mat = np.zeros((len(days_sorted), hi - lo + 1))
    for i, d in enumerate(days_sorted):
        for r, v in per_day[d].items():
            mat[i, r - lo] = v
    cum = np.vstack([np.zeros((1, mat.shape[1])), np.cumsum(mat, axis=0)])
    return days_sorted, cum, lo, by_day, add


def main():
    nq_identity()
    # ES corrected
    days_sorted, cum, lo, by_day, add = es_bar_matrix()
    es_md = MarketData("ES", "tick")
    nq_md = MarketData("NQ", "tick")
    tot = defaultdict(lambda: [0, 0, 0])
    for day, hhmm in HOLDOUT:
        for market, md_tick in (("ES", es_md), ("NQ", nq_md)):
            actual = load_all_snapshot(
                ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv"),
                day, hhmm)
            if actual is None:
                print(f"skip {market} {day}")
                continue
            end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
            act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
            act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
            win, sens = (90, 15) if market == "ES" else (30, 100)
            # production: tick
            part = partial_day(market, end, "tick")
            arr = md_tick.vap_window(end.date(), win, part)
            vi, pi = extrema_units(arr, sens)
            key = f"{market}/production-tick"
            tot[key][0] += (score(act_l, near3(to_prices(vi, md_tick.lo), actual["Close"]), 0.0)
                            + score(act_h, near3(to_prices(pi, md_tick.lo), actual["Close"]), 0.0))
            tot[key][1] += (score(act_l, near3(to_prices(vi, md_tick.lo), actual["Close"]), TICK)
                            + score(act_h, near3(to_prices(pi, md_tick.lo), actual["Close"]), TICK))
            tot[key][2] += 6
            if market == "ES":
                end_day = end.date()
                partb: dict[int, float] = defaultdict(float)
                for t, h, l, v in by_day[end_day]:
                    if t <= hhmm:
                        add(partb, h, l, v)
                i0 = bisect.bisect_left(days_sorted, end_day - timedelta(days=90))
                i1 = bisect.bisect_left(days_sorted, end_day)
                arr2 = (cum[i1] - cum[i0]).copy()
                for r, v in partb.items():
                    arr2[r - lo] += v
                vi, pi = extrema_units(arr2, 15)
                key = "ES/corrected-bar-w0.25"
                tot[key][0] += (score(act_l, near3(to_prices(vi, lo), actual["Close"]), 0.0)
                                + score(act_h, near3(to_prices(pi, lo), actual["Close"]), 0.0))
                tot[key][1] += (score(act_l, near3(to_prices(vi, lo), actual["Close"]), TICK)
                                + score(act_h, near3(to_prices(pi, lo), actual["Close"]), TICK))
                tot[key][2] += 6
    print("\n### Holdout near-3 hits")
    for k, (e0, e25, n) in sorted(tot.items()):
        print(f"  {k:26} tol0={e0}/{n} tol.25={e25}/{n}")


if __name__ == "__main__":
    main()
