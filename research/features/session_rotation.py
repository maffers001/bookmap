"""Session-scale rotation length / extension / speed (H59 / EXP-0068).

Distinct from EXP-0030 micro swings (3-min lookback / 20s confirm). Here:
  lookback 15 min, confirm 60 s, ignore rotations < 15 pts.
Average = last N same-direction completed rotations; if fewer than
``min_same_dir`` this session, fall back to the prior clean session's
same-direction mean (causal — only sessions before the current one).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa

from research.features.swings import SwingConfig, SwingPivot, detect_swings

PTS_PER_TICK = 0.25


@dataclass
class SessionRotationConfig:
    swing: SwingConfig = field(
        default_factory=lambda: SwingConfig(
            lookback_ms=900_000,
            confirm_ms=60_000,
            tick_size=1.0,
        )
    )
    min_rotation_pts: float = 15.0
    avg_n: int = 4
    min_same_dir: int = 2
    pts_per_tick: float = PTS_PER_TICK


@dataclass(frozen=True)
class CompletedSessionRotation:
    end_confirm_ts_ns: int
    start_ts_ns: int
    length_pts: float
    duration_sec: float
    direction: int  # +1 up (LOW→HIGH), -1 down (HIGH→LOW)

    @property
    def speed_pts_per_min(self) -> float:
        if self.duration_sec <= 0:
            return float("nan")
        return self.length_pts / (self.duration_sec / 60.0)


def completed_session_rotations(
    pivots: list[SwingPivot],
    *,
    min_rotation_pts: float = 15.0,
    pts_per_tick: float = PTS_PER_TICK,
) -> list[CompletedSessionRotation]:
    """Alternating swing chain; drop rotations shorter than min_rotation_pts."""
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

    out: list[CompletedSessionRotation] = []
    for i in range(1, len(chain)):
        a, b = chain[i - 1], chain[i]
        length_pts = abs(b.price_ticks - a.price_ticks) * pts_per_tick
        if length_pts < min_rotation_pts:
            continue
        direction = 1 if b.kind == "HIGH" else -1
        dur = max(0.0, (b.ts_ns - a.ts_ns) / 1e9)
        out.append(
            CompletedSessionRotation(
                end_confirm_ts_ns=b.confirm_ts_ns,
                start_ts_ns=a.ts_ns,
                length_pts=float(length_pts),
                duration_sec=dur,
                direction=direction,
            )
        )
    return out


def _last_pivot_asof(
    pivots: list[SwingPivot], asof_ts_ns: int, kind: str | None = None
) -> SwingPivot | None:
    best: SwingPivot | None = None
    for p in pivots:
        if p.confirm_ts_ns > asof_ts_ns:
            continue
        if kind is not None and p.kind != kind:
            continue
        if best is None or p.confirm_ts_ns > best.confirm_ts_ns:
            best = p
    return best


def _open_leg(
    pivots: list[SwingPivot],
    asof_ts_ns: int,
    px_ticks: int,
    pts_per_tick: float,
) -> tuple[int, float, int] | None:
    """(leg_dir, extension_pts, origin_ts_ns) or None."""
    low = _last_pivot_asof(pivots, asof_ts_ns, "LOW")
    high = _last_pivot_asof(pivots, asof_ts_ns, "HIGH")
    last = _last_pivot_asof(pivots, asof_ts_ns, None)
    if last is None:
        return None

    if low is not None and high is not None:
        if px_ticks >= high.price_ticks:
            return 1, float(px_ticks - low.price_ticks) * pts_per_tick, low.ts_ns
        if px_ticks <= low.price_ticks:
            return -1, float(high.price_ticks - px_ticks) * pts_per_tick, high.ts_ns

    if last.kind == "LOW":
        return 1, float(max(0, px_ticks - last.price_ticks)) * pts_per_tick, last.ts_ns
    return -1, float(max(0, last.price_ticks - px_ticks)) * pts_per_tick, last.ts_ns


def _avg_same_dir(
    rots: list[CompletedSessionRotation],
    asof_ts_ns: int,
    direction: int,
    avg_n: int,
    min_same_dir: int,
) -> tuple[float | None, float | None]:
    """(avg_length_pts, median_speed) for same-direction rotations confirmed by asof."""
    lengths: list[float] = []
    speeds: list[float] = []
    for r in rots:
        if r.end_confirm_ts_ns > asof_ts_ns:
            break
        if r.direction != direction:
            continue
        lengths.append(r.length_pts)
        sp = r.speed_pts_per_min
        if np.isfinite(sp):
            speeds.append(sp)
    if len(lengths) < min_same_dir:
        return None, None
    window = lengths[-avg_n:]
    med_speed = float(np.median(speeds[-avg_n:])) if speeds else None
    return float(np.mean(window)), med_speed


@dataclass
class PriorSessionRotationStats:
    """Causal prior-session same-direction means (pts and speed)."""
    up_avg_pts: float = float("nan")
    down_avg_pts: float = float("nan")
    up_med_speed: float = float("nan")
    down_med_speed: float = float("nan")


def prior_session_stats(
    rots: list[CompletedSessionRotation],
    *,
    avg_n: int = 4,
) -> PriorSessionRotationStats:
    up = [r for r in rots if r.direction > 0]
    dn = [r for r in rots if r.direction < 0]
    out = PriorSessionRotationStats()
    if up:
        out.up_avg_pts = float(np.mean([r.length_pts for r in up[-avg_n:]]))
        sps = [r.speed_pts_per_min for r in up if np.isfinite(r.speed_pts_per_min)]
        if sps:
            out.up_med_speed = float(np.median(sps[-avg_n:]))
    if dn:
        out.down_avg_pts = float(np.mean([r.length_pts for r in dn[-avg_n:]]))
        sps = [r.speed_pts_per_min for r in dn if np.isfinite(r.speed_pts_per_min)]
        if sps:
            out.down_med_speed = float(np.median(sps[-avg_n:]))
    return out


def rotation_state_at(
    pivots: list[SwingPivot],
    rots: list[CompletedSessionRotation],
    tr_ts: np.ndarray,
    tr_px_ticks: np.ndarray,
    asof_ts_ns: int,
    cfg: SessionRotationConfig,
    prior: PriorSessionRotationStats | None = None,
) -> dict:
    empty = {
        "rot_avg_pts": np.nan,
        "rot_extension_pts": np.nan,
        "rot_ratio": np.nan,
        "rot_leg_dir": 0,
        "rot_ready": 0,
        "rot_from_prior": 0,
        "rot_speed": np.nan,
        "rot_med_speed": np.nan,
        "rot_speed_ratio": np.nan,
    }
    j = int(np.searchsorted(tr_ts, asof_ts_ns, side="right") - 1)
    if j < 0:
        return empty
    leg = _open_leg(pivots, asof_ts_ns, int(tr_px_ticks[j]), cfg.pts_per_tick)
    if leg is None:
        return empty
    leg_dir, extension, origin_ts = leg
    avg, med_speed = _avg_same_dir(
        rots, asof_ts_ns, leg_dir, cfg.avg_n, cfg.min_same_dir
    )
    from_prior = 0
    if avg is None and prior is not None:
        from_prior = 1
        if leg_dir > 0 and np.isfinite(prior.up_avg_pts):
            avg = prior.up_avg_pts
            med_speed = prior.up_med_speed if np.isfinite(prior.up_med_speed) else None
        elif leg_dir < 0 and np.isfinite(prior.down_avg_pts):
            avg = prior.down_avg_pts
            med_speed = prior.down_med_speed if np.isfinite(prior.down_med_speed) else None

    ready = avg is not None and avg > 0
    ratio = float(extension / avg) if ready else np.nan
    dur_sec = max(0.0, (asof_ts_ns - origin_ts) / 1e9)
    speed = (extension / (dur_sec / 60.0)) if dur_sec > 1e-6 else np.nan
    speed_ratio = (
        float(speed / med_speed)
        if ready and med_speed is not None and np.isfinite(med_speed) and med_speed > 0 and np.isfinite(speed)
        else np.nan
    )
    return {
        "rot_avg_pts": float(avg) if ready else np.nan,
        "rot_extension_pts": extension,
        "rot_ratio": ratio,
        "rot_leg_dir": leg_dir,
        "rot_ready": 1 if ready else 0,
        "rot_from_prior": from_prior if ready else 0,
        "rot_speed": float(speed) if np.isfinite(speed) else np.nan,
        "rot_med_speed": float(med_speed) if med_speed is not None and np.isfinite(med_speed) else np.nan,
        "rot_speed_ratio": speed_ratio,
    }


def annotate_session_rotation(
    events: pa.Table,
    trades: pa.Table,
    *,
    cfg: SessionRotationConfig | None = None,
    prior: PriorSessionRotationStats | None = None,
) -> pa.Table:
    """Append session-rotation columns to events (by event_ts_ns)."""
    cfg = cfg or SessionRotationConfig()
    if events.num_rows == 0:
        return events
    pivots = detect_swings(trades, cfg.swing)
    rots = completed_session_rotations(
        pivots,
        min_rotation_pts=cfg.min_rotation_pts,
        pts_per_tick=cfg.pts_per_tick,
    )
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = np.round(trades.column("price").to_numpy().astype(np.float64) / cfg.swing.tick_size).astype(
        np.int64
    )
    order = np.argsort(ts)
    tr_ts, tr_px = ts[order], px[order]
    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)

    n = len(ev_ts)
    cols = {
        "rot_avg_pts": np.full(n, np.nan),
        "rot_extension_pts": np.full(n, np.nan),
        "rot_ratio": np.full(n, np.nan),
        "rot_leg_dir": np.zeros(n, dtype=np.int8),
        "rot_ready": np.zeros(n, dtype=np.int8),
        "rot_from_prior": np.zeros(n, dtype=np.int8),
        "rot_speed": np.full(n, np.nan),
        "rot_med_speed": np.full(n, np.nan),
        "rot_speed_ratio": np.full(n, np.nan),
    }
    for i, t0 in enumerate(ev_ts):
        st = rotation_state_at(pivots, rots, tr_ts, tr_px, int(t0), cfg, prior)
        for k, v in st.items():
            cols[k][i] = v

    out = events
    type_map = {
        "rot_leg_dir": pa.int8(),
        "rot_ready": pa.int8(),
        "rot_from_prior": pa.int8(),
    }
    for name, arr in cols.items():
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=type_map.get(name, pa.float64())))
    return out


def session_rotations_for_trades(
    trades: pa.Table,
    *,
    cfg: SessionRotationConfig | None = None,
) -> list[CompletedSessionRotation]:
    cfg = cfg or SessionRotationConfig()
    pivots = detect_swings(trades, cfg.swing)
    return completed_session_rotations(
        pivots,
        min_rotation_pts=cfg.min_rotation_pts,
        pts_per_tick=cfg.pts_per_tick,
    )
