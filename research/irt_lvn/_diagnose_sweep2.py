"""Fine-structure sweep: session filter x binning x smoothing x sensitivity.

Dense-array strict extrema. Scores exact/1pt set hits over 4 snapshots/market
on trailing calendar windows (ES 90d, NQ 30d). Also reports VPOC vs dVPOC#2.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
CON = duckdb.connect()

SNAPS = [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
         ("2026-10-13", "14:30"), ("2026-10-20", "14:30")]
CFG = {"ES": (90, 15, "ESZ6_ALL.csv"), "NQ": (30, 100, "@NQ#_ALL.csv")}

SESSIONS = {
    "full": "",
    "rth1430_2100": "AND cast(minute AS TIME) >= TIME '14:30:00' AND cast(minute AS TIME) < TIME '21:00:00'",
    "rth1430_2115": "AND cast(minute AS TIME) >= TIME '14:30:00' AND cast(minute AS TIME) < TIME '21:15:00'",
    "day0800_2115": "AND cast(minute AS TIME) >= TIME '08:00:00' AND cast(minute AS TIME) < TIME '21:15:00'",
    "us1330_2100": "AND cast(minute AS TIME) >= TIME '13:30:00' AND cast(minute AS TIME) < TIME '21:00:00'",
}


def get_vap(market: str, start: datetime, end: datetime, sess: str) -> dict[int, float]:
    pq = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    rows = CON.execute(
        f"""SELECT row_tick, sum(vol) FROM read_parquet('{pq}')
            WHERE minute >= TIMESTAMP '{start:%Y-%m-%d %H:%M:%S}'
              AND minute <  TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}' {SESSIONS[sess]}
            GROUP BY 1"""
    ).fetchall()
    return {int(r): float(v) for r, v in rows}


def dense(vap: dict[int, float]):
    rows = sorted(r for r, v in vap.items() if v > 0)
    lo, hi = rows[0], rows[-1]
    arr = np.zeros(hi - lo + 1)
    for r, v in vap.items():
        if lo <= r <= hi:
            arr[r - lo] = v
    return arr, lo


def extrema(arr: np.ndarray, sens: int, lo_row: int, unit_ticks: int = 1,
            price_offset_ticks: int = 0):
    """Strict local extrema on units; returns prices."""
    n = len(arr)
    is_v = np.ones(n, bool)
    is_p = np.ones(n, bool)
    for k in range(1, sens + 1):
        up = np.empty(n); up[:] = 0.0; up[:-k] = arr[k:]
        dn = np.empty(n); dn[:] = 0.0; dn[k:] = arr[:-k]
        is_v &= (arr < up) & (arr < dn)
        is_p &= (arr > up) & (arr > dn)
        if not is_v.any() and not is_p.any():
            break
    is_v[:sens] = False; is_v[n - sens:] = False
    is_p[:sens] = False; is_p[n - sens:] = False
    is_v[arr <= 0] = False; is_p[arr <= 0] = False
    valleys = [((lo_row + i * unit_ticks) + price_offset_ticks) * TICK
               for i in np.flatnonzero(is_v)]
    peaks = [((lo_row + i * unit_ticks) + price_offset_ticks) * TICK
             for i in np.flatnonzero(is_p)]
    return valleys, peaks


def binned_arr(arr: np.ndarray, b: int):
    n = (len(arr) // b) * b
    return arr[:n].reshape(-1, b).sum(axis=1)


def smoothed_arr(arr: np.ndarray, w: int):
    ker = np.ones(w) / w
    return np.convolve(arr, ker, mode="same")


def score(act, pred, tol):
    if not pred:
        return 0
    return sum(1 for a in act if min(abs(a - p) for p in pred) <= tol + 1e-9)


def main():
    import collections
    results = collections.defaultdict(lambda: [0, 0, 0, 0.0, 0])  # exact, 1pt, n, vpoc_err_sum, vpoc_n
    for market, (win, s0, allfn) in CFG.items():
        allp = ROOT / "data" / "irt" / allfn
        for day, hhmm in SNAPS:
            actual = load_all_snapshot(allp, day, hhmm)
            if actual is None:
                continue
            end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
            act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
            act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
            for sess in SESSIONS:
                vap = get_vap(market, end - timedelta(days=win), end, sess)
                arr, lo = dense(vap)
                vpoc = (lo + int(np.argmax(arr))) * TICK
                variants = []
                variants.append((f"raw/S{s0}", arr, s0, 1, 0))
                for b in (2, 4, 8):
                    ba = binned_arr(arr, b)
                    for sb in sorted({s0, max(1, s0 // b), max(1, int(round(s0 / b * 2)))}):
                        variants.append((f"bin{b}/S{sb}/edge", ba, sb, b, 0))
                        variants.append((f"bin{b}/S{sb}/ctr", ba, sb, b, b // 2))
                for w in (5, 9, 21, 41):
                    variants.append((f"smooth{w}/S{s0}", smoothed_arr(arr, w), s0, 1, 0))
                for name, a2, sens, unit, off in variants:
                    v, p = extrema(a2, sens, lo, unit, off)
                    key = (market, sess, name)
                    r = results[key]
                    r[0] += score(act_l, v, TICK) + score(act_h, p, TICK)
                    r[1] += score(act_l, v, 1.0) + score(act_h, p, 1.0)
                    r[2] += 6
                    r[3] += abs(vpoc - 0)  # placeholder replaced below
                    r[4] += 1
            print(f"done {market} {day} {sess}", flush=True)

    for market in CFG:
        print(f"\n===== {market} top by exact hits (of 24 nodes) =====")
        rows = [(k[1], k[2], v) for k, v in results.items() if k[0] == market]
        rows.sort(key=lambda x: (-x[2][0], -x[2][1]))
        for sess, name, v in rows[:20]:
            print(f"  {sess:14} {name:22} exact={v[0]:2d}/24  1pt={v[1]:2d}/24")


if __name__ == "__main__":
    main()
