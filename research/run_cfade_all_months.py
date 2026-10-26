"""CFADE OOS across all months with trades parquet (UK 14:30–17:00).

Freeze signal: against_m04 + skip_er0.3, primary-feed only.
Default brackets: vol_rr sl1/tp2. Optional --tp-r / --sl-r for other R:R.
Pass multiple --variant sl1tp2,sl1tp3 to run several brackets in one tape pass.

Writes results/CFADE_ALL_MONTHS_<TAG>/.
"""
from __future__ import annotations

import argparse
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
# May tapes misfiled under June names (see CFADE anatomy / June audit).
EXCLUDE_SESSIONS = {"200626", "220626"}

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


def _tag(sl_r: float, tp_r: float) -> str:
    def fmt(x: float) -> str:
        return str(int(x)) if float(x).is_integer() else str(x).replace(".", "p")
    return f"SL{fmt(sl_r)}TP{fmt(tp_r)}"


def _rollup(rows: list[dict], parts: list, sessions: list[str], sl_r: float, tp_r: float) -> dict:
    port_eq = port_peak = port_max_dd = 0.0
    for r in rows:
        port_eq += r.get("net", 0.0) or 0.0
        port_peak = max(port_peak, port_eq)
        port_max_dd = max(port_max_dd, port_peak - port_eq)
        r["portfolio_equity_after"] = round(port_eq, 2)
        r["portfolio_high_after"] = round(port_peak, 2)

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
    return {
        "window": "UK 14:30-17:00",
        "signal": "against_m04 + skip_er0.3 (DIG4 freeze)",
        "brackets": f"vol_rr sl{sl_r}/tp{tp_r} (k=1, floor=4, cap=40)",
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
        "_port": (port_eq, port_peak, port_max_dd),
    }


def _write_report(out: Path, report: dict, sl_r: float, tp_r: float) -> None:
    out.mkdir(parents=True, exist_ok=True)
    summary = report["summary"]
    port_eq, port_peak, port_max_dd = report.pop("_port")
    month_summaries = report["by_month"]
    rows = report["per_session"]

    md = [
        f"# CFADE all months — SL {sl_r:g}R / TP {tp_r:g}R",
        "",
        "Signal: **against_m04** + **skip_er0.3**, UK **14:30–17:00**, "
        "confirm stamp, flat-only, 5 s cooldown.",
        f"Brackets: `make_vol_bracket_rr(1, tp_r={tp_r:g}, sl_r={sl_r:g})` clip 4–40; "
        f"1-tick slip, commission {COMMISSION} pt/RT. Primary feed only.",
        f"Excluded mislabeled sessions: {sorted(EXCLUDE_SESSIONS)}.",
        "",
        "## Overall",
        "",
        f"- Sessions: **{len(rows)}** · Days traded: **{summary['days_traded']}** · "
        f"Days+: **{summary['days_pos']}**",
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
    print(f"--- {_tag(sl_r, tp_r)} by month ---", flush=True)
    for m in month_summaries:
        print(
            f"{m['label']}: days={m['sessions']} n={m['n']} net={m['net']:+.1f} "
            f"high={m['portfolio_high']:+.1f} dd={m['portfolio_max_dd']:.1f}",
            flush=True,
        )
    print(
        f"{_tag(sl_r, tp_r)} ALL n={summary.get('n')} net={summary.get('net')} "
        f"port_high={port_peak:+.1f} port_end={port_eq:+.1f}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--variant",
        action="append",
        default=None,
        help="sl_r:tp_r e.g. 1:2 or 1:3 (repeatable). Default: 1:2 and 1:3.",
    )
    args = ap.parse_args(argv)
    raw_vars = args.variant or ["1:2", "1:3"]
    variants: list[tuple[float, float]] = []
    for v in raw_vars:
        a, b = v.split(":")
        variants.append((float(a), float(b)))

    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    results = Path(cfg["results_dir"])
    sessions = [s for s in _discover_sessions(pq) if s not in EXCLUDE_SESSIONS]
    skipped = sorted(EXCLUDE_SESSIONS & set(_discover_sessions(pq)))
    if skipped:
        print(f"excluding mislabeled sessions: {skipped}", flush=True)

    gate = make_daytype_skip(max_er=0.3)
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
    brackets = {
        (sl, tp): make_vol_bracket_rr(1.0, tp_r=tp, sl_r=sl, floor=4.0, cap=40.0)
        for sl, tp in variants
    }

    # per-variant accumulators
    rows_v: dict[tuple[float, float], list] = {k: [] for k in brackets}
    parts_v: dict[tuple[float, float], list] = {k: [] for k in brackets}

    for sid in sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            for k in brackets:
                rows_v[k].append({
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
            for k in brackets:
                rows_v[k].append({
                    "session": sid, "month": sid[2:4], "n": 0, "wr": None,
                    "gross": 0.0, "net": 0.0, "equity_high": 0.0, "equity_end": 0.0,
                    "day_max_dd": 0.0, "equity_path": [], "fades": 0, "~$": 0.0,
                })
            print(f"{sid} n=  0 fades=0", flush=True)
            continue

        line_bits = [f"{sid} fades={fades.num_rows}"]
        for (sl_r, tp_r), br in brackets.items():
            s = simulate_session(
                sid, pair.nq, pair.es, sim_cfg,
                gate=gate,
                bracket_fn=br,
                vwap_from_ns=pair.vwap_from_ns,
                events=fades,
            )
            parts_v[(sl_r, tp_r)].append(s)
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
            rows_v[(sl_r, tp_r)].append(row)
            line_bits.append(f"{_tag(sl_r, tp_r)} n={row['n']} net={row['net']:+.1f}")
        print(" | ".join(line_bits), flush=True)

    for (sl_r, tp_r) in variants:
        tag = _tag(sl_r, tp_r)
        # Keep legacy folder name for 1:2
        folder = "CFADE_ALL_MONTHS_SL1TP2" if (sl_r, tp_r) == (1.0, 2.0) else f"CFADE_ALL_MONTHS_{tag}"
        report = _rollup(rows_v[(sl_r, tp_r)], parts_v[(sl_r, tp_r)], sessions, sl_r, tp_r)
        _write_report(results / folder, report, sl_r, tp_r)


if __name__ == "__main__":
    main()
