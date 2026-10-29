"""Retest frozen CFADE + MAT075 (rot_fresh ∩ vwap_near) on the clean sample.

No ES LVN. Writes monthly stats + equity curves to
``results/RETEST_CFADE_MAT075/``.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.correlated.study_combined_book import _plot_equity
from research.mat_context.cache import build_cache
from research.mat_context.plot_0075_equity import _day_nets, _overlay, _path
from research.mat_context.run_queue import _resim_masked
from research.run_cfade_all_months import _sid_chrono_key

MAT_ARM = "rot_fresh_and_vwap_near"
COMM = F.COMMISSION
MONTH_NAMES = {
    "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
    "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct",
}


def _month(sid: str) -> str:
    return sid[2:4]


def _stats_from_trades(trades, sessions: list[str]) -> dict:
    day = defaultdict(float)
    for t in trades:
        day[t.session_id] += t.pnl - COMM
    path = _path(dict(day), sessions)
    n = len(trades)
    wins = sum(1 for t in trades if (t.pnl - COMM) > 0)
    pe = path[-1]["equity"] if path else 0.0
    peak = dd = 0.0
    for r in path:
        peak = max(peak, r["equity"])
        dd = max(dd, peak - r["equity"])
    by_m: dict[str, dict] = {}
    for sid in sessions:
        m = _month(sid)
        slot = by_m.setdefault(m, {"net": 0.0, "n_days": 0, "days_pos": 0, "n_tr": 0, "wins": 0})
        dn = day.get(sid, 0.0)
        slot["net"] += dn
        slot["n_days"] += 1
        if dn > 0:
            slot["days_pos"] += 1
    for t in trades:
        m = _month(t.session_id)
        if m not in by_m:
            continue
        by_m[m]["n_tr"] += 1
        if (t.pnl - COMM) > 0:
            by_m[m]["wins"] += 1
    return {
        "n": n,
        "wr": round(wins / n, 3) if n else None,
        "net": round(pe, 2),
        "max_dd": round(dd, 2),
        "days_pos": sum(1 for sid in sessions if day.get(sid, 0.0) > 0),
        "days": len(sessions),
        "path": path,
        "by_month": {
            m: {
                "net": round(v["net"], 2),
                "n_days": v["n_days"],
                "days_pos": v["days_pos"],
                "n": v["n_tr"],
                "wr": round(v["wins"] / v["n_tr"], 3) if v["n_tr"] else None,
            }
            for m, v in sorted(by_m.items())
        },
    }


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "RETEST_CFADE_MAT075"
    out.mkdir(parents=True, exist_ok=True)

    print("Building MAT cache…", flush=True)
    cache = build_cache(verbose=False)
    sessions = sorted(cache.sessions, key=_sid_chrono_key)
    print(f"Cache sessions={len(sessions)} (excl {sorted(F.EXCLUDE)})", flush=True)

    # March slice for headline
    march = [s for s in sessions if _month(s) == "03"]
    print(f"March sessions in sample: {len(march)} → {march}", flush=True)

    print(f"Re-sim MAT {MAT_ARM}…", flush=True)
    mat_sims = _resim_masked(cache, MAT_ARM)
    mat_trades = [t for s in mat_sims.values() for t in s.trades]

    print("Simulating CFADE frozen…", flush=True)
    cfade_trades = []
    for sid in sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            print(f"  {sid}: no pair", flush=True)
            continue
        s = F.simulate_cfade(pair)
        cfade_trades.extend(s.trades)
        print(f"  {sid}: cfade={s.n} net={s.net:+.1f}", flush=True)

    books = {
        "cfade": (cfade_trades, "CFADE frozen (against_m04 + ER≤0.30, SL1/TP2)"),
        "mat075": (mat_trades, "MAT075 rot_fresh ∩ vwap_near (vol k=1)"),
    }

    # Independent sum day nets
    day_c = defaultdict(float)
    day_m = defaultdict(float)
    for t in cfade_trades:
        day_c[t.session_id] += t.pnl - COMM
    for t in mat_trades:
        day_m[t.session_id] += t.pnl - COMM
    day_sum = {s: day_c.get(s, 0.0) + day_m.get(s, 0.0) for s in sessions}
    sum_trades_n = len(cfade_trades) + len(mat_trades)
    sum_wins = sum(1 for t in cfade_trades if (t.pnl - COMM) > 0) + sum(
        1 for t in mat_trades if (t.pnl - COMM) > 0
    )

    stats = {}
    paths = {}
    for key, (trades, title) in books.items():
        st = _stats_from_trades(trades, sessions)
        stats[key] = st
        paths[key] = st["path"]
        _plot_equity(st["path"], out / f"equity_{key}.svg", title)
        print(f"{key}: n={st['n']} net={st['net']:+.1f} maxDD={st['max_dd']:.1f}", flush=True)

    path_sum = _path(day_sum, sessions)
    peak = dd = 0.0
    for r in path_sum:
        peak = max(peak, r["equity"])
        dd = max(dd, peak - r["equity"])
    by_m_sum: dict[str, dict] = {}
    for sid in sessions:
        m = _month(sid)
        slot = by_m_sum.setdefault(m, {"net": 0.0, "n_days": 0, "days_pos": 0})
        dn = day_sum.get(sid, 0.0)
        slot["net"] += dn
        slot["n_days"] += 1
        if dn > 0:
            slot["days_pos"] += 1
    stats["independent_sum"] = {
        "n": sum_trades_n,
        "wr": round(sum_wins / sum_trades_n, 3) if sum_trades_n else None,
        "net": round(path_sum[-1]["equity"], 2) if path_sum else 0.0,
        "max_dd": round(dd, 2),
        "days_pos": sum(1 for s in sessions if day_sum.get(s, 0.0) > 0),
        "days": len(sessions),
        "path": path_sum,
        "by_month": {
            m: {
                "net": round(v["net"], 2),
                "n_days": v["n_days"],
                "days_pos": v["days_pos"],
                "n": None,
                "wr": None,
            }
            for m, v in sorted(by_m_sum.items())
        },
    }
    paths["independent_sum"] = path_sum
    _plot_equity(
        path_sum,
        out / "equity_independent_sum.svg",
        "CFADE + MAT075 independent sum (pts)",
    )
    _overlay(
        {
            "cfade": paths["cfade"],
            "mat075": paths["mat075"],
            "independent_sum": path_sum,
        },
        out / "equity_overlay.svg",
        "CFADE vs MAT075 vs independent sum",
    )

    # March-only equity
    if march:
        for key, (trades, title) in books.items():
            mtrades = [t for t in trades if t.session_id in set(march)]
            st = _stats_from_trades(mtrades, march)
            _plot_equity(
                st["path"],
                out / f"equity_{key}_march.svg",
                f"{title} — March 2026 only",
            )
            stats[f"{key}_march"] = st
            print(
                f"{key} March: n={st['n']} net={st['net']:+.1f} maxDD={st['max_dd']:.1f}",
                flush=True,
            )

    # daily csv
    with (out / "daily.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "session", "month",
                "cfade_net", "cfade_eq",
                "mat075_net", "mat075_eq",
                "sum_net", "sum_eq",
            ],
        )
        w.writeheader()
        for i, sid in enumerate(sessions):
            w.writerow({
                "session": sid,
                "month": _month(sid),
                "cfade_net": paths["cfade"][i]["day_net"],
                "cfade_eq": paths["cfade"][i]["equity"],
                "mat075_net": paths["mat075"][i]["day_net"],
                "mat075_eq": paths["mat075"][i]["equity"],
                "sum_net": path_sum[i]["day_net"],
                "sum_eq": path_sum[i]["equity"],
            })

    months = sorted({_month(s) for s in sessions})
    md = [
        "# Retest — frozen CFADE + MAT075",
        "",
        f"Clean sessions: **{len(sessions)}** (excl. `{sorted(F.EXCLUDE)}`). "
        f"March 2026 newly added: **{len(march)}** days.",
        "",
        "Algos:",
        "- **CFADE:** against_m04 + ER≤0.30, vol RR SL1/TP2 (cap 40)",
        "- **MAT075:** frozen MAT trend+VWAP vol k=1 + keep-gate "
        "`rot_fresh_and_vwap_near` (EXP-0075)",
        "",
        "Bookmap-parity fills (TP/SL at working price) + 0.25 pt/RT. "
        "No ES LVN (no LVN history that far back).",
        "",
        "## All-sample summary",
        "",
        "| book | n | WR | net pts | maxDD | days+ |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key in ("cfade", "mat075", "independent_sum"):
        s = stats[key]
        md.append(
            f"| {key} | {s['n']} | {s['wr']} | {s['net']:+.1f} | "
            f"{s['max_dd']:.1f} | {s['days_pos']}/{s['days']} |"
        )

    if "cfade_march" in stats:
        md += [
            "",
            "## March 2026 only",
            "",
            "| book | n | WR | net pts | maxDD | days+ |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for key in ("cfade_march", "mat075_march"):
            s = stats[key]
            md.append(
                f"| {key} | {s['n']} | {s['wr']} | {s['net']:+.1f} | "
                f"{s['max_dd']:.1f} | {s['days_pos']}/{s['days']} |"
            )

    md += [
        "",
        "## Monthly net (pts)",
        "",
        "| month | days | CFADE net | CFADE n/WR | MAT075 net | MAT075 n/WR | sum net |",
        "|---|---:|---:|---|---:|---|---:|",
    ]
    for m in months:
        label = MONTH_NAMES.get(m, m)
        c = stats["cfade"]["by_month"].get(m, {})
        mt = stats["mat075"]["by_month"].get(m, {})
        sm = stats["independent_sum"]["by_month"].get(m, {})
        c_nw = f"{c.get('n')}/{c.get('wr')}" if c.get("n") is not None else "—"
        m_nw = f"{mt.get('n')}/{mt.get('wr')}" if mt.get("n") is not None else "—"
        md.append(
            f"| {label} | {c.get('n_days', 0)} | {c.get('net', 0):+.1f} | {c_nw} | "
            f"{mt.get('net', 0):+.1f} | {m_nw} | {sm.get('net', 0):+.1f} |"
        )

    md += [
        "",
        "## Charts",
        "",
        "![overlay](equity_overlay.svg)",
        "",
        "![cfade](equity_cfade.svg)",
        "",
        "![mat075](equity_mat075.svg)",
        "",
        "![sum](equity_independent_sum.svg)",
        "",
    ]
    if march:
        md += [
            "### March only",
            "",
            "![cfade march](equity_cfade_march.svg)",
            "",
            "![mat075 march](equity_mat075_march.svg)",
            "",
        ]
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
