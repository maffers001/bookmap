"""Manual runner for the mined all-month MAT filter.

Default filter (exact resim winner from EXP-0056_ALLMONTH):
  va_inside & tod_after60 & delta_contra
  on gated MAT (trend+VWAP).

Brackets are vol-scaled: unit = median 1-min range (k=1, clipped ~6–30 pts).
  TP = --tp-r × unit
  SL = --sl-r × unit
  so --tp-r 2 --sl-r 1 → 2R target / 1R stop.

Examples (from repo root, with the project venv):

  .\\.venv\\Scripts\\python.exe -m research.run_mat_allmonth_filter
  .\\.venv\\Scripts\\python.exe -m research.run_mat_allmonth_filter --tp-r 2 --sl-r 1
  .\\.venv\\Scripts\\python.exe -m research.run_mat_allmonth_filter --tp-r 1.5 --sl-r 1 --months 0926,1026
  .\\.venv\\Scripts\\python.exe -m research.run_mat_allmonth_filter --filters va_inside,tod_after60

IN-SAMPLE WARNING: Apr–Oct were used to find this filter. Sweeping TP/SL
here is still mining, not a holdout.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from research.backtest.mat_sim import MatSimConfig, simulate_session
from research.config import load_config
from research.run_mat_range_bars import (
    COMMISSION,
    DISCOVERY_MMYY,
    HOLDOUT_MMYY,
    make_bracket,
    summary_dict,
)
from research.run_mat_range_bars_dig import gated_events
from research.run_mat_allmonth_search import event_masks, load_all, pooled_edges

DEFAULT_FILTERS = ["va_inside", "tod_after60", "delta_contra"]
ALL_MMYY = DISCOVERY_MMYY | HOLDOUT_MMYY


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tp-r", type=float, default=1.0, help="TP multiple of vol unit")
    ap.add_argument("--sl-r", type=float, default=1.0, help="SL multiple of vol unit")
    ap.add_argument(
        "--filters",
        type=str,
        default=",".join(DEFAULT_FILTERS),
        help="Comma-separated mask names AND-ed (see event_masks / candidate_masks)",
    )
    ap.add_argument(
        "--months",
        type=str,
        default="",
        help='Comma-separated MMYY. Quote in PowerShell: --months "0926,1026". '
        "Unquoted 0926 becomes 926; runner zero-pads 3-digit values. Default = all Apr–Oct.",
    )
    ap.add_argument(
        "--tag",
        type=str,
        default="",
        help="Optional results-dir suffix (default encodes filters + RR)",
    )
    args = ap.parse_args()

    filters = [f.strip() for f in args.filters.split(",") if f.strip()]

    def _norm_month(raw: str) -> str:
        """PowerShell turns 0926 → 926 if unquoted; zero-pad back to MMYY."""
        m = raw.strip()
        if m.isdigit() and len(m) in (3, 4):
            return m.zfill(4)
        return m

    months = {_norm_month(m) for m in args.months.split(",") if m.strip()} or set(ALL_MMYY)
    bad = months - ALL_MMYY
    if bad:
        raise SystemExit(f"Unknown month(s) {sorted(bad)}; known={sorted(ALL_MMYY)}")

    cfg = load_config()
    tag = args.tag or (
        f"_{'_'.join(filters)}_RR{args.sl_r:g}_{args.tp_r:g}"
    )
    out = Path(cfg["results_dir"]) / f"EXP-0056_FILTER{tag}"
    out.mkdir(parents=True, exist_ok=True)
    print(
        f"filters={filters}  months={sorted(months)}  "
        f"tp_r={args.tp_r:g} sl_r={args.sl_r:g}  out={out.name}",
        flush=True,
    )

    data_all = load_all(cfg)
    data = {sid: sd for sid, sd in data_all.items() if sd["month"] in months}
    events = gated_events(data, True, True)
    edges = pooled_edges(events)

    sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    br = make_bracket(tp_r=args.tp_r, sl_r=args.sl_r)

    parts: list = []
    by_m: dict[str, list] = defaultdict(list)
    per_day: list[dict] = []
    for sid, sd in sorted(data.items()):
        ev = events[sid]
        if ev.num_rows == 0:
            continue
        masks = event_masks(ev, sd["vf"], edges)
        missing = [f for f in filters if f not in masks]
        if missing:
            raise SystemExit(f"Unknown filter(s) {missing}. Available: {sorted(masks)}")
        mk = np.ones(ev.num_rows, dtype=bool)
        for f in filters:
            mk &= masks[f]
        if not mk.any():
            per_day.append({"session": sid, "month": sd["month"], "n": 0, "net": 0.0})
            continue
        s = simulate_session(
            sid, sd["nq"], sd["es"], sim_cfg,
            gate=None, bracket_fn=br, vwap_from_ns=sd["vf"],
            events=ev.take(np.where(mk)[0]),
        )
        parts.append(s)
        by_m[sd["month"]].append(s)
        per_day.append({
            "session": sid,
            "month": sd["month"],
            "n": s.n,
            "wr": None if s.n == 0 else round(s.wr, 4),
            "net": round(s.net, 2),
            "avg": round(s.net / s.n, 3) if s.n else None,
        })
        print(f"{sid}: n={s.n} net={s.net:+.1f}", flush=True)

    overall = summary_dict(parts)
    overall["by_month"] = {m: summary_dict(ps) for m, ps in sorted(by_m.items())}
    report = {
        "filters": filters,
        "months": sorted(months),
        "tp_r": args.tp_r,
        "sl_r": args.sl_r,
        "n_sessions": len(data),
        "summary": overall,
        "per_day": per_day,
        "warning": "In-sample Apr–Oct mining space; TP/SL sweeps are still exploratory.",
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    md = [
        f"# Manual filter run — {' & '.join(filters)}",
        "",
        f"Bracket: tp_r={args.tp_r:g} sl_r={args.sl_r:g} (R:R {args.sl_r:g}:{args.tp_r:g}). "
        f"Gated MAT trend+VWAP. Months: {', '.join(sorted(months))}.",
        "",
        f"**n={overall.get('n')} avg={overall.get('avg')} net={overall.get('net')} "
        f"WR={overall.get('wr')} PF={overall.get('pf')}**",
        "",
        "| month | n | avg | net |",
        "|---|---:|---:|---:|",
    ]
    for m, s in sorted((overall.get("by_month") or {}).items()):
        md.append(f"| {m} | {s.get('n')} | {s.get('avg')} | {s.get('net')} |")
    md += ["", "| session | month | n | WR | net |", "|---|---|---:|---:|---:|"]
    for r in per_day:
        md.append(
            f"| {r['session']} | {r['month']} | {r['n']} | {r.get('wr')} | {r.get('net'):+.1f} |"
        )
    md.append("")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
    print(
        f"DONE n={overall.get('n')} avg={overall.get('avg')} net={overall.get('net')} "
        f"WR={overall.get('wr')} PF={overall.get('pf')}",
        flush=True,
    )


if __name__ == "__main__":
    main()
