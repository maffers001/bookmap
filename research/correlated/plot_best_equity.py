"""Standalone equity curves for best CFADE-family strategies.

Strategies
  1. CFADE frozen
  2. EXP-0061 confirm-burst continuation
  3. MAT frozen (causal, trend+VWAP, vol k=1)
  4. MAT + skip_range175
  5. Allocator (c) = MAT(ER>0.3 & range<=175) + CFADE independent sum
     (EXP-0057 ship, without 0061)

Writes results/BEST_STRATEGY_EQUITY/{summary.md, daily.csv, equity_*.svg}.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from research.backtest.mat_sim import combine_gates, make_daytype_skip
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.correlated.study_combined_book import _plot_equity
from research.features.corr_exhaust import ExhaustParams, build_exhaust_exit_fn
from research.run_cfade_all_months import _sid_chrono_key

COMM = F.COMMISSION


def _day_nets_from_trades(trades) -> dict[str, float]:
    d: dict[str, float] = defaultdict(float)
    for t in trades:
        d[t.session_id] += t.pnl - COMM
    return dict(d)


def _path(day_nets: dict[str, float], sessions: list[str]) -> list[dict]:
    pe = ph = pdd = 0.0
    rows = []
    for sid in sessions:
        pe += day_nets.get(sid, 0.0)
        ph = max(ph, pe)
        pdd = max(pdd, ph - pe)
        rows.append({
            "session": sid,
            "month": sid[2:4],
            "day_net": round(day_nets.get(sid, 0.0), 2),
            "equity": round(pe, 2),
        })
    return rows


def _stats(path: list[dict], n_trades: int, wins: int) -> dict:
    nets = [r["day_net"] for r in path]
    eq = [r["equity"] for r in path]
    pe = eq[-1] if eq else 0.0
    ph = max(eq) if eq else 0.0
    # maxDD from path
    peak = dd = 0.0
    for e in eq:
        peak = max(peak, e)
        dd = max(dd, peak - e)
    by_m: dict[str, float] = defaultdict(float)
    for r in path:
        by_m[r["month"]] += r["day_net"]
    june = by_m.get("06", 0.0)
    return {
        "n": n_trades,
        "wr": round(wins / n_trades, 3) if n_trades else None,
        "net": round(pe, 2),
        "high": round(ph, 2),
        "max_dd": round(dd, 2),
        "days_pos": sum(1 for x in nets if x > 0),
        "days": sum(1 for x in nets if True),
        "june_net": round(june, 2),
        "june_share": round(june / pe, 3) if abs(pe) > 1e-9 else None,
        "by_month": {k: round(v, 2) for k, v in sorted(by_m.items())},
    }


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "BEST_STRATEGY_EQUITY"
    out.mkdir(parents=True, exist_ok=True)
    sessions = F.clean_sessions(pq)

    books: dict[str, list] = {
        "cfade": [],
        "cont0061": [],
        "mat": [],
        "mat_skip_range175": [],
        "mat_c": [],  # ER>0.3 & range<=175 only
    }

    gate_mat = F.mat_frozen_gate()
    gate_r175 = combine_gates(gate_mat, make_daytype_skip(max_range=175.0))
    br_mat = F.mat_vol_bracket()
    p_ex = ExhaustParams(
        es_imb_max=-0.4, es_min_vol=0, use_imb=True, use_vol=False,
        nq_min_imbalance=0.55, nq_min_vol=25, nq_min_trades=6,
        max_wait_sec=5.0,
    )

    for sid in sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            print(f"{sid} no pair", flush=True)
            continue

        sc = F.simulate_cfade(pair)
        books["cfade"].extend(sc.trades)

        nq_b, es_b = pair.bars(100)
        confirm = H.confirm_bursts(pair)
        if confirm.num_rows:
            s61 = F.simulate_cfade(
                pair, events=confirm,
                exit_fn=build_exhaust_exit_fn(nq_b, es_b, p_ex),
            )
            books["cont0061"].extend(s61.trades)

        sm = F.simulate_mat(pair, gate=gate_mat, bracket_fn=br_mat)
        books["mat"].extend(sm.trades)

        sm175 = F.simulate_mat(pair, gate=gate_r175, bracket_fn=br_mat)
        books["mat_skip_range175"].extend(sm175.trades)

        for t in sm.trades:
            if t.er60 is not None and np.isfinite(t.er60) and t.er60 > 0.30:
                if t.range60 is not None and np.isfinite(t.range60) and t.range60 <= 175.0:
                    books["mat_c"].append(t)

        print(
            f"{sid} cfade={sc.n} 0061={len(books['cont0061'])} mat={sm.n}",
            flush=True,
        )

    # day nets
    day = {k: _day_nets_from_trades(v) for k, v in books.items()}
    # allocator c = mat_c + cfade
    day["allocator_c"] = {
        s: day["mat_c"].get(s, 0.0) + day["cfade"].get(s, 0.0) for s in sessions
    }

    titles = {
        "cfade": "CFADE frozen (against_m04 + ER<=0.30, SL1/TP2 cap40)",
        "cont0061": "EXP-0061 confirm-burst continuation + ES-against exit",
        "mat": "MAT frozen (causal, trend+VWAP, vol k=1)",
        "mat_skip_range175": "MAT + skip_range175",
        "mat_c": "MAT allocator-c filter only (ER>0.30 and range<=175)",
        "allocator_c": "EXP-0057 allocator (c): MAT(c) + CFADE (independent sum)",
    }

    summary_rows = []
    daily_wide: dict[str, dict] = {s: {"session": s, "month": s[2:4]} for s in sessions}

    for key, title in titles.items():
        trades = books.get(key, [])
        if key == "allocator_c":
            n = len(books["mat_c"]) + len(books["cfade"])
            wins = sum(1 for t in books["mat_c"] if t.pnl - COMM > 0) + sum(
                1 for t in books["cfade"] if t.pnl - COMM > 0
            )
        else:
            n = len(trades)
            wins = sum(1 for t in trades if t.pnl - COMM > 0)
        path = _path(day[key], sessions)
        st = _stats(path, n, wins)
        _plot_equity(path, out / f"equity_{key}.svg", title)
        summary_rows.append({"key": key, "title": title, **st})
        for r in path:
            daily_wide[r["session"]][f"{key}_net"] = r["day_net"]
            daily_wide[r["session"]][f"{key}_eq"] = r["equity"]
        print(
            f"{key}: n={n} net={st['net']:+.1f} maxDD={st['max_dd']:.1f} "
            f"june={st['june_net']:+.1f} share={st['june_share']}",
            flush=True,
        )

    # Overlay chart (all strategies, normalized optional — raw pts)
    _plot_overlay(
        {k: _path(day[k], sessions) for k in titles},
        out / "equity_overlay.svg",
        titles,
    )

    # daily csv
    fieldnames = ["session", "month"] + [
        x for k in titles for x in (f"{k}_net", f"{k}_eq")
    ]
    with (out / "daily.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for sid in sessions:
            w.writerow(daily_wide[sid])

    md = [
        "# Best-strategy standalone equity curves",
        "",
        "113 clean sessions (excl. 200626/220626). Costs 1-tick + 0.25 pt/RT.",
        "",
        "## Summary",
        "",
        "| strategy | n | WR | net | maxDD | days+ | June net | June share |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary_rows:
        md.append(
            f"| {r['key']} | {r['n']} | {r['wr']} | {r['net']:+.1f} | {r['max_dd']:.1f} | "
            f"{r['days_pos']}/{r['days']} | {r['june_net']:+.1f} | {r['june_share']} |"
        )
    md += [
        "",
        "## By month (net pts)",
        "",
        "| strategy | " + " | ".join(f"{m:02}" if False else m for m in ["03", "04", "05", "06", "07", "08", "09", "10"]) + " |",
        "|---|" + "|".join(["---:"] * 8) + "|",
    ]
    # fix month header
    months = ["03", "04", "05", "06", "07", "08", "09", "10"]
    md[-2] = "| strategy | " + " | ".join(months) + " |"
    for r in summary_rows:
        bm = r["by_month"]
        md.append(
            "| " + r["key"] + " | " + " | ".join(f"{bm.get(m, 0):+.0f}" for m in months) + " |"
        )
    md += [
        "",
        "## Charts",
        "",
        "![overlay](equity_overlay.svg)",
        "",
    ]
    for r in summary_rows:
        md.append(f"### {r['key']}")
        md.append("")
        md.append(f"![{r['key']}](equity_{r['key']}.svg)")
        md.append("")

    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    (out / "report.json").write_text(json.dumps({"strategies": summary_rows}, indent=2), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


def _plot_overlay(paths: dict[str, list[dict]], out_path: Path, titles: dict[str, str]) -> None:
    colors = {
        "cfade": "#1f4e79",
        "cont0061": "#2a9d8f",
        "mat": "#e76f51",
        "mat_skip_range175": "#f4a261",
        "mat_c": "#9b2226",
        "allocator_c": "#6a4c93",
    }
    # use first path for x labels
    any_path = next(iter(paths.values()))
    labels = [r["session"] for r in any_path]
    n = len(labels)
    all_ys = [float(r["equity"]) for path in paths.values() for r in path]
    w, h = 1000, 420
    pad_l, pad_r, pad_t, pad_b = 64, 160, 40, 56
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    ymin = min(0.0, min(all_ys))
    ymax = max(0.0, max(all_ys))
    span = ymax - ymin or 1.0
    ymin -= 0.05 * span
    ymax += 0.05 * span

    def x_at(i: int) -> float:
        return pad_l + (i / max(n - 1, 1)) * plot_w

    def y_at(v: float) -> float:
        return pad_t + (1.0 - (v - ymin) / (ymax - ymin)) * plot_h

    yticks = np.linspace(ymin, ymax, 5)
    grid = []
    for v in yticks:
        yy = y_at(v)
        grid.append(
            f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{w-pad_r}" y2="{yy:.1f}" stroke="#ddd"/>'
            f'<text x="{pad_l-8}" y="{yy+4:.1f}" text-anchor="end" font-size="11" fill="#444">{v:.0f}</text>'
        )
    polylines = []
    legend = []
    for li, (key, path) in enumerate(paths.items()):
        col = colors.get(key, "#333")
        ys = [float(r["equity"]) for r in path]
        pts = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(ys))
        polylines.append(
            f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2"/>'
        )
        ly = pad_t + 14 + li * 18
        legend.append(
            f'<rect x="{w-pad_r+12}" y="{ly-10}" width="14" height="3" fill="{col}"/>'
            f'<text x="{w-pad_r+30}" y="{ly}" font-size="11" fill="#222">{key}</text>'
        )
    step = max(1, n // 10)
    xtick_i = list(range(0, n, step)) + ([n - 1] if n - 1 not in range(0, n, step) else [])
    xticks = []
    for i in xtick_i:
        xx = x_at(i)
        xticks.append(
            f'<text x="{xx:.1f}" y="{h-16}" text-anchor="middle" font-size="10" fill="#444" '
            f'transform="rotate(-40 {xx:.1f},{h-16})">{labels[i]}</text>'
        )
    y0 = y_at(0.0)
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <rect width="100%" height="100%" fill="#fafafa"/>
  <text x="{(w-pad_r)/2}" y="24" text-anchor="middle" font-size="15" fill="#222">
    Best strategies — standalone equity (NQ pts)
  </text>
  {''.join(grid)}
  <line x1="{pad_l}" y1="{y0:.1f}" x2="{w-pad_r}" y2="{y0:.1f}" stroke="#888" stroke-dasharray="4 3"/>
  {''.join(polylines)}
  {''.join(legend)}
  {''.join(xticks)}
</svg>
'''
    out_path.write_text(svg, encoding="utf-8")
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
