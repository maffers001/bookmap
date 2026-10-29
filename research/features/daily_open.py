"""Daily Open (DOP) break detection and trap-side features (EXP-0078 / H69).

DOP = first traded price at/after UK session open (caller supplies open_ns).
After a grace window, volume builds on one side of the DOP (trap / false
side). The first print that pierces >= pierce_ticks through the DOP to the
opposite side is the break; trade side = break direction.

All features at the break stamp are causal (use only trades with ts <= stamp).
Prices in this module are parquet tick units unless ``_pts`` is in the name.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

PTS_PER_TICK = 0.25


@dataclass
class DopState:
    valid: bool
    dop_ticks: float = float("nan")
    dop_pts: float = float("nan")
    open_ts_ns: int = 0
    first_print_ts_ns: int = 0
    n_trades_to_open: int = 0


@dataclass
class DopBreak:
    event_ts_ns: int
    side: int  # +1 long (break above), -1 short (break below)
    entry_px_pts: float
    dop_ticks: float
    dop_pts: float
    trap_side: int  # +1 trap above (short setup), -1 trap below (long setup)
    # trap-side amount
    trap_vol: float
    trap_delta: float  # signed so trapped aggression is positive
    total_build_vol: float
    trap_share: float
    vol_above: float
    vol_below: float
    # shape
    trap_excursion_pts: float
    build_dur_sec: float
    extreme_vol_share: float  # share of trap_vol in extreme quartile of excursion
    path_efficiency: float
    time_extreme_to_break_sec: float
    vpoc_pts: float
    vpoc_on_trap: int
    # book1 brackets (pts)
    sl_pts: float
    tp_pts: float
    r_pts: float


def _sorted(trades: pa.Table) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64)
    sz = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    o = np.argsort(ts, kind="stable")
    return ts[o], px[o], sz[o], buy[o]


def session_dop(
    trades: pa.Table,
    open_ns: int,
    *,
    pts_per_tick: float = PTS_PER_TICK,
    max_first_print_delay_sec: float = 60.0,
) -> DopState:
    """First trade at/after ``open_ns`` defines the Daily Open."""
    if trades.num_rows == 0:
        return DopState(valid=False)
    ts, px, _, _ = _sorted(trades)
    k = ts >= open_ns
    if not k.any():
        return DopState(valid=False)
    i0 = int(np.argmax(k))  # first True
    first_ts = int(ts[i0])
    if first_ts - int(open_ns) > int(max_first_print_delay_sec * 1e9):
        return DopState(valid=False)
    dop = float(px[i0])
    return DopState(
        valid=True,
        dop_ticks=dop,
        dop_pts=dop * pts_per_tick,
        open_ts_ns=int(open_ns),
        first_print_ts_ns=first_ts,
        n_trades_to_open=int(i0),
    )


def detect_dop_break(
    trades: pa.Table,
    dop: DopState,
    open_ns: int,
    window_end_ns: int,
    *,
    grace_min: float = 5.0,
    pierce_ticks: float = 1.0,
    pts_per_tick: float = PTS_PER_TICK,
    v_extreme_frac: float = 0.25,
) -> DopBreak | None:
    """First DOP break after grace; trap side = majority build volume.

    Long: majority volume below DOP, then print >= dop + pierce.
    Short: majority volume above DOP, then print <= dop - pierce.
    Features computed on [open, break] inclusive of the break print's
    prior state (trap metrics exclude the break print itself).
    """
    if not dop.valid or trades.num_rows == 0:
        return None
    ts, px, sz, buy = _sorted(trades)
    grace_ns = open_ns + int(grace_min * 60 * 1e9)
    dop_t = float(dop.dop_ticks)
    pierce = float(pierce_ticks)

    vol_above = vol_below = 0.0
    delta_above = delta_below = 0.0  # buy - sell on each side
    # path for efficiency / excursion
    path_pts = 0.0
    last_px = dop_t
    max_above = 0.0  # ticks above dop
    max_below = 0.0  # ticks below dop
    extreme_ts_above = open_ns
    extreme_ts_below = open_ns
    # VAP for VPOC (tick -> vol)
    vap: dict[int, float] = {}
    # prints on trap side for extreme-quartile share (store as we go; recompute at break)
    below_prints: list[tuple[float, float]] = []  # (dist_below_ticks, size)
    above_prints: list[tuple[float, float]] = []

    for i in range(len(ts)):
        t = int(ts[i])
        if t < open_ns or t > window_end_ns:
            continue
        p = float(px[i])
        s = float(sz[i])
        is_buy = bool(buy[i])
        signed_delta = s if is_buy else -s

        # Update build stats for prints strictly before we test break
        # (current print can be the break trigger; trap metrics exclude it).
        if t < grace_ns or t >= grace_ns:
            # Always accumulate build until a break fires; break test only after grace.
            pass

        # Accumulate this print into build if we have not broken yet — but for
        # the break test we need trap side from prints *before* this one when
        # this print is the candidate break. So: test break using prior state,
        # then accumulate.
        if t >= grace_ns:
            total = vol_above + vol_below
            if total > 0:
                if vol_below > vol_above:
                    trap = -1  # below
                    if p >= dop_t + pierce:
                        return _finalize_break(
                            t, p, +1, trap, dop_t, open_ns,
                            vol_above, vol_below, delta_above, delta_below,
                            max_above, max_below, path_pts, last_px,
                            extreme_ts_above, extreme_ts_below,
                            vap, below_prints, above_prints,
                            pts_per_tick, v_extreme_frac,
                        )
                elif vol_above > vol_below:
                    trap = +1  # above
                    if p <= dop_t - pierce:
                        return _finalize_break(
                            t, p, -1, trap, dop_t, open_ns,
                            vol_above, vol_below, delta_above, delta_below,
                            max_above, max_below, path_pts, last_px,
                            extreme_ts_above, extreme_ts_below,
                            vap, below_prints, above_prints,
                            pts_per_tick, v_extreme_frac,
                        )
                else:
                    # Tie: use max excursion side as trap
                    if max_below >= max_above and max_below > 0:
                        trap = -1
                        if p >= dop_t + pierce:
                            return _finalize_break(
                                t, p, +1, trap, dop_t, open_ns,
                                vol_above, vol_below, delta_above, delta_below,
                                max_above, max_below, path_pts, last_px,
                                extreme_ts_above, extreme_ts_below,
                                vap, below_prints, above_prints,
                                pts_per_tick, v_extreme_frac,
                            )
                    elif max_above > max_below:
                        trap = +1
                        if p <= dop_t - pierce:
                            return _finalize_break(
                                t, p, -1, trap, dop_t, open_ns,
                                vol_above, vol_below, delta_above, delta_below,
                                max_above, max_below, path_pts, last_px,
                                extreme_ts_above, extreme_ts_below,
                                vap, below_prints, above_prints,
                                pts_per_tick, v_extreme_frac,
                            )

        # Accumulate print into build (including during grace)
        path_pts += abs(p - last_px) * pts_per_tick
        last_px = p
        row = int(round(p))
        vap[row] = vap.get(row, 0.0) + s
        if p > dop_t:
            vol_above += s
            delta_above += signed_delta
            dist = p - dop_t
            above_prints.append((dist, s))
            if dist > max_above:
                max_above = dist
                extreme_ts_above = t
        elif p < dop_t:
            vol_below += s
            delta_below += signed_delta
            dist = dop_t - p
            below_prints.append((dist, s))
            if dist > max_below:
                max_below = dist
                extreme_ts_below = t
        else:
            # At DOP: split half / half for share neutrality
            vol_above += s * 0.5
            vol_below += s * 0.5

    return None


def _finalize_break(
    t: int,
    p: float,
    side: int,
    trap: int,
    dop_t: float,
    open_ns: int,
    vol_above: float,
    vol_below: float,
    delta_above: float,
    delta_below: float,
    max_above: float,
    max_below: float,
    path_pts: float,
    last_px: float,
    extreme_ts_above: int,
    extreme_ts_below: int,
    vap: dict[int, float],
    below_prints: list[tuple[float, float]],
    above_prints: list[tuple[float, float]],
    pts_per_tick: float,
    v_extreme_frac: float,
) -> DopBreak:
    total = vol_above + vol_below
    if trap < 0:
        trap_vol = vol_below
        # trapped aggression = buy aggressors below DOP (positive when bulls trapped)
        trap_delta = delta_below
        excursion_ticks = max_below
        extreme_ts = extreme_ts_below
        side_prints = below_prints
    else:
        trap_vol = vol_above
        # trapped aggression = sell aggressors above DOP → -delta_above positive when bears trapped
        trap_delta = -delta_above
        excursion_ticks = max_above
        extreme_ts = extreme_ts_above
        side_prints = above_prints

    trap_share = float(trap_vol / total) if total > 0 else float("nan")
    excursion_pts = float(excursion_ticks * pts_per_tick)
    # extreme quartile: prints with dist >= (1 - v_extreme_frac) * max excursion
    thr = (1.0 - v_extreme_frac) * excursion_ticks if excursion_ticks > 0 else 0.0
    ext_vol = sum(s for d, s in side_prints if d >= thr - 1e-12)
    extreme_vol_share = float(ext_vol / trap_vol) if trap_vol > 0 else float("nan")

    net_from_dop = abs(float(last_px) - dop_t) * pts_per_tick
    efficiency = float(net_from_dop / path_pts) if path_pts > 1e-12 else float("nan")

    # VPOC
    if vap:
        vpoc_row = max(vap.items(), key=lambda kv: kv[1])[0]
        vpoc_pts = float(vpoc_row * pts_per_tick)
        dop_pts = dop_t * pts_per_tick
        if trap < 0:
            vpoc_on_trap = int(vpoc_pts < dop_pts)
        else:
            vpoc_on_trap = int(vpoc_pts > dop_pts)
    else:
        vpoc_pts = float("nan")
        vpoc_on_trap = 0

    entry_pts = float(p * pts_per_tick)
    dop_pts = float(dop_t * pts_per_tick)
    # Book 1: SL 1 tick beyond DOP on trap side
    if side > 0:
        sl_level = dop_pts - pts_per_tick
        r_pts = entry_pts - sl_level
    else:
        sl_level = dop_pts + pts_per_tick
        r_pts = sl_level - entry_pts
    r_pts = max(r_pts, pts_per_tick)
    tp_pts = 2.0 * r_pts
    sl_pts = r_pts

    return DopBreak(
        event_ts_ns=int(t),
        side=int(side),
        entry_px_pts=entry_pts,
        dop_ticks=dop_t,
        dop_pts=dop_pts,
        trap_side=int(trap),
        trap_vol=float(trap_vol),
        trap_delta=float(trap_delta),
        total_build_vol=float(total),
        trap_share=trap_share,
        vol_above=float(vol_above),
        vol_below=float(vol_below),
        trap_excursion_pts=excursion_pts,
        build_dur_sec=float((t - open_ns) / 1e9),
        extreme_vol_share=extreme_vol_share,
        path_efficiency=efficiency,
        time_extreme_to_break_sec=float((t - extreme_ts) / 1e9),
        vpoc_pts=vpoc_pts,
        vpoc_on_trap=vpoc_on_trap,
        sl_pts=float(sl_pts),
        tp_pts=float(tp_pts),
        r_pts=float(r_pts),
    )


def annotate_causal_trap_state(
    rows: list[dict],
    *,
    prior_n: int = 14,
) -> list[dict]:
    """Add trap_vol_z / trap_delta_z from prior sessions only (same market).

    ``rows`` chronological for ONE market; each needs ``trap_vol`` and
    ``trap_delta``. z = (x - median_prior) / (1.4826 * MAD); NaN until
    ``prior_n`` prior rows exist.
    """
    hist_vol: list[float] = []
    hist_delta: list[float] = []
    for r in rows:
        if len(hist_vol) >= prior_n:
            med_v = float(np.median(hist_vol[-prior_n:]))
            mad_v = float(np.median(np.abs(np.asarray(hist_vol[-prior_n:]) - med_v)))
            scale_v = 1.4826 * mad_v if mad_v > 1e-12 else float("nan")
            r["trap_vol_z"] = (
                float((r["trap_vol"] - med_v) / scale_v) if np.isfinite(scale_v) else float("nan")
            )
            r["trap_vol_vs_med"] = float(r["trap_vol"] / med_v) if med_v > 0 else float("nan")
            med_d = float(np.median(hist_delta[-prior_n:]))
            mad_d = float(np.median(np.abs(np.asarray(hist_delta[-prior_n:]) - med_d)))
            scale_d = 1.4826 * mad_d if mad_d > 1e-12 else float("nan")
            r["trap_delta_z"] = (
                float((r["trap_delta"] - med_d) / scale_d) if np.isfinite(scale_d) else float("nan")
            )
            r["prior_trap_vol_med"] = med_v
            r["prior_trap_delta_med"] = med_d
        else:
            r["trap_vol_z"] = float("nan")
            r["trap_delta_z"] = float("nan")
            r["trap_vol_vs_med"] = float("nan")
            r["prior_trap_vol_med"] = float("nan")
            r["prior_trap_delta_med"] = float("nan")
        if np.isfinite(r.get("trap_vol", np.nan)) and r["trap_vol"] > 0:
            hist_vol.append(float(r["trap_vol"]))
        if np.isfinite(r.get("trap_delta", np.nan)):
            hist_delta.append(float(r["trap_delta"]))
    return rows


def is_v_shape(
    bk: DopBreak | dict,
    *,
    extreme_vol_share_min: float = 0.40,
    excursion_pts_min: float = 5.0,
) -> bool:
    if isinstance(bk, DopBreak):
        evs = bk.extreme_vol_share
        exc = bk.trap_excursion_pts
    else:
        evs = float(bk.get("extreme_vol_share", np.nan))
        exc = float(bk.get("trap_excursion_pts", np.nan))
    return bool(
        np.isfinite(evs) and evs >= extreme_vol_share_min
        and np.isfinite(exc) and exc >= excursion_pts_min
    )
