"""Exit-management variants on frozen vol k=1 base (trend+VWAP, UK 14:30-17:00).

Pre-registered: time stop 60/120; BE 0.4/0.6 of bracket; trail 0.5/0.75;
BE+time. Reports old/new cohorts, days+, maxDD, loss-tail (<= -20 pts).
Output: results/MAT2_EXITS_VOLK1/
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


def loss_tail(s) -> dict:
    bad = [tr for tr in s.trades if tr.pnl <= -20]
    return {"n": len(bad), "sum": round(sum(tr.pnl for tr in bad), 2)}


def pack(s, cache_keys):
    old = filter_summary(s, lambda t: t.session_id in OLD, COMMISSION)
    new = filter_summary(s, lambda t: t.session_id in NEW, COMMISSION)
    days_pos = sum(
        1 for sid in cache_keys
        if filter_summary(s, lambda t, x=sid: t.session_id == x, COMMISSION).net > 0
    )
    by_day = [
        filter_summary(s, lambda t, x=sid: t.session_id == x, COMMISSION).net
        for sid in cache_keys
    ]
    return {
        **s.to_dict(),
        "old_net": round(old.net, 2),
        "new_net": round(new.net, 2),
        "old_n": old.n,
        "new_n": new.n,
        "days_pos": days_pos,
        "worst_day": round(min(by_day), 2) if by_day else None,
        "loss_tail": loss_tail(s),
    }


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    raw = Path(cfg["raw_dir"])
    out = Path(cfg["results_dir"]) / "MAT2_EXITS_VOLK1"
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name for p in raw.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (pq / f"session_id={p.name}").exists()
    )
    cache = {}
    for sid in sessions:
        nq = filt(_load_trades(pq, sid, "NQ"))
        es0 = _load_trades(pq, sid, "ES")
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows:
            mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
            cache[sid] = (nq, es, bounds(mid)[0])
    print(f"cached {len(cache)}", flush=True)

    gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    br = make_vol_bracket(1.0)

    variants: list[tuple[str, MatSimConfig]] = [
        ("base_vol_k1", MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION)),
        ("comparator_12p5_30", MatSimConfig(
            slip_ticks=1.0, tp_pts=12.5, sl_pts=30.0, commission_pts_rt=COMMISSION,
        )),
        ("time60", MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, time_stop_sec=60)),
        ("time120", MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, time_stop_sec=120)),
        ("be04", MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, be_frac=0.4)),
        ("be06", MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, be_frac=0.6)),
        ("trail05", MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, trail_frac=0.5)),
        ("trail075", MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, trail_frac=0.75)),
        ("be04_time60", MatSimConfig(
            slip_ticks=1.0, commission_pts_rt=COMMISSION, be_frac=0.4, time_stop_sec=60,
        )),
        ("be06_time120", MatSimConfig(
            slip_ticks=1.0, commission_pts_rt=COMMISSION, be_frac=0.6, time_stop_sec=120,
        )),
    ]

    results = {}
    md = [
        "# MAT2 exit management on vol k=1",
        "",
        f"Commission {COMMISSION} pt/RT, 1-tick slip, trend+VWAP, UK 14:30–17:00. "
        f"Sessions={len(cache)}.",
        "",
        "| variant | n | WR | gross | net | old | new | days+ | maxDD | worst | tail_n | tail_sum |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, sim_cfg in variants:
        use_br = None if name.startswith("comparator") else br
        parts = [
            simulate_session(
                sid, nq, es, sim_cfg, gate=gate, bracket_fn=use_br, vwap_from_ns=vf,
            )
            for sid, (nq, es, vf) in cache.items()
        ]
        all_s = merge_summaries(parts, COMMISSION)
        row = pack(all_s, cache.keys())
        results[name] = row
        lt = row["loss_tail"]
        print(
            f"{name:20s} n={row['n']:4d} net={row['net']:+8.1f} "
            f"old={row['old_net']:+7.1f} new={row['new_net']:+7.1f} "
            f"dd={row['max_dd']:.0f} tail={lt['n']}/{lt['sum']}",
            flush=True,
        )
        md.append(
            f"| {name} | {row['n']} | {row['wr']} | {row['gross']:+.1f} | {row['net']:+.1f} | "
            f"{row['old_net']:+.1f} | {row['new_net']:+.1f} | {row['days_pos']} | "
            f"{row['max_dd']} | {row['worst_day']} | {lt['n']} | {lt['sum']} |"
        )

    md.append("")
    (out / "report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
