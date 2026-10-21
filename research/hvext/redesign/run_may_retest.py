"""May 2026 OOS: tip-proximal retest (R5 peak_band) vs freeze R10.

May was not used in EXP-0038 Sep/Oct/holdout. Same fill rule as
retest_near_tip: timeout 300s, stopBeyond 2, tpMult 3, per-event tip risk.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from research.hvext.redesign import runner as R
from research.hvext.redesign.run_research_fixes import _mae_mfe_for_trades

MAY = [
    "010526", "040526", "050526", "070526", "080526",
    "110526", "120526", "130526", "140526", "150526",
    "180526", "190526", "200526", "210526", "260526",
    "280526", "290526",
]
N_SEEDS = 10


def run_rp(label: str, rp: R.RetestParams) -> dict:
    print(f"\n--- {label} ---", flush=True)
    pk, tr = R.run_month(MAY, R.PANEL, entry="retest", retest=rp)
    nul = R.null_retest_paired(MAY, pk, rp, R.PANEL, n_seeds=N_SEEDS)
    nul_s = {k: v for k, v in nul.items() if k != "runs"}
    mm = _mae_mfe_for_trades(tr)
    risks = [t.sl_pts for t in tr]
    n = pk.get("n") or 0
    net = pk.get("net") or 0
    exp = net / n if n else 0.0
    day_nets = {}
    for t in tr:
        day_nets[t.session_id] = day_nets.get(t.session_id, 0.0) + (t.pnl - 0.25)
    days_pos = sum(1 for v in day_nets.values() if v > 0)
    print(
        f"  May n={n} net={net} wr={pk.get('wr')} fill={pk.get('fill_rate')} "
        f"exp/t={exp:+.2f} riskMed={float(np.median(risks)) if risks else None} "
        f"days+={days_pos}/{len(day_nets)} fires={pk.get('n_fires_gated')}"
    )
    print(
        f"  null n~{nul['n_mean']:.0f} net {nul['net_mean']:+.1f}±{nul['net_sd']:.0f} "
        f"P(null>=cell)={nul['frac_null_ge_cell']:.2f} z={nul.get('z')}"
    )
    print(
        f"  MAE med={mm['mae'].get('median')} MFE med={mm['mfe'].get('median')} "
        f"MFE>MAE={mm.get('mfe_gt_mae_frac')}"
    )
    daily = [
        {"sid": sid, "net": round(day_nets.get(sid, 0.0), 2),
         "n": sum(1 for t in tr if t.session_id == sid)}
        for sid in MAY
    ]
    return {
        "label": label,
        "rp": rp.__dict__,
        "cell": pk,
        "null": nul_s,
        "exp_per_trade": exp,
        "risk_pts_median": float(np.median(risks)) if risks else None,
        "mae_mfe": mm,
        "days_pos": days_pos,
        "n_days_traded": len(day_nets),
        "daily": daily,
    }


def main() -> int:
    R.clear_cache()
    print(f"=== May 2026 OOS retest ({len(MAY)} sessions) ===", flush=True)
    cells = []
    for r, tag in ((5.0, "peak_band"), (10.0, "freeze"), (2.5, "tight"), (7.5, "mid")):
        rp = R.RetestParams(
            retest_pts=r, timeout_sec=300, stop_beyond_pts=2, tp_mult=3.0
        )
        cells.append(run_rp(f"R{r:g}_t300_sb2_x3_{tag}", rp))

    dest = Path("results/EXP-0038/may_retest_near_tip.json")
    dest.write_text(json.dumps({"stage": "may_retest_near_tip", "sessions": MAY, "cells": cells},
                               indent=2, default=str))
    print(f"\nWrote {dest}")

    r5 = next(c for c in cells if "peak_band" in c["label"])
    r10 = next(c for c in cells if "freeze" in c["label"])
    lines = [
        "## May 2026 OOS — tip-proximal retest vs freeze\n",
        f"- {len(MAY)} May sessions (held out of Sep/Oct/prior holdout). "
        f"Same rule: timeout 300 / sb2 / ×3 / per-event tip risk; null {N_SEEDS} seeds.\n",
    ]
    for c in cells:
        pk, nl = c["cell"], c["null"]
        lines.append(
            f"- `{c['label']}`: n={pk.get('n')} net={pk.get('net')} wr={pk.get('wr')} "
            f"fill={pk.get('fill_rate')} exp/t={c['exp_per_trade']:+.2f} "
            f"riskMed={c['risk_pts_median']} days+={c['days_pos']}/{c['n_days_traded']} | "
            f"null {nl['net_mean']:+.0f}±{nl['net_sd']:.0f} P={nl['frac_null_ge_cell']:.2f} "
            f"z={nl.get('z')} | MAE/MFE med "
            f"{c['mae_mfe']['mae'].get('median')}/{c['mae_mfe']['mfe'].get('median')}"
        )
    lines.append(
        f"- R5 vs freeze on May: net {(r5['cell'].get('net') or 0) - (r10['cell'].get('net') or 0):+.1f}; "
        f"exp/t {(r5['exp_per_trade']) - (r10['exp_per_trade']):+.2f}.\n"
        f"- Wrote `{dest.as_posix()}`.\n"
    )
    R.append_summary("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
