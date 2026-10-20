"""Sweep retuned Gate B (outside-ticks denom) and Gate A (vs tip) on Sep+Oct."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, SimSummary, simulate_session
from research.features.hv_extreme import HvExtremeConfig, hv_extreme_events
from research.run_experiment import _load_trades, load_config

SEP = [
    "010926",
    "020926",
    "040926",
    "080926",
    "100926",
    "150926",
    "160926",
    "170926",
    "180926",
    "210926",
    "220926",
    "250926",
    "280926",
    "290926",
    "300926",
]
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


def pack(trades: list) -> dict:
    s = SimSummary()
    equity = peak = 0.0
    days: dict[str, float] = {}
    for t in trades:
        s.trades.append(t)
        s.n += 1
        if t.win:
            s.wins += 1
            s.gross_win += max(t.pnl, 0.0)
        else:
            s.gross_loss += abs(min(t.pnl, 0.0))
            if t.pnl > 0:
                s.gross_win += t.pnl
        s.gross += t.pnl
        net = t.pnl - 0.25
        s.commission_total += 0.25
        s.net += net
        equity += net
        peak = max(peak, equity)
        s.max_dd = max(s.max_dd, peak - equity)
        days[t.session_id] = days.get(t.session_id, 0.0) + net
    d = s.to_dict()
    d["n_sessions"] = len(days)
    d["days_pos"] = sum(1 for v in days.values() if v > 0)
    d["per_session"] = {k: round(v, 2) for k, v in sorted(days.items())}
    return d


def run_month(parquet_dir: Path, sessions: list[str], cfg: HvExtremeConfig) -> dict:
    sim_cfg = MatSimConfig(
        signal_cooldown_sec=30,
        slip_ticks=0,
        flat_only=True,
        commission_pts_rt=0.25,
        pts_per_tick=0.25,
    )
    bfn = lambda _sv: (30.0, 10.0)
    all_trades = []
    ratios_b = []
    ratios_a = []
    for sid in sessions:
        nq = _load_trades(parquet_dir, sid, "NQ")
        if nq.num_rows == 0:
            continue
        ev = hv_extreme_events(nq, cfg, window_ns=None, attach_med_r30=False)
        if ev.num_rows == 0:
            continue
        ratios_b.extend(ev.column("leg_vol_ratio").to_pylist())
        ratios_a.extend(ev.column("leg_vol_vs_tip").to_pylist())
        s = simulate_session(
            sid, nq, pa.table({}), sim_cfg, bracket_fn=bfn, events=ev, vol_window_min=30
        )
        all_trades.extend(s.trades)
    summary = pack(all_trades)
    if ratios_b:
        summary["b_p50"] = float(np.nanpercentile(ratios_b, 50))
        summary["b_p90"] = float(np.nanpercentile(ratios_b, 90))
        summary["a_p50"] = float(np.nanpercentile(ratios_a, 50))
        summary["a_p90"] = float(np.nanpercentile(ratios_a, 90))
    return summary


def cell(vs_avg: float = 0.0, vs_tip: float = 0.0) -> HvExtremeConfig:
    return HvExtremeConfig(
        move_pts=20,
        confirm_pts=10,
        peak_band_pts=5,
        high_vol_mult=2.0,
        min_path_efficiency=0.0,
        max_pullback_pts=5.0,
        max_leg_sec=0.0,
        max_leg_vol_vs_avg=vs_avg,
        max_leg_vol_vs_tip=vs_tip,
        lookback_min=15,
        cooldown_sec=30,
    )


def main() -> int:
    parquet_dir = Path(load_config()["parquet_dir"])
    print("Gate A/B retune | FULL SESSION | Move20 C10 B5 HV2 pb5 | SL10/TP30")
    print("Gate B denom = avg × max(1, moveTicks - bandHalf)")
    print("Gate A = legVol / extremeVol")

    variants = [
        ("baseline", 0.0, 0.0),
        ("B<1.0 (retuned)", 1.0, 0.0),
        ("B<0.5", 0.5, 0.0),
        ("B<0.25", 0.25, 0.0),
        ("B<0.15", 0.15, 0.0),
        ("B<0.10", 0.10, 0.0),
        ("A<1.0", 0.0, 1.0),
        ("A<0.5", 0.0, 0.5),
        ("A<0.25", 0.0, 0.25),
        ("A<0.15", 0.0, 0.15),
        ("A<0.10", 0.0, 0.10),
        ("A<0.5 & B<0.5", 0.5, 0.5),
        ("A<0.25 & B<0.25", 0.25, 0.25),
    ]

    rows = []
    for label, b, a in variants:
        cfg = cell(b, a)
        print(f"\n=== {label} ===", flush=True)
        sep = run_month(parquet_dir, SEP, cfg)
        oct_ = run_month(parquet_dir, OCT, cfg)
        both = (sep.get("net") or 0) + (oct_.get("net") or 0)
        print(
            f"  Sep n={sep['n']} net={sep.get('net')} pf={sep.get('pf')}  "
            f"Oct n={oct_['n']} net={oct_.get('net')} pf={oct_.get('pf')}  "
            f"Both={both:+.1f}"
        )
        if label == "baseline":
            print(
                f"  B ratio p50/p90 Sep={sep.get('b_p50'):.3f}/{sep.get('b_p90'):.3f} "
                f"Oct={oct_.get('b_p50'):.3f}/{oct_.get('b_p90'):.3f}"
            )
            print(
                f"  A ratio p50/p90 Sep={sep.get('a_p50'):.3f}/{sep.get('a_p90'):.3f} "
                f"Oct={oct_.get('a_p50'):.3f}/{oct_.get('a_p90'):.3f}"
            )
        rows.append(
            {
                "label": label,
                "vs_avg": b,
                "vs_tip": a,
                "sep": sep,
                "oct": oct_,
                "both_net": round(both, 2),
            }
        )

    ranked = sorted(rows, key=lambda r: r["both_net"], reverse=True)
    print("\n========== RANKED ==========")
    print(f"{'label':22s} {'Sep n':>5} {'Sep net':>9} {'Oct n':>5} {'Oct net':>9} {'Both':>9}")
    for r in ranked:
        print(
            f"{r['label']:22s} {r['sep']['n']:5d} {r['sep'].get('net'):+9.1f} "
            f"{r['oct']['n']:5d} {r['oct'].get('net'):+9.1f} {r['both_net']:+9.1f}"
        )

    dest = Path("results/EXP-0037/sep_oct_leg_vol_gates_ab.json")
    dest.write_text(json.dumps({"ranked": ranked, "rows": rows}, indent=2, default=str))
    print(f"\nWrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
