"""EXP-0056 / H47 — MAT signals by 50-tick range-bar type.

Stages:
  validate  — Stage 0 synthetic + real-session invariants
  discover  — Stage 1 stratification + Stage 2 arms on Apr–Aug 2026
  holdout   — Stage 3 locked Sep–Oct once a frozen arm is recorded

Usage:
  python -m research.run_mat_range_bars --stage validate
  python -m research.run_mat_range_bars --stage discover
  python -m research.run_mat_range_bars --stage holdout
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.mat_sim import (
    MatSimConfig,
    combine_gates,
    detect_signals,
    make_trend_mode,
    make_vol_bracket,
    make_vol_bracket_rr,
    make_vwap_side,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.correlated.harness import _prefer_single_feed
from research.features.mat_fail_fade import filter_gated_mat
from research.features.range_bar_profile import (
    RangeBarProfileConfig,
    attach_mat_range_context,
    filter_arm_mask,
    validate_session_invariants,
    validate_synthetic,
)
from research.run_experiment import _load_trades
from research.run_mat_may import day_equity_stats

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25
DISCOVERY_MMYY = {"0426", "0526", "0626", "0726", "0826"}
HOLDOUT_MMYY = {"0926", "1026"}
ARMS = ("base", "vol", "delta", "vol_delta", "va_break", "vol_va_break", "va_inside")
PRIMARY_CTX = "prior"
SECONDARY_CTX = "partial"


def sid_month(sid: str) -> str:
    return sid[2:6] if len(sid) >= 6 else ""


def cohort(sid: str) -> str:
    m = sid_month(sid)
    if m in DISCOVERY_MMYY:
        return "discovery"
    if m in HOLDOUT_MMYY:
        return "holdout"
    return "other"


def bounds(mid: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(mid / 1e9, tz=timezone.utc).astimezone(UK).date()
    return (
        int(datetime.combine(d, time(14, 30), tzinfo=UK).timestamp() * 1e9),
        int(datetime.combine(d, time(17, 0), tzinfo=UK).timestamp() * 1e9),
    )


def filt(t: pa.Table) -> pa.Table:
    if t.num_rows == 0:
        return t
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    s, e = bounds(int(ts[len(ts) // 2]))
    k = (ts >= s) & (ts <= e)
    return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)


def list_sessions(pq_dir: Path) -> list[str]:
    return sorted(
        p.name.replace("session_id=", "")
        for p in pq_dir.iterdir()
        if p.is_dir()
        and p.name.startswith("session_id=")
        and (p / "trades.parquet").exists()
        and "smoke" not in p.name
    )


def summary_dict(parts: list, commission: float = COMMISSION) -> dict:
    if not parts:
        return {"n": 0, "net": 0.0, "avg": None, "wr": None, "pf": None, "days": 0, "days_pos": 0}
    m = merge_summaries(parts, commission)
    d = m.to_dict()
    d["avg"] = round(m.net / m.n, 3) if m.n else None
    days = days_pos = 0
    for p in parts:
        if p.n == 0:
            continue
        days += 1
        eq = day_equity_stats(p.trades, commission)
        if eq["equity_end"] > 0:
            days_pos += 1
    d["days"] = days
    d["days_pos"] = days_pos
    return d


def discovery_pass(arm_sum: dict, base_avg: float | None) -> tuple[bool, str]:
    """Plan verdict: n≥50, avg ≥ base+0.5, ≥3/5 months net+, no month >60% net."""
    if arm_sum.get("n", 0) < 50:
        return False, f"n={arm_sum.get('n')} < 50"
    avg = arm_sum.get("avg")
    if avg is None:
        return False, "no avg"
    if base_avg is None:
        return False, "no base avg"
    if avg < base_avg + 0.5:
        return False, f"avg {avg} < base {base_avg}+0.5"
    by_m = arm_sum.get("by_month") or {}
    pos_months = [m for m, s in by_m.items() if (s.get("net") or 0) > 0]
    if len(pos_months) < 3:
        return False, f"only {len(pos_months)}/5 months net+"
    total_net = arm_sum.get("net") or 0.0
    if total_net > 0:
        for m, s in by_m.items():
            mn = s.get("net") or 0.0
            if mn > 0 and mn / total_net > 0.60:
                return False, f"month {m} contributes {mn/total_net:.0%} of net"
    return True, "pass"


def build_mat_gate(*, trend: bool = True, vwap: bool = True):
    """MAT entry gate. Both off → ungated (all causal MAT stamps kept)."""
    gates = []
    if trend:
        gates.append(make_trend_mode("with"))
    if vwap:
        gates.append(make_vwap_side(True))
    if not gates:
        return None
    return combine_gates(*gates) if len(gates) > 1 else gates[0]


def make_bracket(*, tp_r: float = 1.0, sl_r: float = 1.0, k: float = 1.0):
    """Vol-scaled brackets: unit = k × median 1-min range; TP=tp_r×unit, SL=sl_r×unit.

    ``tp_r=1, sl_r=1`` matches frozen symmetric vol k=1 (clip 6–30 via rr helper
    floors/caps differ slightly — use make_vol_bracket when 1:1 for exact parity).
    """
    if abs(tp_r - 1.0) < 1e-9 and abs(sl_r - 1.0) < 1e-9:
        return make_vol_bracket(k)
    return make_vol_bracket_rr(k, tp_r=tp_r, sl_r=sl_r)


def run_tag(*, tp_r: float, sl_r: float, trend: bool, vwap: bool) -> str:
    """Results-dir suffix so configs don't overwrite each other."""
    parts = []
    if not (abs(tp_r - 1.0) < 1e-9 and abs(sl_r - 1.0) < 1e-9):
        parts.append(f"RR{sl_r:g}_{tp_r:g}")
    gates = []
    if not trend:
        gates.append("notrend")
    if not vwap:
        gates.append("novwap")
    if gates:
        parts.append("_".join(gates))
    return ("_" + "_".join(parts)) if parts else ""


