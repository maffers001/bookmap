"""All-sessions MAT backtest on frozen base = vol k=1 (trend+VWAP, UK 14:30-17:00).

Cohorts: old 14 (in-sample) vs new 9 (OOS). Also reports TP12.5/SL30 comparator
and fixed ±10. Writes results/MAT2_ALL_SESSIONS_VOLK1/.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    combine_gates,
    filter_summary,
    make_trend_mode,
    make_vol_bracket,
    make_vwap_side,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25

OLD = {
    "260326", "030826", "040826", "050826",
    "160926", "170926", "180926", "210926", "220926",
    "011026", "021026", "051026", "061026", "071026",
}
NEW = {
    "081026", "091026", "121026", "131026", "141026",
    "151026", "161026", "290926", "300926",
}


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


def cohort_of(sid: str) -> str:
    if sid in OLD:
        return "old"
    if sid in NEW:
        return "new"
    return "other"


def pack_cohort(s: SimSummary, sessions: set[str]) -> dict:
    sub = filter_summary(s, lambda t: t.session_id in sessions, COMMISSION)
    d = sub.to_dict()
    d["~$"] = round(sub.net * 20, 0)
    return d


def per_session_rows(s: SimSummary, cache_keys) -> list[dict]:
    rows = []
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for sid in sorted(cache_keys):
        sub = filter_summary(s, lambda t, x=sid: t.session_id == x, COMMISSION)
        equity += sub.net
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        reasons = {"tp": 0, "sl": 0, "eod": 0, "time": 0, "be": 0, "trail": 0}
        for tr in sub.trades:
            reasons[tr.reason] = reasons.get(tr.reason, 0) + 1
        rows.append({
            "session": sid,
            "cohort": cohort_of(sid),
            **sub.to_dict(),
            "~$": round(sub.net * 20, 0),
            "equity_after": round(equity, 2),
            "dd_after": round(peak - equity, 2),
            "reasons": reasons,
        })
    return rows


def run_variant(cache, name, sim_cfg, gate, bracket_fn=None) -> tuple[SimSummary, dict]:
    parts = []
    for sid, (nq, es, vf) in cache.items():
        parts.append(simulate_session(
            sid, nq, es, sim_cfg, gate=gate,
            regime_window_min=60, trend_window_min=30, vol_window_min=30,
            bracket_fn=bracket_fn, vwap_from_ns=vf,
        ))
    all_s = merge_summaries(parts, COMMISSION)
    per = per_session_rows(all_s, cache.keys())
    old = pack_cohort(all_s, OLD & set(cache.keys()))
    new = pack_cohort(all_s, NEW & set(cache.keys()))
    report = {
        "name": name,
        "all": all_s.to_dict(),
        "old": old,
        "new": new,
        "per_session": per,
        "days_pos_old": sum(1 for r in per if r["cohort"] == "old" and r["net"] > 0),
        "days_pos_new": sum(1 for r in per if r["cohort"] == "new" and r["net"] > 0),
        "worst_day": min(per, key=lambda r: r["net"]) if per else None,
    }
    return all_s, report


def md_table_cohort(report: dict) -> list[str]:
    lines = [
        "| cohort | n | WR | gross | net | ~$ @ $20/pt | PF | maxDD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label in (("all", "all"), ("old", "old 14"), ("new", "**new 9**")):
        r = report[key]
        lines.append(
            f"| {label} | {r['n']} | {r['wr']} | {r.get('gross', r['net']):+.2f} | "
            f"{r['net']:+.2f} | {r.get('~$', round(r['net']*20)):.0f} | {r['pf']} | {r['max_dd']} |"
        )
    return lines


def md_per_session(report: dict) -> list[str]:
    lines = [
        "| session | cohort | n | WR | gross | net | ~$ | equity | DD after |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in report["per_session"]:
        lines.append(
            f"| {r['session']} | {r['cohort']} | {r['n']} | {r['wr']} | "
            f"{r.get('gross', r['net']):+.2f} | {r['net']:+.2f} | {r['~$']:.0f} | "
            f"{r['equity_after']:+.2f} | {r['dd_after']:.1f} |"
        )
    return lines


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    raw = Path(cfg["raw_dir"])
    out = Path(cfg["results_dir"]) / "MAT2_ALL_SESSIONS_VOLK1"
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name for p in raw.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (pq / f"session_id={p.name}").exists()
    )
    cache: dict[str, tuple[pa.Table, pa.Table, int]] = {}
    for sid in sessions:
        nq = filt(_load_trades(pq, sid, "NQ"))
        es0 = _load_trades(pq, sid, "ES")
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows:
            mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
            cache[sid] = (nq, es, bounds(mid)[0])
    print(f"cached {len(cache)} sessions", flush=True)

    # Data hygiene note for 290926/300926
    hygiene = {
        "290926_vs_300926": "NOT duplicates — different calendar days "
        "(2026-09-29 vs 2026-09-30), different trade counts and sim P&L. "
        "Prior all-sessions identical rows were a script bug.",
    }

    base_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    variants = [
        ("vol_k1", MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal"),
         make_vol_bracket(1.0)),
        ("vol_k1_confirm", MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="confirm"),
         make_vol_bracket(1.0)),
        ("tp12p5_sl30", MatSimConfig(slip_ticks=1.0, tp_pts=12.5, sl_pts=30.0,
                                     commission_pts_rt=COMMISSION, es_stamp="causal"), None),
        ("fixed_tp10", MatSimConfig(slip_ticks=1.0, tp_pts=10.0, sl_pts=10.0,
                                    commission_pts_rt=COMMISSION, es_stamp="causal"), None),
    ]

    results = {"hygiene": hygiene, "commission_pts_rt": COMMISSION}
    md = [
        "# MAT2 all-sessions — base = vol k=1",
        "",
        f"Sessions: **{len(cache)}** (old {len(OLD & set(cache))} / "
        f"new {len(NEW & set(cache))}). UK 14:30–17:00, trend=1 + VWAP, 1-tick slip, "
        f"commission {COMMISSION} pt/RT.",
        "",
        "## Data hygiene",
        "",
        hygiene["290926_vs_300926"],
        "",
    ]

    for name, sim_cfg, br in variants:
        print(f"running {name}…", flush=True)
        _, report = run_variant(cache, name, sim_cfg, base_gate, br)
        results[name] = report
        md.append(f"## {name}")
        md.append("")
        md.extend(md_table_cohort(report))
        md.append("")
        md.append(f"days+ old/new: {report['days_pos_old']}/{report['days_pos_new']}")
        if report["worst_day"]:
            w = report["worst_day"]
            md.append(f"worst day: `{w['session']}` net {w['net']:+.2f}")
        md.append("")
        if name == "vol_k1":
            md.append("### Per session (vol_k1) + equity / DD path")
            md.append("")
            md.extend(md_per_session(report))
            md.append("")

    md.extend([
        "## Comparison (all sessions, 1-tick + commission)",
        "",
        "| config | n | WR | gross | net | maxDD |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for name, _, _ in variants:
        r = results[name]["all"]
        md.append(
            f"| {name} | {r['n']} | {r['wr']} | {r['gross']:+.2f} | "
            f"{r['net']:+.2f} | {r['max_dd']} |"
        )
    md.append("")

    (out / "report.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    # Also refresh legacy path name for plan references
    legacy = Path(cfg["results_dir"]) / "MAT2_ALL_SESSIONS_TP12p5_SL30"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "summary.md").write_text("\n".join(md), encoding="utf-8")
    (legacy / "report.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
