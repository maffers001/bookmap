"""Same-aggressor multi-print sweep detection from trade tape."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class SweepConfig:
    min_prints: int = 5
    min_vol: float = 5.0
    min_span_ticks: float = 1.0
    max_duration_ms: int = 2000
    tick_size: float = 1.0
    cooldown_ms: int = 500
    # "end" = last print of cluster (legacy / default, matches pre-fix EXP-0017).
    # "qualify" = first print at which prints/vol/span thresholds are all met
    # (live-tradeable; stamp = last input used).
    stamp: str = "end"


def aggressor_sweep_events(
    trades: pa.Table,
    cfg: SweepConfig | None = None,
) -> pa.Table:
    """Emit continuation events for qualifying aggressor clusters.

    Side = aggressor direction (+1 buy / -1 sell). Does not use is_exec_end
    (unreliable in extracts).

    ``cfg.stamp``:
      - ``"end"`` (default): ``event_ts_ns`` = last print of the cluster.
      - ``"qualify"``: ``event_ts_ns`` = first print where min_prints, min_vol
        and min_span_ticks are all satisfied. Always keep ``end_ts_ns``.
    """
    cfg = cfg or SweepConfig()
    if cfg.stamp not in ("end", "qualify"):
        raise ValueError(f"unknown sweep stamp: {cfg.stamp}")
    if trades.num_rows == 0 or "aggressor_order_id" not in trades.column_names:
        return pa.table({})

    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64)
    sz = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    aid = trades.column("aggressor_order_id").to_numpy(zero_copy_only=False)

    order = np.argsort(ts)
    ts, px, sz, buy = ts[order], px[order], sz[order], buy[order]
    aid = [aid[i] for i in order]
    tick_px = np.round(px / cfg.tick_size)

    # Accumulate per aggressor id
    # n, vol, min_px, max_px, buy_vol, sell_vol, first_ts, last_ts, qualify_ts
    acc: dict[str, list] = {}
    for i in range(len(ts)):
        a = aid[i]
        if not a:
            continue
        e = acc.get(a)
        if e is None:
            e = [
                1,
                float(sz[i]),
                float(tick_px[i]),
                float(tick_px[i]),
                float(sz[i]) if buy[i] else 0.0,
                0.0 if buy[i] else float(sz[i]),
                int(ts[i]),
                int(ts[i]),
                None,  # qualify_ts
            ]
            acc[a] = e
        else:
            e[0] += 1
            e[1] += float(sz[i])
            p = float(tick_px[i])
            e[2] = min(e[2], p)
            e[3] = max(e[3], p)
            if buy[i]:
                e[4] += float(sz[i])
            else:
                e[5] += float(sz[i])
            e[7] = int(ts[i])
        # First print at which all thresholds are met
        if e[8] is None:
            span = e[3] - e[2]
            if (
                e[0] >= cfg.min_prints
                and e[1] >= cfg.min_vol
                and span >= cfg.min_span_ticks
            ):
                e[8] = int(ts[i])

    max_dur_ns = int(cfg.max_duration_ms * 1_000_000)
    events = []
    for a, e in acc.items():
        n, vol, lo, hi, bv, sv, t0, t1, q_ts = e
        if n < cfg.min_prints or vol < cfg.min_vol:
            continue
        span = hi - lo
        if span < cfg.min_span_ticks:
            continue
        if t1 - t0 > max_dur_ns:
            continue
        # Require one-sided aggression (dominant side ≥ 70% of vol)
        if bv + sv <= 0:
            continue
        if bv >= 0.7 * (bv + sv):
            side = 1
        elif sv >= 0.7 * (bv + sv):
            side = -1
        else:
            continue
        qualify_ts = int(q_ts) if q_ts is not None else int(t1)
        stamp_ts = qualify_ts if cfg.stamp == "qualify" else int(t1)
        events.append({
            "event_ts_ns": stamp_ts,
            "end_ts_ns": int(t1),
            "qualify_ts_ns": qualify_ts,
            "side": side,
            "n_prints": n,
            "total_vol": vol,
            "span_ticks": span,
            "duration_ms": (t1 - t0) / 1e6,
            "aggressor_order_id": a,
        })

    if not events:
        return pa.table({})

    # Sort and apply cooldown on same side (by stamped time)
    events.sort(key=lambda x: x["event_ts_ns"])
    cooldown_ns = int(cfg.cooldown_ms * 1_000_000)
    last_fire = {-1: -10**18, 1: -10**18}
    kept = []
    for ev in events:
        s = ev["side"]
        if ev["event_ts_ns"] - last_fire[s] < cooldown_ns:
            continue
        kept.append(ev)
        last_fire[s] = ev["event_ts_ns"]

    if not kept:
        return pa.table({})

    return pa.table({
        "event_ts_ns": np.array([e["event_ts_ns"] for e in kept], dtype=np.int64),
        "end_ts_ns": np.array([e["end_ts_ns"] for e in kept], dtype=np.int64),
        "qualify_ts_ns": np.array([e["qualify_ts_ns"] for e in kept], dtype=np.int64),
        "side": np.array([e["side"] for e in kept], dtype=np.int8),
        "event_type": ["aggressor_sweep"] * len(kept),
        "n_prints": np.array([e["n_prints"] for e in kept], dtype=np.int32),
        "total_vol": np.array([e["total_vol"] for e in kept], dtype=np.float64),
        "span_ticks": np.array([e["span_ticks"] for e in kept], dtype=np.float64),
        "duration_ms": np.array([e["duration_ms"] for e in kept], dtype=np.float64),
        "aggressor_order_id": [e["aggressor_order_id"] for e in kept],
    })


def cross_confirm_sweeps(
    nq_events: pa.Table,
    es_events: pa.Table,
    *,
    sync_ms: int = 1500,
    stamp: str = "nq",
) -> pa.Table:
    """Keep NQ sweeps with same-side ES sweep within ±sync_ms; annotate lag.

    ``stamp`` modes:
      - ``"nq"`` (default, legacy): keep NQ ``event_ts_ns`` even if ES is later.
      - ``"confirm"``: ``event_ts_ns = max(nq, es)`` — tradable, no look-ahead.
    Signed ``es_lag_ms`` = es_ts − nq_ts (negative ⇒ ES first).
    """
    if stamp not in ("nq", "confirm"):
        raise ValueError(f"unknown dual stamp: {stamp}")
    if nq_events.num_rows == 0 or es_events.num_rows == 0:
        return pa.table({})

    nq_ts = nq_events.column("event_ts_ns").to_numpy().astype(np.int64)
    nq_side = nq_events.column("side").to_numpy()
    es_ts = es_events.column("event_ts_ns").to_numpy().astype(np.int64)
    es_side = es_events.column("side").to_numpy()
    order = np.argsort(es_ts)
    es_ts, es_side = es_ts[order], es_side[order]
    window = int(sync_ms * 1_000_000)

    keep = []
    lags = []
    confirm_ts = []
    for i in range(len(nq_ts)):
        lo = np.searchsorted(es_ts, nq_ts[i] - window, side="left")
        hi = np.searchsorted(es_ts, nq_ts[i] + window, side="right")
        best = None
        best_es = None
        for j in range(lo, hi):
            if int(es_side[j]) != int(nq_side[i]):
                continue
            lag = int(es_ts[j] - nq_ts[i])  # signed: negative = ES first
            if best is None or abs(lag) < abs(best):
                best = lag
                best_es = int(es_ts[j])
        if best is not None and best_es is not None:
            keep.append(i)
            lags.append(best / 1e6)
            confirm_ts.append(max(int(nq_ts[i]), best_es) if stamp == "confirm" else int(nq_ts[i]))

    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    out = nq_events.take(idx)
    for name in ("es_lag_ms", "event_type"):
        if name in out.column_names:
            out = out.drop(name)
    if stamp == "confirm":
        out = out.set_column(
            out.schema.get_field_index("event_ts_ns"),
            "event_ts_ns",
            pa.array(confirm_ts, type=pa.int64()),
        )
    out = out.append_column("es_lag_ms", pa.array(lags, type=pa.float64()))
    out = out.append_column(
        "event_type", pa.array(["dual_aggressor_sweep"] * len(idx), type=pa.string())
    )
    return out
