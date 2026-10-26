"""IRT Profile-style LVN/HVN detection (devNodes / sensitivity).

Linn Software docs: a sensitivity of S requires a peak (HVN) to be strictly
higher than the S price rows above and below it (valleys / LVNs are symmetric
minima). Row height = 0.25 pt for ES and NQ.

Settings mirrored from the user's IRT Profile prefs:
  ES: 90-day volume profile, sensitivity 15
  NQ: 30-day volume profile, sensitivity 100

Sensitivity S is in **ticks** (= price rows at row height 0.25). Treating
15/100 as chart points failed validation — see
``results/LVN_PROFILE_VALIDATE/summary.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import numpy as np

UK = ZoneInfo("Europe/London")
TICK = 0.25

MARKET_CFG = {
    "ES": {
        "window_days": 90,
        "sensitivity": 15,  # ticks (±S neighbourhood)
        "tick_path": "data/irt/@ES#_1Tick.csv",
        "all_path": "data/irt/ESZ6_ALL.csv",
    },
    "NQ": {
        "window_days": 30,
        "sensitivity": 100,  # ticks
        "tick_path": "data/irt/@NQ#_Export_1tick.csv",
        "all_path": "data/irt/@NQ#_ALL.csv",
    },
}


@dataclass(frozen=True)
class Node:
    price: float
    volume: float
    kind: str  # "LVN" | "HVN"


def price_to_row(px: float, tick: float = TICK) -> int:
    return int(round(px / tick))


def row_to_price(row: int, tick: float = TICK) -> float:
    return row * tick


def detect_nodes(
    vap: dict[int, float] | np.ndarray,
    *,
    sensitivity: int,
    tick: float = TICK,
    strict: bool = True,
    skip_edge: bool = True,
    smooth: int = 0,
) -> tuple[list[Node], list[Node]]:
    """Return (valleys/LVN, peaks/HVN) from a volume-at-price map.

    ``vap`` may be ``{row_tick: volume}`` or a dense array indexed by row offset
    with an accompanying min-row — prefer the dict form.
    """
    if isinstance(vap, np.ndarray):
        raise TypeError("pass a dict[int,float] row→volume map")
    if not vap:
        return [], []
    rows = sorted(r for r, v in vap.items() if v > 0)
    if len(rows) < 2 * sensitivity + 1:
        return [], []
    vol = {r: float(vap[r]) for r in rows}
    if smooth and smooth > 1:
        # Simple centered moving average over ``smooth`` rows (odd preferred).
        half = smooth // 2
        sm: dict[int, float] = {}
        for r in rows:
            vals = [vol.get(r + k, 0.0) for k in range(-half, half + 1)]
            sm[r] = sum(vals) / len(vals)
        vol = sm

    lo, hi = rows[0], rows[-1]
    valleys: list[Node] = []
    peaks: list[Node] = []
    # Iterate every populated row; neighbours may be missing (treated as 0).
    for r in rows:
        if skip_edge and (r - lo < sensitivity or hi - r < sensitivity):
            continue
        v = vol[r]
        is_valley = True
        is_peak = True
        for k in range(1, sensitivity + 1):
            above = vol.get(r + k, 0.0)
            below = vol.get(r - k, 0.0)
            if strict:
                if not (v < above and v < below):
                    is_valley = False
                if not (v > above and v > below):
                    is_peak = False
            else:
                if not (v <= above and v <= below):
                    is_valley = False
                if not (v >= above and v >= below):
                    is_peak = False
            if not is_valley and not is_peak:
                break
        # A flat zero shelf is not a useful node
        if v <= 0:
            continue
        if is_valley and not is_peak:
            valleys.append(Node(row_to_price(r, tick), v, "LVN"))
        elif is_peak and not is_valley:
            peaks.append(Node(row_to_price(r, tick), v, "HVN"))
    return valleys, peaks


def nearest_n(nodes: list[Node], ref: float, n: int = 3) -> list[Node]:
    """IRT export order: farthest…nearest as LVN1…LVN3 → return nearest-first then reverse for labels."""
    ranked = sorted(nodes, key=lambda nd: abs(nd.price - ref))
    return ranked[:n]


def label_nearest_three(nodes: list[Node], ref: float) -> dict[str, float]:
    """Map LVN1/2/3 (or HVN1/2/3) as farthest / mid / nearest to ``ref``."""
    top = nearest_n(nodes, ref, 3)
    # pad
    while len(top) < 3:
        top.append(Node(0.0, 0.0, top[0].kind if top else "LVN"))
    # nearest-first → assign LVN3, LVN2, LVN1
    kind = top[0].kind if top[0].price else "LVN"
    out = {
        f"{kind}3": top[0].price,
        f"{kind}2": top[1].price,
        f"{kind}1": top[2].price,
    }
    return out


def parse_ts(s: str) -> datetime:
    s = s.strip().strip('"').replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UK)
        except ValueError:
            continue
    raise ValueError(s)


def load_all_snapshot(
    all_path: Path, day: str, hhmm: str = "14:30"
) -> dict[str, float] | None:
    """Exact bar at day hhmm (not substring match on :14:30)."""
    target_prefix = f"{day} {hhmm}"
    best = None
    with all_path.open(encoding="utf-8", newline="") as f:
        import csv

        for row in csv.DictReader(f):
            ds = row["Date"].strip().strip('"')
            if ds.startswith(target_prefix) and (
                len(ds) == len(target_prefix)
                or ds[len(target_prefix) : len(target_prefix) + 1] in ("", ":")
            ):
                # Prefer exact hhmm or hhmm:00 over hhmm:30
                if ds == target_prefix or ds.startswith(target_prefix + ":00"):
                    best = row
                    break
                if best is None:
                    best = row
    if best is None:
        return None
    return {
        "Close": float(best["Close"]),
        "LVN1": float(best["LVN1"]),
        "LVN2": float(best["LVN2"]),
        "LVN3": float(best["LVN3"]),
        "HVN1": float(best["HVN1"]),
        "HVN2": float(best["HVN2"]),
        "HVN3": float(best["HVN3"]),
        "Date": best["Date"].strip().strip('"'),
    }


def daily_vap_path(market: str, root: Path | None = None) -> Path:
    root = root or Path(__file__).resolve().parents[2]
    return root / "data" / "irt" / f"vap_daily_{market}.parquet"


def aggregate_vap_window(
    market: str,
    start: datetime,
    end: datetime,
    *,
    tick_path: Path | None = None,
    root: Path | None = None,
) -> dict[int, float]:
    """Volume-at-price over [start, end].

    Uses precomputed daily parquet for whole days in ``[start.date, end.date)``,
    then adds the partial end-day from the tick file up to ``end``.
    """
    import duckdb

    root = root or Path(__file__).resolve().parents[2]
    cfg = MARKET_CFG[market]
    tick_path = tick_path or (root / cfg["tick_path"])
    daily = daily_vap_path(market, root)
    con = duckdb.connect()
    vap: dict[int, float] = {}

    start_day = start.date()
    end_day = end.date()
    # Whole days strictly before end_day and on/after start_day
    if daily.exists():
        q = f"""
            SELECT row_tick, sum(vol) AS vol
            FROM read_parquet('{str(daily).replace(chr(92), "/")}')
            WHERE day >= DATE '{start_day.isoformat()}'
              AND day <  DATE '{end_day.isoformat()}'
            GROUP BY 1
        """
        for r, v in con.execute(q).fetchall():
            if v and v > 0:
                vap[int(r)] = vap.get(int(r), 0.0) + float(v)

    # Partial end day from ticks (and any start-day partial if start is mid-day
    # on the same calendar day as end — rare for our snapshots).
    src = str(tick_path).replace("\\", "/")
    end_s = end.strftime("%Y-%m-%d %H:%M:%S")
    start_s = start.strftime("%Y-%m-%d %H:%M:%S")
    if start_day == end_day:
        where = f"DATE >= TIMESTAMP '{start_s}' AND DATE <= TIMESTAMP '{end_s}'"
    else:
        where = (
            f"DATE >= TIMESTAMP '{end_day.isoformat()} 00:00:00' "
            f"AND DATE <= TIMESTAMP '{end_s}'"
        )
    q2 = f"""
        SELECT cast(round(PRICE / {TICK}) AS BIGINT) AS row_tick,
               cast(sum(TICKVOL) AS DOUBLE) AS vol
        FROM read_csv('{src}', delim='\\t', header=true, sample_size=20000)
        WHERE {where}
        GROUP BY 1
    """
    for r, v in con.execute(q2).fetchall():
        if v and v > 0:
            vap[int(r)] = vap.get(int(r), 0.0) + float(v)
    return vap


# Back-compat alias
def aggregate_vap_duckdb(
    tick_path: Path,
    start: datetime,
    end: datetime,
    market: str | None = None,
) -> dict[int, float]:
    if market is None:
        name = tick_path.name.upper()
        market = "NQ" if "NQ" in name else "ES"
    return aggregate_vap_window(market, start, end, tick_path=tick_path)


def match_score(
    predicted: dict[str, float],
    actual: dict[str, float],
    keys: Iterable[str],
    tol: float = TICK,
) -> dict[str, float]:
    errs = []
    hits = 0
    n = 0
    for k in keys:
        a = actual.get(k, 0.0)
        p = predicted.get(k, 0.0)
        if a == 0.0 and p == 0.0:
            continue
        n += 1
        e = abs(p - a)
        errs.append(e)
        if e <= tol + 1e-9:
            hits += 1
    if not errs:
        return {"n": 0, "hit_pct": 0.0, "median": float("nan"), "p90": float("nan")}
    arr = np.asarray(errs, dtype=float)
    return {
        "n": n,
        "hit_pct": 100.0 * hits / n,
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
        "mean": float(arr.mean()),
    }


def compute_labeled_nodes(
    vap: dict[int, float],
    close: float,
    *,
    sensitivity: int,
    strict: bool = True,
    skip_edge: bool = True,
    smooth: int = 0,
) -> tuple[dict[str, float], dict[str, float], list[Node], list[Node]]:
    valleys, peaks = detect_nodes(
        vap, sensitivity=sensitivity, strict=strict, skip_edge=skip_edge, smooth=smooth
    )
    lvn = label_nearest_three(valleys, close)
    hvn = label_nearest_three(peaks, close)
    return lvn, hvn, valleys, peaks
