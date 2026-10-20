"""Re-run EXP-0038 pieces after fixing tip window / P4 / P6 / per-row risk / MBO / MAE-MFE."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import simulate_session
from research.features.hv_extreme import HvExtremeConfig, HvExtremeMoveTracker
from research.features.regime import build_regime_index, lookup_regime
from research.hvext.redesign.enrich import tip_against_mask, tip_trapped_mask
from research.hvext.redesign import runner as R

HOLDOUT = ["030826", "040826", "050826", "260326"]
FREEZE_RP = R.RetestParams(
    retest_pts=10, timeout_sec=300, stop_beyond_pts=2, tp_mult=3.0
)


def _pct(xs, p):
    xs = [x for x in xs if x is not None and np.isfinite(x)]
    return float(np.percentile(xs, p)) if xs else None


def tip_feature_stats() -> dict:
    fracs60, fracs30, tip_max = [], [], []
    for sid in R.ALL:
        ev = R.cached_events(sid, R.PANEL, enrich=True)
        if ev.num_rows == 0:
            continue
        fracs60.extend(ev.column("tip_buy_frac").to_pylist())
        fracs30.extend(ev.column("tip_buy_frac_30s").to_pylist())
        tip_max.extend(ev.column("tip_max_order").to_pylist())
    return {
        "n_fires": len(fracs60),
        "tip_buy_frac_60s_p50": _pct(fracs60, 50),
        "tip_buy_frac_60s_p90": _pct(fracs60, 90),
        "tip_buy_frac_30s_p50": _pct(fracs30, 50),
        "tip_buy_frac_30s_p90": _pct(fracs30, 90),
        "frac_ge_0.6": float(np.mean([f >= 0.6 for f in fracs60 if np.isfinite(f)])),
        "frac_ge_0.7": float(np.mean([f >= 0.7 for f in fracs60 if np.isfinite(f)])),
        "tip_max_order_p50": _pct(tip_max, 50),
        "tip_max_order_p90": _pct(tip_max, 90),
    }


def run_p2_fixed() -> dict:
    cfg = R.PANEL
    rows = []

    def add(label, pf):
        print(f"  P2 {label}", flush=True)
        row = R.run_both(cfg, post_filter=pf, label=label)
        row["verdict"] = R.verdict(row)
        print(f"    {R.fmt_cell(row)} → {row['verdict']}")
        rows.append(row)

    add("baseline", None)
    for frac in (0.55, 0.6, 0.7, 0.8):
        add(f"2a_trapped60s>={frac}", lambda ev, f=frac: tip_trapped_mask(ev, f))
        add(
            f"2a_control60s>={frac}",
            lambda ev, f=frac: tip_against_mask(ev, f),
        )
        add(
            f"2a_trapped30s>={frac}",
            lambda ev, f=frac: tip_trapped_mask(ev, f, col="tip_buy_frac_30s"),
        )
    for n in (20, 50, 100):
        add(
            f"2b_tip_max60s>={n}",
            lambda ev, n=n: ev.column("tip_max_order").to_numpy() >= n,
        )
    add(
        "2b50+2c0.5x",
        lambda ev: (ev.column("tip_max_order").to_numpy() >= 50)
        & (
            ev.column("leg_max_order_same_side").to_numpy()
            < 0.5 * np.maximum(ev.column("tip_max_order").to_numpy(), 1.0)
        ),
    )
    add(
        "2a0.6+2b20",
        lambda ev: tip_trapped_mask(ev, 0.6)
        & (ev.column("tip_max_order").to_numpy() >= 20),
    )
    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    return {"stage": "p2_fixed_tip60s", "ranked": ranked, "rows": rows}


def run_p2_mbo() -> dict:
    """MBO absorber on tip — uses with_mbo enrich."""
    cfg = R.PANEL
    rows = []
    R.clear_cache()

    def month(sessions, pf=None):
        all_trades = []
        for sid in sessions:
            nq = R.load_nq(sid)
            if nq.num_rows == 0:
                continue
            ev = R.cached_events(sid, cfg, enrich=True, with_mbo=True)
            if pf is not None:
                from research.hvext.redesign.enrich import filter_events

                ev = filter_events(ev, pf(ev))
            if ev.num_rows == 0:
                continue
            s = simulate_session(
                sid,
                nq,
                pa.table({}),
                R.default_sim(),
                bracket_fn=R.fixed_bracket,
                events=ev,
                vol_window_min=30,
            )
            all_trades.extend(s.trades)
        return R.pack(all_trades), all_trades

    def both(label, pf):
        print(f"  MBO {label}", flush=True)
        sep, st = month(R.SEP, pf)
        oct_, ot = month(R.OCT, pf)
        both_p = R.pack(st + ot)
        row = {
            "label": label,
            "sep": sep,
            "oct": oct_,
            "both": both_p,
            "both_net": both_p.get("net"),
        }
        row["verdict"] = R.verdict(row)
        print(f"    {R.fmt_cell(row)} → {row['verdict']}")
        rows.append(row)

    both("baseline_mbo_enrich", None)
    both(
        "2d_mbo_rest>=50",
        lambda ev: np.isfinite(ev.column("mbo_tip_max_resting").to_numpy())
        & (ev.column("mbo_tip_max_resting").to_numpy() >= 50),
    )
    both(
        "2d_mbo_rest>=100",
        lambda ev: np.isfinite(ev.column("mbo_tip_max_resting").to_numpy())
        & (ev.column("mbo_tip_max_resting").to_numpy() >= 100),
    )
    both(
        "2d_mbo_rest>=20",
        lambda ev: np.isfinite(ev.column("mbo_tip_max_resting").to_numpy())
        & (ev.column("mbo_tip_max_resting").to_numpy() >= 20),
    )
    for thr in (20, 50):
        both(
            f"2d_mbo_absorbed>={thr}",
            lambda ev, thr=thr: np.isfinite(ev.column("mbo_tip_filled").to_numpy())
            & (ev.column("mbo_tip_filled").to_numpy() >= thr),
        )
    both(
        "2d_mbo_absorbed>0",
        lambda ev: np.isfinite(ev.column("mbo_tip_filled").to_numpy())
        & (ev.column("mbo_tip_filled").to_numpy() > 0),
    )
    # Pooled p90 of absorbed volume across all sessions
    vals = []
    for sid in R.ALL:
        ev = R.cached_events(sid, cfg, enrich=True, with_mbo=True)
        if ev.num_rows:
            v = ev.column("mbo_tip_filled").to_numpy()
            vals.extend(v[np.isfinite(v)].tolist())
    p90 = float(np.percentile(vals, 90)) if vals else float("inf")
    if p90 > 0:
        both(
            f"2d_mbo_absorbed>=p90({p90:.0f})",
            lambda ev, p90=p90: np.isfinite(ev.column("mbo_tip_filled").to_numpy())
            & (ev.column("mbo_tip_filled").to_numpy() >= p90),
        )
    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    return {"stage": "p2_mbo", "ranked": ranked, "rows": rows, "absorbed_p90": p90}


def _volscale_arm_time(km: float, kc: float, label: str) -> dict:
    """Detect with Move/Confirm = k × medR30 looked up at each print (arm-time)."""
    sim_cfg = R.default_sim()

    def month(sessions):
        all_trades = []
        for sid in sessions:
            nq = R.load_nq(sid)
            if nq.num_rows == 0:
                continue
            bars, bar_ts, cum_pv, cum_v, trade_ts, trade_px, trade_sz = build_regime_index(
                nq, pts_per_tick=0.25
            )
            ts = nq.column("ts_ns").to_numpy().astype(np.int64)
            px = np.round(nq.column("price").to_numpy().astype(np.float64)).astype(
                np.int64
            )
            sz = nq.column("size").to_numpy().astype(np.float64)
            order = np.argsort(ts, kind="stable")
            ts, px, sz = ts[order], px[order], sz[order]

            # Seed tracker; mutate move/confirm each print from regime
            cfg0 = HvExtremeConfig(
                move_pts=20,
                confirm_pts=10,
                peak_band_pts=5,
                high_vol_mult=2.0,
                max_pullback_pts=5.0,
                lookback_min=15,
                cooldown_sec=30,
            )
            tracker = HvExtremeMoveTracker(cfg0)
            # Precompute medR30 at each trade via sparse lookup every ~1s batch
            # Use lookup on unique minute ends for speed: interpolate last known
            sample_ts = ts[:: max(1, len(ts) // 2000)]  # ~2000 lookups
            if len(sample_ts) == 0:
                continue
            snaps = lookup_regime(
                bars,
                bar_ts,
                cum_pv,
                cum_v,
                sample_ts,
                window_min=30,
                trade_ts=trade_ts,
                trade_px=trade_px,
                trade_sz=trade_sz,
            )
            med_at = np.array([s.median_minute_range for s in snaps], dtype=np.float64)
            # map each trade index to nearest sample
            sample_idx = np.searchsorted(sample_ts, ts, side="right") - 1
            sample_idx = np.clip(sample_idx, 0, len(med_at) - 1)

            rows = []
            last_med = 15.0
            for i, (t, tick, s) in enumerate(zip(ts, px, sz)):
                med = float(med_at[sample_idx[i]])
                if not np.isfinite(med) or med <= 0:
                    med = last_med
                else:
                    last_med = med
                move = float(np.clip(km * med, 10, 40))
                confirm = float(np.clip(kc * med, 5, 25))
                tracker.move_ticks = max(1, int(round(move / 0.25)))
                tracker.confirm_ticks = max(1, int(round(confirm / 0.25)))
                tracker.on_trade(int(t), int(tick), float(s))
                sig = tracker.poll_signal()
                if sig is not None:
                    rows.append(sig)
            if not rows:
                continue
            ev = pa.table(
                {
                    "event_ts_ns": pa.array(
                        [r["event_ts_ns"] for r in rows], type=pa.int64()
                    ),
                    "side": pa.array([r["side"] for r in rows], type=pa.int8()),
                    "event_type": pa.array(["hv_extreme"] * len(rows), type=pa.string()),
                }
            )
            ssum = simulate_session(
                sid,
                nq,
                pa.table({}),
                sim_cfg,
                bracket_fn=R.fixed_bracket,
                events=ev,
                vol_window_min=30,
            )
            all_trades.extend(ssum.trades)
        return R.pack(all_trades), all_trades

    sep, st = month(R.SEP)
    oct_, ot = month(R.OCT)
    both = R.pack(st + ot)
    row = {
        "label": label,
        "sep": sep,
        "oct": oct_,
        "both": both,
        "both_net": both.get("net"),
        "km": km,
        "kc": kc,
    }
    row["verdict"] = R.verdict(row)
    return row


def run_p4_fixed() -> dict:
    rows = []
    print("  P4 fixed baseline", flush=True)
    base = R.run_both(R.PANEL, label="fixed_20_10")
    base["verdict"] = R.verdict(base)
    rows.append(base)

    def med_gate(ev):
        m = ev.column("med_r30").to_numpy().astype(np.float64)
        return np.isfinite(m) & (m >= 12)

    g = R.run_both(R.PANEL, post_filter=med_gate, label="fixed_20_10_medR30>=12")
    g["verdict"] = R.verdict(g)
    rows.append(g)

    for km in (0.75, 1.0, 1.5):
        for kc in (0.4, 0.5):
            label = f"volscale_arm_km{km}_kc{kc}"
            print(f"  P4 {label}", flush=True)
            row = _volscale_arm_time(km, kc, label)
            print(f"    {R.fmt_cell(row)} → {row['verdict']}")
            rows.append(row)
    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    return {"stage": "p4_arm_time", "ranked": ranked, "rows": rows}


def _swings(ts: np.ndarray, px: np.ndarray, confirm: int):
    """Confirm-tick reversal swings: lists of (ts_ns, price_tick)."""
    swings_hi: list[tuple[int, int]] = []
    swings_lo: list[tuple[int, int]] = []
    run_hi, run_lo = int(px[0]), int(px[0])
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
    return swings_hi, swings_lo


def run_p6_fixed() -> dict:
    """Prior swing (excluding tip's own swing) + running session extreme at arm."""
    cfg = R.PANEL
    rows = []

    def month(sessions, x_pts: float, look_min: int, kind: str):
        x_ticks = max(1, int(round(x_pts / cfg.pts_per_tick)))
        look_ns = int(look_min * 60 * 1e9)
        confirm = cfg.confirm_ticks()
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

            swings_hi, swings_lo = _swings(ts, px, confirm)

            t0 = int(ts[0])
            or_end = t0 + 30 * 60 * 1_000_000_000
            or_mask = ts <= or_end
            or_hi = int(px[or_mask].max()) if or_mask.any() else int(px.max())
            or_lo = int(px[or_mask].min()) if or_mask.any() else int(px.min())

            fire_ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
            extremes = ev.column("extreme_tick").to_numpy().astype(np.int64)
            sides = ev.column("side").to_numpy().astype(np.int8)
            arms = (
                ev.column("arm_ns").to_numpy().astype(np.int64)
                if "arm_ns" in ev.column_names
                else fire_ts
            )

            keep = []
            for i in range(len(fire_ts)):
                tip = int(extremes[i])
                side = int(sides[i])
                arm = int(arms[i])
                ok = False
                if kind == "session_extreme_at_arm":
                    j = int(np.searchsorted(ts, arm, side="right"))
                    if j <= 0:
                        continue
                    ref = int(px[:j].max()) if side < 0 else int(px[:j].min())
                    ok = abs(tip - ref) <= x_ticks
                elif kind == "opening_range":
                    ok = abs(tip - or_hi) <= x_ticks or abs(tip - or_lo) <= x_ticks
                else:  # prior_swing — exclude the tip's own swing
                    cands = swings_hi if side < 0 else swings_lo
                    # The tip's own swing: the most recent swing at/before arm
                    # whose price == tip. Skip exactly that one (and anything
                    # after arm), then look for an earlier match.
                    skipped_self = False
                    for st, sp in reversed(cands):
                        if st > arm:
                            continue
                        if not skipped_self and sp == tip:
                            skipped_self = True
                            continue
                        if arm - st > look_ns:
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

    for x_pts in (2, 5):
        for look_min in (30, 60):
            for kind in ("prior_swing", "session_extreme_at_arm", "opening_range"):
                label = f"level_{kind}_X{x_pts}_L{look_min}"
                print(f"  P6 {label}", flush=True)
                sep, st = month(R.SEP, x_pts, look_min, kind)
                oct_, ot = month(R.OCT, x_pts, look_min, kind)
                both = R.pack(st + ot)
                row = {
                    "label": label,
                    "sep": sep,
                    "oct": oct_,
                    "both": both,
                    "both_net": both.get("net"),
                }
                row["verdict"] = R.verdict(row)
                print(f"    {R.fmt_cell(row)} → {row['verdict']}")
                rows.append(row)

    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    return {"stage": "p6_fixed", "ranked": ranked, "rows": rows}


def _mae_mfe_for_trades(trades: list) -> dict:
    """Path MAE/MFE from entry to exit for each TradeResult."""
    by_sid: dict[str, list] = {}
    for t in trades:
        by_sid.setdefault(t.session_id, []).append(t)

    maes, mfes = [], []
    for sid, trs in by_sid.items():
        nq = R.load_nq(sid)
        if nq.num_rows == 0:
            continue
        ts = nq.column("ts_ns").to_numpy().astype(np.int64)
        px = nq.column("price").to_numpy().astype(np.float64) * 0.25
        order = np.argsort(ts)
        ts, px = ts[order], px[order]
        for tr in trs:
            i0 = int(np.searchsorted(ts, tr.entry_ts_ns, side="left"))
            i1 = int(np.searchsorted(ts, tr.exit_ts_ns, side="right"))
            if i0 >= len(ts) or i1 <= i0:
                continue
            entry = float(tr.entry_px)
            path = px[i0:i1]
            if tr.side > 0:
                mfe = float(path.max() - entry)
                mae = float(entry - path.min())
            else:
                mfe = float(entry - path.min())
                mae = float(path.max() - entry)
            maes.append(max(0.0, mae))
            mfes.append(max(0.0, mfe))

    def stats(xs):
        if not xs:
            return {}
        a = np.asarray(xs, dtype=np.float64)
        return {
            "n": int(len(a)),
            "mean": float(np.mean(a)),
            "median": float(np.median(a)),
            "p25": float(np.percentile(a, 25)),
            "p75": float(np.percentile(a, 75)),
        }

    return {
        "mae": stats(maes),
        "mfe": stats(mfes),
        "mfe_gt_mae_frac": float(np.mean(np.array(mfes) > np.array(maes)))
        if maes
        else None,
    }


def run_retest_maemfe() -> dict:
    print("  Retest freeze (per-row risk) Sep+Oct + holdout", flush=True)
    sep, st = R.run_month(R.SEP, R.PANEL, entry="retest", retest=FREEZE_RP)
    oct_, ot = R.run_month(R.OCT, R.PANEL, entry="retest", retest=FREEZE_RP)
    hold, ht = R.run_month(HOLDOUT, R.PANEL, entry="retest", retest=FREEZE_RP)
    both_tr = st + ot
    both = R.pack(both_tr)
    # Uniform-risk comparison (old bug)
    print("  Retest freeze UNIFORM risk (R+sb) for delta", flush=True)
    risk = FREEZE_RP.retest_pts + FREEZE_RP.stop_beyond_pts
    uniform_bfn = lambda _sv, tp=FREEZE_RP.tp_mult * risk, sl=risk: (tp, sl)
    sep_u, stu = R.run_month(
        R.SEP, R.PANEL, entry="retest", retest=FREEZE_RP, bracket_fn=uniform_bfn
    )
    oct_u, otu = R.run_month(
        R.OCT, R.PANEL, entry="retest", retest=FREEZE_RP, bracket_fn=uniform_bfn
    )
    both_u = R.pack(stu + otu)

    out = {
        "per_row_risk": {
            "sep": sep,
            "oct": oct_,
            "both": both,
            "both_net": both.get("net"),
            "holdout": hold,
            "mae_mfe_insample": _mae_mfe_for_trades(both_tr),
            "mae_mfe_holdout": _mae_mfe_for_trades(ht),
        },
        "uniform_risk_legacy": {
            "sep": sep_u,
            "oct": oct_u,
            "both": both_u,
            "both_net": both_u.get("net"),
        },
    }
    print(
        f"    per-row: Sep {sep.get('net')} Oct {oct_.get('net')} both {both.get('net')} "
        f"hold {hold.get('net')} wr={both.get('wr')}"
    )
    print(
        f"    uniform: Sep {sep_u.get('net')} Oct {oct_u.get('net')} both {both_u.get('net')}"
    )
    mm = out["per_row_risk"]["mae_mfe_insample"]
    print(
        f"    IS MAE med={mm.get('mae', {}).get('median')} "
        f"MFE med={mm.get('mfe', {}).get('median')} "
        f"MFE>MAE={mm.get('mfe_gt_mae_frac')}"
    )
    mh = out["per_row_risk"]["mae_mfe_holdout"]
    print(
        f"    OOS MAE med={mh.get('mae', {}).get('median')} "
        f"MFE med={mh.get('mfe', {}).get('median')} "
        f"MFE>MAE={mh.get('mfe_gt_mae_frac')}"
    )
    return out


def main() -> int:
    R.clear_cache()
    print("=== Tip feature stats (60s window) ===", flush=True)
    tip_stats = tip_feature_stats()
    print(json.dumps(tip_stats, indent=2))

    print("\n=== P2 retip ===", flush=True)
    p2 = run_p2_fixed()

    print("\n=== P2 MBO ===", flush=True)
    p2m = run_p2_mbo()

    print("\n=== P4 arm-time volscale ===", flush=True)
    p4 = run_p4_fixed()

    print("\n=== P6 fixed levels ===", flush=True)
    p6 = run_p6_fixed()

    print("\n=== Retest MAE/MFE + per-row risk ===", flush=True)
    rt = run_retest_maemfe()

    payload = {
        "tip_stats": tip_stats,
        "p2": p2,
        "p2_mbo": p2m,
        "p4": p4,
        "p6": p6,
        "retest": rt,
    }
    dest = Path("results/EXP-0038/research_fixes.json")
    dest.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {dest}")

    best_p2 = p2["ranked"][0]
    best_p4 = p4["ranked"][0]
    best_p6 = p6["ranked"][0]
    R.append_summary(
        "## Research fixes (tip window / P4 arm / P6 / per-row risk / MBO / MAE-MFE)\n\n"
        f"- Tip 60s window: buy_frac p50={tip_stats.get('tip_buy_frac_60s_p50')}, "
        f"frac>=0.6={tip_stats.get('frac_ge_0.6')}, "
        f"frac>=0.7={tip_stats.get('frac_ge_0.7')}.\n"
        f"- P2 best after fix: `{best_p2['label']}` both={best_p2.get('both_net')} "
        f"({best_p2.get('verdict')}).\n"
        f"- MBO best: `{p2m['ranked'][0]['label']}` both={p2m['ranked'][0].get('both_net')}.\n"
        f"- P4 arm-time best: `{best_p4['label']}` both={best_p4.get('both_net')}.\n"
        f"- P6 fixed best: `{best_p6['label']}` both={best_p6.get('both_net')} "
        f"n Sep/Oct={best_p6['sep'].get('n')}/{best_p6['oct'].get('n')}.\n"
        f"- Retest per-row risk both={rt['per_row_risk'].get('both_net')} "
        f"(legacy uniform={rt['uniform_risk_legacy'].get('both_net')}); "
        f"holdout={rt['per_row_risk']['holdout'].get('net')}.\n"
        f"- IS MAE/MFE med: "
        f"{rt['per_row_risk']['mae_mfe_insample'].get('mae', {}).get('median')} / "
        f"{rt['per_row_risk']['mae_mfe_insample'].get('mfe', {}).get('median')}; "
        f"OOS: "
        f"{rt['per_row_risk']['mae_mfe_holdout'].get('mae', {}).get('median')} / "
        f"{rt['per_row_risk']['mae_mfe_holdout'].get('mfe', {}).get('median')}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
