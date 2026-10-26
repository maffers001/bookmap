"""Load developing LVN stacks produced by ``build_lvn_stack``."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

UK = ZoneInfo("Europe/London")


@dataclass
class StackSeries:
    """Per-snapshot valley prices, sorted by time."""

    ts_ns: np.ndarray  # int64
    # Parallel jagged lists — use prices_at()
    _prices: list[list[float]]
    _vols: list[list[float]]

    def prices_asof(self, t_ns: int) -> list[float]:
        if len(self.ts_ns) == 0:
            return []
        i = int(np.searchsorted(self.ts_ns, t_ns, side="right") - 1)
        if i < 0:
            return []
        return list(self._prices[i])

    def nearest(self, t_ns: int, centre: float) -> tuple[str, float, float] | None:
        prices = self.prices_asof(t_ns)
        if not prices:
            return None
        px = min(prices, key=lambda p: abs(p - centre))
        return "STACK", float(px), abs(px - centre)


def load_stack_csv(path: Path) -> StackSeries:
    """Load ``date,time_uk,price,...`` long-format stack file."""
    groups: dict[str, list[tuple[float, float]]] = {}
    order: list[str] = []
    with path.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            key = f"{row['date']} {row['time_uk']}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((float(row["price"]), float(row.get("node_volume") or 0)))
    ts_list: list[int] = []
    prices: list[list[float]] = []
    vols: list[list[float]] = []
    for key in order:
        dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UK)
        ts_list.append(int(dt.timestamp() * 1e9))
        g = sorted(groups[key], key=lambda x: x[0])
        prices.append([p for p, _ in g])
        vols.append([v for _, v in g])
    return StackSeries(
        ts_ns=np.asarray(ts_list, dtype=np.int64),
        _prices=prices,
        _vols=vols,
    )
