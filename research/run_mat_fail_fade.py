"""EXP-0048 / H39 — MAT fail-fade dig (arms A + B).

Frozen MAT stamps (causal, trend+VWAP) seed opposite entries when the push
fails to break mat_px (A) or after a held retest/double (B).

Discovery on train; May / Aug / Apr reported as OOS (no retune on April).
Writes results/EXP-0048/.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from itertools import product
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    combine_gates,
    detect_signals,
    make_trend_mode,
    make_vol_bracket,
    make_vwap_side,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.correlated.harness import _prefer_single_feed
from research.features.mat_fail_fade import build_fail_fade_events, filter_gated_mat
from research.run_experiment import _load_trades
from research.run_mat_april import APR
from research.run_mat_august import AUG
from research.run_mat_may import MAY, day_equity_stats

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25

# Primary freeze cell (plan)
PRIMARY = {"arm": "A", "fail_sec": 20.0, "beyond_pts": 0.0, "bracket": "fixed_10_10"}


def bounds(mid: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(mid / 1e9, tz=timezone.utc).astimezone(UK).date()
    return (
        int(datetime.combine(d, time(14, 30), tzinfo=UK).timestamp() * 1e9),
        int(datetime.combine(d, time(17, 0), tzinfo=UK).timestamp() * 1e9),
    )


def filt(t: pa.Table) -> pa.Table:
    if t.num_rows == 0:
        return t
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    s, e = bounds(int(ts[len(ts) // 2]))
    k = (ts >= s) & (ts <= e)
    return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)


def cohort(sid: str) -> str:
    if sid in MAY:
        return "may"
    if sid in AUG:
        return "aug"
    if sid in APR:
        return "apr"
    return "train"


def bracket_spec(name: str):
    if name == "fixed_10_10":
        return MatSimConfig(
            slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal",
            tp_pts=10.0, sl_pts=10.0,
        ), None
    if name == "fixed_30_10":
        return MatSimConfig(
            slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal",
            tp_pts=30.0, sl_pts=10.0,
        ), None
    if name == "vol_k1":
        return MatSimConfig(
            slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal",
        ), make_vol_bracket(1.0)
    raise ValueError(name)


def cell_key(arm: str, fail_sec: float, beyond: float, bracket: str, retest_tol: float | None) -> str:
    if arm == "A":
        return f"A_T{int(fail_sec)}_X{beyond:g}_{bracket}"
    return f"B_T{int(fail_sec)}_X{beyond:g}_tol{retest_tol:g}_{bracket}"


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0048"
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name.replace("session_id=", "")
        for p in pq.iterdir()
        if p.is_dir()
        and p.name.startswith("session_id=")
        and (p / "trades.parquet").exists()
        and "smoke" not in p.name
    )

    mat_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    base_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")

    fail_secs = (10.0, 20.0, 30.0)
    beyonds = (0.0, 1.0)
    brackets = ("fixed_10_10", "fixed_30_10", "vol_k1")
    retest_tols = (1.0, 2.0)

    # Preload per-session NQ/ES + gated MAT events
    session_data: dict[str, dict] = {}
    for sid in sessions:
        nq = filt(_prefer_single_feed(_load_trades(pq, sid, "NQ"), pq, sid, "NQ"))
        es0 = _prefer_single_feed(_load_trades(pq, sid, "ES"), pq, sid, "ES")
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows == 0:
            continue
        mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
        vf = bounds(mid)[0]
        raw = detect_signals(nq, es, base_cfg)
        gated = filter_gated_mat(nq, raw, mat_gate, vwap_from_ns=vf)
        session_data[sid] = {"nq": nq, "es": es, "vf": vf, "mat": gated, "cohort": cohort(sid)}
        print(
            f"load {sid} ({cohort(sid)}): nq={nq.num_rows} mat_raw={raw.num_rows} "
            f"mat_gated={gated.num_rows}",
            flush=True,
        )

    cells = []
    for arm, T, X, br in product(("A", "B"), fail_secs, beyonds, brackets):
        if arm == "A":
            cells.append({
                "arm": arm, "fail_sec": T, "beyond_pts": X, "bracket": br,
                "retest_tol": None, "key": cell_key(arm, T, X, br, None),
            })
        else:
            for tol in retest_tols:
                cells.append({
                    "arm": arm, "fail_sec": T, "beyond_pts": X, "bracket": br,
                    "retest_tol": tol, "key": cell_key(arm, T, X, br, tol),
                })

    # Cache fade events per (arm, T, X, tol) — brackets share the same stamps
    fade_cache: dict[tuple, dict[str, pa.Table]] = {}

    def fades_for(cell: dict) -> dict[str, pa.Table]:
        ck = (
            cell["arm"],
            cell["fail_sec"],
            cell["beyond_pts"],
            cell["retest_tol"] if cell["arm"] == "B" else None,
        )
        if ck in fade_cache:
            return fade_cache[ck]
        by_sid: dict[str, pa.Table] = {}
        for sid, sd in session_data.items():
            by_sid[sid] = build_fail_fade_events(
                sd["nq"],
                sd["mat"],
                arm=cell["arm"],
                fail_sec=cell["fail_sec"],
                beyond_pts=cell["beyond_pts"],
                retest_tol=cell["retest_tol"] or 1.0,
                hold_sec=2.0,
                retest_max_sec=60.0,
            )
        fade_cache[ck] = by_sid
        return by_sid

    results: dict[str, dict] = {}
    for cell in cells:
        key = cell["key"]
        sim_cfg, br_fn = bracket_spec(cell["bracket"])
        fade_by_sid = fades_for(cell)
        parts_by_co: dict[str, list] = {c: [] for c in ("train", "may", "aug", "apr", "all")}
        n_ev = sum(t.num_rows for t in fade_by_sid.values())
        for sid, sd in session_data.items():
            fade = fade_by_sid[sid]
            if fade.num_rows == 0:
                continue
            s = simulate_session(
                sid, sd["nq"], sd["es"], sim_cfg,
                gate=None, bracket_fn=br_fn, vwap_from_ns=sd["vf"], events=fade,
            )
            co = sd["cohort"]
            parts_by_co[co].append(s)
            parts_by_co["all"].append(s)

        summary = {}
        for co, parts in parts_by_co.items():
            if not parts:
                summary[co] = {"n": 0, "net": 0.0, "avg": None, "wr": None, "pf": None}
                continue
            m = merge_summaries(parts, COMMISSION)
            d = m.to_dict()
            d["avg"] = round(m.net / m.n, 3) if m.n else None
            days_pos = 0
            days = 0
            for p in parts:
                if p.n == 0:
                    continue
                days += 1
                eq = day_equity_stats(p.trades, COMMISSION)
                if eq["equity_end"] > 0:
                    days_pos += 1
            d["days"] = days
            d["days_pos"] = days_pos
            summary[co] = d

        results[key] = {
            "cell": cell,
            "n_fade_events": n_ev,
            "summary": summary,
            "is_primary": key == cell_key("A", 20.0, 0.0, "fixed_10_10", None),
        }
        tr = summary["train"]
        print(
            f"{key}: train n={tr.get('n')} avg={tr.get('avg')} net={tr.get('net')} "
            f"| may={summary['may'].get('net')} aug={summary['aug'].get('net')} "
            f"apr={summary['apr'].get('net')}",
            flush=True,
        )

    # Rank train for A and B separately (fixed_10_10 sensitivity focus)
    def train_score(k: str) -> float:
        a = results[k]["summary"]["train"].get("avg")
        return a if a is not None else -1e9

    a_keys = [k for k in results if k.startswith("A_")]
    b_keys = [k for k in results if k.startswith("B_")]
    best_a = max(a_keys, key=train_score) if a_keys else None
    best_b = max(b_keys, key=train_score) if b_keys else None
    primary_key = cell_key("A", 20.0, 0.0, "fixed_10_10", None)

    def verdict_for(key: str | None) -> str:
        if key is None:
            return "n/a"
        s = results[key]["summary"]
        tr_avg = s["train"].get("avg")
        if tr_avg is None or tr_avg <= 0:
            return "reject (train ≤ 0)"
        may_n = s["may"].get("net") or 0
        aug_n = s["aug"].get("net") or 0
        if may_n < 0 or aug_n < 0:
            # same-sign check on net
            if (may_n > 0) != (tr_avg > 0) or (aug_n > 0) != (tr_avg > 0):
                return "reject (May/Aug flip vs train)"
            if may_n < 0 and aug_n < 0:
                return "reject (May+Aug both negative)"
        if may_n >= 0 and aug_n >= 0 and tr_avg > 0:
            return "dig/paper candidate (train+ and May+Aug non-neg); Apr one-shot only"
        return "mixed OOS — no ship"

    md = [
        "# EXP-0048 — MAT fail-fade (H39)",
        "",
        "Gated MAT (causal, trend+VWAP) → fade when push fails to break `mat_px` "
        "(arm A at T) or after held retest (arm B).",
        "",
        f"**Primary cell:** `{primary_key}`",
        f"**Best train A:** `{best_a}` — {verdict_for(best_a)}",
        f"**Best train B:** `{best_b}` — {verdict_for(best_b)}",
        f"**Primary verdict:** {verdict_for(primary_key)}",
        "",
        "April is observation-contaminated — do not choose knobs on Apr.",
        "",
        "| cell | train n | train avg | train net | may net | aug net | apr net |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    # Sort: primary first, then by train avg desc
    ordered = sorted(
        results.keys(),
        key=lambda k: (0 if k == primary_key else 1, -(train_score(k))),
    )
    for k in ordered:
        s = results[k]["summary"]
        md.append(
            f"| {k} | {s['train'].get('n')} | {s['train'].get('avg')} | "
            f"{s['train'].get('net')} | {s['may'].get('net')} | "
            f"{s['aug'].get('net')} | {s['apr'].get('net')} |"
        )
    md.append("")

    report = {
        "exp": "EXP-0048",
        "hypothesis": "H39",
        "primary": PRIMARY,
        "primary_key": primary_key,
        "best_train_A": best_a,
        "best_train_B": best_b,
        "verdict_primary": verdict_for(primary_key),
        "verdict_best_A": verdict_for(best_a),
        "verdict_best_B": verdict_for(best_b),
        "cells": results,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
    print(f"primary {primary_key}: {verdict_for(primary_key)}", flush=True)


if __name__ == "__main__":
    main()
