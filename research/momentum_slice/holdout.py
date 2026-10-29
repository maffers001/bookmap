"""Holdout validation of frozen RULE_SPEC_V1 on untouched sessions.

  .venv\\Scripts\\python.exe -m research.momentum_slice.holdout
  .venv\\Scripts\\python.exe -m research.momentum_slice.holdout --max-sessions 20

Train = sessions already in results/MOMENTUM_SLICE/features/features.csv.
Outputs results/MOMENTUM_SLICE/holdout/{candidates,features,controls,report}.md
"""
from __future__ import annotations

import argparse
import csv
import statistics as st
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.correlated.harness import label_in_points, list_sessions, load_pair
from research.momentum_slice.detector import (
    ZONE_TICKS, _book_snapshots, cross_confirm, stack_metrics, tape_candidates,
    verify_event,
)
from research.momentum_slice.extract_stall import stall
from research.momentum_slice.features import (
    clearance_context, pre_ignition_trend, pre_move_context, pull_estimate,
    stack_geometry, tape_speed_context,
)
from research.momentum_slice.rules import enrich, f, is_momentum_v1, is_pc_v1
from research.momentum_slice.scan import HORIZONS, TARGETS, bst, scan_session
from research.run_experiment import _random_null_events

OUT = Path("results/MOMENTUM_SLICE/holdout")
TRAIN_FEAT = Path("results/MOMENTUM_SLICE/features/features.csv")
SNAP_OFFSETS = (60, 33, 13, 3)


def train_sessions() -> set[str]:
    with open(TRAIN_FEAT, newline="", encoding="utf-8") as fh:
        return {r["session"] for r in csv.DictReader(fh)}


