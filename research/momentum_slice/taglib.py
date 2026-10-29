"""Shared tag loading: pack may be Excel-saved (cp1252, mangled dates)."""
from __future__ import annotations

import csv
from pathlib import Path

BASE = Path("results/MOMENTUM_SLICE")


def read_csv(path: Path) -> list[dict]:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=enc) as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def load_tagged_features(base: Path = BASE) -> list[dict]:
    feats = read_csv(base / "features/features.csv")
    key = {r["event_id"]: r for r in read_csv(base / "review/review_key.csv")}
    pack = read_csv(base / "review/review_pack.csv")
    by = {(r["session"], r["side"], r["ignition_bst"]): r for r in feats}
    for p in pack:
        k = key.get((p.get("event_id") or "").strip())
        if not k:
            continue
        fr = by.get((k["session"], k["side"], k["ignition_bst"]))
        if fr is None:
            continue
        fr["event_id"] = p["event_id"].strip()
        fr["lst_tag"] = (p.get("lst_tag") or "").strip().upper()
        fr["reason_code"] = (p.get("reason_code") or "").strip().upper()
        fr["notes"] = (p.get("notes") or "").strip()
    return feats
