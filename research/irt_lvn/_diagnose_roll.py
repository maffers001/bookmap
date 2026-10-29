"""Compare chart (*_ALL) daily O/C vs tick-file first/last-minute VWAP.

A constant pre-roll offset between the two series = adjustment mismatch.
Also daily volume ratios tick/ALL for gap detection.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
CON = duckdb.connect()


def all_daily(fn: str):
    days: dict[str, dict] = {}
    with (ROOT / "data" / "irt" / fn).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = row["Date"].strip()[:10]
            try:
                o, c, v = float(row["Open"]), float(row["Close"]), float(row["Volume"])
            except ValueError:
                continue
            if d not in days:
                days[d] = {"open": o, "close": c, "vol": 0.0}
            days[d]["close"] = c
            days[d]["vol"] += v
    return days


def tick_daily(market: str):
    pq = str(ROOT / "data" / "irt" / f"vap_minute_{market}.parquet").replace("\\", "/")
    rows = CON.execute(
        f"""SELECT cast(minute AS DATE) d,
                   cast(sum(vol) AS DOUBLE) vol,
                   arg_min(row_tick, minute) FILTER (WHERE rn_first = 1) AS _x
            FROM (SELECT *, row_number() OVER (PARTITION BY cast(minute AS DATE), minute) rn_first
                  FROM read_parquet('{pq}'))
            GROUP BY 1"""
    ).fetchall() if False else None
    # simpler: per day first/last minute VWAP via two queries
    q = f"""
    WITH m AS (
        SELECT minute, cast(minute AS DATE) AS d,
               cast(sum(row_tick * 0.25 * vol) / sum(vol) AS DOUBLE) AS vwap,
               cast(sum(vol) AS DOUBLE) AS vol
        FROM read_parquet('{pq}') GROUP BY 1, 2
    )
    SELECT d, cast(sum(vol) AS DOUBLE),
           arg_min(vwap, minute), arg_max(vwap, minute),
           min(vwap), max(vwap)
    FROM m GROUP BY 1 ORDER BY 1
    """
    out = {}
    for d, vol, first_vwap, last_vwap, mn, mx in CON.execute(q).fetchall():
        out[d.strftime("%Y-%m-%d")] = {"vol": float(vol), "first": float(first_vwap),
                                        "last": float(last_vwap)}
    return out


for market, fn in (("NQ", "@NQ#_ALL.csv"), ("ES", "ESZ6_ALL.csv")):
    print(f"\n===== {market}: ALL vs tick daily =====")
    ad = all_daily(fn)
    td = tick_daily(market)
    print(f"{'day':12} {'ALLopen':>10} {'tickFirst':>10} {'diffO':>8} | {'ALLclose':>10} {'tickLast':>10} {'diffC':>8} | volRatio")
    for d in sorted(set(ad) & set(td)):
        if d < "2026-09-08" or d > "2026-10-22":
            continue
        a, t = ad[d], td[d]
        print(f"{d:12} {a['open']:10.2f} {t['first']:10.2f} {t['first']-a['open']:+8.2f} | "
              f"{a['close']:10.2f} {t['last']:10.2f} {t['last']-a['close']:+8.2f} | {t['vol']/a['vol']:.4f}")
    # volume ratio outliers all days
    bad = [(d, td[d]["vol"] / ad[d]["vol"]) for d in sorted(set(ad) & set(td))
           if ad[d]["vol"] > 0 and abs(td[d]["vol"] / ad[d]["vol"] - 1) > 0.01]
    print("days with |vol ratio-1|>1%:", [(d, round(r, 3)) for d, r in bad][:20])
