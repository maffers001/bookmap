"""Test the tape-speed hypothesis on the 76 LST candidates.

Splits by pre-ignition relative tape speed (min of NQ/ES, 30 s rate vs prior
10 min baseline) and by burst concentration (max single aggressor-order share
of burst volume): broad/fast participation should continue; slow tape + one
big order should absorb/reverse. Reversal here = candidate-side ret10s < -5,
which is also the raw material for pitch-and-catch (fade) setups.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from research.momentum_slice.analyse_labels import load

BASE = Path("results/MOMENTUM_SLICE")


def stats(rows):
    if not rows:
        return "n=0"
    r10 = np.array([r["ret10s"] for r in rows], float)
    r60 = np.array([r["ret60s"] for r in rows], float)
    return (f"n={len(rows)} ret10s {r10.mean():+.1f}/{np.median(r10):+.1f} ret60s {r60.mean():+.1f} "
            f"P(cont>5) {(r10 > 5).mean():.2f} P(rev<-5) {(r10 < -5).mean():.2f} "
            f"MFE {np.mean([r['mfe'] for r in rows]):.0f} MAE {np.mean([r['mae'] for r in rows]):.0f}")


def terciles(rows, key):
    vals = sorted(r[key] for r in rows if isinstance(r.get(key), float))
    if len(vals) < 9:
        return []
    q1, q2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    return [("low", [r for r in rows if r.get(key, 1e18) <= q1]),
            ("mid", [r for r in rows if q1 < r.get(key, 1e18) <= q2]),
            ("high", [r for r in rows if r.get(key, 1e18) > q2])]


def main():
    rows = load(BASE / "features/features.csv")
    extra = ("min_rel_speed", "nq_rel_speed_30s", "es_rel_speed_30s", "max_order_share",
             "nq_tps_30s", "es_tps_30s", "nq_burst_max_order_share", "es_burst_max_order_share")
    for r in rows:
        for k in extra:
            if k in r and r[k] not in ("", None):
                r[k] = float(r[k])
        r["reversal"] = 1.0 if r["ret10s"] < -5 else 0.0
    lines = ["# Tape-speed hypothesis test (76 candidates, exploratory)", "",
             "rel_speed = 30 s trades/sec before ignition / median 30 s rate, prior 10 min. "
             "min_rel_speed = slower of NQ/ES. max_order_share = largest single aggressor "
             "order's share of burst volume (both markets, max).", ""]
    for key in ("min_rel_speed", "nq_rel_speed_30s", "max_order_share", "nq_tps_30s"):
        lines.append(f"## split by {key}")
        for name, sel in terciles(rows, key):
            lines.append(f"- {name}: {stats(sel)}")
            lines.append(f"  - late-only: {stats([r for r in sel if r.get('tod') == 'late'])}")
        lines.append("")
    lines += ["## Interaction: tape speed x burst concentration", ""]
    med_speed = float(np.median([r["min_rel_speed"] for r in rows]))
    med_share = float(np.median([r["max_order_share"] for r in rows]))
    quads = {
        "fast+broad (hyp: continuation)": [r for r in rows if r["min_rel_speed"] >= med_speed and r["max_order_share"] <= med_share],
        "slow+one-order (hyp: absorption/reversal)": [r for r in rows if r["min_rel_speed"] < med_speed and r["max_order_share"] > med_share],
        "fast+one-order": [r for r in rows if r["min_rel_speed"] >= med_speed and r["max_order_share"] > med_share],
        "slow+broad": [r for r in rows if r["min_rel_speed"] < med_speed and r["max_order_share"] <= med_share],
    }
    for name, sel in quads.items():
        lines.append(f"- {name}: {stats(sel)}")
        lines.append(f"  - late-only: {stats([r for r in sel if r.get('tod') == 'late'])}")
    lines += ["", "## Absorbed set (candidate-side ret10s < -5): pitch-and-catch candidates",
              "If faded, these returned +|ret| in the opposite direction; check "
              "`mae` timing in a dedicated fade labelling before trading this.", ""]
    absorbed = [r for r in rows if r["ret10s"] < -5]
    lines.append(f"- absorbed/reversed: {stats(absorbed)} (fade-side mean would be "
                 f"{-np.mean([r['ret10s'] for r in absorbed]):+.1f} @10s if entered at confirm — "
                 "NOT causal as a fade signal: absorption is only known after the stall)")
    lines.append(f"- their median min_rel_speed "
                 f"{np.median([r['min_rel_speed'] for r in absorbed]):.2f} vs continuers "
                 f"{np.median([r['min_rel_speed'] for r in rows if r['ret10s'] > 5]):.2f}; "
                 f"median max_order_share {np.median([r['max_order_share'] for r in absorbed]):.2f} vs "
                 f"{np.median([r['max_order_share'] for r in rows if r['ret10s'] > 5]):.2f}")
    (BASE / "features/tape_speed.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
