# Plan — MAT after an inefficient rotation (delta efficiency / failed effort)

> Registered as **H76 / EXP-0085** on 2026-10-30. Frozen before any
> rotation-delta feature is computed on MAT events. Machine-readable twin:
> `research/experiments/EXP-0085.yaml`. This plan does not run the test.

## 1. Question

MAT (NQ 100 ms burst + causal ES confirm, trend+VWAP, vol k=1) is a
continuation trigger. The user claim is that it should work **better as a
reversal trigger after aggressive effort has failed**:

> A 30 pt down rotation in NQ prints delta −1000, where a typical 30 pt
> down rotation prints ≈ −400. Sellers spent ~2.5× the normal effort for
> the same distance — the effort was not rewarded. When price then starts
> to undo that rotation and a MAT burst fires **up**, go long.

Two distinct questions, kept separate because they have different ship
implications:

1. **Mechanism:** do counter-rotation MAT bursts after *inefficient*
   rotations outperform counter-rotation MAT bursts after *efficient*
   rotations (event study + ungated P&L)?
2. **Tradable on the shipped indicator:** does restricting the *frozen*
   MAT book to that scenario improve it? (The frozen trend+VWAP gate will
   delete many counter-rotation trades by construction — that is a
   finding, not a bug to be quietly relaxed mid-experiment.)

## 2. Why this is not a closed experiment

- **H1 / H54 (EXP-0002, EXP-0063)** measured effort/result on 100 ms–10 s
  bars, not cumulative delta over a completed swing rotation.
- **H24 (EXP-0030)** and **H59 (EXP-0068)** used rotation *length /
  extension* only (`rot_ratio`), never the delta spent producing the
  rotation. `rot_fresh_0.90` staying invested is compatible with this
  claim: it governs MAT in the direction of the current leg, not a MAT
  burst against a just-completed, effort-heavy leg.
- **H39 fail-fade (EXP-0048)** faded MAT itself after a failed push. Here
  MAT is the *entry trigger for the undo*, in the opposite direction to
  the failed rotation — MAT is traded with its burst, not faded.
- **H28/H64 absorption** is bar-level absorption as entry/exit, not a
  rotation-scale effort baseline.

## 3. Frozen definitions

### 3.1 Rotation (reuse, do not reinvent)

Session-scale swings exactly as EXP-0068 / `session_rotation.py`:
`SwingConfig(lookback_ms=900_000, confirm_ms=60_000)`, alternating pivot
chain, completed rotations with length ≥ 15 pts, UK 14:30–17:00 trades.

A completed rotation \(R\) has direction \(d\in\{+1,-1\}\), start pivot
\((t_s,p_s)\), end pivot \((t_e,p_e)\), length \(L=|p_e-p_s|\) pts, and
is only **usable at/after** `end_confirm_ts_ns` (60 s after the end pivot
print). \(L\), \(t_s\), \(t_e\) describe the past; nothing dated after
the end pivot enters \(R\)'s delta, and nothing is usable before
confirmation.

### 3.2 Rotation delta and inefficiency

Over NQ trades with \(t_s \le t \le t_e\) (aggressor flag
`is_buy_aggressor`, size-weighted):

\[
\Delta_R=\sum \pm \text{size},\qquad
A_R = d\cdot\Delta_R,\qquad
E_R = A_R / L_R \;\;(\text{aligned contracts per point})
\]

\(A_R\) is effort *in the direction of the move*. The user's example:
\(L=30\), \(\Delta=-1000\) on a down rotation \(\Rightarrow E=33.3\)
contracts/pt vs a typical \(400/30=13.3\) \(\Rightarrow\) ratio 2.5.

Baseline \(B_R\): median \(E\) of the **previous same-direction completed
rotations in the same session** (last 4, need ≥ 2). If fewer than 2 exist
this session, fall back to the immediately preceding clean session's
same-direction median (mirrors `session_rotation` prior fallback).
Undefined baseline ⇒ `warmup`, never gated.

\[
\text{IR}_R = E_R / B_R \qquad(\text{inefficiency ratio})
\]

Frozen classes (primary cut fixed now; neighbours are report-only):

| class | rule |
|---|---|
| `inefficient` | IR ≥ 1.50 |
| `efficient` | IR ≤ 1.00 |
| `neutral` | 1.00 < IR < 1.50 |
| `divergent` | \(A_R \le 0\) (price rotated against net aggression — different mechanism, descriptive only, never the gate) |
| `warmup` | no baseline |

