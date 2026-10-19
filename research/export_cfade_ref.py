"""Export Correlated Fade (CFADE) reference signals for Bookmap replay comparison.

Usage:
  python -m research.export_cfade_ref --session 021026
  python -m research.export_cfade_ref --session 021026 --max-er 0.25

Writes results/CFADE_REF_<sid>.md / .csv with:
  - every exhaustion-fade candidate (what the add-on logs as ``CFADE FADE PASS|REJECT``)
    with er60 and the ER-gate verdict, and
  - the expected trades under the frozen rule (ER ≤ max_er, flat-only, 5 s cooldown,
    vol k=1 brackets, 0 slip) — what ``CFADE SIGNAL`` should show.

Frozen rule: results/EXP-0036_DIG4/TEST_AUTOTRADE_SPEC.md.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from research.backtest.mat_sim import (
    MatSimConfig,
    make_daytype_skip,
    make_vol_bracket,
    simulate_session,
)
from research.config import load_config
from research.correlated import harness as H
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events
from research.features.regime import build_regime_index, lookup_regime

UK = ZoneInfo("Europe/London")


def fmt(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(UK).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3] + " UK"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="021026")
    ap.add_argument("--max-er", type=float, default=0.30)
    ap.add_argument("--es-imb-max", type=float, default=-0.4)
    ap.add_argument("--k", type=float, default=1.0)
    args = ap.parse_args()
    sid = args.session
    cfg = load_config()
    pair = H.load_pair(Path(cfg["parquet_dir"]), sid)
    if pair is None:
        raise SystemExit(f"no NQ trades for {sid}")

    p_ex = ExhaustParams(
        es_imb_max=args.es_imb_max, es_min_vol=0, use_imb=True, use_vol=False,
        nq_min_imbalance=0.55, nq_min_vol=25, nq_min_trades=6,
    )
    nq_b, es_b = pair.bars(100)
    bursts = H.confirm_bursts(pair)
    fades = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=5.0)

    # Regime at each fade (completed minutes from UK 14:30, same as the add-on with
    # "Regime resets at window open").
    rows = []
    if fades.num_rows:
        ev_ts = fades.column("event_ts_ns").to_numpy().astype(np.int64)
        bars, bar_ts, cum_pv, cum_v, t_ts, t_px, t_sz = build_regime_index(pair.nq, pts_per_tick=0.25)
        kw = dict(vwap_from_ns=pair.vwap_from_ns, trade_ts=t_ts, trade_px=t_px, trade_sz=t_sz)
        sr = lookup_regime(bars, bar_ts, cum_pv, cum_v, ev_ts, window_min=60, **kw)
        sv = lookup_regime(bars, bar_ts, cum_pv, cum_v, ev_ts, window_min=30, **kw)
        for i in range(fades.num_rows):
            r = {k: fades.column(k)[i].as_py() for k in fades.column_names}
            brk = float(np.clip(args.k * max(sv[i].median_minute_range, 0.25), 6, 30))
            rows.append({
                **r,
                "px": r["nq_close_ticks"] * 0.25,
                "er60": sr[i].efficiency_ratio,
                "n60": sr[i].n_bars,
                "brkt": brk,
                "er_ok": sr[i].efficiency_ratio <= args.max_er,
            })

    sim_cfg = MatSimConfig(slip_ticks=0.0, signal_cooldown_sec=5.0)
    summary = simulate_session(
        sid, pair.nq, pair.es, sim_cfg,
        gate=make_daytype_skip(max_er=args.max_er),
        bracket_fn=make_vol_bracket(args.k),
        vwap_from_ns=pair.vwap_from_ns,
        events=fades if fades.num_rows else None,
    ) if fades.num_rows else None

    out_dir = Path(cfg["results_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"CFADE_REF_{sid}.md"
    csv_path = out_dir / f"CFADE_REF_{sid}.csv"

    lines = [
        f"# CFADE replay reference — session `{sid}`",
        "",
        "Compare Bookmap log lines `CFADE FADE PASS|REJECT` (every candidate) and "
        "`CFADE SIGNAL` (expected trades) to the tables below.",
        "",
        "## Settings (must match panel — defaults)",
        "",
        "- Burst: bar 100 ms, min trades 6, min vol 25, |imb| 0.55, progress 0.25 pts, cooldown 3",
        "- ES sync 1500 ms, Max ES lag 250 (confirm pairing, stamp = max(nq, es))",
        f"- Fade: max wait 5 s; NQ still imb ≥ 0.55 / vol ≥ 25 / trades ≥ 6; **ES against ≤ {args.es_imb_max}**",
        f"- **Max efficiency ratio = {args.max_er}** (regime 60 min, resets at window open)",
        "- Window UK 14:30–17:00; flat-only; signal cooldown 5 s; longs + shorts",
        f"- Brackets: vol k={args.k} clip 6–30 (TP = SL)",
        "",
        f"Confirm bursts: **{bursts.num_rows}** · fade candidates: **{len(rows)}** · "
        f"ER-pass: **{sum(1 for r in rows if r['er_ok'])}** · "
        f"expected trades (flat/cooldown): **{summary.n if summary else 0}**",
    ]
    if summary:
        lines.append(f"Sim gross (0 slip): **{summary.gross:+.1f}** WR={summary.wr:.2f}")
    lines += [
        "",
        "## All fade candidates (`CFADE FADE …`)",
        "",
        "| # | UK time | side | px | burst | wait_ms | es_imb_x | er60 | n60 | brkt | ER verdict | ts_ns |",
        "|--:|---|---|--:|--:|--:|--:|--:|--:|--:|---|--:|",
    ]
    csv = ["n,uk_time,side,px,burst_side,wait_ms,es_imb_x,nq_imb_x,er60,n60,brkt,er_ok,ts_ns"]
    for i, r in enumerate(rows, 1):
        side = "LONG" if r["side"] > 0 else "SHORT"
        verdict = "PASS" if r["er_ok"] else "REJECT regime_er_skip"
        lines.append(
            f"| {i} | {fmt(r['event_ts_ns'])} | {side} | {r['px']:.2f} | {r['burst_side']:+d} | "
            f"{r['wait_ms']:.0f} | {r['es_imb_x']:.2f} | {r['er60']:.3f} | {r['n60']} | "
            f"{r['brkt']:.1f} | {verdict} | {r['event_ts_ns']} |"
        )
        csv.append(
            f"{i},{fmt(r['event_ts_ns'])},{side},{r['px']:.2f},{r['burst_side']},{r['wait_ms']:.0f},"
            f"{r['es_imb_x']:.4f},{r['nq_imb_x']:.4f},{r['er60']:.6f},{r['n60']},{r['brkt']:.2f},"
            f"{int(r['er_ok'])},{r['event_ts_ns']}"
        )

    lines += [
        "",
        "## Expected trades (`CFADE SIGNAL` → order when trading enabled)",
        "",
        "| # | UK time | side | entry_px | tp=sl | er60 | pnl (0 slip) | exit | ts_ns |",
        "|--:|---|---|--:|--:|--:|--:|---|--:|",
    ]
    if summary:
        for i, tr in enumerate(summary.trades, 1):
            side = "LONG" if tr.side > 0 else "SHORT"
            lines.append(
                f"| {i} | {fmt(tr.entry_ts_ns)} | {side} | {tr.entry_px:.2f} | {tr.tp_pts:.1f} | "
                f"{tr.er60:.3f} | {tr.pnl:+.2f} | {tr.reason} | {tr.entry_ts_ns} |"
            )
    lines += [
        "",
        "## How to compare in Bookmap",
        "",
        "1. Install `bookmap-orderflow-cfade.jar`; add **Correlated Fade AutoTrade (ES+NQ)**.",
        "2. Open Replay for this session with **NQ + ES**; attach on NQ; **Enable trading = false**.",
        "3. Leave defaults (Verbose = true). After realtime start, grep the log for `CFADE FADE` / `CFADE SIGNAL`.",
        "4. Match `tsNs=` (exact bar end) + side + `es_imb_x` / `er60` to the tables above.",
        "   Candidates must match exactly; SIGNAL rows match the expected-trades table except where a",
        "   replay fill keeps you in a trade longer/shorter than the sim (flat-only / cooldown).",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    csv_path.write_text("\n".join(csv) + "\n", encoding="utf-8")
    print(f"wrote {md_path} ({len(rows)} candidates, {summary.n if summary else 0} expected trades)")
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    main()
