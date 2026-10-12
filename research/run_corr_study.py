"""EXP-0028 catch-up + EXP-0029 comove, one load per session."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.config import load_config
from research.features.correlation import CatchUpConfig, ComoveConfig, catchup_events, comove_events
from research.features.momentum import (
    cross_confirm_bursts,
    momentum_burst_events,
    trades_to_tape_bars,
)
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
    _load_trades,
    _mean,
    _median,
    _random_null_events,
    _summarize_labeled,
)

HORIZONS = [250, 500, 1000, 2000, 5000, 10000]
TARGETS = [10, 20]
BURST = dict(
    min_trades=6, min_vol=25.0, min_imbalance=0.55,
    min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3,
)


def _pack(parts: list[pa.Table]) -> dict | None:
    if not parts:
        return None
    t = pa.concat_tables(parts)
    return {
        "n_events": int(t.num_rows),
        "p_hit_10pts": _mean(t.column("hit_10pts")),
        "p_hit_20pts": _mean(t.column("hit_20pts")),
        "mean_mfe_pts": _mean(t.column("mfe_pts")),
        "mean_mae_pts": _mean(t.column("mae_pts")),
        "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")),
        "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")),
        "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")),
    }


def _sid(t: pa.Table, sid: str) -> pa.Table:
    if t.num_rows == 0:
        return t
    return t.append_column("session_id", pa.array([sid] * t.num_rows, type=pa.string()))


def _write(exp: str, title: str, summary: dict, events: pa.Table | None, results: Path) -> None:
    out = results / exp
    out.mkdir(parents=True, exist_ok=True)
    if events is not None and events.num_rows:
        p = out / "events.parquet"
        pq.write_table(events, p, compression="zstd")
        summary["events_path"] = str(p)
    summary["experiment"] = exp
    summary["title"] = title
    (out / "report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in summary if k != "sessions"}, indent=2)[:4000])


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    results = Path(cfg["results_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])

    catch_specs = [
        ("w1000_es6_nq3", CatchUpConfig(window_ms=1000, min_es_ticks=6, max_nq_ticks=3)),
        ("w500_es4_nq2", CatchUpConfig(window_ms=500, min_es_ticks=4, max_nq_ticks=2)),
        ("w2000_es8_nq4", CatchUpConfig(window_ms=2000, min_es_ticks=8, max_nq_ticks=4)),
    ]
    comove_cfg = ComoveConfig()

    catch_parts = {name: [] for name, _ in catch_specs}
    catch_hi = {name: [] for name, _ in catch_specs}
    catch_lo = {name: [] for name, _ in catch_specs}
    comove_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    per_catch = {name: [] for name, _ in catch_specs}
    per_comove = []

    for sid in sessions:
        print(f"[corr] {sid}", flush=True)
        nq = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
        es = _filter_rth_uk_table(_load_trades(parquet, sid, "ES"))
        if nq.num_rows == 0 or es.num_rows == 0:
            continue
        path_ts, path_px = build_price_path(nq)

        for name, ccfg in catch_specs:
            ev = catchup_events(nq, es, ccfg)
            lab = label_events(ev, path_ts, path_px, HORIZONS, TARGETS)
            lab = _sid(lab, sid)
            if lab.num_rows:
                catch_parts[name].append(lab)
                if "high_recent_corr" in lab.column_names:
                    h = lab.column("high_recent_corr").to_numpy() == 1
                    if h.any():
                        catch_hi[name].append(lab.take(np.where(h)[0]))
                    if (~h).any():
                        catch_lo[name].append(lab.take(np.where(~h)[0]))
            hit = _mean(lab.column("hit_10pts")) if lab.num_rows else None
            per_catch[name].append({"session_id": sid, "n": int(lab.num_rows), "hit10": hit})
            print(f"  catch {name} n={lab.num_rows} hit10={hit}", flush=True)

        evc = comove_events(nq, es, comove_cfg)
        labc = _sid(label_events(evc, path_ts, path_px, HORIZONS, TARGETS), sid)
        if labc.num_rows:
            comove_parts.append(labc)
        per_comove.append({
            "session_id": sid,
            "n": int(labc.num_rows),
            "hit10": _mean(labc.column("hit_10pts")) if labc.num_rows else None,
        })
        print(f"  comove n={labc.num_rows}", flush=True)

        nq_bars = trades_to_tape_bars(nq, bar_ms=100)
        es_bars = trades_to_tape_bars(es, bar_ms=100)
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **BURST),
            momentum_burst_events(es_bars, **BURST),
            sync_ms=1500,
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            mom = mom.take(np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= 250)[0])
        labm = label_events(mom, path_ts, path_px, HORIZONS, TARGETS)
        if labm.num_rows:
            mom_parts.append(labm)
        null_ev = _random_null_events(nq, 50, seed=hash("corr" + sid) % (2**31))
        labn = label_events(null_ev, path_ts, path_px, HORIZONS, TARGETS)
        if labn.num_rows:
            null_parts.append(labn)

    mom_pack = _pack(mom_parts)
    null_pack = _pack(null_parts)

    # Primary catch-up = first spec
    primary_name = catch_specs[0][0]
    primary_df = pa.concat_tables(catch_parts[primary_name]) if catch_parts[primary_name] else pa.table({})
    dig = {}
    for name, _ in catch_specs:
        dig[name] = {
            "all": _pack(catch_parts[name]),
            "high_recent_corr": _pack(catch_hi[name]),
            "not_high_corr": _pack(catch_lo[name]),
            "sessions": per_catch[name],
        }
    if primary_df.num_rows:
        summary = _summarize_labeled(
            primary_df, HORIZONS, TARGETS,
            extra={
                "sessions": per_catch[primary_name],
                "primary": primary_name,
                "dig": dig,
                "baselines": {"null_random": null_pack, "exp0008_lag250": mom_pack},
            },
        )
    else:
        summary = {
            "status": "no_events",
            "dig": dig,
            "baselines": {"null_random": null_pack, "exp0008_lag250": mom_pack},
        }
    _write("EXP-0028", "ES impulse / NQ quiet catch-up", summary, primary_df if primary_df.num_rows else None, results)
    (results / "EXP-0028" / "dig.json").write_text(json.dumps(dig, indent=2), encoding="utf-8")

    comove_df = pa.concat_tables(comove_parts) if comove_parts else pa.table({})
    if comove_df.num_rows:
        csum = _summarize_labeled(
            comove_df, HORIZONS, TARGETS,
            extra={
                "sessions": per_comove,
                "primary": "comove",
                "baselines": {"null_random": null_pack, "exp0008_lag250": mom_pack},
                "gates": {"window_ms": 500, "min_es_ticks": 4, "min_nq_ticks": 4},
            },
        )
    else:
        csum = {"status": "no_events", "sessions": per_comove, "baselines": {"null_random": null_pack, "exp0008_lag250": mom_pack}}
    _write("EXP-0029", "ES+NQ same-window co-move", csum, comove_df if comove_df.num_rows else None, results)


if __name__ == "__main__":
    main()
