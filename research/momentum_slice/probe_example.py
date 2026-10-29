"""Forensic probe of the 10-Sep-2026 15:24 BST NQ/ES slice-through example.

Usage: .venv\\Scripts\\python.exe -m research.momentum_slice.probe_example
Prints (a) 250ms tape bars 15:23:30-15:26:00 for NQ+ES, (b) book stack above/
below price at several pre-move snapshots, (c) per-price traded-vs-displayed
accounting through the slice window.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

UK = ZoneInfo("Europe/London")
SID = "100926"
BASE = f"data/parquet/session_id={SID}"


def ts_of(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UK).timestamp() * 1e9)


def fmt_ts(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).astimezone(UK).strftime("%H:%M:%S.%f")[:-3]


def load_trades(role: str) -> pa.Table:
    t = pq.read_table(f"{BASE}/trades.parquet", filters=[("role", "=", role)])
    ts = t.column("ts_ns").to_numpy()
    return t.take(np.argsort(ts))


def tape_bars(t: pa.Table, t0: int, t1: int, bar_ms=250):
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    px = t.column("price").to_numpy().astype(np.float64) * 0.25
    sz = t.column("size").to_numpy().astype(np.float64)
    buy = t.column("is_buy_aggressor").to_numpy().astype(bool)
    lo, hi = np.searchsorted(ts, t0), np.searchsorted(ts, t1)
    ts, px, sz, buy = ts[lo:hi], px[lo:hi], sz[lo:hi], buy[lo:hi]
    bar_ns = bar_ms * 1_000_000
    bid = (ts // bar_ns) * bar_ns
    out = []
    for b in np.unique(bid):
        m = bid == b
        bv, sv = sz[m & buy].sum(), sz[m & ~buy].sum()
        out.append((int(b), px[m][0], px[m].max(), px[m].min(), px[m][-1],
                    bv + sv, bv - sv, int(m.sum()), sz[m].max()))
    return out


def replay_book(role: str, snap_times: list[int], slice_win: tuple[int, int]):
    """Single depth pass: snapshots at snap_times + size at slice start/end per price."""
    pf = pq.ParquetFile(f"{BASE}/depth.parquet")
    book: dict[tuple[bool, int], int] = {}
    snaps: dict[int, dict] = {}
    pending = sorted(snap_times)
    start_book: dict | None = None
    for batch in pf.iter_batches(batch_size=1_000_000,
                                 columns=["ts_ns", "role", "is_bid", "price_ticks", "size"]):
        t = pa.Table.from_batches([batch])
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        roles = t.column("role").to_numpy(zero_copy_only=False)
        bid = t.column("is_bid").to_numpy().astype(bool)
        px = t.column("price_ticks").to_numpy().astype(np.int64)
        sz = t.column("size").to_numpy().astype(np.int64)
        # snapshots are taken before processing rows at/after snap time
        while pending and len(ts) and pending[0] <= ts[-1]:
            st = pending.pop(0)
            k = np.searchsorted(ts, st)
            for i in range(k):
                if roles[i] == role:
                    key = (bool(bid[i]), int(px[i]))
                    if sz[i] > 0:
                        book[key] = int(sz[i])
                    else:
                        book.pop(key, None)
            snaps[st] = dict(book)
            ts, roles, bid, px, sz = ts[k:], roles[k:], bid[k:], px[k:], sz[k:]
            if st == slice_win[0]:
                start_book = dict(book)
        for i in range(len(ts)):
            if roles[i] == role:
                key = (bool(bid[i]), int(px[i]))
                if sz[i] > 0:
                    book[key] = int(sz[i])
                else:
                    book.pop(key, None)
        if pending and pending[0] > (ts[-1] if len(ts) else 0) and pf.num_row_groups:
            pass
        if not pending and (len(ts) == 0 or ts[-1] > slice_win[1]):
            # still need end book; keep going until slice end passed
            if ts[-1] > slice_win[1] if len(ts) else False:
                break
    end_book = dict(book)
    return snaps, start_book or {}, end_book


def stack_report(book: dict, ref_ticks: int, label: str):
    asks = sorted((p, s) for (b, p), s in book.items() if not b and s > 0)
    bids = sorted((p, s) for (b, p), s in book.items() if b and s > 0)
    print(f"\n-- {label} (ref {ref_ticks*0.25:.2f}) --")
    for name, levels, rng in (("ASK above", asks, range(0, 41)), ("BID below", bids, range(0, 41))):
        sel = [(p, s) for p, s in levels if 0 <= (p - ref_ticks if name == "ASK above" else ref_ticks - p) <= 40]
        tot = sum(s for _, s in sel)
        big = [(p, s) for p, s in sel if s >= (30 if tot else 0)]
        print(f"{name}: total 0-10pt={tot}  levels={len(sel)}")
        print("   " + " ".join(f"{p*0.25:.2f}:{s}" for p, s in sel if s >= 20))


def main():
    t0, t1 = ts_of("2026-09-10 15:23:30"), ts_of("2026-09-10 15:26:30")
    for role in ("NQ", "ES"):
        tr = load_trades(role)
        print(f"\n===== {role} 250ms bars (O H L C vol delta ntrades maxprint) =====")
        for r in tape_bars(tr, t0, t1):
            print(f"{fmt_ts(r[0])} {r[1]:9.2f} {r[2]:9.2f} {r[3]:9.2f} {r[4]:9.2f} "
                  f"vol={r[5]:6.0f} d={r[6]:+6.0f} n={r[7]:4d} max={r[8]:.0f}")

    # Provisional slice window (refined from bars above on re-run)
    slice_win = (ts_of("2026-09-10 15:24:31"), ts_of("2026-09-10 15:24:52"))
    snap_times = [ts_of("2026-09-10 15:24:00"), ts_of("2026-09-10 15:24:20"),
                  slice_win[0], ts_of("2026-09-10 15:24:45")]
    for role in ("NQ", "ES"):
        tr = load_trades(role)
        ts = tr.column("ts_ns").to_numpy().astype(np.int64)
        px = tr.column("price").to_numpy().astype(np.int64)
        i = np.searchsorted(ts, slice_win[0])
        ref = int(px[i])
        snaps, start_book, end_book = replay_book(role, snap_times, slice_win)
        for st, bk in snaps.items():
            stack_report(bk, ref, f"{role} book @ {fmt_ts(st)}")
        # traded vs displayed inside the stack zone (asks from ref to ref+40 ticks)
        m = (ts >= slice_win[0]) & (ts <= slice_win[1])
        sz = tr.column("size").to_numpy().astype(np.int64)[m]
        buy = tr.column("is_buy_aggressor").to_numpy().astype(bool)[m]
        pxw = px[m]
        print(f"\n-- {role} traded-vs-displayed in slice {fmt_ts(slice_win[0])}-{fmt_ts(slice_win[1])} --")
        print("price   displayed@start  buyVol  endSize  impliedPulled(displayed+? )")
        for p in range(ref, ref + 41):
            disp = start_book.get((False, p), 0)
            tv = int(sz[(pxw == p) & buy].sum())
            if disp or tv:
                print(f"{p*0.25:9.2f} {disp:8d} {tv:8d} {end_book.get((False, p), 0):8d}")


if __name__ == "__main__":
    main()