Also record for display: pts per 100 aligned contracts \(=100/E_R\),
total volume/pt, duration, speed (pts/min), \(L\). IR alone is the gate;
the rest are confound strata (§6).

### 3.3 State at a MAT event

At MAT stamp \(t\) (causal ES stamp, bar-end), take the most recent
completed rotation usable at \(t\) (largest `end_confirm ≤ t`, no later
completed rotation usable at \(t\)) and attach:

- `rd_relation`: `counter` if MAT side \(s=-d\), `with` if \(s=d\);
- `rd_retrace_frac`: fraction of \(R\) already undone at entry —
  down rotation ending at low \(p_e\): \((p_t-p_e)/L\); mirrored for up;
- `rd_age_confirm_min = (t - end_confirm)/60s`;
- rotation id `(session, end_confirm_ts)` for clustering (§6).

**Primary eligibility** (frozen): `counter`, class `inefficient`,
`age_confirm ≤ 30 min`, `retrace_frac ≤ 0.50`. Everything else is a
stratum, not a rescue gate. Report-only neighbours: IR ∈ {1.25, 2.00},
retrace ≤ 0.25, age ≤ 15 min.

## 4. Books and controls (identical brackets/costs within each book)

Host definitions from `research/correlated/cfade_family.py`:
`mat_sim_cfg()` (causal ES, 5 s cooldown, commission 0.25 pt RT),
`make_vol_bracket(1.0, floor=6, cap=30)`, UK 14:30–17:00, flat-only.
Entry-slip sensitivity at 1 and 2 ticks (primary re-sim at the frozen
comparator's setting so the base book reproduces).

| book | events | gate | purpose |
|---|---|---|---|
| **A_all** | `detect_signals` | frozen trend+VWAP | shipped-MAT comparator |
| **A_ctr_ineff** ★ | A signals, primary-eligible | frozen trend+VWAP | Q2 primary |
| **A_ctr_eff** | A signals, counter+efficient | frozen trend+VWAP | internal twin |
| **B_all** | `detect_signals` | none | signal-level comparator |
| **B_ctr_ineff** ★ | B signals, primary-eligible | none | Q1 primary |
| **B_ctr_eff** | B signals, counter+efficient | none | Q1 twin |
| **B_with_ineff** | B signals, with+inefficient | none | direction control |
| **C_confirm_only** | entry at `end_confirm` of inefficient rotations, side \(-d\) | none | does MAT timing add anything over "fade at confirmation"? |
| **null** | time-matched random, same side counts (`harness.null_events`) | frozen | MAT-context mandatory null |

P&L is by **re-simulation from kept events** through
`mat_sim.simulate_session` (flat-only path dependence preserved), not by
post-filtering executed base trades; the post-filter view is reported as
descriptive only. Also report the **first counter MAT per rotation**
sensitivity book (one trade opportunity per failed rotation).

## 5. Stages and kill bars (no stage may be skipped or re-cut)

**Stage 0 — causality + power.**
Truncation invariance: every `rd_*` value at a MAT stamp is bit-identical
when the session's trades are cut at that stamp (and pivots/baselines use
only rotations confirmed by then). Hand-check 3 rotations against a
manual aggressor-delta sum. Counts first: park with counts as the result
if ungated counter-inefficient signals < 100, counter-efficient twin
< 100, **or** frozen-book `A_ctr_ineff` re-sim trades < 20 — in the last
case the frozen-MAT question is answered ("the gate deletes the setup")
and trend/VWAP may **not** be relaxed inside EXP-0085.

**Stage 1 — event study before P&L** (`outcomes/label.py`, points).
All MAT signals labelled at first trade ≥ stamp: ret @10/60/300 s,
hit-10-before−10 race, MFE/MAE to 300 s, split by
class × relation, IR dose bins
\((\le0,\;0\!-\!0.83,\;0.83\!-\!1.25,\;1.25\!-\!1.75,\;>1.75)\),
side, retrace and length terciles.
Proceed only if counter-inefficient beats counter-efficient by
≥ +1.0 pt mean @60 s **and** ≥ +2.0 pt @300 s, and beats null @60 s.
A non-monotone dose response (top IR bin ≤ bottom) fails the stage
regardless of the threshold cells.

