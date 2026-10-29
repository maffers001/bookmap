# Potentially profitable strategies — retest tracker

> Living list of books that still look worth trading or re-validating on
> **fresh / virgin months**. Not a preregistration and not a replacement for
> `EDGE_RANKING.md` or `HYPOTHESES.md` — those keep the full audit trail.
>
> Snapshot started **2026-10-29**. Update the retest log when new data lands;
> do not retune frozen specs on already-read samples.

**How to use:** when a new month of Bookmap/IRT data is converted, re-run each
Tier 1 (and any Tier 2 you still care about) with **frozen** rules. Record
pass / fail / caveats in the Retest log. A strategy drops from Tier 1 only if
fresh months reverse the edge under the same construction.

---

## Tier 1 — primary watchlist

These are the strategies to keep in view and re-test first.

| # | Strategy | Instrument | Status | Frozen pointer | Last strong evidence |
|---|---|---|---|---|---|
| 1 | **ES LVN outer** | ES | Research P&L book; equity tracked | Open-cluster ∩ LVN≤10, **outer** wall, SL**8**/TP**32**, minOpen≥**3**, entry≤17:00 → EOS 21:00 | Apr–Oct: n=109 net **+353.6** pts, 6/7 months+, maxDD 69 (`results/ES_LVN_OUTER_824_*`) |
| 2 | **CFADE** | NQ | Frozen TEST_AUTOTRADE | `against_m04` + ER60 ≤ 0.30, vol RR SL1R/TP2R | DIG4 23d +355; **May OOS +335** (`EXP-0036_DIG4`) |
| 3 | **LST Momentum** | NQ (+ES confirm) | Indicator shipped; SIM / V2 candidates | `MOMENTUM_V1` in `results/MOMENTUM_SLICE/RULE_SPEC_V1.md` | Holdout Gate1 PASS; mat_sim holdout baseline +114; **LONG-only** variants stronger (`entry_sweep/SUMMARY.md`) |
| 4 | **LST Pitch and Catch** | NQ (+ES confirm) | Indicator shipped; **autotrade not ready** | `PC_V1` same RULE_SPEC | Event-study holdout Gate2 PASS; **mat_sim PC_V1 holdout −60** — keep watching / redefine V2, do not size |

### 1. ES LVN outer

- **Idea:** Fade first touch of RTH open resting-cluster ∩ IRT LVN, using the
  **outer** wall (buy min(cluster,LVN) / sell max). Distinct from closed dual
  Bookmap-LVN event studies (H7/H7b).
- **Core run:** `results/ES_LVN_OUTER_824_20261025/` + equity
  `results/ES_LVN_OUTER_824_EQUITY/`.
- **Joint context:** with CFADE + gated MAT, ES LVN alone ≈ **+$17.7k** USD on
  the joint sample (`results/JOINT_CFADE_MAT075_ESLVN/`).
- **Retest ask:** same SL8/TP32 / outer / minOpen≥3 on any virgin months; do
  not re-pick brackets from the known Apr–Oct set.
- **Cousin (not Tier 1):** **NQ LVN outer** — SL48/TP96 flat-only n=24
  **+372** but thin and mid-year soft (`NQ_LVN_OUTER_48_96_EQUITY`); wide
  SL50/TP300 **−1992**. Treat NQ as exploratory until a frozen bracket holds
  on fresh data.

### 2. CFADE (correlated exhaustion fade)

- **Idea:** NQ still bursting with the move; ES imbalance flips against within
  ~5 s → fade NQ on rotational days (ER≤0.30).
- **Spec:** `results/EXP-0036_DIG4/TEST_AUTOTRADE_SPEC.md`.
- **Portfolio:** ship allocator **(c)** = ER60 switch + MAT `skip_range175`
  (EXP-0057 / H48) — retest the **pair**, not only CFADE alone.
- **Caveats:** fill quality (train dies at 2-tick slip); left-tail days; shorts
  carried some train months.
- **Retest ask:** frozen rule on virgin months; side-split and slip 1 vs 2.

### 3. LST Momentum (`MOMENTUM_V1` / V2 candidates)

- **Idea:** Cross-confirmed tape burst that **slices** dominant resting
  liquidity (left behind), with pre-ignition trend ≥ 0 and elevated
  `dom_vs_med`.
- **Home:** `results/MOMENTUM_SLICE/` (`STATUS.md`, `RULE_SPEC_V1.md`, Java
  `LstMomentumModule`).
- **Evidence shape:** tagged TRUE vs FALSE separates cleanly; holdout
  continuation vs controls PASS; bracket backtest positive; **shorts drag** —
  LONG-only / confirm+5s left-behind are the leading V2 candidates after a
  holdout peek (validate only on SIM / new sessions).
