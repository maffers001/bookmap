"""How often open standout clusters are hit after the snap — by rank / significance.

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_open_cluster_hits
    .venv\\Scripts\\python.exe -m research.irt_lvn.study_open_cluster_hits --clusters results/open_clusters_oct2026.csv
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import numpy as np

from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed
from research.irt_lvn.events import PTS_PER_TICK
from research.irt_lvn.load_irt import session_id_to_ymd
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")
WINDOW_END = "17:00"


def _hhmm_ns(y: int, m: int, d: int, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":")[:2])
    return int(datetime(y, m, d, hh, mm, tzinfo=UK).timestamp() * 1e9)


def _load_tape(pq_dir: Path, sid: str, role: str, t0: int, t1: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    tr = _prefer_single_feed(_load_trades(pq_dir, sid, role), pq_dir, sid, role)
    if tr.num_rows == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float64), np.array([], dtype=np.float64)
    ts = tr.column("ts_ns").to_numpy().astype(np.int64)
    px = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
    sz = tr.column("size").to_numpy().astype(np.float64)
    o = np.argsort(ts, kind="stable")
    ts, px, sz = ts[o], px[o], sz[o]
    m = (ts >= t0) & (ts < t1)
    return ts[m], px[m], sz[m]


def _hit_stats(
    ts: np.ndarray,
    px: np.ndarray,
    sz: np.ndarray,
    *,
    side: str,
    lo: float,
    hi: float,
    snap_ns: int,
) -> dict[str, Any]:
    """Touch = print inside [lo, hi]; pierce = trade through beyond the band."""
    if len(ts) == 0:
        return {
            "hit": 0, "pierce": 0, "t_hit_s": None, "t_pierce_s": None,
            "vol_in_band": 0.0, "mfe_toward_pts": 0.0, "max_pierce_pts": 0.0,
        }
    in_band = (px >= lo) & (px <= hi)
    if side == "ASK":
        through = px > hi
        toward = px.max() - lo  # how far price climbed toward/through asks from below
        # MFE toward: max of (px - something)? For ask resistance above mid, "toward" = max(px) relative to lo
        # Use: max price reached; pierce depth = max(0, max_px - hi)
        max_px = float(px.max())
        min_px = float(px.min())
        mfe_toward = max(0.0, max_px - lo)  # not ideal; better: progress from first print
        first = float(px[0])
        mfe_toward = max(0.0, max_px - first) if max_px > first else 0.0
        # cleaner: distance closed from open toward the level
        # For ask above: mid was below; progress = max(0, max_px - first) limited interest
        # Use max excursion toward band from below: max(0, max_px - first)
        pierce_depth = max(0.0, max_px - hi)
    else:
        through = px < lo
        first = float(px[0])
        min_px = float(px.min())
        max_px = float(px.max())
        mfe_toward = max(0.0, first - min_px)
        pierce_depth = max(0.0, lo - min_px)

    hit = bool(in_band.any() or through.any())
    pierced = bool(through.any())
    t_hit = None
    t_pierce = None
    if hit:
        # first touch = first in_band or through
        mask = in_band | through
        i = int(np.argmax(mask))
        t_hit = (int(ts[i]) - snap_ns) / 1e9
    if pierced:
        i = int(np.argmax(through))
        t_pierce = (int(ts[i]) - snap_ns) / 1e9
    vol = float(sz[in_band].sum()) if in_band.any() else 0.0
    return {
        "hit": int(hit),
        "pierce": int(pierced),
        "t_hit_s": None if t_hit is None else round(t_hit, 1),
        "t_pierce_s": None if t_pierce is None else round(t_pierce, 1),
        "vol_in_band": round(vol, 1),
        "mfe_toward_pts": round(float(mfe_toward), 2),
        "max_pierce_pts": round(float(pierce_depth), 2),
    }


def _rate(rows: list[dict], key: str = "hit") -> tuple[int, int, float]:
    n = len(rows)
    if n == 0:
        return 0, 0, float("nan")
    k = sum(int(r[key]) for r in rows)
    return k, n, k / n


def _fmt_rate(k: int, n: int, p: float) -> str:
    if n == 0:
        return "n=0"
    return f"{k}/{n} = {p:.1%}"


def run(*, clusters_path: Path, out_dir: Path, window_end: str) -> None:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    clusters = list(csv.DictReader(clusters_path.open(encoding="utf-8")))
    out_dir.mkdir(parents=True, exist_ok=True)

    # Cache tape per (sid, market)
    tape: dict[tuple[str, str], tuple[np.ndarray, np.ndarray, np.ndarray, int]] = {}
    results: list[dict[str, Any]] = []

    for c in clusters:
        sid = c["session_id"]
        role = c["market"]
        y, m, d = session_id_to_ymd(sid)
        snap_ns = _hhmm_ns(y, m, d, c.get("snap_time_uk") or "14:30")
        end_ns = _hhmm_ns(y, m, d, window_end)
        key = (sid, role)
        if key not in tape:
            ts, px, sz = _load_tape(pq_dir, sid, role, snap_ns, end_ns)
            tape[key] = (ts, px, sz, snap_ns)
            print(f"  tape {sid} {role}: {len(ts)} prints", flush=True)
        ts, px, sz, snap_ns = tape[key]

        lo, hi = float(c["price_lo"]), float(c["price_hi"])
        # zero-width cluster: treat as single tick ± 0
        if hi < lo:
            lo, hi = hi, lo
        hs = _hit_stats(ts, px, sz, side=c["side"], lo=lo, hi=hi, snap_ns=snap_ns)
        row = {
            **{k: c[k] for k in c},
            **hs,
            "rank_in_side": int(c["rank_in_side"]),
            "rank_in_month": int(float(c["rank_in_month"])),
            "significance": float(c["significance"]),
            "size": int(float(c["size"])),
            "dist_mid_pts": float(c["dist_mid_pts"]),
        }
        results.append(row)

    # ---- write per-cluster outcomes
    out_csv = out_dir / "cluster_hits.csv"
    fields = [
        "date", "session_id", "market", "side", "role_vs_mid",
        "centre_price", "price_lo", "price_hi", "size", "significance",
        "rank_in_side", "rank_in_month", "dist_mid_pts", "age_stratum",
        "hit", "pierce", "t_hit_s", "t_pierce_s", "vol_in_band",
        "mfe_toward_pts", "max_pierce_pts",
    ]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    # ---- aggregates
    lines: list[str] = []
    def hdr(s: str) -> None:
        lines.append("")
        lines.append(f"## {s}")
        lines.append("")

    k, n, p = _rate(results, "hit")
    kp, np_, pp = _rate(results, "pierce")
    lines.append("# Open cluster hit study — October 2026")
    lines.append("")
    lines.append(f"Source: `{clusters_path.name}` · window snap→{window_end} UK · n={n} clusters.")
    lines.append("")
    lines.append(f"**Overall hit** (print in band or through): {_fmt_rate(k, n, p)}")
    lines.append(f"**Overall pierce** (trade beyond band): {_fmt_rate(kp, np_, pp)}")
    hit_times = [r["t_hit_s"] for r in results if r["hit"] and r["t_hit_s"] is not None]
    if hit_times:
        lines.append(
            f"Time to first hit (hitters only): median {np.median(hit_times):.0f}s · "
            f"p25 {np.percentile(hit_times, 25):.0f}s · p75 {np.percentile(hit_times, 75):.0f}s"
        )

    hdr("By market")
    lines.append("| market | hit | pierce | n |")
    lines.append("|---|---|---|---:|")
    for mkt in ("NQ", "ES"):
        sub = [r for r in results if r["market"] == mkt]
        k, n, p = _rate(sub, "hit")
        kp, _, pp = _rate(sub, "pierce")
        lines.append(f"| {mkt} | {_fmt_rate(k, n, p)} | {_fmt_rate(kp, n, pp)} | {n} |")

    hdr("By side")
    lines.append("| side | hit | pierce | n |")
    lines.append("|---|---|---|---:|")
    for side in ("BID", "ASK"):
        sub = [r for r in results if r["side"] == side]
        k, n, p = _rate(sub, "hit")
        kp, _, pp = _rate(sub, "pierce")
        lines.append(f"| {side} | {_fmt_rate(k, n, p)} | {_fmt_rate(kp, n, pp)} | {n} |")

    hdr("By rank_in_side (1 = highest significance that session/side)")
    lines.append("| rank | hit | pierce | n | median dist_mid | median size |")
    lines.append("|---:|---|---|---:|---:|---:|")
    for rank in sorted({r["rank_in_side"] for r in results}):
        sub = [r for r in results if r["rank_in_side"] == rank]
        k, n, p = _rate(sub, "hit")
        kp, _, pp = _rate(sub, "pierce")
        dist = float(np.median([r["dist_mid_pts"] for r in sub]))
        sz = float(np.median([r["size"] for r in sub]))
        lines.append(
            f"| {rank} | {_fmt_rate(k, n, p)} | {_fmt_rate(kp, n, pp)} | {n} | {dist:.1f} | {sz:.0f} |"
        )

    # Rank 1 vs rest
    r1 = [r for r in results if r["rank_in_side"] == 1]
    r2p = [r for r in results if r["rank_in_side"] >= 2]
    hdr("Rank 1 vs rank ≥2")
    for label, sub in (("rank 1", r1), ("rank ≥2", r2p)):
        k, n, p = _rate(sub, "hit")
        kp, _, pp = _rate(sub, "pierce")
        lines.append(f"- **{label}**: hit {_fmt_rate(k, n, p)} · pierce {_fmt_rate(kp, n, pp)}")

    # Significance terciles (within market, so ES/NQ scales don't mix)
    hdr("By significance tercile (within market)")
    lines.append("| market | tercile | hit | pierce | n | median sig | median dist |")
    lines.append("|---|---|---|---|---:|---:|---:|")
    for mkt in ("NQ", "ES"):
        sub = [r for r in results if r["market"] == mkt]
        sigs = np.asarray([r["significance"] for r in sub], dtype=np.float64)
        q33, q66 = np.percentile(sigs, [33.333, 66.667])
        buckets = [
            ("low", [r for r in sub if r["significance"] <= q33]),
            ("mid", [r for r in sub if q33 < r["significance"] <= q66]),
            ("high", [r for r in sub if r["significance"] > q66]),
        ]
        for name, b in buckets:
            k, n, p = _rate(b, "hit")
            kp, _, pp = _rate(b, "pierce")
            ms = float(np.median([r["significance"] for r in b])) if b else float("nan")
            md = float(np.median([r["dist_mid_pts"] for r in b])) if b else float("nan")
            lines.append(
                f"| {mkt} | {name} | {_fmt_rate(k, n, p)} | {_fmt_rate(kp, n, pp)} | {n} | {ms:.1f} | {md:.1f} |"
            )

    # Distance control: within each dist quartile, does rank 1 hit more?
    hdr("Rank 1 vs ≥2 within distance-to-mid quartiles (distance confound check)")
    dists = np.asarray([r["dist_mid_pts"] for r in results], dtype=np.float64)
    edges = np.percentile(dists, [0, 25, 50, 75, 100])
    lines.append("| dist quartile | rank1 hit | rank≥2 hit | n1 / n2 |")
    lines.append("|---|---|---|---|")
    for i in range(4):
        lo_e, hi_e = edges[i], edges[i + 1]
        # last bin inclusive
        if i < 3:
            band = [r for r in results if lo_e <= r["dist_mid_pts"] < hi_e]
        else:
            band = [r for r in results if lo_e <= r["dist_mid_pts"] <= hi_e]
        a = [r for r in band if r["rank_in_side"] == 1]
        b = [r for r in band if r["rank_in_side"] >= 2]
        ka, na, pa = _rate(a, "hit")
        kb, nb, pb = _rate(b, "hit")
        lines.append(
            f"| [{lo_e:.1f}, {hi_e:.1f}] | {_fmt_rate(ka, na, pa)} | {_fmt_rate(kb, nb, pb)} | {na} / {nb} |"
        )

    hdr("By age_stratum")
    lines.append("| stratum | hit | pierce | n |")
    lines.append("|---|---|---|---:|")
    for st in ("old", "fresh"):
        sub = [r for r in results if r.get("age_stratum") == st]
        k, n, p = _rate(sub, "hit")
        kp, _, pp = _rate(sub, "pierce")
        lines.append(f"| {st} | {_fmt_rate(k, n, p)} | {_fmt_rate(kp, n, pp)} | {n} |")

    # Logistic-style simple association: hit ~ rank (Spearman / means)
    hdr("Association: higher rank → more hits?")
    # rank_in_side: lower number = higher significance. Correlate rank with hit (expect negative if higher-ranked hit more)
    ranks = np.asarray([r["rank_in_side"] for r in results], dtype=np.float64)
    hits = np.asarray([r["hit"] for r in results], dtype=np.float64)
    sigs = np.asarray([r["significance"] for r in results], dtype=np.float64)
    dists_a = np.asarray([r["dist_mid_pts"] for r in results], dtype=np.float64)
    # Spearman via rank correlation
    def _spearman(x, y):
        xr = x.argsort().argsort().astype(np.float64)
        yr = y.argsort().argsort().astype(np.float64)
        if xr.std() == 0 or yr.std() == 0:
            return float("nan")
        return float(np.corrcoef(xr, yr)[0, 1])

    rho_rank = _spearman(ranks, hits)
    rho_sig = _spearman(sigs, hits)
    rho_dist = _spearman(dists_a, hits)
    lines.append(
        f"- Spearman(rank_in_side, hit) = **{rho_rank:.3f}** "
        f"(negative ⇒ higher-ranked / lower rank number hit more often)"
    )
    lines.append(f"- Spearman(significance, hit) = **{rho_sig:.3f}**")
    lines.append(
        f"- Spearman(dist_mid_pts, hit) = **{rho_dist:.3f}** "
        f"(negative ⇒ closer levels hit more — expected confound)"
    )

    # Per market rank association
    for mkt in ("NQ", "ES"):
        sub = [r for r in results if r["market"] == mkt]
        rho = _spearman(
            np.asarray([r["rank_in_side"] for r in sub], dtype=np.float64),
            np.asarray([r["hit"] for r in sub], dtype=np.float64),
        )
        rho_s = _spearman(
            np.asarray([r["significance"] for r in sub], dtype=np.float64),
            np.asarray([r["hit"] for r in sub], dtype=np.float64),
        )
        lines.append(f"- {mkt}: Spearman(rank, hit)={rho:.3f} · Spearman(sig, hit)={rho_s:.3f}")

    hdr("Verdict")
    # Auto short verdict
    k1, n1, p1 = _rate(r1, "hit")
    k2, n2, p2 = _rate(r2p, "hit")
    delta = p1 - p2
    if abs(delta) < 0.05 and abs(rho_sig) < 0.15:
        verdict = (
            f"No clear rank effect: rank-1 hit {p1:.1%} vs rank≥2 {p2:.1%} "
            f"(Δ={delta:+.1%}). Distance-to-mid is the stronger correlate of being hit."
        )
    elif p1 > p2 + 0.05:
        verdict = (
            f"Higher-ranked clusters are hit **more** often: rank-1 {p1:.1%} vs rank≥2 {p2:.1%} "
            f"(Δ={delta:+.1%}). Check distance-quartile table — if the gap vanishes within "
            f"distance bands, rank is mostly a distance proxy."
        )
    else:
        verdict = (
            f"Higher-ranked clusters are hit **less** often: rank-1 {p1:.1%} vs rank≥2 {p2:.1%} "
            f"(Δ={delta:+.1%}). Larger/older standouts may sit farther from mid or hold better "
            f"(attract without being traded through) — see pierce rates and distance table."
        )
    lines.append(verdict)
    lines.append("")
    lines.append(f"Per-cluster outcomes: `{out_csv.name}`")

    summary = out_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nWrote {summary}", flush=True)
    print(f"Wrote {out_csv}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--clusters",
        default=str(ROOT / "results" / "open_clusters_oct2026.csv"),
    )
    ap.add_argument(
        "--out",
        default=str(ROOT / "results" / "OPEN_CLUSTER_HITS_OCT2026"),
    )
    ap.add_argument("--until", default=WINDOW_END, help="UK session end HH:MM")
    args = ap.parse_args()
    run(clusters_path=Path(args.clusters), out_dir=Path(args.out), window_end=args.until)


if __name__ == "__main__":
    main()
