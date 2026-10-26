"""Build developing LVN stacks from tick-derived minute VAP (IRT Profile semantics).

Single forward pass: sliding window over minute VAP, emit valleys each snapshot.

    .venv\\Scripts\\python.exe -m research.irt_lvn.build_lvn_stack --market both --every 5
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict, deque
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from research.config import ROOT
from research.irt_lvn.lvn_profile import MARKET_CFG, TICK, UK, detect_nodes, row_to_price

OUT_DEFAULT = ROOT / "results" / "LVN_STACK"


def minute_vap_path(market: str) -> Path:
    return ROOT / "data" / "irt" / f"vap_minute_{market}.parquet"


def build_market(
    market: str,
    *,
    out_dir: Path,
    start_day: date | None,
    end_day: date | None,
    every_n_min: int,
    session_start: time = time(8, 0),
    session_end: time = time(21, 0),
) -> Path:
    cfg = MARKET_CFG[market]
    mp = minute_vap_path(market)
    if not mp.exists():
        raise SystemExit(
            f"Missing {mp.name} — run: python research/irt_lvn/_build_minute_vap.py"
        )
    mp_s = str(mp).replace("\\", "/")
    win = timedelta(days=cfg["window_days"])
    sens = cfg["sensitivity"]

    con = duckdb.connect()
    d0, d1 = con.execute(
        f"SELECT min(cast(minute AS DATE)), max(cast(minute AS DATE)) "
        f"FROM read_parquet('{mp_s}')"
    ).fetchone()
    if start_day is None:
        start_day = d0 + timedelta(days=cfg["window_days"])
    if end_day is None:
        end_day = d1

    print(
        f"{market}: emit {start_day}→{end_day} window={cfg['window_days']}d "
        f"sens={sens} ticks every={every_n_min}m",
        flush=True,
    )

    # Stream all minute rows up through end_day (need lookback before start_day)
    stream_start = start_day - timedelta(days=cfg["window_days"] + 1)
    cur = con.execute(
        f"""
        SELECT minute, row_tick, vol
        FROM read_parquet('{mp_s}')
        WHERE cast(minute AS DATE) BETWEEN DATE '{stream_start}' AND DATE '{end_day}'
        ORDER BY minute, row_tick
        """
    )

    # Sliding window of per-minute maps: deque[(datetime, dict[row,vol])]
    window: deque[tuple[datetime, dict[int, float]]] = deque()
    vap: dict[int, float] = defaultdict(float)
    current_minute: datetime | None = None
    minute_map: dict[int, float] = {}

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"LVN_STACK_{market}.csv"
    n_rows = 0
    n_snap = 0

    def flush_minute(m: datetime, mmap: dict[int, float]) -> None:
        nonlocal n_rows, n_snap
        # add to window
        window.append((m, mmap))
        for r, v in mmap.items():
            vap[r] += v
        # expire
        cutoff = m - win
        while window and window[0][0] <= cutoff:
            _, old = window.popleft()
            for r, v in old.items():
                nv = vap[r] - v
                if nv <= 1e-9:
                    vap.pop(r, None)
                else:
                    vap[r] = nv
        # emit?
        if m.date() < start_day or m.date() > end_day:
            return
        mt = m.timetz().replace(tzinfo=None) if m.tzinfo else m.time()
        if not (session_start <= mt <= session_end):
            return
        if every_n_min > 1 and (m.minute % every_n_min) != 0:
            return
        valleys, _ = detect_nodes(
            dict(vap), sensitivity=sens, strict=True, skip_edge=True, smooth=0
        )
        # mid ≈ VPOC of this minute
        if mmap:
            mid_row = max(mmap.items(), key=lambda kv: kv[1])[0]
            mid = row_to_price(mid_row)
        else:
            mid = None
        day_s = m.strftime("%Y-%m-%d")
        tim_s = m.strftime("%H:%M:%S")
        for v in sorted(valleys, key=lambda x: x.price):
            w.writerow(
                [
                    day_s,
                    tim_s,
                    f"{v.price:.2f}",
                    f"{v.volume:.1f}",
                    "LVN",
                    f"{mid:.2f}" if mid is not None else "",
                    len(valleys),
                ]
            )
            n_rows += 1
        n_snap += 1
        if n_snap % 500 == 0:
            print(f"  {market} {m} snaps={n_snap} rows={n_rows}", flush=True)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["date", "time_uk", "price", "node_volume", "kind", "mid", "n_nodes"]
        )
        while True:
            batch = cur.fetchmany(200_000)
            if not batch:
                break
            for minute, row_tick, vol in batch:
                if getattr(minute, "tzinfo", None) is None:
                    minute = minute.replace(tzinfo=UK)
                else:
                    minute = minute.astimezone(UK)
                if current_minute is None:
                    current_minute = minute
                    minute_map = {}
                if minute != current_minute:
                    flush_minute(current_minute, minute_map)
                    current_minute = minute
                    minute_map = {}
                minute_map[int(row_tick)] = minute_map.get(int(row_tick), 0.0) + float(vol)
        if current_minute is not None and minute_map:
            flush_minute(current_minute, minute_map)

    (out_dir / f"summary_{market}.md").write_text(
        "\n".join(
            [
                f"# LVN stack — {market}",
                "",
                f"- Window **{cfg['window_days']}d** · sensitivity **{sens} ticks** · tick {TICK}",
                f"- Emit range: {start_day} → {end_day} (08:00–21:00 UK, every {every_n_min}m)",
                f"- Snapshots: **{n_snap}** · valley rows: **{n_rows}**",
                f"- File: `{out_path.name}`",
                "",
                "See also `results/LVN_PROFILE_VALIDATE/summary.md` for IRT parity notes.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"Wrote {out_path} snaps={n_snap} rows={n_rows}", flush=True)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", choices=("ES", "NQ", "both"), default="both")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--start", default=None, help="YYYY-MM-DD first emit day")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD last emit day")
    ap.add_argument("--every", type=int, default=5, help="Emit every N minutes (default 5)")
    args = ap.parse_args()
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    markets = ["ES", "NQ"] if args.market == "both" else [args.market]
    for m in markets:
        build_market(
            m, out_dir=args.out, start_day=start, end_day=end, every_n_min=args.every
        )


if __name__ == "__main__":
    main()
