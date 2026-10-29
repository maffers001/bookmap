"""Pre-roll identity: does an ALL bar (chart clock -1h) match tick bucket exactly
in September (pre-roll) as it does in October? If prices are offset, the
continuous series is adjusted in one source but not the other.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
CON = duckdb.connect()
SRC = str(ROOT / "data" / "irt" / "@NQ#_Export_1tick.csv").replace("\\", "/")


def tick_bucket(start: datetime, end: datetime):
    r = CON.execute(
        f"""SELECT cast(min(PRICE) AS DOUBLE), cast(max(PRICE) AS DOUBLE),
                   cast(sum(TICKVOL) AS DOUBLE)
            FROM read_csv('{SRC}', delim='\\t', header=true, sample_size=20000)
            WHERE DATE > TIMESTAMP '{start:%Y-%m-%d %H:%M:%S}'
              AND DATE <= TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}'"""
    ).fetchone()
    return r


def main():
    path = ROOT / "data" / "irt" / "@NQ#_ALL.csv"
    want = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = row["Date"].strip()
            for day in ("2026-09-09", "2026-09-14", "2026-09-16", "2026-09-29", "2026-10-07"):
                if d.startswith(f"{day} 14:30:30") or d.startswith(f"{day} 10:00:30"):
                    want.append(row)
    for b in want:
        d = b["Date"].strip()
        end_chart = datetime.strptime(d, "%Y-%m-%d %H:%M:%S")
        end_tick = end_chart - timedelta(hours=1)
        start_tick = end_tick - timedelta(seconds=30)
        mn, mx, vol = tick_bucket(start_tick, end_tick)
        print(f"bar {d} H={b['High']} L={b['Low']} V={b['Volume']} | "
              f"tick(bucket-1h) min={mn} max={mx} vol={vol} "
              f"dH={float(b['High'])-mx:+.2f} dL={float(b['Low'])-mn:+.2f} dV={float(b['Volume'])-vol:+.0f}")


if __name__ == "__main__":
    main()
