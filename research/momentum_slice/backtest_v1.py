"""Phase 3: entry/exit backtest for MOMENTUM_V1 and PC_V1 via mat_sim.

  .venv\\Scripts\\python.exe -m research.momentum_slice.backtest_v1
  .venv\\Scripts\\python.exe -m research.momentum_slice.backtest_v1 --features results/MOMENTUM_SLICE/holdout/features.csv --split holdout

Uses per-event level-based stops when available; reports train/holdout,
open/late, long/short. Freezes winning *structure* into RULE_SPEC_V1 appendix.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, merge_summaries, simulate_session, _rebuild_summary
from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.rules import (
    STOP_BUFFER_PTS, enrich, f, is_momentum_v1, is_pc_v1, momentum_stop_pts,
)

OUT = Path("results/MOMENTUM_SLICE/backtest_v1")


def load_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [enrich(r) for r in csv.DictReader(fh)]


def level_sl(r: dict, default: float = 10.0) -> float:
    sl = momentum_stop_pts(r)
    if sl is None or not np.isfinite(sl) or sl <= 0:
        return default
    return float(min(max(sl, 4.0), 40.0))  # clamp extremes


def run_variant(
    rows: list[dict],
    *,
    entry: str,
    fade: bool,
    tp: float,
    sl_mode: str,
    fixed_sl: float,
    time_stop: float,
    trail: float,
    commission: float = 0.25,
) -> dict:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["session"], []).append(r)
    sim_cfg = MatSimConfig(
        tp_pts=tp, sl_pts=fixed_sl, commission_pts_rt=commission,
        signal_cooldown_sec=30, time_stop_sec=time_stop, trail_pts=trail,
    )
    parts, splits = [], {"open": [], "late": [], "LONG": [], "SHORT": []}
    for sid, evs in sorted(by.items()):
        pair = load_pair(pq_dir, sid)
        if pair is None:
            continue
        ts, side, sls, tps = [], [], [], []
        for r in evs:
            if entry == "confirm":
                t = int(float(r["confirm_ns"]))
            elif entry == "confirm_plus_5s":
                t = int(float(r["confirm_ns"])) + 5_000_000_000
            else:
                t = int(float(r["confirm_ns"]))
            s = int(float(r["side_int"]))
            if fade:
                s = -s
            ts.append(t)
            side.append(s)
            if sl_mode == "level":
                sls.append(level_sl(r, fixed_sl))
            else:
                sls.append(fixed_sl)
            tps.append(tp)
        if not ts:
            continue
        tbl = pa.table({
            "event_ts_ns": np.array(ts, dtype=np.int64),
            "side": np.array(side, dtype=np.int8),
            "sl_pts": np.array(sls, dtype=np.float64),
            "tp_pts": np.array(tps, dtype=np.float64),
        })
        s = simulate_session(sid, pair.nq, pair.es, sim_cfg, events=tbl,
                             vwap_from_ns=pair.bounds[0])
        parts.append(s)
        meta = {int(float(r["confirm_ns"])) if entry == "confirm"
                else int(float(r["confirm_ns"])) + 5_000_000_000: r for r in evs}
        # match trades back — entry_ts may equal event_ts
        for tr in s.trades:
            r = meta.get(tr.entry_ts_ns)
            if r is None:
                # try nearest
                for k, rr in meta.items():
                    if abs(k - tr.entry_ts_ns) < 1_000_000:
                        r = rr; break
            if r is None:
                continue
            splits[r.get("tod", "late")].append(tr)
            splits[r.get("side", "LONG")].append(tr)
    merged = merge_summaries(parts, commission) if parts else None
    out = {"all": merged.to_dict() if merged else {"n": 0}}
    for k, trs in splits.items():
        out[k] = _rebuild_summary(trs, commission).to_dict()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default="results/MOMENTUM_SLICE/features/features.csv")
    ap.add_argument("--split", default="train", help="label written into report")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    rows = load_rows(Path(args.features))
    mom = [r for r in rows if is_momentum_v1(r)]
    pc = [r for r in rows if is_pc_v1(r)]
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)

    variants = []
    # Momentum: confirm entry
    for sl_mode, fixed_sl, tp, ts, trail, name in (
        ("level", 10, 16, 0, 0, "mom_confirm_levelSL_tp16"),
        ("level", 10, 10, 0, 0, "mom_confirm_levelSL_tp10"),
        ("level", 10, 24, 0, 0, "mom_confirm_levelSL_tp24"),
        ("level", 10, 16, 60, 0, "mom_confirm_levelSL_tp16_t60"),
        ("level", 10, 16, 0, 8, "mom_confirm_levelSL_tp16_trail8"),
        ("fixed", 8, 16, 0, 0, "mom_confirm_sl8_tp16"),
        ("fixed", 10, 10, 0, 0, "mom_confirm_sl10_tp10"),
    ):
        variants.append(("momentum", name, mom, dict(
            entry="confirm", fade=False, tp=tp, sl_mode=sl_mode,
            fixed_sl=fixed_sl, time_stop=ts, trail=trail)))
    # PC fade at +5s
    for sl_mode, fixed_sl, tp, ts, name in (
        ("level", 10, 16, 60, "pc_fade5_levelSL_tp16_t60"),
        ("level", 10, 10, 60, "pc_fade5_levelSL_tp10_t60"),
        ("fixed", 8, 16, 60, "pc_fade5_sl8_tp16_t60"),
        ("fixed", 10, 10, 60, "pc_fade5_sl10_tp10_t60"),
        ("level", 10, 16, 0, "pc_fade5_levelSL_tp16"),
    ):
        variants.append(("pc", name, pc, dict(
            entry="confirm_plus_5s", fade=True, tp=tp, sl_mode=sl_mode,
            fixed_sl=fixed_sl, time_stop=ts, trail=0)))

    report = {"split": args.split, "n_mom": len(mom), "n_pc": len(pc), "variants": {}}
    lines = [f"# Backtest V1 ({args.split})", "",
             f"MOMENTUM_V1 n={len(mom)}; PC_V1 n={len(pc)}", "",
             f"Stop buffer={STOP_BUFFER_PTS} pts beyond level/pitch.", ""]
    for family, name, pool, kw in variants:
        print(f"running {name} n={len(pool)}", flush=True)
        if not pool:
            report["variants"][name] = {"n": 0}
            continue
        res = run_variant(pool, **kw)
        report["variants"][name] = res
        a = res.get("all", {})
        lines.append(f"## {name}")
        lines.append(f"- all: n={a.get('n')} wr={a.get('wr')} pf={a.get('pf')} "
                     f"net={a.get('net')} maxDD={a.get('max_dd')}")
        for sub in ("late", "open", "LONG", "SHORT"):
            s = res.get(sub, {})
            if s.get("n"):
                lines.append(f"- {sub}: n={s.get('n')} wr={s.get('wr')} pf={s.get('pf')} net={s.get('net')}")
        lines.append("")

    # Pick winners by late PF then net (structure freeze hint)
    def score(v):
        late = v.get("late") or {}
        return (late.get("pf") or 0, late.get("net") or -1e9)

    mom_vars = {k: v for k, v in report["variants"].items() if k.startswith("mom_") and isinstance(v, dict) and v.get("late")}
    pc_vars = {k: v for k, v in report["variants"].items() if k.startswith("pc_") and isinstance(v, dict) and v.get("late")}
    best_mom = max(mom_vars, key=lambda k: score(mom_vars[k])) if mom_vars else None
    best_pc = max(pc_vars, key=lambda k: score(pc_vars[k])) if pc_vars else None
    report["best_momentum_structure"] = best_mom
    report["best_pc_structure"] = best_pc
    lines += ["## Structure freeze candidates (best late PF/net)",
              f"- momentum: **{best_mom}**",
              f"- pc: **{best_pc}**", ""]

    (out_dir / f"backtest_{args.split}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / f"backtest_{args.split}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"-> {out_dir}/backtest_{args.split}.{{json,md}}")


if __name__ == "__main__":
    main()
