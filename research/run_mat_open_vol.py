"""MAT v4 open-vol dig: prior-session median in first 20m, else fixed 10.

Compares frozen base (live vol k=1) vs open-anchor on train / May / Aug.
Writes results/MAT2_OPEN_VOL_V4/.
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
    make_trend_mode,
    make_vol_bracket,
    make_vol_bracket_open_anchor,
    make_vwap_side,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.correlated.harness import _prefer_single_feed
from research.features.regime import build_regime_index
from research.run_experiment import _load_trades
from research.run_mat_may import MAY, day_equity_stats
from research.run_mat_august import AUG

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25
OPEN_PERIOD_SEC = 20 * 60
OPEN_FIXED = 10.0


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


def session_median_range(nq: pa.Table) -> float | None:
    """Median 1-min range over the filtered session (completed minutes)."""
    if nq.num_rows == 0:
        return None
    bars, bar_ts, cum_pv, cum_v, *_ = build_regime_index(nq, pts_per_tick=0.25)
    if len(bars) < 5:
        return None
    # Prefer direct bar ranges (full RTH window median) over rolling window.
    ranges = np.array([b.high - b.low for b in bars], dtype=np.float64)
    med = float(np.median(ranges))
    return med if np.isfinite(med) and med > 0 else None


def cohort(sid: str) -> str:
    if sid in MAY:
        return "may"
    if sid in AUG:
        return "aug"
    return "train"


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "MAT2_OPEN_VOL_V4"
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name.replace("session_id=", "")
        for p in pq.iterdir()
        if p.is_dir() and p.name.startswith("session_id=")
        and (p / "trades.parquet").exists()
        and "smoke" not in p.name
    )

    # Chronological prior medians
    prior_by_sid: dict[str, float | None] = {}
    last_med: float | None = None
    for sid in sessions:
        prior_by_sid[sid] = last_med
        nq = filt(_prefer_single_feed(_load_trades(pq, sid, "NQ"), pq, sid, "NQ"))
        med = session_median_range(nq)
        if med is not None:
            last_med = med

    gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    sim = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    br_base = make_vol_bracket(1.0)

    results = {"base": {}, "open_anchor": {}}
    for label, use_anchor in (("base", False), ("open_anchor", True)):
        parts = []
        rows = []
        for sid in sessions:
            nq = filt(_prefer_single_feed(_load_trades(pq, sid, "NQ"), pq, sid, "NQ"))
            es0 = _prefer_single_feed(_load_trades(pq, sid, "ES"), pq, sid, "ES")
            es = filt(es0) if es0.num_rows else es0
            if nq.num_rows == 0:
                continue
            mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
            vf = bounds(mid)[0]
            if use_anchor:
                br = make_vol_bracket_open_anchor(
                    1.0,
                    prior_median=prior_by_sid.get(sid),
                    open_period_sec=OPEN_PERIOD_SEC,
                    session_open_ns=vf,
                    open_fixed_pts=OPEN_FIXED,
                )
            else:
                br = br_base
            s = simulate_session(
                sid, nq, es, sim, gate=gate, bracket_fn=br, vwap_from_ns=vf,
            )
            parts.append(s)
            eq = day_equity_stats(s.trades, COMMISSION)
            rows.append({
                "session": sid, "cohort": cohort(sid),
                "prior_med": prior_by_sid.get(sid),
                **s.to_dict(), **eq,
                "avg": round(s.net / s.n, 3) if s.n else None,
            })
            print(
                f"{label} {sid}: n={s.n} net={s.net:+.1f} prior={prior_by_sid.get(sid)}",
                flush=True,
            )
        merged = merge_summaries(parts, COMMISSION)
        by_co = {"all": merged.to_dict() | {"avg": round(merged.net / merged.n, 3) if merged.n else None}}
        for co in ("train", "may", "aug"):
            from research.backtest.mat_sim import filter_summary
            pred = {
                "train": lambda t: t.session_id not in MAY and t.session_id not in AUG,
                "may": lambda t: t.session_id in MAY,
                "aug": lambda t: t.session_id in AUG,
            }[co]
            sub = filter_summary(merged, pred, COMMISSION)
            by_co[co] = sub.to_dict() | {"avg": round(sub.net / sub.n, 3) if sub.n else None}
        results[label] = {"summary": by_co, "per_session": rows}

    md = [
        "# MAT v4 open-vol dig (prior median → fixed 10)",
        "",
        "First **20 min** after UK 14:30: brackets use **prior session** median "
        "1-min range (k=1, clip 6–30); if no prior, **fixed 10**. After 20 min: "
        "live vol k=1 as frozen base.",
        "",
        "| arm | cohort | n | avg | net | PF |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for label in ("base", "open_anchor"):
        for co in ("train", "may", "aug", "all"):
            r = results[label]["summary"][co]
            md.append(
                f"| {label} | {co} | {r['n']} | {r.get('avg')} | {r.get('net')} | {r.get('pf')} |"
            )
    md.append("")
    (out / "report.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
