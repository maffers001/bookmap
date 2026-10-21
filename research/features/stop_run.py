"""Stop-run fingerprint detector (inferred; CME never tags stops).

A stop-run candidate fires when price pierces a causal reference level by
``pierce_ticks``, then a short same-side aggression window shows a burst /
sweep / multi-aggressor fingerprint. Optional MBO far-side pull and ES
same-side confirm raise the score. Outcome (stall vs continue) is labeled
post-hoc and never part of the signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

from research.features.significant_swing import (
    SigSwingConfig,
    cluster_pivots,
    detect_pullaway_pivots,
)

UK = ZoneInfo("Europe/London")
PTS_PER_TICK = 0.25

LEVEL_KINDS = (
    "SWING_HIGH",
    "SWING_LOW",
    "SESSION_HIGH",
    "SESSION_LOW",
    "ON_HIGH",
    "ON_LOW",
)


@dataclass
class StopRunConfig:
    pierce_ticks: int = 2
    window_ms: int = 2000
    min_vol: float = 25.0
    min_prints: int = 8
    min_imbalance: float = 0.60
    min_sweeps: int = 1
    sweep_min_span_ticks: float = 2.0
    sweep_min_prints: int = 3
    mbo_radius_ticks: int = 4
    mbo_pre_ms: int = 1000
    mbo_min_cancels: int = 5
    mbo_min_pull_ratio: float = 0.60
    sync_ms: int = 1500
    cooldown_ms: int = 5000
    reclaim_sec: float = 5.0
    lookback_sec: float = 600.0
    pullaway_pts: float = 20.0
    cluster_tol_pts: float = 5.0
    invalidate_beyond_pts: float = 5.0
    use_cluster: bool = False
    level_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(LEVEL_KINDS)
    )
    pts_per_tick: float = PTS_PER_TICK
    tick_size: float = 1.0
    session_start_uk: time = time(14, 30)
    # Score component thresholds (binary flags summed into score 0-4)
    score_need_vol: bool = True
    score_need_imbalance: bool = True
    score_need_sweep: bool = True
    score_need_multi_aggr: bool = True
    min_score: int = 2  # fire only if at least this many components
    min_aggressors: int = 3


@dataclass(frozen=True)
class RefLevel:
    kind: str
    price_pts: float
    live_ts_ns: int  # first usable
    dead_ts_ns: int | None = None  # exclusive; None = still live at end


def _uk_rth_start(mid_ns: int, start: time) -> int:
    d = datetime.fromtimestamp(mid_ns / 1e9, tz=timezone.utc).astimezone(UK).date()
    return int(datetime.combine(d, start, tzinfo=UK).timestamp() * 1e9)


def _sorted_tape(
    trades: pa.Table, pts_per_tick: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * pts_per_tick
    sz = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    if "aggressor_order_id" in trades.column_names:
        aid = trades.column("aggressor_order_id").to_numpy(zero_copy_only=False)
    else:
        aid = np.array([""] * len(ts), dtype=object)
    order = np.argsort(ts)
    ts, px, sz, buy = ts[order], px[order], sz[order], buy[order]
    aid = [aid[i] for i in order]
    return ts, px, sz, buy, aid


def causal_levels(trades: pa.Table, cfg: StopRunConfig | None = None) -> list[RefLevel]:
    """Causal reference levels: pullaway swings + session/overnight H/L."""
    cfg = cfg or StopRunConfig()
    if trades.num_rows == 0:
        return []

    ts, px, _, _, _ = _sorted_tape(trades, cfg.pts_per_tick)
    mid = int(ts[len(ts) // 2])
    rth_start = _uk_rth_start(mid, cfg.session_start_uk)
    levels: list[RefLevel] = []
    kinds = cfg.level_kinds

    # --- Pullaway swing highs/lows ---
    if "SWING_HIGH" in kinds or "SWING_LOW" in kinds:
        swing_cfg = SigSwingConfig(
            lookback_sec=cfg.lookback_sec,
            pullaway_pts=cfg.pullaway_pts,
            cluster_tol_pts=cfg.cluster_tol_pts,
            invalidate_beyond_pts=cfg.invalidate_beyond_pts,
            pts_per_tick=cfg.pts_per_tick,
            tick_size=cfg.tick_size,
        )
        pivots = detect_pullaway_pivots(trades, swing_cfg)
        if cfg.use_cluster:
            pivots = cluster_pivots(pivots, tol_pts=cfg.cluster_tol_pts)
        # Walk tape to invalidate when price goes invalidate_beyond through
        for piv in pivots:
            kind = "SWING_HIGH" if piv.kind == "HIGH" else "SWING_LOW"
            if kind not in kinds:
                continue
            dead = None
            inv = cfg.invalidate_beyond_pts
            for i in range(len(ts)):
                if int(ts[i]) < piv.confirm_ts_ns:
                    continue
                p = float(px[i])
                if piv.kind == "HIGH" and p >= piv.price_pts + inv - 1e-12:
                    dead = int(ts[i])
                    break
                if piv.kind == "LOW" and p <= piv.price_pts - inv + 1e-12:
                    dead = int(ts[i])
                    break
            levels.append(
                RefLevel(
                    kind=kind,
                    price_pts=float(piv.price_pts),
                    live_ts_ns=int(piv.confirm_ts_ns),
                    dead_ts_ns=dead,
                )
            )

    # --- Overnight high/low (extract start → RTH open), live from RTH open ---
    pre = ts < rth_start
    if pre.any():
        on_hi = float(np.max(px[pre]))
        on_lo = float(np.min(px[pre]))
        on_hi_dead = None
        on_lo_dead = None
        for i in range(len(ts)):
            if int(ts[i]) < rth_start:
                continue
            p = float(px[i])
            if on_hi_dead is None and p >= on_hi + cfg.invalidate_beyond_pts - 1e-12:
                on_hi_dead = int(ts[i])
            if on_lo_dead is None and p <= on_lo - cfg.invalidate_beyond_pts + 1e-12:
                on_lo_dead = int(ts[i])
            if on_hi_dead is not None and on_lo_dead is not None:
                break
        if "ON_HIGH" in kinds:
            levels.append(RefLevel("ON_HIGH", on_hi, rth_start, on_hi_dead))
        if "ON_LOW" in kinds:
            levels.append(RefLevel("ON_LOW", on_lo, rth_start, on_lo_dead))

    # --- Session high/low ---
    # Lock an extreme after pullaway_pts away, then it is pierceable until
    # invalidated. Mirrors discretionary "stops above the high of day".
    if "SESSION_HIGH" in kinds or "SESSION_LOW" in kinds:
        run_hi = None
        run_lo = None
        locked_hi: float | None = None
        locked_lo: float | None = None
        locked_hi_ts: int | None = None
        locked_lo_ts: int | None = None
        pull = cfg.pullaway_pts
        inv = cfg.invalidate_beyond_pts
        for i in range(len(ts)):
            t = int(ts[i])
            if t < rth_start:
                continue
            p = float(px[i])
            if run_hi is None or p > run_hi + 1e-12:
                run_hi = p
            if run_lo is None or p < run_lo - 1e-12:
                run_lo = p
            # Lock after pullaway from running extreme
            if (
                "SESSION_HIGH" in kinds
                and locked_hi is None
                and run_hi is not None
                and run_hi - p >= pull - 1e-12
            ):
                locked_hi = run_hi
                locked_hi_ts = t
            if (
                "SESSION_LOW" in kinds
                and locked_lo is None
                and run_lo is not None
                and p - run_lo >= pull - 1e-12
            ):
                locked_lo = run_lo
                locked_lo_ts = t
            # Invalidate locked level when pierced deeply; allow re-lock later
            if locked_hi is not None and p >= locked_hi + inv - 1e-12:
                levels.append(
                    RefLevel("SESSION_HIGH", locked_hi, locked_hi_ts, t)
                )
                locked_hi = None
                locked_hi_ts = None
                run_hi = p  # new running high after break
            if locked_lo is not None and p <= locked_lo - inv + 1e-12:
                levels.append(
                    RefLevel("SESSION_LOW", locked_lo, locked_lo_ts, t)
                )
                locked_lo = None
                locked_lo_ts = None
                run_lo = p
        if locked_hi is not None and locked_hi_ts is not None:
            levels.append(RefLevel("SESSION_HIGH", locked_hi, locked_hi_ts, None))
        if locked_lo is not None and locked_lo_ts is not None:
            levels.append(RefLevel("SESSION_LOW", locked_lo, locked_lo_ts, None))

    levels.sort(key=lambda lv: lv.live_ts_ns)
    return levels


def _mbo_pull_ratio(
    mbo: pa.Table,
    pierce_ts: int,
    level_tick: int,
    side: int,
    cfg: StopRunConfig,
    *,
    _cache: dict | None = None,
) -> float:
    """Far-side cancel/(cancel+send) near level in the pre-pierce window.

    side +1 (through HIGH) → ask-book (is_bid=False) pull; -1 → bid-book.
    Cancels resolve side/price via prior send. Returns NaN if no MBO activity.

    Scans only [pierce_ts - warmup, pierce_ts] (warmup=60s) so we do not walk
    the full multi-10M MBO stream per event.
    """
    if mbo is None or mbo.num_rows == 0:
        return float("nan")
    pre_ns = int(cfg.mbo_pre_ms * 1_000_000)
    warmup_ns = 60_000_000_000  # 60s to resolve order ids
    t_warm = pierce_ts - warmup_ns
    t0 = pierce_ts - pre_ns
    want_bid = side < 0  # bid book for short runs

    if _cache is not None and "sorted" in _cache:
        m_ts, m_op, m_oid, m_px, m_bid_null, m_bid_vals = _cache["sorted"]
    else:
        m_ts = mbo.column("ts_ns").to_numpy().astype(np.int64)
        m_op = mbo.column("op").to_numpy(zero_copy_only=False)
        m_oid = mbo.column("order_id").to_numpy(zero_copy_only=False)
        m_px = mbo.column("price_ticks").to_numpy().astype(np.int64)
        m_bid_col = mbo.column("is_bid")
        m_bid_null = m_bid_col.is_null().to_numpy()
        m_bid_vals = m_bid_col.fill_null(False).to_numpy().astype(bool)
        order = np.argsort(m_ts)
        m_ts = m_ts[order]
        m_op = m_op[order]
        m_oid = [m_oid[i] for i in order]
        m_px = m_px[order]
        m_bid_null = m_bid_null[order]
        m_bid_vals = m_bid_vals[order]
        packed = (m_ts, m_op, m_oid, m_px, m_bid_null, m_bid_vals)
        if _cache is not None:
            _cache["sorted"] = packed
        m_ts, m_op, m_oid, m_px, m_bid_null, m_bid_vals = packed

    lo = int(np.searchsorted(m_ts, t_warm, side="left"))
    hi = int(np.searchsorted(m_ts, pierce_ts, side="right"))
    active: dict[str, tuple[bool, int]] = {}
    cancels = sends = 0
    for idx in range(lo, hi):
        ts = int(m_ts[idx])
        op = m_op[idx]
        oid = m_oid[idx]
        if not oid:
            continue
        if op == "send":
            if m_bid_null[idx]:
                continue
            is_bid = bool(m_bid_vals[idx])
            px = int(m_px[idx])
            active[oid] = (is_bid, px)
            if t0 <= ts <= pierce_ts and is_bid == want_bid:
                if abs(px - level_tick) <= cfg.mbo_radius_ticks:
                    sends += 1
        elif op == "replace":
            if oid not in active:
                continue
            is_bid, _ = active[oid]
            px = int(m_px[idx])
            active[oid] = (is_bid, px)
            if t0 <= ts <= pierce_ts and is_bid == want_bid:
                if abs(px - level_tick) <= cfg.mbo_radius_ticks:
                    sends += 1
        elif op == "cancel":
            if oid not in active:
                continue
            is_bid, px = active.pop(oid)
            if t0 <= ts <= pierce_ts and is_bid == want_bid:
                if abs(px - level_tick) <= cfg.mbo_radius_ticks:
                    cancels += 1
    total = cancels + sends
    if total <= 0:
        return float("nan")
    return cancels / total


def _es_same_side_burst_ts(
    es_ts: np.ndarray,
    es_px: np.ndarray,
    es_sz: np.ndarray,
    es_buy: np.ndarray,
    event_ts: int,
    side: int,
    sync_ms: int,
    *,
    bar_ms: int = 100,
    min_trades: int = 6,
    min_vol: float = 15.0,
    min_imbalance: float = 0.55,
) -> int | None:
    """Nearest same-side 100ms burst on ES within ±sync_ms of event_ts."""
    if len(es_ts) == 0:
        return None
    window = int(sync_ms * 1_000_000)
    bar_ns = int(bar_ms * 1_000_000)
    lo = event_ts - window
    hi = event_ts + window
    # Aggregate bars in [lo, hi]
    mask = (es_ts >= lo - bar_ns) & (es_ts <= hi + bar_ns)
    if not mask.any():
        return None
    idx = np.where(mask)[0]
    bars: dict[int, list] = {}  # slot -> [buy, sell, n, open, close]
    for i in idx:
        slot = (int(es_ts[i]) // bar_ns) * bar_ns
        e = bars.get(slot)
        if e is None:
            bars[slot] = [
                float(es_sz[i]) if es_buy[i] else 0.0,
                0.0 if es_buy[i] else float(es_sz[i]),
                1,
                float(es_px[i]),
                float(es_px[i]),
            ]
        else:
            if es_buy[i]:
                e[0] += float(es_sz[i])
            else:
                e[1] += float(es_sz[i])
            e[2] += 1
            e[4] = float(es_px[i])
    best = None
    best_abs = None
    for slot, e in bars.items():
        end_ts = slot + bar_ns - 1
        if end_ts < lo or end_ts > hi:
            continue
        total = e[0] + e[1]
        if total < min_vol or e[2] < min_trades:
            continue
        imb = (e[0] - e[1]) / total
        net = e[4] - e[3]
        if side > 0 and not (imb >= min_imbalance and net > 0):
            continue
        if side < 0 and not (imb <= -min_imbalance and net < 0):
            continue
        lag = abs(end_ts - event_ts)
        if best is None or lag < best_abs:
            best = end_ts
            best_abs = lag
    return best


def stop_run_events(
    nq_trades: pa.Table,
    es_trades: pa.Table | None = None,
    mbo: pa.Table | None = None,
    cfg: StopRunConfig | None = None,
    *,
    levels: list[RefLevel] | None = None,
) -> pa.Table:
    """Emit stop-run events stamped at fingerprint window end (causal)."""
    cfg = cfg or StopRunConfig()
    if nq_trades.num_rows == 0:
        return pa.table({})

    ts, px, sz, buy, aid = _sorted_tape(nq_trades, cfg.pts_per_tick)
    if levels is None:
        levels = causal_levels(nq_trades, cfg)
    if not levels:
        return pa.table({})

    es_ts = es_px = es_sz = es_buy = None
    if es_trades is not None and es_trades.num_rows:
        es_ts, es_px, es_sz, es_buy, _ = _sorted_tape(es_trades, cfg.pts_per_tick)

    mbo_cache: dict = {}
    window_ns = int(cfg.window_ms * 1_000_000)
    cooldown_ns = int(cfg.cooldown_ms * 1_000_000)
    pierce_pts = cfg.pierce_ticks * cfg.pts_per_tick
    tick_pts = cfg.pts_per_tick

    # Per level: whether pierce already opened a window
    pierced: set[int] = set()  # id(level) once pierced while live
    # Active windows: list of dicts
    active_wins: list[dict] = []
    events: list[dict] = []
    last_fire = {-1: -10**18, 1: -10**18}

    # Index into sorted tape for window close
    i = 0
    n = len(ts)

    def _close_window(w: dict, end_i: int) -> None:
        side = w["side"]
        t_end = int(ts[end_i]) if end_i < n else w["pierce_ts"] + window_ns
        # Collect prints in [pierce_i, end_i]
        i0 = w["pierce_i"]
        i1 = end_i
        if i1 < i0:
            return
        buy_vol = sell_vol = 0.0
        sizes = []
        aids: dict[str, list] = {}  # oid -> [n, vol, min_tick, max_tick]
        max_pen = 0.0
        level_px = w["level_px"]
        for j in range(i0, i1 + 1):
            sizes.append(float(sz[j]))
            if buy[j]:
                buy_vol += float(sz[j])
            else:
                sell_vol += float(sz[j])
            a = aid[j]
            if a:
                tick = round(float(px[j]) / tick_pts)
                e = aids.get(a)
                if e is None:
                    aids[a] = [1, float(sz[j]), tick, tick]
                else:
                    e[0] += 1
                    e[1] += float(sz[j])
                    e[2] = min(e[2], tick)
                    e[3] = max(e[3], tick)
            if side > 0:
                max_pen = max(max_pen, (float(px[j]) - level_px) / tick_pts)
            else:
                max_pen = max(max_pen, (level_px - float(px[j])) / tick_pts)

        total = buy_vol + sell_vol
        n_prints = i1 - i0 + 1
        if total <= 0 or n_prints < cfg.min_prints:
            return
        imb = (buy_vol - sell_vol) / total
        if side > 0:
            side_vol = buy_vol
            side_imb = imb
        else:
            side_vol = sell_vol
            side_imb = -imb
        if side_vol < cfg.min_vol or side_imb < cfg.min_imbalance:
            return

        n_sweeps = 0
        for a, e in aids.items():
            if e[0] >= cfg.sweep_min_prints and (e[3] - e[2]) >= cfg.sweep_min_span_ticks:
                n_sweeps += 1
        n_aggressors = len(aids)
        med_sz = float(np.median(sizes)) if sizes else 0.0

        # Score components (0-4)
        c_vol = 1 if side_vol >= cfg.min_vol else 0
        c_imb = 1 if side_imb >= cfg.min_imbalance else 0
        c_swp = 1 if n_sweeps >= cfg.min_sweeps else 0
        c_agg = 1 if n_aggressors >= cfg.min_aggressors else 0
        score = c_vol + c_imb + c_swp + c_agg
        if score < cfg.min_score:
            return

        # Stamp at window end (last print used)
        event_ts = int(ts[i1])
        if event_ts - last_fire[side] < cooldown_ns:
            return

        level_tick = int(round(level_px / tick_pts))
        pull = _mbo_pull_ratio(
            mbo, w["pierce_ts"], level_tick, side, cfg, _cache=mbo_cache
        )
        pull_flag = (
            not np.isnan(pull)
            and pull >= cfg.mbo_min_pull_ratio
            # cancels counted inside ratio; require some absolute activity
        )

        es_lag = float("nan")
        es_conf = False
        if es_ts is not None:
            es_burst = _es_same_side_burst_ts(
                es_ts, es_px, es_sz, es_buy, event_ts, side, cfg.sync_ms
            )
            if es_burst is not None:
                es_lag = (es_burst - event_ts) / 1e6
                es_conf = True

        events.append({
            "event_ts_ns": event_ts,
            "side": side,
            "event_type": "stop_run",
            "level_px": level_px,
            "level_kind": w["level_kind"],
            "pierce_ts_ns": w["pierce_ts"],
            "n_prints": n_prints,
            "total_vol": total,
            "side_vol": side_vol,
            "imbalance": side_imb,
            "n_aggressors": n_aggressors,
            "n_sweeps": n_sweeps,
            "max_penetration_ticks": max_pen,
            "median_print_size": med_sz,
            "pull_ratio": pull if not np.isnan(pull) else -1.0,
            "mbo_pull": bool(pull_flag),
            "es_lag_ms": es_lag if not np.isnan(es_lag) else 1e9,
            "es_confirmed": es_conf,
            "score": score,
            "c_vol": c_vol,
            "c_imb": c_imb,
            "c_sweep": c_swp,
            "c_aggr": c_agg,
        })
        last_fire[side] = event_ts

    while i < n:
        t = int(ts[i])
        p = float(px[i])

        # Close any windows whose end has arrived
        still = []
        for w in active_wins:
            if t >= w["end_ts"]:
                # Find last print index <= end_ts
                end_i = i
                while end_i > w["pierce_i"] and int(ts[end_i]) > w["end_ts"]:
                    end_i -= 1
                _close_window(w, end_i)
            else:
                still.append(w)
        active_wins = still

        # Check live levels for pierce
        for lv in levels:
            if id(lv) in pierced:
                continue
            if t < lv.live_ts_ns:
                continue
            if lv.dead_ts_ns is not None and t >= lv.dead_ts_ns:
                pierced.add(id(lv))
                continue
            is_high = lv.kind.endswith("HIGH")
            if is_high:
                if p >= lv.price_pts + pierce_pts - 1e-12:
                    side = 1
                else:
                    continue
            else:
                if p <= lv.price_pts - pierce_pts + 1e-12:
                    side = -1
                else:
                    continue
            pierced.add(id(lv))
            active_wins.append({
                "side": side,
                "level_px": lv.price_pts,
                "level_kind": lv.kind,
                "pierce_ts": t,
                "pierce_i": i,
                "end_ts": t + window_ns,
            })

        i += 1

    # Flush remaining windows at end of tape
    for w in active_wins:
        end_i = n - 1
        _close_window(w, end_i)

    if not events:
        return pa.table({})

    events.sort(key=lambda e: e["event_ts_ns"])
    return pa.table({
        "event_ts_ns": np.array([e["event_ts_ns"] for e in events], dtype=np.int64),
        "side": np.array([e["side"] for e in events], dtype=np.int8),
        "event_type": [e["event_type"] for e in events],
        "level_px": np.array([e["level_px"] for e in events], dtype=np.float64),
        "level_kind": [e["level_kind"] for e in events],
        "pierce_ts_ns": np.array([e["pierce_ts_ns"] for e in events], dtype=np.int64),
        "n_prints": np.array([e["n_prints"] for e in events], dtype=np.int32),
        "total_vol": np.array([e["total_vol"] for e in events], dtype=np.float64),
        "side_vol": np.array([e["side_vol"] for e in events], dtype=np.float64),
        "imbalance": np.array([e["imbalance"] for e in events], dtype=np.float64),
        "n_aggressors": np.array([e["n_aggressors"] for e in events], dtype=np.int32),
        "n_sweeps": np.array([e["n_sweeps"] for e in events], dtype=np.int32),
        "max_penetration_ticks": np.array(
            [e["max_penetration_ticks"] for e in events], dtype=np.float64
        ),
        "median_print_size": np.array(
            [e["median_print_size"] for e in events], dtype=np.float64
        ),
        "pull_ratio": np.array([e["pull_ratio"] for e in events], dtype=np.float64),
        "mbo_pull": np.array([e["mbo_pull"] for e in events], dtype=bool),
        "es_lag_ms": np.array([e["es_lag_ms"] for e in events], dtype=np.float64),
        "es_confirmed": np.array([e["es_confirmed"] for e in events], dtype=bool),
        "score": np.array([e["score"] for e in events], dtype=np.int8),
        "c_vol": np.array([e["c_vol"] for e in events], dtype=np.int8),
        "c_imb": np.array([e["c_imb"] for e in events], dtype=np.int8),
        "c_sweep": np.array([e["c_sweep"] for e in events], dtype=np.int8),
        "c_aggr": np.array([e["c_aggr"] for e in events], dtype=np.int8),
    })


def resolve_outcome(
    events: pa.Table,
    nq_trades: pa.Table,
    reclaim_sec: float = 5.0,
    *,
    pts_per_tick: float = PTS_PER_TICK,
) -> pa.Table:
    """Post-hoc label: stall (reclaim inside level) vs continue.

    Never part of the signal. Adds ``outcome`` and ``reclaim_ts_ns`` columns.
    """
    if events.num_rows == 0 or nq_trades.num_rows == 0:
        return events
    ts, px, _, _, _ = _sorted_tape(nq_trades, pts_per_tick)
    reclaim_ns = int(reclaim_sec * 1e9)
    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy().astype(int)
    levels = events.column("level_px").to_numpy().astype(np.float64)

    outcomes = []
    reclaim_ts = []
    for k in range(len(ev_ts)):
        t0 = int(ev_ts[k])
        side = int(sides[k])
        lv = float(levels[k])
        i0 = int(np.searchsorted(ts, t0, side="right"))
        t_end = t0 + reclaim_ns
        found = None
        for j in range(i0, len(ts)):
            if int(ts[j]) > t_end:
                break
            p = float(px[j])
            if side > 0 and p <= lv + 1e-12:
                found = int(ts[j])
                break
            if side < 0 and p >= lv - 1e-12:
                found = int(ts[j])
                break
        if found is not None:
            outcomes.append("stall")
            reclaim_ts.append(found)
        else:
            outcomes.append("continue")
            reclaim_ts.append(0)

    out = events
    for name in ("outcome", "reclaim_ts_ns"):
        if name in out.column_names:
            out = out.drop(name)
    out = out.append_column("outcome", pa.array(outcomes, type=pa.string()))
    out = out.append_column(
        "reclaim_ts_ns", pa.array(reclaim_ts, type=pa.int64())
    )
    return out


def fade_events_at_reclaim(events: pa.Table) -> pa.Table:
    """Re-stamp stall events at reclaim time with flipped side for fade P&L."""
    if events.num_rows == 0 or "outcome" not in events.column_names:
        return pa.table({})
    outcome = events.column("outcome").to_numpy(zero_copy_only=False)
    reclaim = events.column("reclaim_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy().astype(np.int8)
    keep = []
    for i in range(len(outcome)):
        if outcome[i] == "stall" and reclaim[i] > 0:
            keep.append(i)
    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    out = events.take(idx)
    new_ts = reclaim[idx]
    new_side = -sides[idx]
    # replace columns
    names = out.column_names
    cols = []
    for name in names:
        if name == "event_ts_ns":
            cols.append(pa.array(new_ts, type=pa.int64()))
        elif name == "side":
            cols.append(pa.array(new_side, type=pa.int8()))
        else:
            cols.append(out.column(name))
    return pa.table({n: c for n, c in zip(names, cols)})
