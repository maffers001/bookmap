"""Test clearance / pullback / leave-behind as LST continuation discriminators.

User hyp: runners trade past the dominant resting size and leave it behind
(passive trapped); reversals lose momentum and pull back into/through it.

Metrics (NQ primary; both-market flags where noted):
  clear_*_pts      signed distance past max level at stamp
  peak_past_*_pts  max distance past max level by stamp
  pullback_*_pts   drawdown from peak within the window
  left_behind_*    1 if cleared >=1 pt past and never returned to/through level
  both_left_behind_* / both_cleared_*  ES+NQ agree

Stamps: confirm (causal at our entry stamp), c2s/c5s (later clearance confirm).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from research.momentum_slice.analyse_labels import load
from research.momentum_slice.analyse_tape_speed import stats

BASE = Path("results/MOMENTUM_SLICE")
NUM = (
    "nq_clear_confirm_pts", "nq_peak_past_confirm_pts", "nq_pullback_confirm_pts",
    "nq_clear_c2s_pts", "nq_peak_past_c2s_pts", "nq_pullback_c2s_pts",
    "nq_clear_c5s_pts", "nq_peak_past_c5s_pts", "nq_pullback_c5s_pts",
    "nq_left_behind_confirm", "nq_left_behind_c2s", "nq_left_behind_c5s",
    "both_left_behind_confirm", "both_left_behind_c2s", "both_left_behind_c5s",
    "both_cleared_confirm", "both_cleared_c2s", "both_cleared_c5s",
    "nq_cleared_confirm", "nq_cleared_c2s", "nq_cleared_c5s",
    "ret10s", "ret60s", "mfe", "mae",
)


def main():
    rows = load(BASE / "features/features.csv")
    for r in rows:
        for k in NUM:
            if k in r and r[k] not in ("", None):
                try:
                    r[k] = float(r[k])
                except ValueError:
                    pass
    lines = ["# Clearance / pullback / leave-behind test (exploratory)", "",
             "Dominant resting level from ignition−3 s book; clearance = signed "
             "pts past that level. left_behind = cleared ≥1 pt and never returned "
             "to/through the level by the stamp.", ""]

    for stamp, note in (
        ("confirm", "causal at confirm entry"),
        ("c2s", "2 s after confirm — later clearance filter"),
        ("c5s", "5 s after confirm"),
    ):
        lines.append(f"## stamp={stamp} ({note})")
        for key, yes, no in (
            (f"nq_left_behind_{stamp}", 1, 0),
            (f"both_left_behind_{stamp}", 1, 0),
            (f"nq_cleared_{stamp}", 1, 0),
            (f"both_cleared_{stamp}", 1, 0),
        ):
            if key not in rows[0]:
                continue
            a = [r for r in rows if r.get(key) == yes]
            b = [r for r in rows if r.get(key) == no]
            lines.append(f"- {key}=1: {stats(a)}")
            lines.append(f"  - late: {stats([r for r in a if r.get('tod') == 'late'])}")
            lines.append(f"- {key}=0: {stats(b)}")
            lines.append(f"  - late: {stats([r for r in b if r.get('tod') == 'late'])}")

        # pullback terciles among those that cleared
        cleared = [r for r in rows if r.get(f"nq_cleared_{stamp}") == 1
                   and isinstance(r.get(f"nq_pullback_{stamp}_pts"), float)]
        if len(cleared) >= 9:
            vals = sorted(r[f"nq_pullback_{stamp}_pts"] for r in cleared)
            q1, q2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
            for name, sel in (
                ("low pullback", [r for r in cleared if r[f"nq_pullback_{stamp}_pts"] <= q1]),
                ("mid pullback", [r for r in cleared if q1 < r[f"nq_pullback_{stamp}_pts"] <= q2]),
                ("high pullback", [r for r in cleared if r[f"nq_pullback_{stamp}_pts"] > q2]),
            ):
                lines.append(f"- cleared & {name}: {stats(sel)}")
                lines.append(f"  - late: {stats([r for r in sel if r.get('tod') == 'late'])}")
        lines.append("")

    # Interaction: left behind AND low pullback at confirm
    if all(k in rows[0] for k in ("nq_left_behind_confirm", "nq_pullback_confirm_pts")):
        pb = [r["nq_pullback_confirm_pts"] for r in rows
              if isinstance(r.get("nq_pullback_confirm_pts"), float)]
        med_pb = float(np.median(pb)) if pb else 0.0
        runners = [r for r in rows if r.get("nq_left_behind_confirm") == 1
                   and isinstance(r.get("nq_pullback_confirm_pts"), float)
                   and r["nq_pullback_confirm_pts"] <= med_pb]
        traps = [r for r in rows if r.get("nq_left_behind_confirm") == 0
                 or (isinstance(r.get("nq_pullback_confirm_pts"), float)
                     and r["nq_pullback_confirm_pts"] > med_pb)]
        lines += ["## Interaction: left-behind + low pullback (confirm)", "",
                  f"median pullback among all = {med_pb:.1f} pts",
                  f"- runners (left behind & pullback≤med): {stats(runners)}",
                  f"  - late: {stats([r for r in runners if r.get('tod') == 'late'])}",
                  f"- rest: {stats(traps)}",
                  f"  - late: {stats([r for r in traps if r.get('tod') == 'late'])}", ""]

    # Continuers vs reversers feature medians
    cont = [r for r in rows if r["ret10s"] > 5]
    rev = [r for r in rows if r["ret10s"] < -5]
    lines += ["## Feature medians: continuers (ret10s>5) vs reversers (ret10s<-5)", ""]
    for k in ("nq_clear_confirm_pts", "nq_peak_past_confirm_pts", "nq_pullback_confirm_pts",
              "nq_left_behind_confirm", "nq_clear_c2s_pts", "nq_pullback_c2s_pts",
              "nq_left_behind_c2s", "both_left_behind_c2s"):
        if k not in rows[0]:
            continue
        def med(xs, key=k):
            vals = [x[key] for x in xs if isinstance(x.get(key), (int, float))]
            return float(np.median(vals)) if vals else float("nan")
        lines.append(f"- {k}: cont {med(cont):.2f}  rev {med(rev):.2f}")

    (BASE / "features/clearance.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
