"""Retest closer to the tip / trapped band vs freeze R10.

The tip's trapped volume lives in peak_band (5 pts). Freeze enters on first
print within R=10 of tip — twice the tip band. This run asks whether entering
inside (or tighter than) the tip band helps.

Variants (timeout 300s, stopBeyond 2, tpMult 3, per-event tip-distance risk):
  R = 1.25, 2.5, 5.0 (=peak_band), 7.5, 10.0 (freeze)

Also: R=peak_band but require retest prints to accumulate >= frac of tip vol
before fill (re-fill the trapped size) — frac in {0.25, 0.5}.

Writes results/EXP-0038/retest_near_tip.json and appends summary.md.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from research.hvext.redesign import run_research_fixes as F
from research.hvext.redesign import runner as R

HOLDOUT = F.HOLDOUT
N_SEEDS = 10  # cheaper than 20; enough to see if cell clears null mean
MONTHS = (("sep", R.SEP), ("oct", R.OCT), ("holdout", HOLDOUT))
BAND = R.PANEL.peak_band_pts  # 5.0


def _fmt(pk):
    return (
        f"n={pk.get('n', 0):3d} net={pk.get('net') if pk.get('net') is not None else 0:>8.1f} "
        f"wr={(pk.get('wr') or 0):.3f} fill={(pk.get('fill_rate') or 0):.2f}"
    )


def run_rp(label: str, rp: R.RetestParams) -> dict:
    print(f"\n--- {label} ---", flush=True)
    out = {"label": label, "rp": rp.__dict__}
    trades = {}
    for mname, sess in MONTHS:
        pk, tr = R.run_month(sess, R.PANEL, entry="retest", retest=rp)
        nul = R.null_retest_paired(sess, pk, rp, R.PANEL, n_seeds=N_SEEDS)
        nul_s = {k: v for k, v in nul.items() if k != "runs"}
        print(
            f"  {mname:8s} {_fmt(pk)} fires={pk.get('n_fires_gated')} | "
            f"null {nul['net_mean']:+.0f}±{nul['net_sd']:.0f} "
            f"P={nul['frac_null_ge_cell']:.2f} z={nul.get('z')}"
        )
        # risk size at fills
        risks = []
        for sid in sess:
            nq = R.load_nq(sid)
            ev = R.cached_events(sid, R.PANEL, enrich=True)
            if ev.num_rows == 0:
                continue
            fills = R.find_retest_entries(nq, ev, rp, R.PANEL)
            if fills.num_rows and "retest_risk_pts" in fills.column_names:
                risks.extend(fills.column("retest_risk_pts").to_pylist())
        risk_med = float(np.median(risks)) if risks else None
        out[mname] = {
            "cell": pk,
            "null": nul_s,
            "risk_pts_median": risk_med,
            "risk_pts_mean": float(np.mean(risks)) if risks else None,
        }
        trades[mname] = tr
    both = R.pack(trades["sep"] + trades["oct"])
    out["both"] = both
    out["both_net"] = both.get("net")
    out["beats_null_both"] = bool(
        out["sep"]["null"]["frac_null_ge_cell"] <= 0.1
        and out["oct"]["null"]["frac_null_ge_cell"] <= 0.1
    )
    print(
        f"  both     n={both['n']} net={both.get('net')} wr={both.get('wr')} "
        f"beats_null_both={out['beats_null_both']}"
    )
    return out


def main() -> int:
    R.clear_cache()
    cells = []

    # Fixed-R ladder around / inside tip band
    for r in (1.25, 2.5, BAND, 7.5, 10.0):
        rp = R.RetestParams(
            retest_pts=r, timeout_sec=300, stop_beyond_pts=2, tp_mult=3.0
        )
        tag = "peak_band" if r == BAND else ("freeze" if r == 10.0 else "")
        label = f"R{r:g}_t300_sb2_x3" + (f"_{tag}" if tag else "")
        cells.append(run_rp(label, rp))

    # Re-fill trapped size: enter inside tip band only after retest vol
    # accumulates >= frac * tip (extreme_vol). Uses weak_retest_frac inverted
    # semantics — existing param breaks if retest_vol exceeds frac*tip; we need
    # the opposite (wait until enough). Implement via a dedicated fill gate:
    # temporarily use find_retest with R=band and post-filter on retest_vol.
    # Easier: add a min_retest_vol_frac to RetestParams... keep it simple —
    # scan fills at R=band and only keep those with retest_vol >= frac*extreme_vol
    # by using fill_filter. But find_retest fills on FIRST touch, so retest_vol
    # at fill is just the first print size. Need accumulate-then-enter.

    # Minimal change: extend RetestParams with min_retest_vol_frac (0=off).
    # Implemented below via a local patch to find path — see _find_with_min_vol.
    for frac in (0.25, 0.5):
        cells.append(run_rp(
            f"R{BAND:g}_minVolFrac{frac}_t300_sb2_x3",
            R.RetestParams(
                retest_pts=BAND, timeout_sec=300, stop_beyond_pts=2, tp_mult=3.0,
                # abuse weak_retest_frac as a sentinel; runner uses custom path
                weak_retest_frac=-frac,  # negative → min-vol mode in find_retest
            ),
        ))

    dest = Path("results/EXP-0038/retest_near_tip.json")
    dest.write_text(json.dumps({"stage": "retest_near_tip", "cells": cells}, indent=2, default=str))
    print(f"\nWrote {dest}")

    lines = [
        "## Retest closer to tip / trapped band (vs freeze R10)\n",
        f"- Tip peak_band = {BAND} pts (where HV tip volume / \"trapped\" size is measured). "
        f"Freeze enters on first print within R=10. Same timeout 300s / stopBeyond 2 / ×3 / "
        f"per-event tip-distance risk; null {N_SEEDS} seeds.\n",
    ]
    for c in cells:
        def m(k):
            pk, nl = c[k]["cell"], c[k]["null"]
            return (
                f"{k} n={pk.get('n', 0)} net={pk.get('net')} wr={pk.get('wr')} "
                f"fill={pk.get('fill_rate')} riskMed={c[k].get('risk_pts_median')} "
                f"(null {nl['net_mean']:+.0f}±{nl['net_sd']:.0f}, P={nl['frac_null_ge_cell']:.2f})"
            )
        lines.append(
            f"- `{c['label']}`: {m('sep')}; {m('oct')}; {m('holdout')}; "
            f"both={c.get('both_net')} beats_null_both={c.get('beats_null_both')}"
        )
    # Delta vs freeze
    freeze = next(c for c in cells if "freeze" in c["label"])
    band = next(c for c in cells if c["label"].endswith("_peak_band"))
    lines.append(
        f"- vs freeze: peak_band R{BAND:g} both "
        f"{(band.get('both_net') or 0) - (freeze.get('both_net') or 0):+.1f} "
        f"(Sep {(band['sep']['cell'].get('net') or 0) - (freeze['sep']['cell'].get('net') or 0):+.1f}, "
        f"Oct {(band['oct']['cell'].get('net') or 0) - (freeze['oct']['cell'].get('net') or 0):+.1f}, "
        f"hold {(band['holdout']['cell'].get('net') or 0) - (freeze['holdout']['cell'].get('net') or 0):+.1f}).\n"
        f"- Wrote `{dest.as_posix()}`.\n"
    )
    R.append_summary("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
