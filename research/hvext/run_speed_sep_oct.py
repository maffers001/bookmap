"""Test 'fast move' clean-leg gates on Sep+Oct full session (panel shape).

Base shape: Move20 Confirm10 Band5 HV2.
Speed axis: max_leg_sec x min_path_efficiency x max_pullback.
Brackets: fixed 10/30 and vol 1R/2.5R.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    make_vol_bracket_rr,
    simulate_session,
)
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
    d["n_long"] = sum(1 for t in trades if t.side > 0)
    d["n_short"] = sum(1 for t in trades if t.side < 0)
    d["net_long"] = round(sum((t.pnl - 0.25) for t in trades if t.side > 0), 2)
    d["net_short"] = round(sum((t.pnl - 0.25) for t in trades if t.side < 0), 2)
    d["per_session"] = {k: round(v, 2) for k, v in sorted(days.items())}
    return d


def run_cell(
    parquet_dir: Path,
    sessions: list[str],
    cfg: HvExtremeConfig,
    bracket_fn,
) -> dict:
    sim_cfg = MatSimConfig(
        signal_cooldown_sec=30,
        slip_ticks=0,
        flat_only=True,
        commission_pts_rt=0.25,
        pts_per_tick=0.25,
    )
    all_trades = []
    for sid in sessions:
        nq = _load_trades(parquet_dir, sid, "NQ")
        if nq.num_rows == 0:
            continue
        # Exact in-loop gates (leg/eff/pullback cancel watch)
        ev = hv_extreme_events(nq, cfg, window_ns=None, attach_med_r30=True)
        if ev.num_rows == 0:
            continue
        s = simulate_session(
            sid, nq, pa.table({}), sim_cfg, bracket_fn=bracket_fn, events=ev, vol_window_min=30
        )
        all_trades.extend(s.trades)
    return pack(all_trades)


def main() -> int:
    parquet_dir = Path(load_config()["parquet_dir"])
    print("Speed-gate sweep | FULL SESSION | shape Move20 Confirm10 Band5 HV2")
    print("Brackets: fixed 10/30 and vol_rr 1R/2.5R")

    # Baseline (panel) + speed-oriented cells
    cells = [
        # baseline
        dict(label="base_pb5_leg0_eff0", max_pullback_pts=5.0, max_leg_sec=0.0, min_path_efficiency=0.0),
        # fast legs
        dict(label="leg15_pb5_eff0", max_pullback_pts=5.0, max_leg_sec=15.0, min_path_efficiency=0.0),
        dict(label="leg30_pb5_eff0", max_pullback_pts=5.0, max_leg_sec=30.0, min_path_efficiency=0.0),
        dict(label="leg60_pb5_eff0", max_pullback_pts=5.0, max_leg_sec=60.0, min_path_efficiency=0.0),
        # high efficiency (clean/fast path)
        dict(label="eff0.5_pb5_leg0", max_pullback_pts=5.0, max_leg_sec=0.0, min_path_efficiency=0.5),
        dict(label="eff0.6_pb5_leg0", max_pullback_pts=5.0, max_leg_sec=0.0, min_path_efficiency=0.6),
        dict(label="eff0.75_pb5_leg0", max_pullback_pts=5.0, max_leg_sec=0.0, min_path_efficiency=0.75),
        # combine speed
        dict(label="leg30_eff0.5_pb5", max_pullback_pts=5.0, max_leg_sec=30.0, min_path_efficiency=0.5),
        dict(label="leg30_eff0.6_pb5", max_pullback_pts=5.0, max_leg_sec=30.0, min_path_efficiency=0.6),
        dict(label="leg15_eff0.6_pb5", max_pullback_pts=5.0, max_leg_sec=15.0, min_path_efficiency=0.6),
        dict(label="leg30_eff0.6_pb3", max_pullback_pts=3.0, max_leg_sec=30.0, min_path_efficiency=0.6),
        dict(label="leg15_eff0.75_pb3", max_pullback_pts=3.0, max_leg_sec=15.0, min_path_efficiency=0.75),
        # very fast / tight
        dict(label="leg10_eff0.6_pb5", max_pullback_pts=5.0, max_leg_sec=10.0, min_path_efficiency=0.6),
        dict(label="leg30_eff0.5_pb0", max_pullback_pts=0.0, max_leg_sec=30.0, min_path_efficiency=0.5),
    ]

    brackets = {
        "fixed_10_30": lambda _sv: (30.0, 10.0),
        "vol_rr_1_2.5": make_vol_bracket_rr(1.0, tp_r=2.5, sl_r=1.0, floor=4.0, cap=40.0),
    }

    rows = []
    for cell in cells:
        cfg = HvExtremeConfig(
            move_pts=20,
            confirm_pts=10,
            peak_band_pts=5,
            high_vol_mult=2.0,
            min_path_efficiency=cell["min_path_efficiency"],
            max_pullback_pts=cell["max_pullback_pts"],
            max_leg_sec=cell["max_leg_sec"],
            lookback_min=15,
            cooldown_sec=30,
        )
        for bname, bfn in brackets.items():
            print(f"\n--- {cell['label']} | {bname} ---", flush=True)
            sep = run_cell(parquet_dir, SEP, cfg, bfn)
            oct_ = run_cell(parquet_dir, OCT, cfg, bfn)
            both_net = (sep.get("net") or 0) + (oct_.get("net") or 0)
            both_n = sep["n"] + oct_["n"]
            row = {
                "cell": cell["label"],
                "bracket": bname,
                **{k: cell[k] for k in ("max_pullback_pts", "max_leg_sec", "min_path_efficiency")},
                "sep_n": sep["n"],
                "sep_net": sep.get("net"),
                "sep_pf": sep.get("pf"),
                "sep_wr": sep.get("wr"),
                "sep_days_pos": f"{sep['days_pos']}/{sep['n_sessions']}",
                "oct_n": oct_["n"],
                "oct_net": oct_.get("net"),
                "oct_pf": oct_.get("pf"),
                "oct_wr": oct_.get("wr"),
                "oct_days_pos": f"{oct_['days_pos']}/{oct_['n_sessions']}",
                "both_n": both_n,
                "both_net": round(both_net, 2),
                "sep_per_session": sep.get("per_session"),
                "oct_per_session": oct_.get("per_session"),
            }
            rows.append(row)
            print(
                f"  Sep n={sep['n']} net={sep.get('net')} pf={sep.get('pf')}  "
                f"Oct n={oct_['n']} net={oct_.get('net')} pf={oct_.get('pf')}  "
                f"Both net={both_net:+.1f}",
                flush=True,
            )

    # Rank by both_net
    ranked = sorted(rows, key=lambda r: r.get("both_net") or -1e9, reverse=True)
    print("\n========== RANKED BY Sep+Oct NET ==========")
    print(
        f"{'cell':22s} {'bracket':14s} {'Sep n':>5} {'Sep net':>8} {'Oct n':>5} "
        f"{'Oct net':>8} {'Both':>8} {'Sep PF':>7} {'Oct PF':>7}"
    )
    for r in ranked:
        print(
            f"{r['cell']:22s} {r['bracket']:14s} {r['sep_n']:5d} {r['sep_net']:+8.1f} "
            f"{r['oct_n']:5d} {r['oct_net']:+8.1f} {r['both_net']:+8.1f} "
            f"{str(r['sep_pf']):>7} {str(r['oct_pf']):>7}"
        )

    dest = Path("results/EXP-0037/sep_oct_speed_gates.json")
    dest.write_text(
        json.dumps({"rows": rows, "ranked": ranked[:20]}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nWrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
