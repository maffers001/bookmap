"""One-shot Sep–Oct holdout for frozen EXP-0056_DIG rule.

Reads results/EXP-0056_DIG/frozen.json and refuses to run without it.
Does not retune. Compares frozen arm vs ungated-filter base under same gates/brackets.
"""
from __future__ import annotations

import json
from pathlib import Path

from research.backtest.mat_sim import MatSimConfig, detect_signals
from research.config import load_config
from research.correlated.harness import _prefer_single_feed
from research.features.range_bar_profile import RangeBarProfileConfig, attach_mat_range_context
from research.run_experiment import _load_trades
from research.run_mat_may import day_equity_stats
from research.run_mat_range_bars import (
    COMMISSION,
    HOLDOUT_MMYY,
    bounds,
    filt,
    list_sessions,
    make_bracket,
    sid_month,
)
from research.run_mat_range_bars_dig import candidate_masks, gated_events, simulate_mask


def load_holdout_raw(cfg: dict) -> dict[str, dict]:
    pq_dir = Path(cfg["parquet_dir"])
    base_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    rb_cfg = RangeBarProfileConfig()
    data: dict[str, dict] = {}
    for sid in list_sessions(pq_dir):
        if sid_month(sid) not in HOLDOUT_MMYY:
            continue
        nq = filt(_prefer_single_feed(_load_trades(pq_dir, sid, "NQ"), pq_dir, sid, "NQ"))
        es0 = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows == 0:
            continue
        mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
        vf = bounds(mid)[0]
        raw = detect_signals(nq, es, base_cfg)
        ev, _bars, _prof = attach_mat_range_context(nq, raw, cfg=rb_cfg)
        data[sid] = {
            "nq": nq, "es": es, "vf": vf, "raw_events": ev, "month": sid_month(sid),
        }
        print(f"load {sid} (holdout/{sid_month(sid)}): raw_mat={raw.num_rows}", flush=True)
    return data


def main() -> None:
    cfg = load_config()
    out = Path(cfg["results_dir"]) / "EXP-0056_DIG"
    frozen_path = out / "frozen.json"
    if not frozen_path.exists():
        raise SystemExit(f"No freeze file at {frozen_path}")
    freeze = json.loads(frozen_path.read_text(encoding="utf-8"))
    arm = freeze["arm"]
    tp_r = float(freeze.get("tp_r", 1.0))
    sl_r = float(freeze.get("sl_r", 1.0))
    trend = bool(freeze.get("mat_trend", True))
    vwap = bool(freeze.get("mat_vwap", True))
    print(
        f"HOLDOUT once: arm={arm} trend={trend} vwap={vwap} tp_r={tp_r} sl_r={sl_r}",
        flush=True,
    )

    data = load_holdout_raw(cfg)
    events = gated_events(data, trend, vwap)
    base = simulate_mask(data, events, "base", tp_r=tp_r, sl_r=sl_r)
    frozen = simulate_mask(data, events, arm, tp_r=tp_r, sl_r=sl_r)

    disc_avg = (freeze.get("discovery") or {}).get("avg")
    hold_ok = (
        frozen.get("avg") is not None
        and frozen["avg"] > 0
        and (frozen.get("net") or 0) > 0
        and disc_avg is not None
        and (frozen["avg"] > 0) == (float(disc_avg) > 0)
    )
    verdict = "holdout PASS" if hold_ok else "holdout FAIL — reject / park"

    from research.backtest.mat_sim import simulate_session
    import numpy as np

    sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    br = make_bracket(tp_r=tp_r, sl_r=sl_r)
    per_day = []
    for sid, sd in sorted(data.items()):
        ev = events[sid]
        if ev.num_rows == 0:
            continue
        mask = candidate_masks(ev)[arm]
        if not mask.any():
            per_day.append({"session": sid, "month": sd["month"], "n": 0, "net": 0.0, "note": "no signals"})
            continue
        s = simulate_session(
            sid, sd["nq"], sd["es"], sim_cfg,
            gate=None, bracket_fn=br, vwap_from_ns=sd["vf"],
            events=ev.take(np.where(mask)[0]),
        )
        eq = day_equity_stats(s.trades, COMMISSION) if s.n else {
            "equity_end": 0.0, "equity_high": 0.0, "day_max_dd": 0.0,
        }
        per_day.append({
            "session": sid,
            "month": sd["month"],
            "n": s.n,
            "wr": None if s.n == 0 else round(s.wr, 4),
            "net": round(s.net, 2),
            "avg": round(s.net / s.n, 3) if s.n else None,
            **{k: eq[k] for k in ("equity_end", "equity_high", "day_max_dd")},
        })
        print(
            f"{sid}: n={s.n} net={s.net:+.1f}",
            flush=True,
        )

    report = {
        "status": "ran",
        "frozen": freeze,
        "holdout_months": sorted(HOLDOUT_MMYY),
        "n_sessions": len(data),
        "base": base,
        "arm": frozen,
        "holdout_ok": hold_ok,
        "verdict": verdict,
        "per_day": per_day,
    }
    (out / "holdout_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    md = [
        "# EXP-0056_DIG holdout — Sep–Oct 2026 (one-shot)",
        "",
        f"Frozen: **`{arm}`**, gates trend={trend} vwap={vwap}, "
        f"bracket tp_r={tp_r} sl_r={sl_r} (prior dig discovery avg={disc_avg}).",
        "",
        f"**Verdict: {verdict}**",
        "",
        "## Summary",
        "",
        f"- Base: n={base.get('n')} avg={base.get('avg')} net={base.get('net')}",
        f"- Frozen arm: n={frozen.get('n')} avg={frozen.get('avg')} net={frozen.get('net')} "
        f"WR={frozen.get('wr')} PF={frozen.get('pf')}",
        "",
        "### By month",
        "",
        "| month | n | avg | net |",
        "|---|---:|---:|---:|",
    ]
    for m, s in sorted((frozen.get("by_month") or {}).items()):
        md.append(f"| {m} | {s.get('n')} | {s.get('avg')} | {s.get('net')} |")
    md += [
        "",
        "## Per session",
        "",
        "| session | month | n | WR | net | day high | day end | day DD |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in per_day:
        if r.get("note"):
            md.append(f"| {r['session']} | {r['month']} | 0 | — | 0 | — | — | {r['note']} |")
            continue
        md.append(
            f"| {r['session']} | {r['month']} | {r['n']} | {r['wr']} | {r['net']:+.1f} | "
            f"{r['equity_high']:+.1f} | {r['equity_end']:+.1f} | {r['day_max_dd']:.1f} |"
        )
    md.append("")
    (out / "holdout_summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'holdout_summary.md'}", flush=True)
    print(verdict, flush=True)


if __name__ == "__main__":
    main()
