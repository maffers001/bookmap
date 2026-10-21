"""Shared harness for the correlated ES/NQ experiments (EXP-0035..0043).

Keeps ``run_experiment.py`` to a single dispatch line. Everything here is in
chart points (``pts_per_tick=0.25``) and causal (events stamped when known).

Cohorts follow the frozen-base convention used by ``run_mat_overlays``:
``OLD`` = the 14 sessions available when the base was chosen (TRAIN + HOLD as
EXP-0034), ``NEW`` = sessions added afterwards (incl. 161026). ``NEW`` is the
de-facto out-of-sample cohort for anything tuned on ``OLD``.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    detect_signals,
    filter_summary,
    merge_summaries,
)
from research.features.momentum import trades_to_tape_bars
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _load_trades,
    _random_null_events,
    _summarize_labeled,
)

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25
PTS_PER_TICK = 0.25

TRAIN = ["260326", "030826", "040826", "050826", "160926", "170926", "180926", "210926", "220926"]
HOLD = ["011026", "021026", "051026", "061026", "071026"]
OLD = set(TRAIN) | set(HOLD)


def cohort(sid: str) -> str:
    if sid in TRAIN:
        return "train"
    if sid in HOLD:
        return "hold"
    return "new"


# ---------------------------------------------------------------- loading

def uk_bounds(mid_ns: int, start: time = time(14, 30), end: time = time(17, 0)) -> tuple[int, int]:
    d = datetime.fromtimestamp(mid_ns / 1e9, tz=timezone.utc).astimezone(UK).date()
    return (
        int(datetime.combine(d, start, tzinfo=UK).timestamp() * 1e9),
        int(datetime.combine(d, end, tzinfo=UK).timestamp() * 1e9),
    )


def filter_window(t: pa.Table, bounds: tuple[int, int]) -> pa.Table:
    if t.num_rows == 0:
        return t
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    k = (ts >= bounds[0]) & (ts <= bounds[1])
    return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)


class Pair:
    """NQ/ES trades for one session in the UK 14:30-17:00 window, plus lazily
    built 100 ms tape bars and the window start (VWAP anchor)."""

    def __init__(self, sid: str, nq: pa.Table, es: pa.Table, bounds: tuple[int, int]):
        self.sid = sid
        self.nq = nq
        self.es = es
        self.bounds = bounds
        self._bars: dict[int, tuple[pa.Table, pa.Table]] = {}
        self._path: tuple[np.ndarray, np.ndarray] | None = None
        self._path_es: tuple[np.ndarray, np.ndarray] | None = None

    @property
    def vwap_from_ns(self) -> int:
        return self.bounds[0]

    def bars(self, bar_ms: int = 100) -> tuple[pa.Table, pa.Table]:
        if bar_ms not in self._bars:
            nq_b = trades_to_tape_bars(self.nq, bar_ms=bar_ms)
            es_b = trades_to_tape_bars(self.es, bar_ms=bar_ms) if self.es.num_rows else pa.table({})
            self._bars[bar_ms] = (nq_b, es_b)
        return self._bars[bar_ms]

    def path(self) -> tuple[np.ndarray, np.ndarray]:
        if self._path is None:
            self._path = build_price_path(self.nq, pts_per_tick=PTS_PER_TICK)
        return self._path

    def path_es(self) -> tuple[np.ndarray, np.ndarray]:
        if self._path_es is None:
            self._path_es = build_price_path(self.es, pts_per_tick=PTS_PER_TICK)
        return self._path_es


def _prefer_single_feed(table: pa.Table, parquet_dir: Path, sid: str, role: str) -> pa.Table:
    """Keep one alias per role so research matches live (primary chart feed).

    Extracts often record the same contract on two Bookmap aliases (e.g.
    ``NQM6.CME@RITHMIC`` + ``NQM6@TV``) with *different* timestamps. Merging
    them doubles tape density and invents fades that a single-feed live replay
    can never see. Prefer ``meta.primary_alias`` when it matches ``role``, else
    the densest alias (stable fallback).
    """
    if table.num_rows == 0 or "alias" not in table.column_names:
        return table
    aliases = table.column("alias").unique().to_pylist()
    if len(aliases) <= 1:
        return table
    pick = None
    meta_path = parquet_dir / f"session_id={sid}" / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        primary = meta.get("primary_alias")
        instruments = meta.get("instruments") or []
        if role == "NQ" and primary in aliases:
            pick = primary
        elif role == "ES" and primary:
            # Same venue/connection family as the NQ primary when present.
            suffix = ""
            if "@" in str(primary):
                suffix = str(primary).split("@", 1)[1]
            for a in aliases:
                if suffix and str(a).endswith("@" + suffix):
                    pick = a
                    break
            if pick is None:
                for inst in instruments:
                    if inst.get("role") == "ES" and inst.get("alias") in aliases:
                        pick = inst["alias"]
                        break
    if pick is None:
        # Densest alias.
        al = table.column("alias").to_pylist()
        pick = max(aliases, key=lambda a: al.count(a))
    mask = np.array([a == pick for a in table.column("alias").to_pylist()])
    return table.take(np.where(mask)[0])


def load_pair(parquet_dir: Path, sid: str) -> Pair | None:
    nq0 = _prefer_single_feed(_load_trades(parquet_dir, sid, "NQ"), parquet_dir, sid, "NQ")
    if nq0.num_rows == 0:
        return None
    ts = nq0.column("ts_ns").to_numpy()
    b = uk_bounds(int(ts[len(ts) // 2]))
    nq = filter_window(nq0, b)
    if nq.num_rows == 0:
        return None
    es0 = _prefer_single_feed(_load_trades(parquet_dir, sid, "ES"), parquet_dir, sid, "ES")
    es = filter_window(es0, b) if es0.num_rows else es0
    return Pair(sid, nq, es, b)


def load_depth_window(
    parquet_dir: Path, sid: str, role: str, bounds: tuple[int, int], batch_rows: int = 2_000_000
) -> pa.Table:
    """Stream depth.parquet and keep only ``role`` rows inside ``bounds``.

    Avoids materialising the full (tens of millions of rows) depth table.
    """
    path = parquet_dir / f"session_id={sid}" / "depth.parquet"
    if not path.exists() or path.stat().st_size == 0:
        print(f"MISSING depth.parquet for session {sid}", flush=True)
        return pa.table({})
    pf = pq.ParquetFile(path)
    cols = ["ts_ns", "role", "is_bid", "price_ticks", "size"]
    parts = []
    for batch in pf.iter_batches(batch_size=batch_rows, columns=cols):
        t = pa.Table.from_batches([batch])
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        roles = t.column("role").to_numpy(zero_copy_only=False)
        k = (ts >= bounds[0]) & (ts <= bounds[1]) & (roles == role)
        if k.any():
            parts.append(t.take(np.where(k)[0]))
    return pa.concat_tables(parts) if parts else pa.table({})


def list_sessions(parquet_dir: Path, exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    out = []
    for p in sorted(parquet_dir.iterdir()):
        if not p.is_dir() or not p.name.startswith("session_id="):
            continue
        sid = p.name.replace("session_id=", "")
        if sid == "smoke_synthetic" or sid in exclude:
            continue
        out.append(sid)
    return out


# ---------------------------------------------------------------- matching

def cross_match(
    nq_ev: pa.Table,
    es_ev: pa.Table,
    *,
    sync_ms: int,
    match_col: str = "side",
    same_side: bool = True,
    stamp: str = "confirm",
) -> pa.Table:
    """Generic ES/NQ event matcher.

    For each NQ event find the nearest ES event within ``sync_ms`` (both
    directions for ``stamp="confirm"``, ES at/before NQ for ``"causal"``) whose
    ``match_col`` equals (``same_side``) or is opposite to the NQ value.
    Output keeps all NQ columns, adds ``es_<col>`` for every ES column,
    ``es_lag_ms`` / ``es_signed_lag_ms`` (negative = ES first),
    ``nq_event_ts_ns`` / ``es_event_ts_ns``; ``event_ts_ns`` = max(nq, es) for
    confirm, nq for causal.
    """
    if nq_ev.num_rows == 0 or es_ev.num_rows == 0:
        return pa.table({})
    nq_ts = nq_ev.column("event_ts_ns").to_numpy().astype(np.int64)
    nq_key = nq_ev.column(match_col).to_numpy()
    es_ts0 = es_ev.column("event_ts_ns").to_numpy().astype(np.int64)
    order = np.argsort(es_ts0, kind="stable")
    es_ts = es_ts0[order]
    es_key = es_ev.column(match_col).to_numpy()[order]
    win = int(sync_ms * 1_000_000)

    keep, best_j, lag, slag = [], [], [], []
    for i in range(len(nq_ts)):
        t = int(nq_ts[i])
        lo = int(np.searchsorted(es_ts, t - win, side="left"))
        hi = int(np.searchsorted(es_ts, t if stamp == "causal" else t + win, side="right"))
        bj, babs, bsg = -1, None, 0
        for j in range(lo, hi):
            ok = (es_key[j] == nq_key[i]) if same_side else (es_key[j] == -nq_key[i])
            if not ok:
                continue
            sg = int(es_ts[j] - t)
            if babs is None or abs(sg) < babs:
                bj, babs, bsg = j, abs(sg), sg
        if bj >= 0:
            keep.append(i)
            best_j.append(bj)
            lag.append(babs / 1e6)
            slag.append(bsg / 1e6)
    if not keep:
        return pa.table({})
    ki = np.asarray(keep, dtype=np.int64)
    kj = np.asarray(best_j, dtype=np.int64)
    es_sel = es_ev.take(order[kj])
    nq_sel = nq_ev.take(ki)
    ev_ts = np.maximum(nq_ts[ki], es_ts[kj]) if stamp == "confirm" else nq_ts[ki]
    cols: dict = {"event_ts_ns": ev_ts.astype(np.int64)}
    for name in nq_sel.column_names:
        if name == "event_ts_ns":
            continue
        cols[name] = nq_sel.column(name)
    for name in es_sel.column_names:
        if name == "event_ts_ns":
            continue
        cols[f"es_{name}"] = es_sel.column(name)
    cols["es_lag_ms"] = np.asarray(lag, dtype=np.float64)
    cols["es_signed_lag_ms"] = np.asarray(slag, dtype=np.float64)
    cols["nq_event_ts_ns"] = nq_ts[ki]
    cols["es_event_ts_ns"] = es_ts[kj]
    return pa.table(cols)


# ---------------------------------------------------------------- depth cache

def level_contacts(parquet_dir: Path, pair: "Pair", results_dir: Path, *, ll_cfg=None) -> pa.Table | None:
    """Contacted NQ large levels (LargeLevelTracker) in the UK window, cached at
    ``results/_cache/<sid>_nq_level_contacts.parquet``. None if no depth."""
    from research.features.liquidity import LargeLevelConfig, run_liquidity_tracker

    cache_dir = results_dir / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{pair.sid}_nq_level_contacts.parquet"
    if path.exists():
        t = pq.read_table(path)
        return None if t.num_rows == 0 and t.schema.metadata and t.schema.metadata.get(b"no_depth") else t
    depth = load_depth_window(parquet_dir, pair.sid, "NQ", pair.bounds)
    if depth.num_rows == 0:
        empty = pa.table({"contact_ns": pa.array([], pa.int64())})
        empty = empty.replace_schema_metadata({b"no_depth": b"1"})
        pq.write_table(empty, path)
        return None
    levels = run_liquidity_tracker(depth, pair.nq, cfg=ll_cfg or LargeLevelConfig(), rth_uk_only=False)
    rows = [lv for lv in levels if lv.contacted and lv.contact_ns > 0]
    t = pa.table({
        "contact_ns": np.asarray([lv.contact_ns for lv in rows], dtype=np.int64),
        "level_side": [lv.side for lv in rows],
        "price_ticks": np.asarray([lv.price for lv in rows], dtype=np.int64),
        "initial_size": np.asarray([lv.initial_size for lv in rows], dtype=np.int64),
        "executed_into": np.asarray([lv.executed_into for lv in rows], dtype=np.int64),
        "max_penetration_ticks": np.asarray([lv.max_penetration_ticks for lv in rows], dtype=np.int64),
        "replenish_count": np.asarray([lv.replenish_count for lv in rows], dtype=np.int64),
        "label": [lv.label for lv in rows],
    })
    pq.write_table(t, path, compression="zstd")
    return t


# ---------------------------------------------------------------- event study

def confirm_bursts(pair: Pair, sim_cfg: MatSimConfig | None = None) -> pa.Table:
    """Corrected EXP-0008 comparator: ES+NQ bursts, ``confirm`` stamp, no gates."""
    cfg = sim_cfg or MatSimConfig(es_stamp="confirm")
    if cfg.es_stamp != "confirm":
        cfg = MatSimConfig(**{**cfg.__dict__, "es_stamp": "confirm"})
    return detect_signals(pair.nq, pair.es, cfg)


def _with_session(t: pa.Table, sid: str) -> pa.Table:
    if t.num_rows == 0:
        return t
    if "session_id" in t.column_names:
        return t
    return t.append_column("session_id", pa.array([sid] * t.num_rows))


def label_in_points(
    events: pa.Table,
    pair: Pair,
    horizons: list[int],
    targets: list[float],
    *,
    role: str = "NQ",
) -> pa.Table:
    """Label forward outcomes on NQ (default) or ES chart points."""
    if events.num_rows == 0:
        return pa.table({})
    if role == "ES":
        if pair.es.num_rows == 0:
            return pa.table({})
        ts, px = pair.path_es()
    else:
        ts, px = pair.path()
    return _with_session(label_events(events, ts, px, horizons, targets), pair.sid)


def null_events(pair: Pair, n: int, seed: int = 42) -> pa.Table:
    return _random_null_events(pair.nq, n, seed=seed)


def summarize(parts: list[pa.Table], horizons: list[int], targets: list[float]) -> dict:
    parts = [p for p in parts if p.num_rows > 0]
    if not parts:
        return {"status": "no_events", "n_events": 0}
    cols = set(parts[0].column_names)
    for p in parts[1:]:
        cols &= set(p.column_names)
    keep = [c for c in parts[0].column_names if c in cols]
    df = pa.concat_tables([p.select(keep) for p in parts], promote_options="default")
    return _summarize_labeled(df, horizons, targets)


def summarize_by_cohort(parts: list[tuple[str, pa.Table]], horizons, targets) -> dict:
    out = {"all": summarize([t for _, t in parts], horizons, targets)}
    for c in ("train", "hold", "new", "old"):
        sel = [t for sid, t in parts if (cohort(sid) == c) or (c == "old" and sid in OLD)]
        out[c] = summarize(sel, horizons, targets)
    return out


# ---------------------------------------------------------------- P&L

def pack_pnl(s: SimSummary, sids: list[str], commission: float = COMMISSION) -> dict:
    day_nets = []
    days_pos = 0
    for sid in sids:
        dn = filter_summary(s, lambda t, x=sid: t.session_id == x, commission).net
        day_nets.append(dn)
        days_pos += dn > 0
    by = {}
    for c in ("train", "hold", "old", "new"):
        sub = filter_summary(
            s, lambda t, c=c: (cohort(t.session_id) == c) or (c == "old" and t.session_id in OLD), commission
        )
        by[f"{c}_net"] = round(sub.net, 2)
        by[f"{c}_n"] = sub.n
    reasons: dict[str, dict] = {}
    for tr in s.trades:
        r = reasons.setdefault(tr.reason, {"n": 0, "gross": 0.0})
        r["n"] += 1
        r["gross"] += tr.pnl
    for r in reasons.values():
        r["gross"] = round(r["gross"], 2)
    return {
        **s.to_dict(),
        **by,
        "days_pos": int(days_pos),
        "n_sessions": len(sids),
        "worst_day": round(min(day_nets), 2) if day_nets else None,
        "best_day": round(max(day_nets), 2) if day_nets else None,
        "by_reason": reasons,
    }


def merge(parts: list[SimSummary], commission: float = COMMISSION) -> SimSummary:
    return merge_summaries(parts, commission)


# ---------------------------------------------------------------- report

def fmt(x, nd=1) -> str:
    if x is None:
        return "–"
    if isinstance(x, float):
        if not np.isfinite(x):
            return "–"
        return f"{x:+.{nd}f}" if nd else f"{x:.0f}"
    return str(x)


def event_row(name: str, s: dict) -> str:
    if not s or s.get("n_events", 0) == 0:
        return f"| {name} | 0 | – | – | – | – | – | – | – |"
    return (
        f"| {name} | {s['n_events']} | {s.get('p_hit_5pts', float('nan')):.2f} | "
        f"{s.get('p_hit_10pts', float('nan')):.2f} | {s.get('p_hit_20pts', float('nan')):.2f} | "
        f"{fmt(s.get('mean_ret_pts_10000ms'), 2)} | {fmt(s.get('mean_ret_pts_60000ms'), 2)} | "
        f"{fmt(s.get('mean_mfe_pts'), 1)} | {fmt(s.get('mean_mae_pts'), 1)} |"
    )


EVENT_HDR = [
    "| set | n | hit5 | hit10 | hit20 | @10s | @60s | MFE | MAE |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
]


def pnl_row(name: str, r: dict) -> str:
    return (
        f"| {name} | {r['n']} | {r['wr'] if r['wr'] is not None else '–'} | {r['gross']:+.1f} | "
        f"{r['net']:+.1f} | {r['train_net']:+.1f} | {r['hold_net']:+.1f} | {r['new_net']:+.1f} | "
        f"{r['days_pos']}/{r['n_sessions']} | {r['max_dd']:.0f} | {r['worst_day']:+.1f} |"
    )


PNL_HDR = [
    "| variant | n | WR | gross | net | train | hold | new | days+ | maxDD | worst |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
]


def write_report(results_dir: Path, report: dict, md_lines: list[str]) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (results_dir / "summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
