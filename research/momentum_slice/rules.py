"""Frozen RULE_SPEC_V1 classifiers (no post-hoc threshold fitting).

See results/MOMENTUM_SLICE/RULE_SPEC_V1.md.
"""
from __future__ import annotations

from research.momentum_slice.detector import ZONE_TICKS

# --- Frozen thresholds (RULE_SPEC_V1) ---
TREND_MIN_PTS = 0.0
DOM_VS_MED_MIN = 4.0
LEFT_BEHIND = 1.0

PC_PULLBACK_PTS_MIN = 4.0
PC_PULLBACK_RATIO_MIN = 0.70
PC_SLOW_RATIO_MAX = 0.85
PC_NET_PTS_MAX = 1.0

STOP_BUFFER_PTS = 2.0


def f(r: dict, k: str):
    v = r.get(k)
    if v in ("", "nan", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def enrich(r: dict) -> dict:
    """Add derived fields used by frozen rules (idempotent)."""
    stack, mx, dens = f(r, "nq_stack"), f(r, "nq_maxlvl"), f(r, "nq_density")
    if stack and mx is not None and stack > 0:
        r["nq_dom_share"] = mx / stack
    else:
        r["nq_dom_share"] = None
    med = (stack / (dens * ZONE_TICKS)) if dens and dens > 0 and stack else None
    r["nq_dom_vs_med"] = (mx / med) if med and mx is not None and med > 0 else None
    n_tr = f(r, "nq_burst_n_trades")
    r["nq_burst_tps"] = (n_tr / 2.0) if n_tr is not None else None
    if r.get("trend_5m_pts") in (None, "", "nan") and r.get("nq_trend_5m_pts") not in (None, "", "nan"):
        r["trend_5m_pts"] = r["nq_trend_5m_pts"]
    for stamp in ("confirm", "c5s"):
        peak = f(r, f"nq_peak_past_{stamp}_pts")
        pb = f(r, f"nq_pullback_{stamp}_pts")
        key = f"nq_pullback_ratio_{stamp}"
        if r.get(key) in (None, "", "nan") and peak and peak > 0 and pb is not None:
            r[key] = pb / peak
    return r


def is_momentum_v1(r: dict) -> bool:
    """MOMENTUM_V1 hard gates: left_behind + with-trend + dominant prominence."""
    enrich(r)
    return (
        f(r, "nq_left_behind_confirm") == LEFT_BEHIND
        and (f(r, "trend_5m_pts") or -1e9) >= TREND_MIN_PTS
        and (r.get("nq_dom_vs_med") or 0) >= DOM_VS_MED_MIN
    )


def is_pc_v1(r: dict) -> bool:
    """PC_V1 stall gates at confirm+5s feature stamp."""
    enrich(r)
    return (
        f(r, "nq_left_behind_c5s") == 0
        and (f(r, "nq_pullback_c5s_pts") or 0) >= PC_PULLBACK_PTS_MIN
        and (f(r, "nq_pullback_ratio_c5s") or 0) >= PC_PULLBACK_RATIO_MIN
        and (f(r, "nq_post5_slow_ratio") or 9) <= PC_SLOW_RATIO_MAX
        and (f(r, "nq_post5_net_pts") or 9) <= PC_NET_PTS_MAX
    )


def momentum_stop_pts(r: dict) -> float | None:
    """Distance from entry to stop just beyond sliced dominant level + buffer."""
    enrich(r)
    entry = f(r, "entry_px") or f(r, "nq_px_ignition")
    hold = f(r, "nq_hold_px")
    side = f(r, "side_int")
    if entry is None or hold is None or side is None:
        # Fallback: use peak-past as proxy distance to level
        peak = f(r, "nq_peak_past_confirm_pts")
        if peak is None:
            return None
        return abs(peak) + STOP_BUFFER_PTS
    # Long: stop below level; short: stop above. Dist = side*(entry - hold) when past.
    dist = side * (entry - hold)
    if dist < 0:
        dist = abs(entry - hold)
    return float(dist + STOP_BUFFER_PTS)


def quality_readouts(r: dict) -> dict:
    enrich(r)
    return {
        "trend_5m_pts": f(r, "trend_5m_pts"),
        "nq_dom_vs_med": r.get("nq_dom_vs_med"),
        "nq_burst_tps": r.get("nq_burst_tps"),
        "nq_pullback_ratio_confirm": f(r, "nq_pullback_ratio_confirm"),
        "nq_pullback_ratio_c5s": f(r, "nq_pullback_ratio_c5s"),
        "es_dist_max_ticks": f(r, "es_dist_max_ticks"),
        "es_consume": f(r, "es_consume"),
        "nq_hold_state": r.get("nq_hold_state"),
        "nq_post5_slow_ratio": f(r, "nq_post5_slow_ratio"),
        "nq_post5_net_pts": f(r, "nq_post5_net_pts"),
    }
