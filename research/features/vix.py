"""Causal VIX (FRED VIXCLS) for session-level MAT gates (H65 / EXP-0074).

Prior close only: for session calendar day D, use the most recent VIXCLS
print strictly before D. Trailing tercile uses the prior ``lookback`` VIX
closes before D (no same-day leak).
"""
from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa

FRED_VIXCLS_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"
DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data" / "external" / "vixcls.csv"


@dataclass
class VixConfig:
    lookback_closes: int = 60  # trading days of VIX history for tercile
    cache_path: Path = DEFAULT_CACHE
    refresh_if_stale_days: int = 2


def sid_to_date(sid: str) -> date:
    """DDMMYY → calendar date (assume 20YY)."""
    return date(2000 + int(sid[4:6]), int(sid[2:4]), int(sid[0:2]))


def fetch_vixcls(
    *,
    cache_path: Path | None = None,
    force: bool = False,
    url: str = FRED_VIXCLS_URL,
) -> dict[date, float]:
    """Load VIXCLS daily closes; cache under data/external/."""
    path = Path(cache_path or DEFAULT_CACHE)
    path.parent.mkdir(parents=True, exist_ok=True)
    use_cache = path.exists() and not force
    if use_cache:
        age = datetime.now().timestamp() - path.stat().st_mtime
        if age > VixConfig().refresh_if_stale_days * 86400:
            use_cache = False
    if not use_cache:
        with urllib.request.urlopen(url, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        path.write_text(raw, encoding="utf-8")
    else:
        raw = path.read_text(encoding="utf-8")

    out: dict[date, float] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("DATE"):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        ds, vs = parts[0].strip(), parts[1].strip()
        if not vs or vs.upper() == ".":
            continue
        try:
            d = date.fromisoformat(ds)
            out[d] = float(vs)
        except ValueError:
            continue
    return out


def prior_close(vix: dict[date, float], d: date) -> float:
    """Most recent VIXCLS close strictly before calendar day ``d``."""
    best: date | None = None
    for k in vix:
        if k < d and (best is None or k > best):
            best = k
    # linear scan is fine for ~10k rows; prefer walking back for speed
    if best is None:
        # walk back up to 14 calendar days
        for i in range(1, 15):
            cand = d - timedelta(days=i)
            if cand in vix:
                return float(vix[cand])
        return float("nan")
    return float(vix[best])


def prior_close_fast(sorted_dates: list[date], values: np.ndarray, d: date) -> float:
    """Binary-search prior close given sorted date keys and aligned values."""
    import bisect
    i = bisect.bisect_left(sorted_dates, d) - 1
    if i < 0:
        return float("nan")
    return float(values[i])


def trailing_terciles(
    sorted_dates: list[date],
    values: np.ndarray,
    d: date,
    *,
    lookback: int = 60,
) -> tuple[float, float, float]:
    """Return (prior_close, p33.3, p66.7) of prior ``lookback`` closes."""
    import bisect
    i = bisect.bisect_left(sorted_dates, d) - 1
    if i < 0:
        return float("nan"), float("nan"), float("nan")
    prior = float(values[i])
    lo = max(0, i - lookback + 1)
    window = values[lo : i + 1]
    if len(window) < max(20, lookback // 3):
        return prior, float("nan"), float("nan")
    p33 = float(np.percentile(window, 100.0 / 3.0))
    p66 = float(np.percentile(window, 100.0 * 2.0 / 3.0))
    return prior, p33, p66


def session_vix_table(
    sessions: list[str],
    *,
    cfg: VixConfig | None = None,
    force_fetch: bool = False,
) -> dict[str, dict[str, float]]:
    """Per-sid: vix_prior, vix_p33/p66, vix_hi_tercile (1/0)."""
    cfg = cfg or VixConfig()
    series = fetch_vixcls(cache_path=cfg.cache_path, force=force_fetch)
    dates = sorted(series.keys())
    vals = np.asarray([series[d] for d in dates], dtype=np.float64)
    out: dict[str, dict[str, float]] = {}
    for sid in sessions:
        d = sid_to_date(sid)
        prior, p33, p66 = trailing_terciles(dates, vals, d, lookback=cfg.lookback_closes)
        out[sid] = {
            "vix_prior": prior,
            "vix_p33": p33,
            "vix_p66": p66,
            "vix_hi_tercile": (
                1.0 if (np.isfinite(prior) and np.isfinite(p66) and prior >= p66) else 0.0
            ),
        }
    return out


def annotate_vix(ev: pa.Table, feat: dict[str, float]) -> pa.Table:
    """Stamp constant session-level VIX columns onto an event table."""
    n = ev.num_rows
    if n == 0:
        return ev
    prior = float(feat.get("vix_prior", float("nan")))
    p33 = float(feat.get("vix_p33", float("nan")))
    p66 = float(feat.get("vix_p66", float("nan")))
    hi = float(feat.get("vix_hi_tercile", 0.0))
    out = ev
    for name, arr in (
        ("vix_prior", np.full(n, prior, dtype=np.float64)),
        ("vix_p33", np.full(n, p33, dtype=np.float64)),
        ("vix_p66", np.full(n, p66, dtype=np.float64)),
        ("vix_hi_tercile", np.full(n, hi, dtype=np.float64)),
    ):
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.float64()))
    return out


def label_vix_abs(x: float) -> str:
    if not np.isfinite(x):
        return "vix_na"
    if x < 15:
        return "vix_lt_15"
    if x < 18:
        return "vix_15_18"
    if x < 20:
        return "vix_18_20"
    if x < 25:
        return "vix_20_25"
    return "vix_ge_25"


def label_vix_tercile(prior: float, p33: float, p66: float) -> str:
    if not np.isfinite(prior) or not np.isfinite(p33) or not np.isfinite(p66):
        return "vix_terc_na"
    if prior >= p66:
        return "vix_terc_hi"
    if prior < p33:
        return "vix_terc_lo"
    return "vix_terc_mid"
