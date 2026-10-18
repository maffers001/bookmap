"""
Convert Session Extractor NDJSON → partitioned Parquet.

Usage:
  python -m research.extract.to_parquet
  python -m research.extract.to_parquet --session 011026
  python -m research.extract.to_parquet --only depth,mbo --skip-existing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.json as paj
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

# Explicit parse options for pyarrow.json (NDJSON)
_DEPTH_PARSE = paj.ParseOptions(newlines_in_values=False, unexpected_field_behavior="ignore")
_DEPTH_READ = paj.ReadOptions(use_threads=True, block_size=1 << 20)
_MBO_PARSE = _DEPTH_PARSE
_MBO_READ = _DEPTH_READ
_TRADE_PARSE = _DEPTH_PARSE
_TRADE_READ = _DEPTH_READ


def _iter_ndjson(path: Path, batch_size: int = 100_000):
    """Fallback line-by-line reader (trades / small files)."""
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


def _normalize_depth_batch(tbl: pa.Table, session_id: str) -> pa.Table:
    """Map raw NDJSON columns → DEPTH_SCHEMA, fill missing, add session_id."""
    n = tbl.num_rows
    names = set(tbl.column_names)

    def col(name: str, dtype, default):
        if name in names:
            c = tbl.column(name)
            if pa.types.is_null(c.type):
                return pa.array([default] * n, type=dtype)
            try:
                return c.cast(dtype)
            except Exception:
                return pa.array(
                    [default if v is None else v for v in c.to_pylist()],
                    type=dtype,
                )
        return pa.array([default] * n, type=dtype)

    return pa.table({
        "ts_ns": col("ts_ns", pa.int64(), 0),
        "alias": col("alias", pa.string(), ""),
        "role": col("role", pa.string(), "OTHER"),
        "symbol": col("symbol", pa.string(), ""),
        "is_bid": col("is_bid", pa.bool_(), False),
        "price_ticks": col("price_ticks", pa.int32(), 0),
        "size": col("size", pa.int32(), 0),
        "session_id": pa.array([session_id] * n, type=pa.string()),
    }, schema=DEPTH_SCHEMA)


def _normalize_mbo_batch(tbl: pa.Table, session_id: str) -> pa.Table:
    n = tbl.num_rows
    names = set(tbl.column_names)

    def col(name: str, dtype, default):
        if name in names:
            c = tbl.column(name)
            if pa.types.is_null(c.type):
                return pa.array([default] * n, type=dtype)
            try:
                return c.cast(dtype)
            except Exception:
                vals = []
                for v in c.to_pylist():
                    if v is None:
                        vals.append(default)
                    elif dtype == pa.string():
                        vals.append(str(v))
                    else:
                        vals.append(v)
                return pa.array(vals, type=dtype)
        return pa.array([default] * n, type=dtype)

    # order_id may be int in some extracts
    if "order_id" in names:
        oid = tbl.column("order_id")
        order_id = pa.array(
            ["" if v is None else str(v) for v in oid.to_pylist()],
            type=pa.string(),
        )
    else:
        order_id = pa.array([""] * n, type=pa.string())

    return pa.table({
        "ts_ns": col("ts_ns", pa.int64(), 0),
        "alias": col("alias", pa.string(), ""),
        "role": col("role", pa.string(), "OTHER"),
        "symbol": col("symbol", pa.string(), ""),
        "op": col("op", pa.string(), ""),
        "order_id": order_id,
        "is_bid": col("is_bid", pa.bool_(), False),
        "price_ticks": col("price_ticks", pa.int32(), 0),
        "size": col("size", pa.int32(), 0),
        "session_id": pa.array([session_id] * n, type=pa.string()),
    }, schema=MBO_SCHEMA)


def _stream_ndjson_to_parquet(
    src: Path,
    dest: Path,
    session_id: str,
    *,
    kind: str,
    batch_size: int = 200_000,
) -> int:
    """Stream large NDJSON → parquet via pyarrow.json (block-wise). Returns row count."""
    if not src.exists():
        return 0
    normalize = _normalize_depth_batch if kind == "depth" else _normalize_mbo_batch
    schema = DEPTH_SCHEMA if kind == "depth" else MBO_SCHEMA
    writer = None
    n_rows = 0
    try:
        # pyarrow.json.read_json reads whole file by default; use open_json for streaming
        reader = paj.open_json(
            str(src),
            read_options=paj.ReadOptions(use_threads=True, block_size=8 << 20),
            parse_options=paj.ParseOptions(
                newlines_in_values=False,
                unexpected_field_behavior="ignore",
            ),
        )
        pending: list[pa.Table] = []
        pending_n = 0
        for batch in reader:
            tbl = normalize(pa.Table.from_batches([batch]), session_id)
            pending.append(tbl)
            pending_n += tbl.num_rows
            if pending_n >= batch_size:
                chunk = pa.concat_tables(pending)
                if writer is None:
                    writer = pq.ParquetWriter(dest, schema, compression="zstd")
                writer.write_table(chunk)
                n_rows += chunk.num_rows
                pending, pending_n = [], 0
        if pending:
            chunk = pa.concat_tables(pending)
            if writer is None:
                writer = pq.ParquetWriter(dest, schema, compression="zstd")
            writer.write_table(chunk)
            n_rows += chunk.num_rows
    except Exception as e:
        # Fallback: line-by-line JSON (slower, lower peak RAM for broken files)
        print(f"  WARN: pyarrow.json failed ({e}); falling back to line reader", flush=True)
        if writer is not None:
            writer.close()
            writer = None
        if dest.exists():
            dest.unlink()
        n_rows = 0
        for batch in _iter_ndjson(src, batch_size=batch_size):
            if kind == "depth":
                raw = pa.table({
                    "ts_ns": [int(r["ts_ns"]) for r in batch],
                    "alias": [r.get("alias", "") for r in batch],
                    "role": [r.get("role", "OTHER") for r in batch],
                    "symbol": [r.get("symbol", "") for r in batch],
                    "is_bid": [bool(r.get("is_bid")) for r in batch],
                    "price_ticks": [int(r["price_ticks"]) for r in batch],
                    "size": [int(r["size"]) for r in batch],
                })
                tbl = _normalize_depth_batch(raw, session_id)
            else:
                raw = pa.table({
                    "ts_ns": [int(r["ts_ns"]) for r in batch],
                    "alias": [r.get("alias", "") for r in batch],
                    "role": [r.get("role", "OTHER") for r in batch],
                    "symbol": [r.get("symbol", "") for r in batch],
                    "op": [r.get("op", "") for r in batch],
                    "order_id": [str(r.get("order_id") or "") for r in batch],
                    "is_bid": [bool(r["is_bid"]) if "is_bid" in r else False for r in batch],
                    "price_ticks": [int(r.get("price_ticks") or 0) for r in batch],
                    "size": [int(r.get("size") or 0) for r in batch],
                })
                tbl = _normalize_mbo_batch(raw, session_id)
            if writer is None:
                writer = pq.ParquetWriter(dest, schema, compression="zstd")
            writer.write_table(tbl)
            n_rows += tbl.num_rows
    finally:
        if writer is not None:
            writer.close()
    return n_rows


def convert_session(
    session_dir: Path,
    parquet_root: Path,
    *,
    only: set[str] | None = None,
    skip_existing: bool = False,
) -> dict:
    session_id = session_dir.name
    meta_path = session_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    out_base = parquet_root / f"session_id={session_id}"
    out_base.mkdir(parents=True, exist_ok=True)

    want = only or {"trades", "depth", "mbo"}
    stats = {"session_id": session_id, "trades": 0, "depth": 0, "mbo": 0, "skipped": []}

    # Trades (smaller — keep concat path; optional pyarrow.json)
    if "trades" in want:
        trade_out = out_base / "trades.parquet"
        if skip_existing and trade_out.exists() and trade_out.stat().st_size > 0:
            stats["skipped"].append("trades")
            try:
                stats["trades"] = pq.read_metadata(trade_out).num_rows
            except Exception:
                stats["trades"] = -1
        else:
            trade_parts = []
            for batch in _iter_ndjson(session_dir / "trades.ndjson"):
                trade_parts.append(_trades_table(batch, session_id))
                stats["trades"] += len(batch)
            if trade_parts:
                pq.write_table(
                    pa.concat_tables(trade_parts), trade_out, compression="zstd"
                )

    if "depth" in want:
        depth_out = out_base / "depth.parquet"
        if skip_existing and depth_out.exists() and depth_out.stat().st_size > 0:
            stats["skipped"].append("depth")
            try:
                stats["depth"] = pq.read_metadata(depth_out).num_rows
            except Exception:
                stats["depth"] = -1
        else:
            print(f"  streaming depth.ndjson …", flush=True)
            stats["depth"] = _stream_ndjson_to_parquet(
                session_dir / "depth.ndjson", depth_out, session_id, kind="depth"
            )

    if "mbo" in want:
        mbo_out = out_base / "mbo.parquet"
        if skip_existing and mbo_out.exists() and mbo_out.stat().st_size > 0:
            stats["skipped"].append("mbo")
            try:
                stats["mbo"] = pq.read_metadata(mbo_out).num_rows
            except Exception:
                stats["mbo"] = -1
        else:
            print(f"  streaming mbo.ndjson …", flush=True)
            stats["mbo"] = _stream_ndjson_to_parquet(
                session_dir / "mbo.ndjson", mbo_out, session_id, kind="mbo"
            )

    (out_base / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    stats["meta"] = meta
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NDJSON extract → Parquet")
    parser.add_argument("--session", help="Single session_id under raw_dir")
    parser.add_argument("--raw-dir", help="Override raw_dir")
    parser.add_argument("--parquet-dir", help="Override parquet_dir")
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated subset: trades,depth,mbo (default: all)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip tables that already have a non-empty parquet file",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    raw_dir = Path(args.raw_dir or cfg["raw_dir"])
    parquet_dir = Path(args.parquet_dir or cfg["parquet_dir"])
    parquet_dir.mkdir(parents=True, exist_ok=True)

    only = None
    if args.only:
        only = {x.strip() for x in args.only.split(",") if x.strip()}
        bad = only - {"trades", "depth", "mbo"}
        if bad:
            print(f"Unknown --only kinds: {bad}", file=sys.stderr)
            return 2

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
        print(f"Converting {session_dir.name} ...", flush=True)
        stats = convert_session(
            session_dir,
            parquet_dir,
            only=only,
            skip_existing=args.skip_existing,
        )
        meta = stats.get("meta") or {}
        print(
            f"  trades={stats['trades']} depth={stats['depth']} mbo={stats['mbo']} "
            f"skipped={stats.get('skipped')} "
            f"meta_depth={meta.get('depth_count')} meta_mbo={meta.get('mbo_count')} "
            f"-> {parquet_dir / ('session_id=' + stats['session_id'])}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
