"""Distribution/bin/smooth variants, tol-0 primary metric.

ES: bar VAP from ALL bars with distribution variants; anchored 2026-08-07.
NQ: tick VAP trailing 30d with smoothing/binning transforms.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import numpy as np

from research.irt_lvn._diagnose_grid import MarketData, partial_day, score
from research.irt_lvn._diagnose_grid2 import near3
from research.irt_lvn.lvn_profile import load_all_snapshot

ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25
SNAPS = [("2026-10-07", "14:30"), ("2026-10-09", "14:30"),
         ("2026-10-13", "14:30"), ("2026-10-20", "14:30")]


def extrema_units(arr, sens):
    n = len(arr)
    is_v = np.ones(n, bool); is_p = np.ones(n, bool)
    for k in range(1, sens + 1):
        up = np.zeros(n); up[:-k] = arr[k:]
        dn = np.zeros(n); dn[k:] = arr[:-k]
        is_v &= (arr < up) & (arr < dn)
        is_p &= (arr > up) & (arr > dn)
        if not is_v.any() and not is_p.any():
            break
    is_v[:sens] = False; is_v[n - sens:] = False
    is_p[:sens] = False; is_p[n - sens:] = False
    is_v[arr <= 0] = False; is_p[arr <= 0] = False
    return np.flatnonzero(is_v), np.flatnonzero(is_p)


def to_prices(idxs, lo, unit=1, off=0):
    return [float((lo + i * unit + off) * TICK) for i in idxs]


def build_bar_vap(market, start_s, end_s, mode):
    fn = "ESZ6_ALL.csv" if market == "ES" else "@NQ#_ALL.csv"
    vap: dict[int, float] = defaultdict(float)
    with (ROOT / "data" / "irt" / fn).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = row["Date"].strip()
            if d < start_s or d[:16] > end_s:
                continue
            try:
                o, h, l, c, v = (float(row["Open"]), float(row["High"]), float(row["Low"]),
                                 float(row["Close"]), float(row["Volume"]))
            except ValueError:
                continue
            if v <= 0 or h <= 0:
                continue
            ro, rh, rl, rc = (int(round(x / TICK)) for x in (o, h, l, c))
            if mode == "uniform_incl":
                share = v / (rh - rl + 1)
                for r in range(rl, rh + 1):
                    vap[r] += share
            elif mode == "uniform_excl":
                if rh == rl:
                    vap[rl] += v
                else:
                    share = v / (rh - rl)
                    for r in range(rl, rh):
                        vap[r] += share
            elif mode == "trapezoid":
                # continuous uniform on [L,H] integrated over tick-centred cells
                if rh == rl:
                    vap[rl] += v
                else:
                    share = v / (rh - rl)
                    for r in range(rl, rh + 1):
                        vap[r] += share * (0.5 if r in (rl, rh) else 1.0)
            elif mode == "path":
                if c >= o:
                    legs = [(ro, rl), (rl, rh), (rh, rc)]
                else:
                    legs = [(ro, rh), (rh, rl), (rl, rc)]
                seq = []
                for a, b in legs:
                    step = 1 if b >= a else -1
                    seq.extend(range(a, b + step, step))
                w = v / len(seq)
                for r in seq:
                    vap[r] += w
    return dict(vap)


def dense(vap):
    rows = sorted(r for r, v in vap.items() if v > 0)
    lo, hi = rows[0], rows[-1]
    arr = np.zeros(hi - lo + 1)
    for r, v in vap.items():
        arr[r - lo] = v
    return arr, lo


def eval_nodes(act_l, act_h, close, v, p):
    e0 = score(act_l, near3(v, close), 0.0) + score(act_h, near3(p, close), 0.0)
    e25 = score(act_l, near3(v, close), TICK) + score(act_h, near3(p, close), TICK)
    return e0, e25


def es_variants():
    print("\n### ES bar-distribution variants, anchor 2026-08-07, S=15 raw unless noted")
    results = defaultdict(lambda: [0, 0])
    for day, hhmm in SNAPS:
        actual = load_all_snapshot(ROOT / "data" / "irt" / "ESZ6_ALL.csv", day, hhmm)
        act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
        act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
        for mode in ("uniform_incl", "uniform_excl", "trapezoid", "path"):
            vap = build_bar_vap("ES", "2026-08-07 00:00", f"{day} {hhmm}", mode)
            arr, lo = dense(vap)
            vi, pi = extrema_units(arr, 15)
            e0, e25 = eval_nodes(act_l, act_h, actual["Close"],
                                 to_prices(vi, lo), to_prices(pi, lo))
            results[mode][0] += e0; results[mode][1] += e25
            # binned variants on this VAP
            for b, sb in ((2, 7), (2, 15), (4, 4), (4, 15)):
                n = (len(arr) // b) * b
                ba = arr[:n].reshape(-1, b).sum(axis=1)
                bvi, bpi = extrema_units(ba, sb)
                for rep, off in (("edge", 0), ("ctr", b // 2)):
                    e0b, e25b = eval_nodes(act_l, act_h, actual["Close"],
                                           to_prices(bvi, lo, b, off), to_prices(bpi, lo, b, off))
                    key = f"{mode}/bin{b}/S{sb}/{rep}"
                    results[key][0] += e0b; results[key][1] += e25b
    for k, (e0, e25) in sorted(results.items(), key=lambda kv: -kv[1][0]):
        print(f"  {k:28} tol0={e0:2d}/24 tol.25={e25:2d}/24")


def nq_variants():
    print("\n### NQ tick trailing30 transforms")
    md = MarketData("NQ", "tick")
    results = defaultdict(lambda: [0, 0])
    for day, hhmm in SNAPS:
        actual = load_all_snapshot(ROOT / "data" / "irt" / "@NQ#_ALL.csv", day, hhmm)
        end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
        part = partial_day("NQ", end, "tick")
        arr = md.vap_window(end.date(), 30, part)
        act_l = [actual[k] for k in ("LVN1", "LVN2", "LVN3")]
        act_h = [actual[k] for k in ("HVN1", "HVN2", "HVN3")]
        transforms = {"raw/S100": (arr, 100, 1, 0)}
        for w in (3, 5, 9, 17, 33, 65):
            sm = np.convolve(arr, np.ones(w) / w, mode="same")
            transforms[f"smooth{w}/S100"] = (sm, 100, 1, 0)
            transforms[f"smooth{w}/S50"] = (sm, 50, 1, 0)
        for b, sb in ((2, 50), (4, 25)):
            n = (len(arr) // b) * b
            ba = arr[:n].reshape(-1, b).sum(axis=1)
            transforms[f"bin{b}/S{sb}/edge"] = (ba, sb, b, 0)
            transforms[f"bin{b}/S{sb}/ctr"] = (ba, sb, b, b // 2)
        for name, (a2, sens, unit, off) in transforms.items():
            vi, pi = extrema_units(a2, sens)
            e0, e25 = eval_nodes(act_l, act_h, actual["Close"],
                                 to_prices(vi, md.lo, unit, off),
                                 to_prices(pi, md.lo, unit, off))
            results[name][0] += e0; results[name][1] += e25
    for k, (e0, e25) in sorted(results.items(), key=lambda kv: -kv[1][0]):
        print(f"  {k:22} tol0={e0:2d}/24 tol.25={e25:2d}/24")


if __name__ == "__main__":
    es_variants()
    nq_variants()
