"""Full manual-tag discriminator analysis (review pack completed).

  .venv\\Scripts\\python.exe -m research.momentum_slice.analyse_tags
"""
from __future__ import annotations

import statistics as st
from collections import Counter
from pathlib import Path

from research.momentum_slice.detector import ZONE_TICKS
from research.momentum_slice.taglib import BASE, load_tagged_features


def f(r, k):
    v = r.get(k)
    if v in ("", "nan", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def ret_stats(rows, key="ret10s"):
    xs = [f(r, key) for r in rows]
    xs = [x for x in xs if x is not None]
    if not xs:
        return "n=0"
    mae = [f(r, "mae") for r in rows if f(r, "mae") is not None]
    return (f"n={len(xs)} {key} {st.mean(xs):+.1f}/{st.median(xs):+.1f} "
            f"P>5 {sum(x > 5 for x in xs)/len(xs):.2f} P<-5 {sum(x < -5 for x in xs)/len(xs):.2f} "
            f"MAE {st.mean(mae):.0f}")


FEATURES = [
    "nq_dom_share", "nq_dom_vs_med", "es_dom_share", "nq_dist_max_ticks", "es_dist_max_ticks",
    "nq_maxlvl", "nq_n_big", "es_n_big", "nq_stack", "nq_density", "es_density",
    "nq_consume", "es_consume", "nq_persist", "nq_pull_est", "es_pull_est",
    "nq_burst_tps", "nq_tps_30s", "min_base_rel", "max_order_share",
    "nq_left_behind_confirm", "nq_pullback_confirm_pts", "nq_pullback_c5s_pts",
    "both_cleared_confirm", "prior_run_5m_pts", "dist_vwap_pts", "range_5m_pts",
    "compression_1m_5m", "es_signed_lag_ms",
]


def main():
    rows = load_tagged_features()
    for r in rows:
        stack, mx, dens = f(r, "nq_stack"), f(r, "nq_maxlvl"), f(r, "nq_density")
        r["nq_dom_share_v"] = (mx / stack) if stack and mx is not None else None
        med = (stack / (dens * ZONE_TICKS)) if dens and stack else None
        r["nq_dom_vs_med_v"] = (mx / med) if med and mx else None
        es_s, es_m = f(r, "es_stack"), f(r, "es_maxlvl")
        r["es_dom_share_v"] = (es_m / es_s) if es_s and es_m else None
        r["nq_burst_tps"] = (f(r, "nq_burst_n_trades") / 2.0) if f(r, "nq_burst_n_trades") else None
    # alias for FEATURES names
    for r in rows:
        r["nq_dom_share"] = r["nq_dom_share_v"]
        r["nq_dom_vs_med"] = r["nq_dom_vs_med_v"]
        r["es_dom_share"] = r["es_dom_share_v"]

    tagged = [r for r in rows if r.get("lst_tag") in ("TRUE", "FALSE", "UNSURE")]
    lines = ["# Manual-tag discriminator analysis", "",
             f"tags: {dict(Counter(r['lst_tag'] for r in tagged))} of {len(rows)} candidates "
             f"(pack covered 60)", "",
             f"reasons (FALSE): {dict(Counter(r['reason_code'] for r in tagged if r['lst_tag']=='FALSE'))}", ""]
    for tag in ("TRUE", "FALSE"):
        sel = [r for r in tagged if r["lst_tag"] == tag]
        lines.append(f"## {tag}: {ret_stats(sel)}")
        lines.append(f"  late: {ret_stats([r for r in sel if r.get('tod')=='late'])}")
    lines.append("")

    lines.append("## Feature medians TRUE vs FALSE (higher TRUE = continuation-like)")
    lines.append("")
    lines.append("| feature | TRUE med | FALSE med |")
    lines.append("|---|---:|---:|")
    for k in FEATURES:
        def med(tag):
            vals = [f(r, k) for r in tagged if r["lst_tag"] == tag]
            vals = [x for x in vals if x is not None]
            if k in ("nq_dom_share", "nq_dom_vs_med", "es_dom_share", "nq_burst_tps"):
                vals = [r[k] for r in tagged if r["lst_tag"] == tag and isinstance(r.get(k), (int, float))]
            return st.median(vals) if vals else float("nan")
        lines.append(f"| {k} | {med('TRUE'):.3f} | {med('FALSE'):.3f} |")
    lines.append("")

    # Categorical
    for col in ("nq_hold_state", "tod"):
        lines.append(f"## {col} by tag")
        for tag in ("TRUE", "FALSE"):
            sel = [r for r in tagged if r["lst_tag"] == tag]
            lines.append(f"- {tag}: {dict(Counter(r.get(col) for r in sel))}")
        lines.append("")

    # Frozen working rule from earlier (medians frozen in STATUS, not refit here except fast cut noted)
    bt = sorted(r["nq_burst_tps"] for r in rows if r["nq_burst_tps"] is not None)
    fast_cut = bt[2 * len(bt) // 3]
    def rule(r):
        return ((r["nq_dom_share"] or 0) >= 0.078
                and f(r, "nq_left_behind_confirm") == 1
                and (f(r, "nq_pullback_confirm_pts") or 99) <= 2.1
                and (r["nq_burst_tps"] or 0) > fast_cut)
    lines.append("## Frozen recipe: dom_share>=0.078 + left_behind + pullback<=2.1 + fast_burst")
    for scope, pool in (("tagged", tagged), ("all76", rows), ("late-tagged", [r for r in tagged if r.get("tod") == "late"])):
        sel = [r for r in pool if rule(r)]
        tp = sum(1 for r in sel if r.get("lst_tag") == "TRUE")
        lines.append(f"- {scope}: fires {len(sel)}; TRUE {tp} "
                     f"({tp/len(sel):.0%} of fired, tagged only) {ret_stats(sel)}")
    lines.append("")
    # Simpler causal gates
    gates = {
        "left_behind only": lambda r: f(r, "nq_left_behind_confirm") == 1,
        "left_behind + pullback<=2.1": lambda r: f(r, "nq_left_behind_confirm") == 1 and (f(r, "nq_pullback_confirm_pts") or 99) <= 2.1,
        "fast_burst only": lambda r: (r["nq_burst_tps"] or 0) > fast_cut,
        "dom_share>=0.078 only": lambda r: (r["nq_dom_share"] or 0) >= 0.078,
        "NO_RETEST state": lambda r: r.get("nq_hold_state") == "NO_RETEST",
    }
    lines.append("## Single/simple gates (tagged precision + outcomes)")
    for name, fn in gates.items():
        sel = [r for r in tagged if fn(r)]
        tp = sum(1 for r in sel if r["lst_tag"] == "TRUE")
        lines.append(f"- {name}: tagged fires {len(sel)}, TRUE {tp} ({tp/len(sel):.0%}) {ret_stats(sel)}" if sel else f"- {name}: 0")
    lines.append("")

    # Notes themes (keyword counts in FALSE notes)
    lines.append("## FALSE-note themes (manual count from notes text)")
    themes = {
        "pitch and catch": "pitch and catch", "pitcha": "pitch", "not taken": "not take",
        "doesn't take": "doesn't take", "does not take": "does not", "never reached": "never",
        "slow": "slow", "counter": "counter", "revers": "revers", "pullback": "pullback",
        "stall": "stall", "hold": "hold", "with trend": "trend", "follow through": "follow",
    }
    cnt = Counter()
    for r in tagged:
        txt = (r.get("notes") or "").lower()
        for kw, label in themes.items():
            if kw in txt:
                cnt[(r["lst_tag"], label)] += 1
    for (tag, label), n in sorted(cnt.items()):
        lines.append(f"- {tag} / '{label}': {n}")
    lines.append("")
    lines.append("Decision gate: recipe should be high-precision on tags AND beat "
                 "controls late (see discriminator.md). If precision < ~60% or "
                 "late outcomes not better than all-late, do not expand as-is.")

    (BASE / "features/tag_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
