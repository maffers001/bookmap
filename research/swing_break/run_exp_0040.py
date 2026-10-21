"""EXP-0040 — MAT bounce at significant S/R.

Primary A: stratify frozen MAT by bounce/at/none (long@LOW, short@HIGH).
Secondary B: touch-first + MAT causal confirm.
May 2026 held out. Writes results/EXP-0040/.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    TradeResult,
    combine_gates,
    filter_summary,
    make_trend_mode,
    make_vol_bracket,
    make_vol_bracket_rr,
    make_vwap_side,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.correlated.harness import _prefer_single_feed
from research.features.momentum import momentum_burst_events, trades_to_tape_bars
from research.features.significant_swing import (
    SigSwingConfig,
    filter_hold_side_buffer,
    label_signal_sr,
    mat_confirm_breaks,
    prepare_session_levels,
    touch_events,
)
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25
BUFFERS = (0.0, 2.5, 5.0)
WINDOWS = (30, 60, 120, 300)
PRIMARY_WIN = 60
MAY = {
    "010526", "040526", "050526", "070526", "080526",
    "110526", "120526", "130526", "140526", "150526",
    "180526", "190526", "200526", "210526", "260526",
    "280526", "290526",
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


@dataclass
class LabeledTrade:
    tr: TradeResult
    state: str
    swing_px: float
    cluster_n: int
    pullaway_pts: float
    secs_since_touch: float
    bounce_window_sec: float
    buffer_satisfied: tuple[float, ...]
    level_mode: str


def pack_trades(trades: list[TradeResult], commission: float = COMMISSION) -> dict:
    s = SimSummary()
    equity = peak = 0.0
    for tr in trades:
        s.trades.append(tr)
        s.n += 1
        s.gross += tr.pnl
        s.commission_total += commission
        net = tr.pnl - commission
        s.net += net
        if tr.win:
            s.wins += 1
        if tr.pnl > 0:
            s.gross_win += tr.pnl
        else:
            s.gross_loss += abs(tr.pnl)
        equity += net
        peak = max(peak, equity)
        s.max_dd = max(s.max_dd, peak - equity)
    d = s.to_dict()
    d["avg"] = round(s.net / s.n, 3) if s.n else None
    days = {tr.session_id for tr in trades}
    day_nets = [
        sum(tr.pnl - commission for tr in trades if tr.session_id == sid)
        for sid in days
    ]
    d["days"] = len(days)
    d["days_pos"] = sum(1 for x in day_nets if x > 0)
    d["worst_day"] = round(min(day_nets), 2) if day_nets else None
    d["loss_tail_n"] = sum(1 for tr in trades if tr.pnl <= -20)
    d["loss_tail_sum"] = round(sum(tr.pnl for tr in trades if tr.pnl <= -20), 2)
    return d


def cohort_split(trades: list[TradeResult]) -> tuple[list[TradeResult], list[TradeResult]]:
    train = [t for t in trades if t.session_id not in MAY]
    may = [t for t in trades if t.session_id in MAY]
    return train, may


def load_session(pq: Path, sid: str) -> tuple[pa.Table, pa.Table, int] | None:
    nq0 = _load_trades(pq, sid, "NQ")
    es0 = _load_trades(pq, sid, "ES")
    nq0 = _prefer_single_feed(nq0, pq, sid, "NQ")
    es0 = _prefer_single_feed(es0, pq, sid, "ES")
    nq = filt(nq0)
    es = filt(es0) if es0.num_rows else es0
    if nq.num_rows == 0:
        return None
    mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
    return nq, es, bounds(mid)[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--no-gates",
        action="store_true",
        help="Drop trend+VWAP (S/R paradigm; writes EXP-0040_NOGATE)",
    )
    args = ap.parse_args()
    use_gates = not args.no_gates

    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    exp_tag = "EXP-0040" if use_gates else "EXP-0040_NOGATE"
    out = Path(cfg["results_dir"]) / exp_tag
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name.replace("session_id=", "")
        for p in pq.iterdir()
        if p.is_dir() and p.name.startswith("session_id=")
        and (p / "trades.parquet").exists()
        and "smoke" not in p.name
    )
    print(f"sessions with parquet: {len(sessions)} gates={'trend+VWAP' if use_gates else 'NONE'}", flush=True)

    gate = (
        combine_gates(make_trend_mode("with"), make_vwap_side(True))
        if use_gates
        else None
    )
    br_sym = make_vol_bracket(1.0)
    sim_cfg = MatSimConfig(
        slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal",
    )
    swing_cfg = SigSwingConfig(lookback_sec=600.0, pullaway_pts=20.0, cluster_tol_pts=5.0)

    all_base: list[TradeResult] = []
    session_levels: dict[str, dict] = {}
    sanity = []

    for sid in sessions:
        loaded = load_session(pq, sid)
        if loaded is None:
            continue
        nq, es, vf = loaded
        summary = simulate_session(
            sid, nq, es, sim_cfg, gate=gate, bracket_fn=br_sym, vwap_from_ns=vf,
        )
        all_base.extend(summary.trades)

        piv_s, lev_s = prepare_session_levels(nq, swing_cfg, use_cluster=False)
        piv_c, lev_c = prepare_session_levels(nq, swing_cfg, use_cluster=True)
        n_touch_s = sum(
            1 for lv in lev_s
            if lv.first_touch_ts_ns is not None
            and (lv.first_break_ts_ns is None or lv.first_touch_ts_ns < lv.first_break_ts_ns)
        )
        session_levels[sid] = {
            "nq": nq, "es": es, "vf": vf,
            "levels_single": lev_s, "levels_clustered": lev_c,
        }
        sanity.append({
            "session": sid,
            "cohort": "may" if sid in MAY else "train",
            "mat_n": summary.n,
            "pivots_single": len(piv_s),
            "pivots_clustered": len(piv_c),
            "levels_single": len(lev_s),
            "levels_clustered": len(lev_c),
            "touches_single": n_touch_s,
            "breaks_single": sum(1 for lv in lev_s if lv.first_break_ts_ns is not None),
        })
        print(
            f"{sid}: mat={summary.n} piv_s={len(piv_s)} touch_s={n_touch_s}",
            flush=True,
        )

    base_train, base_may = cohort_split(all_base)
    report: dict = {
        "base": {
            "all": pack_trades(all_base),
            "train": pack_trades(base_train),
            "may": pack_trades(base_may),
        },
        "sanity": sanity,
    }

    def label_all(window: int, level_mode: str) -> list[LabeledTrade]:
        key = "levels_single" if level_mode == "single" else "levels_clustered"
        out_l: list[LabeledTrade] = []
        for tr in all_base:
            levels = session_levels[tr.session_id][key]
            lab = label_signal_sr(
                tr.side, tr.entry_ts_ns, tr.entry_px, levels,
                bounce_window_sec=float(window), buffers=BUFFERS,
            )
            out_l.append(LabeledTrade(
                tr=tr, state=lab.state, swing_px=lab.swing_px,
                cluster_n=lab.cluster_n, pullaway_pts=lab.pullaway_pts,
                secs_since_touch=lab.secs_since_touch,
                bounce_window_sec=lab.bounce_window_sec,
                buffer_satisfied=lab.buffer_satisfied,
                level_mode=level_mode,
            ))
        return out_l

    def stratum_trades(
        labeled: list[LabeledTrade],
        *,
        bounce_buf: float | None = None,
        state: str | None = None,
        side: int | None = None,
    ) -> list[TradeResult]:
        rows = []
        for lb in labeled:
            if side is not None and lb.tr.side != side:
                continue
            if bounce_buf is not None:
                if bounce_buf not in lb.buffer_satisfied:
                    continue
            elif state is not None:
                if lb.state != state:
                    continue
            rows.append(lb.tr)
        return rows

    strata_tables: dict = {}
    labeled_60_single = label_all(PRIMARY_WIN, "single")
    labeled_60_clust = label_all(PRIMARY_WIN, "clustered")

    def add_stratum(name: str, trades: list[TradeResult]) -> None:
        trn, my = cohort_split(trades)
        strata_tables[name] = {
            "all": pack_trades(trades),
            "train": pack_trades(trn),
            "may": pack_trades(my),
        }

    add_stratum("base", all_base)
    for mode, labeled in (("single", labeled_60_single), ("clustered", labeled_60_clust)):
        for buf in BUFFERS:
            for side_name, side in (("long", 1), ("short", -1), ("both", None)):
                if mode == "single":
                    tr = stratum_trades(labeled, bounce_buf=buf, side=side)
                else:
                    tr = [
                        lb.tr for lb in labeled
                        if (side is None or lb.tr.side == side)
                        and buf in lb.buffer_satisfied
                        and lb.cluster_n >= 2
                    ]
                add_stratum(f"w{PRIMARY_WIN}_{mode}_bounce_{buf:g}_{side_name}", tr)
                if mode == "clustered":
                    tr3 = [
                        lb.tr for lb in labeled
                        if (side is None or lb.tr.side == side)
                        and buf in lb.buffer_satisfied
                        and lb.cluster_n >= 3
                    ]
                    add_stratum(f"w{PRIMARY_WIN}_cluster3_bounce_{buf:g}_{side_name}", tr3)

        for state in ("at", "none"):
            for side_name, side in (("long", 1), ("short", -1), ("both", None)):
                if mode == "single":
                    tr = stratum_trades(labeled, state=state, side=side)
                else:
                    tr = [
                        lb.tr for lb in labeled
                        if (side is None or lb.tr.side == side)
                        and lb.state == state and lb.cluster_n >= 2
                    ]
                add_stratum(f"w{PRIMARY_WIN}_{mode}_{state}_{side_name}", tr)

    window_table = {}
    for w in WINDOWS:
        lab = label_all(w, "single")
        tr = stratum_trades(lab, bounce_buf=5.0, side=None)
        trn, my = cohort_split(tr)
        none_tr = stratum_trades(lab, state="none", side=None)
        trn_n, my_n = cohort_split(none_tr)
        window_table[f"w{w}_single_bounce_5"] = {
            "all": pack_trades(tr), "train": pack_trades(trn), "may": pack_trades(my),
        }
        window_table[f"w{w}_single_none"] = {
            "all": pack_trades(none_tr),
            "train": pack_trades(trn_n),
            "may": pack_trades(my_n),
        }
    report["strata_60"] = strata_tables
    report["window_sweep"] = window_table

    def events_from_labeled(labeled: list[LabeledTrade], bounce_buf: float) -> dict[str, pa.Table]:
        by_sid: dict[str, list[LabeledTrade]] = {}
        for lb in labeled:
            if bounce_buf not in lb.buffer_satisfied:
                continue
            by_sid.setdefault(lb.tr.session_id, []).append(lb)
        return {
            sid: pa.table({
                "event_ts_ns": np.asarray([lb.tr.entry_ts_ns for lb in lbs], dtype=np.int64),
                "side": np.asarray([lb.tr.side for lb in lbs], dtype=np.int8),
                "swing_px": np.asarray([lb.swing_px for lb in lbs], dtype=np.float64),
            })
            for sid, lbs in by_sid.items()
        }

    def run_bracket_variant(
        events_by_sid: dict[str, pa.Table],
        bracket_fn=None,
        struct: bool = False,
    ) -> dict:
        parts = []
        for sid, ev in events_by_sid.items():
            if sid not in session_levels:
                continue
            sl = session_levels[sid]
            if struct:
                n = ev.num_rows
                swing = ev.column("swing_px").to_numpy()
                nq = sl["nq"]
                ts = nq.column("ts_ns").to_numpy().astype(np.int64)
                px = nq.column("price").to_numpy().astype(np.float64) * 0.25
                order = np.argsort(ts)
                ts, px = ts[order], px[order]
                ev_ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
                sl_arr = np.empty(n, dtype=np.float64)
                tp_arr = np.empty(n, dtype=np.float64)
                for i in range(n):
                    ei = int(np.searchsorted(ts, int(ev_ts[i]), side="left"))
                    if ei >= len(ts):
                        sl_arr[i] = np.nan
                        tp_arr[i] = np.nan
                        continue
                    entry = float(px[ei])
                    risk = float(np.clip(abs(entry - float(swing[i])) + 1.0, 4.0, 40.0))
                    sl_arr[i] = risk
                    tp_arr[i] = 2.0 * risk
                ev = ev.append_column("tp_pts", pa.array(tp_arr))
                ev = ev.append_column("sl_pts", pa.array(sl_arr))
                br = None
            else:
                br = bracket_fn
            parts.append(simulate_session(
                sid, sl["nq"], sl["es"], sim_cfg, gate=gate,
                bracket_fn=br, vwap_from_ns=sl["vf"], events=ev,
            ))
        merged = merge_summaries(parts, COMMISSION) if parts else SimSummary()
        trn = filter_summary(merged, lambda t: t.session_id not in MAY, COMMISSION)
        my = filter_summary(merged, lambda t: t.session_id in MAY, COMMISSION)
        return {
            "all": merged.to_dict() | {"avg": round(merged.net / merged.n, 3) if merged.n else None},
            "train": trn.to_dict() | {"avg": round(trn.net / trn.n, 3) if trn.n else None},
            "may": my.to_dict() | {"avg": round(my.net / my.n, 3) if my.n else None},
        }

    ev_b5 = events_from_labeled(labeled_60_single, 5.0)
    ev_b0 = events_from_labeled(labeled_60_single, 0.0)
    brackets = {}
    for tag, evmap in (("bounce_5", ev_b5), ("bounce_0", ev_b0)):
        brackets[f"{tag}_sym_k1"] = run_bracket_variant(evmap, bracket_fn=make_vol_bracket(1.0))
        brackets[f"{tag}_rr_1_2"] = run_bracket_variant(
            evmap, bracket_fn=make_vol_bracket_rr(1.0, tp_r=2, sl_r=1),
        )
        brackets[f"{tag}_rr_1_3"] = run_bracket_variant(
            evmap, bracket_fn=make_vol_bracket_rr(1.0, tp_r=3, sl_r=1),
        )
        brackets[f"{tag}_struct_1_2"] = run_bracket_variant(evmap, struct=True)

    base_ev: dict[str, list[TradeResult]] = {}
    for tr in all_base:
        base_ev.setdefault(tr.session_id, []).append(tr)
    base_ev_tables = {
        sid: pa.table({
            "event_ts_ns": np.asarray([t.entry_ts_ns for t in trs], dtype=np.int64),
            "side": np.asarray([t.side for t in trs], dtype=np.int8),
            "swing_px": np.asarray([t.entry_px for t in trs], dtype=np.float64),
        })
        for sid, trs in base_ev.items()
    }
    brackets["base_sym_k1"] = report["base"]
    brackets["base_rr_1_2"] = run_bracket_variant(
        base_ev_tables, bracket_fn=make_vol_bracket_rr(1.0, tp_r=2, sl_r=1),
    )
    brackets["base_rr_1_3"] = run_bracket_variant(
        base_ev_tables, bracket_fn=make_vol_bracket_rr(1.0, tp_r=3, sl_r=1),
    )
    report["brackets"] = brackets

    # --- Design B: touch-first + MAT confirm ---
    design_b = {}
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100,
    )
    for buf in BUFFERS:
        for min_c, ctag in ((1, "single"), (2, "cluster2")):
            parts = []
            n_touch = 0
            n_confirmed = 0
            for sid, sl in session_levels.items():
                levels = sl["levels_single"] if min_c == 1 else sl["levels_clustered"]
                tev = touch_events(sl["nq"], levels, min_cluster_n=min_c)
                n_touch += tev.num_rows
                if tev.num_rows == 0:
                    continue
                nq_bars = trades_to_tape_bars(sl["nq"], bar_ms=100)
                es_bars = trades_to_tape_bars(sl["es"], bar_ms=100) if sl["es"].num_rows else pa.table({})
                nq_b = momentum_burst_events(nq_bars, **burst_kw)
                es_b = momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({})
                conf = mat_confirm_breaks(tev, nq_b, es_b, sync_ms=1500, max_es_lag_ms=250.0)
                if conf.num_rows:
                    conf = filter_hold_side_buffer(conf, sl["nq"], buffer_pts=buf)
                n_confirmed += conf.num_rows
                if conf.num_rows == 0:
                    continue
                parts.append(simulate_session(
                    sid, sl["nq"], sl["es"], sim_cfg, gate=gate,
                    bracket_fn=make_vol_bracket(1.0), vwap_from_ns=sl["vf"], events=conf,
                ))
            merged = merge_summaries(parts, COMMISSION) if parts else SimSummary()
            trn = filter_summary(merged, lambda t: t.session_id not in MAY, COMMISSION)
            my = filter_summary(merged, lambda t: t.session_id in MAY, COMMISSION)
            key = f"B_buf{buf:g}_{ctag}_sym_k1"
            design_b[key] = {
                "n_touches": n_touch,
                "n_mat_confirmed": n_confirmed,
                "all": merged.to_dict() | {"avg": round(merged.net / merged.n, 3) if merged.n else None},
                "train": trn.to_dict() | {"avg": round(trn.net / trn.n, 3) if trn.n else None},
                "may": my.to_dict() | {"avg": round(my.net / my.n, 3) if my.n else None},
            }
            if buf == 5.0 and min_c == 1:
                parts2 = []
                for sid, sl in session_levels.items():
                    tev = touch_events(sl["nq"], sl["levels_single"], min_cluster_n=1)
                    if tev.num_rows == 0:
                        continue
                    nq_bars = trades_to_tape_bars(sl["nq"], bar_ms=100)
                    es_bars = trades_to_tape_bars(sl["es"], bar_ms=100) if sl["es"].num_rows else pa.table({})
                    conf = mat_confirm_breaks(
                        tev,
                        momentum_burst_events(nq_bars, **burst_kw),
                        momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
                    )
                    if conf.num_rows:
                        conf = filter_hold_side_buffer(conf, sl["nq"], buffer_pts=5.0)
                    if conf.num_rows == 0:
                        continue
                    parts2.append(simulate_session(
                        sid, sl["nq"], sl["es"], sim_cfg, gate=gate,
                        bracket_fn=make_vol_bracket_rr(1.0, tp_r=2, sl_r=1),
                        vwap_from_ns=sl["vf"], events=conf,
                    ))
                m2 = merge_summaries(parts2, COMMISSION) if parts2 else SimSummary()
                trn2 = filter_summary(m2, lambda t: t.session_id not in MAY, COMMISSION)
                my2 = filter_summary(m2, lambda t: t.session_id in MAY, COMMISSION)
                design_b["B_buf5_single_rr_1_2"] = {
                    "all": m2.to_dict() | {"avg": round(m2.net / m2.n, 3) if m2.n else None},
                    "train": trn2.to_dict() | {"avg": round(trn2.net / trn2.n, 3) if trn2.n else None},
                    "may": my2.to_dict() | {"avg": round(my2.net / my2.n, 3) if my2.n else None},
                }
            print(
                f"B buf={buf} {ctag}: touches={n_touch} confirmed={n_confirmed} "
                f"n={merged.n} net={merged.net:+.1f}",
                flush=True,
            )

    report["design_b"] = design_b

    gate_note = "trend+VWAP" if use_gates else "**no trend/VWAP gates** (S/R paradigm)"
    md = [
        f"# {exp_tag} — MAT bounce at significant S/R",
        "",
        f"Frozen MAT: vol k=1, causal ES, {gate_note}, UK 14:30–17:00, 1-tick, 0.25 pt/RT.",
        "Pivots: **20 pt pullaway**, lookback 10m. Side map: long@LOW / short@HIGH.",
        "May 2026 = OOS (no retune). Opposite of EXP-0039 breakout.",
        "",
        "## Base MAT",
        "",
        _md_cohort_row(report["base"]),
        "",
        "## Headline: bounce_5 vs none @ 60s (single levels)",
        "",
        "| stratum | cohort | n | WR | avg | net | PF | maxDD | days+ |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in (
        "w60_single_bounce_5_both",
        "w60_single_bounce_5_long",
        "w60_single_bounce_5_short",
        "w60_single_none_both",
        "w60_single_at_both",
        "base",
    ):
        block = strata_tables.get(name) or report["base"]
        for cohort in ("train", "may", "all"):
            r = block[cohort]
            md.append(
                f"| {name} | {cohort} | {r['n']} | {r.get('wr')} | {r.get('avg')} | "
                f"{r.get('net')} | {r.get('pf')} | {r.get('max_dd')} | "
                f"{r.get('days_pos')}/{r.get('days')} |"
            )

    md.extend(["", "## Bounce window sweep (buffer 5, single, both sides)", ""])
    md.append("| window | stratum | cohort | n | avg | net | PF |")
    md.append("|---:|---|---|---:|---:|---:|---:|")
    for w in WINDOWS:
        for stem in (f"w{w}_single_bounce_5", f"w{w}_single_none"):
            block = window_table[stem]
            for cohort in ("train", "may"):
                r = block[cohort]
                md.append(
                    f"| {w} | {stem} | {cohort} | {r['n']} | {r.get('avg')} | "
                    f"{r.get('net')} | {r.get('pf')} |"
                )

    md.extend(["", "## Buffers @ 60s single both", ""])
    md.append("| buffer | cohort | n | avg | net | PF |")
    md.append("|---:|---|---:|---:|---:|---:|")
    for buf in BUFFERS:
        name = f"w60_single_bounce_{buf:g}_both"
        for cohort in ("train", "may"):
            r = strata_tables[name][cohort]
            md.append(
                f"| {buf:g} | {cohort} | {r['n']} | {r.get('avg')} | {r.get('net')} | {r.get('pf')} |"
            )

    md.extend(["", "## Cluster >= 2 @ 60s bounce_5", ""])
    md.append("| side | cohort | n | avg | net | PF |")
    md.append("|---|---|---:|---:|---:|---:|")
    for side_name in ("both", "long", "short"):
        name = f"w60_clustered_bounce_5_{side_name}"
        if name not in strata_tables:
            continue
        for cohort in ("train", "may"):
            r = strata_tables[name][cohort]
            md.append(
                f"| {side_name} | {cohort} | {r['n']} | {r.get('avg')} | {r.get('net')} | {r.get('pf')} |"
            )

    md.extend(["", "## Brackets on bounce_5 @ 60s single (re-sim)", ""])
    md.append("| variant | cohort | n | avg | net | PF | maxDD |")
    md.append("|---|---|---:|---:|---:|---:|---:|")
    for key in sorted(brackets.keys()):
        for cohort in ("train", "may"):
            r = brackets[key][cohort]
            md.append(
                f"| {key} | {cohort} | {r['n']} | {r.get('avg')} | {r.get('net')} | "
                f"{r.get('pf')} | {r.get('max_dd')} |"
            )

    md.extend(["", "## Design B (touch-first + MAT confirm)", ""])
    md.append("| variant | touches | confirmed | cohort | n | avg | net | PF |")
    md.append("|---|---:|---:|---|---:|---:|---:|---:|")
    for key, block in design_b.items():
        for cohort in ("train", "may"):
            r = block[cohort]
            md.append(
                f"| {key} | {block.get('n_touches', '')} | {block.get('n_mat_confirmed', '')} | "
                f"{cohort} | {r['n']} | {r.get('avg')} | {r.get('net')} | {r.get('pf')} |"
            )

    b5 = strata_tables["w60_single_bounce_5_both"]["train"]
    none = strata_tables["w60_single_none_both"]["train"]
    b5m = strata_tables["w60_single_bounce_5_both"]["may"]
    nonem = strata_tables["w60_single_none_both"]["may"]
    md.extend([
        "",
        "## Verdict (pre-registered)",
        "",
        f"- Train bounce_5 avg={b5.get('avg')} n={b5['n']} PF={b5.get('pf')} vs "
        f"none avg={none.get('avg')} n={none['n']} PF={none.get('pf')}",
        f"- May bounce_5 avg={b5m.get('avg')} n={b5m['n']} vs none avg={nonem.get('avg')} n={nonem['n']}",
        "",
    ])
    lift = (
        b5["n"] >= 30
        and b5.get("avg") is not None
        and none.get("avg") is not None
        and b5["avg"] > none["avg"]
        and (b5.get("pf") or 0) > (none.get("pf") or 0)
    )
    may_ok = (
        b5m.get("avg") is not None
        and nonem.get("avg") is not None
        and b5m["avg"] > nonem["avg"]
    )
    if lift and may_ok:
        md.append("**Ship candidate:** bounce_5 beats none on train and May.")
    elif lift:
        md.append("**Train lift only — May disagrees or thin. Do not ship; dig carefully.**")
    else:
        md.append("**Reject / park:** bounce_5 does not clearly beat none on train.")
    md.append("")

    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    (out / "sanity.json").write_text(json.dumps(sanity, indent=2), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


def _md_cohort_row(block: dict) -> str:
    lines = [
        "| cohort | n | WR | avg | net | PF | maxDD | days+ |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cohort in ("train", "may", "all"):
        r = block[cohort]
        lines.append(
            f"| {cohort} | {r['n']} | {r.get('wr')} | {r.get('avg')} | {r.get('net')} | "
            f"{r.get('pf')} | {r.get('max_dd')} | {r.get('days_pos')}/{r.get('days')} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
