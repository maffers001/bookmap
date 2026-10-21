"""CFADE sl1/tp2 OOS on May 2026 sessions (UK 14:30–17:00).

Frozen signal: against_m04 + skip_er0.3 (EXP-0036_DIG4).
Brackets: make_vol_bracket_rr(k=1, sl_r=1, tp_r=2), clip 4–40.
1-tick slip, 0.25 pt/RT commission.

Uses ``harness.load_pair`` single-feed filter (meta ``primary_alias``, usually
Rithmic) so results match Bookmap live/replay. Merging TV+Rithmic aliases
inflates tape density and is not used.

Per day: n, WR, gross, net, equity_high (peak of running day P&L), end equity.
Writes results/CFADE_MAY2026_SL1TP2/.
"""
from __future__ import annotations

import json
from pathlib import Path

from research.backtest.mat_sim import (
    MatSimConfig,
    make_daytype_skip,
    make_vol_bracket_rr,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.correlated import harness as H
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events
from research.run_mat_may import MAY, day_equity_stats

COMMISSION = 0.25


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "CFADE_MAY2026_SL1TP2"
    out.mkdir(parents=True, exist_ok=True)

    gate = make_daytype_skip(max_er=0.3)
    br = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0)
    sim_cfg = MatSimConfig(
        slip_ticks=1.0,
        commission_pts_rt=COMMISSION,
        signal_cooldown_sec=5.0,
        tp_pts=10,
        sl_pts=10,
    )
    p_ex = ExhaustParams(
        es_imb_max=-0.4, es_min_vol=0, use_imb=True, use_vol=False,
        nq_min_imbalance=0.55, nq_min_vol=25, nq_min_trades=6,
    )

    rows = []
    parts = []
    missing = []
    for sid in MAY:
        if not (pq / f"session_id={sid}" / "trades.parquet").exists():
            missing.append(sid)
            continue
        pair = H.load_pair(pq, sid)
        if pair is None:
            rows.append({
                "session": sid, "n": 0, "note": "no NQ/ES pair in window",
                "net": 0.0, "gross": 0.0, "equity_high": 0.0, "equity_end": 0.0,
                "day_max_dd": 0.0, "equity_path": [],
            })
            continue

        nq_b, es_b = pair.bars(100)
        bursts = H.confirm_bursts(pair)
        fades = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=5.0)
        if fades.num_rows == 0:
            rows.append({
                "session": sid, "n": 0, "wr": None, "gross": 0.0, "net": 0.0,
                "equity_high": 0.0, "equity_end": 0.0, "day_max_dd": 0.0,
                "equity_path": [], "fades": 0, "~$": 0.0,
            })
            print(f"{sid} n=  0 fades=0 net=+0.0 high=+0.0", flush=True)
            continue

        s = simulate_session(
            sid, pair.nq, pair.es, sim_cfg,
            gate=gate,
            bracket_fn=br,
            vwap_from_ns=pair.vwap_from_ns,
            events=fades,
        )
        parts.append(s)
        eq = day_equity_stats(s.trades, COMMISSION)
        d = s.to_dict()
        row = {
            "session": sid,
            "fades": int(fades.num_rows),
            **d,
            "avg": round(s.net / s.n, 2) if s.n else None,
            "~$": round(s.net * 20, 0),
            **eq,
        }
        rows.append(row)
        print(
            f"{sid} n={row['n']:3d} fades={row['fades']:3d} net={row['net']:+8.1f} "
            f"high={row['equity_high']:+8.1f} end={row['equity_end']:+8.1f} "
            f"dd={row['day_max_dd']:.1f}",
            flush=True,
        )

    all_s = merge_summaries(parts, COMMISSION) if parts else None
    port_eq = 0.0
    port_peak = 0.0
    port_max_dd = 0.0
    for r in rows:
        port_eq += r.get("net", 0.0) or 0.0
        port_peak = max(port_peak, port_eq)
        port_max_dd = max(port_max_dd, port_peak - port_eq)
        r["portfolio_equity_after"] = round(port_eq, 2)
        r["portfolio_high_after"] = round(port_peak, 2)

    summary = all_s.to_dict() if all_s else {"n": 0, "net": 0.0, "wr": None, "gross": 0.0}
    days_pos = sum(1 for r in rows if (r.get("net") or 0) > 0)
    days_traded = sum(1 for r in rows if (r.get("n") or 0) > 0)
    report = {
        "window": "UK 14:30-17:00",
        "signal": "against_m04 + skip_er0.3 (DIG4 freeze)",
        "brackets": "vol_rr sl1/tp2 (k=1, floor=4, cap=40)",
        "costs": "1-tick slip, commission 0.25 pt/RT",
        "sessions_requested": MAY,
        "missing_parquet": missing,
        "summary": {
            **summary,
            "days": len(rows),
            "days_traded": days_traded,
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
        "# CFADE May 2026 OOS — SL 1R / TP 2R",
        "",
        "Signal: **against_m04** + **skip_er0.3**, UK **14:30–17:00**, "
        "confirm stamp, flat-only, 5 s cooldown.",
        "Brackets: `make_vol_bracket_rr(1, tp_r=2, sl_r=1)` clip 4–40; "
        f"1-tick slip, commission {COMMISSION} pt/RT.",
        "",
        "**equity_high** = peak of running day P&L (from flat at 14:30) within "
        "that session's closed trades.",
        "",
        "## Summary",
        "",
        f"- Sessions with parquet: **{len(rows)}** / {len(MAY)}"
        + (f" (missing: {', '.join(missing)})" if missing else ""),
        f"- Days with ≥1 trade: **{days_traded}** · Days+: **{days_pos}** / {len(rows)}",
        f"- Trades: **{summary.get('n', 0)}**  WR={summary.get('wr')}  "
        f"gross={summary.get('gross')}  net=**{summary.get('net')}**",
        f"- Portfolio end / high / maxDD: "
        f"**{port_eq:+.1f}** / **{port_peak:+.1f}** / {port_max_dd:.1f}",
        "",
        "## Per session",
        "",
        "| session | fades | n | WR | gross | net | equity_high | equity_end | day DD | ~$ | portfolio after |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        if r.get("note"):
            md.append(
                f"| {r['session']} | — | — | — | — | — | — | — | — | — | {r['note']} |"
            )
            continue
        wr = r.get("wr")
        wr_s = f"{wr:.2f}" if isinstance(wr, float) else "—"
        md.append(
            f"| {r['session']} | {r.get('fades', 0)} | {r['n']} | {wr_s} | "
            f"{r.get('gross', 0):+.1f} | {r['net']:+.1f} | {r['equity_high']:+.1f} | "
            f"{r['equity_end']:+.1f} | {r['day_max_dd']:.1f} | {r['~$']:.0f} | "
            f"{r['portfolio_equity_after']:+.1f} |"
        )
    md.append("")

    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
    print(
        f"MAY CFADE n={summary.get('n')} net={summary.get('net')} "
        f"port_high={port_peak:+.1f} port_end={port_eq:+.1f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
