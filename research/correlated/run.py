"""Dispatch for ``detector: correlated_*`` experiments (EXP-0035..0043)."""
from __future__ import annotations

import importlib

_DETECTORS = {
    "correlated_absorb": "research.correlated.exp_0035_absorb",
    "correlated_exhaust": "research.correlated.exp_0036_exhaust",
}


def run_correlated(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    det = str(exp.get("detector", ""))
    mod_name = _DETECTORS.get(det)
    if mod_name is None:
        raise SystemExit(f"Unknown correlated detector: {det!r} (known: {sorted(_DETECTORS)})")
    mod = importlib.import_module(mod_name)
    return mod.run(cfg, exp, session_ids)
