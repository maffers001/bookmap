"""Build a Bookmap VAP seed directory from tick / daily parquet.

Seed layout (consumed by ``LvnVapSeedFile``):

    data/irt/vap_seed_ES/
      meta.json
      daily.csv     # day,row_tick,vol  (whole days before through day)
      minutes.csv   # minute,row_tick,vol  (through day up to --through)

Example (morning live, join 13:55 UK)::

    .venv\\Scripts\\python.exe -m research.irt_lvn.build_vap_seed --market both --through 13:55

Replay (cut at end of a study day)::

    .venv\\Scripts\\python.exe -m research.irt_lvn.build_vap_seed --market ES --day 2025-10-15 --through 21:00
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb

from research.config import ROOT
from research.irt_lvn.lvn_profile import MARKET_CFG, TICK, UK, daily_vap_path


def minute_vap_path(market: str) -> Path:
    return ROOT / "data" / "irt" / f"vap_minute_{market}.parquet"


def parse_hm(s: str) -> time:
    parts = s.strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return time(h, m)


def build_one(
    market: str,
    *,
    through_day: date,
    through_hm: time,
    lookback_days: int,
    out_root: Path,
) -> Path:
    cfg = MARKET_CFG[market]
    through = datetime.combine(through_day, through_hm, tzinfo=UK)
    # Headroom beyond UI default so knobs can go longer without rebuild
    window = max(lookback_days, cfg["window_days"] + 30)
    start_day = through_day - timedelta(days=window - 1)

    out_dir = out_root / f"vap_seed_{market}"
    out_dir.mkdir(parents=True, exist_ok=True)

    daily_path = daily_vap_path(market)
    minute_path = minute_vap_path(market)
    tick_path = ROOT / cfg["tick_path"]
    con = duckdb.connect()

    # --- daily.csv: whole days in [start_day, through_day)
    daily_rows: list[tuple[str, int, float]] = []
    if daily_path.exists():
        dp = str(daily_path).replace("\\", "/")
        q = f"""
            SELECT cast(day AS DATE) AS d, row_tick, cast(sum(vol) AS DOUBLE) AS vol
            FROM read_parquet('{dp}')
            WHERE day >= DATE '{start_day.isoformat()}'
              AND day <  DATE '{through_day.isoformat()}'
            GROUP BY 1, 2
            HAVING vol > 0
            ORDER BY 1, 2
        """
        for d, r, v in con.execute(q).fetchall():
            daily_rows.append((d.isoformat() if hasattr(d, "isoformat") else str(d), int(r), float(v)))
    else:
        print(f"WARN {market}: no {daily_path.name} — daily.csv empty; rely on minutes/ticks")

    daily_csv = out_dir / "daily.csv"
    with daily_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["day", "row_tick", "vol"])
        w.writerows(daily_rows)

    # --- minutes.csv: through_day from 00:00 through through timestamp
    minute_rows: list[tuple[str, int, float]] = []
    end_s = through.strftime("%Y-%m-%d %H:%M:%S")
    start_s = f"{through_day.isoformat()} 00:00:00"

    if minute_path.exists():
        mp = str(minute_path).replace("\\", "/")
        q = f"""
            SELECT minute, row_tick, cast(vol AS DOUBLE) AS vol
            FROM read_parquet('{mp}')
            WHERE minute >= TIMESTAMP '{start_s}'
              AND minute <= TIMESTAMP '{end_s}'
              AND vol > 0
            ORDER BY 1, 2
        """
        for m, r, v in con.execute(q).fetchall():
            if hasattr(m, "strftime"):
                ms = m.strftime("%Y-%m-%d %H:%M:%S")
            else:
                ms = str(m).replace("T", " ")[:19]
            minute_rows.append((ms, int(r), float(v)))
    elif tick_path.exists():
        src = str(tick_path).replace("\\", "/")
        q = f"""
            SELECT date_trunc('minute', DATE) AS minute,
                   cast(round(PRICE / {TICK}) AS BIGINT) AS row_tick,
                   cast(sum(TICKVOL) AS DOUBLE) AS vol
            FROM read_csv('{src}', delim='\\t', header=true, sample_size=20000)
            WHERE DATE >= TIMESTAMP '{start_s}'
              AND DATE <= TIMESTAMP '{end_s}'
            GROUP BY 1, 2
            HAVING vol > 0
            ORDER BY 1, 2
        """
        for m, r, v in con.execute(q).fetchall():
            if hasattr(m, "strftime"):
                ms = m.strftime("%Y-%m-%d %H:%M:%S")
            else:
                ms = str(m).replace("T", " ")[:19]
            minute_rows.append((ms, int(r), float(v)))
    else:
        raise SystemExit(f"{market}: need minute parquet or tick file")

    minutes_csv = out_dir / "minutes.csv"
    with minutes_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["minute", "row_tick", "vol"])
        w.writerows(minute_rows)

    meta = {
        "market": market,
        "through_uk": through.strftime("%Y-%m-%d %H:%M:%S"),
        "tick": TICK,
        "window_days_available": window,
        "sensitivity_default": cfg["sensitivity"],
        "window_days_default": cfg["window_days"],
        "n_daily_rows": len(daily_rows),
        "n_minute_rows": len(minute_rows),
        "note": f"join with Bookmap after {through_hm.strftime('%H:%M')} UK",
    }
    if not daily_rows and not minute_rows:
        raise SystemExit(
            f"{market}: seed empty for through={through} — check vap_daily_/vap_minute_ "
            f"coverage or pass --day inside the parquet range"
        )
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(
        f"{market}: wrote {out_dir} daily={len(daily_rows)} minutes={len(minute_rows)} "
        f"through={meta['through_uk']} lookback≈{window}d",
        flush=True,
    )
    return out_dir


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", choices=["ES", "NQ", "both"], default="both")
    ap.add_argument(
        "--through",
        default="13:55",
        help="UK HH:MM seed cut / Join with Bookmap (default 13:55)",
    )
    ap.add_argument(
        "--day",
        default=None,
        help="UK calendar day YYYY-MM-DD (default: today UK)",
    )
    ap.add_argument(
        "--lookback-days",
        type=int,
        default=0,
        help="Override headroom lookback (0 = market default + 30)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data" / "irt",
        help="Parent directory for vap_seed_{MARKET}/",
    )
    args = ap.parse_args()
    through_hm = parse_hm(args.through)
    if args.day:
        through_day = date.fromisoformat(args.day)
    else:
        through_day = datetime.now(UK).date()
    markets = ["ES", "NQ"] if args.market == "both" else [args.market]
    for m in markets:
        lb = args.lookback_days
        if lb <= 0:
            lb = MARKET_CFG[m]["window_days"] + 30
        build_one(m, through_day=through_day, through_hm=through_hm, lookback_days=lb, out_root=args.out)


if __name__ == "__main__":
    main()
