"""HV Extreme SL10/TP30 with optional daily profit stop ($1000 = 50 NQ pts)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from research.backtest.mat_sim import SimSummary, TradeResult
from research.hvext.run_hvext_sweep import SignalCell, fixed_bracket, pack_summary, simulate_cell
from research.run_experiment import load_config

OCT = [
    "011026",
    "021026",
    "051026",
    "061026",
    "071026",
    "081026",
    "091026",
    "121026",
    "131026",
    "141026",
    "151026",
    "161026",
    "191026",
]
SEP = [
    "010926",
    "020926",
    "040926",
    "080926",
    "100926",
    "150926",
    "160926",
    "170926",
    "180926",
    "210926",
    "220926",
    "250926",
    "280926",
    "290926",
    "300926",
]

COMMISSION = 0.25
# $1000 / ($20 per NQ point) = 50 pts
DAY_STOP_PTS = 50.0
DAY_STOP_USD = 1000.0
USD_PER_PT = 20.0


def apply_day_stop(trades: list[TradeResult], stop_pts: float) -> tuple[list[TradeResult], dict]:
    """Keep trades in time order until day net (after commission) >= stop_pts."""
    by_sid: dict[str, list[TradeResult]] = defaultdict(list)
    for t in trades:
        by_sid[t.session_id].append(t)
    kept: list[TradeResult] = []
    meta = {"days_hit_stop": 0, "trades_skipped": 0, "per_session": {}}
    for sid in sorted(by_sid):
        day = sorted(by_sid[sid], key=lambda t: t.entry_ts_ns)
        net = 0.0
        taken = 0
        skipped = 0
        hit = False
        for t in day:
            if hit:
                skipped += 1
                continue
            kept.append(t)
            taken += 1
            net += t.pnl - COMMISSION
            if net >= stop_pts:
                hit = True
        if hit:
            meta["days_hit_stop"] += 1
        meta["trades_skipped"] += skipped
        meta["per_session"][sid] = {
            "net": round(net, 2),
            "n_taken": taken,
            "n_skipped": skipped,
            "hit_stop": hit,
        }
    return kept, meta


def summarize_trades(trades: list[TradeResult]) -> dict:
    out = SimSummary()
    equity = peak = 0.0
    for t in trades:
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
        net = t.pnl - COMMISSION
        out.commission_total += COMMISSION
        out.net += net
        equity += net
        peak = max(peak, equity)
        out.max_dd = max(out.max_dd, peak - equity)
    return pack_summary(out)


def run_cohort(parquet_dir: Path, sessions: list[str], cell: SignalCell, label: str) -> dict:
    bfn = fixed_bracket(30.0, 10.0)
    base = simulate_cell(parquet_dir, sessions, cell, bfn)
    pack_base = pack_summary(base)
    kept, stop_meta = apply_day_stop(base.trades, DAY_STOP_PTS)
    pack_stop = summarize_trades(kept)

    print(f"\n=== {label} ===")
    print(
        f"NO stop:   n={pack_base['n']} wr={pack_base.get('wr')} pf={pack_base.get('pf')} "
        f"net={pack_base.get('net')} pts (~${(pack_base.get('net') or 0)*USD_PER_PT:.0f}) "
        f"days+={pack_base.get('days_pos')}/{pack_base.get('n_sessions')} maxDD={pack_base.get('max_dd')}"
    )
    print(
        f"+${DAY_STOP_USD:.0f} stop: n={pack_stop['n']} wr={pack_stop.get('wr')} pf={pack_stop.get('pf')} "
        f"net={pack_stop.get('net')} pts (~${(pack_stop.get('net') or 0)*USD_PER_PT:.0f}) "
        f"days+={pack_stop.get('days_pos')}/{pack_stop.get('n_sessions')} maxDD={pack_stop.get('max_dd')}"
    )
    print(
        f"  stop hit on {stop_meta['days_hit_stop']} days; "
        f"skipped {stop_meta['trades_skipped']} trades after stop"
    )
    delta = (pack_stop.get("net") or 0) - (pack_base.get("net") or 0)
    print(f"  delta net: {delta:+.2f} pts (~${delta * USD_PER_PT:+.0f})")

    print("per-session (no stop -> with stop):")
    for sid in sessions:
        a = pack_base["per_session"].get(sid)
        b = stop_meta["per_session"].get(sid)
        if a is None and b is None:
            print(f"  {sid}: no trades")
            continue
        a_s = f"{a:+.2f}" if a is not None else "-"
        if b is None:
            print(f"  {sid}: {a_s} -> -")
        else:
            flag = " STOP" if b["hit_stop"] else ""
            print(
                f"  {sid}: {a_s} -> {b['net']:+.2f} "
                f"(took {b['n_taken']}, skipped {b['n_skipped']}){flag}"
            )

    return {
        "label": label,
        "no_stop": pack_base,
        "with_stop": pack_stop,
        "stop_meta": stop_meta,
        "delta_net_pts": delta,
    }


def main() -> int:
    cfg = load_config()
    p = Path(cfg["parquet_dir"])
    print(
        f"Bracket SL10/TP30 | UK 14:30–17:00 | flat-only | cooldown 30s | "
        f"commission {COMMISSION} | day stop ${DAY_STOP_USD:.0f} = {DAY_STOP_PTS:g} pts @ ${USD_PER_PT:.0f}/pt"
    )

    frozen = SignalCell(
        move_pts=20.0,
        confirm_pts=15.0,
        peak_band_pts=2.0,
        high_vol_mult=2.0,
        min_path_efficiency=0.0,
        max_pullback_pts=5.0,
        max_leg_sec=60.0,
    )
    panel = SignalCell(
        move_pts=20.0,
        confirm_pts=10.0,
        peak_band_pts=5.0,
        high_vol_mult=2.0,
        min_path_efficiency=0.0,
        max_pullback_pts=5.0,
        max_leg_sec=0.0,
    )

    results = {
        "day_stop_usd": DAY_STOP_USD,
        "day_stop_pts": DAY_STOP_PTS,
        "usd_per_pt": USD_PER_PT,
        "cohorts": {},
    }
    for name, sessions in (("october", OCT), ("september", SEP)):
        print(f"\n######## {name.upper()} ({len(sessions)} sessions) ########")
        results["cohorts"][name] = {
            "frozen": run_cohort(p, sessions, frozen, f"{name} / frozen signal"),
            "panel": run_cohort(p, sessions, panel, f"{name} / panel signal"),
        }

    dest = Path("results/EXP-0037/day_profit_stop_1000.json")
    dest.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
