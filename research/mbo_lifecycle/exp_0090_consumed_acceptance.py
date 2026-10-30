"""EXP-0090 / H81 — consumed-level acceptance (left_behind / breach fade).

Usage:
  python -m research.mbo_lifecycle.exp_0090_consumed_acceptance
  python -m research.mbo_lifecycle.exp_0090_consumed_acceptance --max-sessions 10
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.backtest.mat_sim import detect_signals
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.consumed_acceptance import (
    ConsumedAcceptConfig,
    consumed_acceptance_events,
)
from research.features.mbo_lifecycle import LifecycleConfig, run_lifecycle
from research.irt_lvn.run_hold_break_of import load_mbo_window
from research.mat_context.report import write_exp_report
from research.run_cfade_all_months import _sid_chrono_key

HORIZONS = [10_000, 60_000, 300_000]
TARGETS = [5.0, 10.0, 20.0]
NS_MIN = 60 * 1_000_000_000
ARMS = ("left_behind", "retest_hold", "breach_back", "tape_only")


def _load_exp() -> dict:
    path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0090.yaml"
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


def _tape_only_twin(pair: H.Pair, n: int, seed: int) -> pa.Table:
    """MAT bursts with no lifecycle level engagement — tape-only null twin."""
    raw = detect_signals(pair.nq, pair.es, F.mat_sim_cfg())
    if raw.num_rows == 0:
        return H.null_events(pair, max(n, 1), seed=seed)
    # sample up to n
    if raw.num_rows > n > 0:
        rng = np.random.default_rng(seed)
        idx = rng.choice(raw.num_rows, size=n, replace=False)
        raw = raw.take(np.sort(idx))
    arms = ["tape_only"] * raw.num_rows
    return raw.append_column("arm", pa.array(arms, type=pa.string()))


def run(*, max_sessions: int | None = None, skip_docs: bool = False) -> dict:
    cfg = load_config()
    exp = _load_exp()
    pq_dir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0090"
    out.mkdir(parents=True, exist_ok=True)
    kill_cfg = dict(exp.get("kill") or {})
    seed = int(exp.get("placebo_seed", 90))
    acc = dict(exp.get("acceptance") or {})
    lcfg = LifecycleConfig(rth_uk_only=True)
    acfg = ConsumedAcceptConfig(
        accept_sec=float(acc.get("accept_sec", 5)),
        clear_pts=float(acc.get("clear_pts", 1)),
        approach_pts=float(acc.get("approach_pts", 1.5)),
        max_pullback_frac=float(acc.get("max_pullback_frac", 0.25)),
    )

    sessions = F.clean_sessions(pq_dir)
    chrono = sorted(sessions, key=_sid_chrono_key)
    cov = F.depth_mbo_coverage(pq_dir, chrono)
    mbo_sids = set(cov.get("mbo_sessions") or [])
    discovery = [s for s in chrono if s in mbo_sids and F.is_is(s)]
    if max_sessions:
        discovery = discovery[:max_sessions]
    print(f"[0090] discovery={len(discovery)}", flush=True)

    parts: list[pa.Table] = []
    pairs: dict[str, H.Pair] = {}

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
        ca = consumed_acceptance_events(lc_ev, pair.nq, cfg=acfg)
        n_lb = 0
        if ca.num_rows:
            n_lb = sum(1 for a in ca.column("arm").to_pylist() if a == "left_behind")
            twin = _tape_only_twin(pair, max(n_lb, 1), seed=seed + hash(sid) % 10_000)
            # align columns
            for col in ("arm",):
                if col not in twin.column_names:
                    twin = twin.append_column(col, pa.array(["tape_only"] * twin.num_rows))
            # keep only needed cols for concat
            keep_ca = [c for c in ca.column_names if c in (
                "event_ts_ns", "side", "arm", "contact_ts_ns", "level_side",
                "price_ticks", "ca_left_behind", "ca_retest_hold", "ca_breach_back",
                "peak_past_pts", "pullback_frac", "executed", "dominance", "order_id",
                "event_type", "cont_side",
            )]
            ca = ca.select(keep_ca)
            twin_keep = ["event_ts_ns", "side", "arm"]
            twin = twin.select([c for c in twin_keep if c in twin.column_names])
            # pad twin with nulls for missing cols
            for c in keep_ca:
                if c not in twin.column_names:
                    if c in ("event_ts_ns", "contact_ts_ns", "price_ticks"):
                        twin = twin.append_column(c, pa.array(np.zeros(twin.num_rows, dtype=np.int64)))
                    elif c in ("side", "cont_side", "ca_left_behind", "ca_retest_hold", "ca_breach_back"):
                        twin = twin.append_column(c, pa.array(np.zeros(twin.num_rows, dtype=np.int8)))
                    elif c in ("peak_past_pts", "pullback_frac", "executed", "dominance"):
                        twin = twin.append_column(c, pa.array(np.full(twin.num_rows, np.nan)))
                    else:
                        twin = twin.append_column(c, pa.array([""] * twin.num_rows))
            merged = _concat([ca, twin.select(keep_ca)])
        else:
            twin = _tape_only_twin(pair, 5, seed=seed + hash(sid) % 10_000)
            merged = twin
        merged = merged.append_column("session_id", pa.array([sid] * merged.num_rows))
        parts.append(merged)
        print(
            f"  {sid}: lc={lc_ev.num_rows} ca={ca.num_rows if ca.num_rows else 0} "
            f"left_behind={n_lb}",
            flush=True,
        )

    ann = _concat(parts)
    if ann.num_rows:
        pq.write_table(ann, out / "events.parquet", compression="zstd")

    arms_list = ann.column("arm").to_pylist() if ann.num_rows else []
    counts = {a: sum(1 for x in arms_list if x == a) for a in ARMS}

    min_lb = int(kill_cfg.get("min_left_behind", 30))
    min_to = int(kill_cfg.get("min_tape_only", 30))
    power_ok = counts["left_behind"] >= min_lb and counts["tape_only"] >= min_to

    report: dict = {"status": "ok", "experiment": "EXP-0090", "stages": {}}
    md = [
        "# EXP-0090 — Consumed acceptance (H81)",
        "",
        f"Discovery: {len(pairs)} · events: {ann.num_rows}",
        "",
        "## Stage 0 — power",
        "",
        f"left_behind: **{counts['left_behind']}** · retest_hold: {counts['retest_hold']} · "
        f"breach_back: {counts['breach_back']} · tape_only: **{counts['tape_only']}**",
        f"power: **{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    report["stages"]["0"] = {"counts": counts, "power_ok": power_ok}
    if not power_ok:
        report["verdict"] = (
            "reject / park — Stage 0 underpowered (counts are the result; no retune)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title="EXP-0090", md_lines=md, report=report)
        if not skip_docs:
            _write_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    print("[0090] Stage 1…", flush=True)
    lab_store = {a: [] for a in ARMS}
    for sid, pair in pairs.items():
        mask = np.array([ann.column("session_id")[i].as_py() == sid for i in range(ann.num_rows)])
        if not mask.any():
            continue
        sub = ann.take(np.where(mask)[0])
        for arm in ARMS:
            tbl = _filter_arm(sub, arm)
            if tbl.num_rows == 0:
                continue
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

    lift60 = float(kill_cfg.get("stage1_lift_60s_pts", 1.0))
    lift300 = float(kill_cfg.get("stage1_lift_300s_pts", 2.0))
    lb, to = stage1["left_behind"], stage1["tape_only"]
    m60lb, m60to = lb.get("mean_ret_pts_60000ms"), to.get("mean_ret_pts_60000ms")
    m300lb, m300to = lb.get("mean_ret_pts_300000ms"), to.get("mean_ret_pts_300000ms")
    s1 = {
        "lift_60": (
            m60lb is not None and m60to is not None
            and np.isfinite(m60lb) and np.isfinite(m60to)
            and (m60lb - m60to) >= lift60
        ),
        "lift_300": (
            m300lb is not None and m300to is not None
            and np.isfinite(m300lb) and np.isfinite(m300to)
            and (m300lb - m300to) >= lift300
        ),
    }
    s1_pass = all(s1.values())
    report["stages"]["1"] = {"summaries": stage1, "checks": s1, "pass": s1_pass}
    md += ["", f"Checks: {s1}", f"**Stage 1: {'PASS' if s1_pass else 'FAIL'}**", ""]
    report["verdict"] = (
        "PASS research Stage 1 — candidate left_behind continuation; virgin months before ship"
        if s1_pass
        else "reject / park — Stage 1 event-study fail (no Stage 2 P&L, no retune)"
    )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    write_exp_report(out, title="EXP-0090", md_lines=md, report=report)
    if not skip_docs:
        _write_docs(report["verdict"])
    print(report["verdict"], flush=True)
    return report


def _write_docs(verdict: str) -> None:
    hyp = Path("docs/HYPOTHESES.md")
    text = hyp.read_text(encoding="utf-8")
    marker = "## H81 — Consumed-level acceptance (left_behind / breach fade)\n"
    if marker not in text:
        block = (
            f"\n{marker}\n"
            "A tracked resting level classified `consumed` by aggression, with "
            "price leaving it behind (no pullback / no reclaim) at +5 s, "
            "continues; a breach back through the level is a fade (P&C). "
            "Tape-only twin mandatory. See `docs/MBO_LIFECYCLE_PLAN.md`.\n\n"
            f"**Test:** EXP-0090.\n"
            f"**Result (EXP-0090): {verdict}.** See `results/EXP-0090/`.\n\n"
            "---\n"
        )
        anchor = "## Unregistered candidates"
        idx = text.find(anchor)
        if idx >= 0:
            text = text[:idx] + block + text[idx:]
        else:
            text = text + block
        hyp.write_text(text, encoding="utf-8")

    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    note = (
        f"\n### 2026-10-30 — EXP-0090 consumed acceptance (H81)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0090/`.\n"
        f"- Runner: `python -m research.mbo_lifecycle.exp_0090_consumed_acceptance`.\n"
    )
    if "### 2026-10-30 — EXP-0090 consumed acceptance (H81)" not in lt:
        log.write_text(lt + note, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sessions", type=int, default=None)
    ap.add_argument("--skip-docs", action="store_true")
    args = ap.parse_args()
    run(max_sessions=args.max_sessions, skip_docs=args.skip_docs)


if __name__ == "__main__":
    main()
