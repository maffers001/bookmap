"""Sensitivity dig for EXP-0019 gates."""
from pathlib import Path

import pyarrow as pa

from research.config import load_config
from research.features.value_depart import ValueDepartConfig, build_value_features, value_depart_events
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import _filter_rth_uk_table, _list_sessions, _load_trades, _mean

cfg = load_config()
pd = Path(cfg["parquet_dir"])
horizons = [250, 500, 1000, 2000, 5000, 10000]
targets = [10, 20]

for require in ((True, True), (False, False), (True, False)):
    vcfg = ValueDepartConfig(require_cd_regime=require[0], require_price_confirm=require[1])
    parts = {"s20": [], "s4": []}
    for sid in _list_sessions(pd, "all", []):
        nq = _filter_rth_uk_table(_load_trades(pd, sid, "NQ"))
        if nq.num_rows == 0:
            continue
        feat = build_value_features(nq, vcfg)
        path_ts, path_px = build_price_path(nq)
        for kind in ("s20", "s4"):
            lab = label_events(
                value_depart_events(feat, vcfg, kind=kind),
                path_ts,
                path_px,
                horizons,
                targets,
            )
            if lab.num_rows:
                parts[kind].append(lab)
    for kind, plist in parts.items():
        if not plist:
            print(f"regime={require[0]} pconf={require[1]} {kind}: n=0")
            continue
        t = pa.concat_tables(plist)
        print(
            f"regime={require[0]} pconf={require[1]} {kind}: "
            f"n={t.num_rows} hit10={_mean(t.column('hit_10pts')):.3f} "
            f"@10s={_mean(t.column('ret_pts_10000ms')):.2f}"
        )
