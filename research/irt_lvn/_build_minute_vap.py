"""Aggregate tick files into per-minute volume-at-price parquet."""
from __future__ import annotations

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
JOBS = [
    ("ES", ROOT / "data/irt/@ES#_1Tick.csv", ROOT / "data/irt/vap_minute_ES.parquet"),
    ("NQ", ROOT / "data/irt/@NQ#_Export_1tick.csv", ROOT / "data/irt/vap_minute_NQ.parquet"),
]


def main() -> None:
    con = duckdb.connect()
    for name, src, dst in JOBS:
        print(f"Building {name} minute VAP → {dst.name} …", flush=True)
        if dst.exists():
            dst.unlink()
        src_s = str(src).replace("\\", "/")
        dst_s = str(dst).replace("\\", "/")
        con.execute(
            f"""
            COPY (
                SELECT
                    date_trunc('minute', DATE) AS minute,
                    cast(round(PRICE / {TICK}) AS BIGINT) AS row_tick,
                    cast(sum(TICKVOL) AS DOUBLE) AS vol
                FROM read_csv('{src_s}', delim='\\t', header=true, sample_size=20000)
                GROUP BY 1, 2
                ORDER BY 1, 2
            ) TO '{dst_s}' (FORMAT PARQUET)
            """
        )
        n = con.execute(
            f"SELECT count(*), count(DISTINCT minute) FROM read_parquet('{dst_s}')"
        ).fetchone()
        print(f"  rows={n[0]} minutes={n[1]}", flush=True)


if __name__ == "__main__":
    main()
