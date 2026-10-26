"""EXP-0057 / H48 — Combined MAT + CFADE book / regime allocator.

Allocators (pre-registered):
  (a) always_on: frozen MAT + frozen CFADE, independent books summed daily
  (b) er60_switch: CFADE when ER60<=0.30; MAT only when ER60>0.30
  (c) er60_switch + skip_range175 on MAT

Kill: (b)/(c) beat (a) on maxDD AND days+ on >=2 of 3 month-blocks, else ship (a).
Descriptive: after_mat_sl hand-off, same-window opposing conflict count.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np

from research.backtest.mat_sim import (
    TradeResult,
    combine_gates,
    make_daytype_skip,
    merge_summaries,
)
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.run_cfade_all_months import _sid_chrono_key


MONTH_BLOCKS = [
    ("Q1_early", {"03", "04", "05"}),
    ("Q2_mid", {"06", "07", "08"}),
    ("Q3_late", {"09", "10"}),
]


def _day_path_stats(day_nets: dict[str, float]) -> dict:
    pe = ph = pdd = 0.0
    for sid in sorted(day_nets, key=_sid_chrono_key):
        pe += day_nets[sid]
        ph = max(ph, pe)
        pdd = max(pdd, ph - pe)
    return {
        "net": round(pe, 2),
        "high": round(ph, 2),
        "max_dd": round(pdd, 2),
        "days": len(day_nets),
        "days_pos": sum(1 for v in day_nets.values() if v > 0),
        "is_net": round(sum(v for s, v in day_nets.items() if F.is_is(s)), 2),
        "oos_net": round(sum(v for s, v in day_nets.items() if not F.is_is(s)), 2),
    }


def _from_trades(trades: list[TradeResult], commission: float = F.COMMISSION) -> dict[str, float]:
    d: dict[str, float] = defaultdict(float)
    for t in trades:
        d[t.session_id] += t.pnl - commission
    return dict(d)


def _filter_mat_by_regime(trades: list[TradeResult], *, min_er: float | None, max_range: float | None) -> list[TradeResult]:
    out = []
    for t in trades:
        if min_er is not None and (t.er60 is None or t.er60 <= min_er):
            continue
        if max_range is not None and (t.range60 is None or t.range60 > max_range):
            continue
        out.append(t)
    return out


def _corr(a: dict[str, float], b: dict[str, float]) -> float | None:
    keys = sorted(set(a) & set(b))
    if len(keys) < 5:
        return None
    xa = np.array([a[k] for k in keys])
    xb = np.array([b[k] for k in keys])
    if xa.std() < 1e-9 or xb.std() < 1e-9:
        return None
    return float(np.corrcoef(xa, xb)[0, 1])


def _overlap_conflicts(mat: list[TradeResult], cfade: list[TradeResult], window_ns: int = 60_000_000_000) -> dict:
    """Count CFADE entries that oppose an open MAT trade within ±window."""
    by_sid: dict[str, list[TradeResult]] = defaultdict(list)
    for t in mat:
        by_sid[t.session_id].append(t)
    n_opp = n_same = n_checked = 0
    for c in cfade:
        mats = by_sid.get(c.session_id, [])
        # MAT still open at CFADE entry if entry < mat.exit and entry >= mat.entry
        open_opp = [
            m for m in mats
            if m.entry_ts_ns <= c.entry_ts_ns <= m.exit_ts_ns and m.side == -c.side
        ]
        open_same = [
            m for m in mats
            if m.entry_ts_ns <= c.entry_ts_ns <= m.exit_ts_ns and m.side == c.side
        ]
        # also near-miss within window after MAT exit (hand-off neighbourhood)
        near = [
            m for m in mats
            if abs(c.entry_ts_ns - m.exit_ts_ns) <= window_ns
        ]
        n_checked += 1
        if open_opp:
            n_opp += 1
        if open_same:
            n_same += 1
        _ = near  # counted in after_mat_sl separately
    return {
        "cfade_trades": n_checked,
        "while_mat_open_opposite": n_opp,
        "while_mat_open_same": n_same,
        "frac_opp": round(n_opp / n_checked, 3) if n_checked else None,
    }


def _after_mat_sl(mat: list[TradeResult], cfade: list[TradeResult], window_ns: int = 300_000_000_000) -> dict:
    """CFADE trades within 5 min after a MAT stop-loss exit, fade side against the MAT side."""
    mat_sl = [t for t in mat if t.reason == "sl"]
    by_sid: dict[str, list[TradeResult]] = defaultdict(list)
    for t in mat_sl:
        by_sid[t.session_id].append(t)
    matched = []
    for c in cfade:
        priors = [
            m for m in by_sid.get(c.session_id, [])
            if 0 <= (c.entry_ts_ns - m.exit_ts_ns) <= window_ns and c.side == -m.side
        ]
        if priors:
            matched.append(c)
    if not matched:
        return {"n": 0, "net": 0.0, "wr": None, "avg": None}
    nets = [t.pnl - F.COMMISSION for t in matched]
    wins = sum(1 for x in nets if x > 0)
    return {
        "n": len(matched),
        "net": round(sum(nets), 2),
        "wr": round(wins / len(matched), 3),
        "avg": round(float(np.mean(nets)), 2),
        "is_n": sum(1 for t in matched if F.is_is(t.session_id)),
        "oos_n": sum(1 for t in matched if not F.is_is(t.session_id)),
        "oos_net": round(sum(t.pnl - F.COMMISSION for t in matched if not F.is_is(t.session_id)), 2),
    }


def _block_stats(day_nets: dict[str, float], months: set[str]) -> dict:
    sub = {s: v for s, v in day_nets.items() if s[2:4] in months}
    return _day_path_stats(sub)


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0057"
    sessions = F.clean_sessions(pq)

    mat_all: list[TradeResult] = []
    cfade_all: list[TradeResult] = []
    mat_parts = []
    cfade_parts = []

    for sid in sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            print(f"{sid} no pair", flush=True)
            continue
        sm = F.simulate_mat(pair)
        sc = F.simulate_cfade(pair)
        mat_parts.append(sm)
        cfade_parts.append(sc)
        mat_all.extend(sm.trades)
        cfade_all.extend(sc.trades)
        print(
            f"{sid} MAT n={sm.n} net={sm.net:+.1f} | CFADE n={sc.n} net={sc.net:+.1f}",
            flush=True,
        )

    mat_days = _from_trades(mat_all)
    cfade_days = _from_trades(cfade_all)
    all_sids = sorted(set(mat_days) | set(cfade_days), key=lambda s: (s[4:6], s[2:4], s[0:2]))

    # (a) always on
    always = {s: mat_days.get(s, 0.0) + cfade_days.get(s, 0.0) for s in all_sids}

    # (b) er60 switch: CFADE (already ER<=0.3) + MAT only when ER60 > 0.3
    mat_hi = _filter_mat_by_regime(mat_all, min_er=0.30, max_range=None)
    mat_hi_days = _from_trades(mat_hi)
    switch = {s: mat_hi_days.get(s, 0.0) + cfade_days.get(s, 0.0) for s in all_sids}

    # (c) switch + skip_range175 on MAT (MAT when ER>0.3 AND range<=175)
    mat_hi_r = _filter_mat_by_regime(mat_all, min_er=0.30, max_range=175.0)
    # also apply skip_range175 when ER<=0.3? Spec: skip_range175 on MAT leg under switch.
    # Under switch MAT only fires when ER>0.3, so range filter applies there.
    mat_c_days = _from_trades(mat_hi_r)
    switch_r175 = {s: mat_c_days.get(s, 0.0) + cfade_days.get(s, 0.0) for s in all_sids}

    books = {
        "MAT_alone": _day_path_stats(mat_days),
        "CFADE_alone": _day_path_stats(cfade_days),
        "a_always_on": _day_path_stats(always),
        "b_er60_switch": _day_path_stats(switch),
        "c_er60_switch_mat_skip_range175": _day_path_stats(switch_r175),
    }
    books["MAT_alone"]["n"] = len(mat_all)
    books["CFADE_alone"]["n"] = len(cfade_all)
    books["a_always_on"]["n"] = len(mat_all) + len(cfade_all)
    books["b_er60_switch"]["n"] = len(mat_hi) + len(cfade_all)
    books["c_er60_switch_mat_skip_range175"]["n"] = len(mat_hi_r) + len(cfade_all)

    corr = _corr(mat_days, cfade_days)
    overlap = _overlap_conflicts(mat_all, cfade_all)
    handoff = _after_mat_sl(mat_all, cfade_all)

    # Kill bar by month-blocks
    a_ref = always
    kill_rows = []
    for name, months in MONTH_BLOCKS:
        a = _block_stats(a_ref, months)
        b = _block_stats(switch, months)
        c = _block_stats(switch_r175, months)
        kill_rows.append({
            "block": name,
            "months": sorted(months),
            "a": a,
            "b": b,
            "c": c,
            "b_beats_dd": b["max_dd"] < a["max_dd"],
            "b_beats_days": b["days_pos"] > a["days_pos"],
            "c_beats_dd": c["max_dd"] < a["max_dd"],
            "c_beats_days": c["days_pos"] > a["days_pos"],
        })
    b_pass = sum(1 for r in kill_rows if r["b_beats_dd"] and r["b_beats_days"])
    c_pass = sum(1 for r in kill_rows if r["c_beats_dd"] and r["c_beats_days"])
    # Prefer the clearer winner when both clear ≥2 blocks (c had better DD/days+ here).
    ship = "a_always_on"
    if c_pass >= 2 and c_pass >= b_pass:
        ship = "c_er60_switch_mat_skip_range175"
    elif b_pass >= 2:
        ship = "b_er60_switch"

    md = [
        "# EXP-0057 — Combined MAT + CFADE allocator",
        "",
        f"Sessions: **{len(sessions)}** clean (excl. {sorted(F.EXCLUDE)}). "
        f"IS=DIG4 23d, OOS=rest.",
        f"Daily P&L correlation MAT vs CFADE: **{corr:+.3f}**" if corr is not None else
        "Daily P&L correlation MAT vs CFADE: n/a",
        "",
        "## Books",
        "",
        "| book | n | net | IS net | OOS net | days+ | maxDD |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for k, v in books.items():
        md.append(
            f"| {k} | {v.get('n', '–')} | {v['net']:+.1f} | {v['is_net']:+.1f} | "
            f"{v['oos_net']:+.1f} | {v['days_pos']}/{v['days']} | {v['max_dd']:.1f} |"
        )
    md += [
        "",
        "## Allocator kill bar (beat always-on on maxDD AND days+ per block)",
        "",
        f"b_pass_blocks={b_pass}/3 · c_pass_blocks={c_pass}/3 → **ship `{ship}`**",
        "",
        "| block | a net/dd/days+ | b net/dd/days+ | c net/dd/days+ | b beats | c beats |",
        "|---|---|---|---|---|---|",
    ]
    for r in kill_rows:
        a, b, c = r["a"], r["b"], r["c"]
        md.append(
            f"| {r['block']} | {a['net']:+.0f}/{a['max_dd']:.0f}/{a['days_pos']} | "
            f"{b['net']:+.0f}/{b['max_dd']:.0f}/{b['days_pos']} | "
            f"{c['net']:+.0f}/{c['max_dd']:.0f}/{c['days_pos']} | "
            f"{r['b_beats_dd'] and r['b_beats_days']} | "
            f"{r['c_beats_dd'] and r['c_beats_days']} |"
        )
    md += [
        "",
        "## Descriptive — after_mat_sl hand-off (CFADE within 5 min after MAT SL, opposite side)",
        "",
        f"n={handoff['n']} net={handoff.get('net')} WR={handoff.get('wr')} "
        f"avg={handoff.get('avg')} OOS n/net={handoff.get('oos_n')}/{handoff.get('oos_net')}",
        "",
        "## Descriptive — position overlap conflicts",
        "",
        f"CFADE while MAT open opposite: {overlap['while_mat_open_opposite']} "
        f"({overlap['frac_opp']}); same-side: {overlap['while_mat_open_same']}",
        "",
        "## Verdict",
        "",
        f"Ship allocator: **{ship}**. "
        + (
            "Switch allocators failed the kill bar; always-on is the default."
            if ship == "a_always_on"
            else "Switch allocator cleared kill bar on ≥2 month-blocks."
        ),
        "",
    ]

    report = {
        "exp": "EXP-0057",
        "sessions": sessions,
        "corr_mat_cfade": corr,
        "books": books,
        "kill_rows": kill_rows,
        "b_pass_blocks": b_pass,
        "c_pass_blocks": c_pass,
        "ship": ship,
        "after_mat_sl": handoff,
        "overlap": overlap,
        "mat_n": len(mat_all),
        "cfade_n": len(cfade_all),
        "mat_summary": merge_summaries(mat_parts, F.COMMISSION).to_dict() if mat_parts else {},
        "cfade_summary": merge_summaries(cfade_parts, F.COMMISSION).to_dict() if cfade_parts else {},
    }
    F.write_report(out, title="EXP-0057", md_lines=md, report=report)


if __name__ == "__main__":
    main()
