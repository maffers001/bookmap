"""ES open ∩ LVN levels: does a 1m reversal candle improve fades vs first-touch?

Frozen level set: ES · confluence ≤10 pts of LVN2/3.
Baseline: first-touch fade limit 2 pts outside · SL8/TP16.
Alt: wait for pinbar / doji / train-track / engulfing on the touch bar
(or next bar while still near the level), then enter at pattern close.

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_es_lvn_candle_confirm
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import numpy as np

from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed
from research.features.regime import MinuteBar
from research.irt_lvn.events import PTS_PER_TICK
from research.irt_lvn.load_irt import session_id_to_ymd
from research.irt_lvn.study_es_lvn_vol_brackets import _fade, _load_month_levels, _pack
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")

PATTERNS = ("pinbar", "doji", "train", "engulf")


def _hhmm_ns(y: int, m: int, d: int, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":")[:2])
    return int(datetime(y, m, d, hh, mm, tzinfo=UK).timestamp() * 1e9)


@dataclass
class BarView:
    o: float
    h: float
    l: float
    c: float
    ts_ns: int
    end_ns: int

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def range(self) -> float:
        return max(self.h - self.l, 1e-9)

    @property
    def upper(self) -> float:
        return self.h - max(self.o, self.c)

    @property
    def lower(self) -> float:
        return min(self.o, self.c) - self.l

    @property
    def bull(self) -> bool:
        return self.c > self.o

    @property
    def bear(self) -> bool:
        return self.c < self.o


def _as_view(b: MinuteBar) -> BarView:
    return BarView(b.open, b.high, b.low, b.close, b.ts_ns, b.end_ns)


def _touches_level(b: BarView, *, side: str, centre: float, band: float) -> bool:
    if side == "BID":
        return b.l <= centre + band
    return b.h >= centre - band


def _pinbar(b: BarView, *, side: str, min_range: float = 1.5) -> bool:
    if b.range < min_range:
        return False
    if side == "BID":
        # hammer / bullish pin: long lower wick, close near high
        return (
            b.lower >= 2.0 * max(b.body, 0.25)
            and b.lower >= 0.55 * b.range
            and b.upper <= 0.25 * b.range
            and (b.c - b.l) / b.range >= 0.55
        )
    return (
        b.upper >= 2.0 * max(b.body, 0.25)
        and b.upper >= 0.55 * b.range
        and b.lower <= 0.25 * b.range
        and (b.h - b.c) / b.range >= 0.55
    )


def _doji(b: BarView, *, min_range: float = 1.0) -> bool:
    if b.range < min_range:
        return False
    return b.body <= max(0.25, 0.15 * b.range)


def _engulf(prev: BarView, cur: BarView, *, side: str) -> bool:
    if side == "BID":
        # bullish engulfing after a down bar
        return (
            prev.bear
            and cur.bull
            and cur.o <= prev.c
            and cur.c >= prev.o
            and cur.body > prev.body
        )
    return (
        prev.bull
        and cur.bear
        and cur.o >= prev.c
        and cur.c <= prev.o
        and cur.body > prev.body
    )


def _train(prev: BarView, cur: BarView, *, side: str) -> bool:
    """Two opposite candles of similar size; second rejects at the level."""
    if prev.range < 1.25 or cur.range < 1.25:
        return False
    size_ok = abs(prev.body - cur.body) <= 0.35 * max(prev.body, cur.body, 0.5)
    if not size_ok:
        # also allow similar full ranges (classic train-track look)
        size_ok = abs(prev.range - cur.range) <= 0.30 * max(prev.range, cur.range)
    if not size_ok:
        return False
    if side == "BID":
        # bear into support, bull out; lows aligned
        return (
            prev.bear
            and cur.bull
            and abs(prev.l - cur.l) <= 0.75
            and cur.c > prev.c
        )
    return (
        prev.bull
        and cur.bear
        and abs(prev.h - cur.h) <= 0.75
        and cur.c < prev.c
    )


def _classify(
    bars: list[BarView], i: int, *, side: str, centre: float, band: float,
) -> list[str]:
    """Patterns on bar i (touch bar) using i-1 when needed. Bar must touch level."""
    cur = bars[i]
    if not _touches_level(cur, side=side, centre=centre, band=band):
        return []
    hits: list[str] = []
    if _pinbar(cur, side=side):
        hits.append("pinbar")
    if _doji(cur):
        hits.append("doji")
    if i >= 1:
        prev = bars[i - 1]
        if _engulf(prev, cur, side=side):
            hits.append("engulf")
        if _train(prev, cur, side=side):
            hits.append("train")
    return hits


def _first_touch_bar_idx(
    bars: list[BarView], *, side: str, centre: float, band: float, after_ns: int,
) -> int | None:
    for i, b in enumerate(bars):
        if b.end_ns <= after_ns:
            continue
        if _touches_level(b, side=side, centre=centre, band=band):
            return i
    return None


def _fade_from(
    ts, px, *, side: str, entry: float, tp: float, sl: float,
    from_ns: int, snap_ns: int, exit_slip: float, commission: float,
) -> dict[str, Any] | None:
    m = ts >= from_ns
    if not m.any():
        return None
    return _fade(
        ts[m], px[m],
        side=side, entry=entry, tp=tp, sl=sl,
        snap_ns=snap_ns, exit_slip=exit_slip, commission=commission,
    )


def _market_bracket(
    ts, px, *, side: str, entry: float, tp: float, sl: float,
    from_ns: int, snap_ns: int, exit_slip: float, commission: float,
) -> dict[str, Any] | None:
    """Enter immediately at ``entry`` once ``from_ns`` is reached (no limit wait)."""
    m = ts >= from_ns
    if not m.any():
        return None
    ts2, px2 = ts[m], px[m]
    # use first print as fill proxy if entry is the pattern close
    fill_px = entry
    sign = 1.0 if side == "BID" else -1.0
    if side == "BID":
        hit_tp, hit_sl = px2 >= fill_px + tp, px2 <= fill_px - sl
        mfe, mae = float(px2.max() - fill_px), float(fill_px - px2.min())
    else:
        hit_tp, hit_sl = px2 <= fill_px - tp, px2 >= fill_px + sl
        mfe, mae = float(fill_px - px2.min()), float(px2.max() - fill_px)
    if hit_tp.any() or hit_sl.any():
        j_tp = int(np.argmax(hit_tp)) if hit_tp.any() else 10**18
        j_sl = int(np.argmax(hit_sl)) if hit_sl.any() else 10**18
        if not hit_tp.any():
            j_tp = 10**18
        if not hit_sl.any():
            j_sl = 10**18
        reason, pnl = ("sl", -sl - exit_slip) if j_sl <= j_tp else ("tp", tp - exit_slip)
    else:
        reason = "time"
        pnl = sign * (float(px2[-1]) - fill_px) - exit_slip
    return {
        "pnl_net": pnl - commission,
        "exit": reason,
        "mfe": mfe,
        "mae": mae,
        "sl": sl,
        "tp": tp,
        "t_entry_s": (int(ts2[0]) - snap_ns) / 1e9,
        "entry": fill_px,
    }


def _hold_from_touch(
    ts, px, *, side: str, centre: float, tp: float, sl: float, offset: float,
) -> dict[str, Any] | None:
    """Did the level 'hold' under first-touch fade geometry (diagnostic only)?"""
    entry = centre - offset if side == "BID" else centre + offset
    return _fade(
        ts, px, side=side, entry=entry, tp=tp, sl=sl,
        snap_ns=int(ts[0]) if len(ts) else 0,
        exit_slip=0.0, commission=0.0,
    )


def run(*, out_dir: Path, near_pts: float, offset: float, until: str, band: float) -> None:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    months = {
        "OCT": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_OCT" / "clusters_with_lvn.csv",
        "SEP": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_SEP" / "clusters_with_lvn.csv",
        "MAY": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_MAY" / "clusters_with_lvn.csv",
    }

    exit_slip = PTS_PER_TICK
    commission = 0.125
    tp, sl = 16.0, 8.0
    # confirm window: touch bar + next 2 bars
    look_ahead = 2

    lines = [
        "# ES ∩ LVN — candle confirmation vs first-touch",
        "",
        f"Levels: ES · LVN≤{near_pts:g} · frozen SL{sl:g}/TP{tp:g}.",
        f"Touch band: centre ±{band:g} pts. Patterns on 1m bars (pinbar, doji, train, engulf).",
        "Pattern entry: at confirming bar **close** (market), not first-touch limit.",
        f"Baseline: first-touch limit outside+{offset:g}.",
        "",
    ]

    all_events: list[dict] = []
    grid: list[dict] = []

    for mo, ann in months.items():
        if not ann.exists():
            print(f"{mo}: missing {ann}", flush=True)
            continue
        levels = _load_month_levels(ann, near_pts=near_pts)
        print(f"{mo}: {len(levels)} ES confluence levels", flush=True)

        sess_ids = sorted({lv["session_id"] for lv in levels})
        sess_data: dict[str, dict] = {}
        for sid in sess_ids:
            y, m, d = session_id_to_ymd(sid)
            snap_ns = _hhmm_ns(y, m, d, "14:30")
            end_ns = _hhmm_ns(y, m, d, until)
            tr = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
            # tape for fade sim
            ts_all = tr.column("ts_ns").to_numpy().astype(np.int64)
            px_all = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
            o = np.argsort(ts_all, kind="stable")
            ts_all, px_all = ts_all[o], px_all[o]
            msk = (ts_all >= snap_ns) & (ts_all < end_ns)
            # minute bars: a few pre-open bars so engulf/train can see prev
            pre = snap_ns - 5 * 60_000_000_000
            m_bar = (ts_all >= pre) & (ts_all < end_ns)
            if not m_bar.any():
                sess_data[sid] = {"ok": False}
                continue
            bars_raw = _minute_bars_from_arrays(ts_all[m_bar], px_all[m_bar])
            sess_data[sid] = {
                "ok": True,
                "ts": ts_all[msk],
                "px": px_all[msk],
                "snap_ns": snap_ns,
                "bars": [_as_view(b) for b in bars_raw],
            }
            print(f"  {sid}: {len(bars_raw)} 1m bars", flush=True)

        # --- per-level event classification ---
        events: list[dict] = []
        for lv in levels:
            sd = sess_data.get(lv["session_id"])
            if not sd or not sd["ok"]:
                continue
            bars: list[BarView] = sd["bars"]
            snap_ns = sd["snap_ns"]
            centre = lv["centre"]
            side = lv["side"]
            ti = _first_touch_bar_idx(
                bars, side=side, centre=centre, band=band, after_ns=snap_ns,
            )
            if ti is None:
                events.append({
                    **lv, "month": mo, "touched": 0, "patterns": "",
                    "confirm_i": None,
                })
                continue

            # search confirm on touch bar .. touch+look_ahead
            found: list[str] = []
            confirm_i = None
            for j in range(ti, min(ti + 1 + look_ahead, len(bars))):
                pats = _classify(bars, j, side=side, centre=centre, band=band)
                if pats:
                    found = pats
                    confirm_i = j
                    break
                # still near level? if price already ran away, stop looking
                b = bars[j]
                if side == "BID" and b.l > centre + band + 4.0:
                    break
                if side == "ASK" and b.h < centre - band - 4.0:
                    break

            # invalidate if already blown through SL from centre before confirm
            blown = False
            if confirm_i is not None:
                for j in range(ti, confirm_i + 1):
                    b = bars[j]
                    if side == "BID" and b.l <= centre - sl:
                        blown = True
                        break
                    if side == "ASK" and b.h >= centre + sl:
                        blown = True
                        break

            events.append({
                **lv,
                "month": mo,
                "touched": 1,
                "touch_i": ti,
                "touch_t_s": (bars[ti].ts_ns - snap_ns) / 1e9,
                "patterns": "|".join(found) if found and not blown else "",
                "confirm_i": confirm_i if found and not blown else None,
                "confirm_t_s": (
                    (bars[confirm_i].end_ns - snap_ns) / 1e9
                    if confirm_i is not None and found and not blown else None
                ),
                "confirm_close": (
                    bars[confirm_i].c
                    if confirm_i is not None and found and not blown else None
                ),
                "blown_before_confirm": int(blown),
            })

        all_events.extend(events)

        # --- trade books ---
        books: dict[str, list[dict]] = {
            "first_touch": [],
            "any_pattern": [],
            **{f"pat_{p}": [] for p in PATTERNS},
            "no_pattern_touch": [],  # touched but no confirm — diagnostic fade still?
        }

        for ev in events:
            if not ev.get("touched"):
                continue
            sd = sess_data[ev["session_id"]]
            ts, px, snap_ns = sd["ts"], sd["px"], sd["snap_ns"]
            centre, side = ev["centre"], ev["side"]
            entry_ft = centre - offset if side == "BID" else centre + offset

            ft = _fade(
                ts, px, side=side, entry=entry_ft, tp=tp, sl=sl,
                snap_ns=snap_ns, exit_slip=exit_slip, commission=commission,
            )
            if ft is not None:
                row = {
                    **ft, "date": ev["date"], "session_id": ev["session_id"],
                    "side": side, "centre": centre, "patterns": ev["patterns"],
                    "book": "first_touch", "month": mo,
                }
                books["first_touch"].append(row)

            pats = [p for p in (ev["patterns"] or "").split("|") if p]
            if pats and ev.get("confirm_close") is not None:
                # enter at pattern close; for BID long near close, ASK short
                entry_px = float(ev["confirm_close"])
                # slight adverse: BID entry = close (buying support), ASK = close
                from_ns = int(sd["bars"][ev["confirm_i"]].end_ns)
                trd = _market_bracket(
                    ts, px, side=side, entry=entry_px, tp=tp, sl=sl,
                    from_ns=from_ns, snap_ns=snap_ns,
                    exit_slip=exit_slip, commission=commission,
                )
                if trd is not None:
                    base = {
                        **trd, "date": ev["date"], "session_id": ev["session_id"],
                        "side": side, "centre": centre, "patterns": ev["patterns"],
                        "month": mo,
                    }
                    books["any_pattern"].append({**base, "book": "any_pattern"})
                    for p in pats:
                        books[f"pat_{p}"].append({**base, "book": f"pat_{p}"})
            elif ev.get("touched") and not pats:
                # optional: still record first-touch as the no-pattern cohort path
                if ft is not None:
                    books["no_pattern_touch"].append({
                        **ft, "date": ev["date"], "session_id": ev["session_id"],
                        "side": side, "centre": centre, "patterns": "",
                        "book": "no_pattern_touch", "month": mo,
                    })

        # pattern frequency among touches
        touched = [e for e in events if e.get("touched")]
        n_touch = len(touched)
        freq = {p: sum(1 for e in touched if p in (e.get("patterns") or "")) for p in PATTERNS}
        any_n = sum(1 for e in touched if e.get("patterns"))

        lines += [
            f"## {mo}",
            "",
            f"Levels {len(levels)} · touched {n_touch} "
            f"({n_touch / max(len(levels), 1):.0%}) · "
            f"any pattern confirm {any_n} ({any_n / max(n_touch, 1):.0%} of touches)",
            "",
            "Pattern frequency (of touches): "
            + " · ".join(f"{p}={freq[p]}" for p in PATTERNS),
            "",
            "| book | n | WR | net | avg | days+ | maxDD |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]

        for bname, trades in books.items():
            p = _pack(trades)
            grid.append({
                "month": mo, "book": bname,
                "n": p["n"], "wr": p["wr"], "net": p["net"], "avg": p["avg"],
                "days_plus": p["days_plus"], "days": p["days"], "maxDD": p["maxDD"],
                "n_touch": n_touch, "n_any_pattern": any_n, **{f"freq_{k}": freq[k] for k in PATTERNS},
            })
            if p["n"] == 0:
                lines.append(f"| {bname} | 0 | — | — | — | — | — |")
            else:
                lines.append(
                    f"| {bname} | {p['n']} | {p['wr']:.0%} | {p['net']:+.1f} | "
                    f"{p['avg']:+.2f} | {p['days_plus']}/{p['days']} | {p['maxDD']:.0f} |"
                )

        # hold diagnostic: first-touch exit distribution split by pattern present
        with_p = [e for e in touched if e.get("patterns")]
        without = [e for e in touched if not e.get("patterns")]

        def _ft_exits(evs: list[dict]) -> dict[str, int]:
            out = {"tp": 0, "sl": 0, "time": 0, "n": 0}
            for e in evs:
                sd = sess_data[e["session_id"]]
                entry = e["centre"] - offset if e["side"] == "BID" else e["centre"] + offset
                trd = _fade(
                    sd["ts"], sd["px"], side=e["side"], entry=entry, tp=tp, sl=sl,
                    snap_ns=sd["snap_ns"], exit_slip=0.0, commission=0.0,
                )
                if trd is None:
                    continue
                out["n"] += 1
                out[trd["exit"]] = out.get(trd["exit"], 0) + 1
            return out

        wp, wop = _ft_exits(with_p), _ft_exits(without)
        lines += [
            "",
            "First-touch exit mix (same geometry) by whether a pattern also formed:",
            f"- With pattern (n={wp['n']}): tp={wp['tp']} sl={wp['sl']} time={wp['time']}"
            + (f" · WR≈{wp['tp'] / max(wp['n'], 1):.0%} (tp-rate)" if wp["n"] else ""),
            f"- No pattern (n={wop['n']}): tp={wop['tp']} sl={wop['sl']} time={wop['time']}"
            + (f" · WR≈{wop['tp'] / max(wop['n'], 1):.0%} (tp-rate)" if wop["n"] else ""),
            "",
        ]

        # dump trades for key books
        for bname in ("first_touch", "any_pattern", "pat_pinbar", "pat_doji", "pat_train", "pat_engulf"):
            tpath = out_dir / f"trades_{mo}_{bname}.csv"
            with tpath.open("w", newline="", encoding="utf-8") as f:
                fields = [
                    "date", "session_id", "side", "centre", "patterns", "entry",
                    "pnl_net", "exit", "mfe", "mae", "t_entry_s", "book", "month",
                ]
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for t in books[bname]:
                    w.writerow(t)

    # cross-month
    lines += [
        "## Cross-month",
        "",
        "| book | Oct n/net/avg | Sep n/net/avg | May n/net/avg | Sum net |",
        "|---|---|---|---|---:|",
    ]
    book_names = [
        "first_touch", "any_pattern", "pat_pinbar", "pat_doji", "pat_train", "pat_engulf",
        "no_pattern_touch",
    ]
    for bname in book_names:
        cells = {}
        for mo in ("OCT", "SEP", "MAY"):
            g = next((x for x in grid if x["month"] == mo and x["book"] == bname), None)
            cells[mo] = g

        def cell(g):
            if not g or g["n"] == 0:
                return "—"
            return f"{g['n']}/{g['net']:+.0f}/{g['avg']:+.2f}"

        snet = sum((cells[m]["net"] if cells[m] else 0.0) for m in cells)
        lines.append(
            f"| {bname} | {cell(cells['OCT'])} | {cell(cells['SEP'])} | "
            f"{cell(cells['MAY'])} | {snet:+.0f} |"
        )

    lines += [
        "",
        "## Verdict",
        "",
        "Pattern confirm helps only if **any_pattern** (or a single pattern book) "
        "beats **first_touch** on avg/WR without collapsing n to noise, and holds "
        "on Sep/May — not an Oct-only candle fit.",
        "",
    ]

    # events dump
    epath = out_dir / "touch_events.csv"
    with epath.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "month", "date", "session_id", "side", "centre", "lvn_dist", "rank",
            "touched", "touch_t_s", "patterns", "confirm_t_s", "confirm_close",
            "blown_before_confirm",
        ]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for e in all_events:
            w.writerow(e)

    gpath = out_dir / "book_grid.csv"
    with gpath.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "month", "book", "n", "wr", "net", "avg", "days_plus", "days", "maxDD",
            "n_touch", "n_any_pattern",
            "freq_pinbar", "freq_doji", "freq_train", "freq_engulf",
        ]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for g in grid:
            w.writerow(g)

    summary = out_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(summary.read_text(encoding="utf-8"))
    print(f"Wrote {summary}", flush=True)


def _minute_bars_from_arrays(ts: np.ndarray, px: np.ndarray) -> list[MinuteBar]:
    if len(ts) == 0:
        return []
    bar_ns = 60_000_000_000
    bar_id = (ts // bar_ns) * bar_ns
    uniq, start = np.unique(bar_id, return_index=True)
    ends = np.append(start[1:], len(ts))
    out: list[MinuteBar] = []
    for s, e, uid in zip(start, ends, uniq):
        sl = slice(s, e)
        out.append(MinuteBar(
            ts_ns=int(uid),
            open=float(px[sl][0]),
            high=float(px[sl].max()),
            low=float(px[sl].min()),
            close=float(px[sl][-1]),
            volume=float(e - s),
            end_ns=int(uid) + bar_ns,
        ))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out", type=Path,
        default=ROOT / "results" / "ES_LVN_CANDLE_CONFIRM",
    )
    ap.add_argument("--near", type=float, default=10.0)
    ap.add_argument("--offset", type=float, default=2.0)
    ap.add_argument("--until", default="17:00")
    ap.add_argument(
        "--band", type=float, default=1.5,
        help="Touch tolerance around centre (pts)",
    )
    args = ap.parse_args()
    run(
        out_dir=args.out, near_pts=args.near, offset=args.offset,
        until=args.until, band=args.band,
    )


if __name__ == "__main__":
    main()
