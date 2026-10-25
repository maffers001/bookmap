"""Conviction checks for frozen ES/NQ ∩ LVN first-touch fades.

1. Ablations: both (outer confluence), cluster-only, LVN-only, far-from-LVN,
   random-shifted levels.
2. Persistence: require resting size still present in a band around the level
   shortly before fill.
3. Robustness: drop top-3 winners; day-clustered bootstrap CI on net.

Frozen configs (do not retune):
  ES: outer · LVN≤10 · outside+2 · SL8/TP24 · min-open 5
  NQ: band12 · size≥80 · outer · LVN≤10 · outside+8 · SL40/TP64 · min-open 5

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_lvn_conviction
    .venv\\Scripts\\python.exe -m research.irt_lvn.study_lvn_conviction --book ES
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

import numpy as np

from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed
from research.irt_lvn.events import PTS_PER_TICK
from research.irt_lvn.load_irt import session_id_to_ymd
from research.irt_lvn.run_hold_break_of import load_depth_window_with_book
from research.irt_lvn.study_es_lvn_bracket_8_24 import (
    MONTHS,
    _anchor_level,
    _fade,
    _hhmm_ns,
    _md_table,
    _open_mid,
)
from research.irt_lvn.study_es_lvn_vol_brackets import _pack
from research.run_experiment import _load_trades

RNG_SEED = 42
N_BOOT = 5_000

ES_ANN = MONTHS
NQ_ANN = {
    mo: ROOT / "results" / f"OPEN_CLUSTER_LVN_CONFLUENCE_NQ_BAND12_{mo}" / "clusters_with_lvn.csv"
    for mo in MONTHS
}

BOOKS: dict[str, dict[str, Any]] = {
    "ES": {
        "market": "ES",
        "ann": ES_ANN,
        "near_pts": 10.0,
        "offset": 2.0,
        "sl": 8.0,
        "tp": 24.0,
        "min_open_dist": 5.0,
        "min_size": 0.0,
        "persist_band_pts": 2.0,
        "persist_frac": 0.50,
        "commission": 0.125,
        "shift_lo": 8.0,
        "shift_hi": 25.0,
    },
    "NQ": {
        "market": "NQ",
        "ann": NQ_ANN,
        "near_pts": 10.0,
        "offset": 8.0,
        "sl": 40.0,
        "tp": 64.0,
        "min_open_dist": 5.0,
        "min_size": 80.0,
        "persist_band_pts": 4.0,
        "persist_frac": 0.50,
        "commission": 0.25,
        "shift_lo": 20.0,
        "shift_hi": 60.0,
    },
}


def _load_levels(
    ann: dict[str, Path],
    *,
    market: str,
    near_pts: float,
    min_size: float,
    confluence: bool | None,
) -> list[dict]:
    """Load levels. confluence True=near, False=far, None=ignore flag."""
    key = f"confluence_{near_pts:g}"
    out: list[dict] = []
    for mo, path in ann.items():
        if not path.exists():
            continue
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        for r in rows:
            if r["market"] != market:
                continue
            size = float(r["size"]) if r.get("size") not in (None, "") else 0.0
            if size < min_size:
                continue
            flag = r.get(key)
            if confluence is True and str(flag) != "1":
                continue
            if confluence is False and str(flag) != "0":
                continue
            out.append({
                "date": r["date"],
                "session_id": r["session_id"],
                "side": r["side"],
                "centre": float(r["centre_price"]),
                "lvn_px": float(r["lvn_px"]) if r.get("lvn_px") not in (None, "") else None,
                "lvn_dist": float(r["lvn_dist"]) if r.get("lvn_dist") not in (None, "") else None,
                "size": size,
                "month": mo,
                "confluence": str(flag) == "1",
            })
    return out


def _level_for_arm(lv: dict, *, arm: str, rng: np.random.Generator, cfg: dict) -> tuple[float, str] | None:
    centre = float(lv["centre"])
    lvn = lv.get("lvn_px")
    if arm == "both":
        return _anchor_level(lv, level_mode="outer")
    if arm == "cluster":
        return centre, "cluster"
    if arm == "lvn":
        if lvn is None:
            return None
        return float(lvn), "lvn"
    if arm == "far_cluster":
        return centre, "cluster"
    if arm == "shift":
        # Push level further from mid in the defensive direction, or random side.
        shift = float(rng.uniform(cfg["shift_lo"], cfg["shift_hi"]))
        if lv["side"] == "BID":
            return centre - shift, "shift"
        return centre + shift, "shift"
    raise ValueError(arm)


def _simulate_arm(
    levels: list[dict],
    *,
    cfg: dict,
    arm: str,
    pq_dir: Path,
    entry_until: str,
    eos: str,
    seed: int,
) -> list[dict[str, Any]]:
    market = cfg["market"]
    rng = np.random.default_rng(seed)
    exit_slip = PTS_PER_TICK
    commission = float(cfg["commission"])
    offset = float(cfg["offset"])
    sl = float(cfg["sl"])
    tp = float(cfg["tp"])
    min_open = float(cfg["min_open_dist"])

    sess_ids = sorted({lv["session_id"] for lv in levels})
    tapes: dict[str, tuple] = {}
    for sid in sess_ids:
        y, m, d = session_id_to_ymd(sid)
        snap_ns = _hhmm_ns(y, m, d, "14:30")
        entry_until_ns = _hhmm_ns(y, m, d, entry_until)
        eos_ns = _hhmm_ns(y, m, d, eos)
        tr = _prefer_single_feed(_load_trades(pq_dir, sid, market), pq_dir, sid, market)
        ts = tr.column("ts_ns").to_numpy().astype(np.int64)
        px = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
        o = np.argsort(ts, kind="stable")
        ts, px = ts[o], px[o]
        open_mid = _open_mid(ts, px, snap_ns)
        msk = (ts >= snap_ns) & (ts < eos_ns)
        tapes[sid] = (ts[msk], px[msk], snap_ns, entry_until_ns, open_mid)

    trades: list[dict[str, Any]] = []
    for lv in levels:
        got = _level_for_arm(lv, arm=arm, rng=rng, cfg=cfg)
        if got is None:
            continue
        level_px, level_src = got
        ts, px, snap_ns, entry_until_ns, open_mid = tapes[lv["session_id"]]
        if open_mid is None:
            continue
        dist_open = abs(level_px - open_mid)
        if dist_open < min_open:
            continue
        entry = level_px - offset if lv["side"] == "BID" else level_px + offset
        trd = _fade(
            ts, px, side=lv["side"], entry=entry, tp=tp, sl=sl,
            snap_ns=snap_ns, entry_until_ns=entry_until_ns,
            exit_slip=exit_slip, commission=commission,
        )
        if trd is None:
            continue
        # absolute fill time from entry_ts string is available; also keep ns
        y, m, d = session_id_to_ymd(lv["session_id"])
        # recover fill ns from t_entry_s
        fill_ns = int(snap_ns + trd["t_entry_s"] * 1e9)
        trades.append({
            **trd,
            "date": lv["date"],
            "session_id": lv["session_id"],
            "market": market,
            "side": lv["side"],
            "centre": lv["centre"],
            "size": lv.get("size"),
            "lvn_px": lv.get("lvn_px"),
            "level_px": level_px,
            "level_src": level_src,
            "arm": arm,
            "open_mid": open_mid,
            "dist_open": dist_open,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "fill_ns": fill_ns,
            "snap_size": float(lv.get("size") or 0.0),
            "month": lv["month"],
        })
    return trades


def _band_size(book: dict[int, int], centre_px: float, band_pts: float) -> float:
    lo = int(round((centre_px - band_pts) / PTS_PER_TICK))
    hi = int(round((centre_px + band_pts) / PTS_PER_TICK))
    return float(sum(sz for px, sz in book.items() if lo <= px <= hi and sz > 0))


def _annotate_persistence(
    trades: list[dict[str, Any]],
    *,
    cfg: dict,
    pq_dir: Path,
) -> list[dict[str, Any]]:
    """Add resting size at fill−1s in a band around level; flag persist_ok."""
    market = cfg["market"]
    band = float(cfg["persist_band_pts"])
    frac = float(cfg["persist_frac"])
    by_sid: dict[str, list[dict]] = {}
    for t in trades:
        by_sid.setdefault(t["session_id"], []).append(t)

    out: list[dict[str, Any]] = []
    for sid, rows in by_sid.items():
        rows = sorted(rows, key=lambda r: r["fill_ns"])
        times = [int(r["fill_ns"] - 1_000_000_000) for r in rows]
        # One fold to the last check time, then replay snapshots in order.
        last_t = max(times) if times else 0
        try:
            depth, bids0, asks0 = load_depth_window_with_book(
                pq_dir, sid, market, (min(times), last_t),
            )
        except Exception:
            depth, bids0, asks0 = None, {}, {}
        bids, asks = dict(bids0), dict(asks0)
        # depth rows are inside [min(times), last_t]; fold chronologically
        if depth is not None and depth.num_rows > 0:
            d_ts = depth.column("ts_ns").to_numpy().astype(np.int64)
            d_bid = depth.column("is_bid").to_numpy().astype(bool)
            d_px = depth.column("price_ticks").to_numpy().astype(np.int64)
            d_sz = depth.column("size").to_numpy().astype(np.int64)
            order = np.argsort(d_ts, kind="stable")
            d_ts, d_bid, d_px, d_sz = d_ts[order], d_bid[order], d_px[order], d_sz[order]
        else:
            d_ts = np.array([], dtype=np.int64)
            d_bid = d_px = d_sz = d_ts
        i = 0
        n = len(d_ts)
        for r, t_check in zip(rows, times):
            while i < n and int(d_ts[i]) <= t_check:
                book = bids if bool(d_bid[i]) else asks
                px, sz = int(d_px[i]), int(d_sz[i])
                if sz <= 0:
                    book.pop(px, None)
                else:
                    book[px] = sz
                i += 1
            book = bids if r["side"] == "BID" else asks
            live = _band_size(book, float(r["level_px"]), band)
            need = max(1.0, frac * float(r["snap_size"] or 0.0))
            out.append({
                **r,
                "persist_size": live,
                "persist_need": need,
                "persist_ok": bool(live >= need),
            })
    return out


def _day_bootstrap(trades: list[dict[str, Any]], *, n_boot: int, seed: int) -> dict[str, float]:
    if not trades:
        return {"p05": float("nan"), "p50": float("nan"), "p95": float("nan"), "p_pos": float("nan")}
    by: dict[str, float] = {}
    for t in trades:
        by[t["session_id"]] = by.get(t["session_id"], 0.0) + float(t["pnl_net"])
    days = np.asarray(list(by.values()), dtype=np.float64)
    rng = np.random.default_rng(seed)
    n = len(days)
    sums = np.empty(n_boot, dtype=np.float64)
    for i in range(n_boot):
        draws = days[rng.integers(0, n, size=n)]
        sums[i] = float(draws.sum())
    return {
        "p05": float(np.percentile(sums, 5)),
        "p50": float(np.percentile(sums, 50)),
        "p95": float(np.percentile(sums, 95)),
        "p_pos": float((sums > 0).mean()),
    }


def _robustness(trades: list[dict[str, Any]]) -> dict[str, Any]:
    p = _pack(trades)
    ordered = sorted(trades, key=lambda t: -float(t["pnl_net"]))
    drop3 = ordered[3:] if len(ordered) > 3 else []
    p_drop = _pack(drop3)
    boot = _day_bootstrap(trades, n_boot=N_BOOT, seed=RNG_SEED)
    return {
        "n": p["n"],
        "wr": p["wr"],
        "net": p["net"],
        "avg": p["avg"],
        "days_plus": p["days_plus"],
        "days": p["days"],
        "maxDD": p["maxDD"],
        "net_drop3": p_drop["net"],
        "avg_drop3": p_drop["avg"],
        "n_drop3": p_drop["n"],
        "boot_p05": boot["p05"],
        "boot_p50": boot["p50"],
        "boot_p95": boot["p95"],
        "boot_p_pos": boot["p_pos"],
    }


def _fmt_pack(p: dict[str, Any]) -> str:
    if not p.get("n"):
        return "n=0"
    wr = f"{p['wr']:.0%}" if p.get("wr") is not None else "—"
    avg = f"{p['avg']:+.2f}" if p.get("avg") is not None else "—"
    return f"n={p['n']} WR={wr} net={p['net']:+.1f} avg={avg}"


def run_book(
    book_name: str,
    *,
    out_dir: Path,
    entry_until: str,
    eos: str,
) -> str:
    cfg = BOOKS[book_name]
    market = cfg["market"]
    ann = cfg["ann"]
    pq_dir = Path(load_config()["parquet_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    near = _load_levels(
        ann, market=market, near_pts=cfg["near_pts"], min_size=cfg["min_size"], confluence=True,
    )
    far = _load_levels(
        ann, market=market, near_pts=cfg["near_pts"], min_size=cfg["min_size"], confluence=False,
    )

    lines = [
        f"# {book_name} ∩ LVN — conviction checks",
        "",
        f"Frozen: {market} · LVN≤{cfg['near_pts']:g} · outside+{cfg['offset']:g} · "
        f"SL{cfg['sl']:g}/TP{cfg['tp']:g} · min-open {cfg['min_open_dist']:g}"
        + (f" · size≥{cfg['min_size']:g}" if cfg["min_size"] else "")
        + ".",
        f"Windows: fills to {entry_until}, hold to {eos} UK.",
        "",
        f"Near-LVN levels loaded: **{len(near)}**. Far-LVN (size-matched) levels: **{len(far)}**.",
        "",
        "## 1. Ablations",
        "",
    ]

    arms = [
        ("both", near, "confluence outer (frozen)"),
        ("cluster", near, "same confluence rows, cluster centre only"),
        ("lvn", near, "same confluence rows, LVN price only"),
        ("far_cluster", far, "far from LVN, fade cluster centre"),
        ("shift", near, "confluence rows, level randomly shifted away"),
    ]

    abl_rows: list[list[str]] = []
    arm_trades: dict[str, list[dict]] = {}
    for arm, levels, desc in arms:
        trades = _simulate_arm(
            levels, cfg=cfg, arm=arm, pq_dir=pq_dir,
            entry_until=entry_until, eos=eos, seed=RNG_SEED + hash(arm) % 10_000,
        )
        arm_trades[arm] = trades
        p = _pack(trades)
        rob = _robustness(trades)
        abl_rows.append([
            arm,
            desc,
            str(p["n"]),
            f"{p['wr']:.0%}" if p["wr"] is not None else "—",
            f"{p['net']:+.1f}" if p["n"] else "—",
            f"{p['avg']:+.2f}" if p["avg"] is not None else "—",
            f"{p['days_plus']}/{p['days']}" if p["n"] else "—",
            f"{rob['boot_p05']:+.0f}…{rob['boot_p95']:+.0f}" if p["n"] else "—",
            f"{rob['boot_p_pos']:.0%}" if p["n"] else "—",
        ])
        print(f"  [{book_name}] ablation {arm}: {_fmt_pack(p)}", flush=True)

    lines += _md_table(
        ["arm", "description", "n", "WR", "net", "avg", "days+", "boot net 5–95%", "P(net>0)"],
        abl_rows,
        right={2, 3, 4, 5, 6, 7, 8},
    )
    both = arm_trades["both"]
    far_t = arm_trades["far_cluster"]
    shift_t = arm_trades["shift"]
    lines += [
        "",
        "### Ablation read",
        "",
    ]
    if both and far_t:
        db = _pack(both)["avg"] or 0.0
        df = _pack(far_t)["avg"] or 0.0
        lines.append(
            f"- Confluence vs far: Δavg **{db - df:+.2f}** "
            f"(both {db:+.2f} vs far {df:+.2f})."
        )
    if both and shift_t:
        db = _pack(both)["avg"] or 0.0
        ds = _pack(shift_t)["avg"] or 0.0
        lines.append(
            f"- Confluence vs random shift: Δavg **{db - ds:+.2f}** "
            f"(both {db:+.2f} vs shift {ds:+.2f})."
        )
    if both and arm_trades["cluster"] and arm_trades["lvn"]:
        lines.append(
            f"- Anchor parts: cluster {_fmt_pack(_pack(arm_trades['cluster']))}; "
            f"LVN {_fmt_pack(_pack(arm_trades['lvn']))}; "
            f"both {_fmt_pack(_pack(both))}."
        )
    lines.append("")

    # Persistence on frozen both arm
    print(f"  [{book_name}] persistence annotations…", flush=True)
    both_p = _annotate_persistence(both, cfg=cfg, pq_dir=pq_dir)
    kept = [t for t in both_p if t["persist_ok"]]
    dropped = [t for t in both_p if not t["persist_ok"]]
    lines += [
        "## 2. Persistence (resting size still there 1s before fill)",
        "",
        f"Band ±{cfg['persist_band_pts']:g} pts around level; require "
        f"≥{cfg['persist_frac']:.0%} of snap cluster size.",
        "",
    ]
    pers_rows = [
        ["all fills", *_pack_row(_pack(both_p))],
        ["persist OK", *_pack_row(_pack(kept))],
        ["pulled / thin", *_pack_row(_pack(dropped))],
    ]
    lines += _md_table(
        ["set", "n", "WR", "net", "avg", "days+"],
        pers_rows,
        right={1, 2, 3, 4, 5},
    )
    if both_p:
        n_ok = sum(1 for t in both_p if t["persist_ok"])
        lines += [
            "",
            f"Persist OK: **{n_ok}/{len(both_p)}** "
            f"({n_ok / len(both_p):.0%}).",
            "",
        ]

    # Robustness
    rob = _robustness(both_p if both_p else both)
    lines += [
        "## 3. Robustness (frozen both arm)",
        "",
        f"- Full: {_fmt_pack(rob)} · days+ {rob['days_plus']}/{rob['days']} · maxDD {rob['maxDD']:.0f}.",
    ]
    if rob["avg_drop3"] is not None:
        lines.append(
            f"- Drop top-3 winners: n={rob['n_drop3']} net={rob['net_drop3']:+.1f} "
            f"avg={rob['avg_drop3']:+.2f}."
        )
    else:
        lines.append(f"- Drop top-3 winners: n={rob['n_drop3']} (empty).")
    lines += [
        f"- Day-clustered bootstrap ({N_BOOT} resamples): "
        f"net 5/50/95% = {rob['boot_p05']:+.1f} / {rob['boot_p50']:+.1f} / {rob['boot_p95']:+.1f}; "
        f"P(net>0)={rob['boot_p_pos']:.0%}.",
        "",
    ]

    # Verdict
    lines += ["## Verdict", ""]
    both_avg = _pack(both)["avg"] if both else None
    far_avg = _pack(far_t)["avg"] if far_t else None
    shift_avg = _pack(shift_t)["avg"] if shift_t else None
    persist_avg = _pack(kept)["avg"] if kept else None
    checks = []
    if both_avg is not None and far_avg is not None:
        checks.append(("confluence > far", both_avg > far_avg, f"Δavg {both_avg - far_avg:+.2f}"))
    if both_avg is not None and shift_avg is not None:
        checks.append(("confluence > shift", both_avg > shift_avg, f"Δavg {both_avg - shift_avg:+.2f}"))
    if persist_avg is not None and both_avg is not None:
        checks.append(
            ("persist-OK still +EV", (persist_avg > 0 and _pack(kept)["net"] > 0),
             f"avg {persist_avg:+.2f}")
        )
    checks.append(
        ("drop-top-3 still +net", rob["net_drop3"] > 0, f"net {rob['net_drop3']:+.1f}")
    )
    checks.append(
        ("bootstrap P(net>0) ≥ 80%", rob["boot_p_pos"] >= 0.80, f"{rob['boot_p_pos']:.0%}")
    )
    checks.append(
        ("bootstrap 5% > 0", rob["boot_p05"] > 0, f"{rob['boot_p05']:+.1f}")
    )
    n_pass = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        lines.append(f"- {'PASS' if ok else 'FAIL'} — {name} ({detail}).")
    lines += [
        "",
        f"**Score: {n_pass}/{len(checks)} checks passed.**",
        "",
    ]

    # Write trades
    fields = [
        "date", "session_id", "market", "side", "arm", "centre", "size", "lvn_px",
        "level_px", "level_src", "entry", "sl", "tp", "pnl_net", "exit",
        "mfe", "mae", "t_entry_s", "duration_s", "entry_ts", "exit_ts",
        "open_mid", "dist_open", "persist_size", "persist_need", "persist_ok", "month",
    ]
    tpath = out_dir / f"trades_{book_name}_both.csv"
    with tpath.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for t in sorted(both_p if both_p else both, key=lambda r: (r["date"], r["t_entry_s"])):
            w.writerow(t)

    abl_path = out_dir / f"ablation_{book_name}.csv"
    with abl_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["arm", "n", "wr", "net", "avg", "days_plus", "days",
                        "boot_p05", "boot_p50", "boot_p95", "boot_p_pos",
                        "net_drop3", "avg_drop3"],
        )
        w.writeheader()
        for arm, _, _ in arms:
            tr = arm_trades[arm]
            r = _robustness(tr)
            w.writerow({
                "arm": arm,
                "n": r["n"], "wr": r["wr"], "net": r["net"], "avg": r["avg"],
                "days_plus": r["days_plus"], "days": r["days"],
                "boot_p05": r["boot_p05"], "boot_p50": r["boot_p50"],
                "boot_p95": r["boot_p95"], "boot_p_pos": r["boot_p_pos"],
                "net_drop3": r["net_drop3"], "avg_drop3": r["avg_drop3"],
            })

    summary = out_dir / f"summary_{book_name}.md"
    text = "\n".join(lines) + "\n"
    summary.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return text


def _pack_row(p: dict[str, Any]) -> list[str]:
    if p["n"] == 0:
        return ["0", "—", "—", "—", "—"]
    return [
        str(p["n"]),
        f"{p['wr']:.0%}",
        f"{p['net']:+.1f}",
        f"{p['avg']:+.2f}",
        f"{p['days_plus']}/{p['days']}",
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--book", choices=("ES", "NQ", "BOTH"), default="BOTH")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "LVN_CONVICTION")
    ap.add_argument("--entry-until", default="17:00")
    ap.add_argument("--eos", default="21:00")
    args = ap.parse_args()
    books = ["ES", "NQ"] if args.book == "BOTH" else [args.book]
    chunks: list[str] = []
    for b in books:
        print(f"=== {b} ===", flush=True)
        chunks.append(run_book(
            b, out_dir=args.out, entry_until=args.entry_until, eos=args.eos,
        ))
    if len(chunks) > 1:
        combined = args.out / "summary.md"
        combined.write_text(
            "# LVN confluence — conviction suite\n\n"
            + "\n---\n\n".join(chunks),
            encoding="utf-8",
        )
        print(f"Wrote {combined}", flush=True)


if __name__ == "__main__":
    main()
