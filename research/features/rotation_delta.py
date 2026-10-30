"""Rotation delta-efficiency (H76 / EXP-0085).

Session-scale completed rotations (identical swing chain to
``session_rotation``) annotated with aggressor delta over [start_pivot,
end_pivot], effort-per-pt, and causal same-direction baseline IR.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa

from research.features.session_rotation import (
    PTS_PER_TICK,
    SessionRotationConfig,
)
from research.features.swings import SwingConfig, SwingPivot, detect_swings

IR_INEFFICIENT = 1.50
IR_EFFICIENT = 1.00
MAX_AGE_CONFIRM_MIN = 30.0
MAX_RETRACE_FRAC = 0.50
BASELINE_N = 4
MIN_SAME_DIR = 2


@dataclass
class RotationDeltaConfig:
    swing: SwingConfig = field(
        default_factory=lambda: SwingConfig(
            lookback_ms=900_000,
            confirm_ms=60_000,
            tick_size=1.0,
        )
    )
    min_rotation_pts: float = 15.0
    avg_n: int = BASELINE_N
    min_same_dir: int = MIN_SAME_DIR
    pts_per_tick: float = PTS_PER_TICK
    ir_inefficient: float = IR_INEFFICIENT
    ir_efficient: float = IR_EFFICIENT
    max_age_confirm_min: float = MAX_AGE_CONFIRM_MIN
    max_retrace_frac: float = MAX_RETRACE_FRAC


@dataclass(frozen=True)
class CompletedRotationDelta:
    """One completed rotation with tape delta over the pivot-to-pivot window."""

    end_confirm_ts_ns: int
    start_ts_ns: int  # start pivot print
    end_ts_ns: int  # end pivot print (delta window closes here)
    start_px_pts: float
    end_px_pts: float
    length_pts: float
    duration_sec: float
    direction: int  # +1 up (LOW→HIGH), -1 down (HIGH→LOW)
    delta: float  # signed aggressor sum (+buy / −sell)
    aligned_delta: float  # direction * delta
    effort_per_pt: float  # aligned_delta / length
    total_vol: float
    baseline_effort: float = float("nan")
    ir: float = float("nan")
    class_name: str = "warmup"
    from_prior: int = 0

    @property
    def speed_pts_per_min(self) -> float:
        if self.duration_sec <= 0:
            return float("nan")
        return self.length_pts / (self.duration_sec / 60.0)

    @property
    def pts_per_100_aligned(self) -> float:
        if not np.isfinite(self.effort_per_pt) or abs(self.effort_per_pt) < 1e-12:
            return float("nan")
        return 100.0 / self.effort_per_pt

    @property
    def vol_per_pt(self) -> float:
        if self.length_pts <= 0:
            return float("nan")
        return self.total_vol / self.length_pts


@dataclass
class PriorSessionDeltaStats:
    """Causal prior-session same-direction median effort-per-pt."""

    up_med_effort: float = float("nan")
    down_med_effort: float = float("nan")


def _alternating_chain(pivots: list[SwingPivot]) -> list[SwingPivot]:
    if not pivots:
        return []
    ordered = sorted(pivots, key=lambda p: (p.confirm_ts_ns, p.ts_ns))
    chain: list[SwingPivot] = []
    for p in ordered:
        if not chain:
            chain.append(p)
            continue
        if p.kind == chain[-1].kind:
            if p.kind == "HIGH" and p.price_ticks >= chain[-1].price_ticks:
                chain[-1] = p
            elif p.kind == "LOW" and p.price_ticks <= chain[-1].price_ticks:
                chain[-1] = p
            continue
        chain.append(p)
    return chain


def _trade_arrays(
    trades: pa.Table, *, pts_per_tick: float, tick_size: float = 1.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sorted (ts, px_pts, signed_size, size, pref_signed, pref_vol)."""
    if trades.num_rows == 0:
        empty = np.array([], dtype=np.int64)
        z = np.array([], dtype=np.float64)
        return empty, z, z, z, z
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    # Parquet price is tick units; chart points = ticks * pts_per_tick.
    px_ticks = np.round(trades.column("price").to_numpy().astype(np.float64) / tick_size)
    px = px_ticks * pts_per_tick
    sz = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    signed = np.where(buy, sz, -sz)
    order = np.argsort(ts, kind="stable")
    ts, px, signed, sz = ts[order], px[order], signed[order], sz[order]
    pref_signed = np.concatenate([[0.0], np.cumsum(signed)])
    pref_vol = np.concatenate([[0.0], np.cumsum(sz)])
    return ts, px, signed, pref_signed, pref_vol


