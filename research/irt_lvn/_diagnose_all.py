"""Diagnose IRT *_ALL.csv exports: cadence, node spacing/labelling, dVPOC.

No VAP involved - just what the export itself implies about the algorithm.
Stdlib + numpy only (runs in .venv).
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
D = ROOT / "data" / "irt"


def load(fn: str):
    with (D / fn).open(encoding="utf-8", newline="") as f:
        r = csv.reader(f)
        hdr = [h.strip() for h in next(r)]
        # positional columns (duplicates exist)
        idx: dict[str, list[int]] = defaultdict(list)
        for i, h in enumerate(hdr):
            idx[h].append(i)
        rows = list(r)
    def col(name: str, occ: int = 0) -> np.ndarray:
        i = idx[name][occ]
        out = []
        for row in rows:
            try:
                out.append(float(row[i]))
            except (ValueError, IndexError):
                out.append(0.0)
        return np.array(out)

    def parse_ts(s: str) -> datetime:
        s = s.strip().strip('"')
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        raise ValueError(s)

    ts = np.array([parse_ts(row[idx["Date"][0]]) for row in rows])
    return hdr, idx, ts, col


def analyse(name: str, fn: str) -> None:
    hdr, idx, ts, col = load(fn)
    print(f"\n===== {name} {fn} =====")
    print("cols:", hdr)
    n = len(ts)
    print("span:", ts[0], "->", ts[-1], "rows:", n)
    secs = np.array([(b - a).total_seconds() for a, b in zip(ts[:-1], ts[1:])])
    print("cadence sec counts:", Counter(secs.tolist()).most_common(6))
    per_day = Counter(t.date() for t in ts)
    pd_vals = np.array(sorted(per_day.values()))
    print(f"rows/day median={np.median(pd_vals):.0f} min={pd_vals.min()} max={pd_vals.max()} "
          f"days={len(pd_vals)}")

    close = col("Close")
    lvn = [col(f"LVN{i}") for i in (1, 2, 3)]
    hvn = [col(f"HVN{i}") for i in (1, 2, 3)]
    dvpoc = [col("dVPOC", k) for k in range(len(idx["dVPOC"]))]
    print("dVPOC occurrences:", len(dvpoc))

    any_node = np.zeros(n, bool)
    for a in lvn + hvn:
        any_node |= a != 0
    print(f"rows with any node nonzero: {any_node.sum()} / {n} ({100*any_node.mean():.1f}%)")
    for nm, a in zip(["LVN1","LVN2","LVN3","HVN1","HVN2","HVN3"], lvn + hvn):
        print(f"  {nm:5} nz={np.sum(a!=0):7d} ({100*np.mean(a!=0):5.1f}%)")
    for k, a in enumerate(dvpoc):
        print(f"  dVPOC#{k+1} nz={np.sum(a!=0):7d} ({100*np.mean(a!=0):5.1f}%) "
              f"changes on {100*np.mean(a[1:]!=a[:-1]):.2f}% of bars")

    m = any_node
    if not m.any():
        return
    for kind, arrs in (("LVN", lvn), ("HVN", hvn)):
        A = np.vstack(arrs).T  # n x 3
        full = m & np.all(A != 0, axis=1)
        if full.sum():
            d = np.abs(A[full] - close[full, None])
            mono = np.mean((d[:, 0] >= d[:, 1]) & (d[:, 1] >= d[:, 2]))
            print(f"  {kind}: |LVN1-close|>=|LVN2|>=|LVN3| in {100*mono:.1f}% of full rows")
            dmin, dmax = d.min(axis=1), d.max(axis=1)
            print(f"  {kind}: nearest dist p50={np.median(dmin):.2f} p90={np.percentile(dmin,90):.2f} | "
                  f"farthest-of-3 p50={np.median(dmax):.2f} p90={np.percentile(dmax,90):.2f}")

    def spacing(arrs_a, arrs_b, same: bool) -> np.ndarray:
        A = np.vstack(arrs_a).T[m]
        B = np.vstack(arrs_b).T[m]
        vals = []
        for ra, rb in zip(A, B):
            for x in ra[ra != 0]:
                for y in rb[rb != 0]:
                    if same and y <= x:
                        continue
                    vals.append(abs(x - y))
        return np.array(vals)

    for label, a, b, same in [("LVN-LVN", lvn, lvn, True),
                              ("HVN-HVN", hvn, hvn, True),
                              ("LVN-HVN", lvn, hvn, False)]:
        v = spacing(a, b, same)
        if len(v):
            print(f"  {label}: spacing p5={np.percentile(v,5):.2f} p25={np.percentile(v,25):.2f} "
                  f"median={np.median(v):.2f} frac<3.75={100*np.mean(v<3.75):.1f}% "
                  f"frac<1.0={100*np.mean(v<1.0):.1f}% frac<15(NQ100?)...")

    allnodes = np.concatenate([a[m] for a in lvn + hvn])
    allnodes = allnodes[allnodes != 0]
    frac = Counter(np.round(np.abs(allnodes) % 1.0, 2).tolist())
    print("  node price frac-part counts:", dict(sorted(frac.items())))

    # persistence of LVN set on node rows
    order = np.argsort(ts[m])
    A = np.vstack(lvn).T[m][order]
    sets = [frozenset(r) - {0.0} for r in A]
    for lag in (1, 10, 30, 100, 500):
        if len(sets) > lag:
            same = np.mean([sets[i] == sets[i - lag] for i in range(lag, len(sets))])
            print(f"    LVN set identical to {lag} node-rows ago: {100*same:.1f}%")
    trip = [tuple(r) for r in A]
    if len(trip) > 1:
        print(f"    LVN label-triple changes: {100*np.mean([a!=b for a,b in zip(trip[1:],trip[:-1])]):.1f}%")

    # snapshots around validation times
    dv = np.vstack(dvpoc).T
    H = np.vstack(hvn).T
    L = np.vstack(lvn).T
    for day, hh in (("2026-10-07", "14:2"), ("2026-10-07", "15:0"),
                    ("2026-10-09", "14:2"), ("2026-10-20", "14:2")):
        sel = [i for i, t in enumerate(ts)
               if t.strftime("%Y-%m-%d") == day and t.strftime("%H:%M").startswith(hh)]
        print(f"  -- {day} {hh}x  ({len(sel)} rows)")
        for i in sel[:4]:
            print(f"     {ts[i]} C={close[i]:.2f} dVPOC={dv[i]} HVN={H[i]} LVN={L[i]}")


if __name__ == "__main__":
    analyse("ES", "ESZ6_ALL.csv")
    analyse("NQ", "@NQ#_ALL.csv")
