"""Dig EXP-0032: range_ticks × mode × min_imbalance × avg_n."""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa

from research.config import load_config
from research.features.range_imbalance import (
    MODES,
    RangeImbalanceConfig,
    annotate_vs_prior,
    range_imbalance_events,
    trades_to_enriched_range_bars,
)
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
    _load_trades,
    _mean,
    _median,
)

OUT = Path(__file__).resolve().parents[1] / "results" / "EXP-0032" / "dig.json"


def pack(t: pa.Table) -> dict | None:
    if t.num_rows == 0:
        return None
    return {
        "n": int(t.num_rows),
        "hit10": round(float(_mean(t.column("hit_10pts"))), 3),
        "hit20": round(float(_mean(t.column("hit_20pts"))), 3),
        "ret10s": round(float(_mean(t.column("ret_pts_10000ms"))), 2),
        "med10s": round(float(_median(t.column("ret_pts_10000ms"))), 2),
        "mfe": round(float(_mean(t.column("mfe_pts"))), 2),
        "mae": round(float(_mean(t.column("mae_pts"))), 2),
    }


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])
    horizons = [250, 500, 1000, 2000, 5000, 10000]
    targets = [10, 20]

    ranges = [5, 10, 22, 50]
    modes = list(MODES)
    # First pass: defaults across range × mode; then sensitivity on best-looking cells
    grid = []
    for rt in ranges:
        for mode in modes:
            grid.append(dict(range_ticks=rt, mode=mode, min_imbalance=0.30, avg_n=20))
    # sensitivity on primary-ish cells
    for rt in (10, 22):
        for mode in ("imb_opp_low", "imb_opp_low_fast", "imb_opp_low_fast_hivol", "with_bar"):
            for mi in (0.20, 0.40, 0.50):
                grid.append(dict(range_ticks=rt, mode=mode, min_imbalance=mi, avg_n=20))
            for an in (10, 30):
                grid.append(dict(range_ticks=rt, mode=mode, min_imbalance=0.30, avg_n=an))

    # de-dupe
    seen = set()
    uniq = []
    for g in grid:
        key = (g["range_ticks"], g["mode"], g["min_imbalance"], g["avg_n"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(g)

    results = []
    for g in uniq:
        label = f"r{g['range_ticks']}_{g['mode']}_mi{g['min_imbalance']}_an{g['avg_n']}"
        print(f"dig {label} …", flush=True)
        parts = []
        for sid in sessions:
            nq = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
            if nq.num_rows == 0:
                continue
            bars = trades_to_enriched_range_bars(
                nq, range_ticks=g["range_ticks"], tick_size=1.0
            )
            if bars.num_rows == 0:
                continue
            bars = annotate_vs_prior(bars, avg_n=g["avg_n"])
            ricfg = RangeImbalanceConfig(
                range_ticks=g["range_ticks"],
                avg_n=g["avg_n"],
                min_imbalance=g["min_imbalance"],
                min_bars_warmup=10,
            )
            path_ts, path_px = build_price_path(nq)
            lab = label_events(
                range_imbalance_events(bars, ricfg, mode=g["mode"]),
                path_ts,
                path_px,
                horizons,
                targets,
            )
            if lab.num_rows:
                parts.append(lab)
        row = {"label": label, **g, "result": pack(pa.concat_tables(parts)) if parts else None}
        results.append(row)
        r = row["result"] or {}
        print(
            f"  n={r.get('n')} hit10={r.get('hit10')} ret10s={r.get('ret10s')} mae={r.get('mae')}",
            flush=True,
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")

    # rank by hit10 among n>=30
    ranked = [
        r for r in results
        if r.get("result") and r["result"]["n"] >= 30
    ]
    ranked.sort(key=lambda r: (r["result"]["hit10"], r["result"]["ret10s"]), reverse=True)
    top = [
        {
            "label": r["label"],
            "n": r["result"]["n"],
            "hit10": r["result"]["hit10"],
            "ret10s": r["result"]["ret10s"],
            "mae": r["result"]["mae"],
        }
        for r in ranked[:15]
    ]
    (OUT.parent / "dig_top.json").write_text(json.dumps(top, indent=2), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)
    print("TOP (n>=30):", flush=True)
    for t in top[:10]:
        print(f"  {t}", flush=True)


if __name__ == "__main__":
    main()
