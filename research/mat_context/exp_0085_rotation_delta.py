"""EXP-0085 / H76 — MAT after an inefficient rotation (delta-efficiency).

Stages (frozen in research/experiments/EXP-0085.yaml +
docs/MAT_ROTATION_DELTA_EFFICIENCY_PLAN.md):
  Stage 0  truncation causality + power counts
  Stage 1  event study (counter-ineff vs counter-eff + IR dose + null)
  Stage 2  re-sim P&L books (Q1 ungated / Q2 frozen trend+VWAP)

Usage:
  python -m research.mat_context.exp_0085_rotation_delta
  python -m research.mat_context.exp_0085_rotation_delta --max-sessions 5  # smoke
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.backtest.mat_sim import (
    detect_signals,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.rotation_delta import (
    annotate_rotation_delta,
    completed_rotation_deltas,
    confirm_only_events,
    first_per_rotation_mask,
    prior_session_delta_stats,
    rotation_deltas_for_trades,
)
from research.features.swings import detect_swings
from research.mat_context.report import arm_pack, write_exp_report
from research.run_cfade_all_months import _sid_chrono_key

HORIZONS = [10_000, 60_000, 300_000]
TARGETS = [5.0, 10.0, 20.0]
IR_BINS = [
    ("ir_le_0", -1e18, 0.0),
    ("ir_0_0.83", 0.0, 0.83),
    ("ir_0.83_1.25", 0.83, 1.25),
    ("ir_1.25_1.75", 1.25, 1.75),
    ("ir_gt_1.75", 1.75, 1e18),
]


def _load_exp() -> dict:
    path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0085.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _filter_mask(ev: pa.Table, name: str) -> pa.Table:
    if ev.num_rows == 0 or name not in ev.column_names:
        return pa.table({})
    mk = ev.column(name).to_numpy().astype(np.int8) == 1
    return ev.take(np.where(mk)[0]) if mk.any() else pa.table({})


def _sim_events(
    sid: str,
    pair: H.Pair,
    ev: pa.Table,
    *,
    gate,
    slip: float = 0.0,
):
    if ev.num_rows == 0:
        return simulate_session(
            sid, pair.nq, pair.es, F.mat_sim_cfg(slip_ticks=slip),
            gate=gate, bracket_fn=F.mat_vol_bracket(),
            vwap_from_ns=pair.vwap_from_ns, events=pa.table({}),
        )
    cols = {
        "event_ts_ns": ev.column("event_ts_ns"),
        "side": ev.column("side"),
    }
    return simulate_session(
        sid, pair.nq, pair.es, F.mat_sim_cfg(slip_ticks=slip),
        gate=gate, bracket_fn=F.mat_vol_bracket(),
        vwap_from_ns=pair.vwap_from_ns, events=pa.table(cols),
    )


def _pack(sims: dict[str, object]) -> dict:
    parts, is_p, oos_p = [], [], []
    for sid, s in sims.items():
        parts.append(s)
        (is_p if F.is_is(sid) else oos_p).append(s)
    return arm_pack(parts, is_p, oos_p)


def _mean_ret(lab: pa.Table, col: str) -> float:
    if lab.num_rows == 0 or col not in lab.column_names:
        return float("nan")
    v = lab.column(col).to_numpy().astype(np.float64)
    v = v[np.isfinite(v)]
    return float(np.mean(v)) if len(v) else float("nan")


def _concat(parts: list[pa.Table]) -> pa.Table:
    parts = [p for p in parts if p.num_rows]
    if not parts:
        return pa.table({})
    cols = set(parts[0].column_names)
    for p in parts[1:]:
        cols &= set(p.column_names)
    keep = [c for c in parts[0].column_names if c in cols]
    return pa.concat_tables([p.select(keep) for p in parts], promote_options="default")


def _random_keep_p90(
    pool_pnls: np.ndarray, n_keep: int, *, seed: int, draws: int = 1000
) -> float:
    """p90 of mean pnl over random same-n subsets of pool (OOS comparator)."""
    if n_keep <= 0 or len(pool_pnls) == 0:
        return float("nan")
    n_keep = min(n_keep, len(pool_pnls))
    rng = np.random.default_rng(seed)
    avgs = np.empty(draws, dtype=np.float64)
    for i in range(draws):
        idx = rng.choice(len(pool_pnls), size=n_keep, replace=False)
        avgs[i] = float(np.mean(pool_pnls[idx]))
    return float(np.quantile(avgs, 0.90))


def _book_kill(
    arm: dict,
    base: dict,
    twin: dict,
    *,
    placebo_p90: float,
    min_oos_n: int,
    min_lift: float,
) -> dict:
    a_oos, b_oos, t_oos = arm["oos"], base["oos"], twin["oos"]
    avg = a_oos.get("avg")
    kill = {
        "oos_net_pos": a_oos["net"] > 0,
        "beat_base_oos_avg_plus_lift": (
            avg is not None
            and b_oos.get("avg") is not None
            and avg >= b_oos["avg"] + min_lift
        ),
        "beat_twin_oos_avg": (
            avg is not None
            and t_oos.get("avg") is not None
            and avg > t_oos["avg"]
        ),
        "beat_placebo_p90": (
            avg is not None
            and np.isfinite(placebo_p90)
            and avg > placebo_p90
        ),
        "n_oos_ge_min": a_oos["n"] >= min_oos_n,
        "placebo_p90": None if not np.isfinite(placebo_p90) else round(placebo_p90, 4),
    }
    kill["pass"] = all(
        kill[k]
        for k in (
            "oos_net_pos",
            "beat_base_oos_avg_plus_lift",
            "beat_twin_oos_avg",
            "beat_placebo_p90",
            "n_oos_ge_min",
        )
    )
    return kill


def _oos_trade_pnls(sims: dict, commission: float = F.COMMISSION) -> np.ndarray:
    out = []
    for sid, s in sims.items():
        if F.is_is(sid):
            continue
        for t in s.trades:
            out.append(t.pnl - commission)
    return np.asarray(out, dtype=np.float64)


def run(*, max_sessions: int | None = None, skip_docs: bool = False) -> dict:
    cfg = load_config()
    exp = _load_exp()
    pq_dir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0085"
    out.mkdir(parents=True, exist_ok=True)
    kill_cfg = dict(exp.get("kill") or {})
    seed = int(exp.get("placebo_seed", 85))

    sessions = F.clean_sessions(pq_dir)
    chrono = sorted(sessions, key=_sid_chrono_key)
    if max_sessions:
        chrono = chrono[: max_sessions]

    # ------------------------------------------------------------------ collect
    print(f"[0085] building rotation-delta + MAT over {len(chrono)} sessions", flush=True)
    prior_by: dict[str, object] = {}
    rots_by: dict[str, list] = {}
    ann_parts: list[pa.Table] = []
    confirm_parts: list[pa.Table] = []
    pairs: dict[str, H.Pair] = {}
    hand_checks: list[dict] = []

    for sid in chrono:
        pair = H.load_pair(pq_dir, sid)
        if pair is None:
            continue
        pairs[sid] = pair
        # causal prior = immediately preceding clean session with rotations
        prior = None
        try:
            idx = chrono.index(sid)
        except ValueError:
            idx = -1
        for prev in chrono[:idx][::-1]:
            if prev in prior_by:
                prior = prior_by[prev]
                break

        rots = rotation_deltas_for_trades(pair.nq, prior=prior)
        rots_by[sid] = rots
        prior_by[sid] = prior_session_delta_stats(rots)

        raw = detect_signals(pair.nq, pair.es, F.mat_sim_cfg())
        if raw.num_rows:
            ann = annotate_rotation_delta(raw, pair.nq, prior=prior, rots=rots)
            ann = ann.append_column(
                "session_id", pa.array([sid] * ann.num_rows, type=pa.string())
            )
            ann_parts.append(ann)
        c_ev = confirm_only_events(rots, class_name="inefficient")
        if c_ev.num_rows:
            c_ev = c_ev.append_column(
                "session_id", pa.array([sid] * c_ev.num_rows, type=pa.string())
            )
            confirm_parts.append(c_ev)

        # collect up to 3 hand-check rotations (manual delta vs stored)
        if len(hand_checks) < 3 and rots:
            r = rots[-1]
            ts = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
            sz = pair.nq.column("size").to_numpy().astype(np.float64)
            buy = pair.nq.column("is_buy_aggressor").to_numpy().astype(bool)
            order = np.argsort(ts)
            ts, sz, buy = ts[order], sz[order], buy[order]
            lo = int(np.searchsorted(ts, r.start_ts_ns, side="left"))
            hi = int(np.searchsorted(ts, r.end_ts_ns, side="right"))
            manual = float(np.where(buy[lo:hi], sz[lo:hi], -sz[lo:hi]).sum()) if hi > lo else 0.0
            hand_checks.append({
                "session": sid,
                "dir": r.direction,
                "length": r.length_pts,
                "stored_delta": r.delta,
                "manual_delta": manual,
                "match": abs(manual - r.delta) < 1e-6,
                "class": r.class_name,
                "ir": r.ir,
            })
        n_ineff = sum(1 for r in rots if r.class_name == "inefficient")
        print(
            f"  {sid}: rots={len(rots)} ineff={n_ineff} mat={raw.num_rows}",
            flush=True,
        )

    ann_all = _concat(ann_parts)
    confirm_all = _concat(confirm_parts)
    if ann_all.num_rows:
        pq.write_table(ann_all, out / "events.parquet", compression="zstd")

    report: dict = {"status": "ok", "experiment": "EXP-0085", "stages": {}}
    md = [
        "# EXP-0085 — MAT after an inefficient rotation (H76)",
        "",
        "Frozen: `research/experiments/EXP-0085.yaml` / "
        "`docs/MAT_ROTATION_DELTA_EFFICIENCY_PLAN.md`.",
        f"Sessions: {len(pairs)} · annotated MAT signals: {ann_all.num_rows}",
        "",
    ]

    # -------------------------------------------------------------- Stage 0
    n_ctr_ineff = int(ann_all.column("rd_primary").to_numpy().sum()) if ann_all.num_rows else 0
    n_ctr_eff = int(ann_all.column("rd_counter_eff").to_numpy().sum()) if ann_all.num_rows else 0
    n_with_ineff = int(ann_all.column("rd_with_ineff").to_numpy().sum()) if ann_all.num_rows else 0

    # Truncation invariance on up to 3 primary / any annotated events
    trunc_rows = []
    if ann_all.num_rows:
        # prefer primary stamps
        pri = _filter_mask(ann_all, "rd_primary")
        sample = pri if pri.num_rows else ann_all
        take_n = min(3, sample.num_rows)
        for i in range(take_n):
            sid = sample.column("session_id")[i].as_py()
            stamp = int(sample.column("event_ts_ns")[i].as_py())
            side = int(sample.column("side")[i].as_py())
            pair = pairs.get(sid)
            if pair is None:
                continue
            prior = None
            try:
                idx = chrono.index(sid)
            except ValueError:
                idx = -1
            for prev in chrono[:idx][::-1]:
                if prev in prior_by:
                    prior = prior_by[prev]
                    break
            ev = pa.table({
                "event_ts_ns": np.asarray([stamp], dtype=np.int64),
                "side": np.asarray([side], dtype=np.int8),
            })
            full_rots = rots_by.get(sid) or []
            full = annotate_rotation_delta(ev, pair.nq, prior=prior, rots=full_rots)
            ts = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
            cut = pair.nq.take(np.where(ts <= stamp)[0])
            # Causal truncate: only rotations confirmed by stamp (deltas already
            # closed at end_pivot ≤ confirm ≤ stamp). Re-detecting swings on a
            # cut tape can reshuffle unrelated pending pivots — report as
            # diagnostic only; kill bar uses confirm-filtered rots.
            rots_filt = [r for r in full_rots if r.end_confirm_ts_ns <= stamp]
            trunc = annotate_rotation_delta(ev, cut, prior=prior, rots=rots_filt)
            ok = True
            diffs = {}
            for col in ("rd_ir", "rd_class", "rd_primary", "rd_delta", "rd_end_confirm_ts_ns"):
                a = full.column(col)[0].as_py()
                b = trunc.column(col)[0].as_py()
                if isinstance(a, float):
                    match = (np.isnan(a) and (b is None or (isinstance(b, float) and np.isnan(b)))) or (
                        isinstance(b, float) and np.isfinite(a) and np.isfinite(b) and abs(a - b) < 1e-9
                    )
                else:
                    match = a == b
                if not match:
                    ok = False
                    diffs[col] = (a, b)
            piv = detect_swings(cut)
            rots_re = completed_rotation_deltas(piv, cut, prior=prior)
            re_ann = annotate_rotation_delta(ev, cut, prior=prior, rots=rots_re)
            re_ok = True
            for col in ("rd_ir", "rd_class", "rd_primary", "rd_delta"):
                a = full.column(col)[0].as_py()
                b = re_ann.column(col)[0].as_py()
                if isinstance(a, float):
                    match = (np.isnan(a) and (isinstance(b, float) and np.isnan(b))) or (
                        isinstance(b, float) and np.isfinite(a) and np.isfinite(b) and abs(a - b) < 1e-9
                    )
                else:
                    match = a == b
                if not match:
                    re_ok = False
                    break
            trunc_rows.append({
                "session": sid, "stamp": stamp, "match": ok,
                "redetect_match": re_ok, "diffs": diffs,
            })

    trunc_ok = bool(trunc_rows) and all(r["match"] for r in trunc_rows)
    hand_ok = bool(hand_checks) and all(r["match"] for r in hand_checks)

    # Power: also need A_ctr_ineff resim trade count under frozen gate
    gate = F.mat_frozen_gate()
    sims_A_ctr_ineff: dict = {}
    for sid, pair in pairs.items():
        # find annotated events for sid
        if ann_all.num_rows == 0:
            continue
        sid_mask = np.array([
            ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)
        ])
        if not sid_mask.any():
            continue
        sub = ann_all.take(np.where(sid_mask)[0])
        kept = _filter_mask(sub, "rd_primary")
        sims_A_ctr_ineff[sid] = _sim_events(sid, pair, kept, gate=gate)

    n_A_ctr_ineff = sum(s.n for s in sims_A_ctr_ineff.values())
    min_sig = int(kill_cfg.get("min_ungated_signals_each_counter_class", 100))
    min_frozen = int(kill_cfg.get("min_frozen_resim_trades", 20))
    power_ok = (
        n_ctr_ineff >= min_sig
        and n_ctr_eff >= min_sig
        and n_A_ctr_ineff >= min_frozen
    )

    report["stages"]["0"] = {
        "n_sessions": len(pairs),
        "n_mat_signals": ann_all.num_rows,
        "n_ctr_ineff_signals": n_ctr_ineff,
        "n_ctr_eff_signals": n_ctr_eff,
        "n_with_ineff_signals": n_with_ineff,
        "n_A_ctr_ineff_trades": n_A_ctr_ineff,
        "gate_deletion": {
            "primary_signals": n_ctr_ineff,
            "frozen_trades": n_A_ctr_ineff,
            "deleted_frac": (
                None if n_ctr_ineff == 0
                else round(1.0 - n_A_ctr_ineff / n_ctr_ineff, 3)
            ),
        },
        "truncation": trunc_rows,
        "truncation_ok": trunc_ok,
        "hand_checks": hand_checks,
        "hand_ok": hand_ok,
        "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — causality + power",
        "",
        f"Counter-inefficient (primary) signals: **{n_ctr_ineff}** · "
        f"counter-efficient twin: **{n_ctr_eff}** · with-ineff: {n_with_ineff}",
        f"Frozen A_ctr_ineff trades: **{n_A_ctr_ineff}** "
        f"(gate deletes {report['stages']['0']['gate_deletion']['deleted_frac']} of primary signals)",
        f"Truncation: **{'PASS' if trunc_ok else 'FAIL'}** ({len(trunc_rows)} checks) · "
        f"hand-delta: **{'PASS' if hand_ok else 'FAIL'}** · "
        f"power: **{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    if not trunc_ok or not hand_ok or not power_ok:
        why = (
            "Stage 0 causality failed"
            if (not trunc_ok or not hand_ok)
            else "Stage 0 underpowered (counts are the result; no retune)"
        )
        if n_A_ctr_ineff < min_frozen and n_ctr_ineff >= min_sig:
            why += (
                f" — frozen MAT gate leaves only {n_A_ctr_ineff} trades "
                f"(< {min_frozen}); Q2 answered as gate-deletes-setup; "
                "trend/VWAP not relaxed inside EXP-0085"
            )
        report["verdict"] = f"reject / park — {why}"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title="EXP-0085", md_lines=md, report=report)
        if not skip_docs:
            _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # -------------------------------------------------------------- Stage 1
    print("[0085] Stage 1 event study…", flush=True)
    lab_store: dict[str, list[pa.Table]] = {
        "all": [], "ctr_ineff": [], "ctr_eff": [], "with_ineff": [], "null": [],
    }
    for name, *_ in IR_BINS:
        lab_store[name] = []

    rng = np.random.default_rng(seed)
    for sid, pair in pairs.items():
        if ann_all.num_rows == 0:
            continue
        sid_mask = np.array([
            ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)
        ])
        if not sid_mask.any():
            continue
        sub = ann_all.take(np.where(sid_mask)[0])
        sets = {
            "all": sub,
            "ctr_ineff": _filter_mask(sub, "rd_primary"),
            "ctr_eff": _filter_mask(sub, "rd_counter_eff"),
            "with_ineff": _filter_mask(sub, "rd_with_ineff"),
        }
        # IR dose bins among counter-relation only
        if "rd_relation" in sub.column_names and "rd_ir" in sub.column_names:
            rel = sub.column("rd_relation").to_numpy(zero_copy_only=False)
            ir = sub.column("rd_ir").to_numpy().astype(np.float64)
            counter = np.array([r == "counter" for r in rel])
            for bname, lo, hi in IR_BINS:
                if bname == "ir_le_0":
                    mk = counter & np.isfinite(ir) & (ir <= 0)
                elif bname == "ir_gt_1.75":
                    mk = counter & np.isfinite(ir) & (ir > lo)
                else:
                    mk = counter & np.isfinite(ir) & (ir > lo) & (ir <= hi)
                sets[bname] = sub.take(np.where(mk)[0]) if mk.any() else pa.table({})

        n_null = max(sets["ctr_ineff"].num_rows, 1)
        nul = H.null_events(pair, n_null, seed=seed + hash(sid) % 10_000)
        # match side distribution of ctr_ineff if present
        if sets["ctr_ineff"].num_rows and nul.num_rows:
            sides = sets["ctr_ineff"].column("side").to_numpy()
            # overwrite null sides with a bootstrap of primary sides
            pick = rng.choice(sides, size=nul.num_rows, replace=True)
            nul = nul.drop("side").append_column(
                "side", pa.array(pick.astype(np.int8), type=pa.int8())
            )
        sets["null"] = nul

        for name, tbl in sets.items():
            if tbl.num_rows == 0:
                continue
            lab = H.label_in_points(tbl, pair, HORIZONS, TARGETS)
            if lab.num_rows:
                lab_store[name].append(lab)

    stage1 = {}
    md += [
        "## Stage 1 — event study (MAT stamp, points)",
        "",
        "| set | n | hit5 | hit10 | @10s | @60s | @300s | MFE | MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in (
        "all", "ctr_ineff", "ctr_eff", "with_ineff", "null",
        *[b[0] for b in IR_BINS],
    ):
        s = H.summarize(lab_store[name], HORIZONS, TARGETS)
        stage1[name] = s
        if s.get("n_events"):
            md.append(
                f"| {name} | {s['n_events']} | "
                f"{s.get('p_hit_5pts', float('nan')):.2f} | "
                f"{s.get('p_hit_10pts', float('nan')):.2f} | "
                f"{H.fmt(s.get('mean_ret_pts_10000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_60000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_300000ms'), 2)} | "
                f"{H.fmt(s.get('mean_mfe_pts'), 1)} | "
                f"{H.fmt(s.get('mean_mae_pts'), 1)} |"
            )
        else:
            md.append(f"| {name} | 0 | – | – | – | – | – | – | – |")

    # Side / length / speed strata on ctr_ineff vs ctr_eff (descriptive)
    def _stratum_table(key: str) -> list[tuple[str, dict]]:
        parts = lab_store.get(key) or []
        df = _concat(parts)
        if df.num_rows == 0:
            return []
        out = []
        side = df.column("side").to_numpy().astype(int)
        for lab, mk in (("long", side > 0), ("short", side < 0)):
            sub = df.take(np.where(mk)[0]) if mk.any() else pa.table({})
            if sub.num_rows:
                out.append((f"{key}_{lab}", H.summarize([sub], HORIZONS, TARGETS)))
        if "rd_length_pts" in df.column_names:
            L = df.column("rd_length_pts").to_numpy().astype(np.float64)
            ok = np.isfinite(L)
            if ok.sum() >= 9:
                q1, q2 = np.quantile(L[ok], [1 / 3, 2 / 3])
                for lab, mk in (
                    ("len_lo", ok & (L <= q1)),
                    ("len_mid", ok & (L > q1) & (L <= q2)),
                    ("len_hi", ok & (L > q2)),
                ):
                    sub = df.take(np.where(mk)[0])
                    if sub.num_rows:
                        out.append((f"{key}_{lab}", H.summarize([sub], HORIZONS, TARGETS)))
        if "rd_speed" in df.column_names:
            sp = df.column("rd_speed").to_numpy().astype(np.float64)
            ok = np.isfinite(sp)
            if ok.sum() >= 9:
                q1, q2 = np.quantile(sp[ok], [1 / 3, 2 / 3])
                for lab, mk in (
                    ("spd_lo", ok & (sp <= q1)),
                    ("spd_mid", ok & (sp > q1) & (sp <= q2)),
                    ("spd_hi", ok & (sp > q2)),
                ):
                    sub = df.take(np.where(mk)[0])
                    if sub.num_rows:
                        out.append((f"{key}_{lab}", H.summarize([sub], HORIZONS, TARGETS)))
        return out

    strata = _stratum_table("ctr_ineff") + _stratum_table("ctr_eff")
    stage1["strata"] = {n: s for n, s in strata}
    md += ["", "### Confound strata (descriptive)", "",
           "| stratum | n | hit10 | @60s | @300s |",
           "|---|---:|---:|---:|---:|"]
    for name, s in strata:
        md.append(
            f"| {name} | {s['n_events']} | {s.get('p_hit_10pts', float('nan')):.2f} | "
            f"{H.fmt(s.get('mean_ret_pts_60000ms'), 2)} | "
            f"{H.fmt(s.get('mean_ret_pts_300000ms'), 2)} |"
        )

    ci = stage1["ctr_ineff"]
    ce = stage1["ctr_eff"]
    nul = stage1["null"]
    lift_60 = float(kill_cfg.get("stage1_lift_60s_pts", 1.0))
    lift_300 = float(kill_cfg.get("stage1_lift_300s_pts", 2.0))
    m60_i = ci.get("mean_ret_pts_60000ms")
    m60_e = ce.get("mean_ret_pts_60000ms")
    m300_i = ci.get("mean_ret_pts_300000ms")
    m300_e = ce.get("mean_ret_pts_300000ms")
    m60_n = nul.get("mean_ret_pts_60000ms")

    # Dose: top IR bin vs bottom among counter
    top = stage1.get("ir_gt_1.75") or {}
    bot = stage1.get("ir_le_0") or {}
    # If le_0 empty, use lowest populated counter bin as bottom
    if not bot.get("n_events"):
        for bname, *_ in IR_BINS:
            if stage1.get(bname, {}).get("n_events"):
                bot = stage1[bname]
                break
    top60 = top.get("mean_ret_pts_60000ms")
    bot60 = bot.get("mean_ret_pts_60000ms")
    dose_ok = (
        top.get("n_events", 0) > 0
        and bot.get("n_events", 0) > 0
        and top60 is not None
        and bot60 is not None
        and np.isfinite(top60)
        and np.isfinite(bot60)
        and top60 > bot60
    )

    s1_checks = {
        "lift_60": (
            m60_i is not None and m60_e is not None
            and np.isfinite(m60_i) and np.isfinite(m60_e)
            and (m60_i - m60_e) >= lift_60
        ),
        "lift_300": (
            m300_i is not None and m300_e is not None
            and np.isfinite(m300_i) and np.isfinite(m300_e)
            and (m300_i - m300_e) >= lift_300
        ),
        "beat_null_60": (
            m60_i is not None and m60_n is not None
            and np.isfinite(m60_i) and np.isfinite(m60_n)
            and m60_i > m60_n
        ),
        "dose_top_gt_bottom": dose_ok,
    }
    stage1_pass = all(s1_checks.values())
    report["stages"]["1"] = {
        "summaries": {k: v for k, v in stage1.items() if k != "strata"},
        "strata": stage1.get("strata"),
        "checks": s1_checks,
        "pass": stage1_pass,
        "deltas": {
            "ctr_ineff_minus_eff_60": None if m60_i is None or m60_e is None else round(m60_i - m60_e, 3),
            "ctr_ineff_minus_eff_300": None if m300_i is None or m300_e is None else round(m300_i - m300_e, 3),
        },
    }
    md += [
        "",
        f"Checks: {s1_checks}",
        f"**Stage 1: {'PASS' if stage1_pass else 'FAIL'}**",
        "",
    ]
    if not stage1_pass:
        report["verdict"] = (
            "reject / park — Stage 1 event-study fail (no Stage 2 P&L, no retune)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title="EXP-0085", md_lines=md, report=report)
        if not skip_docs:
            _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # -------------------------------------------------------------- Stage 2
    print("[0085] Stage 2 P&L…", flush=True)
    book_names = [
        "A_all", "A_ctr_ineff", "A_ctr_eff",
        "B_all", "B_ctr_ineff", "B_ctr_eff", "B_with_ineff",
        "C_confirm_only", "null",
        "B_ctr_ineff_first",  # sensitivity
    ]
    sims: dict[str, dict] = {b: {} for b in book_names}
    gate_A = F.mat_frozen_gate()
    gate_off = None  # simulate_session with gate=None = always accept after events

    def _always(side, sr, st, px):
        return True

    for sid, pair in pairs.items():
        sid_mask = np.array([
            ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)
        ]) if ann_all.num_rows else np.array([], dtype=bool)
        sub = ann_all.take(np.where(sid_mask)[0]) if sid_mask.any() else pa.table({})
        c_mask = np.array([
            confirm_all.column("session_id")[i].as_py() == sid
            for i in range(confirm_all.num_rows)
        ]) if confirm_all.num_rows else np.array([], dtype=bool)
        c_sub = confirm_all.take(np.where(c_mask)[0]) if c_mask.any() else pa.table({})

        sets = {
            "A_all": (sub, gate_A),
            "A_ctr_ineff": (_filter_mask(sub, "rd_primary"), gate_A),
            "A_ctr_eff": (_filter_mask(sub, "rd_counter_eff"), gate_A),
            "B_all": (sub, _always),
            "B_ctr_ineff": (_filter_mask(sub, "rd_primary"), _always),
            "B_ctr_eff": (_filter_mask(sub, "rd_counter_eff"), _always),
            "B_with_ineff": (_filter_mask(sub, "rd_with_ineff"), _always),
            "C_confirm_only": (c_sub, _always),
        }
        # first-per-rotation sensitivity on B primary
        pri = _filter_mask(sub, "rd_primary")
        if pri.num_rows and "rd_end_confirm_ts_ns" in pri.column_names:
            fk = first_per_rotation_mask(pri)
            first = pri.take(np.where(fk)[0]) if fk.any() else pa.table({})
        else:
            first = pa.table({})
        sets["B_ctr_ineff_first"] = (first, _always)

        n_base = max(sub.num_rows, 1)
        nul = H.null_events(pair, n_base, seed=seed + 17 + (hash(sid) % 10_000))
        sets["null"] = (nul, gate_A)

        for bname, (ev, g) in sets.items():
            sims[bname][sid] = _sim_events(sid, pair, ev, gate=g if g is not None else _always)

        print(f"  [pnl] {sid}", flush=True)

    books = {b: _pack(sims[b]) for b in book_names}
    # Slip sensitivity on Q1/Q2 primaries
    slip_sens = {}
    for slip in (1.0, 2.0):
        for bname, mask_name, g in (
            ("A_ctr_ineff", "rd_primary", gate_A),
            ("B_ctr_ineff", "rd_primary", _always),
        ):
            s2 = {}
            for sid, pair in pairs.items():
                sid_mask = np.array([
                    ann_all.column("session_id")[i].as_py() == sid
                    for i in range(ann_all.num_rows)
                ]) if ann_all.num_rows else np.array([], dtype=bool)
                sub = ann_all.take(np.where(sid_mask)[0]) if sid_mask.any() else pa.table({})
                s2[sid] = _sim_events(
                    sid, pair, _filter_mask(sub, mask_name), gate=g, slip=slip
                )
            slip_sens[f"{bname}_slip{int(slip)}"] = _pack(s2)

    # Placebo p90 from OOS comparator pools
    a_pool = _oos_trade_pnls(sims["A_all"])
    b_pool = _oos_trade_pnls(sims["B_all"])
    a_n = books["A_ctr_ineff"]["oos"]["n"]
    b_n = books["B_ctr_ineff"]["oos"]["n"]
    a_p90 = _random_keep_p90(a_pool, a_n, seed=seed + 1)
    b_p90 = _random_keep_p90(b_pool, b_n, seed=seed + 2)

    min_oos = int(kill_cfg.get("min_oos_trades_promote", 50))
    min_lift = float(kill_cfg.get("min_avg_lift_pts", 1.0))
    kills = {
        "Q2_A_ctr_ineff": _book_kill(
            books["A_ctr_ineff"], books["A_all"], books["A_ctr_eff"],
            placebo_p90=a_p90, min_oos_n=min_oos, min_lift=min_lift,
        ),
        "Q1_B_ctr_ineff": _book_kill(
            books["B_ctr_ineff"], books["B_all"], books["B_ctr_eff"],
            placebo_p90=b_p90, min_oos_n=min_oos, min_lift=min_lift,
        ),
    }

    md += [
        "## Stage 2 — P&L (re-sim from kept events)",
        "",
        "| book | n | WR | net | avg | IS net | OOS net | OOS avg | days+ | maxDD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for b in book_names:
        v = books[b]
        a, o = v["all"], v["oos"]
        md.append(
            f"| {b} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{v['is']['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} | "
            f"{a.get('days_pos')}/{a.get('days')} | {a.get('max_dd')} |"
        )
    md += ["", "### Slip sensitivity", ""]
    for k, v in slip_sens.items():
        o = v["oos"]
        md.append(f"- {k}: OOS n={o['n']} net={o['net']:+.1f} avg={o.get('avg')}")

    q1 = kills["Q1_B_ctr_ineff"]["pass"]
    q2 = kills["Q2_A_ctr_ineff"]["pass"]
    report["stages"]["2"] = {
        "books": books,
        "kills": kills,
        "slip_sensitivity": slip_sens,
        "q1_pass": q1,
        "q2_pass": q2,
    }
    md += [
        "",
        f"Q1 (ungated B_ctr_ineff) kill: {kills['Q1_B_ctr_ineff']}",
        f"Q2 (frozen A_ctr_ineff) kill: {kills['Q2_A_ctr_ineff']}",
        "",
        f"**Stage 2: Q1={'PASS' if q1 else 'FAIL'} · Q2={'PASS' if q2 else 'FAIL'}**",
        "",
    ]

    if q2:
        report["verdict"] = (
            "PASS Q2 — candidate MAT keep-condition; ship only after virgin "
            "future months (Sep–Oct already MAT-mined)"
        )
    elif q1:
        report["verdict"] = (
            "PASS Q1 only / FAIL Q2 — mechanism holds ungated; new EXP-0086 "
            "counter-MAT (no trend/VWAP) on virgin months; no live-gate edit"
        )
    else:
        report["verdict"] = (
            "reject / park — Stage 2 P&L fail on both Q1 and Q2 (no retune)"
        )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    write_exp_report(out, title="EXP-0085", md_lines=md, report=report)
    if not skip_docs:
        _write_verdict_docs(report["verdict"])
    print(report["verdict"], flush=True)
    return report


def _write_verdict_docs(verdict: str) -> None:
    hyp = Path("docs/HYPOTHESES.md")
    text = hyp.read_text(encoding="utf-8")
    needle = "**Result:** pending — plan registered 2026-10-30, runner not yet built."
    if needle in text:
        text = text.replace(
            needle,
            f"**Result (EXP-0085): {verdict}.** See `results/EXP-0085/`.",
            1,
        )
        hyp.write_text(text, encoding="utf-8")

    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    old = (
        "| EXP-0085 | H76 MAT after inefficient rotation (delta-efficiency) | "
        "registered — pending run | 113 clean sessions (DIG4 IS) | "
        "counter-MAT after rotation with IR≥1.5; frozen plan "
        "`docs/MAT_ROTATION_DELTA_EFFICIENCY_PLAN.md`; runner not yet built |"
    )
    new = (
        f"| EXP-0085 | H76 MAT after inefficient rotation (delta-efficiency) | "
        f"ran — **{verdict.split('—')[0].strip()}** | 113 clean sessions | "
        f"{verdict}; see notes |"
    )
    if old in lt:
        lt = lt.replace(old, new, 1)
    note = (
        f"\n### 2026-10-30 — EXP-0085 MAT rotation delta-efficiency run (H76)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0085/`.\n"
        f"- Runner: `python -m research.mat_context.exp_0085_rotation_delta`.\n"
    )
    if "### 2026-10-30 — EXP-0085 MAT rotation delta-efficiency run (H76)" not in lt:
        anchor = "### 2026-10-30 — EXP-0085 MAT rotation delta-efficiency registered (H76)"
        idx = lt.find(anchor)
        nxt = lt.find("\n### ", idx + 5) if idx >= 0 else -1
        if nxt > 0:
            lt = lt[:nxt] + note + lt[nxt:]
        else:
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