def build_features(cfg: dict, sid: str, cand_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    pq_dir = Path(cfg["parquet_dir"])
    pair = load_pair(pq_dir, sid)
    if pair is None or pair.es.num_rows == 0:
        return [], []
    by_key = {(r["session"], r["side"], r["ignition_bst"]): r for r in cand_rows}
    nq_c, es_c = tape_candidates(pair.nq, "NQ"), tape_candidates(pair.es, "ES")
    events = cross_confirm(nq_c, es_c)
    evs = [e for e in events
           if (sid, "LONG" if e["side"] > 0 else "SHORT", bst(e["ignition_ns"])) in by_key]
    feat_rows: list[dict] = []
    if evs:
        depth_path = pq_dir / f"session_id={sid}" / "depth.parquet"
        if not depth_path.exists():
            print(f"  {sid}: no depth.parquet — skip feature build", flush=True)
            # still emit controls below
            evs = []
        snaps: dict[str, dict] = {}
    if evs:
        for role, cands in (("NQ", [e["nq"] for e in evs]), ("ES", [e["es"] for e in evs])):
            want, bands = [], []
            for c in cands:
                ref = int(round(c["pre_price_ticks"]))
                bands.append((ref - ZONE_TICKS - 8, ref + ZONE_TICKS + 8))
                want += [c["ignition_ns"] - s * 1_000_000_000 for s in SNAP_OFFSETS]
                want.append(c["ignition_ns"] + 5_000_000_000)
            snaps[role] = _book_snapshots(str(depth_path), role, want, bands)
        for ev in evs:
            v = verify_event(ev, snaps["NQ"], snaps["ES"], pair.nq, pair.es)
            side = ev["side"]
            key = (sid, "LONG" if side > 0 else "SHORT", bst(ev["ignition_ns"]))
            row = dict(by_key[key])
            row.update(
                ignition_ns=ev["ignition_ns"], confirm_ns=ev["confirm_ns"],
                side_int=side,
                tod="open" if bst(ev["ignition_ns"])[11:16] < "14:55" else "late",
                es_signed_lag_ms=round((ev["es"]["ignition_ns"] - ev["nq"]["ignition_ns"]) / 1e6),
            )
            for role, tr in (("nq", pair.nq), ("es", pair.es)):
                c = ev[role]
                g = stack_geometry(
                    snaps[role.upper()].get(c["ignition_ns"] - 3_000_000_000, {}),
                    side, c["pre_price_ticks"], role.upper(),
                )
                row[f"{role}_dist_max_ticks"] = round(g["dist_to_max_ticks"], 1)
                row[f"{role}_dist_top_ticks"] = round(g["dist_to_top_ticks"], 1)
                row[f"{role}_top3_conc"] = round(g["top3_conc"], 3)
                curve = []
                for s in SNAP_OFFSETS:
                    bk = snaps[role.upper()].get(c["ignition_ns"] - s * 1_000_000_000, {})
                    curve.append(stack_metrics(bk, side, c["pre_price_ticks"], role.upper())["stack_sum"])
                row[f"{role}_stack_curve"] = "/".join(str(x) for x in curve)
                row[f"{role}_pull_est"] = round(pull_estimate(
                    snaps[role.upper()].get(c["ignition_ns"] - 3_000_000_000, {}),
                    snaps[role.upper()].get(c["ignition_ns"] + 5_000_000_000, {}),
                    tr, side, c["pre_price_ticks"], c["ignition_ns"],
                    c["ignition_ns"] + 5_000_000_000), 3)
                for k, vv in clearance_context(
                    tr, side=side, ignition_ns=ev["ignition_ns"], confirm_ns=ev["confirm_ns"],
                    pre_price_pts=float(row[f"{role}_px_ignition"]),
                    dist_max_ticks=float(row.get(f"{role}_dist_max_ticks") or "nan"),
                ).items():
                    row[f"{role}_{k}"] = round(vv, 3) if isinstance(vv, float) else vv
                for k, vv in tape_speed_context(tr, ev["ignition_ns"], ev["confirm_ns"]).items():
                    row[f"{role}_{k}"] = round(vv, 3) if isinstance(vv, float) else vv
                for k, vv in pre_ignition_trend(
                    tr, side=side, ignition_ns=ev["ignition_ns"], window_start_ns=pair.bounds[0],
                ).items():
                    row[f"{role}_{k}"] = round(vv, 3) if isinstance(vv, float) else vv
            for k, vv in stall(pair.nq, side, ev["ignition_ns"], ev["confirm_ns"]).items():
                row[f"nq_{k}"] = round(vv, 3) if isinstance(vv, float) else vv
            # pullback ratios
            for stamp in ("confirm", "c5s"):
                peak = row.get(f"nq_peak_past_{stamp}_pts")
                pb = row.get(f"nq_pullback_{stamp}_pts")
                if peak not in (None, "", "nan") and pb not in (None, "", "nan") and float(peak) > 0:
                    row[f"nq_pullback_ratio_{stamp}"] = round(float(pb) / float(peak), 3)
            nq_t = row.get("nq_trend_5m_pts")
            if nq_t not in (None, "", "nan"):
                row["trend_5m_pts"] = float(nq_t)
                row["with_trend"] = int(float(nq_t) >= 0)
            row.update({k: (round(vv, 2) if isinstance(vv, float) else vv)
                        for k, vv in pre_move_context(
                            pair.nq, ev["confirm_ns"], side, pair.bounds[0]).items()})
            # Forward labels already on candidate row; keep them
            enrich(row)
            row["momentum_v1"] = int(is_momentum_v1(row))
            row["pc_v1"] = int(is_pc_v1(row))
            feat_rows.append(row)

    # Controls
    cross_igns = {e["nq"]["ignition_ns"] for e in events}
    nq_only = [c for c in nq_c if all(abs(c["ignition_ns"] - x) > 5_000_000_000 for x in cross_igns)]
    ctrl_defs = [("nq_only", c["confirm_ns"], c["side"]) for c in nq_only]
    nulls = _random_null_events(pair.nq, max(4, 2 * max(1, len(evs))), seed=abs(hash(sid)) % 10000)
    if nulls.num_rows:
        ctrl_defs += [("random", int(t), int(s)) for t, s in
                      zip(nulls.column("event_ts_ns").to_numpy(), nulls.column("side").to_numpy())]
    ctrl_rows: list[dict] = []
    if ctrl_defs:
        tbl = pa.table({"event_ts_ns": [d[1] for d in ctrl_defs],
                        "side": np.array([d[2] for d in ctrl_defs], dtype=np.int8)})
        lab = label_in_points(tbl, pair, HORIZONS, TARGETS)
        for i, (kind, ts, side) in enumerate(ctrl_defs):
            if i >= lab.num_rows:
                break
            ctrl_rows.append(dict(
                session=sid, control=kind, side=side,
                tod="open" if bst(ts)[11:16] < "14:55" else "late",
                ret10s=round(lab.column("ret_pts_10000ms")[i].as_py(), 1),
                ret60s=round(lab.column("ret_pts_60000ms")[i].as_py(), 1),
                mfe=round(lab.column("mfe_pts")[i].as_py(), 1),
                mae=round(lab.column("mae_pts")[i].as_py(), 1),
            ))
    return feat_rows, ctrl_rows


def fade_label(cfg: dict, rows: list[dict]) -> None:
    """Label fade outcomes from confirm+5s, opposite side (PC entry proxy)."""
    pq_dir = Path(cfg["parquet_dir"])
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["session"], []).append(r)
    for sid, evs in by.items():
        pair = load_pair(pq_dir, sid)
        if pair is None:
            continue
        tbl = pa.table({
            "event_ts_ns": np.array([int(float(r["confirm_ns"])) + 5_000_000_000 for r in evs], dtype=np.int64),
            "side": np.array([-int(float(r["side_int"])) for r in evs], dtype=np.int8),
        })
        lab = label_in_points(tbl, pair, HORIZONS, TARGETS)
        for i, r in enumerate(evs):
            if i >= lab.num_rows:
                break
            r["fade_ret10s"] = round(lab.column("ret_pts_10000ms")[i].as_py(), 1)
            r["fade_ret60s"] = round(lab.column("ret_pts_60000ms")[i].as_py(), 1)
            r["fade_mfe"] = round(lab.column("mfe_pts")[i].as_py(), 1)
            r["fade_mae"] = round(lab.column("mae_pts")[i].as_py(), 1)


