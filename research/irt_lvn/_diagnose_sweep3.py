"""Bar-derived VAP constructions across snapshots (IRT may distribute bar volume).

Constructions: uniform over [L,H], OHLC-path weighted, close-only, bar-TPO.
Windows: ES cal90 S15, NQ cal30 S100. Score exact/1pt set hits of 6 nodes.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
SNAPS = [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
         ("2026-10-13", "14:30"), ("2026-10-20", "14:30")]
CFG = {"ES": (90, 15, "ESZ6_ALL.csv"), "NQ": (30, 100, "@NQ#_ALL.csv")}


def load_bars(market: str):
    fn = CFG[market][2]
    bars = []
    with (ROOT / "data" / "irt" / fn).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                bars.append((row["Date"].strip(),
                             float(row["Open"]), float(row["High"]),
                             float(row["Low"]), float(row["Close"]),
                             float(row["Volume"])))
            except (ValueError, KeyError):
                continue
    return bars


def build_vaps(bars, start_s: str, end_s: str):
    uniform: dict[int, float] = defaultdict(float)
    path: dict[int, float] = defaultdict(float)
    close_v: dict[int, float] = defaultdict(float)
    tpo: dict[int, float] = defaultdict(float)
    for d, o, h, l, c, v in bars:
        if d < start_s or d > end_s or v <= 0 or h <= 0:
            continue
        ro, rh, rl, rc = (int(round(x / TICK)) for x in (o, h, l, c))
        share = v / (rh - rl + 1)
        for r in range(rl, rh + 1):
            uniform[r] += share
            tpo[r] += 1.0
        close_v[rc] += v
        # OHLC path: down-up or up-down by bar direction
        if c >= o:
            legs = [(ro, rl), (rl, rh), (rh, rc)]
        else:
            legs = [(ro, rh), (rh, rl), (rl, rc)]
        seq: list[int] = []
        for a, b in legs:
            step = 1 if b >= a else -1
            seq.extend(range(a, b + step, step))
        if seq:
            w = v / len(seq)
            for r in seq:
                path[r] += w
    return {"uniform": dict(uniform), "path": dict(path),
            "close": dict(close_v), "tpo_bar": dict(tpo)}


def nodes(vap, sens):
    rows = sorted(r for r, v in vap.items() if v > 0)
    if len(rows) < 2 * sens + 1:
        return [], []
    lo, hi = rows[0], rows[-1]
    valleys, peaks = [], []
    for r in rows:
        if r - lo < sens or hi - r < sens:
            continue
        v = vap[r]
        if v <= 0:
            continue
        if all(v < vap.get(r + k, 0.0) and v < vap.get(r - k, 0.0) for k in range(1, sens + 1)):
            valleys.append(r * TICK)
        if all(v > vap.get(r + k, 0.0) and v > vap.get(r - k, 0.0) for k in range(1, sens + 1)):
            peaks.append(r * TICK)
    return valleys, peaks


def score(act, pred, tol):
    if not pred:
        return 0
    return sum(1 for a in act if min(abs(a - p) for p in pred) <= tol + 1e-9)


def main():
    for market, (win, sens, _) in CFG.items():
        bars = load_bars(market)
        tot = defaultdict(lambda: [0, 0])
        for day, hhmm in SNAPS:
            actual = load_all_snapshot(ROOT / "data" / "irt" / CFG[market][2], day, hhmm)
            if actual is None:
                continue
            end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
            start_s = (end - timedelta(days=win)).strftime("%Y-%m-%d %H:%M")
            end_s = f"{day} {hhmm}"
            act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
            act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
            vaps = build_vaps(bars, start_s, end_s)
            for name, vap in vaps.items():
                v, p = nodes(vap, sens)
                vpoc = max(vap, key=lambda r: vap[r]) * TICK if vap else 0.0
                e = score(act_l, v, TICK) + score(act_h, p, TICK)
                o = score(act_l, v, 1.0) + score(act_h, p, 1.0)
                tot[name][0] += e
                tot[name][1] += o
                print(f"{market} {day} {name:10} exact={e}/6 1pt={o}/6 VPOC={vpoc:.2f} nV={len(v)} nP={len(p)}")
        print(f"== {market} totals (24 nodes): " +
              ", ".join(f"{k} exact={v[0]} 1pt={v[1]}" for k, v in tot.items()) + "\n")


if __name__ == "__main__":
    main()
