"""Merge tape-speed/burst-breadth features into features.csv (trades-only, fast).

  .venv\\Scripts\\python.exe -m research.momentum_slice.extract_tape_speed
"""
from __future__ import annotations

import csv
from pathlib import Path

from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.features import tape_speed_context

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
            ign, conf = int(float(r["ignition_ns"])), int(float(r["confirm_ns"]))
            for role, tr in (("nq", pair.nq), ("es", pair.es)):
                for k, v in tape_speed_context(tr, ign, conf).items():
                    r[f"{role}_{k}"] = round(v, 3) if isinstance(v, float) else v
            r["min_rel_speed"] = round(min(float(r["nq_rel_speed_30s"]), float(r["es_rel_speed_30s"])), 3)
            r["max_order_share"] = round(max(float(r.get("nq_burst_max_order_share", 0)),
                                             float(r.get("es_burst_max_order_share", 0))), 3)
        print(sid, "done", flush=True)
    with open(P, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"merged tape-speed into {P} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
