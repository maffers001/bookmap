"""Merge clearance/pullback/leave-behind features into features.csv.

  .venv\\Scripts\\python.exe -m research.momentum_slice.extract_clearance
"""
from __future__ import annotations

import csv
from pathlib import Path

from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.features import clearance_context

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
                for k, v in clearance_context(
                    tr, side=side, ignition_ns=ign, confirm_ns=conf,
                    pre_price_pts=float(r[px_key]), dist_max_ticks=dist,
                ).items():
                    r[f"{role}_{k}"] = round(v, 3) if isinstance(v, float) else v
            # Combined: cleared past the dominant level in BOTH markets, and left behind
            for stamp in ("confirm", "c2s", "c5s"):
                nq_lb = r.get(f"nq_left_behind_{stamp}")
                es_lb = r.get(f"es_left_behind_{stamp}")
                nq_cl = r.get(f"nq_cleared_{stamp}")
                es_cl = r.get(f"es_cleared_{stamp}")
                if nq_lb is not None and es_lb is not None:
                    r[f"both_left_behind_{stamp}"] = int(nq_lb) * int(es_lb)
                if nq_cl is not None and es_cl is not None:
                    r[f"both_cleared_{stamp}"] = int(nq_cl) * int(es_cl)
                nq_pb = r.get(f"nq_pullback_{stamp}_pts")
                if nq_pb not in (None, ""):
                    r[f"nq_pullback_{stamp}_pts"] = float(nq_pb)
        print(sid, "done", flush=True)
    with open(P, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"merged clearance into {P} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
