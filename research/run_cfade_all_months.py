"""CFADE sl1/tp2 OOS across all months with trades parquet (UK 14:30–17:00).

Freeze: against_m04 + skip_er0.3, vol_rr sl1/tp2, primary-feed only.
Writes results/CFADE_ALL_MONTHS_SL1TP2/ with per-month and overall summary.
"""
from __future__ import annotations

import json
from collections import defaultdict
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
from research.run_mat_may import day_equity_stats

COMMISSION = 0.25

MONTH_NAMES = {
    "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
    "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct",
}


def _sid_chrono_key(sid: str) -> tuple[str, str, str]:
    """DDMMYY → (YY, MM, DD) for calendar order."""
    return sid[4:6], sid[2:4], sid[0:2]


def _discover_sessions(pq: Path) -> list[str]:
    out = []
    for p in pq.iterdir():
        if not p.is_dir() or not p.name.startswith("session_id="):
            continue
        sid = p.name.replace("session_id=", "")
        if len(sid) != 6 or not sid.isdigit():
            continue
        if not (p / "trades.parquet").exists():
            continue
        out.append(sid)
    return sorted(out, key=_sid_chrono_key)


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "CFADE_ALL_MONTHS_SL1TP2"
    out.mkdir(parents=True, exist_ok=True)

    sessions = _discover_sessions(pq)
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
    for sid in sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            rows.append({
                "session": sid, "month": sid[2:4], "n": 0,
                "note": "no NQ/ES pair in window",
                "net": 0.0, "gross": 0.0, "equity_high": 0.0, "equity_end": 0.0,
                "day_max_dd": 0.0, "equity_path": [], "fades": 0, "~$": 0.0,
            })
            print(f"{sid} no pair", flush=True)
            continue

        nq_b, es_b = pair.bars(100)
        bursts = H.confirm_bursts(pair)
        fades = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=5.0)
        if fades.num_rows == 0:
            rows.append({
                "session": sid, "month": sid[2:4], "n": 0, "wr": None,
                "gross": 0.0, "net": 0.0, "equity_high": 0.0, "equity_end": 0.0,
                "day_max_dd": 0.0, "equity_path": [], "fades": 0, "~$": 0.0,
            })
            print(f"{sid} n=  0 fades=0 net=+0.0", flush=True)
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
            "month": sid[2:4],
            "fades": int(fades.num_rows),
            **d,
            "avg": round(s.net / s.n, 2) if s.n else None,
            "~$": round(s.net * 20, 0),
            **eq,
        }
        rows.append(row)
        print(
            f"{sid} n={row['n']:3d} fades={row['fades']:3d} net={row['net']:+8.1f} "
            f"high={row['equity_high']:+8.1f} end={row['equity_end']:+8.1f}",
            flush=True,
        )

    # portfolio path chronological
    port_eq = 0.0
    port_peak = 0.0
    port_max_dd = 0.0
    for r in rows:
        port_eq += r.get("net", 0.0) or 0.0
        port_peak = max(port_peak, port_eq)
        port_max_dd = max(port_max_dd, port_peak - port_eq)
        r["portfolio_equity_after"] = round(port_eq, 2)
        r["portfolio_high_after"] = round(port_peak, 2)

    # per-month rollups
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
        days_pos = sum(1 for x in nets if x > 0)
        days_traded = sum(1 for x in ns if x > 0)
        # month-internal portfolio path
        pe = 0.0
        ph = 0.0
        pdd = 0.0
        for x in nets:
            pe += x
            ph = max(ph, pe)
            pdd = max(pdd, ph - pe)
        month_summaries.append({
            "month": mm,
            "label": MONTH_NAMES.get(mm, mm),
            "sessions": len(rs),
            "days_traded": days_traded,
            "days_pos": days_pos,
            "n": n,
            "net": round(net, 2),
            "avg_trade": round(net / n, 2) if n else None,
            "portfolio_end": round(pe, 2),
            "portfolio_high": round(ph, 2),
            "portfolio_max_dd": round(pdd, 2),
            "worst_day": round(min(nets), 2) if nets else None,
            "best_day": round(max(nets), 2) if nets else None,
            "~$": round(net * 20, 0),
        })

    all_s = merge_summaries(parts, COMMISSION) if parts else None
    summary = all_s.to_dict() if all_s else {"n": 0, "net": 0.0, "wr": None, "gross": 0.0}
    days_pos = sum(1 for r in rows if (r.get("net") or 0) > 0)
    days_traded = sum(1 for r in rows if (r.get("n") or 0) > 0)

    report = {
        "window": "UK 14:30-17:00",
        "signal": "against_m04 + skip_er0.3 (DIG4 freeze)",
        "brackets": "vol_rr sl1/tp2 (k=1, floor=4, cap=40)",
        "costs": "1-tick slip, commission 0.25 pt/RT",
        "sessions": sessions,
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
        "by_month": month_summaries,
        "per_session": rows,
    }

    md = [
        "# CFADE all months — SL 1R / TP 2R",
        "",
        "Signal: **against_m04** + **skip_er0.3**, UK **14:30–17:00**, "
        "confirm stamp, flat-only, 5 s cooldown.",
        "Brackets: `make_vol_bracket_rr(1, tp_r=2, sl_r=1)` clip 4–40; "
        f"1-tick slip, commission {COMMISSION} pt/RT. Primary feed only.",
        "",
        "## Overall",
        "",
        f"- Sessions: **{len(rows)}** · Days traded: **{days_traded}** · "
        f"Days+: **{days_pos}**",
        f"- Trades: **{summary.get('n', 0)}**  WR={summary.get('wr')}  "
        f"gross={summary.get('gross')}  net=**{summary.get('net')}**",
        f"- Portfolio end / high / maxDD: "
        f"**{port_eq:+.1f}** / **{port_peak:+.1f}** / {port_max_dd:.1f}",
        "",
        "## By month",
        "",
        "| month | days | traded | days+ | n | net | high | maxDD | ~$ |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for m in month_summaries:
        md.append(
            f"| {m['label']} | {m['sessions']} | {m['days_traded']} | "
            f"{m['days_pos']} | {m['n']} | {m['net']:+.1f} | "
            f"{m['portfolio_high']:+.1f} | {m['portfolio_max_dd']:.1f} | "
            f"{m['~$']:.0f} |"
        )
    md += [
        "",
        "## Per session",
        "",
        "| session | fades | n | WR | net | equity_high | day DD | ~$ | portfolio after |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        if r.get("note"):
            md.append(f"| {r['session']} | — | — | — | — | — | — | — | {r['note']} |")
            continue
        wr = r.get("wr")
        wr_s = f"{wr:.2f}" if isinstance(wr, float) else "—"
        md.append(
            f"| {r['session']} | {r.get('fades', 0)} | {r['n']} | {wr_s} | "
            f"{r['net']:+.1f} | {r['equity_high']:+.1f} | {r['day_max_dd']:.1f} | "
            f"{r['~$']:.0f} | {r['portfolio_equity_after']:+.1f} |"
        )
    md.append("")

    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
    print("--- by month ---", flush=True)
    for m in month_summaries:
        print(
            f"{m['label']}: days={m['sessions']} n={m['n']} net={m['net']:+.1f} "
            f"high={m['portfolio_high']:+.1f} dd={m['portfolio_max_dd']:.1f}",
            flush=True,
        )
    print(
        f"ALL n={summary.get('n')} net={summary.get('net')} "
        f"port_high={port_peak:+.1f} port_end={port_eq:+.1f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