def run_validate(
    cfg: dict,
    out: Path,
    *,
    trend: bool = True,
    vwap: bool = True,
) -> dict:
    errs = validate_synthetic()
    report = {"synthetic_errors": errs, "synthetic_ok": not errs, "sessions": []}
    print(f"Stage0 synthetic: {'PASS' if not errs else 'FAIL'} {errs}", flush=True)

    pq_dir = Path(cfg["parquet_dir"])
    # Spot-check a few discovery sessions
    samples = [s for s in list_sessions(pq_dir) if cohort(s) == "discovery"][:3]
    mat_gate = build_mat_gate(trend=trend, vwap=vwap)
    base_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    rb_cfg = RangeBarProfileConfig()

    all_ok = not errs
    for sid in samples:
        nq = filt(_prefer_single_feed(_load_trades(pq_dir, sid, "NQ"), pq_dir, sid, "NQ"))
        es0 = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows == 0:
            continue
        mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
        vf = bounds(mid)[0]
        raw = detect_signals(nq, es, base_cfg)
        gated = (
            filter_gated_mat(nq, raw, mat_gate, vwap_from_ns=vf)
            if mat_gate is not None
            else raw
        )
        ev, bars, profile = attach_mat_range_context(nq, gated, cfg=rb_cfg)
        e = validate_session_invariants(nq, ev, bars, profile)
        report["sessions"].append({
            "session": sid,
            "bars": bars.num_rows,
            "mat_gated": gated.num_rows,
            "errors": e,
        })
        print(
            f"  {sid}: bars={bars.num_rows} mat={gated.num_rows} "
            f"{'PASS' if not e else 'FAIL '+str(e)}",
            flush=True,
        )
        if e:
            all_ok = False

    report["ok"] = all_ok
    (out / "stage0_validate.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _load_discovery_sessions(
    cfg: dict,
    months: set[str],
    *,
    trend: bool = True,
    vwap: bool = True,
) -> dict[str, dict]:
    pq_dir = Path(cfg["parquet_dir"])
    mat_gate = build_mat_gate(trend=trend, vwap=vwap)
    base_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    rb_cfg = RangeBarProfileConfig()
    gate_lbl = (
        "trend+VWAP" if trend and vwap
        else "trend" if trend
        else "VWAP" if vwap
        else "none"
    )
    data: dict[str, dict] = {}
    for sid in list_sessions(pq_dir):
        if sid_month(sid) not in months:
            continue
        nq = filt(_prefer_single_feed(_load_trades(pq_dir, sid, "NQ"), pq_dir, sid, "NQ"))
        es0 = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows == 0:
            continue
        mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
        vf = bounds(mid)[0]
        raw = detect_signals(nq, es, base_cfg)
        gated = (
            filter_gated_mat(nq, raw, mat_gate, vwap_from_ns=vf)
            if mat_gate is not None
            else raw
        )
        ev, bars, profile = attach_mat_range_context(nq, gated, cfg=rb_cfg)
        data[sid] = {
            "nq": nq, "es": es, "vf": vf, "events": ev, "bars": bars, "profile": profile,
            "month": sid_month(sid), "cohort": cohort(sid),
        }
        print(
            f"load {sid} ({cohort(sid)}/{sid_month(sid)} gate={gate_lbl}): "
            f"nq={nq.num_rows} mat_raw={raw.num_rows} mat={gated.num_rows} bars={bars.num_rows}",
            flush=True,
        )
    return data


