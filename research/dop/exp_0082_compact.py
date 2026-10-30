"""EXP-0082 / H73 — DOP break compact trap-side node + clean departure (NQ, tape).

Stages (frozen in research/experiments/EXP-0082.yaml):
  Stage 0  coverage + truncation causality + power
  Stage 1  event study A (all) / C (compact) / L (clean) / K (compact+clean)
           + dose-response on node share, node delta, pullbacks vs null
  Stage 2  Book 2 P&L gate K vs A (+ fixed 20/40 asym secondary)
  Stage 3  Sep-Oct semi + March semi-virgin with --semi-oos after pass

Usage: python -m research.dop.exp_0082_compact [--semi-oos]
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
from research.features.dop_compact import compute_compact_trap_features

FLOATS = [
    "entry_px_pts", "dop_pts", "trap_vol", "trap_delta", "trap_share",
    "trap_excursion_pts", "build_dur_sec",
    "trap_vol_node", "node_vol", "node_share5", "node_w60_pts",
    "node_lo_pts", "node_hi_pts", "node_span_pts", "node_vpoc_pts",
    "node_dist_dop_pts", "node_trap_delta", "node_delta_share",
    "node_buy_vol", "node_sell_vol", "node_dur_sec", "node_end_to_break_sec",
    "depart_sec", "depart_dist_pts", "max_pullback_pts",
    "depart_path_efficiency",
]
INTS = [
    "event_ts_ns", "side", "n_pullbacks", "compact", "clean_departure", "arm_k",
]

ARMS = ("A", "C", "L", "K")


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


def _arm_of(r: dict) -> dict[str, bool]:
    return {
        "A": True,
        "C": bool(r["compact"]),
        "L": bool(r["clean_departure"]),
        "K": bool(r["arm_k"]),
    }


# ---------------------------------------------------------------- rows

def build_rows(parquet_dir: Path, sids: list[str], hist: dict, exp: dict) -> list[dict]:
    ccfg = dict(exp.get("compact") or {})
    rows: list[dict] = []
    for sid in sids:
        st = (hist.get(sid) or {}).get("NQ") or {}
        bk = st.get("break")
        if bk is None:
            continue
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        open_ns, _ = pair.bounds
        f = compute_compact_trap_features(
            pair.nq,
            dop_ticks=float(bk.dop_ticks), side=int(bk.side),
            break_ts_ns=int(bk.event_ts_ns), open_ns=open_ns,
            band_pts=float(ccfg.get("band_pts", 5.0)),
            compact_share_min=float(ccfg.get("compact_share_min", 0.50)),
            pullback_pts=float(ccfg.get("pullback_pts", 2.0)),
            clean_max_pullbacks=int(ccfg.get("clean_max_pullbacks", 1)),
        )
        if not np.isfinite(f["node_share5"]):
            continue
        rows.append({
            "session_id": sid, "market": "NQ",
            "event_ts_ns": int(bk.event_ts_ns), "side": int(bk.side),
            "entry_px_pts": float(bk.entry_px_pts), "dop_pts": float(bk.dop_pts),
            "trap_vol": float(bk.trap_vol), "trap_delta": float(bk.trap_delta),
            "trap_share": float(bk.trap_share),
            "trap_excursion_pts": float(bk.trap_excursion_pts),
            "build_dur_sec": float(bk.build_dur_sec),
            **f,
        })
        print(
            f"  [compact] {sid}: side={bk.side:+d} share5={f['node_share5']:.2f} "
            f"dshare={f['node_delta_share']:+.2f} pb={f['n_pullbacks']} K={f['arm_k']}",
            flush=True,
        )
    return rows


# ---------------------------------------------------------------- collect

def collect(parquet_dir: Path, sids: list[str], rows: list[dict], exp: dict,
            horizons, targets) -> dict:
    by_sid = {r["session_id"]: r for r in rows if r["session_id"] in set(sids)}
    lab_store: dict[str, list[pa.Table]] = {a: [] for a in ARMS}
    lab_store["null"] = []
    sim_trades: dict[tuple[str, str], list] = {}
    asym = dict(exp.get("asym_nq") or {"sl_pts": 20.0, "tp_pts": 40.0})
    rng = np.random.default_rng(int(exp.get("placebo_seed", 82)))
    grace_ns = int(float((exp.get("compact") or {}).get("grace_min", 5)) * 60 * 1e9)

    for sid in sids:
        if sid not in by_sid:
            continue
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        r = by_sid[sid]
        ev = events_to_table([r])
        arms = _arm_of(r)
        sets = {a: (ev if arms[a] else pa.table({})) for a in ARMS}
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
        for arm in ARMS:
            tbl = sets[arm]
            if tbl.num_rows == 0:
                continue
            s2 = simulate_session(
                sid, pair.nq, pair.es, cfg, vwap_from_ns=pair.bounds[0],
                bracket_fn=make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0),
                events=tbl,
            )
            sim_trades.setdefault((arm, "book2"), []).extend(s2.trades)
            if arm in ("A", "K"):
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
        print(f"  [evt] {sid}: K={r['arm_k']}", flush=True)
    return {"events": list(by_sid.values()), "labeled": lab_store, "sim_trades": sim_trades}


def _strata(data: dict, horizons, targets) -> list[tuple[str, dict]]:
    """Dose-response strata from the labeled Arm A table (features ride along)."""
    parts = [p for p in data["labeled"]["A"] if p.num_rows]
    if not parts:
        return []
    cols = set(parts[0].column_names)
    for p in parts[1:]:
        cols &= set(p.column_names)
    keep = [c for c in parts[0].column_names if c in cols]
    df = pa.concat_tables([p.select(keep) for p in parts], promote_options="default")
    out: list[tuple[str, dict]] = []

    def add(name: str, mask: np.ndarray) -> None:
        sub = _filter_table(df, mask)
        if sub.num_rows:
            out.append((name, H.summarize([sub], horizons, targets)))

    def terciles(col: str, prefix: str) -> None:
        v = df.column(col).to_numpy().astype(np.float64)
        ok = np.isfinite(v)
        if ok.sum() < 9:
            return
        q1, q2 = np.quantile(v[ok], [1 / 3, 2 / 3])
        add(f"{prefix}_lo", ok & (v <= q1))
        add(f"{prefix}_mid", ok & (v > q1) & (v <= q2))
        add(f"{prefix}_hi", ok & (v > q2))

    terciles("node_share5", "share5")
    terciles("node_delta_share", "node_delta")
    terciles("node_dist_dop_pts", "node_dist")
    terciles("depart_sec", "depart_sec")
    pb = df.column("n_pullbacks").to_numpy().astype(np.int64)
    add("pb_0", pb == 0)
    add("pb_1", pb == 1)
    add("pb_2plus", pb >= 2)
    k = df.column("arm_k").to_numpy().astype(np.int64) == 1
    ds = df.column("node_delta_share").to_numpy().astype(np.float64)
    add("K_delta_pos", k & np.isfinite(ds) & (ds > 0))
    add("K_delta_nonpos", k & np.isfinite(ds) & (ds <= 0))
    side = df.column("side").to_numpy().astype(np.int64)
    add("K_long", k & (side > 0))
    add("K_short", k & (side < 0))
    add("A_long", side > 0)
    add("A_short", side < 0)
    return out


def _books_md(data: dict, arms_books: list[tuple[str, str]]) -> tuple[dict, list[str]]:
    books: dict = {}
    lines: list[str] = []
    for arm, book in arms_books:
        sim = sim_from_trades(data["sim_trades"].get((arm, book), []))
        books.setdefault(book, {})[arm] = {**sim.to_dict(), "avg": _avg(sim)}
        lines.append(
            f"- {book} {arm}: n={sim.n} net={sim.net:+.1f} avg={_avg(sim):+.2f}"
        )
    return books, lines


def run(exp_path: Path | None = None, semi_oos: bool = False) -> dict:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / "EXP-0082"
    results_dir.mkdir(parents=True, exist_ok=True)
    exp = yaml.safe_load(
        (exp_path or Path("research/experiments/EXP-0082.yaml")).read_text(encoding="utf-8")
    )
    horizons = list(exp.get("horizons_ms") or [60000, 300000])
    targets = list(exp.get("point_targets_nq") or [5, 10, 20])
    exclude = set(exp.get("exclude_sessions") or [])
    disc_months = set(exp.get("discovery_months") or [])
    semi_months = set(exp.get("semi_oos_months") or [])
    march_months = set(exp.get("march_months") or [])
    kill = dict(exp.get("kill") or {})
    ccfg = dict(exp.get("compact") or {})

    sids_all = all_sessions(parquet_dir, exclude)
    disc_ids = [s for s in sids_all if _month(s) in disc_months]
    semi_ids = [s for s in sids_all if _month(s) in semi_months]
    march_ids = [s for s in sids_all if _month(s) in march_months]
    need = disc_ids + (semi_ids + march_ids if semi_oos else [])
    print(f"[0082] DOP history for {len(need)} sessions", flush=True)
    hist = compute_break_history(
        parquet_dir, _sorted_ids(need),
        {"grace_min": float(ccfg.get("grace_min", 5)),
         "pierce_ticks": float(ccfg.get("pierce_ticks", 1)),
         "prior_sessions": 14},
    )
    rows_disc = build_rows(parquet_dir, disc_ids, hist, exp)

    report: dict = {"status": "ok", "experiment": exp["name"], "stages": {}}
    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        "Frozen design: `research/experiments/EXP-0082.yaml` / H73. "
        "Compact = best <=5pt trap-side band holds >=50% of trap volume; "
        "clean = <=1 pullback >=2pt from last in-node print to the break.",
        "",
    ]

    data = collect(parquet_dir, disc_ids, rows_disc, exp, horizons, targets)
    events = data["events"]
    if events:
        pq.write_table(events_to_table(events), results_dir / "events.parquet", compression="zstd")

    # ---------------------------------------------------------- Stage 0
    n_a = len(events)
    n_c = sum(int(r["compact"]) for r in events)
    n_l = sum(int(r["clean_departure"]) for r in events)
    n_k = sum(int(r["arm_k"]) for r in events)
    trunc_rows = []
    for r in events[:3]:
        pair = load_pair_full(parquet_dir, r["session_id"])
        if pair is None:
            continue
        open_ns, _ = pair.bounds
        bk_ts = int(r["event_ts_ns"])
        ts = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
        cut = pair.nq.take(np.where(ts <= bk_ts)[0])
        f = compute_compact_trap_features(
            cut, dop_ticks=float(r["dop_pts"]) / 0.25, side=int(r["side"]),
            break_ts_ns=bk_ts, open_ns=open_ns,
            band_pts=float(ccfg.get("band_pts", 5.0)),
            compact_share_min=float(ccfg.get("compact_share_min", 0.50)),
            pullback_pts=float(ccfg.get("pullback_pts", 2.0)),
            clean_max_pullbacks=int(ccfg.get("clean_max_pullbacks", 1)),
        )
        match = (
            int(f["arm_k"]) == int(r["arm_k"])
            and int(f["n_pullbacks"]) == int(r["n_pullbacks"])
            and abs(float(f["node_share5"]) - float(r["node_share5"])) < 1e-9
        )
        trunc_rows.append({"session": r["session_id"], "match": bool(match)})
    trunc_ok = all(x["match"] for x in trunc_rows) if trunc_rows else False
    power_ok = n_a >= int(kill.get("min_arm_a", 30)) and n_k >= int(kill.get("min_gate_n", 10))
    report["stages"]["0"] = {
        "n_breaks": n_a, "n_compact": n_c, "n_clean": n_l, "n_k": n_k,
        "truncation": trunc_rows, "truncation_ok": trunc_ok, "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — coverage, causality, power",
        "",
        f"NQ first DOP breaks: {n_a} · compact {n_c} · clean {n_l} · **K {n_k}**",
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
    for name in ("A", "C", "L", "K", "null"):
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
    md += ["", "### Dose-response / splits (Arm A features, descriptive)", "",
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
    books, blines = _books_md(
        data, [(a, "book2") for a in ARMS] + [("A", "asym"), ("K", "asym")]
    )
    md += ["## Stage 2 — P&L at DOP entry (discovery)", ""] + blines + [""]
    lift_min = float(kill.get("min_avg_lift_pts", 1.0))
    b2 = books["book2"]
    checks = {
        "gate_n_ok": b2["K"]["n"] >= int(kill.get("min_gate_n", 10)),
        "avg_lift_ok": bool(b2["K"]["avg"] >= b2["A"]["avg"] + lift_min),
        "net_positive": bool(b2["K"]["net"] > 0),
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
            "discovery PASS — frozen; Sep-Oct/March semi reads not yet taken. "
            "Re-run with --semi-oos once."
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 3 semi
    groups: dict[str, list[str]] = {"sep_oct": semi_ids, "march": march_ids}
    s_books: dict = {}
    md += ["## Stage 3 — Sep-Oct semi + March semi-virgin (one read)", ""]
    pooled_trades: dict[tuple[str, str], list] = {}
    for gname, gids in groups.items():
        grows = build_rows(parquet_dir, gids, hist, exp)
        gdata = collect(parquet_dir, gids, grows, exp, horizons, targets)
        if gdata["events"]:
            pq.write_table(
                events_to_table(gdata["events"]),
                results_dir / f"semi_{gname}_events.parquet", compression="zstd",
            )
        gb, glines = _books_md(
            gdata, [(a, "book2") for a in ARMS] + [("A", "asym"), ("K", "asym")]
        )
        s_books[gname] = gb
        md += [f"### {gname}", ""] + glines + [""]
        for key, tr in gdata["sim_trades"].items():
            pooled_trades.setdefault(key, []).extend(tr)
    pooled: dict = {}
    for arm in ("A", "K"):
        sim = sim_from_trades(pooled_trades.get((arm, "book2"), []))
        pooled[arm] = {**sim.to_dict(), "avg": _avg(sim)}
    semi_pass = bool(
        pooled["K"]["n"] >= 5 and pooled["K"]["net"] > 0
        and pooled["K"]["avg"] > pooled["A"]["avg"]
    )
    report["stages"]["3"] = {"groups": s_books, "pooled_book2": pooled, "pass": semi_pass}
    md += [
        f"Pooled Book 2: A n={pooled['A']['n']} avg={pooled['A']['avg']:+.2f} · "
        f"K n={pooled['K']['n']} net={pooled['K']['net']:+.1f} avg={pooled['K']['avg']:+.2f}",
        "",
        f"**Stage 3: {'PASS' if semi_pass else 'FAIL'}**",
        "",
    ]
    report["verdict"] = (
        "discovery + semi reads PASS — pending virgin-month validation (separate phase)"
        if semi_pass else
        "reject / park — Stage 3 semi read fail (no retune)"
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
            + f"**Result (EXP-0082): {verdict}.** See `results/EXP-0082/`."
            + text[idx + len("**Result:** pending."):]
        )
        hyp.write_text(text, encoding="utf-8")
    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    lt = lt.replace(
        "| EXP-0082 | H73 DOP compact trap node + clean departure | registered — pending run | Apr–Aug tape discovery; Sep–Oct semi + Mar semi-virgin once | ≤5pt node ≥50% trap vol ∧ ≤1 pullback ≥2pt gate at DOP entry; see notes |",
        f"| EXP-0082 | H73 DOP compact trap node + clean departure | ran — **{verdict.split('—')[0].strip()}** | Apr–Aug tape discovery | {verdict}; see notes |",
        1,
    )
    note = (
        f"\n### 2026-10-30 — EXP-0082 DOP compact trap node run (H73)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0082/`.\n"
        f"- Runner: `python -m research.dop.exp_0082_compact`.\n"
    )
    if "### 2026-10-30 — EXP-0082 DOP compact trap node run" not in lt:
        anchor = "### 2026-10-30 — EXP-0082 DOP compact trap node registered (H73)"
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
                    help="read Sep-Oct + March semi sets once (only after discovery pass)")
    ap.add_argument("--exp", type=Path, default=None)
    args = ap.parse_args()
    run(args.exp, semi_oos=args.semi_oos)


if __name__ == "__main__":
    main()
