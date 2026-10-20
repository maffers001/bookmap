"""Analyze patterns on losing vs winning October full-session days."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, simulate_session
from research.features.hv_extreme import HvExtremeConfig, hv_extreme_events
from research.run_experiment import _load_trades, load_config

UK = ZoneInfo("Europe/London")
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


def main() -> int:
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
    p = Path(load_config()["parquet_dir"])

    rows: list[dict] = []
    for sid in OCT:
        nq = _load_trades(p, sid, "NQ")
        ev = hv_extreme_events(nq, signal, window_ns=None, attach_med_r30=True)
        ev_by_ts = {}
        for i in range(ev.num_rows):
            ts = int(ev.column("event_ts_ns")[i].as_py())
            ev_by_ts[ts] = {c: ev.column(c)[i].as_py() for c in ev.column_names}
        s = simulate_session(
            sid, nq, pa.table({}), sim_cfg, bracket_fn=bfn, events=ev, vol_window_min=30
        )
        day_net = sum(t.pnl - 0.25 for t in s.trades)
        for t in s.trades:
            best = None
            best_d = 1e18
            for ets, erow in ev_by_ts.items():
                d = abs(ets - t.entry_ts_ns)
                if d < best_d:
                    best_d = d
                    best = erow
            dt = datetime.fromtimestamp(t.entry_ts_ns / 1e9, tz=timezone.utc).astimezone(UK)
            rows.append(
                {
                    "sid": sid,
                    "day_net": day_net,
                    "losing_day": day_net < 0,
                    "side": t.side,
                    "pnl": t.pnl - 0.25,
                    "win": t.win,
                    "reason": t.reason,
                    "hour": dt.hour,
                    "tod": dt.hour + dt.minute / 60,
                    "hold_s": (
                        (t.exit_ts_ns - t.entry_ts_ns) / 1e9 if t.exit_ts_ns else float("nan")
                    ),
                    "med_r30": t.med_r30,
                    "vol_ratio": best.get("vol_ratio") if best else float("nan"),
                    "path_eff": best.get("path_eff") if best else float("nan"),
                    "pullback": best.get("max_pullback_ticks") if best else float("nan"),
                    "leg_sec": best.get("leg_sec") if best else float("nan"),
                }
            )

    print("=== DAY SUMMARY ===")
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        by[r["sid"]].append(r)
    for sid in OCT:
        rs = by[sid]
        if not rs:
            continue
        net = rs[0]["day_net"]
        n = len(rs)
        wr = sum(1 for r in rs if r["win"]) / n
        long_n = sum(1 for r in rs if r["side"] > 0)
        short_n = n - long_n
        long_net = sum(r["pnl"] for r in rs if r["side"] > 0)
        short_net = sum(r["pnl"] for r in rs if r["side"] < 0)
        sl = sum(1 for r in rs if r["reason"] == "sl")
        tp = sum(1 for r in rs if r["reason"] == "tp")
        eod = sum(1 for r in rs if r["reason"] == "eod")
        med_r = float(np.nanmedian([r["med_r30"] for r in rs]))
        med_eff = float(np.nanmedian([r["path_eff"] for r in rs]))
        med_vr = float(np.nanmedian([r["vol_ratio"] for r in rs]))
        mid = n // 2
        first = sum(r["pnl"] for r in rs[:mid]) if mid else 0
        second = sum(r["pnl"] for r in rs[mid:])
        hours = sorted({r["hour"] for r in rs})
        tag = "LOSS" if net < 0 else "WIN "
        print(
            f"{tag} {sid} net={net:+7.1f} n={n:2d} wr={wr:.0%} "
            f"L/S={long_n}/{short_n} Lnet={long_net:+6.1f} Snet={short_net:+6.1f} "
            f"TP/SL/EOD={tp}/{sl}/{eod} medR={med_r:.1f} eff={med_eff:.2f} vr={med_vr:.2f} "
            f"1stHalf/2nd={first:+.0f}/{second:+.0f} hoursUK={hours}"
        )

    print("\n=== LOSING vs WINNING DAYS (trade-level) ===")
    for label, pred in [("LOSING DAYS", True), ("WINNING DAYS", False)]:
        rs = [r for r in rows if r["losing_day"] is pred]
        print(f"--- {label} n_trades={len(rs)} ---")
        print(
            "  side: long",
            sum(1 for r in rs if r["side"] > 0),
            "short",
            sum(1 for r in rs if r["side"] < 0),
            "long_net",
            round(sum(r["pnl"] for r in rs if r["side"] > 0), 1),
            "short_net",
            round(sum(r["pnl"] for r in rs if r["side"] < 0), 1),
        )
        print(
            "  reason:",
            {k: sum(1 for r in rs if r["reason"] == k) for k in ("tp", "sl", "eod")},
        )
        for name, key in [
            ("med_r30", "med_r30"),
            ("path_eff", "path_eff"),
            ("vol_ratio", "vol_ratio"),
            ("pullback_ticks", "pullback"),
            ("leg_sec", "leg_sec"),
            ("hold_s", "hold_s"),
        ]:
            vals = [r[key] for r in rs]
            q = np.nanpercentile(vals, [25, 50, 75])
            print(f"  {name} p25/50/75: {q[0]:.2f} / {q[1]:.2f} / {q[2]:.2f}")
        buckets = [
            (0, 10),
            (10, 12),
            (12, 14),
            (14, 15),
            (15, 16),
            (16, 17),
            (17, 18),
            (18, 20),
            (20, 24),
        ]
        for a, b in buckets:
            sub = [r for r in rs if a <= r["hour"] < b]
            if not sub:
                continue
            print(
                f"  UK {a:02d}-{b:02d}: n={len(sub)} "
                f"net={sum(r['pnl'] for r in sub):+.1f} "
                f"wr={sum(1 for r in sub if r['win']) / len(sub):.0%}"
            )

    # Filter candidate lifts (in-sample — triage only)
    print("\n=== FILTER TRIAGE (full Oct, in-sample) ===")
    base_net = sum(r["pnl"] for r in rows)
    base_n = len(rows)
    print(f"baseline: n={base_n} net={base_net:+.1f}")

    def eval_filter(name: str, pred):
        kept = [r for r in rows if pred(r)]
        if not kept:
            print(f"  {name}: kept 0")
            return
        net = sum(r["pnl"] for r in kept)
        wr = sum(1 for r in kept if r["win"]) / len(kept)
        # day nets under filter
        day_nets: dict[str, float] = defaultdict(float)
        for r in kept:
            day_nets[r["sid"]] += r["pnl"]
        days_pos = sum(1 for v in day_nets.values() if v > 0)
        print(
            f"  {name}: n={len(kept)} ({100*len(kept)/base_n:.0f}%) "
            f"net={net:+.1f} (d{net-base_net:+.1f}) wr={wr:.0%} "
            f"days+={days_pos}/{len(day_nets)}"
        )

    eval_filter("long only", lambda r: r["side"] > 0)
    eval_filter("short only", lambda r: r["side"] < 0)
    eval_filter("path_eff >= 0.5", lambda r: r["path_eff"] >= 0.5)
    eval_filter("path_eff >= 0.6", lambda r: r["path_eff"] >= 0.6)
    eval_filter("path_eff >= 0.75", lambda r: r["path_eff"] >= 0.75)
    eval_filter("vol_ratio >= 2.5", lambda r: r["vol_ratio"] >= 2.5)
    eval_filter("vol_ratio >= 3.0", lambda r: r["vol_ratio"] >= 3.0)
    eval_filter("pullback <= 2 ticks", lambda r: r["pullback"] <= 2)
    eval_filter("pullback <= 3 ticks", lambda r: r["pullback"] <= 3)
    eval_filter("leg_sec <= 30", lambda r: r["leg_sec"] <= 30)
    eval_filter("leg_sec <= 60", lambda r: r["leg_sec"] <= 60)
    eval_filter("med_r30 >= 8", lambda r: r["med_r30"] >= 8)
    eval_filter("med_r30 >= 12", lambda r: r["med_r30"] >= 12)
    eval_filter("med_r30 <= 20", lambda r: r["med_r30"] <= 20)
    eval_filter("UK 14:30-21:00", lambda r: 14.5 <= r["tod"] < 21)
    eval_filter("UK 14:30-17:00", lambda r: 14.5 <= r["tod"] < 17)
    eval_filter("UK 15:00-21:00", lambda r: 15 <= r["tod"] < 21)
    eval_filter("skip UK 14-15", lambda r: not (14 <= r["hour"] < 15))
    eval_filter("skip UK 17-18", lambda r: not (17 <= r["hour"] < 18))
    eval_filter("skip UK 18-20", lambda r: not (18 <= r["hour"] < 20))
    eval_filter("long + eff>=0.5", lambda r: r["side"] > 0 and r["path_eff"] >= 0.5)
    eval_filter(
        "eff>=0.5 & vr>=2.5",
        lambda r: r["path_eff"] >= 0.5 and r["vol_ratio"] >= 2.5,
    )
    eval_filter(
        "eff>=0.6 & pullback<=3",
        lambda r: r["path_eff"] >= 0.6 and r["pullback"] <= 3,
    )
    eval_filter(
        "long & UK15-21 & eff>=0.5",
        lambda r: r["side"] > 0 and 15 <= r["tod"] < 21 and r["path_eff"] >= 0.5,
    )

    # Consecutive loss streaks within day on losing days
    print("\n=== STREAKS ON LOSING DAYS ===")
    for sid in OCT:
        rs = by[sid]
        if not rs or rs[0]["day_net"] >= 0:
            continue
        streak = max_streak = 0
        for r in rs:
            if not r["win"]:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        # after 2 losses in a row, what happens next?
        print(f"  {sid}: max_loss_streak={max_streak} n={len(rs)} net={rs[0]['day_net']:+.1f}")

    # What if stop trading day after 2 consecutive losses?
    print("\n=== DAY-LEVEL RISK FILTERS (triage) ===")
    for max_consec in (2, 3):
        kept_pnl = 0.0
        kept_n = 0
        for sid in OCT:
            rs = by[sid]
            streak = 0
            for r in rs:
                if streak >= max_consec:
                    continue
                kept_pnl += r["pnl"]
                kept_n += 1
                if r["win"]:
                    streak = 0
                else:
                    streak += 1
        print(f"  stop day after {max_consec} consec losses: n={kept_n} net={kept_pnl:+.1f}")

    for max_day_loss in (-30, -40, -50):
        kept_pnl = 0.0
        kept_n = 0
        for sid in OCT:
            rs = by[sid]
            cum = 0.0
            for r in rs:
                if cum <= max_day_loss:
                    continue
                kept_pnl += r["pnl"]
                kept_n += 1
                cum += r["pnl"]
        print(f"  stop day if cum <= {max_day_loss}: n={kept_n} net={kept_pnl:+.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
