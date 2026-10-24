"""EXP-0047 — MAT confirm at causal NQ LVN fade (asym R:R).

Level-first: LVN touch (from below → short / from above → long), then MAT
causal same-side within confirm window. Arms: no gates vs trend+VWAP.
Brackets: sym_k1, rr_1_2, rr_1_3, struct_1_2.
Train = pre-May; OOS = May + August reported separately.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
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
from research.events.lvn import TouchConfig, detect_lvn_touches
from research.features.momentum import momentum_burst_events, trades_to_tape_bars
from research.features.volume_profile import VolumeProfileConfig, causal_lvn_snapshots
from research.lvn_mat.confirm import mat_confirm_lvn_touches
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25
WINDOWS = (30, 60, 120, 300)
PRIMARY_WIN = 60
MAY = {
    "010526", "040526", "050526", "070526", "080526",
    "110526", "120526", "130526", "140526", "150526",
    "180526", "190526", "200526", "210526", "260526",
    "280526", "290526",
}
AUG = {
    "030826", "040826", "050826", "060826", "070826",
    "100826", "110826", "120826", "130826", "140826",
    "240826", "250826", "260826", "270826", "280826",
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


def cohort(sid: str) -> str:
    if sid in MAY:
        return "may"
    if sid in AUG:
        return "aug"
    return "train"


def pack(s: SimSummary) -> dict:
    d = s.to_dict()
    d["avg"] = round(s.net / s.n, 3) if s.n else None
    return d


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0047"
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name.replace("session_id=", "")
        for p in pq.iterdir()
        if p.is_dir() and p.name.startswith("session_id=")
        and (p / "trades.parquet").exists()
        and "smoke" not in p.name
    )
    print(f"sessions: {len(sessions)}", flush=True)

    vp_cfg = VolumeProfileConfig(
        lookback_ms=1_800_000,
        refresh_ms=30_000,
        lvn_frac_of_peak=0.40,
        require_hvn_sandwich=False,
        min_profile_volume=0.0,
        edge_margin_ticks=0,
    )
    touch_cfg = TouchConfig(touch_cooldown_ms=5000)
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100,
    )
    sim_cfg = MatSimConfig(
        slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal",
    )
    gate_none = None
    gate_tv = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    brackets = {
        "sym_k1": make_vol_bracket(1.0),
        "rr_1_2": make_vol_bracket_rr(1.0, tp_r=2, sl_r=1, floor=4.0, cap=40.0),
        "rr_1_3": make_vol_bracket_rr(1.0, tp_r=3, sl_r=1, floor=4.0, cap=40.0),
    }

    # Precompute per-session touches + bursts
    sess: dict[str, dict] = {}
    for sid in sessions:
        nq0 = _prefer_single_feed(_load_trades(pq, sid, "NQ"), pq, sid, "NQ")
        es0 = _prefer_single_feed(_load_trades(pq, sid, "ES"), pq, sid, "ES")
        nq = filt(nq0)
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows == 0:
            continue
        mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
        vf = bounds(mid)[0]
        snaps = causal_lvn_snapshots(nq, vp_cfg)
        touches = detect_lvn_touches(nq, snaps, touch_cfg)
        nq_bars = trades_to_tape_bars(nq, bar_ms=100)
        es_bars = trades_to_tape_bars(es, bar_ms=100) if es.num_rows else pa.table({})
        nq_b = momentum_burst_events(nq_bars, **burst_kw)
        es_b = (
            momentum_burst_events(es_bars, **burst_kw)
            if es_bars.num_rows else pa.table({})
        )
        n_from_below = int((touches.column("approach").to_numpy() == 1).sum()) if touches.num_rows else 0
        n_from_above = int((touches.column("approach").to_numpy() == -1).sum()) if touches.num_rows else 0
        sess[sid] = {
            "nq": nq, "es": es, "vf": vf, "touches": touches,
            "nq_b": nq_b, "es_b": es_b,
            "n_touch": touches.num_rows,
            "n_from_below": n_from_below,
            "n_from_above": n_from_above,
            "cohort": cohort(sid),
        }
        print(
            f"{sid} ({cohort(sid)}): touches={touches.num_rows} "
            f"below→short={n_from_below} above→long={n_from_above}",
            flush=True,
        )

    def run_arm(
        *,
        window: int,
        gate_name: str,
        gate,
        bracket_name: str,
        bracket_fn,
        struct: bool = False,
        require_near: bool = True,
        approach: int | None = None,
    ) -> dict:
        parts = []
        n_touch = 0
        n_conf = 0
        for sid, sl in sess.items():
            tev = sl["touches"]
            n_touch += tev.num_rows
            if tev.num_rows == 0:
                continue
            if approach is not None:
                ap = tev.column("approach").to_numpy()
                tev = tev.take(np.where(ap == approach)[0])
                if tev.num_rows == 0:
                    continue
            conf = mat_confirm_lvn_touches(
                tev, sl["nq_b"], sl["es_b"], sl["nq"],
                confirm_window_sec=float(window),
                near_lvn_pts=5.0,
                require_near=require_near,
            )
            n_conf += conf.num_rows
            if conf.num_rows == 0:
                continue
            if struct:
                # SL = |entry − lvn_mid| + 1 clipped; TP = 2×
                n = conf.num_rows
                swing = conf.column("swing_px").to_numpy()
                ts = sl["nq"].column("ts_ns").to_numpy().astype(np.int64)
                px = sl["nq"].column("price").to_numpy().astype(np.float64) * 0.25
                order = np.argsort(ts)
                ts, px = ts[order], px[order]
                ev_ts = conf.column("event_ts_ns").to_numpy().astype(np.int64)
                sl_arr = np.empty(n)
                tp_arr = np.empty(n)
                for i in range(n):
                    ei = int(np.searchsorted(ts, int(ev_ts[i]), side="left"))
                    if ei >= len(ts):
                        sl_arr[i] = np.nan
                        tp_arr[i] = np.nan
                        continue
                    risk = float(np.clip(abs(float(px[ei]) - float(swing[i])) + 1.0, 4.0, 40.0))
                    sl_arr[i] = risk
                    tp_arr[i] = 2.0 * risk
                conf = conf.append_column("tp_pts", pa.array(tp_arr))
                conf = conf.append_column("sl_pts", pa.array(sl_arr))
                br = None
            else:
                br = bracket_fn
            parts.append(simulate_session(
                sid, sl["nq"], sl["es"], sim_cfg, gate=gate,
                bracket_fn=br, vwap_from_ns=sl["vf"], events=conf,
            ))
        merged = merge_summaries(parts, COMMISSION) if parts else SimSummary()
        trn = filter_summary(merged, lambda t: t.session_id not in MAY and t.session_id not in AUG, COMMISSION)
        may = filter_summary(merged, lambda t: t.session_id in MAY, COMMISSION)
        aug = filter_summary(merged, lambda t: t.session_id in AUG, COMMISSION)
        return {
            "gate": gate_name,
            "window": window,
            "bracket": bracket_name,
            "require_near": require_near,
            "approach": approach,
            "n_touches": n_touch,
            "n_confirmed": n_conf,
            "all": pack(merged),
            "train": pack(trn),
            "may": pack(may),
            "aug": pack(aug),
        }

    report: dict = {"sanity": [
        {"session": sid, **{k: v for k, v in sl.items() if k in (
            "cohort", "n_touch", "n_from_below", "n_from_above"
        )}}
        for sid, sl in sess.items()
    ]}
    cells = {}

    # Headline matrix @ 60s
    for gate_name, gate in (("none", gate_none), ("trend_vwap", gate_tv)):
        for br_name, br_fn in brackets.items():
            key = f"w{PRIMARY_WIN}_{gate_name}_{br_name}"
            cells[key] = run_arm(
                window=PRIMARY_WIN, gate_name=gate_name, gate=gate,
                bracket_name=br_name, bracket_fn=br_fn,
            )
            print(
                f"{key}: conf={cells[key]['n_confirmed']} "
                f"train n={cells[key]['train']['n']} net={cells[key]['train'].get('net')} "
                f"may n={cells[key]['may']['n']} net={cells[key]['may'].get('net')}",
                flush=True,
            )
        # struct on primary window both gates
        key = f"w{PRIMARY_WIN}_{gate_name}_struct_1_2"
        cells[key] = run_arm(
            window=PRIMARY_WIN, gate_name=gate_name, gate=gate,
            bracket_name="struct_1_2", bracket_fn=None, struct=True,
        )
        print(f"{key}: train n={cells[key]['train']['n']} net={cells[key]['train'].get('net')}", flush=True)

    # Window sweep: rr_1_2, no gates (S/R-style) + trend_vwap
    for w in WINDOWS:
        for gate_name, gate in (("none", gate_none), ("trend_vwap", gate_tv)):
            key = f"w{w}_{gate_name}_rr_1_2"
            if key in cells:
                continue
            cells[key] = run_arm(
                window=w, gate_name=gate_name, gate=gate,
                bracket_name="rr_1_2", bracket_fn=brackets["rr_1_2"],
            )

    # Approach split @ 60s none + rr_1_2
    for ap, aname in ((1, "from_below_short"), (-1, "from_above_long")):
        key = f"w60_none_rr_1_2_{aname}"
        cells[key] = run_arm(
            window=60, gate_name="none", gate=gate_none,
            bracket_name="rr_1_2", bracket_fn=brackets["rr_1_2"],
            approach=ap,
        )

    # Without near filter (MAT anywhere after touch within window)
    cells["w60_none_rr_1_2_any_dist"] = run_arm(
        window=60, gate_name="none", gate=gate_none,
        bracket_name="rr_1_2", bracket_fn=brackets["rr_1_2"],
        require_near=False,
    )

    report["cells"] = cells

    # MAT base comparator (same sessions) for context
    base = {}
    for gate_name, gate in (("none", gate_none), ("trend_vwap", gate_tv)):
        parts = []
        for sid, sl in sess.items():
            parts.append(simulate_session(
                sid, sl["nq"], sl["es"], sim_cfg, gate=gate,
                bracket_fn=make_vol_bracket(1.0), vwap_from_ns=sl["vf"],
            ))
        m = merge_summaries(parts, COMMISSION)
        base[gate_name] = {
            "all": pack(m),
            "train": pack(filter_summary(m, lambda t: t.session_id not in MAY and t.session_id not in AUG, COMMISSION)),
            "may": pack(filter_summary(m, lambda t: t.session_id in MAY, COMMISSION)),
            "aug": pack(filter_summary(m, lambda t: t.session_id in AUG, COMMISSION)),
        }
    report["mat_base"] = base

    md = [
        "# EXP-0047 — MAT confirm at causal LVN fade",
        "",
        "NQ causal LVN touch (EXP-0011-style profile, no sandwich) → MAT causal "
        "same-side within confirm window; entry near LVN (±5 pts). "
        "UK 14:30–17:00, 1-tick, 0.25 pt/RT. May + Aug = OOS.",
        "",
        "User geometry: cross LVN from below → MAT sell for reversal (and mirror).",
        "",
        "## MAT base (context)",
        "",
        "| gates | cohort | n | avg | net | PF |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for gn, block in base.items():
        for co in ("train", "may", "aug"):
            r = block[co]
            md.append(
                f"| {gn} | {co} | {r['n']} | {r.get('avg')} | {r.get('net')} | {r.get('pf')} |"
            )

    md.extend([
        "",
        "## Headline @ 60s (near LVN)",
        "",
        "| cell | cohort | touches→conf | n | WR | avg | net | PF | maxDD |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    headline_keys = [
        k for k in sorted(cells)
        if k.startswith("w60_") and "from_" not in k and "any_dist" not in k
    ]
    for key in headline_keys:
        c = cells[key]
        for co in ("train", "may", "aug"):
            r = c[co]
            md.append(
                f"| {key} | {co} | {c['n_confirmed']} | {r['n']} | {r.get('wr')} | "
                f"{r.get('avg')} | {r.get('net')} | {r.get('pf')} | {r.get('max_dd')} |"
            )

    md.extend([
        "",
        "## Confirm window sweep (rr_1_2)",
        "",
        "| window | gates | cohort | n | avg | net | PF |",
        "|---:|---|---|---:|---:|---:|---:|",
    ])
    for w in WINDOWS:
        for gn in ("none", "trend_vwap"):
            c = cells[f"w{w}_{gn}_rr_1_2"]
            for co in ("train", "may"):
                r = c[co]
                md.append(
                    f"| {w} | {gn} | {co} | {r['n']} | {r.get('avg')} | "
                    f"{r.get('net')} | {r.get('pf')} |"
                )

    md.extend([
        "",
        "## Approach split @ 60s none rr_1_2",
        "",
        "| approach | cohort | n | avg | net | PF |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for aname in ("from_below_short", "from_above_long"):
        c = cells[f"w60_none_rr_1_2_{aname}"]
        for co in ("train", "may", "aug"):
            r = c[co]
            md.append(
                f"| {aname} | {co} | {r['n']} | {r.get('avg')} | {r.get('net')} | {r.get('pf')} |"
            )

    anyd = cells["w60_none_rr_1_2_any_dist"]
    md.extend([
        "",
        "## Near filter off @ 60s none rr_1_2",
        "",
        f"- confirmed={anyd['n_confirmed']}",
        f"- train n={anyd['train']['n']} avg={anyd['train'].get('avg')} net={anyd['train'].get('net')}",
        f"- may n={anyd['may']['n']} avg={anyd['may'].get('avg')} net={anyd['may'].get('net')}",
        "",
    ])

    # Verdict: primary = w60_none_rr_1_2 (S/R paradigm, asym)
    prim = cells["w60_none_rr_1_2"]
    prim_g = cells["w60_trend_vwap_rr_1_2"]
    md.extend([
        "## Verdict",
        "",
        f"- Primary (no gates, rr_1_2 @60s): train n={prim['train']['n']} "
        f"avg={prim['train'].get('avg')} net={prim['train'].get('net')} | "
        f"may n={prim['may']['n']} avg={prim['may'].get('avg')} net={prim['may'].get('net')} | "
        f"aug n={prim['aug']['n']} avg={prim['aug'].get('avg')} net={prim['aug'].get('net')}",
        f"- Same + trend/VWAP: train n={prim_g['train']['n']} "
        f"avg={prim_g['train'].get('avg')} net={prim_g['train'].get('net')} | "
        f"may n={prim_g['may']['n']} avg={prim_g['may'].get('avg')} net={prim_g['may'].get('net')}",
        "",
    ])
    t, m = prim["train"], prim["may"]
    ok_train = (
        t["n"] >= 30
        and t.get("avg") is not None
        and t["avg"] > 0
        and (t.get("pf") or 0) > 1.0
    )
    may_ok = m.get("avg") is not None and m["avg"] > 0
    if ok_train and may_ok:
        md.append("**Ship candidate:** train clears and May agrees in sign.")
    elif ok_train:
        md.append("**Train only — May disagrees or thin. Do not ship.**")
    elif t["n"] < 30:
        md.append("**Reject / thin:** n < 30 on train primary cell.")
    else:
        md.append("**Reject / park:** primary cell not clearly +EV on train.")
    md.append("")

    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
