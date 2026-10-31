"""Phase 4 — count-first paired lifecycle + iceberg proxy on frozen triggers.

Annotates MAT (and optionally CFADE) stamps with:
  - lc_paired: same-side ES lifecycle event within ±2 s
  - iceberg hold-side proxy at stamp

No new entry. Preregistration earned only if n≥50 triggers with the
feature and descriptive δ≥0.2 (reported; CI90 left for follow-up).

Usage:
  python -m research.mbo_lifecycle.run_paired_iceberg --max-sessions 15
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.mat_sim import detect_signals
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.mbo_lifecycle import (
    LifecycleConfig,
    iceberg_proxy_at_price,
    paired_lifecycle,
    run_lifecycle,
)
from research.irt_lvn.run_hold_break_of import load_mbo_window
from research.run_cfade_all_months import _sid_chrono_key

NS_MIN = 60 * 1_000_000_000
HORIZONS = [10_000, 60_000, 300_000]
TARGETS = [5.0, 10.0, 20.0]


def run(*, max_sessions: int | None = None) -> dict:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "MBO_PAIRED_ICEBERG"
    out.mkdir(parents=True, exist_ok=True)
    lcfg = LifecycleConfig(rth_uk_only=True)

    sessions = F.clean_sessions(pq_dir)
    chrono = sorted(sessions, key=_sid_chrono_key)
    cov = F.depth_mbo_coverage(pq_dir, chrono)
    mbo_sids = set(cov.get("mbo_sessions") or [])
    discovery = [s for s in chrono if s in mbo_sids and F.is_is(s)]
    if max_sessions:
        discovery = discovery[:max_sessions]
    print(f"[paired] {len(discovery)} sessions", flush=True)

    rows = []
    for sid in discovery:
        pair = H.load_pair(pq_dir, sid)
        if pair is None:
            continue
        lo, hi = pair.bounds
        mbo_nq = load_mbo_window(pq_dir, sid, "NQ", (lo - 5 * NS_MIN, hi))
        mbo_es = load_mbo_window(pq_dir, sid, "ES", (lo - 5 * NS_MIN, hi))
        if mbo_nq.num_rows == 0:
            continue
        lc_nq = run_lifecycle(mbo_nq, pair.nq, cfg=lcfg, data_start_ns=lo - 5 * NS_MIN)
        lc_es = (
            run_lifecycle(mbo_es, pair.es, cfg=lcfg, data_start_ns=lo - 5 * NS_MIN)
            if mbo_es.num_rows and pair.es.num_rows
            else pa.table({})
        )
        lc_nq = paired_lifecycle(lc_nq, lc_es, sync_ms=2000) if lc_nq.num_rows else lc_nq

        mat = detect_signals(pair.nq, pair.es, F.mat_sim_cfg())
        if mat.num_rows == 0:
            continue
        # label MAT for descriptive δ
        lab = H.label_in_points(mat, pair, HORIZONS, TARGETS)
        ret60 = lab.column("ret_pts_60000ms").to_numpy().astype(np.float64) if lab.num_rows else np.array([])
        mat_ts = mat.column("event_ts_ns").to_numpy().astype(np.int64)
        mat_side = mat.column("side").to_numpy().astype(int)

        nq_ts = lc_nq.column("event_ts_ns").to_numpy().astype(np.int64) if lc_nq.num_rows else np.zeros(0, dtype=np.int64)
        nq_side = lc_nq.column("level_side").to_pylist() if lc_nq.num_rows else []
        nq_paired = lc_nq.column("lc_paired").to_numpy().astype(np.int8) if lc_nq.num_rows and "lc_paired" in lc_nq.column_names else np.zeros(0, dtype=np.int8)
        nq_state = lc_nq.column("state").to_pylist() if lc_nq.num_rows else []
        nq_px = lc_nq.column("price_ticks").to_numpy().astype(np.int64) if lc_nq.num_rows else np.zeros(0, dtype=np.int64)

        paired_rets = []
        unpaired_rets = []
        ice_rets = []
        no_ice_rets = []
        n_paired = n_ice = 0

        for i in range(len(mat_ts)):
            # nearest NQ lifecycle within ±2s same side relative to trade
            # long MAT → ASK consumed / BID defended interesting; use any same clock
            win = 2_000_000_000
            lo_i = int(np.searchsorted(nq_ts, mat_ts[i] - win))
            hi_i = int(np.searchsorted(nq_ts, mat_ts[i] + win))
            is_paired = False
            is_ice = False
            for j in range(lo_i, hi_i):
                if nq_paired[j] == 1:
                    is_paired = True
                # hold-side iceberg: BID for long, ASK for short
                hold = "BID" if mat_side[i] > 0 else "ASK"
                ice = iceberg_proxy_at_price(
                    lc_nq, price_ticks=int(nq_px[j]), level_side=hold, stamp_ns=int(mat_ts[i])
                )
                if ice["iceberg"] >= 1.0:
                    is_ice = True
            if i < len(ret60) and np.isfinite(ret60[i]):
                if is_paired:
                    paired_rets.append(ret60[i]); n_paired += 1
                else:
                    unpaired_rets.append(ret60[i])
                if is_ice:
                    ice_rets.append(ret60[i]); n_ice += 1
                else:
                    no_ice_rets.append(ret60[i])

        def _mean(xs):
            return float(np.mean(xs)) if xs else float("nan")

        row = {
            "session": sid,
            "n_mat": int(len(mat_ts)),
            "n_lc_nq": int(lc_nq.num_rows),
            "n_lc_es": int(lc_es.num_rows),
            "n_paired_triggers": n_paired,
            "n_iceberg_triggers": n_ice,
            "mean_ret60_paired": _mean(paired_rets),
            "mean_ret60_unpaired": _mean(unpaired_rets),
            "mean_ret60_ice": _mean(ice_rets),
            "mean_ret60_no_ice": _mean(no_ice_rets),
        }
        rows.append(row)
        print(
            f"  {sid}: mat={row['n_mat']} paired={n_paired} ice={n_ice} "
            f"δ_pair={row['mean_ret60_paired'] - row['mean_ret60_unpaired'] if np.isfinite(row['mean_ret60_paired']) and np.isfinite(row['mean_ret60_unpaired']) else float('nan'):.2f}",
            flush=True,
        )

    # pool
    n_paired = sum(r["n_paired_triggers"] for r in rows)
    n_ice = sum(r["n_iceberg_triggers"] for r in rows)
    # weighted means
    def pool_delta(key_a, key_b, n_key):
        num = den = 0.0
        for r in rows:
            a, b, n = r[key_a], r[key_b], r[n_key]
            if n > 0 and np.isfinite(a) and np.isfinite(b):
                num += (a - b) * n
                den += n
        return float(num / den) if den else float("nan")

    d_pair = pool_delta("mean_ret60_paired", "mean_ret60_unpaired", "n_paired_triggers")
    d_ice = pool_delta("mean_ret60_ice", "mean_ret60_no_ice", "n_iceberg_triggers")

    paired_ok = n_paired >= 50 and np.isfinite(d_pair) and abs(d_pair) >= 0.2
    ice_ok = n_ice >= 50 and np.isfinite(d_ice) and abs(d_ice) >= 0.2

    report = {
        "n_sessions": len(rows),
        "n_paired_triggers": n_paired,
        "n_iceberg_triggers": n_ice,
        "delta_paired_minus_unpaired_60s": d_pair,
        "delta_ice_minus_no_ice_60s": d_ice,
        "paired_earns_prereg": paired_ok,
        "iceberg_earns_prereg": ice_ok,
        "sessions": rows,
        "verdict": (
            f"paired={'earn' if paired_ok else 'no'}; iceberg={'earn' if ice_ok else 'no'} "
            f"(need n≥50 and |δ|≥0.2)"
        ),
    }
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    md = [
        "# MBO paired lifecycle + iceberg counts (Phase 4)",
        "",
        f"Sessions: {len(rows)} · paired triggers: **{n_paired}** · iceberg: **{n_ice}**",
        "",
        f"δ @60s paired−unpaired: **{d_pair:.3f}** → earn prereg: **{paired_ok}**",
        f"δ @60s ice−no_ice: **{d_ice:.3f}** → earn prereg: **{ice_ok}**",
        "",
        f"**Verdict: {report['verdict']}**",
        "",
    ]
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(report["verdict"], flush=True)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sessions", type=int, default=None)
    args = ap.parse_args()
    run(max_sessions=args.max_sessions)


if __name__ == "__main__":
    main()