def run_arm(
    data: dict[str, dict],
    arm: str,
    context: str,
    *,
    tp_r: float = 1.0,
    sl_r: float = 1.0,
) -> dict:
    sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    br = make_bracket(tp_r=tp_r, sl_r=sl_r)
    parts_all: list = []
    parts_month: dict[str, list] = defaultdict(list)
    n_events = 0
    for sid, sd in data.items():
        ev = sd["events"]
        mask = filter_arm_mask(ev, arm, context=context)  # type: ignore[arg-type]
        n_events += int(mask.sum())
        if not mask.any():
            continue
        filtered = ev.take(np.where(mask)[0])
        # simulate_session only needs event_ts_ns + side
        s = simulate_session(
            sid, sd["nq"], sd["es"], sim_cfg,
            gate=None, bracket_fn=br, vwap_from_ns=sd["vf"], events=filtered,
        )
        parts_all.append(s)
        parts_month[sd["month"]].append(s)
    out = summary_dict(parts_all)
    out["n_events"] = n_events
    out["by_month"] = {m: summary_dict(ps) for m, ps in sorted(parts_month.items())}
    return out


def stage1_strata(
    data: dict[str, dict],
    *,
    tp_r: float = 1.0,
    sl_r: float = 1.0,
) -> dict:
    """Descriptive join of base MAT trades to prior-bar context bins."""
    sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    br = make_bracket(tp_r=tp_r, sl_r=sl_r)
    # Collect per-trade context by simulating base and matching entry_ts
    rows = []
    for sid, sd in data.items():
        ev = sd["events"]
        if ev.num_rows == 0:
            continue
        s = simulate_session(
            sid, sd["nq"], sd["es"], sim_cfg,
            gate=None, bracket_fn=br, vwap_from_ns=sd["vf"], events=ev,
        )
        # Map event_ts -> context
        ev_ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        for tr in s.trades:
            # find nearest event at/before entry
            j = int(np.searchsorted(ev_ts, tr.entry_ts_ns, side="right")) - 1
            if j < 0:
                continue
            rows.append({
                "session": sid,
                "month": sd["month"],
                "side": tr.side,
                "pnl_net": tr.pnl - COMMISSION,
                "win": tr.win,
                "vol_ratio": ev.column("prior_vol_ratio")[j].as_py(),
                "adf": ev.column("prior_aligned_delta_frac")[j].as_py(),
                "pos": ev.column("prior_pos")[j].as_py(),
                "prior_ok": ev.column("prior_ok")[j].as_py(),
                "bar_dir": ev.column("prior_direction")[j].as_py(),
            })

    def agg(subset: list[dict]) -> dict:
        if not subset:
            return {"n": 0, "avg": None, "net": 0.0, "wr": None}
        pnls = [r["pnl_net"] for r in subset]
        wins = sum(1 for r in subset if r["win"])
        return {
            "n": len(subset),
            "avg": round(float(np.mean(pnls)), 3),
            "net": round(float(np.sum(pnls)), 2),
            "wr": round(wins / len(subset), 4),
        }

    # volume terciles among prior_ok with finite ratio
    ok = [r for r in rows if r["prior_ok"] and r["vol_ratio"] is not None and np.isfinite(r["vol_ratio"])]
    strata: dict = {"all": agg(rows), "prior_ok": agg([r for r in rows if r["prior_ok"]])}
    if len(ok) >= 9:
        ratios = np.array([r["vol_ratio"] for r in ok], dtype=np.float64)
        q33, q66 = np.quantile(ratios, [1 / 3, 2 / 3])
        strata["vol_tercile"] = {
            "low": agg([r for r in ok if r["vol_ratio"] <= q33]),
            "mid": agg([r for r in ok if q33 < r["vol_ratio"] <= q66]),
            "high": agg([r for r in ok if r["vol_ratio"] > q66]),
            "cuts": [round(float(q33), 3), round(float(q66), 3)],
        }
    # aligned delta bins
    strata["adf_bins"] = {
        "lt0": agg([r for r in rows if r["prior_ok"] and r["adf"] is not None and r["adf"] < 0]),
        "0_0.2": agg([r for r in rows if r["prior_ok"] and r["adf"] is not None and 0 <= r["adf"] < 0.2]),
        "ge0.2": agg([r for r in rows if r["prior_ok"] and r["adf"] is not None and r["adf"] >= 0.2]),
    }
    # position
    positions = sorted({r["pos"] for r in rows if r["prior_ok"]})
    strata["position"] = {p: agg([r for r in rows if r["pos"] == p]) for p in positions}
    # side
    strata["side"] = {
        "long": agg([r for r in rows if r["side"] > 0]),
        "short": agg([r for r in rows if r["side"] < 0]),
    }
    # bar direction vs MAT
    strata["bar_dir_align"] = {
        "with": agg([r for r in rows if r["prior_ok"] and r["bar_dir"] == r["side"] and r["bar_dir"] != 0]),
        "against": agg([r for r in rows if r["prior_ok"] and r["bar_dir"] == -r["side"] and r["bar_dir"] != 0]),
        "flat": agg([r for r in rows if r["prior_ok"] and r["bar_dir"] == 0]),
    }
    return strata


