"""Reaction-confirmation test for EXP-0043 (post-review follow-up).

Pre-touch orderflow did not separate hold from break; the first 2 s of the
reaction did (``TakeScore_at``, ``StackConsume``). That is only useful if a
decision can still be made at ``tc = t_touch + confirm_ms``. This module asks:

* **Undecided at tc** — neither the ≥reject nor the ≥extension threshold has
  been hit by tc (using prints ≤ tc only). Everything below is restricted to
  these events.
* **Confirmation gating** — does a score computed on ``[0, tc]`` predict the
  final HOLD / BREAK label among undecided events, and does trading its sign
  beat always-fade / always-continue from the same entry?
* **Plain reclaim** — the EXP-0042-style entry: fade when price comes back
  through the level after a pierce. Same brackets, so it is directly
  comparable.

Entries fill on the first print strictly after the trigger with ``slip_pts``
against the trade; stops fill at the stop price minus slip, targets at the
target price; everything is time-stopped at ``t_touch + horizon`` on the last
print, with slip. ``commission_pts_rt`` is deducted per round trip.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from research.irt_lvn.events import PTS_PER_TICK


@dataclass
class ConfirmConfig:
    confirm_ms: int = 2_000
    horizon_ms: int = 120_000
    reject_pts: float = 30.0
    extension_pts: float = 30.0
    brackets: tuple[tuple[float, float], ...] = ((20.0, 15.0), (30.0, 15.0), (12.5, 30.0))
    slip_ticks: float = 1.0
    commission_pts_rt: float = 0.25
    gate_scores: tuple[str, ...] = ("TakeScore_at", "StackConsume", "BreakLikeness")
    gate_thresholds: tuple[float, ...] = (0.25, 0.5)
    reclaim_min_pierce_pts: float = 2.0  # a "reclaim" needs a real pierce first
    confirm_sweep_ms: tuple[int, ...] = (1_000, 3_000, 5_000)  # extra confirmation times (fade/cont at tc)
    sweep_bracket: tuple[float, float] = (30.0, 15.0)

    @property
    def slip_pts(self) -> float:
        return float(self.slip_ticks) * PTS_PER_TICK


def path_arrays(trades: pa.Table) -> tuple[np.ndarray, np.ndarray]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
    o = np.argsort(ts, kind="stable")
    return ts[o], px[o]


def _state_at(ts: np.ndarray, px: np.ndarray, t_touch: int, t_c: int, level: float, approach: int,
              reject: float, extension: float) -> tuple[float, float, bool, float, int]:
    """(px_at_tc, pierce_at_tc, undecided, reject_so_far, t_beyond) using prints in [t_touch, t_c]."""
    lo = int(np.searchsorted(ts, t_touch, side="left"))
    hi = int(np.searchsorted(ts, t_c, side="right"))
    if hi <= lo:
        j = max(0, lo - 1)
        return float(px[j]), 0.0, True, 0.0, -1
    seg = px[lo:hi]
    sgn = 1.0 if approach > 0 else -1.0
    beyond = (seg - level) * sgn          # >0 = beyond the level
    pierce = float(max(0.0, beyond.max()))
    rej = float(max(0.0, (-beyond).max()))
    decided = pierce >= extension or rej >= reject
    tb = -1
    k = np.where(beyond > 0)[0]
    if len(k):
        tb = int(ts[lo + int(k[0])])
    return float(seg[-1]), pierce, not decided, rej, tb


def _first_reclaim(ts: np.ndarray, px: np.ndarray, t_touch: int, t_end: int, level: float, approach: int,
                   min_pierce: float) -> int:
    """First print back on the hold side of the level after price has gone ≥ min_pierce beyond it (or -1)."""
    lo = int(np.searchsorted(ts, t_touch, side="left"))
    hi = int(np.searchsorted(ts, t_end, side="right"))
    if hi <= lo:
        return -1
    seg = px[lo:hi]
    sgn = 1.0 if approach > 0 else -1.0
    beyond = (seg - level) * sgn
    k0 = np.where(beyond >= min_pierce)[0]
    if len(k0) == 0:
        return -1
    k = np.where(beyond[int(k0[0]):] <= 0.0)[0]
    return int(ts[lo + int(k0[0]) + int(k[0])]) if len(k) else -1


def bracket_pnl(ts: np.ndarray, px: np.ndarray, t_trigger: int, side: int, tp: float, sl: float,
                t_stop: int, slip: float, comm: float) -> tuple[float, str, int]:
    """Simulate one bracket. side +1 long / -1 short. Returns (pnl_pts, exit_reason, t_exit)."""
    i0 = int(np.searchsorted(ts, t_trigger, side="right"))
    if i0 >= len(ts) or ts[i0] > t_stop:
        return 0.0, "no_fill", -1
    raw = float(px[i0])
    fill = raw + side * slip
    i1 = int(np.searchsorted(ts, t_stop, side="right"))
    seg = px[i0 + 1:i1]
    if len(seg) == 0:
        return -comm, "time", int(ts[i0])
    adverse = (raw - seg) * side   # >0 = against us
    favour = (seg - raw) * side
    hit_sl = np.where(adverse >= sl)[0]
    hit_tp = np.where(favour >= tp)[0]
    k_sl = int(hit_sl[0]) if len(hit_sl) else None
    k_tp = int(hit_tp[0]) if len(hit_tp) else None
    if k_sl is not None and (k_tp is None or k_sl <= k_tp):  # stop first on ties (conservative)
        exit_px = raw - side * sl - side * slip
        return float(side * (exit_px - fill) - comm), "sl", int(ts[i0 + 1 + k_sl])
    if k_tp is not None:
        exit_px = raw + side * tp
        return float(side * (exit_px - fill) - comm), "tp", int(ts[i0 + 1 + k_tp])
    exit_px = float(seg[-1]) - side * slip
    return float(side * (exit_px - fill) - comm), "time", int(ts[i1 - 1])


def confirmation_table(trades: pa.Table, events: pa.Table, cfg: ConfirmConfig) -> pa.Table:
    """Append per-event confirmation state + strategy P&L columns (one session)."""
    if events.num_rows == 0:
        return events
    ts, px = path_arrays(trades)
    conf_ns = int(cfg.confirm_ms * 1_000_000)
    hor_ns = int(cfg.horizon_ms * 1_000_000)
    slip, comm = cfg.slip_pts, cfg.commission_pts_rt

    touches = [int(x) for x in events.column("t_touch_ns").to_pylist()]
    levels = [float(x) for x in events.column("level_px").to_pylist()]
    apps = [int(x) for x in events.column("approach").to_pylist()]

    cols: dict[str, list[float]] = {
        "px_at_tc": [], "pierce_at_tc": [], "reject_at_tc": [], "undecided_at_tc": [],
        "t_reclaim_ns": [], "reclaim_pierce_pts": [],
    }
    for tp, sl in cfg.brackets:
        tag = f"tp{tp:g}_sl{sl:g}"
        for strat in ("fade_tc", "cont_tc", "fade_reclaim"):
            cols[f"pnl_{strat}_{tag}"] = []
            cols[f"exit_{strat}_{tag}"] = []
    s_tp, s_sl = cfg.sweep_bracket
    s_tag = f"tp{s_tp:g}_sl{s_sl:g}"
    for cm in cfg.confirm_sweep_ms:
        k = cm // 1000
        for name in (f"undecided_{k}s", f"pierce_{k}s", f"px_hold_side_{k}s", f"pnl_fade_{k}s_{s_tag}", f"pnl_cont_{k}s_{s_tag}"):
            cols[name] = []

    for t0, L, a in zip(touches, levels, apps):
        t_stop = t0 + hor_ns
        fade_side = -a
        cont_side = a
        # confirmation-time sweep (fixed sweep bracket)
        for cm in cfg.confirm_sweep_ms:
            k = cm // 1000
            tck = t0 + int(cm * 1_000_000)
            p_k, pierce_k, undec_k, _, _ = _state_at(ts, px, t0, tck, L, a, cfg.reject_pts, cfg.extension_pts)
            cols[f"undecided_{k}s"].append(1.0 if undec_k else 0.0)
            cols[f"pierce_{k}s"].append(pierce_k)
            cols[f"px_hold_side_{k}s"].append(1.0 if (p_k - L) * (1.0 if a > 0 else -1.0) <= 0.0 else 0.0)
            for strat, side in (("fade", fade_side), ("cont", cont_side)):
                if not undec_k:
                    cols[f"pnl_{strat}_{k}s_{s_tag}"].append(float("nan"))
                    continue
                pnl, reason, _ = bracket_pnl(ts, px, tck, side, s_tp, s_sl, t_stop, slip, comm)
                cols[f"pnl_{strat}_{k}s_{s_tag}"].append(float("nan") if reason == "no_fill" else pnl)
        tc = t0 + conf_ns
        p_tc, pierce, undec, rej, tb = _state_at(ts, px, t0, tc, L, a, cfg.reject_pts, cfg.extension_pts)
        cols["px_at_tc"].append(p_tc)
        cols["pierce_at_tc"].append(pierce)
        cols["reject_at_tc"].append(rej)
        cols["undecided_at_tc"].append(1.0 if undec else 0.0)
        # plain reclaim: pierce may happen any time in the horizon, not just before tc
        t_rc = _first_reclaim(ts, px, t0, t_stop, L, a, cfg.reclaim_min_pierce_pts)
        cols["t_reclaim_ns"].append(float(t_rc))
        if t_rc > 0:
            _, pr, _, _, _ = _state_at(ts, px, t0, t_rc, L, a, cfg.reject_pts, cfg.extension_pts)
        else:
            pr = 0.0
        cols["reclaim_pierce_pts"].append(pr)
        cols.setdefault("px_hold_side_tc", []).append(1.0 if (p_tc - L) * (1.0 if a > 0 else -1.0) <= 0.0 else 0.0)
        for tp, sl in cfg.brackets:
            tag = f"tp{tp:g}_sl{sl:g}"
            for strat, side, trig in (("fade_tc", fade_side, tc), ("cont_tc", cont_side, tc), ("fade_reclaim", fade_side, t_rc)):
                if trig <= 0 or (strat != "fade_reclaim" and not undec):
                    cols[f"pnl_{strat}_{tag}"].append(float("nan"))
                    cols[f"exit_{strat}_{tag}"].append(float("nan"))
                    continue
                pnl, reason, _ = bracket_pnl(ts, px, trig, side, tp, sl, t_stop, slip, comm)
                if reason == "no_fill":
                    pnl = float("nan")
                cols[f"pnl_{strat}_{tag}"].append(pnl)
                cols[f"exit_{strat}_{tag}"].append({"sl": -1.0, "tp": 1.0, "time": 0.0}.get(reason, float("nan")))

    out = events
    for k, v in cols.items():
        out = out.append_column(k, pa.array([float(x) for x in v], type=pa.float64()))
    return out


# ------------------------------------------------------------- aggregation

def _fin(a) -> np.ndarray:
    x = np.asarray(a, dtype=np.float64)
    return x[np.isfinite(x)]


def pack_pnl(pnl: np.ndarray, sess: np.ndarray, ts: np.ndarray) -> dict[str, Any]:
    m = np.isfinite(pnl)
    p, s, t = pnl[m], sess[m], ts[m]
    if len(p) == 0:
        return {"n": 0, "net": 0.0, "avg": None, "days_pos": 0, "days": 0, "tpd": None, "max_dd": 0.0, "win": None}
    o = np.argsort(t)
    cum = np.cumsum(p[o])
    dd = float(np.max(np.maximum.accumulate(cum) - cum)) if len(cum) else 0.0
    by: dict[str, float] = {}
    for x, sid in zip(p, s):
        by[str(sid)] = by.get(str(sid), 0.0) + float(x)
    return {
        "n": int(len(p)), "net": round(float(p.sum()), 2), "avg": round(float(p.mean()), 3),
        "days_pos": int(sum(1 for v in by.values() if v > 0)), "days": len(by),
        "tpd": round(len(p) / max(1, len(by)), 2), "max_dd": round(dd, 2),
        "win": round(float(np.mean(p > 0)), 3),
    }


def gated_pnl(tbl: pa.Table, score: str, thr: float, tag: str) -> np.ndarray:
    """Per-event P&L for: continue if score ≥ thr, fade if score ≤ −thr, else abstain (nan)."""
    s = tbl.column(score).to_numpy().astype(np.float64)
    fade = tbl.column(f"pnl_fade_tc_{tag}").to_numpy().astype(np.float64)
    cont = tbl.column(f"pnl_cont_tc_{tag}").to_numpy().astype(np.float64)
    out = np.full(len(s), np.nan)
    out[s >= thr] = cont[s >= thr]
    out[s <= -thr] = fade[s <= -thr]
    return out


def fade_when_score_le(tbl: pa.Table, score: str, thr: float, tag: str) -> np.ndarray:
    """Per-event fade P&L when ``score ≤ thr`` and the event was undecided at +confirm_ms; else nan."""
    s = tbl.column(score).to_numpy().astype(np.float64)
    und = tbl.column("undecided_at_tc").to_numpy().astype(np.float64) >= 0.5
    fade = tbl.column(f"pnl_fade_tc_{tag}").to_numpy().astype(np.float64)
    out = np.full(len(s), np.nan)
    m = und & (s <= thr)
    out[m] = fade[m]
    return out


def score_tertile_break_rate(tbl: pa.Table, score: str) -> list[dict[str, Any]]:
    """Among undecided HOLD/BREAK events: P(BREAK) by tertile of score."""
    lab = np.array(tbl.column("label").to_pylist())
    und = tbl.column("undecided_at_tc").to_numpy().astype(np.float64) >= 0.5
    m = und & ((lab == "HOLD") | (lab == "BREAK"))
    if m.sum() < 6:
        return []
    s = tbl.column(score).to_numpy().astype(np.float64)[m]
    isb = (lab[m] == "BREAK").astype(float)
    q1, q2 = np.percentile(s, [33.3, 66.6])
    out = []
    for nm, lo, hi in (("low", -np.inf, q1), ("mid", q1, q2), ("high", q2, np.inf)):
        k = (s >= lo) & (s < hi)
        out.append({"tertile": nm, "n": int(k.sum()), "p_break": round(float(isb[k].mean()), 3) if k.any() else None,
                    "lo": None if not np.isfinite(lo) else round(float(lo), 3)})
    return out
