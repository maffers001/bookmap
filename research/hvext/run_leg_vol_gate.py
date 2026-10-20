"""Backtest Gate B (quiet leg outside Peak band) on Sep+Oct full session."""
from __future__ import annotations

import json
from pathlib import Path

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
    day_n: dict[str, int] = {}
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
        day_n[t.session_id] = day_n.get(t.session_id, 0) + 1
    d = s.to_dict()
    d["n_sessions"] = len(days)
    d["days_pos"] = sum(1 for v in days.values() if v > 0)
    d["n_long"] = sum(1 for t in trades if t.side > 0)
    d["n_short"] = sum(1 for t in trades if t.side < 0)
    d["net_long"] = round(sum((t.pnl - 0.25) for t in trades if t.side > 0), 2)
    d["net_short"] = round(sum((t.pnl - 0.25) for t in trades if t.side < 0), 2)
    d["per_session"] = {k: round(v, 2) for k, v in sorted(days.items())}
    d["per_session_n"] = {k: day_n[k] for k in sorted(day_n)}
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
    ratios = []
    for sid in sessions:
        nq = _load_trades(parquet_dir, sid, "NQ")
        if nq.num_rows == 0:
            continue
        # Gate off first to get ratio distribution when measuring baseline;
        # for gated runs, in-loop cancel is applied.
        ev = hv_extreme_events(nq, cfg, window_ns=None, attach_med_r30=True)
        if ev.num_rows == 0:
            continue
        if "leg_vol_ratio" in ev.column_names:
            ratios.extend(ev.column("leg_vol_ratio").to_pylist())
        s = simulate_session(
            sid, nq, pa.table({}), sim_cfg, bracket_fn=bfn, events=ev, vol_window_min=30
        )
        all_trades.extend(s.trades)
    summary = pack(all_trades)
    if ratios:
        import numpy as np

        summary["leg_vol_ratio_p25"] = float(np.nanpercentile(ratios, 25))
        summary["leg_vol_ratio_p50"] = float(np.nanpercentile(ratios, 50))
        summary["leg_vol_ratio_p75"] = float(np.nanpercentile(ratios, 75))
    return summary


def main() -> int:
    parquet_dir = Path(load_config()["parquet_dir"])
    print("Gate B sweep | FULL SESSION | Move20 Confirm10 Band5 HV2 pb5 | SL10/TP30")
    print("legVol = prints outside Peak band; ratio = legVol / (avgVolPerTick * moveTicks)")

    thresholds = [0.0, 0.75, 1.0, 1.25]
    rows = []
    for thr in thresholds:
        label = "baseline (gate off)" if thr <= 0 else f"max_leg_vol_vs_avg<{thr:g}"
        cfg = HvExtremeConfig(
            move_pts=20,
            confirm_pts=10,
            peak_band_pts=5,
            high_vol_mult=2.0,
            min_path_efficiency=0.0,
            max_pullback_pts=5.0,
            max_leg_sec=0.0,
            max_leg_vol_vs_avg=thr,
            lookback_min=15,
            cooldown_sec=30,
        )
        print(f"\n=== {label} ===", flush=True)
        sep = run_month(parquet_dir, SEP, cfg)
        oct_ = run_month(parquet_dir, OCT, cfg)
        both = (sep.get("net") or 0) + (oct_.get("net") or 0)
        print(
            f"  Sep n={sep['n']} net={sep.get('net')} pf={sep.get('pf')} "
            f"days+={sep['days_pos']}/{sep['n_sessions']}"
        )
        print(
            f"  Oct n={oct_['n']} net={oct_.get('net')} pf={oct_.get('pf')} "
            f"days+={oct_['days_pos']}/{oct_['n_sessions']}"
        )
        print(f"  Both net={both:+.2f}")
        if thr <= 0 and sep.get("leg_vol_ratio_p50") is not None:
            print(
                f"  Sep legVolRatio p25/50/75="
                f"{sep.get('leg_vol_ratio_p25'):.2f}/"
                f"{sep.get('leg_vol_ratio_p50'):.2f}/"
                f"{sep.get('leg_vol_ratio_p75'):.2f}"
            )
            print(
                f"  Oct legVolRatio p25/50/75="
                f"{oct_.get('leg_vol_ratio_p25'):.2f}/"
                f"{oct_.get('leg_vol_ratio_p50'):.2f}/"
                f"{oct_.get('leg_vol_ratio_p75'):.2f}"
            )
        rows.append(
            {
                "label": label,
                "max_leg_vol_vs_avg": thr,
                "sep": sep,
                "oct": oct_,
                "both_net": round(both, 2),
            }
        )

    print("\n========== SUMMARY ==========")
    print(f"{'gate':28s} {'Sep n':>5} {'Sep net':>9} {'Oct n':>5} {'Oct net':>9} {'Both':>9}")
    for r in rows:
        print(
            f"{r['label']:28s} {r['sep']['n']:5d} {r['sep'].get('net'):+9.1f} "
            f"{r['oct']['n']:5d} {r['oct'].get('net'):+9.1f} {r['both_net']:+9.1f}"
        )

    dest = Path("results/EXP-0037/sep_oct_leg_vol_gate.json")
    dest.write_text(json.dumps({"rows": rows}, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
