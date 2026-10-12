"""One dig: soften flush-into-hold gates to see if pattern exists on these days."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.features.flush_hold import FlushHoldConfig, cross_confirm_flush_hold, flush_into_hold_events
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
    _load_depth,
    _load_trades,
    _mean,
    _median,
    _random_null_events,
)


def pack(parts: list[pa.Table]) -> dict | None:
    if not parts:
        return None
    t = pa.concat_tables(parts)
    return {
        "n": int(t.num_rows),
        "hit10": round(float(_mean(t.column("hit_10pts"))), 3),
        "ret10s": round(float(_mean(t.column("ret_pts_10000ms"))), 2),
        "med10s": round(float(_median(t.column("ret_pts_10000ms"))), 2),
    }


def run_spec(name: str, nq_kw: dict, sessions, parquet) -> dict:
    dual_parts, nq_parts, null_parts = [], [], []
    horizons = [250, 500, 1000, 2000, 5000, 10000]
    targets = [10, 20]
    for sid in sessions:
        nq_tr = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
        es_tr = _filter_rth_uk_table(_load_trades(parquet, sid, "ES"))
        nq_d = _load_depth(parquet, sid, "NQ")
        es_d = _load_depth(parquet, sid, "ES")
        if nq_tr.num_rows == 0 or nq_d.num_rows == 0:
            continue
        nq_cfg = FlushHoldConfig(**nq_kw)
        es_kw = {
            **nq_kw,
            "absolute_size": int(nq_kw.get("absolute_size", 40) * 2),
            "min_flush_ticks": max(15.0, float(nq_kw.get("min_flush_ticks", 40)) * 0.4),
            "min_execute_into": float(nq_kw.get("min_execute_into", 10)) * 2,
        }
        es_cfg = FlushHoldConfig(**es_kw)
        nq_ev = flush_into_hold_events(nq_tr, nq_d, nq_cfg, rth_uk_only=True)
        es_ev = (
            flush_into_hold_events(es_tr, es_d, es_cfg, rth_uk_only=True)
            if es_tr.num_rows and es_d.num_rows
            else pa.table({})
        )
        dual = cross_confirm_flush_hold(nq_ev, es_ev, sync_ms=2000)
        path_ts, path_px = build_price_path(nq_tr)
        for parts, ev in ((dual_parts, dual), (nq_parts, nq_ev)):
            if ev.num_rows:
                lab = label_events(ev, path_ts, path_px, horizons, targets)
                if lab.num_rows:
                    parts.append(lab)
        null_ev = _random_null_events(nq_tr, 50, seed=hash(name + sid) % (2**31))
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)
        if lab_null.num_rows:
            null_parts.append(lab_null)
        print(f"  {name} {sid}: nq={nq_ev.num_rows} dual={dual.num_rows}", flush=True)
    return {
        "name": name,
        **nq_kw,
        "dual": pack(dual_parts),
        "nq_only": pack(nq_parts),
        "null": pack(null_parts),
    }


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])
    base = dict(
        flush_window_ms=2000,
        z_score=99.0,
        tick_radius=12,
        reclaim_ticks=2,
        cooldown_ms=3000,
        tick_size=1.0,
        require_lvn_in_path=False,
    )
    specs = [
        ("default_80", {**base, "min_flush_ticks": 80, "absolute_size": 40, "min_execute_into": 20, "min_consume_ratio": 0.3, "max_penetration_ticks": 6, "hold_ms": 500}),
        ("flush40", {**base, "min_flush_ticks": 40, "absolute_size": 40, "min_execute_into": 20, "min_consume_ratio": 0.3, "max_penetration_ticks": 6, "hold_ms": 500}),
        ("flush40_soft", {**base, "min_flush_ticks": 40, "absolute_size": 25, "min_execute_into": 10, "min_consume_ratio": 0.15, "max_penetration_ticks": 10, "hold_ms": 250}),
        ("flush20_soft", {**base, "min_flush_ticks": 20, "absolute_size": 25, "min_execute_into": 10, "min_consume_ratio": 0.15, "max_penetration_ticks": 12, "hold_ms": 250}),
        ("flush120", {**base, "min_flush_ticks": 120, "absolute_size": 25, "min_execute_into": 10, "min_consume_ratio": 0.15, "max_penetration_ticks": 10, "hold_ms": 250}),
    ]
    out = []
    for name, kw in specs:
        print(f"=== {name} ===", flush=True)
        row = run_spec(name, kw, sessions, parquet)
        out.append(row)
        print(json.dumps({k: row[k] for k in ("name", "dual", "nq_only", "null", "min_flush_ticks", "absolute_size")}, indent=2), flush=True)
    Path("results/EXP-0027").mkdir(parents=True, exist_ok=True)
    Path("results/EXP-0027/dig_sensitivity.json").write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
