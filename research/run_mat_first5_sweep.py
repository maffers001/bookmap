"""First-5-minute MAT indicator/param sweep on frozen vol k=1 open.

Simulates full UK 14:30–17:00 tape (so exits can complete), then keeps only
trades with entry in 14:30–14:35. Base = trend+VWAP, causal, vol k=1, 1-tick,
0.25 pt/RT. One-factor and a few combo variants vs that base.

Writes results/MAT2_TOD_FIRST5_SWEEP/.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    combine_gates,
    filter_summary,
    gate_always,
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
YARD_END = time(17, 0)

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


def filt(t: pa.Table, lo: int, hi: int) -> pa.Table:
    if t.num_rows == 0:
        return t
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    k = (ts >= lo) & (ts <= hi)
    return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)


def uk_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).astimezone(UK)


def in_first5(tr) -> bool:
    dt = uk_dt(tr.entry_ts_ns)
    open_dt = datetime.combine(dt.date(), OPEN, tzinfo=UK)
    mins = (dt - open_dt).total_seconds() / 60.0
    return 0 <= mins < 5


def pack(s: SimSummary) -> dict:
    d = s.to_dict()
    d["avg"] = round(s.net / s.n, 2) if s.n else None
    days = {tr.session_id for tr in s.trades}
    day_nets = [
        filter_summary(s, lambda t, x=sid: t.session_id == x, COMMISSION).net
        for sid in days
    ]
    d["days"] = len(days)
    d["days_pos"] = sum(1 for x in day_nets if x > 0)
    d["worst_day"] = round(min(day_nets), 2) if day_nets else None
    old = filter_summary(s, lambda t: t.session_id in OLD, COMMISSION)
    new = filter_summary(s, lambda t: t.session_id in NEW, COMMISSION)
    d["old_net"] = round(old.net, 2)
    d["new_net"] = round(new.net, 2)
    d["old_n"] = old.n
    d["new_n"] = new.n
    return d


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    raw = Path(cfg["raw_dir"])
    out = Path(cfg["results_dir"]) / "MAT2_TOD_FIRST5_SWEEP"
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name for p in raw.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (pq / f"session_id={p.name}").exists()
    )
    cache: dict[str, tuple[pa.Table, pa.Table, int]] = {}
    for sid in sessions:
        nq_full = _load_trades(pq, sid, "NQ")
        es_full = _load_trades(pq, sid, "ES")
        if nq_full.num_rows == 0:
            continue
        mid = int(nq_full.column("ts_ns").to_numpy()[nq_full.num_rows // 2])
        d = session_date(mid)
        lo, hi = ns_at(d, OPEN), ns_at(d, YARD_END)
        nq = filt(nq_full, lo, hi)
        es = filt(es_full, lo, hi) if es_full.num_rows else es_full
        if nq.num_rows:
            cache[sid] = (nq, es, lo)
    print(f"cached {len(cache)} sessions", flush=True)

    base_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    base_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    base_br = make_vol_bracket(1.0)

    # (name, cfg, gate, bracket_fn|None) — use _FIXED to force cfg.tp/sl (no vol bracket)
    _FIXED = object()
    variants: list[tuple[str, MatSimConfig, object, object]] = []

    def add(name, cfg=None, gate=None, br=_FIXED):
        if br is _FIXED:
            bracket = base_br
        elif br is None:
            bracket = None  # use MatSimConfig.tp_pts / sl_pts
        else:
            bracket = br
        variants.append((
            name,
            cfg if cfg is not None else base_cfg,
            gate if gate is not None else base_gate,
            bracket,
        ))

    add("BASE_vol_k1_causal_trend_vwap")

    # --- Burst detector ---
    for mt in (4, 8, 10):
        add(f"min_trades={mt}", replace(base_cfg, min_trades=mt))
    for mv in (15.0, 40.0, 50.0):
        add(f"min_vol={mv:g}", replace(base_cfg, min_vol=mv))
    for mi in (0.45, 0.65, 0.75):
        add(f"min_imb={mi}", replace(base_cfg, min_imbalance=mi))
    for mp in (0.5, 2.0, 3.0):
        add(f"min_progress={mp}", replace(base_cfg, min_progress_ticks=mp))
    for cd in (1, 5):
        add(f"cooldown_bars={cd}", replace(base_cfg, cooldown_bars=cd))
    for bm in (50, 200):
        add(f"bar_ms={bm}", replace(base_cfg, bar_ms=bm))
    for sc in (0.0, 10.0):
        add(f"signal_cd_sec={sc:g}", replace(base_cfg, signal_cooldown_sec=sc))

    # --- ES sync ---
    for lag in (50.0, 100.0, 500.0, 1500.0):
        add(f"max_es_lag={lag:g}", replace(base_cfg, max_es_lag_ms=lag))
    for syn in (500, 1000, 2500):
        add(f"sync_ms={syn}", replace(base_cfg, sync_ms=syn))
    add("stamp=confirm", replace(base_cfg, es_stamp="confirm"))

    # --- Gates ---
    add("gate=off", gate=gate_always)
    add("gate=trend_only", gate=make_trend_mode("with"))
    add("gate=vwap_only", gate=make_vwap_side(True))
    add("gate=trend_vwap_cd", replace(base_cfg, cd_gate=True))

    # --- Brackets (still first-5 entries only) ---
    for k in (0.5, 1.5, 2.0, 3.0):
        add(f"bracket_vol_k={k}", br=make_vol_bracket(k))
    add("bracket_fixed_10_10", replace(base_cfg, tp_pts=10.0, sl_pts=10.0), br=None)
    add("bracket_fixed_12p5_30", replace(base_cfg, tp_pts=12.5, sl_pts=30.0), br=None)
    add("bracket_fixed_15_15", replace(base_cfg, tp_pts=15.0, sl_pts=15.0), br=None)
    add("bracket_fixed_20_20", replace(base_cfg, tp_pts=20.0, sl_pts=20.0), br=None)
    add("bracket_fixed_8_8", replace(base_cfg, tp_pts=8.0, sl_pts=8.0), br=None)
    add("bracket_fixed_25_25", replace(base_cfg, tp_pts=25.0, sl_pts=25.0), br=None)

    # --- Combos aimed at open noise ---
    add(
        "combo_tight_burst_lag100",
        replace(base_cfg, min_trades=8, min_vol=40.0, min_imbalance=0.65, max_es_lag_ms=100.0),
    )
    add(
        "combo_loose_burst_vol_k1",
        replace(base_cfg, min_trades=4, min_vol=15.0, min_imbalance=0.45),
    )
    add(
        "combo_confirm_lag100_vol_k1",
        replace(base_cfg, es_stamp="confirm", max_es_lag_ms=100.0),
    )
    add(
        "combo_progress2_imb65",
        replace(base_cfg, min_progress_ticks=2.0, min_imbalance=0.65),
    )

    results = {}
    rows = []
    for name, sim_cfg, gate, br in variants:
        parts = [
            simulate_session(
                sid, nq, es, sim_cfg, gate=gate, bracket_fn=br, vwap_from_ns=vf,
            )
            for sid, (nq, es, vf) in cache.items()
        ]
        all_s = merge_summaries(parts, COMMISSION)
        first5 = filter_summary(all_s, in_first5, COMMISSION)
        row = pack(first5)
        results[name] = row
        print(
            f"{name:36s} n={row['n']:3d} net={row['net']:+7.1f} avg={row['avg']} "
            f"old={row['old_net']:+7.1f} new={row['new_net']:+7.1f} "
            f"days+={row['days_pos']}/{row['days']}",
            flush=True,
        )
        rows.append((name, row))

    base_net = results["BASE_vol_k1_causal_trend_vwap"]["net"]
    improved = [
        (n, r) for n, r in rows
        if n != "BASE_vol_k1_causal_trend_vwap"
        and r["n"] >= 20
        and r["net"] > base_net
        and r["new_net"] > 0  # require OOS not wrecked
    ]
    improved.sort(key=lambda x: x[1]["net"], reverse=True)

    md = [
        "# MAT2 first-5-minute param sweep",
        "",
        "**Brackets default = vol k=1** (not TP12.5/SL30). Entries only in UK "
        "**14:30–14:35**; path UK 14:30–17:00 so exits can complete. "
        f"trend+VWAP, causal ES, 1-tick slip, commission {COMMISSION} pt/RT. "
        f"Sessions={len(cache)}.",
        "",
        f"BASE first-5 net = **{base_net:+.1f}** "
        f"(n={results['BASE_vol_k1_causal_trend_vwap']['n']}).",
        "",
        "## All variants",
        "",
        "| variant | n | WR | gross | net | avg | old | new | days+/days | maxDD | vs base |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, r in rows:
        delta = r["net"] - base_net
        md.append(
            f"| {name} | {r['n']} | {r['wr']} | {r.get('gross', 0):+.1f} | {r['net']:+.1f} | "
            f"{r['avg']} | {r['old_net']:+.1f} | {r['new_net']:+.1f} | "
            f"{r['days_pos']}/{r['days']} | {r['max_dd']} | {delta:+.1f} |"
        )

    md.extend([
        "",
        "## Improvers vs BASE (n≥20, new net > 0, net > base)",
        "",
    ])
    if not improved:
        md.append("None — no variant beat BASE on net while keeping new-cohort positive and n≥20.")
    else:
        md.append("| variant | n | net | avg | old | new | Δ vs base |")
        md.append("|---|---:|---:|---:|---:|---:|---:|")
        for name, r in improved:
            md.append(
                f"| {name} | {r['n']} | {r['net']:+.1f} | {r['avg']} | "
                f"{r['old_net']:+.1f} | {r['new_net']:+.1f} | {r['net']-base_net:+.1f} |"
            )
    md.append("")

    (out / "report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out}", flush=True)
    print(f"improvers: {len(improved)}", flush=True)


if __name__ == "__main__":
    main()
