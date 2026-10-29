"""Equity curves for EXP-0075 / H66 keep-gate arms.

Writes results/MAT_CONTEXT_EQUITY/{equity_0075_*.svg, summary_0075.md, daily_0075.csv}.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated.study_combined_book import _plot_equity
from research.mat_context.cache import build_cache
from research.mat_context.run_queue import _resim_masked
from research.run_cfade_all_months import _sid_chrono_key

ARMS = [
    ("vwap_near_0.5", "EXP-0075 vwap_near_0.5"),
    ("atr_expanded", "EXP-0075 atr_expanded"),
    ("rot_fresh_and_vwap_near", "EXP-0075 rot_fresh ∩ vwap_near"),
]


def _day_nets(sims: dict) -> dict[str, float]:
    d: dict[str, float] = defaultdict(float)
    for sid, s in sims.items():
        for t in s.trades:
            d[sid] += t.pnl - F.COMMISSION
    return dict(d)


def _path(day: dict[str, float], sessions: list[str]) -> list[dict]:
    pe = 0.0
    rows = []
    for sid in sessions:
        pe += day.get(sid, 0.0)
        rows.append({"session": sid, "day_net": round(day.get(sid, 0.0), 2), "equity": round(pe, 2)})
    return rows


def _overlay(paths: dict[str, list[dict]], out: Path, title: str) -> None:
    """Multi-series SVG overlay (shared y-scale)."""
    if not paths:
        return
    colors = {
        "vwap_near_0.5": "#1f4e79",
        "atr_expanded": "#c45c26",
        "rot_fresh_and_vwap_near": "#2a7f62",
        "base": "#888888",
    }
    # align sessions from first path
    first = next(iter(paths.values()))
    labels = [r["session"] for r in first]
    n = len(labels)
    all_ys = [r["equity"] for path in paths.values() for r in path]
    ymin = min(0.0, min(all_ys))
    ymax = max(0.0, max(all_ys))
    if abs(ymax - ymin) < 1e-9:
        ymax = ymin + 1.0
    span = ymax - ymin
    ymin -= 0.05 * span
    ymax += 0.05 * span

    w, h = 960, 400
    pad_l, pad_r, pad_t, pad_b = 64, 24, 40, 72
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    def x_at(i: int) -> float:
        return pad_l + (i / max(n - 1, 1)) * plot_w

    def y_at(v: float) -> float:
        return pad_t + (1.0 - (v - ymin) / (ymax - ymin)) * plot_h

    import numpy as np
    yticks = np.linspace(ymin, ymax, 5)
    grid = []
    for v in yticks:
        yy = y_at(v)
        grid.append(
            f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{w-pad_r}" y2="{yy:.1f}" '
            f'stroke="#ddd" stroke-width="1"/>'
            f'<text x="{pad_l-8}" y="{yy+4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#444">{v:.0f}</text>'
        )
    y0 = y_at(0.0)
    polylines = []
    legend = []
    lx, ly = pad_l, pad_t - 8
    for i, (key, path) in enumerate(paths.items()):
        col = colors.get(key, "#333")
        pts = " ".join(f"{x_at(j):.1f},{y_at(float(r['equity'])):.1f}" for j, r in enumerate(path))
        polylines.append(
            f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2"/>'
        )
        legend.append(
            f'<rect x="{lx + i*210}" y="{ly-10}" width="12" height="12" fill="{col}"/>'
            f'<text x="{lx + i*210 + 18}" y="{ly}" font-size="11" fill="#333">{key}</text>'
        )
    step = max(1, n // 10)
    xtick_i = list(range(0, n, step))
    if xtick_i[-1] != n - 1:
        xtick_i.append(n - 1)
    xticks = []
    for i in xtick_i:
        xx = x_at(i)
        xticks.append(
            f'<text x="{xx:.1f}" y="{h-18}" text-anchor="middle" '
            f'font-size="10" fill="#444" transform="rotate(-40 {xx:.1f},{h-18})">'
            f'{labels[i]}</text>'
        )
    title_esc = title.replace("&", "&amp;").replace("<", "&lt;")
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <rect width="100%" height="100%" fill="#fafafa"/>
  <text x="{w/2}" y="22" text-anchor="middle" font-size="15" font-family="Segoe UI,sans-serif" fill="#222">{title_esc}</text>
  {''.join(legend)}
  {''.join(grid)}
  <line x1="{pad_l}" y1="{y0:.1f}" x2="{w-pad_r}" y2="{y0:.1f}" stroke="#888" stroke-width="1" stroke-dasharray="4 3"/>
  {''.join(polylines)}
  {''.join(xticks)}
  <text x="16" y="{pad_t+plot_h/2}" text-anchor="middle" font-size="11" fill="#444"
        transform="rotate(-90 16,{pad_t+plot_h/2})">Equity (NQ pts)</text>
</svg>
'''
    out.write_text(svg, encoding="utf-8")
    print(f"wrote {out}", flush=True)


def main() -> None:
    cfg = load_config()
    out = Path(cfg["results_dir"]) / "MAT_CONTEXT_EQUITY"
    out.mkdir(parents=True, exist_ok=True)

    print("Building MAT context cache…", flush=True)
    cache = build_cache(verbose=False)
    sessions = sorted(cache.sessions, key=_sid_chrono_key)
    print(f"Cache ready: {len(sessions)} sessions", flush=True)

    base_sims = {sid: cache.bundles[sid].sim for sid in cache.sessions}
    paths: dict[str, list[dict]] = {}
    stats_rows = []

    for arm, title in [("base", "EXP-0075 base MAT")] + ARMS:
        if arm == "base":
            sims = base_sims
        else:
            print(f"Re-sim {arm}…", flush=True)
            sims = _resim_masked(cache, arm)
        day = _day_nets(sims)
        path = _path(day, sessions)
        paths[arm] = path
        trades = [t for s in sims.values() for t in s.trades]
        n = len(trades)
        wins = sum(1 for t in trades if (t.pnl - F.COMMISSION) > 0)
        pe = path[-1]["equity"] if path else 0.0
        peak = dd = 0.0
        for r in path:
            peak = max(peak, r["equity"])
            dd = max(dd, peak - r["equity"])
        stats_rows.append({
            "arm": arm, "n": n, "wr": round(wins / n, 3) if n else None,
            "net": pe, "max_dd": round(dd, 1),
            "days_pos": sum(1 for r in path if r["day_net"] > 0),
            "days": len(path),
        })
        fname = "equity_0075_base.svg" if arm == "base" else f"equity_0075_{arm}.svg"
        _plot_equity(path, out / fname, title)
        print(f"  {arm}: n={n} net={pe:+.1f} maxDD={dd:.1f}", flush=True)

    _overlay(
        {k: paths[k] for k, _ in ARMS},
        out / "equity_0075_overlay.svg",
        "EXP-0075 / H66 — near-VWAP + ATR-expanded arms",
    )

    # daily csv
    csv_path = out / "daily_0075.csv"
    fieldnames = ["session"] + [a for a, _ in [("base", "")] + ARMS]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, sid in enumerate(sessions):
            row = {"session": sid}
            for arm in fieldnames[1:]:
                row[arm] = paths[arm][i]["equity"]
            w.writerow(row)

    md = [
        "# EXP-0075 / H66 equity",
        "",
        "Daily cumulative net (Bookmap-parity fills: TP/SL at working price, 0.25 pt/RT commission). Research only — no live defaults.",
        "",
        "| arm | n | WR | net | maxDD | days+ |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in stats_rows:
        md.append(
            f"| {r['arm']} | {r['n']} | {r['wr']} | {r['net']:+.1f} | "
            f"{r['max_dd']:.1f} | {r['days_pos']}/{r['days']} |"
        )
    md += [
        "",
        "![overlay](equity_0075_overlay.svg)",
        "",
        "![vwap_near](equity_0075_vwap_near_0.5.svg)",
        "",
        "![atr_expanded](equity_0075_atr_expanded.svg)",
        "",
        "![rot_fresh_and_vwap_near](equity_0075_rot_fresh_and_vwap_near.svg)",
        "",
    ]
    (out / "summary_0075.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {out / 'summary_0075.md'}", flush=True)


if __name__ == "__main__":
    main()
