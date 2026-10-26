"""EXP-0060 / H51 — Role-swapped CFADE (ES-led, NQ-against exhaustion)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    detect_signals,
    make_vol_bracket_rr,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events
from research.features.momentum import momentum_burst_events


def _shift_bars(bars: pa.Table, shift_ns: int) -> pa.Table:
    if bars.num_rows == 0:
        return bars
    ts = bars.column("ts_ns").to_numpy().astype(np.int64) + shift_ns
    cols = {c: bars.column(c) for c in bars.column_names if c != "ts_ns"}
    cols["ts_ns"] = pa.array(ts)
    return pa.table({c: cols[c] for c in bars.column_names})


def _pack(parts_all, parts_is, parts_oos, commission: float) -> dict:
    def one(ps):
        if not ps:
            return {"n": 0, "net": 0.0, "avg": None, "wr": None}
        s = merge_summaries(ps, commission)
        d = s.to_dict()
        d["avg"] = round(s.net / s.n, 2) if s.n else None
        return d
    return {"all": one(parts_all), "is": one(parts_is), "oos": one(parts_oos)}


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0060"
    sessions = F.clean_sessions(pq)

    gate = F.frozen_gate()
    # ES brackets for fade-ES; NQ brackets for fade-NQ
    es_br = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=2.0, cap=15.0)
    nq_br = F.frozen_bracket()
    p_ex = ExhaustParams(
        es_imb_max=-0.4, es_min_vol=0, use_imb=True, use_vol=False,
        nq_min_imbalance=0.55, nq_min_vol=25, nq_min_trades=6,
    )
    burst_kw = dict(min_trades=6, min_vol=25.0, min_imbalance=0.55,
                    min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100)
    det_cfg = MatSimConfig(es_stamp="confirm", slip_ticks=1.0)

    keys = [
        "A_fade_ES", "B_fade_NQ",
        "twin_ESonly_fade_ES", "placebo_shiftNQ_fade_ES", "null_fade_ES",
    ]
    parts = {k: [] for k in keys}
    is_p = {k: [] for k in keys}
    oos_p = {k: [] for k in keys}

    for i, sid in enumerate(sessions):
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        nq_b, es_b = pair.bars(100)
        # ES-led confirm (swap args to detect_signals)
        confirm = detect_signals(pair.es, pair.nq, det_cfg)
        # Exhaust: ES still pushing (nq_bars slot), NQ against (es_bars slot)
        fades = exhaust_fade_events(confirm, es_b, nq_b, p_ex, max_wait_sec=5.0)

        # Twin: ES-only burst → NQ against
        es_only = momentum_burst_events(es_b, **burst_kw)
        twin = exhaust_fade_events(es_only, es_b, nq_b, p_ex, max_wait_sec=5.0)

        # Placebo: NQ bars shifted +60s
        nq_shift = _shift_bars(nq_b, 60_000_000_000)
        placebo = exhaust_fade_events(confirm, es_b, nq_shift, p_ex, max_wait_sec=5.0)

        null = H.null_events(pair, max(1, fades.num_rows), seed=6000 + i) if fades.num_rows else pa.table({})

        cfg_es = MatSimConfig(
            slip_ticks=1.0, commission_pts_rt=F.ES_COMMISSION,
            signal_cooldown_sec=5.0, tp_pts=10, sl_pts=10, pts_per_tick=F.ES_PTS_PER_TICK,
        )
        cfg_nq = F.frozen_sim_cfg()

        sims = {}
        if fades.num_rows:
            sims["A_fade_ES"] = simulate_session(
                sid, pair.es, pair.nq, cfg_es, gate=gate, bracket_fn=es_br,
                vwap_from_ns=pair.vwap_from_ns, events=fades,
            )
            sims["B_fade_NQ"] = simulate_session(
                sid, pair.nq, pair.es, cfg_nq, gate=gate, bracket_fn=nq_br,
                vwap_from_ns=pair.vwap_from_ns, events=fades,
            )
        if twin.num_rows:
            sims["twin_ESonly_fade_ES"] = simulate_session(
                sid, pair.es, pair.nq, cfg_es, gate=gate, bracket_fn=es_br,
                vwap_from_ns=pair.vwap_from_ns, events=twin,
            )
        if placebo.num_rows:
            sims["placebo_shiftNQ_fade_ES"] = simulate_session(
                sid, pair.es, pair.nq, cfg_es, gate=gate, bracket_fn=es_br,
                vwap_from_ns=pair.vwap_from_ns, events=placebo,
            )
        if null.num_rows:
            sims["null_fade_ES"] = simulate_session(
                sid, pair.es, pair.nq, cfg_es, gate=gate, bracket_fn=es_br,
                vwap_from_ns=pair.vwap_from_ns, events=null,
            )

        for k, s in sims.items():
            parts[k].append(s)
            (is_p[k] if F.is_is(sid) else oos_p[k]).append(s)

        a = sims.get("A_fade_ES")
        print(
            f"{sid} confirm={confirm.num_rows} fades={fades.num_rows} "
            f"A={a.net if a else 0:+.1f}",
            flush=True,
        )

    books = {
        k: _pack(parts[k], is_p[k], oos_p[k],
                 F.ES_COMMISSION if "fade_ES" in k or k.startswith("twin") or k.startswith("placebo") or k.startswith("null") else F.COMMISSION)
        for k in keys
    }
    # Fix commission for B_fade_NQ
    books["B_fade_NQ"] = _pack(parts["B_fade_NQ"], is_p["B_fade_NQ"], oos_p["B_fade_NQ"], F.COMMISSION)

    A = books["A_fade_ES"]
    twin = books["twin_ESonly_fade_ES"]
    plac = books["placebo_shiftNQ_fade_ES"]
    nul = books["null_fade_ES"]
    B = books["B_fade_NQ"]

    def beat(a, b):
        return a["oos"]["avg"] is not None and b["oos"]["avg"] is not None and a["oos"]["avg"] > b["oos"]["avg"]

    kill_A = {
        "beat_twin_oos": beat(A, twin),
        "beat_placebo_oos": beat(A, plac) and beat(A, nul),
        "oos_net_pos": A["oos"]["net"] > 0,
    }
    kill_A["pass"] = kill_A["beat_twin_oos"] and kill_A["beat_placebo_oos"] and kill_A["oos_net_pos"]
    kill_B = {
        "beat_twin_oos": B["oos"]["avg"] is not None and twin["oos"]["avg"] is not None and B["oos"]["avg"] > twin["oos"]["avg"],
        "oos_net_pos": B["oos"]["net"] > 0,
    }
    # B twin isn't really ES-only fade NQ; report descriptive
    kill_B["pass"] = kill_B["oos_net_pos"] and B["oos"]["avg"] is not None and B["oos"]["avg"] > 0

    md = [
        "# EXP-0060 — Role-swapped CFADE",
        "",
        "ES-led confirm → NQ-against while ES still pushes → fade.",
        "",
        "| book | n | WR | net | avg | IS net | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for k, v in books.items():
        a, i, o = v["all"], v["is"], v["oos"]
        md.append(
            f"| {k} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{i['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} |"
        )
    md += [
        "",
        f"## Kill A (fade ES): **{'PASS' if kill_A['pass'] else 'FAIL'}** "
        f"(twin={kill_A['beat_twin_oos']} placebo={kill_A['beat_placebo_oos']} oos+={kill_A['oos_net_pos']})",
        f"## Kill B (fade NQ): **{'PASS' if kill_B['pass'] else 'FAIL'}** "
        f"(oos+={kill_B['oos_net_pos']})",
        "",
    ]
    F.write_report(out, title="EXP-0060", md_lines=md, report={
        "exp": "EXP-0060", "books": books, "kill_A": kill_A, "kill_B": kill_B,
    })


if __name__ == "__main__":
    main()
