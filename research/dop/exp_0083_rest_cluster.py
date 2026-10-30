"""EXP-0083 / H74 — DOP break compact resting cluster, trap side (NQ, depth).

Stages (frozen in research/experiments/EXP-0083.yaml):
  Stage 0  coverage + truncation causality + power
  Stage 1  event study A (all depth breaks) / R (cluster_share_pre >= 0.50)
           vs null + descriptive splits
  Stage 2  Book 2 P&L gate R vs A (+ fixed 20/40 asym secondary)
  Stage 3  Sep-Oct semi-holdout with --semi-oos after discovery pass

Usage: python -m research.dop.exp_0083_rest_cluster [--semi-oos]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.backtest.mat_sim import MatSimConfig, SimSummary, make_vol_bracket_rr, simulate_session
from research.config import load_config
from research.correlated import harness as H
from research.dop.exp_0078_dop import _month, all_sessions, compute_break_history, load_pair_full
from research.dop.exp_0079_zone import build_zone_rows, zone_events
from research.features.dop_rest_cluster import compute_rest_cluster_features

NS_MIN = 60_000_000_000

FLOATS = [
    "entry_px_pts", "dop_pts",
    "cluster_share_pre", "cluster_size_pre", "cluster_zone_resting",
    "cluster_lo_pts", "cluster_hi_pts", "cluster_dist_dop_pts",
    "cluster_max_level", "cluster_snap_age_sec",
    "cluster_share_break", "cluster_remaining_frac", "sup_share_pre",
    "rest_max_pullback_pts",
]
INTS = [
    "event_ts_ns", "side", "cluster_n_levels", "cluster_persist",
    "rest_pb_ok", "rest_n_pullbacks", "arm_r",
]
ARMS = ("A", "R")


def events_to_table(rows: list[dict]) -> pa.Table:
    if not rows:
        return pa.table({})
    cols: dict[str, pa.Array] = {}
    cols["event_ts_ns"] = pa.array([int(r["event_ts_ns"]) for r in rows], pa.int64())
    cols["side"] = pa.array([int(r["side"]) for r in rows], pa.int8())
    for name in FLOATS:
        cols[name] = pa.array([float(r.get(name, np.nan)) for r in rows], pa.float64())
    for name in INTS:
        if name in ("event_ts_ns", "side"):
            continue
        cols[name] = pa.array([int(r.get(name, 0) or 0) for r in rows], pa.int64())
    cols["session_id"] = pa.array([r["session_id"] for r in rows])
    cols["market"] = pa.array([r["market"] for r in rows])
    return pa.table(cols)


def _filter_table(t: pa.Table, mask: np.ndarray) -> pa.Table:
    if t.num_rows == 0:
        return t
    idx = np.where(mask)[0]
    return t.take(idx) if len(idx) else t.slice(0, 0)


def sim_from_trades(trades: list) -> SimSummary:
    from research.backtest.mat_sim import _rebuild_summary
    return _rebuild_summary(list(trades), H.COMMISSION)


def _avg(s: SimSummary) -> float:
    return (s.net / s.n) if s.n else float("nan")


def _sorted_ids(sids):
    return sorted(set(sids), key=lambda s: (int(s[4:6]), int(s[2:4]), int(s[0:2])))


# ---------------------------------------------------------------- rows

def build_rest_rows(parquet_dir: Path, sids: list[str], hist: dict,
                    zone_rows: list[dict], exp: dict) -> list[dict]:
    rcfg = dict(exp.get("rest_cluster") or {})
    zone_by_sid = {r["session_id"]: r for r in zone_events(zone_rows)}
    rows: list[dict] = []
    for sid in sids:
        zr = zone_by_sid.get(sid)
        if zr is None:
            continue
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        open_ns, _ = pair.bounds
        bk_ts = int(zr["event_ts_ns"])
        depth = H.load_depth_window(
            parquet_dir, sid, "NQ",
            (open_ns - int(rcfg.get("depth_seed_min_before_open", 30)) * NS_MIN, bk_ts),
        )
        f = compute_rest_cluster_features(
            depth, pair.nq,
            dop_ticks=float(zr["dop_pts"]) / 0.25,
            side=int(zr["side"]), break_ts_ns=bk_ts,
            zone_pts=float(rcfg.get("zone_pts", 30.0)),
            band_pts=float(rcfg.get("band_pts", 5.0)),
            share_min=float(rcfg.get("share_min", 0.50)),
            snapshot_offsets_sec=tuple(
                int(x) for x in (rcfg.get("snapshot_offsets_sec") or [300, 180, 60])
            ),
            pullback_pts=float(rcfg.get("pullback_pts", 2.0)),
        )
        if not np.isfinite(f["cluster_share_pre"]):
            continue
        rows.append({
            "session_id": sid, "market": "NQ",
            "event_ts_ns": bk_ts, "side": int(zr["side"]),
            "entry_px_pts": float(zr["entry_px_pts"]), "dop_pts": float(zr["dop_pts"]),
            **f,
        })
        print(
            f"  [rest] {sid}: side={zr['side']:+d} share={f['cluster_share_pre']:.2f} "
            f"size={f['cluster_size_pre']:.0f} persist={f['cluster_persist']} R={f['arm_r']}",
            flush=True,
        )
    return rows


# ---------------------------------------------------------------- collect

def collect(parquet_dir: Path, sids: list[str], rows: list[dict], exp: dict,
            horizons, targets) -> dict:
    by_sid = {r["session_id"]: r for r in rows if r["session_id"] in set(sids)}
    lab_store: dict[str, list[pa.Table]] = {"A": [], "R": [], "null": []}
    sim_trades: dict[tuple[str, str], list] = {}
    asym = dict(exp.get("asym_nq") or {"sl_pts": 20.0, "tp_pts": 40.0})
    rng = np.random.default_rng(int(exp.get("placebo_seed", 83)))
    grace_ns = int(float((exp.get("rest_cluster") or {}).get("grace_min", 5)) * 60 * 1e9)

    for sid in sids:
        if sid not in by_sid:
            continue
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        r = by_sid[sid]
        ev = events_to_table([r])
        sets = {"A": ev, "R": ev if r["arm_r"] else pa.table({})}
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
        for arm, tbl in sets.items():
            if arm == "null" or tbl.num_rows == 0:
                continue
            s2 = simulate_session(
                sid, pair.nq, pair.es, cfg, vwap_from_ns=pair.bounds[0],
                bracket_fn=make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0),
                events=tbl,
            )
            sim_trades.setdefault((arm, "book2"), []).extend(s2.trades)
            ev_asym = tbl.append_column(
                "tp_pts", pa.array([float(asym["tp_pts"])] * tbl.num_rows, pa.float64())
            ).append_column(
                "sl_pts", pa.array([float(asym["sl_pts"])] * tbl.num_rows, pa.float64())
            )
            sa = simulate_session(
                sid, pair.nq, pair.es, cfg, vwap_from_ns=pair.bounds[0],
                events=ev_asym,
            )
            sim_trades.setdefault((arm, "asym"), []).extend(sa.trades)
        print(f"  [evt] {sid}: R={r['arm_r']}", flush=True)
    return {"events": list(by_sid.values()), "labeled": lab_store, "sim_trades": sim_trades}


def _strata(data: dict, horizons, targets) -> list[tuple[str, dict]]:
    parts = [p for p in data["labeled"]["A"] if p.num_rows]
    if not parts:
        return []
    cols = set(parts[0].column_names)
    for p in parts[1:]:
        cols &= set(p.column_names)
    keep = [c for c in parts[0].column_names if c in cols]
    df = pa.concat_tables([p.select(keep) for p in parts], promote_options="default")
    out: list[tuple[str, dict]] = []

    def add(name, mask):
        sub = _filter_table(df, mask)
        if sub.num_rows:
            out.append((name, H.summarize([sub], horizons, targets)))

    def terciles(col, prefix):
        v = df.column(col).to_numpy().astype(np.float64)
        ok = np.isfinite(v)
        if ok.sum() < 9:
            return
        q1, q2 = np.quantile(v[ok], [1 / 3, 2 / 3])
        add(f"{prefix}_lo", ok & (v <= q1))
        add(f"{prefix}_mid", ok & (v > q1) & (v <= q2))
        add(f"{prefix}_hi", ok & (v > q2))

    terciles("cluster_share_pre", "share")
    terciles("cluster_size_pre", "size")
    terciles("sup_share_pre", "sup_share")
    terciles("cluster_dist_dop_pts", "dist")
    r = df.column("arm_r").to_numpy().astype(np.int64) == 1
    pb = df.column("rest_n_pullbacks").to_numpy().astype(np.int64)
    add("R_pb_le1", r & (pb <= 1))
    add("R_pb_ge2", r & (pb >= 2))
    persist = df.column("cluster_persist").to_numpy().astype(np.int64)
    add("R_persist_ge2", r & (persist >= 2))
    rem = df.column("cluster_remaining_frac").to_numpy().astype(np.float64)
    add("R_remaining_lt05", r & np.isfinite(rem) & (rem < 0.5))
    add("R_remaining_ge05", r & np.isfinite(rem) & (rem >= 0.5))
    side = df.column("side").to_numpy().astype(np.int64)
    add("R_long", r & (side > 0))
    add("R_short", r & (side < 0))
    return out


def _books(data: dict) -> tuple[dict, list[str]]:
    books: dict = {}
    lines: list[str] = []
    for book in ("book2", "asym"):
        for arm in ARMS:
            sim = sim_from_trades(data["sim_trades"].get((arm, book), []))
            books.setdefault(book, {})[arm] = {**sim.to_dict(), "avg": _avg(sim)}
            lines.append(f"- {book} {arm}: n={sim.n} net={sim.net:+.1f} avg={_avg(sim):+.2f}")
    return books, lines


def run(exp_path: Path | None = None, semi_oos: bool = False) -> dict:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    results_root = Path(cfg["results_dir"])
    results_dir = results_root / "EXP-0083"
    results_dir.mkdir(parents=True, exist_ok=True)
    zone_cache_dir = results_root / "EXP-0079"
    exp = yaml.safe_load(
        (exp_path or Path("research/experiments/EXP-0083.yaml")).read_text(encoding="utf-8")
    )
    horizons = list(exp.get("horizons_ms") or [60000, 300000])
    targets = list(exp.get("point_targets_nq") or [5, 10, 20])
    exclude = set(exp.get("exclude_sessions") or [])
    disc_months = set(exp.get("discovery_months") or [])
    semi_months = set(exp.get("semi_oos_months") or [])
    kill = dict(exp.get("kill") or {})
    rcfg = dict(exp.get("rest_cluster") or {})

    sids_all = all_sessions(parquet_dir, exclude)
    disc_ids = [s for s in sids_all if _month(s) in disc_months]
    semi_ids = [s for s in sids_all if _month(s) in semi_months]
    need = disc_ids + (semi_ids if semi_oos else [])
    print(f"[0083] DOP history + zone rows for {len(need)} sessions", flush=True)
    hist = compute_break_history(
        parquet_dir, _sorted_ids(need),
        {"grace_min": float(rcfg.get("grace_min", 5)),
         "pierce_ticks": float(rcfg.get("pierce_ticks", 1)),
         "prior_sessions": 14},
    )
    zone_exp = {"dop_zone": {
        "grace_min": 5, "pierce_ticks": 1, "zone_inner_pts": 10.0,
        "zone_outer_pts": 20.0, "traversal_cap_sec": 60,
        "zone_entry_deadline_min": 15, "traversal_observe_min": 30,
        "depth_seed_min_before_open": 30, "large_pct": 99.9, "large_min": 15,
    }}
    zrows_disc = build_zone_rows(parquet_dir, disc_ids, hist, zone_exp, zone_cache_dir)
    rows_disc = build_rest_rows(parquet_dir, disc_ids, hist, zrows_disc, exp)

    report: dict = {"status": "ok", "experiment": exp["name"], "stages": {}}
    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        "Frozen design: `research/experiments/EXP-0083.yaml` / H74. "
        "Cluster = best <=5pt trap-zone (30pt) resting band at break-300/-180/-60s; "
        "R = max pre-break share >= 0.50.",
        "",
    ]

    data = collect(parquet_dir, disc_ids, rows_disc, exp, horizons, targets)
    events = data["events"]
    if events:
        pq.write_table(events_to_table(events), results_dir / "events.parquet", compression="zstd")

    # ---------------------------------------------------------- Stage 0
    n_a = len(events)
    n_r = sum(int(r["arm_r"]) for r in events)
    trunc_rows = []
    for r in events[:3]:
        pair = load_pair_full(parquet_dir, r["session_id"])
        if pair is None:
            continue
        open_ns, _ = pair.bounds
        bk_ts = int(r["event_ts_ns"])
        depth = H.load_depth_window(
            parquet_dir, r["session_id"], "NQ",
            (open_ns - int(rcfg.get("depth_seed_min_before_open", 30)) * NS_MIN, bk_ts),
        )
        dts = depth.column("ts_ns").to_numpy().astype(np.int64) if depth.num_rows else np.array([], dtype=np.int64)
        cut_d = depth.take(np.where(dts <= bk_ts)[0]) if depth.num_rows else depth
        tts = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
        cut_t = pair.nq.take(np.where(tts <= bk_ts)[0])
        f = compute_rest_cluster_features(
            cut_d, cut_t, dop_ticks=float(r["dop_pts"]) / 0.25,
            side=int(r["side"]), break_ts_ns=bk_ts,
            zone_pts=float(rcfg.get("zone_pts", 30.0)),
            band_pts=float(rcfg.get("band_pts", 5.0)),
            share_min=float(rcfg.get("share_min", 0.50)),
            snapshot_offsets_sec=tuple(
                int(x) for x in (rcfg.get("snapshot_offsets_sec") or [300, 180, 60])
            ),
            pullback_pts=float(rcfg.get("pullback_pts", 2.0)),
        )
        match = (
            int(f["arm_r"]) == int(r["arm_r"])
            and abs(float(f["cluster_share_pre"]) - float(r["cluster_share_pre"])) < 1e-9
        )
        trunc_rows.append({"session": r["session_id"], "match": bool(match)})
    trunc_ok = all(x["match"] for x in trunc_rows) if trunc_rows else False
    power_ok = n_a >= int(kill.get("min_arm_a", 30)) and n_r >= int(kill.get("min_gate_n", 10))
    report["stages"]["0"] = {
        "n_depth_breaks": n_a, "n_r": n_r,
        "truncation": trunc_rows, "truncation_ok": trunc_ok, "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — coverage, causality, power",
        "",
        f"Depth DOP breaks: {n_a} · **R (compact resting) {n_r}**",
        f"Truncation invariance: **{'PASS' if trunc_ok else 'FAIL'}** ({len(trunc_rows)} checks) · "
        f"power: **{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    if not trunc_ok or not power_ok:
        why = "Stage 0 causality failed" if not trunc_ok else (
            "Stage 0 underpowered (counts are the result; no retune)"
        )
        report["verdict"] = f"reject / park — {why}"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 1
    stage1 = {}
    md += [
        "## Stage 1 — event study (discovery, NQ, from DOP entry)",
        "",
        "| set | n | hit5 | hit10 | @60s | @300s | @900s |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("A", "R", "null"):
        s = H.summarize(data["labeled"][name], horizons, targets)
        stage1[name] = s
        if s.get("n_events"):
            md.append(
                f"| {name} | {s['n_events']} | "
                f"{s.get('p_hit_5pts', float('nan')):.2f} | {s.get('p_hit_10pts', float('nan')):.2f} | "
                f"{H.fmt(s.get('mean_ret_pts_60000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_300000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_900000ms'), 2)} |"
            )
        else:
            md.append(f"| {name} | 0 | – | – | – | – | – |")
    strata = _strata(data, horizons, targets)
    stage1["strata"] = {name: s for name, s in strata}
    md += ["", "### Descriptive splits (never gates)", "",
           "| stratum | n | hit10 | @60s | @300s | @900s |",
           "|---|---:|---:|---:|---:|---:|"]
    for name, s in strata:
        if s.get("n_events"):
            md.append(
                f"| {name} | {s['n_events']} | {s.get('p_hit_10pts', float('nan')):.2f} | "
                f"{H.fmt(s.get('mean_ret_pts_60000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_300000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_900000ms'), 2)} |"
            )
    report["stages"]["1"] = stage1
    md += [""]

    # ---------------------------------------------------------- Stage 2
    books, blines = _books(data)
    md += ["## Stage 2 — P&L at DOP entry (discovery)", ""] + blines + [""]
    lift_min = float(kill.get("min_avg_lift_pts", 1.0))
    b2 = books["book2"]
    checks = {
        "gate_n_ok": b2["R"]["n"] >= int(kill.get("min_gate_n", 10)),
        "avg_lift_ok": bool(b2["R"]["avg"] >= b2["A"]["avg"] + lift_min),
        "net_positive": bool(b2["R"]["net"] > 0),
    }
    stage_pass = all(checks.values())
    report["stages"]["2"] = {"books": books, "checks": checks, "pass": stage_pass}
    md += [f"Gate checks (Book 2): {checks}", "",
           f"**Stage 2: {'PASS' if stage_pass else 'FAIL'}**", ""]
    if not stage_pass:
        report["verdict"] = (
            "reject / park — Stage 2 discovery P&L fail (no semi read, no retune)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    if not semi_oos:
        report["verdict"] = (
            "discovery PASS — frozen; Sep-Oct semi-holdout not yet read. "
            "Re-run with --semi-oos once."
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 3 semi
    zrows_semi = build_zone_rows(parquet_dir, semi_ids, hist, zone_exp, zone_cache_dir)
    rows_semi = build_rest_rows(parquet_dir, semi_ids, hist, zrows_semi, exp)
    sdata = collect(parquet_dir, semi_ids, rows_semi, exp, horizons, targets)
    if sdata["events"]:
        pq.write_table(
            events_to_table(sdata["events"]),
            results_dir / "semi_oos_events.parquet", compression="zstd",
        )
    s_books, s_lines = _books(sdata)
    sb2 = s_books["book2"]
    semi_pass = bool(
        sb2["R"]["n"] >= 5 and sb2["R"]["avg"] > sb2["A"]["avg"] and sb2["R"]["net"] > 0
    )
    report["stages"]["3"] = {"semi_oos": True, "books": s_books, "pass": semi_pass}
    md += ["## Stage 3 — Sep-Oct semi-holdout (one read)", ""] + s_lines + [
        "",
        f"**Stage 3: {'PASS' if semi_pass else 'FAIL'}**",
        "",
    ]
    report["verdict"] = (
        "discovery + semi-holdout PASS — pending virgin-month validation (separate phase)"
        if semi_pass else
        "reject / park — Stage 3 semi-holdout fail (no retune)"
    )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    H.write_report(results_dir, report, md)
    _write_verdict_docs(report["verdict"])
    print(report["verdict"], flush=True)
    return report


def _write_verdict_docs(verdict: str) -> None:
    hyp = Path("docs/HYPOTHESES.md")
    text = hyp.read_text(encoding="utf-8")
    idx = text.rfind("**Result:** pending.")
    if idx >= 0:
        text = (
            text[:idx]
            + f"**Result (EXP-0083): {verdict}.** See `results/EXP-0083/`."
            + text[idx + len("**Result:** pending."):]
        )
        hyp.write_text(text, encoding="utf-8")
    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    lt = lt.replace(
        "| EXP-0083 | H74 DOP compact resting cluster (depth) | registered — pending run | Apr–Aug depth discovery; Sep–Oct semi once | trap-zone ≤5pt resting band ≥50% of zone resting in last 5 min; see notes |",
        f"| EXP-0083 | H74 DOP compact resting cluster (depth) | ran — **{verdict.split('—')[0].strip()}** | Apr–Aug depth discovery | {verdict}; see notes |",
        1,
    )
    note = (
        f"\n### 2026-10-30 — EXP-0083 DOP resting cluster run (H74)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0083/`.\n"
        f"- Runner: `python -m research.dop.exp_0083_rest_cluster`.\n"
    )
    if "### 2026-10-30 — EXP-0083 DOP resting cluster run" not in lt:
        anchor = "### 2026-10-30 — EXP-0083 DOP compact resting cluster registered (H74)"
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
                    help="read Sep-Oct semi-holdout once (only after discovery pass)")
    ap.add_argument("--exp", type=Path, default=None)
    args = ap.parse_args()
    run(args.exp, semi_oos=args.semi_oos)


if __name__ == "__main__":
    main()
