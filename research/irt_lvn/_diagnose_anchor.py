"""Anchor sweep: VAP = ticks from START DATE (cumulative) to snapshot.

Tests whether IRT profile is a fixed-start developing profile rather than a
trailing window. Uses vap_daily parquet + partial snapshot day from minutes.
Score = IRT LVN+HVN exact-tick hits /6, hits within 1pt, VPOC error.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
CON = duckdb.connect()


def daily_vaps(market: str) -> dict[date, dict[int, float]]:
    pq = str(ROOT / "data" / "irt" / f"vap_daily_{market}.parquet").replace("\\", "/")
    rows = CON.execute(f"SELECT day, row_tick, vol FROM read_parquet('{pq}') ORDER BY 1, 2").fetchall()
    out: dict[date, dict[int, float]] = {}
    for d, r, v in rows:
        out.setdefault(d, {})[int(r)] = float(v)
    return out


def partial_day(market: str, end: datetime) -> dict[int, float]:
    pq = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    rows = CON.execute(
        f"""SELECT row_tick, sum(vol) FROM read_parquet('{pq}')
            WHERE minute >= TIMESTAMP '{end:%Y-%m-%d} 00:00:00'
              AND minute <  TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}' GROUP BY 1"""
    ).fetchall()
    return {int(r): float(v) for r, v in rows}


def nodes(vap, sens):
    rows = sorted(r for r, v in vap.items() if v > 0)
    if len(rows) < 2 * sens + 1:
        return [], []
    lo, hi = rows[0], rows[-1]
    valleys, peaks = [], []
    for r in rows:
        if r - lo < sens or hi - r < sens:
            continue
        v = vap[r]
        if v <= 0:
            continue
        if all(v < vap.get(r + k, 0.0) and v < vap.get(r - k, 0.0) for k in range(1, sens + 1)):
            valleys.append(r * TICK)
        if all(v > vap.get(r + k, 0.0) and v > vap.get(r - k, 0.0) for k in range(1, sens + 1)):
            peaks.append(r * TICK)
    return valleys, peaks


def score_snapshot(market: str, day: str, hhmm: str, sens_list=(15, 100)) -> None:
    allp = ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv")
    actual = load_all_snapshot(allp, day, hhmm)
    end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
    act = [actual[k] for k in ("LVN1", "LVN2", "LVN3", "HVN1", "HVN2", "HVN3")]
    act_l, act_h = act[:3], act[3:]
    daily = daily_vaps(market)
    part = partial_day(market, end)
    end_day = end.date()
    days_sorted = sorted(d for d in daily if d < end_day)
    # cumulative VAP from each anchor (whole days) + partial end day
    results = []
    # suffix[A] = partial end-day + whole days >= A (days_sorted are trading days)
    suffix: dict[date, dict[int, float]] = {}
    run: dict[int, float] = dict(part)
    for d in reversed(days_sorted):
        for r, v in daily[d].items():
            run[r] = run.get(r, 0.0) + v
        suffix[d] = dict(run)
    anchors = [d for d in days_sorted if d >= date(2026, 1, 1)]
    for sens in sens_list:
        scored = []
        for a in anchors:
            vap = suffix[a]
            v, p = nodes(vap, sens)
            vpoc = max(vap, key=lambda r: vap[r]) * TICK if vap else 0.0
            def hit(actx, pred, tol):
                return sum(1 for x in actx if any(abs(x - y) <= tol + 1e-9 for y in pred))
            h_exact = hit(act_l, v, TICK) + hit(act_h, p, TICK)
            h_1pt = hit(act_l, v, 1.0) + hit(act_h, p, 1.0)
            scored.append((h_exact, h_1pt, a, vpoc, len(v), len(p)))
        scored.sort(reverse=True)
        print(f"\n== {market} {day} {hhmm} S={sens}: best anchors by exact hits ==")
        for h_exact, h_1pt, a, vpoc, nv, npk in scored[:10]:
            print(f"   start {a} (days={(end_day-a).days:4d}) hits exact={h_exact}/6 1pt={h_1pt}/6 "
                  f"VPOC={vpoc:.2f} nV={nv} nP={npk}")
        # also print trailing-window anchors for reference (start = end - win)
        for win in (30, 60, 90, 120):
            a = end_day - timedelta(days=win)
            row = next((s for s in scored if s[2] == a), None)
            if row:
                print(f"   [trailing {win}d start {a}] exact={row[0]}/6 1pt={row[1]}/6 VPOC={row[3]:.2f}")


if __name__ == "__main__":
    score_snapshot("ES", "2026-10-07", "14:30", sens_list=(15,))
    score_snapshot("NQ", "2026-10-07", "14:30", sens_list=(100, 50))
