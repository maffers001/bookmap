"""Parity check: Python HV Extreme fires vs Bookmap log HV EXT FIRE lines.

Matches by (side, extreme_tick, trigger_tick) against tipChart-format log lines
whose prices fall inside the extract's NQ range (replay wall-clock ≠ market time).

Usage:
  python -m research.hvext.parity_hvext --session 191026
"""
from __future__ import annotations

import argparse
import gzip
import re
from pathlib import Path

from research.features.hv_extreme import HvExtremeConfig, hv_extreme_events
from research.run_experiment import _load_trades, load_config

FIRE_RE = re.compile(
    r"HV EXT FIRE\s+(LONG|SHORT)\s+extreme=(\d+)"
    r"(?:\s+tipChart=([\d.]+))?"
    r".*?trigger=(\d+)"
    r".*?move=(\d+)"
)


def _read_text(path: Path) -> str:
    if path.suffix == ".gz" or path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return f.read()
    return path.read_text(encoding="utf-8", errors="replace")


def parse_log_fires(log_paths: list[Path], *, px_lo: float, px_hi: float) -> list[dict]:
    out = []
    for path in log_paths:
        try:
            text = _read_text(path)
        except OSError as e:
            print(f"skip {path}: {e}")
            continue
        for line in text.splitlines():
            m = FIRE_RE.search(line)
            if not m:
                continue
            tip = m.group(3)
            move = int(m.group(5))
            # Skip pre-fix logs (Move pts treated as ticks → move=20).
            if tip is None or move < 40:
                continue
            extreme = int(m.group(2))
            trigger = int(m.group(4))
            if not (px_lo <= extreme <= px_hi and px_lo <= trigger <= px_hi):
                continue
            side = 1 if m.group(1) == "LONG" else -1
            out.append(
                {
                    "side": side,
                    "extreme": extreme,
                    "trigger": trigger,
                    "src": path.name,
                    "line": line[:220],
                }
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="191026")
    ap.add_argument("--log-dir", default=r"C:\Bookmap\Logs")
    ap.add_argument("--log-glob", default="log_20261019*")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    nq = _load_trades(parquet_dir, args.session, "NQ")
    if nq.num_rows == 0:
        print(f"No NQ trades for session {args.session} — extract first:")
        print(f"  python -m research.extract.to_parquet --session {args.session}")
        return 1

    px = nq.column("price").to_numpy()
    px_lo, px_hi = float(px.min()), float(px.max())

    panel = HvExtremeConfig(
        move_pts=20,
        confirm_pts=10,
        peak_band_pts=5,
        high_vol_mult=2.0,
        min_path_efficiency=0.0,
        max_pullback_pts=5.0,
        max_leg_sec=0.0,
        lookback_min=15,
        cooldown_sec=30,
    )
    ev = hv_extreme_events(nq, panel, window_ns=None, attach_med_r30=False)
    print(f"Python fires: {ev.num_rows} (NQ rows={nq.num_rows}, px=[{px_lo:.0f},{px_hi:.0f}])")

    log_dir = Path(args.log_dir)
    logs = sorted(
        p for p in log_dir.glob(args.log_glob) if p.is_file() and "common" in p.name
    )
    fires = parse_log_fires(logs, px_lo=px_lo, px_hi=px_hi)
    log_triples = {(f["side"], f["extreme"], f["trigger"]) for f in fires}
    print(
        f"Log FIRE lines (tipChart, in-range): {len(fires)} "
        f"unique triples={len(log_triples)} from {len(logs)} files"
    )

    report = [
        f"# HV Extreme parity — session {args.session}",
        "",
        f"Panel: Move20 Confirm10 Band5 HV2 pullback5 lookback15 cooldown30",
        f"Python fires: {ev.num_rows}",
        f"Log FIRE lines (tipChart + price in extract range): {len(fires)} "
        f"(unique triples {len(log_triples)})",
        "",
    ]

    if ev.num_rows == 0:
        report.append("Python produced 0 fires.")
        out = Path(args.out) if args.out else Path("results/EXP-0037/parity_report.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(report), encoding="utf-8")
        return 1

    py_rows = list(
        zip(
            ev.column("side").to_pylist(),
            ev.column("extreme_tick").to_pylist(),
            ev.column("trigger_tick").to_pylist(),
        )
    )
    matched = 0
    for side, extreme, trigger in py_rows:
        ok = (side, extreme, trigger) in log_triples
        matched += int(ok)
        status = "OK" if ok else "MISS"
        msg = f"  {status} side={side:+d} extreme={extreme} trigger={trigger}"
        print(msg)
        report.append(msg)

    extras = len(log_triples - set(py_rows))
    summary = (
        f"Matched {matched}/{len(py_rows)} python fires in logs; "
        f"log extras (other settings/replays/live): {extras}"
    )
    print(summary)
    report += ["", summary, ""]
    if matched == len(py_rows):
        report.append(
            "PASS: every Python fire with panel settings appears in Bookmap logs "
            "(log wall-clock ignored; match by side/extreme/trigger)."
        )
    else:
        report.append("FAIL: unmatched Python fires — investigate tracker divergence.")

    out = Path(args.out) if args.out else Path("results/EXP-0037/parity_report.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(report), encoding="utf-8")
    print(f"Wrote {out}")
    return 0 if matched == len(py_rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
