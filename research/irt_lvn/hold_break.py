"""Hold / break / ambiguous universe at frozen IRT levels (EXP-0043 / H34)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pyarrow as pa

from research.irt_lvn.events import PTS_PER_TICK, _active_levels, _asof_idx
from research.irt_lvn.load_irt import IrtBars


class HoldBreakConfig:
    pad_pts: float = 3.0
    impulse_pts: float = 30.0
    impulse_lookback_ms: int = 60_000
    decision_horizon_ms: int = 120_000
    reject_pts: float = 30.0
    pierce_max_hold: float = 15.0
    pierce_min_break: float = 15.0
    extension_break_pts: float = 30.0
    cluster_tol_pts: float = 5.0
    cooldown_ms: int = 30_000
    # DOP is the NY open print; a "touch" in the first minutes is tautological.
    dop_exclude_first_min: float = 5.0

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)


def _impulse_ok(
    ts: np.ndarray,
    px: np.ndarray,
    t_touch: int,
    level: float,
    approach: int,
    *,
    lookback_ns: int,
    impulse_pts: float,
) -> tuple[bool, float, float, float]:
    """Prior 60s path travels ≥ impulse_pts toward the level."""
    t0 = t_touch - lookback_ns
    lo = int(np.searchsorted(ts, t0, side="left"))
    hi = int(np.searchsorted(ts, t_touch, side="left"))
    if hi <= lo:
        return False, 0.0, 0.0, 0.0
    seg = px[lo:hi]
    if approach > 0:  # from below
        start = float(seg.min())
        leg = float(level - start)
    else:  # from above
        start = float(seg.max())
        leg = float(start - level)
    dur_s = max(1e-6, (t_touch - int(ts[lo])) / 1e9)
    return leg >= impulse_pts - 1e-9, leg, dur_s, leg / dur_s


def _leg_shape(
    ts: np.ndarray,
    px: np.ndarray,
    t_touch: int,
    level: float,
    approach: int,
    *,
    lookback_ns: int,
    last_ns: int = 10_000_000_000,
) -> tuple[float, float]:
    """Approach-leg shape over the impulse lookback.

    Returns (last10_frac, max_pullback_pts): share of the leg covered in the
    final ``last_ns`` before touch (1 = all of it; a late burst), and the
    largest retrace against the approach direction within the leg.
    """
    t0 = t_touch - lookback_ns
    lo = int(np.searchsorted(ts, t0, side="left"))
    hi = int(np.searchsorted(ts, t_touch, side="left"))
    if hi - lo < 2:
        return 0.0, 0.0
    seg = px[lo:hi] * (1.0 if approach > 0 else -1.0)  # oriented: up = toward level
    lvl = level * (1.0 if approach > 0 else -1.0)
    start = float(seg.min())
    leg = max(1e-9, lvl - start)
    k = int(np.searchsorted(ts[lo:hi], t_touch - last_ns, side="left"))
    p_last = float(seg[k]) if k < len(seg) else float(seg[-1])
    last_frac = float(max(0.0, min(1.0, (lvl - p_last) / leg)))
    run_max = np.maximum.accumulate(seg)
    pullback = float(np.max(run_max - seg))
    return last_frac, pullback


def _classify_path(
    ts: np.ndarray,
    px: np.ndarray,
    t_touch: int,
    level: float,
    approach: int,
    *,
    horizon_ns: int,
    reject_pts: float,
    pierce_max_hold: float,
    pierce_min_break: float,
    extension_break_pts: float,
) -> tuple[str, float, int, int]:
    """Classify HOLD / BREAK / AMBIGUOUS over the decision horizon.

    Returns (label, first_pierce_pts, t_beyond_ns, t_decision_ns).
    Missing times are -1.
    """
    t_end = t_touch + horizon_ns
    lo = int(np.searchsorted(ts, t_touch, side="left"))
    hi = int(np.searchsorted(ts, t_end, side="right"))
    if hi <= lo:
        return "AMBIGUOUS", 0.0, -1, -1

    seg_ts = ts[lo:hi]
    seg_px = px[lo:hi]
    t_beyond = -1
    first_pierce = 0.0
    t_reject = -1
    t_ext = -1
    max_pierce = 0.0

    for i in range(len(seg_px)):
        p = float(seg_px[i])
        t = int(seg_ts[i])
        if approach > 0:  # resistance: beyond = above
            pierce = max(0.0, p - level)
            if t_beyond < 0 and p > level:
                t_beyond = t
            if first_pierce == 0.0 and pierce > 0:
                first_pierce = pierce
            max_pierce = max(max_pierce, pierce)
            if t_reject < 0 and p <= level - reject_pts:
                t_reject = t
            if t_ext < 0 and p >= level + extension_break_pts:
                t_ext = t
        else:  # support: beyond = below
            pierce = max(0.0, level - p)
            if t_beyond < 0 and p < level:
                t_beyond = t
            if first_pierce == 0.0 and pierce > 0:
                first_pierce = pierce
            max_pierce = max(max_pierce, pierce)
            if t_reject < 0 and p >= level + reject_pts:
                t_reject = t
            if t_ext < 0 and p <= level - extension_break_pts:
                t_ext = t

        # Reclaim-first blocks break
        if t_reject >= 0 and (t_ext < 0 or t_reject <= t_ext):
            pierce_out = first_pierce if first_pierce else max_pierce
            if max_pierce < pierce_max_hold:
                return "HOLD", pierce_out, t_beyond, t_reject
            return "AMBIGUOUS", pierce_out, t_beyond, t_reject

        if t_ext >= 0 and (t_reject < 0 or t_ext < t_reject):
            pierce_out = first_pierce if first_pierce else max_pierce
            if max_pierce >= pierce_min_break:
                return "BREAK", pierce_out, t_beyond, t_ext
            return "AMBIGUOUS", pierce_out, t_beyond, t_ext

    pierce_out = first_pierce if first_pierce else max_pierce
    return "AMBIGUOUS", pierce_out, t_beyond, -1


def detect_hold_break_events(
    trades: pa.Table,
    bars: IrtBars,
    *,
    cfg: HoldBreakConfig | None = None,
    window: tuple[int, int] | None = None,
    rth_open_ns: int | None = None,
) -> pa.Table:
    """Detect NQ hold/break/ambiguous events at clustered frozen IRT levels.

    Approach side: +1 from below (resistance test), -1 from above (support).
    A touch fires when a print crosses into (or through) the pad from outside.
    Levels frozen from the last completed IRT bar at first touch; **all active
    levels** within ``cluster_tol_pts`` of the touched level are merged into one
    event (centre = mean). Cooldown is per level id. ``HW_MT_DOP_NY`` is
    ignored for the first ``dop_exclude_first_min`` minutes after ``rth_open_ns``.
    """
    cfg = cfg or HoldBreakConfig()
    if trades.num_rows == 0 or len(bars.ts_ns) == 0:
        return pa.table({})

    ts0 = trades.column("ts_ns").to_numpy().astype(np.int64)
    px_ticks = trades.column("price").to_numpy().astype(np.float64)
    size = trades.column("size").to_numpy().astype(np.float64)
    order = np.argsort(ts0, kind="stable")
    ts = ts0[order]
    px = px_ticks[order] * PTS_PER_TICK
    sz = size[order]

    if window is not None:
        lo_w, hi_w = window
        m = (ts >= lo_w) & (ts <= hi_w)
        if not m.any():
            return pa.table({})
        # Keep lookback context before window for impulse
        lookback_ns = int(cfg.impulse_lookback_ms * 1_000_000)
        t_ctx = int(lo_w) - lookback_ns
        m_ctx = (ts >= t_ctx) & (ts <= hi_w)
        ts_all = ts[m_ctx]
        px_all = px[m_ctx]
        sz_all = sz[m_ctx]
        # Restrict touch detection to window
        m_win = (ts_all >= lo_w) & (ts_all <= hi_w)
    else:
        ts_all, px_all, sz_all = ts, px, sz
        m_win = np.ones(len(ts_all), dtype=bool)
        lookback_ns = int(cfg.impulse_lookback_ms * 1_000_000)

    horizon_ns = int(cfg.decision_horizon_ms * 1_000_000)
    cool_ns = int(cfg.cooldown_ms * 1_000_000)
    pad = float(cfg.pad_pts)
    tol = float(cfg.cluster_tol_pts)

    # Index of first print in window within ts_all
    win_idxs = np.where(m_win)[0]
    if len(win_idxs) == 0:
        return pa.table({})

    last_fire_by_lid: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    dop_cut_ns = (
        int(rth_open_ns + cfg.dop_exclude_first_min * 60_000_000_000)
        if rth_open_ns is not None else None
    )

    for ii in range(len(win_idxs)):
        i = int(win_idxs[ii])
        if i == 0:
            continue
        t = int(ts_all[i])
        p = float(px_all[i])
        prev = float(px_all[i - 1])
        bi = _asof_idx(bars.ts_ns, t)
        levels = _active_levels(bars, bi)
        if dop_cut_ns is not None and t < dop_cut_ns:
            levels = [L for L in levels if L[1] != "DOP"]
        if not levels:
            continue

        # Levels whose pad is crossed into (or jumped through) from outside.
        hits: list[tuple[str, str, float, int]] = []  # kind, lid, px, approach
        for kind, lid, level in levels:
            lo, hi = level - pad, level + pad
            if prev < lo <= p:
                hits.append((kind, lid, level, 1))
            elif prev > hi >= p:
                hits.append((kind, lid, level, -1))
        if not hits:
            continue

        approaches = {h[3] for h in hits}
        if len(approaches) != 1:
            continue  # one print entering two pads from opposite sides — skip
        approach = hits[0][3]

        # Cluster: all ACTIVE levels within tol of any touched level (transitive).
        cluster: list[tuple[str, str, float]] = [(h[0], h[1], h[2]) for h in hits]
        changed = True
        while changed:
            changed = False
            for kind, lid, level in levels:
                if any(c[1] == lid for c in cluster):
                    continue
                if any(abs(level - c[2]) <= tol for c in cluster):
                    cluster.append((kind, lid, level))
                    changed = True
        centre = float(np.mean([c[2] for c in cluster]))
        kinds = sorted({c[0] for c in cluster})
        lids = sorted({c[1] for c in cluster})

        # Per-level cooldown: skip if any member fired recently.
        if any(t - last_fire_by_lid.get(lid, -10**18) < cool_ns for lid in lids):
            continue

        ok, leg, dur_s, speed = _impulse_ok(
            ts_all, px_all, t, centre, approach,
            lookback_ns=lookback_ns,
            impulse_pts=cfg.impulse_pts,
        )
        if not ok:
            continue

        label, first_pierce, t_beyond, t_decision = _classify_path(
            ts_all, px_all, t, centre, approach,
            horizon_ns=horizon_ns,
            reject_pts=cfg.reject_pts,
            pierce_max_hold=cfg.pierce_max_hold,
            pierce_min_break=cfg.pierce_min_break,
            extension_break_pts=cfg.extension_break_pts,
        )

        lo_v = int(np.searchsorted(ts_all, t - lookback_ns, side="left"))
        hi_v = int(np.searchsorted(ts_all, t, side="left"))
        pre_vol = float(sz_all[lo_v:hi_v].sum()) if hi_v > lo_v else 0.0
        last10_frac, pullback = _leg_shape(ts_all, px_all, t, centre, approach, lookback_ns=lookback_ns)

        rows.append({
            "event_ts_ns": t,
            "t_touch_ns": t,
            "t_beyond_ns": t_beyond,
            "t_decision_ns": t_decision,
            "level_px": centre,
            "level_kinds": "|".join(kinds),
            "level_ids": "|".join(lids),
            "cluster_n": len(cluster),
            "approach": approach,  # +1 from_below, -1 from_above
            "approach_str": "from_below" if approach > 0 else "from_above",
            "label": label,
            "first_pierce_pts": float(first_pierce),
            "impulse_pts": float(leg),
            "impulse_dur_s": float(dur_s),
            "impulse_speed": float(speed),
            "pre_touch_vol": pre_vol,
            "leg_last10_frac": last10_frac,
            "leg_max_pullback_pts": pullback,
            "irt_bar_ts_ns": int(bars.ts_ns[bi]) if bi >= 0 else -1,
            "min_after_open": (
                float((t - rth_open_ns) / 60e9) if rth_open_ns is not None else float("nan")
            ),
        })
        for lid in lids:
            last_fire_by_lid[lid] = t

    if not rows:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.asarray([r["event_ts_ns"] for r in rows], dtype=np.int64),
        "t_touch_ns": np.asarray([r["t_touch_ns"] for r in rows], dtype=np.int64),
        "t_beyond_ns": np.asarray([r["t_beyond_ns"] for r in rows], dtype=np.int64),
        "t_decision_ns": np.asarray([r["t_decision_ns"] for r in rows], dtype=np.int64),
        "level_px": np.asarray([r["level_px"] for r in rows], dtype=np.float64),
        "level_kinds": pa.array([r["level_kinds"] for r in rows], type=pa.string()),
        "level_ids": pa.array([r["level_ids"] for r in rows], type=pa.string()),
        "cluster_n": np.asarray([r["cluster_n"] for r in rows], dtype=np.int32),
        "approach": np.asarray([r["approach"] for r in rows], dtype=np.int8),
        "approach_str": pa.array([r["approach_str"] for r in rows], type=pa.string()),
        "label": pa.array([r["label"] for r in rows], type=pa.string()),
        "first_pierce_pts": np.asarray([r["first_pierce_pts"] for r in rows], dtype=np.float64),
        "impulse_pts": np.asarray([r["impulse_pts"] for r in rows], dtype=np.float64),
        "impulse_dur_s": np.asarray([r["impulse_dur_s"] for r in rows], dtype=np.float64),
        "impulse_speed": np.asarray([r["impulse_speed"] for r in rows], dtype=np.float64),
        "pre_touch_vol": np.asarray([r["pre_touch_vol"] for r in rows], dtype=np.float64),
        "leg_last10_frac": np.asarray([r["leg_last10_frac"] for r in rows], dtype=np.float64),
        "leg_max_pullback_pts": np.asarray([r["leg_max_pullback_pts"] for r in rows], dtype=np.float64),
        "irt_bar_ts_ns": np.asarray([r["irt_bar_ts_ns"] for r in rows], dtype=np.int64),
        "min_after_open": np.asarray([r["min_after_open"] for r in rows], dtype=np.float64),
    })


def census_labels(events: pa.Table) -> dict[str, int]:
    out = {"HOLD": 0, "BREAK": 0, "AMBIGUOUS": 0}
    if events.num_rows == 0:
        return out
    for x in events.column("label").to_numpy(zero_copy_only=False):
        s = str(x)
        out[s] = out.get(s, 0) + 1
    return out


def stratified_sample(
    events: pa.Table,
    *,
    n_per_class: int = 100,
    min_per_class: int = 50,
    max_per_session: int = 12,
    seed: int = 42,
    labels: tuple[str, ...] = ("HOLD", "BREAK"),
    keep_ambiguous: bool = True,
) -> pa.Table:
    """Stratified sample by session + approach side; cap per day."""
    if events.num_rows == 0:
        return events
    if "session_id" not in events.column_names:
        raise ValueError("events need session_id for stratified sampling")

    rng = np.random.default_rng(seed)
    lab = events.column("label").to_numpy(zero_copy_only=False)
    sid = events.column("session_id").to_numpy(zero_copy_only=False)
    approach = events.column("approach_str").to_numpy(zero_copy_only=False)

    keep_idx: list[int] = []

    def _cap_session(idxs: list[int]) -> list[int]:
        by_sess: dict[str, list[int]] = defaultdict(list)
        for i in idxs:
            by_sess[str(sid[i])].append(i)
        capped: list[int] = []
        for members in by_sess.values():
            if len(members) <= max_per_session:
                capped.extend(members)
                continue
            below = [i for i in members if str(approach[i]) == "from_below"]
            above = [i for i in members if str(approach[i]) == "from_above"]
            rng.shuffle(below)
            rng.shuffle(above)
            pick: list[int] = []
            half = max_per_session // 2
            if below:
                pick.extend(below[: half if above else max_per_session])
            if above:
                pick.extend(above[: max_per_session - len(pick)])
            if len(pick) < max_per_session:
                rest = [i for i in members if i not in pick]
                rng.shuffle(rest)
                pick.extend(rest[: max_per_session - len(pick)])
            capped.extend(pick[:max_per_session])
        return capped

    for target in labels:
        idxs = [i for i in range(events.num_rows) if str(lab[i]) == target]
        if not idxs:
            continue
        capped = _cap_session(idxs)
        below = [i for i in capped if str(approach[i]) == "from_below"]
        above = [i for i in capped if str(approach[i]) == "from_above"]
        rng.shuffle(below)
        rng.shuffle(above)
        target_n = min(n_per_class, len(capped))
        if len(capped) < min_per_class:
            target_n = len(capped)  # take all
        half = target_n // 2
        picked = below[:half] + above[: target_n - min(half, len(above))]
        # refill
        if len(picked) < target_n:
            rest = [i for i in capped if i not in picked]
            rng.shuffle(rest)
            picked.extend(rest[: target_n - len(picked)])
        keep_idx.extend(picked[:target_n])

    if keep_ambiguous:
        amb = [i for i in range(events.num_rows) if str(lab[i]) == "AMBIGUOUS"]
        if amb:
            capped_a = _cap_session(amb)
            rng.shuffle(capped_a)
            keep_idx.extend(capped_a[:n_per_class])

    if not keep_idx:
        return pa.table({})
    keep_idx = sorted(set(keep_idx))
    return events.take(np.asarray(keep_idx, dtype=np.int64))
