"""EXP-0042 dig — trade shallow reclaim with alternate brackets / HVN TP.

Algo (all arms share the same *entry*):
  1. Dual IRT-level touch (any×any kind) within sync_ms
  2. NQ pierce stays < shallow_max (default 8) and joint hold
  3. Enter fade at NQ reclaim (reject_away back through level)
  4. Exit via bracket variant (see below)

HVN-target arms (asymmetric):
  - TP = distance to nearest NQ HVN in the *profit* direction
    (long → nearest HVN above; short → nearest HVN below), minus buffer
  - Only take if gap ≥ min_gap
  - SL = fixed pts OR stop just beyond the reclaim level

Writes results/EXP-0042_SHALLOW_DIG/.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import yaml

from research.backtest.mat_sim import (
    MatSimConfig,
    TradeResult,
    make_vol_bracket,
    make_vol_bracket_rr,
    simulate_session,
)
from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed
from research.irt_lvn.events import (
    IrtTouchConfig,
    MarketPierceConfig,
    classify_dual_events,
    detect_irt_touches,
    dual_irt_touch_events,
    reclaim_events_table,
)
from research.irt_lvn.hvn import annotate_hvn_brackets
from research.irt_lvn.load_irt import (
    default_irt_paths,
    load_irt_csv,
    session_id_to_ymd,
)
from research.irt_lvn.run_exp_0042 import (
    COMMISSION,
    _pack_trades,
    bounds,
    filt,
    list_parquet_sessions,
    validate_timezone,
)
from research.outcomes.label import build_price_path
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")


def load_exp() -> dict:
    path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0042.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _sim(
    sid: str,
    nq: pa.Table,
    es: pa.Table,
    lo: int,
    events: pa.Table,
    cfg: MatSimConfig,
    bracket_fn=None,
) -> list[TradeResult]:
    if events.num_rows == 0:
        return []
    return simulate_session(
        sid, nq, es, cfg, gate=None, bracket_fn=bracket_fn,
        vwap_from_ns=lo, events=events,
    ).trades


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--month", default="2026-10")
    ap.add_argument("--max-align-err", type=float, default=25.0)
    args = ap.parse_args()

    exp = load_exp()
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    y_sel, m_sel = (int(x) for x in args.month.split("-")[:2])
    tag = f"EXP-0042_SHALLOW_DIG_{y_sel}{m_sel:02d}"
    out = Path(cfg["results_dir"]) / tag
    out.mkdir(parents=True, exist_ok=True)

    irt_paths = default_irt_paths(ROOT)
    print(f"Loading IRT {irt_paths['NQ'].name} / {irt_paths['ES'].name}", flush=True)
    nq_irt = load_irt_csv(irt_paths["NQ"])
    es_irt = load_irt_csv(irt_paths["ES"])

    touch_cfg = IrtTouchConfig(
        sync_ms=int(exp.get("sync_ms_primary", 5000)),
        touch_cooldown_ms=int(exp.get("touch_cooldown_ms", 8000)),
        decision_horizon_ms=int(exp.get("decision_horizon_ms", 120_000)),
        nq=MarketPierceConfig(
            float(exp["nq_pierce_typical"]), float(exp["nq_pierce_fail"]),
            float(exp["nq_reject_away"]), float(exp["nq_pad_pts"]),
        ),
        es=MarketPierceConfig(
            float(exp["es_pierce_typical"]), float(exp["es_pierce_fail"]),
            float(exp["es_reject_away"]), float(exp["es_pad_pts"]),
        ),
    )
    shallow_max = float(exp.get("shallow_pierce_max", 8.0))
    slip = float(exp.get("slip_ticks", 1.0))
    cool = float(exp.get("signal_cooldown_sec", 5.0))
    comm = float(exp.get("commission_pts_rt", COMMISSION))

    nq_days = nq_irt.calendar_days()
    es_days = es_irt.calendar_days()
    eligible = []
    for sid in list_parquet_sessions(pq):
        y, m, d = session_id_to_ymd(sid)
        if (y, m) != (y_sel, m_sel):
            continue
        if (y, m, d) not in nq_days or (y, m, d) not in es_days:
            continue
        eligible.append(sid)
    print(f"{args.month} eligible: {eligible}", flush=True)

    sim_base = MatSimConfig(
        slip_ticks=slip, commission_pts_rt=comm,
        signal_cooldown_sec=cool, flat_only=True,
    )
    br_vol = make_vol_bracket(1.0)
    br_rr2 = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0)
    br_rr3 = make_vol_bracket_rr(1.0, tp_r=3.0, sl_r=1.0)
    sim_fixed = MatSimConfig(
        slip_ticks=slip, commission_pts_rt=comm,
        signal_cooldown_sec=cool, flat_only=True,
        tp_pts=float(exp.get("fixed_tp_pts", 12.5)),
        sl_pts=float(exp.get("fixed_sl_pts", 30.0)),
    )

    # variant_name -> trades
    books: dict[str, list[TradeResult]] = defaultdict(list)
    skipped = []
    session_meta = []
    hvn_gap_hist: list[float] = []

    min_gaps = [15.0, 20.0, 25.0, 30.0, 40.0]
    hvn_sl_specs = [
        ("sl10", "fixed10", 10.0),
        ("sl12", "fixed12", 12.0),
        ("sl15", "fixed15", 15.0),
        ("beyond8", "beyond_level", 8.0),
    ]

    for sid in eligible:
        print(f"[{tag}] {sid}", flush=True)
        nq0 = _prefer_single_feed(_load_trades(pq, sid, "NQ"), pq, sid, "NQ")
        es0 = _prefer_single_feed(_load_trades(pq, sid, "ES"), pq, sid, "ES")
        if nq0.num_rows == 0 or es0.num_rows == 0:
            continue
        mid = int(nq0.column("ts_ns").to_numpy()[nq0.num_rows // 2])
        lo, hi = bounds(mid)
        nq, es = filt(nq0, lo, hi), filt(es0, lo, hi)
        if nq.num_rows == 0 or es.num_rows == 0:
            continue

        y, m, d = session_id_to_ymd(sid)
        day_lo = int(datetime(y, m, d, 0, 0, tzinfo=UK).timestamp() * 1e9)
        day_hi = int(datetime(y, m, d, 23, 59, 59, tzinfo=UK).timestamp() * 1e9)
        nq_bars = nq_irt.slice_range(day_lo, day_hi)
        es_bars = es_irt.slice_range(day_lo, day_hi)
        align = validate_timezone(nq_bars, nq)
        med = align.get("median_abs_err_pts")
        if med is None or med >= args.max_align_err:
            skipped.append(sid)
            print(f"  skip align {med}", flush=True)
            continue

        nq_t = detect_irt_touches(nq, nq_bars, touch_cfg.nq, cooldown_ms=touch_cfg.touch_cooldown_ms)
        es_t = detect_irt_touches(es, es_bars, touch_cfg.es, cooldown_ms=touch_cfg.touch_cooldown_ms)
        dual, _ = dual_irt_touch_events(nq_t, es_t, sync_ms=touch_cfg.sync_ms)
        nq_pts, nq_px = build_price_path(nq)
        es_pts, es_px = build_price_path(es)
        classified = classify_dual_events(dual, nq_pts, nq_px, es_pts, es_px, touch_cfg)
        rec = reclaim_events_table(classified)
        if rec.num_rows == 0:
            session_meta.append({"session": sid, "shallow": 0})
            continue
        pierce = rec.column("nq_pierce_pts").to_numpy().astype(np.float64)
        shallow_m = pierce < shallow_max
        if not shallow_m.any():
            session_meta.append({"session": sid, "shallow": 0})
            continue
        shallow = rec.take(np.where(shallow_m)[0])
        session_meta.append({"session": sid, "shallow": int(shallow.num_rows)})

        # --- baseline books (no HVN filter) ---
        books["vol_k1"].extend(_sim(sid, nq, es, lo, shallow, sim_base, br_vol))
        books["fixed_12p5_30"].extend(_sim(sid, nq, es, lo, shallow, sim_fixed, None))
        books["vol_rr2"].extend(_sim(sid, nq, es, lo, shallow, sim_base, br_rr2))
        books["vol_rr3"].extend(_sim(sid, nq, es, lo, shallow, sim_base, br_rr3))

        # --- HVN asymmetric arms ---
        for g in min_gaps:
            for sl_tag, sl_mode, sl_val in hvn_sl_specs:
                if sl_mode.startswith("fixed"):
                    ev = annotate_hvn_brackets(
                        shallow, nq_bars, nq_pts, nq_px,
                        min_gap=g, tp_buffer=2.0, tp_cap=80.0,
                        sl_mode="fixed15", sl_fixed=sl_val,
                    )
                else:
                    ev = annotate_hvn_brackets(
                        shallow, nq_bars, nq_pts, nq_px,
                        min_gap=g, tp_buffer=2.0, tp_cap=80.0,
                        sl_mode="beyond_level", sl_beyond=sl_val,
                    )
                name = f"hvn_g{int(g)}_{sl_tag}"
                trades = _sim(sid, nq, es, lo, ev, sim_base, None)
                books[name].extend(trades)
                if g == 20.0 and sl_tag == "sl15" and ev.num_rows:
                    hvn_gap_hist.extend(ev.column("hvn_gap_pts").to_pylist())

            # HVN TP + vol SL (per-event TP, vol SL via mixing: set sl from vol at sim)
            # Approximate: annotate TP from HVN, SL = same as tp for filter then override
            # Use event tp_pts from HVN and sl from a second annotate with fixed then replace
            ev_tp = annotate_hvn_brackets(
                shallow, nq_bars, nq_pts, nq_px,
                min_gap=g, tp_buffer=2.0, tp_cap=80.0,
                sl_mode="fixed15", sl_fixed=15.0,
            )
            if ev_tp.num_rows:
                # Replace SL with vol estimate at each event via temporary sim path:
                # attach sl from make_vol_bracket by running regime — simpler: use
                # bracket_fn that returns (ev_tp, vol_sl) — mat_sim prefers event columns.
                # So set sl_pts column to NaN and... no, event columns win entirely.
                # Compute vol SL using median from a quick regime lookup is heavy.
                # Practical: SL = clip(gap/2, 6, 30) as "half-gap risk" asym variant.
                gaps = ev_tp.column("hvn_gap_pts").to_numpy().astype(np.float64)
                tps = ev_tp.column("tp_pts").to_numpy().astype(np.float64)
                sls = np.clip(gaps / 2.0, 6.0, 30.0)
                cols = {n: ev_tp.column(n) for n in ev_tp.column_names}
                cols["tp_pts"] = tps
                cols["sl_pts"] = sls
                ev_half = pa.table(cols)
                books[f"hvn_g{int(g)}_slHalfGap"].extend(
                    _sim(sid, nq, es, lo, ev_half, sim_base, None)
                )

    packed = {k: _pack_trades(v) for k, v in sorted(books.items(), key=lambda kv: kv[0])}
    # rank by net among HVN arms and baselines
    ranked = sorted(
        packed.items(),
        key=lambda kv: (kv[1].get("net") or -1e18),
        reverse=True,
    )

    report = {
        "experiment": tag,
        "month": args.month,
        "algo": {
            "entry": "dual IRT level touch → shallow pierce < "
                     f"{shallow_max} → fade at NQ reclaim",
            "hvn_tp": "TP = nearest NQ HVN in profit direction − 2pt buffer, "
                      "cap 80; trade only if gap ≥ min_gap",
            "hvn_sl": {
                "sl10/12/15": "fixed stop in pts",
                "beyond8": "stop beyond reclaim level by 8 pts",
                "slHalfGap": "SL = clip(gap/2, 6, 30)",
            },
            "baselines": ["vol_k1", "fixed_12p5_30", "vol_rr2", "vol_rr3"],
        },
        "sessions": eligible,
        "skipped_align": skipped,
        "session_meta": session_meta,
        "variants": packed,
        "ranked_net": [{"name": k, **v} for k, v in ranked],
        "hvn_gap_sample_g20": {
            "n": len(hvn_gap_hist),
            "median": round(float(np.median(hvn_gap_hist)), 2) if hvn_gap_hist else None,
            "p25": round(float(np.percentile(hvn_gap_hist, 25)), 2) if hvn_gap_hist else None,
            "p75": round(float(np.percentile(hvn_gap_hist, 75)), 2) if hvn_gap_hist else None,
        },
    }
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        f"# {tag} — Shallow reclaim bracket dig",
        "",
        "## What the algo does",
        "",
        "1. **Setup:** ES and NQ each touch any IRT pool level (LVN / DOP / dVPOC) "
        "same side within 5s.",
        "2. **Shallow:** NQ pierce beyond the level stays **&lt; 8 pts**.",
        "3. **Entry:** fade NQ when price reclaims back through the level "
        "(`reject_away`).",
        "4. **Exit variants** below — baselines use vol/fixed/RR; HVN arms set "
        "**TP = nearest HVN in the profit direction** and only trade if that gap "
        "is wide enough (`min_gap`).",
        "",
        f"**Month:** {args.month} · sessions used: "
        f"{[s for s in eligible if s not in skipped]} · skipped align: `{skipped}`",
        "",
        "## Ranked by net P&L",
        "",
        "| variant | n | net | avg | days+ | tpd | maxDD |",
        "|---------|---|-----|-----|-------|-----|-------|",
    ]
    for k, v in ranked:
        lines.append(
            f"| `{k}` | {v['n']} | {v['net']} | {v['avg']} | "
            f"{v['days_pos']}/{v['days']} | {v['trades_per_day']} | {v['max_dd']} |"
        )

    # highlight baselines + best HVN
    base_names = {"vol_k1", "fixed_12p5_30", "vol_rr2", "vol_rr3"}
    best_hvn = next((k for k, _ in ranked if k.startswith("hvn_")), None)
    lines += [
        "",
        "## Baselines vs best HVN",
        "",
    ]
    for name in ["vol_k1", "fixed_12p5_30", "vol_rr2", "vol_rr3"]:
        if name in packed:
            v = packed[name]
            lines.append(
                f"- **{name}:** n={v['n']} net={v['net']} "
                f"days+={v['days_pos']}/{v['days']} maxDD={v['max_dd']}"
            )
    if best_hvn:
        v = packed[best_hvn]
        lines.append(
            f"- **best HVN `{best_hvn}`:** n={v['n']} net={v['net']} "
            f"days+={v['days_pos']}/{v['days']} maxDD={v['max_dd']}"
        )
    lines += [
        "",
        f"HVN gap sample (g20/sl15 events): `{report['hvn_gap_sample_g20']}`",
        "",
        f"Full JSON: `results/{tag}/report.json`",
    ]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out / 'summary.md'}", flush=True)
    print("Top 8:", flush=True)
    for k, v in ranked[:8]:
        print(f"  {k}: n={v['n']} net={v['net']} days+={v['days_pos']}/{v['days']}", flush=True)


if __name__ == "__main__":
    main()
