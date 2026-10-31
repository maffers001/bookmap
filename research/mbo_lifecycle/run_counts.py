"""Stage-0 coverage / count report for MBO lifecycle states.

Outcomes are NOT labelled — counts only. Park standalone phases if
defended-retest or consumed floors unmet.

Usage:
  python -m research.mbo_lifecycle.run_counts
  python -m research.mbo_lifecycle.run_counts --max-sessions 10 --role NQ
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.mbo_lifecycle import (
    LifecycleConfig,
    annotate_large_dominant,
    build_size_thresholds_from_events,
    run_lifecycle,
    state_counts,
)
from research.irt_lvn.run_hold_break_of import load_mbo_window
from research.run_cfade_all_months import _sid_chrono_key

NS_MIN = 60 * 1_000_000_000

# Pre-registered floors (plan Phase 1 / EXP-0089 Stage 0)
FLOOR_DEFENDED = 30
FLOOR_CONSUMED = 30
FLOOR_PRIMARY = 20
FLOOR_RETEST_EST = 15  # estimated: primary contacts that leave & return — counted loosely


def _month(sid: str) -> str:
    # sid like 020326 → mm=03 yy=26
    if len(sid) >= 6 and sid[:6].isdigit():
        return sid[2:4] + sid[4:6]  # mmyy
    return "??"


def run(*, max_sessions: int | None = None, role: str = "NQ") -> dict:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "MBO_LIFECYCLE_COUNTS"
    out.mkdir(parents=True, exist_ok=True)
    lcfg = LifecycleConfig(rth_uk_only=True)

    sessions = F.clean_sessions(pq_dir)
    chrono = sorted(sessions, key=_sid_chrono_key)
    cov = F.depth_mbo_coverage(pq_dir, chrono)
    mbo_sids = set(cov.get("mbo_sessions") or cov.get("depth_sessions") or [])
    chrono = [s for s in chrono if s in mbo_sids]
    if max_sessions:
        chrono = chrono[:max_sessions]
    print(f"[counts] {len(chrono)} mbo sessions role={role}", flush=True)

    by_month: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_state = defaultdict(int)
    primary_n = 0
    parts: list[pa.Table] = []
    hand: list[dict] = []

    for si, sid in enumerate(chrono):
        print(f"  [{si+1}/{len(chrono)}] {sid} loading…", flush=True)
        pair = H.load_pair(pq_dir, sid)
        if pair is None:
            continue
        lo, hi = pair.bounds
        mbo = load_mbo_window(pq_dir, sid, role, (lo - 5 * NS_MIN, hi))
        if mbo.num_rows == 0:
            print(f"  {sid}: no mbo", flush=True)
            continue
        trades = pair.nq if role == "NQ" else pair.es
        if role == "ES" and (pair.es is None or pair.es.num_rows == 0):
            print(f"  {sid}: no ES trades", flush=True)
            continue
        print(f"  {sid}: mbo={mbo.num_rows} trades={trades.num_rows} running…", flush=True)
        ev = run_lifecycle(mbo, trades, cfg=lcfg, data_start_ns=lo - 5 * NS_MIN)
        if ev.num_rows == 0:
            print(f"  {sid}: 0 events", flush=True)
            continue
        thr = build_size_thresholds_from_events(ev, lcfg)
        ev = annotate_large_dominant(ev, cfg=lcfg, thr_by_min=thr)
        ev = ev.append_column("session_id", pa.array([sid] * ev.num_rows))
        parts.append(ev)
        sc = state_counts(ev)
        mo = _month(sid)
        for k, v in sc.items():
            if k == "n":
                continue
            by_state[k] += v
            by_month[mo][k] += v
        by_month[mo]["n"] += sc["n"]
        n_prim = int(ev.column("lc_primary").to_numpy().sum()) if "lc_primary" in ev.column_names else 0
        primary_n += n_prim
        by_month[mo]["primary"] += n_prim
        print(
            f"  {sid}: n={sc['n']} def={sc.get('defended', 0)} "
            f"comp={sc.get('completed', 0)} cons={sc.get('consumed', 0)} "
            f"pull={sc.get('pulled', 0)} primary={n_prim}",
            flush=True,
        )
        if len(hand) < 3 and n_prim:
            idx = int(np.where(ev.column("lc_primary").to_numpy() == 1)[0][0])
            hand.append({
                "session": sid,
                "order_id": ev.column("order_id")[idx].as_py(),
                "state": ev.column("state")[idx].as_py(),
                "initial_size": ev.column("initial_size")[idx].as_py(),
                "dominance": ev.column("dominance")[idx].as_py(),
                "executed": ev.column("executed")[idx].as_py(),
            })

    all_ev = pa.concat_tables(parts) if parts else pa.table({})
    if all_ev.num_rows:
        pq.write_table(all_ev, out / f"events_{role}.parquet", compression="zstd")

    floors = {
        "defended_ge": by_state["defended"] >= FLOOR_DEFENDED,
        "consumed_ge": by_state["consumed"] >= FLOOR_CONSUMED,
        "primary_ge": primary_n >= FLOOR_PRIMARY,
    }
    power_ok = all(floors.values())
    report = {
        "role": role,
        "n_sessions": len(chrono),
        "n_events": all_ev.num_rows,
        "by_state": dict(by_state),
        "primary_n": primary_n,
        "by_month": {m: dict(v) for m, v in sorted(by_month.items())},
        "floors": floors,
        "power_ok": power_ok,
        "hand_checks": hand,
        "verdict": (
            "counts PASS — standalone phases entitled"
            if power_ok
            else "counts FAIL — park standalone; keep as feature-on-trigger annotator"
        ),
    }
    (out / f"report_{role}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    md = [
        f"# MBO lifecycle Stage-0 counts ({role})",
        "",
        "Outcomes hidden — coverage only. Floors from programme plan.",
        "",
        f"Sessions: **{len(chrono)}** · events: **{all_ev.num_rows}** · "
        f"primary (large∧dominant∧¬seeded): **{primary_n}**",
        "",
        "## By state",
        "",
        "| state | n |",
        "|---|---:|",
    ]
    for k in ("defended", "completed", "consumed", "pulled", "other"):
        md.append(f"| {k} | {by_state.get(k, 0)} |")
    md += [
        "",
        "## Floors",
        "",
        f"- defended ≥ {FLOOR_DEFENDED}: **{floors['defended_ge']}** ({by_state['defended']})",
        f"- consumed ≥ {FLOOR_CONSUMED}: **{floors['consumed_ge']}** ({by_state['consumed']})",
        f"- primary ≥ {FLOOR_PRIMARY}: **{floors['primary_ge']}** ({primary_n})",
        "",
        f"**Verdict: {report['verdict']}**",
        "",
        "## By month",
        "",
        "| month | n | def | comp | cons | pull | primary |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for m, v in sorted(by_month.items()):
        md.append(
            f"| {m} | {v.get('n', 0)} | {v.get('defended', 0)} | {v.get('completed', 0)} | "
            f"{v.get('consumed', 0)} | {v.get('pulled', 0)} | {v.get('primary', 0)} |"
        )
    md += ["", "## Hand checks (primary)", "", "```json", json.dumps(hand, indent=2), "```", ""]
    (out / f"summary_{role}.md").write_text("\n".join(md), encoding="utf-8")
    print(report["verdict"], flush=True)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sessions", type=int, default=None)
    ap.add_argument("--role", default="NQ", choices=["NQ", "ES"])
    args = ap.parse_args()
    run(max_sessions=args.max_sessions, role=args.role)


if __name__ == "__main__":
    main()