def _window_sum(pref: np.ndarray, ts: np.ndarray, t0: int, t1: int) -> float:
    """Sum of values with timestamps in [t0, t1] inclusive via prefix."""
    if len(ts) == 0:
        return 0.0
    lo = int(np.searchsorted(ts, t0, side="left"))
    hi = int(np.searchsorted(ts, t1, side="right"))
    if hi <= lo:
        return 0.0
    return float(pref[hi] - pref[lo])


def classify_ir(
    aligned_delta: float,
    effort_per_pt: float,
    baseline: float | None,
    *,
    ir_inefficient: float = IR_INEFFICIENT,
    ir_efficient: float = IR_EFFICIENT,
) -> tuple[float, str, int]:
    """Return (ir, class_name, from_prior_placeholder). Caller sets from_prior."""
    if aligned_delta <= 0:
        ir = (
            float(effort_per_pt / baseline)
            if baseline is not None and baseline > 0 and np.isfinite(effort_per_pt)
            else float("nan")
        )
        return ir, "divergent", 0
    if baseline is None or not np.isfinite(baseline) or baseline <= 0:
        return float("nan"), "warmup", 0
    ir = float(effort_per_pt / baseline)
    if ir >= ir_inefficient:
        return ir, "inefficient", 0
    if ir <= ir_efficient:
        return ir, "efficient", 0
    return ir, "neutral", 0


def completed_rotation_deltas(
    pivots: list[SwingPivot],
    trades: pa.Table,
    *,
    cfg: RotationDeltaConfig | None = None,
    prior: PriorSessionDeltaStats | None = None,
) -> list[CompletedRotationDelta]:
    """Build completed rotations with delta + causal IR classification."""
    cfg = cfg or RotationDeltaConfig()
    chain = _alternating_chain(pivots)
    if len(chain) < 2:
        return []
    ts, _px, _signed, pref_s, pref_v = _trade_arrays(
        trades, pts_per_tick=cfg.pts_per_tick, tick_size=cfg.swing.tick_size
    )

    # First pass: raw rotations in confirm order
    raw: list[dict] = []
    for i in range(1, len(chain)):
        a, b = chain[i - 1], chain[i]
        length_pts = abs(b.price_ticks - a.price_ticks) * cfg.pts_per_tick
        if length_pts < cfg.min_rotation_pts:
            continue
        direction = 1 if b.kind == "HIGH" else -1
        start_ts = int(a.ts_ns)
        end_ts = int(b.ts_ns)
        delta = _window_sum(pref_s, ts, start_ts, end_ts)
        total_vol = _window_sum(pref_v, ts, start_ts, end_ts)
        aligned = float(direction) * delta
        effort = aligned / length_pts if length_pts > 0 else float("nan")
        dur = max(0.0, (end_ts - start_ts) / 1e9)
        raw.append({
            "end_confirm_ts_ns": int(b.confirm_ts_ns),
            "start_ts_ns": start_ts,
            "end_ts_ns": end_ts,
            "start_px_pts": float(a.price_ticks) * cfg.pts_per_tick,
            "end_px_pts": float(b.price_ticks) * cfg.pts_per_tick,
            "length_pts": float(length_pts),
            "duration_sec": dur,
            "direction": direction,
            "delta": float(delta),
            "aligned_delta": aligned,
            "effort_per_pt": float(effort) if np.isfinite(effort) else float("nan"),
            "total_vol": float(total_vol),
        })

    # Second pass: causal same-direction baseline at each rotation's confirm
    out: list[CompletedRotationDelta] = []
    for i, r in enumerate(raw):
        d = int(r["direction"])
        # prior same-dir rotations already classified (confirmed earlier)
        prev_efforts: list[float] = []
        for j in range(i):
            if int(raw[j]["direction"]) != d:
                continue
            # only those confirmed before this one's confirm (always true by order)
            e = raw[j]["effort_per_pt"]
            # Baseline uses prior rotations' effort regardless of their class,
            # but divergent (aligned<=0) efforts are still valid history for
            # the median of "what same-dir moves typically cost".
            if np.isfinite(e):
                prev_efforts.append(float(e))
        from_prior = 0
        baseline: float | None = None
        if len(prev_efforts) >= cfg.min_same_dir:
            baseline = float(np.median(prev_efforts[-cfg.avg_n :]))
        elif prior is not None:
            cand = prior.up_med_effort if d > 0 else prior.down_med_effort
            if np.isfinite(cand) and cand > 0:
                baseline = float(cand)
                from_prior = 1

        ir, cls, _ = classify_ir(
            r["aligned_delta"],
            r["effort_per_pt"],
            baseline,
            ir_inefficient=cfg.ir_inefficient,
            ir_efficient=cfg.ir_efficient,
        )
        out.append(
            CompletedRotationDelta(
                end_confirm_ts_ns=r["end_confirm_ts_ns"],
                start_ts_ns=r["start_ts_ns"],
                end_ts_ns=r["end_ts_ns"],
                start_px_pts=r["start_px_pts"],
                end_px_pts=r["end_px_pts"],
                length_pts=r["length_pts"],
                duration_sec=r["duration_sec"],
                direction=d,
                delta=r["delta"],
                aligned_delta=r["aligned_delta"],
                effort_per_pt=r["effort_per_pt"],
                total_vol=r["total_vol"],
                baseline_effort=float(baseline) if baseline is not None else float("nan"),
                ir=ir,
                class_name=cls,
                from_prior=from_prior,
            )
        )
    return out


