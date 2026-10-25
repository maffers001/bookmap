"""EXP-0055 / H46 — Opening Range Breakout validated by orderflow.

Design is frozen in research/experiments/EXP-0055.yaml (see HYPOTHESES H46).
Stages run sequentially with kill bars:
  Stage 0  causality (truncation invariance + OR hand-check vs 1-min bars),
           coverage and power (Arm B kept >= 20 AND dropped >= 20 pooled on
           discovery, else park with counts as the result);
  Stage 1  event study Arms A/B/C vs time-matched random null, shifted-ES
           (+60 s) OF placebo and own-market-only OF twin;
  Stage 2  P&L on Book 1 (paper-faithful, R = far OR extreme, TP 2R) and
           Book 2 (vol RR SL1/TP2) with a random-skip placebo gate bar;
  Stage 3  Sep-Oct 2026 holdout, read ONCE only after Stages 1-2 pass and
           only with --stage3. No retuning anywhere.

Usage: python -m research.orb.exp_0055_orb [--stage3]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    filter_summary,
    make_vol_bracket_rr,
    simulate_session,
)
from research.config import load_config
from research.correlated import harness as H
from research.features.large_prints import LargePrintParams, large_print_at, large_print_series
from research.features.momentum import trades_to_tape_bars
from research.features.opening_range import (
    OpeningRange,
    annotate_causal_state,
    build_opening_range,
    detect_breakout,
    first_reentry_ts,
    session_range_pts,
)
from research.features.orb_flow import first_confirm_stamp, flow_at, sweep_flag_at

NS_MIN = 60_000_000_000
OR_LEN_NS = 15 * NS_MIN

# ---------------------------------------------------------------- sessions

def _date_key(sid: str) -> tuple[int, int, int]:
    return (int(sid[4:6]), int(sid[2:4]), int(sid[0:2]))  # YY, MM, DD


def _month(sid: str) -> str:
    return sid[2:6]  # MMYY


def all_sessions(parquet_dir: Path, exclude: set[str]) -> list[str]:
    sids = [s for s in H.list_sessions(parquet_dir) if s not in exclude]
    return sorted(sids, key=_date_key)


def _trades(pair: H.Pair, market: str) -> pa.Table:
    return pair.nq if market == "NQ" else pair.es


def _cut_before(t: pa.Table, ts_ns: int) -> pa.Table:
    if t.num_rows == 0:
        return t
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    k = ts <= ts_ns
    return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)


# ---------------------------------------------------------------- pass 1: OR state

def compute_or_history(parquet_dir: Path, sids: list[str], or_cfg: dict) -> dict:
    """Per session/market OR + causal session state (prior sessions only)."""
    raw: dict[str, dict[str, dict]] = {}
    ranges: dict[str, list[dict]] = {"NQ": [], "ES": []}
    idx_of: dict[tuple[str, str], int] = {}
    for sid in sids:
        pair = H.load_pair(parquet_dir, sid)
        if pair is None:
            continue
        or_start, win_end = pair.bounds
        or_end = or_start + OR_LEN_NS
        raw[sid] = {}
        for mkt in ("NQ", "ES"):
            tr = _trades(pair, mkt)
            orng = build_opening_range(
                tr, or_start, or_end,
                max_first_print_delay_sec=float(or_cfg.get("max_first_print_delay_sec", 60)))
            row = {
                "or": orng,
                "or_volume": orng.volume if orng.valid else float("nan"),
                "or_width_pts": orng.width_pts if orng.valid else float("nan"),
                "session_range_pts": session_range_pts(tr, or_start, win_end),
            }
            raw[sid][mkt] = row
            idx_of[(sid, mkt)] = len(ranges[mkt])
            ranges[mkt].append(row)
        print(f"  [or] {sid}", flush=True)
    prior_n = int(or_cfg.get("prior_sessions", 14))
    for mkt in ("NQ", "ES"):
        annotate_causal_state(ranges[mkt], prior_n=prior_n)
    return raw


# ---------------------------------------------------------------- events

EVENT_FLOATS = [
    "entry_px_pts", "orh_pts", "orl_pts", "or_open_pts", "or_close_pts",
    "or_width_pts", "r_pts", "tp_pts", "sl_pts", "relvol",
    "contraction_ratio", "prior_range_median", "time_to_break_min",
    "t5_imb", "t10_imb", "c5_imb", "c10_imb", "t5_al_imb", "c5_al_imb",
    "t5_vol", "c5_vol", "t5_vol_burst", "t5_eff", "c5_eff",
    "t5_max_print", "lp_traded_cnt", "lp_cross_cnt",
]
EVENT_INTS = [
    "event_ts_ns", "side", "market_code", "or_direction", "arm_b",
    "arm_b_own_only", "arm_b_shifted", "arm_c_ts", "sweep_flag",
    "failed_break_60s", "opp_ts_ns",
]


def events_to_table(rows: list[dict]) -> pa.Table:
    if not rows:
        return pa.table({})
    cols: dict[str, pa.Array] = {}
    cols["event_ts_ns"] = pa.array([int(r["event_ts_ns"]) for r in rows], pa.int64())
    cols["side"] = pa.array([int(r["side"]) for r in rows], pa.int8())
    for name in EVENT_FLOATS:
        if name in ("event_ts_ns", "side"):
            continue
        cols[name] = pa.array([float(r.get(name, np.nan)) for r in rows], pa.float64())
    for name in EVENT_INTS:
        if name in ("event_ts_ns", "side"):
            continue
        cols[name] = pa.array([int(r.get(name, 0)) for r in rows], pa.int64())
    cols["session_id"] = pa.array([r["session_id"] for r in rows])
    cols["market"] = pa.array([r["market"] for r in rows])
    return pa.table(cols)


def build_session_events(pair: H.Pair, or_state: dict, exp: dict) -> list[dict]:
    """All primary breakout events (both markets) for one loaded session."""
    of_cfg = dict(exp.get("orb_flow") or {})
    or_cfg = dict(exp.get("opening_range") or {})
    or_start, win_end = pair.bounds
    or_end = or_start + OR_LEN_NS
    traded_min = float(of_cfg.get("traded_imb_min", 0.30))
    cross_min = float(of_cfg.get("cross_imb_min", 0.0))
    win_primary = float(of_cfg.get("primary_window_sec", 5))
    confirm_sec = float(of_cfg.get("confirm_window_sec", 5))
    pierce = float(or_cfg.get("pierce_ticks", 1))

    nq_bars = pair.bars(100)[0]
    es_bars = pair.bars(100)[1]
    bars = {"NQ": nq_bars, "ES": es_bars}
    lp = large_print_series(
        pair.nq, pair.es, t0_ns=or_start, t1_ns=win_end,
        p=LargePrintParams(
            large_q=float(of_cfg.get("large_q", 0.99)),
            baseline_lookback_min=int(of_cfg.get("large_baseline_lookback_min", 30))))

    ors = {m: or_state[pair.sid][m]["or"] for m in ("NQ", "ES")}
    rows: list[dict] = []
    for mkt in ("NQ", "ES"):
        orng: OpeningRange = ors[mkt]
        if not orng.valid or orng.direction == 0:
            continue
        tr = _trades(pair, mkt)
        bk = detect_breakout(tr, orng, or_end, win_end, pierce_ticks=pierce)
        if bk is None:
            continue
        other = "ES" if mkt == "NQ" else "NQ"
        stamp = bk.event_ts_ns
        side = bk.side
        f_t5 = flow_at(bars[mkt], stamp, 5)
        f_t10 = flow_at(bars[mkt], stamp, 10)
        f_c5 = flow_at(bars[other], stamp, 5)
        f_c10 = flow_at(bars[other], stamp, 10)
        f_c5_shift = flow_at(bars[other], stamp + int(of_cfg.get("shifted_es_placebo_sec", 60)) * 1_000_000_000, 5)

        def _gate(f_t: dict, f_c: dict) -> int:
            it, ic = f_t["imbalance"], f_c["imbalance"]
            if not np.isfinite(it) or side * it < traded_min:
                return 0
            if np.isfinite(ic) and side * ic < cross_min:
                return 0
            return 1

        arm_b = _gate(f_t5, f_c5)
        arm_b_own = 1 if (np.isfinite(f_t5["imbalance"]) and side * f_t5["imbalance"] >= traded_min) else 0
        arm_b_shift = _gate(f_t5, f_c5_shift)
        or_level = orng.orh_pts if side > 0 else orng.orl_pts
        arm_c_ts = first_confirm_stamp(
            bars[mkt], bars[other], stamp, side,
            stamp + int(confirm_sec * 1e9), or_level_pts=or_level,
            traded_imb_min=traded_min, cross_imb_min=cross_min,
            window_sec=win_primary) if not arm_b else stamp
        reentry = first_reentry_ts(tr, orng, side, stamp, min(stamp + 60 * 1_000_000_000, win_end))
        lp_at = large_print_at(lp, stamp)
        lp_traded = lp_at["lp_nq_cnt"][0] if mkt == "NQ" else lp_at["lp_es_cnt"][0]
        lp_cross = lp_at["lp_es_cnt"][0] if mkt == "NQ" else lp_at["lp_nq_cnt"][0]
        r_pts = (bk.entry_px_pts - orng.orl_pts) if side > 0 else (orng.orh_pts - bk.entry_px_pts)
        st = or_state[pair.sid][mkt]
        other_dir = ors[other].direction if ors[other].valid else 0
        rows.append({
            "session_id": pair.sid, "market": mkt,
            "market_code": 0 if mkt == "NQ" else 1,
            "event_ts_ns": stamp, "side": side,
            "entry_px_pts": bk.entry_px_pts,
            "orh_pts": orng.orh_pts, "orl_pts": orng.orl_pts,
            "or_open_pts": orng.open_pts, "or_close_pts": orng.close_pts,
            "or_width_pts": orng.width_pts, "or_direction": orng.direction,
            "r_pts": float(r_pts), "tp_pts": float(2.0 * r_pts), "sl_pts": float(r_pts),
            "relvol": float(st.get("relvol", np.nan)),
            "contraction_ratio": float(st.get("contraction_ratio", np.nan)),
            "prior_range_median": float(st.get("prior_range_median", np.nan)),
            "or_dir_agree": int(other_dir == orng.direction and other_dir != 0),
            "time_to_break_min": (stamp - or_end) / 1e9 / 60.0,
            "t5_imb": f_t5["imbalance"], "t10_imb": f_t10["imbalance"],
            "c5_imb": f_c5["imbalance"], "c10_imb": f_c10["imbalance"],
            "t5_al_imb": side * f_t5["imbalance"] if np.isfinite(f_t5["imbalance"]) else np.nan,
            "c5_al_imb": side * f_c5["imbalance"] if np.isfinite(f_c5["imbalance"]) else np.nan,
            "t5_vol": f_t5["total_vol"], "c5_vol": f_c5["total_vol"],
            "t5_vol_burst": f_t5["vol_burst_ratio"], "t5_eff": f_t5["efficiency"],
            "c5_eff": f_c5["efficiency"], "t5_max_print": f_t5["max_print"],
            "lp_traded_cnt": float(lp_traded), "lp_cross_cnt": float(lp_cross),
            "arm_b": arm_b, "arm_b_own_only": arm_b_own, "arm_b_shifted": arm_b_shift,
            "arm_c_ts": int(arm_c_ts), "sweep_flag": sweep_flag_at(tr, stamp, side),
            "failed_break_60s": 1 if reentry else 0, "opp_ts_ns": bk.opposite_ts_ns,
        })
    return rows


# ---------------------------------------------------------------- labelling helpers

def _filter_table(t: pa.Table, mask: np.ndarray) -> pa.Table:
    if t.num_rows == 0:
        return t
    idx = np.where(mask)[0]
    return t.take(idx) if len(idx) else t.slice(0, 0)


def _arm_c_table(rows: list[dict]) -> pa.Table:
    sub = []
    for r in rows:
        if r.get("arm_c_ts"):
            r2 = dict(r)
            r2["event_ts_ns"] = int(r["arm_c_ts"])
            sub.append(r2)
    return events_to_table(sub)


def _summarize_market(parts: list[pa.Table], horizons, targets) -> dict:
    return H.summarize(parts, horizons, targets)


def _placebo_p90(trades, *, keep_n, draws, seed, commission) -> dict:
    pnls = np.asarray([t.pnl - commission for t in trades], dtype=np.float64)
    n = len(pnls)
    if n == 0 or keep_n <= 0:
        return {"p90_avg": float("nan"), "p50_avg": float("nan"), "n": 0, "keep_n": int(keep_n)}
    k = min(keep_n, n)
    rng = np.random.default_rng(seed)
    avgs = np.array([float(pnls[rng.choice(n, size=k, replace=False)].mean()) for _ in range(draws)])
    return {"p90_avg": float(np.quantile(avgs, 0.90)), "p50_avg": float(np.median(avgs)),
            "n": int(n), "keep_n": int(k), "draws": int(draws)}


def _avg(summary: SimSummary) -> float:
    return (summary.net / summary.n) if summary.n else float("nan")


def _early_late_avg(sim_all: SimSummary, keep_ids: set, months_early: set[str]) -> tuple[float, float]:
    def avg(pred):
        sub = filter_summary(sim_all, pred, H.COMMISSION)
        return (sub.net / sub.n) if sub.n else float("nan")
    a = avg(lambda t: id(t) in keep_ids and _month(t.session_id) in months_early)
    b = avg(lambda t: id(t) in keep_ids and _month(t.session_id) not in months_early)
    return a, b


# ---------------------------------------------------------------- main pipeline

def collect(parquet_dir: Path, sids: list[str], or_state: dict, exp: dict,
            horizons, targets_by_market) -> dict:
    """Load each session once: events, labels (A/B/C/nulls), and both books."""
    out = {
        "events": [], "labeled": {"NQ": {}, "ES": {}},
        "sims": {}, "pairs_seen": [],
    }
    lab_store: dict[str, dict[str, list[pa.Table]]] = {
        m: {"A": [], "B": [], "C": [], "own": [], "shifted": [], "null": []}
        for m in ("NQ", "ES")}
    sim_trades: dict[tuple[str, str, str], list] = {}
    rng_master = np.random.default_rng(int((exp.get("orb_flow") or {}).get("placebo_seed", 54)))

    for sid in sids:
        pair = H.load_pair(parquet_dir, sid)
        if pair is None:
            continue
        out["pairs_seen"].append(sid)
        rows = build_session_events(pair, or_state, exp)
        out["events"].extend(rows)
        if not rows:
            continue
        ev_all = events_to_table(rows)
        for mkt in ("NQ", "ES"):
            mrows = [r for r in rows if r["market"] == mkt]
            if not mrows:
                continue
            targets = targets_by_market[mkt]
            ev = events_to_table(mrows)
            sets = {
                "A": ev,
                "B": _filter_table(ev, np.array([r["arm_b"] == 1 for r in mrows])),
                "C": _arm_c_table(mrows),
                "own": _filter_table(ev, np.array([r["arm_b_own_only"] == 1 for r in mrows])),
                "shifted": _filter_table(ev, np.array([r["arm_b_shifted"] == 1 for r in mrows])),
            }
            # time-matched null: same side, random stamps after OR end
            tr = _trades(pair, mkt)
            ts_all = tr.column("ts_ns").to_numpy().astype(np.int64)
            after = ts_all[ts_all >= pair.bounds[0] + OR_LEN_NS]
            null_rows = []
            if len(after):
                for r in mrows:
                    pick = rng_master.choice(after, size=min(20, len(after)), replace=False)
                    for p in pick:
                        null_rows.append({"session_id": sid, "market": mkt,
                                          "market_code": 0 if mkt == "NQ" else 1,
                                          "event_ts_ns": int(p), "side": int(r["side"])})
            sets["null"] = events_to_table(null_rows)
            for name, tbl in sets.items():
                if tbl.num_rows:
                    lab = H.label_in_points(tbl, pair, horizons, targets, role=mkt)
                    if lab.num_rows:
                        lab_store[mkt][name].append(lab)
            # ---- P&L books (Arm A vs Arm B)
            for arm_name, arm_tbl in (("A", sets["A"]), ("B", sets["B"])):
                if arm_tbl.num_rows == 0:
                    continue
                traded = pair.nq if mkt == "NQ" else pair.es
                cfg = MatSimConfig(slip_ticks=float(exp.get("slip_ticks", 1.0)),
                                   commission_pts_rt=H.COMMISSION)
                s1 = simulate_session(sid, traded, pair.es, cfg,
                                      vwap_from_ns=pair.bounds[0], events=arm_tbl)
                sim_trades.setdefault((mkt, "book1", arm_name), []).extend(s1.trades)
                tbl2 = arm_tbl.drop(["tp_pts", "sl_pts"]) if "tp_pts" in arm_tbl.column_names else arm_tbl
                s2 = simulate_session(
                    sid, traded, pair.es, cfg, vwap_from_ns=pair.bounds[0],
                    bracket_fn=make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0),
                    events=tbl2)
                sim_trades.setdefault((mkt, "book2", arm_name), []).extend(s2.trades)
        print(f"  [evt] {sid}: events={len(rows)}", flush=True)

    for mkt in ("NQ", "ES"):
        for name, parts in lab_store[mkt].items():
            out["labeled"][mkt][name] = parts
    out["sim_trades"] = sim_trades
    return out


def sim_from_trades(trades: list) -> SimSummary:
    from research.backtest.mat_sim import _rebuild_summary
    return _rebuild_summary(list(trades), H.COMMISSION)


def run(exp_path: Path | None = None, stage3: bool = False) -> dict:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / "EXP-0055"
    exp = yaml.safe_load((exp_path or Path("research/experiments/EXP-0055.yaml")).read_text(encoding="utf-8"))
    horizons = list(exp.get("horizons_ms") or [10000, 60000, 300000])
    targets_by_market = {"NQ": list(exp.get("point_targets_nq") or [5, 10, 20]),
                         "ES": list(exp.get("point_targets_es") or [2, 4, 8])}
    exclude = set(exp.get("exclude_sessions") or [])
    disc_months = set(exp.get("discovery_months") or [])
    hold_months = set(exp.get("holdout_months") or [])
    kill = dict(exp.get("kill") or {})
    of_cfg = dict(exp.get("orb_flow") or {})

    sids_all = all_sessions(parquet_dir, exclude)
    print(f"[0055] {len(sids_all)} sessions; building OR history", flush=True)
    or_state = compute_or_history(parquet_dir, sids_all, dict(exp.get("opening_range") or {}))

    disc_ids = [s for s in sids_all if _month(s) in disc_months]
    report: dict = {"status": "ok", "experiment": exp["name"], "stages": {}}
    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        "Frozen design: `research/experiments/EXP-0055.yaml` / H46. "
        "OR = UK 14:30–14:45 per market; breakout in OR direction only, "
        "1-tick pierce, first break only; Arm B = traded 5s imbalance >= "
        f"{of_cfg.get('traded_imb_min')} + cross 5s imbalance >= {of_cfg.get('cross_imb_min')}.",
        "",
    ]

    data = collect(parquet_dir, disc_ids, or_state, exp, horizons, targets_by_market)
    events = data["events"]
    ev_table = events_to_table(events)
    if ev_table.num_rows:
        results_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(ev_table, results_dir / "events.parquet", compression="zstd")

    # ---------------------------------------------------------- Stage 0
    counts = Counter()
    for r in events:
        counts[f"{r['market']}_events"] += 1
        counts[f"{r['market']}_armB"] += int(r["arm_b"])
    n_events = len(events)
    n_armb = sum(int(r["arm_b"]) for r in events)
    coverage = {
        "discovery_sessions": disc_ids, "n_sessions_seen": len(data["pairs_seen"]),
        "n_events": n_events, "n_armB": n_armb, "n_dropped": n_events - n_armb,
        "by_market": dict(sorted(counts.items())),
        "relvol_ge1_share": float(np.mean([r["relvol"] >= 1 for r in events if np.isfinite(r["relvol"])])) if events else None,
    }
    # causality: truncation invariance + OR hand-check on first sessions w/ events
    trunc_rows, hand_rows = [], []
    checked = 0
    for sid in disc_ids:
        srows = [r for r in events if r["session_id"] == sid]
        if not srows or checked >= 3:
            continue
        pair = H.load_pair(parquet_dir, sid)
        if pair is None:
            continue
        checked += 1
        for r in srows[:1]:
            mkt = r["market"]
            tr = _trades(pair, mkt)
            cut = _cut_before(tr, int(r["event_ts_ns"]))
            f_full = flow_at(pair.bars(100)[0] if mkt == "NQ" else pair.bars(100)[1],
                             int(r["event_ts_ns"]), 5)
            bars_cut = trades_to_tape_bars(cut, bar_ms=100)
            f_cut = flow_at(bars_cut, int(r["event_ts_ns"]), 5)
            same = all(
                (not np.isfinite(f_full[k]) and not np.isfinite(f_cut[k])) or
                abs(float(f_full[k]) - float(f_cut[k])) < 1e-9
                for k in ("imbalance", "total_vol", "delta"))
            trunc_rows.append({"session": sid, "market": mkt, "match": bool(same)})
            # OR hand-check vs 1-minute bars
            bars1m = trades_to_tape_bars(tr, bar_ms=60000)
            if bars1m.num_rows:
                ts1 = bars1m.column("ts_ns").to_numpy().astype(np.int64)
                k1 = (ts1 >= pair.bounds[0]) & (ts1 < pair.bounds[0] + OR_LEN_NS)
                hand_rows.append({
                    "session": sid, "market": mkt,
                    "match": bool(k1.any() and
                                  abs(float(bars1m.column("high").to_numpy()[k1].max()) * 0.25 - r["orh_pts"]) < 1e-9 and
                                  abs(float(bars1m.column("low").to_numpy()[k1].min()) * 0.25 - r["orl_pts"]) < 1e-9),
                })
    trunc_ok = all(r["match"] for r in trunc_rows) if trunc_rows else False
    hand_ok = all(r["match"] for r in hand_rows) if hand_rows else False
    min_kept = int(kill.get("min_kept_n", 20))
    min_dropped = int(kill.get("min_dropped_n", 20))
    power_ok = n_armb >= min_kept and (n_events - n_armb) >= min_dropped
    report["stages"]["0"] = {"coverage": coverage, "truncation": trunc_rows,
                             "handcheck": hand_rows, "truncation_ok": trunc_ok,
                             "handcheck_ok": hand_ok, "power_ok": power_ok}
    md += [
        "## Stage 0 — coverage, causality, power",
        "",
        f"Discovery sessions seen: {len(data['pairs_seen'])} · breakout events: **{n_events}** "
        f"(NQ {counts.get('NQ_events', 0)} / ES {counts.get('ES_events', 0)}) · "
        f"Arm B kept: **{n_armb}** · dropped: **{n_events - n_armb}**",
        f"Truncation invariance: **{'PASS' if trunc_ok else 'FAIL'}** ({len(trunc_rows)} checks) · "
        f"OR hand-check vs 1-min bars: **{'PASS' if hand_ok else 'FAIL'}** ({len(hand_rows)} checks)",
        f"Power (kept >= {min_kept} and dropped >= {min_dropped}, pooled): "
        f"**{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    if not (trunc_ok and hand_ok):
        report["verdict"] = "reject — Stage 0 causality failed"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        print(report["verdict"], flush=True)
        return report
    if not power_ok:
        report["verdict"] = ("reject / park — Stage 0 underpowered (counts are the result; "
                              "no event study, no P&L, no holdout, no retune)")
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 1
    stage1: dict = {}
    md += ["## Stage 1 — event study (discovery)", "",
           "| market | set | n | hit(small) | hit(mid) | hit(large) | @10s | @60s | @300s | MFE | MAE |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for mkt in ("NQ", "ES"):
        targets = targets_by_market[mkt]
        for name in ("A", "B", "C", "own", "shifted", "null"):
            s = _summarize_market(data["labeled"][mkt][name], horizons, targets)
            stage1[f"{mkt}_{name}"] = s
            if s.get("n_events"):
                md.append(
                    f"| {mkt} | {name} | {s['n_events']} | "
                    f"{s.get(f'p_hit_{int(targets[0])}pts', float('nan')):.2f} | "
                    f"{s.get(f'p_hit_{int(targets[1])}pts', float('nan')):.2f} | "
                    f"{s.get(f'p_hit_{int(targets[2])}pts', float('nan')):.2f} | "
                    f"{H.fmt(s.get('mean_ret_pts_10000ms'), 2)} | "
                    f"{H.fmt(s.get('mean_ret_pts_60000ms'), 2)} | "
                    f"{H.fmt(s.get('mean_ret_pts_300000ms'), 2)} | "
                    f"{H.fmt(s.get('mean_mfe_pts'), 1)} | {H.fmt(s.get('mean_mae_pts'), 1)} |")
    # strata on Arm A (descriptive)
    strata: dict = {}
    for mkt in ("NQ", "ES"):
        parts = data["labeled"][mkt]["A"]
        if not parts:
            continue
        df = pa.concat_tables(parts, promote_options="default")
        for label, mask in (
            ("relvol_ge1", np.array([np.isfinite(x) and x >= 1 for x in df.column("relvol").to_numpy()])),
            ("relvol_lt1", np.array([np.isfinite(x) and x < 1 for x in df.column("relvol").to_numpy()])),
            ("contract_narrow", np.array([np.isfinite(x) and x <= 1 for x in df.column("contraction_ratio").to_numpy()])),
            ("failed_break", np.array([x == 1 for x in df.column("failed_break_60s").to_numpy()])),
        ):
            sub = _filter_table(df, mask)
            if sub.num_rows:
                strata[f"{mkt}_A_{label}"] = _summarize_market([sub], horizons, targets_by_market[mkt])
    stage1["strata_armA"] = strata
    report["stages"]["1"] = stage1
    md += ["", "Arm A strata (descriptive, not gates): "
           + "; ".join(f"{k}: n={v.get('n_events')} @60s={H.fmt(v.get('mean_ret_pts_60000ms'), 2)}"
                       for k, v in strata.items()), ""]

    # ---------------------------------------------------------- Stage 2
    stage2: dict = {}
    md += ["## Stage 2 — P&L (discovery)", "",
           "| market | book | Arm A n/net/avg | Arm B n/net/avg | placebo p90 avg | checks |",
           "|---|---|---|---|---|---|"]
    early_months = {"0426", "0526", "0626"}
    any_market_pass = False
    other_not_bad = True
    avgs: dict[str, tuple[float, float]] = {}
    for mkt in ("NQ", "ES"):
        lift_min = float(kill.get("min_avg_lift_pts_nq" if mkt == "NQ" else "min_avg_lift_pts_es", 1.0))
        for book in ("book1", "book2"):
            tr_a = data["sim_trades"].get((mkt, book, "A"), [])
            tr_b = data["sim_trades"].get((mkt, book, "B"), [])
            sim_a, sim_b = sim_from_trades(tr_a), sim_from_trades(tr_b)
            avg_a, avg_b = _avg(sim_a), _avg(sim_b)
            placebo = _placebo_p90(tr_a, keep_n=sim_b.n,
                                   draws=int(of_cfg.get("placebo_draws", 1000)),
                                   seed=int(of_cfg.get("placebo_seed", 54)), commission=H.COMMISSION)
            ev_b_ts = {(r["session_id"], int(r["event_ts_ns"])) for r in events
                       if r["market"] == mkt and r["arm_b"] == 1}
            kept_ids = {id(t) for t in tr_a
                        if (t.session_id, t.entry_ts_ns) in ev_b_ts}
            # note: re-simulated Arm B trades are the gate book; early/late on Arm B sim
            e_avg, l_avg = _early_late_avg(sim_b, {id(t) for t in tr_b}, early_months)
            e_a, l_a = _early_late_avg(sim_a, {id(t) for t in tr_a}, early_months)
            checks = {
                "kept_n_ok": sim_b.n >= min_kept,
                "avg_lift_ok": bool(np.isfinite(avg_b) and np.isfinite(avg_a) and avg_b >= avg_a + lift_min),
                "net_retained_ok": bool(sim_b.net >= float(kill.get("min_net_retained_frac", 0.60)) * sim_a.net)
                if sim_a.net > 0 else bool(sim_b.net >= sim_a.net),
                "early_avg_not_worse": bool(not np.isfinite(e_a) or (np.isfinite(e_avg) and e_avg >= e_a - 1e-9)),
                "late_avg_not_worse": bool(not np.isfinite(l_a) or (np.isfinite(l_avg) and l_avg >= l_a - 1e-9)),
                "maxdd_ok": sim_b.max_dd <= sim_a.max_dd + 1e-9,
                "placebo_ok": bool(np.isfinite(avg_b) and np.isfinite(placebo["p90_avg"]) and avg_b > placebo["p90_avg"]),
            }
            passed = all(checks.values())
            stage2[f"{mkt}_{book}"] = {
                "armA": {**sim_a.to_dict(), "avg": avg_a},
                "armB": {**sim_b.to_dict(), "avg": avg_b},
                "placebo": placebo, "checks": checks, "pass": passed,
            }
            md.append(f"| {mkt} | {book} | {sim_a.n}/{sim_a.net:+.1f}/{avg_a:+.2f} | "
                      f"{sim_b.n}/{sim_b.net:+.1f}/{avg_b:+.2f} | {placebo['p90_avg']:+.2f} | "
                      f"{'PASS' if passed else 'FAIL'} |")
            if book == "book2":
                avgs[mkt] = (avg_a, avg_b)
                if passed:
                    any_market_pass = True
    for mkt, (a, b) in avgs.items():
        lift_min = float(kill.get("min_avg_lift_pts_nq" if mkt == "NQ" else "min_avg_lift_pts_es", 1.0))
        if np.isfinite(a) and np.isfinite(b) and b < a - lift_min:
            other_not_bad = False
    stage2_pass = bool(any_market_pass and other_not_bad)
    report["stages"]["2"] = {"books": stage2, "pass": stage2_pass}
    md += ["", f"**Stage 2 (Book 2 primary yardstick): {'PASS' if stage2_pass else 'FAIL'}**", ""]

    if not stage2_pass:
        report["verdict"] = "reject / park — Stage 2 discovery P&L fail (no holdout read, no retune)"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        print(report["verdict"], flush=True)
        return report

    if not stage3:
        report["verdict"] = ("discovery PASS — frozen; holdout (Sep-Oct 2026) not yet read. "
                              "Re-run with --stage3 exactly once to read it.")
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 3 (once)
    hold_ids = [s for s in sids_all if _month(s) in hold_months]
    hdata = collect(parquet_dir, hold_ids, or_state, exp, horizons, targets_by_market)
    hevents = hdata["events"]
    if hevents:
        pq.write_table(events_to_table(hevents), results_dir / "holdout_events.parquet", compression="zstd")
    hstage: dict = {}
    md += ["## Stage 3 — Sep-Oct 2026 holdout (read once, frozen thresholds)", "",
           "| market | book | Arm A n/net/avg | Arm B n/net/avg |",
           "|---|---|---|---|"]
    hold_pass_by_market: dict[str, bool] = {}
    for mkt in ("NQ", "ES"):
        ok_market = False
        for book in ("book1", "book2"):
            tr_a = hdata["sim_trades"].get((mkt, book, "A"), [])
            tr_b = hdata["sim_trades"].get((mkt, book, "B"), [])
            sim_a, sim_b = sim_from_trades(tr_a), sim_from_trades(tr_b)
            avg_a, avg_b = _avg(sim_a), _avg(sim_b)
            hstage[f"{mkt}_{book}"] = {"armA": {**sim_a.to_dict(), "avg": avg_a},
                                       "armB": {**sim_b.to_dict(), "avg": avg_b}}
            md.append(f"| {mkt} | {book} | {sim_a.n}/{sim_a.net:+.1f}/{avg_a:+.2f} | "
                      f"{sim_b.n}/{sim_b.net:+.1f}/{avg_b:+.2f} |")
            if book == "book2" and np.isfinite(avg_a) and np.isfinite(avg_b) \
                    and avg_b > avg_a and sim_b.net > 0:
                ok_market = True
        hold_pass_by_market[mkt] = ok_market
    other_ok = True
    for mkt, (a) in hold_pass_by_market.items():
        pass
    # other market not significantly negative on Book 2 Arm B
    for mkt in ("NQ", "ES"):
        if not hold_pass_by_market[mkt]:
            tr_b = hdata["sim_trades"].get((mkt, "book2", "B"), [])
            sim_b = sim_from_trades(tr_b)
            if sim_b.n and _avg(sim_b) < -float(kill.get("min_avg_lift_pts_nq" if mkt == "NQ" else "min_avg_lift_pts_es", 1.0)):
                other_ok = False
    hold_pass = any(hold_pass_by_market.values()) and other_ok
    report["stages"]["3"] = {"sessions": hold_ids, "books": hstage,
                             "pass_by_market": hold_pass_by_market, "pass": hold_pass}
    report["verdict"] = (
        "PASS holdout — eligible for Bookmap ORB module spec + parity (separate phase)"
        if hold_pass else
        "reject / park — Stage 3 holdout fail (no retune)")
    md += ["", f"**Stage 3: {'PASS' if hold_pass else 'FAIL'}**", "",
           "## Verdict", "", f"**{report['verdict']}**", ""]
    H.write_report(results_dir, report, md)
    print(report["verdict"], flush=True)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage3", action="store_true",
                    help="read the Sep-Oct 2026 holdout ONCE (only after a discovery pass)")
    ap.add_argument("--exp", type=Path, default=None)
    args = ap.parse_args()
    run(args.exp, stage3=args.stage3)


if __name__ == "__main__":
    main()
