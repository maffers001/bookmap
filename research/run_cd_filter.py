"""
H6 — Apply CD trend + pullback-to-MA gate to existing event sets.

  python -m research.run_cd_filter
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import yaml

from research.config import load_config
from research.features.cd import build_cd_series, cd_gate_mask, lookup_cd_at_events


def _stats(d: dict, mask: np.ndarray) -> dict:
    n = int(mask.sum())
    if n == 0:
        return {"n": 0}
    def m(col):
        return float(np.asarray(d[col])[mask].mean())
    out = {
        "n": n,
        "hit10": m("hit_10pts") if "hit_10pts" in d else None,
        "hit20": m("hit_20pts") if "hit_20pts" in d else None,
        "mfe": m("mfe_pts"),
        "mae": m("mae_pts"),
        "r1s": m("ret_pts_1000ms") if "ret_pts_1000ms" in d else None,
        "r2s": m("ret_pts_2000ms") if "ret_pts_2000ms" in d else None,
        "r10s": m("ret_pts_10000ms") if "ret_pts_10000ms" in d else None,
        "med_r10s": float(np.median(np.asarray(d["ret_pts_10000ms"])[mask])) if "ret_pts_10000ms" in d else None,
    }
    return out


def annotate_events(parquet_dir: Path, events_path: Path, tick_size: float = 1.0) -> dict:
    ev = pq.read_table(events_path)
    d = ev.to_pydict()
    n = len(d["event_ts_ns"])
    side = np.asarray(d["side"])
    sessions = np.asarray(d["session_id"])

    # per-session CD lookup
    fields = {
        "cd_ma_trend": np.full(n, np.nan),
        "cd_pullback_long": np.full(n, np.nan),
        "cd_pullback_short": np.full(n, np.nan),
        "cd_dist_to_ma": np.full(n, np.nan),
        "cd_near_ma": np.full(n, np.nan),
        "cd_ma_slope": np.full(n, np.nan),
    }
    for sid in sorted(set(sessions.tolist())):
        idx = np.where(sessions == sid)[0]
        trades_path = parquet_dir / f"session_id={sid}" / "trades.parquet"
        if not trades_path.exists():
            continue
        trades = pq.read_table(trades_path, filters=[("role", "=", "NQ")])
        cd = build_cd_series(trades, tick_size=tick_size)
        got = lookup_cd_at_events(cd, np.asarray(d["event_ts_ns"])[idx].astype(np.int64))
        for k in fields:
            if k in got:
                fields[k][idx] = got[k]

    all_mask = np.ones(n, dtype=bool)
    trend_mask = cd_gate_mask(side, fields, mode="trend_only")
    pb_mask = cd_gate_mask(side, fields, mode="trend_and_pullback")
    # against-flow for contrast
    against = (~trend_mask) & np.isfinite(fields["cd_ma_trend"]) & (fields["cd_ma_trend"] != 0)

    return {
        "source_events": str(events_path),
        "n_total": n,
        "all": _stats(d, all_mask),
        "cd_trend_aligned": _stats(d, trend_mask),
        "cd_trend_and_pullback": _stats(d, pb_mask),
        "cd_against_trend": _stats(d, against),
        "frac_trend_aligned": float(trend_mask.mean()),
        "frac_pullback": float(pb_mask.mean()),
    }


def main() -> int:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"])
    # Price column is in tick units for these extracts (equals price_ticks).
    tick = 1.0

    targets = [
        ("EXP-0005", results_dir / "EXP-0005" / "events.parquet"),
        ("EXP-0002b", results_dir / "EXP-0002b" / "events.parquet"),
        ("EXP-0002", results_dir / "EXP-0002" / "events.parquet"),
    ]

    report = {"experiment": "EXP-0009", "hypothesis": "H6", "filters": {}}
    for name, path in targets:
        if not path.exists():
            report["filters"][name] = {"status": "missing", "path": str(path)}
            continue
        print(f"CD-filter {name} …", flush=True)
        report["filters"][name] = annotate_events(parquet_dir, path, tick_size=tick)
        print(json.dumps(report["filters"][name], indent=2), flush=True)

    out = results_dir / "EXP-0009"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("wrote", out / "report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
