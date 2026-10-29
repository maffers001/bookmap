"""Feed identity test: does any tick-file minute match an *_ALL bar exactly?

If tick file is the same feed IRT built bars from, some minute (possibly
offset by tz / bar-start-vs-end labelling) has min/max/sum == bar L/H/Volume.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
CON = duckdb.connect()


def all_bars(market: str, day: str, hhmm_list: list[str]):
    fn = "ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv"
    out = []
    with (ROOT / "data" / "irt" / fn).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = row["Date"].strip()
            if d.startswith(f"{day} ") and d[11:16] in hhmm_list:
                out.append(row)
    return out


def minute_stats(market: str, centre: datetime, span_h: int = 14):
    pq = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    rows = CON.execute(
        f"""SELECT minute, cast(min(row_tick*{TICK}) AS DOUBLE), cast(max(row_tick*{TICK}) AS DOUBLE),
                   cast(sum(vol) AS DOUBLE)
            FROM read_parquet('{pq}')
            WHERE minute BETWEEN TIMESTAMP '{centre - timedelta(hours=span_h):%Y-%m-%d %H:%M:%S}'
                             AND TIMESTAMP '{centre + timedelta(hours=span_h):%Y-%m-%d %H:%M:%S}'
            GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    return [(m, float(mn), float(mx), float(v)) for m, mn, mx, v in rows]


def daily_vol_all(market: str, day: str) -> float:
    fn = "ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv"
    tot = 0.0
    n = 0
    with (ROOT / "data" / "irt" / fn).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["Date"].strip().startswith(day):
                tot += float(row["Volume"] or 0)
                n += 1
    return tot, n


def daily_vol_tick(market: str, day: str) -> float:
    pq = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    r = CON.execute(
        f"""SELECT sum(vol) FROM read_parquet('{pq}')
            WHERE cast(minute AS DATE) = DATE '{day}'"""
    ).fetchone()
    return r[0]


for market, day in (("ES", "2026-10-07"), ("NQ", "2026-10-07")):
    print(f"\n===== {market} {day} =====")
    print("ALL daily volume sum:", daily_vol_all(market, day))
    print("tick daily volume sum:", daily_vol_tick(market, day))
    bars = all_bars(market, day, ["14:30", "10:00", "18:00"])
    for b in bars:
        bt = datetime.strptime(b["Date"].strip()[:16], "%Y-%m-%d %H:%M")
        lo, hi, vol = float(b["Low"]), float(b["High"]), float(b["Volume"])
        print(f" bar {bt} O={b['Open']} H={hi} L={lo} C={b['Close']} V={vol}")
        stats = minute_stats(market, bt)
        # best candidates by volume closeness
        cand = sorted(stats, key=lambda s: abs(s[3] - vol))[:4]
        for minute, mn, mx, v in sorted(cand, key=lambda s: s[0]):
            dh = (minute - bt).total_seconds() / 3600.0
            print(f"    tick-minute {minute} (dh={dh:+5.1f}) range {mn:.2f}-{mx:.2f} vol={v:.0f}")
        exact = [s for s in stats if abs(s[1] - lo) < 1e-9 and abs(s[2] - hi) < 1e-9]
        for minute, mn, mx, v in exact[:5]:
            dh = (minute - bt).total_seconds() / 3600.0
            print(f"    RANGE-EXACT tick-minute {minute} (dh={dh:+5.1f}) vol={v:.0f} vs bar {vol:.0f}")
        if not exact:
            print("    no range-exact minute in +-14h")
