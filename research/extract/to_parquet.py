"""
Convert Session Extractor NDJSON → partitioned Parquet.

Usage:
  python -m research.extract.to_parquet
  python -m research.extract.to_parquet --session feed_20260918_091523_227
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from research.config import load_config


TRADE_SCHEMA = pa.schema([
    ("ts_ns", pa.int64()),
    ("alias", pa.string()),
    ("role", pa.string()),
    ("symbol", pa.string()),
    ("price", pa.float64()),
    ("size", pa.int32()),
    ("is_buy_aggressor", pa.bool_()),
    ("aggressor_order_id", pa.string()),
    ("is_exec_start", pa.bool_()),
    ("is_exec_end", pa.bool_()),
    ("session_id", pa.string()),
])

DEPTH_SCHEMA = pa.schema([
    ("ts_ns", pa.int64()),
    ("alias", pa.string()),
    ("role", pa.string()),
    ("symbol", pa.string()),
    ("is_bid", pa.bool_()),
    ("price_ticks", pa.int32()),
    ("size", pa.int32()),
    ("session_id", pa.string()),
])

MBO_SCHEMA = pa.schema([
    ("ts_ns", pa.int64()),
    ("alias", pa.string()),
    ("role", pa.string()),
    ("symbol", pa.string()),
    ("op", pa.string()),
    ("order_id", pa.string()),
    ("is_bid", pa.bool_()),
    ("price_ticks", pa.int32()),
    ("size", pa.int32()),
    ("session_id", pa.string()),
])


def _iter_ndjson(path: Path, batch_size: int = 100_000):
    if not path.exists():
        return
    batch = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            batch.append(json.loads(line))
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch


def _trades_table(rows: list[dict], session_id: str) -> pa.Table:
    return pa.table({
        "ts_ns": [int(r["ts_ns"]) for r in rows],
        "alias": [r.get("alias", "") for r in rows],
        "role": [r.get("role", "OTHER") for r in rows],
        "symbol": [r.get("symbol", "") for r in rows],
        "price": [float(r["price"]) for r in rows],
        "size": [int(r["size"]) for r in rows],
        "is_buy_aggressor": [bool(r.get("is_buy_aggressor")) for r in rows],
        "aggressor_order_id": [r.get("aggressor_order_id") or "" for r in rows],
        "is_exec_start": [bool(r.get("is_exec_start")) for r in rows],
        "is_exec_end": [bool(r.get("is_exec_end")) for r in rows],
        "session_id": [session_id] * len(rows),
    }, schema=TRADE_SCHEMA)


def _depth_table(rows: list[dict], session_id: str) -> pa.Table:
    return pa.table({
        "ts_ns": [int(r["ts_ns"]) for r in rows],
        "alias": [r.get("alias", "") for r in rows],
        "role": [r.get("role", "OTHER") for r in rows],
        "symbol": [r.get("symbol", "") for r in rows],
        "is_bid": [bool(r.get("is_bid")) for r in rows],
        "price_ticks": [int(r["price_ticks"]) for r in rows],
        "size": [int(r["size"]) for r in rows],
        "session_id": [session_id] * len(rows),
    }, schema=DEPTH_SCHEMA)


def _mbo_table(rows: list[dict], session_id: str) -> pa.Table:
    return pa.table({
        "ts_ns": [int(r["ts_ns"]) for r in rows],
        "alias": [r.get("alias", "") for r in rows],
        "role": [r.get("role", "OTHER") for r in rows],
        "symbol": [r.get("symbol", "") for r in rows],
        "op": [r.get("op", "") for r in rows],
        "order_id": [r.get("order_id") or "" for r in rows],
        "is_bid": [bool(r["is_bid"]) if "is_bid" in r else None for r in rows],
        "price_ticks": [int(r.get("price_ticks") or 0) for r in rows],
        "size": [int(r.get("size") or 0) for r in rows],
        "session_id": [session_id] * len(rows),
    }, schema=MBO_SCHEMA)


def convert_session(session_dir: Path, parquet_root: Path) -> dict:
    session_id = session_dir.name
    meta_path = session_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    out_base = parquet_root / f"session_id={session_id}"
    out_base.mkdir(parents=True, exist_ok=True)

    stats = {"session_id": session_id, "trades": 0, "depth": 0, "mbo": 0}

    # Trades
    trade_parts = []
    for batch in _iter_ndjson(session_dir / "trades.ndjson"):
        trade_parts.append(_trades_table(batch, session_id))
        stats["trades"] += len(batch)
    if trade_parts:
        table = pa.concat_tables(trade_parts)
        pq.write_table(table, out_base / "trades.parquet", compression="zstd")

    # Depth (large) — write row-group batches via ParquetWriter
    depth_path = out_base / "depth.parquet"
    depth_writer = None
    try:
        for batch in _iter_ndjson(session_dir / "depth.ndjson", batch_size=200_000):
            tbl = _depth_table(batch, session_id)
            if depth_writer is None:
                depth_writer = pq.ParquetWriter(depth_path, DEPTH_SCHEMA, compression="zstd")
            depth_writer.write_table(tbl)
            stats["depth"] += len(batch)
    finally:
        if depth_writer is not None:
            depth_writer.close()

    # MBO
    mbo_parts = []
    for batch in _iter_ndjson(session_dir / "mbo.ndjson"):
        mbo_parts.append(_mbo_table(batch, session_id))
        stats["mbo"] += len(batch)
    if mbo_parts:
        pq.write_table(pa.concat_tables(mbo_parts), out_base / "mbo.parquet", compression="zstd")

    # Copy meta
    (out_base / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    stats["meta"] = meta
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NDJSON extract → Parquet")
    parser.add_argument("--session", help="Single session_id under raw_dir")
    parser.add_argument("--raw-dir", help="Override raw_dir")
    parser.add_argument("--parquet-dir", help="Override parquet_dir")
    args = parser.parse_args(argv)

    cfg = load_config()
    raw_dir = Path(args.raw_dir or cfg["raw_dir"])
    parquet_dir = Path(args.parquet_dir or cfg["parquet_dir"])
    parquet_dir.mkdir(parents=True, exist_ok=True)

    if args.session:
        sessions = [raw_dir / args.session]
    else:
        sessions = sorted(p for p in raw_dir.iterdir() if p.is_dir()) if raw_dir.exists() else []

    if not sessions:
        print(f"No sessions found under {raw_dir}", file=sys.stderr)
        print("Extract with Bookmap Session Extractor first.", file=sys.stderr)
        return 1

    for session_dir in sessions:
        if not session_dir.is_dir():
            print(f"Skip missing {session_dir}", file=sys.stderr)
            continue
        print(f"Converting {session_dir.name} ...")
        stats = convert_session(session_dir, parquet_dir)
        print(
            f"  trades={stats['trades']} depth={stats['depth']} mbo={stats['mbo']} "
            f"-> {parquet_dir / ('session_id=' + stats['session_id'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
