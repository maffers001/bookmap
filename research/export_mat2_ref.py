"""Export MAT2 reference signals for replay comparison (session 021026).

Usage:
  python -m research.export_mat2_ref
  python -m research.export_mat2_ref --session 021026

Writes results/MAT2_REF_<sid>.md and .csv with every expected PASS signal
under v2 defaults: causal ES lag<=250, trend with (completed 30m), VWAP side,
range gate OFF, flat-only, 5s cooldown, TP=SL=10, 0 slip in the list (times/px).
"""
from __future__ import annotations

import argparse
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from research.backtest.mat_sim import (
    MatSimConfig,
    combine_gates,
    detect_signals,
    make_trend_mode,
    make_vwap_side,
    simulate_session,
)
from research.config import load_config
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")
ET = ZoneInfo("America/New_York")


def fmt(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(UK).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3] + " UK"


def uk_open_ns(ts_mid: int) -> int:
    d = datetime.fromtimestamp(ts_mid / 1e9, tz=timezone.utc).astimezone(UK).date()
    return int(datetime.combine(d, time(14, 30), tzinfo=UK).timestamp() * 1e9)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="021026")
    args = ap.parse_args()
    sid = args.session
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    nq = _load_trades(pq, sid, "NQ")
    es = _load_trades(pq, sid, "ES")
    if nq.num_rows == 0:
        raise SystemExit(f"no NQ trades for {sid}")

    mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
    vwap_from = uk_open_ns(mid)

    sim_cfg = MatSimConfig(slip_ticks=0.0)  # list entry px without slip for compare
    gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    summary = simulate_session(
        sid, nq, es, sim_cfg,
        gate=gate,
        regime_window_min=60,
        trend_window_min=30,
        vol_window_min=30,
        vwap_from_ns=vwap_from,
    )

    # Also count raw ES-confirmed bursts (pre-gate) for diagnostics
    raw = detect_signals(nq, es, sim_cfg)

    out_dir = Path(cfg["results_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"MAT2_REF_{sid}.md"
    csv_path = out_dir / f"MAT2_REF_{sid}.csv"

    lines = [
        f"# MAT2 replay reference — session `{sid}`",
        "",
        "Compare Bookmap log lines `MAT2 CANDIDATE PASS` / `MAT2 SIGNAL` to this list.",
        "",
        "## Settings (must match panel)",
        "",
        "- Bar 100, min trades 6, min vol 25, |imb| 0.55, progress 0.25 pts",
        "- ES sync 1500 ms, **Max ES lag 250**, causal (ES before/at NQ)",
        "- **Trend mode = 1 (with)**, trend window 30 — **completed 1m bars only**",
        "- **Require VWAP side = true**, VWAP from UK 14:30",
        "- **Regime max range = 0 (off)**",
        "- Flat-only, signal cooldown 5 s, TP=SL 10 pts",
        "- Verbose candidate log = true",
        "",
        f"Raw ES-confirmed bursts (pre-gate): **{raw.num_rows}**",
        f"PASS after gates + flat/cooldown: **{summary.n}**",
        f"Sim net (0 slip, ±10): **{summary.net:+.1f}** WR={summary.wr:.2f}",
        "",
        "## Expected PASS signals",
        "",
        "| # | UK time | side | entry_px | net30 | vwap | last | ts_ns |",
        "|--:|---|---|--:|--:|--:|--:|--:|",
    ]
    csv = ["n,uk_time,side,entry_px,net30,vwap,last_px,ts_ns,pnl,exit_reason"]
    for i, tr in enumerate(summary.trades, 1):
        side = "LONG" if tr.side > 0 else "SHORT"
        lines.append(
            f"| {i} | {fmt(tr.entry_ts_ns)} | {side} | {tr.entry_px:.2f} | "
            f"{tr.net30:.1f} | {tr.vwap:.2f} | {tr.entry_px:.2f} | {tr.entry_ts_ns} |"
        )
        csv.append(
            f"{i},{fmt(tr.entry_ts_ns)},{side},{tr.entry_px:.2f},{tr.net30:.2f},"
            f"{tr.vwap:.2f},{tr.entry_px:.2f},{tr.entry_ts_ns},{tr.pnl:.2f},{tr.reason}"
        )

    lines.extend([
        "",
        "## How to compare in Bookmap",
        "",
        "1. Install `bookmap-orderflow-mat-v2-diag.jar`, remove/re-add **Momentum AutoTrade v2**.",
        "2. Confirm panel: Trend mode=1, Require VWAP=true, Regime max range=0, Verbose=true, Max ES lag=250.",
        "3. Open Replay for this session with **NQ + ES**, attach v2 on NQ, trading OFF.",
        "4. After realtime start, grep the Bookmap log for `MAT2 CANDIDATE PASS` / `MAT2 SIGNAL`.",
        "5. Match `tsNs=` or UK time + side + px to the table above (px within ~0.25–0.5 pts).",
        "6. If you see many `MAT2 REJECT` with `no_ES_instrument` / `ES_no_same_side`, ES is not feeding.",
        "7. If `trend_mode_fail` / `vwap_side_fail` dominate vs this list, gate inputs disagree — paste a few REJECT lines.",
        "",
    ])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    csv_path.write_text("\n".join(csv) + "\n", encoding="utf-8")
    print(f"wrote {md_path} ({summary.n} signals)")
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    main()