- **Retest ask:** frozen V1 fires + preferred LONG-only flavour on new data;
  parity CSV vs Bookmap replay.

### 4. LST Pitch and Catch (`PC_V1`)

- **Idea:** Same burst family, but stall / absorption at confirm+5s → **fade**
  (opposite of momentum). Prefix `LST-PC` — distinct from legacy Bookmap
  “Pitch and Catch” (HV pitch / LV catch).
- **Home:** same MOMENTUM_SLICE tree; Java `LstPitchCatchModule`.
- **Honest status:** event-study fade@60s holdout PASS and is the reason it
  sits in Tier 1 with Momentum; **P&L simulation of PC_V1 fails OOS**
  (baseline −60). Entry-sweep found no robust autotrade winner yet.
- **Retest ask:** keep logging fires on new sessions; any V2 must be frozen
  *before* reading new-month P&L. Do not promote to size from train/holdout
  peeks alone.

---

## Tier 2 — also potentially profitable (do not forget)

Still positive (or soft-confirm) under research rules; lower priority than
Tier 1 for discretionary focus, but **include in batch retests** when new
months arrive.

| Strategy | Why it’s here | Pointer | Retest note |
|---|---|---|---|
| **MAT base** (vol k=1, trend+VWAP) | Real trend-regime edge; May rotational OOS lost | EXP-0008 / MAT May summaries | Always with allocator or a keep-gate — not naked |
| **MAT+CFADE allocator (c)** | Shipped research: always-on OOS −472 → (c) **+290** | EXP-0057 / H48 | Re-run on virgin months unchanged |
| **Confirm-burst continuation** | PASS n=3055 net +815 (OOS +577); thin per-trade | EXP-0061 / H52 | Java shortlist candidate beside CFADE |
| **MAT `rot_fresh_0.90`** | PASS OOS +212; Sep–Oct already mined for MAT | EXP-0068 / H59 | Needs **future** months before ship |
| **MAT near-VWAP / ATR-exp / combo** | PASS research-only; combo OOS avg +2.76 | EXP-0075 / H66 | No stacking on current OOS; virgin months only |
| **StackConsume-intact fade** | Soft May confirm (+160); only level-OF survivor | EXP-0043_STACK_FADE / H34 | Forward-paper / shadow; AMB/BREAK false fades uncured |

---

## Explicitly not on this list

Closed or ≤ placebo under current constructions — see `EDGE_RANKING.md`
Tier 4 and `HYPOTHESES.md`. Do not quietly revive without a **new**
construction + preregistration. Notable recent closes: DOP thread
(H69–H72 / EXP-0078–0081), dual LVN event studies (H7/H7b), most MAT overlays,
Tier A correlated gates (A1–A4).

---

## Retest checklist (per new data batch)

For each Tier 1 (and optional Tier 2):

1. Confirm sessions are **virgin** relative to that strategy’s last freeze.
2. Run frozen detector / brackets only (no threshold search).
3. Record: n, net, avg, maxDD, days+, side split, and whether sign matches prior.
4. Append a row under **Retest log** below.
5. If fail: park or demote; do not refit on the same batch.

Suggested commands / artefacts (update paths as runners evolve):

| Strategy | Starting point |
|---|---|
| ES LVN outer | `python -m research.irt_lvn.study_es_lvn_bracket_8_24` (outer / SL8 TP32 / min-open 3) → compare to `ES_LVN_OUTER_824_*` |
| CFADE | DIG4 / test-autotrade harness; allocator via EXP-0057 runner |
| LST Mom / PC | `research.momentum_slice.holdout` pattern on new sids; mat_sim with frozen brackets |
| MAT / 0061 / gates | `research.mat_context` / EXP YAML runners as logged in `EXPERIMENT_LOG.md` |

---

## Retest log

| Date | Data (months / n sessions) | Strategy | Result | Notes / artefact |
|---|---|---|---|---|
| *(seed)* 2026-10-29 | See rows above | Tier 1–2 baselines | — | Doc created; baselines are historical, not a new virgin read |

---

## Related docs

- `docs/EDGE_RANKING.md` — older ranked dig queue (CFADE #1, MAT #2 as of 2026-10-21)
- `docs/HYPOTHESES.md` / `docs/EXPERIMENT_LOG.md` — claim ↔ experiment ↔ verdict
- `docs/TIER_A.md` — closed correlated gates on frozen MAT/CFADE (not entries)
- `docs/MyIdeas.txt` — discretionary backlog (LVN automation, DOP closed, etc.)
- `results/MOMENTUM_SLICE/STATUS.md` — LST Mom / PC live status
- `results/JOINT_CFADE_MAT075_ESLVN/` — three-book USD joint sketch
