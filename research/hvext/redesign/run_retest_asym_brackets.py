"""Asymmetric TP/SL on freeze retest entry (Sep+Oct + holdout).

Entry: panel HV fire → retest R10 / 300s / stop-beyond used only for *fill*
timing (tip proximity). Brackets are overridden by the sweep cells.
"""
from __future__ import annotations

import json
from pathlib import Path

from research.backtest.mat_sim import make_vol_bracket_rr
from research.hvext.redesign import runner as R

HOLDOUT = ["030826", "040826", "050826", "260326"]

# Freeze fill geometry (brackets overridden below)
RP = R.RetestParams(
    retest_pts=10, timeout_sec=300, stop_beyond_pts=2, tp_mult=3.0
)


def fixed(tp: float, sl: float):
    return lambda _sv, tp=tp, sl=sl: (tp, sl)


def main() -> int:
    cfg = R.PANEL
    cells: list[tuple[str, object]] = [
        # tip-risk baseline (no override) — same as freeze
        ("tip_risk_x3", None),
        # fixed asymmetric
        ("fixed_TP30_SL10", fixed(30, 10)),
        ("fixed_TP30_SL12.5", fixed(30, 12.5)),
        ("fixed_TP25_SL10", fixed(25, 10)),
        ("fixed_TP20_SL10", fixed(20, 10)),
        ("fixed_TP15_SL10", fixed(15, 10)),
        ("fixed_TP40_SL10", fixed(40, 10)),
        ("fixed_TP30_SL8", fixed(30, 8)),
        ("fixed_TP30_SL15", fixed(30, 15)),
        # vol asymmetric (k=1, medR30 unit)
        ("vol_k1_1R_2R", make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0)),
        ("vol_k1_1R_2.5R", make_vol_bracket_rr(1.0, tp_r=2.5, sl_r=1.0)),
        ("vol_k1_1R_3R", make_vol_bracket_rr(1.0, tp_r=3.0, sl_r=1.0)),
        ("vol_k1_1R_1.5R", make_vol_bracket_rr(1.0, tp_r=1.5, sl_r=1.0)),
        ("vol_k0.75_1R_2.5R", make_vol_bracket_rr(0.75, tp_r=2.5, sl_r=1.0)),
        ("vol_k1.25_1R_2.5R", make_vol_bracket_rr(1.25, tp_r=2.5, sl_r=1.0)),
        ("vol_k1_0.75R_2R", make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=0.75)),
    ]

    rows = []
    print("Freeze retest fill R10/t300 | asymmetric bracket sweep | Sep+Oct + holdout")
    for label, bfn in cells:
        print(f"\n=== {label} ===", flush=True)
        kwargs = dict(entry="retest", retest=RP)
        if bfn is not None:
            kwargs["bracket_fn"] = bfn
        sep, sep_tr = R.run_month(R.SEP, cfg, **kwargs)
        oct_, oct_tr = R.run_month(R.OCT, cfg, **kwargs)
        hold, hold_tr = R.run_month(HOLDOUT, cfg, **kwargs)
        both = R.pack(sep_tr + oct_tr)
        print(
            f"  Sep n={sep['n']} net={sep.get('net'):+g} wr={sep.get('wr')} pf={sep.get('pf')}  "
            f"Oct n={oct_['n']} net={oct_.get('net'):+g} wr={oct_.get('wr')} pf={oct_.get('pf')}  "
            f"Both={both.get('net'):+g} wr={both.get('wr')}"
        )
        print(
            f"  Holdout n={hold['n']} net={hold.get('net'):+g} wr={hold.get('wr')} "
            f"pf={hold.get('pf')} days+={hold.get('days_pos')}/{hold.get('n_sessions')}"
        )
        rows.append(
            {
                "label": label,
                "sep": sep,
                "oct": oct_,
                "both": both,
                "both_net": both.get("net"),
                "holdout": hold,
            }
        )

    ranked = sorted(rows, key=lambda r: (r.get("both_net") or -1e9), reverse=True)
    print("\n========== RANKED by Sep+Oct both_net ==========")
    print(
        f"{'label':22s} {'Sep n':>5} {'Sep net':>9} {'Sep WR':>7} "
        f"{'Oct n':>5} {'Oct net':>9} {'Oct WR':>7} "
        f"{'Both':>9} {'Hold n':>6} {'Hold net':>9} {'Hold WR':>7}"
    )
    for r in ranked:
        print(
            f"{r['label']:22s} "
            f"{r['sep']['n']:5d} {r['sep'].get('net'):+9.1f} {r['sep'].get('wr') or 0:7.3f} "
            f"{r['oct']['n']:5d} {r['oct'].get('net'):+9.1f} {r['oct'].get('wr') or 0:7.3f} "
            f"{r['both_net']:+9.1f} "
            f"{r['holdout']['n']:6d} {r['holdout'].get('net'):+9.1f} "
            f"{(r['holdout'].get('wr') or 0):7.3f}"
        )

    dest = Path("results/EXP-0038/retest_asym_brackets.json")
    dest.write_text(json.dumps({"ranked": ranked, "rows": rows}, indent=2, default=str))
    print(f"\nWrote {dest}")

    # append summary section
    best = ranked[0]
    hold_best = max(rows, key=lambda r: (r["holdout"].get("net") or -1e9))
    R.append_summary(
        "## Retest × asymmetric brackets\n\n"
        f"- Best Sep+Oct: `{best['label']}` both={best.get('both_net')} "
        f"(Sep {best['sep'].get('net')}, Oct {best['oct'].get('net')}; "
        f"WR {best['both'].get('wr')}).\n"
        f"- Best holdout net: `{hold_best['label']}` "
        f"n={hold_best['holdout'].get('n')} wr={hold_best['holdout'].get('wr')} "
        f"net={hold_best['holdout'].get('net')}.\n"
        f"- Tip-risk ×3 freeze baseline both={next(r['both_net'] for r in rows if r['label']=='tip_risk_x3')}.\n"
        f"- Wrote `{dest.as_posix()}`.\n\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
