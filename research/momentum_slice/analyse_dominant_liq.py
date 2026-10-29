"""Dominant-liquidity hypothesis test.

User: momentum works best when a noticeable resting level (or levels) is
taken with speed and minimal pullback — market expected it to hold; when it
doesn't, trapped passives fuel continuation.

Proxies (all from existing features.csv, causal at confirm where noted):
  nq_dom_share   = maxlvl / stack   (single-level concentration of ahead book)
  nq_dom_vs_med  = maxlvl / median_level  (prominence vs typical level size;
                   median inferred from density = stack/(med*ZONE_TICKS))
  taken_clean    = left_behind_confirm=1 AND pullback_confirm <= med
  fast_burst     = top tercile of nq_burst_n_trades

Also joins partial manual tags via review_key + review_pack.
"""
from __future__ import annotations

import csv
import statistics as st
from collections import Counter
from pathlib import Path

from research.momentum_slice.detector import ZONE_TICKS

BASE = Path("results/MOMENTUM_SLICE")


def f(r, k):
    v = r.get(k)
    if v in ("", "nan", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def ret_stats(rows):
    if not rows:
        return "n=0"
    r10 = [f(r, "ret10s") for r in rows]
    r10 = [x for x in r10 if x is not None]
    return (f"n={len(r10)} ret10s mean {st.mean(r10):+.1f} med {st.median(r10):+.1f} "
            f"P>5 {sum(x > 5 for x in r10)/len(r10):.2f} "
            f"P<-5 {sum(x < -5 for x in r10)/len(r10):.2f} "
            f"MAE {st.mean([f(r,'mae') for r in rows if f(r,'mae') is not None]):.0f}")


def terciles(rows, key):
    vals = sorted(r[key] for r in rows if r.get(key) is not None)
    if len(vals) < 9:
        return []
    q1, q2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    return [
        ("low", [r for r in rows if r.get(key) is not None and r[key] <= q1], q1, q2),
        ("mid", [r for r in rows if r.get(key) is not None and q1 < r[key] <= q2], q1, q2),
        ("high", [r for r in rows if r.get(key) is not None and r[key] > q2], q1, q2),
    ]


def main():
    rows = list(csv.DictReader(open(BASE / "features/features.csv", encoding="utf-8")))
    for r in rows:
        stack, mx, dens = f(r, "nq_stack"), f(r, "nq_maxlvl"), f(r, "nq_density")
        r["nq_dom_share"] = (mx / stack) if stack and mx is not None and stack > 0 else None
        med = (stack / (dens * ZONE_TICKS)) if dens and dens > 0 and stack else None
        r["nq_dom_vs_med"] = (mx / med) if med and mx is not None and med > 0 else None
        es_s, es_m = f(r, "es_stack"), f(r, "es_maxlvl")
        r["es_dom_share"] = (es_m / es_s) if es_s and es_m is not None and es_s > 0 else None
        r["burst_tps"] = (f(r, "nq_burst_n_trades") / 2.0) if f(r, "nq_burst_n_trades") else None
        r["pullback"] = f(r, "nq_pullback_confirm_pts")
        r["left"] = f(r, "nq_left_behind_confirm")

    pb = [r["pullback"] for r in rows if r["pullback"] is not None]
    med_pb = st.median(pb) if pb else 2.0
    bt = sorted(r["burst_tps"] for r in rows if r["burst_tps"] is not None)
    fast_cut = bt[2 * len(bt) // 3]
    for r in rows:
        r["taken_clean"] = (r["left"] == 1 and r["pullback"] is not None
                            and r["pullback"] <= med_pb)
        r["fast_burst"] = r["burst_tps"] is not None and r["burst_tps"] > fast_cut

    # tag join
    key = {r["event_id"]: r for r in csv.DictReader(open(BASE / "review/review_key.csv", encoding="utf-8"))}
    pack = list(csv.DictReader(open(BASE / "review/review_pack.csv", encoding="utf-8")))
    by = {(r["session"], r["side"], r["ignition_bst"]): r for r in rows}
    for p in pack:
        k = key.get(p.get("event_id", ""))
        if not k:
            continue
        fr = by.get((k["session"], k["side"], k["ignition_bst"]))
        if fr is not None:
            fr["lst_tag"] = (p.get("lst_tag") or "").strip().upper()

    lines = ["# Dominant-liquidity hypothesis", "",
             f"nq_dom_share = maxlvl/stack; nq_dom_vs_med = maxlvl/inferred_median. "
             f"taken_clean = left_behind & pullback≤{med_pb:.1f} pts. "
             f"fast_burst = burst_tps > {fast_cut:.0f}.", ""]

    for key_name in ("nq_dom_share", "nq_dom_vs_med", "es_dom_share", "nq_top3_conc"):
        if key_name == "nq_top3_conc":
            for r in rows:
                r["nq_top3_conc"] = f(r, "nq_top3_conc")
        lines.append(f"## split by {key_name}")
        for name, sel, q1, q2 in terciles(rows, key_name):
            lines.append(f"- {name} (cuts {q1:.3f}/{q2:.3f}): {ret_stats(sel)}")
            lines.append(f"  - late: {ret_stats([r for r in sel if r.get('tod') == 'late'])}")
        lines.append("")

    # Core interaction: dominant + taken clean + fast
    dom_vals = [r["nq_dom_share"] for r in rows if r["nq_dom_share"] is not None]
    dom_cut = st.median(dom_vals)
    for r in rows:
        r["dom"] = r["nq_dom_share"] is not None and r["nq_dom_share"] >= dom_cut

    combos = {
        "dominant + taken_clean + fast_burst": lambda r: r["dom"] and r["taken_clean"] and r["fast_burst"],
        "dominant + taken_clean": lambda r: r["dom"] and r["taken_clean"],
        "taken_clean + fast_burst (any stack)": lambda r: r["taken_clean"] and r["fast_burst"],
        "dominant but NOT taken_clean": lambda r: r["dom"] and not r["taken_clean"],
        "NOT dominant + taken_clean": lambda r: (not r["dom"]) and r["taken_clean"],
        "slow_burst + NOT left_behind": lambda r: (r["burst_tps"] is not None and r["burst_tps"] <= bt[len(bt)//3]) and r["left"] != 1,
    }
    lines.append(f"## Interactions (dom_share ≥ med {dom_cut:.3f})")
    for name, fn in combos.items():
        sel = [r for r in rows if fn(r)]
        lines.append(f"- {name}: {ret_stats(sel)}")
        lines.append(f"  - late: {ret_stats([r for r in sel if r.get('tod') == 'late'])}")
    lines.append(f"- ALL: {ret_stats(rows)}")
    lines.append(f"  - late: {ret_stats([r for r in rows if r.get('tod') == 'late'])}")
    lines.append("")

    # Tags
    tagged = [r for r in rows if r.get("lst_tag") in ("TRUE", "FALSE")]
    lines.append("## Manual tags vs dominance")
    if not tagged:
        lines.append("(no tags yet)")
    else:
        lines.append(f"tagged {dict(Counter(r['lst_tag'] for r in tagged))}")
        for tag in ("TRUE", "FALSE"):
            sel = [r for r in tagged if r["lst_tag"] == tag]
            ds = [r["nq_dom_share"] for r in sel if r["nq_dom_share"] is not None]
            dm = [r["nq_dom_vs_med"] for r in sel if r["nq_dom_vs_med"] is not None]
            lines.append(f"### {tag} n={len(sel)}")
            lines.append(f"- nq_dom_share med {st.median(ds):.3f}" if ds else "- nq_dom_share n/a")
            lines.append(f"- nq_dom_vs_med med {st.median(dm):.2f}" if dm else "- nq_dom_vs_med n/a")
            lines.append(f"- taken_clean: {sum(1 for r in sel if r['taken_clean'])}/{len(sel)}")
            lines.append(f"- fast_burst: {sum(1 for r in sel if r['fast_burst'])}/{len(sel)}")
            lines.append(f"- dom+taken_clean+fast: "
                         f"{sum(1 for r in sel if r['dom'] and r['taken_clean'] and r['fast_burst'])}/{len(sel)}")
            lines.append(f"- outcomes: {ret_stats(sel)}")
        lines.append("")
        lines.append("### TRUE rate among tagged, by dominance tercile")
        for name, sel, *_ in terciles(tagged, "nq_dom_share"):
            n_t = sum(1 for r in sel if r["lst_tag"] == "TRUE")
            lines.append(f"- {name}: TRUE {n_t}/{len(sel)} ({n_t/len(sel):.0%})  {ret_stats(sel)}")

    # Anchor example
    ex = [r for r in rows if r["session"] == "100926" and r["ignition_bst"].endswith("15:24:38")]
    if ex:
        r = ex[0]
        lines += ["", "## Anchor 100926 15:24:38",
                  f"dom_share={r['nq_dom_share']:.3f} dom_vs_med={r['nq_dom_vs_med']:.1f} "
                  f"maxlvl={r['nq_maxlvl']} stack={r['nq_stack']} "
                  f"taken_clean={r['taken_clean']} fast_burst={r['fast_burst']} "
                  f"pullback={r['pullback']} left={r['left']}"]

    text = "\n".join(lines) + "\n"
    (BASE / "features/dominant_liq.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
