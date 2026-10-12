"""Liquidity reaction features — Python port of LargeLevelTracker (Java)."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class LargeLevelConfig:
    absolute_size: int = 100
    z_score: float = 2.5
    tick_radius: int = 8
    dwell_alert_sec: float = 3.0
    stats_window: int = 200


@dataclass
class TrackedLevel:
    side: str  # BID | ASK
    price: int
    first_seen_ns: int
    initial_size: int
    peak_size: int
    current_size: int
    last_update_ns: int
    near_price_since_ns: int = -1
    contacted: bool = False
    contact_ns: int = -1
    executed_into: int = 0
    replenish_count: int = 0
    max_penetration_ticks: int = 0
    mid_at_contact: int | None = None
    post_move_250ms: float | None = None
    post_move_1s: float | None = None
    post_move_2s: float | None = None
    label: str = "NONE"
    closed: bool = False
    absorb_ns: int = -1
    attracted_before_contact: bool = False

    def consume_ratio(self) -> float:
        if self.initial_size <= 0:
            return float("inf") if self.executed_into > 0 else 0.0
        return self.executed_into / self.initial_size

    def dwell_seconds(self, now_ns: int) -> float:
        if self.near_price_since_ns < 0:
            return 0.0
        return (now_ns - self.near_price_since_ns) / 1e9

    def age_seconds(self, now_ns: int) -> float:
        return (now_ns - self.first_seen_ns) / 1e9


class _RollingStats:
    def __init__(self, window: int) -> None:
        self.window = window
        self._buf: deque[float] = deque()
        self._sum = 0.0
        self._sumsq = 0.0

    def add(self, x: float) -> None:
        self._buf.append(x)
        self._sum += x
        self._sumsq += x * x
        if len(self._buf) > self.window:
            y = self._buf.popleft()
            self._sum -= y
            self._sumsq -= y * y

    def __len__(self) -> int:
        return len(self._buf)

    def z_score(self, x: float) -> float:
        n = len(self._buf)
        if n < 2:
            return 0.0
        mean = self._sum / n
        var = max(0.0, self._sumsq / n - mean * mean)
        sd = var ** 0.5
        return 0.0 if sd < 1e-12 else (x - mean) / sd


class LargeLevelTracker:
    """Faithful port of bookmap-addon LargeLevelTracker."""

    def __init__(self, cfg: LargeLevelConfig | None = None) -> None:
        self.cfg = cfg or LargeLevelConfig()
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self.active: dict[str, TrackedLevel] = {}
        self.recently_closed: list[TrackedLevel] = []
        self.depth_stats = _RollingStats(self.cfg.stats_window)
        self.best_bid = -10**18
        self.best_ask = 10**18
        self.last_trade_price: int | None = None
        self.now_ns = 0

    def on_timestamp(self, ns: int) -> None:
        self.now_ns = ns
        self._update_post_contact_moves()

    def on_depth(self, is_bid: bool, price: int, size: int) -> None:
        book = self.bids if is_bid else self.asks
        if size <= 0:
            if price in book:
                del book[price]
                if is_bid and price == self.best_bid:
                    self.best_bid = max(book) if book else -10**18
                elif (not is_bid) and price == self.best_ask:
                    self.best_ask = min(book) if book else 10**18
        else:
            book[price] = size
            self.depth_stats.add(float(size))
            if is_bid:
                if price > self.best_bid:
                    self.best_bid = price
            else:
                if price < self.best_ask:
                    self.best_ask = price

        side = "BID" if is_bid else "ASK"
        key = f"{side}:{price}"
        existing = self.active.get(key)
        if existing is not None and not existing.closed:
            if size <= 0:
                existing.current_size = 0
                if not existing.contacted:
                    existing.label = "WITHDRAW"
                self._close_level(existing)
            else:
                if size > existing.current_size and existing.contacted:
                    existing.replenish_count += 1
                existing.current_size = size
                existing.peak_size = max(existing.peak_size, size)
                existing.last_update_ns = self.now_ns
        elif size > 0 and self._is_large(size) and self._is_near_price(side, price):
            level = TrackedLevel(
                side=side,
                price=price,
                first_seen_ns=self.now_ns,
                initial_size=size,
                peak_size=size,
                current_size=size,
                last_update_ns=self.now_ns,
            )
            self._classify_approach(level)
            self.active[key] = level
        self._update_dwell_and_approach()

    def on_trade(self, price_ticks: int, size: int, buy_aggressor: bool) -> None:
        self.last_trade_price = price_ticks
        hit_side = "ASK" if buy_aggressor else "BID"
        radius = self.cfg.tick_radius
        for level in list(self.active.values()):
            if level.closed or level.side != hit_side:
                continue
            distance = abs(price_ticks - level.price)
            at_or_through = (
                price_ticks >= level.price if buy_aggressor else price_ticks <= level.price
            )
            if not at_or_through and distance > radius:
                continue
            if not level.contacted:
                level.contacted = True
                level.contact_ns = self.now_ns
                level.mid_at_contact = self._mid_price()
            level.executed_into += int(size)
            if buy_aggressor:
                level.max_penetration_ticks = max(
                    level.max_penetration_ticks, max(0, price_ticks - level.price)
                )
            else:
                level.max_penetration_ticks = max(
                    level.max_penetration_ticks, max(0, level.price - price_ticks)
                )
            self._maybe_mark_absorb(level)
        self._update_dwell_and_approach()

    def drain_closed(self) -> list[TrackedLevel]:
        out = list(self.recently_closed)
        self.recently_closed.clear()
        return out

    def _maybe_mark_absorb(self, level: TrackedLevel) -> None:
        if (
            level.executed_into >= max(1, level.initial_size)
            and level.max_penetration_ticks <= max(1, self.cfg.tick_radius // 2)
        ):
            if level.label != "ABSORB":
                level.absorb_ns = self.now_ns
            level.label = "ABSORB"

    def _classify_approach(self, level: TrackedLevel) -> None:
        mid = self._mid_price()
        trade = self.last_trade_price
        if mid is None or trade is None:
            return
        dist_now = abs(trade - level.price)
        dist_mid = abs(mid - level.price)
        if dist_now < dist_mid and dist_now <= self.cfg.tick_radius:
            level.label = "ATTRACT"
            if not level.contacted:
                level.attracted_before_contact = True

    def _update_dwell_and_approach(self) -> None:
        mid = self._mid_price()
        if mid is None:
            return
        radius = self.cfg.tick_radius
        to_drop: list[str] = []
        for key, level in self.active.items():
            if level.closed:
                to_drop.append(key)
                continue
            distance = abs(mid - level.price)
            if distance <= radius:
                if level.near_price_since_ns < 0:
                    level.near_price_since_ns = self.now_ns
                if not level.contacted and level.dwell_seconds(self.now_ns) >= self.cfg.dwell_alert_sec:
                    level.label = "DWELL_SUPPORT"
                if not level.contacted and level.label == "NONE":
                    self._classify_approach(level)
            elif not level.contacted and distance > radius * 2:
                if level.near_price_since_ns >= 0 and level.label != "WITHDRAW":
                    level.label = "REPEL"
                    self._close_level(level)
            if not level.contacted and distance > radius * 3 and level.age_seconds(self.now_ns) > 30:
                to_drop.append(key)
        for key in to_drop:
            self.active.pop(key, None)

    def _update_post_contact_moves(self) -> None:
        mid = self._mid_price()
        if mid is None:
            return
        for level in list(self.active.values()):
            if level.closed or not level.contacted or level.mid_at_contact is None:
                continue
            elapsed = (self.now_ns - level.contact_ns) / 1e9
            move = float(mid - level.mid_at_contact)
            if level.post_move_250ms is None and elapsed >= 0.25:
                level.post_move_250ms = move
            if level.post_move_1s is None and elapsed >= 1.0:
                level.post_move_1s = move
            if level.post_move_2s is None and elapsed >= 2.0:
                level.post_move_2s = move
                self._finalize_after_contact(level)

    def _finalize_after_contact(self, level: TrackedLevel) -> None:
        self._maybe_mark_absorb(level)
        if level.label == "ABSORB" or level.consume_ratio() >= 0.5:
            if level.post_move_1s is not None:
                moved_away = (
                    level.post_move_1s < 0 if level.side == "ASK" else level.post_move_1s > 0
                )
                if moved_away:
                    level.label = "REPEL"
                elif level.label != "ABSORB":
                    level.label = "ABSORB"
                    if level.absorb_ns < 0:
                        level.absorb_ns = self.now_ns
        self._close_level(level)

    def _close_level(self, level: TrackedLevel) -> None:
        if level.closed:
            return
        level.closed = True
        self.recently_closed.append(level)

    def _is_large(self, size: int) -> bool:
        if size >= self.cfg.absolute_size:
            return True
        if self.cfg.z_score >= 50:
            return False  # treat extreme z as disabled (research absolute-only mode)
        return len(self.depth_stats) >= 10 and self.depth_stats.z_score(float(size)) >= self.cfg.z_score

    def _is_near_price(self, side: str, price: int) -> bool:
        if self.best_bid <= -10**17 or self.best_ask >= 10**17:
            return False
        ref = self.best_bid if side == "BID" else self.best_ask
        return abs(price - ref) <= self.cfg.tick_radius

    def _mid_price(self) -> int | None:
        if self.best_bid <= -10**17 or self.best_ask >= 10**17:
            return self.last_trade_price
        return (self.best_bid + self.best_ask) // 2


def _rth_mask_uk(ts_ns: np.ndarray) -> np.ndarray:
    """UK wall clock via UTC+1 (summer BST approximation)."""
    sec = ((ts_ns // 1_000_000_000) + 3600) % 86400
    start = 14 * 3600 + 30 * 60
    end = 17 * 3600
    return (sec >= start) & (sec <= end)


def run_liquidity_tracker(
    depth: pa.Table,
    trades: pa.Table,
    cfg: LargeLevelConfig | None = None,
    rth_uk_only: bool = True,
) -> list[TrackedLevel]:
    """Replay depth+trades chronologically; return all closed tracked levels."""
    cfg = cfg or LargeLevelConfig()
    if depth.num_rows == 0:
        return []

    d_ts = depth.column("ts_ns").to_numpy()
    d_bid = depth.column("is_bid").to_numpy().astype(bool)
    d_px = depth.column("price_ticks").to_numpy().astype(np.int64)
    d_sz = depth.column("size").to_numpy().astype(np.int64)

    if rth_uk_only:
        m = _rth_mask_uk(d_ts)
        d_ts, d_bid, d_px, d_sz = d_ts[m], d_bid[m], d_px[m], d_sz[m]

    t_ts = trades.column("ts_ns").to_numpy() if trades.num_rows else np.array([], dtype=np.int64)
    if trades.num_rows:
        t_px = trades.column("price").to_numpy().astype(np.int64)
        t_sz = trades.column("size").to_numpy().astype(np.int64)
        t_buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
        if rth_uk_only:
            m = _rth_mask_uk(t_ts)
            t_ts, t_px, t_sz, t_buy = t_ts[m], t_px[m], t_sz[m], t_buy[m]
    else:
        t_px = t_sz = t_buy = np.array([])

    # Merge: depth=0, trade=1; at equal ts process depth first
    n_d, n_t = len(d_ts), len(t_ts)
    i = j = 0
    tracker = LargeLevelTracker(cfg)
    closed: list[TrackedLevel] = []

    while i < n_d or j < n_t:
        take_depth = j >= n_t or (i < n_d and d_ts[i] <= t_ts[j])
        if take_depth:
            tracker.on_timestamp(int(d_ts[i]))
            tracker.on_depth(bool(d_bid[i]), int(d_px[i]), int(d_sz[i]))
            i += 1
        else:
            tracker.on_timestamp(int(t_ts[j]))
            tracker.on_trade(int(t_px[j]), int(t_sz[j]), bool(t_buy[j]))
            j += 1
        closed.extend(tracker.drain_closed())

    # Close remaining contacted levels at end of tape
    for level in list(tracker.active.values()):
        if not level.closed and level.contacted:
            tracker._finalize_after_contact(level)
    # Also emit uncontacted near-BBO dwells so challenge-without-trade is observable
    for level in list(tracker.active.values()):
        if level.closed:
            continue
        if level.near_price_since_ns > 0 and not level.contacted:
            if level.label == "NONE":
                level.label = "DWELL_SUPPORT"
            tracker._close_level(level)
    closed.extend(tracker.drain_closed())
    return closed


def absorption_events_from_levels(
    levels: list[TrackedLevel],
    *,
    min_consume_ratio: float = 1.0,
    max_penetration_ticks: int | None = None,
    require_replenish: bool | None = None,
    tick_radius: int = 8,
) -> pa.Table:
    """H2 absorb events from consume/penetration only (no forward-label leak).

    REPEL in the Java tracker can be set using post-contact mid move — that is
    an outcome, not a feature. Event study uses ratio + penetration only.
    """
    if max_penetration_ticks is None:
        max_penetration_ticks = max(1, tick_radius // 2)

    rows: list[TrackedLevel] = []
    for lv in levels:
        if not lv.contacted or lv.contact_ns <= 0:
            continue
        if lv.consume_ratio() < min_consume_ratio:
            continue
        if lv.max_penetration_ticks > max_penetration_ticks:
            continue
        if require_replenish is True and lv.replenish_count < 1:
            continue
        if require_replenish is False and lv.replenish_count >= 1:
            continue
        rows.append(lv)

    if not rows:
        return pa.table({})

    # Fade the level: BID absorb → LONG, ASK absorb → SHORT
    sides = np.array([1 if r.side == "BID" else -1 for r in rows], dtype=np.int8)
    event_ts = np.array(
        [
            r.absorb_ns if r.absorb_ns > 0 else r.contact_ns
            for r in rows
        ],
        dtype=np.int64,
    )
    return pa.table({
        "event_ts_ns": event_ts,
        "side": sides,
        "event_type": ["absorption"] * len(rows),
        "level_side": [r.side for r in rows],
        "level_price_ticks": np.array([r.price for r in rows], dtype=np.int64),
        "initial_size": np.array([r.initial_size for r in rows], dtype=np.int64),
        "executed_into": np.array([r.executed_into for r in rows], dtype=np.int64),
        "consume_ratio": np.array([r.consume_ratio() for r in rows], dtype=np.float64),
        "max_penetration_ticks": np.array([r.max_penetration_ticks for r in rows], dtype=np.int64),
        "replenish_count": np.array([r.replenish_count for r in rows], dtype=np.int64),
        "tracker_label": [r.label for r in rows],
    })


def dwell_withdraw_events_from_levels(
    levels: list[TrackedLevel],
    *,
    min_dwell_sec: float = 3.0,
    min_initial_size: int = 50,
) -> pa.Table:
    """H3/H9 liquidity persistence events (no post-contact mid leak).

    - dwell_repel: never contacted; left vicinity or still labeled dwell →
      fade away from level (BID→LONG, ASK→SHORT).
    - withdraw: size pulled without trade → continuation
      (ASK withdraw→LONG, BID withdraw→SHORT).
    """
    rows: list[tuple[TrackedLevel, str, int]] = []
    for lv in levels:
        if lv.contacted:
            continue
        if lv.initial_size < min_initial_size:
            continue
        # Approximate event time
        t = lv.last_update_ns if lv.last_update_ns > 0 else lv.first_seen_ns
        if lv.label == "WITHDRAW":
            # continuation through void
            side = 1 if lv.side == "ASK" else -1
            rows.append((lv, "withdraw", side))
        elif lv.label in ("REPEL", "DWELL_SUPPORT"):
            if lv.near_price_since_ns > 0:
                dwell = (t - lv.near_price_since_ns) / 1e9
                if dwell < min_dwell_sec:
                    continue
            elif lv.label == "REPEL":
                continue
            side = 1 if lv.side == "BID" else -1  # move away
            rows.append((lv, "dwell_repel", side))
        else:
            continue

    if not rows:
        return pa.table({})

    return pa.table({
        "event_ts_ns": np.array(
            [
                (r.last_update_ns if r.last_update_ns > 0 else r.first_seen_ns)
                for r, _, _ in rows
            ],
            dtype=np.int64,
        ),
        "side": np.array([s for _, _, s in rows], dtype=np.int8),
        "event_type": [et for _, et, _ in rows],
        "level_side": [r.side for r, _, _ in rows],
        "level_price_ticks": np.array([r.price for r, _, _ in rows], dtype=np.int64),
        "initial_size": np.array([r.initial_size for r, _, _ in rows], dtype=np.int64),
        "executed_into": np.array([r.executed_into for r, _, _ in rows], dtype=np.int64),
        "tracker_label": [r.label for r, _, _ in rows],
        "dwell_sec": np.array(
            [
                (
                    (r.last_update_ns - r.near_price_since_ns) / 1e9
                    if r.near_price_since_ns > 0
                    else 0.0
                )
                for r, _, _ in rows
            ],
            dtype=np.float64,
        ),
    })


def cross_confirm_liquidity_events(
    nq_events: pa.Table,
    es_events: pa.Table,
    *,
    sync_ms: int = 2000,
    require_same_event_type: bool = True,
) -> pa.Table:
    """Keep NQ liquidity events with same-side ES event within sync window."""
    if nq_events.num_rows == 0 or es_events.num_rows == 0:
        return pa.table({})
    nq_ts = nq_events.column("event_ts_ns").to_numpy().astype(np.int64)
    nq_side = nq_events.column("side").to_numpy()
    nq_type = nq_events.column("event_type").to_pylist()
    es_ts = es_events.column("event_ts_ns").to_numpy().astype(np.int64)
    es_side = es_events.column("side").to_numpy()
    es_type = es_events.column("event_type").to_pylist()
    order = np.argsort(es_ts)
    es_ts = es_ts[order]
    es_side = es_side[order]
    es_type = [es_type[i] for i in order]
    window = int(sync_ms * 1_000_000)
    keep = []
    lags = []
    for i in range(len(nq_ts)):
        lo = np.searchsorted(es_ts, nq_ts[i] - window, side="left")
        hi = np.searchsorted(es_ts, nq_ts[i] + window, side="right")
        best = None
        for j in range(lo, hi):
            if int(es_side[j]) != int(nq_side[i]):
                continue
            if require_same_event_type and es_type[j] != nq_type[i]:
                continue
            lag = abs(int(es_ts[j] - nq_ts[i]))
            if best is None or lag < best:
                best = lag
        if best is not None:
            keep.append(i)
            lags.append(best / 1e6)
    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    out = nq_events.take(idx)
    if "es_lag_ms" in out.column_names:
        out = out.drop("es_lag_ms")
    return out.append_column("es_lag_ms", pa.array(lags, type=pa.float64()))


def challenge_without_trade_events(
    levels: list[TrackedLevel],
    *,
    min_dwell_sec: float = 2.0,
    max_consume_ratio: float = 0.15,
    min_initial_size: int = 50,
    max_executed: float | None = None,
) -> pa.Table:
    """Large resting size challenged (near BBO) but barely traded → fade.

    Includes uncontacted dwell/repel and lightly contacted levels with low
    execute-into. Side = move away from level (BID→LONG, ASK→SHORT).
    """
    rows: list[TrackedLevel] = []
    for lv in levels:
        if lv.initial_size < min_initial_size:
            continue
        if lv.near_price_since_ns <= 0:
            continue
        t = lv.last_update_ns if lv.last_update_ns > 0 else lv.first_seen_ns
        dwell = (t - lv.near_price_since_ns) / 1e9
        if dwell < min_dwell_sec:
            continue
        cons = lv.consume_ratio() if lv.initial_size > 0 else 1.0
        if cons > max_consume_ratio:
            continue
        if max_executed is not None and lv.executed_into > max_executed:
            continue
        rows.append(lv)

    if not rows:
        return pa.table({})

    sides = np.array([1 if r.side == "BID" else -1 for r in rows], dtype=np.int8)
    # Event when challenge has lasted long enough without meaningful trade
    event_ts = np.array(
        [
            int(r.near_price_since_ns + int(min_dwell_sec * 1e9))
            for r in rows
        ],
        dtype=np.int64,
    )
    # Clamp to last_update if earlier
    last = np.array(
        [r.last_update_ns if r.last_update_ns > 0 else r.first_seen_ns for r in rows],
        dtype=np.int64,
    )
    event_ts = np.minimum(event_ts, last)

    return pa.table({
        "event_ts_ns": event_ts,
        "side": sides,
        "event_type": ["challenge_wo_trade"] * len(rows),
        "level_side": [r.side for r in rows],
        "level_price_ticks": np.array([r.price for r in rows], dtype=np.int64),
        "initial_size": np.array([r.initial_size for r in rows], dtype=np.int64),
        "executed_into": np.array([r.executed_into for r in rows], dtype=np.int64),
        "consume_ratio": np.array([r.consume_ratio() for r in rows], dtype=np.float64),
        "dwell_sec": np.array(
            [
                (r.last_update_ns - r.near_price_since_ns) / 1e9
                if r.near_price_since_ns > 0
                else 0.0
                for r in rows
            ],
            dtype=np.float64,
        ),
        "was_contacted": np.array([1 if r.contacted else 0 for r in rows], dtype=np.int8),
        "tracker_label": [r.label for r in rows],
    })


def annotate_near_lvn(
    events: pa.Table,
    lvn_snaps: list,
    *,
    pad_ticks: int = 4,
) -> pa.Table:
    """Flag events whose level/entry sits in or next to an active LVN zone."""
    if events.num_rows == 0:
        return events

    def active_zones(ts_ns: int) -> list:
        best = []
        for asof, zones in lvn_snaps:
            if asof <= ts_ns:
                best = zones
            else:
                break
        return best

    ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    if "level_price_ticks" in events.column_names:
        px = events.column("level_price_ticks").to_numpy().astype(np.int64)
    elif "entry_tick" in events.column_names:
        px = events.column("entry_tick").to_numpy().astype(np.int64)
    elif "lvn_mid" in events.column_names:
        px = events.column("lvn_mid").to_numpy().astype(np.int64)
    else:
        px = np.zeros(len(ts), dtype=np.int64)

    near = []
    for i, t0 in enumerate(ts):
        zones = active_zones(int(t0))
        hit = 0
        p = int(px[i])
        for z in zones:
            if (z.lo - pad_ticks) <= p <= (z.hi + pad_ticks):
                hit = 1
                break
        near.append(hit)

    out = events
    if "near_lvn" in out.column_names:
        out = out.drop("near_lvn")
    return out.append_column("near_lvn", pa.array(near, type=pa.int8()))


def aggressive_take_events_from_levels(
    levels: list[TrackedLevel],
    *,
    min_consume_ratio: float = 0.5,
    min_penetration_ticks: int = 1,
    min_executed: float = 0.0,
    require_attracted: bool = False,
) -> pa.Table:
    """Momentum: large level taken through (consume + penetration) → continuation.

    Opposite of absorption: price trades into and *through* the level.
    ASK taken (buy aggression) → LONG; BID taken → SHORT.
    """
    rows: list[TrackedLevel] = []
    for lv in levels:
        if not lv.contacted or lv.contact_ns <= 0:
            continue
        if require_attracted and not lv.attracted_before_contact:
            continue
        if lv.executed_into < min_executed:
            continue
        if lv.consume_ratio() < min_consume_ratio:
            continue
        if lv.max_penetration_ticks < min_penetration_ticks:
            continue
        rows.append(lv)

    if not rows:
        return pa.table({})

    # Continuation through the level
    sides = np.array([1 if r.side == "ASK" else -1 for r in rows], dtype=np.int8)
    event_ts = np.array([r.contact_ns for r in rows], dtype=np.int64)
    return pa.table({
        "event_ts_ns": event_ts,
        "side": sides,
        "event_type": ["aggressive_take"] * len(rows),
        "level_side": [r.side for r in rows],
        "level_price_ticks": np.array([r.price for r in rows], dtype=np.int64),
        "initial_size": np.array([r.initial_size for r in rows], dtype=np.int64),
        "executed_into": np.array([r.executed_into for r in rows], dtype=np.int64),
        "consume_ratio": np.array([r.consume_ratio() for r in rows], dtype=np.float64),
        "max_penetration_ticks": np.array([r.max_penetration_ticks for r in rows], dtype=np.int64),
        "replenish_count": np.array([r.replenish_count for r in rows], dtype=np.int64),
        "attracted": np.array(
            [1 if r.attracted_before_contact else 0 for r in rows], dtype=np.int8
        ),
        "tracker_label": [r.label for r in rows],
    })


def hold_without_trade_events_from_levels(
    levels: list[TrackedLevel],
    *,
    min_dwell_sec: float = 2.0,
    min_initial_size: int = 50,
    max_consume_ratio: float = 0.15,
) -> pa.Table:
    """Opposite-side support: large resting size dwells near BBO with little trade.

    Causal: dwell clock + low consume only (no post-move REPEL leak).
    BID hold → LONG; ASK hold → SHORT.
    """
    rows: list[TrackedLevel] = []
    for lv in levels:
        if lv.initial_size < min_initial_size:
            continue
        if lv.near_price_since_ns <= 0:
            continue
        t = lv.last_update_ns if lv.last_update_ns > 0 else lv.first_seen_ns
        dwell = (t - lv.near_price_since_ns) / 1e9
        if dwell < min_dwell_sec:
            continue
        cons = lv.consume_ratio() if lv.initial_size > 0 else 1.0
        if cons > max_consume_ratio:
            continue
        rows.append(lv)

    if not rows:
        return pa.table({})

    sides = np.array([1 if r.side == "BID" else -1 for r in rows], dtype=np.int8)
    event_ts = np.array(
        [int(r.near_price_since_ns + int(min_dwell_sec * 1e9)) for r in rows],
        dtype=np.int64,
    )
    last = np.array(
        [r.last_update_ns if r.last_update_ns > 0 else r.first_seen_ns for r in rows],
        dtype=np.int64,
    )
    event_ts = np.minimum(event_ts, last)
    return pa.table({
        "event_ts_ns": event_ts,
        "side": sides,
        "event_type": ["hold_wo_trade"] * len(rows),
        "level_side": [r.side for r in rows],
        "level_price_ticks": np.array([r.price for r in rows], dtype=np.int64),
        "initial_size": np.array([r.initial_size for r in rows], dtype=np.int64),
        "executed_into": np.array([r.executed_into for r in rows], dtype=np.int64),
        "consume_ratio": np.array([r.consume_ratio() for r in rows], dtype=np.float64),
        "dwell_sec": np.array(
            [
                (r.last_update_ns - r.near_price_since_ns) / 1e9
                if r.near_price_since_ns > 0
                else 0.0
                for r in rows
            ],
            dtype=np.float64,
        ),
        "tracker_label": [r.label for r in rows],
    })


def micro_regime_composite_events(
    take_events: pa.Table,
    hold_events: pa.Table,
    *,
    pair_ms: int = 3000,
) -> pa.Table:
    """Pair aggression-through-level with opposite-book hold (same side).

    Long: ASK take + BID hold. Short: BID take + ASK hold.
    Event timestamp = later of the two legs (causal when both observed).
    """
    if take_events.num_rows == 0 or hold_events.num_rows == 0:
        return pa.table({})

    t_ts = take_events.column("event_ts_ns").to_numpy().astype(np.int64)
    t_side = take_events.column("side").to_numpy().astype(np.int8)
    t_lvl = take_events.column("level_side").to_pylist()
    h_ts = hold_events.column("event_ts_ns").to_numpy().astype(np.int64)
    h_side = hold_events.column("side").to_numpy().astype(np.int8)
    h_lvl = hold_events.column("level_side").to_pylist()

    h_order = np.argsort(h_ts)
    h_ts = h_ts[h_order]
    h_side = h_side[h_order]
    h_lvl = [h_lvl[i] for i in h_order]
    window = int(pair_ms * 1_000_000)

    out_ts: list[int] = []
    out_side: list[int] = []
    out_gap: list[float] = []
    out_take_lvl: list[str] = []
    out_hold_lvl: list[str] = []
    used_hold: set[int] = set()

    take_order = np.argsort(t_ts)
    for i in take_order:
        want_hold_lvl = "BID" if t_lvl[i] == "ASK" else "ASK"
        if want_hold_lvl == "BID" and int(t_side[i]) != 1:
            continue
        if want_hold_lvl == "ASK" and int(t_side[i]) != -1:
            continue
        lo = np.searchsorted(h_ts, t_ts[i] - window, side="left")
        hi = np.searchsorted(h_ts, t_ts[i] + window, side="right")
        best_j = None
        best_gap = None
        for j in range(lo, hi):
            if j in used_hold:
                continue
            if int(h_side[j]) != int(t_side[i]):
                continue
            if h_lvl[j] != want_hold_lvl:
                continue
            gap = abs(int(h_ts[j] - t_ts[i]))
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_j = j
        if best_j is None:
            continue
        used_hold.add(best_j)
        out_ts.append(int(max(t_ts[i], h_ts[best_j])))
        out_side.append(int(t_side[i]))
        out_gap.append(best_gap / 1e6)
        out_take_lvl.append(t_lvl[i])
        out_hold_lvl.append(h_lvl[best_j])

    if not out_ts:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.array(out_ts, dtype=np.int64),
        "side": np.array(out_side, dtype=np.int8),
        "event_type": ["micro_regime"] * len(out_ts),
        "take_level_side": out_take_lvl,
        "hold_level_side": out_hold_lvl,
        "pair_gap_ms": np.array(out_gap, dtype=np.float64),
    })


# Back-compat thin wrapper used by earlier scaffold
def detect_large_level_events(
    depth: pa.Table,
    cfg: LargeLevelConfig | None = None,
) -> pa.Table:
    cfg = cfg or LargeLevelConfig()
    if depth.num_rows == 0:
        return pa.table({})
    size = depth.column("size").to_numpy().astype(float)
    ts = depth.column("ts_ns").to_numpy()
    is_bid = depth.column("is_bid").to_numpy().astype(bool)
    price_ticks = depth.column("price_ticks").to_numpy()
    large = size >= cfg.absolute_size
    idx = np.where(large & (size > 0))[0]
    if len(idx) == 0:
        return pa.table({})
    return pa.table({
        "event_ts_ns": ts[idx],
        "side": np.where(is_bid[idx], "BID", "ASK"),
        "price_ticks": price_ticks[idx],
        "size": size[idx],
        "label": np.array(["LARGE_SEEN"] * len(idx)),
    })
