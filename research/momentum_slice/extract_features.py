"""Extract causal features for scan candidates + control events.

  .venv\\Scripts\\python.exe -m research.momentum_slice.extract_features \
      results/MOMENTUM_SLICE/scan_sep_oct results/MOMENTUM_SLICE/features

Outputs features.csv (one row per candidate, with confirm/ignition ns),
controls.csv (NQ-only tape bursts + seeded random nulls, forward-labelled).
Recomputes tape/cross per session to recover ns timestamps, matching
candidates.csv rows on (session, side, ignition_bst).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.correlated.harness import label_in_points, load_pair
from research.momentum_slice.detector import (
    ZONE_TICKS, _book_snapshots, cross_confirm, stack_metrics, tape_candidates,
    traded_in_zone, verify_event,
)
from research.momentum_slice.features import pre_move_context, pull_estimate, stack_geometry
from research.momentum_slice.scan import HORIZONS, TARGETS, bst
from research.run_experiment import _random_null_events

SNAP_OFFSETS = (60, 33, 13, 3)  # seconds before ignition


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "results/MOMENTUM_SLICE/scan_sep_oct")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "results/MOMENTUM_SLICE/features")
    out.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    with open(src / "candidates.csv", newline="", encoding="utf-8") as f:
        cand_rows = list(csv.DictReader(f))
    by_key = {(r["session"], r["side"], r["ignition_bst"]): r for r in cand_rows}
    sessions = sorted({r["session"] for r in cand_rows})
    feat_rows, ctrl_rows = [], []
    for sid in sessions:
        pair = load_pair(pq_dir, sid)
        if pair is None or pair.es.num_rows == 0:
            print(f"{sid}: no pair", flush=True); continue
        nq_c, es_c = tape_candidates(pair.nq, "NQ"), tape_candidates(pair.es, "ES")
        events = cross_confirm(nq_c, es_c)
        evs = [e for e in events
               if (sid, "LONG" if e["side"] > 0 else "SHORT", bst(e["ignition_ns"])) in by_key]
        print(f"{sid}: candidates {len(evs)}/{len(events)}", flush=True)
        if evs:
            depth_path = str(pq_dir / f"session_id={sid}" / "depth.parquet")
            snaps = {}
            for role, cands in (("NQ", [e["nq"] for e in evs]), ("ES", [e["es"] for e in evs])):
                want, bands = [], []
                for c in cands:
                    ref = int(round(c["pre_price_ticks"]))
                    bands.append((ref - ZONE_TICKS - 8, ref + ZONE_TICKS + 8))
                    want += [c["ignition_ns"] - s * 1_000_000_000 for s in SNAP_OFFSETS]
                    want.append(c["ignition_ns"] + 5_000_000_000)
                snaps[role] = _book_snapshots(depth_path, role, want, bands)
            for ev in evs:
                v = verify_event(ev, snaps["NQ"], snaps["ES"], pair.nq, pair.es)
                side = ev["side"]
                row = dict(by_key[(sid, "LONG" if side > 0 else "SHORT", bst(ev["ignition_ns"]))])
                row.update(ignition_ns=ev["ignition_ns"], confirm_ns=ev["confirm_ns"],
                           side_int=side, tod="open" if bst(ev["ignition_ns"])[11:16] < "14:55" else "late",
                           es_signed_lag_ms=round((ev["es"]["ignition_ns"] - ev["nq"]["ignition_ns"]) / 1e6))
                for role, tr in (("nq", pair.nq), ("es", pair.es)):
                    c = ev[role]
                    g = stack_geometry(snaps[role.upper()].get(c["ignition_ns"] - 3_000_000_000, {}),
                                       side, c["pre_price_ticks"], role.upper())
                    row[f"{role}_dist_max_ticks"] = round(g["dist_to_max_ticks"], 1)
                    row[f"{role}_dist_top_ticks"] = round(g["dist_to_top_ticks"], 1)
                    row[f"{role}_top3_conc"] = round(g["top3_conc"], 3)
                    curve = []
                    for s in SNAP_OFFSETS:
                        bk = snaps[role.upper()].get(c["ignition_ns"] - s * 1_000_000_000, {})
                        curve.append(stack_metrics(bk, side, c["pre_price_ticks"], role.upper())["stack_sum"])
                    row[f"{role}_stack_curve"] = "/".join(str(x) for x in curve)
                    row[f"{role}_persist60"] = round(curve[0] / curve[-1], 2) if curve[-1] else ""
                    row[f"{role}_pull_est"] = round(pull_estimate(
                        snaps[role.upper()].get(c["ignition_ns"] - 3_000_000_000, {}),
                        snaps[role.upper()].get(c["ignition_ns"] + 5_000_000_000, {}),
                        tr, side, c["pre_price_ticks"], c["ignition_ns"],
                        c["ignition_ns"] + 5_000_000_000), 3)
                row.update({k: (round(vv, 2) if isinstance(vv, float) else vv)
                            for k, vv in pre_move_context(pair.nq, ev["confirm_ns"], side, pair.bounds[0]).items()})
                feat_rows.append(row)
        # controls: NQ-only tape episodes + random nulls (same labelling)
        cross_igns = {e["nq"]["ignition_ns"] for e in events}
        nq_only = [c for c in nq_c if all(abs(c["ignition_ns"] - x) > 5_000_000_000 for x in cross_igns)]
        ctrl_defs = [("nq_only", c["confirm_ns"], c["side"]) for c in nq_only]
        nulls = _random_null_events(pair.nq, max(4, 2 * len(evs)), seed=abs(hash(sid)) % 10000)
        if nulls.num_rows:
            ctrl_defs += [("random", int(t), int(s)) for t, s in
                          zip(nulls.column("event_ts_ns").to_numpy(), nulls.column("side").to_numpy())]
        if ctrl_defs:
            tbl = pa.table({"event_ts_ns": [d[1] for d in ctrl_defs],
                            "side": np.array([d[2] for d in ctrl_defs], dtype=np.int8)})
            lab = label_in_points(tbl, pair, HORIZONS, TARGETS)
            for i, (kind, _, _) in enumerate(ctrl_defs):
                if i >= lab.num_rows:
                    break
                ctrl_rows.append(dict(session=sid, control=kind, side=ctrl_defs[i][2],
                                      **{h: round(lab.column(h)[i].as_py(), 1) for h in
                                         [f"ret_pts_{h}ms" for h in HORIZONS] + ["mfe_pts", "mae_pts"]}))
    for name, rows in (("features", feat_rows), ("controls", ctrl_rows)):
        if rows:
            with open(out / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"features={len(feat_rows)} controls={len(ctrl_rows)} -> {out}")


if __name__ == "__main__":
    main()
