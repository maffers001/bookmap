"""EXP-0081 / H72 — DOP break pre-break final-approach chew (NQ, depth).

Stages (frozen in research/experiments/EXP-0081.yaml):
  Stage 0  approach coverage + truncation causality + power
  Stage 1  event study A (all) / F (approach fast) / S (approach slow)
  Stage 2  Book 2 P&L gate F vs A (+ fixed 20/40 asym secondary)
  Stage 3  Sep-Oct semi-holdout with --semi-oos after discovery pass

Usage: python -m research.dop.exp_0081_approach [--semi-oos]
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
from research.features.dop_zone import compute_approach_features

NS_MIN = 60_000_000_000

FLOATS = [
    "entry_px_pts", "dop_pts", "approach_sec", "approach_speed_pts_s",
    "approach_resting", "approach_resting_max", "executed_into_approach",
    "approach_consume_frac", "pullback_in_approach_pts",
    "approach_path_efficiency", "post_traversal_sec",
]
INTS = [
    "event_ts_ns", "side", "approach_reached", "approach_fast",
    "post_fast", "zone_resting_ok",
]


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


# ---------------------------------------------------------------- approach rows (cached)

def build_approach_rows(parquet_dir: Path, sids: list[str], hist: dict,
                        zone_rows: list[dict], exp: dict, results_dir: Path) -> list[dict]:
    acfg = dict(exp.get("approach") or {})
    zone_by_sid = {r["session_id"]: r for r in zone_events(zone_rows)}
    cache_path = results_dir / "_cache_approach_rows.parquet"
    cached: dict[str, dict] = {}
    if cache_path.exists():
        ct = pq.read_table(cache_path)
        for i in range(ct.num_rows):
            row = {n: ct.column(n)[i].as_py() for n in ct.column_names}
            cached[str(row["session_id"])] = row

    rows: list[dict] = []
    new_rows: list[dict] = []
    for sid in sids:
        zr = zone_by_sid.get(sid)
        if zr is None:
            continue
        if sid in cached:
            # refresh post-break fields from current zone row
            r = dict(cached[sid])
            r["post_fast"] = int(zr.get("fast_traversal") or 0)
            r["post_traversal_sec"] = float(zr.get("traversal_sec", np.nan))
            rows.append(r)
            continue
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        open_ns, _ = pair.bounds
        bk_ts = int(zr["event_ts_ns"])
        depth = H.load_depth_window(
            parquet_dir, sid, "NQ",
            (open_ns - int(acfg.get("depth_seed_min_before_open", 30)) * NS_MIN, bk_ts),
        )
        f = compute_approach_features(
            depth, pair.nq,
            dop_ticks=float(zr["dop_pts"]) / 0.25,
            side=int(zr["side"]), break_ts_ns=bk_ts,
            zone_pts=float(acfg.get("zone_pts", 20.0)),
            fast_cap_sec=float(acfg.get("fast_cap_sec", 60)),
            large_pct=float(acfg.get("large_pct", 99.9)),
            large_min=int(acfg.get("large_min", 15)),
        )
        row = {
            "session_id": sid, "market": "NQ",
            "event_ts_ns": bk_ts, "side": int(zr["side"]),
            "entry_px_pts": float(zr["entry_px_pts"]), "dop_pts": float(zr["dop_pts"]),
            "post_fast": int(zr.get("fast_traversal") or 0),
            "post_traversal_sec": float(zr.get("traversal_sec", np.nan)),
            "zone_resting_ok": 1,
            **f,
        }
        rows.append(row)
        new_rows.append(row)
        print(
            f"  [approach] {sid}: reached={f['approach_reached']} "
            f"sec={f['approach_sec']} fast={f['approach_fast']}",
            flush=True,
        )

    if new_rows:
        all_rows = list(cached.values()) + [
            {k: v for k, v in r.items()} for r in new_rows
        ]
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
    return rows


# ---------------------------------------------------------------- collect

def collect(parquet_dir: Path, sids: list[str], arows: list[dict], exp: dict,
            horizons, targets) -> dict:
    by_sid = {r["session_id"]: r for r in arows if r["session_id"] in set(sids)}
    lab_store: dict[str, list[pa.Table]] = {"A": [], "F": [], "S": [], "null": []}
    sim_trades: dict[tuple[str, str], list] = {}
    asym = dict(exp.get("asym_nq") or {"sl_pts": 20.0, "tp_pts": 40.0})
    rng = np.random.default_rng(int(exp.get("placebo_seed", 81)))
    grace_ns = int(float((exp.get("approach") or {}).get("grace_min", 5)) * 60 * 1e9)

    for sid in sids:
        if sid not in by_sid:
            continue
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        r = by_sid[sid]
        ev = events_to_table([r])
        sets = {
            "A": ev,
            "F": ev if r["approach_fast"] else pa.table({}),
            "S": ev if (r["approach_reached"] and not r["approach_fast"]) else pa.table({}),
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
        for arm, tbl in (("A", sets["A"]), ("F", sets["F"])):
            if tbl.num_rows == 0:
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
        print(f"  [evt] {sid}: fast={r['approach_fast']} reached={r['approach_reached']}", flush=True)
    return {"events": list(by_sid.values()), "labeled": lab_store, "sim_trades": sim_trades}


def run(exp_path: Path | None = None, semi_oos: bool = False) -> dict:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    results_root = Path(cfg["results_dir"])
    results_dir = results_root / "EXP-0081"
    results_dir.mkdir(parents=True, exist_ok=True)
    zone_cache_dir = results_root / "EXP-0079"
    exp = yaml.safe_load(
        (exp_path or Path("research/experiments/EXP-0081.yaml")).read_text(encoding="utf-8")
    )
    horizons = list(exp.get("horizons_ms") or [60000, 300000])
    targets = list(exp.get("point_targets_nq") or [5, 10, 20])
    exclude = set(exp.get("exclude_sessions") or [])
    disc_months = set(exp.get("discovery_months") or [])
    semi_months = set(exp.get("semi_oos_months") or [])
    kill = dict(exp.get("kill") or {})
    acfg = dict(exp.get("approach") or {})

    sids_all = all_sessions(parquet_dir, exclude)
    disc_ids = [s for s in sids_all if _month(s) in disc_months]
    semi_ids = [s for s in sids_all if _month(s) in semi_months]
    need = disc_ids + (semi_ids if semi_oos else [])
    print(f"[0081] DOP history + zone rows for {len(need)} sessions", flush=True)
    hist = compute_break_history(
        parquet_dir, _sorted_ids(need),
        {"grace_min": float(acfg.get("grace_min", 5)),
         "pierce_ticks": float(acfg.get("pierce_ticks", 1)),
         "prior_sessions": 14},
    )
    # zone rows provide the depth universe + post-break concordance fields
    zone_exp = {"dop_zone": {
        "grace_min": 5, "pierce_ticks": 1, "zone_inner_pts": 10.0,
        "zone_outer_pts": 20.0, "traversal_cap_sec": 60,
        "zone_entry_deadline_min": 15, "traversal_observe_min": 30,
        "depth_seed_min_before_open": 30, "large_pct": 99.9, "large_min": 15,
    }}
    zrows_disc = build_zone_rows(parquet_dir, disc_ids, hist, zone_exp, zone_cache_dir)
    arows_disc = build_approach_rows(parquet_dir, disc_ids, hist, zrows_disc, exp, results_dir)

    report: dict = {"status": "ok", "experiment": exp["name"], "stages": {}}
    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        "Frozen design: `research/experiments/EXP-0081.yaml` / H72. "
        "Final 20pt approach into DOP; fast = ≤60 s; gate at DOP entry.",
        "",
    ]

    data = collect(parquet_dir, disc_ids, arows_disc, exp, horizons, targets)
    events = data["events"]
    if events:
        pq.write_table(events_to_table(events), results_dir / "events.parquet", compression="zstd")

    # ---------------------------------------------------------- Stage 0
    n_depth = len(events)
    n_reached = sum(int(r["approach_reached"]) for r in events)
    n_fast = sum(int(r["approach_fast"]) for r in events)
    n_slow = n_reached - n_fast
    # causality: recompute approach for 3 events on break-truncated inputs
    trunc_rows = []
    for r in events[:3]:
        pair = load_pair_full(parquet_dir, r["session_id"])
        if pair is None:
            continue
        open_ns, _ = pair.bounds
        bk_ts = int(r["event_ts_ns"])
        depth = H.load_depth_window(
            parquet_dir, r["session_id"], "NQ",
            (open_ns - int(acfg.get("depth_seed_min_before_open", 30)) * NS_MIN, bk_ts),
        )
        ts = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
        cut = pair.nq.take(np.where(ts <= bk_ts)[0])
        f = compute_approach_features(
            depth, cut, dop_ticks=float(r["dop_pts"]) / 0.25,
            side=int(r["side"]), break_ts_ns=bk_ts,
            zone_pts=float(acfg.get("zone_pts", 20.0)),
            fast_cap_sec=float(acfg.get("fast_cap_sec", 60)),
        )
        match = (
            int(f["approach_reached"]) == int(r["approach_reached"])
            and int(f["approach_fast"]) == int(r["approach_fast"])
            and (
                not np.isfinite(f["approach_sec"])
                or abs(float(f["approach_sec"]) - float(r["approach_sec"])) < 1e-9
            )
        )
        trunc_rows.append({"session": r["session_id"], "match": bool(match)})
    trunc_ok = all(x["match"] for x in trunc_rows) if trunc_rows else False
    power_ok = (
        n_reached >= int(kill.get("min_approach_reached", 30))
        and n_fast >= int(kill.get("min_fast", 10))
        and n_slow >= int(kill.get("min_slow", 10))
    )
    report["stages"]["0"] = {
        "n_depth_breaks": n_depth, "n_reached": n_reached,
        "n_fast": n_fast, "n_slow": n_slow, "n_no_approach": n_depth - n_reached,
        "truncation": trunc_rows, "truncation_ok": trunc_ok, "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — coverage, causality, power",
        "",
        f"Depth DOP breaks: {n_depth} · approach reached (≥20pt excursion): **{n_reached}** "
        f"(fast {n_fast} / slow {n_slow} / no-approach {n_depth - n_reached})",
        f"Truncation invariance: **{'PASS' if trunc_ok else 'FAIL'}** ({len(trunc_rows)} checks) · "
        f"power: **{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    if not trunc_ok:
        report["verdict"] = "reject — Stage 0 causality failed"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report
    if not power_ok:
        report["verdict"] = (
            "reject / park — Stage 0 underpowered (counts are the result; no retune)"
        )
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
    for name in ("A", "F", "S", "null"):
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
    # concordance with post-break fast
    reached = [r for r in events if r["approach_reached"]]
    pf_fast = [r for r in reached if r["approach_fast"]]
    pf_slow = [r for r in reached if not r["approach_fast"]]
    conc = {
        "post_fast_share_approach_fast": (
            float(np.mean([r["post_fast"] for r in pf_fast])) if pf_fast else float("nan")
        ),
        "post_fast_share_approach_slow": (
            float(np.mean([r["post_fast"] for r in pf_slow])) if pf_slow else float("nan")
        ),
    }
    stage1["concordance"] = conc
    report["stages"]["1"] = stage1
    md += [
        "",
        f"Concordance: post-break fast share among approach-fast = "
        f"{conc['post_fast_share_approach_fast']:.2f} vs approach-slow = "
        f"{conc['post_fast_share_approach_slow']:.2f} (descriptive)",
        "",
    ]

    # ---------------------------------------------------------- Stage 2
    books: dict = {}
    md += ["## Stage 2 — P&L at DOP entry (discovery)", ""]
    for book in ("book2", "asym"):
        tr_a = data["sim_trades"].get(("A", book), [])
        tr_f = data["sim_trades"].get(("F", book), [])
        sim_a, sim_f = sim_from_trades(tr_a), sim_from_trades(tr_f)
        avg_a, avg_f = _avg(sim_a), _avg(sim_f)
        books[book] = {
            "A": {**sim_a.to_dict(), "avg": avg_a},
            "F": {**sim_f.to_dict(), "avg": avg_f},
        }
        md.append(
            f"- {book}: A n={sim_a.n} net={sim_a.net:+.1f} avg={avg_a:+.2f} · "
            f"F n={sim_f.n} net={sim_f.net:+.1f} avg={avg_f:+.2f}"
        )
    lift_min = float(kill.get("min_avg_lift_pts", 1.0))
    b2 = books["book2"]
    checks = {
        "gate_n_ok": b2["F"]["n"] >= int(kill.get("min_gate_n", 10)),
        "avg_lift_ok": bool(b2["F"]["avg"] >= b2["A"]["avg"] + lift_min),
        "net_positive": bool(b2["F"]["net"] > 0),
    }
    stage_pass = all(checks.values())
    report["stages"]["2"] = {"books": books, "checks": checks, "pass": stage_pass}
    md += ["", f"Gate checks (Book 2): {checks}", "",
           f"**Stage 2: {'PASS' if stage_pass else 'FAIL'}**", ""]
    if not stage_pass:
        report["verdict"] = (
            "reject / park — Stage 2 discovery P&L fail (no semi-OOS read, no retune)"
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
    arows_semi = build_approach_rows(parquet_dir, semi_ids, hist, zrows_semi, exp, results_dir)
    sdata = collect(parquet_dir, semi_ids, arows_semi, exp, horizons, targets)
    if sdata["events"]:
        pq.write_table(
            events_to_table(sdata["events"]),
            results_dir / "semi_oos_events.parquet", compression="zstd",
        )
    s_books: dict = {}
    for book in ("book2", "asym"):
        sim_a = sim_from_trades(sdata["sim_trades"].get(("A", book), []))
        sim_f = sim_from_trades(sdata["sim_trades"].get(("F", book), []))
        s_books[book] = {
            "A": {**sim_a.to_dict(), "avg": _avg(sim_a)},
            "F": {**sim_f.to_dict(), "avg": _avg(sim_f)},
        }
    sb2 = s_books["book2"]
    semi_pass = bool(
        sb2["F"]["n"] >= 5 and sb2["F"]["avg"] > sb2["A"]["avg"] and sb2["F"]["net"] > 0
    )
    report["stages"]["3"] = {"semi_oos": True, "books": s_books, "pass": semi_pass}
    md += [
        "## Stage 3 — Sep-Oct semi-holdout (one read)",
        "",
        f"Book 2: A n={sb2['A']['n']} avg={sb2['A']['avg']:+.2f} · "
        f"F n={sb2['F']['n']} net={sb2['F']['net']:+.1f} avg={sb2['F']['avg']:+.2f}",
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
            + f"**Result (EXP-0081): {verdict}.** See `results/EXP-0081/`."
            + text[idx + len("**Result:** pending."):]
        )
        hyp.write_text(text, encoding="utf-8")
    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    lt = lt.replace(
        "| EXP-0081 | H72 DOP pre-break approach chew | registered — pending run | Apr–Aug depth discovery; Sep–Oct semi once | final 20pt approach ≤60s gate at DOP entry; see notes |",
        f"| EXP-0081 | H72 DOP pre-break approach chew | ran — **{verdict.split('—')[0].strip()}** | Apr–Aug depth discovery | {verdict}; see notes |",
        1,
    )
    note = (
        f"\n### 2026-10-27 — EXP-0081 DOP approach-chew run (H72)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0081/`.\n"
        f"- Runner: `python -m research.dop.exp_0081_approach`.\n"
    )
    if "### 2026-10-27 — EXP-0081 DOP approach-chew run" not in lt:
        anchor = "### 2026-10-27 — EXP-0081 DOP approach-chew registered (H72)"
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