def choose_freeze(arm_results: dict, base_avg: float | None) -> dict | None:
    """Pick best primary-context arm by avg lift among those that pass."""
    candidates = []
    for arm, res in arm_results.items():
        if arm in ("base", "va_inside"):
            continue
        ok, reason = discovery_pass(res, base_avg)
        if ok:
            lift = (res.get("avg") or 0) - (base_avg or 0)
            candidates.append((lift, arm, reason))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    lift, arm, reason = candidates[0]
    return {
        "arm": arm,
        "context": PRIMARY_CTX,
        "lift": round(lift, 3),
        "reason": reason,
    }


def run_discover(
    cfg: dict,
    out: Path,
    *,
    tp_r: float = 1.0,
    sl_r: float = 1.0,
    trend: bool = True,
    vwap: bool = True,
) -> dict:
    data = _load_discovery_sessions(cfg, DISCOVERY_MMYY, trend=trend, vwap=vwap)
    # Persist combined events for inspection
    if data:
        ev_parts = []
        for sid, sd in data.items():
            ev = sd["events"]
            if ev.num_rows == 0:
                continue
            n = ev.num_rows
            ev2 = ev.append_column("session_id", pa.array([sid] * n, type=pa.string()))
            ev_parts.append(ev2)
        if ev_parts:
            pq.write_table(pa.concat_tables(ev_parts, promote_options="default"), out / "events.parquet")

    strata = stage1_strata(data, tp_r=tp_r, sl_r=sl_r)
    print("Stage1 strata done", flush=True)

    arms_prior: dict[str, dict] = {}
    arms_partial: dict[str, dict] = {}
    for arm in ARMS:
        arms_prior[arm] = run_arm(data, arm, PRIMARY_CTX, tp_r=tp_r, sl_r=sl_r)
        print(
            f"prior/{arm}: n={arms_prior[arm]['n']} avg={arms_prior[arm]['avg']} "
            f"net={arms_prior[arm]['net']}",
            flush=True,
        )
    for arm in ARMS:
        arms_partial[arm] = run_arm(data, arm, SECONDARY_CTX, tp_r=tp_r, sl_r=sl_r)
        print(
            f"partial/{arm}: n={arms_partial[arm]['n']} avg={arms_partial[arm]['avg']} "
            f"net={arms_partial[arm]['net']}",
            flush=True,
        )

    base_avg = arms_prior["base"].get("avg")
    freeze = choose_freeze(arms_prior, base_avg)
    pass_flags = {
        arm: discovery_pass(arms_prior[arm], base_avg)
        for arm in ARMS if arm != "base"
    }
    gate_lbl = (
        "trend+VWAP" if trend and vwap
        else "trend" if trend
        else "VWAP" if vwap
        else "none"
    )
    br_lbl = f"tp_r={tp_r:g} sl_r={sl_r:g} (R:R {sl_r:g}:{tp_r:g})"

    report = {
        "exp": "EXP-0056",
        "hypothesis": "H47",
        "stage": "discover",
        "tp_r": tp_r,
        "sl_r": sl_r,
        "mat_trend": trend,
        "mat_vwap": vwap,
        "discovery_months": sorted(DISCOVERY_MMYY),
        "n_sessions": len(data),
        "stage1_strata": strata,
        "arms_prior": arms_prior,
        "arms_partial": arms_partial,
        "pass_flags_prior": {a: {"ok": ok, "reason": r} for a, (ok, r) in pass_flags.items()},
        "frozen": freeze,
        "verdict": (
            f"freeze {freeze['arm']} (lift={freeze['lift']})"
            if freeze else "reject / park — no primary arm passes discovery"
        ),
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if freeze:
        freeze = {
            **freeze,
            "tp_r": tp_r,
            "sl_r": sl_r,
            "mat_trend": trend,
            "mat_vwap": vwap,
        }
        (out / "frozen.json").write_text(json.dumps(freeze, indent=2), encoding="utf-8")
        report["frozen"] = freeze

    md = [
        "# EXP-0056 / H47 — MAT × 50-tick range-bar context",
        "",
        f"**Bracket:** {br_lbl}",
        f"**MAT gates:** {gate_lbl}",
        f"**Verdict:** {report['verdict']}",
        "",
        "Discovery = Apr–Aug 2026. Primary context = prior completed bar. "
        "Partial current bar is secondary only.",
        "",
        "## Stage 2 — prior-bar arms",
        "",
        "| arm | n | avg | net | WR | PF | days+ | pass |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for arm in ARMS:
        r = arms_prior[arm]
        ok, reason = (True, "baseline") if arm == "base" else pass_flags[arm]
        md.append(
            f"| {arm} | {r.get('n')} | {r.get('avg')} | {r.get('net')} | "
            f"{r.get('wr')} | {r.get('pf')} | {r.get('days_pos')}/{r.get('days')} | "
            f"{'PASS' if ok and arm != 'base' else ('—' if arm == 'base' else 'FAIL: '+reason)} |"
        )
    md += [
        "",
        "## Secondary — partial-bar arms (cannot ship alone)",
        "",
        "| arm | n | avg | net | WR |",
        "|---|---:|---:|---:|---:|",
    ]
    for arm in ARMS:
        r = arms_partial[arm]
        md.append(f"| {arm} | {r.get('n')} | {r.get('avg')} | {r.get('net')} | {r.get('wr')} |")

    md += ["", "## Stage 1 strata (prior, descriptive)", ""]
    md.append("```json")
    md.append(json.dumps(strata, indent=2, default=str)[:4000])
    md.append("```")
    md.append("")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
    print(f"VERDICT: {report['verdict']}", flush=True)
    return report


def run_holdout(
    cfg: dict,
    out: Path,
    *,
    tp_r: float = 1.0,
    sl_r: float = 1.0,
    trend: bool = True,
    vwap: bool = True,
) -> dict:
    frozen_path = out / "frozen.json"
    if not frozen_path.exists():
        msg = "No frozen.json — discovery did not freeze an arm. Holdout refused."
        print(msg, flush=True)
        report = {"status": "refused", "reason": msg}
        (out / "holdout_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    freeze = json.loads(frozen_path.read_text(encoding="utf-8"))
    arm = freeze["arm"]
    context = freeze.get("context", PRIMARY_CTX)
    tp_r = float(freeze.get("tp_r", tp_r))
    sl_r = float(freeze.get("sl_r", sl_r))
    trend = bool(freeze.get("mat_trend", trend))
    vwap = bool(freeze.get("mat_vwap", vwap))
    print(
        f"Holdout frozen arm={arm} context={context} "
        f"tp_r={tp_r} sl_r={sl_r} trend={trend} vwap={vwap}",
        flush=True,
    )

    data = _load_discovery_sessions(cfg, HOLDOUT_MMYY, trend=trend, vwap=vwap)
    base = run_arm(data, "base", context, tp_r=tp_r, sl_r=sl_r)
    frozen = run_arm(data, arm, context, tp_r=tp_r, sl_r=sl_r)
    same_sign = (
        base.get("avg") is not None
        and frozen.get("avg") is not None
        and (frozen["avg"] > 0) == (freeze.get("lift", 0) >= 0 or True)
        and frozen["avg"] > 0
        and (frozen.get("net") or 0) > 0
    )
    # Plan: frozen arm stay positive avg/net with same sign as discovery
    disc = json.loads((out / "report.json").read_text(encoding="utf-8"))
    disc_avg = disc["arms_prior"][arm].get("avg")
    hold_ok = (
        frozen.get("avg") is not None
        and frozen["avg"] > 0
        and (frozen.get("net") or 0) > 0
        and disc_avg is not None
        and (frozen["avg"] > 0) == (disc_avg > 0)
    )
    verdict = "holdout PASS" if hold_ok else "holdout FAIL — reject / park"
    report = {
        "status": "ran",
        "frozen": freeze,
        "base": base,
        "arm": frozen,
        "holdout_ok": hold_ok,
        "verdict": verdict,
    }
    (out / "holdout_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = (out / "summary.md").read_text(encoding="utf-8") if (out / "summary.md").exists() else ""
    md += (
        f"\n## Stage 3 holdout (Sep–Oct)\n\n"
        f"Frozen: `{arm}` ({context}). "
        f"Holdout n={frozen.get('n')} avg={frozen.get('avg')} net={frozen.get('net')}. "
        f"Base avg={base.get('avg')}.\n\n**{verdict}**\n"
    )
    (out / "summary.md").write_text(md, encoding="utf-8")
    print(verdict, flush=True)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--stage",
        choices=("validate", "discover", "holdout"),
        default="discover",
    )
    ap.add_argument(
        "--tp-r",
        type=float,
        default=1.0,
        help="Take-profit multiple of vol unit (default 1 → symmetric with --sl-r 1)",
    )
    ap.add_argument(
        "--sl-r",
        type=float,
        default=1.0,
        help="Stop-loss multiple of vol unit (default 1). R:R = sl_r : tp_r",
    )
    ap.add_argument(
        "--no-trend",
        action="store_true",
        help="Disable MAT with-trend gate (trend mode off)",
    )
    ap.add_argument(
        "--no-vwap",
        action="store_true",
        help="Disable MAT VWAP-side gate",
    )
    # Back-compat shorthand
    ap.add_argument(
        "--bracket",
        choices=("vol_k1", "rr_1_2"),
        default=None,
        help="Deprecated shorthand: vol_k1 → tp/sl 1; rr_1_2 → tp=2 sl=1",
    )
    args = ap.parse_args()
    tp_r, sl_r = args.tp_r, args.sl_r
    if args.bracket == "vol_k1":
        tp_r, sl_r = 1.0, 1.0
    elif args.bracket == "rr_1_2":
        tp_r, sl_r = 2.0, 1.0
    trend = not args.no_trend
    vwap = not args.no_vwap

    cfg = load_config()
    tag = run_tag(tp_r=tp_r, sl_r=sl_r, trend=trend, vwap=vwap)
    out = Path(cfg["results_dir"]) / f"EXP-0056{tag}"
    out.mkdir(parents=True, exist_ok=True)
    print(
        f"out={out.name}  bracket tp_r={tp_r:g} sl_r={sl_r:g}  "
        f"MAT gates trend={trend} vwap={vwap}",
        flush=True,
    )

    if args.stage == "validate":
        run_validate(cfg, out, trend=trend, vwap=vwap)
    elif args.stage == "discover":
        v = run_validate(cfg, out, trend=trend, vwap=vwap)
        if not v.get("ok"):
            print("Stage 0 FAILED — aborting discovery", flush=True)
            return
        run_discover(cfg, out, tp_r=tp_r, sl_r=sl_r, trend=trend, vwap=vwap)
    else:
        run_holdout(cfg, out, tp_r=tp_r, sl_r=sl_r, trend=trend, vwap=vwap)


if __name__ == "__main__":
    main()