def stats(rows: list[dict], key: str) -> str:
    xs = [f(r, key) for r in rows]
    xs = [x for x in xs if x is not None]
    if not xs:
        return "n=0"
    return (f"n={len(xs)} {key} {st.mean(xs):+.1f}/{st.median(xs):+.1f} "
            f"P>5 {sum(x > 5 for x in xs)/len(xs):.2f} "
            f"P<-5 {sum(x < -5 for x in xs)/len(xs):.2f} "
            f"MAE {st.mean([abs(f(r,'mae') or f(r,'mae_pts') or 0) for r in rows]):.0f}")


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k); fields.append(k)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def report(feat: list[dict], ctrl: list[dict]) -> str:
    mom = [r for r in feat if int(r.get("momentum_v1") or 0) == 1]
    pc = [r for r in feat if int(r.get("pc_v1") or 0) == 1]
    nq_only = [r for r in ctrl if r.get("control") == "nq_only"]
    rnd = [r for r in ctrl if r.get("control") == "random"]

    def block(title: str, rows: list[dict], key: str = "ret10s") -> list[str]:
        lines = [f"### {title}", stats(rows, key)]
        late = [r for r in rows if r.get("tod") == "late"]
        open_ = [r for r in rows if r.get("tod") == "open"]
        lines.append(f"  late: {stats(late, key)}")
        lines.append(f"  open: {stats(open_, key)}")
        for side in ("LONG", "SHORT"):
            sub = [r for r in rows if r.get("side") == side]
            if sub:
                lines.append(f"  {side}: {stats(sub, key)}")
        return lines

    lines = [
        "# RULE_SPEC_V1 holdout report", "",
        f"holdout candidates: {len(feat)}; momentum fires: {len(mom)}; PC fires: {len(pc)}",
        f"controls: nq_only={len(nq_only)} random={len(rnd)}", "",
        "## Momentum continuation (entry=confirm)",
    ]
    lines += block("MOMENTUM_V1", mom)
    lines += block("ALL candidates", feat)
    lines += block("NQ-only control", nq_only)
    lines += block("Random control", rnd)
    lines += ["", "## Pitch-and-catch fade (entry=confirm+5s, opposite side)"]
    lines += block("PC_V1 fade", pc, "fade_ret60s")
    lines += block("PC_V1 fade @10s", pc, "fade_ret10s")
    mom_fade = mom  # fade momentum events should lose
    lines += block("Fade MOMENTUM_V1 (should lose)", mom_fade, "fade_ret60s")
    lines += ["", "## Gate check"]

    def mean_of(rows, key, tod=None):
        pool = rows if tod is None else [r for r in rows if r.get("tod") == tod]
        xs = [f(r, key) for r in pool if f(r, key) is not None]
        return st.mean(xs) if xs else None

    # Plan: momentum gate is late-subset; PC fade gate is overall holdout.
    mom_late = mean_of(mom, "ret10s", "late")
    ctrl_late = mean_of(nq_only, "ret10s", "late")
    rnd_late = mean_of(rnd, "ret10s", "late")
    pc_fade60 = mean_of(pc, "fade_ret60s")          # overall holdout
    mom_fade60 = mean_of(mom, "fade_ret60s")         # fading momentum must lose
    pc_fade60_late = mean_of(pc, "fade_ret60s", "late")  # diagnostic only
    g1 = (mom_late is not None and ctrl_late is not None and rnd_late is not None
          and mom_late > ctrl_late and mom_late > rnd_late and mom_late > 0)
    g2 = (pc_fade60 is not None and mom_fade60 is not None
          and pc_fade60 > 0 and mom_fade60 < 0)
    lines.append(f"- Gate1 momentum late vs controls: mom={mom_late} nq_only={ctrl_late} "
                 f"random={rnd_late} → {'PASS' if g1 else 'FAIL'}")
    lines.append(f"- Gate2 PC fade60 (overall)>0 and fade(momentum)<0: pc={pc_fade60} "
                 f"mom_fade={mom_fade60} (late pc diag={pc_fade60_late}) → {'PASS' if g2 else 'FAIL'}")
    lines.append(f"- Overall: {'PASS — proceed to backtest/Java' if (g1 and g2) else 'FAIL — revise as v2 with user'}")
    lines += ["", "## Per-session momentum fires"]
    by_s = Counter(r["session"] for r in mom)
    for sid, n in sorted(by_s.items()):
        sub = [r for r in mom if r["session"] == sid]
        lines.append(f"- {sid}: n={n} {stats(sub, 'ret10s')}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sessions", type=int, default=0)
    ap.add_argument("--sessions", default="", help="comma list override")
    args = ap.parse_args()
    cfg = load_config()
    train = train_sessions()
    if args.sessions:
        sids = [s.strip() for s in args.sessions.split(",") if s.strip()]
    else:
        sids = [s for s in list_sessions(Path(cfg["parquet_dir"])) if s not in train]
    if args.max_sessions:
        sids = sids[: args.max_sessions]
    OUT.mkdir(parents=True, exist_ok=True)
    all_cand, all_feat, all_ctrl = [], [], []
    for i, sid in enumerate(sids, 1):
        print(f"[{i}/{len(sids)}] {sid}", flush=True)
        try:
            cands = scan_session(cfg, sid)
            feats, ctrls = build_features(cfg, sid, cands) if cands else ([], [])
            all_cand += cands
            all_feat += feats
            all_ctrl += ctrls
            print(f"  cand={len(cands)} feat={len(feats)} mom={sum(int(r.get('momentum_v1') or 0) for r in feats)} "
                  f"pc={sum(int(r.get('pc_v1') or 0) for r in feats)}", flush=True)
        except Exception as e:
            print(f"  ERROR {type(e).__name__}: {e}", flush=True)
    if all_feat:
        fade_label(cfg, all_feat)
    write_csv(OUT / "candidates.csv", all_cand)
    write_csv(OUT / "features.csv", all_feat)
    write_csv(OUT / "controls.csv", all_ctrl)
    text = report(all_feat, all_ctrl)
    (OUT / "report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
