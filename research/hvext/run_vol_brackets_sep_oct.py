"""Full-session Sep+Oct HV Extreme with vol-scaled brackets (panel signal)."""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    make_vol_bracket,
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

SIGNAL = HvExtremeConfig(
    move_pts=20,
    confirm_pts=10,
    peak_band_pts=5,
    high_vol_mult=2.0,
    min_path_efficiency=0,
    max_pullback_pts=5,
    max_leg_sec=0,
    lookback_min=15,
    cooldown_sec=30,
)


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
    # sample resolved brackets
    if trades:
        tps = [t.tp_pts for t in trades]
        sls = [t.sl_pts for t in trades]
        d["tp_med"] = round(float(sorted(tps)[len(tps) // 2]), 2)
        d["sl_med"] = round(float(sorted(sls)[len(sls) // 2]), 2)
        d["tp_mean"] = round(sum(tps) / len(tps), 2)
        d["sl_mean"] = round(sum(sls) / len(sls), 2)
    return d


def run_month(parquet_dir: Path, sessions: list[str], bracket_fn, label: str) -> dict:
    sim_cfg = MatSimConfig(
        signal_cooldown_sec=30,
        slip_ticks=0,
        flat_only=True,
        commission_pts_rt=0.25,
        pts_per_tick=0.25,
    )
    all_trades = []
    per_day = []
    for sid in sessions:
        nq = _load_trades(parquet_dir, sid, "NQ")
        if nq.num_rows == 0:
            per_day.append({"session_id": sid, "n": 0, "net": 0.0})
            continue
        ev = hv_extreme_events(nq, SIGNAL, window_ns=None, attach_med_r30=True)
        if ev.num_rows == 0:
            per_day.append({"session_id": sid, "fires": 0, "n": 0, "net": 0.0})
            continue
        s = simulate_session(
            sid, nq, pa.table({}), sim_cfg, bracket_fn=bracket_fn, events=ev, vol_window_min=30
        )
        day_net = sum(t.pnl - 0.25 for t in s.trades)
        tps = [t.tp_pts for t in s.trades]
        sls = [t.sl_pts for t in s.trades]
        per_day.append(
            {
                "session_id": sid,
                "fires": int(ev.num_rows),
                "n": int(s.n),
                "net": round(day_net, 2),
                "tp_med": round(float(sorted(tps)[len(tps) // 2]), 2) if tps else None,
                "sl_med": round(float(sorted(sls)[len(sls) // 2]), 2) if sls else None,
            }
        )
        all_trades.extend(s.trades)
    summary = pack(all_trades)
    print(
        f"\n=== {label} ===\n"
        f"n={summary['n']} wr={summary.get('wr')} pf={summary.get('pf')} "
        f"net={summary.get('net')} maxDD={summary.get('max_dd')} "
        f"days+={summary['days_pos']}/{summary['n_sessions']} "
        f"tp_med={summary.get('tp_med')} sl_med={summary.get('sl_med')}"
    )
    for d in per_day:
        if d.get("n", 0) == 0:
            print(f"  {d['session_id']}: n=0")
            continue
        print(
            f"  {d['session_id']}: n={d['n']:3d} net={d['net']:+8.2f} "
            f"tp/sl med={d['tp_med']}/{d['sl_med']}"
        )
    return {"summary": summary, "per_day": per_day}


def main() -> int:
    parquet_dir = Path(load_config()["parquet_dir"])
    print("Panel: Move20 Confirm10 Band5 HV2 pb5 leg=off | FULL SESSION")
    print("Vol window 30 min | floor 4 / cap 40")

    brackets = {
        "fixed_10_30": (lambda _sv: (30.0, 10.0)),
        "vol_rr_1_2.5": make_vol_bracket_rr(1.0, tp_r=2.5, sl_r=1.0, floor=4.0, cap=40.0),
        "vol_rr_1_3": make_vol_bracket_rr(1.0, tp_r=3.0, sl_r=1.0, floor=4.0, cap=40.0),
        "vol_rr_1.5_3": make_vol_bracket_rr(1.0, tp_r=3.0, sl_r=1.5, floor=4.0, cap=40.0),
        "vol_rr_1_2": make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0),
        "vol_sym_k1": make_vol_bracket(1.0, floor=6.0, cap=30.0),
    }

    out: dict = {"signal": "m20_c10_b5_hv2_pb5", "window": "full_session", "months": {}}
    for month, sessions in (("september", SEP), ("october", OCT)):
        out["months"][month] = {}
        print(f"\n########## {month.upper()} ##########")
        for bname, bfn in brackets.items():
            res = run_month(parquet_dir, sessions, bfn, f"{month} | {bname}")
            out["months"][month][bname] = res

    # Side-by-side summary table
    print("\n========== SUMMARY ==========")
    print(f"{'bracket':18s}  {'Sep n':>5} {'Sep net':>9} {'Sep PF':>7}  "
          f"{'Oct n':>5} {'Oct net':>9} {'Oct PF':>7}  {'Both net':>9}")
    for bname in brackets:
        sep = out["months"]["september"][bname]["summary"]
        oct_ = out["months"]["october"][bname]["summary"]
        both = (sep.get("net") or 0) + (oct_.get("net") or 0)
        print(
            f"{bname:18s}  {sep['n']:5d} {sep.get('net'):+9.1f} {str(sep.get('pf')):>7}  "
            f"{oct_['n']:5d} {oct_.get('net'):+9.1f} {str(oct_.get('pf')):>7}  "
            f"{both:+9.1f}"
        )

    dest = Path("results/EXP-0037/sep_oct_vol_brackets.json")
    dest.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
