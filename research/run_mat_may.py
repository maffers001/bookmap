"""MAT vol k=1 OOS on May 2026 sessions (UK 14:30–17:00).

Frozen base: trend+VWAP, causal ES, 1-tick slip, 0.25 pt/RT, vol k=1.
Per day: n, WR, gross, net, equity_high (peak of running day P&L), end equity.
Writes results/MAT2_MAY2026_VOLK1/.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    combine_gates,
    make_trend_mode,
    make_vol_bracket,
    make_vwap_side,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25

# DDMMYY — May 2026
MAY = [
    "010526", "040526", "050526", "070526", "080526",
    "110526", "120526", "130526", "140526", "150526",
    "180526", "190526", "200526", "210526", "260526",
    "280526", "290526",
]


def bounds(mid: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(mid / 1e9, tz=timezone.utc).astimezone(UK).date()
    return (
        int(datetime.combine(d, time(14, 30), tzinfo=UK).timestamp() * 1e9),
        int(datetime.combine(d, time(17, 0), tzinfo=UK).timestamp() * 1e9),
    )


def filt(t: pa.Table) -> pa.Table:
    if t.num_rows == 0:
        return t
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    s, e = bounds(int(ts[len(ts) // 2]))
    k = (ts >= s) & (ts <= e)
    return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)


def day_equity_stats(trades, commission: float) -> dict:
    """Chronological running equity within the day; equity_high = peak."""
    ordered = sorted(trades, key=lambda tr: tr.entry_ts_ns)
    equity = 0.0
    peak = 0.0
    trough_from_peak = 0.0
    path = []
    for tr in ordered:
        equity += tr.pnl - commission
        peak = max(peak, equity)
        trough_from_peak = max(trough_from_peak, peak - equity)
        path.append(round(equity, 2))
    return {
        "equity_end": round(equity, 2),
        "equity_high": round(peak, 2),
        "day_max_dd": round(trough_from_peak, 2),
        "equity_path": path,
    }


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "MAT2_MAY2026_VOLK1"
    out.mkdir(parents=True, exist_ok=True)

    gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    br = make_vol_bracket(1.0)
    sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")

    rows = []
    parts = []
    missing = []
    for sid in MAY:
        if not (pq / f"session_id={sid}" / "trades.parquet").exists():
            missing.append(sid)
            continue
        nq = filt(_load_trades(pq, sid, "NQ"))
        es0 = _load_trades(pq, sid, "ES")
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows == 0:
            rows.append({
                "session": sid, "n": 0, "note": "no NQ in window",
                "net": 0.0, "gross": 0.0, "equity_high": 0.0, "equity_end": 0.0,
            })
            continue
        mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
        s = simulate_session(
            sid, nq, es, sim_cfg, gate=gate, bracket_fn=br, vwap_from_ns=bounds(mid)[0],
        )
        parts.append(s)
        eq = day_equity_stats(s.trades, COMMISSION)
        d = s.to_dict()
        row = {
            "session": sid,
            **d,
            "avg": round(s.net / s.n, 2) if s.n else None,
            "~$": round(s.net * 20, 0),
            **eq,
        }
        rows.append(row)
        print(
            f"{sid} n={row['n']:3d} net={row['net']:+8.1f} "
            f"high={row['equity_high']:+8.1f} end={row['equity_end']:+8.1f} "
            f"dd={row['day_max_dd']:.1f}",
            flush=True,
        )

    all_s = merge_summaries(parts, COMMISSION) if parts else None
    # Portfolio equity path across days (session order as listed)
    port_eq = 0.0
    port_peak = 0.0
    port_max_dd = 0.0
    for r in rows:
        port_eq += r.get("net", 0.0) or 0.0
        port_peak = max(port_peak, port_eq)
        port_max_dd = max(port_max_dd, port_peak - port_eq)
        r["portfolio_equity_after"] = round(port_eq, 2)
        r["portfolio_high_after"] = round(port_peak, 2)

    summary = all_s.to_dict() if all_s else {"n": 0, "net": 0}
    days_pos = sum(1 for r in rows if (r.get("net") or 0) > 0)
    report = {
        "window": "UK 14:30-17:00",
        "base": "vol_k1 causal trend+VWAP 1-tick commission 0.25",
        "sessions_requested": MAY,
        "missing_parquet": missing,
        "summary": {
            **summary,
            "days": len([r for r in rows if r.get("n", 0) > 0 or "note" not in r]),
            "days_pos": days_pos,
            "portfolio_equity_end": round(port_eq, 2),
            "portfolio_equity_high": round(port_peak, 2),
            "portfolio_max_dd": round(port_max_dd, 2),
            "worst_day": min((r.get("net") or 0) for r in rows) if rows else None,
            "best_day": max((r.get("net") or 0) for r in rows) if rows else None,
        },
        "per_session": rows,
    }

    md = [
        "# MAT2 May 2026 OOS — vol k=1",
        "",
        "Frozen base: **vol k=1**, causal ES, trend=1 + VWAP, UK **14:30–17:00**, "
        f"1-tick slip, commission {COMMISSION} pt/RT.",
        "",
        f"**equity_high** = peak of running day P&L (from flat at 14:30) within that session's trades.",
        "",
        "## Summary",
        "",
        f"- Sessions with data: **{len(rows) - len([r for r in rows if r.get('note')])}** / {len(MAY)}"
        + (f" (missing parquet: {', '.join(missing)})" if missing else ""),
        f"- Trades: **{summary.get('n', 0)}**  WR={summary.get('wr')}  "
        f"gross={summary.get('gross')}  net=**{summary.get('net')}**",
        f"- Days+: **{days_pos}** / {len(rows)}",
        f"- Portfolio end / high / maxDD: "
        f"**{port_eq:+.1f}** / **{port_peak:+.1f}** / {port_max_dd:.1f}",
        "",
        "## Per session",
        "",
        "| session | n | WR | gross | net | equity_high | equity_end | day DD | ~$ | portfolio after |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        if r.get("note"):
            md.append(f"| {r['session']} | — | — | — | — | — | — | — | — | {r['note']} |")
            continue
        md.append(
            f"| {r['session']} | {r['n']} | {r['wr']} | {r.get('gross', 0):+.1f} | "
            f"{r['net']:+.1f} | {r['equity_high']:+.1f} | {r['equity_end']:+.1f} | "
            f"{r['day_max_dd']:.1f} | {r['~$']:.0f} | {r['portfolio_equity_after']:+.1f} |"
        )
    md.append("")

    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
    print(
        f"MAY total n={summary.get('n')} net={summary.get('net')} "
        f"port_high={port_peak:+.1f} port_end={port_eq:+.1f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
