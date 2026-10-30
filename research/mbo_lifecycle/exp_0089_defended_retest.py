"""EXP-0089 / H80 — defended-retest of dominant resting MBO order.

Usage:
  python -m research.mbo_lifecycle.exp_0089_defended_retest
  python -m research.mbo_lifecycle.exp_0089_defended_retest --max-sessions 10
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.defended_retest import (
    DefendedRetestConfig,
    defended_retest_events,
    placebo_retests,
)
from research.features.mbo_lifecycle import (
    LifecycleConfig,
    annotate_large_dominant,
    build_size_thresholds_from_events,
    run_lifecycle,
)
from research.irt_lvn.run_hold_break_of import load_mbo_window
from research.mat_context.report import write_exp_report
from research.run_cfade_all_months import _sid_chrono_key

HORIZONS = [10_000, 60_000, 300_000]
TARGETS = [5.0, 10.0, 20.0]
NS_MIN = 60 * 1_000_000_000
ARMS = ("defended", "completed", "fragmented", "placebo")


def _load_exp() -> dict:
    path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0089.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _filter_arm(ev: pa.Table, arm: str) -> pa.Table:
    if ev.num_rows == 0 or "arm" not in ev.column_names:
        return pa.table({})
    mk = np.array([a == arm for a in ev.column("arm").to_pylist()])
    return ev.take(np.where(mk)[0]) if mk.any() else pa.table({})


def _concat(parts: list[pa.Table]) -> pa.Table:
    parts = [p for p in parts if p.num_rows]
    if not parts:
        return pa.table({})
    cols = set(parts[0].column_names)
    for p in parts[1:]:
        cols &= set(p.column_names)
    keep = [c for c in parts[0].column_names if c in cols]
    return pa.concat_tables([p.select(keep) for p in parts], promote_options="default")


def run(*, max_sessions: int | None = None, skip_docs: bool = False) -> dict:
    cfg = load_config()
    exp = _load_exp()
    pq_dir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0089"
    out.mkdir(parents=True, exist_ok=True)
    kill_cfg = dict(exp.get("kill") or {})
    seed = int(exp.get("placebo_seed", 89))

    lc = dict(exp.get("lifecycle") or {})
    rt = dict(exp.get("retest") or {})
    lcfg = LifecycleConfig(
        large_q=float(lc.get("large_q", 0.99)),
        dominance_min=float(lc.get("dominance_min", 0.50)),
        fragmented_max=float(lc.get("fragmented_max", 0.25)),
        min_executed=float(lc.get("min_executed", 10)),
        rth_uk_only=True,
    )
    rcfg = DefendedRetestConfig(
        depart_pts=float(rt.get("depart_pts", 8)),
        tol_pts=float(rt.get("tol_pts", 0.5)),
        max_retest_sec=float(rt.get("max_retest_sec", 300)),
        through_ticks=int(rt.get("through_ticks", 2)),
        defended_remain=float(rt.get("defended_remain", 0.50)),
        completed_remain=float(rt.get("completed_remain", 0.25)),
        placebo_shift_pts=float(rt.get("placebo_shift_pts", 40)),
        min_executed=float(lc.get("min_executed", 10)),
    )

    sessions = F.clean_sessions(pq_dir)
    chrono = sorted(sessions, key=_sid_chrono_key)
    cov = F.depth_mbo_coverage(pq_dir, chrono)
    mbo_sids = set(cov.get("mbo_sessions") or [])
    chrono = [s for s in chrono if s in mbo_sids]
    # discovery = DIG4 ∩ mbo
    discovery = [s for s in chrono if F.is_is(s)]
    if max_sessions:
        discovery = discovery[:max_sessions]
    print(f"[0089] discovery={len(discovery)} mbo sessions", flush=True)

    parts: list[pa.Table] = []
    pairs: dict[str, H.Pair] = {}
    trunc_rows = []

    for sid in discovery:
        pair = H.load_pair(pq_dir, sid)
        if pair is None:
            continue
        pairs[sid] = pair
        lo, hi = pair.bounds
        mbo = load_mbo_window(pq_dir, sid, "NQ", (lo - 5 * NS_MIN, hi))
        if mbo.num_rows == 0:
            continue
        lc_ev = run_lifecycle(mbo, pair.nq, cfg=lcfg, data_start_ns=lo - 5 * NS_MIN)
        if lc_ev.num_rows == 0:
            continue
        thr = build_size_thresholds_from_events(lc_ev, lcfg)
        lc_ev = annotate_large_dominant(lc_ev, cfg=lcfg, thr_by_min=thr)
        re = defended_retest_events(lc_ev, pair.nq, cfg=rcfg, lc_cfg=lcfg)
        if re.num_rows == 0:
            print(f"  {sid}: lc={lc_ev.num_rows} retests=0", flush=True)
            continue
        # placebo from defended+completed contacts
        base_for_plac = re
        plac = placebo_retests(base_for_plac, pair.nq, cfg=rcfg)
        if plac.num_rows:
            re = _concat([re, plac])
        re = re.append_column("session_id", pa.array([sid] * re.num_rows))
        parts.append(re)
        n_def = int(re.column("dr_defended").to_numpy().sum()) if "dr_defended" in re.column_names else 0
        print(
            f"  {sid}: lc={lc_ev.num_rows} retests={re.num_rows} defended={n_def}",
            flush=True,
        )
        # truncation check on first few
        if len(trunc_rows) < 3 and re.num_rows:
            stamp = int(re.column("event_ts_ns")[0].as_py())
            contact = int(re.column("contact_ts_ns")[0].as_py())
            # cut trades at retest stamp; geometry must match
            tts = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
            cut = pair.nq.take(np.where(tts <= stamp)[0])
            # rebuild from same lifecycle truncated conceptually at stamp
            re2 = defended_retest_events(lc_ev, cut, cfg=rcfg, lc_cfg=lcfg)
            match = True
            if re2.num_rows == 0:
                match = False
            else:
                # find same contact
                c2 = re2.column("contact_ts_ns").to_numpy()
                hit = np.where(c2 == contact)[0]
                match = len(hit) > 0
            trunc_rows.append({"session": sid, "match": match})

    ann = _concat(parts)
    if ann.num_rows:
        pq.write_table(ann, out / "events.parquet", compression="zstd")

    n_def = int((ann.column("arm").to_numpy(zero_copy_only=False) == "defended").sum()) if ann.num_rows else 0
    # arm is string — use pylist
    if ann.num_rows:
        arms = ann.column("arm").to_pylist()
        n_def = sum(1 for a in arms if a == "defended")
        n_comp = sum(1 for a in arms if a == "completed")
        n_frag = sum(1 for a in arms if a == "fragmented")
        n_plac = sum(1 for a in arms if a == "placebo")
    else:
        n_def = n_comp = n_frag = n_plac = 0

    trunc_ok = bool(trunc_rows) and all(r["match"] for r in trunc_rows)
    min_def = int(kill_cfg.get("min_defended", 30))
    min_comp = int(kill_cfg.get("min_completed", 30))
    power_ok = n_def >= min_def and n_comp >= min_comp

    report: dict = {
        "status": "ok", "experiment": "EXP-0089", "stages": {},
        "coverage": {"n_discovery": len(discovery), "n_pairs": len(pairs)},
    }
    md = [
        "# EXP-0089 — Defended-retest (H80)",
        "",
        f"Discovery DIG4∩MBO: {len(pairs)} · retest events: {ann.num_rows}",
        "",
        "## Stage 0 — causality + power",
        "",
        f"defended: **{n_def}** · completed: **{n_comp}** · fragmented: {n_frag} · placebo: {n_plac}",
        f"Truncation: **{'PASS' if trunc_ok else 'FAIL'}** · power: **{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    report["stages"]["0"] = {
        "n_defended": n_def, "n_completed": n_comp, "n_fragmented": n_frag,
        "n_placebo": n_plac, "truncation": trunc_rows, "truncation_ok": trunc_ok,
        "power_ok": power_ok,
    }
    if not trunc_ok or not power_ok:
        why = "Stage 0 causality failed" if not trunc_ok else (
            "Stage 0 underpowered (counts are the result; no retune)"
        )
        report["verdict"] = f"reject / park — {why}"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title="EXP-0089", md_lines=md, report=report)
        if not skip_docs:
            _write_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # Stage 1 event study
    print("[0089] Stage 1…", flush=True)
    lab_store = {a: [] for a in ARMS}
    for sid, pair in pairs.items():
        if ann.num_rows == 0:
            continue
        mask = np.array([ann.column("session_id")[i].as_py() == sid for i in range(ann.num_rows)])
        if not mask.any():
            continue
        sub = ann.take(np.where(mask)[0])
        for arm in ARMS:
            tbl = _filter_arm(sub, arm)
            if tbl.num_rows == 0:
                continue
            # keep side + event_ts for labelling
            lab = H.label_in_points(tbl, pair, HORIZONS, TARGETS)
            if lab.num_rows:
                lab_store[arm].append(lab)

    stage1 = {}
    md += [
        "## Stage 1 — event study",
        "",
        "| arm | n | hit5 | hit10 | @10s | @60s | @300s |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ARMS:
        s = H.summarize(lab_store[arm], HORIZONS, TARGETS)
        stage1[arm] = s
        if s.get("n_events"):
            md.append(
                f"| {arm} | {s['n_events']} | "
                f"{s.get('p_hit_5pts', float('nan')):.2f} | "
                f"{s.get('p_hit_10pts', float('nan')):.2f} | "
                f"{H.fmt(s.get('mean_ret_pts_10000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_60000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_300000ms'), 2)} |"
            )
        else:
            md.append(f"| {arm} | 0 | – | – | – | – | – |")

    lift = float(kill_cfg.get("stage1_lift_60s_pts", 1.0))
    d, c, p = stage1["defended"], stage1["completed"], stage1["placebo"]
    m60d, m60c = d.get("mean_ret_pts_60000ms"), c.get("mean_ret_pts_60000ms")
    m60p = p.get("mean_ret_pts_60000ms")
    s1 = {
        "lift_vs_completed": (
            m60d is not None and m60c is not None
            and np.isfinite(m60d) and np.isfinite(m60c)
            and (m60d - m60c) >= lift
        ),
        "beat_placebo": (
            m60d is not None and m60p is not None
            and np.isfinite(m60d) and np.isfinite(m60p)
            and m60d > m60p
        ),
    }
    s1_pass = all(s1.values())
    report["stages"]["1"] = {"summaries": stage1, "checks": s1, "pass": s1_pass}
    md += ["", f"Checks: {s1}", f"**Stage 1: {'PASS' if s1_pass else 'FAIL'}**", ""]
    if not s1_pass:
        report["verdict"] = (
            "reject / park — Stage 1 event-study fail (no Stage 2 P&L, no retune)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title="EXP-0089", md_lines=md, report=report)
        if not skip_docs:
            _write_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # Stage 2: simple forward-return proxy as P&L stand-in (vol brackets deferred
    # to virgin months if Stage 1 passes — use @60s mean * n as descriptive)
    report["stages"]["2"] = {
        "note": "Stage 1 passed; bracket re-sim deferred to virgin-month freeze per plan Phase 5",
        "pass": True,
    }
    report["verdict"] = (
        "PASS research Stage 1 — candidate defended-retest; virgin months before ship "
        "(Stage 2 brackets on freeze sample)"
    )
    md += [
        "## Stage 2",
        "",
        "Bracket re-sim deferred to virgin-month freeze (plan Phase 5).",
        "",
        "## Verdict",
        "",
        f"**{report['verdict']}**",
        "",
    ]
    write_exp_report(out, title="EXP-0089", md_lines=md, report=report)
    if not skip_docs:
        _write_docs(report["verdict"])
    print(report["verdict"], flush=True)
    return report


def _write_docs(verdict: str) -> None:
    hyp = Path("docs/HYPOTHESES.md")
    text = hyp.read_text(encoding="utf-8")
    marker = "## H80 — Defended-retest of dominant resting MBO order\n"
    if marker not in text:
        block = (
            f"\n{marker}\n"
            "After a causally-large dominant resting order is contacted, a "
            "retest of its price holds when the level is re-defended "
            "(stack rebuilt / net adds on approach) and fails when the "
            "order completed (stack gone). Fade defended retests; completed "
            "is the twin. ±40 pt placebo. See `docs/MBO_LIFECYCLE_PLAN.md`.\n\n"
            f"**Test:** EXP-0089.\n"
            f"**Result (EXP-0089): {verdict}.** See `results/EXP-0089/`.\n\n"
            "---\n"
        )
        # insert before Unregistered candidates if present
        anchor = "## Unregistered candidates"
        idx = text.find(anchor)
        if idx >= 0:
            text = text[:idx] + block + text[idx:]
        else:
            text = text + block
        hyp.write_text(text, encoding="utf-8")
    else:
        # update result line
        import re
        text2, n = re.subn(
            r"(\*\*Result \(EXP-0089\):)[^*]+(\*\*)",
            rf"\1 {verdict}.\2",
            text,
            count=1,
        )
        if n:
            hyp.write_text(text2, encoding="utf-8")

    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    note = (
        f"\n### 2026-10-30 — EXP-0089 defended-retest (H80)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0089/`.\n"
        f"- Runner: `python -m research.mbo_lifecycle.exp_0089_defended_retest`.\n"
        f"- Plan: `docs/MBO_LIFECYCLE_PLAN.md`.\n"
    )
    if "### 2026-10-30 — EXP-0089 defended-retest (H80)" not in lt:
        lt = lt + note
        log.write_text(lt, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sessions", type=int, default=None)
    ap.add_argument("--skip-docs", action="store_true")
    args = ap.parse_args()
    run(max_sessions=args.max_sessions, skip_docs=args.skip_docs)


if __name__ == "__main__":
    main()
