"""Validate Session Extractor output for one session folder."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# UK summer time approx (BST = UTC+1). Fine for Sep validation display.
UK = timezone(timedelta(hours=1))


def fmt(ns: int | None) -> str | None:
    if ns is None or ns < 0:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).astimezone(UK).isoformat()


def main(session_dir: Path) -> int:
    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    print("=== sizes ===")
    for p in sorted(session_dir.iterdir()):
        print(f"  {p.name}: {p.stat().st_size / (1024**3):.3f} GB")

    print("\n=== meta ===")
    print("finished:", meta.get("finished"))
    print("first UK:", fmt(meta.get("first_ts_ns")))
    print("last  UK:", fmt(meta.get("last_ts_ns")))
    for k in ("trade_count", "depth_count", "mbo_count", "skipped_count"):
        print(f"  {k}: {meta.get(k)}")
    print("instruments:")
    for i in meta.get("instruments", []):
        print(" ", i)

    trade_path = session_dir / "trades.ndjson"
    sym = Counter()
    role = Counter()
    buy = Counter()
    n = bad = nonzero = 0
    first_ts = last_ts = None
    first_line = last_line = None
    min_px: dict[str, float] = {}
    max_px: dict[str, float] = {}

    with open(trade_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                r = json.loads(line)
            except Exception:
                bad += 1
                continue
            if first_line is None:
                first_line = r
            last_line = r
            ts = int(r["ts_ns"])
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            s = r.get("symbol") or "?"
            sym[s] += 1
            role[r.get("role") or "?"] += 1
            buy[bool(r.get("is_buy_aggressor"))] += 1
            if int(r.get("size") or 0) > 0:
                nonzero += 1
            px = float(r.get("price") or 0)
            min_px[s] = px if s not in min_px else min(min_px[s], px)
            max_px[s] = px if s not in max_px else max(max_px[s], px)

    print("\n=== trades.ndjson ===")
    print(f"lines={n} nonzero_size={nonzero} bad_json={bad}")
    print("file first/last UK:", fmt(first_ts), fmt(last_ts))
    print("by symbol:", dict(sym))
    print("by role:", dict(role))
    print("buy_aggressor:", dict(buy))
    print("price ranges:", {s: (min_px[s], max_px[s]) for s in sorted(min_px)})
    print("first:", first_line)
    print("last:", last_line)

    depth_path = session_dir / "depth.ndjson"
    dn = 0
    dsym = Counter()
    dfirst = dlast = None
    with open(depth_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            dn += 1
            if dn == 1 or dn % 50 == 0:
                r = json.loads(line)
                if dfirst is None:
                    dfirst = r
                dlast = r
                if dn % 50 == 0:
                    dsym[r.get("symbol") or "?"] += 50

    print("\n=== depth.ndjson ===")
    print(f"lines={dn}")
    print("approx symbol mix (every 50th x50):", dict(dsym))
    if dfirst:
        print("first UK:", fmt(int(dfirst["ts_ns"])), dfirst.get("symbol"), dfirst.get("size"))
    if dlast:
        print("sample near end UK:", fmt(int(dlast["ts_ns"])), dlast.get("symbol"))

    mbo_path = session_dir / "mbo.ndjson"
    mn = 0
    mop = Counter()
    msym = Counter()
    with open(mbo_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            mn += 1
            if mn % 100 == 0:
                r = json.loads(line)
                mop[r.get("op") or "?"] += 100
                msym[r.get("symbol") or "?"] += 100

    print("\n=== mbo.ndjson ===")
    print(f"lines={mn}")
    print("approx op mix:", dict(mop))
    print("approx symbol mix:", dict(msym))

    print("\n=== meta vs file line counts ===")
    print(f"trades meta={meta.get('trade_count')} file={n} delta={(meta.get('trade_count') or 0) - n}")
    print(f"depth  meta={meta.get('depth_count')} file={dn} delta={(meta.get('depth_count') or 0) - dn}")
    print(f"mbo    meta={meta.get('mbo_count')} file={mn} delta={(meta.get('mbo_count') or 0) - mn}")

    ok = n > 0 and dn > 0 and bad == 0 and first_ts is not None and last_ts is not None
    has_nq = any(s.startswith("NQ") and not s.startswith("MNQ") for s in sym)
    has_es = any(s.startswith("ES") and not s.startswith("MES") for s in sym)
    print("\n=== verdict ===")
    print("readable_json:", bad == 0)
    print("has_NQZ:", has_nq)
    print("has_ES:", has_es)
    print("has_MNQ:", any(s.startswith("MNQ") for s in sym))
    print("ok_for_pipeline:", ok and has_nq and has_es)
    return 0 if ok else 1


if __name__ == "__main__":
    session = Path(sys.argv[1] if len(sys.argv) > 1 else "data/raw/210926")
    raise SystemExit(main(session))