def prior_session_delta_stats(
    rots: list[CompletedRotationDelta],
    *,
    avg_n: int = BASELINE_N,
) -> PriorSessionDeltaStats:
    up = [r.effort_per_pt for r in rots if r.direction > 0 and np.isfinite(r.effort_per_pt)]
    dn = [r.effort_per_pt for r in rots if r.direction < 0 and np.isfinite(r.effort_per_pt)]
    out = PriorSessionDeltaStats()
    if up:
        out.up_med_effort = float(np.median(up[-avg_n:]))
    if dn:
        out.down_med_effort = float(np.median(dn[-avg_n:]))
    return out


def rotation_deltas_for_trades(
    trades: pa.Table,
    *,
    cfg: RotationDeltaConfig | None = None,
    prior: PriorSessionDeltaStats | None = None,
) -> list[CompletedRotationDelta]:
    cfg = cfg or RotationDeltaConfig()
    pivots = detect_swings(trades, cfg.swing)
    return completed_rotation_deltas(pivots, trades, cfg=cfg, prior=prior)


def _last_usable(
    rots: list[CompletedRotationDelta], asof_ts_ns: int
) -> CompletedRotationDelta | None:
    best: CompletedRotationDelta | None = None
    for r in rots:
        if r.end_confirm_ts_ns > asof_ts_ns:
            break
        best = r
    return best


def _retrace_frac(r: CompletedRotationDelta, px_pts: float) -> float:
    """Fraction of rotation undone at price px. Clamped to ≥0; can exceed 1."""
    L = r.length_pts
    if L <= 0:
        return float("nan")
    if r.direction < 0:
        # down rotation ended at low end_px; undo = price up from end
        return float((px_pts - r.end_px_pts) / L)
    # up rotation ended at high; undo = price down from end
    return float((r.end_px_pts - px_pts) / L)


