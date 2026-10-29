"""Scan sessions for Liquidity Slice-Through candidates.

  .venv\\Scripts\\python.exe -m research.momentum_slice.scan --sessions 100926,160926
  .venv\\Scripts\\python.exe -m research.momentum_slice.scan --all-recent 24

Outputs results/MOMENTUM_SLICE/candidates.csv (+ summary.md) with one row per
cross-confirmed, depth-verified event: ignition/confirm in BST for Bookmap
replay lookup, stack/consume/delta diagnostics, and forward NQ outcomes
labelled from the causal confirm time.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from research.config import load_config
from research.correlated.harness import load_pair, label_in_points, list_sessions
from research.momentum_slice.detector import (
    CFG, ZONE_TICKS, _book_snapshots, cross_confirm, tape_candidates, verify_event,
)
import pyarrow as pa

UK = ZoneInfo("Europe/London")
HORIZONS = [2000, 5000, 10000, 20000, 60000]
TARGETS = [5.0, 10.0, 20.0, 30.0]


def bst(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).astimezone(UK).strftime("%Y-%m-%d %H:%M:%S")


def scan_session(cfg: dict, sid: str) -> list[dict]:
    pq_dir = Path(cfg["parquet_dir"])
    pair = load_pair(pq_dir, sid)
    if pair is None or pair.es.num_rows == 0:
        print(f"{sid}: no pair", flush=True)
        return []
    nq_c = tape_candidates(pair.nq, "NQ")
    es_c = tape_candidates(pair.es, "ES")
    events = cross_confirm(nq_c, es_c)
    print(f"{sid}: tape NQ={len(nq_c)} ES={len(es_c)} cross={len(events)}", flush=True)
    if not events:
        return []
    depth_path = str(pq_dir / f"session_id={sid}" / "depth.parquet")
    snaps: dict[str, dict] = {}
    for role, cands in (("NQ", [e["nq"] for e in events]), ("ES", [e["es"] for e in events])):
        want, bands = [], []
        for c in cands:
            ref = int(round(c["pre_price_ticks"]))
            bands.append((ref - ZONE_TICKS - 8, ref + ZONE_TICKS + 8))
            want += [c["ignition_ns"] - d for d in (33_000_000_000, 13_000_000_000, 3_000_000_000)]
        snaps[role] = _book_snapshots(depth_path, role, want, bands)
    rows = []
    for ev in events:
        v = verify_event(ev, snaps["NQ"], snaps["ES"], pair.nq, pair.es)
        ev_table = pa.table({"event_ts_ns": [v["confirm_ns"]], "side": [v["side"]]})
        lab = label_in_points(ev_table, pair, HORIZONS, TARGETS)
        r = dict(session=sid, side=("LONG" if v["side"] > 0 else "SHORT"),
                 ignition_bst=bst(v["ignition_ns"]), confirm_bst=bst(v["confirm_ns"]),
                 es_lag_ms=round(v["es_lag_ms"]),
                 entry_px=(round(lab.column("entry_price")[0].as_py(), 2) if lab.num_rows else None),
                 nq_px_ignition=round(v["nq"]["pre_price_ticks"] * 0.25, 2),
                 es_px_ignition=round(v["es"]["pre_price_ticks"] * 0.25, 2))
        for role in ("nq", "es"):
            c = v[role]
            r[f"{role}_disp_pt"] = round(c["disp_ticks"] * 0.25, 2)
            r[f"{role}_vol"] = round(c["vol"]); r[f"{role}_delta"] = round(c["delta"])
            r[f"{role}_stack"] = v[f"{role}_stack"]; r[f"{role}_n_big"] = v[f"{role}_n_big"]
            r[f"{role}_maxlvl"] = v[f"{role}_max_level"]
            r[f"{role}_density"] = round(v[f"{role}_density"], 2)
            r[f"{role}_persist"] = round(v[f"{role}_persist"], 2)
            r[f"{role}_consume"] = round(v[f"{role}_consume"], 2)
        if lab.num_rows:
            for h in HORIZONS:
                r[f"ret{h//1000}s"] = round(lab.column(f"ret_pts_{h}ms")[0].as_py(), 1)
            r["mfe"] = round(lab.column("mfe_pts")[0].as_py(), 1)
            r["mae"] = round(lab.column("mae_pts")[0].as_py(), 1)
        rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default="")
    ap.add_argument("--all-recent", type=int, default=0, help="last N sessions by id")
    ap.add_argument("--out", default="results/MOMENTUM_SLICE")
    args = ap.parse_args()
    cfg = load_config()
    if args.sessions:
        sids = [s.strip() for s in args.sessions.split(",") if s.strip()]
    else:
        sids = list_sessions(Path(cfg["parquet_dir"]))
        if args.all_recent:
            sids = sids[-args.all_recent:]
    rows: list[dict] = []
    for sid in sids:
        try:
            rows += scan_session(cfg, sid)
        except Exception as e:  # keep scanning; report at end
            print(f"{sid}: ERROR {type(e).__name__}: {e}", flush=True)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    if rows:
        with open(out / "candidates.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\n{len(rows)} cross-confirmed candidates -> {out}/candidates.csv")
    for r in rows:
        print(r)


if __name__ == "__main__":
    main()
