"""Pitch-and-catch conversion test.

User definition from FALSE notes: push into liquidity but NOT past the
dominant level (or not with speed), tape slows, sideways/no continuation
(absorption + exhaustion), weak pullbacks on low volume -> reversal.

Causal fade definition (known at confirm+5s):
  PC = nq_left_behind_c5s == 0
       AND nq_pullback_c5s_pts >= 4
       AND nq_post5_slow_ratio <= 0.8      (tape slowed vs burst)
       AND nq_post5_net_pts <= 1           (sideways: no side progress)
Fade entry = confirm+5s, opposite side. Outcomes labelled from there.
Also reports FALSE-tag precision of PC and fade P&L by tag.
"""
from __future__ import annotations

import statistics as st
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.correlated.harness import label_in_points, load_pair
from research.momentum_slice.scan import HORIZONS, TARGETS
from research.momentum_slice.taglib import BASE, load_tagged_features


def f(r, k):
    v = r.get(k)
    if v in ("", "nan", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def stats(rows, key):
    xs = [r[key] for r in rows if r.get(key) is not None]
    if not xs:
        return "n=0"
    return (f"n={len(xs)} {key} {st.mean(xs):+.1f}/{st.median(xs):+.1f} "
            f"P>5 {sum(x > 5 for x in xs)/len(xs):.2f} P<-5 {sum(x < -5 for x in xs)/len(xs):.2f}")


def main():
    rows = load_tagged_features()
    for r in rows:
        r["pc"] = (f(r, "nq_left_behind_c5s") == 0
                   and (f(r, "nq_pullback_c5s_pts") or 0) >= 4
                   and (f(r, "nq_post5_slow_ratio") or 9) <= 0.8
                   and (f(r, "nq_post5_net_pts") or 9) <= 1)
        r["pc_loose"] = (f(r, "nq_left_behind_c5s") == 0
                         and (f(r, "nq_pullback_c5s_pts") or 0) >= 4)

    # Causal fade labelling from confirm+5s
    cfg = load_config(); pq_dir = Path(cfg["parquet_dir"])
    by_sid: dict[str, list] = {}
    for r in rows:
        by_sid.setdefault(r["session"], []).append(r)
    for sid, evs in by_sid.items():
        pair = load_pair(pq_dir, sid)
        if pair is None:
            continue
        tbl = pa.table({
            "event_ts_ns": np.array([int(float(r["confirm_ns"])) + 5_000_000_000 for r in evs], dtype=np.int64),
            "side": np.array([-int(float(r["side_int"])) for r in evs], dtype=np.int8),
        })
        lab = label_in_points(tbl, pair, HORIZONS, TARGETS)
        for i, r in enumerate(evs):
            if i >= lab.num_rows:
                break
            r["fade_ret10s"] = lab.column("ret_pts_10000ms")[i].as_py()
            r["fade_ret20s"] = lab.column("ret_pts_20000ms")[i].as_py()
            r["fade_ret60s"] = lab.column("ret_pts_60000ms")[i].as_py()
            r["fade_mfe"] = lab.column("mfe_pts")[i].as_py()
            r["fade_mae"] = lab.column("mae_pts")[i].as_py()

    tagged = [r for r in rows if r.get("lst_tag") in ("TRUE", "FALSE")]
    pc = [r for r in rows if r["pc"]]
    lines = ["# Pitch-and-catch conversion (causal fade at confirm+5s)", "",
             f"PC fires {len(pc)}/{len(rows)}; among tagged PC: "
             f"{dict(Counter(r['lst_tag'] for r in pc if r.get('lst_tag')))}", "",
             "## Fade outcomes (entry confirm+5s, opposite side)", "",
             f"- PC fade: {stats(pc, 'fade_ret10s')} | {stats(pc, 'fade_ret60s')}",
             f"  - late: {stats([r for r in pc if r.get('tod')=='late'], 'fade_ret10s')}",
             f"- PC-loose (no stall gates) fade: {stats([r for r in rows if r['pc_loose']], 'fade_ret10s')}",
             f"- ALL candidates fade: {stats(rows, 'fade_ret10s')}",
             f"- TRUE-tagged fade (should be bad): {stats([r for r in tagged if r['lst_tag']=='TRUE'], 'fade_ret10s')}",
             f"- FALSE-tagged fade: {stats([r for r in tagged if r['lst_tag']=='FALSE'], 'fade_ret10s')}",
             f"  - FALSE & PC: {stats([r for r in tagged if r['lst_tag']=='FALSE' and r['pc']], 'fade_ret10s')}",
             f"  - FALSE & not PC: {stats([r for r in tagged if r['lst_tag']=='FALSE' and not r['pc']], 'fade_ret10s')}", "",
             "## What fraction of FALSE notes mentioning pitch-and-catch are caught by PC?", ""]
    pc_notes = [r for r in tagged if "pitch" in (r.get("notes") or "").lower()]
    lines.append(f"- notes mention pitch&catch: {len(pc_notes)} "
                 f"({dict(Counter(r['lst_tag'] for r in pc_notes))}); "
                 f"PC catches {sum(1 for r in pc_notes if r['pc'])}/{len(pc_notes)}")
    lines.append(f"- their fade: {stats(pc_notes, 'fade_ret10s')}")
    lines.append("")
    lines.append("## Stall feature medians: PC vs continuers (ret10s>5) vs TRUE")
    for k in ("nq_post5_slow_ratio", "nq_post5_net_pts", "nq_post5_range_pts",
              "nq_post5_vol_ratio", "nq_post5_side_delta_ratio",
              "nq_pullback_c5s_pts", "nq_pullback_ratio_c5s", "nq_pullback_ratio_confirm"):
        def med(pool):
            vals = [f(r, k) for r in pool]
            vals = [x for x in vals if x is not None]
            return f"{st.median(vals):.3f}" if vals else "n/a"
        lines.append(f"- {k}: PC {med(pc)} | cont {med([r for r in rows if (f(r,'ret10s') or 0) > 5])} "
                     f"| TRUE {med([r for r in tagged if r['lst_tag']=='TRUE'])} "
                     f"| FALSE {med([r for r in tagged if r['lst_tag']=='FALSE'])}")
    lines += ["", "Gate: PC fade must beat ALL fade and FALSE fade must beat TRUE fade "
              "late; stall features are causal at +5s only."]
    (BASE / "features/pitch_catch.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
