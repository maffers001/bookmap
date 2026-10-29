"""Merge dominant-level hold / breach / stop features into features.csv.

  .venv\\Scripts\\python.exe -m research.momentum_slice.extract_level_hold
"""
from __future__ import annotations

import csv
from pathlib import Path

from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.features import level_hold_context

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
            conf = int(float(r["confirm_ns"]))
            for role, tr, px_key, dist_key in (
                ("nq", pair.nq, "nq_px_ignition", "nq_dist_max_ticks"),
                ("es", pair.es, "es_px_ignition", "es_dist_max_ticks"),
            ):
                dist = float(r.get(dist_key) or "nan")
                for k, v in level_hold_context(
                    tr, side=side, ignition_ns=ign, confirm_ns=conf,
                    pre_price_pts=float(r[px_key]), dist_max_ticks=dist,
                ).items():
                    r[f"{role}_{k}"] = round(v, 3) if isinstance(v, float) else v
        print(sid, "done", flush=True)
    fields: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k); fields.append(k)
    tmp = P.with_suffix(".tmp.csv")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    tmp.replace(P)
    print(f"merged level-hold into {P} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
