"""P&L dig: stop-run fingerprint as a trade signal (EXP-0041 follow-up).

Variants:
  - continuation at window end (join the run)
  - fade at window end (fade immediately)
  - fade at reclaim (only stall events, enter on reclaim)

Each x gate on/off x bracket {vol_k1, fixed_10_10, fixed_12p5_30}.
Strata on best continuation arm: ES confirm, score>=4, SWING_LOW, SWING_HIGH.

Writes results/EXP-0041_PNL/.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import yaml

from research.backtest.mat_sim import (
    MatSimConfig,
    TradeResult,
    combine_gates,
    make_trend_mode,
    make_vol_bracket,
    make_vwap_side,
    simulate_session,
)
from research.config import load_config
from research.correlated.harness import _prefer_single_feed
from research.features.stop_run import (
    fade_events_at_reclaim,
    resolve_outcome,
    stop_run_events,
)
from research.run_experiment import _load_mbo, _load_trades
from research.stop_run.run_exp_0041 import (
    MAY,
    COMMISSION,
    _cfg_from_yaml,
    bounds,
    cohort_split,
    filt,
    pack_trades,
)


def _filter_ev(ev: pa.Table, pred) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    keep = pred(ev)
    if keep is None or not np.any(keep):
        return ev.slice(0, 0)
    return ev.take(np.where(keep)[0])


def _flip_side(ev: pa.Table) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    sides = -ev.column("side").to_numpy().astype(np.int8)
    names = ev.column_names
    cols = [pa.array(sides, type=pa.int8()) if n == "side" else ev.column(n) for n in names]
    return pa.table({n: c for n, c in zip(names, cols)})


def _sim_all(
    sessions_data: list[tuple],
    events_by_sid: dict[str, pa.Table],
    sim_cfg: MatSimConfig,
    gate,
    bracket_fn,
) -> list[TradeResult]:
    trades: list[TradeResult] = []
    for sid, nq, es, vf in sessions_data:
        ev = events_by_sid.get(sid)
        if ev is None or ev.num_rows == 0:
            continue
        # Need only event_ts_ns + side for sim
        if "event_ts_ns" not in ev.column_names:
            continue
        use = ev.select(["event_ts_ns", "side"])
        s = simulate_session(
            sid, nq, es, sim_cfg,
            gate=gate, bracket_fn=bracket_fn, vwap_from_ns=vf, events=use,
        )
        trades.extend(s.trades)
    return trades


def _block(trades: list[TradeResult]) -> dict:
    tr, may = cohort_split(trades)
    return {
        "all": pack_trades(trades),
        "train": pack_trades(tr),
        "may": pack_trades(may),
    }


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    exp_path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0041.yaml"
    with open(exp_path, encoding="utf-8") as f:
        exp = yaml.safe_load(f)
    sr_cfg = _cfg_from_yaml(exp)

    out = Path(cfg["results_dir"]) / "EXP-0041_PNL"
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name.replace("session_id=", "")
        for p in pq.iterdir()
        if p.is_dir()
        and p.name.startswith("session_id=")
        and (p / "trades.parquet").exists()
        and "smoke" not in p.name
    )
    print(f"sessions: {len(sessions)}", flush=True)

    sessions_data = []
    cont_by: dict[str, pa.Table] = {}
    fade_fire_by: dict[str, pa.Table] = {}
    fade_reclaim_by: dict[str, pa.Table] = {}
    meta_by: dict[str, pa.Table] = {}  # full events with strata cols

    for sid in sessions:
        print(f"[pnl] {sid} …", flush=True)
        nq0 = _prefer_single_feed(_load_trades(pq, sid, "NQ"), pq, sid, "NQ")
        es0 = _prefer_single_feed(_load_trades(pq, sid, "ES"), pq, sid, "ES")
        if nq0.num_rows == 0:
            continue
        nq = filt(nq0)
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows == 0:
            continue
        mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
        s, e = bounds(mid)
        vf = s

        mbo0 = _load_mbo(pq, sid, "NQ")
        mbo = mbo0 if mbo0.num_rows else None

        ev = stop_run_events(nq0, es0 if es0.num_rows else None, mbo, sr_cfg)
        if ev.num_rows:
            ets = ev.column("event_ts_ns").to_numpy().astype(np.int64)
            keep = (ets >= s) & (ets <= e)
            ev = ev.take(np.where(keep)[0]) if keep.any() else ev.slice(0, 0)
        ev = resolve_outcome(ev, nq0, reclaim_sec=sr_cfg.reclaim_sec)

        sessions_data.append((sid, nq, es, vf))
        meta_by[sid] = ev
        cont_by[sid] = ev.select(["event_ts_ns", "side"]) if ev.num_rows else ev
        fade_fire_by[sid] = (
            _flip_side(ev).select(["event_ts_ns", "side"]) if ev.num_rows else ev
        )
        fr = fade_events_at_reclaim(ev)
        fade_reclaim_by[sid] = (
            fr.select(["event_ts_ns", "side"]) if fr.num_rows else fr
        )
        print(
            f"  ev={ev.num_rows} reclaim={fr.num_rows if fr is not None else 0}",
            flush=True,
        )

    gate_on = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    brackets = {
        "vol_k1": make_vol_bracket(1.0),
        "fixed_10_10": None,  # use cfg tp/sl
        "fixed_12p5_30": None,
    }
    sim_cfgs = {
        "vol_k1": MatSimConfig(
            slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal",
            tp_pts=10.0, sl_pts=10.0,
        ),
        "fixed_10_10": MatSimConfig(
            slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal",
            tp_pts=10.0, sl_pts=10.0,
        ),
        "fixed_12p5_30": MatSimConfig(
            slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal",
            tp_pts=12.5, sl_pts=30.0,
        ),
    }

    arms = {
        "continuation": cont_by,
        "fade_at_fire": fade_fire_by,
        "fade_at_reclaim": fade_reclaim_by,
    }

    report: dict = {"arms": {}, "strata": {}}

    for arm_name, by_sid in arms.items():
        report["arms"][arm_name] = {}
        for gate_name, gate in (("gated", gate_on), ("ungated", None)):
            report["arms"][arm_name][gate_name] = {}
            for br_name, br_fn in brackets.items():
                # vol_k1 uses bracket_fn; fixed uses cfg tp/sl with bracket_fn=None
                use_br = br_fn if br_name == "vol_k1" else None
                if br_name == "fixed_10_10":
                    use_br = None
                elif br_name == "fixed_12p5_30":
                    use_br = None
                trades = _sim_all(
                    sessions_data, by_sid, sim_cfgs[br_name], gate, use_br
                )
                # For fixed brackets without bracket_fn, simulate_session uses cfg.tp/sl
                block = _block(trades)
                report["arms"][arm_name][gate_name][br_name] = block
                d = block["all"]
                print(
                    f"{arm_name}/{gate_name}/{br_name}: n={d.get('n')} net={d.get('net')} "
                    f"train={block['train'].get('net')} may={block['may'].get('net')}",
                    flush=True,
                )

    # Strata on continuation ungated vol_k1 and gated vol_k1
    def stratum_events(pred):
        out_m: dict[str, pa.Table] = {}
        for sid, ev in meta_by.items():
            if ev.num_rows == 0:
                out_m[sid] = ev
                continue
            f = _filter_ev(ev, pred)
            out_m[sid] = f.select(["event_ts_ns", "side"]) if f.num_rows else f
        return out_m

    strata_defs = {
        "es_confirmed": lambda t: t.column("es_confirmed").to_numpy().astype(bool),
        "score_ge_4": lambda t: t.column("score").to_numpy().astype(int) >= 4,
        "swing_low": lambda t: np.array(
            [x.as_py() == "SWING_LOW" for x in t.column("level_kind")]
        ),
        "swing_high": lambda t: np.array(
            [x.as_py() == "SWING_HIGH" for x in t.column("level_kind")]
        ),
        "outcome_continue_ex_ante_NO": None,  # skip — look-ahead
    }

    for sname, pred in strata_defs.items():
        if pred is None:
            continue
        by = stratum_events(pred)
        report["strata"][sname] = {}
        for gate_name, gate in (("gated", gate_on), ("ungated", None)):
            trades = _sim_all(
                sessions_data, by, sim_cfgs["vol_k1"], gate, brackets["vol_k1"]
            )
            report["strata"][sname][gate_name] = _block(trades)
            d = report["strata"][sname][gate_name]["all"]
            print(
                f"strata/{sname}/{gate_name}: n={d.get('n')} net={d.get('net')}",
                flush=True,
            )

    # Fade-at-fire strata that looked interesting: fade when we would have stalled
    # is look-ahead — instead report fade_at_fire on ES confirm only
    by_es = stratum_events(
        lambda t: t.column("es_confirmed").to_numpy().astype(bool)
    )
    # flip for fade
    by_es_fade = {sid: _flip_side(ev) if ev.num_rows else ev for sid, ev in by_es.items()}
    # need to rebuild from meta
    by_es_fade = {}
    for sid, ev in meta_by.items():
        if ev.num_rows == 0:
            by_es_fade[sid] = ev
            continue
        f = _filter_ev(ev, lambda t: t.column("es_confirmed").to_numpy().astype(bool))
        by_es_fade[sid] = _flip_side(f).select(["event_ts_ns", "side"]) if f.num_rows else f

    report["strata"]["fade_fire_es_confirmed"] = {}
    for gate_name, gate in (("gated", gate_on), ("ungated", None)):
        trades = _sim_all(
            sessions_data, by_es_fade, sim_cfgs["vol_k1"], gate, brackets["vol_k1"]
        )
        report["strata"]["fade_fire_es_confirmed"][gate_name] = _block(trades)

    (out / "report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )

    # Markdown
    lines = [
        "# EXP-0041_PNL — Stop-run as trade signal",
        "",
        "UK 14:30–17:00, 1-tick slip, 0.25 pt RT commission. May held out.",
        "Entry at fingerprint window end unless noted. `outcome_*` never used for entry.",
        "",
        "## Primary arms (vol k=1)",
        "",
        "| Arm | Gates | n | net | wr | pf | train net | may net | days+ | loo_neg |",
        "|-----|-------|---|-----|----|----|-----------|---------|-------|---------|",
    ]

    def row(arm, gate, br="vol_k1"):
        b = report["arms"][arm][gate][br]
        a, t, m = b["all"], b["train"], b["may"]
        return (
            f"| {arm} | {gate} | {a.get('n')} | {a.get('net')} | {a.get('wr')} "
            f"| {a.get('pf')} | {t.get('net')} | {m.get('net')} "
            f"| {a.get('days_pos')}/{a.get('days')} | {t.get('loo_neg')} |"
        )

    for arm in arms:
        for gate in ("gated", "ungated"):
            lines.append(row(arm, gate))

    lines += [
        "",
        "## Bracket dig (continuation)",
        "",
        "| Gates | Bracket | n | net | train | may |",
        "|-------|---------|---|-----|-------|-----|",
    ]
    for gate in ("gated", "ungated"):
        for br in ("vol_k1", "fixed_10_10", "fixed_12p5_30"):
            b = report["arms"]["continuation"][gate][br]
            lines.append(
                f"| {gate} | {br} | {b['all'].get('n')} | {b['all'].get('net')} "
                f"| {b['train'].get('net')} | {b['may'].get('net')} |"
            )

    lines += [
        "",
        "## Strata (continuation, vol k=1)",
        "",
        "| Stratum | Gates | n | net | train | may |",
        "|---------|-------|---|-----|-------|-----|",
    ]
    for sname, block in report["strata"].items():
        if sname.startswith("fade_"):
            continue
        for gate in ("gated", "ungated"):
            b = block[gate]
            lines.append(
                f"| {sname} | {gate} | {b['all'].get('n')} | {b['all'].get('net')} "
                f"| {b['train'].get('net')} | {b['may'].get('net')} |"
            )

    lines += [
        "",
        "## Fade @ fire × ES confirmed (vol k=1)",
        "",
        "| Gates | n | net | train | may |",
        "|-------|---|-----|-------|-----|",
    ]
    for gate in ("gated", "ungated"):
        b = report["strata"]["fade_fire_es_confirmed"][gate]
        lines.append(
            f"| {gate} | {b['all'].get('n')} | {b['all'].get('net')} "
            f"| {b['train'].get('net')} | {b['may'].get('net')} |"
        )

    # Verdict
    best = report["arms"]["continuation"]["gated"]["vol_k1"]
    fade = report["arms"]["fade_at_fire"]["ungated"]["vol_k1"]
    lines += [
        "",
        "## Verdict",
        "",
        f"- **Join the run (continuation, gated, vol k=1):** "
        f"n={best['all'].get('n')} net={best['all'].get('net')} "
        f"(train {best['train'].get('net')} / May {best['may'].get('net')}). "
        f"Thin sample; train loo_neg={best['train'].get('loo_neg')}.",
        f"- **Fade at fire (ungated, vol k=1):** "
        f"n={fade['all'].get('n')} net={fade['all'].get('net')} "
        f"(train {fade['train'].get('net')} / May {fade['may'].get('net')}).",
        f"- **Fade at reclaim:** see table — often empty under gates "
        f"(reclaim entries rarely pass trend+VWAP).",
        "",
        "Ship bar (train+May positive, loo_neg=0, material n, event-study lift): "
        "**not met**. Do not auto-trade. Visual Stop Run module stands.",
        "",
    ]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {out}", flush=True)


if __name__ == "__main__":
    main()
