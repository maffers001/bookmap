"""EXP-0045 / H36 — open-book standout resting clusters as levels.

Causal pipeline per session:
  1. Snapshot NQ book at 14:35 UK; cluster within ``cluster_ticks``; standout
     by size percentile + age-weighted MBO size; top-N per side.
  2. Lifecycle after snap: alive / pulled / consumed from depth + trades.
  3. First touch from the far side → fade arms T0 / T2.
  4. Break (vol pierce + size < 50%) → retest continue arms R0 / R2.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from research.irt_lvn.confirm import _state_at, bracket_pnl, pack_pnl
from research.irt_lvn.events import PTS_PER_TICK
from research.irt_lvn.of_scores import (
    NS_PER_MS,
    _SessionOF,
    _bbo,
    _mbo_band_flow,
    _snapshot_books_at_times,
    enrich_events_with_scores,
)


@dataclass
class OpenBookConfig:
    book_radius_pts: float = 100.0
    cluster_ticks: int = 2
    standout_pct: float = 99.0
    large_mult: float = 2.0
    top_n_per_side: int = 5
    near_mid_exclude_pts: float = 10.0
    large_size_pct: float = 99.9
    large_size_min: int = 15
    age_scale_s: float = 900.0
    age_w_min: float = 0.25
    age_w_max: float = 1.0
    old_age_s: float = 900.0
    age_cap_s: float = 7200.0
    frac_old_thr: float = 0.5
    alive_frac: float = 0.5
    pull_size_frac: float = 0.25
    pull_trade_frac: float = 0.25
    pad_pts: float = 2.0
    break_floor_pts: float = 8.0
    retest_horizon_ms: int = 1_200_000
    reject_pts: float = 30.0
    extension_pts: float = 30.0
    confirm_ms: int = 2_000
    stack_consume_thr: float = -0.25
    take_at_thr: float = 0.25
    horizon_ms: int = 180_000
    slip_ticks: float = 1.0
    commission_pts_rt: float = 0.25
    sl_floor: float = 6.0
    sl_cap: float = 15.0
    primary_rr: float = 2.0
    placebo_shift_pts: float = 40.0
    # fixed context brackets [tp, sl] — override per market in yaml
    fixed_brackets: tuple[tuple[float, float], ...] = (
        (30.0, 15.0), (20.0, 10.0), (40.0, 10.0), (10.0, 20.0),
    )

    @property
    def slip_pts(self) -> float:
        return float(self.slip_ticks) * PTS_PER_TICK


def _clip(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, x)))


def vol_unit_preopen(ts: np.ndarray, px: np.ndarray, t0: int, t1: int) -> float:
    """Median 1-min range of NQ prints in [t0, t1)."""
    m = (ts >= t0) & (ts < t1)
    if not m.any():
        return 0.25
    t, p = ts[m], px[m]
    bar_ns = 60_000_000_000
    bids = (t // bar_ns) * bar_ns
    ranges: list[float] = []
    for b in np.unique(bids):
        seg = p[bids == b]
        if len(seg):
            ranges.append(float(seg.max() - seg.min()))
    return float(max(np.median(ranges) if ranges else 0.25, 0.25))


def primary_bracket(u: float, cfg: OpenBookConfig) -> tuple[float, float]:
    sl = _clip(u, cfg.sl_floor, cfg.sl_cap)
    return cfg.primary_rr * sl, sl


# ---------------------------------------------------------------- clustering

def _cluster_side(book: dict[int, int], *, lo: int, hi: int, cluster_ticks: int) -> list[dict[str, Any]]:
    """Merge contiguous non-empty prices within ``cluster_ticks`` into clusters."""
    prices = sorted(p for p, s in book.items() if s > 0 and lo <= p <= hi)
    if not prices:
        return []
    out: list[dict[str, Any]] = []
    cur_px = [prices[0]]
    cur_sz = [int(book[prices[0]])]
    for p in prices[1:]:
        if p - cur_px[-1] <= cluster_ticks:
            cur_px.append(p)
            cur_sz.append(int(book[p]))
        else:
            out.append(_pack_cluster(cur_px, cur_sz))
            cur_px, cur_sz = [p], [int(book[p])]
    out.append(_pack_cluster(cur_px, cur_sz))
    return out


def _pack_cluster(px: list[int], sz: list[int]) -> dict[str, Any]:
    tot = float(sum(sz))
    # size-weighted centre
    centre = int(round(sum(p * s for p, s in zip(px, sz)) / tot)) if tot > 0 else px[0]
    return {
        "px_lo": int(min(px)), "px_hi": int(max(px)), "centre_tick": centre,
        "size": tot, "n_prices": len(px), "prices": list(px), "sizes": list(sz),
    }


def snapshot_clusters(
    bids: dict[int, int], asks: dict[int, int], *, mid_tick: int, cfg: OpenBookConfig,
    min_price_size: float = 0.0,
) -> list[dict[str, Any]]:
    """Cluster resting size in ±book_radius of mid.

    If ``min_price_size`` > 0, only prices at/above that size are kept, then
    merged within ``cluster_ticks``. That matches Bookmap "bright" bands: a
    dense ES book must not collapse into one mega-cluster per side.
    """
    rad = int(round(cfg.book_radius_pts / PTS_PER_TICK))
    lo, hi = mid_tick - rad, mid_tick + rad
    rows: list[dict[str, Any]] = []
    for side, book in (("BID", bids), ("ASK", asks)):
        if min_price_size > 0:
            book = {p: s for p, s in book.items() if s >= min_price_size and lo <= p <= hi}
            if not book:
                continue
            # cluster only the bright prices (may have gaps)
            for c in _cluster_side(book, lo=lo, hi=hi, cluster_ticks=cfg.cluster_ticks):
                c["side"] = side
                c["dist_mid_pts"] = abs(c["centre_tick"] - mid_tick) * PTS_PER_TICK
                rows.append(c)
        else:
            for c in _cluster_side(book, lo=lo, hi=hi, cluster_ticks=cfg.cluster_ticks):
                c["side"] = side
                c["dist_mid_pts"] = abs(c["centre_tick"] - mid_tick) * PTS_PER_TICK
                rows.append(c)
    return rows


# ---------------------------------------------------------------- MBO age

def mbo_resting_at(
    m_ts: np.ndarray, m_op: np.ndarray, m_bid: np.ndarray, m_px: np.ndarray, m_sz: np.ndarray,
    snap_ns: int, *, data_start_ns: int, age_cap_s: float,
) -> dict[tuple[bool, int], list[tuple[float, int]]]:
    """Replay resolved MBO to ``snap_ns``; return (is_bid, px) → [(size, first_seen_ns)]."""
    if len(m_ts) == 0:
        return {}
    hi = int(np.searchsorted(m_ts, snap_ns, side="right"))
    # Walk with an order map keyed by synthetic stream index via sequential state.
    # Resolved stream is already (+1 add / −1 cancel) events; we rebuild resting.
    # Use a dict of active lots per (side, px): list of (remaining, first_ns) FIFO.
    lots: dict[tuple[bool, int], list[list[float]]] = {}  # [remaining, first_ns]
    for i in range(hi):
        key = (bool(m_bid[i]), int(m_px[i]))
        op, sz, t = int(m_op[i]), float(m_sz[i]), int(m_ts[i])
        if op > 0:
            first = t if t > data_start_ns else int(snap_ns - age_cap_s * 1e9)
            lots.setdefault(key, []).append([sz, float(first)])
        elif op < 0:
            need = sz
            buf = lots.get(key, [])
            while need > 1e-9 and buf:
                take = min(buf[0][0], need)
                buf[0][0] -= take
                need -= take
                if buf[0][0] <= 1e-9:
                    buf.pop(0)
            if not buf and key in lots:
                del lots[key]
    out: dict[tuple[bool, int], list[tuple[float, int]]] = {}
    for key, buf in lots.items():
        out[key] = [(float(r), int(f)) for r, f in buf if r > 1e-9]
    return out


def age_weight_cluster(
    cluster: dict[str, Any], resting: dict[tuple[bool, int], list[tuple[float, int]]],
    *, snap_ns: int, is_bid: bool, cfg: OpenBookConfig,
) -> dict[str, float]:
    w_sum = 0.0
    raw = 0.0
    old = 0.0
    n_ord = 0
    for p in cluster["prices"]:
        for sz, first in resting.get((is_bid, int(p)), []):
            age_s = min(cfg.age_cap_s, max(0.0, (snap_ns - first) / 1e9))
            w = _clip(age_s / cfg.age_scale_s, cfg.age_w_min, cfg.age_w_max)
            w_sum += sz * w
            raw += sz
            if age_s >= cfg.old_age_s:
                old += sz
            n_ord += 1
    # Depth size not explained by MBO = resting already present at data start → age_cap
    depth_sz = float(cluster["size"])
    missing = max(0.0, depth_sz - raw)
    if missing > 0:
        w_cap = _clip(cfg.age_cap_s / cfg.age_scale_s, cfg.age_w_min, cfg.age_w_max)
        w_sum += missing * w_cap
        raw += missing
        if cfg.age_cap_s >= cfg.old_age_s:
            old += missing
    if raw <= 0:
        raw = depth_sz
        w_sum = raw * cfg.age_w_min
    frac_old = old / raw if raw > 0 else 0.0
    return {
        "age_weighted_size": float(w_sum),
        "mbo_size": float(raw - missing) if missing < raw else 0.0,
        "frac_old": float(frac_old),
        "n_orders": float(n_ord),
        "age_stratum": 1.0 if frac_old >= cfg.frac_old_thr else 0.0,  # 1=old
    }


def select_standouts(
    clusters: list[dict[str, Any]], resting: dict[tuple[bool, int], list[tuple[float, int]]],
    *, snap_ns: int, large_thr: int, cfg: OpenBookConfig,
) -> list[dict[str, Any]]:
    if not clusters:
        return []
    sizes = np.asarray([c["size"] for c in clusters], dtype=np.float64)
    thr_pct = float(np.percentile(sizes, cfg.standout_pct)) if len(sizes) else 0.0
    thr = max(thr_pct, cfg.large_mult * float(large_thr))
    cands: list[dict[str, Any]] = []
    for c in clusters:
        if c["size"] < thr:
            continue
        if c["dist_mid_pts"] < cfg.near_mid_exclude_pts:
            continue
        aw = age_weight_cluster(c, resting, snap_ns=snap_ns, is_bid=(c["side"] == "BID"), cfg=cfg)
        row = {**c, **aw, "standout_thr": thr, "large_thr": float(large_thr)}
        cands.append(row)
    # top N per side by age_weighted_size
    out: list[dict[str, Any]] = []
    for side in ("BID", "ASK"):
        side_rows = [r for r in cands if r["side"] == side]
        side_rows.sort(key=lambda r: r["age_weighted_size"], reverse=True)
        out.extend(side_rows[: cfg.top_n_per_side])
    return out


# ---------------------------------------------------------------- lifecycle + events

def _cluster_size_at(book: dict[int, int], px_lo: int, px_hi: int) -> float:
    return float(sum(s for p, s in book.items() if px_lo <= p <= px_hi and s > 0))


def _trade_into(
    t_ts: np.ndarray, t_px: np.ndarray, t_sz: np.ndarray, t_buy: np.ndarray,
    t0: int, t1: int, *, px_lo: int, px_hi: int, want_buy_aggr: bool,
) -> float:
    if t1 <= t0 or len(t_ts) == 0:
        return 0.0
    i0 = int(np.searchsorted(t_ts, t0, side="left"))
    i1 = int(np.searchsorted(t_ts, t1, side="right"))
    if i1 <= i0:
        return 0.0
    sl = slice(i0, i1)
    m = (t_buy[sl] == want_buy_aggr) & (t_px[sl] >= px_lo) & (t_px[sl] <= px_hi)
    return float(t_sz[sl][m].sum()) if m.any() else 0.0


def detect_level_events(
    levels: list[dict[str, Any]],
    sof: _SessionOF,
    ts: np.ndarray,
    px: np.ndarray,
    *,
    snap_ns: int,
    win_hi: int,
    u: float,
    cfg: OpenBookConfig,
    placebo: bool = False,
) -> pa.Table:
    """For each standout level: lifecycle, first touch, optional break+retest."""
    if not levels or len(ts) == 0:
        return pa.table({})

    break_pts = max(cfg.break_floor_pts, u)
    conf_ns = int(cfg.confirm_ms * NS_PER_MS)
    ret_ns = int(cfg.retest_horizon_ms * NS_PER_MS)
    hor_ns = int(cfg.horizon_ms * NS_PER_MS)
    slip, comm = cfg.slip_pts, cfg.commission_pts_rt
    tp0, sl0 = primary_bracket(u, cfg)
    brackets: list[tuple[str, float, float]] = [("vol2", tp0, sl0)]
    tp3, sl3 = 3.0 * sl0, sl0
    brackets.append(("vol3", tp3, sl3))
    for tp, sl in cfg.fixed_brackets:
        brackets.append((f"tp{tp:g}_sl{sl:g}", tp, sl))

    # Precompute book path after snap (one pass)
    book_times = [snap_ns] + [int(t) for t in sof.d_ts if t > snap_ns]
    # subsample depth times every ~250ms worth by taking every k-th if huge
    if len(book_times) > 200_000:
        step = max(1, len(book_times) // 100_000)
        book_times = book_times[::step]
        if book_times[0] != snap_ns:
            book_times = [snap_ns] + book_times
    books = _snapshot_books_at_times(sof, book_times)
    b_ts = np.asarray(sorted(books.keys()), dtype=np.int64)

    rows: list[dict[str, Any]] = []
    for lv in levels:
        side = lv["side"]
        is_bid = side == "BID"
        # ASK = resistance (approach from below +1); BID = support (from above -1)
        approach = 1 if not is_bid else -1
        L = float(lv["centre_tick"]) * PTS_PER_TICK
        px_lo, px_hi = int(lv["px_lo"]), int(lv["px_hi"])
        snap_sz = float(lv["size"])
        want_buy = not is_bid  # buy aggression eats asks

        # lifecycle scan
        status = "alive"
        t_pull = -1
        t_consume = -1
        traded_cum = 0.0
        if not placebo:
            for t_b in b_ts:
                if t_b < snap_ns:
                    continue
                if t_b > win_hi:
                    break
                bids_b, asks_b = books[int(t_b)]
                book = bids_b if is_bid else asks_b
                cur = _cluster_size_at(book, px_lo, px_hi)
                traded_cum = _trade_into(
                    sof.t_ts, sof.t_px, sof.t_sz, sof.t_buy, snap_ns, int(t_b),
                    px_lo=px_lo, px_hi=px_hi, want_buy_aggr=want_buy,
                )
                if cur < cfg.pull_size_frac * snap_sz and traded_cum < cfg.pull_trade_frac * snap_sz:
                    status = "pulled"
                    t_pull = int(t_b)
                    break
                if traded_cum >= cfg.alive_frac * snap_sz and cur < cfg.alive_frac * snap_sz:
                    status = "consumed"
                    t_consume = int(t_b)
                    # still allow touch before consume; mark but continue for touch scan
                    # break only stops trading *after* pull; consumed levels can still have been touched
                    # For event detection we treat pulled as no-trade; consumed may have been broken via price.
                    break
            alive_until = t_pull if status == "pulled" else win_hi
        else:
            alive_until = win_hi
            status = "alive"

        # first touch while alive: print enters pad from far side
        i0 = int(np.searchsorted(ts, snap_ns, side="right"))
        i1 = int(np.searchsorted(ts, min(alive_until, win_hi), side="right"))
        t_touch = -1
        if i1 > i0:
            seg_t, seg_p = ts[i0:i1], px[i0:i1]
            # far side: for ASK resistance, price approaches from below (px < L - pad), then enters [L-pad, L+pad]
            pad = cfg.pad_pts
            outside = (seg_p < L - pad) if approach > 0 else (seg_p > L + pad)
            inside = np.abs(seg_p - L) <= pad
            # first index inside after having been outside earlier
            seen_out = False
            for k in range(len(seg_p)):
                if outside[k]:
                    seen_out = True
                elif seen_out and inside[k]:
                    t_touch = int(seg_t[k])
                    break

        base = {
            "level_px": L, "side": side, "approach": float(approach),
            "snap_size": snap_sz, "age_weighted_size": float(lv.get("age_weighted_size", snap_sz)),
            "frac_old": float(lv.get("frac_old", 0.0)),
            "age_stratum": float(lv.get("age_stratum", 0.0)),
            "dist_mid_pts": float(lv.get("dist_mid_pts", 0.0)),
            "vol_unit": float(u), "break_pts": float(break_pts),
            "lifecycle": status, "t_pull_ns": float(t_pull), "t_consume_ns": float(t_consume),
            "t_touch_ns": float(t_touch), "has_touch": 1.0 if t_touch > 0 else 0.0,
            "placebo": 1.0 if placebo else 0.0,
            "px_lo": float(px_lo), "px_hi": float(px_hi),
        }
        if t_touch <= 0:
            for name, _, _ in brackets:
                for arm in ("T0", "T2", "R0", "R2"):
                    base[f"pnl_{arm}_{name}"] = float("nan")
            base.update({
                "t_break_ns": -1.0, "t_retest_ns": -1.0, "broke": 0.0,
                "hold_60": float("nan"), "hold_180": float("nan"),
                "mfe_180": float("nan"), "mae_180": float("nan"),
                "level_consume_frac_2s": float("nan"), "level_pull_frac_pre5": float("nan"),
                "undecided_t2": float("nan"), "StackConsume": float("nan"), "TakeScore_at": float("nan"),
            })
            rows.append(base)
            continue

        # path outcomes from touch
        fade_side = -approach
        t_stop = t_touch + hor_ns
        j0 = int(np.searchsorted(ts, t_touch, side="left"))
        j1 = int(np.searchsorted(ts, t_stop, side="right"))
        path = px[j0:j1] if j1 > j0 else px[j0:j0 + 1]
        sgn = float(approach)
        beyond = (path - L) * sgn
        favour_fade = -beyond  # away from break = hold direction for fade
        mfe = float(favour_fade.max()) if len(favour_fade) else 0.0
        mae = float(beyond.max()) if len(beyond) else 0.0
        # hold = never pierces break_pts within horizon; also check 60s
        j60 = int(np.searchsorted(ts, t_touch + 60_000_000_000, side="right"))
        path60 = px[j0:j60] if j60 > j0 else path
        bey60 = (path60 - L) * sgn
        hold_60 = 1.0 if (len(bey60) == 0 or float(bey60.max()) < break_pts) else 0.0
        hold_180 = 1.0 if mae < break_pts else 0.0

        # break: pierce ≥ break_pts AND cluster size < 50% at some time after that pierce
        broke = 0.0
        t_break = -1
        t_retest = -1
        k_br = np.where(beyond >= break_pts)[0]
        if len(k_br):
            t_cand = int(ts[j0 + int(k_br[0])])
            # check size at/after t_cand
            bi = int(np.searchsorted(b_ts, t_cand, side="left"))
            size_ok = placebo  # placebo: price break alone
            if not placebo and bi < len(b_ts):
                for bi2 in range(bi, min(bi + 50, len(b_ts))):
                    bb, aa = books[int(b_ts[bi2])]
                    book = bb if is_bid else aa
                    if _cluster_size_at(book, px_lo, px_hi) < cfg.alive_frac * snap_sz:
                        size_ok = True
                        break
            elif not placebo and status == "consumed":
                size_ok = True
            if size_ok:
                broke = 1.0
                t_break = t_cand
                # retest: back within pad from break side within retest horizon
                j_end = int(np.searchsorted(ts, t_break + ret_ns, side="right"))
                seg = px[j0 + int(k_br[0]): j_end]
                seg_t = ts[j0 + int(k_br[0]): j_end]
                # on break side means beyond > 0; retest = returns to |px-L|<=pad after being beyond
                for k in range(len(seg)):
                    if abs(float(seg[k]) - L) <= pad and k > 0:
                        # ensure we were beyond earlier in this segment
                        if float(((seg[:k] - L) * sgn).max()) >= break_pts * 0.5:
                            t_retest = int(seg_t[k])
                            break

        # level-specific OF raws around touch
        cons_2s = _trade_into(
            sof.t_ts, sof.t_px, sof.t_sz, sof.t_buy, t_touch, t_touch + conf_ns,
            px_lo=px_lo, px_hi=px_hi, want_buy_aggr=want_buy,
        )
        level_consume_frac_2s = cons_2s / snap_sz if snap_sz > 0 else 0.0
        a_pre, c_pre, _, _ = _mbo_band_flow(
            sof, t_touch - 5_000_000_000, t_touch + conf_ns,
            want_bid=is_bid, px_lo=px_lo, px_hi=px_hi,
        )
        tot_m = a_pre + c_pre
        level_pull_frac_pre5 = (c_pre / tot_m) if tot_m > 0 else 0.0

        # undecided at +2s
        _, _, undec, _, _ = _state_at(
            ts, px, t_touch, t_touch + conf_ns, L, approach, cfg.reject_pts, cfg.extension_pts,
        )

        row = {
            **base,
            "t_touch_ns": float(t_touch),
            "t_break_ns": float(t_break),
            "t_retest_ns": float(t_retest),
            "broke": broke,
            "hold_60": hold_60,
            "hold_180": hold_180,
            "mfe_180": mfe,
            "mae_180": mae,
            "level_consume_frac_2s": float(level_consume_frac_2s),
            "level_pull_frac_pre5": float(level_pull_frac_pre5),
            "undecided_t2": 1.0 if undec else 0.0,
            "StackConsume": float("nan"),
            "TakeScore_at": float("nan"),
        }

        # P&L arms (scores filled later for gates; ungated computed now)
        for name, tp, sl in brackets:
            pnl, reason, _ex = bracket_pnl(ts, px, t_touch, fade_side, tp, sl, t_stop, slip, comm)
            row[f"pnl_T0_{name}"] = float("nan") if reason == "no_fill" else pnl
            row[f"pnl_T2_{name}"] = float("nan")
            if t_retest > 0:
                t_r_stop = t_retest + hor_ns
                pnl_r, reason_r, _ex = bracket_pnl(ts, px, t_retest, approach, tp, sl, t_r_stop, slip, comm)
                row[f"pnl_R0_{name}"] = float("nan") if reason_r == "no_fill" else pnl_r
                row[f"pnl_R2_{name}"] = float("nan")
            else:
                row[f"pnl_R0_{name}"] = float("nan")
                row[f"pnl_R2_{name}"] = float("nan")
        rows.append(row)

    if not rows:
        return pa.table({})
    cols: dict[str, Any] = {}
    keys = list(rows[0].keys())
    for k in keys:
        vals = [r[k] for r in rows]
        if isinstance(vals[0], str):
            cols[k] = pa.array(vals, type=pa.string())
        else:
            cols[k] = np.asarray(vals, dtype=np.float64)
    return pa.table(cols)


def apply_of_gates(
    events: pa.Table, sof: _SessionOF, ts: np.ndarray, px: np.ndarray, cfg: OpenBookConfig,
) -> pa.Table:
    """Score touches with of_scores; fill T2/R2 gated P&L."""
    if events.num_rows == 0:
        return events
    has = events.column("has_touch").to_numpy().astype(np.float64) > 0.5
    touch_idx = np.where(has)[0]
    if len(touch_idx) == 0:
        return events
    mini = pa.table({
        "t_touch_ns": events.column("t_touch_ns").take(touch_idx),
        "level_px": events.column("level_px").take(touch_idx),
        "approach": events.column("approach").take(touch_idx),
    })
    scored = enrich_events_with_scores(mini, sof)
    sc_map = {
        int(touch_idx[j]): (
            float(scored.column("StackConsume")[j].as_py()),
            float(scored.column("TakeScore_at")[j].as_py()),
        )
        for j in range(scored.num_rows)
    }

    slip, comm = cfg.slip_pts, cfg.commission_pts_rt
    conf_ns = int(cfg.confirm_ms * NS_PER_MS)
    hor_ns = int(cfg.horizon_ms * NS_PER_MS)
    n = events.num_rows
    stack = np.full(n, np.nan)
    take = np.full(n, np.nan)
    for i, (s, t) in sc_map.items():
        stack[i] = s
        take[i] = t

    bracket_names = [c.replace("pnl_T0_", "") for c in events.column_names if c.startswith("pnl_T0_")]
    u = events.column("vol_unit").to_numpy().astype(np.float64)
    approaches = events.column("approach").to_numpy().astype(np.float64)
    touches = events.column("t_touch_ns").to_numpy().astype(np.float64)
    retests = events.column("t_retest_ns").to_numpy().astype(np.float64)
    undec = events.column("undecided_t2").to_numpy().astype(np.float64)

    new_cols: dict[str, np.ndarray] = {"StackConsume": stack, "TakeScore_at": take}
    for bname in bracket_names:
        t2 = np.full(n, np.nan)
        r2 = np.full(n, np.nan)
        for i in range(n):
            if not np.isfinite(touches[i]) or touches[i] <= 0:
                continue
            tp, sl = _bracket_from_name(bname, float(u[i]), cfg)
            a = int(approaches[i])
            if undec[i] >= 0.5 and np.isfinite(stack[i]) and stack[i] <= cfg.stack_consume_thr:
                pnl, reason, _ex = bracket_pnl(
                    ts, px, int(touches[i]) + conf_ns, -a, tp, sl, int(touches[i]) + hor_ns, slip, comm,
                )
                if reason != "no_fill":
                    t2[i] = pnl
            if retests[i] > 0 and np.isfinite(take[i]) and take[i] >= cfg.take_at_thr:
                pnl, reason, _ex = bracket_pnl(
                    ts, px, int(retests[i]) + conf_ns, a, tp, sl, int(retests[i]) + hor_ns, slip, comm,
                )
                if reason != "no_fill":
                    r2[i] = pnl
        new_cols[f"pnl_T2_{bname}"] = t2
        new_cols[f"pnl_R2_{bname}"] = r2

    out = events
    for k, arr in new_cols.items():
        if k in out.column_names:
            idx = out.column_names.index(k)
            out = out.set_column(idx, k, pa.array(arr, type=pa.float64()))
        else:
            out = out.append_column(k, pa.array(arr, type=pa.float64()))
    return out


def _bracket_from_name(name: str, u: float, cfg: OpenBookConfig) -> tuple[float, float]:
    if name == "vol2":
        return primary_bracket(u, cfg)
    if name == "vol3":
        sl = _clip(u, cfg.sl_floor, cfg.sl_cap)
        return 3.0 * sl, sl
    # tpX_slY
    try:
        left, right = name.split("_")
        tp = float(left.replace("tp", ""))
        sl = float(right.replace("sl", ""))
        return tp, sl
    except Exception:
        return primary_bracket(u, cfg)


def levels_table(levels: list[dict[str, Any]], *, session_id: str, snap_ns: int, u: float) -> pa.Table:
    if not levels:
        return pa.table({})
    rows = []
    for lv in levels:
        rows.append({
            "session_id": session_id,
            "snap_ns": float(snap_ns),
            "side": lv["side"],
            "level_px": float(lv["centre_tick"]) * PTS_PER_TICK,
            "px_lo": float(lv["px_lo"]),
            "px_hi": float(lv["px_hi"]),
            "snap_size": float(lv["size"]),
            "age_weighted_size": float(lv.get("age_weighted_size", lv["size"])),
            "frac_old": float(lv.get("frac_old", 0.0)),
            "age_stratum": float(lv.get("age_stratum", 0.0)),
            "dist_mid_pts": float(lv.get("dist_mid_pts", 0.0)),
            "n_prices": float(lv["n_prices"]),
            "standout_thr": float(lv.get("standout_thr", 0.0)),
            "large_thr": float(lv.get("large_thr", 0.0)),
            "vol_unit": float(u),
        })
    cols: dict[str, Any] = {}
    for k in rows[0]:
        vals = [r[k] for r in rows]
        cols[k] = pa.array(vals, type=pa.string()) if isinstance(vals[0], str) else np.asarray(vals, dtype=np.float64)
    return pa.table(cols)


def arm_stats(tbl: pa.Table, arm: str, bracket: str = "vol2") -> dict[str, Any]:
    col = f"pnl_{arm}_{bracket}"
    if tbl.num_rows == 0 or col not in tbl.column_names:
        return pack_pnl(np.array([]), np.array([]), np.array([]))
    pnl = tbl.column(col).to_numpy().astype(np.float64)
    sess = np.array(tbl.column("session_id").to_pylist()) if "session_id" in tbl.column_names else np.array(["x"] * tbl.num_rows)
    ts = tbl.column("t_touch_ns").to_numpy().astype(np.float64) if "t_touch_ns" in tbl.column_names else np.arange(tbl.num_rows, dtype=np.float64)
    st = pack_pnl(pnl, sess, ts)
    m = np.isfinite(pnl)
    st["se"] = round(float(np.std(pnl[m], ddof=1) / np.sqrt(m.sum())), 3) if m.sum() > 1 else None
    return st
