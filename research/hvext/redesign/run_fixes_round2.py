"""Round-2 fixes: per-event retest risk (flat-only intact), P6 swing, MBO absorber."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from research.hvext.redesign import run_research_fixes as F
from research.hvext.redesign import runner as R

HOLDOUT = F.HOLDOUT
FREEZE_RP = F.FREEZE_RP


def retest_block() -> dict:
    print("=== Retest freeze — per-event risk, single sim (flat-only on) ===", flush=True)
    sep, st = R.run_month(R.SEP, R.PANEL, entry="retest", retest=FREEZE_RP)
    oct_, ot = R.run_month(R.OCT, R.PANEL, entry="retest", retest=FREEZE_RP)
    hold, ht = R.run_month(HOLDOUT, R.PANEL, entry="retest", retest=FREEZE_RP)
    both = R.pack(st + ot)
    print(
        f"  Sep n={sep['n']} net={sep.get('net')} wr={sep.get('wr')} | "
        f"Oct n={oct_['n']} net={oct_.get('net')} wr={oct_.get('wr')} | "
        f"Both n={both['n']} net={both.get('net')} wr={both.get('wr')} pf={both.get('pf')}"
    )
    print(
        f"  Holdout n={hold['n']} net={hold.get('net')} wr={hold.get('wr')} "
        f"days+={hold.get('days_pos')}/{hold.get('n_sessions')}"
    )
    mm_is = F._mae_mfe_for_trades(st + ot)
    mm_oos = F._mae_mfe_for_trades(ht)
    print(
        f"  IS  MAE med={mm_is['mae'].get('median')} MFE med={mm_is['mfe'].get('median')} "
        f"MFE>MAE={mm_is.get('mfe_gt_mae_frac')}"
    )
    print(
        f"  OOS MAE med={mm_oos['mae'].get('median')} MFE med={mm_oos['mfe'].get('median')} "
        f"MFE>MAE={mm_oos.get('mfe_gt_mae_frac')}"
    )
    # Reference: uniform legacy
    risk = FREEZE_RP.retest_pts + FREEZE_RP.stop_beyond_pts
    ubfn = lambda _sv, tp=FREEZE_RP.tp_mult * risk, sl=risk: (tp, sl)
    sep_u, stu = R.run_month(R.SEP, R.PANEL, entry="retest", retest=FREEZE_RP, bracket_fn=ubfn)
    oct_u, otu = R.run_month(R.OCT, R.PANEL, entry="retest", retest=FREEZE_RP, bracket_fn=ubfn)
    both_u = R.pack(stu + otu)
    print(f"  (legacy uniform risk: both n={both_u['n']} net={both_u.get('net')})")
    return {
        "sep": sep,
        "oct": oct_,
        "both": both,
        "both_net": both.get("net"),
        "holdout": hold,
        "mae_mfe_insample": mm_is,
        "mae_mfe_holdout": mm_oos,
        "legacy_uniform_both": both_u,
    }


def mbo_block() -> dict:
    print("\n=== MBO absorber (rest/absorbed joined with trades) ===", flush=True)
    res = F.run_p2_mbo()
    # Feature coverage
    rest_vals, abs_vals = [], []
    for sid in R.ALL:
        ev = R.cached_events(sid, R.PANEL, enrich=True, with_mbo=True)
        if ev.num_rows == 0:
            continue
        rest_vals.extend(ev.column("mbo_tip_max_resting").to_pylist())
        abs_vals.extend(ev.column("mbo_tip_filled").to_pylist())
    rv = np.array([v for v in rest_vals if v is not None and np.isfinite(v)])
    av = np.array([v for v in abs_vals if v is not None and np.isfinite(v)])
    cov = {
        "n_with_mbo": int(len(rv)),
        "rest_p50": float(np.percentile(rv, 50)) if len(rv) else None,
        "rest_p90": float(np.percentile(rv, 90)) if len(rv) else None,
        "absorbed_p50": float(np.percentile(av, 50)) if len(av) else None,
        "absorbed_p90": float(np.percentile(av, 90)) if len(av) else None,
        "absorbed_gt0_frac": float(np.mean(av > 0)) if len(av) else None,
    }
    print("  coverage:", json.dumps(cov))
    res["coverage"] = cov
    return res


def main() -> int:
    R.clear_cache()
    rt = retest_block()
    print("\n=== P6 fixed swing ===", flush=True)
    p6 = F.run_p6_fixed()
    mb = mbo_block()

    payload = {"retest": rt, "p6": p6, "mbo": mb}
    dest = Path("results/EXP-0038/research_fixes_round2.json")
    dest.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {dest}")

    best_p6 = p6["ranked"][0]
    best_mbo = mb["ranked"][0]
    R.append_summary(
        "## Research fixes round 2 (per-event risk / P6 swing / MBO absorber)\n\n"
        f"- Retest freeze, per-event risk with flat-only intact: "
        f"Sep n={rt['sep']['n']} net={rt['sep'].get('net')} / "
        f"Oct n={rt['oct']['n']} net={rt['oct'].get('net')} / "
        f"both={rt.get('both_net')} wr={rt['both'].get('wr')} "
        f"(legacy uniform both={rt['legacy_uniform_both'].get('net')}); "
        f"holdout n={rt['holdout']['n']} net={rt['holdout'].get('net')} "
        f"wr={rt['holdout'].get('wr')}.\n"
        f"- MAE/MFE med IS {rt['mae_mfe_insample']['mae'].get('median')}/"
        f"{rt['mae_mfe_insample']['mfe'].get('median')}, OOS "
        f"{rt['mae_mfe_holdout']['mae'].get('median')}/"
        f"{rt['mae_mfe_holdout']['mfe'].get('median')}.\n"
        f"- P6 (tip's own swing excluded) best: `{best_p6['label']}` "
        f"both={best_p6.get('both_net')} n={best_p6['sep'].get('n')}/{best_p6['oct'].get('n')}.\n"
        f"- MBO absorber best: `{best_mbo['label']}` both={best_mbo.get('both_net')} "
        f"n={best_mbo['sep'].get('n')}/{best_mbo['oct'].get('n')}; "
        f"coverage absorbed>0 frac={mb['coverage'].get('absorbed_gt0_frac')}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
