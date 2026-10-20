"""EXP-0037 staged HV Extreme Move sweep.

Stages:
  1   — signal grid on train (54 cells), events cached
  1b  — clean-leg triage (post-filter) then exact re-run of shortlist
  2   — brackets on top 5 signal cells (train)
  3   — validate top 3 on hold-5; freeze
  4   — final test once on new-9

Usage:
  python -m research.hvext.run_hvext_sweep --stage 1
  python -m research.hvext.run_hvext_sweep --stage all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, fields
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    make_vol_bracket,
    make_vol_bracket_rr,
    simulate_session,
)
from research.features.hv_extreme import HvExtremeConfig, hv_extreme_events
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import _load_trades, _random_null_events, load_config

UK = ZoneInfo("Europe/London")
ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results" / "EXP-0037"
CACHE = RESULTS / "cache"

TRAIN = [
    "260326",
    "030826",
    "040826",
    "050826",
    "160926",
    "170926",
    "180926",
    "210926",
    "220926",
]
VALIDATE = ["011026", "021026", "051026", "061026", "071026"]
FINAL = [
    "081026",
    "091026",
    "121026",
    "131026",
    "141026",
    "151026",
    "161026",
    "290926",
    "300926",
]
RESERVE = [
    "191026",
    "250926",
    "280926",
    "010926",
    "020926",
    "040926",
    "080926",
    "100926",
    "150926",
]


@dataclass(frozen=True)
class SignalCell:
    move_pts: float
    confirm_pts: float
    peak_band_pts: float
    high_vol_mult: float
    min_path_efficiency: float = 0.0
    max_pullback_pts: float = 5.0
    max_leg_sec: float = 0.0
    max_leg_vol_vs_avg: float = 0.0  # 0 = off; Gate B
    max_leg_vol_vs_tip: float = 0.0  # 0 = off; Gate A
    lookback_min: float = 15.0
    cooldown_sec: float = 30.0

    def key(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def label(self) -> str:
        return (
            f"m{self.move_pts:g}_c{self.confirm_pts:g}_b{self.peak_band_pts:g}"
            f"_hv{self.high_vol_mult:g}_eff{self.min_path_efficiency:g}"
            f"_pb{self.max_pullback_pts:g}_leg{self.max_leg_sec:g}"
            f"_lv{self.max_leg_vol_vs_avg:g}_tip{self.max_leg_vol_vs_tip:g}"
        )

    def to_cfg(self) -> HvExtremeConfig:
        return HvExtremeConfig(
            move_pts=self.move_pts,
            confirm_pts=self.confirm_pts,
            peak_band_pts=self.peak_band_pts,
            high_vol_mult=self.high_vol_mult,
            min_path_efficiency=self.min_path_efficiency,
            max_pullback_pts=self.max_pullback_pts,
            max_leg_sec=self.max_leg_sec,
            max_leg_vol_vs_avg=self.max_leg_vol_vs_avg,
            max_leg_vol_vs_tip=self.max_leg_vol_vs_tip,
            lookback_min=self.lookback_min,
            cooldown_sec=self.cooldown_sec,
        )


def uk_bounds(mid_ns: int, start: time = time(14, 30), end: time = time(17, 0)) -> tuple[int, int]:
    d = datetime.fromtimestamp(mid_ns / 1e9, tz=timezone.utc).astimezone(UK).date()
    return (
        int(datetime.combine(d, start, tzinfo=UK).timestamp() * 1e9),
        int(datetime.combine(d, end, tzinfo=UK).timestamp() * 1e9),
    )


def load_nq_full(parquet_dir: Path, sid: str) -> pa.Table:
    return _load_trades(parquet_dir, sid, "NQ")


def events_for_session(
    parquet_dir: Path,
    sid: str,
    cell: SignalCell,
    *,
    use_cache: bool = True,
) -> pa.Table:
    cache_dir = CACHE / cell.key()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{sid}.parquet"
    meta_path = cache_dir / "cell.json"
    if not meta_path.exists():
        meta_path.write_text(json.dumps(asdict(cell), indent=2), encoding="utf-8")
    if use_cache and cache_path.exists():
        return pq.read_table(cache_path)

    nq = load_nq_full(parquet_dir, sid)
    if nq.num_rows == 0:
        ev = pa.table({})
        pq.write_table(ev, cache_path, compression="zstd")
        return ev
    ts = nq.column("ts_ns").to_numpy().astype(np.int64)
    bounds = uk_bounds(int(ts[len(ts) // 2]))
    ev = hv_extreme_events(nq, cell.to_cfg(), window_ns=bounds, attach_med_r30=True)
    pq.write_table(ev, cache_path, compression="zstd")
    return ev


def pack_summary(s: SimSummary) -> dict:
    days: dict[str, float] = {}
    for t in s.trades:
        days[t.session_id] = days.get(t.session_id, 0.0) + (t.pnl - 0.25)
    days_pos = sum(1 for v in days.values() if v > 0)
    n_long = sum(1 for t in s.trades if t.side > 0)
    n_short = sum(1 for t in s.trades if t.side < 0)
    net_long = sum((t.pnl - 0.25) for t in s.trades if t.side > 0)
    net_short = sum((t.pnl - 0.25) for t in s.trades if t.side < 0)
    return {
        **s.to_dict(),
        "n_sessions": len(days),
        "days_pos": days_pos,
        "days_pos_frac": (days_pos / len(days)) if days else float("nan"),
        "n_long": n_long,
        "n_short": n_short,
        "net_long": round(net_long, 2),
        "net_short": round(net_short, 2),
        "per_session": {k: round(v, 2) for k, v in sorted(days.items())},
    }


def simulate_cell(
    parquet_dir: Path,
    sessions: list[str],
    cell: SignalCell,
    bracket_fn,
    *,
    slip_ticks: float = 0.0,
    cooldown_sec: float = 30.0,
) -> SimSummary:
    cfg = MatSimConfig(
        signal_cooldown_sec=cooldown_sec,
        tp_pts=12.5,
        sl_pts=12.5,
        slip_ticks=slip_ticks,
        flat_only=True,
        commission_pts_rt=0.25,
        pts_per_tick=0.25,
    )
    all_trades = []
    for sid in sessions:
        nq = load_nq_full(parquet_dir, sid)
        if nq.num_rows == 0:
            continue
        # Full-session path for regime warm-up; events already UK-window filtered.
        ev = events_for_session(parquet_dir, sid, cell)
        if ev.num_rows == 0:
            continue
        summary = simulate_session(
            sid,
            nq,
            pa.table({}),
            cfg,
            bracket_fn=bracket_fn,
            events=ev,
            vol_window_min=30,
        )
        all_trades.extend(summary.trades)
    out = SimSummary()
    equity = peak = 0.0
    for t in all_trades:
        out.trades.append(t)
        out.n += 1
        if t.win:
            out.wins += 1
            out.gross_win += max(t.pnl, 0.0)
        else:
            out.gross_loss += abs(min(t.pnl, 0.0))
            if t.pnl > 0:
                out.gross_win += t.pnl
        out.gross += t.pnl
        net = t.pnl - 0.25
        out.commission_total += 0.25
        out.net += net
        equity += net
        peak = max(peak, equity)
        out.max_dd = max(out.max_dd, peak - equity)
    return out


def fixed_bracket(tp: float, sl: float):
    def fn(sv):
        return tp, sl

    return fn


def null_control(
    parquet_dir: Path,
    sessions: list[str],
    n_per_session: int,
    bracket_fn,
) -> SimSummary:
    cfg = MatSimConfig(
        signal_cooldown_sec=30,
        slip_ticks=0,
        flat_only=True,
        commission_pts_rt=0.25,
        pts_per_tick=0.25,
    )
    all_trades = []
    rng = np.random.default_rng(42)
    for sid in sessions:
        nq = load_nq_full(parquet_dir, sid)
        if nq.num_rows == 0:
            continue
        ts = nq.column("ts_ns").to_numpy().astype(np.int64)
        bounds = uk_bounds(int(ts[len(ts) // 2]))
        mask = (ts >= bounds[0]) & (ts <= bounds[1])
        if not mask.any():
            continue
        nq_w = nq.take(np.where(mask)[0])
        ev = _random_null_events(nq_w, n=n_per_session, seed=int(rng.integers(0, 1_000_000)))
        if ev.num_rows == 0:
            continue
        summary = simulate_session(
            sid, nq_w, pa.table({}), cfg, bracket_fn=bracket_fn, events=ev
        )
        all_trades.extend(summary.trades)
    out = SimSummary()
    for t in all_trades:
        out.trades.append(t)
        out.n += 1
        if t.win:
            out.wins += 1
            out.gross_win += max(t.pnl, 0.0)
        else:
            out.gross_loss += abs(min(t.pnl, 0.0))
        out.gross += t.pnl
        out.net += t.pnl - 0.25
        out.commission_total += 0.25
    return out


def signal_grid() -> list[SignalCell]:
    """Stage-1 shape grid — clean-leg gates OFF so stage-1b can post-filter columns."""
    cells = []
    for move in (15.0, 20.0, 25.0):
        for confirm in (5.0, 10.0, 15.0):
            for band in (2.0, 5.0):
                for hv in (1.5, 2.0, 3.0):
                    cells.append(
                        SignalCell(
                            move_pts=move,
                            confirm_pts=confirm,
                            peak_band_pts=band,
                            high_vol_mult=hv,
                            max_pullback_pts=0.0,
                            min_path_efficiency=0.0,
                            max_leg_sec=0.0,
                        )
                    )
    return cells


def passes_robustness(pack: dict, *, min_n: int = 60, min_pf: float = 1.2) -> bool:
    if pack["n"] < min_n:
        return False
    pf = pack.get("pf")
    if pf is None or (isinstance(pf, float) and not np.isfinite(pf)):
        return False
    if pf < min_pf:
        return False
    wr = pack.get("wr") or 0.0
    # rough: for 1:2.5 R:R need ~29% WR; for 12.5/30 ≈ 1:2.4
    if wr < 0.28:
        return False
    dpf = pack.get("days_pos_frac")
    if dpf is None or dpf < 0.55:
        return False
    net = pack.get("net") or 0.0
    dd = pack.get("max_dd") or 0.0
    if net <= 0:
        return False
    if dd > 0.4 * abs(net) and net > 0:
        return False
    per = pack.get("per_session") or {}
    if per and net > 0:
        top = max(per.values())
        if top > 0.4 * net:
            return False
    return True


def stage1(parquet_dir: Path) -> list[dict]:
    RESULTS.mkdir(parents=True, exist_ok=True)
    cells = signal_grid()
    print(f"[stage1] {len(cells)} signal cells × {len(TRAIN)} train sessions", flush=True)
    rows = []
    brackets = {
        "fixed_12p5_30": fixed_bracket(30.0, 12.5),
        "vol_rr_1_2p5": make_vol_bracket_rr(1.0, tp_r=2.5, sl_r=1.0, floor=4.0, cap=40.0),
    }
    for i, cell in enumerate(cells):
        print(f"[stage1] {i+1}/{len(cells)} {cell.label()}", flush=True)
        # warm event cache once
        for sid in TRAIN:
            events_for_session(parquet_dir, sid, cell)
        row = {"cell": cell.label(), "key": cell.key(), **asdict(cell)}
        for bname, bfn in brackets.items():
            s = simulate_cell(parquet_dir, TRAIN, cell, bfn)
            pack = pack_summary(s)
            for k, v in pack.items():
                if k == "per_session":
                    row[f"{bname}_per_session"] = v
                else:
                    row[f"{bname}_{k}"] = v
            row[f"{bname}_robust"] = passes_robustness(pack)
        rows.append(row)
    out_path = RESULTS / "stage1_cells.json"
    out_path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    # rank by fixed net then vol net
    ranked = sorted(
        rows,
        key=lambda r: (
            r.get("fixed_12p5_30_robust", False),
            1 if (r.get("fixed_12p5_30_n") or 0) >= 60 else 0,
            1 if (r.get("fixed_12p5_30_n") or 0) >= 40 else 0,
            r.get("fixed_12p5_30_net") or -1e9,
            r.get("vol_rr_1_2p5_net") or -1e9,
        ),
        reverse=True,
    )
    (RESULTS / "stage1_ranked.json").write_text(
        json.dumps(ranked[:20], indent=2, default=str), encoding="utf-8"
    )
    print(f"[stage1] wrote {out_path}; top={ranked[0]['cell'] if ranked else None}", flush=True)
    return ranked


def _post_filter_events(ev: pa.Table, pb: float, eff: float, leg: float) -> pa.Table:
    """Approximate clean-leg gates on cached columns (triage only)."""
    if ev.num_rows == 0:
        return ev
    mask = np.ones(ev.num_rows, dtype=bool)
    if pb > 0:
        pb_ticks = int(round(pb / 0.25))
        mask &= ev.column("max_pullback_ticks").to_numpy() <= pb_ticks
    if eff > 0:
        mask &= ev.column("path_eff").to_numpy() >= eff
    if leg > 0:
        mask &= ev.column("leg_sec").to_numpy() <= leg
    if mask.all():
        return ev
    return ev.take(np.where(mask)[0])


def stage1b(parquet_dir: Path, ranked: list[dict] | None = None) -> list[dict]:
    if ranked is None:
        ranked = json.loads((RESULTS / "stage1_ranked.json").read_text(encoding="utf-8"))
    seeds = ranked[:10]
    gate_grid = [
        (pb, eff, leg)
        for pb in (0.0, 5.0, 8.0)
        for eff in (0.0, 0.6, 0.75)
        for leg in (0.0, 60.0)
    ]
    print(f"[stage1b] post-filter triage {len(seeds)} seeds × {len(gate_grid)} gates", flush=True)
    bfn = fixed_bracket(30.0, 12.5)
    triage: list[dict] = []
    for seed in seeds:
        base = SignalCell(
            move_pts=seed["move_pts"],
            confirm_pts=seed["confirm_pts"],
            peak_band_pts=seed["peak_band_pts"],
            high_vol_mult=seed["high_vol_mult"],
            max_pullback_pts=0.0,
            min_path_efficiency=0.0,
            max_leg_sec=0.0,
        )
        for pb, eff, leg in gate_grid:
            # Simulate with post-filtered cached events (approximation).
            cfg = MatSimConfig(
                signal_cooldown_sec=30,
                slip_ticks=0,
                flat_only=True,
                commission_pts_rt=0.25,
                pts_per_tick=0.25,
            )
            all_trades = []
            for sid in TRAIN:
                nq = load_nq_full(parquet_dir, sid)
                if nq.num_rows == 0:
                    continue
                ev = _post_filter_events(
                    events_for_session(parquet_dir, sid, base), pb, eff, leg
                )
                if ev.num_rows == 0:
                    continue
                summary = simulate_session(
                    sid, nq, pa.table({}), cfg, bracket_fn=bfn, events=ev, vol_window_min=30
                )
                all_trades.extend(summary.trades)
            s = SimSummary()
            for t in all_trades:
                s.trades.append(t)
                s.n += 1
                if t.win:
                    s.wins += 1
                    s.gross_win += max(t.pnl, 0.0)
                else:
                    s.gross_loss += abs(min(t.pnl, 0.0))
                s.gross += t.pnl
                s.net += t.pnl - 0.25
                s.commission_total += 0.25
            pack = pack_summary(s)
            triage.append(
                {
                    "cell": (
                        f"m{base.move_pts:g}_c{base.confirm_pts:g}_b{base.peak_band_pts:g}"
                        f"_hv{base.high_vol_mult:g}_eff{eff:g}_pb{pb:g}_leg{leg:g}"
                    ),
                    "move_pts": base.move_pts,
                    "confirm_pts": base.confirm_pts,
                    "peak_band_pts": base.peak_band_pts,
                    "high_vol_mult": base.high_vol_mult,
                    "max_pullback_pts": pb,
                    "min_path_efficiency": eff,
                    "max_leg_sec": leg,
                    "triage_net": pack.get("net"),
                    "triage_n": pack.get("n"),
                    "triage_pf": pack.get("pf"),
                    "triage_robust": passes_robustness(pack),
                }
            )
    triage_ranked = sorted(
        triage,
        key=lambda r: (
            r.get("triage_robust", False),
            # Prefer adequate sample before raw net (avoids n=3 lottery cells).
            1 if (r.get("triage_n") or 0) >= 40 else 0,
            1 if (r.get("triage_n") or 0) >= 20 else 0,
            r.get("triage_net") or -1e9,
        ),
        reverse=True,
    )
    (RESULTS / "stage1b_triage.json").write_text(
        json.dumps(triage_ranked[:40], indent=2, default=str), encoding="utf-8"
    )
    # Exact in-loop re-run: prefer n>=20 triage rows, else fall back to top nets.
    shortlist = [r for r in triage_ranked if (r.get("triage_n") or 0) >= 20][:12]
    if len(shortlist) < 5:
        shortlist = triage_ranked[:12]
    print(f"[stage1b] exact re-run shortlist n={len(shortlist)}", flush=True)
    rows = []
    for seed in shortlist:
        cell = SignalCell(
            move_pts=seed["move_pts"],
            confirm_pts=seed["confirm_pts"],
            peak_band_pts=seed["peak_band_pts"],
            high_vol_mult=seed["high_vol_mult"],
            max_pullback_pts=seed["max_pullback_pts"],
            min_path_efficiency=seed["min_path_efficiency"],
            max_leg_sec=seed["max_leg_sec"],
        )
        s = simulate_cell(parquet_dir, TRAIN, cell, bfn)
        pack = pack_summary(s)
        rows.append(
            {
                "cell": cell.label(),
                "key": cell.key(),
                **asdict(cell),
                **{f"fixed_{k}": v for k, v in pack.items() if k != "per_session"},
                "fixed_per_session": pack["per_session"],
                "fixed_robust": passes_robustness(pack),
            }
        )
        print(
            f"  {cell.label()} n={pack['n']} net={pack['net']} pf={pack.get('pf')} "
            f"robust={passes_robustness(pack)}",
            flush=True,
        )
    ranked_b = sorted(
        rows,
        key=lambda r: (r.get("fixed_robust", False), r.get("fixed_net") or -1e9),
        reverse=True,
    )
    (RESULTS / "stage1b_ranked.json").write_text(
        json.dumps(ranked_b[:30], indent=2, default=str), encoding="utf-8"
    )
    return ranked_b


def stage2(parquet_dir: Path, ranked_b: list[dict] | None = None) -> list[dict]:
    if ranked_b is None:
        ranked_b = json.loads((RESULTS / "stage1b_ranked.json").read_text(encoding="utf-8"))
    top5 = [r for r in ranked_b if (r.get("fixed_n") or 0) >= 40][:5]
    if len(top5) < 3:
        top5 = ranked_b[:5]
    print(f"[stage2] brackets on {len(top5)} cells", flush=True)
    fixed_grid = [(sl, tp) for sl in (10.0, 12.5, 15.0) for tp in (20.0, 30.0, 40.0)]
    vol_grid = [(sl_r, tp_r) for sl_r in (1.0, 1.5) for tp_r in (2.0, 2.5, 3.0, 4.0)]
    rows = []
    for seed in top5:
        cell = SignalCell(**{f.name: seed[f.name] for f in fields(SignalCell)})
        for sl, tp in fixed_grid:
            s = simulate_cell(parquet_dir, TRAIN, cell, fixed_bracket(tp, sl))
            pack = pack_summary(s)
            rows.append(
                {
                    "cell": cell.label(),
                    "key": cell.key(),
                    "bracket": f"fixed_{sl:g}_{tp:g}",
                    "sl": sl,
                    "tp": tp,
                    **{f"m_{k}": v for k, v in pack.items() if k != "per_session"},
                    "m_per_session": pack["per_session"],
                    "m_robust": passes_robustness(pack),
                    **asdict(cell),
                }
            )
        for sl_r, tp_r in vol_grid:
            bfn = make_vol_bracket_rr(1.0, tp_r=tp_r, sl_r=sl_r, floor=4.0, cap=40.0)
            s = simulate_cell(parquet_dir, TRAIN, cell, bfn)
            pack = pack_summary(s)
            rows.append(
                {
                    "cell": cell.label(),
                    "key": cell.key(),
                    "bracket": f"vol_rr_{sl_r:g}_{tp_r:g}",
                    "sl_r": sl_r,
                    "tp_r": tp_r,
                    **{f"m_{k}": v for k, v in pack.items() if k != "per_session"},
                    "m_per_session": pack["per_session"],
                    "m_robust": passes_robustness(pack),
                    **asdict(cell),
                }
            )
        # symmetric vol ref
        s = simulate_cell(parquet_dir, TRAIN, cell, make_vol_bracket(1.0, 6.0, 30.0))
        pack = pack_summary(s)
        rows.append(
            {
                "cell": cell.label(),
                "key": cell.key(),
                "bracket": "vol_sym_k1",
                **{f"m_{k}": v for k, v in pack.items() if k != "per_session"},
                "m_per_session": pack["per_session"],
                "m_robust": passes_robustness(pack),
                **asdict(cell),
            }
        )
    ranked = sorted(
        rows,
        key=lambda r: (r.get("m_robust", False), r.get("m_net") or -1e9),
        reverse=True,
    )
    (RESULTS / "stage2_ranked.json").write_text(
        json.dumps(ranked[:40], indent=2, default=str), encoding="utf-8"
    )
    print(f"[stage2] top={ranked[0]['cell']} {ranked[0]['bracket']} net={ranked[0].get('m_net')}", flush=True)
    return ranked


def _cell_from_row(row: dict) -> SignalCell:
    return SignalCell(**{f.name: row[f.name] for f in fields(SignalCell)})


def _bracket_fn_from_row(row: dict):
    b = row["bracket"]
    if b.startswith("fixed_"):
        return fixed_bracket(float(row["tp"]), float(row["sl"]))
    if b.startswith("vol_rr_"):
        return make_vol_bracket_rr(
            1.0, tp_r=float(row["tp_r"]), sl_r=float(row["sl_r"]), floor=4.0, cap=40.0
        )
    if b == "vol_sym_k1":
        return make_vol_bracket(1.0, 6.0, 30.0)
    raise ValueError(b)


def stage3(parquet_dir: Path, ranked2: list[dict] | None = None) -> dict:
    if ranked2 is None:
        ranked2 = json.loads((RESULTS / "stage2_ranked.json").read_text(encoding="utf-8"))
    top3 = ranked2[:3]
    print(f"[stage3] validate {len(top3)} on hold-5", flush=True)
    results = []
    for row in top3:
        cell = _cell_from_row(row)
        bfn = _bracket_fn_from_row(row)
        train = pack_summary(simulate_cell(parquet_dir, TRAIN, cell, bfn))
        hold = pack_summary(simulate_cell(parquet_dir, VALIDATE, cell, bfn))
        results.append(
            {
                "cell": cell.label(),
                "bracket": row["bracket"],
                "key": cell.key(),
                "row": row,
                "train": train,
                "validate": hold,
                "train_robust": passes_robustness(train),
                "validate_ok": hold["n"] >= 15
                and (hold.get("net") or 0) > 0
                and (hold.get("pf") or 0) >= 1.0,
            }
        )
        print(
            f"  {cell.label()} {row['bracket']} train_net={train['net']} "
            f"val_net={hold['net']} val_ok={results[-1]['validate_ok']}",
            flush=True,
        )
    # Prefer validate_ok then train net
    results.sort(
        key=lambda r: (r["validate_ok"], r["train_robust"], r["train"].get("net") or -1e9),
        reverse=True,
    )
    frozen = results[0]
    backup = results[1] if len(results) > 1 else None
    out = {
        "frozen": {
            "cell": frozen["cell"],
            "bracket": frozen["bracket"],
            "signal": asdict(_cell_from_row(frozen["row"])),
            "bracket_row": {
                k: frozen["row"].get(k)
                for k in ("bracket", "sl", "tp", "sl_r", "tp_r")
                if k in frozen["row"] or frozen["row"].get(k) is not None
            },
            "train": frozen["train"],
            "validate": frozen["validate"],
        },
        "backup": None
        if backup is None
        else {
            "cell": backup["cell"],
            "bracket": backup["bracket"],
            "signal": asdict(_cell_from_row(backup["row"])),
            "train": backup["train"],
            "validate": backup["validate"],
        },
        "candidates": results,
    }
    # clean bracket_row
    br = out["frozen"]["bracket_row"]
    out["frozen"]["bracket_row"] = {k: v for k, v in br.items() if v is not None}
    (RESULTS / "frozen.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"[stage3] FROZEN {out['frozen']['cell']} {out['frozen']['bracket']}", flush=True)
    return out


def stage4(parquet_dir: Path, frozen: dict | None = None) -> dict:
    if frozen is None:
        frozen = json.loads((RESULTS / "frozen.json").read_text(encoding="utf-8"))
    sig = frozen["frozen"]["signal"]
    cell = SignalCell(**sig)
    # rebuild bracket from frozen row
    br = frozen["frozen"]["bracket_row"]
    bname = frozen["frozen"]["bracket"]
    row = {"bracket": bname, **br, **sig}
    bfn = _bracket_fn_from_row(row)
    print(f"[stage4] FINAL TEST {cell.label()} {bname} on {FINAL}", flush=True)
    train = pack_summary(simulate_cell(parquet_dir, TRAIN, cell, bfn))
    validate = pack_summary(simulate_cell(parquet_dir, VALIDATE, cell, bfn))
    test = pack_summary(simulate_cell(parquet_dir, FINAL, cell, bfn))
    # slip sensitivity
    test_slip1 = pack_summary(
        simulate_cell(parquet_dir, FINAL, cell, bfn, slip_ticks=1.0)
    )
    # null
    null_n = max(20, test["n"] // max(1, len(FINAL)))
    null_s = pack_summary(
        null_control(parquet_dir, FINAL, null_n, bfn)
    )
    ship = (
        (test.get("net") or 0) > 0
        and (test.get("pf") or 0) >= 1.2
        and (test.get("days_pos_frac") or 0) >= 0.55
    )
    # Event-study labels on final-test fires (hit +10/+20/+30, MFE/MAE).
    label_stats = _label_stats(parquet_dir, FINAL, cell)
    report = {
        "frozen": frozen["frozen"],
        "train": train,
        "validate": validate,
        "final_test": test,
        "final_test_slip1": test_slip1,
        "null_final": null_s,
        "label_events": label_stats,
        "ship": ship,
        "reserve_sessions": RESERVE,
    }
    (RESULTS / "final_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    _write_cells_and_trades(report, cell, bfn, parquet_dir)
    _write_summary_md(report)
    print(
        f"[stage4] test net={test['net']} pf={test.get('pf')} "
        f"days+={test.get('days_pos_frac')} ship={ship}",
        flush=True,
    )
    return report


def _label_stats(parquet_dir: Path, sessions: list[str], cell: SignalCell) -> dict:
    hits = {10: 0, 20: 0, 30: 0}
    n = 0
    mfe_all: list[float] = []
    mae_all: list[float] = []
    ret60: list[float] = []
    ret120: list[float] = []
    for sid in sessions:
        nq = load_nq_full(parquet_dir, sid)
        if nq.num_rows == 0:
            continue
        ev = events_for_session(parquet_dir, sid, cell)
        if ev.num_rows == 0:
            continue
        path_ts, path_px = build_price_path(nq, pts_per_tick=0.25)
        lab = label_events(
            ev,
            path_ts,
            path_px,
            horizons_ms=[60_000, 120_000],
            point_targets=[10.0, 20.0, 30.0],
        )
        if lab.num_rows == 0:
            continue
        n += lab.num_rows
        for tgt in (10, 20, 30):
            col = f"hit_{tgt}pts"
            if col in lab.column_names:
                hits[tgt] += int(np.nansum(lab.column(col).to_numpy()))
        if "mfe_pts" in lab.column_names:
            mfe_all.extend(lab.column("mfe_pts").to_numpy().tolist())
        if "mae_pts" in lab.column_names:
            mae_all.extend(lab.column("mae_pts").to_numpy().tolist())
        if "ret_pts_60000ms" in lab.column_names:
            ret60.extend(lab.column("ret_pts_60000ms").to_numpy().tolist())
        if "ret_pts_120000ms" in lab.column_names:
            ret120.extend(lab.column("ret_pts_120000ms").to_numpy().tolist())
    return {
        "n": n,
        "hit_10_rate": (hits[10] / n) if n else None,
        "hit_20_rate": (hits[20] / n) if n else None,
        "hit_30_rate": (hits[30] / n) if n else None,
        "mfe_med": float(np.nanmedian(mfe_all)) if mfe_all else None,
        "mae_med": float(np.nanmedian(mae_all)) if mae_all else None,
        "ret_60s_med": float(np.nanmedian(ret60)) if ret60 else None,
        "ret_120s_med": float(np.nanmedian(ret120)) if ret120 else None,
    }


def _write_cells_and_trades(
    report: dict, cell: SignalCell, bfn, parquet_dir: Path
) -> None:
    """Persist cells.parquet + trades.parquet for the frozen config across cohorts."""
    rows = []
    trade_rows = []
    for cohort, sessions in (("train", TRAIN), ("validate", VALIDATE), ("final_test", FINAL)):
        s = simulate_cell(parquet_dir, sessions, cell, bfn)
        pack = pack_summary(s)
        rows.append(
            {
                "cohort": cohort,
                "cell": cell.label(),
                **{k: v for k, v in pack.items() if k != "per_session"},
            }
        )
        for t in s.trades:
            trade_rows.append(
                {
                    "cohort": cohort,
                    "session_id": t.session_id,
                    "side": t.side,
                    "entry_ts_ns": t.entry_ts_ns,
                    "exit_ts_ns": t.exit_ts_ns,
                    "entry_px": t.entry_px,
                    "exit_px": t.exit_px,
                    "tp": t.tp_pts,
                    "sl": t.sl_pts,
                    "pnl": t.pnl,
                    "win": bool(t.win),
                    "reason": t.reason,
                }
            )
    if rows:
        pq.write_table(pa.Table.from_pylist(rows), RESULTS / "cells.parquet", compression="zstd")
    if trade_rows:
        pq.write_table(
            pa.Table.from_pylist(trade_rows), RESULTS / "trades.parquet", compression="zstd"
        )


def _write_summary_md(report: dict) -> None:
    f = report["frozen"]
    t, v, te = report["train"], report["validate"], report["final_test"]
    lines = [
        "# EXP-0037 — HV Extreme Move",
        "",
        f"**Frozen signal:** `{f['cell']}`",
        f"**Frozen bracket:** `{f['bracket']}`",
        "",
        "## Signal params",
        "```json",
        json.dumps(f["signal"], indent=2),
        "```",
        "",
        "## Bracket",
        "```json",
        json.dumps(f["bracket_row"], indent=2),
        "```",
        "",
        "## Cohort P&L (UK 14:30–17:00, commission 0.25 rt, flat-only, cooldown 30s)",
        "",
        "| Cohort | n | WR | PF | Net | Days+ | MaxDD |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, pack in (("train", t), ("validate", v), ("final_test", te)):
        lines.append(
            f"| {name} | {pack['n']} | {pack.get('wr')} | {pack.get('pf')} | "
            f"{pack.get('net')} | {pack.get('days_pos')}/{pack.get('n_sessions')} | "
            f"{pack.get('max_dd')} |"
        )
    lines += [
        "",
        f"**Ship threshold met:** {report['ship']}",
        "",
        f"Slip+1 tick (final): net={report['final_test_slip1'].get('net')} "
        f"pf={report['final_test_slip1'].get('pf')}",
        f"Null control (final): net={report['null_final'].get('net')} "
        f"pf={report['null_final'].get('pf')} n={report['null_final'].get('n')}",
        "",
        f"**Reserve (untouched):** {', '.join(RESERVE)}",
        "",
        "## Event-study (final test fires)",
        "```json",
        json.dumps(report.get("label_events", {}), indent=2),
        "```",
        "",
        "## Per-session final test",
        "```json",
        json.dumps(te.get("per_session", {}), indent=2),
        "```",
        "",
        "## Long vs short (final test)",
        f"- long n={te.get('n_long')} net={te.get('net_long')}",
        f"- short n={te.get('n_short')} net={te.get('net_short')}",
        "",
    ]
    (RESULTS / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--stage",
        default="all",
        choices=["1", "1b", "2", "3", "4", "all"],
    )
    args = ap.parse_args(argv)
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    RESULTS.mkdir(parents=True, exist_ok=True)

    if args.stage in ("1", "all"):
        ranked = stage1(parquet_dir)
    else:
        ranked = None
    if args.stage in ("1b", "all"):
        ranked_b = stage1b(parquet_dir, ranked)
    else:
        ranked_b = None
    if args.stage in ("2", "all"):
        ranked2 = stage2(parquet_dir, ranked_b)
    else:
        ranked2 = None
    if args.stage in ("3", "all"):
        frozen = stage3(parquet_dir, ranked2)
    else:
        frozen = None
    if args.stage in ("4", "all"):
        stage4(parquet_dir, frozen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
