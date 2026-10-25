"""Third-filter search on all-month near-miss anchors (Apr–Oct, in-sample).

Anchors from run_mat_allmonth_search exact resim that were positive in 6/7
months. Screens one extra causal filter on each anchor's executed trades,
then exactly re-simulates the best triples. Still mining — no holdout left.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from research.backtest.mat_sim import MatSimConfig, simulate_session
from research.config import load_config
from research.run_mat_range_bars import COMMISSION, make_bracket, summary_dict
from research.run_mat_range_bars_dig import gated_events
from research.run_mat_allmonth_search import (
    all_months_ok,
    evaluate,
    event_masks,
    load_all,
    pooled_edges,
)

ANCHORS = {
    "va_inside & tod_after60": ["va_inside", "tod_after60"],
    "delta_contra & rg_net60_against": ["delta_contra", "rg_net60_against"],
}


def main() -> None:
    cfg = load_config()
    out = Path(cfg["results_dir"]) / "EXP-0056_ALLMONTH"
    out.mkdir(parents=True, exist_ok=True)
    data = load_all(cfg)
    events = gated_events(data, True, True)
    edges = pooled_edges(events)
    sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    br = make_bracket(tp_r=1.0, sl_r=1.0)

    # Per-session gated event masks, computed once.
    sess_masks: dict[str, dict] = {}
    for sid, sd in data.items():
        ev = events[sid]
        sess_masks[sid] = event_masks(ev, sd["vf"], edges) if ev.num_rows else {}

    def combined_mask(sid: str, parts: list[str]) -> np.ndarray:
        ev = events[sid]
        mk = np.ones(ev.num_rows, dtype=bool)
        for p in parts:
            mk &= sess_masks[sid][p]
        return mk

    def exact_resim(parts: list[str]) -> dict:
        parts_all = []
        by_m: dict[str, list] = defaultdict(list)
        for sid, sd in data.items():
            ev = events[sid]
            if ev.num_rows == 0:
                continue
            mk = combined_mask(sid, parts)
            if not mk.any():
                continue
            s = simulate_session(
                sid, sd["nq"], sd["es"], sim_cfg, gate=None, bracket_fn=br,
                vwap_from_ns=sd["vf"], events=ev.take(np.where(mk)[0]),
            )
            parts_all.append(s)
            by_m[sd["month"]].append(s)
        r = summary_dict(parts_all)
        r["by_month"] = {mo: summary_dict(ps) for mo, ps in sorted(by_m.items())}
        r["all_months_positive"] = (
            len(r["by_month"]) == 7
            and all((s.get("net") or 0) > 0 and (s.get("avg") or -9) > 0 for s in r["by_month"].values())
            and r.get("n", 0) >= 50
        )
        return r

    report: dict = {"anchors": {}, "triples_resim": {}, "confirmed": []}
    shortlist: list[tuple[str, list[str], dict]] = []

    for aname, aparts in ANCHORS.items():
        # Anchor trades tagged with every single mask (for extra-filter screen).
        trade_extra: dict[str, list[bool]] = defaultdict(list)
        pnls: list[float] = []
        months: list[str] = []
        single_names: list[str] = []
        for sid, sd in data.items():
            ev = events[sid]
            if ev.num_rows == 0:
                continue
            masks = sess_masks[sid]
            if not single_names:
                single_names = [k for k in masks if k not in ("base",) + tuple(aparts)]
            amk = combined_mask(sid, aparts)
            if not amk.any():
                continue
            sub_idx = np.where(amk)[0]
            sub = ev.take(sub_idx)
            s = simulate_session(
                sid, sd["nq"], sd["es"], sim_cfg, gate=None, bracket_fn=br,
                vwap_from_ns=sd["vf"], events=sub,
            )
            sub_ts = sub.column("event_ts_ns").to_numpy().astype(np.int64)
            for tr in s.trades:
                j = int(np.searchsorted(sub_ts, tr.entry_ts_ns, side="right")) - 1
                if j < 0:
                    continue
                oi = int(sub_idx[j])
                pnls.append(tr.pnl - COMMISSION)
                months.append(sd["month"])
                for name in single_names:
                    trade_extra[name].append(bool(masks[name][oi]))
        pnls_a = np.array(pnls)
        months_a = np.array(months)
        base_eval = evaluate(np.ones(len(pnls_a), dtype=bool), pnls_a, months_a)
        report["anchors"][aname] = base_eval
        print(f"anchor {aname}: trades={len(pnls_a)} avg={base_eval['avg']} months+={base_eval['months_pos']}/7", flush=True)

        wins = {}
        for name in single_names:
            mk = np.array(trade_extra[name], dtype=bool)
            r = evaluate(mk, pnls_a, months_a)
            if all_months_ok(r):
                wins[name] = r
        for name in sorted(wins, key=lambda k: wins[k]["min_month_avg"] or -9, reverse=True)[:10]:
            r = wins[name]
            print(f"  SCREEN-WIN +{name}: n={r['n']} avg={r['avg']} min_month_avg={r['min_month_avg']}", flush=True)
            shortlist.append((f"{aname} & {name}", aparts + [name], r))
        report["anchors"][aname + " screen_wins"] = wins

    # Exact resim top triples by descriptive min-month avg (max 6).
    shortlist.sort(key=lambda x: x[2]["min_month_avg"] or -9, reverse=True)
    for name, parts, _scr in shortlist[:6]:
        r = exact_resim(parts)
        report["triples_resim"][name] = r
        print(f"RESIM {name}: n={r.get('n')} avg={r.get('avg')} net={r.get('net')} all7+={r['all_months_positive']}", flush=True)
        if r["all_months_positive"]:
            report["confirmed"].append(name)

    (out / "triples_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = [
        "# All-month triples on near-miss anchors (in-sample)",
        "",
        f"**Confirmed all-7-months-positive triples: {report['confirmed'] or 'none'}**",
        "",
        "| triple | n | avg | net | WR | PF | all 7 + |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for name, r in report["triples_resim"].items():
        md.append(f"| {name} | {r.get('n')} | {r.get('avg')} | {r.get('net')} | {r.get('wr')} | {r.get('pf')} | {r['all_months_positive']} |")
    md += ["", "### By month", "", "| triple | month | n | avg | net |", "|---|---|---:|---:|---:|"]
    for name, r in report["triples_resim"].items():
        for mo, s in sorted((r.get("by_month") or {}).items()):
            md.append(f"| {name} | {mo} | {s.get('n')} | {s.get('avg')} | {s.get('net')} |")
    md.append("")
    (out / "triples_summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"CONFIRMED TRIPLES: {report['confirmed']}", flush=True)


if __name__ == "__main__":
    main()
