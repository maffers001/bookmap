"""EXP-0078 / H69 — Daily Open (DOP) break, trap-side amount dose-response.

Stages (kill bars frozen in research/experiments/EXP-0078.yaml):
  Stage 0  causality + 21:00 coverage + power
  Stage 1  event study Arms A/B/C + trap_vol/trap_delta dose-response
  Stage 2  P&L Book 1 (SL beyond DOP, TP 2R) and Book 2 (vol RR)
  Stage 3  Sep-Oct 2026 holdout — only with --stage3 after discovery pass

Usage: python -m research.dop.exp_0078_dop [--stage3]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import time
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
from research.features.daily_open import (
    annotate_causal_trap_state,
    detect_dop_break,
    is_v_shape,
    session_dop,
)
from research.features.momentum import trades_to_tape_bars
from research.features.orb_flow import flow_at

NS_MIN = 60_000_000_000
WIN_END = time(21, 0)
WIN_START = time(14, 30)


def _date_key(sid: str) -> tuple[int, int, int]:
    return (int(sid[4:6]), int(sid[2:4]), int(sid[0:2]))


def _month(sid: str) -> str:
    return sid[2:6]


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


def load_pair_full(parquet_dir: Path, sid: str) -> H.Pair | None:
    return H.load_pair(parquet_dir, sid, start=WIN_START, end=WIN_END)


# ---------------------------------------------------------------- pass 1: history for causal z

def compute_break_history(parquet_dir: Path, sids: list[str], dop_cfg: dict) -> dict:
    """Per session/market break row + causal trap_vol_z / trap_delta_z."""
    raw: dict[str, dict[str, dict]] = {}
    series: dict[str, list[dict]] = {"NQ": [], "ES": []}
    grace = float(dop_cfg.get("grace_min", 5))
    pierce = float(dop_cfg.get("pierce_ticks", 1))
    delay = float(dop_cfg.get("max_first_print_delay_sec", 60))
    prior_n = int(dop_cfg.get("prior_sessions", 14))

    for sid in sids:
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        open_ns, win_end = pair.bounds
        raw[sid] = {}
        for mkt in ("NQ", "ES"):
            tr = _trades(pair, mkt)
            row: dict = {
                "session_id": sid, "market": mkt, "break": None,
                "trap_vol": float("nan"), "trap_delta": float("nan"),
                "coverage_ok": False, "dop_ok": False,
            }
            if tr.num_rows == 0:
                raw[sid][mkt] = row
                series[mkt].append(row)
                continue
            ts = tr.column("ts_ns").to_numpy().astype(np.int64)
            last_ts = int(ts[-1])
            # coverage: tape reaches at least 20:30 UK (30 min before window end)
            row["coverage_ok"] = last_ts >= (win_end - 30 * NS_MIN)
            row["last_ts_ns"] = last_ts
            dop = session_dop(tr, open_ns, max_first_print_delay_sec=delay)
            row["dop_ok"] = bool(dop.valid)
            row["dop_pts"] = dop.dop_pts if dop.valid else float("nan")
            if dop.valid:
                bk = detect_dop_break(
                    tr, dop, open_ns, win_end,
                    grace_min=grace, pierce_ticks=pierce,
                )
                if bk is not None:
                    row["break"] = bk
                    row["trap_vol"] = bk.trap_vol
                    row["trap_delta"] = bk.trap_delta
            raw[sid][mkt] = row
            series[mkt].append(row)
        print(f"  [dop] {sid}", flush=True)

    for mkt in ("NQ", "ES"):
        annotate_causal_trap_state(series[mkt], prior_n=prior_n)
    return raw


# ---------------------------------------------------------------- events

EVENT_FLOATS = [
    "entry_px_pts", "dop_pts", "trap_vol", "trap_delta", "trap_share",
    "total_build_vol", "trap_vol_z", "trap_delta_z", "trap_vol_vs_med",
    "trap_excursion_pts", "build_dur_sec", "extreme_vol_share",
    "path_efficiency", "time_extreme_to_break_sec", "vpoc_pts",
    "r_pts", "tp_pts", "sl_pts", "t5_imb", "t5_al_imb", "t5_vol",
    "t5_delta", "failed_break_60s", "failed_break_300s",
]
EVENT_INTS = [
    "event_ts_ns", "side", "market_code", "trap_side", "vpoc_on_trap",
    "arm_b", "arm_c", "is_v", "coverage_ok",
]


def events_to_table(rows: list[dict]) -> pa.Table:
    if not rows:
        return pa.table({})
    cols: dict[str, pa.Array] = {}
    cols["event_ts_ns"] = pa.array([int(r["event_ts_ns"]) for r in rows], pa.int64())
    cols["side"] = pa.array([int(r["side"]) for r in rows], pa.int8())
    for name in EVENT_FLOATS:
        cols[name] = pa.array([float(r.get(name, np.nan)) for r in rows], pa.float64())
    for name in EVENT_INTS:
        if name in ("event_ts_ns", "side"):
            continue
        cols[name] = pa.array([int(r.get(name, 0)) for r in rows], pa.int64())
    cols["session_id"] = pa.array([r["session_id"] for r in rows])
    cols["market"] = pa.array([r["market"] for r in rows])
    return pa.table(cols)


def _failed_break(trades: pa.Table, dop_pts: float, side: int, from_ns: int, to_ns: int) -> int:
    """1 if price reclaims through DOP within window (failed break)."""
    if trades.num_rows == 0:
        return 0
    ts, px, _, _ = (
        trades.column("ts_ns").to_numpy().astype(np.int64),
        trades.column("price").to_numpy().astype(np.float64),
        None, None,
    )
    o = np.argsort(ts)
    ts, px = ts[o], px[o]
    k = (ts > from_ns) & (ts <= to_ns)
    if not k.any():
        return 0
    px_w = px[k] * 0.25
    if side > 0:
        # long break failed if print back below DOP
        return int(bool(np.any(px_w <= dop_pts)))
    return int(bool(np.any(px_w >= dop_pts)))


def build_session_events(pair: H.Pair, hist: dict, exp: dict) -> list[dict]:
    dop_cfg = dict(exp.get("daily_open") or {})
    flow_win = float(dop_cfg.get("break_flow_window_sec", 5))
    imb_min = float(dop_cfg.get("break_imb_min", 0.20))
    vol_z_min = float(dop_cfg.get("trap_vol_z_min", 0.0))
    share_min = float(dop_cfg.get("trap_share_min", 0.55))
    v_share = float(dop_cfg.get("v_extreme_vol_share_min", 0.40))
    v_exc = {
        "NQ": float(dop_cfg.get("v_excursion_pts_nq", 5.0)),
        "ES": float(dop_cfg.get("v_excursion_pts_es", 2.0)),
    }
    open_ns, win_end = pair.bounds
    bars = {"NQ": pair.bars(100)[0], "ES": pair.bars(100)[1]}
    rows: list[dict] = []
    sid_hist = hist.get(pair.sid) or {}
    for mkt in ("NQ", "ES"):
        st = sid_hist.get(mkt) or {}
        bk = st.get("break")
        if bk is None:
            continue
        tr = _trades(pair, mkt)
        f5 = flow_at(bars[mkt], bk.event_ts_ns, flow_win)
        al_imb = (
            float(bk.side * f5["imbalance"])
            if np.isfinite(f5["imbalance"]) else float("nan")
        )
        trap_vol_z = float(st.get("trap_vol_z", np.nan))
        trap_delta_z = float(st.get("trap_delta_z", np.nan))
        trap_share = float(bk.trap_share)
        arm_b = int(
            np.isfinite(trap_vol_z) and trap_vol_z >= vol_z_min
            and np.isfinite(trap_share) and trap_share >= share_min
        )
        arm_c = int(arm_b and np.isfinite(al_imb) and al_imb >= imb_min)
        v_flag = int(is_v_shape(
            bk, extreme_vol_share_min=v_share, excursion_pts_min=v_exc[mkt],
        ))
        fail60 = _failed_break(
            tr, bk.dop_pts, bk.side, bk.event_ts_ns,
            min(bk.event_ts_ns + int(60e9), win_end),
        )
        fail300 = _failed_break(
            tr, bk.dop_pts, bk.side, bk.event_ts_ns,
            min(bk.event_ts_ns + int(300e9), win_end),
        )
        rows.append({
            "session_id": pair.sid, "market": mkt,
            "market_code": 0 if mkt == "NQ" else 1,
            "event_ts_ns": bk.event_ts_ns, "side": bk.side,
            "entry_px_pts": bk.entry_px_pts, "dop_pts": bk.dop_pts,
            "trap_side": bk.trap_side,
            "trap_vol": bk.trap_vol, "trap_delta": bk.trap_delta,
            "trap_share": trap_share, "total_build_vol": bk.total_build_vol,
            "trap_vol_z": trap_vol_z, "trap_delta_z": trap_delta_z,
            "trap_vol_vs_med": float(st.get("trap_vol_vs_med", np.nan)),
            "trap_excursion_pts": bk.trap_excursion_pts,
            "build_dur_sec": bk.build_dur_sec,
            "extreme_vol_share": bk.extreme_vol_share,
            "path_efficiency": bk.path_efficiency,
            "time_extreme_to_break_sec": bk.time_extreme_to_break_sec,
            "vpoc_pts": bk.vpoc_pts, "vpoc_on_trap": bk.vpoc_on_trap,
            "r_pts": bk.r_pts, "tp_pts": bk.tp_pts, "sl_pts": bk.sl_pts,
            "t5_imb": f5["imbalance"], "t5_al_imb": al_imb,
            "t5_vol": f5["total_vol"], "t5_delta": f5["delta"],
            "arm_b": arm_b, "arm_c": arm_c, "is_v": v_flag,
            "coverage_ok": int(bool(st.get("coverage_ok"))),
            "failed_break_60s": float(fail60),
            "failed_break_300s": float(fail300),
        })
    return rows


def _filter_table(t: pa.Table, mask: np.ndarray) -> pa.Table:
    if t.num_rows == 0:
        return t
    idx = np.where(mask)[0]
    return t.take(idx) if len(idx) else t.slice(0, 0)


def _avg(summary: SimSummary) -> float:
    return (summary.net / summary.n) if summary.n else float("nan")


def _placebo_p90(trades, *, keep_n, draws, seed, commission) -> dict:
    pnls = np.asarray([t.pnl - commission for t in trades], dtype=np.float64)
    n = len(pnls)
    if n == 0 or keep_n <= 0:
        return {"p90_avg": float("nan"), "p50_avg": float("nan"), "n": 0, "keep_n": int(keep_n)}
    k = min(keep_n, n)
    rng = np.random.default_rng(seed)
    avgs = np.array([float(pnls[rng.choice(n, size=k, replace=False)].mean()) for _ in range(draws)])
    return {
        "p90_avg": float(np.quantile(avgs, 0.90)),
        "p50_avg": float(np.median(avgs)),
        "n": int(n), "keep_n": int(k), "draws": int(draws),
    }


def _early_late_avg(sim: SimSummary, months_early: set[str]) -> tuple[float, float]:
    def avg(pred):
        sub = filter_summary(sim, pred, H.COMMISSION)
        return (sub.net / sub.n) if sub.n else float("nan")
    a = avg(lambda t: _month(t.session_id) in months_early)
    b = avg(lambda t: _month(t.session_id) not in months_early)
    return a, b


def sim_from_trades(trades: list) -> SimSummary:
    from research.backtest.mat_sim import _rebuild_summary
    return _rebuild_summary(list(trades), H.COMMISSION)


def _tercile_masks(vals: np.ndarray) -> dict[str, np.ndarray]:
    finite = vals[np.isfinite(vals)]
    if len(finite) < 9:
        return {}
    q33, q66 = np.quantile(finite, [1 / 3, 2 / 3])
    return {
        "lo": np.isfinite(vals) & (vals <= q33),
        "mid": np.isfinite(vals) & (vals > q33) & (vals <= q66),
        "hi": np.isfinite(vals) & (vals > q66),
    }


def collect(parquet_dir: Path, sids: list[str], hist: dict, exp: dict,
            horizons, targets_by_market) -> dict:
    out = {"events": [], "labeled": {"NQ": {}, "ES": {}}, "sim_trades": {}, "pairs_seen": []}
    lab_store: dict[str, dict[str, list[pa.Table]]] = {
        m: {"A": [], "B": [], "C": [], "null": []} for m in ("NQ", "ES")
    }
    sim_trades: dict[tuple[str, str, str], list] = {}
    rng = np.random.default_rng(int((exp.get("dop_flow") or {}).get("placebo_seed", 78)))
    grace_ns = int(float((exp.get("daily_open") or {}).get("grace_min", 5)) * 60 * 1e9)

    for sid in sids:
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        out["pairs_seen"].append(sid)
        rows = build_session_events(pair, hist, exp)
        out["events"].extend(rows)
        if not rows:
            continue
        for mkt in ("NQ", "ES"):
            mrows = [r for r in rows if r["market"] == mkt]
            if not mrows:
                continue
            targets = targets_by_market[mkt]
            ev = events_to_table(mrows)
            sets = {
                "A": ev,
                "B": _filter_table(ev, np.array([r["arm_b"] == 1 for r in mrows])),
                "C": _filter_table(ev, np.array([r["arm_c"] == 1 for r in mrows])),
            }
            tr = _trades(pair, mkt)
            ts_all = tr.column("ts_ns").to_numpy().astype(np.int64) if tr.num_rows else np.array([], dtype=np.int64)
            after = ts_all[ts_all >= pair.bounds[0] + grace_ns]
            null_rows = []
            if len(after):
                for r in mrows:
                    pick = rng.choice(after, size=min(20, len(after)), replace=False)
                    for p in pick:
                        null_rows.append({
                            "session_id": sid, "market": mkt,
                            "market_code": 0 if mkt == "NQ" else 1,
                            "event_ts_ns": int(p), "side": int(r["side"]),
                        })
            sets["null"] = events_to_table(null_rows) if null_rows else pa.table({})
            for name, tbl in sets.items():
                if tbl.num_rows:
                    lab = H.label_in_points(tbl, pair, horizons, targets, role=mkt)
                    if lab.num_rows:
                        lab_store[mkt][name].append(lab)
            for arm_name, arm_tbl in (("A", sets["A"]), ("B", sets["B"]), ("C", sets["C"])):
                if arm_tbl.num_rows == 0:
                    continue
                traded = pair.nq if mkt == "NQ" else pair.es
                cfg = MatSimConfig(
                    slip_ticks=float(exp.get("slip_ticks", 1.0)),
                    commission_pts_rt=H.COMMISSION,
                )
                # Book 1: per-event tp/sl already on table
                s1 = simulate_session(
                    sid, traded, pair.es, cfg,
                    vwap_from_ns=pair.bounds[0], events=arm_tbl,
                )
                sim_trades.setdefault((mkt, "book1", arm_name), []).extend(s1.trades)
                tbl2 = arm_tbl.drop(["tp_pts", "sl_pts"]) if "tp_pts" in arm_tbl.column_names else arm_tbl
                s2 = simulate_session(
                    sid, traded, pair.es, cfg,
                    vwap_from_ns=pair.bounds[0],
                    bracket_fn=make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0),
                    events=tbl2,
                )
                sim_trades.setdefault((mkt, "book2", arm_name), []).extend(s2.trades)
        print(f"  [evt] {sid}: events={len(rows)}", flush=True)

    for mkt in ("NQ", "ES"):
        for name, parts in lab_store[mkt].items():
            out["labeled"][mkt][name] = parts
    out["sim_trades"] = sim_trades
    return out


def run(exp_path: Path | None = None, stage3: bool = False) -> dict:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / "EXP-0078"
    exp = yaml.safe_load(
        (exp_path or Path("research/experiments/EXP-0078.yaml")).read_text(encoding="utf-8")
    )
    horizons = list(exp.get("horizons_ms") or [10000, 60000, 300000])
    targets_by_market = {
        "NQ": list(exp.get("point_targets_nq") or [5, 10, 20]),
        "ES": list(exp.get("point_targets_es") or [2, 4, 8]),
    }
    exclude = set(exp.get("exclude_sessions") or [])
    disc_months = set(exp.get("discovery_months") or [])
    hold_months = set(exp.get("holdout_months") or [])
    kill = dict(exp.get("kill") or {})
    dop_cfg = dict(exp.get("daily_open") or {})
    of_cfg = dict(exp.get("dop_flow") or {})

    sids_all = all_sessions(parquet_dir, exclude)
    print(f"[0078] {len(sids_all)} sessions; building DOP break history (14:30–21:00)", flush=True)
    hist = compute_break_history(parquet_dir, sids_all, dop_cfg)

    disc_ids = [s for s in sids_all if _month(s) in disc_months]
    report: dict = {"status": "ok", "experiment": exp["name"], "stages": {}}
    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        "Frozen design: `research/experiments/EXP-0078.yaml` / H69. "
        "DOP = first print at/after UK 14:30; grace 5 min; first ≥1-tick break "
        "away from majority-volume trap side; long and short mirrored. "
        f"Arm B = trap_vol_z ≥ {dop_cfg.get('trap_vol_z_min')} ∧ "
        f"trap_share ≥ {dop_cfg.get('trap_share_min')}.",
        "",
    ]

    data = collect(parquet_dir, disc_ids, hist, exp, horizons, targets_by_market)
    events = data["events"]
    ev_table = events_to_table(events)
    results_dir.mkdir(parents=True, exist_ok=True)
    if ev_table.num_rows:
        pq.write_table(ev_table, results_dir / "events.parquet", compression="zstd")

    # ---------------------------------------------------------- Stage 0
    counts = Counter()
    n_long = n_short = 0
    cov_ok = dop_ok = 0
    n_sess_cov = 0
    for sid in disc_ids:
        sh = hist.get(sid) or {}
        for mkt in ("NQ", "ES"):
            st = sh.get(mkt) or {}
            if st.get("coverage_ok"):
                cov_ok += 1
            if st.get("dop_ok"):
                dop_ok += 1
            n_sess_cov += 1
    for r in events:
        counts[f"{r['market']}_events"] += 1
        counts[f"{r['market']}_armB"] += int(r["arm_b"])
        if r["side"] > 0:
            n_long += 1
        else:
            n_short += 1

    n_events = len(events)
    n_armb = sum(int(r["arm_b"]) for r in events)
    n_nq = counts.get("NQ_events", 0)
    n_es = counts.get("ES_events", 0)

    # causality: truncation invariance on a few events
    trunc_rows = []
    hand_rows = []
    checked = 0
    for sid in disc_ids:
        srows = [r for r in events if r["session_id"] == sid]
        if not srows or checked >= 3:
            continue
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            continue
        checked += 1
        for r in srows[:1]:
            mkt = r["market"]
            tr = _trades(pair, mkt)
            cut = _cut_before(tr, int(r["event_ts_ns"]))
            open_ns = pair.bounds[0]
            dop_full = session_dop(tr, open_ns)
            dop_cut = session_dop(cut, open_ns)
            same_dop = (
                dop_full.valid and dop_cut.valid
                and abs(dop_full.dop_ticks - dop_cut.dop_ticks) < 1e-9
            )
            bk_full = detect_dop_break(
                tr, dop_full, open_ns, pair.bounds[1],
                grace_min=float(dop_cfg.get("grace_min", 5)),
            ) if dop_full.valid else None
            bk_cut = detect_dop_break(
                cut, dop_cut, open_ns, int(r["event_ts_ns"]),
                grace_min=float(dop_cfg.get("grace_min", 5)),
            ) if dop_cut.valid else None
            same_break = (
                bk_full is not None and bk_cut is not None
                and bk_full.event_ts_ns == bk_cut.event_ts_ns
                and bk_full.side == bk_cut.side
                and abs(bk_full.trap_vol - bk_cut.trap_vol) < 1e-6
            )
            trunc_rows.append({
                "session": sid, "market": mkt,
                "dop_match": bool(same_dop), "break_match": bool(same_break),
                "match": bool(same_dop and same_break),
            })
            # hand-check DOP vs first 1-min bar open
            bars1m = trades_to_tape_bars(tr, bar_ms=60000)
            if bars1m.num_rows and dop_full.valid:
                ts1 = bars1m.column("ts_ns").to_numpy().astype(np.int64)
                k1 = (ts1 >= open_ns) & (ts1 < open_ns + NS_MIN)
                if k1.any():
                    first_open = float(bars1m.column("open").to_numpy()[k1][0]) * 0.25
                    hand_rows.append({
                        "session": sid, "market": mkt,
                        "match": abs(first_open - dop_full.dop_pts) < 1e-9,
                        "dop_pts": dop_full.dop_pts, "bar_open": first_open,
                    })

    trunc_ok = all(r["match"] for r in trunc_rows) if trunc_rows else False
    hand_ok = all(r["match"] for r in hand_rows) if hand_rows else False
    min_a = int(kill.get("min_arm_a_per_market", 30))
    min_side = int(kill.get("min_side_pooled", 10))
    power_ok = n_nq >= min_a and n_es >= min_a and n_long >= min_side and n_short >= min_side
    coverage = {
        "discovery_sessions": len(disc_ids),
        "n_sessions_seen": len(data["pairs_seen"]),
        "n_events": n_events, "n_armB": n_armb,
        "n_long": n_long, "n_short": n_short,
        "by_market": dict(sorted(counts.items())),
        "coverage_ok_market_sessions": cov_ok,
        "dop_ok_market_sessions": dop_ok,
        "market_session_slots": n_sess_cov,
    }
    report["stages"]["0"] = {
        "coverage": coverage, "truncation": trunc_rows, "handcheck": hand_rows,
        "truncation_ok": trunc_ok, "handcheck_ok": hand_ok, "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — coverage, causality, power",
        "",
        f"Discovery sessions seen: {len(data['pairs_seen'])} · break events: **{n_events}** "
        f"(NQ {n_nq} / ES {n_es}; long {n_long} / short {n_short}) · "
        f"Arm B kept: **{n_armb}** · dropped: **{n_events - n_armb}**",
        f"Tape coverage (≥20:30 UK): {cov_ok}/{n_sess_cov} market-sessions · "
        f"DOP valid: {dop_ok}/{n_sess_cov}",
        f"Truncation invariance: **{'PASS' if trunc_ok else 'FAIL'}** ({len(trunc_rows)} checks) · "
        f"DOP hand-check vs 1-min open: **{'PASS' if hand_ok else 'FAIL'}** ({len(hand_rows)} checks)",
        f"Power (Arm A ≥ {min_a}/market and ≥ {min_side} each side): "
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
        report["verdict"] = (
            "reject / park — Stage 0 underpowered (counts are the result; "
            "no event study, no P&L, no holdout, no retune)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 1
    stage1: dict = {}
    md += [
        "## Stage 1 — event study (discovery)",
        "",
        "| market | set | n | hit(small) | hit(mid) | hit(large) | @10s | @60s | @300s | MFE | MAE |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mkt in ("NQ", "ES"):
        targets = targets_by_market[mkt]
        for name in ("A", "B", "C", "null"):
            s = H.summarize(data["labeled"][mkt][name], horizons, targets)
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
                    f"{H.fmt(s.get('mean_mfe_pts'), 1)} | {H.fmt(s.get('mean_mae_pts'), 1)} |"
                )

    # dose-response + side/shape strata on Arm A
    md += ["", "### Dose-response (Arm A, trap-side amount)", ""]
    dose: dict = {}
    for mkt in ("NQ", "ES"):
        parts = data["labeled"][mkt]["A"]
        if not parts:
            continue
        df = pa.concat_tables(parts, promote_options="default")
        targets = targets_by_market[mkt]
        for feat in ("trap_vol", "trap_delta", "trap_vol_z", "trap_delta_z"):
            if feat not in df.column_names:
                continue
            vals = df.column(feat).to_numpy().astype(np.float64)
            masks = _tercile_masks(vals)
            for lab, mask in masks.items():
                sub = _filter_table(df, mask)
                if sub.num_rows:
                    key = f"{mkt}_A_{feat}_{lab}"
                    dose[key] = H.summarize([sub], horizons, targets)
        # side split
        side = df.column("side").to_numpy() if "side" in df.column_names else None
        if side is not None:
            for lab, mask in (("long", side > 0), ("short", side < 0)):
                sub = _filter_table(df, mask)
                if sub.num_rows:
                    dose[f"{mkt}_A_{lab}"] = H.summarize([sub], horizons, targets)
        if "is_v" in df.column_names:
            iv = df.column("is_v").to_numpy()
            for lab, mask in (("v_shape", iv == 1), ("sideways", iv == 0)):
                sub = _filter_table(df, mask)
                if sub.num_rows:
                    dose[f"{mkt}_A_{lab}"] = H.summarize([sub], horizons, targets)
        # joint high vol + high delta
        if "trap_vol_z" in df.column_names and "trap_delta_z" in df.column_names:
            vz = df.column("trap_vol_z").to_numpy().astype(np.float64)
            dz = df.column("trap_delta_z").to_numpy().astype(np.float64)
            both = np.isfinite(vz) & np.isfinite(dz) & (vz >= 0) & (dz >= 0)
            sub = _filter_table(df, both)
            if sub.num_rows:
                dose[f"{mkt}_A_hi_vol_and_delta"] = H.summarize([sub], horizons, targets)

    stage1["dose_response"] = dose
    md += [
        "| stratum | n | @60s | hit mid |",
        "|---|---:|---:|---:|",
    ]
    for k, v in sorted(dose.items()):
        mkt = k.split("_")[0]
        targets = targets_by_market[mkt]
        mid = targets[1]
        md.append(
            f"| {k} | {v.get('n_events', 0)} | "
            f"{H.fmt(v.get('mean_ret_pts_60000ms'), 2)} | "
            f"{v.get(f'p_hit_{int(mid)}pts', float('nan')):.2f} |"
        )
    report["stages"]["1"] = stage1
    md += [""]

    # ---------------------------------------------------------- Stage 2
    stage2: dict = {}
    md += [
        "## Stage 2 — P&L (discovery)",
        "",
        "| market | book | Arm A n/net/avg | Arm B n/net/avg | placebo p90 avg | checks |",
        "|---|---|---|---|---|---|",
    ]
    early_months = {"0426", "0526", "0626"}
    any_market_pass = False
    other_not_bad = True
    avgs: dict[str, tuple[float, float]] = {}
    min_kept = int(kill.get("min_kept_n", 20))
    for mkt in ("NQ", "ES"):
        lift_min = float(kill.get(
            "min_avg_lift_pts_nq" if mkt == "NQ" else "min_avg_lift_pts_es", 1.0,
        ))
        for book in ("book1", "book2"):
            tr_a = data["sim_trades"].get((mkt, book, "A"), [])
            tr_b = data["sim_trades"].get((mkt, book, "B"), [])
            sim_a, sim_b = sim_from_trades(tr_a), sim_from_trades(tr_b)
            avg_a, avg_b = _avg(sim_a), _avg(sim_b)
            placebo = _placebo_p90(
                tr_a, keep_n=sim_b.n,
                draws=int(of_cfg.get("placebo_draws", 1000)),
                seed=int(of_cfg.get("placebo_seed", 78)),
                commission=H.COMMISSION,
            )
            e_a, l_a = _early_late_avg(sim_a, early_months)
            e_b, l_b = _early_late_avg(sim_b, early_months)
            checks = {
                "kept_n_ok": sim_b.n >= min_kept,
                "avg_lift_ok": bool(
                    np.isfinite(avg_b) and np.isfinite(avg_a) and avg_b >= avg_a + lift_min
                ),
                "net_retained_ok": (
                    bool(sim_b.net >= float(kill.get("min_net_retained_frac", 0.60)) * sim_a.net)
                    if sim_a.net > 0 else bool(sim_b.net >= sim_a.net)
                ),
                "early_avg_not_worse": bool(
                    not np.isfinite(e_a) or (np.isfinite(e_b) and e_b >= e_a - 1e-9)
                ),
                "late_avg_not_worse": bool(
                    not np.isfinite(l_a) or (np.isfinite(l_b) and l_b >= l_a - 1e-9)
                ),
                "maxdd_ok": sim_b.max_dd <= sim_a.max_dd + 1e-9,
                "placebo_ok": bool(
                    np.isfinite(avg_b) and np.isfinite(placebo["p90_avg"])
                    and avg_b > placebo["p90_avg"]
                ),
            }
            passed = all(checks.values())
            # side soft-flag
            long_b = [t for t in tr_b if t.side > 0]
            short_b = [t for t in tr_b if t.side < 0]
            side_note = {
                "long_n": len(long_b),
                "long_avg": float(np.mean([t.pnl - H.COMMISSION for t in long_b])) if long_b else float("nan"),
                "short_n": len(short_b),
                "short_avg": float(np.mean([t.pnl - H.COMMISSION for t in short_b])) if short_b else float("nan"),
            }
            stage2[f"{mkt}_{book}"] = {
                "armA": {**sim_a.to_dict(), "avg": avg_a},
                "armB": {**sim_b.to_dict(), "avg": avg_b},
                "placebo": placebo, "checks": checks, "pass": passed,
                "side_split_armB": side_note,
            }
            md.append(
                f"| {mkt} | {book} | {sim_a.n}/{sim_a.net:+.1f}/{avg_a:+.2f} | "
                f"{sim_b.n}/{sim_b.net:+.1f}/{avg_b:+.2f} | {placebo['p90_avg']:+.2f} | "
                f"{'PASS' if passed else 'FAIL'} |"
            )
            if book == "book2":
                avgs[mkt] = (avg_a, avg_b)
                if passed:
                    any_market_pass = True
    for mkt, (a, b) in avgs.items():
        lift_min = float(kill.get(
            "min_avg_lift_pts_nq" if mkt == "NQ" else "min_avg_lift_pts_es", 1.0,
        ))
        if np.isfinite(a) and np.isfinite(b) and b < a - lift_min:
            other_not_bad = False
    stage2_pass = bool(any_market_pass and other_not_bad)
    report["stages"]["2"] = {"books": stage2, "pass": stage2_pass}
    md += ["", f"**Stage 2 (Book 2 primary yardstick): {'PASS' if stage2_pass else 'FAIL'}**", ""]

    # soft side flag
    for key, cell in stage2.items():
        if not key.endswith("_book2"):
            continue
        ss = cell.get("side_split_armB") or {}
        md.append(
            f"- {key} Arm B side split: long n={ss.get('long_n')} avg={H.fmt(ss.get('long_avg'), 2)}; "
            f"short n={ss.get('short_n')} avg={H.fmt(ss.get('short_avg'), 2)}"
        )
    md += [""]

    if not stage2_pass:
        report["verdict"] = "reject / park — Stage 2 discovery P&L fail (no holdout read, no retune)"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        _write_verdict_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    if not stage3:
        report["verdict"] = (
            "discovery PASS — frozen; holdout (Sep-Oct 2026) not yet read. "
            "Re-run with --stage3 exactly once to read it."
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        print(report["verdict"], flush=True)
        return report

    # ---------------------------------------------------------- Stage 3
    hold_ids = [s for s in sids_all if _month(s) in hold_months]
    hdata = collect(parquet_dir, hold_ids, hist, exp, horizons, targets_by_market)
    hevents = hdata["events"]
    if hevents:
        pq.write_table(
            events_to_table(hevents),
            results_dir / "holdout_events.parquet",
            compression="zstd",
        )
    hstage: dict = {}
    md += [
        "## Stage 3 — Sep-Oct 2026 holdout (read once, frozen thresholds)",
        "",
        "| market | book | Arm A n/net/avg | Arm B n/net/avg |",
        "|---|---|---|---|",
    ]
    hold_pass_by_market: dict[str, bool] = {}
    for mkt in ("NQ", "ES"):
        ok_market = False
        for book in ("book1", "book2"):
            tr_a = hdata["sim_trades"].get((mkt, book, "A"), [])
            tr_b = hdata["sim_trades"].get((mkt, book, "B"), [])
            sim_a, sim_b = sim_from_trades(tr_a), sim_from_trades(tr_b)
            avg_a, avg_b = _avg(sim_a), _avg(sim_b)
            hstage[f"{mkt}_{book}"] = {
                "armA": {**sim_a.to_dict(), "avg": avg_a},
                "armB": {**sim_b.to_dict(), "avg": avg_b},
            }
            md.append(
                f"| {mkt} | {book} | {sim_a.n}/{sim_a.net:+.1f}/{avg_a:+.2f} | "
                f"{sim_b.n}/{sim_b.net:+.1f}/{avg_b:+.2f} |"
            )
            if (
                book == "book2" and np.isfinite(avg_a) and np.isfinite(avg_b)
                and avg_b > avg_a and sim_b.net > 0
            ):
                ok_market = True
        hold_pass_by_market[mkt] = ok_market
    other_ok = True
    for mkt in ("NQ", "ES"):
        if not hold_pass_by_market[mkt]:
            tr_b = hdata["sim_trades"].get((mkt, "book2", "B"), [])
            sim_b = sim_from_trades(tr_b)
            lift_min = float(kill.get(
                "min_avg_lift_pts_nq" if mkt == "NQ" else "min_avg_lift_pts_es", 1.0,
            ))
            if sim_b.n and _avg(sim_b) < -lift_min:
                other_ok = False
    hold_pass = any(hold_pass_by_market.values()) and other_ok
    report["stages"]["3"] = {
        "sessions": hold_ids, "books": hstage,
        "pass_by_market": hold_pass_by_market, "pass": hold_pass,
    }
    report["verdict"] = (
        "PASS holdout — eligible for Bookmap DOP module spec + parity (separate phase)"
        if hold_pass else
        "reject / park — Stage 3 holdout fail (no retune)"
    )
    md += ["", f"**Stage 3: {'PASS' if hold_pass else 'FAIL'}**", "",
           "## Verdict", "", f"**{report['verdict']}**", ""]
    H.write_report(results_dir, report, md)
    _write_verdict_docs(report["verdict"])
    print(report["verdict"], flush=True)
    return report


def _write_verdict_docs(verdict: str) -> None:
    """Patch HYPOTHESES + EXPERIMENT_LOG with the run verdict."""
    hyp = Path("docs/HYPOTHESES.md")
    text = hyp.read_text(encoding="utf-8")
    old = "**Result:** pending."
    new = f"**Result (EXP-0078): {verdict}.** See `results/EXP-0078/`."
    if old in text:
        hyp.write_text(text.replace(old, new, 1), encoding="utf-8")
    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    lt = lt.replace(
        "| EXP-0078 | H69 DOP break trap-side amount | registered — pending run | Apr–Aug discovery; Sep–Oct holdout once | enter at first DOP break; Arm B = trap_vol_z≥0 ∩ share≥0.55; see notes |",
        f"| EXP-0078 | H69 DOP break trap-side amount | ran — **{verdict.split('—')[0].strip()}** | Apr–Aug discovery | {verdict}; see notes |",
        1,
    )
    note = (
        f"\n### 2026-10-27 — EXP-0078 DOP break run (H69)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0078/`.\n"
        f"- Runner: `python -m research.dop.exp_0078_dop`.\n"
    )
    if "### 2026-10-27 — EXP-0078 DOP break run" not in lt:
        anchor = "### 2026-10-27 — EXP-0078 Daily Open (DOP) break registered (H69)"
        if anchor in lt:
            # insert run note after the registration block's first blank-line-separated section
            idx = lt.find(anchor)
            # find next ### after this one
            nxt = lt.find("\n### ", idx + 5)
            if nxt > 0:
                lt = lt[:nxt] + note + lt[nxt:]
            else:
                lt = lt + note
        else:
            lt = note + lt
    log.write_text(lt, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--stage3", action="store_true",
        help="read the Sep-Oct 2026 holdout ONCE (only after a discovery pass)",
    )
    ap.add_argument("--exp", type=Path, default=None)
    args = ap.parse_args()
    run(args.exp, stage3=args.stage3)


if __name__ == "__main__":
    main()
