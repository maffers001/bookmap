"""Gate NQ∩LVN fades when ES is near LVN2/LVN3 and/or DOP.

Post-filters an existing NQ trades CSV (no re-sim of fills). At each NQ
``entry_ts``, look up ES last print and as-of IRT ES LVN2/LVN3/DOP; keep the
trade if min |ES_px − level| ≤ ``--es-near``.

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_nq_es_level_gate \\
      --src results/NQ_LVN_OUTER_48_96_FLAT/trades_ALL.csv \\
      --out results/NQ_ES_LEVEL_GATE_48_96
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed
from research.correlated.study_combined_book import _plot_equity
from research.irt_lvn.events import PTS_PER_TICK, _active_levels, _asof_idx
from research.irt_lvn.load_irt import KIND_DOP, KIND_LVN, default_irt_paths, load_irt_csv, session_id_to_ymd
from research.irt_lvn.study_es_intraday_lvn_large import _session_irt
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")
MONTH_ORDER = ["APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT"]


def _parse_entry_ts(s: str) -> int:
    """ISO-8601 entry_ts → ns since epoch."""
    dt = datetime.fromisoformat(s)
    return int(dt.timestamp() * 1e9)


def _equity_paths(rows: list[dict[str, Any]]) -> tuple[list[dict], list[dict], dict[str, float]]:
    rows = sorted(rows, key=lambda r: r["entry_ts"])
    trade_path: list[dict] = []
    eq = peak = maxdd = 0.0
    wins = 0
    by_day: dict[str, float] = defaultdict(float)
    day_meta: dict[str, tuple[str, str]] = {}
    for r in rows:
        pnl = float(r["pnl_net"])
        eq += pnl
        peak = max(peak, eq)
        maxdd = max(maxdd, peak - eq)
        if pnl > 0:
            wins += 1
        trade_path.append({
            "session": r["session_id"],
            "equity": round(eq, 2),
            "day_net": round(pnl, 2),
            "date": r["date"],
            "month": r.get("month") or "",
        })
        d = r["date"]
        by_day[d] += pnl
        day_meta[d] = (r["session_id"], r.get("month") or "")

    daily_path: list[dict] = []
    eq = peak = maxdd_d = 0.0
    for d in sorted(by_day):
        eq += by_day[d]
        peak = max(peak, eq)
        maxdd_d = max(maxdd_d, peak - eq)
        sid, mo = day_meta[d]
        daily_path.append({
            "session": sid,
            "equity": round(eq, 2),
            "day_net": round(by_day[d], 2),
            "date": d,
            "month": mo,
        })
    stats = {
        "n": len(rows),
        "wins": wins,
        "wr": (wins / len(rows)) if rows else 0.0,
        "net": trade_path[-1]["equity"] if trade_path else 0.0,
        "maxdd_trade": maxdd,
        "maxdd_daily": maxdd_d,
        "days": len(daily_path),
        "days_pos": sum(1 for r in daily_path if r["day_net"] > 0),
    }
    return trade_path, daily_path, stats


def _plot_overlay(
    series: dict[str, list[dict]],
    out_path: Path,
    title: str,
) -> None:
    """Simple multi-polyline SVG (session-aligned by date union)."""
    if not series:
        return
    # union of dates across series, sorted
    all_dates: list[str] = sorted({r["date"] for path in series.values() for r in path})
    if not all_dates:
        return
    # build equity reindexed by date (carry forward)
    rebuilt: dict[str, list[float]] = {}
    for key, path in series.items():
        by_d = {r["date"]: float(r["equity"]) for r in path}
        eq = 0.0
        ys: list[float] = []
        for d in all_dates:
            if d in by_d:
                eq = by_d[d]
            ys.append(eq)
        rebuilt[key] = ys

    colors = ["#1f4e79", "#c45c26", "#2a9d8f", "#6a4c93", "#bc4749", "#588157"]
    w, h = 960, 380
    pad_l, pad_r, pad_t, pad_b = 64, 160, 40, 64
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    all_ys = [y for ys in rebuilt.values() for y in ys] or [0.0]
    ymin = min(0.0, min(all_ys))
    ymax = max(0.0, max(all_ys))
    if abs(ymax - ymin) < 1e-9:
        ymax = ymin + 1.0
    span = ymax - ymin
    ymin -= 0.05 * span
    ymax += 0.05 * span
    n = len(all_dates)

    def x_at(i: int) -> float:
        return pad_l + (i / max(n - 1, 1)) * plot_w

    def y_at(v: float) -> float:
        return pad_t + (1.0 - (v - ymin) / (ymax - ymin)) * plot_h

    y0 = y_at(0.0)
    grid = []
    for v in np.linspace(ymin, ymax, 5):
        yy = y_at(float(v))
        grid.append(
            f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{w-pad_r}" y2="{yy:.1f}" '
            f'stroke="#ddd" stroke-width="1"/>'
            f'<text x="{pad_l-8}" y="{yy+4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#444">{v:.0f}</text>'
        )
    step = max(1, n // 10)
    xtick_i = list(range(0, n, step))
    if xtick_i[-1] != n - 1:
        xtick_i.append(n - 1)
    xticks = []
    for i in xtick_i:
        xx = x_at(i)
        lab = all_dates[i][5:]  # MM-DD
        xticks.append(
            f'<text x="{xx:.1f}" y="{h-20}" text-anchor="middle" font-size="10" fill="#444" '
            f'transform="rotate(-40 {xx:.1f},{h-20})">{lab}</text>'
        )

    polylines = []
    legend = []
    for i, (key, ys) in enumerate(rebuilt.items()):
        col = colors[i % len(colors)]
        pts = " ".join(f"{x_at(j):.1f},{y_at(v):.1f}" for j, v in enumerate(ys))
        polylines.append(
            f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2"/>'
        )
        ly = pad_t + 16 + i * 18
        legend.append(
            f'<rect x="{w-pad_r+12}" y="{ly-10}" width="12" height="12" fill="{col}"/>'
            f'<text x="{w-pad_r+28}" y="{ly}" font-size="11" fill="#222">{key}</text>'
        )

    title_esc = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <rect width="100%" height="100%" fill="#fafafa"/>
  <text x="{w/2}" y="24" text-anchor="middle" font-size="15" font-family="Segoe UI,sans-serif" fill="#222">{title_esc}</text>
  {''.join(grid)}
  <line x1="{pad_l}" y1="{y0:.1f}" x2="{w-pad_r}" y2="{y0:.1f}" stroke="#888" stroke-width="1" stroke-dasharray="4 3"/>
  {''.join(polylines)}
  {''.join(xticks)}
  {''.join(legend)}
  <text x="16" y="{pad_t+plot_h/2}" text-anchor="middle" font-size="11" fill="#444"
        transform="rotate(-90 16,{pad_t+plot_h/2})">Equity (NQ pts)</text>
</svg>
'''
    out_path.write_text(svg, encoding="utf-8")
    print(f"wrote {out_path}", flush=True)


def _es_levels_at(bars, t_ns: int, *, mode: str) -> list[tuple[str, float]]:
    """mode: lvn | dop | lvn_dop"""
    bi = _asof_idx(bars.ts_ns, t_ns)
    out: list[tuple[str, float]] = []
    for kind, lid, px in _active_levels(bars, bi):
        if mode in ("lvn", "lvn_dop") and kind == KIND_LVN:
            out.append((lid, px))
        elif mode in ("dop", "lvn_dop") and kind == KIND_DOP:
            out.append((lid, px))
    return out


def _annotate_trades(
    trades: list[dict[str, Any]],
    *,
    pq_dir: Path,
    irt_es,
    mode: str,
) -> list[dict[str, Any]]:
    """Add es_px / es_level / es_dist columns; cache ES tape+bars per session."""
    by_sid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_sid[t["session_id"]].append(t)

    cache: dict[str, tuple[Any, np.ndarray, np.ndarray]] = {}
    out: list[dict[str, Any]] = []

    for sid, rows in by_sid.items():
        if sid not in cache:
            bars, _shift = _session_irt(irt_es, sid, pq_dir, role="ES")
            tr = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
            if tr.num_rows == 0 or len(bars.ts_ns) == 0:
                cache[sid] = (bars, np.array([], dtype=np.int64), np.array([], dtype=np.float64))
            else:
                ts = tr.column("ts_ns").to_numpy().astype(np.int64)
                px = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
                o = np.argsort(ts, kind="stable")
                cache[sid] = (bars, ts[o], px[o])
        bars, ts, px = cache[sid]
        for r in rows:
            t_ns = _parse_entry_ts(r["entry_ts"])
            row = dict(r)
            if len(ts) == 0:
                row["es_px"] = ""
                row["es_level_id"] = ""
                row["es_level_px"] = ""
                row["es_dist"] = ""
                out.append(row)
                continue
            j = int(np.searchsorted(ts, t_ns, side="right")) - 1
            if j < 0:
                j = 0
            es_px = float(px[j])
            levels = _es_levels_at(bars, t_ns, mode=mode)
            if not levels:
                row["es_px"] = f"{es_px:.2f}"
                row["es_level_id"] = ""
                row["es_level_px"] = ""
                row["es_dist"] = ""
                out.append(row)
                continue
            lid, lpx = min(levels, key=lambda x: abs(x[1] - es_px))
            dist = abs(lpx - es_px)
            row["es_px"] = f"{es_px:.2f}"
            row["es_level_id"] = lid
            row["es_level_px"] = f"{lpx:.2f}"
            row["es_dist"] = f"{dist:.2f}"
            out.append(row)
    out.sort(key=lambda r: r["entry_ts"])
    return out


def _filter_band(rows: list[dict[str, Any]], near: float) -> list[dict[str, Any]]:
    kept = []
    for r in rows:
        d = r.get("es_dist")
        if d in (None, ""):
            continue
        if float(d) <= near:
            kept.append(r)
    return kept


def _pack_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    _, _, st = _equity_paths(rows)
    by_m: dict[str, float] = defaultdict(float)
    for r in rows:
        by_m[r.get("month") or ""] += float(r["pnl_net"])
    st["by_month"] = {k: round(v, 1) for k, v in by_m.items() if k}
    return st


def run(
    *,
    src: Path,
    out: Path,
    bands: list[float],
    modes: list[str],
) -> None:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    irt_paths = default_irt_paths(ROOT)
    print(f"Loading ES IRT from {irt_paths['ES']}", flush=True)
    irt_es = load_irt_csv(irt_paths["ES"])

    base = list(csv.DictReader(src.open(encoding="utf-8")))
    if not base:
        raise SystemExit(f"No trades in {src}")
    print(f"Base trades: {len(base)} from {src}", flush=True)

    out.mkdir(parents=True, exist_ok=True)
    grid_rows: list[dict[str, Any]] = []
    daily_series: dict[str, list[dict]] = {}

    # ungated baseline
    tpath, dpath, st0 = _equity_paths(base)
    _plot_equity(
        dpath,
        out / "equity_ungated_daily.svg",
        f"NQ ungated | n={st0['n']} net={st0['net']:+.0f}",
    )
    daily_series["ungated"] = dpath
    grid_rows.append({
        "mode": "ungated", "es_near": "", **{
            k: st0[k] for k in ("n", "wr", "net", "maxdd_daily", "days_pos", "days")
        },
    })

    for mode in modes:
        if mode not in ("lvn", "dop", "lvn_dop"):
            raise SystemExit(f"unknown mode {mode!r}; use lvn|dop|lvn_dop")
        print(f"Annotating mode={mode} …", flush=True)
        annotated = _annotate_trades(base, pq_dir=pq_dir, irt_es=irt_es, mode=mode)

        # write full annotated once per mode
        ann_path = out / f"trades_annotated_{mode}.csv"
        fields = list(annotated[0].keys()) if annotated else []
        with ann_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in annotated:
                w.writerow(r)

        for near in bands:
            kept = _filter_band(annotated, near)
            st = _pack_stats(kept)
            tag = f"{mode}_near{near:g}"
            _, dpath, _ = _equity_paths(kept)
            _plot_equity(
                dpath,
                out / f"equity_{tag}_daily.svg",
                f"NQ | ES {mode}≤{near:g} | n={st['n']} net={st['net']:+.0f}",
            )
            daily_series[tag] = dpath
            # gated trades csv
            gpath = out / f"trades_{tag}.csv"
            with gpath.open("w", newline="", encoding="utf-8") as f:
                if kept:
                    w = csv.DictWriter(f, fieldnames=list(kept[0].keys()))
                    w.writeheader()
                    for r in kept:
                        w.writerow(r)
                else:
                    f.write("")
            grid_rows.append({
                "mode": mode,
                "es_near": near,
                "n": st["n"],
                "wr": round(st["wr"], 3),
                "net": round(st["net"], 1),
                "maxdd_daily": round(st["maxdd_daily"], 1),
                "days_pos": st["days_pos"],
                "days": st["days"],
                "by_month": st["by_month"],
            })
            print(
                f"  {tag}: n={st['n']} WR={st['wr']:.0%} net={st['net']:+.1f} "
                f"maxDD={st['maxdd_daily']:.1f} days+={st['days_pos']}/{st['days']}",
                flush=True,
            )

    # overlay: ungated + best net gated (among n>=5)
    gated = [g for g in grid_rows if g["mode"] != "ungated" and g["n"] >= 5]
    best = max(gated, key=lambda g: g["net"]) if gated else None
    overlay_keys = ["ungated"]
    if best:
        overlay_keys.append(f"{best['mode']}_near{best['es_near']:g}")
    # also add a mid band lvn_dop_near8 if present
    for k in ("lvn_dop_near8", "lvn_near8", "lvn_dop_near12"):
        if k in daily_series and k not in overlay_keys:
            overlay_keys.append(k)
            break
    _plot_overlay(
        {k: daily_series[k] for k in overlay_keys if k in daily_series},
        out / "equity_overlay.svg",
        "NQ∩LVN gated by ES near LVN/DOP",
    )

    # grid csv
    with (out / "gate_grid.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["mode", "es_near", "n", "wr", "net", "maxdd_daily", "days_pos", "days"],
        )
        w.writeheader()
        for g in grid_rows:
            w.writerow({
                "mode": g["mode"],
                "es_near": g["es_near"],
                "n": g["n"],
                "wr": g["wr"],
                "net": g["net"],
                "maxdd_daily": g["maxdd_daily"],
                "days_pos": g["days_pos"],
                "days": g["days"],
            })

    # summary md
    lines = [
        "# NQ∩LVN gated by ES near LVN / DOP",
        "",
        f"Source: `{src.as_posix()}`",
        "",
        "At each NQ fill, require ES last print within **es_near** pts of as-of "
        "ES **LVN2/LVN3** and/or **DOP** (IRT chart export).",
        "",
        "## Grid",
        "",
        "| mode | es_near | n | WR | net | maxDD | days+ |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for g in grid_rows:
        near = g["es_near"] if g["es_near"] != "" else "—"
        wr = f"{g['wr']:.0%}" if isinstance(g["wr"], float) else g["wr"]
        if g["mode"] == "ungated":
            wr = f"{st0['wr']:.0%}"
        lines.append(
            f"| {g['mode']} | {near} | {g['n']} | {wr} | {g['net']:+.1f} | "
            f"{g['maxdd_daily']:.1f} | {g['days_pos']}/{g['days']} |"
        )
    if best:
        lines += [
            "",
            f"Best gated (n≥5): **`{best['mode']}` ≤ {best['es_near']:g}** "
            f"net **{best['net']:+.1f}** (n={best['n']}) vs ungated **{st0['net']:+.1f}**.",
        ]
    lines += [
        "",
        "## Charts",
        "",
        "![overlay](equity_overlay.svg)",
        "",
        "![ungated](equity_ungated_daily.svg)",
        "",
    ]
    if best:
        tag = f"{best['mode']}_near{best['es_near']:g}"
        lines.append(f"![best gated](equity_{tag}_daily.svg)")
        lines.append("")
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out / 'summary.md'}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--src",
        type=Path,
        default=ROOT / "results" / "NQ_LVN_OUTER_48_96_FLAT" / "trades_ALL.csv",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "results" / "NQ_ES_LEVEL_GATE_48_96",
    )
    ap.add_argument(
        "--bands",
        default="4,8,12,20",
        help="Comma-separated ES vicinity bands in points",
    )
    ap.add_argument(
        "--modes",
        default="lvn,lvn_dop",
        help="Comma-separated: lvn | lvn_dop | dop",
    )
    args = ap.parse_args()
    bands = [float(x) for x in args.bands.split(",") if x.strip()]
    modes = [x.strip() for x in args.modes.split(",") if x.strip()]
    run(src=args.src, out=args.out, bands=bands, modes=modes)


if __name__ == "__main__":
    main()
