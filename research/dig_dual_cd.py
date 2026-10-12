"""One-pass dig: dual CD sparsity on EXP-0022 events + soft same-sign gate."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from research.config import load_config
from research.run_experiment import _mean, _median


def main() -> None:
    path = Path("results/EXP-0022/events.parquet")
    t = pq.read_table(path)
    side = t.column("side").to_numpy()
    hit = t.column("hit_10pts").to_numpy().astype(float)
    ret10 = t.column("ret_pts_10000ms").to_numpy().astype(float)
    nq_tr = t.column("nq_cd_ma_trend").to_numpy()
    es_tr = t.column("es_cd_ma_trend").to_numpy()
    nq_reg = t.column("nq_cd_bb_regime").to_numpy()
    es_reg = t.column("es_cd_bb_regime").to_numpy()
    nq_pct = t.column("nq_cd_bb_width_pct").to_numpy()
    es_pct = t.column("es_cd_bb_width_pct").to_numpy()

    def pack(mask, name):
        n = int(mask.sum())
        if n == 0:
            return {"name": name, "n": 0}
        return {
            "name": name,
            "n": n,
            "hit10": round(float(hit[mask].mean()), 3),
            "ret10s": round(float(ret10[mask].mean()), 2),
            "med_ret10s": round(float(np.median(ret10[mask])), 2),
        }

    same_sign = (
        np.isfinite(nq_tr)
        & np.isfinite(es_tr)
        & (nq_tr != 0)
        & (es_tr != 0)
        & (np.sign(nq_tr) == np.sign(es_tr))
    )
    same_sign_with_side = same_sign & (
        ((side > 0) & (nq_tr > 0)) | ((side < 0) & (nq_tr < 0))
    )
    both_pct_ge_50 = (
        np.isfinite(nq_pct) & np.isfinite(es_pct) & (nq_pct >= 0.5) & (es_pct >= 0.5)
    )
    both_pct_ge_60 = (
        np.isfinite(nq_pct) & np.isfinite(es_pct) & (nq_pct >= 0.6) & (es_pct >= 0.6)
    )
    nq_wide_es_any = (nq_reg == 1) & np.isfinite(es_reg)
    es_nonzero = np.isfinite(es_tr) & (es_tr != 0)
    nq_nonzero = np.isfinite(nq_tr) & (nq_tr != 0)

    dist = {
        "n_events": int(t.num_rows),
        "frac_nq_trend_nonzero": float(nq_nonzero.mean()),
        "frac_es_trend_nonzero": float(es_nonzero.mean()),
        "frac_nq_wide": float((nq_reg == 1).mean()),
        "frac_es_wide": float((es_reg == 1).mean()),
        "frac_nq_narrow": float((nq_reg == -1).mean()),
        "frac_es_narrow": float((es_reg == -1).mean()),
        "mean_nq_bb_pct": float(np.nanmean(nq_pct)),
        "mean_es_bb_pct": float(np.nanmean(es_pct)),
        "es_trend_value_counts": {
            str(k): int(v)
            for k, v in zip(*np.unique(np.nan_to_num(es_tr, nan=-99), return_counts=True))
        },
        "nq_trend_value_counts": {
            str(k): int(v)
            for k, v in zip(*np.unique(np.nan_to_num(nq_tr, nan=-99), return_counts=True))
        },
    }

    strata = [
        pack(np.ones(len(hit), dtype=bool), "all"),
        pack(t.column("nq_trend_aligned").to_numpy() == 1, "nq_trend_aligned"),
        pack(t.column("es_trend_aligned").to_numpy() == 1, "es_trend_aligned"),
        pack(t.column("dual_trend_aligned").to_numpy() == 1, "dual_trend_aligned"),
        pack(same_sign, "es_nq_same_sign_trend"),
        pack(same_sign_with_side, "same_sign_and_side"),
        pack(both_pct_ge_50, "both_bb_pct_ge_50"),
        pack(both_pct_ge_60, "both_bb_pct_ge_60"),
        pack(nq_wide_es_any & (es_reg == 1), "dual_wide"),
        pack(nq_wide_es_any & (es_reg != 0) & (nq_reg == es_reg), "dual_same_regime"),
    ]

    out = {"dist": dist, "strata": strata}
    Path("results/EXP-0022").mkdir(parents=True, exist_ok=True)
    Path("results/EXP-0022/dig.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
