"""Dig BB-regime interactions: flipped challenge continuation + LVN×narrow."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.config import load_config
from research.features.cd import build_cd_series, lookup_regime_at
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _load_trades,
    _mean,
    _median,
)

ROOT = Path(__file__).resolve().parents[1]


def _stats(t: pa.Table) -> dict:
    if t is None or t.num_rows == 0:
        return {"n": 0}
    return {
        "n": int(t.num_rows),
        "hit10": _mean(t.column("hit_10pts")),
        "hit20": _mean(t.column("hit_20pts")) if "hit_20pts" in t.column_names else None,
        "r2s": _mean(t.column("ret_pts_2000ms")),
        "r10s": _mean(t.column("ret_pts_10000ms")),
        "med_r10s": _median(t.column("ret_pts_10000ms")),
    }


def main() -> None:
    cfg = load_config()
    horizons = [250, 500, 1000, 2000, 5000, 10000]
    targets = [10, 20]
    report: dict = {}

    # --- Flip EXP-0016 dual events to continuation ---
    p16 = ROOT / "results/EXP-0016/events.parquet"
    if p16.exists():
        d = pq.read_table(p16)
        # Rebuild labels with flipped side
        parts = []
        for sid in sorted({x.as_py() for x in d.column("session_id")}):
            idx = np.where(np.array(d.column("session_id").to_pylist()) == sid)[0]
            sub = d.take(idx)
            nq = _filter_rth_uk_table(_load_trades(Path(cfg["parquet_dir"]), sid, "NQ"))
            # flip side
            sides = (-sub.column("side").to_numpy().astype(np.int8)).astype(np.int8)
            ev = pa.table({
                "event_ts_ns": sub.column("event_ts_ns"),
                "side": sides,
                "event_type": ["challenge_cont"] * sub.num_rows,
            })
            # keep regime cols
            path_ts, path_px = build_price_path(nq)
            lab = label_events(ev, path_ts, path_px, horizons, targets)
            if lab.num_rows == 0:
                continue
            for col in ("cd_bb_regime", "cd_bb_width_pct", "near_lvn", "session_id"):
                if col in sub.column_names:
                    # align by event_ts
                    src_ts = sub.column("event_ts_ns").to_numpy()
                    lab_ts = lab.column("event_ts_ns").to_numpy()
                    # same order expected
                    if len(src_ts) == len(lab_ts):
                        if col in lab.column_names:
                            lab = lab.drop(col)
                        lab = lab.append_column(col, sub.column(col))
            parts.append(lab)
        if parts:
            flipped = pa.concat_tables(parts)
            reg = flipped.column("cd_bb_regime").to_numpy()
            report["challenge_as_continuation"] = {
                "all": _stats(flipped),
                "wide": _stats(flipped.take(np.where(reg == 1)[0])) if (reg == 1).any() else {"n": 0},
                "mid": _stats(flipped.take(np.where(reg == 0)[0])) if (reg == 0).any() else {"n": 0},
            }

    # --- EXP-0011 dual LVN × BB regime ---
    p11 = ROOT / "results/EXP-0011/events.parquet"
    if p11.exists():
        d = pq.read_table(p11)
        parts = []
        for sid in sorted({x.as_py() for x in d.column("session_id")}):
            idx = np.where(np.array(d.column("session_id").to_pylist()) == sid)[0]
            sub = d.take(idx)
            nq = _filter_rth_uk_table(_load_trades(Path(cfg["parquet_dir"]), sid, "NQ"))
            cd = build_cd_series(nq, range_ticks=22, tick_size=1.0)
            w, p, r = lookup_regime_at(cd, sub.column("event_ts_ns").to_numpy())
            out = sub
            for name, arr, typ in (
                ("cd_bb_width", w, pa.float64()),
                ("cd_bb_width_pct", p, pa.float64()),
                ("cd_bb_regime", r, pa.int8()),
            ):
                if name in out.column_names:
                    out = out.drop(name)
                out = out.append_column(name, pa.array(arr, type=typ))
            parts.append(out)
        d11 = pa.concat_tables(parts)
        reg = d11.column("cd_bb_regime").to_numpy()
        report["dual_lvn_by_regime"] = {
            "all": _stats(d11),
            "wide": _stats(d11.take(np.where(reg == 1)[0])) if (reg == 1).any() else {"n": 0},
            "narrow": _stats(d11.take(np.where(reg == -1)[0])) if (reg == -1).any() else {"n": 0},
            "mid": _stats(d11.take(np.where(reg == 0)[0])) if (reg == 0).any() else {"n": 0},
        }

    # --- EXP-0015 path note already in report; summarize session stability ---
    p15 = ROOT / "results/EXP-0015/report.json"
    if p15.exists():
        report["exp0015_strata"] = json.loads(p15.read_text(encoding="utf-8")).get("strata")

    out = ROOT / "results/EXP-0015/bb_regime_dig.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("Wrote", out)


if __name__ == "__main__":
    main()
