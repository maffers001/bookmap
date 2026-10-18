"""Re-score saved events with wider stop/targets and longer path window.

Default research used hit±10 within 10s. NQ volatility may need 20/20–40/40
and a longer max horizon so the stop/target can actually fill.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.config import load_config
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
    _load_trades,
    _mean,
    _median,
    _random_null_events,
)

ROOT = Path(__file__).resolve().parents[1]
# Chart points (label.py scales parquet ticks × 0.25). Pre-fix TARGETS were ticks.
TARGETS = [10, 15, 20, 25, 30, 40]
HORIZONS = [250, 500, 1000, 2000, 5000, 10000, 20000, 30000, 60000]


def _pack(lab: pa.Table) -> dict:
    if lab.num_rows == 0:
        return {"n": 0}
    out: dict = {
        "n": int(lab.num_rows),
        "mean_mfe": round(float(_mean(lab.column("mfe_pts"))), 2),
        "mean_mae": round(float(_mean(lab.column("mae_pts"))), 2),
        "ret10s": round(float(_mean(lab.column("ret_pts_10000ms"))), 2),
        "ret30s": round(float(_mean(lab.column("ret_pts_30000ms"))), 2),
        "ret60s": round(float(_mean(lab.column("ret_pts_60000ms"))), 2),
        "med60s": round(float(_median(lab.column("ret_pts_60000ms"))), 2),
    }
    for t in TARGETS:
        hit = lab.column(f"hit_{t}pts").to_numpy().astype(float)
        stop = lab.column(f"stopped_before_{t}pts").to_numpy().astype(float)
        # expectancy proxy among resolved: hit rate of ±T race
        resolved = hit + stop
        out[f"hit{t}"] = round(float(hit.mean()), 3)
        out[f"stop{t}"] = round(float(stop.mean()), 3)
        out[f"resolve{t}"] = round(float(resolved.mean()), 3)
        # win rate among those that hit stop or target
        mask = resolved > 0
        if mask.any():
            out[f"wr{t}"] = round(float(hit[mask].mean()), 3)
        else:
            out[f"wr{t}"] = None
    return out


def relabel_events(ev: pa.Table, paths: dict[str, tuple]) -> pa.Table:
    """Relabel per session using session_id column."""
    if ev.num_rows == 0 or "session_id" not in ev.column_names:
        return pa.table({})
    parts = []
    sids = ev.column("session_id").to_pylist()
    for sid in sorted(set(sids)):
        idx = [i for i, s in enumerate(sids) if s == sid]
        if sid not in paths:
            continue
        path_ts, path_px = paths[sid]
        sub = ev.take(np.asarray(idx, dtype=np.int64))
        # keep only event_ts_ns + side for clean relabel; copy metadata after
        core = pa.table({
            "event_ts_ns": sub.column("event_ts_ns"),
            "side": sub.column("side"),
        })
        lab = label_events(core, path_ts, path_px, HORIZONS, TARGETS)
        if lab.num_rows == 0:
            continue
        lab = lab.append_column(
            "session_id", pa.array([sid] * lab.num_rows, type=pa.string())
        )
        parts.append(lab)
    if not parts:
        return pa.table({})
    return pa.concat_tables(parts)


def load_set(name: str, path: Path, *, lag250: bool = False, mask_col: str | None = None, mask_val=1) -> pa.Table:
    if not path.exists():
        return pa.table({})
    t = pq.read_table(path)
    if lag250 and "es_lag_ms" in t.column_names:
        lag = np.abs(t.column("es_lag_ms").to_numpy())
        t = t.take(np.where(lag <= 250)[0])
    if mask_col and mask_col in t.column_names:
        m = t.column(mask_col).to_numpy() == mask_val
        t = t.take(np.where(m)[0])
    return t


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])
    paths = {}
    null_parts = []
    for sid in sessions:
        nq = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
        if nq.num_rows == 0:
            continue
        paths[sid] = build_price_path(nq)
        null_ev = _random_null_events(nq, 50, seed=hash("wide" + sid) % (2**31))
        lab = label_events(null_ev, paths[sid][0], paths[sid][1], HORIZONS, TARGETS)
        if lab.num_rows:
            null_parts.append(
                lab.append_column("session_id", pa.array([sid] * lab.num_rows, type=pa.string()))
            )
        print(f"loaded path {sid}", flush=True)

    sets = {
        "null": pa.concat_tables(null_parts) if null_parts else pa.table({}),
        "exp0008_all": load_set("0008", ROOT / "results/EXP-0008/events.parquet"),
        "exp0008_lag250": load_set("0008", ROOT / "results/EXP-0008/events.parquet", lag250=True),
        "exp0024_lag250": load_set("0024", ROOT / "results/EXP-0024/events.parquet"),
        "exp0025_swing_break": load_set(
            "0025", ROOT / "results/EXP-0025/events.parquet", mask_col="swing_break", mask_val=1
        ),
        "exp0025_all_lag250": load_set("0025", ROOT / "results/EXP-0025/events.parquet"),
        "exp0028_catchup": load_set("0028", ROOT / "results/EXP-0028/events.parquet"),
        "exp0029_comove": load_set("0029", ROOT / "results/EXP-0029/events.parquet"),
        "exp0011_lvn": load_set("0011", ROOT / "results/EXP-0011/events.parquet"),
        "exp0005_absorb": load_set("0005", ROOT / "results/EXP-0005/events.parquet"),
        "exp0020_imbalance": load_set("0020", ROOT / "results/EXP-0020/events.parquet"),
        "exp0017_sweep": load_set("0017", ROOT / "results/EXP-0017/events.parquet"),
        "exp0019_value": load_set("0019", ROOT / "results/EXP-0019/events.parquet"),
    }

    report = {
        "targets": TARGETS,
        "horizons_ms": HORIZONS,
        "note": "Symmetric stop=target; path window max 60s so wider targets can resolve.",
        "sets": {},
    }
    for name, ev in sets.items():
        print(f"relabel {name} n={ev.num_rows} …", flush=True)
        if name == "null":
            # already labeled with wide targets
            lab = ev
        else:
            # strip old outcome cols by rebuilding from ts/side/session
            if ev.num_rows == 0:
                report["sets"][name] = {"n": 0}
                continue
            keep = ["event_ts_ns", "side", "session_id"]
            cols = {c: ev.column(c) for c in keep if c in ev.column_names}
            if "session_id" not in cols:
                report["sets"][name] = {"n": int(ev.num_rows), "error": "no session_id"}
                continue
            lab = relabel_events(pa.table(cols), paths)
        report["sets"][name] = _pack(lab)
        print(json.dumps(report["sets"][name], indent=2), flush=True)

    out = ROOT / "results/WIDE_TARGETS"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Compact comparison table for hit10/20/30/40 and wr
    compact = []
    for name, s in report["sets"].items():
        if not s or s.get("n", 0) == 0:
            continue
        compact.append({
            "set": name,
            "n": s["n"],
            "hit10": s.get("hit10"),
            "wr10": s.get("wr10"),
            "hit20": s.get("hit20"),
            "wr20": s.get("wr20"),
            "hit30": s.get("hit30"),
            "wr30": s.get("wr30"),
            "hit40": s.get("hit40"),
            "wr40": s.get("wr40"),
            "ret60s": s.get("ret60s"),
            "med60s": s.get("med60s"),
            "mfe": s.get("mean_mfe"),
            "mae": s.get("mean_mae"),
        })
    (out / "compact.json").write_text(json.dumps(compact, indent=2), encoding="utf-8")
    print("=== compact ===")
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
