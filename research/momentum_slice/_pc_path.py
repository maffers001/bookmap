"""Compute post-confirm path features for every candidate (train + holdout).

For each candidate, from NQ trades:
  peak_px / t_peak      extreme in slice direction over [ignition, confirm+5s]
  pb{D}                 max pullback from peak by confirm+D (D=5,10,20,30,45,60)
  entry{D}              last price at confirm+D
  beyond_peak{D}        side*(entry{D}-peak)  (>0 = reclaimed/exceeded peak)
  trough / second push  trough in [t_peak, confirm+20s]; then reclaim in
                        (t_trough, confirm+30s]: reclaim_frac, push_exceeds_peak,
                        push_vol_ratio (vol after trough / burst nq_vol)
  fade_ret{D}_60        fade-direction return 60s after confirm+D entry
Outputs pc_review/path_features_{train,holdout}.csv
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.rules import enrich, f

OUT = Path("results/MOMENTUM_SLICE/pc_review")
DELAYS = (5, 10, 20, 30, 45, 60)


def compute(rows, pairs):
    out = []
    for r in rows:
        sid = r["session"]
        pair = pairs.get(sid)
        if pair is None:
            continue
        nq = pair.nq
        ts = nq.column("ts_ns").to_numpy().astype(np.int64)
        px = nq.column("price").to_numpy().astype(np.float64) * 0.25
        sz = nq.column("size").to_numpy().astype(np.float64)
        order = np.argsort(ts, kind="stable")
        ts, px, sz = ts[order], px[order], sz[order]
        s = int(float(r["side_int"]))
        ign = int(float(r["ignition_ns"]))
        conf = int(float(r["confirm_ns"]))

        def idx(t):
            return int(np.searchsorted(ts, t, side="left"))

        def last_idx(t):
            return int(np.searchsorted(ts, t, side="right")) - 1

        # peak over [ignition, confirm+5s]
        a, b = idx(ign), last_idx(conf + 5_000_000_000)
        if b <= a:
            continue
        seg_px = px[a:b + 1]
        seg_signed = s * seg_px
        k = int(np.argmax(seg_signed))
        peak_px = float(seg_px[k])
        t_peak = int(ts[a + k])

        burst_vol = f(r, "nq_vol") or 0.0
        rec = dict(session=sid, side=r["side"], side_int=s,
                   ignition_bst=r["ignition_bst"], ignition_ns=ign, confirm_ns=conf,
                   peak_px=peak_px, t_peak_ns=t_peak,
                   tod=r.get("tod"), trend_5m_pts=f(r, "trend_5m_pts"),
                   nq_post5_slow_ratio=f(r, "nq_post5_slow_ratio"),
                   nq_post5_net_pts=f(r, "nq_post5_net_pts"))
        # trough in [t_peak, confirm+20s]
        ta, tb = idx(t_peak), last_idx(conf + 20_000_000_000)
        if tb > ta:
            w = s * px[ta:tb + 1]
            tk = int(np.argmin(w))
            trough_px = float(px[ta + tk])
            t_trough = int(ts[ta + tk])
            pb20_at_trough = s * (peak_px - trough_px)
        else:
            trough_px, t_trough, pb20_at_trough = peak_px, t_peak, 0.0
        rec["trough_px"] = trough_px
        rec["t_trough_ns"] = t_trough
        rec["pb_to_trough20"] = round(pb20_at_trough, 2)
        # second push: (t_trough, confirm+30s]
        pa, pb_ = idx(t_trough + 1), last_idx(conf + 30_000_000_000)
        if pb_ > pa and pb20_at_trough > 0:
            w = px[pa:pb_ + 1]
            mk = int(np.argmax(s * w))
            push_px = float(w[mk])
            rec["push_reclaim_frac"] = round(s * (push_px - trough_px) / pb20_at_trough, 3)
            rec["push_beyond_peak_pts"] = round(s * (push_px - peak_px), 2)
            rec["push_fails"] = 1 if s * (push_px - peak_px) <= 1.0 else 0
            # volume after trough up to push extreme
            rec["push_vol_ratio"] = round(float(sz[pa:pa + mk + 1].sum()) / burst_vol, 3) if burst_vol else None
            rec["push_n_trades"] = int(mk + 1)
        else:
            rec["push_reclaim_frac"] = None
            rec["push_beyond_peak_pts"] = None
            rec["push_fails"] = None
            rec["push_vol_ratio"] = None
            rec["push_n_trades"] = 0
        # Causal second-push for +20s entry: only data up to confirm+20s.
        pa2, pb2 = idx(t_trough + 1), last_idx(conf + 20_000_000_000)
        if pb_ > pa and pb2 > pa2 and pb20_at_trough > 0:
            w2 = px[pa2:pb2 + 1]
            mk2 = int(np.argmax(s * w2))
            push_px2 = float(w2[mk2])
            rec["push20_reclaim_frac"] = round(s * (push_px2 - trough_px) / pb20_at_trough, 3)
            rec["push20_beyond_peak_pts"] = round(s * (push_px2 - peak_px), 2)
            rec["push20_fails"] = 1 if s * (push_px2 - peak_px) <= 1.0 else 0
            rec["push20_vol_ratio"] = round(float(sz[pa2:pa2 + mk2 + 1].sum()) / burst_vol, 3) if burst_vol else None
        else:
            rec["push20_reclaim_frac"] = None
            rec["push20_beyond_peak_pts"] = None
            rec["push20_fails"] = None
            rec["push20_vol_ratio"] = None

        for d in DELAYS:
            t_end = conf + d * 1_000_000_000
            wa, wb = idx(t_peak), last_idx(t_end)
            if wb > wa:
                worst = float(np.min(s * px[wa:wb + 1]))
                rec[f"pb{d}"] = round(peak_px - s * worst if False else s * peak_px - worst, 2)
                # s*peak - min(s*px) = pullback
                rec[f"pb{d}"] = round(float(s * peak_px - np.min(s * px[wa:wb + 1])), 2)
            else:
                rec[f"pb{d}"] = 0.0
            ei = last_idx(t_end)
            entry = float(px[ei]) if ei >= 0 else peak_px
            rec[f"entry{d}"] = entry
            rec[f"beyond_peak{d}"] = round(s * (entry - peak_px), 2)
            # fade 60s return from this entry
            fi = last_idx(t_end + 60_000_000_000)
            if fi >= 0 and ei >= 0 and fi > ei:
                rec[f"fade_ret{d}_60"] = round(-s * (float(px[fi]) - entry), 2)
            else:
                rec[f"fade_ret{d}_60"] = None
        out.append(rec)
    return out


def main():
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    for label, feats in (("train", "results/MOMENTUM_SLICE/features/features.csv"),
                         ("holdout", "results/MOMENTUM_SLICE/holdout/features.csv")):
        rows = [enrich(r) for r in csv.DictReader(open(feats, encoding="utf-8"))]
        sids = sorted({r["session"] for r in rows})
        print(f"{label}: loading {len(sids)} sessions", flush=True)
        pairs = {}
        for sid in sids:
            p = load_pair(pq, sid)
            if p is not None:
                pairs[sid] = p
        out = compute(rows, pairs)
        path = OUT / f"path_features_{label}.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(out[0].keys()))
            w.writeheader(); w.writerows(out)
        print(f"{label}: wrote {len(out)} -> {path}")


if __name__ == "__main__":
    main()
