"""MAT-sim overlays on frozen base = vol k=1 + trend + VWAP (UK 14:30-17:00).

Compares causal vs confirm, range175/200 x lag100 risk gates, optional CD gate.
Reports maxDD / worst day / days+ (not just net). Commission 0.25 pt/RT.
"""
from __future__ import annotations

import json
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
    make_daytype_skip,
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
OLD = {
    "260326", "030826", "040826", "050826",
    "160926", "170926", "180926", "210926", "220926",
    "011026", "021026", "051026", "061026", "071026",
}
NEW = {
    "081026", "091026", "121026", "131026", "141026",
    "151026", "161026", "290926", "300926",
}


def bounds(mid: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(mid / 1e9, tz=timezone.utc).astimezone(UK).date()
    return (
        int(datetime.combine(d, time(14, 30), tzinfo=UK).timestamp() * 1e9),
        int(datetime.combine(d, time(17, 0), tzinfo=UK).timestamp() * 1e9),
    )


def filt(t: pa.Table) -> pa.Table:
    if t.num_rows == 0:
        return t
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    s, e = bounds(int(ts[len(ts) // 2]))
    k = (ts >= s) & (ts <= e)
    return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)


def pack(s: SimSummary, cache_keys) -> dict:
    old = filter_summary(s, lambda t: t.session_id in OLD, COMMISSION)
    new = filter_summary(s, lambda t: t.session_id in NEW, COMMISSION)
    day_nets = []
    days_pos = 0
    for sid in cache_keys:
        dn = filter_summary(s, lambda t, x=sid: t.session_id == x, COMMISSION).net
        day_nets.append(dn)
        if dn > 0:
            days_pos += 1
    return {
        **s.to_dict(),
        "old_net": round(old.net, 2),
        "new_net": round(new.net, 2),
        "old_n": old.n,
        "new_n": new.n,
        "days_pos": days_pos,
        "n_sessions": len(cache_keys),
        "worst_day": round(min(day_nets), 2) if day_nets else None,
        "best_day": round(max(day_nets), 2) if day_nets else None,
    }


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    raw = Path(cfg["raw_dir"])
    out = Path(cfg["results_dir"]) / "MAT2_OVERLAYS_VOLK1"
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name for p in raw.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (pq / f"session_id={p.name}").exists()
    )
    cache: dict[str, tuple[pa.Table, pa.Table, int]] = {}
    for sid in sessions:
        nq = filt(_load_trades(pq, sid, "NQ"))
        es0 = _load_trades(pq, sid, "ES")
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows:
            cache[sid] = (nq, es, bounds(int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2]))[0])
    print(f"cached {len(cache)} sessions", flush=True)

    base_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    br = make_vol_bracket(1.0)

    variants: list[tuple[str, MatSimConfig, object, object]] = []
    for stamp in ("causal", "confirm"):
        variants.append((
            f"base_{stamp}",
            MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp=stamp),
            base_gate, br,
        ))
        for R in (175, 200):
            variants.append((
                f"{stamp}_skip_range{R}",
                MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp=stamp),
                combine_gates(base_gate, make_daytype_skip(max_range=float(R))),
                br,
            ))
            variants.append((
                f"{stamp}_skip_range{R}_lag100",
                MatSimConfig(
                    slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp=stamp,
                    max_es_lag_ms=100.0,
                ),
                combine_gates(base_gate, make_daytype_skip(max_range=float(R))),
                br,
            ))
        variants.append((
            f"{stamp}_lag100",
            MatSimConfig(
                slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp=stamp,
                max_es_lag_ms=100.0,
            ),
            base_gate, br,
        ))
        variants.append((
            f"{stamp}_cd_gate",
            MatSimConfig(
                slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp=stamp, cd_gate=True,
            ),
            base_gate, br,
        ))

    results = {}
    md = [
        "# MAT2 overlays on vol k=1",
        "",
        f"Commission {COMMISSION} pt/RT · 1-tick slip · trend+VWAP · UK 14:30–17:00 · "
        f"n_sessions={len(cache)}",
        "",
        "| variant | n | WR | gross | net | old | new | days+ | maxDD | worst |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, sim_cfg, gate, bracket_fn in variants:
        parts = [
            simulate_session(
                sid, nq, es, sim_cfg, gate=gate, bracket_fn=bracket_fn, vwap_from_ns=vf,
            )
            for sid, (nq, es, vf) in cache.items()
        ]
        all_s = merge_summaries(parts, COMMISSION)
        row = pack(all_s, cache.keys())
        results[name] = row
        print(
            f"{name:36s} n={row['n']:4d} net={row['net']:+8.1f} "
            f"old={row['old_net']:+7.1f} new={row['new_net']:+7.1f} "
            f"dd={row['max_dd']:.0f} days+={row['days_pos']} worst={row['worst_day']}",
            flush=True,
        )
        md.append(
            f"| {name} | {row['n']} | {row['wr']} | {row['gross']:+.1f} | {row['net']:+.1f} | "
            f"{row['old_net']:+.1f} | {row['new_net']:+.1f} | {row['days_pos']} | "
            f"{row['max_dd']} | {row['worst_day']} |"
        )

    md.extend([
        "",
        "Pick risk gates by **maxDD / worst day / days+**, not net alone.",
        "CD gate: report only — ship only if new-9 cohort agrees with old.",
        "",
    ])
    (out / "report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
