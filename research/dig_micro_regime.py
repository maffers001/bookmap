"""One-pass dig: why EXP-0021 composite is empty; loosen take/attract once."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.features.liquidity import (
    LargeLevelConfig,
    aggressive_take_events_from_levels,
    cross_confirm_liquidity_events,
    hold_without_trade_events_from_levels,
    micro_regime_composite_events,
    run_liquidity_tracker,
)
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
    _load_depth,
    _load_trades,
    _mean,
)


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])
    horizons = [250, 500, 1000, 2000, 5000, 10000]
    targets = [10, 20]

    specs = [
        {"name": "default", "nq_sz": 40, "attr": True, "take": 0.5, "pen": 1},
        {"name": "no_attr", "nq_sz": 40, "attr": False, "take": 0.5, "pen": 1},
        {"name": "soft_take", "nq_sz": 40, "attr": False, "take": 0.3, "pen": 0},
        {"name": "nq25_soft", "nq_sz": 25, "attr": False, "take": 0.3, "pen": 0},
        {"name": "nq25_attr", "nq_sz": 25, "attr": True, "take": 0.3, "pen": 0},
    ]
    out = []
    for sp in specs:
        nq_cfg = LargeLevelConfig(
            absolute_size=sp["nq_sz"], z_score=99.0, tick_radius=10, dwell_alert_sec=2.0
        )
        es_cfg = LargeLevelConfig(
            absolute_size=80, z_score=99.0, tick_radius=10, dwell_alert_sec=2.0
        )
        n_take = n_hold = n_comp = n_dual = 0
        dual_parts = []
        nq_parts = []
        take_parts = []
        for sid in sessions:
            nq_tr = _load_trades(parquet, sid, "NQ")
            es_tr = _load_trades(parquet, sid, "ES")
            nq_d = _load_depth(parquet, sid, "NQ")
            es_d = _load_depth(parquet, sid, "ES")
            if nq_tr.num_rows == 0 or nq_d.num_rows == 0:
                continue
            nq_rth = _filter_rth_uk_table(nq_tr)
            nq_levels = run_liquidity_tracker(nq_d, nq_tr, cfg=nq_cfg, rth_uk_only=True)
            es_levels = (
                run_liquidity_tracker(es_d, es_tr, cfg=es_cfg, rth_uk_only=True)
                if es_d.num_rows and es_tr.num_rows
                else []
            )
            nq_take = aggressive_take_events_from_levels(
                nq_levels,
                min_consume_ratio=sp["take"],
                min_penetration_ticks=sp["pen"],
                require_attracted=sp["attr"],
            )
            es_take = aggressive_take_events_from_levels(
                es_levels,
                min_consume_ratio=sp["take"],
                min_penetration_ticks=sp["pen"],
                require_attracted=sp["attr"],
            )
            nq_hold = hold_without_trade_events_from_levels(
                nq_levels, min_dwell_sec=2.0, min_initial_size=nq_cfg.absolute_size
            )
            es_hold = hold_without_trade_events_from_levels(
                es_levels, min_dwell_sec=2.0, min_initial_size=es_cfg.absolute_size
            )
            nq_comp = micro_regime_composite_events(nq_take, nq_hold, pair_ms=3000)
            es_comp = micro_regime_composite_events(es_take, es_hold, pair_ms=3000)
            dual = cross_confirm_liquidity_events(
                nq_comp, es_comp, sync_ms=2500, require_same_event_type=True
            )
            take_dual = cross_confirm_liquidity_events(
                nq_take, es_take, sync_ms=2500, require_same_event_type=True
            )
            n_take += nq_take.num_rows
            n_hold += nq_hold.num_rows
            n_comp += nq_comp.num_rows
            n_dual += dual.num_rows
            path_ts, path_px = build_price_path(nq_rth if nq_rth.num_rows else nq_tr)
            for parts, ev in (
                (dual_parts, dual),
                (nq_parts, nq_comp),
                (take_parts, take_dual),
            ):
                if ev.num_rows:
                    lab = label_events(ev, path_ts, path_px, horizons, targets)
                    if lab.num_rows:
                        parts.append(lab)
            print(
                f"  {sp['name']} {sid}: take={nq_take.num_rows} hold={nq_hold.num_rows} "
                f"comp={nq_comp.num_rows} dual={dual.num_rows}",
                flush=True,
            )

        def pack(parts):
            if not parts:
                return None
            t = pa.concat_tables(parts)
            return {
                "n": int(t.num_rows),
                "hit10": round(float(_mean(t.column("hit_10pts"))), 3),
                "ret10s": round(float(_mean(t.column("ret_pts_10000ms"))), 2),
            }

        row = {
            **sp,
            "nq_take": n_take,
            "nq_hold": n_hold,
            "nq_comp": n_comp,
            "dual_comp": n_dual,
            "dual_labeled": pack(dual_parts),
            "nq_labeled": pack(nq_parts),
            "take_dual_labeled": pack(take_parts),
        }
        out.append(row)
        print(json.dumps(row, indent=2), flush=True)

    Path("results/EXP-0021").mkdir(parents=True, exist_ok=True)
    Path("results/EXP-0021/dig_sensitivity.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8"
    )
    print("wrote results/EXP-0021/dig_sensitivity.json")


if __name__ == "__main__":
    main()
