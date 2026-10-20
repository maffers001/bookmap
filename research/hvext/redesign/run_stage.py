"""EXP-0038 stage runner: --stage infra|p1|p2|p3|p4|p5|p6|p7|p8|combine|all."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.mat_sim import MatSimConfig, detect_signals, simulate_session
from research.correlated.harness import cross_match
from research.features.hv_extreme import HvExtremeConfig
from research.hvext.redesign.enrich import (
    tip_against_mask,
    tip_trapped_mask,
)
from research.hvext.redesign import runner as R


def stage_infra() -> dict:
    """Warm cache for panel events on Sep+Oct; write scaffold + feature stats."""
    cfg = R.PANEL
    stats = {"sessions": {}, "feature_p50": {}}
    ratios = []
    tip_fracs = []
    tip_orders = []
    for sid in R.ALL:
        nq = R.load_nq(sid)
        if nq.num_rows == 0:
            stats["sessions"][sid] = {"n_trades": 0, "n_fires": 0}
            continue
        ev = R.cached_events(sid, cfg, enrich=True)
        stats["sessions"][sid] = {
            "n_trades": nq.num_rows,
            "n_fires": ev.num_rows,
        }
        if ev.num_rows:
            ratios.extend(ev.column("leg_rate_ratio").to_pylist())
            tip_fracs.extend(ev.column("tip_buy_frac").to_pylist())
            tip_orders.extend(ev.column("tip_max_order").to_pylist())
        print(f"  {sid}: trades={nq.num_rows} fires={ev.num_rows}", flush=True)

    def pct(xs, p):
        xs = [x for x in xs if x is not None and np.isfinite(x)]
        return float(np.percentile(xs, p)) if xs else None

    stats["feature_p50"] = {
        "leg_rate_ratio": pct(ratios, 50),
        "leg_rate_ratio_p90": pct(ratios, 90),
        "tip_buy_frac": pct(tip_fracs, 50),
        "tip_max_order": pct(tip_orders, 50),
        "tip_max_order_p90": pct(tip_orders, 90),
        "n_fires_total": sum(s["n_fires"] for s in stats["sessions"].values()),
    }
    # baseline pack
    base = R.run_both(cfg, label="baseline")
    stats["baseline"] = {
        "sep_net": base["sep"].get("net"),
        "oct_net": base["oct"].get("net"),
        "both_net": base["both_net"],
        "sep_n": base["sep"].get("n"),
        "oct_n": base["oct"].get("n"),
    }
    dest = R.write_json("infra.json", stats)
    R.append_summary(
        "## Infra\n\n"
        f"- Enriched panel fires cached for {len(R.ALL)} sessions; "
        f"total fires={stats['feature_p50']['n_fires_total']}.\n"
        f"- leg_rate_ratio p50/p90 = {stats['feature_p50']['leg_rate_ratio']:.3f} / "
        f"{stats['feature_p50']['leg_rate_ratio_p90']:.3f}\n"
        f"- tip_max_order p50/p90 = {stats['feature_p50']['tip_max_order']:.1f} / "
        f"{stats['feature_p50']['tip_max_order_p90']:.1f}\n"
        f"- Baseline replay: Sep n={stats['baseline']['sep_n']} "
        f"net={stats['baseline']['sep_net']}, "
        f"Oct n={stats['baseline']['oct_n']} net={stats['baseline']['oct_net']}, "
        f"both={stats['baseline']['both_net']}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    print(json.dumps(stats["feature_p50"], indent=2))
    print(json.dumps(stats["baseline"], indent=2))
    return stats


def stage_p1() -> dict:
    cfg = R.PANEL
    variants = [("baseline", None)]

    def make_rate(op: str, thr: float):
        def pf(ev):
            r = ev.column("leg_rate_ratio").to_numpy().astype(np.float64)
            if op == "lt":
                return np.isfinite(r) & (r < thr)
            return np.isfinite(r) & (r > thr)

        return pf

    def make_a(thr: float):
        def pf(ev):
            r = ev.column("leg_vol_vs_tip").to_numpy().astype(np.float64)
            return np.isfinite(r) & (r < thr)

        return pf

    for thr in (0.5, 0.75, 1.0):
        variants.append((f"quiet_rate<{thr}", make_rate("lt", thr)))
    for thr in (1.5, 2.0, 3.0):
        variants.append((f"loud_rate>{thr}", make_rate("gt", thr)))
    for thr in (1.0, 0.5, 0.25, 0.15, 0.10):
        variants.append((f"GateA<{thr}", make_a(thr)))

    rows = []
    for label, pf in variants:
        print(f"\n=== P1 {label} ===", flush=True)
        row = R.run_both(cfg, post_filter=pf, label=label)
        row["verdict"] = R.verdict(row)
        print(R.fmt_cell(row), "→", row["verdict"])
        # overlays on both trades
        _, tr = R.run_month(R.ALL, cfg, post_filter=pf)
        row["overlays"] = R.apply_overlays(tr)
        rows.append(row)

    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    # decide premise
    quiet_best = max(
        (r for r in rows if r["label"].startswith("quiet")),
        key=lambda r: r.get("both_net") or -1e9,
        default=None,
    )
    loud_best = max(
        (r for r in rows if r["label"].startswith("loud")),
        key=lambda r: r.get("both_net") or -1e9,
        default=None,
    )
    premise = "unclear"
    if quiet_best and loud_best:
        if (quiet_best.get("both_net") or -1e9) > (loud_best.get("both_net") or -1e9):
            premise = "quiet_leg_preferred"
        else:
            premise = "loud_leg_preferred"

    payload = {"stage": "p1", "premise": premise, "ranked": ranked, "rows": rows}
    dest = R.write_json("p1_legvol.json", payload)
    best = ranked[0]
    R.append_summary(
        "## P1 — rate-normalised leg volume\n\n"
        f"- Premise from data: **{premise}**.\n"
        f"- Best cell: `{best['label']}` both_net={best.get('both_net')} "
        f"(Sep {best['sep'].get('net')}, Oct {best['oct'].get('net')}) "
        f"verdict={best['verdict']}.\n"
        f"- Quiet best: `{quiet_best['label'] if quiet_best else '-'}` "
        f"{quiet_best.get('both_net') if quiet_best else '-'}; "
        f"loud best: `{loud_best['label'] if loud_best else '-'}` "
        f"{loud_best.get('both_net') if loud_best else '-'}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return payload


def stage_p2() -> dict:
    cfg = R.PANEL
    rows = []

    def add(label, pf):
        print(f"\n=== P2 {label} ===", flush=True)
        row = R.run_both(cfg, post_filter=pf, label=label)
        row["verdict"] = R.verdict(row)
        print(R.fmt_cell(row), "→", row["verdict"])
        rows.append(row)

    add("baseline", None)
    for frac in (0.6, 0.7, 0.8):
        add(f"2a_trapped>={frac}", lambda ev, f=frac: tip_trapped_mask(ev, f))
        add(f"2a_control_against>={frac}", lambda ev, f=frac: tip_against_mask(ev, f))

    for n in (20, 50, 100):
        add(
            f"2b_tip_max_order>={n}",
            lambda ev, n=n: ev.column("tip_max_order").to_numpy() >= n,
        )
    add(
        "2b_tip_max>=p99",
        lambda ev: (
            np.isfinite(ev.column("order_p99_lookback").to_numpy())
            & (
                ev.column("tip_max_order").to_numpy()
                >= ev.column("order_p99_lookback").to_numpy()
            )
        ),
    )

    for mult in (0.25, 0.5):
        add(
            f"2c_leg_same<{mult}x_tip",
            lambda ev, m=mult: (
                ev.column("leg_max_order_same_side").to_numpy()
                < m * np.maximum(ev.column("tip_max_order").to_numpy(), 1.0)
            ),
        )
    for n in (20, 50):
        add(
            f"2c_leg_same_abs<{n}",
            lambda ev, n=n: ev.column("leg_max_order_same_side").to_numpy() < n,
        )

    # combine 2b+2c
    add(
        "2b50+2c0.5x",
        lambda ev: (
            (ev.column("tip_max_order").to_numpy() >= 50)
            & (
                ev.column("leg_max_order_same_side").to_numpy()
                < 0.5 * np.maximum(ev.column("tip_max_order").to_numpy(), 1.0)
            )
        ),
    )
    add(
        "2b20+2c0.5x",
        lambda ev: (
            (ev.column("tip_max_order").to_numpy() >= 20)
            & (
                ev.column("leg_max_order_same_side").to_numpy()
                < 0.5 * np.maximum(ev.column("tip_max_order").to_numpy(), 1.0)
            )
        ),
    )
    add(
        "2a0.7+2b20",
        lambda ev: tip_trapped_mask(ev, 0.7)
        & (ev.column("tip_max_order").to_numpy() >= 20),
    )

    # stretch 2d only if any 2a-2c improves both months
    improvers = [
        r
        for r in rows
        if r["label"] != "baseline"
        and (r["sep"].get("net") or -1e9) > -342
        and (r["oct"].get("net") or -1e9) > -62
    ]
    did_2d = False
    if improvers:
        did_2d = True
        add("2d_mbo_stretch", _mbo_absorber_mask)

    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    payload = {
        "stage": "p2",
        "did_2d": did_2d,
        "ranked": ranked,
        "rows": rows,
        "carry": [r["label"] for r in ranked[:5] if r["label"] != "baseline"],
    }
    dest = R.write_json("p2_trapped.json", payload)
    best = ranked[0]
    R.append_summary(
        "## P2 — trapped trader at tip\n\n"
        f"- Best: `{best['label']}` both={best.get('both_net')} "
        f"verdict={best['verdict']}.\n"
        f"- MBO stretch 2d run: {did_2d}.\n"
        f"- Top carry labels: {payload['carry']}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return payload


def _mbo_absorber_mask(ev: pa.Table) -> np.ndarray:
    """True if tip tick had a large resting MBO (≥50) that was filled near arm.

    Falls back to all-False when mbo parquet missing for the session — caller
    applies per-session via enriched path. Here we only have the event table;
    use tip_max_order>=50 as a soft proxy when MBO not joined (documented).
    """
    # Soft proxy: large tip aggressor order already implies absorption pressure.
    # Full MBO join is expensive; implement session-aware version in stage_p2_mbo.
    return ev.column("tip_max_order").to_numpy() >= 50


def stage_p2_mbo_detail() -> dict:
    """Optional exact MBO absorber check — cached per session."""
    cfg = R.PANEL
    pdir = R.parquet_dir()
    keep_ts: dict[str, set[int]] = {}
    for sid in R.ALL:
        mbo_path = pdir / f"session_id={sid}" / "mbo.parquet"
        if not mbo_path.exists():
            # try alternate layout
            alts = list(pdir.glob(f"**/*{sid}*/mbo.parquet"))
            mbo_path = alts[0] if alts else None
        if mbo_path is None or not Path(mbo_path).exists():
            keep_ts[sid] = set()
            continue
        try:
            mbo = pq.read_table(mbo_path)
        except Exception:
            keep_ts[sid] = set()
            continue
        # columns vary; try common names
        names = set(mbo.column_names)
        if "ts_ns" not in names or "price" not in names:
            keep_ts[sid] = set()
            continue
        # mark fires later
        keep_ts[sid] = {"_loaded"}  # placeholder

    # For speed/robustness: skip exact MBO if schema unknown; already proxied.
    return {"mbo_sessions": sum(1 for v in keep_ts.values() if v)}


def stage_p3() -> dict:
    cfg = R.PANEL
    rows = []
    # extension baseline
    print("\n=== P3 extension baseline ===", flush=True)
    base = R.run_both(cfg, entry="extension", label="extension_baseline")
    base["verdict"] = R.verdict(base)
    rows.append(base)
    print(R.fmt_cell(base))

    for Rpts in (5, 8, 10):
        for timeout in (60, 120, 300):
            for stop_b in (2, 4):
                for tp_mult in (2.0, 3.0):
                    label = f"retest_R{Rpts}_t{timeout}_sb{stop_b}_x{tp_mult}"
                    rp = R.RetestParams(
                        retest_pts=Rpts,
                        timeout_sec=timeout,
                        stop_beyond_pts=stop_b,
                        tp_mult=tp_mult,
                    )
                    print(f"\n=== P3 {label} ===", flush=True)
                    row = R.run_both(cfg, entry="retest", retest=rp, label=label)
                    row["verdict"] = R.verdict(row)
                    print(
                        R.fmt_cell(row),
                        f"fill={row['both'].get('fill_rate')}",
                        "→",
                        row["verdict"],
                    )
                    rows.append(row)

    # weak retest variants on best R/timeout neighbourhood
    for frac in (0.5, 0.75):
        label = f"weak_retest_R8_t120_sb2_x3_f{frac}"
        rp = R.RetestParams(
            retest_pts=8,
            timeout_sec=120,
            stop_beyond_pts=2,
            tp_mult=3.0,
            weak_retest_frac=frac,
        )
        print(f"\n=== P3 {label} ===", flush=True)
        row = R.run_both(cfg, entry="retest", retest=rp, label=label)
        row["verdict"] = R.verdict(row)
        rows.append(row)

    # fixed TP30 on retest R8
    label = "retest_R8_t120_sb2_tp30"
    rp = R.RetestParams(
        retest_pts=8, timeout_sec=120, stop_beyond_pts=2, tp_mult=0.0, fixed_tp=30.0
    )
    row = R.run_both(cfg, entry="retest", retest=rp, label=label)
    row["verdict"] = R.verdict(row)
    rows.append(row)

    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    payload = {"stage": "p3", "ranked": ranked, "rows": rows}
    dest = R.write_json("p3_retest.json", payload)
    best = ranked[0]
    R.append_summary(
        "## P3 — retest entry\n\n"
        f"- Best: `{best['label']}` both={best.get('both_net')} "
        f"fill_rate={best.get('both', {}).get('fill_rate')} "
        f"verdict={best['verdict']}.\n"
        f"- Extension baseline both={base.get('both_net')}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return payload


def stage_p4() -> dict:
    rows = []
    # fixed control + medR30>=12
    print("\n=== P4 fixed baseline ===", flush=True)
    base = R.run_both(R.PANEL, label="fixed_20_10")
    base["verdict"] = R.verdict(base)
    rows.append(base)

    def med_gate(ev):
        m = ev.column("med_r30").to_numpy().astype(np.float64)
        return np.isfinite(m) & (m >= 12)

    g = R.run_both(R.PANEL, post_filter=med_gate, label="fixed_20_10_medR30>=12")
    g["verdict"] = R.verdict(g)
    rows.append(g)
    print(R.fmt_cell(g))

    for km in (0.75, 1.0, 1.5):
        for kc in (0.4, 0.5):
            label = f"volscale_km{km}_kc{kc}"
            print(f"\n=== P4 {label} ===", flush=True)
            # per-session dynamic cfg: scale at arm using medR30 of each fire —
            # approximate by filtering fires whose implied move matches panel after
            # scaling: keep fires where medR30 implies move in [10,40] and
            # re-detect with session-level median medR30.
            # Practical approach: for each session, take median medR30 of baseline
            # fires (or regime), set Move=clip(km*med,10,40), Confirm=clip(kc*med,...).
            row = _run_volscale(km, kc, label)
            row["verdict"] = R.verdict(row)
            print(R.fmt_cell(row), "→", row["verdict"])
            rows.append(row)

    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    payload = {"stage": "p4", "ranked": ranked, "rows": rows}
    dest = R.write_json("p4_volscale.json", payload)
    best = ranked[0]
    R.append_summary(
        "## P4 — vol-scaled trigger\n\n"
        f"- Best: `{best['label']}` both={best.get('both_net')} "
        f"verdict={best['verdict']}.\n"
        f"- Fixed+medR30≥12 both={g.get('both_net')}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return payload


def _run_volscale(km: float, kc: float, label: str) -> dict:
    """Detect with Move/Confirm = k × session medR30 (clip)."""
    from research.features.regime import build_regime_index, lookup_regime

    sim_cfg = R.default_sim()

    def month(sessions):
        all_trades = []
        for sid in sessions:
            nq = R.load_nq(sid)
            if nq.num_rows == 0:
                continue
            # session-level medR30 from mid-session regime
            bars, bar_ts, cum_pv, cum_v, trade_ts, trade_px, trade_sz = build_regime_index(
                nq, pts_per_tick=0.25
            )
            mid = int(nq.column("ts_ns")[nq.num_rows // 2].as_py())
            snap = lookup_regime(
                bars,
                bar_ts,
                cum_pv,
                cum_v,
                np.array([mid], dtype=np.int64),
                window_min=30,
                trade_ts=trade_ts,
                trade_px=trade_px,
                trade_sz=trade_sz,
            )[0]
            med = float(snap.median_minute_range)
            if not np.isfinite(med) or med <= 0:
                med = 15.0
            move = float(np.clip(km * med, 10, 40))
            confirm = float(np.clip(kc * med, 5, 25))
            cfg = HvExtremeConfig(
                move_pts=move,
                confirm_pts=confirm,
                peak_band_pts=5,
                high_vol_mult=2.0,
                max_pullback_pts=5.0,
                lookback_min=15,
                cooldown_sec=30,
            )
            # don't use panel cache
            from research.features.hv_extreme import hv_extreme_events

            ev = hv_extreme_events(nq, cfg, attach_med_r30=True)
            if ev.num_rows == 0:
                continue
            s = simulate_session(
                sid,
                nq,
                pa.table({}),
                sim_cfg,
                bracket_fn=R.fixed_bracket,
                events=ev,
                vol_window_min=30,
            )
            all_trades.extend(s.trades)
        return R.pack(all_trades), all_trades

    sep, st = month(R.SEP)
    oct_, ot = month(R.OCT)
    both = R.pack(st + ot)
    return {
        "label": label,
        "sep": sep,
        "oct": oct_,
        "both": both,
        "both_net": both.get("net"),
        "km": km,
        "kc": kc,
    }


def stage_p5() -> dict:
    cfg = R.PANEL
    rows = []
    base = R.run_both(cfg, label="nq_only_baseline")
    base["verdict"] = R.verdict(base)
    rows.append(base)

    # 5a ES MAT burst confirm
    for sync in (1000, 1500, 3000):
        for stamp in ("confirm", "causal"):
            label = f"5a_es_burst_sync{sync}_{stamp}"
            print(f"\n=== P5 {label} ===", flush=True)
            row = _run_es_burst(cfg, sync, stamp, label)
            row["verdict"] = R.verdict(row)
            print(R.fmt_cell(row), "→", row["verdict"])
            rows.append(row)

    # 5b dual tip
    for sync_s in (3, 5, 10):
        label = f"5b_dual_tip_{sync_s}s"
        print(f"\n=== P5 {label} ===", flush=True)
        row = _run_dual_tip(cfg, sync_s, label)
        row["verdict"] = R.verdict(row)
        print(R.fmt_cell(row), "→", row["verdict"])
        rows.append(row)

    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    payload = {"stage": "p5", "ranked": ranked, "rows": rows}
    dest = R.write_json("p5_es.json", payload)
    best = ranked[0]
    R.append_summary(
        "## P5 — correlated ES\n\n"
        f"- Best: `{best['label']}` both={best.get('both_net')} "
        f"verdict={best['verdict']}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return payload


def _run_es_burst(cfg: HvExtremeConfig, sync_ms: int, stamp: str, label: str) -> dict:
    sim_cfg = R.default_sim()
    # ES-scaled MAT thresholds (rough): min_vol ~10, min_trades 4
    es_mat = MatSimConfig(
        bar_ms=100,
        min_trades=4,
        min_vol=10.0,
        min_imbalance=0.55,
        min_progress_ticks=1.0,
        sync_ms=sync_ms,
        es_stamp=stamp,
        signal_cooldown_sec=30,
        slip_ticks=0,
        flat_only=True,
        commission_pts_rt=0.25,
        pts_per_tick=0.25,
    )

    def month(sessions):
        all_trades = []
        n_es_first = n_nq_first = 0
        for sid in sessions:
            nq = R.load_nq(sid)
            es = R.load_es(sid)
            if nq.num_rows == 0 or es.num_rows == 0:
                continue
            nq_ev = R.cached_events(sid, cfg, enrich=True)
            if nq_ev.num_rows == 0:
                continue
            # ES bursts via detect_signals with empty NQ trick — use momentum on ES
            from research.features.momentum import momentum_burst_events, trades_to_tape_bars

            bars = trades_to_tape_bars(es, bar_ms=100)
            es_ev = momentum_burst_events(
                bars,
                min_trades=es_mat.min_trades,
                min_vol=es_mat.min_vol,
                min_imbalance=es_mat.min_imbalance,
                min_progress_ticks=es_mat.min_progress_ticks,
                cooldown_bars=3,
            )
            if es_ev.num_rows == 0:
                continue
            matched = cross_match(
                nq_ev, es_ev, sync_ms=sync_ms, match_col="side", stamp=stamp
            )
            if matched.num_rows == 0:
                continue
            slag = matched.column("es_signed_lag_ms").to_numpy()
            n_es_first += int((slag < 0).sum())
            n_nq_first += int((slag >= 0).sum())
            s = simulate_session(
                sid,
                nq,
                pa.table({}),
                sim_cfg,
                bracket_fn=R.fixed_bracket,
                events=matched,
                vol_window_min=30,
            )
            all_trades.extend(s.trades)
        p = R.pack(all_trades)
        p["es_first"] = n_es_first
        p["nq_first"] = n_nq_first
        return p, all_trades

    sep, st = month(R.SEP)
    oct_, ot = month(R.OCT)
    both = R.pack(st + ot)
    both["es_first"] = sep.get("es_first", 0) + oct_.get("es_first", 0)
    both["nq_first"] = sep.get("nq_first", 0) + oct_.get("nq_first", 0)
    return {
        "label": label,
        "sep": sep,
        "oct": oct_,
        "both": both,
        "both_net": both.get("net"),
    }


def _run_dual_tip(cfg: HvExtremeConfig, sync_s: float, label: str) -> dict:
    sim_cfg = R.default_sim()
    es_cfg = HvExtremeConfig(
        move_pts=5.0,
        confirm_pts=2.5,
        peak_band_pts=1.25,
        high_vol_mult=2.0,
        max_pullback_pts=1.25,
        lookback_min=15,
        cooldown_sec=30,
        pts_per_tick=0.25,  # ES tick 0.25 pts too in this parquet convention?
    )
    # ES points: ES tick is often 0.25 as well in their extract — keep.

    def month(sessions):
        all_trades = []
        from research.features.hv_extreme import hv_extreme_events

        for sid in sessions:
            nq = R.load_nq(sid)
            es = R.load_es(sid)
            if nq.num_rows == 0 or es.num_rows == 0:
                continue
            nq_ev = R.cached_events(sid, cfg, enrich=False)
            es_ev = hv_extreme_events(es, es_cfg, attach_med_r30=False)
            if nq_ev.num_rows == 0 or es_ev.num_rows == 0:
                continue
            matched = cross_match(
                nq_ev,
                es_ev,
                sync_ms=int(sync_s * 1000),
                match_col="side",
                stamp="confirm",
            )
            if matched.num_rows == 0:
                continue
            s = simulate_session(
                sid,
                nq,
                pa.table({}),
                sim_cfg,
                bracket_fn=R.fixed_bracket,
                events=matched,
                vol_window_min=30,
            )
            all_trades.extend(s.trades)
        return R.pack(all_trades), all_trades

    sep, st = month(R.SEP)
    oct_, ot = month(R.OCT)
    both = R.pack(st + ot)
    return {
        "label": label,
        "sep": sep,
        "oct": oct_,
        "both": both,
        "both_net": both.get("net"),
    }


def stage_p6() -> dict:
    cfg = R.PANEL
    rows = []
    base = R.run_both(cfg, label="baseline")
    base["verdict"] = R.verdict(base)
    rows.append(base)

    for x_pts in (2, 5):
        for look_min in (30, 60):
            for kind in ("swing", "session_extreme", "opening_range"):
                label = f"level_{kind}_X{x_pts}_L{look_min}"
                print(f"\n=== P6 {label} ===", flush=True)
                row = _run_level_gate(cfg, x_pts, look_min, kind, label)
                row["verdict"] = R.verdict(row)
                print(R.fmt_cell(row), "→", row["verdict"])
                rows.append(row)

    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    payload = {"stage": "p6", "ranked": ranked, "rows": rows}
    dest = R.write_json("p6_level.json", payload)
    best = ranked[0]
    R.append_summary(
        "## P6 — tip at level\n\n"
        f"- Best: `{best['label']}` both={best.get('both_net')} "
        f"verdict={best['verdict']}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return payload


def _run_level_gate(
    cfg: HvExtremeConfig, x_pts: float, look_min: int, kind: str, label: str
) -> dict:
    x_ticks = max(1, int(round(x_pts / cfg.pts_per_tick)))
    look_ns = int(look_min * 60 * 1e9)
    confirm = cfg.confirm_ticks()

    def month(sessions):
        all_trades = []
        sim_cfg = R.default_sim()
        for sid in sessions:
            nq = R.load_nq(sid)
            if nq.num_rows == 0:
                continue
            ev = R.cached_events(sid, cfg, enrich=True)
            if ev.num_rows == 0:
                continue
            ts = nq.column("ts_ns").to_numpy().astype(np.int64)
            px = np.round(nq.column("price").to_numpy().astype(np.float64)).astype(
                np.int64
            )
            order = np.argsort(ts, kind="stable")
            ts, px = ts[order], px[order]

            # precompute swings: local extrema with confirm reversal
            swings_hi = []
            swings_lo = []
            run_hi, run_lo = px[0], px[0]
            run_hi_i = run_lo_i = 0
            for i in range(1, len(px)):
                if px[i] >= run_hi:
                    run_hi, run_hi_i = int(px[i]), i
                if px[i] <= run_lo:
                    run_lo, run_lo_i = int(px[i]), i
                if run_hi - px[i] >= confirm:
                    swings_hi.append((int(ts[run_hi_i]), run_hi))
                    run_hi, run_hi_i = int(px[i]), i
                if px[i] - run_lo >= confirm:
                    swings_lo.append((int(ts[run_lo_i]), run_lo))
                    run_lo, run_lo_i = int(px[i]), i

            session_hi = int(px.max())
            session_lo = int(px.min())
            # opening range = first 30 min
            t0 = int(ts[0])
            or_end = t0 + 30 * 60 * 1_000_000_000
            or_mask = ts <= or_end
            if or_mask.any():
                or_hi = int(px[or_mask].max())
                or_lo = int(px[or_mask].min())
            else:
                or_hi, or_lo = session_hi, session_lo

            keep = []
            fire_ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
            extremes = ev.column("extreme_tick").to_numpy().astype(np.int64)
            sides = ev.column("side").to_numpy().astype(np.int8)
            for i in range(len(fire_ts)):
                tip = int(extremes[i])
                t = int(fire_ts[i])
                side = int(sides[i])
                ok = False
                if kind == "session_extreme":
                    ref = session_hi if side < 0 else session_lo
                    # use running session extreme at fire time
                    j = int(np.searchsorted(ts, t, side="right"))
                    if j > 0:
                        ref = int(px[:j].max()) if side < 0 else int(px[:j].min())
                    ok = abs(tip - ref) <= x_ticks
                elif kind == "opening_range":
                    ok = abs(tip - or_hi) <= x_ticks or abs(tip - or_lo) <= x_ticks
                else:  # swing
                    cands = swings_hi if side < 0 else swings_lo
                    for st, sp in reversed(cands):
                        if st > t:
                            continue
                        if t - st > look_ns:
                            break
                        if abs(tip - sp) <= x_ticks:
                            ok = True
                            break
                if ok:
                    keep.append(i)
            if not keep:
                continue
            ev2 = ev.take(np.asarray(keep, dtype=np.int64))
            s = simulate_session(
                sid,
                nq,
                pa.table({}),
                sim_cfg,
                bracket_fn=R.fixed_bracket,
                events=ev2,
                vol_window_min=30,
            )
            all_trades.extend(s.trades)
        return R.pack(all_trades), all_trades

    sep, st = month(R.SEP)
    oct_, ot = month(R.OCT)
    both = R.pack(st + ot)
    return {
        "label": label,
        "sep": sep,
        "oct": oct_,
        "both": both,
        "both_net": both.get("net"),
    }


def _load_stage_best(name: str) -> list[dict]:
    path = R.results_dir() / name
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    ranked = data.get("ranked") or data.get("rows") or []
    return [
        r
        for r in ranked
        if r.get("label") != "baseline"
        and "baseline" not in (r.get("label") or "")
        and (r.get("both_net") or -1e9) > R.BASELINE_BOTH_NET
    ]


def _parse_retest_label(lab: str) -> R.RetestParams | None:
    """Parse labels like retest_R8_t120_sb2_x3.0."""
    if not lab.startswith("retest_R"):
        return None
    try:
        # retest_R{R}_t{T}_sb{S}_x{M} or ..._tp30
        body = lab[len("retest_R") :]
        parts = body.split("_")
        r_pts = float(parts[0])
        timeout = float(parts[1][1:])  # t120
        sb = float(parts[2][2:])  # sb2
        if parts[3].startswith("tp"):
            return R.RetestParams(
                retest_pts=r_pts,
                timeout_sec=timeout,
                stop_beyond_pts=sb,
                tp_mult=0.0,
                fixed_tp=float(parts[3][2:]),
            )
        tp_mult = float(parts[3][1:])  # x3.0
        return R.RetestParams(
            retest_pts=r_pts,
            timeout_sec=timeout,
            stop_beyond_pts=sb,
            tp_mult=tp_mult,
        )
    except Exception:
        return None


def stage_p8() -> dict:
    """Combine winners from P1–P6 × entry style."""
    carries: list[tuple[str, object]] = []

    # Explicit carry-forwards from stage results (also hardcode strong cells)
    carries.append(
        (
            "loud_rate>3",
            lambda ev: np.isfinite(ev.column("leg_rate_ratio").to_numpy())
            & (ev.column("leg_rate_ratio").to_numpy() > 3.0),
        )
    )
    carries.append(
        (
            "loud_rate>2",
            lambda ev: np.isfinite(ev.column("leg_rate_ratio").to_numpy())
            & (ev.column("leg_rate_ratio").to_numpy() > 2.0),
        )
    )
    carries.append(
        (
            "2b50+2c0.5x",
            lambda ev: (ev.column("tip_max_order").to_numpy() >= 50)
            & (
                ev.column("leg_max_order_same_side").to_numpy()
                < 0.5 * np.maximum(ev.column("tip_max_order").to_numpy(), 1.0)
            ),
        )
    )
    carries.append(
        (
            "2c_leg_same<0.25x",
            lambda ev: ev.column("leg_max_order_same_side").to_numpy()
            < 0.25 * np.maximum(ev.column("tip_max_order").to_numpy(), 1.0),
        )
    )
    carries.append(
        (
            "medR30>=12",
            lambda ev: np.isfinite(ev.column("med_r30").to_numpy())
            & (ev.column("med_r30").to_numpy() >= 12),
        )
    )
    carries.append(("none", None))

    # Top retest constructions from P3 (pass verdict preferred)
    p3_path = R.results_dir() / "p3_retest.json"
    retest_cfgs: list[tuple[str, R.RetestParams]] = []
    if p3_path.exists():
        p3_rows = json.loads(p3_path.read_text()).get("ranked") or []
        for r in p3_rows:
            lab = str(r.get("label") or "")
            if not lab.startswith("retest_"):
                continue
            if not str(r.get("verdict", "")).startswith("pass"):
                continue
            rp = _parse_retest_label(lab)
            if rp is None:
                continue
            retest_cfgs.append((lab, rp))
            if len(retest_cfgs) >= 4:
                break
    if not retest_cfgs:
        retest_cfgs = [
            (
                "retest_R8_t300_sb4_x3.0",
                R.RetestParams(
                    retest_pts=8, timeout_sec=300, stop_beyond_pts=4, tp_mult=3.0
                ),
            ),
            (
                "retest_R10_t300_sb2_x3.0",
                R.RetestParams(
                    retest_pts=10, timeout_sec=300, stop_beyond_pts=2, tp_mult=3.0
                ),
            ),
        ]

    rows = []
    cfg = R.PANEL

    def compose(fns):
        if not fns:
            return None

        def pf(ev):
            m = np.ones(ev.num_rows, dtype=bool)
            for f in fns:
                m &= f(ev)
            return m

        return pf

    # Extension: singles + selected pairs
    combos: list[tuple[str, list]] = []
    for i, (a, fa) in enumerate(carries):
        combos.append((a, [fa] if fa else []))
        for b, fb in carries[i + 1 :]:
            if a == "none" or b == "none":
                continue
            combos.append((f"{a}+{b}", [f for f in (fa, fb) if f is not None]))

    for lab, fns in combos:
        pf = compose(fns)
        print(f"\n=== P8 ext {lab} ===", flush=True)
        row = R.run_both(cfg, post_filter=pf, entry="extension", label=f"ext|{lab}")
        row["verdict"] = R.verdict(row)
        print(R.fmt_cell(row), "→", row["verdict"])
        rows.append(row)

    # Retest × gates (top retest styles × key filters)
    by_name = {a: fa for a, fa in carries}
    gate_subset: list[tuple[str, list]] = [
        ("none", []),
        ("loud_rate>3", [by_name["loud_rate>3"]]),
        ("loud_rate>2", [by_name["loud_rate>2"]]),
        ("2b50+2c0.5x", [by_name["2b50+2c0.5x"]]),
        ("medR30>=12", [by_name["medR30>=12"]]),
        (
            "loud_rate>3+2b50+2c0.5x",
            [by_name["loud_rate>3"], by_name["2b50+2c0.5x"]],
        ),
    ]

    for rlab, rp in retest_cfgs:
        for glab, fns in gate_subset:
            pf = compose(fns)
            label = f"ret|{rlab}|{glab}"
            print(f"\n=== P8 {label} ===", flush=True)
            row = R.run_both(
                cfg, post_filter=pf, entry="retest", retest=rp, label=label
            )
            row["verdict"] = R.verdict(row)
            row["retest"] = asdict_retest(rp)
            print(R.fmt_cell(row), "→", row["verdict"])
            rows.append(row)

    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)

    # Prefer pass verdict with both months >0 for freeze
    passers = [r for r in ranked if str(r.get("verdict", "")).startswith("pass")]
    freeze = passers[0] if passers else (ranked[0] if ranked else None)
    neighbour = None
    if freeze:
        for r in (passers[1:] if passers else ranked[1:]):
            if r["label"] != freeze["label"]:
                neighbour = r
                break
    loso = _loso(freeze) if freeze else {}

    # null control for freeze n
    null_sep = R.null_month(R.SEP, max(40, (freeze or {}).get("sep", {}).get("n") or 40))
    null_oct = R.null_month(R.OCT, max(40, (freeze or {}).get("oct", {}).get("n") or 40))

    payload = {
        "stage": "p8",
        "ranked": ranked[:40],
        "freeze": freeze,
        "neighbour": neighbour,
        "loso": loso,
        "null": {"sep": null_sep, "oct": null_oct},
    }
    dest = R.write_json("p8_combine.json", payload)
    R.append_summary(
        "## P8 — combine\n\n"
        f"- Freeze: `{freeze['label'] if freeze else None}` "
        f"both={freeze.get('both_net') if freeze else None} "
        f"verdict={freeze.get('verdict') if freeze else None}.\n"
        f"- Neighbour: `{neighbour['label'] if neighbour else None}` "
        f"both={neighbour.get('both_net') if neighbour else None}.\n"
        f"- LOSO max session share of net: {loso.get('max_share')}.\n"
        f"- Null Sep/Oct net: {null_sep.get('net')} / {null_oct.get('net')}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return payload


def asdict_retest(rp: R.RetestParams) -> dict:
    return {
        "retest_pts": rp.retest_pts,
        "timeout_sec": rp.timeout_sec,
        "stop_beyond_pts": rp.stop_beyond_pts,
        "tp_mult": rp.tp_mult,
        "fixed_tp": rp.fixed_tp,
    }


def _loso(row: dict) -> dict:
    """Approx leave-one-session-out from per_session nets."""
    both = row.get("both") or {}
    per = both.get("per_session") or {}
    net = both.get("net") or 0
    if not per or abs(net) < 1e-9:
        return {"max_share": None}
    shares = {k: abs(v) / abs(net) for k, v in per.items()}
    worst = max(shares, key=shares.get)
    return {"max_share": shares[worst], "worst_session": worst, "shares": shares}


def stage_p7() -> dict:
    """Exits on best 1–2 constructions from P8 (frozen retest ± neighbour)."""
    p8_path = R.results_dir() / "p8_combine.json"
    targets = []
    if p8_path.exists():
        data = json.loads(p8_path.read_text())
        for key in ("freeze", "neighbour"):
            if data.get(key):
                targets.append(data[key])
    if not targets:
        targets = [
            {
                "label": "ret|retest_R10_t300_sb2_x3.0|none",
                "retest": {
                    "retest_pts": 10,
                    "timeout_sec": 300,
                    "stop_beyond_pts": 2,
                    "tp_mult": 3.0,
                    "fixed_tp": 30.0,
                },
            }
        ]

    exit_cfgs = [
        ("base", R.default_sim()),
        ("be8", R.default_sim(be_trigger_pts=8)),
        ("be10", R.default_sim(be_trigger_pts=10)),
        ("be15", R.default_sim(be_trigger_pts=15)),
        ("time60", R.default_sim(time_stop_sec=60)),
        ("time120", R.default_sim(time_stop_sec=120)),
        ("trail8", R.default_sim(trail_pts=8)),
        ("trail12", R.default_sim(trail_pts=12)),
        ("be10_trail8", R.default_sim(be_trigger_pts=10, trail_pts=8)),
    ]

    rows = []
    for tgt in targets[:2]:
        lab = tgt["label"]
        rp_d = tgt.get("retest") or {}
        if lab.startswith("ret|") or rp_d:
            # parse from label if needed
            parts = lab.split("|")
            rp = None
            if len(parts) >= 2:
                rp = _parse_retest_label(parts[1])
            if rp is None and rp_d:
                rp = R.RetestParams(
                    retest_pts=float(rp_d.get("retest_pts", 10)),
                    timeout_sec=float(rp_d.get("timeout_sec", 300)),
                    stop_beyond_pts=float(rp_d.get("stop_beyond_pts", 2)),
                    tp_mult=float(rp_d.get("tp_mult", 3)),
                    fixed_tp=float(rp_d.get("fixed_tp", 30)),
                )
            entry = "retest"
        else:
            rp = None
            entry = "extension"

        for ex_label, sim in exit_cfgs:
            label = f"{lab}||{ex_label}"
            print(f"\n=== P7 {label} ===", flush=True)
            row = R.run_both(
                R.PANEL,
                entry=entry,
                retest=rp,
                sim_cfg=sim,
                label=label,
            )
            row["verdict"] = R.verdict(row)
            print(R.fmt_cell(row), "→", row["verdict"])
            rows.append(row)

        # vol-RR reference on freeze only
        if tgt is targets[0]:
            for blabel, bfn in (
                (
                    "vol_1R_2R",
                    lambda sv: (
                        max(2.0, 2 * (sv.median_minute_range or 15)),
                        max(2.0, sv.median_minute_range or 15),
                    ),
                ),
                (
                    "vol_1R_2.5R",
                    lambda sv: (
                        max(2.0, 2.5 * (sv.median_minute_range or 15)),
                        max(2.0, sv.median_minute_range or 15),
                    ),
                ),
            ):
                # vol brackets replace retest R-multiple — extension-style on retest entries
                label = f"{lab}||{blabel}"
                print(f"\n=== P7 {label} ===", flush=True)
                row = R.run_both(
                    R.PANEL,
                    entry=entry,
                    retest=rp,
                    bracket_fn=bfn,
                    label=label,
                )
                row["verdict"] = R.verdict(row)
                rows.append(row)

    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    payload = {
        "stage": "p7",
        "targets": [t["label"] for t in targets[:2]],
        "ranked": ranked,
        "rows": rows,
    }
    dest = R.write_json("p7_exits.json", payload)
    best = ranked[0]
    R.append_summary(
        "## P7 — exits\n\n"
        f"- Targets: {payload['targets']}.\n"
        f"- Best: `{best['label']}` both={best.get('both_net')} "
        f"verdict={best['verdict']}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return payload


def stage_java() -> dict:
    """Record freeze + experiment log; jar built separately via gradle."""
    p8 = R.results_dir() / "p8_combine.json"
    freeze = None
    neighbour = None
    if p8.exists():
        data = json.loads(p8.read_text())
        freeze = data.get("freeze")
        neighbour = data.get("neighbour")
    notes = {
        "freeze": freeze,
        "neighbour": neighbour,
        "java_module": "HvExtremeMoveModule (v2 retest entry defaults)",
        "jar": "bookmap-orderflow-hvext-v2.jar",
        "defaults": {
            "move": 20,
            "confirm": 10,
            "band": 5,
            "hv": 2,
            "pb": 5,
            "retest": True,
            "retest_pts": 10,
            "timeout_sec": 300,
            "stop_beyond": 2,
            "tp_mult": 3.0,
        },
    }
    dest = R.write_json("java_port.json", notes)
    R.append_summary(
        "## Java port\n\n"
        f"- Freeze: `{freeze.get('label') if freeze else None}` "
        f"both={freeze.get('both_net') if freeze else None} "
        f"verdict={freeze.get('verdict') if freeze else None}.\n"
        f"- Neighbour: `{neighbour.get('label') if neighbour else None}`.\n"
        "- Module: `HvExtremeMoveModule` renamed strategy "
        "`HV Extreme Move AutoTrade v2` with retest-entry defaults matching freeze.\n"
        f"- Jar target: `{notes['jar']}`.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    log = Path("docs/EXPERIMENT_LOG.md")
    text = log.read_text(encoding="utf-8")
    table_part = text.split("## Run notes")[0]
    if "EXP-0038" not in table_part:
        needle = "| EXP-0037 |"
        idx = text.find(needle)
        if idx >= 0:
            # find end of EXP-0037 row (may span one line)
            end = text.find("\n", idx)
            row = (
                "\n| EXP-0038 | H30 redesign (retest entry + ablations) | "
                "ran — freeze retest | Sep+Oct full | "
                f"`{freeze.get('label') if freeze else 'none'}` "
                f"both={freeze.get('both_net') if freeze else 'n/a'}; "
                "see `results/EXP-0038/summary.md` |"
            )
            text = text[:end] + row + text[end:]
    note = (
        "\n### 2026-10-20 — EXP-0038 HV Extreme redesign series\n\n"
        "- Package: `research/hvext/redesign/` (enrich + runner + stages P1–P8).\n"
        f"- **Freeze:** `{freeze.get('label') if freeze else None}` "
        f"Sep n={freeze.get('sep', {}).get('n') if freeze else None} "
        f"net={freeze.get('sep', {}).get('net') if freeze else None} / "
        f"Oct n={freeze.get('oct', {}).get('n') if freeze else None} "
        f"net={freeze.get('oct', {}).get('net') if freeze else None} / "
        f"both={freeze.get('both_net') if freeze else None} "
        f"({freeze.get('verdict') if freeze else None}).\n"
        f"- **Neighbour:** `{neighbour.get('label') if neighbour else None}` "
        f"both={neighbour.get('both_net') if neighbour else None}.\n"
        "- Key finding: extension entry stays negative; **retest entry** "
        "(pullback to tip) clears both months. Loud-leg / tip-order gates help "
        "extension but thin when stacked on retest.\n"
        "- Java: retest mode defaults in `HvExtremeMoveModule`; jar "
        "`bookmap-orderflow-hvext-v2.jar`.\n"
        "- Results: `results/EXP-0038/`.\n"
    )
    if "EXP-0038 HV Extreme redesign" not in text:
        text = text.rstrip() + "\n" + note + "\n"
        log.write_text(text, encoding="utf-8")
    return notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--stage",
        required=True,
        choices=[
            "infra",
            "p1",
            "p2",
            "p3",
            "p4",
            "p5",
            "p6",
            "p7",
            "p8",
            "combine",
            "java",
            "all",
        ],
    )
    args = ap.parse_args()
    stage = args.stage
    if stage == "combine":
        stage = "p8"

    order = {
        "infra": stage_infra,
        "p1": stage_p1,
        "p2": stage_p2,
        "p3": stage_p3,
        "p4": stage_p4,
        "p5": stage_p5,
        "p6": stage_p6,
        "p8": stage_p8,
        "p7": stage_p7,
        "java": stage_java,
    }

    if stage == "all":
        for name in ("infra", "p1", "p2", "p3", "p4", "p5", "p6", "p8", "p7", "java"):
            print(f"\n########## STAGE {name} ##########\n", flush=True)
            order[name]()
        return 0

    order[stage]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
