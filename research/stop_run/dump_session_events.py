"""Dump Python stop-run events for one session (replay parity checklist).

Usage:
  python -m research.stop_run.dump_session_events --session 021026

Compare ``SR RUN`` lines in Bookmap log (File → Show log file) against
``event_ts_ns`` / ``level_px`` / ``side`` here after attaching Stop Run on
the same replay.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from research.config import load_config
from research.correlated.harness import _prefer_single_feed
from research.features.stop_run import resolve_outcome, stop_run_events
from research.run_experiment import _load_mbo, _load_trades
from research.stop_run.run_exp_0041 import _cfg_from_yaml, bounds, filt

UK = ZoneInfo("Europe/London")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True)
    ap.add_argument("--no-mbo", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    exp_path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0041.yaml"
    with open(exp_path, encoding="utf-8") as f:
        exp = yaml.safe_load(f)
    sr_cfg = _cfg_from_yaml(exp)

    sid = args.session
    nq = _prefer_single_feed(_load_trades(pq, sid, "NQ"), pq, sid, "NQ")
    es = _prefer_single_feed(_load_trades(pq, sid, "ES"), pq, sid, "ES")
    mbo = None if args.no_mbo else _load_mbo(pq, sid, "NQ")
    if mbo is not None and mbo.num_rows == 0:
        mbo = None

    ev = stop_run_events(nq, es if es.num_rows else None, mbo, sr_cfg)
    if ev.num_rows:
        mid = int(filt(nq).column("ts_ns").to_numpy()[0]) if filt(nq).num_rows else int(
            nq.column("ts_ns").to_numpy()[nq.num_rows // 2]
        )
        s, e = bounds(int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2]))
        ets = ev.column("event_ts_ns").to_numpy()
        keep = (ets >= s) & (ets <= e)
        import numpy as np
        ev = ev.take(np.where(keep)[0]) if keep.any() else ev.slice(0, 0)
    ev = resolve_outcome(ev, nq, reclaim_sec=sr_cfg.reclaim_sec)

    rows = []
    for i in range(ev.num_rows):
        ts = int(ev.column("event_ts_ns")[i].as_py())
        uk = datetime.fromtimestamp(ts / 1e9, tz=timezone.utc).astimezone(UK)
        rows.append({
            "event_ts_ns": ts,
            "uk_time": uk.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "side": int(ev.column("side")[i].as_py()),
            "level_kind": ev.column("level_kind")[i].as_py(),
            "level_px": float(ev.column("level_px")[i].as_py()),
            "score": int(ev.column("score")[i].as_py()),
            "n_aggressors": int(ev.column("n_aggressors")[i].as_py()),
            "n_sweeps": int(ev.column("n_sweeps")[i].as_py()),
            "es_confirmed": bool(ev.column("es_confirmed")[i].as_py()),
            "outcome": ev.column("outcome")[i].as_py(),
        })
    out = Path(cfg["results_dir"]) / "EXP-0041" / f"parity_{sid}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"wrote {len(rows)} events -> {out}")
    for r in rows:
        print(
            f"  {r['uk_time']} side={r['side']:+d} {r['level_kind']} "
            f"lvl={r['level_px']:.2f} score={r['score']} outcome={r['outcome']}"
        )


if __name__ == "__main__":
    main()
