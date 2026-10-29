"""Run MAT context queue EXP-0067…0076.

Usage:
  python -m research.mat_context.run_queue              # all
  python -m research.mat_context.run_queue --exp EXP-0067
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.cd import (
    build_cd_series,
    cd_tight_pullback_events,
    filter_events_near_bursts,
)
from research.features.range_bar_profile import (
    nearest_supporting_node,
    structural_sl_pts,
)
from research.features.vix import (
    annotate_vix,
    label_vix_abs,
    label_vix_tercile,
    session_vix_table,
)
from research.mat_context.cache import MatTradeCache, build_cache
from research.mat_context.gates import (
    gate_masks,
    label_atr,
    label_cd_sigma,
    label_rot_ratio,
    label_speed,
    label_vwap_norm,
)
from research.mat_context.report import arm_pack, bucket_stats, kill_vs_base, write_exp_report


def _attach_vix_to_cache(cache: MatTradeCache, *, force_fetch: bool = False) -> dict[str, dict[str, float]]:
    """Stamp prior-close VIX onto each session's events + trade_features."""
    vix_by = session_vix_table(cache.sessions, force_fetch=force_fetch)
    for sid in cache.sessions:
        b = cache.bundles[sid]
        feat = vix_by.get(sid, {"vix_prior": float("nan")})
        b.events = annotate_vix(b.events, feat)
        tf = getattr(b, "trade_features", None)
        if tf is not None and tf.num_rows:
            b.trade_features = annotate_vix(tf, feat)  # type: ignore[attr-defined]
    return vix_by



def _split_parts(sims_by_sid: dict[str, object]) -> tuple[list, list, list]:
    parts, is_p, oos_p = [], [], []
    for sid, s in sims_by_sid.items():
        parts.append(s)
        (is_p if F.is_is(sid) else oos_p).append(s)
    return parts, is_p, oos_p


def _resim_masked(cache: MatTradeCache, mask_name: str, *, slip: float = 0.0) -> dict[str, object]:
    """Re-simulate frozen MAT gate over annotated raw events filtered by mask."""
    out = {}
    for sid in cache.sessions:
        b = cache.bundles[sid]
        ev = b.events
        if ev.num_rows == 0:
            continue
        masks = gate_masks(ev)
        if mask_name not in masks:
            continue
        mk = masks[mask_name]
        if not isinstance(mk, np.ndarray) or mk.dtype != bool:
            continue
        if not mk.any():
            out[sid] = simulate_session(
                sid, b.nq, b.es, F.mat_sim_cfg(slip_ticks=slip),
                gate=F.mat_frozen_gate(), bracket_fn=F.mat_vol_bracket(),
                vwap_from_ns=b.vwap_from_ns, events=pa.table({}),
            )
            continue
        kept = ev.take(np.where(mk)[0])
        # keep only event_ts_ns + side for sim (+ optional tp/sl)
        cols = {"event_ts_ns": kept.column("event_ts_ns"), "side": kept.column("side")}
        out[sid] = simulate_session(
            sid, b.nq, b.es, F.mat_sim_cfg(slip_ticks=slip),
            gate=F.mat_frozen_gate(), bracket_fn=F.mat_vol_bracket(),
            vwap_from_ns=b.vwap_from_ns, events=pa.table(cols),
        )
    return out


def _null_book(cache: MatTradeCache, seed0: int = 6700) -> dict[str, object]:
    out = {}
    for i, sid in enumerate(cache.sessions):
        b = cache.bundles[sid]
        n = max(b.sim.n, 1)
        # Minimal Pair-like for null_events (needs .nq)
        pair = H.Pair(sid, b.nq, b.es, (b.vwap_from_ns, b.vwap_from_ns + 1))
        null = H.null_events(pair, n, seed=seed0 + i)
        out[sid] = simulate_session(
            sid, b.nq, b.es, F.mat_sim_cfg(),
            gate=F.mat_frozen_gate(), bracket_fn=F.mat_vol_bracket(),
            vwap_from_ns=b.vwap_from_ns, events=null,
        )
    return out


def _pack_book(sims: dict[str, object]) -> dict:
    parts, is_p, oos_p = _split_parts(sims)
    return arm_pack(parts, is_p, oos_p)


