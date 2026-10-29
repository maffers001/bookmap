"""Shared protocol for CFADE-family similarly-themed strategy investigations.

Phase 0 constants: clean 113-session sample, DIG4 23d IS, frozen CFADE
signal/brackets/costs. All EXP-0057+ runners import from here.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import yaml

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    TradeResult,
    combine_gates,
    make_daytype_skip,
    make_trend_mode,
    make_vol_bracket,
    make_vol_bracket_rr,
    make_vwap_side,
    merge_summaries,
    simulate_session,
)
from research.correlated import harness as H
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events
from research.run_cfade_all_months import _discover_sessions, _sid_chrono_key

# --- Sample discipline -------------------------------------------------------

EXCLUDE = {
    "200626", "220626",  # May tapes misfiled under June names
    "300326",  # exact duplicate of 270326 (same trades hash / timestamps)
}

DIG4_23 = [
    "011026", "021026", "030826", "040826", "050826", "051026", "061026",
    "071026", "081026", "091026", "121026", "131026", "141026", "151026",
    "160926", "161026", "170926", "180926", "210926", "220926", "260326",
    "290926", "300926",
]
DIG4_23D = set(DIG4_23)

# --- Frozen CFADE ------------------------------------------------------------

COMMISSION = 0.25
ES_COMMISSION = 0.125  # ES round-trip in chart points (existing IRT convention)
NQ_PTS_PER_TICK = 0.25
ES_PTS_PER_TICK = 0.25  # ES parquet also in tick units; ES tick = 0.25 pts

FROZEN_EXHAUST = ExhaustParams(
    es_imb_max=-0.4,
    es_min_vol=0,
    use_imb=True,
    use_vol=False,
    nq_min_imbalance=0.55,
    nq_min_vol=25,
    nq_min_trades=6,
)
FROZEN_MAX_WAIT_SEC = 5.0
FROZEN_ER_MAX = 0.3


def frozen_gate():
    return make_daytype_skip(max_er=FROZEN_ER_MAX)


def frozen_bracket():
    """Vol RR SL1/TP2 with independent clip 4–40 (the cap *is* the edge)."""
    return make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0)


def frozen_sim_cfg(*, slip_ticks: float = 0.0, commission: float = COMMISSION) -> MatSimConfig:
    """CFADE path sim — bracket exits match Bookmap (fill at TP/SL, no exit slip)."""
    return MatSimConfig(
        slip_ticks=slip_ticks,
        commission_pts_rt=commission,
        signal_cooldown_sec=5.0,
        tp_pts=10,
        sl_pts=10,
    )


def mat_frozen_gate():
    """Live MAT: trend+VWAP + optional daytype overlays layered by caller."""
    return combine_gates(make_trend_mode("with"), make_vwap_side(True))


def mat_vol_bracket():
    return make_vol_bracket(1.0, floor=6.0, cap=30.0)


def mat_sim_cfg(*, slip_ticks: float = 0.0, es_stamp: str = "causal") -> MatSimConfig:
    """MAT path sim aligned with Bookmap Replay bracket fills.

    Default ``slip_ticks=0``: TP/SL are offsets from the fill and exit at the
    working price (no exit slip). ``commission_pts_rt`` remains a separate
    round-trip cost for research nets; Bookmap SIM price P&L is ``gross``.
    """
    return MatSimConfig(
        slip_ticks=slip_ticks,
        commission_pts_rt=COMMISSION,
        signal_cooldown_sec=5.0,
        tp_pts=10,
        sl_pts=10,
        es_stamp=es_stamp,
    )


# --- Sessions ----------------------------------------------------------------

def clean_sessions(parquet_dir: Path) -> list[str]:
    return [s for s in _discover_sessions(parquet_dir) if s not in EXCLUDE]


def is_is(sid: str) -> bool:
    return sid in DIG4_23D


def split_label(sid: str) -> str:
    return "IS" if is_is(sid) else "OOS"


# --- Event builders ----------------------------------------------------------

def cfade_events(pair: H.Pair) -> pa.Table:
    nq_b, es_b = pair.bars(100)
    bursts = H.confirm_bursts(pair)
    return exhaust_fade_events(
        bursts, nq_b, es_b, FROZEN_EXHAUST, max_wait_sec=FROZEN_MAX_WAIT_SEC
    )


def simulate_cfade(
    pair: H.Pair,
    *,
    events: pa.Table | None = None,
    gate=None,
    bracket_fn=None,
    slip_ticks: float = 0.0,
    primary: str = "nq",
    commission: float | None = None,
    cfg: MatSimConfig | None = None,
    exit_fn=None,
) -> SimSummary:
    """Simulate CFADE-style events. ``primary='es'`` trades the ES path."""
    ev = events if events is not None else cfade_events(pair)
    if ev.num_rows == 0:
        return SimSummary()
    g = gate if gate is not None else frozen_gate()
    br = bracket_fn if bracket_fn is not None else frozen_bracket()
    if primary == "es":
        pts = ES_PTS_PER_TICK
        comm = ES_COMMISSION if commission is None else commission
        traded, other = pair.es, pair.nq
        sim = cfg or MatSimConfig(
            slip_ticks=slip_ticks,
            commission_pts_rt=comm,
            signal_cooldown_sec=5.0,
            tp_pts=10,
            sl_pts=10,
            pts_per_tick=pts,
        )
    else:
        traded, other = pair.nq, pair.es
        sim = cfg or frozen_sim_cfg(slip_ticks=slip_ticks, commission=commission or COMMISSION)
    return simulate_session(
        pair.sid, traded, other, sim,
        gate=g, bracket_fn=br,
        vwap_from_ns=pair.vwap_from_ns,
        events=ev,
        exit_fn=exit_fn,
    )


def simulate_mat(pair: H.Pair, *, gate=None, bracket_fn=None, slip_ticks: float = 0.0) -> SimSummary:
    g = gate if gate is not None else mat_frozen_gate()
    br = bracket_fn if bracket_fn is not None else mat_vol_bracket()
    return simulate_session(
        pair.sid, pair.nq, pair.es, mat_sim_cfg(slip_ticks=slip_ticks),
        gate=g, bracket_fn=br, vwap_from_ns=pair.vwap_from_ns,
    )


# --- Stats / reporting -------------------------------------------------------

@dataclass
class BookStats:
    n: int = 0
    net: float = 0.0
    gross: float = 0.0
    wr: float | None = None
    avg: float | None = None
    days: int = 0
    days_pos: int = 0
    max_dd: float = 0.0
    is_n: int = 0
    is_net: float = 0.0
    oos_n: int = 0
    oos_net: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def day_nets_from_trades(trades: list[TradeResult], commission: float = COMMISSION) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for t in trades:
        out[t.session_id] += t.pnl - commission
    return dict(out)


def stats_from_day_nets(day_nets: dict[str, float], trades: list[TradeResult] | None = None,
                        commission: float = COMMISSION) -> BookStats:
    s = BookStats()
    if not day_nets and not trades:
        return s
    nets = list(day_nets.values())
    s.days = len(day_nets)
    s.days_pos = sum(1 for v in nets if v > 0)
    pe = ph = pdd = 0.0
    for sid in sorted(day_nets, key=_sid_chrono_key):
        pe += day_nets[sid]
        ph = max(ph, pe)
        pdd = max(pdd, ph - pe)
        if is_is(sid):
            s.is_net += day_nets[sid]
        else:
            s.oos_net += day_nets[sid]
    s.net = round(pe, 2)
    s.max_dd = round(pdd, 2)
    s.is_net = round(s.is_net, 2)
    s.oos_net = round(s.oos_net, 2)
    if trades:
        s.n = len(trades)
        wins = sum(1 for t in trades if (t.pnl - commission) > 0)
        s.wr = round(wins / s.n, 3) if s.n else None
        s.gross = round(sum(t.pnl for t in trades), 2)
        s.avg = round(s.net / s.n, 2) if s.n else None
        s.is_n = sum(1 for t in trades if is_is(t.session_id))
        s.oos_n = s.n - s.is_n
    return s


def merge_sim_parts(parts: list[SimSummary]) -> SimSummary:
    return merge_summaries(parts, COMMISSION) if parts else SimSummary()


def write_report(out: Path, *, title: str, md_lines: list[str], report: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


def load_yaml(name: str) -> dict:
    path = Path(__file__).resolve().parents[1] / "experiments" / name
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def depth_mbo_coverage(parquet_dir: Path, sessions: list[str]) -> dict:
    depth, mbo, both = [], [], []
    for sid in sessions:
        p = parquet_dir / f"session_id={sid}"
        has_d = (p / "depth.parquet").exists()
        has_m = (p / "mbo.parquet").exists()
        if has_d:
            depth.append(sid)
        if has_m:
            mbo.append(sid)
        if has_d and has_m:
            both.append(sid)
    return {
        "n_sessions": len(sessions),
        "n_depth": len(depth),
        "n_mbo": len(mbo),
        "n_both": len(both),
        "depth_sessions": depth,
        "mbo_sessions": mbo,
        "both_sessions": both,
    }
