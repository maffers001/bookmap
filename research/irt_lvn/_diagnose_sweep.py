"""Sweep VAP construction variants vs IRT node SETS (not labels).

For each snapshot, score: fraction of IRT LVN/HVN within tol of *any*
predicted node, plus median nearest distance. Variants vary window
(calendar/session days), transform (raw / binned / smoothed), sensitivity.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

from research.irt_lvn.lvn_profile import load_all_snapshot, row_to_price

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


def trading_days(market: str, end: datetime) -> list:
    pq = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    rows = CON.execute(
        f"""SELECT DISTINCT cast(minute AS DATE) d FROM read_parquet('{pq}')
            WHERE cast(minute AS DATE) <= DATE '{end:%Y-%m-%d}' ORDER BY 1"""
    ).fetchall()
    return [r[0] for r in rows]


def detect(vap: dict[int, float], sens: int) -> tuple[list[float], list[float]]:
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
        is_v = all(v < vap.get(r + k, 0.0) and v < vap.get(r - k, 0.0)
                   for k in range(1, sens + 1))
        is_p = all(v > vap.get(r + k, 0.0) and v > vap.get(r - k, 0.0)
                   for k in range(1, sens + 1))
        if is_v and not is_p:
            valleys.append(r * TICK)
        elif is_p and not is_v:
            peaks.append(r * TICK)
    return valleys, peaks


def binned(vap: dict[int, float], b: int) -> dict[int, float]:
    out: dict[int, float] = {}
    for r, v in vap.items():
        key = (r // b) * b
        out[key] = out.get(key, 0.0) + v
    return out


def smoothed(vap: dict[int, float], w: int) -> dict[int, float]:
    half = w // 2
    rows = sorted(vap)
    out = {}
    for r in rows:
        vals = [vap.get(r + k, 0.0) for k in range(-half, half + 1)]
        out[r] = sum(vals) / len(vals)
    return out


def score(act: list[float], pred: list[float]) -> tuple[float, float, float]:
    if not act or not pred:
        return 0.0, 0.0, float("nan")
    dists = [min(abs(a - p) for p in pred) for a in act]
    return (float(np.mean([d <= TICK + 1e-9 for d in dists])),
            float(np.mean([d <= 1.0 + 1e-9 for d in dists])),
            float(np.median(dists)))


SNAPS = {
    "ES": [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
           ("2026-10-13", "14:30"), ("2026-10-20", "14:30")],
    "NQ": [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
           ("2026-10-13", "14:30"), ("2026-10-20", "14:30")],
}
SENS = {"ES": 15, "NQ": 100}


def variants(market: str, end: datetime):
    days = trading_days(market, end)
    base_windows = {}
    for w in (20, 30, 45, 60, 90, 120):
        base_windows[f"cal{w}"] = get_vap(market, end - timedelta(days=w), end)
    for ns in (20, 30, 60, 90):
        if len(days) > ns:
            sd = days[-ns]
            base_windows[f"sess{ns}"] = get_vap(
                market, datetime(sd.year, sd.month, sd.day), end)
    s0 = SENS[market]
    for wname, vap in base_windows.items():
        yield f"{wname}/raw/S{s0}", vap, s0
        for sf in (0.5, 2, 4):
            yield f"{wname}/raw/S{int(s0*sf)}", vap, max(1, int(round(s0 * sf)))
        for b in (2, 4):
            bv = binned(vap, b)
            # node price reported at bin's low edge row
            yield f"{wname}/bin{b}/S{s0}bins", bv, s0
            yield f"{wname}/bin{b}/S{s0//b}bins", bv, max(1, s0 // b)
        for wsm in (5, 9, 21):
            yield f"{wname}/smooth{wsm}/S{s0}", smoothed(vap, wsm), s0


def main() -> None:
    import collections
    agg = collections.defaultdict(list)
    for market, snaps in SNAPS.items():
        allp = ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv")
        for day, hhmm in snaps:
            actual = load_all_snapshot(allp, day, hhmm)
            if actual is None:
                print(f"skip {market} {day}")
                continue
            end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
            act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
            act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
            for name, vap, sens in variants(market, end):
                v, p = detect(vap, sens)
                sl = score(act_l, v)
                sh = score(act_h, p)
                agg[(market, name)].append((sl, sh))
            print(f"done {market} {day}", flush=True)

    for market in SNAPS:
        print(f"\n===== {market} (mean over snapshots; LVN hit@tick / hit@1pt / medDist | HVN same) =====")
        rows_out = []
        for (m, name), lst in agg.items():
            if m != market:
                continue
            ml = np.array([[x[0][0], x[0][1], x[0][2]] for x in lst], dtype=float)
            mh = np.array([[x[1][0], x[1][1], x[1][2]] for x in lst], dtype=float)
            rows_out.append((name, np.nanmean(ml[:, 0]), np.nanmean(ml[:, 1]),
                             np.nanmedian(ml[:, 2]), np.nanmean(mh[:, 0]),
                             np.nanmean(mh[:, 1]), np.nanmedian(mh[:, 2])))
        rows_out.sort(key=lambda r: -(r[1] + r[4]))
        for r in rows_out[:25]:
            print(f"  {r[0]:28} LVN {r[1]*100:5.1f}% {r[2]*100:5.1f}% med{r[3]:7.2f} | "
                  f"HVN {r[4]*100:5.1f}% {r[5]*100:5.1f}% med{r[6]:7.2f}")


if __name__ == "__main__":
    main()
