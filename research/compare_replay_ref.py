"""Compare Bookmap MAT2 SIGNAL log lines to an offline reference CSV.

Usage:
  python -m research.compare_replay_ref --ref results/MAT2_REF_021026_confirm_vol_k1.csv \\
      --log %TEMP%\\bookmap_mat2.log

Parses lines containing ``MAT2 SIGNAL`` or ``MAT2 SIGNAL(confirm)`` with ``tsNs=``.
Match window default ±250 ms and same side; reports match rate.
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


SIGNAL_RE = re.compile(
    r"MAT2 SIGNAL(?:\(confirm\))?\s+(LONG|SHORT).*?tsNs=(\d+)",
    re.IGNORECASE,
)


def load_ref(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({
                "side": row["side"].upper(),
                "ts_ns": int(row["ts_ns"]),
                "entry_px": float(row.get("entry_px") or 0),
            })
    return rows


def load_log_signals(path: Path) -> list[dict]:
    out = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for m in SIGNAL_RE.finditer(text):
        out.append({"side": m.group(1).upper(), "ts_ns": int(m.group(2))})
    return out


def match(ref: list[dict], live: list[dict], window_ms: float) -> dict:
    window_ns = int(window_ms * 1_000_000)
    used = set()
    matched = 0
    details = []
    for r in ref:
        best = None
        best_abs = None
        for j, L in enumerate(live):
            if j in used:
                continue
            if L["side"] != r["side"]:
                continue
            d = abs(L["ts_ns"] - r["ts_ns"])
            if d <= window_ns and (best_abs is None or d < best_abs):
                best_abs = d
                best = j
        if best is not None:
            used.add(best)
            matched += 1
            details.append({"ref_ts": r["ts_ns"], "lag_ms": best_abs / 1e6, "side": r["side"]})
        else:
            details.append({"ref_ts": r["ts_ns"], "lag_ms": None, "side": r["side"]})
    unmatched_live = [live[j] for j in range(len(live)) if j not in used]
    return {
        "ref_n": len(ref),
        "live_n": len(live),
        "matched": matched,
        "match_rate": round(matched / len(ref), 4) if ref else float("nan"),
        "unmatched_ref": len(ref) - matched,
        "unmatched_live": len(unmatched_live),
        "details": details,
        "extra_live": unmatched_live[:20],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True, help="Reference CSV from export_mat2_ref")
    ap.add_argument("--log", required=True, help="Bookmap log excerpt / full log")
    ap.add_argument("--window-ms", type=float, default=250.0)
    ap.add_argument("--out", default="", help="Optional markdown report path")
    args = ap.parse_args()

    ref = load_ref(Path(args.ref))
    live = load_log_signals(Path(args.log))
    result = match(ref, live, args.window_ms)

    lines = [
        "# MAT2 replay vs reference",
        "",
        f"- ref: `{args.ref}` (n={result['ref_n']})",
        f"- log: `{args.log}` (signals={result['live_n']})",
        f"- window: ±{args.window_ms:g} ms",
        f"- **matched {result['matched']}/{result['ref_n']} "
        f"({100*result['match_rate']:.1f}%)**",
        f"- unmatched ref: {result['unmatched_ref']}; extra live: {result['unmatched_live']}",
        "",
    ]
    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    # Always print compact JSON-ish summary for scripting
    print(
        f"MATCH_RATE={result['match_rate']} matched={result['matched']} "
        f"ref={result['ref_n']} live={result['live_n']}"
    )


if __name__ == "__main__":
    main()
