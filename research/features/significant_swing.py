"""Distance-confirmed (pullaway) swing highs/lows and MAT level-state labels.

Unlike ``swings.py`` (time-confirm 20s), a pivot is confirmed only after price
pulls away by ``pullaway_pts`` without a new extreme. Used by EXP-0039
(breakout) and EXP-0040 (S/R bounce).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


PTS_PER_TICK = 0.25


@dataclass
class SigSwingConfig:
    lookback_sec: float = 600.0  # 10 min
    pullaway_pts: float = 20.0
    cluster_tol_pts: float = 5.0
    invalidate_beyond_pts: float = 5.0  # level dead once price goes this far through
    at_tol_pts: float = 2.5
    pts_per_tick: float = PTS_PER_TICK
    tick_size: float = 1.0  # parquet price units


@dataclass(frozen=True)
class SigPivot:
    ts_ns: int
    confirm_ts_ns: int
    price_pts: float
    kind: str  # HIGH | LOW
    pullaway_pts: float
    cluster_n: int = 1


@dataclass
class ActiveLevel:
    kind: str
    price_pts: float
    confirm_ts_ns: int
    pivot_ts_ns: int
    cluster_n: int
    pullaway_pts: float
    first_break_ts_ns: int | None = None
    first_touch_ts_ns: int | None = None  # first hold-side print within at_tol
    last_touch_ts_ns: int | None = None  # most recent (end-of-walk) hold-side touch
    touch_times_ns: list | None = None  # all hold-side touches (causal as-of lookup)
    broken: bool = False


def _sorted_trades(trades: pa.Table, cfg: SigSwingConfig) -> tuple[np.ndarray, np.ndarray]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * cfg.pts_per_tick
    order = np.argsort(ts)
    return ts[order], px[order]


def detect_pullaway_pivots(
    trades: pa.Table,
    cfg: SigSwingConfig | None = None,
) -> list[SigPivot]:
    """Causal HIGH/LOW pivots confirmed by pullaway distance."""
    cfg = cfg or SigSwingConfig()
    if trades.num_rows == 0:
        return []
    ts, px = _sorted_trades(trades, cfg)
    lookback_ns = int(cfg.lookback_sec * 1e9)
    pull = float(cfg.pullaway_pts)

    pivots: list[SigPivot] = []
    pending_high: tuple[int, float] | None = None  # (extreme_ts, extreme_px)
    pending_low: tuple[int, float] | None = None
    j0 = 0

    for i in range(len(ts)):
        t = int(ts[i])
        p = float(px[i])
        while j0 < i and int(ts[j0]) < t - lookback_ns:
            j0 += 1

        # --- HIGH pending ---
        if pending_high is not None:
            et, ep = pending_high
            if p > ep + 1e-12:
                pending_high = (t, p)
            elif ep - p >= pull - 1e-12:
                pivots.append(
                    SigPivot(
                        ts_ns=et,
                        confirm_ts_ns=t,
                        price_pts=ep,
                        kind="HIGH",
                        pullaway_pts=pull,
                    )
                )
                pending_high = None

        # --- LOW pending ---
        if pending_low is not None:
            et, ep = pending_low
            if p < ep - 1e-12:
                pending_low = (t, p)
            elif p - ep >= pull - 1e-12:
                pivots.append(
                    SigPivot(
                        ts_ns=et,
                        confirm_ts_ns=t,
                        price_pts=ep,
                        kind="LOW",
                        pullaway_pts=pull,
                    )
                )
                pending_low = None

        # Start / refresh candidate if at lookback extreme
        wmax = float(np.max(px[j0 : i + 1]))
        wmin = float(np.min(px[j0 : i + 1]))
        if p >= wmax - 1e-12:
            if pending_high is None or p > pending_high[1] + 1e-12:
                pending_high = (t, p)
        if p <= wmin + 1e-12:
            if pending_low is None or p < pending_low[1] - 1e-12:
                pending_low = (t, p)

    return pivots


def cluster_pivots(
    pivots: list[SigPivot],
    *,
    tol_pts: float = 5.0,
) -> list[SigPivot]:
    """Merge same-kind pivots within tol_pts; level = max HIGH / min LOW.

    cluster_n = number of raw pivots in the merge. Emits one pivot per cluster
    at the confirm time of the last member (causal: usable after that).
    """
    if not pivots:
        return []
    by_kind: dict[str, list[SigPivot]] = {"HIGH": [], "LOW": []}
    for p in pivots:
        by_kind[p.kind].append(p)

    out: list[SigPivot] = []
    for kind, items in by_kind.items():
        items = sorted(items, key=lambda x: x.confirm_ts_ns)
        used = [False] * len(items)
        for i, a in enumerate(items):
            if used[i]:
                continue
            group = [a]
            used[i] = True
            # Grow cluster with any later pivot within tol of current group tip
            tip = a.price_pts
            for j in range(i + 1, len(items)):
                if used[j]:
                    continue
                b = items[j]
                if kind == "HIGH":
                    if abs(b.price_pts - tip) <= tol_pts or abs(b.price_pts - a.price_pts) <= tol_pts:
                        # also allow within tol of any member
                        if any(abs(b.price_pts - g.price_pts) <= tol_pts for g in group):
                            group.append(b)
                            used[j] = True
                            tip = max(tip, b.price_pts)
                else:
                    if any(abs(b.price_pts - g.price_pts) <= tol_pts for g in group):
                        group.append(b)
                        used[j] = True
                        tip = min(tip, b.price_pts)
            if kind == "HIGH":
                price = max(g.price_pts for g in group)
            else:
                price = min(g.price_pts for g in group)
            last = max(group, key=lambda g: g.confirm_ts_ns)
            first = min(group, key=lambda g: g.ts_ns)
            out.append(
                SigPivot(
                    ts_ns=first.ts_ns,
                    confirm_ts_ns=last.confirm_ts_ns,
                    price_pts=price,
                    kind=kind,
                    pullaway_pts=a.pullaway_pts,
                    cluster_n=len(group),
                )
            )
    out.sort(key=lambda p: p.confirm_ts_ns)
    return out


def build_break_timeline(
    trades: pa.Table,
    pivots: list[SigPivot],
    cfg: SigSwingConfig | None = None,
) -> list[ActiveLevel]:
    """Walk tape; return activated levels (objects updated with first_break_ts).

    A level becomes active at confirm_ts and is appended once. It is replaced by
    a new same-kind pivot further out (higher HIGH / lower LOW). first_break is
    the first print at/beyond the level. ``broken`` when beyond invalidate_beyond_pts.
    """
    cfg = cfg or SigSwingConfig()
    if trades.num_rows == 0 or not pivots:
        return []
    ts, px = _sorted_trades(trades, cfg)
    pivots_sorted = sorted(pivots, key=lambda p: p.confirm_ts_ns)

    levels: list[ActiveLevel] = []
    active_high: ActiveLevel | None = None
    active_low: ActiveLevel | None = None
    pi = 0

    def activate(piv: SigPivot) -> ActiveLevel:
        lv = ActiveLevel(
            kind=piv.kind,
            price_pts=piv.price_pts,
            confirm_ts_ns=piv.confirm_ts_ns,
            pivot_ts_ns=piv.ts_ns,
            cluster_n=piv.cluster_n,
            pullaway_pts=piv.pullaway_pts,
            touch_times_ns=[],
        )
        levels.append(lv)
        return lv

    for i in range(len(ts)):
        t = int(ts[i])
        p = float(px[i])
        while pi < len(pivots_sorted) and pivots_sorted[pi].confirm_ts_ns <= t:
            piv = pivots_sorted[pi]
            pi += 1
            if piv.kind == "HIGH":
                if active_high is None or piv.price_pts >= active_high.price_pts - 1e-12:
                    active_high = activate(piv)
            else:
                if active_low is None or piv.price_pts <= active_low.price_pts + 1e-12:
                    active_low = activate(piv)

        if active_high is not None and not active_high.broken:
            # Hold-side touch (from below) within at_tol, before/at level
            if (
                p <= active_high.price_pts + 1e-12
                and active_high.price_pts - p <= cfg.at_tol_pts + 1e-12
            ):
                if active_high.first_touch_ts_ns is None:
                    active_high.first_touch_ts_ns = t
                active_high.last_touch_ts_ns = t
                if active_high.touch_times_ns is not None:
                    active_high.touch_times_ns.append(t)
            if p >= active_high.price_pts - 1e-12:
                if active_high.first_break_ts_ns is None:
                    active_high.first_break_ts_ns = t
                if p >= active_high.price_pts + cfg.invalidate_beyond_pts - 1e-12:
                    active_high.broken = True
                    active_high = None
        if active_low is not None and not active_low.broken:
            # Hold-side touch (from above) within at_tol, before/at level
            if (
                p >= active_low.price_pts - 1e-12
                and p - active_low.price_pts <= cfg.at_tol_pts + 1e-12
            ):
                if active_low.first_touch_ts_ns is None:
                    active_low.first_touch_ts_ns = t
                active_low.last_touch_ts_ns = t
                if active_low.touch_times_ns is not None:
                    active_low.touch_times_ns.append(t)
            if p <= active_low.price_pts + 1e-12:
                if active_low.first_break_ts_ns is None:
                    active_low.first_break_ts_ns = t
                if p <= active_low.price_pts - cfg.invalidate_beyond_pts + 1e-12:
                    active_low.broken = True
                    active_low = None

    return levels


def _active_at(
    levels: list[ActiveLevel],
    kind: str,
    asof_ns: int,
) -> ActiveLevel | None:
    """Latest level of kind with confirm_ts <= asof (still the active one at asof).

    Reconstructs which level was active: among levels confirmed by asof, pick
    the one that was current (last activated before asof that wasn't superseded).
    """
    cands = [lv for lv in levels if lv.kind == kind and lv.confirm_ts_ns <= asof_ns]
    if not cands:
        return None
    # Last confirmed before asof is the active one (activation replaces prior)
    return max(cands, key=lambda lv: lv.confirm_ts_ns)


@dataclass
class LevelLabel:
    state: str  # through_0 | through_2.5 | through_5 | at | none
    swing_px: float
    cluster_n: int
    pullaway_pts: float
    secs_since_break: float
    break_window_sec: float
    buffer_satisfied: tuple[float, ...]  # which through buffers this signal meets


def label_signal(
    side: int,
    entry_ts_ns: int,
    entry_px: float,
    levels: list[ActiveLevel],
    *,
    break_window_sec: float = 60.0,
    buffers: tuple[float, ...] = (0.0, 2.5, 5.0),
    at_tol_pts: float = 2.5,
) -> LevelLabel:
    """Label one MAT signal vs the active swing of the trade direction."""
    kind = "HIGH" if side > 0 else "LOW"
    lv = _active_at(levels, kind, entry_ts_ns)
    empty = LevelLabel(
        state="none",
        swing_px=float("nan"),
        cluster_n=0,
        pullaway_pts=float("nan"),
        secs_since_break=float("nan"),
        break_window_sec=break_window_sec,
        buffer_satisfied=(),
    )
    if lv is None:
        return empty

    win_ns = int(break_window_sec * 1e9)
    beyond = (entry_px - lv.price_pts) if side > 0 else (lv.price_pts - entry_px)
    dist = abs(entry_px - lv.price_pts)

    secs_since = float("nan")
    if lv.first_break_ts_ns is not None and entry_ts_ns >= lv.first_break_ts_ns:
        secs_since = (entry_ts_ns - lv.first_break_ts_ns) / 1e9

    satisfied: list[float] = []
    in_window = (
        lv.first_break_ts_ns is not None
        and entry_ts_ns >= lv.first_break_ts_ns
        and entry_ts_ns - lv.first_break_ts_ns <= win_ns
    )
    if in_window and beyond >= -1e-12:
        for b in buffers:
            if beyond >= b - 1e-12:
                satisfied.append(b)

    if satisfied:
        # Primary state tag = largest buffer met (headline uses through_5)
        top = max(satisfied)
        state = f"through_{top:g}"
    elif dist <= at_tol_pts + 1e-12 and not in_window:
        state = "at"
    elif dist <= at_tol_pts + 1e-12 and lv.first_break_ts_ns is None:
        state = "at"
    else:
        state = "none"

    return LevelLabel(
        state=state,
        swing_px=lv.price_pts,
        cluster_n=lv.cluster_n,
        pullaway_pts=lv.pullaway_pts,
        secs_since_break=secs_since,
        break_window_sec=break_window_sec,
        buffer_satisfied=tuple(satisfied),
    )


@dataclass
class SrLevelLabel:
    state: str  # bounce_0 | bounce_2.5 | bounce_5 | at | none
    swing_px: float
    cluster_n: int
    pullaway_pts: float
    secs_since_touch: float
    bounce_window_sec: float
    buffer_satisfied: tuple[float, ...]


def label_signal_sr(
    side: int,
    entry_ts_ns: int,
    entry_px: float,
    levels: list[ActiveLevel],
    *,
    bounce_window_sec: float = 60.0,
    buffers: tuple[float, ...] = (0.0, 2.5, 5.0),
    at_tol_pts: float = 2.5,
) -> SrLevelLabel:
    """Label MAT vs S/R fade level: long↔LOW support, short↔HIGH resistance."""
    kind = "LOW" if side > 0 else "HIGH"
    lv = _active_at(levels, kind, entry_ts_ns)
    empty = SrLevelLabel(
        state="none",
        swing_px=float("nan"),
        cluster_n=0,
        pullaway_pts=float("nan"),
        secs_since_touch=float("nan"),
        bounce_window_sec=bounce_window_sec,
        buffer_satisfied=(),
    )
    if lv is None:
        return empty
    # Broken through → not a hold; exclude from bounce/at
    if lv.first_break_ts_ns is not None and entry_ts_ns >= lv.first_break_ts_ns:
        return SrLevelLabel(
            state="none",
            swing_px=lv.price_pts,
            cluster_n=lv.cluster_n,
            pullaway_pts=lv.pullaway_pts,
            secs_since_touch=float("nan"),
            bounce_window_sec=bounce_window_sec,
            buffer_satisfied=(),
        )

    win_ns = int(bounce_window_sec * 1e9)
    # Hold-side distance: long above LOW, short below HIGH
    hold = (entry_px - lv.price_pts) if side > 0 else (lv.price_pts - entry_px)
    dist = abs(entry_px - lv.price_pts)

    # Recent touch as-of entry (not end-of-session last_touch)
    touch_ts = None
    if lv.touch_times_ns:
        for t in reversed(lv.touch_times_ns):
            if t <= entry_ts_ns:
                touch_ts = t
                break
    secs_since = float("nan")
    if touch_ts is not None:
        secs_since = (entry_ts_ns - touch_ts) / 1e9

    in_window = (
        touch_ts is not None
        and entry_ts_ns - touch_ts <= win_ns
    )

    satisfied: list[float] = []
    if in_window and hold >= -1e-12:
        for b in buffers:
            if hold >= b - 1e-12:
                satisfied.append(b)

    if satisfied:
        top = max(satisfied)
        state = f"bounce_{top:g}"
    elif dist <= at_tol_pts + 1e-12:
        state = "at"
    else:
        state = "none"

    return SrLevelLabel(
        state=state,
        swing_px=lv.price_pts,
        cluster_n=lv.cluster_n,
        pullaway_pts=lv.pullaway_pts,
        secs_since_touch=secs_since,
        bounce_window_sec=bounce_window_sec,
        buffer_satisfied=tuple(satisfied),
    )


def touch_events(
    trades: pa.Table,
    levels: list[ActiveLevel],
    *,
    at_tol_pts: float = 2.5,
    min_cluster_n: int = 1,
) -> pa.Table:
    """First hold-side touch per level (before break); side = bounce direction.

    Uses ``first_touch_ts_ns`` from ``build_break_timeline``. ``trades`` /
    ``at_tol_pts`` kept for API symmetry with ``break_events``.
    """
    del trades, at_tol_pts  # timeline already stamped
    rows_ts: list[int] = []
    rows_side: list[int] = []
    rows_swing: list[float] = []
    rows_cn: list[int] = []
    rows_pull: list[float] = []

    for lv in levels:
        if lv.cluster_n < min_cluster_n:
            continue
        if lv.first_touch_ts_ns is None:
            continue
        # Touch at/after break is not a hold
        if (
            lv.first_break_ts_ns is not None
            and lv.first_touch_ts_ns >= lv.first_break_ts_ns
        ):
            continue
        side = 1 if lv.kind == "LOW" else -1
        rows_ts.append(int(lv.first_touch_ts_ns))
        rows_side.append(side)
        rows_swing.append(lv.price_pts)
        rows_cn.append(lv.cluster_n)
        rows_pull.append(lv.pullaway_pts)

    if not rows_ts:
        return pa.table({})
    order = np.argsort(np.asarray(rows_ts, dtype=np.int64))
    return pa.table({
        "event_ts_ns": np.asarray(rows_ts, dtype=np.int64)[order],
        "side": np.asarray(rows_side, dtype=np.int8)[order],
        "swing_px": np.asarray(rows_swing, dtype=np.float64)[order],
        "cluster_n": np.asarray(rows_cn, dtype=np.int32)[order],
        "pullaway_pts": np.asarray(rows_pull, dtype=np.float64)[order],
    })


def filter_hold_side_buffer(
    events: pa.Table,
    trades: pa.Table,
    *,
    buffer_pts: float,
) -> pa.Table:
    """Keep events whose stamp print is hold-side by >= buffer_pts from swing_px."""
    if events.num_rows == 0:
        return events
    cfg = SigSwingConfig()
    ts, px = _sorted_trades(trades, cfg)
    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy().astype(int)
    swing = events.column("swing_px").to_numpy().astype(np.float64)
    keep = []
    for i in range(len(ev_ts)):
        ei = int(np.searchsorted(ts, int(ev_ts[i]), side="left"))
        if ei >= len(ts):
            continue
        entry = float(px[ei])
        hold = (entry - float(swing[i])) if sides[i] > 0 else (float(swing[i]) - entry)
        if hold >= buffer_pts - 1e-12:
            keep.append(i)
    if not keep:
        return pa.table({})
    return events.take(np.asarray(keep, dtype=np.int64))


def break_events(
    trades: pa.Table,
    levels: list[ActiveLevel],
    *,
    buffer_pts: float = 0.0,
    min_cluster_n: int = 1,
) -> pa.Table:
    """First print beyond level+buffer per level (one event per level)."""
    cfg = SigSwingConfig()
    if trades.num_rows == 0:
        return pa.table({})
    ts, px = _sorted_trades(trades, cfg)

    # Re-walk with only levels that meet cluster filter; fire at buffer
    # Use levels list with confirm times — find first print after confirm beyond level+buf
    rows_ts: list[int] = []
    rows_side: list[int] = []
    rows_swing: list[float] = []
    rows_buf: list[float] = []
    rows_cn: list[int] = []
    rows_pull: list[float] = []

    for lv in levels:
        if lv.cluster_n < min_cluster_n:
            continue
        side = 1 if lv.kind == "HIGH" else -1
        thresh = lv.price_pts + buffer_pts if side > 0 else lv.price_pts - buffer_pts
        # search first print after confirm
        i0 = int(np.searchsorted(ts, lv.confirm_ts_ns, side="left"))
        fire_ts = None
        for i in range(i0, len(ts)):
            p = float(px[i])
            if side > 0 and p >= thresh - 1e-12:
                fire_ts = int(ts[i])
                break
            if side < 0 and p <= thresh + 1e-12:
                fire_ts = int(ts[i])
                break
        if fire_ts is None:
            continue
        rows_ts.append(fire_ts)
        rows_side.append(side)
        rows_swing.append(lv.price_pts)
        rows_buf.append(buffer_pts)
        rows_cn.append(lv.cluster_n)
        rows_pull.append(lv.pullaway_pts)

    if not rows_ts:
        return pa.table({})
    order = np.argsort(np.asarray(rows_ts, dtype=np.int64))
    return pa.table({
        "event_ts_ns": np.asarray(rows_ts, dtype=np.int64)[order],
        "side": np.asarray(rows_side, dtype=np.int8)[order],
        "swing_px": np.asarray(rows_swing, dtype=np.float64)[order],
        "buffer_pts": np.asarray(rows_buf, dtype=np.float64)[order],
        "cluster_n": np.asarray(rows_cn, dtype=np.int32)[order],
        "pullaway_pts": np.asarray(rows_pull, dtype=np.float64)[order],
    })


def mat_confirm_breaks(
    break_ev: pa.Table,
    nq_burst: pa.Table,
    es_burst: pa.Table,
    *,
    sync_ms: int = 1500,
    max_es_lag_ms: float = 250.0,
) -> pa.Table:
    """Keep break events that get a same-side MAT causal burst at/after break within sync.

    Stamp = max(break_ts, mat event_ts). Uses cross_confirm on bursts then matches
    to breaks: NQ burst in [break, break+sync] with ES causal confirm and lag<=max.
    """
    from research.features.momentum import cross_confirm_bursts

    if break_ev.num_rows == 0:
        return pa.table({})
    mat = cross_confirm_bursts(nq_burst, es_burst, sync_ms=sync_ms, stamp="causal")
    if mat.num_rows == 0:
        return pa.table({})
    lag = mat.column("es_lag_ms").to_numpy()
    mat = mat.take(np.where(lag <= max_es_lag_ms)[0])
    if mat.num_rows == 0:
        return pa.table({})

    b_ts = break_ev.column("event_ts_ns").to_numpy().astype(np.int64)
    b_side = break_ev.column("side").to_numpy().astype(int)
    m_ts = mat.column("event_ts_ns").to_numpy().astype(np.int64)
    m_side = mat.column("side").to_numpy().astype(int)
    sync_ns = int(sync_ms * 1_000_000)

    keep_b: list[int] = []
    stamp_ts: list[int] = []
    for i in range(len(b_ts)):
        lo = int(np.searchsorted(m_ts, b_ts[i], side="left"))
        hi = int(np.searchsorted(m_ts, b_ts[i] + sync_ns, side="right"))
        best = None
        for j in range(lo, hi):
            if int(m_side[j]) != int(b_side[i]):
                continue
            if best is None or m_ts[j] < best:
                best = int(m_ts[j])
        if best is not None:
            keep_b.append(i)
            stamp_ts.append(max(int(b_ts[i]), best))

    if not keep_b:
        return pa.table({})
    idx = np.asarray(keep_b, dtype=np.int64)
    out = break_ev.take(idx)
    out = out.set_column(
        out.schema.get_field_index("event_ts_ns"),
        "event_ts_ns",
        pa.array(stamp_ts, type=pa.int64()),
    )
    return out


def prepare_session_levels(
    trades: pa.Table,
    cfg: SigSwingConfig | None = None,
    *,
    use_cluster: bool = False,
) -> tuple[list[SigPivot], list[ActiveLevel]]:
    """Pivots (+ optional cluster merge) and break timeline for one session."""
    cfg = cfg or SigSwingConfig()
    pivots = detect_pullaway_pivots(trades, cfg)
    if use_cluster:
        pivots = cluster_pivots(pivots, tol_pts=cfg.cluster_tol_pts)
    levels = build_break_timeline(trades, pivots, cfg)
    return pivots, levels
