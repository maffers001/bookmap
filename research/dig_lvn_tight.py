"""One-pass dig: lookback × lvn_frac for tightened EXP-0023 LVNs."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.events.lvn import TouchConfig, dual_lvn_touch_events
from research.features.volume_profile import VolumeProfileConfig
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
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
        "hit20": round(float(_mean(t.column("hit_20pts"))), 3),
        "ret10s": round(float(_mean(t.column("ret_pts_10000ms"))), 2),
        "med10s": round(float(_median(t.column("ret_pts_10000ms"))), 2),
    }


def run_spec(
    lookback_ms: int,
    frac: float,
    sessions: list[str],
    parquet: Path,
    *,
    min_vol: float = 500.0,
) -> dict:
    vp = VolumeProfileConfig(
        lookback_ms=lookback_ms,
        refresh_ms=30_000,
        lvn_frac_of_peak=frac,
        min_profile_volume=min_vol,
        require_hvn_sandwich=True,
        hvn_frac_of_poc=0.50,
        hvn_search_ticks=20,
        edge_margin_ticks=6,
    )
    touch = TouchConfig(sync_ms=2000, touch_cooldown_ms=5000)
    horizons = [250, 500, 1000, 2000, 5000, 10000]
    targets = [10, 20]
    dual_parts: list[pa.Table] = []
    nq_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    zone_stats = []
    for sid in sessions:
        nq = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
        es = _filter_rth_uk_table(_load_trades(parquet, sid, "ES"))
        if nq.num_rows == 0:
            continue
        dual, nq_only, meta = dual_lvn_touch_events(nq, es, vp, touch)
        zone_stats.append({
            "session": sid,
            "nq_zones_last": meta.get("nq_zones_last"),
            "nq_touches": meta.get("nq_touches"),
            "dual": int(dual.num_rows),
        })
        path_ts, path_px = build_price_path(nq)
        for parts, ev in (
            (dual_parts, dual),
            (nq_parts, nq_only),
        ):
            if ev.num_rows:
                lab = label_events(ev, path_ts, path_px, horizons, targets)
                if lab.num_rows:
                    parts.append(lab)
        n_null = max(int(dual.num_rows), 50)
        null_ev = _random_null_events(nq, n_null, seed=hash(f"lvn{sid}{lookback_ms}{frac}") % (2**31))
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)
        if lab_null.num_rows:
            null_parts.append(lab_null)
        print(
            f"  lb={lookback_ms // 60000}m frac={frac} {sid}: "
            f"dual={dual.num_rows} nq={nq_only.num_rows} zones={meta.get('nq_zones_last')}",
            flush=True,
        )
    return {
        "lookback_min": lookback_ms // 60_000,
        "lvn_frac": frac,
        "min_profile_volume": min_vol,
        "dual": pack(dual_parts),
        "nq_only": pack(nq_parts),
        "null": pack(null_parts),
        "zone_stats": zone_stats,
    }


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])
    specs = []
    for lb_min in (20, 30, 45):
        for frac in (0.25, 0.35, 0.40):
            print(f"=== lookback={lb_min}m frac={frac} ===", flush=True)
            specs.append(run_spec(lb_min * 60_000, frac, sessions, parquet))
    # Also try stricter / looser min volume at default 30m / 0.35
    print("=== min_vol variants @ 30m / 0.35 ===", flush=True)
    for mv in (200.0, 500.0, 1500.0):
        specs.append(run_spec(1_800_000, 0.35, sessions, parquet, min_vol=mv))

    out_dir = Path("results/EXP-0023")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dig_sensitivity.json").write_text(json.dumps(specs, indent=2), encoding="utf-8")
    # Compact table
    compact = []
    for s in specs:
        d = s.get("dual") or {"n": 0}
        compact.append({
            "lookback_min": s["lookback_min"],
            "frac": s["lvn_frac"],
            "min_vol": s["min_profile_volume"],
            "dual_n": d.get("n", 0),
            "dual_hit10": d.get("hit10"),
            "dual_ret10s": d.get("ret10s"),
            "nq_n": (s.get("nq_only") or {}).get("n"),
            "nq_hit10": (s.get("nq_only") or {}).get("hit10"),
            "null_hit10": (s.get("null") or {}).get("hit10"),
        })
    (out_dir / "dig_compact.json").write_text(json.dumps(compact, indent=2), encoding="utf-8")
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
