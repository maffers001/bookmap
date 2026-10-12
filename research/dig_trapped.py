"""Dig EXP-0031b: vol_mult × pullback window × LVN frac × quiet-vs-peak."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.features.trapped import TrappedConfig, trapped_events
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
    _load_trades,
    _mean,
    _median,
)

OUT = Path(__file__).resolve().parents[1] / "results" / "EXP-0031" / "dig_b.json"


def pack(t: pa.Table) -> dict | None:
    if t.num_rows == 0:
        return None
    shorts = t.column("side").to_numpy() == -1
    out = {
        "n": int(t.num_rows),
        "n_short": int(shorts.sum()),
        "hit10": round(float(_mean(t.column("hit_10pts"))), 3),
        "ret10s": round(float(_mean(t.column("ret_pts_10000ms"))), 2),
        "med10s": round(float(_median(t.column("ret_pts_10000ms"))), 2),
        "mae": round(float(_mean(t.column("mae_pts"))), 2),
    }
    if shorts.any():
        s = t.take(np.where(shorts)[0])
        out["short_hit10"] = round(float(_mean(s.column("hit_10pts"))), 3)
        out["short_ret10s"] = round(float(_mean(s.column("ret_pts_10000ms"))), 2)
    if "quiet_ms" in t.column_names:
        out["mean_quiet_ms"] = round(float(_mean(t.column("quiet_ms"))), 1)
    if "vol_mult" in t.column_names:
        out["mean_vol_mult"] = round(float(_mean(t.column("vol_mult"))), 2)
    return out


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])
    horizons = [250, 500, 1000, 2000, 5000, 10000]
    targets = [10, 20]
    grid = [
        # defaults + neighbors
        dict(min_vol_mult=2.0, pullback_max_ms=30_000, lvn_frac_of_shoulder=0.55, max_pullback_vs_peak=0.45),
        dict(min_vol_mult=1.5, pullback_max_ms=30_000, lvn_frac_of_shoulder=0.55, max_pullback_vs_peak=0.45),
        dict(min_vol_mult=2.5, pullback_max_ms=30_000, lvn_frac_of_shoulder=0.55, max_pullback_vs_peak=0.45),
        dict(min_vol_mult=2.0, pullback_max_ms=20_000, lvn_frac_of_shoulder=0.55, max_pullback_vs_peak=0.45),
        dict(min_vol_mult=2.0, pullback_max_ms=45_000, lvn_frac_of_shoulder=0.55, max_pullback_vs_peak=0.45),
        dict(min_vol_mult=2.0, pullback_max_ms=30_000, lvn_frac_of_shoulder=0.70, max_pullback_vs_peak=0.45),
        dict(min_vol_mult=2.0, pullback_max_ms=30_000, lvn_frac_of_shoulder=0.40, max_pullback_vs_peak=0.45),
        dict(min_vol_mult=2.0, pullback_max_ms=30_000, lvn_frac_of_shoulder=0.55, max_pullback_vs_peak=0.60),
        dict(min_vol_mult=2.0, pullback_max_ms=30_000, lvn_frac_of_shoulder=0.55, max_pullback_vs_peak=0.30),
        dict(min_vol_mult=1.5, pullback_max_ms=45_000, lvn_frac_of_shoulder=0.70, max_pullback_vs_peak=0.60),
        dict(min_vol_mult=1.5, pullback_max_ms=30_000, lvn_frac_of_shoulder=0.70, max_pullback_vs_peak=0.60, lvn_band_ticks=40),
        dict(min_vol_mult=1.5, pullback_max_ms=30_000, lvn_frac_of_shoulder=0.70, max_pullback_vs_peak=0.60, lvn_band_ticks=60),
    ]
    results = []
    for g in grid:
        label = (
            f"vm{g['min_vol_mult']}_pb{g['pullback_max_ms']//1000}_"
            f"lf{g['lvn_frac_of_shoulder']}_pv{g['max_pullback_vs_peak']}_"
            f"b{g.get('lvn_band_ticks', 50)}"
        )
        print(f"dig {label} …", flush=True)
        tcfg = TrappedConfig(**g)
        parts = []
        for sid in sessions:
            nq = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
            if nq.num_rows == 0:
                continue
            path_ts, path_px = build_price_path(nq)
            lab = label_events(
                trapped_events(nq, tcfg), path_ts, path_px, horizons, targets
            )
            if lab.num_rows:
                parts.append(lab)
        row = {"label": label, **g, "result": pack(pa.concat_tables(parts)) if parts else None}
        results.append(row)
        r = row["result"] or {}
        print(
            f"  n={r.get('n')} short_hit={r.get('short_hit10')} "
            f"ret={r.get('short_ret10s')} quiet={r.get('mean_quiet_ms')}",
            flush=True,
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
