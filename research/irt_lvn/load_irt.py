"""Positional loader for Investor/RT chart exports (duplicate header names)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

UK = ZoneInfo("Europe/London")

KIND_LVN = "LVN"
KIND_DOP = "DOP"
KIND_DVPOC = "dVPOC"


def _nth_index(header: list[str], name: str, n: int) -> int:
    found = 0
    for i, h in enumerate(header):
        if h.strip() == name:
            found += 1
            if found == n:
                return i
    raise KeyError(f"{name!r} occurrence #{n} not in header: {header}")


def _parse_float(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_ts_ns(date_str: str, tz: ZoneInfo = UK) -> int:
    """IRT Date → ns. Bare dates become midnight; seconds optional."""
    s = date_str.strip().strip('"')
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=tz)
            return int(dt.timestamp() * 1e9)
        except ValueError:
            continue
    raise ValueError(f"unparseable IRT Date: {date_str!r}")


@dataclass(frozen=True)
class IrtColumns:
    date: int
    close: int
    dop: int
    dvpoc2: int
    lvn2: int
    lvn3: int
    hvn1: int
    hvn2: int
    hvn3: int


def resolve_columns(header: list[str]) -> IrtColumns:
    header = [h.strip() for h in header]
    return IrtColumns(
        date=_nth_index(header, "Date", 1),
        close=_nth_index(header, "Close", 1),
        dop=_nth_index(header, "HW_MT_DOP_NY", 1),
        dvpoc2=_nth_index(header, "dVPOC", 2),
        lvn2=_nth_index(header, "LVN2", 1),
        lvn3=_nth_index(header, "LVN3", 1),
        hvn1=_nth_index(header, "HVN1", 1),
        hvn2=_nth_index(header, "HVN2", 1),
        hvn3=_nth_index(header, "HVN3", 1),
    )


@dataclass
class IrtBars:
    """As-of level series from one IRT CSV (chart points)."""

    symbol: str
    ts_ns: np.ndarray  # int64, sorted
    close: np.ndarray
    dop: np.ndarray
    dvpoc: np.ndarray
    lvn2: np.ndarray
    lvn3: np.ndarray
    hvn1: np.ndarray
    hvn2: np.ndarray
    hvn3: np.ndarray
    columns: IrtColumns
    days: set[tuple[int, int, int]]

    def slice_range(self, lo_ns: int, hi_ns: int) -> IrtBars:
        m = (self.ts_ns >= lo_ns) & (self.ts_ns <= hi_ns)
        empty = not m.any()

        def take(a: np.ndarray) -> np.ndarray:
            return a[:0] if empty else a[m]

        return IrtBars(
            self.symbol,
            take(self.ts_ns),
            take(self.close),
            take(self.dop),
            take(self.dvpoc),
            take(self.lvn2),
            take(self.lvn3),
            take(self.hvn1),
            take(self.hvn2),
            take(self.hvn3),
            self.columns,
            set() if empty else self.days,
        )

    def calendar_days(self) -> set[tuple[int, int, int]]:
        return set(self.days)

    def hvns_at(self, ts_ns: int) -> list[float]:
        """As-of HVN1/2/3 at timestamp (non-zero only)."""
        if len(self.ts_ns) == 0:
            return []
        i = int(np.searchsorted(self.ts_ns, ts_ns, side="right") - 1)
        if i < 0:
            return []
        out = []
        for a in (self.hvn1, self.hvn2, self.hvn3):
            v = float(a[i])
            if v != 0.0:
                out.append(v)
        return out


def load_irt_csv(path: Path | str, *, tz: ZoneInfo = UK) -> IrtBars:
    path = Path(path)
    with path.open(encoding="utf-8", newline="") as f:
        header = [h.strip() for h in f.readline().rstrip("\n").rstrip("\r").split(",")]
        cols = resolve_columns(header)
        rows_ts: list[int] = []
        rows_close: list[float] = []
        rows_dop: list[float] = []
        rows_dvpoc: list[float] = []
        rows_lvn2: list[float] = []
        rows_lvn3: list[float] = []
        rows_hvn1: list[float] = []
        rows_hvn2: list[float] = []
        rows_hvn3: list[float] = []
        days: set[tuple[int, int, int]] = set()
        for line in f:
            parts = line.rstrip("\n").rstrip("\r").split(",")
            if len(parts) <= cols.close:
                continue
            try:
                ts = _parse_ts_ns(parts[cols.date], tz=tz)
            except ValueError:
                continue
            rows_ts.append(ts)
            rows_close.append(_parse_float(parts[cols.close]))
            rows_dop.append(_parse_float(parts[cols.dop]))
            rows_dvpoc.append(_parse_float(parts[cols.dvpoc2]))
            rows_lvn2.append(_parse_float(parts[cols.lvn2]))
            rows_lvn3.append(_parse_float(parts[cols.lvn3]))
            rows_hvn1.append(_parse_float(parts[cols.hvn1]))
            rows_hvn2.append(_parse_float(parts[cols.hvn2]))
            rows_hvn3.append(_parse_float(parts[cols.hvn3]))
            ds = parts[cols.date].strip().strip('"')[:10]
            try:
                y, m, d = (int(x) for x in ds.split("-"))
                days.add((y, m, d))
            except ValueError:
                pass

    ts = np.asarray(rows_ts, dtype=np.int64)
    order = np.argsort(ts, kind="stable")
    sym = path.stem.replace("@", "").replace("#_ALL", "").replace("#", "")
    return IrtBars(
        symbol=sym,
        ts_ns=ts[order],
        close=np.asarray(rows_close, dtype=np.float64)[order],
        dop=np.asarray(rows_dop, dtype=np.float64)[order],
        dvpoc=np.asarray(rows_dvpoc, dtype=np.float64)[order],
        lvn2=np.asarray(rows_lvn2, dtype=np.float64)[order],
        lvn3=np.asarray(rows_lvn3, dtype=np.float64)[order],
        hvn1=np.asarray(rows_hvn1, dtype=np.float64)[order],
        hvn2=np.asarray(rows_hvn2, dtype=np.float64)[order],
        hvn3=np.asarray(rows_hvn3, dtype=np.float64)[order],
        columns=cols,
        days=days,
    )


def coverage_stats(bars: IrtBars) -> dict:
    n = len(bars.ts_ns)
    if n == 0:
        return {"n": 0}

    def nz(a: np.ndarray) -> int:
        return int(np.sum(a != 0.0))

    return {
        "n": n,
        "first_ts_ns": int(bars.ts_ns[0]),
        "last_ts_ns": int(bars.ts_ns[-1]),
        "dop_nz": nz(bars.dop),
        "dvpoc_nz": nz(bars.dvpoc),
        "lvn2_nz": nz(bars.lvn2),
        "lvn3_nz": nz(bars.lvn3),
        "hvn1_nz": nz(bars.hvn1),
        "hvn2_nz": nz(bars.hvn2),
        "hvn3_nz": nz(bars.hvn3),
        "columns": {
            "dop": bars.columns.dop,
            "dvpoc2": bars.columns.dvpoc2,
            "lvn2": bars.columns.lvn2,
            "lvn3": bars.columns.lvn3,
            "hvn1": bars.columns.hvn1,
            "hvn2": bars.columns.hvn2,
            "hvn3": bars.columns.hvn3,
        },
    }


def session_id_to_ymd(sid: str) -> tuple[int, int, int]:
    """DDMMYY → (y, m, d)."""
    dd, mm, yy = int(sid[0:2]), int(sid[2:4]), int(sid[4:6])
    year = 2000 + yy
    return year, mm, dd


def default_irt_paths(repo_root: Path) -> dict[str, Path]:
    """Prefer continuous `@NQ#` / `@ES#` exports; fall back to front-month names."""
    d = repo_root / "data" / "irt"
    nq_candidates = ("@NQ#_ALL.csv", "NQZ6_ALL.csv", "@NQ#_ALL.CSV")
    es_candidates = ("ESZ6_ALL.csv", "@ES#_ALL.csv", "ESZ6_ALL.CSV")

    def _pick(cands: tuple[str, ...], label: str) -> Path:
        for name in cands:
            p = d / name
            if p.exists():
                return p
        raise FileNotFoundError(
            f"No IRT {label} CSV in {d}; tried {list(cands)}"
        )

    return {"NQ": _pick(nq_candidates, "NQ"), "ES": _pick(es_candidates, "ES")}
