"""Refined tape-speed test: BASE speed (ign-300s..-30s) instead of immediate tape.

Hypotheses (post-hoc, from analyse_tape_speed):
  H1 continuation: fast/broad base -> slice continues.
  H2 exclusion/fade flag: slow base + concentrated (one-order) burst ->
     absorbed/reverses; removing these should improve the remaining
     continuation set, and fading them (at confirm, upper bound only)
     should be positive.
Splits shown overall and late-only (post-14:55 BST), where the effect
appeared before.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from research.momentum_slice.analyse_labels import load
from research.momentum_slice.analyse_tape_speed import stats, terciles

BASE = Path("results/MOMENTUM_SLICE")
KEYS = ("min_base_rel", "min_base_tps", "nq_base_tps", "nq_base_rel_speed", "max_order_share")


def main():
    rows = load(BASE / "features/features.csv")
    for r in rows:
        for k in KEYS:
            if k in r and r[k] not in ("", None):
                r[k] = float(r[k])
    lines = ["# Base-speed hypothesis test (post-hoc, exploratory)", "",
             "base = trades/sec over [ignition-300s, ignition-30s]; base_rel vs "
             "30 s blocks over the preceding 10 min. min_* = slower of NQ/ES.", ""]
    for key in ("min_base_rel", "min_base_tps", "nq_base_tps"):
        lines.append(f"## split by {key}")
        for name, sel in terciles(rows, key):
            lines.append(f"- {name}: {stats(sel)}")
            lines.append(f"  - late-only: {stats([r for r in sel if r.get('tod') == 'late'])}")
        lines.append("")
    med_rel = float(np.nanmedian([r["min_base_rel"] for r in rows]))
    med_share = float(np.nanmedian([r["max_order_share"] for r in rows if isinstance(r.get("max_order_share"), float)]))
    for r in rows:
        if not isinstance(r.get("max_order_share"), float):
            r["max_order_share"] = float(r["max_order_share"])
    flag = [r for r in rows if r["min_base_rel"] < med_rel and r["max_order_share"] > med_share]
    keep = [r for r in rows if r not in flag]
    fast_broad = [r for r in rows if r["min_base_rel"] >= med_rel and r["max_order_share"] <= med_share]
    lines += ["## Exclusion / fade flag: slow base + concentrated burst", "",
              f"flag definition: min_base_rel < {med_rel:.2f} AND max_order_share > {med_share:.2f}", "",
              f"- FLAGGED (fade candidates): {stats(flag)}",
              f"  - late-only: {stats([r for r in flag if r.get('tod') == 'late'])}",
              f"  - fade-side @10s (enter at confirm, upper bound): "
              f"{-np.mean([r['ret10s'] for r in flag]):+.1f} mean over n={len(flag)}",
              f"- KEPT (continuation set after exclusion): {stats(keep)}",
              f"  - late-only: {stats([r for r in keep if r.get('tod') == 'late'])}",
              f"- fast+broad base (H1): {stats(fast_broad)}",
              f"  - late-only: {stats([r for r in fast_broad if r.get('tod') == 'late'])}",
              f"- ALL (reference): {stats(rows)}",
              f"  - late-only: {stats([r for r in rows if r.get('tod') == 'late'])}", "",
              "Verdict guide: H2 supported if FLAGGED reverses (ret10s clearly negative, "
              "esp. late) AND KEPT beats ALL late. H1 supported if fast+broad beats ALL late. "
              "Medians are in-sample split points — any rule needs frozen thresholds + holdout."]
    (BASE / "features/base_speed.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
