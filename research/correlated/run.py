"""Dispatch for ``detector: correlated_*`` experiments (EXP-0035..0043)."""
from __future__ import annotations

import importlib

_DETECTORS = {
    "correlated_absorb": "research.correlated.exp_0035_absorb",
    "correlated_exhaust": "research.correlated.exp_0036_exhaust",
    "correlated_exhaust_fade_dig": "research.correlated.exp_0036_fade_dig",
    "correlated_exhaust_fade_freeze": "research.correlated.exp_0036_fade_freeze",
    "correlated_divergence": "research.correlated.exp_0049_divergence",
    "correlated_corr_gate": "research.correlated.exp_0050_corr_gate",
    "correlated_vol_ratio": "research.correlated.exp_0051_vol_ratio",
    "correlated_aggr_flow": "research.correlated.exp_0052_aggr_flow",
    "correlated_large_prints": "research.correlated.exp_0053_large_prints",
    "correlated_book_agreement": "research.correlated.exp_0054_book_agreement",
}


def run_correlated(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    det = str(exp.get("detector", ""))
    mod_name = _DETECTORS.get(det)
    if mod_name is None:
        raise SystemExit(f"Unknown correlated detector: {det!r} (known: {sorted(_DETECTORS)})")
    mod = importlib.import_module(mod_name)
    return mod.run(cfg, exp, session_ids)
