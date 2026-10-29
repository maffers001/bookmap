"""Regenerate MAT / joint equity curves after Bookmap-parity fill fix.

Builds the MAT feature cache once, then rewrites:
  - results/MAT_CONTEXT_EQUITY (0075 + 0068/0073)
  - results/JOINT_CFADE_MAT075_ESLVN
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import simulate_session
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated.study_combined_book import _plot_equity
from research.features.corr_absorb import AbsorbParams, absorption_bars
from research.features.momentum import trades_to_tape_bars
from research.mat_context import cache as cache_mod
from research.mat_context import plot_0075_equity as plot75
from research.mat_context import plot_joint_cfade_mat_eslvn as plot_joint
from research.mat_context.plot_0075_equity import _day_nets, _overlay, _path
from research.mat_context.run_queue import _resim_masked
from research.run_cfade_all_months import _sid_chrono_key


def _write_0068_0073(cache, sessions: list[str], out: Path) -> None:
    print("Re-sim EXP-0068 rot_fresh_0.90…", flush=True)
    sims68 = _resim_masked(cache, "rot_fresh_0.90")
    path68 = _path(_day_nets(sims68), sessions)
    tr68 = [t for s in sims68.values() for t in s.trades]
    pe68 = path68[-1]["equity"] if path68 else 0.0
    peak = dd68 = 0.0
    for r in path68:
        peak = max(peak, r["equity"])
        dd68 = max(dd68, peak - r["equity"])
    _plot_equity(path68, out / "equity_0068_rot_fresh.svg", "EXP-0068 rot_fresh_0.90")
    print(f"  0068: n={len(tr68)} net={pe68:+.1f} maxDD={dd68:.1f}", flush=True)

    print("Re-sim EXP-0073 nq_absorb_exit…", flush=True)
    p_abs = AbsorbParams()
    sims73: dict = {}
    for sid in cache.sessions:
        b = cache.bundles[sid]
        nq_b = trades_to_tape_bars(b.nq, bar_ms=100)
        nq_abs = absorption_bars(nq_b, p_abs, market="NQ") if nq_b.num_rows else pa.table({})
        if nq_abs.num_rows == 0 or b.events.num_rows == 0:
            sims73[sid] = b.sim
            continue
        abs_ts = nq_abs.column("event_ts_ns").to_numpy().astype(np.int64)
        aggr = nq_abs.column("aggr_side").to_numpy().astype(int)

        def make_exit(abs_ts=abs_ts, aggr=aggr):
            def exit_fn(entry_ts_ns, side, row):
                lo = int(np.searchsorted(abs_ts, entry_ts_ns, side="right"))
                for j in range(lo, len(abs_ts)):
                    if int(aggr[j]) == -int(side):
                        return int(abs_ts[j]), "absorb_exit"
                return None

            return exit_fn

        ev = b.events
        sims73[sid] = simulate_session(
            sid, b.nq, b.es, F.mat_sim_cfg(),
            gate=F.mat_frozen_gate(), bracket_fn=F.mat_vol_bracket(),
            vwap_from_ns=b.vwap_from_ns,
            events=pa.table({
                "event_ts_ns": ev.column("event_ts_ns"),
                "side": ev.column("side"),
            }),
            exit_fn=make_exit(),
        )
    path73 = _path(_day_nets(sims73), sessions)
    tr73 = [t for s in sims73.values() for t in s.trades]
    pe73 = path73[-1]["equity"] if path73 else 0.0
    peak = dd73 = 0.0
    for r in path73:
        peak = max(peak, r["equity"])
        dd73 = max(dd73, peak - r["equity"])
    _plot_equity(path73, out / "equity_0073_absorb_exit.svg", "EXP-0073 nq_absorb_exit")
    print(f"  0073: n={len(tr73)} net={pe73:+.1f} maxDD={dd73:.1f}", flush=True)

    _overlay(
        {"rot_fresh_0.90": path68, "nq_absorb_exit": path73},
        out / "equity_overlay_0068_0073.svg",
        "MAT context PASS arms — 0068 rot_fresh vs 0073 absorb",
    )
    wr68 = (sum(1 for t in tr68 if (t.pnl - F.COMMISSION) > 0) / len(tr68)) if tr68 else None
    wr73 = (sum(1 for t in tr73 if (t.pnl - F.COMMISSION) > 0) / len(tr73)) if tr73 else None
    md = [
        "# MAT context PASS arms — equity",
        "",
        "Bookmap-parity fills; 0.25 pt/RT commission.",
        "",
        "| arm | n | WR | net | max DD | days+ |",
        "|---|---:|---:|---:|---:|---:|",
        f"| EXP-0068 rot_fresh_0.90 | {len(tr68)} | "
        f"{None if wr68 is None else round(wr68, 3)} | {pe68:+.1f} | {dd68:.1f} | "
        f"{sum(1 for r in path68 if r['day_net'] > 0)} |",
        f"| EXP-0073 nq_absorb_exit | {len(tr73)} | "
        f"{None if wr73 is None else round(wr73, 3)} | {pe73:+.1f} | {dd73:.1f} | "
        f"{sum(1 for r in path73 if r['day_net'] > 0)} |",
        "",
        "![0068](equity_0068_rot_fresh.svg)",
        "",
        "![0073](equity_0073_absorb_exit.svg)",
        "",
        "![overlay](equity_overlay_0068_0073.svg)",
        "",
    ]
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


def main() -> None:
    cfg = load_config()
    print("Building MAT cache (shared)…", flush=True)
    cache = cache_mod.build_cache(verbose=False)
    sessions = sorted(cache.sessions, key=_sid_chrono_key)
    print(f"Cache ready: {len(sessions)} sessions", flush=True)

    # Reuse one cache for both plot mains.
    def _cached_build(**_kwargs):
        return cache

    plot75.build_cache = _cached_build  # type: ignore[attr-defined]
    plot_joint.build_cache = _cached_build  # type: ignore[attr-defined]

    print("=== MAT_CONTEXT 0075 ===", flush=True)
    plot75.main()

    print("=== MAT_CONTEXT 0068 / 0073 ===", flush=True)
    out75 = Path(cfg["results_dir"]) / "MAT_CONTEXT_EQUITY"
    _write_0068_0073(cache, sessions, out75)

    print("=== JOINT CFADE + MAT075 + ES LVN ===", flush=True)
    plot_joint.main()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
