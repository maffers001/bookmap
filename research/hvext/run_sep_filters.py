"""September full-session HV Extreme: apply Oct-suggested filters (OOS check).

Panel: Move20 Confirm10 Band5 HV2 pb5 leg=off, SL10/TP30.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, SimSummary, simulate_session
from research.features.hv_extreme import HvExtremeConfig, hv_extreme_events
from research.run_experiment import _load_trades, load_config

UK = ZoneInfo("Europe/London")
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
    d["days_pos_frac"] = (d["days_pos"] / len(days)) if days else float("nan")
    d["n_long"] = sum(1 for t in trades if t.side > 0)
    d["n_short"] = sum(1 for t in trades if t.side < 0)
    d["net_long"] = round(sum((t.pnl - 0.25) for t in trades if t.side > 0), 2)
    d["net_short"] = round(sum((t.pnl - 0.25) for t in trades if t.side < 0), 2)
    d["per_session"] = {k: round(v, 2) for k, v in sorted(days.items())}
    d["per_session_n"] = {k: day_n[k] for k in sorted(day_n)}
    return d


def main() -> int:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    signal = HvExtremeConfig(
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
    sim_cfg = MatSimConfig(
        signal_cooldown_sec=30,
        slip_ticks=0,
        flat_only=True,
        commission_pts_rt=0.25,
        pts_per_tick=0.25,
    )
    bfn = lambda _sv: (30.0, 10.0)

    print("September FULL SESSION — panel Move20/C10/B5/HV2/pb5, SL10/TP30")
    print("Sessions:", ", ".join(SEP))

    # Build annotated trade rows (trade object + features)
    annotated: list[dict] = []
    for sid in SEP:
        nq = _load_trades(parquet_dir, sid, "NQ")
        if nq.num_rows == 0:
            print(f"  {sid}: no NQ — skip")
            continue
        ev = hv_extreme_events(nq, signal, window_ns=None, attach_med_r30=True)
        evmap = {
            int(ev.column("event_ts_ns")[i].as_py()): {
                c: ev.column(c)[i].as_py() for c in ev.column_names
            }
            for i in range(ev.num_rows)
        }
        s = simulate_session(
            sid, nq, pa.table({}), sim_cfg, bracket_fn=bfn, events=ev, vol_window_min=30
        )
        day_net = sum(t.pnl - 0.25 for t in s.trades)
        print(f"  {sid}: fires={ev.num_rows} trades={s.n} net={day_net:+.2f}")
        for t in s.trades:
            best = None
            best_d = 1e18
            for ets, erow in evmap.items():
                d = abs(ets - t.entry_ts_ns)
                if d < best_d:
                    best_d = d
                    best = erow
            dt = datetime.fromtimestamp(t.entry_ts_ns / 1e9, tz=timezone.utc).astimezone(UK)
            annotated.append(
                {
                    "trade": t,
                    "sid": sid,
                    "side": t.side,
                    "pnl": t.pnl - 0.25,
                    "med_r30": float(t.med_r30),
                    "tod": dt.hour + dt.minute / 60.0,
                    "path_eff": float(best["path_eff"]) if best else float("nan"),
                    "vol_ratio": float(best["vol_ratio"]) if best else float("nan"),
                }
            )

    filters = {
        "baseline (no filter)": lambda r: True,
        "long only": lambda r: r["side"] > 0,
        "medR30 >= 12": lambda r: r["med_r30"] >= 12,
        "UK tod >= 15:00": lambda r: r["tod"] >= 15,
        "skip UK 14:00-15:30": lambda r: not (14.0 <= r["tod"] < 15.5),
        "long + medR>=12": lambda r: r["side"] > 0 and r["med_r30"] >= 12,
        "tod>=15 + medR>=12": lambda r: r["tod"] >= 15 and r["med_r30"] >= 12,
        "long + tod>=15": lambda r: r["side"] > 0 and r["tod"] >= 15,
        "long + tod>=15 + medR>=12": lambda r: (
            r["side"] > 0 and r["tod"] >= 15 and r["med_r30"] >= 12
        ),
        "skip 14-15.5 + long": lambda r: r["side"] > 0 and not (14.0 <= r["tod"] < 15.5),
        "skip 14-15.5 + medR>=12": lambda r: r["med_r30"] >= 12
        and not (14.0 <= r["tod"] < 15.5),
    }

    print("\n=== FILTER RESULTS (September full session) ===")
    results = {}
    base_net = None
    for name, pred in filters.items():
        kept = [r for r in annotated if pred(r)]
        trades = [r["trade"] for r in kept]
        summary = pack(trades)
        if base_net is None:
            base_net = summary["net"] or 0.0
        delta = (summary["net"] or 0.0) - base_net
        print(
            f"{name:32s}  n={summary['n']:3d}  net={summary['net']:+8.2f}  "
            f"d={delta:+7.1f}  wr={summary.get('wr')}  pf={summary.get('pf')}  "
            f"days+={summary['days_pos']}/{summary['n_sessions']}  "
            f"maxDD={summary.get('max_dd')}"
        )
        results[name] = summary

    # Per-day for baseline and best stacked filter
    print("\n=== PER-DAY: baseline vs long+tod>=15+medR>=12 ===")
    key = "long + tod>=15 + medR>=12"
    print(f"{'sid':8s}  {'base_n':>6} {'base_net':>9}  {'filt_n':>6} {'filt_net':>9}")
    base_ps = results["baseline (no filter)"]["per_session"]
    base_pn = results["baseline (no filter)"]["per_session_n"]
    filt_ps = results[key]["per_session"]
    filt_pn = results[key]["per_session_n"]
    for sid in SEP:
        bn = base_pn.get(sid, 0)
        bnet = base_ps.get(sid, 0.0)
        fn = filt_pn.get(sid, 0)
        fnet = filt_ps.get(sid, 0.0)
        if bn == 0 and fn == 0:
            continue
        print(f"{sid:8s}  {bn:6d} {bnet:+9.2f}  {fn:6d} {fnet:+9.2f}")

    # Also show long-only and medR>=12 per day
    print("\n=== PER-DAY: long only ===")
    ps = results["long only"]["per_session"]
    pn = results["long only"]["per_session_n"]
    for sid in SEP:
        if sid in pn:
            print(f"  {sid}: n={pn[sid]:3d} net={ps[sid]:+.2f}")

    print("\n=== PER-DAY: medR30 >= 12 ===")
    ps = results["medR30 >= 12"]["per_session"]
    pn = results["medR30 >= 12"]["per_session_n"]
    for sid in SEP:
        if sid in pn:
            print(f"  {sid}: n={pn[sid]:3d} net={ps[sid]:+.2f}")

    out = {
        "window": "full_session",
        "month": "september",
        "sessions": SEP,
        "signal": "m20_c10_b5_hv2_pb5",
        "bracket": {"sl": 10.0, "tp": 30.0},
        "filters": {k: v for k, v in results.items()},
    }
    dest = Path("results/EXP-0037/september_full_session_filters.json")
    dest.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