**Stage 2 — P&L.** Verdicts are issued separately for Q1 and Q2.
A book **passes** (research PASS, MAT_CONTEXT_PROTOCOL promote rule) iff
on the non-DIG4 remainder ("OOS" below): net > 0, avg ≥ corresponding
`*_all` OOS avg + 1.0 pt, avg > its `*_ctr_eff` twin, avg > random-keep
placebo p90 (1,000 same-n draws), n_OOS ≥ 50, plus reported (not tuned):
days+, maxDD, top-3 day share, leave-one-session-out, 2-tick slip,
long/short split, month blocks.

**Stage 3 — interpretation, frozen in advance.**
- Q2 pass ⇒ candidate keep-condition for MAT; ship only after virgin
  future months (Sep–Oct is already MAT-mined; see protocol).
- Q2 fail/empty but Q1 pass ⇒ the setup wants a **counter-MAT variant
  without trend/VWAP**; that is a new preregistration (EXP-0086) on
  virgin months, not a live-gate edit and not an EXP-0085 retune.
- Both fail ⇒ reject / park H76. No IR-threshold, swing-parameter, or
  retrace-window search on this sample.

## 6. Confounds that must be shown, not assumed away

1. **Length/speed artefact:** short or slow rotations mechanically have
   high contracts/pt. Show the Stage-1 lift inside length terciles and
   inside speed terciles; if the lift lives in only one cell, say so.
2. **Vol regime:** IR vs `med_r30` / rotation total volume — baseline is
   same-session same-direction, but show the correlation anyway.
3. **Clustering:** one rotation can label many MAT bursts. Report
   n rotations / n sessions behind each cell and the
   first-per-rotation book.
4. **Side asymmetry:** long-after-down vs short-after-up reported
   separately throughout; a one-sided result is a stratum finding, not
   the claim.
5. **Gate interaction:** how many primary-eligible signals the frozen
   trend+VWAP gate removes, and why (price vs VWAP, 30 m trend sign at
   stamp) — this is the expected failure mode of Q2.
6. **ES delta (secondary only):** ES aligned delta over the same
   \([t_s,t_e]\) window, ES pts/pt. Descriptive split inside
   counter-inefficient; it cannot promote a failed NQ primary
   (H28/H54 lesson).

## 7. Implementation steps (when the run is authorised)

1. `research/features/rotation_delta.py` — extend the
   `session_rotation` pivot chain with end-pivot ts/prices (or recompute
   the identical chain), prefix-sum signed size for O(1) rotation delta,
   causal same-direction baseline + prior-session fallback,
   `annotate_rotation_delta(events, trades, prior=...)` emitting the
   `rd_*` columns of §3.3. No changes to existing `rot_*` columns.
2. `research/features/test_rotation_delta.py` — synthetic tape:
   30 pt down rotation at −1000 vs prior −400 ⇒ IR = 2.5 / `inefficient`;
   mirrored up case; \(A\le0\) ⇒ `divergent`; truncation invariance;
   nothing usable before `end_confirm`.
3. `research/mat_context/exp_0085_rotation_delta.py` — standalone runner
   over `cfade_family.clean_sessions` (excl. 200626/220626/300326),
   reusing `harness.load_pair`, `detect_signals`, `simulate_session`,
   `label_in_points`; writes `results/EXP-0085/{summary.md,report.json,
   events.parquet}`. Standalone first; only integrate into
   `mat_context/cache.py` if a stage passes.
4. Run order:
   `python -m research.features.test_rotation_delta` →
   `python -m research.mat_context.exp_0085_rotation_delta` (Stages 0–2
   in one pass, stage gates enforced in-code) → append verdict to
   `docs/HYPOTHESES.md` (H76) and `docs/EXPERIMENT_LOG.md` (EXP-0085).
5. No Java. A Bookmap-side rotation-delta tracker (the live analogue
   already has anchors in `MatPriorRotAnchor` / `EffortResultModule`)
   is justified only after a Stage-2 pass **and** virgin-month
   validation.

## 8. Worked expectation (what would change our mind)

If the claim is real, the signature is specific: counter-inefficient
MAT should show a *dose response* in IR (more wasted effort ⇒ better
undo trades), beat its efficient twin on both @60 s/@300 s paths and
re-sim P&L, hold for both sides, and survive first-per-rotation
de-clustering — while with-rotation inefficient MAT does not. Anything
weaker (one side, one month block, only post-filtered trades, only at
a neighbour threshold) is logged as a fail, per the frozen bars above.
