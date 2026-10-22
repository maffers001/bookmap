"""EXP-0044 / H35 — Dwell + intact break-side wall (optional dual take trigger).

    python -m research.irt_lvn.run_dwell_hold                  # Oct
    python -m research.irt_lvn.run_dwell_hold --month 2026-09
    python -m research.irt_lvn.run_dwell_hold --month 2026-05
    python -m research.irt_lvn.run_dwell_hold --dig            # train+OOS report from events
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed, list_sessions
from research.features.regime import build_regime_index
from research.irt_lvn.dwell import (
    DwellConfig,
    arm_pack,
    duration_bucket_stats,
    enrich_events_with_dwell,
    monotone_nondecreasing,
)
from research.irt_lvn.hold_break import (
    HoldBreakConfig,
    census_labels,
    detect_hold_break_events,
    stratified_sample,
)
from research.irt_lvn.load_irt import default_irt_paths, load_irt_csv, session_id_to_ymd
from research.irt_lvn.of_scores import ScoreConfig, prepare_session_of
from research.irt_lvn.run_exp_0042 import validate_timezone
from research.irt_lvn.run_hold_break_of import (
    basis_offset,
    bounds,
    filt,
    load_depth_window_with_book,
    load_mbo_window,
    shift_bars,
)
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")


def _month_tag(month: str) -> str:
    y, m = (int(x) for x in month.split("-")[:2])
    return "" if (y, m) == (2026, 10) else f"_{y}{m:02d}"


def _r(x, nd: int = 3):
    if x is None:
        return "—"
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return "—"


def _dwell_cfg(exp: dict) -> DwellConfig:
    return DwellConfig(
        vol_lookback_min=int(exp.get("vol_lookback_min", 30)),
        pierce_min_k=float(exp.get("pierce_min_k", 1.0)),
        pierce_min_floor=float(exp.get("pierce_min_floor", 6.0)),
        pierce_min_cap=float(exp.get("pierce_min_cap", 20.0)),
        pierce_max_k=float(exp.get("pierce_max_k", 2.0)),
        pierce_max_floor=float(exp.get("pierce_max_floor", 12.0)),
        pierce_max_cap=float(exp.get("pierce_max_cap", 30.0)),
        band_k=float(exp.get("band_k", 0.75)),
        band_floor=float(exp.get("band_floor", 6.0)),
        band_cap=float(exp.get("band_cap", 18.0)),
        min_dwell_ms=int(exp.get("min_dwell_ms", 15_000)),
        max_dwell_ms=int(exp.get("max_dwell_ms", 90_000)),
        consume_max=float(exp.get("consume_max", 0.25)),
        stack_remain_min=float(exp.get("stack_remain_min", 0.5)),
        protect_consume_max=float(exp.get("protect_consume_max", 0.2)),
        reject_pts=float(exp.get("reject_pts", 30.0)),
        extension_pts=float(exp.get("extension_break_pts", 30.0)),
        tp_pts=float(exp.get("tp_pts", 30.0)),
        sl_pts=float(exp.get("sl_pts", 15.0)),
        slip_ticks=float(exp.get("slip_ticks", 1.0)),
        commission_pts_rt=float(exp.get("commission_pts_rt", 0.25)),
        horizon_ms=int(exp.get("horizon_ms", 120_000)),
        tick_radius=int(exp.get("tick_radius", 8)),
        far_band_ticks=int(exp.get("far_band_ticks", 20)),
        es_large_pct=float(exp.get("es_large_pct", 99.0)),
        large_take_sync_ms=int(exp.get("large_take_sync_ms", 2_000)),
    )


def _es_arrays(es: pa.Table) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if es.num_rows == 0:
        z = np.array([], dtype=np.int64)
        return z, np.array([], dtype=np.float64), np.array([], dtype=bool)
    ts = es.column("ts_ns").to_numpy().astype(np.int64)
    sz = es.column("size").to_numpy().astype(np.float64)
    buy = es.column("is_buy_aggressor").to_numpy().astype(bool)
    o = np.argsort(ts, kind="stable")
    return ts[o], sz[o], buy[o]


def run_month(exp: dict, month: str, out: Path, *, smoke: bool = False) -> Path:
    cfg_root = load_config()
    pq_dir = Path(cfg_root["parquet_dir"])
    y_sel, m_sel = (int(x) for x in month.split("-")[:2])
    out.mkdir(parents=True, exist_ok=True)

    max_align = float(exp.get("max_align_err_pts", 25.0))
    exclude = set(exp.get("exclude_sessions", []))
    basis_mad_max = float(exp.get("basis_shift_mad_max_pts", 2.0))
    placebo_shift = float(exp.get("placebo_shift_pts", 40.0))

    hb_cfg = HoldBreakConfig(
        pad_pts=float(exp.get("pad_pts", 3.0)),
        impulse_pts=float(exp.get("impulse_pts", 30.0)),
        impulse_lookback_ms=int(exp.get("impulse_lookback_ms", 60_000)),
        decision_horizon_ms=int(exp.get("decision_horizon_ms", 120_000)),
        reject_pts=float(exp.get("reject_pts", 30.0)),
        pierce_max_hold=float(exp.get("pierce_max_hold", 15.0)),
        pierce_min_break=float(exp.get("pierce_min_break", 15.0)),
        extension_break_pts=float(exp.get("extension_break_pts", 30.0)),
        cluster_tol_pts=float(exp.get("cluster_tol_pts", 5.0)),
        cooldown_ms=int(exp.get("cooldown_ms", 30_000)),
        dop_exclude_first_min=float(exp.get("dop_exclude_first_min", 5.0)),
    )
    # Need post-window covering max dwell + horizon for path / book
    post_ms = int(exp.get("max_dwell_ms", 90_000)) + int(exp.get("horizon_ms", 120_000)) + 5_000
    sc_cfg = ScoreConfig(
        large_size_pct=float(exp.get("large_size_pct", 99.9)),
        large_size_min=int(exp.get("large_size_min", 15)),
        tick_radius=int(exp.get("tick_radius", 8)),
        far_band_ticks=int(exp.get("far_band_ticks", 20)),
        z_lookback_ms=int(exp.get("z_lookback_ms", 1_800_000)),
        diagnostic_post_ms=post_ms,
    )
    dw_cfg = _dwell_cfg(exp)

    nq_irt = load_irt_csv(default_irt_paths(ROOT)["NQ"])
    nq_days = nq_irt.calendar_days()

    eligible = []
    for sid in list_sessions(pq_dir, exclude=exclude):
        try:
            y, m, d = session_id_to_ymd(sid)
        except Exception:
            continue
        if (y, m) != (y_sel, m_sel) or (y, m, d) not in nq_days:
            continue
        dpath = pq_dir / f"session_id={sid}" / "depth.parquet"
        mpath = pq_dir / f"session_id={sid}" / "mbo.parquet"
        if not (dpath.exists() and dpath.stat().st_size and mpath.exists() and mpath.stat().st_size):
            continue
        eligible.append(sid)
    if smoke:
        eligible = eligible[:1]
    print(f"Eligible {month} sessions: {eligible} -> {out}", flush=True)

    universe_parts: list[pa.Table] = []
    placebo_parts: list[pa.Table] = []
    skipped_align: list[str] = []
    basis_shifts: dict[str, float] = {}
    session_census: dict[str, dict] = {}

    def _tag(ev: pa.Table, sid: str, placebo: float) -> pa.Table:
        ev = ev.append_column("session_id", pa.array([sid] * ev.num_rows, type=pa.string()))
        return ev.append_column("placebo_shift", pa.array([placebo] * ev.num_rows, type=pa.float64()))

    for sid in eligible:
        print(f"[universe] {sid}", flush=True)
        nq0 = _prefer_single_feed(_load_trades(pq_dir, sid, "NQ"), pq_dir, sid, "NQ")
        if nq0.num_rows == 0:
            session_census[sid] = {"note": "empty"}
            continue
        mid = int(nq0.column("ts_ns").to_numpy()[nq0.num_rows // 2])
        lo, hi = bounds(mid)
        look_ns = int(hb_cfg.impulse_lookback_ms * 1_000_000)
        nq_ctx = filt(nq0, lo - look_ns, hi + int(hb_cfg.decision_horizon_ms * 1_000_000))
        if nq_ctx.num_rows == 0:
            session_census[sid] = {"note": "empty_window"}
            continue
        y, m, d = session_id_to_ymd(sid)
        day_lo = int(datetime(y, m, d, 0, 0, tzinfo=UK).timestamp() * 1e9)
        day_hi = int(datetime(y, m, d, 23, 59, 59, tzinfo=UK).timestamp() * 1e9)
        nq_bars = nq_irt.slice_range(day_lo, day_hi)
        nq_win = filt(nq0, lo, hi)
        align = validate_timezone(nq_bars, nq_win)
        med = align.get("median_abs_err_pts")
        basis = None
        if med is not None and med >= max_align:
            basis = basis_offset(nq_bars, nq_win)
            if basis.get("n", 0) >= 20 and basis["mad_pts"] <= basis_mad_max:
                shift = -round(basis["median_pts"] / 0.25) * 0.25
                nq_bars = shift_bars(nq_bars, shift)
                align = validate_timezone(nq_bars, nq_win)
                med = align.get("median_abs_err_pts")
                basis_shifts[sid] = shift
                print(f"  basis shift {shift:+.2f} pts (mad {basis['mad_pts']}) -> median_err={med}", flush=True)
        if med is None or med >= max_align:
            skipped_align.append(sid)
            session_census[sid] = {"note": "skip_align", "align": align, "basis": basis}
            print(f"  skip align {med}", flush=True)
            continue
        ev = detect_hold_break_events(nq_ctx, nq_bars, cfg=hb_cfg, window=(lo, hi), rth_open_ns=lo)
        cen = census_labels(ev)
        pl_cen: dict[str, int] = {}
        for sgn in (+1.0, -1.0):
            pl = detect_hold_break_events(
                nq_ctx, shift_bars(nq_bars, sgn * placebo_shift), cfg=hb_cfg, window=(lo, hi), rth_open_ns=lo
            )
            for k_, v_ in census_labels(pl).items():
                pl_cen[k_] = pl_cen.get(k_, 0) + v_
            if pl.num_rows:
                placebo_parts.append(_tag(pl, sid, sgn * placebo_shift))
        session_census[sid] = {
            "align": align, "basis": basis, "basis_shift": basis_shifts.get(sid),
            "census": cen, "n": ev.num_rows, "placebo_census": pl_cen,
        }
        print(f"  align ok median_err={med}  census {cen}  placebo {pl_cen}", flush=True)
        if ev.num_rows:
            universe_parts.append(_tag(ev, sid, 0.0))

    if not universe_parts:
        (out / "summary.md").write_text("# EXP-0044\n\nNo events.\n", encoding="utf-8")
        print("No universe events.", flush=True)
        return out

    universe = pa.concat_tables(universe_parts, promote_options="default")
    samp_kw = dict(
        n_per_class=int(exp.get("sample_n_per_class", 100)),
        min_per_class=int(exp.get("sample_min_per_class", 50)),
        max_per_session=int(exp.get("max_per_session", 12)),
        seed=int(exp.get("sample_seed", 42)),
    )
    sampled = stratified_sample(universe, **samp_kw)
    placebo_univ = pa.concat_tables(placebo_parts, promote_options="default") if placebo_parts else pa.table({})
    placebo_s = stratified_sample(placebo_univ, **samp_kw) if placebo_univ.num_rows else pa.table({})
    print(
        f"Universe {census_labels(universe)} n={universe.num_rows} | Sampled {census_labels(sampled)} n={sampled.num_rows} | "
        f"Placebo sampled {placebo_s.num_rows}",
        flush=True,
    )
    pq.write_table(universe, out / "universe.parquet")
    pq.write_table(sampled, out / "sample_pre_score.parquet")
    if placebo_univ.num_rows:
        pq.write_table(placebo_univ, out / "placebo_universe.parquet")

    scored_parts: list[pa.Table] = []
    placebo_scored: list[pa.Table] = []
    sids = sorted(
        set(sampled.column("session_id").to_pylist())
        | set(placebo_s.column("session_id").to_pylist() if placebo_s.num_rows else [])
    )
    for sid in sids:
        print(f"[dwell] {sid}", flush=True)
        smask = np.array(sampled.column("session_id").to_pylist()) == sid
        sev = sampled.take(np.where(smask)[0]) if smask.any() else sampled.slice(0, 0)
        pmask = (
            np.array(placebo_s.column("session_id").to_pylist()) == sid
            if placebo_s.num_rows else np.array([], dtype=bool)
        )
        pev = placebo_s.take(np.where(pmask)[0]) if pmask.any() else pa.table({})

        nq0 = _prefer_single_feed(_load_trades(pq_dir, sid, "NQ"), pq_dir, sid, "NQ")
        es0 = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
        mid = int(nq0.column("ts_ns").to_numpy()[nq0.num_rows // 2])
        lo, hi = bounds(mid)
        look_ns = int(sc_cfg.z_lookback_ms * 1_000_000)
        post_ns = int(sc_cfg.diagnostic_post_ms * 1_000_000)
        db = (lo - look_ns, hi + post_ns)
        depth, ib, ia = load_depth_window_with_book(pq_dir, sid, "NQ", db)
        mbo = load_mbo_window(pq_dir, sid, "NQ", db)
        trades = filt(nq0, db[0], db[1])
        es = filt(es0, db[0], db[1])
        print(
            f"  depth={depth.num_rows} mbo={mbo.num_rows} nq={trades.num_rows} es={es.num_rows} "
            f"events={sev.num_rows} placebo={pev.num_rows}",
            flush=True,
        )
        sof = prepare_session_of(trades, depth, mbo, cfg=sc_cfg, bounds=(lo, hi), init_books=(ib, ia))
        if sof is None:
            print("  skip: no OF", flush=True)
            continue
        bars, *_ = build_regime_index(trades)
        es_ts, es_sz, es_buy = _es_arrays(es)
        if sev.num_rows:
            scored_parts.append(
                enrich_events_with_dwell(sev, sof, dw_cfg, minute_bars=bars, es_ts=es_ts, es_sz=es_sz, es_buy=es_buy)
            )
        if pev.num_rows:
            placebo_scored.append(
                enrich_events_with_dwell(pev, sof, dw_cfg, minute_bars=bars, es_ts=es_ts, es_sz=es_sz, es_buy=es_buy)
            )
        print(f"  scored {sev.num_rows}+{pev.num_rows}", flush=True)

    events = pa.concat_tables(scored_parts, promote_options="default") if scored_parts else pa.table({})
    placebo = pa.concat_tables(placebo_scored, promote_options="default") if placebo_scored else pa.table({})
    if events.num_rows:
        pq.write_table(events, out / "events.parquet")
    if placebo.num_rows:
        pq.write_table(placebo, out / "placebo_events.parquet")

    from research.irt_lvn.confirm import pack_pnl

    a = arm_pack(events, "arm_a_fire", "pnl_arm_a") if events.num_rows else {}
    b = arm_pack(events, "arm_b_fire", "pnl_arm_b") if events.num_rows else {}
    if events.num_rows and "pnl_arm_a_protect" in events.column_names:
        pnl_p = events.column("pnl_arm_a_protect").to_numpy().astype(np.float64)
        fire_p = np.isfinite(pnl_p)
        sess = np.array(events.column("session_id").to_pylist())
        ts = events.column("t_touch_ns").to_numpy().astype(np.float64)
        ap = pack_pnl(pnl_p, sess, ts)
        if fire_p.sum() > 1:
            ap["se"] = round(float(np.std(pnl_p[fire_p], ddof=1) / np.sqrt(fire_p.sum())), 3)
        lab = np.array(events.column("label").to_pylist())
        ap["hold_share"] = round(float(np.mean(lab[fire_p] == "HOLD")), 3) if fire_p.any() else None
    else:
        ap = {}

    buckets = duration_bucket_stats(events) if events.num_rows else {}
    pa_ = arm_pack(placebo, "arm_a_fire", "pnl_arm_a") if placebo.num_rows else {}

    # strata splits
    strata: dict[str, dict] = {}
    if events.num_rows and "dwell_stratum" in events.column_names:
        st = np.array(events.column("dwell_stratum").to_pylist())
        for name in ("touch_dwell", "pierce_dwell"):
            m = st == name
            if not m.any():
                strata[name] = {}
                continue
            sub = events.take(np.where(m)[0])
            strata[name] = arm_pack(sub, "arm_a_fire", "pnl_arm_a")

    report = {
        "month": month,
        "universe": census_labels(universe),
        "sampled": census_labels(sampled),
        "arm_a": a,
        "arm_b": b,
        "arm_a_protect": ap,
        "placebo_arm_a": pa_,
        "duration_buckets": buckets,
        "strata": strata,
        "sessions": session_census,
        "skipped_align": skipped_align,
        "basis_shifts": basis_shifts,
        "n_events": events.num_rows,
        "n_placebo": placebo.num_rows,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    L = [
        f"# EXP-0044 — Dwell + intact break-side wall ({month})",
        "",
        f"Sampled `{census_labels(sampled)}` n={sampled.num_rows}. Bracket TP{dw_cfg.tp_pts:g}/SL{dw_cfg.sl_pts:g}.",
        "",
        "## Arms",
        "",
        "| arm | n | net | avg | win | days+ | maxDD | HOLD share |",
        "|---|---:|---:|---:|---:|---|---:|---:|",
        f"| A (dwell+intact) | {a.get('n', 0)} | {a.get('net', 0)} | {_r(a.get('avg'))} | {_r(a.get('win'))} | "
        f"{a.get('days_pos', 0)}/{a.get('days', 0)} | {a.get('max_dd', 0)} | {_r(a.get('hold_share'))} |",
        f"| B (+ NQ/ES large take) | {b.get('n', 0)} | {b.get('net', 0)} | {_r(b.get('avg'))} | {_r(b.get('win'))} | "
        f"{b.get('days_pos', 0)}/{b.get('days', 0)} | {b.get('max_dd', 0)} | {_r(b.get('hold_share'))} |",
        f"| A + protect stack | {ap.get('n', 0)} | {ap.get('net', 0)} | {_r(ap.get('avg'))} | {_r(ap.get('win'))} | "
        f"{ap.get('days_pos', 0)}/{ap.get('days', 0)} | {ap.get('max_dd', 0)} | {_r(ap.get('hold_share'))} |",
        f"| Placebo A (±{placebo_shift:g}) | {pa_.get('n', 0)} | {pa_.get('net', 0)} | {_r(pa_.get('avg'))} | "
        f"{_r(pa_.get('win'))} | {pa_.get('days_pos', 0)}/{pa_.get('days', 0)} | {pa_.get('max_dd', 0)} | — |",
        "",
        "## Duration survival (enter if still intact at T s)",
        "",
        "| T s | n | net | avg | hold_rate | days+ |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for name in ("15", "20", "30", "45"):
        st = buckets.get(name, {})
        L.append(
            f"| {name} | {st.get('n', 0)} | {st.get('net', 0)} | {_r(st.get('avg'))} | "
            f"{_r(st.get('hold_rate'))} | {st.get('days_pos', 0)}/{st.get('days', 0)} |"
        )
    L += ["", "## Strata (Arm A)", ""]
    for name, st in strata.items():
        L.append(f"- **{name}:** n={st.get('n', 0)} net={st.get('net', 0)} avg={_r(st.get('avg'))}")
    L.append("")
    (out / "summary.md").write_text("\n".join(L), encoding="utf-8")
    print(f"Wrote {out / 'summary.md'}", flush=True)
    return out


def _load_month_events(month: str) -> pa.Table | None:
    p = ROOT / "results" / f"EXP-0044{_month_tag(month)}" / "events.parquet"
    if not p.exists() or p.stat().st_size == 0:
        return None
    return pq.read_table(p)


def _load_month_placebo(month: str) -> pa.Table | None:
    p = ROOT / "results" / f"EXP-0044{_month_tag(month)}" / "placebo_events.parquet"
    if not p.exists() or p.stat().st_size == 0:
        return None
    return pq.read_table(p)


def _stack0043_fade(month: str) -> dict | None:
    """Context: frozen EXP-0043 StackConsume fade from existing STACK_FADE or events."""
    # Prefer STACK_FADE report if present
    sf = ROOT / "results" / "EXP-0043_STACK_FADE" / "report.json"
    if sf.exists():
        rep = json.loads(sf.read_text(encoding="utf-8"))
        # structure may be by month
        for key in ("by_month", "months", "results"):
            if key in rep and month in rep[key]:
                return rep[key][month].get("overall") or rep[key][month]
        if "oos" in rep and month == rep.get("oos_month"):
            return rep.get("oos", {}).get("overall")
    # fallback: events with StackConsume
    p = ROOT / "results" / f"EXP-0043{_month_tag(month)}" / "events.parquet"
    if not p.exists():
        return None
    from research.irt_lvn.confirm import fade_when_score_le, pack_pnl
    tbl = pq.read_table(p)
    if "StackConsume" not in tbl.column_names or "pnl_fade_tc_tp30_sl15" not in tbl.column_names:
        return None
    pnl = fade_when_score_le(tbl, "StackConsume", -0.25, "tp30_sl15")
    sess = np.array(tbl.column("session_id").to_pylist())
    ts = tbl.column("t_touch_ns").to_numpy().astype(np.float64)
    return pack_pnl(pnl, sess, ts)


def run_dig(exp: dict, out: Path) -> None:
    train = [str(m) for m in exp.get("train_months", ["2026-10", "2026-09"])]
    oos = str(exp.get("oos_month", "2026-05"))
    out.mkdir(parents=True, exist_ok=True)

    months = train + [oos]
    ev = {m: _load_month_events(m) for m in months}
    pl = {m: _load_month_placebo(m) for m in months}
    missing = [m for m, t in ev.items() if t is None]
    if missing:
        raise SystemExit(f"missing EXP-0044 events for {missing}; run run_dwell_hold --month … first")

    train_tbl = pa.concat_tables([ev[m] for m in train], promote_options="default")
    report: dict = {"train_months": train, "oos_month": oos}

    L = [
        "# EXP-0044 dig — dwell + intact wall (H35)",
        "",
        f"Train = {', '.join(train)}. OOS = {oos} (read once). Bracket TP30/SL15.",
        "",
        "## Arm A / B / protect / placebo by month",
        "",
        "| month | role | Arm A n/net/avg/days+/DD | Arm B | protect | placebo A |",
        "|---|---|---|---|---|---|",
    ]
    by_month: dict = {}
    for m in months:
        role = "OOS" if m == oos else "train"
        a = arm_pack(ev[m], "arm_a_fire", "pnl_arm_a")
        b = arm_pack(ev[m], "arm_b_fire", "pnl_arm_b")
        pnl_p = ev[m].column("pnl_arm_a_protect").to_numpy().astype(np.float64)
        from research.irt_lvn.confirm import pack_pnl
        sess = np.array(ev[m].column("session_id").to_pylist())
        ts = ev[m].column("t_touch_ns").to_numpy().astype(np.float64)
        ap = pack_pnl(pnl_p, sess, ts)
        pa_ = arm_pack(pl[m], "arm_a_fire", "pnl_arm_a") if pl[m] is not None else {}
        by_month[m] = {"role": role, "arm_a": a, "arm_b": b, "protect": ap, "placebo_a": pa_}
        L.append(
            f"| {m} | {role} | {a['n']}/{a['net']}/{_r(a['avg'])}/{a['days_pos']}/{a['days']}/{a['max_dd']} | "
            f"{b['n']}/{b['net']}/{_r(b['avg'])} | {ap['n']}/{ap['net']}/{_r(ap['avg'])} | "
            f"{pa_.get('n', 0)}/{pa_.get('net', 0)}/{_r(pa_.get('avg'))} |"
        )
    report["by_month"] = by_month

    # (1) duration claim on train
    buckets_tr = duration_bucket_stats(train_tbl)
    buckets_oos = duration_bucket_stats(ev[oos])
    report["duration_train"] = buckets_tr
    report["duration_oos"] = buckets_oos
    hold_rates = [buckets_tr[k].get("hold_rate") for k in ("15", "20", "30", "45") if k in buckets_tr]
    avgs = [buckets_tr[k].get("avg") for k in ("15", "20", "30", "45") if k in buckets_tr]
    mono_h = monotone_nondecreasing(hold_rates)
    mono_a = monotone_nondecreasing(avgs)
    report["duration_monotone_hold"] = mono_h
    report["duration_monotone_avg"] = mono_a

    L += [
        "",
        "## (1) Duration claim — survival at T s (still intact → enter fade)",
        "",
        "### Train (Oct+Sep pooled)",
        "",
        "| T s | n | net | avg | hold_rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for k in ("15", "20", "30", "45"):
        st = buckets_tr.get(k, {})
        L.append(f"| {k} | {st.get('n', 0)} | {st.get('net', 0)} | {_r(st.get('avg'))} | {_r(st.get('hold_rate'))} |")
    L += [
        "",
        f"Monotone non-decreasing hold_rate: **{mono_h}**. Monotone avg: **{mono_a}**.",
        "",
        "### OOS (May) — read once",
        "",
        "| T s | n | net | avg | hold_rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for k in ("15", "20", "30", "45"):
        st = buckets_oos.get(k, {})
        L.append(f"| {k} | {st.get('n', 0)} | {st.get('net', 0)} | {_r(st.get('avg'))} | {_r(st.get('hold_rate'))} |")

    # (2) vs StackConsume baseline
    L += ["", "## (2) Arm A vs EXP-0043 StackConsume≤−0.25 fade (context)", "",
          "| month | Arm A net/avg | StackConsume fade net/avg |", "|---|---|---|"]
    vs43: dict = {}
    for m in months:
        sc = _stack0043_fade(m)
        a = by_month[m]["arm_a"]
        vs43[m] = {"arm_a": a, "stack_fade": sc}
        if sc:
            L.append(f"| {m} | {a['net']}/{_r(a['avg'])} | {sc.get('net')}/{_r(sc.get('avg'))} |")
        else:
            L.append(f"| {m} | {a['net']}/{_r(a['avg'])} | — |")
    report["vs_stack_consume"] = vs43

    # (3) Arm B vs A on May
    a_oos = by_month[oos]["arm_a"]
    b_oos = by_month[oos]["arm_b"]
    d_avg = (b_oos.get("avg") or 0) - (a_oos.get("avg") or 0)
    se_b = b_oos.get("se") or 0
    adopt_b = (
        b_oos.get("n", 0) >= 20
        and (b_oos.get("avg") or -1) > (a_oos.get("avg") or 0)
        and abs(d_avg) >= (se_b or 0)
        and (b_oos.get("max_dd") or 1e9) <= (a_oos.get("max_dd") or 0)
    )
    report["arm_b_adopt"] = adopt_b

    # (4) protect gate — select on train
    a_tr = arm_pack(train_tbl, "arm_a_fire", "pnl_arm_a")
    pnl_p = train_tbl.column("pnl_arm_a_protect").to_numpy().astype(np.float64)
    from research.irt_lvn.confirm import pack_pnl
    sess = np.array(train_tbl.column("session_id").to_pylist())
    ts = train_tbl.column("t_touch_ns").to_numpy().astype(np.float64)
    ap_tr = pack_pnl(pnl_p, sess, ts)
    select_protect = ap_tr["n"] >= 40 and (ap_tr.get("avg") or -1) > (a_tr.get("avg") or 0)
    report["protect_selected"] = select_protect
    report["protect_train"] = ap_tr
    ap_oos = by_month[oos]["protect"]

    L += [
        "",
        "## (3) Arm B vs Arm A on OOS",
        "",
        f"Arm A: n={a_oos['n']} avg {_r(a_oos['avg'])} ± {_r(a_oos.get('se'))} maxDD {a_oos['max_dd']}. "
        f"Arm B: n={b_oos['n']} avg {_r(b_oos['avg'])} ± {_r(b_oos.get('se'))} maxDD {b_oos['max_dd']}. "
        f"Δavg {_r(d_avg)}. **Adopt Arm B: {adopt_b}**.",
        "",
        "## (4) Protective-stack dig",
        "",
        f"Train protect n={ap_tr['n']} avg {_r(ap_tr.get('avg'))} vs Arm A avg {_r(a_tr.get('avg'))}. "
        f"**Selected: {select_protect}**.",
    ]
    if select_protect:
        L.append(
            f"OOS protect: n={ap_oos['n']} net {ap_oos['net']} avg {_r(ap_oos.get('avg'))} "
            f"maxDD {ap_oos['max_dd']} vs Arm A avg {_r(a_oos.get('avg'))} maxDD {a_oos['max_dd']}."
        )
    else:
        L.append("Not selected on train — May protect numbers are appendix only (not for adoption).")

    # Decision
    a_pos = all(by_month[m]["arm_a"].get("net", 0) > 0 for m in months)
    days_frac = (a_oos.get("days_pos", 0) / max(1, a_oos.get("days", 1)))
    placebo_beats = (a_oos.get("avg") or 0) > (by_month[oos]["placebo_a"].get("avg") or -1e9)
    soft = (
        a_oos.get("net", 0) > 0
        and days_frac >= 0.5
        and (mono_h or mono_a)
        and placebo_beats
        and not (a_oos.get("avg") or 0) < 0
    )
    if not (mono_h or mono_a):
        verdict = "reject / park — duration claim flips or flat on train"
    elif (a_oos.get("avg") or 0) <= (by_month[oos]["placebo_a"].get("avg") or 0) and by_month[oos]["placebo_a"].get("n", 0) > 10:
        verdict = "reject / park — Arm A ≤ placebo on May"
    elif soft:
        verdict = "soft confirm Arm A — dig / forward-paper; Arm B " + ("adopted" if adopt_b else "not adopted")
    elif a_oos.get("net", 0) > 0:
        verdict = "mixed — Arm A net+ on May but duration/days+/placebo checks incomplete"
    else:
        verdict = "reject / park — Arm A not confirmed on May"

    report["verdict"] = verdict
    L += ["", "## Decision", "", f"- **{verdict}**", ""]
    if not a_pos:
        L.append("- Note: Arm A net not positive in every month.")
    L.append(f"Artifacts: `{out / 'report.json'}`.")
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(L), encoding="utf-8")
    print(verdict)
    print(f"Wrote {out / 'summary.md'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="EXP-0044 dwell hold")
    ap.add_argument("--config", default=str(ROOT / "research" / "experiments" / "EXP-0044.yaml"))
    ap.add_argument("--month", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--dig", action="store_true", help="Aggregate train+OOS dig from existing events")
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as f:
        exp = yaml.safe_load(f)

    if args.dig:
        out = Path(args.out) if args.out else ROOT / "results" / "EXP-0044_DWELL"
        run_dig(exp, out)
        return

    month = args.month or str(exp.get("month_prefix", "2026-10"))
    y, m = (int(x) for x in month.split("-")[:2])
    if args.out:
        out = Path(args.out)
    else:
        out = ROOT / "results" / f"EXP-0044{_month_tag(month)}"
    run_month(exp, month, out, smoke=args.smoke)


if __name__ == "__main__":
    main()
