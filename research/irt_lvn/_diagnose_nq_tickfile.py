"""What's inside the NQ tick export? symbols, dup minutes vs ALL 30s bars."""
from __future__ import annotations

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
CON = duckdb.connect()
SRC = str(ROOT / "data" / "irt" / "@NQ#_Export_1tick.csv").replace("\\", "/")

cols = CON.execute(
    f"SELECT * FROM read_csv('{SRC}', delim='\\t', header=true, sample_size=20000) LIMIT 2"
).fetchall()
print("sample rows:", cols)
desc = CON.execute(
    f"DESCRIBE SELECT * FROM read_csv('{SRC}', delim='\\t', header=true, sample_size=20000)"
).fetchall()
print("columns:", desc)
sym_col = desc[0][0]
print(CON.execute(
    f"""SELECT "{sym_col}", count(*), cast(sum(TICKVOL) AS DOUBLE)
        FROM read_csv('{SRC}', delim='\\t', header=true, sample_size=20000)
        GROUP BY 1"""
).fetchall())
# trades per second at a sample time; are there duplicate (time,price,vol) rows?
print(CON.execute(
    f"""SELECT DATE, PRICE, TICKVOL, count(*) c
        FROM read_csv('{SRC}', delim='\\t', header=true, sample_size=20000)
        WHERE DATE BETWEEN TIMESTAMP '2026-10-07 14:30:00' AND TIMESTAMP '2026-10-07 14:30:10'
        GROUP BY 1,2,3 ORDER BY 1 LIMIT 20"""
).fetchall())
print("rows in 2026-10-07 14:30:00-14:30:30:",
      CON.execute(
          f"""SELECT count(*), cast(sum(TICKVOL) AS DOUBLE)
              FROM read_csv('{SRC}', delim='\\t', header=true, sample_size=20000)
              WHERE DATE > TIMESTAMP '2026-10-07 14:30:00'
                AND DATE <= TIMESTAMP '2026-10-07 14:30:30'"""
      ).fetchone())
