"""Merge base-speed features into features.csv (trades-only).

  .venv\\Scripts\\python.exe -m research.momentum_slice.extract_base_speed
"""
from __future__ import annotations

import csv
from pathlib import Path

from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.features import base_speed_context

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
            ign = int(float(r["ignition_ns"]))
            for role, tr in (("nq", pair.nq), ("es", pair.es)):
                for k, v in base_speed_context(tr, ign).items():
                    r[f"{role}_{k}"] = round(v, 3) if isinstance(v, float) else v
            r["min_base_rel"] = round(min(float(r["nq_base_rel_speed"]), float(r["es_base_rel_speed"])), 3)
            r["min_base_tps"] = round(min(float(r["nq_base_tps"]), float(r["es_base_tps"])), 3)
        print(sid, "done", flush=True)
    with open(P, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"merged base-speed into {P} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
