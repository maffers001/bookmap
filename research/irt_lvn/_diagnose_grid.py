"""Grid search window(days) x sensitivity for tick VAP and bar-uniform VAP.

Daily VAP matrices (per calendar day in file clock) + partial snapshot day.
Score = exact / 1pt set hits over snapshots, LVN+HVN (24 nodes/market).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
CON = duckdb.connect()
SNAPS = [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
         ("2026-10-13", "14:30"), ("2026-10-20", "14:30")]


def extrema_prices(arr: np.ndarray, lo_row: int, sens: int):
    """Strict extrema (same logic as _diagnose_sweep2.extrema, unit=1)."""
    n = len(arr)
    if n < 2 * sens + 1:
        return [], []
    is_v = np.ones(n, bool)
    is_p = np.ones(n, bool)
    for k in range(1, sens + 1):
        up = np.zeros(n); up[:-k] = arr[k:]
        dn = np.zeros(n); dn[k:] = arr[:-k]
        is_v &= (arr < up) & (arr < dn)
        is_p &= (arr > up) & (arr > dn)
        if not is_v.any() and not is_p.any():
            break
    is_v[:sens] = False; is_v[n - sens:] = False
    is_p[:sens] = False; is_p[n - sens:] = False
    is_v[arr <= 0] = False; is_p[arr <= 0] = False
    valleys = [(lo_row + i) * TICK for i in np.flatnonzero(is_v)]
    peaks = [(lo_row + i) * TICK for i in np.flatnonzero(is_p)]
    return valleys, peaks


def score(act, pred, tol):
    if not pred:
        return 0
    return sum(1 for a in act if min(abs(a - p) for p in pred) <= tol + 1e-9)


class MarketData:
    def __init__(self, market: str, construction: str):
        self.market = market
        self.construction = construction
        if construction == "tick":
            pq = str(ROOT / "data" / "irt" / f"vap_daily_{market}.parquet").replace("\\", "/")
            rows = CON.execute(
                f"SELECT day, row_tick, vol FROM read_parquet('{pq}')").fetchall()
            per_day: dict[date, dict[int, float]] = defaultdict(dict)
            for d, r, v in rows:
                per_day[d][int(r)] = float(v)
        else:  # bar_uniform from *_ALL.csv bars
            fn = "ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv"
            per_day = defaultdict(lambda: defaultdict(float))
            with (ROOT / "data" / "irt" / fn).open(encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        d = datetime.strptime(row["Date"].strip()[:10], "%Y-%m-%d").date()
                        h, l, v = float(row["High"]), float(row["Low"]), float(row["Volume"])
                    except (ValueError, KeyError):
                        continue
                    if v <= 0 or h <= 0:
                        continue
                    rl, rh = int(round(l / TICK)), int(round(h / TICK))
                    share = v / (rh - rl + 1)
                    dd = per_day[d]
                    for r in range(rl, rh + 1):
                        dd[r] += share
            per_day = {d: dict(v) for d, v in per_day.items()}
        self.days = sorted(per_day)
        self.day_index = {d: i for i, d in enumerate(self.days)}
        all_rows = [r for dv in per_day.values() for r in dv]
        self.lo, self.hi = min(all_rows), max(all_rows)
        mat = np.zeros((len(self.days), self.hi - self.lo + 1))
        for d, dv in per_day.items():
            i = self.day_index[d]
            for r, v in dv.items():
                mat[i, r - self.lo] = v
        self.cum = np.vstack([np.zeros((1, mat.shape[1])), np.cumsum(mat, axis=0)])

    def vap_window(self, end_day: date, n_days: int, partial: dict[int, float]) -> np.ndarray:
        """Whole calendar days in [end_day-n_days, end_day) + partial end day."""
        # cum over day rows whose date in range: use date arithmetic via index search
        import bisect
        start_day = end_day - timedelta(days=n_days)
        i0 = bisect.bisect_left(self.days, start_day)
        i1 = bisect.bisect_left(self.days, end_day)
        arr = self.cum[i1] - self.cum[i0]
        out = arr.copy()
        for r, v in partial.items():
            if self.lo <= r <= self.hi:
                out[r - self.lo] += v
        return out


def partial_day(market: str, end: datetime, construction: str,
                bars_cache=None) -> dict[int, float]:
    if construction == "tick":
        pq = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
        rows = CON.execute(
            f"""SELECT row_tick, sum(vol) FROM read_parquet('{pq}')
                WHERE minute >= TIMESTAMP '{end:%Y-%m-%d} 00:00:00'
                  AND minute <  TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}' GROUP BY 1"""
        ).fetchall()
        return {int(r): float(v) for r, v in rows}
    # bar_uniform partial: bars on end date with bar time <= end (bar end-labelled)
    fn = "ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv"
    out: dict[int, float] = defaultdict(float)
    end_s = end.strftime("%Y-%m-%d %H:%M")
    day_s = end.strftime("%Y-%m-%d")
    with (ROOT / "data" / "irt" / fn).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = row["Date"].strip()
            if not d.startswith(day_s) or d[:16] > end_s:
                continue
            try:
                h, l, v = float(row["High"]), float(row["Low"]), float(row["Volume"])
            except ValueError:
                continue
            if v <= 0 or h <= 0:
                continue
            rl, rh = int(round(l / TICK)), int(round(h / TICK))
            share = v / (rh - rl + 1)
            for r in range(rl, rh + 1):
                out[r] += share
    return dict(out)


def run(market: str, construction: str, sens_list, win_list):
    md = MarketData(market, construction)
    allp = ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv")
    snap_data = []
    for day, hhmm in SNAPS:
        actual = load_all_snapshot(allp, day, hhmm)
        if actual is None:
            continue
        end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
        part = partial_day(market, end, construction)
        snap_data.append((day, end, actual, part))
    results = []
    for w in win_list:
        # precompute VAP per snapshot for this window
        vaps = [(day, actual, md.vap_window(end.date(), w, part))
                for day, end, actual, part in snap_data]
        for sens in sens_list:
            exact = one = 0
            vpoc_err = []
            for day, actual, arr in vaps:
                v, p = extrema_prices(arr, md.lo, sens)
                act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
                act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
                exact += score(act_l, v, TICK) + score(act_h, p, TICK)
                one += score(act_l, v, 1.0) + score(act_h, p, 1.0)
            results.append((exact, one, w, sens))
    results.sort(reverse=True)
    print(f"\n===== {market}/{construction} top 15 (of {len(snap_data)*6} nodes) =====")
    for exact, one, w, sens in results[:15]:
        print(f"  win={w:4}d S={sens:4} exact={exact:2d} 1pt={one:2d}")


if __name__ == "__main__":
    run("ES", "bar_uniform", [5, 7, 10, 12, 15, 20, 25, 30], list(range(10, 151, 5)))
    run("ES", "tick", [7, 10, 15, 20], list(range(10, 151, 10)))
    run("NQ", "tick", [25, 40, 50, 65, 80, 100, 130, 160, 200], list(range(5, 121, 5)))
    run("NQ", "bar_uniform", [50, 100], list(range(5, 61, 5)))
