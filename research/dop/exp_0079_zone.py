"""EXP-0079 / H70 — DOP break beyond-zone traversal (NQ, depth).

Stages (frozen in research/experiments/EXP-0079.yaml):
  Stage 0  depth feasibility + causality + power
  Stage 1  event study: A (DOP entry) vs T (fast traversal, mechanism)
           vs S (slow/no traversal) vs C (confirm entry at zone exit)
  Stage 2  Book 2 P&L: C vs A, random-skip placebo
  Stage 3  Sep-Oct semi-OOS with --semi-oos only after discovery pass

Usage: python -m research.dop.exp_0079_zone [--semi-oos]
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.backtest.mat_sim import MatSimConfig, SimSummary, make_vol_bracket_rr, simulate_session
from research.config import load_config
from research.correlated import harness as H
from research.dop.exp_0078_dop import _month, all_sessions, compute_break_history, load_pair_full
from research.features.dop_zone import compute_zone_features

NS_MIN = 60_000_000_000

ZONE_FLOATS = [
    "entry_px_pts", "dop_pts", "zone_resting", "zone_max", "zone_resting_m60",
    "stack_build_60s", "time_to_zone_sec", "time_to_exit_sec", "traversal_sec",
    "executed_into_zone", "consume_frac", "pullback_in_zone_pts",
    "zone_resting_at_exit", "replenish_est", "confirm_px_pts", "large_thr",
]
ZONE_INTS = [
    "event_ts_ns", "side", "depth_ok", "zone_n_levels", "zone_n_large",
    "reached_zone", "reached_exit", "fast_traversal", "confirm_ts_ns",
]


def events_to_table(rows: list[dict]) -> pa.Table:
    if not rows:
        return pa.table({})
    cols: dict[str, pa.Array] = {}
    cols["event_ts_ns"] = pa.array([int(r["event_ts_ns"]) for r in rows], pa.int64())
    cols["side"] = pa.array([int(r["side"]) for r in rows], pa.int8())
    for name in ZONE_FLOATS:
        cols[name] = pa.array([float(r.get(name, np.nan)) for r in rows], pa.float64())
    for name in ZONE_INTS:
        if name in ("event_ts_ns", "side"):
            continue
        cols[name] = pa.array([int(r.get(name, 0)) for r in rows], pa.int64())
    cols["session_id"] = pa.array([r["session_id"] for r in rows])
    cols["market"] = pa.array([r["market"] for r in rows])
    return pa.table(cols)


def _filter_table(t: pa.Table, mask: np.ndarray) -> pa.Table:
    if t.num_rows == 0:
        return t
    idx = np.where(mask)[0]
    return t.take(idx) if len(idx) else t.slice(0, 0)


def _avg(s: SimSummary) -> float:
    return (s.net / s.n) if s.n else float("nan")


def sim_from_trades(trades: list) -> SimSummary:
    from research.backtest.mat_sim import _rebuild_summary
    return _rebuild_summary(list(trades), H.COMMISSION)


def _placebo_p90(trades, *, keep_n, draws, seed, commission) -> dict:
    pnls = np.asarray([t.pnl - commission for t in trades], dtype=np.float64)
    n = len(pnls)
    if n == 0 or keep_n <= 0:
        return {"p90_avg": float("nan"), "n": 0, "keep_n": int(keep_n)}
    k = min(keep_n, n)
    rng = np.random.default_rng(seed)
    avgs = np.array([float(pnls[rng.choice(n, size=k, replace=False)].mean()) for _ in range(draws)])
    return {"p90_avg": float(np.quantile(avgs, 0.90)), "n": int(n), "keep_n": int(k)}


# ---------------------------------------------------------------- zone rows (cached)

def build_zone_rows(parquet_dir: Path, sids: list[str], hist: dict, exp: dict,
                    results_dir: Path) -> list[dict]:
    zcfg = dict(exp.get("dop_zone") or {})
    inner = float(zcfg.get("zone_inner_pts", 10.0))
    outer = float(zcfg.get("zone_outer_pts", 20.0))
    cap = float(zcfg.get("traversal_cap_sec", 60))
    deadline = float(zcfg.get("zone_entry_deadline_min", 15))
    observe = float(zcfg.get("traversal_observe_min", 30))
    seed_min = int(zcfg.get("depth_seed_min_before_open", 30))

    cache_path = results_dir / "_cache_zone_rows.parquet"
    cached: dict[str, dict] = {}
    if cache_path.exists():
        ct = pq.read_table(cache_path)
        for i in range(ct.num_rows):
            row = {n: ct.column(n)[i].as_py() for n in ct.column_names}
            cached[str(row["session_id"])] = row

    rows: list[dict] = []
    new_rows: list[dict] = []
    for sid in sids:
        if sid in cached:
            rows.append(cached[sid])
            continue
        st = (hist.get(sid) or {}).get("NQ") or {}
        bk = st.get("break")
        if bk is None:
            row = {"session_id": sid, "market": "NQ", "has_break": 0, "depth_ok": 0}
            rows.append(row)
            new_rows.append(row)
            continue
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        open_ns, win_end = pair.bounds
        depth = H.load_depth_window(
            parquet_dir, sid, "NQ",
            (open_ns - seed_min * NS_MIN, min(win_end, bk.event_ts_ns + int(observe * 60 * 1e9))),
        )
        if depth.num_rows == 0:
            row = {
                "session_id": sid, "market": "NQ", "has_break": 1, "depth_ok": 0,
                "event_ts_ns": bk.event_ts_ns, "side": bk.side,
                "entry_px_pts": bk.entry_px_pts, "dop_pts": bk.dop_pts,
            }
            rows.append(row)
            new_rows.append(row)
            print(f"  [zone] {sid}: no depth", flush=True)
            continue
        f = compute_zone_features(
            depth, pair.nq,
            dop_ticks=bk.dop_ticks, side=bk.side, break_ts_ns=bk.event_ts_ns,
            inner_pts=inner, outer_pts=outer, traversal_cap_sec=cap,
            zone_entry_deadline_min=deadline, observe_min=observe,
            large_pct=float(zcfg.get("large_pct", 99.9)),
            large_min=int(zcfg.get("large_min", 15)),
        )
        row = {
            "session_id": sid, "market": "NQ", "has_break": 1,
            "event_ts_ns": bk.event_ts_ns, "side": bk.side,
            "entry_px_pts": bk.entry_px_pts, "dop_pts": bk.dop_pts,
            **f,
        }
        rows.append(row)
        new_rows.append(row)
        print(
            f"  [zone] {sid}: side={bk.side:+d} resting={f['zone_resting']} "
            f"trav={f['traversal_sec']} fast={f['fast_traversal']}",
            flush=True,
        )

    if new_rows:
        all_rows = list(cached.values()) + new_rows
        # keep only complete-ish dicts aligned to union of keys
        keys: list[str] = []
        for r in all_rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        cols = {}
        for k in keys:
            vals = [r.get(k) for r in all_rows]
            if k in ("session_id", "market"):
                cols[k] = pa.array([str(v) if v is not None else "" for v in vals])
            elif isinstance(next((v for v in vals if v is not None), 0), (int, np.integer)):
                cols[k] = pa.array([int(v) if v is not None else 0 for v in vals], pa.int64())
            else:
                cols[k] = pa.array(
                    [float(v) if v is not None else float("nan") for v in vals], pa.float64()
                )
        results_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(cols), cache_path, compression="zstd")
    return [r for r in rows if r.get("has_break")]


def zone_events(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if not r.get("depth_ok"):
            continue
        out.append(dict(r))
    return out


def confirm_events(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if not r.get("fast_traversal") or not r.get("confirm_ts_ns"):
            continue
        r2 = dict(r)
        r2["event_ts_ns"] = int(r["confirm_ts_ns"])
        r2["entry_px_pts"] = float(r["confirm_px_pts"])
        out.append(r2)
    return out


# ---------------------------------------------------------------- collect labels + sims

def collect(parquet_dir: Path, sids: list[str], zrows: list[dict], exp: dict,
            horizons, targets) -> dict:
    ev_rows = zone_events([r for r in zrows if r["session_id"] in set(sids)])
    by_sid = {r["session_id"]: r for r in ev_rows}
    conf_by_sid = {r["session_id"]: r for r in confirm_events(ev_rows)}
    lab_store: dict[str, list[pa.Table]] = {"A": [], "T": [], "S": [], "C": [], "null": []}
    sim_trades: dict[str, list] = {"A": [], "C": []}
    rng = np.random.default_rng(int(exp.get("placebo_seed", 79)))
    grace_ns = int(float((exp.get("dop_zone") or {}).get("grace_min", 5)) * 60 * 1e9)
    pairs_seen = []

    for sid in sids:
        if sid not in by_sid:
            continue
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        pairs_seen.append(sid)
        r = by_sid[sid]
        ev_a = events_to_table([r])
        sets = {
            "A": ev_a,
            "T": ev_a if r["fast_traversal"] else pa.table({}),
            "S": pa.table({}) if r["fast_traversal"] else ev_a,
            "C": events_to_table([conf_by_sid[sid]]) if sid in conf_by_sid else pa.table({}),
        }
        ts_all = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
        after = ts_all[ts_all >= pair.bounds[0] + grace_ns]
        null_rows = []
        if len(after):
            pick = rng.choice(after, size=min(20, len(after)), replace=False)
            for p in pick:
                null_rows.append({
                    "session_id": sid, "market": "NQ",
                    "event_ts_ns": int(p), "side": int(r["side"]),
                })
        sets["null"] = events_to_table(null_rows) if null_rows else pa.table({})
        for name, tbl in sets.items():
            if tbl.num_rows:
                lab = H.label_in_points(tbl, pair, horizons, targets, role="NQ")
                if lab.num_rows:
                    lab_store[name].append(lab)
        cfg = MatSimConfig(
            slip_ticks=float(exp.get("slip_ticks", 1.0)),
            commission_pts_rt=H.COMMISSION,
        )
        for arm, tbl in (("A", sets["A"]), ("C", sets["C"])):
            if tbl.num_rows == 0:
                continue
            s = simulate_session(
                sid, pair.nq, pair.es, cfg,
                vwap_from_ns=pair.bounds[0],
                bracket_fn=make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0),
                events=tbl,
            )
            sim_trades[arm].extend(s.trades)
        print(f"  [evt] {sid}: fast={r['fast_traversal']}", flush=True)
    return {"events": ev_rows, "labeled": lab_store, "sim_trades": sim_trades,
            "pairs_seen": pairs_seen}


def _fmt_cell(s: dict, targets) -> str:
    if not s or not s.get("n_events"):
        return "| 0 | – | – | – | – |"
    return (
        f"| {s['n_events']} | {s.get(f'p_hit_{int(targets[0])}pts', float('nan')):.2f} | "
        f"{s.get(f'p_hit_{int(targets[1])}pts', float('nan')):.2f} | "
        f"{H.fmt(s.get('mean_ret_pts_60000ms'), 2)} | {H.fmt(s.get('mean_ret_pts_300000ms'), 2)} |"
    )


def run(exp_path: Path | None = None, semi_oos: bool = False) -> dict:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / "EXP-0079"
    results_dir.mkdir(parents=True, exist_ok=True)
    exp = yaml.safe_load(
        (exp_path or Path("research/experiments/EXP-0079.yaml")).read_text(encoding="utf-8")
    )
    horizons = list(exp.get("horizons_ms") or [60000, 300000])
    targets = list(exp.get("point_targets_nq") or [5, 10, 20])
    exclude = set(exp.get("exclude_sessions") or [])
    disc_months = set(exp.get("discovery_months") or [])
    semi_months = set(exp.get("semi_oos_months") or [])
    kill = dict(exp.get("kill") or {})
    zcfg = dict(exp.get("dop_zone") or {})

    sids_all = all_sessions(parquet_dir, exclude)
    disc_ids = [s for s in sids_all if _month(s) in disc_months]
    semi_ids = [s for s in sids_all if _month(s) in semi_months]
    need_ids = disc_ids + (semi_ids if semi_oos else [])
    print(f"[0079] building DOP history for {len(need_ids)} sessions", flush=True)
    hist = compute_break_history(
        parquet_dir, sorted(set(need_ids), key=lambda s: (int(s[4:6]), int(s[2:4]), int(s[0:2]))),
        {"grace_min": float(zcfg.get("grace_min", 5)),
         "pierce_ticks": float(zcfg.get("pierce_ticks", 1)),
         "prior_sessions": 14},
    )
    print(f"[0079] zone features (depth) for discovery", flush=True)
    zrows_disc = build_zone_rows(parquet_dir, disc_ids, hist, exp, results_dir)

    report: dict = {"status": "ok", "experiment": exp["name"], "stages": {}}
    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        "Frozen design: `research/experiments/EXP-0079.yaml` / H70. "
        "NQ DOP breaks with depth; zone = DOP±10..20 pts beyond side; "
        "fast = entered ≤15 min, traversed ≤60 s; C enters at zone exit.",
        "",
    ]

    data = collect(parquet_dir, disc_ids, zrows_disc, exp, horizons, targets)
    events = data["events"]
    if events:
        pq.write_table(events_to_table(events), results_dir / "events.parquet", compression="zstd")

    # ---------------------------------------------------------- Stage 0
    n_breaks_all = len(zrows_disc)
    n_depth = len(events)
    n_fast = sum(int(r["fast_traversal"]) for r in events)
    n_long = sum(1 for r in events if r["side"] > 0)
    # causality: recompute 3 events with inputs truncated at break
    trunc_rows = []
    checked = 0
    for r in events:
        if checked >= 3:
            break
        pair = load_pair_full(parquet_dir, r["session_id"])
        if pair is None:
            continue
        open_ns, win_end = pair.bounds
        bk_ts = int(r["event_ts_ns"])
        depth = H.load_depth_window(
            parquet_dir, r["session_id"], "NQ",
            (open_ns - int(zcfg.get("depth_seed_min_before_open", 30)) * NS_MIN, bk_ts),
        )
        ts = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
        cut_trades = pair.nq.take(np.where(ts <= bk_ts)[0])
        # dop_ticks from pts
        f_cut = compute_zone_features(
            depth, cut_trades,
            dop_ticks=float(r["dop_pts"]) / 0.25, side=int(r["side"]), break_ts_ns=bk_ts,
            inner_pts=float(zcfg.get("zone_inner_pts", 10.0)),
            outer_pts=float(zcfg.get("zone_outer_pts", 20.0)),
        )
        match = (
            np.isfinite(f_cut["zone_resting"])
            and abs(f_cut["zone_resting"] - float(r["zone_resting"])) < 1e-6
            and abs(f_cut["stack_build_60s"] - float(r["stack_build_60s"])) < 1e-9
        )
        trunc_rows.append({"session": r["session_id"], "match": bool(match)})
        checked += 1
    trunc_ok = all(x["match"] for x in trunc_rows) if trunc_rows else False

    min_breaks = int(kill.get("min_depth_breaks", 40))
    min_fast = int(kill.get("min_fast_traversals", 15))
    power_ok = n_depth >= min_breaks and n_fast >= min_fast
    report["stages"]["0"] = {
        "n_breaks_all": n_breaks_all, "n_depth_breaks": n_depth,
        "n_fast": n_fast, "n_long": n_long, "n_short": n_depth - n_long,
        "truncation": trunc_rows, "truncation_ok": trunc_ok, "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — depth feasibility, causality, power",
        "",
        f"DOP breaks on discovery sessions: {n_breaks_all} · with usable depth: **{n_depth}** "
        f"(long {n_long} / short {n_depth - n_long}) · fast traversals: **{n_fast}**",
        f"Truncation invariance (zone at break): **{'PASS' if trunc_ok else 'FAIL'}** ({len(trunc_rows)} checks)",
        f"Power (depth breaks ≥ {min_breaks} and fast ≥ {min_fast}): **{'PASS' if power_ok else 'FAIL'}**",
        "",
        "Sample zone rows:",
        "",
        "| session | side | resting | stack_build_60s | trav_s | fast |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in events[:10]:
        md.append(
            f"| {r['session_id']} | {int(r['side']):+d} | {float(r['zone_resting']):.0f} | "
            f"{H.fmt(float(r['stack_build_60s']), 2)} | {H.fmt(float(r['traversal_sec']), 1)} | "
            f"{int(r['fast_traversal'])} |"
        )
    md += [""]
    if not trunc_ok:
        report["verdict"] = "reject — Stage 0 causality failed"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report
    if not power_ok:
        report["verdict"] = (
            "reject / park — Stage 0 underpowered (counts are the result; "
            "no event study, no P&L, no semi-OOS, no retune)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 1
    stage1 = {}
    md += [
        "## Stage 1 — event study (discovery, NQ)",
        "",
        "| set | n | hit5 | hit10 | @60s | @300s |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in ("A", "T", "S", "C", "null"):
        s = H.summarize(data["labeled"][name], horizons, targets)
        stage1[name] = s
        md.append(f"| {name} {_fmt_cell(s, targets)}")

    # Q1 strata on A (labeled A table)
    strata: dict = {}
    parts = data["labeled"]["A"]
    if parts:
        df = pa.concat_tables(parts, promote_options="default")
        if "zone_resting" in df.column_names:
            vals = df.column("zone_resting").to_numpy().astype(np.float64)
            fin = vals[np.isfinite(vals)]
            if len(fin) >= 9:
                q33, q66 = np.quantile(fin, [1 / 3, 2 / 3])
                for lab, mask in (
                    ("resting_lo", vals <= q33), ("resting_mid", (vals > q33) & (vals <= q66)),
                    ("resting_hi", vals > q66),
                ):
                    sub = _filter_table(df, mask)
                    if sub.num_rows:
                        strata[f"A_{lab}"] = H.summarize([sub], horizons, targets)
        if "stack_build_60s" in df.column_names:
            sb = df.column("stack_build_60s").to_numpy().astype(np.float64)
            for lab, mask in (("pulled", sb < 0), ("built", sb >= 0)):
                sub = _filter_table(df, np.isfinite(sb) & mask)
                if sub.num_rows:
                    strata[f"A_stack_{lab}"] = H.summarize([sub], horizons, targets)
        if "consume_frac" in df.column_names:
            cf = df.column("consume_frac").to_numpy().astype(np.float64)
            fin = cf[np.isfinite(cf)]
            if len(fin) >= 9:
                med = float(np.median(fin))
                for lab, mask in (("consume_lo", cf <= med), ("consume_hi", cf > med)):
                    sub = _filter_table(df, np.isfinite(cf) & mask)
                    if sub.num_rows:
                        strata[f"TplusS_{lab}"] = H.summarize([sub], horizons, targets)
    stage1["strata"] = strata
    md += ["", "Q1 / mechanism strata (descriptive):", "",
           "| stratum | n | hit5 | hit10 | @60s | @300s |", "|---|---:|---:|---:|---:|---:|"]
    for k, s in sorted(strata.items()):
        md.append(f"| {k} {_fmt_cell(s, targets)}")
    report["stages"]["1"] = stage1
    md += [""]

    # ---------------------------------------------------------- Stage 2
    tr_a = data["sim_trades"].get("A", [])
    tr_c = data["sim_trades"].get("C", [])
    sim_a, sim_c = sim_from_trades(tr_a), sim_from_trades(tr_c)
    avg_a, avg_c = _avg(sim_a), _avg(sim_c)
    placebo = _placebo_p90(
        tr_a, keep_n=sim_c.n,
        draws=int(exp.get("placebo_draws", 1000)),
        seed=int(exp.get("placebo_seed", 79)),
        commission=H.COMMISSION,
    )
    min_conf = int(kill.get("min_confirm_n", 15))
    checks = {
        "confirm_n_ok": sim_c.n >= min_conf,
        "avg_beats_a": bool(np.isfinite(avg_c) and np.isfinite(avg_a) and avg_c > avg_a),
        "net_positive": bool(sim_c.net > 0),
        "placebo_ok": bool(
            np.isfinite(avg_c) and np.isfinite(placebo["p90_avg"]) and avg_c > placebo["p90_avg"]
        ),
    }
    stage2_pass = all(checks.values())
    report["stages"]["2"] = {
        "armA": {**sim_a.to_dict(), "avg": avg_a},
        "armC": {**sim_c.to_dict(), "avg": avg_c},
        "placebo": placebo, "checks": checks, "pass": stage2_pass,
    }
    md += [
        "## Stage 2 — P&L Book 2 vol RR (discovery)",
        "",
        f"Arm A (DOP entry): n={sim_a.n} net={sim_a.net:+.1f} avg={avg_a:+.2f}",
        f"Arm C (zone-exit confirm): n={sim_c.n} net={sim_c.net:+.1f} avg={avg_c:+.2f}",
        f"Random-skip placebo p90 avg (keep C n from A): {placebo['p90_avg']:+.2f}",
        f"Checks: {checks}",
        "",
        f"**Stage 2: {'PASS' if stage2_pass else 'FAIL'}**",
        "",
    ]
    if not stage2_pass:
        report["verdict"] = "reject / park — Stage 2 discovery P&L fail (no semi-OOS read, no retune)"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    if not semi_oos:
        report["verdict"] = (
            "discovery PASS — frozen; Sep-Oct semi-OOS not yet read. "
            "Re-run with --semi-oos to read it (semi-OOS, not clean holdout)."
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 3 semi-OOS
    zrows_semi = build_zone_rows(parquet_dir, semi_ids, hist, exp, results_dir)
    sdata = collect(parquet_dir, semi_ids, zrows_semi, exp, horizons, targets)
    if sdata["events"]:
        pq.write_table(
            events_to_table(sdata["events"]),
            results_dir / "semi_oos_events.parquet", compression="zstd",
        )
    s_tr_a = sdata["sim_trades"].get("A", [])
    s_tr_c = sdata["sim_trades"].get("C", [])
    s_sim_a, s_sim_c = sim_from_trades(s_tr_a), sim_from_trades(s_tr_c)
    s_avg_a, s_avg_c = _avg(s_sim_a), _avg(s_sim_c)
    semi_pass = bool(
        s_sim_c.n >= 5 and np.isfinite(s_avg_c) and np.isfinite(s_avg_a)
        and s_avg_c > s_avg_a and s_sim_c.net > 0
    )
    report["stages"]["3"] = {
        "semi_oos": True,
        "armA": {**s_sim_a.to_dict(), "avg": s_avg_a},
        "armC": {**s_sim_c.to_dict(), "avg": s_avg_c},
        "pass": semi_pass,
    }
    md += [
        "## Stage 3 — Sep-Oct 2026 semi-OOS (not a clean holdout)",
        "",
        f"Arm A: n={s_sim_a.n} net={s_sim_a.net:+.1f} avg={s_avg_a:+.2f} · "
        f"Arm C: n={s_sim_c.n} net={s_sim_c.net:+.1f} avg={s_avg_c:+.2f}",
        "",
        f"**Stage 3 semi-OOS: {'PASS' if semi_pass else 'FAIL'}**",
        "",
    ]
    report["verdict"] = (
        "discovery + semi-OOS PASS — pending virgin-month validation (separate phase)"
        if semi_pass else
        "reject / park — Stage 3 semi-OOS fail (no retune)"
    )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    H.write_report(results_dir, report, md)
    _write_verdict_docs(report["verdict"])
    print(report["verdict"], flush=True)
    return report


def _write_verdict_docs(verdict: str) -> None:
    hyp = Path("docs/HYPOTHESES.md")
    text = hyp.read_text(encoding="utf-8")
    old = "**Result:** pending."
    # only replace the H70 one: it is the last occurrence before Unregistered
    idx = text.rfind(old)
    if idx >= 0:
        text = text[:idx] + f"**Result (EXP-0079): {verdict}.** See `results/EXP-0079/`." + text[idx + len(old):]
        hyp.write_text(text, encoding="utf-8")
    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    lt = lt.replace(
        "| EXP-0079 | H70 DOP beyond-zone traversal (depth) | registered — pending run | Apr–Aug depth discovery; Sep–Oct semi-OOS | NQ zone DOP±10..20; fast traversal ≤60s → confirm entry at ±20; see notes |",
        f"| EXP-0079 | H70 DOP beyond-zone traversal (depth) | ran — **{verdict.split('—')[0].strip()}** | Apr–Aug depth discovery | {verdict}; see notes |",
        1,
    )
    note = (
        f"\n### 2026-10-27 — EXP-0079 DOP zone traversal run (H70)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0079/`.\n"
        f"- Runner: `python -m research.dop.exp_0079_zone`.\n"
    )
    if "### 2026-10-27 — EXP-0079 DOP zone traversal run" not in lt:
        anchor = "### 2026-10-27 — EXP-0079 DOP beyond-zone traversal registered (H70)"
        idx = lt.find(anchor)
        nxt = lt.find("\n### ", idx + 5) if idx >= 0 else -1
        if nxt > 0:
            lt = lt[:nxt] + note + lt[nxt:]
        else:
            lt = lt + note
    log.write_text(lt, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--semi-oos", action="store_true",
                    help="read Sep-Oct semi-OOS (only after a discovery pass)")
    ap.add_argument("--exp", type=Path, default=None)
    args = ap.parse_args()
    run(args.exp, semi_oos=args.semi_oos)


if __name__ == "__main__":
    main()
