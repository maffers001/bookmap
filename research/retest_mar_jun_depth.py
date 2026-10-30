"""Retest March + June after depth parquet filled in.

Runs frozen CFADE, MAT075, and LST MOMENTUM_V1 / PC_V1 on clean Mar+Jun
sessions only. Depth is required for LST; CFADE/MAT are trades-path but
included for the same virgin-month batch.

  .venv\\Scripts\\python.exe -m research.retest_mar_jun_depth
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, merge_summaries, simulate_session
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.correlated.study_combined_book import _plot_equity
from research.mat_context.cache import build_cache
from research.mat_context.plot_0075_equity import _day_nets, _path
from research.mat_context.run_queue import _resim_masked
from research.momentum_slice.backtest_v1 import level_sl, load_rows, run_variant
from research.momentum_slice.holdout import build_features, fade_label, write_csv
from research.momentum_slice.rules import is_momentum_v1, is_pc_v1
from research.momentum_slice.scan import scan_session
from research.run_cfade_all_months import _sid_chrono_key

OUT = Path("results/RETEST_MAR_JUN_DEPTH")
MAT_ARM = "rot_fresh_and_vwap_near"
COMM = F.COMMISSION
MONTHS = ("03", "06")
MONTH_NAMES = {"03": "Mar", "06": "Jun"}

# Frozen structures from RULE_SPEC_V1 / train freeze
MOM_VARIANT = dict(
    entry="confirm", fade=False, tp=16.0, sl_mode="level",
    fixed_sl=10.0, time_stop=60.0, trail=0.0,
)
PC_VARIANT = dict(
    entry="confirm_plus_5s", fade=True, tp=10.0, sl_mode="level",
    fixed_sl=10.0, time_stop=60.0, trail=0.0,
)


def _month(sid: str) -> str:
    return sid[2:4]


def _target_sessions(pq: Path) -> list[str]:
    return sorted(
        [s for s in F.clean_sessions(pq) if _month(s) in MONTHS],
        key=_sid_chrono_key,
    )


def _stats_trades(trades, sessions: list[str]) -> dict:
    day: dict[str, float] = defaultdict(float)
    for t in trades:
        day[t.session_id] += t.pnl - COMM
    path = _path(dict(day), sessions)
    n = len(trades)
    wins = sum(1 for t in trades if (t.pnl - COMM) > 0)
    pe = path[-1]["equity"] if path else 0.0
    peak = dd = 0.0
    for r in path:
        peak = max(peak, r["equity"])
        dd = max(dd, peak - r["equity"])
    by_m = {}
    for m, label in MONTH_NAMES.items():
        sids = [s for s in sessions if _month(s) == m]
        tr = [t for t in trades if t.session_id in set(sids)]
        nw = sum(1 for t in tr if (t.pnl - COMM) > 0)
        by_m[m] = {
            "name": label,
            "days": len(sids),
            "n": len(tr),
            "wr": round(nw / len(tr), 3) if tr else None,
            "net": round(sum(day.get(s, 0.0) for s in sids), 2),
            "days_pos": sum(1 for s in sids if day.get(s, 0.0) > 0),
        }
    return {
        "n": n,
        "wr": round(wins / n, 3) if n else None,
        "net": round(pe, 2),
        "max_dd": round(dd, 2),
        "days_pos": sum(1 for s in sessions if day.get(s, 0.0) > 0),
        "days": len(sessions),
        "path": path,
        "by_month": by_m,
        "day": dict(day),
    }


def _sim_dict_to_day(res: dict, rows: list[dict]) -> dict[str, float]:
    """Approximate day nets from variant result is hard; re-sim for equity."""
    return {}


def _run_lst_variant_with_trades(rows: list[dict], **kw):
    """Like run_variant but also return TradeResult list for equity."""
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["session"], []).append(r)
    sim_cfg = MatSimConfig(
        tp_pts=kw["tp"], sl_pts=kw["fixed_sl"], commission_pts_rt=COMM,
        signal_cooldown_sec=30, time_stop_sec=kw["time_stop"], trail_pts=kw["trail"],
    )
    all_trades = []
    parts = []
    for sid, evs in sorted(by.items()):
        pair = H.load_pair(pq_dir, sid)
        if pair is None:
            continue
        ts, side, sls, tps = [], [], [], []
        for r in evs:
            if kw["entry"] == "confirm":
                t = int(float(r["confirm_ns"]))
            else:
                t = int(float(r["confirm_ns"])) + 5_000_000_000
            s = int(float(r["side_int"]))
            if kw["fade"]:
                s = -s
            ts.append(t)
            side.append(s)
            if kw["sl_mode"] == "level":
                sls.append(level_sl(r, kw["fixed_sl"]))
            else:
                sls.append(kw["fixed_sl"])
            tps.append(kw["tp"])
        if not ts:
            continue
        tbl = pa.table({
            "event_ts_ns": np.array(ts, dtype=np.int64),
            "side": np.array(side, dtype=np.int8),
            "sl_pts": np.array(sls, dtype=np.float64),
            "tp_pts": np.array(tps, dtype=np.float64),
        })
        s = simulate_session(
            sid, pair.nq, pair.es, sim_cfg, events=tbl, vwap_from_ns=pair.bounds[0]
        )
        parts.append(s)
        all_trades.extend(s.trades)
    merged = merge_summaries(parts, COMM) if parts else None
    return merged, all_trades


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    OUT.mkdir(parents=True, exist_ok=True)
    sessions = _target_sessions(pq)
    print(f"Target Mar+Jun clean sessions: {len(sessions)}", flush=True)
    print(f"  Mar={[s for s in sessions if _month(s)=='03']}", flush=True)
    print(f"  Jun={[s for s in sessions if _month(s)=='06']}", flush=True)
    print(f"  excl={sorted(F.EXCLUDE)}", flush=True)

    # Depth gate check
    missing_dep = [
        s for s in sessions
        if not (pq / f"session_id={s}" / "depth.parquet").exists()
        or (pq / f"session_id={s}" / "depth.parquet").stat().st_size == 0
    ]
    if missing_dep:
        print(f"WARNING missing depth parquet: {missing_dep}", flush=True)
    else:
        print("All target sessions have depth.parquet.", flush=True)

    # --- LST scan + features ---
    print("\n=== LST scan + features (needs depth) ===", flush=True)
    all_cand, all_feat = [], []
    for i, sid in enumerate(sessions, 1):
        print(f"[{i}/{len(sessions)}] {sid}", flush=True)
        try:
            cands = scan_session(cfg, sid)
            feats, _ = build_features(cfg, sid, cands) if cands else ([], [])
            all_cand += cands
            all_feat += feats
            print(
                f"  cand={len(cands)} feat={len(feats)} "
                f"mom={sum(int(r.get('momentum_v1') or 0) for r in feats)} "
                f"pc={sum(int(r.get('pc_v1') or 0) for r in feats)}",
                flush=True,
            )
        except Exception as e:
            print(f"  ERROR {type(e).__name__}: {e}", flush=True)
    if all_feat:
        fade_label(cfg, all_feat)
    write_csv(OUT / "lst_candidates.csv", all_cand)
    write_csv(OUT / "lst_features.csv", all_feat)

    # enrich for rules
    from research.momentum_slice.rules import enrich
    feat_rows = [enrich(r) for r in all_feat]
    mom_rows = [r for r in feat_rows if is_momentum_v1(r)]
    pc_rows = [r for r in feat_rows if is_pc_v1(r)]
    print(f"LST fires: MOMENTUM_V1={len(mom_rows)} PC_V1={len(pc_rows)}", flush=True)

    print("Sim MOMENTUM_V1 mom_confirm_levelSL_tp16_t60…", flush=True)
    mom_sum, mom_trades = _run_lst_variant_with_trades(mom_rows, **MOM_VARIANT)
    print("Sim PC_V1 pc_fade5_levelSL_tp10_t60…", flush=True)
    pc_sum, pc_trades = _run_lst_variant_with_trades(pc_rows, **PC_VARIANT)

    lst_stats = {
        "momentum_v1": _stats_trades(mom_trades, sessions),
        "pc_v1": _stats_trades(pc_trades, sessions),
    }
    if mom_sum:
        lst_stats["momentum_v1"]["sim"] = mom_sum.to_dict()
    if pc_sum:
        lst_stats["pc_v1"]["sim"] = pc_sum.to_dict()

    # --- CFADE + MAT075 ---
    print("\n=== CFADE + MAT075 ===", flush=True)
    print("Building MAT cache (full sample for gates/priors)…", flush=True)
    cache = build_cache(verbose=False)
    # Restrict to target sessions present in cache
    sess_set = set(sessions)
    mat_sims = _resim_masked(cache, MAT_ARM)
    mat_trades = [
        t for sid, s in mat_sims.items() if sid in sess_set for t in s.trades
    ]
    cfade_trades = []
    for sid in sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            print(f"  {sid}: no pair", flush=True)
            continue
        s = F.simulate_cfade(pair)
        cfade_trades.extend(s.trades)
        print(f"  {sid}: cfade={s.n} net={s.net:+.1f}", flush=True)

    cfade_st = _stats_trades(cfade_trades, sessions)
    mat_st = _stats_trades(mat_trades, sessions)

    books = {
        "cfade": (cfade_st, "CFADE frozen — Mar+Jun"),
        "mat075": (mat_st, "MAT075 rot_fresh∩vwap_near — Mar+Jun"),
        "momentum_v1": (lst_stats["momentum_v1"], "LST MOMENTUM_V1 levelSL/tp16/t60 — Mar+Jun"),
        "pc_v1": (lst_stats["pc_v1"], "LST PC_V1 fade+5s levelSL/tp10/t60 — Mar+Jun"),
    }

    for key, (st, title) in books.items():
        _plot_equity(st["path"], OUT / f"equity_{key}.svg", title)
        for m, label in MONTH_NAMES.items():
            sids = [s for s in sessions if _month(s) == m]
            day = {s: st["day"].get(s, 0.0) for s in sids}
            _plot_equity(_path(day, sids), OUT / f"equity_{key}_{label.lower()}.svg",
                         f"{title} ({label} only)")
        print(
            f"{key}: n={st['n']} wr={st['wr']} net={st['net']:+.1f} maxDD={st['max_dd']:.1f}",
            flush=True,
        )

    # Overlay CFADE vs MAT075 vs Mom
    from research.mat_context.plot_0075_equity import _overlay
    _overlay(
        {
            "cfade": books["cfade"][0]["path"],
            "mat075": books["mat075"][0]["path"],
            "momentum_v1": books["momentum_v1"][0]["path"],
            "pc_v1": books["pc_v1"][0]["path"],
        },
        OUT / "equity_overlay.svg",
        "Mar+Jun depth retest — CFADE / MAT075 / LST Mom / PC",
    )

    # daily csv
    with (OUT / "daily.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["session", "month"] + [f"{k}_net" for k in books] + [f"{k}_eq" for k in books]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, sid in enumerate(sessions):
            row = {"session": sid, "month": _month(sid)}
            for k, (st, _) in books.items():
                row[f"{k}_net"] = st["path"][i]["day_net"]
                row[f"{k}_eq"] = st["path"][i]["equity"]
            w.writerow(row)

    # summary md
    md = [
        "# Retest — March + June (depth filled)",
        "",
        f"Clean Mar+Jun sessions: **{len(sessions)}** "
        f"(Mar {sum(1 for s in sessions if _month(s)=='03')}, "
        f"Jun {sum(1 for s in sessions if _month(s)=='06')}; "
        f"excl `{sorted(F.EXCLUDE)}`).",
        "",
        "Depth parquet is now present for these months (LST needs it; "
        "CFADE/MAT are trades-path). Frozen specs only — no retune.",
        "",
        "## Headline (Mar+Jun combined)",
        "",
        "| book | n | WR | net pts | maxDD | days+ |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for k, (st, _) in books.items():
        md.append(
            f"| {k} | {st['n']} | {st['wr']} | {st['net']:+.1f} | "
            f"{st['max_dd']:.1f} | {st['days_pos']}/{st['days']} |"
        )
    md += [
        "",
        "## By month",
        "",
        "| month | days | CFADE n/WR/net | MAT075 n/WR/net | Mom n/WR/net | PC n/WR/net |",
        "|---|---:|---|---|---|---|",
    ]
    for m, label in MONTH_NAMES.items():
        cells = []
        for k, (st, _) in books.items():
            bm = st["by_month"][m]
            cells.append(f"{bm['n']}/{bm['wr']}/{bm['net']:+.1f}")
        md.append(
            f"| {label} | {books['cfade'][0]['by_month'][m]['days']} | "
            + " | ".join(cells) + " |"
        )
    md += [
        "",
        "### LST notes",
        f"- Candidates scanned: {len(all_cand)}; feature rows: {len(all_feat)}",
        f"- MOMENTUM_V1 fires: {len(mom_rows)}; PC_V1 fires: {len(pc_rows)}",
        "- Mom structure: `mom_confirm_levelSL_tp16_t60`",
        "- PC structure: `pc_fade5_levelSL_tp10_t60`",
        "",
        "## Charts",
        "",
        "![overlay](equity_overlay.svg)",
        "",
    ]
    for k in books:
        md += [f"![{k}](equity_{k}.svg)", ""]
    (OUT / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (OUT / "report.json").write_text(
        json.dumps({
            "sessions": sessions,
            "missing_depth": missing_dep,
            "lst_candidates": len(all_cand),
            "lst_features": len(all_feat),
            "mom_fires": len(mom_rows),
            "pc_fires": len(pc_rows),
            "books": {
                k: {kk: vv for kk, vv in st.items() if kk not in ("path", "day")}
                for k, (st, _) in books.items()
            },
        }, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {OUT / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
