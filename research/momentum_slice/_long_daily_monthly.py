"""Daily / monthly P&L for LONG-only MOMENTUM_V1 (frozen brackets)."""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

OUT = Path("results/MOMENTUM_SLICE/entry_sweep")


def parse_session(sid: str) -> tuple[str, str]:
    """DDMMYY -> (YYYY-MM-DD, YYYY-MM)."""
    dd, mm, yy = sid[:2], sid[2:4], sid[4:6]
    day = f"20{yy}-{mm}-{dd}"
    month = f"20{yy}-{mm}"
    return day, month


def load_longs(path: Path) -> list[dict]:
    rows = []
    for r in csv.DictReader(open(path, encoding="utf-8")):
        if int(r["side"]) != 1:
            continue
        day, month = parse_session(r["session"])
        rows.append({
            "session": r["session"],
            "day": day,
            "month": month,
            "net": float(r["net"]),
            "pnl": float(r["pnl"]),
            "entry_ts_ns": int(r["entry_ts_ns"]),
        })
    rows.sort(key=lambda x: x["entry_ts_ns"])
    return rows


def summarize(rows: list[dict], label: str) -> str:
    by_day: dict[str, list[float]] = defaultdict(list)
    by_month: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r["net"])
        by_month[r["month"]].append(r["net"])

    lines = [f"# {label} — LONG momentum daily / monthly", ""]
    eq = 0.0
    day_eq = []
    lines.append("## Daily")
    lines.append("| day | n | net | cum |")
    lines.append("|---|---|---|---|")
    for day in sorted(by_day):
        xs = by_day[day]
        net = round(sum(xs), 2)
        eq = round(eq + net, 2)
        day_eq.append(eq)
        lines.append(f"| {day} | {len(xs)} | {net:+.2f} | {eq:+.2f} |")
    lines.append("")
    lines.append("## Monthly")
    lines.append("| month | n | net | win_days | lose_days |")
    lines.append("|---|---|---|---|---|")
    # win/lose days within month
    day_net = {d: sum(by_day[d]) for d in by_day}
    month_eq = 0.0
    for month in sorted(by_month):
        xs = by_month[month]
        net = round(sum(xs), 2)
        month_eq = round(month_eq + net, 2)
        days = [d for d in by_day if d.startswith(month)]
        wd = sum(1 for d in days if day_net[d] > 0)
        ld = sum(1 for d in days if day_net[d] < 0)
        lines.append(f"| {month} | {len(xs)} | {net:+.2f} | {wd} | {ld} |")
    lines.append("")
    lines.append(f"Total: n={len(rows)} net={sum(r['net'] for r in rows):+.2f} pts")
    lines.append(f"Day equity: {','.join(str(x) for x in day_eq)}")
    return "\n".join(lines) + "\n"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for split, path in (
        ("train", Path("results/MOMENTUM_SLICE/backtest_v1/equity_train_mom_t60.csv")),
        ("holdout", Path("results/MOMENTUM_SLICE/backtest_v1/equity_holdout_mom_t60.csv")),
    ):
        rows = load_longs(path)
        text = summarize(rows, f"{split} MOM_V1 LONG-only")
        out = OUT / f"long_daily_monthly_{split}.md"
        out.write_text(text, encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
