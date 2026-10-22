"""EXP-0044 / H35 — dwell near IRT level with intact break-side wall.

Causal state machine after first touch:
  * vol unit ``u`` = median NQ minute-range over prior 30 RTH minutes
  * pierce_min / pierce_max / dwell band are clipped multiples of ``u``
  * strata: ``touch_dwell`` (never hit pierce_min) vs ``pierce_dwell``
  * break-side intact: consume_frac of reference beyond stack ≤ consume_max
    and remaining stack ≥ stack_remain_min × reference
  * Arm A: fade at first t ≥ touch+min_dwell with dwell+intact
  * Arm B: same eligibility, then first hold-side large take on NQ or ES
  * protective stack: post-extreme hold-side MBO net add that stays unfilled
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from research.features.regime import build_regime_index, regime_at
from research.irt_lvn.confirm import bracket_pnl, pack_pnl
from research.irt_lvn.events import PTS_PER_TICK
from research.irt_lvn.of_scores import (
    NS_PER_MS,
    _SessionOF,
    _band_ticks,
    _beyond_hold_sides,
    _book_band_stats,
    _mbo_band_flow,
    _snapshot_books_at_times,
)


@dataclass
class DwellConfig:
    vol_lookback_min: int = 30
    pierce_min_k: float = 1.0
    pierce_min_floor: float = 6.0
    pierce_min_cap: float = 20.0
    pierce_max_k: float = 2.0
    pierce_max_floor: float = 12.0
    pierce_max_cap: float = 30.0
    band_k: float = 0.75
    band_floor: float = 6.0
    band_cap: float = 18.0
    min_dwell_ms: int = 15_000
    max_dwell_ms: int = 90_000
    consume_max: float = 0.25
    stack_remain_min: float = 0.5
    protect_consume_max: float = 0.2
    reject_pts: float = 30.0
    extension_pts: float = 30.0
    tp_pts: float = 30.0
    sl_pts: float = 15.0
    slip_ticks: float = 1.0
    commission_pts_rt: float = 0.25
    horizon_ms: int = 120_000
    tick_radius: int = 8
    far_band_ticks: int = 20
    es_large_pct: float = 99.0
    large_take_sync_ms: int = 2_000
    # path sampling while scanning dwell (ms)
    scan_step_ms: int = 250

    @property
    def slip_pts(self) -> float:
        return float(self.slip_ticks) * PTS_PER_TICK


def _clip(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, x)))


def vol_unit_at(bars, asof_ns: int, window_min: int) -> float:
    snap = regime_at(bars, asof_ns=asof_ns, window_min=window_min)
    return float(max(snap.median_minute_range, 0.25))


def vol_thresholds(u: float, cfg: DwellConfig) -> tuple[float, float, float]:
    pmin = _clip(cfg.pierce_min_k * u, cfg.pierce_min_floor, cfg.pierce_min_cap)
    pmax = _clip(cfg.pierce_max_k * u, cfg.pierce_max_floor, cfg.pierce_max_cap)
    band = _clip(cfg.band_k * u, cfg.band_floor, cfg.band_cap)
    return pmin, pmax, band


def _consume_in_band(
    sof: _SessionOF, t0: int, t1: int, *, beyond_buy: bool, band_lo: int, band_hi: int
) -> float:
    if t1 <= t0 or len(sof.t_ts) == 0:
        return 0.0
    i0 = int(np.searchsorted(sof.t_ts, t0, side="left"))
    i1 = int(np.searchsorted(sof.t_ts, t1, side="right"))
    if i1 <= i0:
        return 0.0
    sl = slice(i0, i1)
    m = (sof.t_buy[sl] == beyond_buy) & (sof.t_px[sl] >= band_lo) & (sof.t_px[sl] <= band_hi)
    return float(sof.t_sz[sl][m].sum()) if m.any() else 0.0


def _hold_consume_in_band(
    sof: _SessionOF, t0: int, t1: int, *, hold_buy: bool, band_lo: int, band_hi: int
) -> float:
    return _consume_in_band(sof, t0, t1, beyond_buy=hold_buy, band_lo=band_lo, band_hi=band_hi)


def _nq_large_take_times(
    sof: _SessionOF, *, hold_book: str, t0: int, t1: int, min_exec: float
) -> list[int]:
    """Contact times of hold-side large levels being traded into in [t0, t1]."""
    out: list[int] = []
    for lv in sof.closed_levels:
        if not getattr(lv, "contacted", False) or lv.contact_ns <= 0:
            continue
        if lv.side != hold_book:
            continue
        if not (t0 <= int(lv.contact_ns) <= t1):
            continue
        if float(lv.executed_into) < min_exec:
            continue
        out.append(int(lv.contact_ns))
    out.sort()
    return out


def _es_large_take_times(
    es_ts: np.ndarray,
    es_sz: np.ndarray,
    es_buy: np.ndarray,
    *,
    hold_buy: bool,
    t0: int,
    t1: int,
    size_thr: float,
) -> list[int]:
    """ES aggressor prints toward hold side with size ≥ thr in [t0, t1]."""
    if len(es_ts) == 0 or size_thr <= 0:
        return []
    lo = int(np.searchsorted(es_ts, t0, side="left"))
    hi = int(np.searchsorted(es_ts, t1, side="right"))
    if hi <= lo:
        return []
    sl = slice(lo, hi)
    m = (es_buy[sl] == hold_buy) & (es_sz[sl] >= size_thr)
    if not m.any():
        return []
    return [int(x) for x in es_ts[sl][m]]


def evaluate_dwell_event(
    sof: _SessionOF,
    *,
    t_touch: int,
    level_px: float,
    approach: int,
    label: str,
    cfg: DwellConfig,
    vol_u: float,
    es_ts: np.ndarray,
    es_sz: np.ndarray,
    es_buy: np.ndarray,
    book_at: dict[int, tuple[dict[int, int], dict[int, int]]] | None = None,
) -> dict[str, Any]:
    """Return feature + arm P&L dict for one touch (nan P&L when arm did not fire)."""
    pmin, pmax, band = vol_thresholds(vol_u, cfg)
    min_ns = int(cfg.min_dwell_ms * NS_PER_MS)
    max_ns = int(cfg.max_dwell_ms * NS_PER_MS)
    hor_ns = int(cfg.horizon_ms * NS_PER_MS)
    step_ns = int(cfg.scan_step_ms * NS_PER_MS)
    slip, comm = cfg.slip_pts, cfg.commission_pts_rt
    fade_side = -int(approach)

    beyond_book, hold_book, beyond_buy, hold_buy = _beyond_hold_sides(approach)
    near_lo, near_hi, far_lo, far_hi = _band_ticks(level_px, approach, cfg.tick_radius, cfg.far_band_ticks)
    band_lo = min(near_lo, near_hi, far_lo, far_hi)
    band_hi = max(near_lo, near_hi, far_lo, far_hi)
    lvl_tick = int(round(level_px / PTS_PER_TICK))
    sgn = 1.0 if approach > 0 else -1.0

    ts = sof.t_ts
    px_pts = sof.t_px.astype(np.float64) * PTS_PER_TICK

    i0 = int(np.searchsorted(ts, t_touch, side="left"))
    i_end = int(np.searchsorted(ts, t_touch + max_ns + hor_ns, side="right"))
    nan = float("nan")
    base = {
        "vol_u": vol_u, "pierce_min": pmin, "pierce_max_elig": pmax, "dwell_band": band,
        "stratum": "", "max_pierce": 0.0, "t_pierce_extreme_ns": float(nan),
        "t_clock0_ns": float(t_touch),         "eligible": 0.0, "dwell_s_at_a": float(nan), "intact_dwell_s": float(nan),
        "consume_frac_a": float(nan), "stack_remain_frac_a": float(nan),
        "protect_add": 0.0, "protect_consume_frac": float(nan), "protect_inside_extreme": 0.0,
        "arm_a_fire": 0.0, "arm_b_fire": 0.0, "arm_b_source": 0.0,  # 1=NQ, 2=ES
        "t_entry_a_ns": float(nan), "t_entry_b_ns": float(nan),
        "pnl_arm_a": float(nan), "pnl_arm_b": float(nan),
        "pnl_arm_a_protect": float(nan),
        # survival entries at longer dwell thresholds (duration claim)
        "pnl_dwell_15": float(nan), "pnl_dwell_20": float(nan),
        "pnl_dwell_30": float(nan), "pnl_dwell_45": float(nan),
        "alive_15": 0.0, "alive_20": 0.0, "alive_30": 0.0, "alive_45": 0.0,
        "abort_reason": "",
        "label": label,
    }

    if i_end <= i0:
        base["abort_reason"] = "no_path"
        return base

    seg_ts = ts[i0:i_end]
    seg_px = px_pts[i0:i_end]
    beyond = (seg_px - level_px) * sgn
    hold_away = -beyond

    # pierce path stats within max dwell window
    i_dwell = int(np.searchsorted(seg_ts, t_touch + max_ns, side="right"))
    if i_dwell <= 0:
        base["abort_reason"] = "no_dwell_path"
        return base
    dwell_beyond = beyond[:i_dwell]
    max_pierce = float(max(0.0, dwell_beyond.max())) if len(dwell_beyond) else 0.0
    base["max_pierce"] = max_pierce

    if max_pierce >= cfg.extension_pts or max_pierce > pmax:
        # path already a break / too deep — not a dwell candidate
        base["abort_reason"] = "path_break_or_too_deep"
        base["stratum"] = "pierce_dwell" if max_pierce >= pmin else "touch_dwell"
        return base

    # pierce extreme time (first print achieving max pierce in dwell window)
    if max_pierce >= pmin:
        stratum = "pierce_dwell"
        k_ext = int(np.argmax(dwell_beyond))
        t_extreme = int(seg_ts[k_ext])
        clock0 = t_extreme
    else:
        stratum = "touch_dwell"
        t_extreme = t_touch
        clock0 = t_touch
    base["stratum"] = stratum
    base["t_pierce_extreme_ns"] = float(t_extreme)
    base["t_clock0_ns"] = float(clock0)

    # book snapshots: reference (touch or extreme) + candidate decision times
    times = [t_touch, t_extreme]
    t_scan = clock0 + min_ns
    t_deadline = clock0 + max_ns
    while t_scan <= t_deadline:
        times.append(int(t_scan))
        t_scan += step_ns
    if book_at is None:
        book_at = _snapshot_books_at_times(sof, times)
    else:
        missing = [t for t in times if t not in book_at]
        if missing:
            book_at.update(_snapshot_books_at_times(sof, missing))

    def _beyond_sum(t: int) -> float:
        bids, asks = book_at.get(t, (sof.init_bids, sof.init_asks))
        bdict = asks if beyond_book == "ASK" else bids
        near = _book_band_stats(bdict, near_lo, near_hi, sof.large_thr)
        far = _book_band_stats(bdict, far_lo, far_hi, sof.large_thr)
        return near["sum"] + far["sum"]

    ref_t = t_extreme if stratum == "pierce_dwell" else t_touch
    stack0 = _beyond_sum(ref_t)
    if stack0 <= 0:
        # fall back to touch book
        stack0 = _beyond_sum(t_touch)
    if stack0 <= 0:
        base["abort_reason"] = "no_break_side_stack"
        return base

    # protective band: between level and pierce extreme (hold side of extreme)
    if approach > 0:
        prot_lo, prot_hi = lvl_tick, int(round((level_px + max_pierce) / PTS_PER_TICK))
    else:
        prot_lo = int(round((level_px - max_pierce) / PTS_PER_TICK))
        prot_hi = lvl_tick
    if prot_lo > prot_hi:
        prot_lo, prot_hi = prot_hi, prot_lo

    def _intact_at(t_dec: int) -> tuple[bool, float, float]:
        consumed = _consume_in_band(sof, ref_t, t_dec, beyond_buy=beyond_buy, band_lo=band_lo, band_hi=band_hi)
        cf = consumed / stack0
        rem = _beyond_sum(t_dec)
        rf = rem / stack0
        ok = cf <= cfg.consume_max and rf >= cfg.stack_remain_min
        return ok, cf, rf

    def _in_band_and_undecided(t_dec: int) -> tuple[bool, str]:
        j = int(np.searchsorted(seg_ts, t_dec, side="right")) - 1
        if j < 0:
            return False, "no_px"
        # path from clock0 to t_dec must stay in band and not reject/extend
        j0 = int(np.searchsorted(seg_ts, clock0, side="left"))
        if j < j0:
            return False, "no_px"
        sub_b = beyond[j0:j + 1]
        sub_h = hold_away[j0:j + 1]
        if len(sub_b) == 0:
            return False, "no_px"
        if float(sub_b.max()) >= cfg.extension_pts:
            return False, "extension"
        if float(sub_h.max()) >= cfg.reject_pts:
            return False, "reject"
        if stratum == "pierce_dwell":
            # pocket: hold-side within band of level; beyond-side up to max(band, max_pierce)
            # but do not push a new extreme past the reference pierce (+1 pt slack)
            if float(sub_h.max()) > band:
                return False, "left_band"
            if float(sub_b.max()) > pmax:
                return False, "re_pierce_deep"
            if float(sub_b.max()) > max_pierce + 1.0:
                return False, "re_pierce_deep"
        else:
            dist = np.abs(seg_px[j0:j + 1] - level_px)
            if float(dist.max()) > band:
                return False, "left_band"
        return True, ""

    # find Arm A entry: first scan time ≥ clock0+min with intact + in band
    t_a = -1
    cf_a = rf_a = nan
    abort = "deadline"
    t_cand = clock0 + min_ns
    while t_cand <= t_deadline:
        ok_path, why = _in_band_and_undecided(t_cand)
        if not ok_path:
            abort = why
            if why in ("extension", "reject", "left_band", "re_pierce_deep"):
                break
            t_cand += step_ns
            continue
        ok_i, cf, rf = _intact_at(t_cand)
        if ok_i:
            t_a = int(t_cand)
            cf_a, rf_a = cf, rf
            abort = ""
            break
        abort = "stack_eaten"
        t_cand += step_ns

    base["abort_reason"] = abort

    # intact dwell length: scan from clock0 to deadline while path+stack hold
    t_fail = clock0
    t_scan2 = clock0 + step_ns
    while t_scan2 <= t_deadline:
        ok_path, why = _in_band_and_undecided(t_scan2)
        if not ok_path:
            break
        ok_i, _, _ = _intact_at(t_scan2)
        if not ok_i:
            break
        t_fail = t_scan2
        t_scan2 += step_ns
    else:
        t_fail = t_deadline
    base["intact_dwell_s"] = (t_fail - clock0) / 1e9

    if t_a < 0:
        t_feat = int(t_fail) if t_fail > clock0 else min(t_deadline, int(seg_ts[min(len(seg_ts) - 1, i_dwell - 1)]))
    else:
        t_feat = t_a
        base["eligible"] = 1.0
        base["dwell_s_at_a"] = (t_a - clock0) / 1e9
        base["consume_frac_a"] = cf_a
        base["stack_remain_frac_a"] = rf_a

    # protective stack: MBO net add on hold side in protect band after extreme/touch
    a_add, a_can, _, _ = _mbo_band_flow(
        sof, ref_t, t_feat, want_bid=(hold_book == "BID"), px_lo=prot_lo, px_hi=prot_hi
    )
    net_prot = max(0.0, a_add - a_can)
    base["protect_add"] = net_prot
    if net_prot > 0:
        eaten = _hold_consume_in_band(
            sof, ref_t, t_feat, hold_buy=hold_buy, band_lo=prot_lo, band_hi=prot_hi
        )
        base["protect_consume_frac"] = eaten / net_prot
        base["protect_inside_extreme"] = 1.0 if stratum == "pierce_dwell" else 0.0
    else:
        base["protect_consume_frac"] = nan
        base["protect_inside_extreme"] = 0.0

    # survival entries at 15/20/30/45 s (duration claim) — enter if still alive then
    for thr_s, key in ((15, "15"), (20, "20"), (30, "30"), (45, "45")):
        t_thr = clock0 + int(thr_s * 1e9)
        if t_thr > t_deadline:
            continue
        ok_path, _ = _in_band_and_undecided(t_thr)
        ok_i, _, _ = _intact_at(t_thr)
        if ok_path and ok_i:
            base[f"alive_{key}"] = 1.0
            pnl_t, _, _ = bracket_pnl(
                ts, px_pts, t_thr, fade_side, cfg.tp_pts, cfg.sl_pts, t_thr + hor_ns, slip, comm
            )
            base[f"pnl_dwell_{key}"] = float(pnl_t)

    if t_a < 0:
        return base

    # Arm A P&L (first eligibility = 15 s when alive)
    pnl_a, _, _ = bracket_pnl(ts, px_pts, t_a, fade_side, cfg.tp_pts, cfg.sl_pts, t_a + hor_ns, slip, comm)
    base["arm_a_fire"] = 1.0
    base["t_entry_a_ns"] = float(t_a)
    base["pnl_arm_a"] = float(pnl_a)
    if not np.isfinite(base["pnl_dwell_15"]):
        base["pnl_dwell_15"] = float(pnl_a)
        base["alive_15"] = 1.0

    if (
        base["protect_add"] > 0
        and np.isfinite(base["protect_consume_frac"])
        and base["protect_consume_frac"] <= cfg.protect_consume_max
    ):
        base["pnl_arm_a_protect"] = float(pnl_a)

    # Arm B: first large take on hold side (NQ LargeLevel or ES large print) after eligibility
    nq_hits = _nq_large_take_times(
        sof, hold_book=hold_book, t0=t_a, t1=t_deadline, min_exec=max(10.0, float(sof.large_thr) * 0.5)
    )
    es_thr = 0.0
    if len(es_sz):
        pre = es_sz[es_ts < t_touch]
        if len(pre) >= 50:
            es_thr = float(np.percentile(pre, cfg.es_large_pct))
        elif len(es_sz) >= 50:
            es_thr = float(np.percentile(es_sz, cfg.es_large_pct))
    es_hits = _es_large_take_times(
        es_ts, es_sz, es_buy, hold_buy=hold_buy, t0=t_a, t1=t_deadline, size_thr=es_thr
    )
    # also NQ large aggressor proxy (symmetric to ES) toward hold side
    nq_thr = float(sof.large_thr)
    i_lo = int(np.searchsorted(ts, t_a, side="left"))
    i_hi = int(np.searchsorted(ts, t_deadline, side="right"))
    nq_proxy: list[int] = []
    if i_hi > i_lo:
        sl = slice(i_lo, i_hi)
        m = (sof.t_buy[sl] == hold_buy) & (sof.t_sz[sl] >= nq_thr)
        if m.any():
            nq_proxy = [int(x) for x in ts[sl][m]]
    # merge NQ level hits + NQ proxy as source 1; ES as source 2
    nq_all = sorted(set(nq_hits + nq_proxy))
    t_b = -1
    src = 0.0
    i_n = i_e = 0
    while i_n < len(nq_all) or i_e < len(es_hits):
        tn = nq_all[i_n] if i_n < len(nq_all) else None
        te = es_hits[i_e] if i_e < len(es_hits) else None
        if tn is not None and (te is None or tn <= te):
            ok_path, _ = _in_band_and_undecided(tn)
            ok_i, _, _ = _intact_at(tn)
            if ok_path and ok_i:
                t_b, src = tn, 1.0
                break
            i_n += 1
        else:
            ok_path, _ = _in_band_and_undecided(int(te))  # type: ignore[arg-type]
            ok_i, _, _ = _intact_at(int(te))  # type: ignore[arg-type]
            if ok_path and ok_i:
                t_b, src = int(te), 2.0  # type: ignore[arg-type]
                break
            i_e += 1

    if t_b >= 0:
        pnl_b, _, _ = bracket_pnl(ts, px_pts, t_b, fade_side, cfg.tp_pts, cfg.sl_pts, t_b + hor_ns, slip, comm)
        base["arm_b_fire"] = 1.0
        base["arm_b_source"] = src
        base["t_entry_b_ns"] = float(t_b)
        base["pnl_arm_b"] = float(pnl_b)

    return base


def enrich_events_with_dwell(
    events: pa.Table,
    sof: _SessionOF,
    cfg: DwellConfig,
    *,
    minute_bars,
    es_ts: np.ndarray,
    es_sz: np.ndarray,
    es_buy: np.ndarray,
) -> pa.Table:
    """Append dwell / arm columns for all events in one session."""
    if events.num_rows == 0:
        return events
    touches = [int(x) for x in events.column("t_touch_ns").to_pylist()]
    levels = [float(x) for x in events.column("level_px").to_pylist()]
    apps = [int(x) for x in events.column("approach").to_pylist()]
    labs = [str(x) for x in events.column("label").to_pylist()]

    # batch book snapshots across events
    times: list[int] = []
    min_ns = int(cfg.min_dwell_ms * NS_PER_MS)
    max_ns = int(cfg.max_dwell_ms * NS_PER_MS)
    step_ns = int(cfg.scan_step_ms * NS_PER_MS)
    for t0 in touches:
        times.append(t0)
        t = t0 + min_ns
        while t <= t0 + max_ns:
            times.append(int(t))
            t += step_ns
        # pierce extreme unknown yet — score_event will fetch extras
    book_at = _snapshot_books_at_times(sof, times)

    rows = []
    for t0, L, a, lab in zip(touches, levels, apps, labs):
        u = vol_unit_at(minute_bars, t0, cfg.vol_lookback_min)
        rows.append(
            evaluate_dwell_event(
                sof,
                t_touch=t0,
                level_px=L,
                approach=a,
                label=lab,
                cfg=cfg,
                vol_u=u,
                es_ts=es_ts,
                es_sz=es_sz,
                es_buy=es_buy,
                book_at=book_at,
            )
        )

    out = events
    skip = {"label", "abort_reason", "stratum"}
    # string cols
    out = out.append_column("dwell_stratum", pa.array([r["stratum"] for r in rows], type=pa.string()))
    out = out.append_column("dwell_abort", pa.array([r["abort_reason"] for r in rows], type=pa.string()))
    for k in rows[0]:
        if k in skip or k in ("dwell_stratum", "dwell_abort"):
            continue
        if k == "stratum" or k == "abort_reason":
            continue
        out = out.append_column(k, pa.array([float(r[k]) for r in rows], type=pa.float64()))
    return out


# Survival thresholds for the duration claim (enter if still intact at T)
SURVIVAL_THRESHOLDS = (15, 20, 30, 45)


def duration_bucket_stats(tbl: pa.Table) -> dict[str, dict]:
    """Survival at T seconds: among events still intact+in-band at clock0+T, hold rate + fade P&L."""
    if tbl.num_rows == 0:
        return {}
    lab = np.array(tbl.column("label").to_pylist())
    sess = np.array(tbl.column("session_id").to_pylist())
    ts = tbl.column("t_touch_ns").to_numpy().astype(np.float64)
    out: dict[str, dict] = {}
    for thr in SURVIVAL_THRESHOLDS:
        alive_c = f"alive_{thr}"
        pnl_c = f"pnl_dwell_{thr}"
        if alive_c not in tbl.column_names or pnl_c not in tbl.column_names:
            out[str(thr)] = {"n": 0, "net": 0.0, "avg": None, "hold_rate": None}
            continue
        alive = tbl.column(alive_c).to_numpy().astype(np.float64) >= 0.5
        pnl = tbl.column(pnl_c).to_numpy().astype(np.float64)
        m = alive & np.isfinite(pnl)
        st = pack_pnl(pnl[m], sess[m], ts[m])
        n = int(m.sum())
        hold_rate = float(np.mean(lab[m] == "HOLD")) if n else None
        out[str(thr)] = {**st, "hold_rate": None if hold_rate is None else round(hold_rate, 3)}
    return out


def arm_pack(tbl: pa.Table, fire_col: str, pnl_col: str) -> dict:
    if tbl.num_rows == 0 or pnl_col not in tbl.column_names:
        return pack_pnl(np.array([]), np.array([]), np.array([]))
    fire = tbl.column(fire_col).to_numpy().astype(np.float64) >= 0.5
    pnl = tbl.column(pnl_col).to_numpy().astype(np.float64).copy()
    pnl[~fire] = np.nan
    sess = np.array(tbl.column("session_id").to_pylist())
    ts = tbl.column("t_touch_ns").to_numpy().astype(np.float64)
    st = pack_pnl(pnl, sess, ts)
    fired = fire & np.isfinite(pnl)
    if fired.sum() > 1:
        st["se"] = round(float(np.std(pnl[fired], ddof=1) / np.sqrt(fired.sum())), 3)
    else:
        st["se"] = None
    lab = np.array(tbl.column("label").to_pylist())
    st["hold_share"] = round(float(np.mean(lab[fired] == "HOLD")), 3) if fired.any() else None
    st["amb_share"] = round(float(np.mean(lab[fired] == "AMBIGUOUS")), 3) if fired.any() else None
    return st


def monotone_nondecreasing(vals: list[float | None]) -> bool:
    xs = [v for v in vals if v is not None]
    if len(xs) < 2:
        return False
    return all(xs[i] <= xs[i + 1] + 1e-12 for i in range(len(xs) - 1))
