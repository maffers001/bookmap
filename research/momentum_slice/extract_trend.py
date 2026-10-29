"""Merge pre-ignition trend features into features.csv.

  .venv\\Scripts\\python.exe -m research.momentum_slice.extract_trend
"""
from __future__ import annotations

import csv
from pathlib import Path

from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.features import pre_ignition_trend

P = Path("results/MOMENTUM_SLICE/features/features.csv")


def main():
    with open(P, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    cfg = load_config(); pq_dir = Path(cfg["parquet_dir"])
    by_session: dict[str, list] = {}
    for r in rows:
        by_session.setdefault(r["session"], []).append(r)
    for sid, evs in sorted(by_session.items()):
        pair = load_pair(pq_dir, sid)
        if pair is None:
            continue
        for r in evs:
            side = int(float(r["side_int"]))
            ign = int(float(r["ignition_ns"]))
            for role, tr in (("nq", pair.nq), ("es", pair.es)):
                for k, v in pre_ignition_trend(
                    tr, side=side, ignition_ns=ign, window_start_ns=pair.bounds[0]
                ).items():
                    r[f"{role}_{k}"] = round(v, 3) if isinstance(v, float) else v
            # Combined: both markets with-trend, or NQ alone if ES missing
            nq_t = r.get("nq_trend_5m_pts")
            es_t = r.get("es_trend_5m_pts")
            if nq_t not in (None, "", "nan"):
                r["trend_5m_pts"] = float(nq_t)
                r["with_trend"] = int(float(nq_t) >= 0)
            if nq_t not in (None, "", "nan") and es_t not in (None, "", "nan"):
                r["both_with_trend"] = int(float(nq_t) >= 0 and float(es_t) >= 0)
        print(sid, "done", flush=True)
    fields, seen = [], set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k); fields.append(k)
    tmp = P.with_suffix(".tmp.csv")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    tmp.replace(P)
    print(f"merged trend into {P} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
