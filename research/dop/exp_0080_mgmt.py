"""EXP-0080 / H71 — DOP entry + beyond-zone stall-scratch exit (NQ).

Reuses EXP-0079 zone features (cache in results/EXP-0079).
Arms: BASE (Book 2 vol RR bracket only) vs MGMT (bracket + stall scratch).
Discovery Apr-Aug; Sep-Oct one read with --semi-oos after discovery pass.

Usage: python -m research.dop.exp_0080_mgmt [--semi-oos]
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


def stall_ts_ns(row: dict, stall_cfg: dict) -> int:
    """Causal scratch decision time (0 = no scratch; fast traversal)."""
    if int(row.get("fast_traversal") or 0):
        return 0
    brk = int(row["event_ts_ns"])
    deadline_ns = int(float(stall_cfg.get("entry_deadline_min", 15)) * 60 * 1e9)
    cap_ns = int(float(stall_cfg.get("traversal_cap_sec", 60)) * 1e9)
    ttz = float(row.get("time_to_zone_sec", np.nan))
    if np.isfinite(ttz) and ttz * 1e9 <= deadline_ns:
        return brk + int(ttz * 1e9) + cap_ns
    return brk + deadline_ns


def mgmt_events(rows: list[dict], stall_cfg: dict) -> pa.Table:
    if not rows:
        return pa.table({})
    return pa.table({
        "event_ts_ns": pa.array([int(r["event_ts_ns"]) for r in rows], pa.int64()),
        "side": pa.array([int(r["side"]) for r in rows], pa.int8()),
        "stall_ts_ns": pa.array([stall_ts_ns(r, stall_cfg) for r in rows], pa.int64()),
        "fast_traversal": pa.array([int(r.get("fast_traversal") or 0) for r in rows], pa.int64()),
        "session_id": pa.array([r["session_id"] for r in rows]),
    })


def _stall_exit(entry_ts_ns: int, side: int, row: dict):
    stall = int(row.get("stall_ts_ns") or 0)
    if stall > int(entry_ts_ns):
        return stall, "stall"
    return None


def sim_from_trades(trades: list) -> SimSummary:
    from research.backtest.mat_sim import _rebuild_summary
    return _rebuild_summary(list(trades), H.COMMISSION)


def _avg(s: SimSummary) -> float:
    return (s.net / s.n) if s.n else float("nan")


def collect(parquet_dir: Path, sids: list[str], zrows: list[dict], exp: dict) -> dict:
    stall_cfg = dict(exp.get("stall") or {})
    ev_rows = zone_events([r for r in zrows if r["session_id"] in set(sids)])
    by_sid = {r["session_id"]: r for r in ev_rows}
    trades_out: dict[str, list] = {"BASE": [], "MGMT": []}
    for sid in sids:
        if sid not in by_sid:
            continue
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        ev = mgmt_events([by_sid[sid]], stall_cfg)
        cfg = MatSimConfig(
            slip_ticks=float(exp.get("slip_ticks", 1.0)),
            commission_pts_rt=H.COMMISSION,
        )
        bracket = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0)
        s_base = simulate_session(
            sid, pair.nq, pair.es, cfg, vwap_from_ns=pair.bounds[0],
            bracket_fn=bracket, events=ev,
        )
        trades_out["BASE"].extend(s_base.trades)
        s_mgmt = simulate_session(
            sid, pair.nq, pair.es, cfg, vwap_from_ns=pair.bounds[0],
            bracket_fn=make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0),
            events=ev, exit_fn=_stall_exit,
        )
        trades_out["MGMT"].extend(s_mgmt.trades)
        print(f"  [evt] {sid}: fast={by_sid[sid].get('fast_traversal')}", flush=True)
    return {"events": ev_rows, "sim_trades": trades_out}


def _sorted_ids(sids: list[str]) -> list[str]:
    return sorted(set(sids), key=lambda s: (int(s[4:6]), int(s[2:4]), int(s[0:2])))


def run(exp_path: Path | None = None, semi_oos: bool = False) -> dict:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    results_root = Path(cfg["results_dir"])
    results_dir = results_root / "EXP-0080"
    results_dir.mkdir(parents=True, exist_ok=True)
    zone_cache_dir = results_root / "EXP-0079"  # share EXP-0079 zone cache
    exp = yaml.safe_load(
        (exp_path or Path("research/experiments/EXP-0080.yaml")).read_text(encoding="utf-8")
    )
    exclude = set(exp.get("exclude_sessions") or [])
    disc_months = set(exp.get("discovery_months") or [])
    semi_months = set(exp.get("semi_oos_months") or [])
    kill = dict(exp.get("kill") or {})
    zcfg = dict(exp.get("dop_zone") or {})

    sids_all = all_sessions(parquet_dir, exclude)
    disc_ids = [s for s in sids_all if _month(s) in disc_months]
    semi_ids = [s for s in sids_all if _month(s) in semi_months]
    need = disc_ids + (semi_ids if semi_oos else [])
    print(f"[0080] DOP history for {len(need)} sessions", flush=True)
    hist = compute_break_history(
        parquet_dir, _sorted_ids(need),
        {"grace_min": float(zcfg.get("grace_min", 5)),
         "pierce_ticks": float(zcfg.get("pierce_ticks", 1)),
         "prior_sessions": 14},
    )
    zrows_disc = build_zone_rows(parquet_dir, disc_ids, hist, exp, zone_cache_dir)

    report: dict = {"status": "ok", "experiment": exp["name"], "stages": {}}
    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        "Frozen design: `research/experiments/EXP-0080.yaml` / H71. "
        "DOP entry (NQ depth universe); MGMT scratches at break+15 min if "
        "zone not entered, or zone-entry+60 s if not crossed; Book 2 vol RR.",
        "",
    ]

    data = collect(parquet_dir, disc_ids, zrows_disc, exp)
    events = data["events"]
    if events:
        pq.write_table(
            mgmt_events(events, dict(exp.get("stall") or {})),
            results_dir / "events.parquet", compression="zstd",
        )

    # ---------------------------------------------------------- Stage 0
    n_depth = len(events)
    n_fast = sum(int(r.get("fast_traversal") or 0) for r in events)
    n_stall = n_depth - n_fast
    power_ok = (
        n_depth >= int(kill.get("min_depth_breaks", 40))
        and n_fast >= int(kill.get("min_fast", 15))
        and n_stall >= int(kill.get("min_stall", 15))
    )
    report["stages"]["0"] = {
        "n_depth_breaks": n_depth, "n_fast": n_fast, "n_stall": n_stall,
        "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — counts / power",
        "",
        f"Depth DOP breaks: **{n_depth}** (fast {n_fast} / stall-candidates {n_stall}) · "
        f"power: **{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    if not power_ok:
        report["verdict"] = "reject / park — Stage 0 underpowered; DOP thread closed"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 1 discovery P&L
    tr_base = data["sim_trades"]["BASE"]
    tr_mgmt = data["sim_trades"]["MGMT"]
    sim_base, sim_mgmt = sim_from_trades(tr_base), sim_from_trades(tr_mgmt)
    avg_base, avg_mgmt = _avg(sim_base), _avg(sim_mgmt)
    # scratched subset diagnostic: trades exited by stall in MGMT vs same entries in BASE
    stall_keys = {
        (t.session_id, t.entry_ts_ns) for t in tr_mgmt if t.reason == "stall"
    }
    base_scratched = [t for t in tr_base if (t.session_id, t.entry_ts_ns) in stall_keys]
    mgmt_scratched = [t for t in tr_mgmt if t.reason == "stall"]
    diag = {
        "n_scratched": len(mgmt_scratched),
        "base_avg_on_scratched": (
            float(np.mean([t.pnl - H.COMMISSION for t in base_scratched]))
            if base_scratched else float("nan")
        ),
        "mgmt_avg_on_scratched": (
            float(np.mean([t.pnl - H.COMMISSION for t in mgmt_scratched]))
            if mgmt_scratched else float("nan")
        ),
    }
    lift_min = float(kill.get("min_avg_lift_pts", 1.0))
    checks = {
        "avg_lift_ok": bool(
            np.isfinite(avg_mgmt) and np.isfinite(avg_base)
            and avg_mgmt >= avg_base + lift_min
        ),
        "net_ok": bool(sim_mgmt.net > sim_base.net),
        "maxdd_ok": bool(sim_mgmt.max_dd <= sim_base.max_dd + 1e-9),
    }
    stage_pass = all(checks.values())
    report["stages"]["1"] = {
        "BASE": {**sim_base.to_dict(), "avg": avg_base},
        "MGMT": {**sim_mgmt.to_dict(), "avg": avg_mgmt},
        "scratch_diagnostic": diag, "checks": checks, "pass": stage_pass,
    }
    md += [
        "## Stage 1 — discovery P&L (Book 2 vol RR)",
        "",
        f"BASE (bracket only): n={sim_base.n} net={sim_base.net:+.1f} avg={avg_base:+.2f} maxDD={sim_base.max_dd:.1f}",
        f"MGMT (+ stall scratch): n={sim_mgmt.n} net={sim_mgmt.net:+.1f} avg={avg_mgmt:+.2f} maxDD={sim_mgmt.max_dd:.1f}",
        f"Scratched: {diag['n_scratched']} trades · BASE avg on them "
        f"{diag['base_avg_on_scratched']:+.2f} → MGMT avg {diag['mgmt_avg_on_scratched']:+.2f}",
        f"Checks (lift ≥ {lift_min}, net, maxDD): {checks}",
        "",
        f"**Stage 1: {'PASS' if stage_pass else 'FAIL'}**",
        "",
    ]
    if not stage_pass:
        report["verdict"] = (
            "reject / park — Stage 1 discovery fail (no Sep-Oct read, no retune); DOP thread closed"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    if not semi_oos:
        report["verdict"] = (
            "discovery PASS — frozen; Sep-Oct not yet read. "
            "Re-run with --semi-oos once to read it."
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 2 Sep-Oct one read
    zrows_semi = build_zone_rows(parquet_dir, semi_ids, hist, exp, zone_cache_dir)
    sdata = collect(parquet_dir, semi_ids, zrows_semi, exp)
    if sdata["events"]:
        pq.write_table(
            mgmt_events(sdata["events"], dict(exp.get("stall") or {})),
            results_dir / "semi_oos_events.parquet", compression="zstd",
        )
    s_base = sim_from_trades(sdata["sim_trades"]["BASE"])
    s_mgmt = sim_from_trades(sdata["sim_trades"]["MGMT"])
    s_avg_base, s_avg_mgmt = _avg(s_base), _avg(s_mgmt)
    semi_pass = bool(
        s_mgmt.n >= 5 and np.isfinite(s_avg_mgmt) and np.isfinite(s_avg_base)
        and s_avg_mgmt > s_avg_base and s_mgmt.net > 0
    )
    report["stages"]["2"] = {
        "semi_oos": True,
        "BASE": {**s_base.to_dict(), "avg": s_avg_base},
        "MGMT": {**s_mgmt.to_dict(), "avg": s_avg_mgmt},
        "pass": semi_pass,
    }
    md += [
        "## Stage 2 — Sep-Oct semi-holdout (one read)",
        "",
        f"BASE: n={s_base.n} net={s_base.net:+.1f} avg={s_avg_base:+.2f} · "
        f"MGMT: n={s_mgmt.n} net={s_mgmt.net:+.1f} avg={s_avg_mgmt:+.2f}",
        "",
        f"**Stage 2: {'PASS' if semi_pass else 'FAIL'}**",
        "",
    ]
    report["verdict"] = (
        "discovery + semi-holdout PASS — pending virgin-month validation (separate phase)"
        if semi_pass else
        "reject / park — Sep-Oct semi-holdout fail (no retune); DOP thread closed"
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
            + f"**Result (EXP-0080): {verdict}.** See `results/EXP-0080/`."
            + text[idx + len("**Result:** pending."):]
        )
        hyp.write_text(text, encoding="utf-8")
    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    lt = lt.replace(
        "| EXP-0080 | H71 DOP entry + stall-scratch exit | registered — pending run | Apr–Aug discovery (training); Sep–Oct one read | scratch if zone not entered 15m / not crossed 60s; final DOP test; see notes |",
        f"| EXP-0080 | H71 DOP entry + stall-scratch exit | ran — **{verdict.split('—')[0].strip()}** | Apr–Aug discovery | {verdict}; see notes |",
        1,
    )
    note = (
        f"\n### 2026-10-27 — EXP-0080 DOP stall-scratch run (H71)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0080/`.\n"
        f"- Runner: `python -m research.dop.exp_0080_mgmt`.\n"
    )
    if "### 2026-10-27 — EXP-0080 DOP stall-scratch run" not in lt:
        anchor = "### 2026-10-27 — EXP-0080 DOP stall-scratch exit registered (H71)"
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
