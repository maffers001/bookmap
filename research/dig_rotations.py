"""Sensitivity dig for EXP-0030 rotation filter: avg_n × mult × swing lookback."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.features.liquidity import (
    LargeLevelConfig,
    absorption_events_from_levels,
    run_liquidity_tracker,
)
from research.features.momentum import (
    cross_confirm_bursts,
    momentum_burst_events,
    trades_to_tape_bars,
)
from research.features.rotations import (
    RotationConfig,
    annotate_rotation_state,
    momentum_rotation_ok,
    reversal_rotation_ok,
)
from research.features.swings import SwingConfig
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
    _load_depth,
    _load_trades,
    _mean,
    _median,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "EXP-0030" / "dig.json"


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


def run_cfg(
    sessions: list[str],
    parquet: Path,
    *,
    avg_n: int,
    mult: float,
    lb_ms: int,
) -> dict:
    rot_cfg = RotationConfig(
        swing=SwingConfig(lookback_ms=lb_ms, confirm_ms=20_000, tick_size=1.0),
        avg_n=avg_n,
        extend_mult=mult,
        min_rotations=3,
    )
    burst_kw = dict(
        min_trades=6,
        min_vol=25.0,
        min_imbalance=0.55,
        min_progress_ticks=1.0,
        tick_size=1.0,
        cooldown_bars=3,
    )
    ll_cfg = LargeLevelConfig(absolute_size=50, z_score=99.0, tick_radius=8)
    horizons = [250, 500, 1000, 2000, 5000, 10000]
    targets = [10, 20]
    mom_parts = []
    abs_parts = []
    for sid in sessions:
        nq = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
        es = _filter_rth_uk_table(_load_trades(parquet, sid, "ES"))
        depth = _load_depth(parquet, sid, "NQ")
        if nq.num_rows == 0:
            continue
        events = cross_confirm_bursts(
            momentum_burst_events(trades_to_tape_bars(nq, 100), **burst_kw),
            momentum_burst_events(trades_to_tape_bars(es, 100), **burst_kw)
            if es.num_rows
            else pa.table({}),
            sync_ms=1500,
        )
        if events.num_rows and "es_lag_ms" in events.column_names:
            events = events.take(
                np.where(np.abs(events.column("es_lag_ms").to_numpy()) <= 250)[0]
            )
        events = annotate_rotation_state(events, nq, rot_cfg)
        path_ts, path_px = build_price_path(nq)
        lab = label_events(events, path_ts, path_px, horizons, targets)
        if lab.num_rows:
            mom_parts.append(lab)
        if depth.num_rows:
            levels = run_liquidity_tracker(depth, nq, cfg=ll_cfg, rth_uk_only=False)
            abs_ev = annotate_rotation_state(
                absorption_events_from_levels(
                    levels, min_consume_ratio=1.0, tick_radius=8
                ),
                nq,
                rot_cfg,
            )
            lab_a = label_events(abs_ev, path_ts, path_px, horizons, targets)
            if lab_a.num_rows:
                abs_parts.append(lab_a)

    mom = pa.concat_tables(mom_parts) if mom_parts else pa.table({})
    abs_df = pa.concat_tables(abs_parts) if abs_parts else pa.table({})
    row: dict = {
        "avg_n": avg_n,
        "mult": mult,
        "swing_lb_min": lb_ms // 60_000,
    }
    if mom.num_rows:
        ready = mom.column("rot_ready").to_numpy() == 1
        ok = momentum_rotation_ok(mom) & ready
        ext = ready & (mom.column("rot_extended").to_numpy() == 1)
        row["mom_all"] = pack(mom)
        row["mom_fresh"] = pack(mom.take(np.where(ok)[0])) if ok.any() else None
        row["mom_extended"] = pack(mom.take(np.where(ext)[0])) if ext.any() else None
        row["pct_ready"] = round(float(ready.mean()), 3)
        row["pct_ok"] = round(float(ok.mean()), 3)
    if abs_df.num_rows:
        aok = reversal_rotation_ok(abs_df)
        row["abs_all"] = pack(abs_df)
        row["abs_extended"] = pack(abs_df.take(np.where(aok)[0])) if aok.any() else None
    return row


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])
    results = []
    for lb_min in (2, 3, 5):
        for avg_n in (5, 8, 12):
            for mult in (0.75, 1.0, 1.25):
                print(f"dig lb={lb_min}m avg_n={avg_n} mult={mult} …", flush=True)
                row = run_cfg(
                    sessions,
                    parquet,
                    avg_n=avg_n,
                    mult=mult,
                    lb_ms=lb_min * 60_000,
                )
                results.append(row)
                mf = row.get("mom_fresh") or {}
                ma = row.get("mom_all") or {}
                print(
                    f"  mom_all hit={ma.get('hit10')} n={ma.get('n')} | "
                    f"fresh hit={mf.get('hit10')} n={mf.get('n')} pct_ok={row.get('pct_ok')}",
                    flush=True,
                )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
