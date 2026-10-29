"""NQ: VAP over last N CME sessions (session starts 23:00 tick-clock), shift -1h."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

from research.irt_lvn._diagnose_grid import score
from research.irt_lvn._diagnose_grid2 import near3
from research.irt_lvn._diagnose_variants import extrema_units, to_prices
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
CON = duckdb.connect()
PQ = str(ROOT / "data" / "irt" / "vap_minute_NQ.parquet").replace("\\", "/")
SNAPS = [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
         ("2026-10-13", "14:30"), ("2026-10-20", "14:30"),
         ("2026-09-30", "14:30"), ("2026-10-08", "14:30"),
         ("2026-10-14", "14:30"), ("2026-10-15", "14:30"),
         ("2026-10-16", "14:30"), ("2026-10-19", "14:30")]

SESS_LABELS = [r[0] for r in CON.execute(
    f"""SELECT DISTINCT cast(minute - INTERVAL 23 HOUR AS DATE) s
        FROM read_parquet('{PQ}') ORDER BY 1""").fetchall()]


def session_vap(end: datetime, n_sess: int):
    cur_label = (end - timedelta(hours=23)).date()
    idx = SESS_LABELS.index(cur_label)
    start_label = SESS_LABELS[max(0, idx - n_sess + 1)]
    rows = CON.execute(
        f"""SELECT row_tick, cast(sum(vol) AS DOUBLE) FROM read_parquet('{PQ}')
            WHERE minute < TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}'
              AND cast(minute - INTERVAL 23 HOUR AS DATE) >= DATE '{start_label}'
            GROUP BY 1"""
    ).fetchall()
    return {int(r): float(v) for r, v in rows}


def main():
    allp = ROOT / "data" / "irt" / "@NQ#_ALL.csv"
    for n_sess in (20, 25, 30, 35):
        for sens in (100,):
            e0 = n = 0
            per = []
            for day, hhmm in SNAPS:
                actual = load_all_snapshot(allp, day, hhmm)
                if actual is None:
                    continue
                end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M") - timedelta(hours=1)
                vap = session_vap(end, n_sess)
                rows = sorted(vap)
                lo = rows[0]
                arr = np.zeros(rows[-1] - lo + 1)
                for r, v in vap.items():
                    arr[r - lo] = v
                vi, pi = extrema_units(arr, sens)
                v, p = to_prices(vi, lo), to_prices(pi, lo)
                act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
                act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
                a = (score(act_l, near3(v, actual["Close"]), 0.0)
                     + score(act_h, near3(p, actual["Close"]), 0.0))
                e0 += a; n += 6
                per.append(f"{day[5:]}:{a}")
            print(f"sess={n_sess} S={sens}: tol0={e0}/{n}  {' '.join(per)}", flush=True)


if __name__ == "__main__":
    main()
