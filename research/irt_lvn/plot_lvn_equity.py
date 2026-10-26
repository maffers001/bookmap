"""Equity curve for core ES open-cluster ∩ LVN outer (SL8/TP32).

Reads results/ES_LVN_OUTER_824_20261025/trades_ALL.csv and writes
results/ES_LVN_OUTER_824_EQUITY/{summary.md, daily.csv, equity_*.svg}.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from research.config import ROOT
from research.correlated.study_combined_book import _plot_equity

DEFAULT_SRC = ROOT / "results" / "ES_LVN_OUTER_824_20261025" / "trades_ALL.csv"
DEFAULT_OUT = ROOT / "results" / "ES_LVN_OUTER_824_EQUITY"
MONTH_ORDER = ["APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    rows = list(csv.DictReader(args.src.open(encoding="utf-8")))
    rows.sort(key=lambda r: r["entry_ts"])

    trade_path: list[dict] = []
    eq = peak = maxdd = 0.0
    wins = 0
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
            "month": r["month"],
        })

    by_day: dict[str, float] = defaultdict(float)
    day_meta: dict[str, tuple[str, str]] = {}
    for r in rows:
        d = r["date"]
        by_day[d] += float(r["pnl_net"])
        day_meta[d] = (r["session_id"], r["month"])

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

    args.out.mkdir(parents=True, exist_ok=True)
    _plot_equity(
        trade_path,
        args.out / "equity_trades.svg",
        "ES open-cluster x LVN outer | SL8/TP32 | minOpen3 | trade equity",
    )
    _plot_equity(
        daily_path,
        args.out / "equity_daily.svg",
        "ES open-cluster x LVN outer | SL8/TP32 | minOpen3 | daily equity",
    )

    with (args.out / "daily.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "session", "month", "day_net", "equity"])
        w.writeheader()
        for r in daily_path:
            w.writerow({
                "date": r["date"],
                "session": r["session"],
                "month": r["month"],
                "day_net": r["day_net"],
                "equity": r["equity"],
            })

    by_m: dict[str, float] = defaultdict(float)
    for r in rows:
        by_m[r["month"]] += float(r["pnl_net"])

    n = len(rows)
    net = trade_path[-1]["equity"] if trade_path else 0.0
    days_pos = sum(1 for r in daily_path if r["day_net"] > 0)
    wr = f"{wins / n:.0%}" if n else "—"
    avg = f"{net / n:+.2f}" if n else "—"

    lines = [
        "# ES open-cluster ∩ LVN — core equity",
        "",
        f"Source: `{args.src.as_posix()}`",
        "",
        "Rules: outer wall · SL**8** / TP**32** · min |level−open| ≥ **3** · "
        "entry ≤17:00 UK · hold →21:00 · commission in `pnl_net`.",
        "",
        "## Summary",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| n trades | {n} |",
        f"| WR | {wr} |",
        f"| net (pts) | {net:+.1f} |",
        f"| maxDD (trade path) | {maxdd:.1f} |",
        f"| maxDD (daily path) | {maxdd_d:.1f} |",
        f"| days+ | {days_pos}/{len(daily_path)} |",
        f"| avg / trade | {avg} |",
        "",
        "## By month",
        "",
        "| month | net |",
        "|---|---:|",
    ]
    for mo in MONTH_ORDER:
        if mo in by_m:
            lines.append(f"| {mo} | {by_m[mo]:+.1f} |")
    lines += [
        "",
        "## Charts",
        "",
        "![daily equity](equity_daily.svg)",
        "",
        "![trade equity](equity_trades.svg)",
        "",
    ]
    (args.out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        f"Wrote {args.out}  n={n} WR={wr} net={net:+.1f} "
        f"maxDD_trade={maxdd:.1f} maxDD_daily={maxdd_d:.1f} "
        f"days+={days_pos}/{len(daily_path)}"
    )


if __name__ == "__main__":
    main()
