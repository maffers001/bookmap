"""EXP-0088 / H79 — MAT burst absorption at tracked level (C4 one-shot).

Primary = breakthrough keep-gate on frozen MAT. Exactly one attempt.

Usage:
  python -m research.mat_context.exp_0088_burst_absorption
  python -m research.mat_context.exp_0088_burst_absorption --max-sessions 5
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
from research.features.burst_level_absorption import (
    BurstLevelConfig,
    annotate_burst_level,
)
from research.irt_lvn.run_hold_break_of import load_depth_window_with_book
from research.mat_context.report import arm_pack, write_exp_report
from research.run_cfade_all_months import _sid_chrono_key

HORIZONS = [10_000, 60_000, 300_000]
TARGETS = [5.0, 10.0, 20.0]
NS_MIN = 60 * 1_000_000_000
ARMS = ("base", "breakthrough", "absorbed", "no_level")


def _load_exp() -> dict:
    path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0088.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _filter_flag(ev: pa.Table, col: str) -> pa.Table:
    if ev.num_rows == 0 or col not in ev.column_names:
        return pa.table({})
    mk = ev.column(col).to_numpy().astype(np.int8) == 1
    return ev.take(np.where(mk)[0]) if mk.any() else pa.table({})


def _sim(sid, pair, ev, *, gate, slip=0.0):
    if ev.num_rows == 0:
        return simulate_session(
            sid, pair.nq, pair.es, F.mat_sim_cfg(slip_ticks=slip),
            gate=gate, bracket_fn=F.mat_vol_bracket(),
            vwap_from_ns=pair.vwap_from_ns, events=pa.table({}),
        )
    cols = {"event_ts_ns": ev.column("event_ts_ns"), "side": ev.column("side")}
    return simulate_session(
        sid, pair.nq, pair.es, F.mat_sim_cfg(slip_ticks=slip),
        gate=gate, bracket_fn=F.mat_vol_bracket(),
        vwap_from_ns=pair.vwap_from_ns, events=pa.table(cols),
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


def _book_kill(arm, base, twin, *, placebo_p90, min_oos_n, min_lift):
    a, b, t = arm["oos"], base["oos"], twin["oos"]
    avg = a.get("avg")
    kill = {
        "oos_net_pos": a["net"] > 0,
        "beat_base_plus_lift": (
            avg is not None and b.get("avg") is not None and avg >= b["avg"] + min_lift
        ),
        "beat_twin": avg is not None and t.get("avg") is not None and avg > t["avg"],
        "beat_placebo_p90": avg is not None and np.isfinite(placebo_p90) and avg > placebo_p90,
        "n_oos_ge_min": a["n"] >= min_oos_n,
        "placebo_p90": None if not np.isfinite(placebo_p90) else round(placebo_p90, 4),
    }
    kill["pass"] = all(
        kill[k]
        for k in (
            "oos_net_pos",
            "beat_base_plus_lift",
            "beat_twin",
            "beat_placebo_p90",
            "n_oos_ge_min",
        )
    )
    return kill


def run(*, max_sessions: int | None = None, skip_docs: bool = False) -> dict:
    cfg = load_config()
    exp = _load_exp()
    pq_dir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0088"
    out.mkdir(parents=True, exist_ok=True)
    kill_cfg = dict(exp.get("kill") or {})
    lcfg = dict(exp.get("level") or {})
    seed = int(exp.get("placebo_seed", 88))
    bl_cfg = BurstLevelConfig(
        engage_window_sec=float(lcfg.get("engage_window_sec", 3.0)),
        min_executed=float(lcfg.get("min_executed", 10)),
        breakthrough_pierce_ticks=float(lcfg.get("breakthrough_pierce_ticks", 4)),
        breakthrough_consume_ratio=float(lcfg.get("breakthrough_consume_ratio", 1.0)),
    )
    seed_min = int(lcfg.get("depth_seed_min_before_open", 30))

    sessions = F.clean_sessions(pq_dir)
    chrono = sorted(sessions, key=_sid_chrono_key)
    if max_sessions:
        chrono = chrono[:max_sessions]
    cov = F.depth_mbo_coverage(pq_dir, chrono)
    depth_sids = set(cov["depth_sessions"])
    print(f"[0088] {len(chrono)} sessions, depth={len(depth_sids)}", flush=True)

    gate = F.mat_frozen_gate()
    ann_parts: list[pa.Table] = []
    pairs: dict[str, H.Pair] = {}
    hand: list[dict] = []

    for sid in chrono:
        if sid not in depth_sids:
            continue
        pair = H.load_pair(pq_dir, sid)
        if pair is None:
            continue
        pairs[sid] = pair
        open_ns = pair.bounds[0]
        depth, _ib, _ia = load_depth_window_with_book(
            pq_dir, sid, "NQ",
            (open_ns - seed_min * NS_MIN, pair.bounds[1]),
        )
        raw = detect_signals(pair.nq, pair.es, F.mat_sim_cfg())
        if raw.num_rows == 0:
            continue
        # tracker needs trades in the window; use NQ trades from pair
        ann = annotate_burst_level(raw, depth, pair.nq, cfg=bl_cfg)
        ann = ann.append_column("session_id", pa.array([sid] * ann.num_rows))
        ann_parts.append(ann)
        n_bt = int(ann.column("bl_breakthrough").to_numpy().sum())
        print(
            f"  {sid}: mat={ann.num_rows} bt={n_bt} "
            f"abs={int(ann.column('bl_absorbed').to_numpy().sum())} "
            f"nl={int(ann.column('bl_no_level').to_numpy().sum())}",
            flush=True,
        )
        if len(hand) < 3 and n_bt:
            idx = int(np.where(ann.column("bl_breakthrough").to_numpy() == 1)[0][0])
            hand.append({
                "session": sid,
                "executed": ann.column("bl_executed")[idx].as_py(),
                "penetration": ann.column("bl_penetration")[idx].as_py(),
                "consume_ratio": ann.column("bl_consume_ratio")[idx].as_py(),
            })

    ann_all = _concat(ann_parts)
    if ann_all.num_rows:
        pq.write_table(ann_all, out / "events.parquet", compression="zstd")

    report: dict = {"status": "ok", "experiment": "EXP-0088", "stages": {}, "coverage": cov}
    md = [
        "# EXP-0088 — MAT burst level absorption (H79, C4 one-shot)",
        "",
        "Frozen: `research/experiments/EXP-0088.yaml`. Primary = breakthrough "
        "keep-gate. Exactly one pre-registered attempt — close either way.",
        f"Depth sessions used: {len(pairs)} · annotated MAT: {ann_all.num_rows}",
        "",
    ]

    n_bt = int(ann_all.column("bl_breakthrough").to_numpy().sum()) if ann_all.num_rows else 0
    n_abs = int(ann_all.column("bl_absorbed").to_numpy().sum()) if ann_all.num_rows else 0
    n_nl = int(ann_all.column("bl_no_level").to_numpy().sum()) if ann_all.num_rows else 0

    # Stage 0 truncation: cut depth+trades at stamp+engage_window
    trunc_rows = []
    if ann_all.num_rows and pairs:
        sample = _filter_flag(ann_all, "bl_breakthrough")
        if sample.num_rows == 0:
            sample = ann_all
        win_ns = int(bl_cfg.engage_window_sec * 1e9)
        for i in range(min(3, sample.num_rows)):
            sid = sample.column("session_id")[i].as_py()
            stamp = int(sample.column("event_ts_ns")[i].as_py())
            side = int(sample.column("side")[i].as_py())
            pair = pairs[sid]
            open_ns = pair.bounds[0]
            depth, _ib, _ia = load_depth_window_with_book(
                pq_dir, sid, "NQ", (open_ns - seed_min * NS_MIN, pair.bounds[1]),
            )
            ev = pa.table({
                "event_ts_ns": np.asarray([stamp], dtype=np.int64),
                "side": np.asarray([side], dtype=np.int8),
            })
            full = annotate_burst_level(ev, depth, pair.nq, cfg=bl_cfg)
            decision = stamp + win_ns
            if depth.num_rows:
                dts = depth.column("ts_ns").to_numpy().astype(np.int64)
                cut_d = depth.take(np.where(dts <= decision)[0])
            else:
                cut_d = depth
            tts = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
            cut_t = pair.nq.take(np.where(tts <= decision)[0])
            trunc = annotate_burst_level(ev, cut_d, cut_t, cfg=bl_cfg)
            ok = True
            diffs = {}
            for col in ("bl_class", "bl_breakthrough", "bl_executed"):
                a, b = full.column(col)[0].as_py(), trunc.column(col)[0].as_py()
                if isinstance(a, float):
                    match = (
                        (a is None and b is None)
                        or (
                            isinstance(a, float)
                            and isinstance(b, float)
                            and ((np.isnan(a) and np.isnan(b)) or abs(a - b) < 1e-6)
                        )
                    )
                else:
                    match = a == b
                if not match:
                    ok = False
                    diffs[col] = (a, b)
            trunc_rows.append({"session": sid, "match": ok, "diffs": diffs})

    trunc_ok = bool(trunc_rows) and all(r["match"] for r in trunc_rows)

    sims_bt: dict = {}
    for sid, pair in pairs.items():
        if ann_all.num_rows == 0:
            continue
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        if not mask.any():
            continue
        sub = ann_all.take(np.where(mask)[0])
        sims_bt[sid] = _sim(sid, pair, _filter_flag(sub, "bl_breakthrough"), gate=gate)
    n_bt_trades = sum(s.n for s in sims_bt.values())
    min_sig = int(kill_cfg.get("min_primary_signals", 50))
    min_tr = int(kill_cfg.get("min_resim_trades", 20))
    power_ok = n_bt >= min_sig and n_bt_trades >= min_tr

    report["stages"]["0"] = {
        "n_depth_sessions": len(pairs),
        "n_mat": ann_all.num_rows,
        "n_breakthrough": n_bt,
        "n_absorbed": n_abs,
        "n_no_level": n_nl,
        "n_breakthrough_trades": n_bt_trades,
        "truncation": trunc_rows,
        "truncation_ok": trunc_ok,
        "hand": hand,
        "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — causality + power",
        "",
        f"breakthrough: **{n_bt}** · absorbed: {n_abs} · no_level: {n_nl}",
        f"breakthrough frozen trades: **{n_bt_trades}**",
        f"Truncation: **{'PASS' if trunc_ok else 'FAIL'}** · "
        f"power: **{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    if not trunc_ok or not power_ok:
        why = (
            "Stage 0 causality failed"
            if not trunc_ok
            else "Stage 0 underpowered (counts are the result; C4 closed, no retune)"
        )
        report["verdict"] = f"reject / park — {why}"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title="EXP-0088", md_lines=md, report=report)
        if not skip_docs:
            _write_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # Stage 1
    print("[0088] Stage 1…", flush=True)
    lab_store = {a: [] for a in (*ARMS, "null")}
    rng = np.random.default_rng(seed)
    for sid, pair in pairs.items():
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        if not mask.any():
            continue
        sub = ann_all.take(np.where(mask)[0])
        sets = {
            "base": sub,
            "breakthrough": _filter_flag(sub, "bl_breakthrough"),
            "absorbed": _filter_flag(sub, "bl_absorbed"),
            "no_level": _filter_flag(sub, "bl_no_level"),
        }
        n_null = max(sets["breakthrough"].num_rows, 1)
        nul = H.null_events(pair, n_null, seed=seed + hash(sid) % 10_000)
        if sets["breakthrough"].num_rows and nul.num_rows:
            sides = sets["breakthrough"].column("side").to_numpy()
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
        "## Stage 1 — event study",
        "",
        "| set | n | hit5 | hit10 | @10s | @60s | @300s | MFE | MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in (*ARMS, "null"):
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
    bt, ab, n = stage1["breakthrough"], stage1["absorbed"], stage1["null"]
    m60bt, m60ab = bt.get("mean_ret_pts_60000ms"), ab.get("mean_ret_pts_60000ms")
    m300bt, m300ab = bt.get("mean_ret_pts_300000ms"), ab.get("mean_ret_pts_300000ms")
    m60n = n.get("mean_ret_pts_60000ms")
    s1 = {
        "lift_60": (
            m60bt is not None
            and m60ab is not None
            and np.isfinite(m60bt)
            and np.isfinite(m60ab)
            and (m60bt - m60ab) >= lift_60
        ),
        "lift_300": (
            m300bt is not None
            and m300ab is not None
            and np.isfinite(m300bt)
            and np.isfinite(m300ab)
            and (m300bt - m300ab) >= lift_300
        ),
        "beat_null_60": (
            m60bt is not None
            and m60n is not None
            and np.isfinite(m60bt)
            and np.isfinite(m60n)
            and m60bt > m60n
        ),
    }
    s1_pass = all(s1.values())
    report["stages"]["1"] = {"summaries": stage1, "checks": s1, "pass": s1_pass}
    md += ["", f"Checks: {s1}", f"**Stage 1: {'PASS' if s1_pass else 'FAIL'}**", ""]
    if not s1_pass:
        report["verdict"] = (
            "reject / park — Stage 1 event-study fail "
            "(C4 closed; no Stage 2 P&L, no retune)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title="EXP-0088", md_lines=md, report=report)
        if not skip_docs:
            _write_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # Stage 2
    print("[0088] Stage 2…", flush=True)
    sims = {a: {} for a in ARMS}
    sims["null"] = {}
    for sid, pair in pairs.items():
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        sub = ann_all.take(np.where(mask)[0]) if mask.any() else pa.table({})
        sets = {
            "base": sub,
            "breakthrough": _filter_flag(sub, "bl_breakthrough"),
            "absorbed": _filter_flag(sub, "bl_absorbed"),
            "no_level": _filter_flag(sub, "bl_no_level"),
        }
        for a, ev in sets.items():
            sims[a][sid] = _sim(sid, pair, ev, gate=gate)
        nul = H.null_events(pair, max(sub.num_rows, 1), seed=seed + 99 + hash(sid) % 10_000)
        sims["null"][sid] = _sim(sid, pair, nul, gate=gate)

    books = {a: _pack(sims[a]) for a in (*ARMS, "null")}
    pool = _oos_pnls(sims["base"])
    p90 = _random_keep_p90(pool, books["breakthrough"]["oos"]["n"], seed=seed + 1)
    min_oos = int(kill_cfg.get("min_oos_trades_promote", 50))
    min_lift = float(kill_cfg.get("min_avg_lift_pts", 1.0))
    kill = _book_kill(
        books["breakthrough"],
        books["base"],
        books["absorbed"],
        placebo_p90=p90,
        min_oos_n=min_oos,
        min_lift=min_lift,
    )
    md += [
        "## Stage 2 — P&L",
        "",
        "| book | n | WR | net | avg | IS net | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for a in (*ARMS, "null"):
        v = books[a]
        aa, oo = v["all"], v["oos"]
        md.append(
            f"| {a} | {aa['n']} | {aa.get('wr')} | {aa['net']:+.1f} | {aa.get('avg')} | "
            f"{v['is']['net']:+.1f} | {oo['net']:+.1f} | {oo.get('avg')} |"
        )
    md += ["", f"Kill: {kill}", f"**Stage 2: {'PASS' if kill['pass'] else 'FAIL'}**", ""]
    report["stages"]["2"] = {"books": books, "kill": kill, "pass": kill["pass"]}
    report["verdict"] = (
        "PASS research — candidate breakthrough keep-gate; virgin months before ship "
        "(C4 closed either way)"
        if kill["pass"]
        else "reject / park — Stage 2 P&L fail (C4 closed; no retune)"
    )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    write_exp_report(out, title="EXP-0088", md_lines=md, report=report)
    if not skip_docs:
        _write_docs(report["verdict"])
    print(report["verdict"], flush=True)
    return report


def _write_docs(verdict: str) -> None:
    hyp = Path("docs/HYPOTHESES.md")
    text = hyp.read_text(encoding="utf-8")
    needle = "## H79 — MAT burst absorption at tracked level (C4 one-shot)\n"
    idx = text.find(needle)
    if idx >= 0:
        ridx = text.find("**Result:** pending — registered 2026-10-30.", idx)
        end = text.find("\n---\n", idx)
        if ridx >= 0 and (end < 0 or ridx < end):
            text = (
                text[:ridx]
                + f"**Result (EXP-0088): {verdict}.** See `results/EXP-0088/`."
                + text[ridx + len("**Result:** pending — registered 2026-10-30.") :]
            )
            hyp.write_text(text, encoding="utf-8")
    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    old = (
        "| EXP-0088 | H79 MAT burst level absorption (C4) | registered — pending run | "
        "depth sessions (DIG4 IS) | breakthrough keep-gate; one-shot then close; see notes |"
    )
    new = (
        f"| EXP-0088 | H79 MAT burst level absorption (C4) | "
        f"ran — **{verdict.split('—')[0].strip()}** | "
        f"depth sessions | {verdict}; see notes |"
    )
    if old in lt:
        lt = lt.replace(old, new, 1)
    note = (
        f"\n### 2026-10-30 — EXP-0088 MAT burst absorption run (H79, C4 closed)\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/EXP-0088/`.\n"
        f"- Runner: `python -m research.mat_context.exp_0088_burst_absorption`.\n"
        f"- C4 one-shot closed; no further absorption-on-MAT retunes on this sample.\n"
    )
    if "### 2026-10-30 — EXP-0088 MAT burst absorption run (H79, C4 closed)" not in lt:
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
