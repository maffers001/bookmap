"""MAT vol k=1 time-of-day segmentation (UK 14:30–21:00).

IMPORTANT: brackets are **vol-scaled k=1**, not fixed TP12.5/SL30.

Segments (by entry time):
  1. First 5 minutes  (14:30–14:35)
  2. First 15 minutes (14:30–14:45)
  3. Half-hour buckets 14:30–21:00
  4. Hour buckets 14:30–21:00
  5. Closing 10 minutes (20:50–21:00)

Base: trend+VWAP, 1-tick slip, 0.25 pt/RT, causal ES, vol k=1.
Also reports the prior 14:30–17:00 window as a reference row.
Writes results/MAT2_TOD_VOLK1/.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    TradeResult,
    combine_gates,
    filter_summary,
    make_trend_mode,
    make_vol_bracket,
    make_vwap_side,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25
OPEN = time(14, 30)
CLOSE = time(21, 0)

OLD = {
    "260326", "030826", "040826", "050826",
    "160926", "170926", "180926", "210926", "220926",
    "011026", "021026", "051026", "061026", "071026",
}
NEW = {
    "081026", "091026", "121026", "131026", "141026",
    "151026", "161026", "290926", "300926",
}


def session_date(mid_ns: int):
    return datetime.fromtimestamp(mid_ns / 1e9, tz=timezone.utc).astimezone(UK).date()


def ns_at(d, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=UK).timestamp() * 1e9)


def bounds_full(mid: int) -> tuple[int, int]:
    d = session_date(mid)
    return ns_at(d, OPEN), ns_at(d, CLOSE)


def filt(t: pa.Table, lo: int, hi: int) -> pa.Table:
    if t.num_rows == 0:
        return t
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    k = (ts >= lo) & (ts <= hi)
    return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)


def uk_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).astimezone(UK)


def pack(s: SimSummary) -> dict:
    d = s.to_dict()
    d["avg"] = round(s.net / s.n, 2) if s.n else None
    d["~$"] = round(s.net * 20, 0)
    days = {tr.session_id for tr in s.trades}
    day_nets = []
    for sid in days:
        day_nets.append(filter_summary(s, lambda t, x=sid: t.session_id == x, COMMISSION).net)
    d["days"] = len(days)
    d["days_pos"] = sum(1 for x in day_nets if x > 0)
    d["worst_day"] = round(min(day_nets), 2) if day_nets else None
    return d


def segment_pred(name: str, kind: str):
    """Return predicate on TradeResult for a named segment."""

    def mins_from_open(tr: TradeResult) -> float:
        dt = uk_dt(tr.entry_ts_ns)
        open_dt = datetime.combine(dt.date(), OPEN, tzinfo=UK)
        return (dt - open_dt).total_seconds() / 60.0

    def clock(tr: TradeResult) -> time:
        return uk_dt(tr.entry_ts_ns).time()

    if kind == "first5":
        return lambda tr: 0 <= mins_from_open(tr) < 5
    if kind == "first15":
        return lambda tr: 0 <= mins_from_open(tr) < 15
    if kind == "close10":
        return lambda tr: time(20, 50) <= clock(tr) <= time(21, 0)
    if kind == "half":
        # name like "14:30-15:00"
        start_s, end_s = name.split("-")
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        t0, t1 = time(sh, sm), time(eh, em)

        def pred(tr: TradeResult, a=t0, b=t1) -> bool:
            c = clock(tr)
            if a < b:
                return a <= c < b
            return a <= c <= b  # shouldn't happen

        return pred
    if kind == "hour":
        start_s, end_s = name.split("-")
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        t0, t1 = time(sh, sm), time(eh, em)

        def pred(tr: TradeResult, a=t0, b=t1) -> bool:
            c = clock(tr)
            return a <= c < b

        return pred
    if kind == "window":
        # name like "14:30-17:00"
        start_s, end_s = name.split("-")
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        t0, t1 = time(sh, sm), time(eh, em)

        def pred(tr: TradeResult, a=t0, b=t1) -> bool:
            c = clock(tr)
            return a <= c < b if a < b else a <= c <= b

        return pred
    raise ValueError(kind)


def half_labels() -> list[str]:
    labels = []
    start = datetime.combine(datetime(2000, 1, 1).date(), OPEN)
    end = datetime.combine(datetime(2000, 1, 1).date(), CLOSE)
    cur = start
    while cur < end:
        nxt = cur + timedelta(minutes=30)
        labels.append(f"{cur.strftime('%H:%M')}-{nxt.strftime('%H:%M')}")
        cur = nxt
    return labels


def hour_labels() -> list[str]:
    # 14:30-15:30, 15:30-16:30, ... 20:30-21:00 (last stub)
    labels = []
    start = datetime.combine(datetime(2000, 1, 1).date(), OPEN)
    end = datetime.combine(datetime(2000, 1, 1).date(), CLOSE)
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(hours=1), end)
        labels.append(f"{cur.strftime('%H:%M')}-{nxt.strftime('%H:%M')}")
        cur = nxt
    return labels


def row_block(title: str, rows: list[tuple[str, SimSummary]]) -> list[str]:
    lines = [
        f"## {title}",
        "",
        "| segment | n | WR | gross | net | avg | PF | maxDD | days+/days | worst | old net | new net |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, s in rows:
        all_p = pack(s)
        old = pack(filter_summary(s, lambda t: t.session_id in OLD, COMMISSION))
        new = pack(filter_summary(s, lambda t: t.session_id in NEW, COMMISSION))
        lines.append(
            f"| {name} | {all_p['n']} | {all_p['wr']} | {all_p.get('gross', 0):+.1f} | "
            f"{all_p['net']:+.1f} | {all_p['avg']} | {all_p['pf']} | {all_p['max_dd']} | "
            f"{all_p['days_pos']}/{all_p['days']} | {all_p['worst_day']} | "
            f"{old['net']:+.1f} | {new['net']:+.1f} |"
        )
    lines.append("")
    return lines


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    raw = Path(cfg["raw_dir"])
    out = Path(cfg["results_dir"]) / "MAT2_TOD_VOLK1"
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name for p in raw.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (pq / f"session_id={p.name}").exists()
    )
    cache: dict[str, tuple[pa.Table, pa.Table, int]] = {}
    coverage = []
    for sid in sessions:
        nq_full = _load_trades(pq, sid, "NQ")
        es_full = _load_trades(pq, sid, "ES")
        if nq_full.num_rows == 0:
            continue
        mid = int(nq_full.column("ts_ns").to_numpy()[nq_full.num_rows // 2])
        lo, hi = bounds_full(mid)
        nq = filt(nq_full, lo, hi)
        es = filt(es_full, lo, hi) if es_full.num_rows else es_full
        if nq.num_rows == 0:
            continue
        cache[sid] = (nq, es, lo)
        ts = nq.column("ts_ns").to_numpy().astype(np.int64)
        coverage.append({
            "session": sid,
            "first_uk": uk_dt(int(ts[0])).strftime("%H:%M:%S"),
            "last_uk": uk_dt(int(ts[-1])).strftime("%H:%M:%S"),
            "nq_rows": int(nq.num_rows),
        })
    print(f"cached {len(cache)} sessions (UK {OPEN.strftime('%H:%M')}–{CLOSE.strftime('%H:%M')})", flush=True)

    gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    br = make_vol_bracket(1.0)
    sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")

    parts = []
    for sid, (nq, es, vf) in cache.items():
        parts.append(simulate_session(
            sid, nq, es, sim_cfg, gate=gate, bracket_fn=br, vwap_from_ns=vf,
        ))
    all_s = merge_summaries(parts, COMMISSION)
    print(f"all trades n={all_s.n} net={all_s.net:+.1f}", flush=True)

    def seg(kind: str, name: str) -> SimSummary:
        return filter_summary(all_s, segment_pred(name, kind), COMMISSION)

    blocks: list[tuple[str, list[tuple[str, SimSummary]]]] = []

    # Reference windows
    blocks.append(("Reference windows", [
        ("all 14:30–21:00", all_s),
        ("14:30–17:00 (prior yardstick)", seg("window", "14:30-17:00")),
        ("17:00–21:00", seg("window", "17:00-21:00")),
    ]))

    # 1 & 2 — first 5 / 15
    blocks.append(("1–2. Open: first 5 / 15 minutes", [
        ("first 5m (14:30–14:35)", seg("first5", "first5")),
        ("first 15m (14:30–14:45)", seg("first15", "first15")),
        ("after first 5m", filter_summary(
            all_s, lambda tr: not segment_pred("first5", "first5")(tr), COMMISSION,
        )),
        ("after first 15m", filter_summary(
            all_s, lambda tr: not segment_pred("first15", "first15")(tr), COMMISSION,
        )),
    ]))

    # 3 — half hours
    halves = [(lab, seg("half", lab)) for lab in half_labels()]
    blocks.append(("3. Half-hour chunks (14:30–21:00)", halves))

    # 4 — hours
    hours = [(lab, seg("hour", lab)) for lab in hour_labels()]
    blocks.append(("4. Hourly chunks (14:30–21:00)", hours))

    # 5 — close
    blocks.append(("5. Closing 10 minutes (20:50–21:00)", [
        ("close 10m (20:50–21:00)", seg("close10", "close10")),
        ("before close 10m", filter_summary(
            all_s, lambda tr: not segment_pred("close10", "close10")(tr), COMMISSION,
        )),
    ]))

    md = [
        "# MAT2 time-of-day — vol k=1",
        "",
        f"Sessions: **{len(cache)}**. Window UK **14:30–21:00**, trend=1 + VWAP, "
        f"1-tick slip, commission {COMMISSION} pt/RT, causal ES, vol k=1.",
        "",
        "Segmentation is by **entry time**. Exits may fall outside the segment.",
        "",
    ]
    report: dict = {"coverage": coverage, "commission_pts_rt": COMMISSION, "segments": {}}

    for title, rows in blocks:
        md.extend(row_block(title, rows))
        report["segments"][title] = {
            name: {
                "all": pack(s),
                "old": pack(filter_summary(s, lambda t: t.session_id in OLD, COMMISSION)),
                "new": pack(filter_summary(s, lambda t: t.session_id in NEW, COMMISSION)),
            }
            for name, s in rows
        }

    # Highlight best/worst half-hours by avg pnl (min n=10)
    usable = [(n, s) for n, s in halves if s.n >= 10]
    if usable:
        by_avg = sorted(usable, key=lambda x: (x[1].net / x[1].n), reverse=True)
        by_net = sorted(usable, key=lambda x: x[1].net, reverse=True)
        md.extend([
            "## Highlights (half-hours with n≥10)",
            "",
            f"- Best avg: `{by_avg[0][0]}` avg {by_avg[0][1].net/by_avg[0][1].n:+.2f} "
            f"(n={by_avg[0][1].n}, net {by_avg[0][1].net:+.1f})",
            f"- Worst avg: `{by_avg[-1][0]}` avg {by_avg[-1][1].net/by_avg[-1][1].n:+.2f} "
            f"(n={by_avg[-1][1].n}, net {by_avg[-1][1].net:+.1f})",
            f"- Best net: `{by_net[0][0]}` {by_net[0][1].net:+.1f}",
            f"- Worst net: `{by_net[-1][0]}` {by_net[-1][1].net:+.1f}",
            "",
        ])

    md.extend([
        "## Session coverage (NQ in window)",
        "",
        "| session | first | last | nq rows |",
        "|---|---|---|---:|",
    ])
    for c in coverage:
        md.append(f"| {c['session']} | {c['first_uk']} | {c['last_uk']} | {c['nq_rows']} |")
    md.append("")

    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