def state_at_rotation_delta(
    rots: list[CompletedRotationDelta],
    tr_ts: np.ndarray,
    tr_px_pts: np.ndarray,
    asof_ts_ns: int,
    side: int,
    *,
    cfg: RotationDeltaConfig | None = None,
) -> dict:
    """Causal rotation-delta features at asof for a MAT-side event."""
    cfg = cfg or RotationDeltaConfig()
    empty = {
        "rd_ready": 0,
        "rd_dir": 0,
        "rd_length_pts": np.nan,
        "rd_delta": np.nan,
        "rd_aligned_delta": np.nan,
        "rd_effort_per_pt": np.nan,
        "rd_baseline": np.nan,
        "rd_ir": np.nan,
        "rd_class": "none",
        "rd_from_prior": 0,
        "rd_relation": "none",
        "rd_retrace_frac": np.nan,
        "rd_age_confirm_min": np.nan,
        "rd_end_confirm_ts_ns": 0,
        "rd_speed": np.nan,
        "rd_vol_per_pt": np.nan,
        "rd_total_vol": np.nan,
        "rd_pts_per_100": np.nan,
        "rd_primary": 0,
        "rd_counter_eff": 0,
        "rd_with_ineff": 0,
    }
    r = _last_usable(rots, asof_ts_ns)
    if r is None:
        return empty
    j = int(np.searchsorted(tr_ts, asof_ts_ns, side="right") - 1)
    if j < 0:
        return empty
    px = float(tr_px_pts[j])
    relation = "counter" if int(side) == -int(r.direction) else (
        "with" if int(side) == int(r.direction) else "none"
    )
    age_min = (asof_ts_ns - r.end_confirm_ts_ns) / 60e9
    retrace = _retrace_frac(r, px)
    primary = int(
        relation == "counter"
        and r.class_name == "inefficient"
        and np.isfinite(age_min)
        and age_min <= cfg.max_age_confirm_min
        and np.isfinite(retrace)
        and retrace <= cfg.max_retrace_frac
        and retrace >= 0.0
    )
    counter_eff = int(
        relation == "counter"
        and r.class_name == "efficient"
        and np.isfinite(age_min)
        and age_min <= cfg.max_age_confirm_min
        and np.isfinite(retrace)
        and retrace <= cfg.max_retrace_frac
        and retrace >= 0.0
    )
    with_ineff = int(
        relation == "with"
        and r.class_name == "inefficient"
        and np.isfinite(age_min)
        and age_min <= cfg.max_age_confirm_min
    )
    return {
        "rd_ready": 1,
        "rd_dir": int(r.direction),
        "rd_length_pts": float(r.length_pts),
        "rd_delta": float(r.delta),
        "rd_aligned_delta": float(r.aligned_delta),
        "rd_effort_per_pt": float(r.effort_per_pt),
        "rd_baseline": float(r.baseline_effort),
        "rd_ir": float(r.ir) if np.isfinite(r.ir) else np.nan,
        "rd_class": r.class_name,
        "rd_from_prior": int(r.from_prior),
        "rd_relation": relation,
        "rd_retrace_frac": float(retrace) if np.isfinite(retrace) else np.nan,
        "rd_age_confirm_min": float(age_min),
        "rd_end_confirm_ts_ns": int(r.end_confirm_ts_ns),
        "rd_speed": float(r.speed_pts_per_min) if np.isfinite(r.speed_pts_per_min) else np.nan,
        "rd_vol_per_pt": float(r.vol_per_pt) if np.isfinite(r.vol_per_pt) else np.nan,
        "rd_total_vol": float(r.total_vol),
        "rd_pts_per_100": float(r.pts_per_100_aligned) if np.isfinite(r.pts_per_100_aligned) else np.nan,
        "rd_primary": primary,
        "rd_counter_eff": counter_eff,
        "rd_with_ineff": with_ineff,
    }


