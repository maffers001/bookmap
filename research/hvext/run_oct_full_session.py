"""HV Extreme panel signal, fixed SL10/TP30, full session (no UK window cut).

Signal: Move20 / Confirm10 / Band5 / HV2 / pb5 / leg off.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, SimSummary, simulate_session
from research.features.hv_extreme import HvExtremeConfig, hv_extreme_events
from research.run_experiment import _load_trades, load_config

OCT = [
    "011026",
    "021026",
    "051026",
    "061026",
    "071026",
    "081026",
    "091026",
    "121026",
    "131026",
    "141026",
    "151026",
    "161026",
    "191026",
]


def pack_summary(s: SimSummary) -> dict:
    days: dict[str, float] = {}
    day_n: dict[str, int] = {}
    for t in s.trades:
        days[t.session_id] = days.get(t.session_id, 0.0) + (t.pnl - 0.25)
        day_n[t.session_id] = day_n.get(t.session_id, 0) + 1
    days_pos = sum(1 for v in days.values() if v > 0)
    return {
        **s.to_dict(),
        "n_sessions": len(days),
        "days_pos": days_pos,
        "days_pos_frac": (days_pos / len(days)) if days else float("nan"),
        "n_long": sum(1 for t in s.trades if t.side > 0),
        "n_short": sum(1 for t in s.trades if t.side < 0),
        "net_long": round(sum((t.pnl - 0.25) for t in s.trades if t.side > 0), 2),
        "net_short": round(sum((t.pnl - 0.25) for t in s.trades if t.side < 0), 2),
        "per_session": {k: round(v, 2) for k, v in sorted(days.items())},
        "per_session_n": {k: day_n[k] for k in sorted(day_n)},
    }


def main() -> int:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    signal = HvExtremeConfig(
        move_pts=20.0,
        confirm_pts=10.0,
        peak_band_pts=5.0,
        high_vol_mult=2.0,
        min_path_efficiency=0.0,
        max_pullback_pts=5.0,
        max_leg_sec=0.0,
        lookback_min=15.0,
        cooldown_sec=30.0,
    )
    sim_cfg = MatSimConfig(
        signal_cooldown_sec=30.0,
        slip_ticks=0.0,
        flat_only=True,
        commission_pts_rt=0.25,
        pts_per_tick=0.25,
    )

    def bracket_fn(_sv):
        return 30.0, 10.0

    print("October FULL SESSION (no UK window cut)")
    print("Signal: Move20 Confirm10 Band5 HV2 pb5 leg=off")
    print("Bracket: SL 10 / TP 30 | flat-only | cooldown 30s | commission 0.25")
    print("Sessions:", ", ".join(OCT))

    all_trades = []
    per_day = []
    for sid in OCT:
        nq = _load_trades(parquet_dir, sid, "NQ")
        if nq.num_rows == 0:
            print(f"  {sid}: no NQ trades")
            per_day.append({"session_id": sid, "n": 0, "net": 0.0})
            continue
        # Full session: warm-up + keep all fires (window_ns=None).
        ev = hv_extreme_events(nq, signal, window_ns=None, attach_med_r30=True)
        if ev.num_rows == 0:
            print(f"  {sid}: 0 fires")
            per_day.append({"session_id": sid, "n": 0, "net": 0.0, "fires": 0})
            continue
        summary = simulate_session(
            sid,
            nq,
            pa.table({}),
            sim_cfg,
            bracket_fn=bracket_fn,
            events=ev,
            vol_window_min=30,
        )
        day_net = sum(t.pnl - 0.25 for t in summary.trades)
        print(
            f"  {sid}: fires={ev.num_rows}  trades={summary.n}  "
            f"net={day_net:+.2f}  wr={summary.wr if summary.n else float('nan'):.3f}"
        )
        per_day.append(
            {
                "session_id": sid,
                "fires": int(ev.num_rows),
                "n": int(summary.n),
                "net": round(day_net, 2),
                "wins": int(summary.wins),
            }
        )
        all_trades.extend(summary.trades)

    out = SimSummary()
    equity = peak = 0.0
    for t in all_trades:
        out.trades.append(t)
        out.n += 1
        if t.win:
            out.wins += 1
            out.gross_win += max(t.pnl, 0.0)
        else:
            out.gross_loss += abs(min(t.pnl, 0.0))
            if t.pnl > 0:
                out.gross_win += t.pnl
        out.gross += t.pnl
        net = t.pnl - 0.25
        out.commission_total += 0.25
        out.net += net
        equity += net
        peak = max(peak, equity)
        out.max_dd = max(out.max_dd, peak - equity)

    summary = pack_summary(out)
    print(
        f"\nTOTAL n={summary['n']} wr={summary.get('wr')} pf={summary.get('pf')} "
        f"net={summary.get('net')} maxDD={summary.get('max_dd')} "
        f"days+={summary.get('days_pos')}/{summary.get('n_sessions')}"
    )
    print(
        f"long n={summary['n_long']} net={summary['net_long']}  "
        f"short n={summary['n_short']} net={summary['net_short']}"
    )

    report = {
        "window": "full_session",
        "sessions": OCT,
        "signal": {
            "move_pts": 20,
            "confirm_pts": 10,
            "peak_band_pts": 5,
            "high_vol_mult": 2.0,
            "max_pullback_pts": 5.0,
            "max_leg_sec": 0.0,
            "min_path_efficiency": 0.0,
        },
        "bracket": {"sl": 10.0, "tp": 30.0},
        "summary": summary,
        "per_day": per_day,
    }
    dest = Path("results/EXP-0037/october_full_session_10_30.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
