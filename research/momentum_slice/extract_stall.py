"""Post-confirm stall features (for pitch-and-catch): tape slowing, no
progress, weak pullback volume in [confirm, confirm+5s] vs the burst window.

  .venv\\Scripts\\python.exe -m research.momentum_slice.extract_stall
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.detector import _arrays

P = Path("results/MOMENTUM_SLICE/features/features.csv")


def stall(trades, side: int, ign: int, conf: int) -> dict:
    ts, px, sz, buy = _arrays(trades)
    px = px * 0.25

    def win(t0, t1):
        lo = int(np.searchsorted(ts, t0)); hi = int(np.searchsorted(ts, t1))
        return px[lo:hi], sz[lo:hi], buy[lo:hi]

    bp, bs, _ = win(ign, conf)
    pp, ps, pb = win(conf, conf + 5_000_000_000)
    out = {}
    burst_tps = len(bp) / 2.0 if len(bp) else 0.0
    if len(pp):
        out["post5_tps"] = len(pp) / 5.0
        out["post5_slow_ratio"] = (len(pp) / 5.0) / burst_tps if burst_tps else float("nan")
        out["post5_net_pts"] = float(side * (pp[-1] - pp[0]))
        out["post5_range_pts"] = float(pp.max() - pp.min())
        signed = np.where(pb, ps, -ps)
        delta = float(signed.sum())
        out["post5_side_delta_ratio"] = float(side * delta / ps.sum()) if ps.sum() else 0.0
        out["post5_vol_ratio"] = float(ps.sum() / bs.sum()) if len(bs) and bs.sum() else float("nan")
    return out


def main():
    with open(P, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    cfg = load_config(); pq_dir = Path(cfg["parquet_dir"])
    by_session: dict[str, list] = {}
    for r in rows:
        by_session.setdefault(r["session"], []).append(r)
    for sid, evs in sorted(by_session.items()):
        pair = load_pair(pq_dir, sid)
        if pair is None:
            continue
        for r in evs:
            side = int(float(r["side_int"]))
            ign, conf = int(float(r["ignition_ns"])), int(float(r["confirm_ns"]))
            for k, v in stall(pair.nq, side, ign, conf).items():
                r[f"nq_{k}"] = round(v, 3) if isinstance(v, float) else v
            # pullback as fraction of peak-past (scale-free vs absolute pts)
            for stamp in ("confirm", "c5s"):
                peak = r.get(f"nq_peak_past_{stamp}_pts")
                pb = r.get(f"nq_pullback_{stamp}_pts")
                if peak not in (None, "", "nan") and pb not in (None, "", "nan") and float(peak) > 0:
                    r[f"nq_pullback_ratio_{stamp}"] = round(float(pb) / float(peak), 3)
        print(sid, "done", flush=True)
    fields, seen = [], set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k); fields.append(k)
    tmp = P.with_suffix(".tmp.csv")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    tmp.replace(P)
    print(f"merged stall into {P} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
