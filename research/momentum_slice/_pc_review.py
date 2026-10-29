"""Join user-tagged good/excellent P&C review notes to full features."""
from __future__ import annotations

import csv
import re
import statistics as st
from pathlib import Path

from research.momentum_slice.rules import enrich, f

OUT = Path("results/MOMENTUM_SLICE/pc_review")
OUT.mkdir(parents=True, exist_ok=True)

GOOD_WORDS = ("excellent", "great", "good pitch", "good example of a pitch", "turns into a  pitch")


def classify(notes: str, tag: str, reason: str) -> str:
    n = (notes or "").lower()
    has_pc = "pitch" in n
    if has_pc and any(w in n for w in ("excellent", "great", "good")):
        return "PC_GOOD_EXPLICIT"
    if has_pc:
        return "PC_MENTION"
    if tag == "TRUE":
        return "MOM_TRUE"
    return "OTHER_FALSE"


def main():
    pack = list(csv.DictReader(open("results/MOMENTUM_SLICE/review/review_pack.csv", encoding="cp1252")))
    feats = {}
    for r in csv.DictReader(open("results/MOMENTUM_SLICE/features/features.csv", encoding="utf-8")):
        enrich(r)
        # features ignition has seconds; pack only minutes -> key on session/side/minute
        key = (r["session"], r["side"], r["ignition_bst"][:16])
        feats[key] = r

    rows = []
    for p in pack:
        cls = classify(p.get("notes", ""), p.get("lst_tag", ""), p.get("reason_code", ""))
        # pack ignition format DD/MM/YYYY HH:MM -> convert to ISO minute
        m = re.match(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}:\d{2})", p["ignition_bst"])
        iso_min = f"{m.group(3)}-{m.group(2)}-{m.group(1)} {m.group(4)}" if m else p["ignition_bst"][:16]
        # session in pack may be non-padded (11026 = 011026)
        sess = p["session"].zfill(6)
        r = feats.get((sess, p["side"], iso_min)) or feats.get((p["session"], p["side"], iso_min))
        row = dict(event_id=p["event_id"], session=sess, side=p["side"],
                   ignition=p["ignition_bst"], cls=cls, tag=p.get("lst_tag"),
                   reason=p.get("reason_code"), notes=p.get("notes", ""),
                   matched=bool(r))
        if r:
            for k in ("nq_pullback_c5s_pts", "nq_pullback_ratio_c5s", "nq_peak_past_c5s_pts",
                      "nq_left_behind_c5s", "nq_left_behind_confirm", "nq_pullback_confirm_pts",
                      "nq_post5_slow_ratio", "nq_post5_net_pts", "nq_post5_range_pts",
                      "nq_post5_tps", "nq_post5_side_delta_ratio", "nq_post5_vol_ratio",
                      "trend_5m_pts", "nq_dom_vs_med", "nq_consume", "nq_persist",
                      "nq_dist_max_ticks", "ret10s", "ret20s", "ret60s", "mfe", "mae",
                      "nq_hold_state", "nq_hold_min_past_pts", "nq_hold_bounce_pts",
                      "nq_burst_tps", "min_rel_speed", "max_order_share", "tod"):
                row[k] = r.get(k)
            row["is_pc_v1"] = (
                f(r, "nq_left_behind_c5s") == 0
                and (f(r, "nq_pullback_c5s_pts") or 0) >= 4
                and (f(r, "nq_pullback_ratio_c5s") or 0) >= 0.70
                and (f(r, "nq_post5_slow_ratio") or 9) <= 0.85
                and (f(r, "nq_post5_net_pts") or 9) <= 1.0)
        rows.append(row)

    cols = list(rows[0].keys())
    with open(OUT / "tagged_pc_joined.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader(); w.writerows(rows)

    keys = ["nq_pullback_c5s_pts", "nq_pullback_ratio_c5s", "nq_peak_past_c5s_pts",
            "nq_post5_slow_ratio", "nq_post5_net_pts", "nq_post5_range_pts",
            "nq_pullback_confirm_pts", "trend_5m_pts", "nq_dom_vs_med",
            "nq_consume", "ret10s", "ret60s", "mae", "mfe"]
    lines = ["# Tagged P&C review — joined features", ""]
    groups = {}
    for r in rows:
        groups.setdefault(r["cls"], []).append(r)
    for cls, gr in sorted(groups.items()):
        lines.append(f"## {cls} n={len(gr)} (matched {sum(g['matched'] for g in gr)})")
        for k in keys:
            xs = [f(g, k) for g in gr if g.get(k) not in (None, "")]
            xs = [x for x in xs if x is not None]
            if xs:
                lines.append(f"- {k}: med {st.median(xs):.2f} mean {st.mean(xs):.2f} min {min(xs):.2f} max {max(xs):.2f} (n={len(xs)})")
        if cls.startswith("PC"):
            lines.append(f"- PC_V1 fires among them: {sum(1 for g in gr if g.get('is_pc_v1'))}/{len(gr)}")
        lines.append("")
    lines.append("## Explicit good/excellent P&C examples")
    lines.append("| id | session side time | pullback_c5s | ratio | slow | net5 | ret60 (mom dir) | PC_V1? | notes |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r["cls"] == "PC_GOOD_EXPLICIT":
            lines.append(
                f"| {r['event_id']} | {r['session']} {r['side']} {r['ignition']} | "
                f"{r.get('nq_pullback_c5s_pts')} | {r.get('nq_pullback_ratio_c5s')} | "
                f"{r.get('nq_post5_slow_ratio')} | {r.get('nq_post5_net_pts')} | {r.get('ret60s')} | "
                f"{r.get('is_pc_v1')} | {r['notes']} |")
    text = "\n".join(lines) + "\n"
    (OUT / "tagged_summary.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
