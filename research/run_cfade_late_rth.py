"""CFADE sl1/tp2 on late RTH windows: UK 17:00–18:00 and 18:00–21:00.

Same freeze as afternoon: against_m04 + skip_er0.3, vol_rr sl1/tp2, primary feed.

Tape is loaded from 14:30 through window end so ER / vol brackets have the
same lookback as live. Only fades stamped inside the target window are traded.

Writes results/CFADE_LATE_RTH_SL1TP2/.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import time
from pathlib import Path

import numpy as np
import pyarrow as pa

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
from research.run_cfade_all_months import MONTH_NAMES, _discover_sessions
from research.run_mat_may import day_equity_stats

COMMISSION = 0.25
WARMUP_START = time(14, 30)

WINDOWS = [
    ("1700_1800", time(17, 0), time(18, 0), "UK 17:00-18:00"),
    ("1800_2100", time(18, 0), time(21, 0), "UK 18:00-21:00"),
]


def _filter_events(events: pa.Table, lo: int, hi: int) -> pa.Table:
    if events.num_rows == 0:
        return events
    ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    k = (ts >= lo) & (ts <= hi)
    return events.take(np.where(k)[0]) if k.any() else events.slice(0, 0)


def _run_window(
    pq: Path,
    sessions: list[str],
    win_start: time,
    win_end: time,
    label: str,
) -> dict:
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
    no_data = []
    for sid in sessions:
        # Warmup from RTH open through window end (ER/vol lookback).
        pair = H.load_pair(pq, sid, start=WARMUP_START, end=win_end)
        if pair is None:
            no_data.append(sid)
            rows.append({
                "session": sid, "month": sid[2:4], "n": 0,
                "note": "no NQ/ES in warmup→window",
                "net": 0.0, "gross": 0.0, "equity_high": 0.0, "equity_end": 0.0,
                "day_max_dd": 0.0, "fades": 0, "~$": 0.0,
            })
            continue

        mid = int(pair.nq.column("ts_ns").to_numpy()[len(pair.nq) // 2])
        win_bounds = H.uk_bounds(mid, start=win_start, end=win_end)
        # Require some tape inside the target window itself.
        in_win = H.filter_window(pair.nq, win_bounds)
        if in_win.num_rows < 50:
            no_data.append(sid)
            rows.append({
                "session": sid, "month": sid[2:4], "n": 0,
                "note": "thin/empty target window",
                "net": 0.0, "gross": 0.0, "equity_high": 0.0, "equity_end": 0.0,
                "day_max_dd": 0.0, "fades": 0, "~$": 0.0,
            })
            continue

        nq_b, es_b = pair.bars(100)
        bursts = H.confirm_bursts(pair)
        fades_all = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=5.0)
        fades = _filter_events(fades_all, win_bounds[0], win_bounds[1])
        if fades.num_rows == 0:
            rows.append({
                "session": sid, "month": sid[2:4], "n": 0, "wr": None,
                "gross": 0.0, "net": 0.0, "equity_high": 0.0, "equity_end": 0.0,
                "day_max_dd": 0.0, "fades": 0, "~$": 0.0,
            })
            print(f"  {sid} n=  0 fades=0", flush=True)
            continue

        # VWAP / daytype from RTH open, not window open.
        vwap_from = H.uk_bounds(mid, start=WARMUP_START, end=win_end)[0]
        s = simulate_session(
            sid, pair.nq, pair.es, sim_cfg,
            gate=gate,
            bracket_fn=br,
            vwap_from_ns=vwap_from,
            events=fades,
        )
        parts.append(s)
        eq = day_equity_stats(s.trades, COMMISSION)
        d = s.to_dict()
        row = {
            "session": sid,
            "month": sid[2:4],
            "fades": int(fades.num_rows),
            **d,
            "avg": round(s.net / s.n, 2) if s.n else None,
            "~$": round(s.net * 20, 0),
            **eq,
        }
        rows.append(row)
        print(
            f"  {sid} n={row['n']:3d} fades={row['fades']:3d} "
            f"net={row['net']:+8.1f} high={row['equity_high']:+8.1f}",
            flush=True,
        )

    port_eq = port_peak = port_max_dd = 0.0
    for r in rows:
        port_eq += r.get("net", 0.0) or 0.0
        port_peak = max(port_peak, port_eq)
        port_max_dd = max(port_max_dd, port_peak - port_eq)
        r["portfolio_equity_after"] = round(port_eq, 2)

    by_m: dict[str, list] = defaultdict(list)
    for r in rows:
        by_m[r["month"]].append(r)
    month_summaries = []
    for mm in sorted(by_m.keys()):
        rs = by_m[mm]
        nets = [r.get("net") or 0.0 for r in rs]
        ns = [r.get("n") or 0 for r in rs]
        n = sum(ns)
        net = sum(nets)
        pe = ph = pdd = 0.0
        for x in nets:
            pe += x
            ph = max(ph, pe)
            pdd = max(pdd, ph - pe)
        month_summaries.append({
            "month": mm,
            "label": MONTH_NAMES.get(mm, mm),
            "sessions": len(rs),
            "days_traded": sum(1 for x in ns if x > 0),
            "days_pos": sum(1 for x in nets if x > 0),
            "n": n,
            "net": round(net, 2),
            "portfolio_high": round(ph, 2),
            "portfolio_max_dd": round(pdd, 2),
            "~$": round(net * 20, 0),
        })

    all_s = merge_summaries(parts, COMMISSION) if parts else None
    summary = all_s.to_dict() if all_s else {"n": 0, "net": 0.0, "wr": None, "gross": 0.0}
    return {
        "window": label,
        "warmup": "UK 14:30 → window end; fades filtered to window",
        "no_data_sessions": no_data,
        "summary": {
            **summary,
            "days": len(rows),
            "days_traded": sum(1 for r in rows if (r.get("n") or 0) > 0),
            "days_pos": sum(1 for r in rows if (r.get("net") or 0) > 0),
            "portfolio_equity_end": round(port_eq, 2),
            "portfolio_equity_high": round(port_peak, 2),
            "portfolio_max_dd": round(port_max_dd, 2),
        },
        "by_month": month_summaries,
        "per_session": rows,
    }


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "CFADE_LATE_RTH_SL1TP2"
    out.mkdir(parents=True, exist_ok=True)
    sessions = _discover_sessions(pq)

    reports = {}
    md = [
        "# CFADE late RTH — SL 1R / TP 2R",
        "",
        "Same freeze as 14:30–17:00: **against_m04** + **skip_er0.3**, "
        "vol_rr sl1/tp2, primary feed, 1-tick slip, 0.25 pt/RT.",
        "",
        "Tape loaded **14:30 → window end** (ER/vol lookback). Only fades "
        "stamped inside the target window are traded.",
        "",
    ]

    for key, start, end, label in WINDOWS:
        print(f"=== {label} ===", flush=True)
        rep = _run_window(pq, sessions, start, end, label)
        reports[key] = rep
        s = rep["summary"]
        md += [
            f"## {label}",
            "",
            f"- Sessions: **{s['days']}** · traded: **{s['days_traded']}** · "
            f"days+: **{s['days_pos']}**"
            + (f" · no/thin data: {len(rep['no_data_sessions'])}" if rep["no_data_sessions"] else ""),
            f"- Trades: **{s.get('n', 0)}**  WR={s.get('wr')}  "
            f"gross={s.get('gross')}  net=**{s.get('net')}**",
            f"- Portfolio end / high / maxDD: "
            f"**{s['portfolio_equity_end']:+.1f}** / "
            f"**{s['portfolio_equity_high']:+.1f}** / {s['portfolio_max_dd']:.1f}",
            "",
            "| month | days | traded | days+ | n | net | high | maxDD | ~$ |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for m in rep["by_month"]:
            md.append(
                f"| {m['label']} | {m['sessions']} | {m['days_traded']} | "
                f"{m['days_pos']} | {m['n']} | {m['net']:+.1f} | "
                f"{m['portfolio_high']:+.1f} | {m['portfolio_max_dd']:.1f} | "
                f"{m['~$']:.0f} |"
            )
        md.append("")
        print(
            f"{label}: n={s.get('n')} net={s.get('net')} "
            f"high={s['portfolio_equity_high']:+.1f} end={s['portfolio_equity_end']:+.1f}",
            flush=True,
        )

    # Side-by-side vs afternoon reference if present
    aft = Path(cfg["results_dir"]) / "CFADE_ALL_MONTHS_SL1TP2" / "report.json"
    if aft.exists():
        aft_rep = json.loads(aft.read_text(encoding="utf-8"))
        a = aft_rep.get("summary", {})
        md += [
            "## vs afternoon (14:30–17:00)",
            "",
            "| window | n | WR | net | high | maxDD |",
            "|---|---:|---:|---:|---:|---:|",
            f"| 14:30-17:00 | {a.get('n')} | {a.get('wr')} | {a.get('net')} | "
            f"{a.get('portfolio_equity_high')} | {a.get('portfolio_max_dd')} |",
        ]
        for key, _, _, label in WINDOWS:
            s = reports[key]["summary"]
            md.append(
                f"| {label} | {s.get('n')} | {s.get('wr')} | {s.get('net')} | "
                f"{s.get('portfolio_equity_high')} | {s.get('portfolio_max_dd')} |"
            )
        md.append("")

    (out / "report.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
