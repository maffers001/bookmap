"""SWING_HIGH join-run P&L scored on October (vs May / September)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import yaml

from research.backtest.mat_sim import (
    MatSimConfig,
    combine_gates,
    make_trend_mode,
    make_vol_bracket,
    make_vwap_side,
    simulate_session,
)
from research.config import load_config
from research.correlated.harness import _prefer_single_feed
from research.features.stop_run import resolve_outcome, stop_run_events
from research.run_experiment import _load_mbo, _load_trades
from research.stop_run.run_exp_0041 import (
    MAY,
    COMMISSION,
    _cfg_from_yaml,
    bounds,
    filt,
    pack_trades,
)

OCT = {
    "011026", "021026", "051026", "061026", "071026", "081026", "091026",
    "121026", "131026", "141026", "151026", "161026", "191026",
}
SEP = {
    "010926", "020926", "040926", "080926", "100926", "150926", "160926",
    "170926", "180926", "210926", "220926", "250926", "280926", "290926", "300926",
}


def cohort(sid: str) -> str:
    if sid in MAY:
        return "may"
    if sid in OCT:
        return "october"
    if sid in SEP:
        return "september"
    return "other"


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    exp_path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0041.yaml"
    with open(exp_path, encoding="utf-8") as f:
        exp = yaml.safe_load(f)
    sr_cfg = _cfg_from_yaml(exp)

    sessions = sorted(
        p.name.replace("session_id=", "")
        for p in pq.iterdir()
        if p.is_dir()
        and p.name.startswith("session_id=")
        and (p / "trades.parquet").exists()
        and "smoke" not in p.name
    )

    gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    br = make_vol_bracket(1.0)
    sim_cfg = MatSimConfig(
        slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal",
    )

    results = {
        "swing_high_gated": [],
        "swing_high_ungated": [],
        "all_cont_gated": [],
    }
    per_session = []

    for sid in sessions:
        print(f"{sid} ({cohort(sid)}) …", flush=True)
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

        n_sh = 0
        if ev.num_rows and "level_kind" in ev.column_names:
            kinds = [x.as_py() for x in ev.column("level_kind")]
            n_sh = sum(1 for k in kinds if k == "SWING_HIGH")
            mask = np.array([k == "SWING_HIGH" for k in kinds])
            sh = ev.take(np.where(mask)[0]) if mask.any() else ev.slice(0, 0)
        else:
            sh = ev.slice(0, 0)

        def run(events, gated: bool):
            if events.num_rows == 0:
                return []
            use = events.select(["event_ts_ns", "side"])
            summary = simulate_session(
                sid, nq, es, sim_cfg,
                gate=gate if gated else None,
                bracket_fn=br,
                vwap_from_ns=vf,
                events=use,
            )
            return summary.trades

        sh_g = run(sh, True)
        sh_u = run(sh, False)
        all_g = run(ev, True)
        results["swing_high_gated"].extend(sh_g)
        results["swing_high_ungated"].extend(sh_u)
        results["all_cont_gated"].extend(all_g)

        def net(trs):
            return round(sum(t.pnl - COMMISSION for t in trs), 2)

        per_session.append({
            "session": sid,
            "cohort": cohort(sid),
            "events": int(ev.num_rows),
            "swing_high_events": n_sh,
            "sh_gated_n": len(sh_g),
            "sh_gated_net": net(sh_g),
            "sh_ungated_n": len(sh_u),
            "sh_ungated_net": net(sh_u),
            "all_gated_n": len(all_g),
            "all_gated_net": net(all_g),
        })
        print(
            f"  sh_ev={n_sh} sh_gated n={len(sh_g)} net={net(sh_g)} "
            f"ungated n={len(sh_u)} net={net(sh_u)}",
            flush=True,
        )

    def by_cohort(trades):
        out = {}
        for name in ("october", "may", "september", "other", "all"):
            if name == "all":
                subset = trades
            else:
                subset = [t for t in trades if cohort(t.session_id) == name]
            out[name] = pack_trades(subset)
        return out

    report = {
        "swing_high_gated": by_cohort(results["swing_high_gated"]),
        "swing_high_ungated": by_cohort(results["swing_high_ungated"]),
        "all_cont_gated": by_cohort(results["all_cont_gated"]),
        "per_session": per_session,
    }

    out = Path(cfg["results_dir"]) / "EXP-0041_PNL_OCT"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )

    lines = [
        "# EXP-0041 — SWING_HIGH join-run vs October",
        "",
        "Continuation at window end, vol k=1, 1-tick slip, 0.25 RT. "
        "SWING_HIGH stratum only (join upside stop runs through pullaway highs).",
        "",
        f"October sessions ({len(OCT)}): {', '.join(sorted(OCT))}",
        "",
        "## SWING_HIGH gated (trend+VWAP)",
        "",
        "| Cohort | n | net | wr | pf | days+ | top3 | loo_neg | avg |",
        "|--------|---|-----|----|----|-------|------|---------|-----|",
    ]
    for name in ("october", "may", "september", "other", "all"):
        d = report["swing_high_gated"][name]
        lines.append(
            f"| {name} | {d.get('n')} | {d.get('net')} | {d.get('wr')} | {d.get('pf')} "
            f"| {d.get('days_pos')}/{d.get('days')} | {d.get('top3_share')} "
            f"| {d.get('loo_neg')} | {d.get('avg')} |"
        )

    lines += [
        "",
        "## SWING_HIGH ungated",
        "",
        "| Cohort | n | net | wr | pf | days+ | top3 | loo_neg | avg |",
        "|--------|---|-----|----|----|-------|------|---------|-----|",
    ]
    for name in ("october", "may", "september", "other", "all"):
        d = report["swing_high_ungated"][name]
        lines.append(
            f"| {name} | {d.get('n')} | {d.get('net')} | {d.get('wr')} | {d.get('pf')} "
            f"| {d.get('days_pos')}/{d.get('days')} | {d.get('top3_share')} "
            f"| {d.get('loo_neg')} | {d.get('avg')} |"
        )

    lines += [
        "",
        "## October session detail (SWING_HIGH)",
        "",
        "| Session | sh events | gated n | gated net | ungated n | ungated net |",
        "|---------|-----------|---------|-----------|-----------|-------------|",
    ]
    for row in per_session:
        if row["cohort"] != "october":
            continue
        lines.append(
            f"| {row['session']} | {row['swing_high_events']} | {row['sh_gated_n']} "
            f"| {row['sh_gated_net']} | {row['sh_ungated_n']} | {row['sh_ungated_net']} |"
        )

    oct_g = report["swing_high_gated"]["october"]
    may_g = report["swing_high_gated"]["may"]
    sep_g = report["swing_high_gated"]["september"]
    lines += [
        "",
        "## Read",
        "",
        f"- **October gated:** n={oct_g.get('n')} net={oct_g.get('net')} "
        f"wr={oct_g.get('wr')} days+={oct_g.get('days_pos')}/{oct_g.get('days')} "
        f"loo_neg={oct_g.get('loo_neg')}",
        f"- **May gated (prior OOS):** n={may_g.get('n')} net={may_g.get('net')} "
        f"wr={may_g.get('wr')}",
        f"- **September gated:** n={sep_g.get('n')} net={sep_g.get('net')} "
        f"wr={sep_g.get('wr')}",
        "",
    ]
    n_oct = oct_g.get("n") or 0
    net_oct = oct_g.get("net") or 0
    net_may = may_g.get("net") or 0
    if n_oct == 0:
        lines.append(
            "October produced **no** gated SWING_HIGH trades — stratum empty on this month."
        )
    elif net_oct > 0 and net_may > 0:
        lines.append(
            "October agrees in sign with May, but n remains tiny — still below ship bar."
        )
    elif net_oct <= 0:
        lines.append(
            "**October flips / fails** vs the May-looking SWING_HIGH story — "
            "treat prior May 5/5 as noise. Do not ship."
        )
    else:
        lines.append("Mixed — see tables. Do not ship on n alone.")

    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\nWrote {out}", flush=True)


if __name__ == "__main__":
    main()
