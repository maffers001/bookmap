"""Dig EXP-0033: bar_ms × imbalance × vol gates × progress × dual-range sync."""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa

from research.config import load_config
from research.features.simultaneous import (
    SimultaneousFlowConfig,
    dual_range_imbalance_events,
    simultaneous_flow_events,
)
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
    _load_trades,
    _mean,
    _median,
)

OUT = Path(__file__).resolve().parents[1] / "results" / "EXP-0033" / "dig.json"


def pack(t: pa.Table) -> dict | None:
    if t.num_rows == 0:
        return None
    return {
        "n": int(t.num_rows),
        "hit10": round(float(_mean(t.column("hit_10pts"))), 3),
        "ret10s": round(float(_mean(t.column("ret_pts_10000ms"))), 2),
        "med10s": round(float(_median(t.column("ret_pts_10000ms"))), 2),
        "mae": round(float(_mean(t.column("mae_pts"))), 2),
    }


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])
    horizons = [250, 500, 1000, 2000, 5000, 10000]
    targets = [10, 20]

    flow_grid = []
    for bar in (100, 250, 500):
        for mi in (0.40, 0.55, 0.65):
            for prog in (0.0, 1.0):
                flow_grid.append(
                    dict(
                        kind="flow",
                        bar_ms=bar,
                        min_imbalance=mi,
                        min_progress_ticks=prog,
                        min_vol_nq=25.0,
                        min_vol_es=15.0,
                    )
                )
        # softer / harder vol
        flow_grid.append(
            dict(
                kind="flow",
                bar_ms=bar,
                min_imbalance=0.55,
                min_progress_ticks=0.0,
                min_vol_nq=15.0,
                min_vol_es=10.0,
            )
        )
        flow_grid.append(
            dict(
                kind="flow",
                bar_ms=bar,
                min_imbalance=0.55,
                min_progress_ticks=1.0,
                min_vol_nq=40.0,
                min_vol_es=25.0,
            )
        )

    range_grid = []
    for rt in (10, 22):
        for sync in (100, 250, 500):
            for mi in (0.30, 0.50):
                range_grid.append(
                    dict(
                        kind="dual_range",
                        range_ticks=rt,
                        sync_ms=sync,
                        min_imbalance=mi,
                        mode="imb_opp_low",
                    )
                )

    results = []
    for g in flow_grid:
        label = (
            f"flow_b{g['bar_ms']}_mi{g['min_imbalance']}_"
            f"p{g['min_progress_ticks']}_vn{g['min_vol_nq']}"
        )
        print(f"dig {label} …", flush=True)
        parts = []
        scfg = SimultaneousFlowConfig(
            bar_ms=g["bar_ms"],
            min_imbalance=g["min_imbalance"],
            min_vol_nq=g["min_vol_nq"],
            min_vol_es=g["min_vol_es"],
            min_progress_ticks=g["min_progress_ticks"],
        )
        for sid in sessions:
            nq = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
            es = _filter_rth_uk_table(_load_trades(parquet, sid, "ES"))
            if nq.num_rows == 0 or es.num_rows == 0:
                continue
            path_ts, path_px = build_price_path(nq)
            lab = label_events(
                simultaneous_flow_events(nq, es, scfg),
                path_ts,
                path_px,
                horizons,
                targets,
            )
            if lab.num_rows:
                parts.append(lab)
        row = {"label": label, **g, "result": pack(pa.concat_tables(parts)) if parts else None}
        results.append(row)
        r = row["result"] or {}
        print(f"  n={r.get('n')} hit10={r.get('hit10')} ret={r.get('ret10s')}", flush=True)

    for g in range_grid:
        label = f"dr_r{g['range_ticks']}_s{g['sync_ms']}_mi{g['min_imbalance']}"
        print(f"dig {label} …", flush=True)
        parts = []
        for sid in sessions:
            nq = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
            es = _filter_rth_uk_table(_load_trades(parquet, sid, "ES"))
            if nq.num_rows == 0 or es.num_rows == 0:
                continue
            path_ts, path_px = build_price_path(nq)
            lab = label_events(
                dual_range_imbalance_events(
                    nq,
                    es,
                    range_ticks=g["range_ticks"],
                    sync_ms=g["sync_ms"],
                    mode=g["mode"],
                    min_imbalance=g["min_imbalance"],
                ),
                path_ts,
                path_px,
                horizons,
                targets,
            )
            if lab.num_rows:
                parts.append(lab)
        row = {"label": label, **g, "result": pack(pa.concat_tables(parts)) if parts else None}
        results.append(row)
        r = row["result"] or {}
        print(f"  n={r.get('n')} hit10={r.get('hit10')} ret={r.get('ret10s')}", flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    ranked = [r for r in results if r.get("result") and r["result"]["n"] >= 30]
    ranked.sort(key=lambda r: (r["result"]["hit10"], r["result"]["ret10s"]), reverse=True)
    top = [{"label": r["label"], **r["result"]} for r in ranked[:15]]
    (OUT.parent / "dig_top.json").write_text(json.dumps(top, indent=2), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)
    print("TOP:", flush=True)
    for t in top[:10]:
        print(f"  {t}", flush=True)


if __name__ == "__main__":
    main()
