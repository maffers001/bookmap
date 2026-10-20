"""Stage 0 - plumbing: per-event retest risk (flat-only intact), P6 swing fix,
and the paired random-null retest control.

Writes results/EXP-0038/stage0_plumbing.json and appends to summary.md.
"""
from __future__ import annotations

import json
from pathlib import Path

from research.hvext.redesign import run_research_fixes as F
from research.hvext.redesign import runner as R

HOLDOUT = F.HOLDOUT
FREEZE_RP = F.FREEZE_RP


def _cell(sessions, label):
    pk, tr = R.run_month(sessions, R.PANEL, entry="retest", retest=FREEZE_RP)
    nul = R.null_retest_paired(sessions, pk, FREEZE_RP, R.PANEL, n_seeds=20)
    print(
        f"  {label:8s} cell n={pk['n']:3d} net={pk.get('net'):>8} wr={pk.get('wr')} "
        f"fill={pk.get('fill_rate'):.2f} | null(n/sess={nul['n_per_session']}) "
        f"n~{nul['n_mean']:.0f} net mean={nul['net_mean']:.1f} "
        f"[{nul['net_min']:.1f},{nul['net_max']:.1f}] wr~{nul['wr_mean']:.3f} "
        f"fill~{nul['fill_rate_mean']:.2f} | cell-null={nul['cell_minus_null_mean']:+.1f} "
        f"beats_max={nul['beats_null_max']}"
    )
    return {"cell": pk, "null": nul}, tr


def main() -> int:
    R.clear_cache()
    print("=== Stage 0: freeze retest (R10/300s/sb2/x3, per-event risk) vs paired null ===")
    sep, st = _cell(R.SEP, "Sep")
    oct_, ot = _cell(R.OCT, "Oct")
    hold, _ = _cell(HOLDOUT, "Holdout")
    both = R.pack(st + ot)
    both_null_mean = sep["null"]["net_mean"] + oct_["null"]["net_mean"]
    both_null_max = sep["null"]["net_max"] + oct_["null"]["net_max"]
    print(
        f"  Both     cell n={both['n']} net={both.get('net')} wr={both.get('wr')} | "
        f"null mean={both_null_mean:.1f} max={both_null_max:.1f}"
    )

    # Uniform-risk legacy variant, same null treatment (bracket_fn override)
    risk = FREEZE_RP.retest_pts + FREEZE_RP.stop_beyond_pts
    ubfn = lambda _sv, tp=FREEZE_RP.tp_mult * risk, sl=risk: (tp, sl)
    print("\n=== Uniform-risk variant (TP 36 / SL 12) vs paired null ===")
    uni = {}
    for label, sess in (("Sep", R.SEP), ("Oct", R.OCT), ("Holdout", HOLDOUT)):
        pk, _ = R.run_month(sess, R.PANEL, entry="retest", retest=FREEZE_RP, bracket_fn=ubfn)
        nul = R.null_retest_paired(sess, pk, FREEZE_RP, R.PANEL, n_seeds=20, bracket_fn=ubfn)
        print(
            f"  {label:8s} cell n={pk['n']:3d} net={pk.get('net'):>8} wr={pk.get('wr')} | "
            f"null net mean={nul['net_mean']:.1f} [{nul['net_min']:.1f},{nul['net_max']:.1f}] "
            f"wr~{nul['wr_mean']:.3f} | cell-null={nul['cell_minus_null_mean']:+.1f} "
            f"beats_max={nul['beats_null_max']}"
        )
        uni[label.lower()] = {"cell": pk, "null": nul}

    payload = {
        "stage": "stage0_plumbing",
        "retest_params": FREEZE_RP.__dict__,
        "per_event_risk": {
            "sep": sep,
            "oct": oct_,
            "holdout": hold,
            "both": both,
            "both_null_mean": both_null_mean,
            "both_null_max": both_null_max,
        },
        "uniform_risk": uni,
        "notes": [
            "Per-event tp_pts/sl_pts consumed inside simulate_session; flat-only + 30s cooldown intact.",
            "P6 prior_swing excludes the tip's own swing (see research_fixes_round2.json).",
            "Null = random RTH fire times, random side, synthetic tip move_pts against side; same fill rule.",
        ],
    }
    dest = Path("results/EXP-0038/stage0_plumbing.json")
    dest.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {dest}")

    def line(label, d):
        c, n = d["cell"], d["null"]
        return (
            f"  - {label}: cell n={c['n']} net={c.get('net')} wr={c.get('wr')}; "
            f"null n~{n['n_mean']:.0f} net {n['net_mean']:.1f} [{n['net_min']:.1f}, {n['net_max']:.1f}] "
            f"wr~{n['wr_mean']:.3f}; cell-null {n['cell_minus_null_mean']:+.1f}; "
            f"beats null max: {n['beats_null_max']}"
        )

    R.append_summary(
        "## Stage 0 - plumbing (per-event risk, P6 swing, paired null retest)\n\n"
        "- `simulate_session` consumes per-event `tp_pts`/`sl_pts`; retest runs as a single "
        "sim per session (flat-only + cooldown intact). P6 prior_swing excludes the tip's own swing. "
        "New `runner.random_null_retest_events` / `null_retest_month` / `null_retest_paired`: "
        "random fires + synthetic tip at move_pts, identical fill rule, 20 seeds.\n"
        "- Freeze retest, per-event risk:\n"
        + line("Sep", sep) + "\n" + line("Oct", oct_) + "\n" + line("Holdout", hold) + "\n"
        f"  - Both: cell n={both['n']} net={both.get('net')} wr={both.get('wr')}; "
        f"null mean {both_null_mean:.1f}, max {both_null_max:.1f}.\n"
        "- Uniform risk (TP36/SL12):\n"
        + line("Sep", uni["sep"]) + "\n" + line("Oct", uni["oct"]) + "\n" + line("Holdout", uni["holdout"]) + "\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

