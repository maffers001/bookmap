"""EXP-0036_DIG4 — freeze confirmation for test auto-trade.

Single idea from DIG3: ``against_m04`` fade + ``daytype_skip(max_er=E)``.
Neighbours E ∈ {0.25, 0.30, 0.35}. No other filters. If 0.30 and ≥1 neighbour
clear the ship bar → write frozen TEST_AUTOTRADE spec.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    make_daytype_skip,
    make_vol_bracket,
    simulate_session,
)
from research.correlated import harness as H
from research.correlated.exp_0036_fade_dig import SHIP_BAR, _concentration
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events


def _sim(
    pairs: list[H.Pair],
    by_sid: dict[str, pa.Table],
    *,
    gate=None,
    slip: float = 1.0,
) -> SimSummary:
    br = make_vol_bracket(1.0)
    cfg = MatSimConfig(
        slip_ticks=slip, commission_pts_rt=H.COMMISSION, tp_pts=10, sl_pts=10,
        signal_cooldown_sec=5.0,
    )
    parts = [
        simulate_session(
            p.sid, p.nq, p.es, cfg, gate=gate, bracket_fn=br,
            vwap_from_ns=p.vwap_from_ns, events=by_sid[p.sid],
        )
        for p in pairs if p.sid in by_sid
    ]
    return H.merge(parts) if parts else SimSummary()


def _loo(trades, sids: list[str], commission: float = H.COMMISSION) -> tuple[list[dict], int]:
    base_net = sum(t.pnl - commission for t in trades)
    rows = []
    for sid in sids:
        net = sum(t.pnl - commission for t in trades if t.session_id != sid)
        rows.append({
            "drop": sid,
            "cohort": H.cohort(sid),
            "net": round(net, 2),
            "delta": round(net - base_net, 2),
        })
    n_neg = sum(1 for r in rows if r["net"] < 0)
    return rows, n_neg


def _clears(r: dict, conc: dict, loo_neg: int) -> dict:
    checks = {
        "train_ok": r["train_net"] > 0,
        "hold_ok": r["hold_net"] > 0,
        "new_ok": r["new_net"] > 0,
        "days_ok": conc["days_pos"] >= 14,
        "top3_ok": (conc["top3_share_of_pos"] or 1.0) <= 0.55,
        "loo_ok": loo_neg <= 2,
    }
    return {**checks, "cleared": all(checks.values())}


def run(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / exp["name"]
    exclude = set(str(s) for s in exp.get("exclude_sessions") or [])
    bar_ms = int(exp.get("bar_ms", 100))
    fade_wait = float(exp.get("fade_max_wait_sec", 5.0))
    ers = [float(x) for x in (exp.get("er_neighbours") or [0.25, 0.30, 0.35])]
    slip = float(exp.get("slip_ticks", 1.0))

    pairs = []
    for sid in session_ids:
        if sid in exclude:
            continue
        p = H.load_pair(parquet_dir, sid)
        if p is not None:
            pairs.append(p)
    sids = [p.sid for p in pairs]
    print(f"[DIG4] {len(pairs)} sessions; ER neighbours={ers}", flush=True)

    p_ex = ExhaustParams(**(exp.get("exhaust") or {}))
    by_sid: dict[str, pa.Table] = {}
    raw: list[pa.Table] = []
    for p in pairs:
        nq_b, es_b = p.bars(bar_ms)
        bursts = H.confirm_bursts(p)
        fe = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=fade_wait)
        if fe.num_rows:
            fe = H._with_session(fe, p.sid)
            by_sid[p.sid] = fe
            raw.append(fe)
        print(f"  {p.sid}: bursts={bursts.num_rows} fade={fe.num_rows}", flush=True)

    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        exp.get("description", "").strip(),
        "",
        f"**Pre-registered ship bar (each neighbour):** {SHIP_BAR}",
        "",
        f"Sessions={len(sids)} · UK 14:30–17:00 · {slip}-tick slip · {H.COMMISSION} pt/RT · "
        f"fade = against_m04 (es_imb_max={p_ex.es_imb_max})",
        "",
        "## Candidate definitions",
        "",
        "| name | gate | notes |",
        "|---|---|---|",
        "| ungated | none | DIG2/DIG3 baseline (train≈0 expected) |",
    ]
    for er in ers:
        md.append(f"| skip_er{er:g} | daytype_skip(max_er={er}) | freeze candidate if clears |")
    md.append("")

    report: dict = {
        "status": "ok",
        "sessions": sids,
        "exhaust": asdict(p_ex),
        "ship_bar": SHIP_BAR,
        "candidates": {},
    }

    # Ungated + ER neighbours
    specs: list[tuple[str, object]] = [("ungated", None)]
    for er in ers:
        specs.append((f"skip_er{er:g}", make_daytype_skip(max_er=er)))

    md += ["## Results", "", *H.PNL_HDR]
    cleared = []
    for name, gate in specs:
        sim = _sim(pairs, by_sid, gate=gate, slip=slip)
        r = H.pack_pnl(sim, sids)
        conc = _concentration(sim.trades, sids)
        loo_rows, loo_neg = _loo(sim.trades, sids)
        checks = _clears(r, conc, loo_neg)
        report["candidates"][name] = {
            **r,
            "concentration": conc,
            "loo_neg": loo_neg,
            "loo": loo_rows,
            "checks": checks,
        }
        md.append(H.pnl_row(name, r))
        print(
            f"  {name:14s} n={r['n']:4d} net={r['net']:+8.1f} "
            f"train={r['train_net']:+7.1f} hold={r['hold_net']:+7.1f} new={r['new_net']:+7.1f} "
            f"days+={conc['days_pos']} top3={conc['top3_share_of_pos']} "
            f"loo_neg={loo_neg} cleared={checks['cleared']}",
            flush=True,
        )

    md.append("")
    for name, gate in specs:
        c = report["candidates"][name]
        conc = c["concentration"]
        ch = c["checks"]
        md += [
            f"### {name}",
            "",
            f"days+ {conc['days_pos']}/23 · top3 share={conc['top3_share_of_pos']} "
            f"(top3 {conc['top3_net']:+.1f}; worst3 {conc['worst3_net']:+.1f}) · "
            f"loo_neg={c['loo_neg']}",
            f"- top3: {conc['top3']}",
            f"- worst3: {conc['worst3']}",
            f"- checks: {ch}",
            "",
            "| drop | cohort | net without | Δ |",
            "|---|---|---:|---:|",
        ]
        for row in sorted(c["loo"], key=lambda x: x["net"]):
            md.append(f"| {row['drop']} | {row['cohort']} | {row['net']:+.1f} | {row['delta']:+.1f} |")
        md.append("")
        if name.startswith("skip_er") and ch["cleared"]:
            cleared.append(name)

    # 2-tick slip on freeze candidate (0.30) as live-readiness check
    md += ["## Live-readiness (2-tick slip on skip_er0.3)", "", *H.PNL_HDR]
    gate30 = make_daytype_skip(max_er=0.3)
    sim2 = _sim(pairs, by_sid, gate=gate30, slip=2.0)
    r2 = H.pack_pnl(sim2, sids)
    conc2 = _concentration(sim2.trades, sids)
    loo2, neg2 = _loo(sim2.trades, sids)
    ch2 = _clears(r2, conc2, neg2)
    report["slip2_skip_er0.3"] = {**r2, "concentration": conc2, "loo_neg": neg2, "checks": ch2}
    md.append(H.pnl_row("skip_er0.3 · slip2", r2))
    md.append(
        f"days+ {conc2['days_pos']} · top3={conc2['top3_share_of_pos']} · "
        f"loo_neg={neg2} · cleared={ch2['cleared']}"
    )
    md.append("")

    # Decision
    c30 = report["candidates"].get("skip_er0.3", {})
    cleared_30 = bool(c30.get("checks", {}).get("cleared"))
    neighbour_ok = any(n != "skip_er0.3" for n in cleared)
    if cleared_30 and neighbour_ok:
        decision = (
            "FREEZE for TEST_AUTOTRADE: against_m04 + skip_er0.3 (+ ≥1 neighbour cleared). "
            "No further param search on these 23d. Paper/test-size live next."
        )
        freeze = True
    elif cleared_30 and not neighbour_ok:
        decision = (
            "skip_er0.3 clears alone but no neighbour clears — do NOT freeze; "
            "fragile single-cell. Keep open dig / drop ER gate."
        )
        freeze = False
    else:
        decision = (
            "skip_er0.3 does not clear the bar on confirmation — drop ER gate; "
            "ungated m04 remains open dig only (not test-autotrade ready)."
        )
        freeze = False

    md += ["## Decision", "", f"**{decision}**", ""]
    report["decision"] = {
        "text": decision,
        "freeze": freeze,
        "cleared_neighbours": cleared,
        "skip_er0.3_cleared": cleared_30,
        "neighbour_ok": neighbour_ok,
    }
    print(f"[DIG4] decision: {decision}", flush=True)

    # Frozen spec (always write; mark status)
    spec_lines = _write_spec(freeze, p_ex, report, sids)
    spec_path = results_dir / "TEST_AUTOTRADE_SPEC.md"
    results_dir.mkdir(parents=True, exist_ok=True)
    spec_path.write_text("\n".join(spec_lines) + "\n", encoding="utf-8")
    md += ["## Test auto-trade spec", "", f"Wrote `{spec_path}`.", ""]
    report["spec_path"] = str(spec_path)

    H.write_report(results_dir, report, md)
    out: dict = {"summary": report}
    if raw:
        out["events"] = pa.concat_tables(raw, promote_options="default")
    return out


def _write_spec(freeze: bool, p_ex: ExhaustParams, report: dict, sids: list[str]) -> list[str]:
    c30 = report["candidates"].get("skip_er0.3", {})
    status = "FROZEN — ready for paper / small-size test auto-trade" if freeze else (
        "NOT FROZEN — dig incomplete; do not auto-trade"
    )
    lines = [
        "# H29 fade — TEST_AUTOTRADE spec",
        "",
        f"**Status:** {status}",
        "",
        "Source: EXP-0036_DIG4 (confirm DIG3 `skip_er0.3`). Yardstick = MAT vol-k=1 P&L.",
        "",
        "## Signal (causal)",
        "",
        "1. Detect ES+NQ **confirm** momentum bursts (same as MAT / EXP-0008 corrected):",
        "   100 ms tape bars, min_trades=6, min_vol=25, min_imbalance=0.55,",
        "   min_progress_ticks=1, sync_ms=1500, max_es_lag_ms=250, stamp=confirm",
        "   (enter at max(nq_ts, es_ts)).",
        "2. After the burst, find the first 100 ms bar where:",
        f"   - NQ still with-burst: side×nq_imb ≥ {p_ex.nq_min_imbalance}, "
        f"nq_vol ≥ {p_ex.nq_min_vol}, n_trades ≥ {p_ex.nq_min_trades}",
        f"   - ES against: side×es_imb ≤ {p_ex.es_imb_max} on a bar with es_vol > 0",
        "   - within 5 s of the burst stamp",
        "3. **Fade** the burst (side = −burst_side) at that bar's **close**.",
        "",
        "## Gates (frozen candidate)",
        "",
        "- Window: UK **14:30–17:00** only",
        "- **ER skip:** take the trade only if 60-min efficiency ratio "
        "`er60 = |net| / path` (path = Σ|Δclose| over completed minutes) on NQ ≤ **0.30** "
        "(same `RegimeSnapshot.efficiency_ratio` as `make_daytype_skip(max_er=0.3)` / "
        "MAT regime window 60)",
        "- **No** trend-with / VWAP-with (hurts the fade)",
        "- **No** short-only filter (report stratum only; do not encode yet)",
        "- Flat-only + signal cooldown **5 s**",
        "",
        "## Brackets / costs",
        "",
        "- TP = SL = clip(1.0 × median_30m_minute_range, 6, 30)  → **vol k=1**",
        "- Slip assumption in research: 1 tick (0.25 pt); dig also checked 2-tick",
        "- Commission in research: 0.25 pt/RT",
        "",
        "## DIG4 numbers (skip_er0.3, 1-tick)",
        "",
    ]
    if c30:
        conc = c30.get("concentration", {})
        lines += [
            f"- n={c30.get('n')} WR={c30.get('wr')} net={c30.get('net'):+.1f}",
            f"- train={c30.get('train_net'):+.1f} hold={c30.get('hold_net'):+.1f} "
            f"new={c30.get('new_net'):+.1f}",
            f"- days+={conc.get('days_pos')}/23 top3_share={conc.get('top3_share_of_pos')} "
            f"loo_neg={c30.get('loo_neg')}",
            f"- checks={c30.get('checks')}",
            "",
        ]
    slip2 = report.get("slip2_skip_er0.3") or {}
    if slip2:
        lines += [
            f"2-tick slip: n={slip2.get('n')} net={slip2.get('net'):+.1f} "
            f"train={slip2.get('train_net'):+.1f} hold={slip2.get('hold_net'):+.1f} "
            f"new={slip2.get('new_net'):+.1f} cleared={slip2.get('checks', {}).get('cleared')}",
            "",
        ]
    lines += [
        "## Test auto-trade rules (ops)",
        "",
        "- Size: **1 contract** (or smallest allowed) until ≥5 live sessions logged",
        "- Hours: UK 14:30–17:00 only; flatten by 17:00",
        "- Do **not** retune thresholds on these 23 research days",
        "- Log every signal (fired / skipped-by-ER) with er60, wait_ms, es_imb_x",
        "- Kill switch: stop if live net < −150 pts or 3 consecutive losing sessions",
        "- Compare live fills vs sim with 1- and 2-tick slip assumptions weekly",
        "",
        "## Implementation notes",
        "",
        "- Research runner: `detector: correlated_exhaust_fade_freeze` / "
        "`research.features.corr_exhaust.exhaust_fade_events`",
        "- Live: Bookmap add-on **Correlated Fade AutoTrade (ES+NQ)** "
        "(`bookmap-addon/.../trade/CorrFadeAutoTradeModule.java` over "
        "`core/CorrFadeDetector.java`), JAR `build/libs/bookmap-orderflow-cfade.jar`. "
        "Separate book from MAT; defaults = this spec; trading OFF by default.",
        "- Offline parity: detector + RegimeTracker replayed on the 23 sessions reproduce "
        "the research fade list 132/132 (stamp, side, es_imb_x, wait, close) and identical "
        "er60 / bracket at each fade.",
        "- Replay reference: `python -m research.export_cfade_ref --session <sid>` → "
        "`results/CFADE_REF_<sid>.md` (all candidates + ER verdict + expected trades).",
        "- ER uses the **same** 60-min completed-minute regime as "
        "`research.features.regime.lookup_regime(..., window_min=60)`; the add-on resets its "
        "regime tracker at UK 14:30 so the first hour matches the window-filtered research.",
        "- Not automated: flatten at 17:00 and the 3-losing-sessions stop (manual).",
        "",
        f"Sessions in freeze sample ({len(sids)}): {', '.join(sids)}",
        "",
    ]
    return lines
