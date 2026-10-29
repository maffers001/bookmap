"""Test bar-derived VAP constructions vs tick VAP for IRT node/VPOC match."""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
CON = duckdb.connect()


def tick_vaps(market: str, start: datetime, end: datetime):
    pq = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    vol = {int(r): float(v) for r, v in CON.execute(
        f"""SELECT row_tick, sum(vol) FROM read_parquet('{pq}')
            WHERE minute >= TIMESTAMP '{start:%Y-%m-%d %H:%M:%S}'
              AND minute <  TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}' GROUP BY 1""").fetchall()}
    tpo = {int(r): float(v) for r, v in CON.execute(
        f"""SELECT row_tick, count(*) FROM read_parquet('{pq}')
            WHERE minute >= TIMESTAMP '{start:%Y-%m-%d %H:%M:%S}'
              AND minute <  TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}' GROUP BY 1""").fetchall()}
    return vol, tpo


def bar_vaps(market: str, start: datetime, end: datetime):
    fn = "ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv"
    path = ROOT / "data" / "irt" / fn
    uniform: dict[int, float] = defaultdict(float)
    close_v: dict[int, float] = defaultdict(float)
    tpo_bar: dict[int, float] = defaultdict(float)
    sdate, edate = start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = row["Date"].strip()
            if d < sdate or d >= edate:
                continue
            try:
                hi, lo, cl, v = (float(row["High"]), float(row["Low"]),
                                 float(row["Close"]), float(row["Volume"]))
            except ValueError:
                continue
            if v <= 0 or hi <= 0:
                continue
            rlo, rhi = int(round(lo / TICK)), int(round(hi / TICK))
            share = v / (rhi - rlo + 1)
            for r in range(rlo, rhi + 1):
                uniform[r] += share
                tpo_bar[r] += 1.0
            close_v[int(round(cl / TICK))] += v
    return dict(uniform), dict(close_v), dict(tpo_bar)


def eff_sens(vap, price, kind, smax=400):
    r = int(round(price / TICK))
    v = vap.get(r, 0.0)
    s = 0
    for k in range(1, smax + 1):
        a, b = vap.get(r + k, 0.0), vap.get(r - k, 0.0)
        if kind == "LVN":
            if not (v < a and v < b):
                break
        else:
            if not (v > a and v > b):
                break
        s = k
    return s


def nodes(vap, sens):
    rows = sorted(r for r, v in vap.items() if v > 0)
    lo, hi = rows[0], rows[-1]
    valleys, peaks = [], []
    for r in rows:
        if r - lo < sens or hi - r < sens:
            continue
        v = vap[r]
        if all(v < vap.get(r + k, 0.0) and v < vap.get(r - k, 0.0) for k in range(1, sens + 1)):
            valleys.append(r * TICK)
        if all(v > vap.get(r + k, 0.0) and v > vap.get(r - k, 0.0) for k in range(1, sens + 1)):
            peaks.append(r * TICK)
    return valleys, peaks


def report(market, day, hhmm, win_days, sens, dvpoc2):
    allp = ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv")
    actual = load_all_snapshot(allp, day, hhmm)
    end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
    start = end - timedelta(days=win_days)
    tv, tt = tick_vaps(market, start, end)
    bu, bc, bt = bar_vaps(market, start, end)
    cands = {
        "tick_vol": tv, "tick_tpo_min": tt, "bar_uniform": bu,
        "bar_close": bc, "bar_tpo": bt,
    }
    act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
    act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
    print(f"\n===== {market} {day} {hhmm} win=cal{win_days} S={sens} dVPOC#2={dvpoc2} =====")
    for name, vap in cands.items():
        vpoc = max(vap, key=lambda r: vap[r]) * TICK if vap else 0.0
        v, p = nodes(vap, sens)
        def hit(act, pred):
            return sum(1 for a in act if any(abs(a - x) <= TICK + 1e-9 for x in pred))
        def hit1(act, pred):
            return sum(1 for a in act if any(abs(a - x) <= 1.0 + 1e-9 for x in pred))
        effs = [eff_sens(vap, a, "LVN") for a in act_l] + [eff_sens(vap, a, "HVN") for a in act_h]
        print(f"  {name:14} VPOC={vpoc:9.2f} (vs #2 {vpoc-dvpoc2:+7.2f}) rows={len(vap):6} "
              f"LVN hit={hit(act_l, v)}/3 1pt={hit1(act_l, v)}/3 | HVN hit={hit(act_h, p)}/3 1pt={hit1(act_h, p)}/3 "
              f"| effS act nodes L+H={effs}")


if __name__ == "__main__":
    report("ES", "2026-10-07", "14:30", 90, 15, 7764.00)
    report("NQ", "2026-10-07", "14:30", 30, 100, 30800.00)
