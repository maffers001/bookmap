"""Find tick 30s buckets matching an *_ALL 30s bar (H/L/V) on 2026-10-07."""
from __future__ import annotations

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
CON = duckdb.connect()
SRC = str(ROOT / "data" / "irt" / "@NQ#_Export_1tick.csv").replace("\\", "/")

q = f"""
WITH b AS (
  SELECT date_trunc('minute', DATE) + CASE WHEN extract(second FROM DATE) < 30
                                           THEN INTERVAL 0 SECOND ELSE INTERVAL 30 SECOND END AS bucket,
         cast(min(PRICE) AS DOUBLE) AS mn, cast(max(PRICE) AS DOUBLE) AS mx,
         cast(sum(TICKVOL) AS DOUBLE) AS vol, count(*) AS n
  FROM read_csv('{SRC}', delim='\\t', header=true, sample_size=20000)
  WHERE DATE >= TIMESTAMP '2026-10-07 00:00:00' AND DATE < TIMESTAMP '2026-10-08 00:00:00'
  GROUP BY 1
)
SELECT * FROM b ORDER BY vol DESC LIMIT 10
"""
print("top 30s buckets by volume Oct7:")
for r in CON.execute(q).fetchall():
    print(" ", r)

q2 = f"""
WITH b AS (
  SELECT date_trunc('minute', DATE) + CASE WHEN extract(second FROM DATE) < 30
                                           THEN INTERVAL 0 SECOND ELSE INTERVAL 30 SECOND END AS bucket,
         cast(min(PRICE) AS DOUBLE) AS mn, cast(max(PRICE) AS DOUBLE) AS mx,
         cast(sum(TICKVOL) AS DOUBLE) AS vol
  FROM read_csv('{SRC}', delim='\\t', header=true, sample_size=20000)
  WHERE DATE >= TIMESTAMP '2026-10-07 00:00:00' AND DATE < TIMESTAMP '2026-10-08 00:00:00'
  GROUP BY 1
)
SELECT * FROM b WHERE abs(mn - 31215.25) < 2 AND abs(mx - 31257.75) < 2 ORDER BY bucket
"""
print("buckets near bar 14:30:30 H/L (31257.75/31215.25):")
for r in CON.execute(q2).fetchall():
    print(" ", r)

q3 = f"""
SELECT date_trunc('minute', DATE) AS m, cast(min(PRICE) AS DOUBLE), cast(max(PRICE) AS DOUBLE),
       cast(sum(TICKVOL) AS DOUBLE)
FROM read_csv('{SRC}', delim='\\t', header=true, sample_size=20000)
WHERE DATE >= TIMESTAMP '2026-10-07 00:00:00' AND DATE < TIMESTAMP '2026-10-08 00:00:00'
GROUP BY 1 ORDER BY sum(TICKVOL) DESC LIMIT 5
"""
print("top minutes Oct7:")
for r in CON.execute(q3).fetchall():
    print(" ", r)
