"""EXP-0045 / H36 — Open-book standout resting orders as levels.

    python -m research.irt_lvn.run_open_book_levels                  # Oct
    python -m research.irt_lvn.run_open_book_levels --month 2026-09
    python -m research.irt_lvn.run_open_book_levels --month 2026-05
    python -m research.irt_lvn.run_open_book_levels --dig            # cross-month verdict
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed, list_sessions
from research.irt_lvn.events import PTS_PER_TICK
from research.irt_lvn.load_irt import session_id_to_ymd
from research.irt_lvn.of_scores import (
    NS_PER_MS,
    ScoreConfig,
    _bbo,
    _resolve_mbo,
    _snapshot_books_at_times,
    prepare_session_of,
)
from research.irt_lvn.open_book import (
    OpenBookConfig,
    apply_of_gates,
    arm_stats,
    detect_level_events,
    levels_table,
    mbo_resting_at,
    select_standouts,
    snapshot_clusters,
    vol_unit_preopen,
)
from research.irt_lvn.run_hold_break_of import (
    filt,
    load_depth_window_with_book,
    load_mbo_window,
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


def _hhmm_ns(y: int, m: int, d: int, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":")[:2])
    return int(datetime(y, m, d, hh, mm, tzinfo=UK).timestamp() * 1e9)


def _cfg(exp: dict) -> OpenBookConfig:
    fb = exp.get("fixed_brackets")
    fixed = (
        tuple(tuple(float(x) for x in b) for b in fb)
        if fb else ((30.0, 15.0), (20.0, 10.0), (40.0, 10.0), (10.0, 20.0))
    )
    return OpenBookConfig(
        book_radius_pts=float(exp.get("book_radius_pts", 100.0)),
        cluster_ticks=int(exp.get("cluster_ticks", 2)),
        standout_pct=float(exp.get("standout_pct", 99.0)),
        large_mult=float(exp.get("large_mult", 2.0)),
        top_n_per_side=int(exp.get("top_n_per_side", 5)),
        near_mid_exclude_pts=float(exp.get("near_mid_exclude_pts", 10.0)),
        large_size_pct=float(exp.get("large_size_pct", 99.9)),
        large_size_min=int(exp.get("large_size_min", 15)),
        age_scale_s=float(exp.get("age_scale_s", 900.0)),
        age_w_min=float(exp.get("age_w_min", 0.25)),
        age_w_max=float(exp.get("age_w_max", 1.0)),
        old_age_s=float(exp.get("old_age_s", 900.0)),
        age_cap_s=float(exp.get("age_cap_s", 7200.0)),
        frac_old_thr=float(exp.get("frac_old_thr", 0.5)),
        alive_frac=float(exp.get("alive_frac", 0.5)),
        pull_size_frac=float(exp.get("pull_size_frac", 0.25)),
        pull_trade_frac=float(exp.get("pull_trade_frac", 0.25)),
        pad_pts=float(exp.get("pad_pts", 2.0)),
        break_floor_pts=float(exp.get("break_floor_pts", 8.0)),
        retest_horizon_ms=int(exp.get("retest_horizon_ms", 1_200_000)),
        reject_pts=float(exp.get("reject_pts", 30.0)),
        extension_pts=float(exp.get("extension_pts", 30.0)),
        confirm_ms=int(exp.get("confirm_ms", 2_000)),
        stack_consume_thr=float(exp.get("stack_consume_thr", -0.25)),
        take_at_thr=float(exp.get("take_at_thr", 0.25)),
        horizon_ms=int(exp.get("horizon_ms", 180_000)),
        slip_ticks=float(exp.get("slip_ticks", 1.0)),
        commission_pts_rt=float(exp.get("commission_pts_rt", 0.25)),
        sl_floor=float(exp.get("sl_floor", 6.0)),
        sl_cap=float(exp.get("sl_cap", 15.0)),
        primary_rr=float(exp.get("primary_rr", 2.0)),
        placebo_shift_pts=float(exp.get("placebo_shift_pts", 40.0)),
        fixed_brackets=fixed,
    )


def _exp_tag(exp: dict) -> str:
    return str(exp.get("name", "EXP-0045"))


def run_month(exp: dict, month: str, out: Path, *, smoke: bool = False) -> dict:
    cfg_root = load_config()
    pq_dir = Path(cfg_root["parquet_dir"])
    y_sel, m_sel = (int(x) for x in month.split("-")[:2])
    out.mkdir(parents=True, exist_ok=True)
    exclude = set(exp.get("exclude_sessions", []))
    ob = _cfg(exp)
    snap_hhmm = str(exp.get("snap_time", "14:35"))
    end_hhmm = str(exp.get("window_end", "17:00"))
    vol_a = str(exp.get("vol_start", "14:00"))
    vol_b = str(exp.get("vol_end", "14:30"))
    post_ms = int(ob.horizon_ms) + int(ob.retest_horizon_ms) + 5_000
    sc_cfg = ScoreConfig(
        large_size_pct=ob.large_size_pct,
        large_size_min=ob.large_size_min,
        tick_radius=int(exp.get("tick_radius", 8)),
        far_band_ticks=int(exp.get("far_band_ticks", 20)),
        z_lookback_ms=int(exp.get("z_lookback_ms", 1_800_000)),
        diagnostic_post_ms=post_ms,
    )

    eligible = []
    for sid in list_sessions(pq_dir, exclude=exclude):
        try:
            y, m, d = session_id_to_ymd(sid)
        except Exception:
            continue
        if (y, m) != (y_sel, m_sel):
            continue
        dpath = pq_dir / f"session_id={sid}" / "depth.parquet"
        mpath = pq_dir / f"session_id={sid}" / "mbo.parquet"
        if dpath.exists() and dpath.stat().st_size and mpath.exists() and mpath.stat().st_size:
            eligible.append(sid)
    if smoke:
        eligible = eligible[:1]
    print(f"Eligible {month} sessions: {len(eligible)} -> {out}", flush=True)

    level_parts: list[pa.Table] = []
    event_parts: list[pa.Table] = []
    placebo_parts: list[pa.Table] = []
    census: dict[str, Any] = {}

    role = str(exp.get("role", "NQ"))
    print(f"Role={role} standout_pct={ob.standout_pct}", flush=True)

    for sid in eligible:
        print(f"[session] {sid}", flush=True)
        y, m, d = session_id_to_ymd(sid)
        open_ns = _hhmm_ns(y, m, d, "14:30")
        snap_ns = _hhmm_ns(y, m, d, snap_hhmm)
        win_hi = _hhmm_ns(y, m, d, end_hhmm)
        vol_lo = _hhmm_ns(y, m, d, vol_a)
        vol_hi = _hhmm_ns(y, m, d, vol_b)
        age_lo = snap_ns - int(ob.age_cap_s * 1e9)

        tr0 = _prefer_single_feed(_load_trades(pq_dir, sid, role), pq_dir, sid, role)
        if tr0.num_rows == 0:
            census[sid] = {"note": "empty"}
            continue
        tr = filt(tr0, vol_lo, win_hi + int(post_ms * NS_PER_MS))
        ts = tr.column("ts_ns").to_numpy().astype(np.int64)
        px = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
        o = np.argsort(ts, kind="stable")
        ts, px = ts[o], px[o]
        u = vol_unit_preopen(ts, px, vol_lo, vol_hi)

        depth, init_b, init_a = load_depth_window_with_book(
            pq_dir, sid, role, (age_lo, win_hi + int(post_ms * NS_PER_MS)),
        )
        mbo = load_mbo_window(pq_dir, sid, role, (age_lo, win_hi + int(post_ms * NS_PER_MS)))
        if depth.num_rows == 0:
            census[sid] = {"note": "no_depth"}
            continue

        sof = prepare_session_of(
            tr0, depth, mbo, cfg=sc_cfg, bounds=(open_ns, win_hi), init_books=(init_b, init_a),
        )
        if sof is None:
            census[sid] = {"note": "sof_fail"}
            continue

        # book at snap
        books = _snapshot_books_at_times(sof, [snap_ns])
        bids, asks = books.get(snap_ns, (dict(sof.init_bids), dict(sof.init_asks)))
        bb, aa = _bbo(bids, asks)
        if bb is None or aa is None:
            census[sid] = {"note": "no_bbo"}
            continue
        mid = (bb + aa) // 2

        # Bright-price floor on the snap book, then cluster those prices only
        # (avoids dense ES books merging into one mega-cluster per side).
        standout_size_pct = float(exp.get("standout_size_pct", ob.large_size_pct))
        rad_t = int(round(ob.book_radius_pts / PTS_PER_TICK))
        nz_snap = np.asarray(
            [
                s for p, s in list(bids.items()) + list(asks.items())
                if s > 0 and abs(p - mid) <= rad_t
            ],
            dtype=np.float64,
        )
        bright_floor = int(max(
            ob.large_size_min,
            float(np.percentile(nz_snap, standout_size_pct)) if len(nz_snap) else ob.large_size_min,
        ))
        clusters = snapshot_clusters(
            bids, asks, mid_tick=mid, cfg=ob, min_price_size=float(bright_floor),
        )
        m_ts, m_op, m_bid, m_px, m_sz, _st = _resolve_mbo(mbo, age_lo, snap_ns + 1)
        data_start = int(m_ts[0]) if len(m_ts) else age_lo
        resting = mbo_resting_at(
            m_ts, m_op, m_bid, m_px, m_sz, snap_ns,
            data_start_ns=data_start, age_cap_s=ob.age_cap_s,
        )
        # Among bright clusters, keep those ≥ standout_pct of bright-cluster sizes
        # (large_thr=1 → floor is driven by cluster percentile only).
        standouts = select_standouts(
            clusters, resting, snap_ns=snap_ns, large_thr=1, cfg=ob,
        )
        lt = levels_table(standouts, session_id=sid, snap_ns=snap_ns, u=u)
        if lt.num_rows:
            level_parts.append(lt)

        ev = detect_level_events(
            standouts, sof, ts, px, snap_ns=snap_ns, win_hi=win_hi, u=u, cfg=ob, placebo=False,
        )
        if ev.num_rows:
            ev = ev.append_column("session_id", pa.array([sid] * ev.num_rows, type=pa.string()))
            ev = apply_of_gates(ev, sof, ts, px, ob)
            event_parts.append(ev)

        # placebo: shift ±40
        pl_levels: list[dict] = []
        for sgn in (+1.0, -1.0):
            shift_ticks = int(round(sgn * ob.placebo_shift_pts / PTS_PER_TICK))
            for lv in standouts:
                pl = dict(lv)
                pl["centre_tick"] = int(lv["centre_tick"]) + shift_ticks
                pl["px_lo"] = int(lv["px_lo"]) + shift_ticks
                pl["px_hi"] = int(lv["px_hi"]) + shift_ticks
                pl_levels.append(pl)
        pev = detect_level_events(
            pl_levels, sof, ts, px, snap_ns=snap_ns, win_hi=win_hi, u=u, cfg=ob, placebo=True,
        )
        if pev.num_rows:
            pev = pev.append_column("session_id", pa.array([sid] * pev.num_rows, type=pa.string()))
            pev = apply_of_gates(pev, sof, ts, px, ob)
            placebo_parts.append(pev)

        n_touch = int(ev.column("has_touch").to_numpy().sum()) if ev.num_rows else 0
        n_pull = int(np.sum(np.array(ev.column("lifecycle").to_pylist()) == "pulled")) if ev.num_rows else 0
        census[sid] = {
            "n_standout": len(standouts),
            "n_touch": n_touch,
            "n_pull": n_pull,
            "large_thr": sof.large_thr,
            "vol_unit": u,
            "mid": mid * PTS_PER_TICK,
        }
        print(f"  standouts={len(standouts)} touches={n_touch} u={u:.2f} large_thr={sof.large_thr}", flush=True)

    levels = pa.concat_tables(level_parts) if level_parts else pa.table({})
    events = pa.concat_tables(event_parts, promote_options="default") if event_parts else pa.table({})
    placebo = pa.concat_tables(placebo_parts, promote_options="default") if placebo_parts else pa.table({})

    if levels.num_rows:
        pq.write_table(levels, out / "levels.parquet")
    if events.num_rows:
        pq.write_table(events, out / "events.parquet")
    if placebo.num_rows:
        pq.write_table(placebo, out / "placebo_events.parquet")

    report = _month_report(month, events, placebo, levels, census, ob)
    report["standout_pct"] = ob.standout_pct
    report["role"] = role
    report["experiment"] = _exp_tag(exp)
    report["hypothesis"] = exp.get("hypothesis", "")
    if events.num_rows and "has_touch" in events.column_names:
        has = events.column("has_touch").to_numpy() > 0.5
        touched = _subset(events, has)
        bnames = [c.replace("pnl_T0_", "") for c in events.column_names if c.startswith("pnl_T0_")]
        report["t0_brackets"] = {bn: arm_stats(touched, "T0", bn) for bn in bnames}
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text(_month_md(report, month, out), encoding="utf-8")
    print(f"Wrote {out / 'summary.md'}  touches={report.get('n_touch', 0)}", flush=True)
    return report


def _subset(tbl: pa.Table, mask: np.ndarray) -> pa.Table:
    idx = np.where(mask)[0]
    return tbl.take(idx) if len(idx) else tbl.slice(0, 0)


def _month_report(
    month: str, events: pa.Table, placebo: pa.Table, levels: pa.Table,
    census: dict, cfg: OpenBookConfig,
) -> dict:
    has = events.column("has_touch").to_numpy() > 0.5 if events.num_rows else np.array([], dtype=bool)
    touched = _subset(events, has) if events.num_rows else events
    old_m = touched.column("age_stratum").to_numpy() >= 0.5 if touched.num_rows else np.array([], dtype=bool)
    fresh_m = ~old_m if len(old_m) else old_m
    life = np.array(events.column("lifecycle").to_pylist()) if events.num_rows else np.array([])
    age_all = events.column("age_stratum").to_numpy() if events.num_rows else np.array([])

    def _pull_rate(stratum_old: bool | None) -> float | None:
        if len(life) == 0:
            return None
        m = np.ones(len(life), dtype=bool) if stratum_old is None else (
            age_all >= 0.5 if stratum_old else age_all < 0.5
        )
        if not m.any():
            return None
        return float(np.mean(life[m] == "pulled"))

    arms = {}
    for arm in ("T0", "T2", "R0", "R2"):
        arms[arm] = {
            "all": arm_stats(touched, arm),
            "old": arm_stats(_subset(touched, old_m), arm) if touched.num_rows else arm_stats(pa.table({}), arm),
            "fresh": arm_stats(_subset(touched, fresh_m), arm) if touched.num_rows else arm_stats(pa.table({}), arm),
        }
    ph_has = placebo.column("has_touch").to_numpy() > 0.5 if placebo.num_rows else np.array([], dtype=bool)
    ph_t = _subset(placebo, ph_has) if placebo.num_rows else placebo
    placebo_arms = {arm: arm_stats(ph_t, arm) for arm in ("T0", "T2", "R0", "R2")}

    hold60 = touched.column("hold_60").to_numpy() if touched.num_rows else np.array([])
    hold180 = touched.column("hold_180").to_numpy() if touched.num_rows else np.array([])

    return {
        "month": month,
        "n_sessions": len(census), "n_levels": int(levels.num_rows),
        "n_touch": int(has.sum()) if len(has) else 0,
        "n_retest": int(np.sum(touched.column("t_retest_ns").to_numpy() > 0)) if touched.num_rows else 0,
        "n_broke": int(np.sum(touched.column("broke").to_numpy() > 0.5)) if touched.num_rows else 0,
        "hold_rate_60": _r(float(np.nanmean(hold60)), 3) if len(hold60) else None,
        "hold_rate_180": _r(float(np.nanmean(hold180)), 3) if len(hold180) else None,
        "pull_rate_all": _pull_rate(None),
        "pull_rate_old": _pull_rate(True),
        "pull_rate_fresh": _pull_rate(False),
        "arms": arms,
        "placebo_arms": placebo_arms,
        "census": census,
        "claims": {
            "old_T0_gt_fresh": _gt_avg(arms["T0"]["old"], arms["T0"]["fresh"]),
            "old_T0_gt_placebo": _gt_avg(arms["T0"]["old"], placebo_arms["T0"]),
            "T2_ge_T0": _ge_avg_dd(arms["T2"]["all"], arms["T0"]["all"]),
            "R0_gt_0": (arms["R0"]["all"].get("net") or 0) > 0,
            "R0_gt_placebo": _gt_avg(arms["R0"]["all"], placebo_arms["R0"]),
            "pull_fresh_gt_old": (
                (_pull_rate(False) or 0) > (_pull_rate(True) or 0)
                if _pull_rate(False) is not None and _pull_rate(True) is not None else None
            ),
        },
    }


def _gt_avg(a: dict, b: dict) -> bool | None:
    if a.get("avg") is None or b.get("avg") is None:
        return None
    return float(a["avg"]) > float(b["avg"])


def _ge_avg_dd(a: dict, b: dict) -> bool | None:
    if a.get("avg") is None or b.get("avg") is None:
        return None
    return float(a["avg"]) >= float(b["avg"]) and float(a.get("max_dd") or 0) <= float(b.get("max_dd") or 1e9)


def _fmt_arm(st: dict) -> str:
    return f"{st.get('n', 0)}/{st.get('net', 0)}/{_r(st.get('avg'))}/{st.get('days_pos', 0)}/{st.get('days', 0)}/{st.get('max_dd', 0)}"


def _month_md(report: dict, month: str, out: Path) -> str:
    tag = report.get("experiment", "EXP-0045")
    role = report.get("role", "NQ")
    L = [
        f"# {tag} — Open-book standout levels ({role}, {month})",
        "",
        f"Sessions {report['n_sessions']} · levels {report['n_levels']} · touches {report['n_touch']} · "
        f"broke {report['n_broke']} · retests {report['n_retest']}. "
        f"Hold@60 {_r(report['hold_rate_60'])} · Hold@180 {_r(report['hold_rate_180'])}. "
        f"Pull rate all/old/fresh: {_r(report['pull_rate_all'])} / {_r(report['pull_rate_old'])} / {_r(report['pull_rate_fresh'])}.",
        "",
        "## Primary bracket vol2 — arms (n/net/avg/days+/days/maxDD)",
        "",
        "| arm | all | old | fresh | placebo |",
        "|---|---|---|---|---|",
    ]
    for arm in ("T0", "T2", "R0", "R2"):
        a = report["arms"][arm]
        L.append(
            f"| {arm} | {_fmt_arm(a['all'])} | {_fmt_arm(a['old'])} | {_fmt_arm(a['fresh'])} | "
            f"{_fmt_arm(report['placebo_arms'][arm])} |"
        )
    if report.get("t0_brackets"):
        L += ["", "## T0 fade across brackets (n/net/avg/days+/DD)", "",
              "| bracket | n/net/avg/days+/days/maxDD |", "|---|---|"]
        for bn, st in report["t0_brackets"].items():
            L.append(f"| {bn} | {_fmt_arm(st)} |")
    L += ["", "## Claims (this month)", ""]
    for k, v in report["claims"].items():
        L.append(f"- `{k}`: **{v}**")
    L += ["", f"Artifacts: `{out}`.", ""]
    return "\n".join(L)


def run_dig(exp: dict, root: Path) -> Path:
    """Cross-month verdict: Oct train, Sep+May OOS."""
    tag = _exp_tag(exp)
    dig = str(exp.get("dig_tag", "OPEN"))
    role = str(exp.get("role", "NQ"))
    out = root / "results" / f"{tag}_{dig}"
    out.mkdir(parents=True, exist_ok=True)
    months = ["2026-10"] + list(exp.get("oos_months", ["2026-09", "2026-05"]))
    reports = {}
    for m in months:
        p = root / "results" / f"{tag}{_month_tag(m)}" / "report.json"
        if p.exists():
            reports[m] = json.loads(p.read_text(encoding="utf-8"))

    L = [
        f"# {tag} dig — open-book standout levels ({role}, {exp.get('hypothesis', '')})",
        "",
        "Oct = develop. Sep and May = frozen OOS (read once).",
        "",
        "## Touches / pull / hold",
        "",
        "| month | role | sessions | levels | touches | retests | hold60 | pull all/old/fresh |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for m in months:
        r = reports.get(m)
        if not r:
            L.append(f"| {m} | — | — | — | — | — | — | — |")
            continue
        role = "train" if m == "2026-10" else "OOS"
        L.append(
            f"| {m} | {role} | {r['n_sessions']} | {r['n_levels']} | {r['n_touch']} | {r['n_retest']} | "
            f"{_r(r['hold_rate_60'])} | {_r(r['pull_rate_all'])}/{_r(r['pull_rate_old'])}/{_r(r['pull_rate_fresh'])} |"
        )

    L += ["", "## Arm T0 / T2 / R0 (vol2) by month", "",
          "| month | T0 all | T0 old | T0 fresh | T0 placebo | T2 | R0 |",
          "|---|---|---|---|---|---|---|"]
    for m in months:
        r = reports.get(m)
        if not r:
            continue
        a = r["arms"]
        L.append(
            f"| {m} | {_fmt_arm(a['T0']['all'])} | {_fmt_arm(a['T0']['old'])} | {_fmt_arm(a['T0']['fresh'])} | "
            f"{_fmt_arm(r['placebo_arms']['T0'])} | {_fmt_arm(a['T2']['all'])} | {_fmt_arm(a['R0']['all'])} |"
        )
    # bracket grid on T0 for train month if present
    oct_br = reports.get("2026-10", {}).get("t0_brackets") or {}
    if oct_br:
        L += ["", "## Oct T0 bracket grid", "", "| bracket | n/net/avg/days+/days/maxDD |", "|---|---|"]
        for bn, st in oct_br.items():
            L.append(f"| {bn} | {_fmt_arm(st)} |")

    # Verdict from registered claims
    oct_r = reports.get("2026-10", {})
    claims_oct = oct_r.get("claims", {})
    oos_ok = []
    for m in exp.get("oos_months", ["2026-09", "2026-05"]):
        r = reports.get(m)
        if not r:
            continue
        c = r["claims"]
        oos_ok.append({
            "month": m,
            "old_gt_fresh": c.get("old_T0_gt_fresh"),
            "old_gt_placebo": c.get("old_T0_gt_placebo"),
            "R0_gt_0": c.get("R0_gt_0"),
            "R0_gt_placebo": c.get("R0_gt_placebo"),
        })

    soft = (
        claims_oct.get("old_T0_gt_fresh") is True
        and claims_oct.get("old_T0_gt_placebo") is True
        and claims_oct.get("R0_gt_0") is True
        and claims_oct.get("R0_gt_placebo") is True
        and all(x.get("old_gt_placebo") is not False for x in oos_ok)
        and all(x.get("R0_gt_placebo") is not False for x in oos_ok)
    )
    reject = (
        claims_oct.get("old_T0_gt_placebo") is False
        or claims_oct.get("old_T0_gt_fresh") is False
    )
    if soft:
        verdict = "soft confirm"
    elif reject:
        verdict = "reject / park — old ≈ fresh ≈ placebo on train (or worse)"
    else:
        verdict = "mixed / park — claims incomplete or thin n; keep as dig only"

    L += ["", "## Registered claims (Oct)", ""]
    for k, v in claims_oct.items():
        L.append(f"- `{k}`: **{v}**")
    L += ["", "## OOS claim snapshot", ""]
    for x in oos_ok:
        L.append(f"- {x['month']}: old>fresh={x['old_gt_fresh']} old>placebo={x['old_gt_placebo']} "
                 f"R0>0={x['R0_gt_0']} R0>placebo={x['R0_gt_placebo']}")
    L += ["", f"## Decision", "", f"**{verdict}**", "", f"Artifacts: `{out}`.", ""]

    report = {"months": reports, "claims_oct": claims_oct, "oos": oos_ok, "verdict": verdict}
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[-8:]), flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="EXP-0045.yaml", help="Experiment yaml under research/experiments/")
    ap.add_argument("--month", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--dig", action="store_true")
    ap.add_argument("--standout-pct", type=float, default=None, help="Override standout percentile")
    args = ap.parse_args()
    cfg_path = ROOT / "research" / "experiments" / args.config
    exp = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if args.standout_pct is not None:
        exp["standout_pct"] = float(args.standout_pct)
    tag = _exp_tag(exp)

    if args.dig:
        run_dig(exp, ROOT)
        return

    month = args.month or exp.get("month_prefix", "2026-10")
    out = Path(args.out) if args.out else ROOT / "results" / f"{tag}{_month_tag(month)}"
    rep = run_month(exp, month, out, smoke=args.smoke)
    # auto-loosen on Oct if thin
    if month.startswith("2026-10") and not args.smoke and rep.get("n_touch", 0) < 40 and float(exp.get("standout_pct", 99)) >= 99:
        print(f"Oct touches={rep['n_touch']} < 40 — re-running with standout_pct=98", flush=True)
        exp["standout_pct"] = 98.0
        out98 = ROOT / "results" / tag
        rep = run_month(exp, month, out98, smoke=False)
        note = out98 / "P98_NOTE.txt"
        note.write_text(
            f"Loosened standout_pct 99→98 because Oct first-touches were <40 under p99.\n"
            f"Final Oct touches={rep.get('n_touch')}. Frozen for Sep/May. role={exp.get('role')}.\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