def annotate_rotation_delta(
    events: pa.Table,
    trades: pa.Table,
    *,
    cfg: RotationDeltaConfig | None = None,
    prior: PriorSessionDeltaStats | None = None,
    rots: list[CompletedRotationDelta] | None = None,
) -> pa.Table:
    """Append ``rd_*`` columns to events (by event_ts_ns + side)."""
    cfg = cfg or RotationDeltaConfig()
    if events.num_rows == 0:
        return events
    if rots is None:
        rots = rotation_deltas_for_trades(trades, cfg=cfg, prior=prior)
    ts, px, *_rest = _trade_arrays(
        trades, pts_per_tick=cfg.pts_per_tick, tick_size=cfg.swing.tick_size
    )
    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = (
        events.column("side").to_numpy().astype(int)
        if "side" in events.column_names
        else np.zeros(len(ev_ts), dtype=int)
    )
    n = len(ev_ts)
    float_cols = {
        "rd_length_pts": np.full(n, np.nan),
        "rd_delta": np.full(n, np.nan),
        "rd_aligned_delta": np.full(n, np.nan),
        "rd_effort_per_pt": np.full(n, np.nan),
        "rd_baseline": np.full(n, np.nan),
        "rd_ir": np.full(n, np.nan),
        "rd_retrace_frac": np.full(n, np.nan),
        "rd_age_confirm_min": np.full(n, np.nan),
        "rd_speed": np.full(n, np.nan),
        "rd_vol_per_pt": np.full(n, np.nan),
        "rd_total_vol": np.full(n, np.nan),
        "rd_pts_per_100": np.full(n, np.nan),
    }
    int_cols = {
        "rd_ready": np.zeros(n, dtype=np.int8),
        "rd_dir": np.zeros(n, dtype=np.int8),
        "rd_from_prior": np.zeros(n, dtype=np.int8),
        "rd_primary": np.zeros(n, dtype=np.int8),
        "rd_counter_eff": np.zeros(n, dtype=np.int8),
        "rd_with_ineff": np.zeros(n, dtype=np.int8),
        "rd_end_confirm_ts_ns": np.zeros(n, dtype=np.int64),
    }
    class_a = [""] * n
    rel_a = [""] * n

    for i in range(n):
        st = state_at_rotation_delta(
            rots, ts, px, int(ev_ts[i]), int(sides[i]), cfg=cfg
        )
        for k in float_cols:
            float_cols[k][i] = st[k]
        for k in int_cols:
            int_cols[k][i] = st[k]
        class_a[i] = str(st["rd_class"])
        rel_a[i] = str(st["rd_relation"])

    out = events
    for name, arr in float_cols.items():
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.float64()))
    for name, arr in int_cols.items():
        if name in out.column_names:
            out = out.drop(name)
        typ = pa.int64() if name == "rd_end_confirm_ts_ns" else pa.int8()
        out = out.append_column(name, pa.array(arr, type=typ))
    for name, vals in (("rd_class", class_a), ("rd_relation", rel_a)):
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(vals, type=pa.string()))
    return out


def es_aligned_delta_for_rotation(
    r: CompletedRotationDelta,
    es_trades: pa.Table,
    *,
    pts_per_tick: float = PTS_PER_TICK,
) -> float:
    """ES aggressor delta over the same [start, end] window, aligned to NQ dir."""
    if es_trades.num_rows == 0:
        return float("nan")
    ts, _px, _s, pref_s, _pv = _trade_arrays(es_trades, pts_per_tick=pts_per_tick)
    delta = _window_sum(pref_s, ts, r.start_ts_ns, r.end_ts_ns)
    return float(r.direction) * delta


def confirm_only_events(
    rots: list[CompletedRotationDelta],
    *,
    class_name: str = "inefficient",
) -> pa.Table:
    """Enter at end_confirm of matching rotations, side = −direction."""
    rows_ts: list[int] = []
    rows_side: list[int] = []
    rows_confirm: list[int] = []
    for r in rots:
        if r.class_name != class_name:
            continue
        rows_ts.append(int(r.end_confirm_ts_ns))
        rows_side.append(int(-r.direction))
        rows_confirm.append(int(r.end_confirm_ts_ns))
    if not rows_ts:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.asarray(rows_ts, dtype=np.int64),
        "side": np.asarray(rows_side, dtype=np.int8),
        "rd_end_confirm_ts_ns": np.asarray(rows_confirm, dtype=np.int64),
        "rd_class": pa.array([class_name] * len(rows_ts), type=pa.string()),
        "rd_relation": pa.array(["counter"] * len(rows_ts), type=pa.string()),
        "rd_primary": np.ones(len(rows_ts), dtype=np.int8),
    })


def first_per_rotation_mask(ev: pa.Table) -> np.ndarray:
    """Keep the first event (by time) per ``rd_end_confirm_ts_ns``."""
    n = ev.num_rows
    if n == 0:
        return np.array([], dtype=bool)
    ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
    conf = ev.column("rd_end_confirm_ts_ns").to_numpy().astype(np.int64)
    order = np.argsort(ts, kind="stable")
    seen: set[int] = set()
    keep = np.zeros(n, dtype=bool)
    for i in order:
        c = int(conf[i])
        if c <= 0 or c in seen:
            continue
        seen.add(c)
        keep[i] = True
    return keep


# Re-export SessionRotationConfig alias for callers that want both
def session_cfg_from_delta(cfg: RotationDeltaConfig) -> SessionRotationConfig:
    return SessionRotationConfig(
        swing=cfg.swing,
        min_rotation_pts=cfg.min_rotation_pts,
        avg_n=cfg.avg_n,
        min_same_dir=cfg.min_same_dir,
        pts_per_tick=cfg.pts_per_tick,
    )