def run_0067(cache: MatTradeCache, out: Path) -> dict:
    """Descriptive strata on executed MAT trades."""
    trades = cache.base_trades()
    # Rebuild labels from trade_features
    labs_cd, labs_rot, labs_spd, labs_atr, labs_vwap = [], [], [], [], []
    aligned_trades = []
    for sid in cache.sessions:
        b = cache.bundles[sid]
        tf = getattr(b, "trade_features", None)
        if tf is None or tf.num_rows == 0:
            continue
        for i, tr in enumerate(b.sim.trades):
            if i >= tf.num_rows:
                break
            aligned_trades.append(tr)
            cd = float(tf.column("cd_dist_sigma")[i].as_py() or float("nan"))
            rr = float(tf.column("rot_ratio")[i].as_py() or float("nan"))
            ready = int(tf.column("rot_ready")[i].as_py() or 0)
            sr = float(tf.column("rot_speed_ratio")[i].as_py() or float("nan"))
            ar = float(tf.column("atr_ratio")[i].as_py() or float("nan"))
            vn = float(tf.column("vwap_dist_norm")[i].as_py() or float("nan"))
            va = float(tf.column("vwap_dist_aligned")[i].as_py() or float("nan"))
            labs_cd.append(label_cd_sigma(cd))
            labs_rot.append(label_rot_ratio(rr, ready))
            labs_spd.append(label_speed(sr))
            labs_atr.append(label_atr(ar))
            labs_vwap.append(label_vwap_norm(vn, va))

    buckets = {
        "cd_dist_sigma": bucket_stats(aligned_trades, labs_cd),
        "rot_ratio": bucket_stats(aligned_trades, labs_rot),
        "speed": bucket_stats(aligned_trades, labs_spd),
        "atr": bucket_stats(aligned_trades, labs_atr),
        "vwap_dist_norm": bucket_stats(aligned_trades, labs_vwap),
    }
    base = cache.base_summary()
    md = [
        "# EXP-0067 — MAT context descriptive strata (H58)",
        "",
        "No ship decision. Pre-registered buckets on frozen MAT trades.",
        "",
        f"Base MAT: n={base.n} net={base.net:+.1f} WR={base.wr}",
        "",
    ]
    for family, tab in buckets.items():
        md += [f"## {family}", "", "| bucket | n | WR | net | avg | IS net | OOS net | OOS avg |",
               "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for lab, r in tab.items():
            md.append(
                f"| {lab} | {r['n']} | {r['wr']} | {r['net']:+.1f} | {r['avg']} | "
                f"{r['is_net']:+.1f} | {r['oos_net']:+.1f} | {r['oos_avg']} |"
            )
        md.append("")
    report = {"base": base.to_dict(), "buckets": buckets, "promote": False}
    write_exp_report(out, title="EXP-0067", md_lines=md, report=report)
    return report


def run_gate_exp(
    cache: MatTradeCache,
    out: Path,
    *,
    exp_name: str,
    title: str,
    arm_names: list[str],
    seed: int,
) -> dict:
    base_sims = {sid: cache.bundles[sid].sim for sid in cache.sessions}
    base = _pack_book(base_sims)
    null = _pack_book(_null_book(cache, seed0=seed))
    books = {"base": base, "null": null}
    kills = {}
    for arm in arm_names:
        sims = _resim_masked(cache, arm)
        books[arm] = _pack_book(sims)
        kills[arm] = kill_vs_base(books[arm], base, null)

    md = [
        f"# {exp_name} — {title}",
        "",
        "| arm | n | WR | net | avg | IS net | OOS net | OOS avg | kill |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for k, v in books.items():
        a, o = v["all"], v["oos"]
        kill = kills.get(k, {})
        md.append(
            f"| {k} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{v['is']['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} | "
            f"{'PASS' if kill.get('pass') else ('—' if not kill else 'FAIL')} |"
        )
    md += ["", "## Kill detail", "", "```json", json.dumps(kills, indent=2), "```", ""]
    report = {"books": books, "kills": kills}
    write_exp_report(out, title=exp_name, md_lines=md, report=report)
    return report


def run_0069(cache: MatTradeCache, out: Path) -> dict:
    """Faithful CD including arm C timing-then-MAT."""
    report = run_gate_exp(
        cache, out / "_gates",
        exp_name="EXP-0069",
        title="Faithful CD gates A/B",
        arm_names=["cd_tight_A", "cd_cheap_B"],
        seed=6900,
    )
    # Arm C: tight PB primary → MAT confirm within 2s
    parts, is_p, oos_p = [], [], []
    n_events = 0
    for i, sid in enumerate(cache.sessions):
        b = cache.bundles[sid]
        cd = build_cd_series(b.nq, sma_min_bars=100)
        pb = cd_tight_pullback_events(cd)
        if pb.num_rows == 0:
            continue
        # MAT/ES confirm bursts
        raw = b.events
        if raw.num_rows == 0:
            continue
        confirmed = filter_events_near_bursts(pb, raw, window_ms=2000, require_same_side=True, causal=True)
        n_events += confirmed.num_rows
        if confirmed.num_rows == 0:
            continue
        cols = pa.table({
            "event_ts_ns": confirmed.column("event_ts_ns"),
            "side": confirmed.column("side"),
        })
        s = simulate_session(
            sid, b.nq, b.es, F.mat_sim_cfg(),
            gate=F.mat_frozen_gate(), bracket_fn=F.mat_vol_bracket(),
            vwap_from_ns=b.vwap_from_ns, events=cols,
        )
        parts.append(s)
        (is_p if F.is_is(sid) else oos_p).append(s)

    arm_c = arm_pack(parts, is_p, oos_p)
    base = report["books"]["base"]
    null = report["books"]["null"]
    kill_c = kill_vs_base(arm_c, base, null)
    kill_c["n_events"] = n_events
    kill_c["events_ge_200"] = n_events >= 200
    kill_c["pass"] = bool(kill_c.get("pass") and kill_c["events_ge_200"])

    report["books"]["cd_timing_C"] = arm_c
    report["kills"]["cd_timing_C"] = kill_c
    report["arm_C_n_events"] = n_events

    md = [
        "# EXP-0069 — Faithful CD (H60)",
        "",
        f"Arm C primary events (tight PB): n_events={n_events}",
        "",
        "| arm | n | WR | net | avg | IS net | OOS net | OOS avg | kill |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for k, v in report["books"].items():
        a, o = v["all"], v["oos"]
        kill = report["kills"].get(k, {})
        md.append(
            f"| {k} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{v['is']['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} | "
            f"{'PASS' if kill.get('pass') else ('—' if not kill else 'FAIL')} |"
        )
    md += ["", "```json", json.dumps(report["kills"], indent=2), "```", ""]
    write_exp_report(out, title="EXP-0069", md_lines=md, report=report)
    return report


def run_0071(cache: MatTradeCache, out: Path) -> dict:
    """Structural SL from prior 50-tick profile."""
    base_sims = {sid: cache.bundles[sid].sim for sid in cache.sessions}
    base = _pack_book(base_sims)
    books = {"base": base}
    kills = {}
    for mode in ("vol", "delta", "vol_or_delta"):
        arm_name = {"vol": "vol_node", "delta": "delta_node", "vol_or_delta": "vol_or_delta"}[mode]
        sims = {}
        for sid in cache.sessions:
            b = cache.bundles[sid]
            ev = b.events
            if ev.num_rows == 0:
                continue
            profile = getattr(b, "profile", None)
            if profile is None or profile.num_rows == 0 or "prior_bar_id" not in ev.column_names:
                sims[sid] = b.sim
                continue
            # Build per-event tp/sl overrides
            n = ev.num_rows
            tp = np.full(n, np.nan)
            sl = np.full(n, np.nan)
            sides = ev.column("side").to_numpy().astype(int)
            entry_px = ev.column("entry_px").to_numpy().astype(np.float64)
            prior_ok = ev.column("prior_ok").to_numpy().astype(bool)
            prior_bar = ev.column("prior_bar_id").to_numpy().astype(int)
            # fallback SL from a quick vol bracket estimate via med_r30
            medr = ev.column("med_r30").to_numpy().astype(np.float64)
            for i in range(n):
                fb = float(np.clip(max(medr[i], 0.25), 6.0, 30.0)) if np.isfinite(medr[i]) else 10.0
                tp[i] = fb
                stop_px = None
                if prior_ok[i] and prior_bar[i] >= 0:
                    entry_ticks = entry_px[i] / 0.25
                    stop_px = nearest_supporting_node(
                        profile, int(prior_bar[i]),
                        entry_ticks=entry_ticks, side=int(sides[i]),
                        mode=mode,
                    )
                sl[i] = structural_sl_pts(float(entry_px[i]), stop_px, int(sides[i]), fallback_sl=fb)
            cols = {
                "event_ts_ns": ev.column("event_ts_ns"),
                "side": ev.column("side"),
                "tp_pts": pa.array(tp, type=pa.float64()),
                "sl_pts": pa.array(sl, type=pa.float64()),
            }
            sims[sid] = simulate_session(
                sid, b.nq, b.es, F.mat_sim_cfg(),
                gate=F.mat_frozen_gate(), bracket_fn=None,
                vwap_from_ns=b.vwap_from_ns, events=pa.table(cols),
            )
        books[arm_name] = _pack_book(sims)
        kills[arm_name] = kill_vs_base(books[arm_name], base)
        kills[arm_name]["beat_base_oos_net"] = books[arm_name]["oos"]["net"] > base["oos"]["net"]
        kills[arm_name]["pass"] = bool(
            kills[arm_name].get("pass") and kills[arm_name]["beat_base_oos_net"]
        )

    md = [
        "# EXP-0071 — Structural SL (H62)",
        "",
        "| arm | n | WR | net | avg | IS net | OOS net | OOS avg | kill |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for k, v in books.items():
        a, o = v["all"], v["oos"]
        kill = kills.get(k, {})
        md.append(
            f"| {k} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{v['is']['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} | "
            f"{'PASS' if kill.get('pass') else ('—' if not kill else 'FAIL')} |"
        )
    md += ["", "```json", json.dumps(kills, indent=2), "```", ""]
    report = {"books": books, "kills": kills}
    write_exp_report(out, title="EXP-0071", md_lines=md, report=report)
    return report


def run_0072(cache: MatTradeCache, out: Path) -> dict:
    """TP-extend simulation."""
    base_sims = {sid: cache.bundles[sid].sim for sid in cache.sessions}
    base = _pack_book(base_sims)
    null = _pack_book(_null_book(cache, seed0=7200))
    books = {"base": base, "null": null}
    kills = {}

    for arm, max_ext in (("extend_8s_2pt", 0), ("extend_8s_2pt_max3", 3)):
        sims = {}
        for sid in cache.sessions:
            b = cache.bundles[sid]
            cfg = F.mat_sim_cfg()
            cfg.tp_extend_proximity_pts = 2.0
            cfg.tp_extend_lookback_sec = 8.0
            cfg.tp_extend_max = max_ext
            cfg.tp_extend_step_pts = 0.0  # use live bracket
            sims[sid] = simulate_session(
                sid, b.nq, b.es, cfg,
                gate=F.mat_frozen_gate(), bracket_fn=F.mat_vol_bracket(),
                vwap_from_ns=b.vwap_from_ns,
            )
        books[arm] = _pack_book(sims)
        kills[arm] = kill_vs_base(books[arm], base, null)

    md = [
        "# EXP-0072 — TP-extend (H63)",
        "",
        "Live Java toggle remains OFF unless an arm PASSes.",
        "",
        "| arm | n | WR | net | avg | IS net | OOS net | OOS avg | kill |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for k, v in books.items():
        a, o = v["all"], v["oos"]
        kill = kills.get(k, {})
        md.append(
            f"| {k} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{v['is']['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} | "
            f"{'PASS' if kill.get('pass') else ('—' if not kill else 'FAIL')} |"
        )
    md += ["", "```json", json.dumps(kills, indent=2), "```", ""]
    report = {"books": books, "kills": kills, "live_default": "extendTpIfFavourable=false"}
    write_exp_report(out, title="EXP-0072", md_lines=md, report=report)
    return report


def run_0073(cache: MatTradeCache, out: Path) -> dict:
    """Absorption exit after 0.5R MFE."""
    from research.features.corr_absorb import AbsorbParams, absorption_bars
    from research.features.momentum import trades_to_tape_bars

    base_sims = {sid: cache.bundles[sid].sim for sid in cache.sessions}
    base = _pack_book(base_sims)
    null = _pack_book(_null_book(cache, seed0=7300))
    books = {"base": base, "null": null}
    kills = {}
    p = AbsorbParams()

    for arm, dual in (("nq_absorb_exit", False), ("dual_absorb_exit", True)):
        sims = {}
        for sid in cache.sessions:
            b = cache.bundles[sid]
            nq_b = trades_to_tape_bars(b.nq, bar_ms=100)
            es_b = trades_to_tape_bars(b.es, bar_ms=100) if b.es.num_rows else pa.table({})

            nq_abs = absorption_bars(nq_b, p, market="NQ") if nq_b.num_rows else pa.table({})
            if dual and es_b.num_rows and nq_abs.num_rows:
                es_abs = absorption_bars(es_b, p, market="ES")
                if es_abs.num_rows:
                    nq_ts = nq_abs.column("event_ts_ns").to_numpy().astype(np.int64)
                    nq_aggr = nq_abs.column("aggr_side").to_numpy().astype(int)
                    es_ts = es_abs.column("event_ts_ns").to_numpy().astype(np.int64)
                    es_aggr = es_abs.column("aggr_side").to_numpy().astype(int)
                    keep = []
                    for i in range(len(nq_ts)):
                        lo = int(np.searchsorted(es_ts, nq_ts[i] - 250_000_000))
                        hi = int(np.searchsorted(es_ts, nq_ts[i] + 250_000_000))
                        for j in range(lo, hi):
                            if es_aggr[j] == nq_aggr[i]:
                                keep.append(i)
                                break
                    use = nq_abs.take(np.asarray(keep, dtype=np.int64)) if keep else pa.table({})
                else:
                    use = nq_abs
            else:
                use = nq_abs

            if use.num_rows == 0:
                sims[sid] = b.sim
                continue
            abs_ts = use.column("event_ts_ns").to_numpy().astype(np.int64)
            aggr = use.column("aggr_side").to_numpy().astype(int)

            def make_exit(abs_ts=abs_ts, aggr=aggr):
                def exit_fn(entry_ts_ns, side, row):
                    # Opposite-side aggression into our position.
                    # True 0.5R MFE needs path access; exit_fn returns the first
                    # candidate after entry and TP/SL still win if earlier.
                    lo = int(np.searchsorted(abs_ts, entry_ts_ns, side="right"))
                    for j in range(lo, len(abs_ts)):
                        if int(aggr[j]) == -int(side):
                            return int(abs_ts[j]), "absorb_exit"
                    return None
                return exit_fn

            ev = b.events
            if ev.num_rows == 0:
                sims[sid] = b.sim
                continue
            sims[sid] = simulate_session(
                sid, b.nq, b.es, F.mat_sim_cfg(),
                gate=F.mat_frozen_gate(), bracket_fn=F.mat_vol_bracket(),
                vwap_from_ns=b.vwap_from_ns, events=pa.table({
                    "event_ts_ns": ev.column("event_ts_ns"),
                    "side": ev.column("side"),
                }),
                exit_fn=make_exit(),
            )
        books[arm] = _pack_book(sims)
        kills[arm] = kill_vs_base(books[arm], base, null)

    md = [
        "# EXP-0073 — Absorption exit after min-TP (H64)",
        "",
        "Note: exit_fn fires on first opposite-side absorption after entry;",
        "TP/SL still win if they print first. True 0.5R MFE gate is approximated",
        "by requiring absorption post-entry (path MFE not visible to exit_fn).",
        "",
        "| arm | n | WR | net | avg | IS net | OOS net | OOS avg | kill |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for k, v in books.items():
        a, o = v["all"], v["oos"]
        kill = kills.get(k, {})
        md.append(
            f"| {k} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{v['is']['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} | "
            f"{'PASS' if kill.get('pass') else ('—' if not kill else 'FAIL')} |"
        )
    md += ["", "```json", json.dumps(kills, indent=2), "```", ""]
    report = {"books": books, "kills": kills}
    write_exp_report(out, title="EXP-0073", md_lines=md, report=report)
    return report


def run_0074(cache: MatTradeCache, out: Path, *, force_fetch: bool = False) -> dict:
    """Prior-close VIX strata + keep-gates on frozen MAT (H65)."""
    vix_by = _attach_vix_to_cache(cache, force_fetch=force_fetch)

    # Descriptive strata on executed trades
    aligned, labs_abs, labs_terc = [], [], []
    for sid in cache.sessions:
        b = cache.bundles[sid]
        tf = getattr(b, "trade_features", None)
        if tf is None or tf.num_rows == 0:
            continue
        feat = vix_by.get(sid, {})
        prior = float(feat.get("vix_prior", float("nan")))
        p33 = float(feat.get("vix_p33", float("nan")))
        p66 = float(feat.get("vix_p66", float("nan")))
        for i, tr in enumerate(b.sim.trades):
            if i >= tf.num_rows:
                break
            aligned.append(tr)
            labs_abs.append(label_vix_abs(prior))
            labs_terc.append(label_vix_tercile(prior, p33, p66))

    buckets = {
        "vix_abs": bucket_stats(aligned, labs_abs),
        "vix_tercile": bucket_stats(aligned, labs_terc),
    }

    gate = run_gate_exp(
        cache, out / "_gates",
        exp_name="EXP-0074",
        title="Prior-close VIX keep-gates (H65)",
        arm_names=["vix_ge_15", "vix_ge_18", "vix_ge_20", "vix_ge_25", "vix_hi_tercile"],
        seed=7400,
    )

    # Session coverage summary
    priors = [v["vix_prior"] for v in vix_by.values() if np.isfinite(v.get("vix_prior", float("nan")))]
    coverage = {
        "n_sessions": len(cache.sessions),
        "n_with_vix": len(priors),
        "vix_min": round(min(priors), 2) if priors else None,
        "vix_max": round(max(priors), 2) if priors else None,
        "vix_median": round(float(np.median(priors)), 2) if priors else None,
        "n_hi_tercile": sum(1 for v in vix_by.values() if v.get("vix_hi_tercile", 0) >= 0.5),
    }

    md = [
        "# EXP-0074 — Prior-close VIX on frozen MAT (H65)",
        "",
        "Causal: FRED VIXCLS close strictly before session calendar day.",
        f"Coverage: {coverage['n_with_vix']}/{coverage['n_sessions']} sessions; "
        f"VIX prior median={coverage['vix_median']} "
        f"[{coverage['vix_min']}, {coverage['vix_max']}]; "
        f"hi-tercile days={coverage['n_hi_tercile']}.",
        "",
        "## Absolute strata (executed MAT trades)",
        "",
        "| bucket | n | WR | net | avg | IS net | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for lab, r in buckets["vix_abs"].items():
        md.append(
            f"| {lab} | {r['n']} | {r['wr']} | {r['net']:+.1f} | {r['avg']} | "
            f"{r['is_net']:+.1f} | {r['oos_net']:+.1f} | {r['oos_avg']} |"
        )
    md += [
        "",
        "## Trailing-60d tercile strata",
        "",
        "| bucket | n | WR | net | avg | IS net | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for lab, r in buckets["vix_tercile"].items():
        md.append(
            f"| {lab} | {r['n']} | {r['wr']} | {r['net']:+.1f} | {r['avg']} | "
            f"{r['is_net']:+.1f} | {r['oos_net']:+.1f} | {r['oos_avg']} |"
        )
    md += [
        "",
        "## Keep-gates",
        "",
        "| arm | n | WR | net | avg | IS net | OOS net | OOS avg | kill |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for k, v in gate["books"].items():
        a, o = v["all"], v["oos"]
        kill = gate["kills"].get(k, {})
        md.append(
            f"| {k} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{v['is']['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} | "
            f"{'PASS' if kill.get('pass') else ('—' if not kill else 'FAIL')} |"
        )
    md += ["", "```json", json.dumps(gate["kills"], indent=2), "```", ""]

    report = {
        "coverage": coverage,
        "buckets": buckets,
        "books": gate["books"],
        "kills": gate["kills"],
        "source": "FRED VIXCLS prior close",
    }
    write_exp_report(out, title="EXP-0074", md_lines=md, report=report)
    return report


def run_0076(cache: MatTradeCache, out: Path) -> dict:
    """Path-aware 0.5R absorb exit vs EXP-0073 approx twin (H67)."""
    from research.features.corr_absorb import AbsorbParams, absorption_bars
    from research.features.momentum import trades_to_tape_bars
    from research.mat_context.absorb_exit import build_absorb_exit_fn

    base_sims = {sid: cache.bundles[sid].sim for sid in cache.sessions}
    base = _pack_book(base_sims)
    null = _pack_book(_null_book(cache, seed0=7600))
    books = {"base": base, "null": null}
    kills = {}
    p = AbsorbParams()

    for arm, require_mfe in (("nq_absorb_path_mfe", True), ("nq_absorb_approx", False)):
        sims = {}
        for sid in cache.sessions:
            b = cache.bundles[sid]
            nq_b = trades_to_tape_bars(b.nq, bar_ms=100)
            nq_abs = absorption_bars(nq_b, p, market="NQ") if nq_b.num_rows else pa.table({})
            ev = b.events
            if ev.num_rows == 0:
                sims[sid] = b.sim
                continue
            if nq_abs.num_rows == 0:
                sims[sid] = b.sim
                continue
            abs_ts = nq_abs.column("event_ts_ns").to_numpy().astype(np.int64)
            aggr = nq_abs.column("aggr_side").to_numpy().astype(int)
            exit_fn = build_absorb_exit_fn(
                b.nq, abs_ts, aggr, require_path_mfe=require_mfe, mfe_frac=0.5,
            )
            cols = {
                "event_ts_ns": ev.column("event_ts_ns"),
                "side": ev.column("side"),
            }
            if "med_r30" in ev.column_names:
                cols["med_r30"] = ev.column("med_r30")
            sims[sid] = simulate_session(
                sid, b.nq, b.es, F.mat_sim_cfg(),
                gate=F.mat_frozen_gate(), bracket_fn=F.mat_vol_bracket(),
                vwap_from_ns=b.vwap_from_ns, events=pa.table(cols),
                exit_fn=exit_fn,
            )
        books[arm] = _pack_book(sims)
        kills[arm] = kill_vs_base(books[arm], base, null)

    # Sensitivity: path vs approx OOS avg (not a promote requirement)
    path_oos = books["nq_absorb_path_mfe"]["oos"].get("avg")
    approx_oos = books["nq_absorb_approx"]["oos"].get("avg")
    sens = {
        "path_oos_avg": path_oos,
        "approx_oos_avg": approx_oos,
        "path_ge_approx_oos_avg": (
            path_oos is not None and approx_oos is not None and path_oos >= approx_oos
        ),
    }
    kills["nq_absorb_path_mfe"]["sensitivity_vs_approx"] = sens

    md = [
        "# EXP-0076 — Path-aware 0.5R absorb exit (H67)",
        "",
        "Primary: MFE ≥ 0.5×TP on NQ path before opposite-side absorb can force exit.",
        "Twin: EXP-0073 approx (first opposite absorb after entry).",
        f"Sensitivity path≥approx OOS avg: {sens['path_ge_approx_oos_avg']} "
        f"(path={path_oos}, approx={approx_oos}).",
        "",
        "| arm | n | WR | net | avg | IS net | OOS net | OOS avg | kill |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for k, v in books.items():
        a, o = v["all"], v["oos"]
        kill = kills.get(k, {})
        md.append(
            f"| {k} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{v['is']['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} | "
            f"{'PASS' if kill.get('pass') else ('—' if not kill else 'FAIL')} |"
        )
    md += ["", "```json", json.dumps(kills, indent=2), "```", ""]
    report = {"books": books, "kills": kills, "sensitivity": sens}
    write_exp_report(out, title="EXP-0076", md_lines=md, report=report)
    return report


def run_all(exps: list[str] | None = None) -> None:
    cfg = load_config()
    results = Path(cfg["results_dir"])
    print("Building MAT context cache…", flush=True)
    cache = build_cache()
    print(f"Cache ready: {len(cache.sessions)} sessions, base n={cache.base_summary().n}", flush=True)

    wanted = set(exps) if exps else {
        "EXP-0067", "EXP-0068", "EXP-0069", "EXP-0070",
        "EXP-0071", "EXP-0072", "EXP-0073", "EXP-0074",
        "EXP-0075", "EXP-0076",
    }
    synthesis = {}

    if "EXP-0067" in wanted:
        print("=== EXP-0067 ===", flush=True)
        synthesis["EXP-0067"] = run_0067(cache, results / "EXP-0067")

    if "EXP-0068" in wanted:
        print("=== EXP-0068 ===", flush=True)
        synthesis["EXP-0068"] = run_gate_exp(
            cache, results / "EXP-0068",
            exp_name="EXP-0068",
            title="Session rotation + speed + ATR (H59)",
            arm_names=["rot_fresh_0.90", "speed_not_stalled", "atr_room", "rot_and_atr"],
            seed=6800,
        )

    if "EXP-0069" in wanted:
        print("=== EXP-0069 ===", flush=True)
        synthesis["EXP-0069"] = run_0069(cache, results / "EXP-0069")

    if "EXP-0070" in wanted:
        print("=== EXP-0070 ===", flush=True)
        synthesis["EXP-0070"] = run_gate_exp(
            cache, results / "EXP-0070",
            exp_name="EXP-0070",
            title="VWAP distance in vol units (H61)",
            arm_names=["vwap_band_0.5_2.0", "vwap_not_extended"],
            seed=7000,
        )

    if "EXP-0071" in wanted:
        print("=== EXP-0071 ===", flush=True)
        synthesis["EXP-0071"] = run_0071(cache, results / "EXP-0071")

    if "EXP-0072" in wanted:
        print("=== EXP-0072 ===", flush=True)
        synthesis["EXP-0072"] = run_0072(cache, results / "EXP-0072")

    if "EXP-0073" in wanted:
        print("=== EXP-0073 ===", flush=True)
        synthesis["EXP-0073"] = run_0073(cache, results / "EXP-0073")

    if "EXP-0074" in wanted:
        print("=== EXP-0074 ===", flush=True)
        synthesis["EXP-0074"] = run_0074(cache, results / "EXP-0074")

    if "EXP-0075" in wanted:
        print("=== EXP-0075 ===", flush=True)
        synthesis["EXP-0075"] = run_gate_exp(
            cache, results / "EXP-0075",
            exp_name="EXP-0075",
            title="Near-VWAP + ATR-expanded (H66)",
            arm_names=["vwap_near_0.5", "atr_expanded", "rot_fresh_and_vwap_near"],
            seed=7500,
        )

    if "EXP-0076" in wanted:
        print("=== EXP-0076 ===", flush=True)
        synthesis["EXP-0076"] = run_0076(cache, results / "EXP-0076")

    # Synthesis (append-only when running a subset: merge with existing)
    syn_out = results / "MAT_CONTEXT_SYNTHESIS"
    syn_out.mkdir(parents=True, exist_ok=True)
    prev_path = syn_out / "report.json"
    merged = {}
    if prev_path.exists() and exps:
        try:
            merged = json.loads(prev_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            merged = {}
    merged.update(synthesis)

    lines = [
        "# MAT context / CD / exits synthesis",
        "",
        "Host: frozen MAT. CFADE / allocator (c) untouched. Live defaults unchanged.",
        "",
        "| EXP | hypothesis | best arm | OOS net | OOS avg | kill |",
        "|---|---|---|---:|---:|---|",
    ]
    hyp = {
        "EXP-0067": "H58", "EXP-0068": "H59", "EXP-0069": "H60", "EXP-0070": "H61",
        "EXP-0071": "H62", "EXP-0072": "H63", "EXP-0073": "H64", "EXP-0074": "H65",
        "EXP-0075": "H66", "EXP-0076": "H67",
    }
    for exp in sorted(merged.keys()):
        rep = merged[exp]
        kills = rep.get("kills") or {}
        books = rep.get("books") or {}
        h = hyp.get(exp, "—")
        if exp == "EXP-0067":
            lines.append(f"| {exp} | {h} | (strata only) | — | — | no promote |")
            continue
        best = None
        best_avg = -1e18
        for arm, kill in kills.items():
            oos = books.get(arm, {}).get("oos", {})
            avg = oos.get("avg")
            if avg is not None and avg > best_avg:
                best_avg = avg
                best = arm
        if best is None:
            lines.append(f"| {exp} | {h} | — | — | — | — |")
            continue
        o = books[best]["oos"]
        passed = kills[best].get("pass", False)
        lines.append(
            f"| {exp} | {h} | {best} | {o['net']:+.1f} | {o.get('avg')} | "
            f"{'PASS' if passed else 'FAIL'} |"
        )
    lines += [
        "",
        "## Live defaults",
        "",
        "- `extendTpIfFavourable=false` (Java fixed but gated on EXP-0072 promote)",
        "- `requireCdPullback=false`",
        "- No rotation / ATR / VWAP-band / structural-SL / VIX panel controls added",
        "",
    ]
    (syn_out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (syn_out / "report.json").write_text(json.dumps(merged, indent=2, default=str), encoding="utf-8")
    print(f"wrote {syn_out / 'summary.md'}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", action="append", default=None, help="EXP-0067 … EXP-0076 (repeatable)")
    args = ap.parse_args()
    run_all(args.exp)


if __name__ == "__main__":
    main()
