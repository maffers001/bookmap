"""IRT level approach / dual sync / hold-fail classification (H33)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.irt_lvn.load_irt import KIND_DOP, KIND_DVPOC, KIND_LVN, IrtBars

PTS_PER_TICK = 0.25


@dataclass
class MarketPierceConfig:
    pierce_typical: float
    pierce_fail: float
    reject_away: float
    pad_pts: float


@dataclass
class IrtTouchConfig:
    sync_ms: int = 5000
    touch_cooldown_ms: int = 8000
    decision_horizon_ms: int = 120_000
    nq: MarketPierceConfig | None = None
    es: MarketPierceConfig | None = None

    def __post_init__(self) -> None:
        if self.nq is None:
            self.nq = MarketPierceConfig(12.0, 35.0, 15.0, 3.0)
        if self.es is None:
            self.es = MarketPierceConfig(4.0, 12.0, 5.0, 1.25)


def _asof_idx(bar_ts: np.ndarray, t: int) -> int:
    """Largest i with bar_ts[i] <= t, or -1."""
    i = int(np.searchsorted(bar_ts, t, side="right") - 1)
    return i


def _active_levels(bars: IrtBars, i: int) -> list[tuple[str, str, float]]:
    """(kind, level_id, price) for non-zero levels at bar i."""
    out: list[tuple[str, str, float]] = []
    if i < 0 or i >= len(bars.ts_ns):
        return out
    for kind, lid, px in (
        (KIND_LVN, "LVN2", float(bars.lvn2[i])),
        (KIND_LVN, "LVN3", float(bars.lvn3[i])),
        (KIND_DOP, "DOP", float(bars.dop[i])),
        (KIND_DVPOC, "dVPOC", float(bars.dvpoc[i])),
    ):
        if px != 0.0:
            out.append((kind, lid, px))
    return out


def detect_irt_touches(
    trades: pa.Table,
    bars: IrtBars,
    mkt: MarketPierceConfig,
    *,
    cooldown_ms: int = 8000,
) -> pa.Table:
    """Single-market approaches into pad around IRT levels (chart points)."""
    if trades.num_rows == 0 or len(bars.ts_ns) == 0:
        return pa.table({})

    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px_ticks = trades.column("price").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts = ts[order]
    px = px_ticks[order] * PTS_PER_TICK

    cool_ns = int(cooldown_ms * 1_000_000)
    last_fire: dict[str, int] = {}  # kind -> last ts
    events: list[dict] = []
    pad = float(mkt.pad_pts)

    for i in range(1, len(ts)):
        bi = _asof_idx(bars.ts_ns, int(ts[i]))
        levels = _active_levels(bars, bi)
        if not levels:
            continue
        p = float(px[i])
        prev = float(px[i - 1])
        for kind, lid, level in levels:
            lo, hi = level - pad, level + pad
            if not (lo <= p <= hi):
                continue
            # approach from outside the pad
            if prev < lo:
                approach = 1  # from below → resistance test → fade short
            elif prev > hi:
                approach = -1  # from above → support test → fade long
            else:
                continue
            last = last_fire.get(kind, -10**18)
            if int(ts[i]) - last < cool_ns:
                continue
            side = -approach
            events.append({
                "event_ts_ns": int(ts[i]),
                "side": side,
                "approach": approach,
                "kind": kind,
                "level_id": lid,
                "level_px": level,
                "entry_px": p,
            })
            last_fire[kind] = int(ts[i])
            break  # one fire per trade print

    if not events:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.array([e["event_ts_ns"] for e in events], dtype=np.int64),
        "side": np.array([e["side"] for e in events], dtype=np.int8),
        "approach": np.array([e["approach"] for e in events], dtype=np.int8),
        "kind": [e["kind"] for e in events],
        "level_id": [e["level_id"] for e in events],
        "level_px": np.array([e["level_px"] for e in events], dtype=np.float64),
        "entry_px": np.array([e["entry_px"] for e in events], dtype=np.float64),
        "event_type": ["irt_touch"] * len(events),
    })


def dual_irt_touch_events(
    nq_touches: pa.Table,
    es_touches: pa.Table,
    *,
    sync_ms: int = 5000,
) -> tuple[pa.Table, dict]:
    """Cross-kind dual: NQ touch + any ES touch same approach within sync_ms."""
    meta = {
        "nq_touches": int(nq_touches.num_rows),
        "es_touches": int(es_touches.num_rows),
        "sync_ms": sync_ms,
    }
    if nq_touches.num_rows == 0 or es_touches.num_rows == 0:
        meta["dual_touches"] = 0
        return pa.table({}), meta

    nq_ts = nq_touches.column("event_ts_ns").to_numpy().astype(np.int64)
    nq_app = nq_touches.column("approach").to_numpy().astype(np.int8)
    es_ts = es_touches.column("event_ts_ns").to_numpy().astype(np.int64)
    es_app = es_touches.column("approach").to_numpy().astype(np.int8)
    es_kind = es_touches.column("kind").to_pylist()
    es_lid = es_touches.column("level_id").to_pylist()
    es_lpx = es_touches.column("level_px").to_numpy().astype(np.float64)

    order = np.argsort(es_ts, kind="stable")
    es_ts = es_ts[order]
    es_app = es_app[order]
    es_kind = [es_kind[i] for i in order]
    es_lid = [es_lid[i] for i in order]
    es_lpx = es_lpx[order]

    window = int(sync_ms * 1_000_000)
    keep: list[int] = []
    lags: list[float] = []
    es_kinds: list[str] = []
    es_lids: list[str] = []
    es_lpxs: list[float] = []
    es_tss: list[int] = []

    for i in range(len(nq_ts)):
        lo = int(np.searchsorted(es_ts, nq_ts[i] - window, side="left"))
        hi = int(np.searchsorted(es_ts, nq_ts[i] + window, side="right"))
        best = None
        best_abs = None
        for j in range(lo, hi):
            if es_app[j] != nq_app[i]:
                continue
            lag = abs(int(es_ts[j] - nq_ts[i]))
            if best_abs is None or lag < best_abs:
                best_abs = lag
                best = j
        if best is not None:
            keep.append(i)
            lags.append(best_abs / 1e6)
            es_kinds.append(es_kind[best])
            es_lids.append(es_lid[best])
            es_lpxs.append(float(es_lpx[best]))
            es_tss.append(int(es_ts[best]))

    meta["dual_touches"] = len(keep)
    if not keep:
        return pa.table({}), meta

    idx = np.asarray(keep, dtype=np.int64)
    dual = pa.table({
        "event_ts_ns": nq_ts[idx],
        "side": nq_touches.column("side").take(idx),
        "approach": nq_app[idx],
        "nq_kind": nq_touches.column("kind").take(idx),
        "nq_level_id": nq_touches.column("level_id").take(idx),
        "nq_level_px": nq_touches.column("level_px").take(idx),
        "nq_entry_px": nq_touches.column("entry_px").take(idx),
        "es_kind": es_kinds,
        "es_level_id": es_lids,
        "es_level_px": np.asarray(es_lpxs, dtype=np.float64),
        "es_event_ts_ns": np.asarray(es_tss, dtype=np.int64),
        "es_lag_ms": np.asarray(lags, dtype=np.float64),
        "same_kind": [
            str(nq_touches.column("kind")[int(i)].as_py()) == es_kinds[k]
            for k, i in enumerate(idx)
        ],
        "event_type": ["irt_dual_touch"] * len(idx),
    })
    return dual, meta


def _classify_one(
    path_ts: np.ndarray,
    path_px: np.ndarray,
    t0: int,
    approach: int,
    level: float,
    mkt: MarketPierceConfig,
    horizon_ms: int,
) -> dict:
    """hold / fail / ambiguous + pierce depth + optional reclaim stamp."""
    i0 = int(np.searchsorted(path_ts, t0, side="left"))
    if i0 >= len(path_ts):
        return {
            "outcome": "ambiguous",
            "pierce_pts": 0.0,
            "reclaim_ts_ns": None,
            "clean_fail": False,
        }
    t_end = t0 + int(horizon_ms * 1_000_000)
    i1 = int(np.searchsorted(path_ts, t_end, side="right"))
    if i1 <= i0:
        i1 = min(i0 + 1, len(path_ts))

    seg = path_px[i0:i1]
    seg_ts = path_ts[i0:i1]
    if len(seg) == 0:
        return {
            "outcome": "ambiguous",
            "pierce_pts": 0.0,
            "reclaim_ts_ns": None,
            "clean_fail": False,
        }

    # pierce beyond level in approach direction
    if approach > 0:
        pierce = float(np.max(seg) - level)
    else:
        pierce = float(level - np.min(seg))
    pierce = max(0.0, pierce)

    fail_thr = float(mkt.pierce_fail)
    reject = float(mkt.reject_away)
    typical = float(mkt.pierce_typical)
    pullback_cap = typical / 3.0

    reclaim_ts = None
    hold = False
    for j in range(len(seg)):
        p = float(seg[j])
        if approach > 0:
            # resistance: hold when back below level by reject_away
            if p <= level - reject:
                hold = True
                reclaim_ts = int(seg_ts[j])
                break
        else:
            if p >= level + reject:
                hold = True
                reclaim_ts = int(seg_ts[j])
                break

    # clean fail: reach fail_thr with limited pullback from extreme excursion path
    clean_fail = False
    if pierce >= fail_thr:
        # track max adverse from level and min pullback before hitting fail
        running_pierce = 0.0
        min_after_ext = 0.0
        for p in seg:
            if approach > 0:
                cur = float(p) - level
                if cur > running_pierce:
                    running_pierce = cur
                    min_after_ext = cur
                else:
                    min_after_ext = min(min_after_ext, cur)
                if running_pierce >= fail_thr and (running_pierce - min_after_ext) <= pullback_cap:
                    clean_fail = True
                    break
            else:
                cur = level - float(p)
                if cur > running_pierce:
                    running_pierce = cur
                    min_after_ext = cur
                else:
                    min_after_ext = min(min_after_ext, cur)
                if running_pierce >= fail_thr and (running_pierce - min_after_ext) <= pullback_cap:
                    clean_fail = True
                    break

    if pierce >= fail_thr or clean_fail:
        outcome = "fail"
        # still record reclaim if it happened before fail threshold was dominant
        if hold and pierce < fail_thr:
            outcome = "hold"
    elif hold:
        outcome = "hold"
    else:
        outcome = "ambiguous"

    # Reclaim-valid for fade entry: hold with pierce never reaching fail
    if outcome == "hold" and pierce < fail_thr and reclaim_ts is not None:
        pass
    else:
        if outcome != "hold":
            reclaim_ts = None

    return {
        "outcome": outcome,
        "pierce_pts": round(pierce, 3),
        "reclaim_ts_ns": reclaim_ts,
        "clean_fail": bool(clean_fail),
    }


def classify_dual_events(
    dual: pa.Table,
    nq_path_ts: np.ndarray,
    nq_path_px: np.ndarray,
    es_path_ts: np.ndarray,
    es_path_px: np.ndarray,
    cfg: IrtTouchConfig,
) -> pa.Table:
    """Annotate dual touches with NQ/ES outcomes and reclaim stamp."""
    if dual.num_rows == 0:
        return dual
    assert cfg.nq is not None and cfg.es is not None

    n = dual.num_rows
    nq_out = []
    es_out = []
    nq_pierce = []
    es_pierce = []
    reclaim_ts = []
    reclaim_side = []
    joint = []

    ts0 = dual.column("event_ts_ns").to_numpy().astype(np.int64)
    es_t0 = dual.column("es_event_ts_ns").to_numpy().astype(np.int64)
    approach = dual.column("approach").to_numpy().astype(np.int8)
    nq_lvl = dual.column("nq_level_px").to_numpy().astype(np.float64)
    es_lvl = dual.column("es_level_px").to_numpy().astype(np.float64)
    sides = dual.column("side").to_numpy().astype(np.int8)

    for i in range(n):
        nq_c = _classify_one(
            nq_path_ts, nq_path_px, int(ts0[i]), int(approach[i]),
            float(nq_lvl[i]), cfg.nq, cfg.decision_horizon_ms,
        )
        es_c = _classify_one(
            es_path_ts, es_path_px, int(es_t0[i]), int(approach[i]),
            float(es_lvl[i]), cfg.es, cfg.decision_horizon_ms,
        )
        nq_out.append(nq_c["outcome"])
        es_out.append(es_c["outcome"])
        nq_pierce.append(nq_c["pierce_pts"])
        es_pierce.append(es_c["pierce_pts"])

        # joint: both hold, or NQ hold and ES not fail
        if nq_c["outcome"] == "hold" and es_c["outcome"] != "fail":
            j = "hold"
            r_ts = nq_c["reclaim_ts_ns"]
        elif nq_c["outcome"] == "fail" or es_c["outcome"] == "fail":
            j = "fail"
            r_ts = None
        else:
            j = "ambiguous"
            r_ts = None
        joint.append(j)
        reclaim_ts.append(-1 if r_ts is None else int(r_ts))
        reclaim_side.append(int(sides[i]) if r_ts is not None else 0)

    cols = {name: dual.column(name) for name in dual.column_names}
    cols["nq_outcome"] = nq_out
    cols["es_outcome"] = es_out
    cols["joint_outcome"] = joint
    cols["nq_pierce_pts"] = np.asarray(nq_pierce, dtype=np.float64)
    cols["es_pierce_pts"] = np.asarray(es_pierce, dtype=np.float64)
    cols["reclaim_ts_ns"] = np.asarray(reclaim_ts, dtype=np.int64)
    cols["reclaim_side"] = np.asarray(reclaim_side, dtype=np.int8)
    return pa.table(cols)


def reclaim_events_table(classified: pa.Table) -> pa.Table:
    """P&L stamps: reclaim_ts where joint hold and reclaim valid."""
    if classified.num_rows == 0:
        return pa.table({})
    rts = classified.column("reclaim_ts_ns").to_numpy().astype(np.int64)
    sides = classified.column("reclaim_side").to_numpy().astype(np.int8)
    joint = classified.column("joint_outcome").to_pylist()
    keep = [i for i in range(len(rts)) if rts[i] > 0 and joint[i] == "hold" and sides[i] != 0]
    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    return pa.table({
        "event_ts_ns": rts[idx],
        "side": sides[idx],
        "event_type": ["irt_reclaim"] * len(idx),
        "nq_kind": classified.column("nq_kind").take(idx),
        "es_kind": classified.column("es_kind").take(idx),
        "touch_ts_ns": classified.column("event_ts_ns").take(idx),
        "nq_pierce_pts": classified.column("nq_pierce_pts").take(idx),
        "nq_level_px": classified.column("nq_level_px").take(idx),
        "approach": classified.column("approach").take(idx),
        "same_kind": classified.column("same_kind").take(idx),
    })


def pierce_bucket(pts: float, *, nq: bool = True) -> str:
    if nq:
        edges = [(8, "0-8"), (15, "8-15"), (25, "15-25"), (35, "25-35")]
    else:
        edges = [(3, "0-3"), (5, "3-5"), (8, "5-8"), (12, "8-12")]
    for hi, label in edges:
        if pts < hi:
            return label
    return f"{edges[-1][0]}+"
