"""One-time: aggregate tick files into daily volume-at-price parquet (fast reuse)."""
from __future__ import annotations

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25

JOBS = [
    ("ES", ROOT / "data" / "irt" / "@ES#_1Tick.csv", ROOT / "data" / "irt" / "vap_daily_ES.parquet"),
    ("NQ", ROOT / "data" / "irt" / "@NQ#_Export_1tick.csv", ROOT / "data" / "irt" / "vap_daily_NQ.parquet"),
]


def main() -> None:
    con = duckdb.connect()
    for name, src, dst in JOBS:
        print(f"Building {name} daily VAP → {dst.name} …", flush=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        src_s = str(src).replace("\\", "/")
        dst_s = str(dst).replace("\\", "/")
        # DATE auto-detects as TIMESTAMP; PRICE/TICKVOL numeric.
        sql = f"""
            COPY (
                SELECT
                    cast(DATE AS DATE) AS day,
                    cast(round(PRICE / {TICK}) AS BIGINT) AS row_tick,
                    cast(sum(TICKVOL) AS DOUBLE) AS vol,
                    cast(count(*) AS BIGINT) AS n_prints
                FROM read_csv(
                    '{src_s}',
                    delim='\\t',
                    header=true,
                    sample_size=20000
                )
                GROUP BY 1, 2
                ORDER BY 1, 2
            ) TO '{dst_s}' (FORMAT PARQUET)
        """
        con.execute(sql)
        n = con.execute(
            f"SELECT count(*), count(DISTINCT day) FROM read_parquet('{dst_s}')"
        ).fetchone()
        print(f"  rows={n[0]} days={n[1]}", flush=True)


if __name__ == "__main__":
    main()
