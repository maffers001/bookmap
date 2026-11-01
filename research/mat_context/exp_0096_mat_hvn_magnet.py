"""EXP-0096 / H87 and EXP-0097 / H88 — HVN magnet + dVPOC clearance dig.

Usage:
  python -m research.mat_context.exp_0096_mat_hvn_magnet --market nq
  python -m research.mat_context.exp_0096_mat_hvn_magnet --market es
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.backtest.mat_sim import detect_signals, simulate_session
from research.config import ROOT, load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.mat_hvn_magnet import HvnMagnetConfig, annotate_hvn_magnet
from research.irt_lvn.load_irt import default_irt_paths, load_irt_csv
from research.irt_lvn.study_es_intraday_lvn_large import _session_irt
from research.mat_context.report import arm_pack, write_exp_report
from research.run_cfade_all_months import _sid_chrono_key

HORIZONS = [10_000, 60_000, 300_000]
TARGETS = [5.0, 10.0, 20.0]
CACHE_EXP = "EXP-0096"
ANNOTATE_VERSION = "hvn-magnet-poc-v1"
POC_MINS = (10, 15, 20)
PRIMARY_POC = 15


def _load_exp(market: str) -> dict:
    name = "EXP-0096.yaml" if market == "nq" else "EXP-0097.yaml"
    path = Path(__file__).resolve().parents[1] / "experiments" / name
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
            "oos_net_pos", "beat_base_plus_lift", "beat_twin",
            "beat_placebo_p90", "n_oos_ge_min",
        )
    )
    return kill


def _cols_for(market: str) -> dict[str, str]:
    p = "nq" if market == "nq" else "es"
    return {
        "toward": f"hvn_{p}_toward",
        "away": f"hvn_{p}_away",
        "near": f"hvn_{p}_near",
        "dist": f"hvn_{p}_dist",
        "poc_dist": f"hvn_{p}_poc_dist",
        "toward_poc15": f"hvn_{p}_toward_poc15",
        "toward_poc10": f"hvn_{p}_toward_poc10",
        "toward_poc20": f"hvn_{p}_toward_poc20",
        "away_poc15": f"hvn_{p}_away_poc15",
    }


def run(*, market: str, max_sessions: int | None = None, skip_docs: bool = False) -> dict:
    market = market.lower()
    assert market in ("nq", "es")
    exp_name = "EXP-0096" if market == "nq" else "EXP-0097"
    hyp = "H87" if market == "nq" else "H88"
    C = _cols_for(market)
    primary_col = C["toward_poc15"]
    twin_col = C["toward"]  # toward_near = near∧toward (existing flag)

    cfg = load_config()
    exp = _load_exp(market)
    pq_dir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / exp_name
    out.mkdir(parents=True, exist_ok=True)
    cache_path = Path(cfg["results_dir"]) / CACHE_EXP / "events.parquet"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_marker = cache_path.with_name("annotate_version.txt")

    kill_cfg = dict(exp.get("kill") or {})
    vcfg = dict(exp.get("vicinity") or {})
    seed = int(exp.get("placebo_seed", 96 if market == "nq" else 97))
    hv_cfg = HvnMagnetConfig(nq_band_pts=5.0, es_band_pts=2.0)
    band = float(vcfg.get("band_pts", 5.0 if market == "nq" else 2.0))

    sessions = F.clean_sessions(pq_dir)
    chrono = sorted(sessions, key=_sid_chrono_key)
    if max_sessions:
        chrono = chrono[:max_sessions]

    print(f"[{exp_name}] market={market} band=±{band} poc_dig={list(POC_MINS)} sessions={len(chrono)}", flush=True)
    irt_paths = default_irt_paths(ROOT)
    irt_nq_all = load_irt_csv(irt_paths["NQ"])
    irt_es_all = load_irt_csv(irt_paths["ES"])

    gate_off = None
    pairs: dict[str, H.Pair] = {}
    session_bars: dict[str, tuple] = {}

    cache_ok = (
        cache_path.exists()
        and cache_marker.exists()
        and cache_marker.read_text(encoding="utf-8").strip() == ANNOTATE_VERSION
    )
    if cache_path.exists() and not cache_ok and not max_sessions:
        print(f"[{exp_name}] ignoring stale annotate cache", flush=True)

    if cache_ok and not max_sessions:
        print(f"[{exp_name}] loading cached annotate {cache_path}", flush=True)
        ann_all = pq.read_table(cache_path)
        sids = set(ann_all.column("session_id").to_pylist()) if ann_all.num_rows else set()
        for sid in chrono:
            if sid not in sids:
                continue
            pair = H.load_pair(pq_dir, sid)
            if pair is not None:
                pairs[sid] = pair
        if ann_all.num_rows and exp_name != CACHE_EXP:
            pq.write_table(ann_all, out / "events.parquet", compression="zstd")
    else:
        ann_parts: list[pa.Table] = []
        for sid in chrono:
            pair = H.load_pair(pq_dir, sid)
            if pair is None:
                continue
            pairs[sid] = pair
            nq_b, nq_sh = _session_irt(irt_nq_all, sid, pq_dir, "NQ")
            es_b, es_sh = _session_irt(irt_es_all, sid, pq_dir, "ES")
            session_bars[sid] = (nq_b, es_b)
            raw = detect_signals(pair.nq, pair.es, F.mat_sim_cfg())
            if raw.num_rows == 0:
                continue
            ann = annotate_hvn_magnet(raw, pair.nq, pair.es, nq_b, es_b, cfg=hv_cfg)
            ann = ann.append_column("session_id", pa.array([sid] * ann.num_rows))
            ann_parts.append(ann)
            print(
                f"  {sid}: mat={ann.num_rows} "
                f"t15={int(ann.column(C['toward_poc15']).to_numpy().sum())} "
                f"t={int(ann.column(C['toward']).to_numpy().sum())} "
                f"(shift nq={nq_sh} es={es_sh})",
                flush=True,
            )
        ann_all = _concat(ann_parts)
        if ann_all.num_rows:
            pq.write_table(ann_all, cache_path, compression="zstd")
            cache_marker.write_text(ANNOTATE_VERSION + "\n", encoding="utf-8")
            if exp_name != CACHE_EXP:
                pq.write_table(ann_all, out / "events.parquet", compression="zstd")

    def _n(col: str) -> int:
        return int(ann_all.column(col).to_numpy().sum()) if ann_all.num_rows else 0

    n_prim = _n(primary_col)
    n_twin = _n(twin_col)
    print(
        f"[{exp_name}] annotated={ann_all.num_rows} primary_poc15={n_prim} "
        f"toward_near={n_twin} dig10={_n(C['toward_poc10'])} dig20={_n(C['toward_poc20'])}",
        flush=True,
    )

    report: dict = {
        "status": "ok", "experiment": exp_name, "hypothesis": hyp,
        "market": market, "band_pts": band, "poc_mins": list(POC_MINS),
        "primary": "toward_poc15", "twin": "toward_near", "stages": {},
    }
    md = [
        f"# {exp_name} — HVN magnet + dVPOC clearance dig ({market.upper()}, {hyp})",
        "",
        f"Frozen: `research/experiments/{exp_name}.yaml`. Ungated MAT. "
        f"Near HVN ±{band:g} ∧ toward ∧ |px−dVPOC|≥poc_min. "
        f"Primary = **toward_poc15**; twin = toward_near (no POC filter). "
        f"Dig {list(POC_MINS)}; neighbours cannot promote alone.",
        f"Annotated: {ann_all.num_rows} · poc15: {n_prim} · toward_near: {n_twin} · "
        f"poc10: {_n(C['toward_poc10'])} · poc20: {_n(C['toward_poc20'])}",
        "",
    ]

    # Stage 0
    trunc_rows = []
    if ann_all.num_rows and pairs:
        sample = _filter_flag(ann_all, primary_col)
        if sample.num_rows == 0:
            sample = ann_all
        for i in range(min(3, sample.num_rows)):
            sid = sample.column("session_id")[i].as_py()
            stamp = int(sample.column("event_ts_ns")[i].as_py())
            pair = pairs.get(sid)
            if pair is None:
                continue
            bars_pair = session_bars.get(sid)
            if bars_pair is None:
                nq_b, _ = _session_irt(irt_nq_all, sid, pq_dir, "NQ")
                es_b, _ = _session_irt(irt_es_all, sid, pq_dir, "ES")
                session_bars[sid] = (nq_b, es_b)
            else:
                nq_b, es_b = bars_pair
            ev = pa.table({
                "event_ts_ns": np.asarray([stamp], dtype=np.int64),
                "side": np.asarray([int(sample.column("side")[i].as_py())], dtype=np.int8),
            })
            full = annotate_hvn_magnet(ev, pair.nq, pair.es, nq_b, es_b, cfg=hv_cfg)

            def _cut_trades(t: pa.Table, cut: int) -> pa.Table:
                if t.num_rows == 0:
                    return t
                ts = t.column("ts_ns").to_numpy().astype(np.int64)
                return t.take(np.where(ts <= cut)[0])

            nq_cut = _cut_trades(pair.nq, stamp)
            es_cut = _cut_trades(pair.es, stamp)
            nq_b_cut = nq_b.slice_range(0, stamp) if len(nq_b.ts_ns) else nq_b
            es_b_cut = es_b.slice_range(0, stamp) if len(es_b.ts_ns) else es_b
            trunc = annotate_hvn_magnet(ev, nq_cut, es_cut, nq_b_cut, es_b_cut, cfg=hv_cfg)
            ok = True
            diffs = {}
            for col in (C["dist"], C["poc_dist"], primary_col):
                a, b = full.column(col)[0].as_py(), trunc.column(col)[0].as_py()
                if isinstance(a, float):
                    match = (
                        (a is None and b is None)
                        or (
                            isinstance(a, float) and isinstance(b, float)
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

    sims_p: dict = {}
    for sid, pair in pairs.items():
        if ann_all.num_rows == 0:
            continue
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        if not mask.any():
            continue
        sub = ann_all.take(np.where(mask)[0])
        sims_p[sid] = _sim(sid, pair, _filter_flag(sub, primary_col), gate=gate_off)
    n_prim_trades = sum(s.n for s in sims_p.values())
    min_sig = int(kill_cfg.get("min_primary_signals", 50))
    min_tr = int(kill_cfg.get("min_resim_trades", 20))
    power_ok = n_prim >= min_sig and n_prim_trades >= min_tr

    report["stages"]["0"] = {
        "n_mat": ann_all.num_rows, "n_primary": n_prim, "n_twin": n_twin,
        "n_primary_trades": n_prim_trades,
        "dig_n": {k: _n(C[f"toward_poc{k}"]) for k in POC_MINS},
        "truncation": trunc_rows, "truncation_ok": trunc_ok, "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — causality + power",
        "",
        f"toward_poc15: **{n_prim}** · toward_near twin: {n_twin}",
        f"dig counts: poc10={_n(C['toward_poc10'])} poc15={n_prim} poc20={_n(C['toward_poc20'])}",
        f"primary ungated trades: **{n_prim_trades}**",
        f"Truncation: **{'PASS' if trunc_ok else 'FAIL'}** · "
        f"power: **{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    if not trunc_ok or not power_ok:
        why = (
            "Stage 0 causality failed" if not trunc_ok
            else "Stage 0 underpowered (counts are the result; no retune)"
        )
        report["verdict"] = f"reject / park — {why}"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title=exp_name, md_lines=md, report=report)
        if not skip_docs:
            _write_docs(exp_name, hyp, market, report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # Stage 1
    print(f"[{exp_name}] Stage 1…", flush=True)
    arm_names = (
        "base", "toward_near", "toward_poc10", "toward_poc15",
        "toward_poc20", "away_poc15", "null",
    )
    arm_cols = {
        "base": None,
        "toward_near": twin_col,
        "toward_poc10": C["toward_poc10"],
        "toward_poc15": primary_col,
        "toward_poc20": C["toward_poc20"],
        "away_poc15": C["away_poc15"],
    }
    lab_store = {a: [] for a in arm_names}
    rng = np.random.default_rng(seed)
    for sid, pair in pairs.items():
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        if not mask.any():
            continue
        sub = ann_all.take(np.where(mask)[0])
        sets = {"base": sub}
        for name, col in arm_cols.items():
            if col is not None:
                sets[name] = _filter_flag(sub, col)
        n_null = max(sets["toward_poc15"].num_rows, 1)
        nul = H.null_events(pair, n_null, seed=seed + hash(sid) % 10_000)
        if sets["toward_poc15"].num_rows and nul.num_rows:
            sides = sets["toward_poc15"].column("side").to_numpy()
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
        "## Stage 1 — event study (primary vs twin + dig)",
        "",
        "| set | n | hit5 | hit10 | @10s | @60s | @300s | MFE | MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in arm_names:
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
    prim, twin, nl = stage1["toward_poc15"], stage1["toward_near"], stage1["null"]
    m60p, m60t = prim.get("mean_ret_pts_60000ms"), twin.get("mean_ret_pts_60000ms")
    m300p, m300t = prim.get("mean_ret_pts_300000ms"), twin.get("mean_ret_pts_300000ms")
    m60n = nl.get("mean_ret_pts_60000ms")
    s1 = {
        "lift_60": (
            m60p is not None and m60t is not None
            and np.isfinite(m60p) and np.isfinite(m60t)
            and (m60p - m60t) >= lift_60
        ),
        "lift_300": (
            m300p is not None and m300t is not None
            and np.isfinite(m300p) and np.isfinite(m300t)
            and (m300p - m300t) >= lift_300
        ),
        "beat_null_60": (
            m60p is not None and m60n is not None
            and np.isfinite(m60p) and np.isfinite(m60n)
            and m60p > m60n
        ),
    }
    s1_pass = all(s1.values())
    report["stages"]["1"] = {"summaries": stage1, "checks": s1, "pass": s1_pass}
    md += [
        "",
        f"_Dig note: neighbours (poc10/poc20) are descriptive; cannot promote if primary fails._",
        f"Checks: {s1}",
        f"**Stage 1: {'PASS' if s1_pass else 'FAIL'}**",
        "",
    ]
    if not s1_pass:
        report["verdict"] = (
            "reject / park — Stage 1 event-study fail (no Stage 2 P&L, no retune; "
            "neighbours not promoted)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title=exp_name, md_lines=md, report=report)
        if not skip_docs:
            _write_docs(exp_name, hyp, market, report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # Stage 2
    print(f"[{exp_name}] Stage 2…", flush=True)
    sims = {a: {} for a in arm_names if a != "null"}
    sims["null"] = {}
    for sid, pair in pairs.items():
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        sub = ann_all.take(np.where(mask)[0]) if mask.any() else pa.table({})
        sets = {"base": sub}
        for name, col in arm_cols.items():
            if col is not None:
                sets[name] = _filter_flag(sub, col)
        for a, ev in sets.items():
            sims[a][sid] = _sim(sid, pair, ev, gate=gate_off)
        nul = H.null_events(pair, max(sub.num_rows, 1), seed=seed + 99 + hash(sid) % 10_000)
        sims["null"][sid] = _sim(sid, pair, nul, gate=gate_off)

    books = {a: _pack(sims[a]) for a in sims}
    pool = _oos_pnls(sims["base"])
    p90 = _random_keep_p90(pool, books["toward_poc15"]["oos"]["n"], seed=seed + 1)
    min_oos = int(kill_cfg.get("min_oos_trades_promote", 50))
    min_lift = float(kill_cfg.get("min_avg_lift_pts", 1.0))
    kill = _book_kill(
        books["toward_poc15"], books["base"], books["toward_near"],
        placebo_p90=p90, min_oos_n=min_oos, min_lift=min_lift,
    )
    md += [
        "## Stage 2 — P&L (ungated host)",
        "",
        "| book | n | WR | net | avg | IS net | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for a in arm_names:
        v = books[a]
        aa, oo = v["all"], v["oos"]
        md.append(
            f"| {a} | {aa['n']} | {aa.get('wr')} | {aa['net']:+.1f} | {aa.get('avg')} | "
            f"{v['is']['net']:+.1f} | {oo['net']:+.1f} | {oo.get('avg')} |"
        )
    md += ["", f"Kill: {kill}", f"**Stage 2: {'PASS' if kill['pass'] else 'FAIL'}**", ""]
    report["stages"]["2"] = {"books": books, "kill": kill, "pass": kill["pass"]}
    report["verdict"] = (
        "PASS research — candidate HVN-magnet+POC keep-gate; virgin months before ship"
        if kill["pass"]
        else "reject / park — Stage 2 P&L fail (no retune)"
    )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    write_exp_report(out, title=exp_name, md_lines=md, report=report)
    if not skip_docs:
        _write_docs(exp_name, hyp, market, report["verdict"])
    print(report["verdict"], flush=True)
    return report


def _write_docs(exp_name: str, hyp: str, market: str, verdict: str) -> None:
    hyp_path = Path("docs/HYPOTHESES.md")
    text = hyp_path.read_text(encoding="utf-8")
    if hyp == "H87":
        needle = "## H87 — Raw MAT HVN magnet + dVPOC clearance dig (NQ)\n"
        end_marker = "## H88"
    else:
        needle = "## H88 — Raw MAT HVN magnet + dVPOC clearance dig (ES)\n"
        end_marker = "\n---\n"
    pending = "**Result:** pending — registered 2026-11-01."
    idx = text.find(needle)
    if idx >= 0:
        ridx = text.find(pending, idx)
        end = text.find(end_marker, idx)
        if ridx >= 0 and (end < 0 or ridx < end):
            text = (
                text[:ridx]
                + f"**Result ({exp_name}): {verdict}.** See `results/{exp_name}/`."
                + text[ridx + len(pending) :]
            )
            hyp_path.write_text(text, encoding="utf-8")

    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    if market == "nq":
        old = (
            "| EXP-0096 | H87 HVN magnet + dVPOC clearance dig (NQ) | registered — pending run | "
            "clean sessions + IRT | primary toward_poc15; twin toward_near; dig 10/15/20; see notes |"
        )
        new = (
            f"| EXP-0096 | H87 HVN magnet + dVPOC clearance dig (NQ) | "
            f"ran — **{verdict.split('—')[0].strip()}** | "
            f"clean sessions + IRT | {verdict}; see notes |"
        )
        note_h = "### 2026-11-01 — EXP-0096 HVN magnet + dVPOC dig NQ (H87)"
    else:
        old = (
            "| EXP-0097 | H88 HVN magnet + dVPOC clearance dig (ES) | registered — pending run | "
            "clean sessions + IRT | primary toward_poc15; twin toward_near; dig 10/15/20; see notes |"
        )
        new = (
            f"| EXP-0097 | H88 HVN magnet + dVPOC clearance dig (ES) | "
            f"ran — **{verdict.split('—')[0].strip()}** | "
            f"clean sessions + IRT | {verdict}; see notes |"
        )
        note_h = "### 2026-11-01 — EXP-0097 HVN magnet + dVPOC dig ES (H88)"
    if old in lt:
        lt = lt.replace(old, new, 1)
    note = (
        f"\n{note_h}\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/{exp_name}/`.\n"
        f"- Runner: `python -m research.mat_context.exp_0096_mat_hvn_magnet "
        f"--market {market}`.\n"
    )
    if note_h not in lt:
        anchor = "### 2026-11-01 — EXP-0096/0097 HVN magnet + dVPOC clearance dig registered (H87–H88)"
        idx = lt.find(anchor)
        nxt = lt.find("\n### ", idx + 5) if idx >= 0 else -1
        if nxt > 0:
            lt = lt[:nxt] + note + lt[nxt:]
        else:
            lt = lt + note
    log.write_text(lt, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["nq", "es"], required=True)
    ap.add_argument("--max-sessions", type=int, default=None)
    ap.add_argument("--skip-docs", action="store_true")
    args = ap.parse_args()
    run(market=args.market, max_sessions=args.max_sessions, skip_docs=args.skip_docs)


if __name__ == "__main__":
    main()
