"""VAP-level diagnosis for one snapshot: clock alignment, VPOC, node field.

Compares our tick-derived VAP (various windows) against IRT export at the
same timestamp: dVPOC#2/#3, HVN/LVN sets. Uses vap_minute parquet (fast).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

from research.irt_lvn.lvn_profile import detect_nodes, load_all_snapshot, row_to_price

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25


def minute_range(market: str, when: datetime):
    con = duckdb.connect()
    p = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    rows = con.execute(
        f"SELECT row_tick, vol FROM read_parquet('{p}') WHERE minute = TIMESTAMP '{when:%Y-%m-%d %H:%M:%S}'"
    ).fetchall()
    if not rows:
        return None
    ticks = [r for r, _ in rows]
    return min(ticks) * TICK, max(ticks) * TICK, sum(v for _, v in rows)


def get_vap(market: str, start: datetime, end: datetime,
            hour_filter: str | None = None) -> dict[int, float]:
    con = duckdb.connect()
    p = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    hf = ""
    if hour_filter == "rth_uk":  # 14:30-21:00 UK approx US RTH
        hf = "AND cast(minute AS TIME) >= TIME '14:30:00' AND cast(minute AS TIME) < TIME '21:00:00'"
    elif hour_filter == "rth_et_as_uk_minus5":  # if tick clock is ET, RTH 14:30 ET looks like this naive
        hf = "AND cast(minute AS TIME) >= TIME '09:30:00' AND cast(minute AS TIME) < TIME '16:00:00'"
    rows = con.execute(
        f"""SELECT row_tick, sum(vol) FROM read_parquet('{p}')
            WHERE minute >= TIMESTAMP '{start:%Y-%m-%d %H:%M:%S}'
              AND minute <  TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}' {hf}
            GROUP BY 1""",
        ).fetchall()
    return {int(r): float(v) for r, v in rows}


def report(market: str, day: str, hhmm: str, sens: int) -> None:
    cfg_all = ROOT / "data" / "irt" / ("ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv")
    actual = load_all_snapshot(cfg_all, day, hhmm)
    end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
    print(f"\n===== {market} {day} {hhmm} =====")
    print("actual:", actual)

    # clock alignment: ALL bar OHLC for this minute vs tick-minute range
    import csv
    with cfg_all.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["Date"].strip().startswith(f"{day} {hhmm}"):
                print(f"ALL bar O={row['Open']} H={row['High']} L={row['Low']} C={row['Close']} V={row['Volume']}")
                break
    for dh in (-5, -4, -1, 0, 1, 5, 6):
        mr = minute_range(market, end + timedelta(hours=dh))
        print(f"  tick-minute {end + timedelta(hours=dh)} (dh={dh:+d}): range/vol={mr}")

    variants: list[tuple[str, dict[int, float]]] = []
    for days in (5, 10, 20, 30, 60, 90, 120, 180):
        variants.append((f"cal{days}d", get_vap(market, end - timedelta(days=days), end)))
    variants.append(("since_tick_start", get_vap(market, datetime(2020, 1, 1), end)))
    variants.append(("cal90d_rthUK", get_vap(market, end - timedelta(days=90), end, "rth_uk")))
    variants.append(("cal90d_rthETclock", get_vap(market, end - timedelta(days=90), end, "rth_et_as_uk_minus5")))
    variants.append(("cal30d_rthUK", get_vap(market, end - timedelta(days=30), end, "rth_uk")))

    act_lvn = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
    act_hvn = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
    for name, vap in variants:
        if not vap:
            print(f"{name:18} EMPTY")
            continue
        vpoc_row = max(vap, key=lambda r: vap[r])
        valleys, peaks = detect_nodes(vap, sensitivity=sens, strict=True, skip_edge=True)
        vset = [v.price for v in valleys]
        pset = [p.price for p in peaks]

        def nearest3(nodes, ref):
            return sorted(sorted(nodes, key=lambda x: abs(x - ref))[:3])

        def hit(act, pred):
            return sum(1 for a in act if any(abs(a - p) <= TICK + 1e-9 for p in pred))

        def hit1(act, pred):
            return sum(1 for a in act if any(abs(a - p) <= 1.0 + 1e-9 for p in pred))

        print(f"{name:18} VPOC={row_to_price(vpoc_row):.2f} rows={len(vap)} vol={sum(vap.values())/1e6:.1f}M "
              f"nV={len(vset)} nP={len(pset)} LVNset hit={hit(act_lvn, vset)}/3 (1pt {hit1(act_lvn, vset)}/3) "
              f"HVNset hit={hit(act_hvn, pset)}/3 (1pt {hit1(act_hvn, pset)}/3) "
              f"LVNnear3={nearest3(vset, actual['Close'])} HVNnear3={nearest3(pset, actual['Close'])}")
        # volume context at actual nodes: is each an actual local min on this VAP?
        for a in act_lvn:
            r = int(round(a / TICK))
            v = vap.get(r, 0.0)
            lo15 = min(vap.get(r + k, 0.0) for k in range(-sens, sens + 1))
            print(f"      actLVN {a:9.2f} vol={v:10.0f} (min in ±{sens} = {lo15:.0f} at this row? {v <= lo15 + 1e-9})")


if __name__ == "__main__":
    report("ES", "2026-10-07", "14:30", 15)
    report("NQ", "2026-10-07", "14:30", 100)
