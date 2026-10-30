"""EXP-0087 / H78 — MAT stack survival scratch exit (+2s).

Reuses EXP-0086 annotated events (bc_* post-stamp sizes). Primary =
scratch_support exit vs ungated base on the SAME frozen-MAT entries.

Usage:
  python -m research.mat_context.exp_0087_stack_survival
  python -m research.mat_context.exp_0087_stack_survival --max-sessions 5
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.backtest.mat_sim import detect_signals, simulate_session
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.book_corridor import BookCorridorConfig, annotate_book_corridor
from research.features.stack_survival import (
    StackSurvivalConfig,
    annotate_stack_survival,
    make_scratch_exit_fn,
)
from research.irt_lvn.run_hold_break_of import load_depth_window_with_book
from research.mat_context.report import arm_pack, write_exp_report
from research.run_cfade_all_months import _sid_chrono_key

HORIZONS = [10_000, 60_000, 300_000]
TARGETS = [5.0, 10.0, 20.0]
NS_MIN = 60 * 1_000_000_000


def _load_exp() -> dict:
    path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0087.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _sim(sid, pair, ev, *, gate, slip=0.0, exit_fn=None):
    if ev.num_rows == 0:
        return simulate_session(
            sid, pair.nq, pair.es, F.mat_sim_cfg(slip_ticks=slip),
            gate=gate, bracket_fn=F.mat_vol_bracket(),
            vwap_from_ns=pair.vwap_from_ns, events=pa.table({}),
            exit_fn=exit_fn,
        )
    cols = {"event_ts_ns": ev.column("event_ts_ns"), "side": ev.column("side")}
    for c in ev.column_names:
        if c.startswith("sv_") or c.startswith("bc_"):
            cols[c] = ev.column(c)
    return simulate_session(
        sid, pair.nq, pair.es, F.mat_sim_cfg(slip_ticks=slip),
        gate=gate, bracket_fn=F.mat_vol_bracket(),
        vwap_from_ns=pair.vwap_from_ns, events=pa.table(cols),
        exit_fn=exit_fn,
    )


def _pack(sims: dict) -> dict:
    parts, is_p, oos_p = [], [], []
    for sid, s in sims.items():
        parts.append(s)
        (is_p if F.is_is(sid) else oos_p).append(s)
    return arm_pack(parts, is_p, oos_p)


def _concat(parts: list[pa.Table]) -> pa.Table:
    parts = [p for p in parts if p.num_rows]
    if not parts:
        return pa.table({})
    cols = set(parts[0].column_names)
    for p in parts[1:]:
        cols &= set(p.column_names)
    keep = [c for c in parts[0].column_names if c in cols]
    return pa.concat_tables([p.select(keep) for p in parts], promote_options="default")


def _oos_pnls(sims: dict, commission: float = F.COMMISSION) -> np.ndarray:
    out = []
    for sid, s in sims.items():
        if F.is_is(sid):
            continue
        for t in s.trades:
            out.append(t.pnl - commission)
    return np.asarray(out, dtype=np.float64)


def _random_keep_p90(pool: np.ndarray, n_keep: int, *, seed: int, draws: int = 1000) -> float:
    if n_keep <= 0 or len(pool) == 0:
        return float("nan")
    n_keep = min(n_keep, len(pool))
    rng = np.random.default_rng(seed)
    avgs = np.empty(draws)
    for i in range(draws):
        idx = rng.choice(len(pool), size=n_keep, replace=False)
        avgs[i] = float(np.mean(pool[idx]))
    return float(np.quantile(avgs, 0.90))


def _filter_flag(ev: pa.Table, col: str) -> pa.Table:
    if ev.num_rows == 0 or col not in ev.column_names:
        return pa.table({})
    mk = ev.column(col).to_numpy().astype(np.int8) == 1
    return ev.take(np.where(mk)[0]) if mk.any() else pa.table({})


def _load_or_build_events(
    *,
    pq_dir: Path,
    chrono: list[str],
    depth_sids: set[str],
    cache: Path,
    bc_cfg: BookCorridorConfig,
    sv_cfg: StackSurvivalConfig,
    seed_min: int,
    max_sessions: int | None,
) -> tuple[pa.Table, dict]:
    if cache.exists():
        print(f"[0087] loading cached events {cache}", flush=True)
        ann = pq.read_table(cache)
        if "sv_scratch" not in ann.column_names:
            ann = annotate_stack_survival(ann, cfg=sv_cfg)
        # rebuild pairs for sessions present
        pairs = {}
        sids = set(ann.column("session_id").to_pylist()) if ann.num_rows else set()
        for sid in chrono:
            if sid not in sids:
                continue
            pair = H.load_pair(pq_dir, sid)
            if pair is not None:
                pairs[sid] = pair
        return ann, pairs

    print("[0087] no EXP-0086 cache — rebuilding corridor+survival annotate", flush=True)
    pairs = {}
    parts = []
    for sid in chrono:
        if sid not in depth_sids:
            continue
        pair = H.load_pair(pq_dir, sid)
        if pair is None:
            continue
        pairs[sid] = pair
        open_ns = pair.bounds[0]
        depth, ib, ia = load_depth_window_with_book(
            pq_dir, sid, "NQ",
            (open_ns - seed_min * NS_MIN, pair.bounds[1]),
        )
        raw = detect_signals(pair.nq, pair.es, F.mat_sim_cfg())
        if raw.num_rows == 0:
            continue
        ann = annotate_book_corridor(
            raw, depth, pair.nq,
            init_bids=ib, init_asks=ia, cfg=bc_cfg,
            extra_offsets_sec=(2.0, 5.0),
        )
        ann = annotate_stack_survival(ann, cfg=sv_cfg)
        ann = ann.append_column("session_id", pa.array([sid] * ann.num_rows))
        parts.append(ann)
        print(
            f"  {sid}: mat={ann.num_rows} scratch={int(ann.column('sv_scratch').to_numpy().sum())}",
            flush=True,
        )
        if max_sessions and len(pairs) >= max_sessions:
            break
    ann_all = _concat(parts)
    return ann_all, pairs


def run(*, max_sessions: int | None = None, skip_docs: bool = False) -> dict:
    cfg = load_config()
    exp = _load_exp()
    pq_dir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0087"
    out.mkdir(parents=True, exist_ok=True)
    kill_cfg = dict(exp.get("kill") or {})
    scfg = dict(exp.get("survival") or {})
    seed = int(exp.get("placebo_seed", 87))
    sv_cfg = StackSurvivalConfig(
        decision_sec=float(scfg.get("decision_sec", 2.0)),
        descriptive_sec=float(scfg.get("descriptive_sec", 5.0)),
        remain_scratch_max=float(scfg.get("remain_scratch_max", 0.50)),
    )
    # corridor geometry must match EXP-0086 cache
    exp86 = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "experiments" / "EXP-0086.yaml").read_text(
            encoding="utf-8"
        )
    )
    ccfg = dict(exp86.get("corridor") or {})
    bc_cfg = BookCorridorConfig(
        zone_pts=float(ccfg.get("zone_pts", 20)),
        band_pts=float(ccfg.get("band_pts", 5)),
        share_min=float(ccfg.get("share_min", 0.50)),
    )
    seed_min = int(ccfg.get("depth_seed_min_before_open", 30))

    sessions = F.clean_sessions(pq_dir)
    chrono = sorted(sessions, key=_sid_chrono_key)
    if max_sessions:
        chrono = chrono[:max_sessions]
    cov = F.depth_mbo_coverage(pq_dir, chrono)
    depth_sids = set(cov["depth_sessions"])
    cache = Path(cfg["results_dir"]) / "EXP-0086" / "events.parquet"

    ann_all, pairs = _load_or_build_events(
        pq_dir=pq_dir, chrono=chrono, depth_sids=depth_sids, cache=cache,
        bc_cfg=bc_cfg, sv_cfg=sv_cfg, seed_min=seed_min, max_sessions=max_sessions,
    )
    if ann_all.num_rows and "sv_scratch" not in ann_all.column_names:
        ann_all = annotate_stack_survival(ann_all, cfg=sv_cfg)
    if ann_all.num_rows:
        pq.write_table(ann_all, out / "events.parquet", compression="zstd")

    n_scratch = int(ann_all.column("sv_scratch").to_numpy().sum()) if ann_all.num_rows else 0
    n_survive = int(ann_all.column("sv_survive").to_numpy().sum()) if ann_all.num_rows else 0
    print(
        f"[0087] annotated={ann_all.num_rows} scratch={n_scratch} survive={n_survive} "
        f"sessions={len(pairs)}",
        flush=True,
    )

    gate = F.mat_frozen_gate()
    exit_fn = make_scratch_exit_fn(sv_cfg.remain_scratch_max)
    report: dict = {"status": "ok", "experiment": "EXP-0087", "stages": {}, "coverage": cov}
    md = [
        "# EXP-0087 — MAT stack survival scratch exit (H78)",
        "",
        "Frozen: `research/experiments/EXP-0087.yaml`. Primary = scratch exit "
        "when support remain < 0.50 at stamp+2s, vs base on same entries.",
        f"Annotated MAT: {ann_all.num_rows} · scratch fires: {n_scratch}",
        "",
    ]

    # Stage 0 — truncation at decision time + power
    trunc_rows = []
    if ann_all.num_rows and pairs:
        sample = _filter_flag(ann_all, "sv_scratch")
        if sample.num_rows == 0:
            sample = ann_all
        for i in range(min(3, sample.num_rows)):
            sid = sample.column("session_id")[i].as_py()
            stamp = int(sample.column("event_ts_ns")[i].as_py())
            side = int(sample.column("side")[i].as_py())
            decision = stamp + int(sv_cfg.decision_sec * 1e9)
            pair = pairs.get(sid)
            if pair is None:
                continue
            open_ns = pair.bounds[0]
            depth, ib, ia = load_depth_window_with_book(
                pq_dir, sid, "NQ", (open_ns - seed_min * NS_MIN, pair.bounds[1]),
            )
            ev = pa.table({
                "event_ts_ns": np.asarray([stamp], dtype=np.int64),
                "side": np.asarray([side], dtype=np.int8),
            })
            full = annotate_book_corridor(
                ev, depth, pair.nq, init_bids=ib, init_asks=ia, cfg=bc_cfg,
                extra_offsets_sec=(2.0, 5.0),
            )
            full = annotate_stack_survival(full, cfg=sv_cfg)
            if depth.num_rows:
                dts = depth.column("ts_ns").to_numpy().astype(np.int64)
                cut = depth.take(np.where(dts <= decision)[0])
            else:
                cut = depth
            trunc = annotate_book_corridor(
                ev, cut, pair.nq, init_bids=ib, init_asks=ia, cfg=bc_cfg,
                extra_offsets_sec=(2.0, 5.0),
            )
            trunc = annotate_stack_survival(trunc, cfg=sv_cfg)
            ok = True
            diffs = {}
            for col in ("sv_sup_remain", "sv_scratch"):
                a, b = full.column(col)[0].as_py(), trunc.column(col)[0].as_py()
                if isinstance(a, float):
                    match = (
                        (a is None and b is None)
                        or (
                            isinstance(a, float)
                            and isinstance(b, float)
                            and ((np.isnan(a) and np.isnan(b)) or abs(a - b) < 1e-9)
                        )
                    )
                else:
                    match = a == b
                if not match:
                    ok = False
                    diffs[col] = (a, b)
            trunc_rows.append({"session": sid, "match": ok, "diffs": diffs})

    trunc_ok = bool(trunc_rows) and all(r["match"] for r in trunc_rows)

    # power: base + scratch books under frozen gate
    sims_base: dict = {}
    sims_scratch: dict = {}
    for sid, pair in pairs.items():
        if ann_all.num_rows == 0:
            continue
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        if not mask.any():
            continue
        sub = ann_all.take(np.where(mask)[0])
        sims_base[sid] = _sim(sid, pair, sub, gate=gate)
        sims_scratch[sid] = _sim(sid, pair, sub, gate=gate, exit_fn=exit_fn)
    n_base_tr = sum(s.n for s in sims_base.values())
    n_scr_tr = sum(s.n for s in sims_scratch.values())
    min_fires = int(kill_cfg.get("min_scratch_fires", 50))
    min_tr = int(kill_cfg.get("min_resim_trades", 20))
    power_ok = n_scratch >= min_fires and n_base_tr >= min_tr and n_scr_tr >= min_tr

    report["stages"]["0"] = {
        "n_mat": ann_all.num_rows,
        "n_scratch": n_scratch,
        "n_survive": n_survive,
        "n_base_trades": n_base_tr,
        "n_scratch_trades": n_scr_tr,
        "truncation": trunc_rows,
        "truncation_ok": trunc_ok,
        "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — causality + power",
        "",
        f"scratch fires: **{n_scratch}** · survive: {n_survive}",
        f"base trades: **{n_base_tr}** · scratch-exit trades: **{n_scr_tr}**",
        f"Truncation@decision: **{'PASS' if trunc_ok else 'FAIL'}** · "
        f"power: **{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    if not trunc_ok or not power_ok:
        why = (
            "Stage 0 causality failed"
            if not trunc_ok
            else "Stage 0 underpowered (counts are the result; no retune)"
        )
        report["verdict"] = f"reject / park — {why}"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title="EXP-0087", md_lines=md, report=report)
        if not skip_docs:
            _write_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # Stage 1 — scratched path worse than survived (mechanism)
    print("[0087] Stage 1…", flush=True)
    lab_store = {"scratched": [], "survived": [], "delayed_ok": [], "base": [], "null": []}
    rng = np.random.default_rng(seed)
    for sid, pair in pairs.items():
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        if not mask.any():
            continue
        sub = ann_all.take(np.where(mask)[0])
        sets = {
            "scratched": _filter_flag(sub, "sv_scratch"),
            "survived": _filter_flag(sub, "sv_survive"),
            "delayed_ok": _filter_flag(sub, "sv_delayed_ok"),
            "base": sub,
        }
        n_null = max(sets["scratched"].num_rows, 1)
        nul = H.null_events(pair, n_null, seed=seed + hash(sid) % 10_000)
        if sets["scratched"].num_rows and nul.num_rows:
            sides = sets["scratched"].column("side").to_numpy()
            pick = rng.choice(sides, size=nul.num_rows, replace=True)
            nul = nul.drop("side").append_column("side", pa.array(pick.astype(np.int8)))
        sets["null"] = nul
        for name, tbl in sets.items():
            if tbl.num_rows:
                lab = H.label_in_points(tbl, pair, HORIZONS, TARGETS)
                if lab.num_rows:
                    lab_store[name].append(lab)

    stage1 = {}
    md += [
        "## Stage 1 — event study (mechanism: scratched worse than survived)",
        "",
        "| set | n | hit5 | hit10 | @10s | @60s | @300s | MFE | MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("base", "scratched", "survived", "delayed_ok", "null"):
        s = H.summarize(lab_store[name], HORIZONS, TARGETS)
        stage1[name] = s
        if s.get("n_events"):
            md.append(
                f"| {name} | {s['n_events']} | "
                f"{s.get('p_hit_5pts', float('nan')):.2f} | "
                f"{s.get('p_hit_10pts', float('nan')):.2f} | "
                f"{H.fmt(s.get('mean_ret_pts_10000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_60000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_300000ms'), 2)} | "
                f"{H.fmt(s.get('mean_mfe_pts'), 1)} | "
                f"{H.fmt(s.get('mean_mae_pts'), 1)} |"
            )
        else:
            md.append(f"| {name} | 0 | – | – | – | – | – | – | – |")

    lift_60 = float(kill_cfg.get("stage1_lift_60s_pts", 1.0))
    lift_300 = float(kill_cfg.get("stage1_lift_300s_pts", 2.0))
    sc, sv = stage1["scratched"], stage1["survived"]
    m60sc, m60sv = sc.get("mean_ret_pts_60000ms"), sv.get("mean_ret_pts_60000ms")
    m300sc, m300sv = sc.get("mean_ret_pts_300000ms"), sv.get("mean_ret_pts_300000ms")
    # scratched should be WORSE than survived by the lift amounts
    s1 = {
        "scratch_worse_60": (
            m60sc is not None and m60sv is not None
            and np.isfinite(m60sc) and np.isfinite(m60sv)
            and (m60sv - m60sc) >= lift_60
        ),
        "scratch_worse_300": (
            m300sc is not None and m300sv is not None
            and np.isfinite(m300sc) and np.isfinite(m300sv)
            and (m300sv - m300sc) >= lift_300
        ),
    }
    s1_pass = all(s1.values())
    report["stages"]["1"] = {"summaries": stage1, "checks": s1, "pass": s1_pass}
    md += ["", f"Checks: {s1}", f"**Stage 1: {'PASS' if s1_pass else 'FAIL'}**", ""]
    # delayed_ok is descriptive only
    md += [
        "",
        f"_Descriptive delayed_ok n={stage1['delayed_ok'].get('n_events', 0)} "
        f"(cannot promote inside this EXP)._",
        "",
    ]
    if not s1_pass:
        report["verdict"] = (
            "reject / park — Stage 1 mechanism fail "
            "(scratched not worse than survived; no Stage 2, no retune)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title="EXP-0087", md_lines=md, report=report)
        if not skip_docs:
            _write_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # Stage 2 — P&L scratch vs base same entries
    print("[0087] Stage 2…", flush=True)
    sims = {"base": {}, "scratch_support": {}}
    for sid, pair in pairs.items():
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        sub = ann_all.take(np.where(mask)[0]) if mask.any() else pa.table({})
        sims["base"][sid] = _sim(sid, pair, sub, gate=gate)
        sims["scratch_support"][sid] = _sim(sid, pair, sub, gate=gate, exit_fn=exit_fn)

    books = {a: _pack(sims[a]) for a in ("base", "scratch_support")}
    # placebo: random early-exit subset of base OOS — approximate via random-keep
    # of base OOS pnls sized to scratch book n (conservative; same as 0086 pattern)
    pool = _oos_pnls(sims["base"])
    p90 = _random_keep_p90(pool, books["scratch_support"]["oos"]["n"], seed=seed + 1)
    min_oos = int(kill_cfg.get("min_oos_trades_promote", 50))
    min_lift = float(kill_cfg.get("min_avg_lift_pts", 1.0))
    a, b = books["scratch_support"]["oos"], books["base"]["oos"]
    avg = a.get("avg")
    kill = {
        "oos_net_pos": a["net"] > 0,
        "beat_base_plus_lift": (
            avg is not None and b.get("avg") is not None and avg >= b["avg"] + min_lift
        ),
        "beat_placebo_p90": avg is not None and np.isfinite(p90) and avg > p90,
        "n_oos_ge_min": a["n"] >= min_oos,
        "placebo_p90": None if not np.isfinite(p90) else round(p90, 4),
    }
    kill["pass"] = all(
        kill[k]
        for k in ("oos_net_pos", "beat_base_plus_lift", "beat_placebo_p90", "n_oos_ge_min")
    )
    md += [
        "## Stage 2 — P&L (same entries; scratch exit vs base)",
        "",
        "| book | n | WR | net | avg | IS net | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("base", "scratch_support"):
        v = books[name]
        aa, oo = v["all"], v["oos"]
        md.append(
            f"| {name} | {aa['n']} | {aa.get('wr')} | {aa['net']:+.1f} | {aa.get('avg')} | "
            f"{v['is']['net']:+.1f} | {oo['net']:+.1f} | {oo.get('avg')} |"
        )
    md += ["", f"Kill: {kill}", f"**Stage 2: {'PASS' if kill['pass'] else 'FAIL'}**", ""]
    report["stages"]["2"] = {"books": books, "kill": kill, "pass": kill["pass"]}
    report["verdict"] = (
        "PASS research — candidate scratch exit; virgin months before ship"
        if kill["pass"]
        else "reject / park — Stage 2 P&L fail (no retune)"
    )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    write_exp_report(out, title="EXP-0087", md_lines=md, report=report)
    if not skip_docs:
        _write_docs(report["verdict"])
    print(report["verdict"], flush=True)
    return report


def _write_docs(verdict: str) -> None:
    hyp = Path("docs/HYPOTHESES.md")
    text = hyp.read_text(encoding="utf-8")
    needle = (
        "## H78 — MAT stack survival after stamp (scratch if support collapses)\n"
    )
    # find Result line under H78
    idx = text.find(needle)
    if idx >= 0:
        ridx = text.find("**Result:** pending — registered 2026-10-30.", idx)
        if ridx >= 0 and ridx < text.find("## H79", idx):
            text = (
                text[:ridx]
                + f"**Result (EXP-0087): {verdict}.** See `results/EXP-0087/`."
                + text[ridx + len("**Result:** pending — registered 2026-10-30.") :]
            )
            hyp.write_text(text, encoding="utf-8")
    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    old = (
        "| EXP-0087 | H78 MAT stack survival scratch (+2s) | registered — pending run | "
        "depth sessions (DIG4 IS) | scratch if support remain <0.50; see notes |"
    )
    new = (
        f"| EXP-0087 | H78 MAT stack survival scratch (+2s) | "
        f"ran — **{verdict.split('—')[0].strip()}** | "
        f"depth sessions | {verdict}; see notes |"
    )
    if old in lt:
        lt = lt.replace(old, new, 1)
    note = (
        f"\n### 2026-10-30 — EXP-0087 MAT stack survival run (H78)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0087/`.\n"
        f"- Runner: `python -m research.mat_context.exp_0087_stack_survival`.\n"
    )
    if "### 2026-10-30 — EXP-0087 MAT stack survival run (H78)" not in lt:
        anchor = "### 2026-10-30 — EXP-0086…0088 MAT resting-book programme registered (H77–H79)"
        idx = lt.find(anchor)
        nxt = lt.find("\n### ", idx + 5) if idx >= 0 else -1
        if nxt > 0:
            lt = lt[:nxt] + note + lt[nxt:]
        else:
            lt = lt + note
    log.write_text(lt, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sessions", type=int, default=None)
    ap.add_argument("--skip-docs", action="store_true")
    args = ap.parse_args()
    run(max_sessions=args.max_sessions, skip_docs=args.skip_docs)


if __name__ == "__main__":
    main()
