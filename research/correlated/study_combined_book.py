"""Combined one-book test: MAT(allocator c) + CFADE + EXP-0061 continuation.

Allocator (c) from EXP-0057:
  - CFADE: frozen (ER60<=0.30 gate already in CFADE)
  - MAT: only when ER60>0.30 AND range60<=175
  - 0061: confirm-burst continuation with ES-against exit (always eligible)

Modes
  independent — sum daily nets (allows overlapping positions)
  flat_only   — single NQ book, chronological, 5s post-exit cooldown;
                hand-off: CFADE opposite-side entry within 500ms of a 0061
                exhaust exit is allowed with no cooldown wait

Writes results/COMBINED_MAT_CFADE_0061/{summary.md,report.json,daily.csv,equity.png}.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from research.backtest.mat_sim import TradeResult, detect_signals
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.corr_exhaust import ExhaustParams, build_exhaust_exit_fn
from research.run_cfade_all_months import _sid_chrono_key

COMM = F.COMMISSION
COOLDOWN_NS = int(5.0 * 1e9)
HANDOFF_NS = int(0.5 * 1e9)  # 500 ms


@dataclass
class TaggedTrade:
    trade: TradeResult
    kind: str  # mat | cfade | cont0061
    net: float

    @property
    def entry_ts_ns(self) -> int:
        return self.trade.entry_ts_ns

    @property
    def exit_ts_ns(self) -> int:
        return int(self.trade.exit_ts_ns or 0)

    @property
    def side(self) -> int:
        return self.trade.side

    @property
    def reason(self) -> str:
        return self.trade.reason


def _net(t: TradeResult) -> float:
    return t.pnl - COMM


def _day_nets(tagged: list[TaggedTrade]) -> dict[str, float]:
    d: dict[str, float] = defaultdict(float)
    for x in tagged:
        d[x.trade.session_id] += x.net
    return dict(d)


def _corr(a: dict[str, float], b: dict[str, float]) -> float | None:
    keys = sorted(set(a) & set(b))
    if len(keys) < 5:
        return None
    xa = np.array([a[k] for k in keys], dtype=float)
    xb = np.array([b[k] for k in keys], dtype=float)
    if xa.std() < 1e-9 or xb.std() < 1e-9:
        return None
    return float(np.corrcoef(xa, xb)[0, 1])


def _path_stats(day_nets: dict[str, float]) -> dict:
    pe = ph = pdd = 0.0
    path = []
    for sid in sorted(day_nets, key=_sid_chrono_key):
        pe += day_nets[sid]
        ph = max(ph, pe)
        pdd = max(pdd, ph - pe)
        path.append({"session": sid, "day_net": round(day_nets[sid], 2), "equity": round(pe, 2)})
    return {
        "net": round(pe, 2),
        "high": round(ph, 2),
        "max_dd": round(pdd, 2),
        "days": len(day_nets),
        "days_pos": sum(1 for v in day_nets.values() if v > 0),
        "path": path,
    }


def _trade_stats(tagged: list[TaggedTrade]) -> dict:
    if not tagged:
        return {"n": 0, "wins": 0, "wr": None, "net": 0.0, "avg": None, "by_kind": {}}
    wins = sum(1 for x in tagged if x.net > 0)
    by: dict[str, list[TaggedTrade]] = defaultdict(list)
    for x in tagged:
        by[x.kind].append(x)
    kind_stats = {}
    for k, xs in by.items():
        w = sum(1 for x in xs if x.net > 0)
        n = len(xs)
        net = sum(x.net for x in xs)
        kind_stats[k] = {
            "n": n,
            "wins": w,
            "wr": round(w / n, 3) if n else None,
            "net": round(net, 2),
            "avg": round(net / n, 2) if n else None,
        }
    net = sum(x.net for x in tagged)
    n = len(tagged)
    return {
        "n": n,
        "wins": wins,
        "wr": round(wins / n, 3),
        "net": round(net, 2),
        "avg": round(net / n, 2),
        "by_kind": kind_stats,
    }


def _flat_only_merge(candidates: list[TaggedTrade]) -> tuple[list[TaggedTrade], dict]:
    """Greedy chronological flat-only with 5s post-exit cooldown + 0061→CFADE hand-off.

    Hand-off: CFADE fade stamps at the exhaust *bar close*, while 0061's
    recorded exit_ts is the first trade print *at or after* that close — so the
    CFADE entry_ts usually falls *inside* the 0061 hold. After an exhaust exit
    we still take that matching opposite-side CFADE immediately.
    """
    ordered = sorted(candidates, key=lambda x: (x.entry_ts_ns, 0 if x.kind == "cont0061" else 1 if x.kind == "mat" else 2))
    taken: list[TaggedTrade] = []
    used: set[int] = set()  # id(cand)
    last_exit = -10**18
    skipped = {"overlap": 0, "cooldown": 0, "handoff_allowed": 0}

    def _accept(cand: TaggedTrade) -> None:
        nonlocal last_exit
        taken.append(cand)
        used.add(id(cand))
        last_exit = max(int(cand.exit_ts_ns), int(cand.entry_ts_ns))

    i = 0
    while i < len(ordered):
        cand = ordered[i]
        i += 1
        if id(cand) in used:
            continue

        if cand.entry_ts_ns < last_exit:
            skipped["overlap"] += 1
            continue
        if cand.entry_ts_ns < last_exit + COOLDOWN_NS:
            skipped["cooldown"] += 1
            continue

        _accept(cand)

        # Hand-off: if continuation exited on exhaust, pull matching CFADE
        if cand.kind == "cont0061" and cand.reason.startswith("exhaust"):
            for other in ordered:
                if id(other) in used or other.kind != "cfade":
                    continue
                if other.side != -cand.side:
                    continue
                # CFADE stamp during/just after the 0061 hold (exhaust bar)
                if other.entry_ts_ns <= cand.entry_ts_ns:
                    continue
                if other.entry_ts_ns > cand.exit_ts_ns + HANDOFF_NS:
                    continue
                _accept(other)
                skipped["handoff_allowed"] += 1
                break

    return taken, skipped


def _session_books(pair: H.Pair) -> tuple[list[TaggedTrade], list[TaggedTrade], list[TaggedTrade]]:
    """Return (mat_c, cfade, cont0061) tagged trade lists for one session."""
    # MAT full then filter to allocator-c
    sm = F.simulate_mat(pair)
    mat_c = []
    for t in sm.trades:
        if t.er60 is None or not np.isfinite(t.er60) or t.er60 <= 0.30:
            continue
        if t.range60 is None or not np.isfinite(t.range60) or t.range60 > 175.0:
            continue
        mat_c.append(TaggedTrade(t, "mat", _net(t)))

    sc = F.simulate_cfade(pair)
    cfade = [TaggedTrade(t, "cfade", _net(t)) for t in sc.trades]

    # 0061 continuation
    nq_b, es_b = pair.bars(100)
    confirm = H.confirm_bursts(pair)
    p_ex = ExhaustParams(
        es_imb_max=-0.4, es_min_vol=0, use_imb=True, use_vol=False,
        nq_min_imbalance=0.55, nq_min_vol=25, nq_min_trades=6,
        max_wait_sec=5.0,
    )
    exit_fn = build_exhaust_exit_fn(nq_b, es_b, p_ex)
    s61 = F.simulate_cfade(
        pair,
        events=confirm if confirm.num_rows else None,
        exit_fn=exit_fn,
    ) if confirm.num_rows else None
    # simulate_cfade with confirm events enters WITH burst side (confirm side), good.
    # But simulate_cfade defaults to CFADE events if events=None — we passed confirm.
    # Gate is frozen CFADE ER<=0.3 — for 0061 we should use same ER gate as CFADE family
    # OR no ER gate? EXP-0061 used frozen_gate (ER<=0.3). Keep that for parity.
    cont = [TaggedTrade(t, "cont0061", _net(t)) for t in (s61.trades if s61 else [])]

    return mat_c, cfade, cont


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _plot_equity(path_rows: list[dict], out_path: Path, title: str) -> None:
    """Write an SVG equity curve (no matplotlib dependency)."""
    if not path_rows:
        return
    title = _xml_escape(title)
    ys = [float(r["equity"]) for r in path_rows]
    labels = [r["session"] for r in path_rows]
    n = len(ys)
    w, h = 960, 360
    pad_l, pad_r, pad_t, pad_b = 64, 24, 40, 56
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    ymin = min(0.0, min(ys))
    ymax = max(0.0, max(ys))
    if abs(ymax - ymin) < 1e-9:
        ymax = ymin + 1.0
    # pad 5%
    span = ymax - ymin
    ymin -= 0.05 * span
    ymax += 0.05 * span

    def x_at(i: int) -> float:
        return pad_l + (i / max(n - 1, 1)) * plot_w

    def y_at(v: float) -> float:
        return pad_t + (1.0 - (v - ymin) / (ymax - ymin)) * plot_h

    pts = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(ys))
    y0 = y_at(0.0)
    # fill under curve to zero
    fill = f"{x_at(0):.1f},{y0:.1f} " + pts + f" {x_at(n-1):.1f},{y0:.1f}"

    # y ticks
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
    # x ticks
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

    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <rect width="100%" height="100%" fill="#fafafa"/>
  <text x="{w/2}" y="24" text-anchor="middle" font-size="15" font-family="Segoe UI,sans-serif" fill="#222">{title}</text>
  {''.join(grid)}
  <line x1="{pad_l}" y1="{y0:.1f}" x2="{w-pad_r}" y2="{y0:.1f}" stroke="#888" stroke-width="1" stroke-dasharray="4 3"/>
  <polygon points="{fill}" fill="#1f4e79" fill-opacity="0.12"/>
  <polyline points="{pts}" fill="none" stroke="#1f4e79" stroke-width="2"/>
  {''.join(xticks)}
  <text x="16" y="{pad_t+plot_h/2}" text-anchor="middle" font-size="11" fill="#444"
        transform="rotate(-90 16,{pad_t+plot_h/2})">Equity (NQ pts)</text>
</svg>
'''
    out_svg = out_path.with_suffix(".svg")
    out_svg.write_text(svg, encoding="utf-8")
    # also copy path expected as .png name → write .svg alongside; keep requested stem
    if out_path.suffix.lower() == ".png":
        # mirror for markdown that references .png — write svg with png-less note
        out_path.with_suffix(".svg").write_text(svg, encoding="utf-8")
    print(f"wrote {out_svg}", flush=True)


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "COMBINED_MAT_CFADE_0061"
    out.mkdir(parents=True, exist_ok=True)
    sessions = F.clean_sessions(pq)

    all_mat: list[TaggedTrade] = []
    all_cfade: list[TaggedTrade] = []
    all_cont: list[TaggedTrade] = []
    all_flat: list[TaggedTrade] = []
    skip_tot = {"overlap": 0, "cooldown": 0, "handoff_allowed": 0}

    for sid in sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            print(f"{sid} no pair", flush=True)
            continue
        mat_c, cfade, cont = _session_books(pair)
        all_mat.extend(mat_c)
        all_cfade.extend(cfade)
        all_cont.extend(cont)

        # flat-only within session (positions don't span sessions)
        merged, skipped = _flat_only_merge(mat_c + cfade + cont)
        all_flat.extend(merged)
        for k in skip_tot:
            skip_tot[k] += skipped[k]

        print(
            f"{sid} mat_c={len(mat_c)} cfade={len(cfade)} cont={len(cont)} "
            f"flat={len(merged)} handoff={skipped['handoff_allowed']}",
            flush=True,
        )

    # Independent books
    d_mat = _day_nets(all_mat)
    d_cfade = _day_nets(all_cfade)
    d_cont = _day_nets(all_cont)
    all_sids = sorted(set(d_mat) | set(d_cfade) | set(d_cont) | {t.trade.session_id for t in all_flat},
                      key=_sid_chrono_key)
    d_indep = {s: d_mat.get(s, 0.0) + d_cfade.get(s, 0.0) + d_cont.get(s, 0.0) for s in all_sids}
    d_flat = _day_nets(all_flat)
    # ensure all sessions present in flat path
    for s in all_sids:
        d_flat.setdefault(s, 0.0)

    books = {
        "mat_c_alone": (_path_stats(d_mat), _trade_stats(all_mat)),
        "cfade_alone": (_path_stats(d_cfade), _trade_stats(all_cfade)),
        "cont0061_alone": (_path_stats(d_cont), _trade_stats(all_cont)),
        "independent_sum": (_path_stats(d_indep), {
            "n": len(all_mat) + len(all_cfade) + len(all_cont),
            "note": "overlapping positions allowed; trade WR not meaningful as one book",
        }),
        "flat_only_combined": (_path_stats(d_flat), _trade_stats(all_flat)),
    }

    corrs = {
        "mat_c_vs_cfade": _corr(d_mat, d_cfade),
        "mat_c_vs_cont0061": _corr(d_mat, d_cont),
        "cfade_vs_cont0061": _corr(d_cfade, d_cont),
    }

    # Daily table
    daily_rows = []
    pe = 0.0
    ph = 0.0
    for sid in all_sids:
        m = d_mat.get(sid, 0.0)
        c = d_cfade.get(sid, 0.0)
        k = d_cont.get(sid, 0.0)
        indep = m + c + k
        flat = d_flat.get(sid, 0.0)
        pe += flat
        ph = max(ph, pe)
        # per-day trade counts in flat book
        day_tr = [t for t in all_flat if t.trade.session_id == sid]
        wins = sum(1 for t in day_tr if t.net > 0)
        daily_rows.append({
            "session": sid,
            "month": sid[2:4],
            "mat_c_net": round(m, 2),
            "cfade_net": round(c, 2),
            "cont0061_net": round(k, 2),
            "independent_net": round(indep, 2),
            "flat_net": round(flat, 2),
            "flat_n": len(day_tr),
            "flat_wins": wins,
            "flat_wr": round(wins / len(day_tr), 3) if day_tr else None,
            "flat_equity": round(pe, 2),
            "flat_dd_from_peak": round(ph - pe, 2),
        })

    # CSV
    import csv
    csv_path = out / "daily.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(daily_rows[0].keys()) if daily_rows else ["session"])
        w.writeheader()
        w.writerows(daily_rows)

    flat_path = books["flat_only_combined"][0]
    _plot_equity(
        flat_path["path"],
        out / "equity.svg",
        "Combined flat-only book — MAT(c) + CFADE + 0061 (NQ pts)",
    )
    _plot_equity(
        books["independent_sum"][0]["path"],
        out / "equity_independent.svg",
        "Independent sum — MAT(c) + CFADE + 0061 (overlapping OK)",
    )

    ts = books["flat_only_combined"][1]
    md = [
        "# Combined book — MAT(allocator c) + CFADE + EXP-0061",
        "",
        "Sample: **113** clean sessions. Costs: 1-tick slip, 0.25 pt/RT.",
        "",
        "**Legs**",
        "- **MAT(c):** causal MAT trend+VWAP vol k=1, only ER60>0.30 and range60≤175",
        "- **CFADE:** frozen against_m04 + ER60≤0.30, vol RR SL1/TP2 clip 4–40",
        "- **0061:** confirm-burst continuation, exit on ES-against or bracket (ER≤0.30 gate)",
        "",
        "**flat_only:** one NQ position; 5 s cooldown after exit; "
        "0061 exhaust → opposite CFADE within 500 ms allowed as hand-off (no cooldown wait).",
        "",
        "## Books",
        "",
        "| book | n | WR | net | avg | days+ | maxDD |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, (ps, ts_) in books.items():
        if name == "independent_sum":
            md.append(
                f"| {name} | {ts_['n']} | – | {ps['net']:+.1f} | – | "
                f"{ps['days_pos']}/{ps['days']} | {ps['max_dd']:.1f} |"
            )
        else:
            md.append(
                f"| {name} | {ts_['n']} | {ts_.get('wr')} | {ps['net']:+.1f} | "
                f"{ts_.get('avg')} | {ps['days_pos']}/{ps['days']} | {ps['max_dd']:.1f} |"
            )

    md += [
        "",
        "## Flat-only by kind",
        "",
        "| kind | n | WR | net | avg |",
        "|---|---:|---:|---:|---:|",
    ]
    for k, v in (ts.get("by_kind") or {}).items():
        md.append(f"| {k} | {v['n']} | {v['wr']} | {v['net']:+.1f} | {v['avg']} |")

    md += [
        "",
        "## Daily P&L correlations (independent legs)",
        "",
        f"- MAT(c) vs CFADE: **{corrs['mat_c_vs_cfade']}**",
        f"- MAT(c) vs 0061: **{corrs['mat_c_vs_cont0061']}**",
        f"- CFADE vs 0061: **{corrs['cfade_vs_cont0061']}**",
        "",
        "## Flat-only skip / hand-off",
        "",
        f"- Skipped overlap: {skip_tot['overlap']}",
        f"- Skipped cooldown: {skip_tot['cooldown']}",
        f"- Hand-offs taken (0061 exhaust → CFADE): **{skip_tot['handoff_allowed']}**",
        "",
        f"## Equity curves",
        "",
        f"![flat-only equity](equity.svg)",
        "",
        f"![independent equity](equity_independent.svg)",
        "",
        "## Daily (flat-only primary)",
        "",
        "| session | mat_c | cfade | 0061 | indep | flat | n | WR | equity | dd |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in daily_rows:
        wr = f"{r['flat_wr']:.2f}" if r["flat_wr"] is not None else "–"
        md.append(
            f"| {r['session']} | {r['mat_c_net']:+.1f} | {r['cfade_net']:+.1f} | "
            f"{r['cont0061_net']:+.1f} | {r['independent_net']:+.1f} | {r['flat_net']:+.1f} | "
            f"{r['flat_n']} | {wr} | {r['flat_equity']:+.1f} | {r['flat_dd_from_peak']:.1f} |"
        )
    md.append("")

    report = {
        "books": {k: {"path": v[0], "trades": v[1]} for k, v in books.items()},
        "correlations": corrs,
        "skip": skip_tot,
        "daily": daily_rows,
        "sessions": sessions,
    }
    # path has nested path list — strip heavy duplicate for json of books path
    slim = json.loads(json.dumps(report, default=str))
    (out / "report.json").write_text(json.dumps(slim, indent=2), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
    print(
        f"flat_only n={ts['n']} WR={ts['wr']} net={books['flat_only_combined'][0]['net']:+.1f} "
        f"maxDD={books['flat_only_combined'][0]['max_dd']:.1f} "
        f"indep_net={books['independent_sum'][0]['net']:+.1f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
