"""Pre-registered keep-gate masks on annotated MAT events."""
from __future__ import annotations

import numpy as np
import pyarrow as pa


def _col(ev: pa.Table, name: str) -> np.ndarray:
    if name not in ev.column_names:
        return np.full(ev.num_rows, np.nan)
    return ev.column(name).to_numpy().astype(np.float64)


def _icol(ev: pa.Table, name: str) -> np.ndarray:
    if name not in ev.column_names:
        return np.zeros(ev.num_rows, dtype=np.int8)
    arr = ev.column(name).to_numpy().astype(np.float64)
    out = np.zeros(ev.num_rows, dtype=np.int8)
    ok = np.isfinite(arr)
    out[ok] = arr[ok].astype(np.int8)
    return out


def gate_masks(ev: pa.Table) -> dict[str, np.ndarray]:
    """All pre-registered single-arm masks for EXP-0068/69/70."""
    n = ev.num_rows
    if n == 0:
        return {}
    side = _icol(ev, "side").astype(int)
    rot_ratio = _col(ev, "rot_ratio")
    rot_ready = _icol(ev, "rot_ready")
    rot_leg = _icol(ev, "rot_leg_dir").astype(int)
    speed_r = _col(ev, "rot_speed_ratio")
    atr_r = _col(ev, "atr_ratio")
    cd_sig = _col(ev, "cd_dist_sigma")
    cd_trend = _col(ev, "cd_ma_trend")
    tight_l = _icol(ev, "cd_tight_pb_long")
    tight_s = _icol(ev, "cd_tight_pb_short")
    cheap = _icol(ev, "cd_cheap")
    sma_ready = _icol(ev, "cd_sma_ready")
    vwda = _col(ev, "vwap_dist_aligned")
    vwdn = _col(ev, "vwap_dist_norm")
    vix = _col(ev, "vix_prior")
    vix_hi = _col(ev, "vix_hi_tercile")

    with_leg = (side == rot_leg) & (rot_leg != 0)
    rot_fresh = (rot_ready == 0) | ((rot_ready == 1) & with_leg & np.isfinite(rot_ratio) & (rot_ratio < 0.90))
    speed_ok = (~np.isfinite(speed_r)) | (speed_r >= 0.5)
    atr_ok = (~np.isfinite(atr_r)) | (atr_r < 1.0)

    arm_a = (
        (sma_ready == 1)
        & (
            ((side > 0) & (tight_l == 1))
            | ((side < 0) & (tight_s == 1))
        )
    )
    arm_b = (
        (sma_ready == 1)
        & (cheap == 1)
        & (
            ((side > 0) & (cd_trend > 0))
            | ((side < 0) & (cd_trend < 0))
        )
    )

    same_side_vwap = np.isfinite(vwda) & (vwda > 0)
    vwap_band = same_side_vwap & np.isfinite(vwdn) & (vwdn >= 0.5) & (vwdn <= 2.0)
    vwap_not_ext = same_side_vwap & np.isfinite(vwdn) & (vwdn <= 2.0)
    vwap_near = same_side_vwap & np.isfinite(vwdn) & (vwdn <= 0.5)
    # Missing ATR → skip (do not silently keep)
    atr_expanded = np.isfinite(atr_r) & (atr_r >= 1.0)

    # Missing VIX → skip (do not silently keep)
    vix_ok = np.isfinite(vix)
    return {
        "base": np.ones(n, dtype=bool),
        "rot_fresh_0.90": rot_fresh.astype(bool),
        "speed_not_stalled": speed_ok.astype(bool),
        "atr_room": atr_ok.astype(bool),
        "rot_and_atr": (rot_fresh & atr_ok).astype(bool),
        "cd_tight_A": arm_a.astype(bool),
        "cd_cheap_B": arm_b.astype(bool),
        "vwap_band_0.5_2.0": vwap_band.astype(bool),
        "vwap_not_extended": vwap_not_ext.astype(bool),
        # H66 / EXP-0075 — near-VWAP + ATR-expanded (opposite of failed room/band)
        "vwap_near_0.5": vwap_near.astype(bool),
        "atr_expanded": atr_expanded.astype(bool),
        "rot_fresh_and_vwap_near": (rot_fresh & vwap_near).astype(bool),
        # H65 / EXP-0074 — prior-close VIX keep-gates
        "vix_ge_15": (vix_ok & (vix >= 15.0)).astype(bool),
        "vix_ge_18": (vix_ok & (vix >= 18.0)).astype(bool),
        "vix_ge_20": (vix_ok & (vix >= 20.0)).astype(bool),
        "vix_ge_25": (vix_ok & (vix >= 25.0)).astype(bool),
        "vix_hi_tercile": (vix_ok & (vix_hi >= 0.5)).astype(bool),
        # descriptive helpers for 0067
        "cd_sig": cd_sig,
        "rot_ratio": rot_ratio,
        "rot_speed_ratio": speed_r,
        "atr_ratio": atr_r,
        "vwap_dist_norm": vwdn,
        "vwap_dist_aligned": vwda,
        "cd_legacy_pb": (
            ((side > 0) & (_icol(ev, "cd_legacy_pb_long") == 1))
            | ((side < 0) & (_icol(ev, "cd_legacy_pb_short") == 1))
        ),
    }


def label_cd_sigma(x: float) -> str:
    if not np.isfinite(x):
        return "cd_na"
    if x <= -0.5:
        return "cd_le_-0.5"
    if x <= 0:
        return "cd_(-0.5,0]"
    if x <= 0.2:
        return "cd_(0,+0.2]"
    if x <= 1.4:
        return "cd_(+0.2,+1.4]"
    return "cd_gt_+1.4"


def label_rot_ratio(x: float, ready: int) -> str:
    if not ready:
        return "rot_not_ready"
    if not np.isfinite(x):
        return "rot_na"
    if x < 0.5:
        return "rot_0_0.5"
    if x < 0.9:
        return "rot_0.5_0.9"
    if x <= 1.1:
        return "rot_0.9_1.1"
    return "rot_gt_1.1"


def label_speed(x: float) -> str:
    if not np.isfinite(x):
        return "speed_na"
    if x < 0.5:
        return "speed_lt_0.5"
    if x <= 1.0:
        return "speed_0.5_1.0"
    return "speed_gt_1.0"


def label_atr(x: float) -> str:
    if not np.isfinite(x):
        return "atr_na"
    if x < 0.7:
        return "atr_lt_0.7"
    if x <= 1.0:
        return "atr_0.7_1.0"
    return "atr_gt_1.0"


def label_vwap_norm(x: float, aligned: float) -> str:
    if not np.isfinite(x):
        return "vwap_na"
    sign = "pos" if (np.isfinite(aligned) and aligned > 0) else "neg"
    if x < 0.5:
        return f"vwap_{sign}_lt_0.5"
    if x <= 2.0:
        return f"vwap_{sign}_0.5_2.0"
    return f"vwap_{sign}_gt_2.0"
