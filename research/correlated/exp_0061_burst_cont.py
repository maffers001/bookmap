"""EXP-0061 / H52 — Confirmed-burst continuation + ES-against hand-off exit."""
from __future__ import annotations

from pathlib import Path

import pyarrow as pa

from research.backtest.mat_sim import merge_summaries, simulate_session
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.corr_exhaust import ExhaustParams, build_exhaust_exit_fn
from research.features.momentum import momentum_burst_events


def _pack(parts, parts_is, parts_oos) -> dict:
    def one(ps):
        if not ps:
            return {"n": 0, "net": 0.0, "avg": None, "wr": None}
        s = merge_summaries(ps, F.COMMISSION)
        d = s.to_dict()
        d["avg"] = round(s.net / s.n, 2) if s.n else None
        return d
    return {"all": one(parts), "is": one(parts_is), "oos": one(parts_oos)}


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0061"
    sessions = F.clean_sessions(pq)

    gate = F.frozen_gate()
    br = F.frozen_bracket()
    sim = F.frozen_sim_cfg()
    # Exhaust exit: ES against while NQ still with the *trade* (continuation side)
    p_ex = ExhaustParams(
        es_imb_max=-0.4, es_min_vol=0, use_imb=True, use_vol=False,
        nq_min_imbalance=0.55, nq_min_vol=25, nq_min_trades=6,
        max_wait_sec=5.0,
    )
    burst_kw = dict(min_trades=6, min_vol=25.0, min_imbalance=0.55,
                    min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100)

    keys = ["cont_confirm", "twin_nq_only", "fade_at_stamp_D", "null"]
    parts = {k: [] for k in keys}
    is_p = {k: [] for k in keys}
    oos_p = {k: [] for k in keys}

    for i, sid in enumerate(sessions):
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        nq_b, es_b = pair.bars(100)
        confirm = H.confirm_bursts(pair)
        nq_only = momentum_burst_events(nq_b, **burst_kw)

        exit_fn = build_exhaust_exit_fn(nq_b, es_b, p_ex)

        sims = {}
        if confirm.num_rows:
            # Continuation: enter with burst side; exit on exhaust or bracket
            sims["cont_confirm"] = simulate_session(
                sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                vwap_from_ns=pair.vwap_from_ns, events=confirm, exit_fn=exit_fn,
            )
            # Anatomy D: fade at stamp (side flipped), brackets only
            fades_d = pa.table({
                "event_ts_ns": confirm.column("event_ts_ns"),
                "side": pa.array((-confirm.column("side").to_numpy().astype(int)).astype("int8")),
            })
            sims["fade_at_stamp_D"] = simulate_session(
                sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                vwap_from_ns=pair.vwap_from_ns, events=fades_d,
            )
        if nq_only.num_rows:
            sims["twin_nq_only"] = simulate_session(
                sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                vwap_from_ns=pair.vwap_from_ns, events=nq_only, exit_fn=exit_fn,
            )
        n_match = confirm.num_rows if confirm.num_rows else 1
        null = H.null_events(pair, n_match, seed=6100 + i)
        sims["null"] = simulate_session(
            sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
            vwap_from_ns=pair.vwap_from_ns, events=null, exit_fn=exit_fn,
        )

        for k, s in sims.items():
            parts[k].append(s)
            (is_p[k] if F.is_is(sid) else oos_p[k]).append(s)

        c = sims.get("cont_confirm")
        print(f"{sid} confirm={confirm.num_rows} cont={c.net if c else 0:+.1f}", flush=True)

    books = {k: _pack(parts[k], is_p[k], oos_p[k]) for k in keys}
    C, T, D, N = books["cont_confirm"], books["twin_nq_only"], books["fade_at_stamp_D"], books["null"]

    def beat(a, b):
        return a["oos"]["avg"] is not None and b["oos"]["avg"] is not None and a["oos"]["avg"] > b["oos"]["avg"]

    kill = {
        "beat_twin_oos": beat(C, T),
        "beat_null_oos": beat(C, N),
        "oos_net_pos": C["oos"]["net"] > 0,
        "beats_fade_D_oos": beat(C, D),  # descriptive
    }
    kill["pass"] = kill["beat_twin_oos"] and kill["beat_null_oos"] and kill["oos_net_pos"]

    md = [
        "# EXP-0061 — Confirmed-burst continuation + ES-against exit",
        "",
        "Enter with confirm burst; exit on first ES-against (exhaust) or vol bracket.",
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
        f"## Verdict: **{'PASS' if kill['pass'] else 'FAIL / park'}**",
        f"twin={kill['beat_twin_oos']} null={kill['beat_null_oos']} "
        f"oos+={kill['oos_net_pos']} vs_D={kill['beats_fade_D_oos']}",
        "",
    ]
    F.write_report(out, title="EXP-0061", md_lines=md, report={
        "exp": "EXP-0061", "books": books, "kill": kill,
    })


if __name__ == "__main__":
    main()
