"""Causal resting-order MBO lifecycle (H80/H81 programme).

Reuses ``_resolve_mbo`` for cancel/replace resolution. Passive fills are
inferred: FIFO queue-ahead at a price vs depth-drop−traded accounting.
Disagreement sets ``fill_uncertain=1``. Seeded orders (present before the
data window) are flagged and excluded from primary arms.

Terminal states (evaluated at contact+2s, refreshed at +5s):
  defended  — executed ≥ 50% of initial, pen ≤ 2 ticks, reload or stack ≥ 50%
  completed — executed, no reload, stack < 25% of initial
  consumed  — pierce ≥ 4 ticks ∧ consume_ratio ≥ 1.0, no reload within 2 s
  pulled    — ≥ 70% cancelled before contact as price approached
  other     — contacted but none of the above
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from research.irt_lvn.of_scores import _resolve_mbo

NS = 1_000_000_000
PTS_PER_TICK = 0.25


@dataclass
class LifecycleConfig:
    watch_radius_ticks: int = 8
    min_executed: float = 10.0
    defended_exec_frac: float = 0.50
    defended_max_pen_ticks: int = 2
    defended_remain_frac: float = 0.50
    completed_remain_frac: float = 0.25
    consumed_pierce_ticks: int = 4
    consumed_consume_ratio: float = 1.0
    consumed_no_reload_sec: float = 2.0
    pulled_cancel_frac: float = 0.70
    approach_lookback_sec: float = 10.0
    large_q: float = 0.99
    large_lookback_min: int = 30
    large_warmup_min: int = 5
    large_min_prints: int = 50
    # Absolute floor: ignore sends below this when building the order map
    # (keeps ~10M-row MBO sessions tractable; causal p99 still applied later).
    track_min_size: float = 20.0
    dominance_min: float = 0.50
    fragmented_max: float = 0.25
    post_sec: tuple[float, ...] = (2.0, 5.0)
    rth_uk_only: bool = True


@dataclass
class _Order:
    oid: str
    is_bid: bool
    price: int
    size: float
    initial_size: float
    first_seen_ns: int
    seeded: bool = False
    queue_ahead: float = 0.0
    executed: float = 0.0
    cancelled: float = 0.0
    replace_count: int = 0
    contact_ns: int = -1
    max_penetration: int = 0
    replenish_count: int = 0
    last_reload_ns: int = -1
    cancelled_before_contact: float = 0.0
    size_at_contact: float = 0.0
    dominance_at_contact: float = float("nan")
    level_size_at_contact: float = float("nan")
    closed: bool = False


@dataclass
class LifecycleEvent:
    event_ts_ns: int
    side: int  # +1 long fade away from BID / continuation through ASK etc. — stamp side
    level_side: str  # BID | ASK
    price_ticks: int
    order_id: str
    initial_size: float
    executed: float
    cancelled: float
    consume_ratio: float
    max_penetration_ticks: int
    replenish_count: int
    dominance: float
    level_size: float
    queue_ahead: float
    age_sec: float
    seeded: int
    fill_uncertain: int
    state: str
    cancelled_before_contact_frac: float
    remain_frac: float
    replace_count: int
    post_sec: float


def _rth_mask_uk(ts_ns: np.ndarray) -> np.ndarray:
    sec = ((ts_ns // NS) + 3600) % 86400
    start = 14 * 3600 + 30 * 60
    end = 17 * 3600
    return (sec >= start) & (sec <= end)


def resolve_mbo_events(
    mbo: pa.Table | None, lo_ext: int, hi_ext: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    """Public wrapper around ``_resolve_mbo`` (add=+1, cancel=−1)."""
    return _resolve_mbo(mbo, lo_ext, hi_ext)


def _causal_size_thresholds(
    sizes: np.ndarray, ts: np.ndarray, cfg: LifecycleConfig
) -> dict[int, float]:
    """Per-minute causal p99 of resting order sizes from completed minutes."""
    if len(ts) == 0:
        return {}
    mins = (ts // 60_000_000_000).astype(np.int64)
    uniq = np.unique(mins)
    by_min: dict[int, list[float]] = {int(m): [] for m in uniq}
    for m, s in zip(mins, sizes):
        if s > 0:
            by_min[int(m)].append(float(s))
    out: dict[int, float] = {}
    sorted_mins = sorted(by_min)
    for m in sorted_mins:
        sel = [
            by_min[x]
            for x in sorted_mins
            if (m - cfg.large_lookback_min) <= x < m and by_min[x]
        ]
        if len(sel) < cfg.large_warmup_min:
            continue
        vals = np.concatenate([np.asarray(v, dtype=np.float64) for v in sel])
        if len(vals) < cfg.large_min_prints:
            continue
        out[int(m)] = float(np.quantile(vals, cfg.large_q))
    return out


def classify_state(
    *,
    executed: float,
    initial_size: float,
    max_penetration: int,
    replenish_count: int,
    remain_frac: float,
    cancelled_before_frac: float,
    contacted: bool,
    reload_within_2s: bool,
    cfg: LifecycleConfig,
) -> str:
    """Classify terminal lifecycle state from contact features."""
    if not contacted:
        if cancelled_before_frac >= cfg.pulled_cancel_frac:
            return "pulled"
        return "other"
    if cancelled_before_frac >= cfg.pulled_cancel_frac and executed < cfg.min_executed:
        return "pulled"
    cr = executed / initial_size if initial_size > 0 else (float("inf") if executed > 0 else 0.0)
    # consumed takes priority when price sliced through
    if (
        max_penetration >= cfg.consumed_pierce_ticks
        and cr >= cfg.consumed_consume_ratio
        and not reload_within_2s
    ):
        return "consumed"
    if (
        executed >= cfg.defended_exec_frac * max(initial_size, 1.0)
        and max_penetration <= cfg.defended_max_pen_ticks
        and (replenish_count >= 1 or remain_frac >= cfg.defended_remain_frac)
    ):
        return "defended"
    if (
        executed >= cfg.min_executed
        and replenish_count == 0
        and remain_frac < cfg.completed_remain_frac
        and max_penetration <= cfg.defended_max_pen_ticks
    ):
        return "completed"
    return "other"


class _LifecycleTracker:
    """Replay MBO + trades; emit contact events with terminal states."""

    def __init__(self, cfg: LifecycleConfig, data_start_ns: int) -> None:
        self.cfg = cfg
        self.data_start_ns = data_start_ns
        self.orders: dict[str, _Order] = {}
        # FIFO at (is_bid, price): list of oid
        self.queue: dict[tuple[bool, int], list[str]] = {}
        self.depth: dict[tuple[bool, int], float] = {}
        self.best_bid = -10**18
        self.best_ask = 10**18
        self.last_trade_px: int | None = None
        self.now_ns = data_start_ns
        self.events: list[LifecycleEvent] = []
        self._pending_close: list[tuple[str, int]] = []  # (oid, decide_ns)
        self.send_sizes: list[tuple[int, float]] = []  # for causal large thr
        self.n_fill_uncertain = 0

    def _key(self, is_bid: bool, px: int) -> tuple[bool, int]:
        return (is_bid, px)

    def _level_size(self, is_bid: bool, px: int) -> float:
        return float(self.depth.get(self._key(is_bid, px), 0.0))

    def _dominance(self, oid: str) -> float:
        o = self.orders.get(oid)
        if o is None:
            return float("nan")
        tot = self._level_size(o.is_bid, o.price)
        if tot <= 0:
            return float("nan")
        return float(o.size / tot)

    def _refresh_bbo(self) -> None:
        bids = [px for (b, px), sz in self.depth.items() if b and sz > 0]
        asks = [px for (b, px), sz in self.depth.items() if (not b) and sz > 0]
        self.best_bid = max(bids) if bids else -10**18
        self.best_ask = min(asks) if asks else 10**18

    def _recompute_depth(self, key: tuple[bool, int]) -> None:
        tot = sum(
            self.orders[x].size
            for x in self.queue.get(key, [])
            if x in self.orders and not self.orders[x].closed
        )
        self.depth[key] = float(tot)

    def on_mbo_add(
        self, ts: int, oid: str, is_bid: bool, px: int, sz: float, *, seeded: bool = False
    ) -> None:
        self.now_ns = ts
        key = self._key(is_bid, px)
        ahead = sum(
            self.orders[x].size
            for x in self.queue.get(key, [])
            if x in self.orders and x != oid and not self.orders[x].closed
        )
        if oid in self.orders and not self.orders[oid].closed:
            o = self.orders[oid]
            old_key = self._key(o.is_bid, o.price)
            if sz > o.size and o.contact_ns > 0:
                o.replenish_count += 1
                o.last_reload_ns = ts
            if o.price != px or o.is_bid != is_bid:
                self._remove_from_queue(o)
                o.price = px
                o.is_bid = is_bid
                self.queue.setdefault(key, []).append(oid)
                o.replace_count += 1
                self._recompute_depth(old_key)
            o.size = sz
            self._recompute_depth(key)
        else:
            o = _Order(
                oid=oid, is_bid=is_bid, price=px, size=sz, initial_size=sz,
                first_seen_ns=ts, seeded=seeded, queue_ahead=ahead,
            )
            self.orders[oid] = o
            self.queue.setdefault(key, []).append(oid)
            self.send_sizes.append((ts, sz))
            self._recompute_depth(key)
        self._refresh_bbo()

    def _remove_from_queue(self, o: _Order) -> None:
        key = self._key(o.is_bid, o.price)
        q = self.queue.get(key, [])
        if o.oid in q:
            q.remove(o.oid)

    def on_mbo_cancel(self, ts: int, oid: str, px: int, sz: float, is_bid: bool) -> None:
        self.now_ns = ts
        o = self.orders.get(oid)
        if o is None or o.closed:
            key = self._key(is_bid, px)
            self.depth[key] = max(0.0, self.depth.get(key, 0.0) - sz)
            self._refresh_bbo()
            return
        take = min(o.size, sz) if sz > 0 else o.size
        o.size = max(0.0, o.size - take)
        o.cancelled += take
        if o.contact_ns < 0:
            o.cancelled_before_contact += take
        key = self._key(o.is_bid, o.price)
        if o.size <= 1e-9:
            o.size = 0.0
            self._remove_from_queue(o)
            if o.contact_ns < 0:
                self._maybe_emit_pull(o, ts)
                o.closed = True
            elif o.oid not in [p[0] for p in self._pending_close]:
                self._pending_close.append(
                    (o.oid, o.contact_ns + int(self.cfg.post_sec[0] * NS))
                )
        self._recompute_depth(key)
        self._refresh_bbo()

    def _near_price(self, is_bid: bool, px: int) -> bool:
        if self.best_bid <= -10**17 or self.best_ask >= 10**17:
            return self.last_trade_px is not None and abs(px - self.last_trade_px) <= self.cfg.watch_radius_ticks
        ref = self.best_bid if is_bid else self.best_ask
        return abs(px - ref) <= self.cfg.watch_radius_ticks

    def _maybe_emit_pull(self, o: _Order, ts: int) -> None:
        if o.seeded:
            return
        if not self._near_price(o.is_bid, o.price):
            return
        frac = o.cancelled_before_contact / max(o.initial_size, 1.0)
        if frac < self.cfg.pulled_cancel_frac:
            return
        self.events.append(self._make_event(o, ts, "pulled", post_sec=0.0, remain=0.0))

    def on_trade(self, ts: int, px: int, sz: float, buy_aggr: bool) -> None:
        self.now_ns = ts
        self.last_trade_px = px
        hit_bid = not buy_aggr  # sell aggressor hits bids
        key = self._key(hit_bid, px)
        q = list(self.queue.get(key, []))
        remaining = float(sz)
        depth_before = self.depth.get(key, 0.0)
        filled_total = 0.0
        for oid in q:
            if remaining <= 1e-9:
                break
            o = self.orders.get(oid)
            if o is None or o.closed or o.size <= 0:
                continue
            take = min(o.size, remaining)
            if take <= 0:
                continue
            if o.contact_ns < 0:
                o.contact_ns = ts
                o.size_at_contact = o.size
                o.dominance_at_contact = self._dominance(oid)
                o.level_size_at_contact = self._level_size(o.is_bid, o.price)
                # schedule post-contact close
                self._pending_close.append(
                    (oid, ts + int(self.cfg.post_sec[0] * NS))
                )
            o.executed += take
            o.size -= take
            remaining -= take
            filled_total += take
            # penetration: how far past best opposite
            if buy_aggr:
                o.max_penetration = max(o.max_penetration, max(0, px - (self.best_ask if self.best_ask < 10**17 else px)))
            else:
                o.max_penetration = max(o.max_penetration, max(0, (self.best_bid if self.best_bid > -10**17 else px) - px))
            if o.size <= 1e-9:
                o.size = 0.0
                self._remove_from_queue(o)
        self._recompute_depth(key)
        if remaining > 1e-6 and filled_total > 0:
            self.n_fill_uncertain += 1
            for oid in q:
                o = self.orders.get(oid)
                if o and o.contact_ns == ts:
                    setattr(o, "_fill_uncertain", True)
        for oid in q:
            o = self.orders.get(oid)
            if o and o.contact_ns > 0 and not o.closed:
                if buy_aggr and not o.is_bid:
                    o.max_penetration = max(o.max_penetration, max(0, px - o.price))
                elif (not buy_aggr) and o.is_bid:
                    o.max_penetration = max(o.max_penetration, max(0, o.price - px))
        # unused depth_before kept for future fill-uncertainty cross-check
        _ = depth_before
        self._flush_pending(ts)
        self._refresh_bbo()

    def _flush_pending(self, ts: int) -> None:
        keep: list[tuple[str, int]] = []
        for oid, decide_ns in self._pending_close:
            if ts < decide_ns:
                keep.append((oid, decide_ns))
                continue
            o = self.orders.get(oid)
            if o is None or o.closed:
                continue
            self._emit_contact(o, decide_ns)
            o.closed = True
        self._pending_close = keep

    def _remain_frac(self, o: _Order) -> float:
        lvl = self._level_size(o.is_bid, o.price)
        return float(lvl / o.initial_size) if o.initial_size > 0 else 0.0

    def _reload_within(self, o: _Order, sec: float) -> bool:
        if o.last_reload_ns < 0 or o.contact_ns < 0:
            return o.replenish_count >= 1 and o.last_reload_ns >= o.contact_ns
        return (o.last_reload_ns - o.contact_ns) <= int(sec * NS) and o.last_reload_ns >= o.contact_ns

    def _emit_contact(self, o: _Order, decide_ns: int) -> None:
        if o.seeded:
            return
        if o.executed < self.cfg.min_executed and o.cancelled_before_contact / max(o.initial_size, 1) < self.cfg.pulled_cancel_frac:
            # too small — skip unless pulled
            return
        remain = self._remain_frac(o)
        reload = self._reload_within(o, self.cfg.consumed_no_reload_sec)
        cbf = o.cancelled_before_contact / max(o.initial_size, 1.0)
        state = classify_state(
            executed=o.executed,
            initial_size=o.initial_size,
            max_penetration=o.max_penetration,
            replenish_count=o.replenish_count,
            remain_frac=remain,
            cancelled_before_frac=cbf,
            contacted=o.contact_ns > 0,
            reload_within_2s=reload,
            cfg=self.cfg,
        )
        post = (decide_ns - o.contact_ns) / NS if o.contact_ns > 0 else 0.0
        self.events.append(self._make_event(o, decide_ns, state, post_sec=post, remain=remain))

    def _make_event(
        self, o: _Order, ts: int, state: str, *, post_sec: float, remain: float
    ) -> LifecycleEvent:
        cr = o.executed / o.initial_size if o.initial_size > 0 else 0.0
        # side convention: for fade-away BID→long(+1), ASK→short(−1);
        # for continuation through ASK→long(+1), BID→short(−1). Stored as level_side;
        # consumers pick orientation. Default stamp side = fade away from level.
        side = 1 if o.is_bid else -1
        uncertain = 1 if getattr(o, "_fill_uncertain", False) else 0
        age = (o.contact_ns - o.first_seen_ns) / NS if o.contact_ns > 0 else (ts - o.first_seen_ns) / NS
        return LifecycleEvent(
            event_ts_ns=int(o.contact_ns if o.contact_ns > 0 else ts),
            side=side,
            level_side="BID" if o.is_bid else "ASK",
            price_ticks=int(o.price),
            order_id=o.oid,
            initial_size=float(o.initial_size),
            executed=float(o.executed),
            cancelled=float(o.cancelled),
            consume_ratio=float(cr),
            max_penetration_ticks=int(o.max_penetration),
            replenish_count=int(o.replenish_count),
            dominance=float(o.dominance_at_contact) if np.isfinite(o.dominance_at_contact) else float("nan"),
            level_size=float(o.level_size_at_contact) if np.isfinite(o.level_size_at_contact) else float("nan"),
            queue_ahead=float(o.queue_ahead),
            age_sec=float(age),
            seeded=1 if o.seeded else 0,
            fill_uncertain=uncertain,
            state=state,
            cancelled_before_contact_frac=float(o.cancelled_before_contact / max(o.initial_size, 1.0)),
            remain_frac=float(remain),
            replace_count=int(o.replace_count),
            post_sec=float(post_sec),
        )

    def finalize(self, end_ns: int) -> None:
        self._flush_pending(end_ns + 1)
        # close any still-open contacted orders
        for o in list(self.orders.values()):
            if o.closed or o.contact_ns < 0:
                continue
            self._emit_contact(o, end_ns)
            o.closed = True


def _events_to_table(events: list[LifecycleEvent]) -> pa.Table:
    if not events:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.array([e.event_ts_ns for e in events], dtype=np.int64),
        "side": np.array([e.side for e in events], dtype=np.int8),
        "level_side": [e.level_side for e in events],
        "price_ticks": np.array([e.price_ticks for e in events], dtype=np.int64),
        "order_id": [e.order_id for e in events],
        "initial_size": np.array([e.initial_size for e in events], dtype=np.float64),
        "executed": np.array([e.executed for e in events], dtype=np.float64),
        "cancelled": np.array([e.cancelled for e in events], dtype=np.float64),
        "consume_ratio": np.array([e.consume_ratio for e in events], dtype=np.float64),
        "max_penetration_ticks": np.array([e.max_penetration_ticks for e in events], dtype=np.int64),
        "replenish_count": np.array([e.replenish_count for e in events], dtype=np.int64),
        "dominance": np.array([e.dominance for e in events], dtype=np.float64),
        "level_size": np.array([e.level_size for e in events], dtype=np.float64),
        "queue_ahead": np.array([e.queue_ahead for e in events], dtype=np.float64),
        "age_sec": np.array([e.age_sec for e in events], dtype=np.float64),
        "seeded": np.array([e.seeded for e in events], dtype=np.int8),
        "fill_uncertain": np.array([e.fill_uncertain for e in events], dtype=np.int8),
        "state": [e.state for e in events],
        "cancelled_before_contact_frac": np.array(
            [e.cancelled_before_contact_frac for e in events], dtype=np.float64
        ),
        "remain_frac": np.array([e.remain_frac for e in events], dtype=np.float64),
        "replace_count": np.array([e.replace_count for e in events], dtype=np.int64),
        "post_sec": np.array([e.post_sec for e in events], dtype=np.float64),
        "event_type": ["mbo_lifecycle"] * len(events),
    })


def run_lifecycle(
    mbo: pa.Table,
    trades: pa.Table,
    *,
    cfg: LifecycleConfig | None = None,
    data_start_ns: int | None = None,
) -> pa.Table:
    """Replay MBO+trades; return lifecycle contact events.

    Uses raw MBO rows (order_id preserved) rather than flattened resolve stream,
    so per-order identity is tracked. Cancels with price=0 are resolved against
    the live order map (same idea as ``_resolve_mbo``).
    """
    cfg = cfg or LifecycleConfig()
    if mbo is None or mbo.num_rows == 0:
        return pa.table({})

    m_ts = mbo.column("ts_ns").to_numpy().astype(np.int64)
    m_op = mbo.column("op").to_numpy(zero_copy_only=False)
    m_oid = mbo.column("order_id").to_numpy(zero_copy_only=False)
    m_px = mbo.column("price_ticks").to_numpy().astype(np.int64)
    m_sz = mbo.column("size").to_numpy().astype(np.float64)
    m_bid = mbo.column("is_bid").fill_null(False).to_numpy().astype(bool)

    if trades.num_rows:
        t_ts = trades.column("ts_ns").to_numpy().astype(np.int64)
        t_px = np.round(trades.column("price").to_numpy().astype(np.float64)).astype(np.int64)
        t_sz = trades.column("size").to_numpy().astype(np.float64)
        t_buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    else:
        t_ts = np.zeros(0, dtype=np.int64)
        t_px = t_sz = np.zeros(0, dtype=np.float64)
        t_buy = np.zeros(0, dtype=bool)

    if cfg.rth_uk_only:
        rm = _rth_mask_uk(m_ts)
        m_ts, m_op, m_oid, m_px, m_sz, m_bid = m_ts[rm], m_op[rm], m_oid[rm], m_px[rm], m_sz[rm], m_bid[rm]
        if len(t_ts):
            trm = _rth_mask_uk(t_ts)
            t_ts, t_px, t_sz, t_buy = t_ts[trm], t_px[trm], t_sz[trm], t_buy[trm]

    if len(m_ts) == 0:
        return pa.table({})

    order_m = np.argsort(m_ts, kind="stable")
    m_ts, m_op, m_oid, m_px, m_sz, m_bid = (
        m_ts[order_m], m_op[order_m], m_oid[order_m], m_px[order_m], m_sz[order_m], m_bid[order_m]
    )
    if len(t_ts):
        to = np.argsort(t_ts, kind="stable")
        t_ts, t_px, t_sz, t_buy = t_ts[to], t_px[to], t_sz[to], t_buy[to]

    start = int(data_start_ns) if data_start_ns is not None else int(m_ts[0])
    tr = _LifecycleTracker(cfg, start)

    # live order map for cancel resolution
    live: dict[str, tuple[bool, int, float]] = {}  # oid → (is_bid, px, sz)

    i = j = 0
    n_m, n_t = len(m_ts), len(t_ts)
    while i < n_m or j < n_t:
        take_mbo = j >= n_t or (i < n_m and m_ts[i] <= t_ts[j])
        if take_mbo:
            ts = int(m_ts[i])
            op = str(m_op[i]).lower()
            oid = str(m_oid[i] or "")
            px, sz, is_bid = int(m_px[i]), float(m_sz[i]), bool(m_bid[i])
            if not oid:
                i += 1
                continue
            if op == "send":
                if sz < cfg.track_min_size:
                    i += 1
                    continue
                seeded = ts <= start
                tr.on_mbo_add(ts, oid, is_bid, px, sz, seeded=seeded)
                live[oid] = (is_bid, px, sz)
            elif op == "replace":
                prev = live.get(oid)
                if prev is not None:
                    tr.on_mbo_cancel(ts, oid, prev[1], prev[2], prev[0])
                    live.pop(oid, None)
                if sz >= cfg.track_min_size:
                    tr.on_mbo_add(ts, oid, is_bid, px, sz, seeded=False)
                    live[oid] = (is_bid, px, sz)
            elif op == "cancel":
                prev = live.get(oid)
                if prev is not None:
                    tr.on_mbo_cancel(ts, oid, prev[1], prev[2], prev[0])
                    del live[oid]
                elif px > 0 and sz >= cfg.track_min_size:
                    tr.on_mbo_cancel(ts, oid, px, sz, is_bid)
            i += 1
        else:
            tr.on_trade(int(t_ts[j]), int(t_px[j]), float(t_sz[j]), bool(t_buy[j]))
            j += 1

    end = int(max(m_ts[-1] if len(m_ts) else start, t_ts[-1] if len(t_ts) else start))
    tr.finalize(end)
    return _events_to_table(tr.events)


def annotate_large_dominant(
    events: pa.Table,
    *,
    cfg: LifecycleConfig | None = None,
    thr_by_min: dict[int, float] | None = None,
) -> pa.Table:
    """Flag dominant + causally-large orders. Requires ``initial_size`` column."""
    cfg = cfg or LifecycleConfig()
    if events.num_rows == 0:
        return events
    ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sz = events.column("initial_size").to_numpy().astype(np.float64)
    dom = events.column("dominance").to_numpy().astype(np.float64)
    seeded = events.column("seeded").to_numpy().astype(np.int8)
    mins = (ts // 60_000_000_000).astype(np.int64)
    thr = thr_by_min or {}
    large = np.zeros(len(ts), dtype=np.int8)
    dominant = np.zeros(len(ts), dtype=np.int8)
    fragmented = np.zeros(len(ts), dtype=np.int8)
    primary = np.zeros(len(ts), dtype=np.int8)
    for i in range(len(ts)):
        t = thr.get(int(mins[i]), float("nan"))
        is_large = np.isfinite(t) and sz[i] >= t
        is_dom = np.isfinite(dom[i]) and dom[i] >= cfg.dominance_min
        is_frag = np.isfinite(dom[i]) and dom[i] < cfg.fragmented_max
        large[i] = 1 if is_large else 0
        dominant[i] = 1 if is_dom else 0
        fragmented[i] = 1 if is_frag else 0
        primary[i] = 1 if (is_large and is_dom and seeded[i] == 0) else 0
    out = events
    for name, arr in (
        ("lc_large", large), ("lc_dominant", dominant),
        ("lc_fragmented", fragmented), ("lc_primary", primary),
    ):
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.int8()))
    return out


def build_size_thresholds_from_events(
    events: pa.Table, cfg: LifecycleConfig | None = None
) -> dict[int, float]:
    cfg = cfg or LifecycleConfig()
    if events.num_rows == 0:
        return {}
    ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sz = events.column("initial_size").to_numpy().astype(np.float64)
    return _causal_size_thresholds(sz, ts, cfg)


def state_counts(events: pa.Table) -> dict[str, int]:
    if events.num_rows == 0:
        return {"defended": 0, "completed": 0, "consumed": 0, "pulled": 0, "other": 0, "n": 0}
    states = events.column("state").to_pylist()
    out = {"defended": 0, "completed": 0, "consumed": 0, "pulled": 0, "other": 0, "n": len(states)}
    for s in states:
        out[s] = out.get(s, 0) + 1
    return out


def iceberg_proxy_at_price(
    events: pa.Table,
    *,
    price_ticks: int,
    level_side: str,
    stamp_ns: int,
    window_sec: float = 30.0,
    min_reloads: int = 2,
    exec_vs_display: float = 2.0,
    max_pen: int = 2,
) -> dict[str, float]:
    """Price-level iceberg proxy near stamp (feature-on-trigger, not entry)."""
    empty = {"iceberg": 0.0, "ice_executed": 0.0, "ice_reloads": 0.0, "ice_peak_display": 0.0}
    if events.num_rows == 0:
        return empty
    ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    px = events.column("price_ticks").to_numpy().astype(np.int64)
    side = events.column("level_side").to_pylist()
    exe = events.column("executed").to_numpy().astype(np.float64)
    init = events.column("initial_size").to_numpy().astype(np.float64)
    rep = events.column("replenish_count").to_numpy().astype(np.int64)
    pen = events.column("max_penetration_ticks").to_numpy().astype(np.int64)
    lo = stamp_ns - int(window_sec * NS)
    m = (
        (ts >= lo) & (ts <= stamp_ns)
        & (px == price_ticks)
        & np.array([s == level_side for s in side])
    )
    if not m.any():
        return empty
    tot_exe = float(exe[m].sum())
    peak = float(init[m].max())
    reloads = int(rep[m].sum())
    max_p = int(pen[m].max()) if m.any() else 0
    hit = (
        peak > 0
        and tot_exe >= exec_vs_display * peak
        and reloads >= min_reloads
        and max_p <= max_pen
    )
    return {
        "iceberg": 1.0 if hit else 0.0,
        "ice_executed": tot_exe,
        "ice_reloads": float(reloads),
        "ice_peak_display": peak,
    }


def paired_lifecycle(
    nq_events: pa.Table,
    es_events: pa.Table,
    *,
    sync_ms: int = 2000,
    same_state: bool = False,
) -> pa.Table:
    """Annotate NQ lifecycle events with same-side ES event within ±sync_ms."""
    if nq_events.num_rows == 0:
        return nq_events
    nq_ts = nq_events.column("event_ts_ns").to_numpy().astype(np.int64)
    nq_side = nq_events.column("level_side").to_pylist()
    nq_state = nq_events.column("state").to_pylist()
    paired = np.zeros(len(nq_ts), dtype=np.int8)
    lag_ms = np.full(len(nq_ts), np.nan)
    if es_events.num_rows == 0:
        out = nq_events
        for name, arr, typ in (
            ("lc_paired", paired, pa.int8()),
            ("lc_es_lag_ms", lag_ms, pa.float64()),
        ):
            if name in out.column_names:
                out = out.drop(name)
            out = out.append_column(name, pa.array(arr, type=typ))
        return out
    es_ts = es_events.column("event_ts_ns").to_numpy().astype(np.int64)
    es_side = es_events.column("level_side").to_pylist()
    es_state = es_events.column("state").to_pylist()
    order = np.argsort(es_ts)
    es_ts = es_ts[order]
    es_side = [es_side[i] for i in order]
    es_state = [es_state[i] for i in order]
    window = int(sync_ms * 1_000_000)
    for i in range(len(nq_ts)):
        lo = int(np.searchsorted(es_ts, nq_ts[i] - window, side="left"))
        hi = int(np.searchsorted(es_ts, nq_ts[i] + window, side="right"))
        best = None
        for j in range(lo, hi):
            if es_side[j] != nq_side[i]:
                continue
            if same_state and es_state[j] != nq_state[i]:
                continue
            lag = int(es_ts[j] - nq_ts[i])
            if best is None or abs(lag) < abs(best):
                best = lag
        if best is not None:
            paired[i] = 1
            lag_ms[i] = best / 1e6
    out = nq_events
    for name, arr, typ in (
        ("lc_paired", paired, pa.int8()),
        ("lc_es_lag_ms", lag_ms, pa.float64()),
    ):
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=typ))
    return out
