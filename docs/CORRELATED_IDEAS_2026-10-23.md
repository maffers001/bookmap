# Correlated ES/NQ orderflow ideas — candidate list (2026-10-23)

> Companion to `RESEARCH_REVIEW_2026-10-23.md` §6.5 and `EDGE_RANKING.md` §D.
> Purpose: enumerate the cross-market constructions we have **not** tested, so
> the next correlated experiment is chosen from a full menu rather than the
> first idea to hand. Each idea has a construction, the null it must beat, the
> data it needs and a kill bar. Nothing here is a result.
>
> **Update 2026-10-24:** §2.1 (EXP-0049), §3.2 (EXP-0050), §2.7
> (EXP-0051, Stage 0), §2.3 (EXP-0052, Stage 1), §4.3 (EXP-0053,
> Stage 1), and §1.3 Claim A (EXP-0054, matters bar) all rejected —
> see result boxes. **Tier A (A1–A4) is complete: all four closed
> reject/park, none read May, nothing ships.** §5 ranking superseded
> by §6; §7 records the EXP-0050 outcome; next dig is Tier B B1 (§1.3
> Claim B execution timer, microprice-only fallback after EXP-0054)
> per §8 / `TIER_A.md`. **§8 is the complete
> audited map of everything else still open, conditional, or closed after
> EXP-0050 — no remaining area should live only in chat.** Goal throughout:
> a Bookmap automated strategy that works.

## 0. What the correlated family has already told us

| Done | Construction | Verdict | Lesson |
|---|---|---|---|
| H4 / MAT (EXP-0008) | NQ tape burst + ES same-side burst within a sync window | **Works** (regime-dependent; May −264) | Simultaneous *aggressor* agreement over ~100 ms–1 s is informative for continuation. |
| H30 / CFADE (EXP-0036) | NQ burst, then one ES bar against while NQ still pushing | **Works** (May +335) | Short-horizon *disagreement* after agreement is a fade. Thin ES on the burst is the best stratum. |
| H18 (EXP-0024) | Signed ES→NQ lag < 250 ms as a filter on MAT | Null in points | Who-prints-first at the 100 ms scale carries nothing once units are points. |
| H22 (EXP-0028) | ES impulse while NQ quiet → NQ catch-up | Null | Price lead/lag at tick scale does not survive. |
| H23 (EXP-0029) | Same-window co-move of ES and NQ *price* | Falsified | Correlated **price** is not the edge; correlated **tape** is. |
| H28 (EXP-0035) | Correlated absorption on 100/250 ms bars at NQ large levels | Reject | Single-bar absorption with ES confirming ≈ NQ-only ≈ null. |
| H31 (EXP-0038 exits) | ES going quiet/against → early MAT exit | Reject | Disagreement is an *entry* signal (CFADE), not an exit signal for a continuation book. |
| H35 Arm B (EXP-0044) | NQ-or-ES large take toward hold side as dwell trigger | Thin / not selected | ES large prints alone, at IRT levels, too rare to read. |
| H37 (EXP-0046) | ES open-book standout levels, traded on ES | Reject | ES resting-order *structure* is as dead as NQ's. |
| H40 (EXP-0049) | Cumulative divergence W=10 s, persist 5 s, fade (§2.1) | **Reject (Stage A)** | Sustained cross-market divergence is transient: persist 0→5 s collapses n 508→10. A shifted-ES placebo fires 10× more than the real ES — coupling itself kills persistence. Nothing survives beyond ~2 s tape horizons. |
| H41 (EXP-0050) | Correlation-regime gate ρ&lt;0.3 skip on CFADE (§3.2) | **Reject (Stage 1)** | At CFADE triggers, ES/NQ are almost always already coupled (ρ&lt;0.3 n=0/132). Skip gate has no mass; apparent lift was warmup-drop and failed placebo. |
| H42 (EXP-0051) | Volume-ratio shock keep-gate on CFADE (§2.7) | **Reject (Stage 0)** | Shock stratum (trailing-z ≥ 1) has only 14/132 candidates < 20 power floor. Third cross-market state in a row that is rare exactly when CFADE fires. |
| H43 (EXP-0052) | Aggressor count/size FLB gate on MAT (§2.3) | **Reject (Stage 1)** | FLB-gated MAT avg +0.59 < comparator +1.55 and < placebo p90; MSQ cell empty at CFADE triggers. Trigger books already live in many-large tape. |
| H44 (EXP-0053) | Large-print clustering both-burst gate on MAT (§4.3) | **Reject (Stage 1)** | Power fine (both 741 signals) but gated avg +1.13 < +1.55, hold/maxDD/placebo fail; `both` ≈ book avg in joined strata. Tape-relative states re-describe the trigger. |
| H45 (EXP-0054) | BBO/top-3 imbalance agreement at frozen triggers (§1.3 Claim A) | **Reject (matters bar)** | Agreement state essentially absent: `mixed` 1,949/1,956 MAT triggers and 132/132 CFADE; `agree_with` trades 0 on both books. Tier A complete — all four strata/gates failed to separate outcomes inside the frozen books. |

Four structural facts the new ideas should respect:

1. **Everything that survived used aggressor tape on both legs, at 100 ms–2 s
   horizons, with NQ as the traded leg.** Book-shape and price-lead constructions
   have failed every time. EXP-0049 extends this: no cumulative-tape
   construction at 5–30 s has survived either.
2. **We have never used the ES book.** `depth` and `mbo` parquet carry the ES
   role on all 52 sessions; the only ES features so far are tape bars and p99
   prints. The whole "correlated book change" family in §1 is new ground.
3. **Disagreement has been tested at one bar only.** CFADE's ES-against is a
   single 100 ms bar. Multi-second cumulative disagreement (§2.1) has now
   been tested (EXP-0049) and rejected as constructed — see §2.1.
4. **Persistence of a threshold state is a cliff, not a dose** (EXP-0049).
   On overlapping rolling windows, requiring a condition at *every* bar close
   multiplies rarity (n 508 → 10 → 1 for persist 0/5/10 s). Future
   constructions should use first-crossing + hysteresis, and should report
   shifted-series placebo counts as a coupling diagnostic.

## 1. Correlated **book** change (the user's example: both books lean the same way before an imbalance)

The hypothesis class: when liquidity *moves* in both books in the same
direction at the same time, it is more likely to be informed (or a shared
algo) than when one book moves alone. Spoofing is usually single-market.

### 1.1 Co-pull — simultaneous near-touch cancellation on the same side

- **Event:** within a 500 ms–2 s window, NQ and ES both lose ≥ X % of resting
  size within k ticks of the BBO on the *same* side (bid pulls in both), with
  no executions explaining the drop (MBO cancel-resolved, not consumed).
- **Claim:** price moves toward the pulled side over 10–60 s more often than
  single-market pulls or placebo windows.
- **Null:** NQ-only pull of the same magnitude; random windows matched on time
  of day and vol.
- **Data:** NQ + ES MBO (have). Baseline per market, per minute-of-day
  (`robust session baselines` plumbing).
- **Kill:** dual-pull hit10 must exceed NQ-only pull by ≥ 5 pp on train with
  n ≥ 200, else drop.

### 1.2 Co-stack — simultaneous same-side adds that are not immediately hit

- Mirror of 1.1: both books add ≥ X % to one side within the window and the
  adds survive ≥ 2 s. Claim: *support/resistance by agreement* — price does not
  trade through that side within 30–60 s. This is the book-side analogue of the
  StackConsume-intact read, but defined by **co-ordinated arrival** rather than
  by an IRT level, so the dead level machinery is not involved.
- Null: NQ-only stack of the same size.

### 1.3 Correlated BBO-imbalance regime

- Rolling 1–5 s signed BBO/top-3 imbalance per market, normalised per market.
  States: agree-bid, agree-ask, disagree, neutral.
- Claim A (filter): MAT / CFADE outcomes stratified by state at trigger.
  Imbalance *alone* failed as a 10-pt predictor (H2); as a **two-market
  agreement state** on an existing trigger it has not been read.
- Claim B (execution): imbalance agreement predicts the next tick well enough
  to time CFADE entries (see REVIEW §6.3). This is the most likely payoff.

> **Result — EXP-0054 / H45 (2026-10-24): Claim A reject / park.**
> Frozen form: depth-replayed top-3 imbalance per market, trailing-5 s
> mean, lean |imb| ≥ 0.20, side-relative `agree_with` vs rest on the
> frozen MAT/CFADE books (stratum only). Causality PASS (truncation
> 238 compared / 0 diffs; chunked vs brute-force replay exact). All 23
> sessions had depth; all 2,088 triggers defined — but the state was
> essentially absent: MAT `mixed` 1,949/1,956, CFADE `mixed` 132/132,
> and `agree_with` had **0 trades on both books** (books reproduced
> exactly: MAT +779, CFADE +663). All matters checks fail; the n=2 MAT
> `agree_against` cell is not a finding. No May, no retune. Claim B
> survives only in its microprice-only form (REVIEW §6.3 fallback):
> the A4 agreement state is not an approved timer variable. This was
> the last Tier A item — Tier A is complete, all reject/park.

### 1.4 Cross-market book-pressure divergence

- NQ book leaning bid (imbalance > +θ) while ES book leans ask (< −θ) for
  ≥ 2 s, both normalised. Claim: the NQ lean is a spoof/trap — fade it. Null:
  NQ lean with ES neutral.
- Reason to test: single-market book leans are routinely faked; a fake is
  cheaper to run in one book than two.

### 1.5 Correlated iceberg / reload

- Iceberg detector per market from MBO reloads (REVIEW §6.7). Claim: an active
  iceberg on the same side in *both* markets at the same price-equivalent
  (basis-adjusted, §3.3) is a much stronger hold than one. Rare; count first.

### 1.6 Correlated large-order lifecycle

- Extend `LargeLevelTracker` to ES. For each NQ large resting order: does an ES
  large order appear/pull/trade within ±N s and on the same side? Build a
  "paired large order" event. Claim: paired orders behave (hold / get hit) more
  predictably than unpaired. Null: unpaired NQ large orders of the same size
  percentile.

## 2. Correlated **tape** (extensions of the two things that work)

### 2.1 Cumulative divergence over 5–30 s (dose-response CFADE)

- Rolling signed aggressor imbalance per market, z-scored per market per
  minute-of-day. Divergence D(t) = z_NQ − z_ES.
- Claim: P(fade works) increases monotonically with |D| held over ≥ T s while
  NQ price keeps making progress. CFADE is the T ≈ 0.1 s, one-bar corner.
- Null: same |D| but NQ price *not* progressing (no trap to fade).
- Kill: monotone in |D| and T on train, and ≥ 50 % of CFADE avg before any
  OOS read.

> **Result — EXP-0049 / H40 (2026-10-24): reject / park as constructed.**
> Primary cell (W=10 s, nq_imb ≥ 0.30, es_imb ≤ 0, D ≥ 0.30, NQ progress
> ≥ 2 pts, persist 5 s, 30 s cooldown, dense 100 ms grid, DIG4 23d discovery):
> **n=10**, far below the ≥ 200 Stage-A kill; |D|-tercile @60s was
> +7.5 / −0.2 / +0.4 (not monotone, and meaningless at n=4/3/3). Stages B–D
> were not run; May was not touched. Causality (truncation invariance)
> passed; alignment spot-checks were clean. Full report:
> `results/EXP-0049/summary.md`.
>
> What the run did establish:
> - **Counts:** persist 0/5/10 s → n=508/10/1; W=5/30 s at persist 5 s →
>   n=2/5; NQ-only twin n=798; CFADE on the same sample n=132. Sustained
>   divergence is a transient state, not a 5–30 s regime.
> - **Shifted-ES (+60 s) placebo fired n=104 vs the real ES n=10.**
>   Decorrelated ES "diverges" for longer precisely because it is not
>   coupled; contemporaneous coupling is what destroys persistence. This
>   motivates §3.2 (coupling strength as a gate) and §4.2 (which market
>   resolves a divergence).
> - **No-progress null was nearly empty (n=3):** with the activity gate on,
>   NQ imbalance ≥ 0.30 over 10 s almost always comes with ≥ 2 pts of
>   progress — imbalance and absolute progress are mechanically coupled.
>   §2.2 must normalise progress by effort, not use an absolute hurdle.
> - **Window axis was confounded:** the same fixed 2 pt hurdle at W=5/10/30 s
>   makes the three windows non-comparable as a dose axis.
>
> What was **not** tested, so the kill must not be over-claimed: the literal
> minute-of-day z-score `D(t)` (EXP-0049 used bounded rolling imbalance +
> a trailing-median activity gate); a first-crossing entry with hysteresis
> (EXP-0049 required every-bar persistence and entered at the end of it,
> i.e. late by construction); `persist0` as a standalone (discovery
> hit10 0.33 / @60s +0.82 sits at/below known large-n nulls ≈ 0.40, so
> do not revive it on this sample). **Closed:** retuning persistence/D
> thresholds on the same 23 days; promoting any neighbour cell.
> Plumbing is reusable: `research/features/corr_divergence.py` and
> `research/correlated/exp_0049_divergence.py` (dense grid, causal,
> truncation-invariant).

### 2.2 Effort/result divergence across markets

- H1 (NQ effort/result at 100 ms) died. Cross-market version: over 5–30 s, ES
  aggressor volume toward direction d is high and ES price moves; NQ aggressor
  volume toward d is also high but NQ price does **not** move (absorbed).
  Claim: NQ is being absorbed against a correlated push → fade NQ once ES
  stalls, or trade NQ catch-up if ES keeps going (test both; pre-register the
  split by ES continuation).

### 2.3 Aggressor-count vs aggressor-size agreement

- Per market, per 1 s: number of distinct aggressive orders
  (`aggressor_order_id`) and mean aggressive size. Four states: few-large,
  many-small, per market → 16 cells.
- Claim: "few-large in both" is institutional and continues; "many-small NQ,
  nothing ES" is retail/algo chop and fades. Founding idea in the handoff,
  never a feature.

> **Result — EXP-0052 / H43 (2026-10-24): reject / park.**
> Frozen form: trailing 5 s distinct-aggressor count + mean-per-aggressor
> vs trailing-30 m medians; FLB keep-gate on frozen MAT (primary),
> MSQ stratum on CFADE (secondary). Causality PASS; FLB power PASS
> (160/1,956 MAT signals). MAT gated avg **+0.59** (n=64) vs comparator
> **+1.55** (n=502, reproduced net +779); net retained 5 %; placebo
> p90 +4.92 → FAIL. MSQ was **empty** at CFADE candidates (0/132 —
> CFADE fires in many-large tape). Lesson: both proven books trigger in
> many-large aggressor states by construction, so count/size states
> defined against tape medians mostly re-describe the trigger itself.
> Plumbing reusable: `research/features/aggr_flow.py`.
> See `results/EXP-0052/`.

### 2.4 Correlated sweep (multi-level take) — EXP-0017 made dual

- Sweep detector exists for NQ. Add ES. Event: NQ sweeps ≥ L levels and ES
  sweeps ≥ L' levels same side within 1 s. Claim: dual sweeps continue; single
  sweeps revert. Null: NQ-only sweep. Cheap — plumbing exists.

### 2.5 Correlated absorption over seconds, not bars (H28 redo)

- H28 used 100/250 ms bars and was rejected with the explicit note that a
  multi-second construction was the only sensible revisit. Event: ≥ 3 s of
  NQ aggressor volume into one price band with ≤ 1 tick of progress **and** ES
  aggressor volume in the same direction also stalls. Trade the reversal.
  Null: NQ-only absorption of the same duration.

### 2.6 Flow inflection synchrony (derivative of cumulative delta)

- Smooth per-market cumulative delta (EMA 2–5 s); event when the slope changes
  sign. Claim: a sign change that happens in both markets within 1 s is a turn;
  in one only it is noise. Measure continuation of the new slope over 30–60 s.
  Null: single-market inflections.

### 2.7 Volume-ratio shock

- Rolling 10 s ratio of NQ aggressor volume to ES aggressor volume, z-scored
  by minute-of-day. Spikes = one market is being traded idiosyncratically.
  Claim: an NQ volume spike with a flat ratio (ES also spiking) is a market-wide
  event and continues; a spike with ratio ≫ 1 is NQ-specific and reverts.
  Relates to CFADE's "low burst ES vol" stratum (+427 vs −78), which is the
  first hint this ratio matters.

> **Result — EXP-0051 / H42 (2026-10-24): reject / park at Stage 0.**
> Frozen form: trailing 10 s ratio, session-causal trailing-30 m robust z
> of log-ratio (deviation from literal minute-of-day z, frozen a priori),
> keep CFADE only when z ≥ 1.0. Causality (truncation + raw-trade
> hand-check) **PASS**. Coverage on 132 candidates: warmup 17 / low 82 /
> mid 19 / **shock 14 < 20 power floor** — so no P&L was run, May was not
> read, and the z cut must not be loosened on this sample. Candidate ratio
> median 0.37 / p90 0.76. The DIG3 low-burst-ES-vol hint does not survive
> translation into a 10 s window ratio state at the trigger: idiosyncratic
> NQ volume over 10 s is rare at CFADE times, the same rarity pattern as
> EXP-0049 (persist) and EXP-0050 (ρ<0.3). Plumbing reusable:
> `research/features/vol_ratio.py`. See `results/EXP-0051/`.

## 3. Cross-market **price/basis** microstructure (new axis)

### 3.1 Synthetic basis (NQ − β·ES) dislocation

- β from rolling 30–60 min regression of 1 s returns. Residual
  r(t) = ΔNQ − β·ΔES over 1–5 s, z-scored.
- Claim: a large residual that is **not** accompanied by NQ aggressor flow in
  that direction mean-reverts (price moved without tape → liquidity pull, not
  buying). A large residual **with** flow continues (idiosyncratic information).
  This separates "NQ moved because someone bought" from "NQ moved because the
  book thinned", using ES as the control. Null: unconditional residual
  reversion.
- This is the only idea on the list that would also give a *continuous* signal
  for the live add-on rather than discrete events.

### 3.2 Correlation-regime gate

- Rolling 60 s / 5 min correlation of 1 s returns. CFADE and MAT both assume
  the markets are coupled. Claim: both books are better when ρ > 0.6 and
  should skip when ρ < 0.3 (sector-rotation days). Pre-register the thresholds
  on Oct/Sep; read May once. Trivial cost; applies to every cross-market idea.

> **Result — EXP-0050 / H41 (2026-10-24): reject / park.**
> Causal ρ (1 s returns, trailing 300 s, min_obs 120) on DIG4 23d CFADE
> candidates: n=132 with **ρ&lt;0.3 = 0**, mid 18, hi 103, warmup 11.
> Comparator-trade skip stratum n=0 ≪ 20 kill. Apparent gated lift
> (avg +8.70 vs ER60 +7.63) came from dropping warmup fades, not from
> skipping decoupled periods, and failed the random-skip placebo p90
> (+8.87). Causality (truncation + corrcoef hand-check) passed. May not
> read; no Java. Lesson: at CFADE trigger times on this sample, ES/NQ are
> almost always already coupled (ρ ≳ 0.3); the skip gate has no mass.
> Plumbing reusable: `research/features/cross_regime.py`. See
> `results/EXP-0050/` and §7.

### 3.3 Price-equivalence mapping for level ideas

- Any "same level in both markets" idea (1.5, 1.6) needs a map from ES price
  to NQ price. Use the rolling β and intercept from 3.1, not a fixed ratio.
  Record the mapping error distribution before using it.

## 4. Correlated **state** / regime ideas

### 4.1 Which market is "in charge" today

- Daily or hourly: Granger-style sign test of whether ES 1 s imbalance predicts
  NQ 1 s return better than the reverse. H18 showed millisecond lead/lag is
  noise; a *slow* lead state (which market the big players are using today) is
  a different question. Use as an allocator feature: MAT with ES-confirm vs
  NQ-first depending on state.

### 4.2 Divergence *persistence*

- How long does a cumulative divergence (2.1) usually last before resolving,
  and which market gives in? If ES typically wins, the resolution is a known
  trade; if it is 50/50, divergence is a volatility signal, not a direction.
  Pure descriptive study; informs 2.1's exit design.

### 4.3 Cross-market large-print clustering

- Count p99 prints in both markets per 5 s. Bursts of large prints in both →
  institutional participation; one only → single-market. Stratify MAT/CFADE
  outcomes by the two-market large-print count. Extends the Arm B idea from
  EXP-0044 off the dead level machinery.

> **Result — EXP-0053 / H44 (2026-10-24): reject / park.**
> Frozen form: causal trailing-30 m p99 thresholds (NQ ≥ 3, ES ≥ 5 lots
> median), burst = ≥ 2 large prints / 5 s, MAT keep-gate on `both`,
> CFADE strata descriptive. Causality + power PASS (both = 741/1,956
> MAT signals; Arm B rarity was a level-machinery artefact). Stage 1
> FAIL: gated MAT avg +1.13 vs +1.55, hold −87, maxDD 368 > 290,
> placebo p90 +2.95. Joined strata: MAT `both` avg 1.58 ≈ book avg —
> both-market bursting is the trigger's normal state, not a separator;
> best cell `nq_only` (4.11, n=46) was not carried and must not be
> promoted post-hoc. CFADE `both` 6.93 < book 7.63. Plumbing reusable:
> `research/features/large_prints.py`. See `results/EXP-0053/`.

## 5. Suggested order

Ranked by (prior from what has worked) × (cheapness given plumbing) ÷ (how
many prior failures the construction resembles).

| # | Idea | Why first | Needs |
|---|---|---|---|
| 1 | 2.1 cumulative divergence | Direct extension of CFADE; trades-only; high n | ES+NQ trades (have) |
| 2 | 3.2 correlation-regime gate | Cheap filter on both live books; applies everywhere | trades |
| 3 | 1.3 correlated BBO-imbalance as **execution timer** for CFADE | Monetises an existing edge; imbalance is good at next-tick | NQ+ES depth |
| 4 | 2.7 volume-ratio shock | CFADE's best stratum already points here | trades |
| 5 | 1.1 / 1.2 co-pull / co-stack | The user's hypothesis; first use of ES MBO; spoof-resistance argument is sound | ES+NQ MBO |
| 6 | 2.3 aggressor count/size cells | Founding idea, columns exist | trades |
| 7 | 2.4 dual sweep | EXP-0017 plumbing exists | trades |
| 8 | 3.1 basis dislocation ± flow | New axis; continuous signal | trades |
| 9 | 2.5 multi-second correlated absorption | H28's stated revisit | depth+trades |
| 10 | 1.6 / 1.5 paired large orders / icebergs | Rare, needs 3.3; count before building | MBO |

Rules for all of them (per `RESEARCH_RULES.md`): points not ticks; confirm
stamp for research, causal for anything headed to the add-on; **NQ-only
version of the same event is the mandatory null** — a correlated idea earns
its keep only by beating its own single-market twin; state target n and
minimum detectable effect in the YAML; freeze on Oct+Sep, read May once.

## 6. After EXP-0049 — areas worth further digging (2026-10-24)

Ranked by expected value toward the goal (a Bookmap automated strategy that
works), not by novelty. Filters on proven, already-live books outrank new
signal hunts, because MAT and CFADE are implemented in the add-on and a
passing filter ships as a parameter + parity check, not a new module.

| Rank | Area | Why | Cost / ship path |
|---|---|---|---|
| ~~1~~ | ~~§3.2 correlation-regime gate~~ | **DONE reject (EXP-0050).** ρ&lt;0.3 empty at CFADE triggers. | — |
| ~~1~~ | ~~§2.7 volume-ratio shock~~ | **DONE reject at Stage 0 (EXP-0051):** shock candidates 14 < 20. Queue advances to Tier A A2 (§2.3) per §8 / `TIER_A.md`. | — |
| 2 | §1.3 Claim B — BBO-imbalance execution timer for CFADE | CFADE train dies at 2-tick slip; execution is the known bottleneck (RESEARCH_REVIEW §6.3). High monetary value if it works, but it is an execution study, not a signal, and needs depth plumbing the gate does not. **After EXP-0054:** the Claim A agreement state did not earn timer-variable status (`agree_with` n=0), so B1 proceeds **microprice-only** under its own preregistration. | NQ+ES depth; entry set (frozen CFADE) is final since EXP-0050–0054 changed nothing. |
| 3 | §4.2 divergence-persistence anatomy | Pure descriptive: lifetime of D exceedances, which market resolves. Explains EXP-0049 and would inform any future divergence exit, but produces no trade by itself. Do it only if §3.2 suggests coupling state is tradable and we need to know why. **EXP-0050 weakens this:** ρ state at triggers is almost always high, so only run it bundled with a §2.7/§3.1 study that needs the anatomy, never standalone. | Cheap on existing plumbing; no P&L, no ship path alone. |
| 4 | §2.2 effort/result, normalised | EXP-0049 showed progress must be measured per unit signed volume (pts per unit delta over 5–30 s, ES as control), not as an absolute hurdle. Genuinely untested in that form, but it is a new-signal hunt after three absorption-adjacent failures (H1, H28, H40). | Trades-only; needs a fresh construction + preregistration. |

Full audit of every other §1–§4 idea (open / conditional / closed, and the
only form in which each may be dug): **§8 below**. §6 is the queue; §8 is
the completeness check so nothing is silently dropped or silently revived.

**Explicitly closed (do not dig):**

- Retuning EXP-0049 persistence / D / imbalance thresholds on the DIG4 23d
  sample, or promoting `persist0` / W5 / W30 neighbours. That is digging
  through a preregistered kill bar.
- Any new level/book-shape first-touch fade (§1 constructions as standalone
  entries). Seven constructions, zero survivors vs placebo; §1 material may
  still serve as *filters or execution timers* on tape-proven triggers.
- Millisecond lead/lag refinements (H18/H22/H23 family, incl. §4.1 as a
  timing signal). Price-lead has failed at every horizon tested.

## 7. EXP-0050 / H41 — correlation-regime gate (DONE reject)

**Choice (2026-10-24):** §3.2 on frozen CFADE. **Outcome: reject / park**
at Stage 1 — see §3.2 result box and `results/EXP-0050/`.

**Why it failed:** the skip cell (ρ &lt; 0.30) had **zero mass** on DIG4
CFADE triggers. Markets are already coupled when CFADE fires; a
decoupling gate cannot filter what does not occur. Apparent lift was
warmup-drop, not low-ρ, and failed the placebo.

**Closed:** retuning the ρ cutoff / window on the same 23d sample.
**Next per §6 ranking (as run):** §2.7 volume-ratio shock — **done,
EXP-0051 reject at Stage 0** (shock n=14 < 20; see §2.7 result box).
Tier A then ran to completion: A2 (EXP-0052), A3 (EXP-0053), and A4
(EXP-0054, §1.3 Claim A) all reject/park. Queue now: Tier B B1 (§1.3
Claim B execution timer, microprice-only after EXP-0054) on the
unchanged frozen CFADE entry set, under its own preregistration.

## 8. Complete remaining dig map after EXP-0050 (2026-10-24)

Audit rule: every idea in §1–§4, plus the correlated-adjacent digs recorded
in `RESEARCH_REVIEW_2026-10-23.md` §6–§7 and `EDGE_RANKING.md`, appears
exactly once below as **OPEN**, **CONDITIONAL** (count/descriptive first,
or only in a restricted form), or **CLOSED**. If it is not in this section
and not in the §0 done-table, it has not been considered — add it here
before running anything. Two lessons from EXP-0049/0050 constrain all of
it: (1) **count first** — no P&L run until the target stratum's event count
on DIG4 23d is reported (gate strata need n ≥ 20 trades, standalone entries
n ≥ 200 events, per the §5 rules); (2) **stratify before you signal** —
descriptive strata on the frozen MAT/CFADE trigger sets are cheaper,
powered by existing n, and ship as a parameter + parity check, whereas
every standalone construction in this family has so far died.

### Tier A — DONE (all four closed reject/park 2026-10-24; was: trades-only, on proven books)

Tracker: [`docs/TIER_A.md`](TIER_A.md) — status board and equal A1–A4
sections (claim, first step, kill/promote template, ship path).

| # | Area | Status / form | First step | Ship path |
|---|---|---|---|---|
| A1 | §2.7 volume-ratio shock | **DONE reject/park (EXP-0051, Stage 0):** shock stratum 14 candidates < 20; no P&L, no May, no retune. | Counts were the result (see §2.7 result box). | Closed. |
| A2 | §2.3 aggressor count vs size | **DONE reject/park (EXP-0052):** FLB-gated MAT avg +0.59 vs +1.55, placebo fail; MSQ empty on CFADE. | Counts + gate P&L read (see §2.3 result box). | Closed. |
| A3 | §4.3 large-print clustering | **DONE reject/park (EXP-0053):** power PASS (both 741 signals) but gated MAT avg +1.13 vs +1.55; hold/maxDD/placebo fail; `both` not a separator. | Gate P&L + joined strata read (see §4.3 result box). | Closed. |
| A4 | §1.3 Claim A — BBO-imbalance agreement state | **DONE reject/park (EXP-0054, matters bar):** causality PASS, all 2,088 triggers defined, but `mixed` 1,949/1,956 MAT + 132/132 CFADE; `agree_with` trades 0 on both books. | Counts + joined strata read (see §1.3 result box). | Closed. B1 falls back to microprice-only; no standalone entry. |

**Tier A completion (2026-10-24).** A1–A4 all failed inside the frozen
books: the cross-market states are either rare exactly when the books
fire (A1 shock 14 < 20; A4 agreement ~absent) or re-describe the burst
trigger conditions (A2 FLB avg +0.59 vs +1.55; A3 `both` ≈ book avg).
None reached May; MAT and CFADE ship exactly as frozen before Tier A.
Full summary: `docs/TIER_A.md` (Tier A completion summary).

### Tier B — Execution & portfolio (no new signal; highest £ value per day)

| # | Area | Status / form | First step | Ship path |
|---|---|---|---|---|
| B1 | §1.3 Claim B — BBO-imbalance execution timer for CFADE | **DONE reject/park (EXP-0058 / H49).** Microprice OOS lift fails IS leg; passive same. Re-test only on *future* months, no retune. | — | Closed on this sample. |
| B2 | StackConsume fade × ES context (H34 follow-up) | **OPEN, two honest variants only** (REVIEW §6.6): ES-aggressor-not-pushing-break gate at +2 s, and progress time-stop (+20/30 s, exit if MFE < 5 pt). Both were specified *before* any ES read on this book; the §6.6 kill bar (May avg + ≥ 1 SE **and** maxDD cut) applies. Plus forward-paper on new depth sessions, no retune. | Read the two filters once on the frozen StackConsume event set. | Second gate on the H34 dig book; still shadow/paper until ≥ 2 more untouched months (REVIEW §3.3). |
| B3 | Combined MAT + CFADE book / allocator + CFADE May anatomy | **DONE ship allocator (c) (EXP-0057 / H48).** `er60_switch + MAT skip_range175`; always-on OOS −472 → (c) +290; corr≈0. `after_mat_sl` descriptive fail (n=91 −158). | — | Portfolio rule for live MAT+CFADE sizing. |

### Tier C — Open new-signal hunts (trades-only; fresh preregistration required)

| # | Area | Status / form | Caution |
|---|---|---|---|
| C1 | §3.1 synthetic-basis dislocation ± flow | **DONE reject/park (EXP-0062 / H53).** All arms OOS negative. | Closed. |
| C2 | §2.2 effort/result, normalised | **DONE reject/park (EXP-0063 / H54).** Beats twin avg; loses null; OOS net −. | Closed. |
| C3 | §2.6 flow-inflection synchrony | **DONE reject/park (EXP-0064 / H55).** Beats twin+null avg; OOS net −. | Closed. |
| C4 | §2.5 multi-second correlated absorption | **CONDITIONAL — one permitted resurrection** (EDGE_RANKING Tier 4 note, EXP-0035 post-mortem): ≥ 3 s execute-into a *tracked level*, ES confirming over the same seconds. This is a different construction from H28's 100 ms bars, not a retune. One attempt, pre-registered, then closed either way. | Heaviest Tier-C build (depth + level tracker); do not start until A-tier is exhausted. |
| C5 | Confirm-burst continuation + ES-against exit | **DONE PASS (EXP-0061 / H52).** n=3055 net +815 OOS +577 avg +0.23; beats twin/null/D. Thin avg — Java shortlist with CFADE hand-off. | Ship candidate (separate plan). |
| C6 | Role-swapped CFADE / ES venue | **DONE reject (EXP-0060 / H51, EXP-0059 / H50).** | Closed. |
| C7 | CFADE persist2 entry | **DONE reject (EXP-0065 / H56).** retain 2%. | Closed. |

### Tier D — Conditional: book/MBO ideas (count first; filter/feature form only)

Standalone first-touch entries from §1 remain **closed** (§6 stop-list:
seven level/book constructions, zero survivors vs placebo). What survives
is the right to serve as *filters, timers, or features on tape triggers*:

| # | Area | Condition |
|---|---|---|
| D1 | §1.1 co-pull / §1.2 co-stack | **DONE count (EXP-0066):** proxy n≫200 but too sensitive (1.3M events); no auto-promote. Needs tighter MBO-resolved cancel definition before filter dig. |
| D2 | §1.5 correlated iceberg / §1.6 paired large orders (+ §3.3 price-equivalence map) | **Still descriptive:** MBO on 98/113; detector deferred pending tighter D1. Feature-on-trigger only. |
| D3 | §1.4 book-pressure divergence (NQ lean vs ES lean ≥ 2 s) | **DONE park (EXP-0066):** CFADE/MAT disagree stratum fails OOS lift ≥+1.0. Standalone spoof-fade remains closed. |
| D4 | §4.2 divergence-persistence anatomy | **Descriptive only, bundled:** lifetime of D-exceedances + which market resolves. Run only as an appendix to A1/C1 if their design needs it (see §6 rank 3 note); produces no trade alone. |
| D5 | §4.1 which-market-in-charge (slow state) | **Allocator form only.** As a *timing* signal it is closed with the H18/H22/H23 family (§6). As a slow daily/h hourly allocator input it folds into B3 — test it there, not as its own experiment. |

### Closed — do not dig (restated so §8 is self-contained)

- §2.1 retunes / neighbour promotion on DIG4 23d (EXP-0049 kill bar), and
  §3.2 ρ-cutoff/window retunes on the same sample (EXP-0050 kill bar).
  The two untested §2.1 sub-variants (literal minute-of-day z-scored `D(t)`;
  first-crossing + hysteresis entry) may only ever run on **fresh sessions
  as a new preregistration** — they are not a DIG4 dig.
- §2.4 dual sweeps as an entry (EXP-0017_REOPEN reject), H28 100/250 ms
  correlated absorption (EXP-0035 reject; C4 is the only successor),
  millisecond lead/lag in any form (H18/H22/H23), correlated *price*
  co-move (H23), and every standalone level/book-shape first-touch fade
  (§1 entries, H33–H37 family).
- MAT-exit use of disagreement (EXP-0038/H31: disagreement is an entry
  signal, not an exit signal), and any second filter selected on a holdout
  month (REVIEW §8).

### Adjacent queues (not correlated ideas, tracked here so they are not lost)

From the 2026-10-19 audit (`EXPERIMENT_LOG.md`) and REVIEW §6.4, the legacy
dig backlog still open after EXP-0014_PNL and EXP-0017_REOPEN rejected:
**EXP-0033 high-vol simultaneous-flow stratum** as a MAT overlay (points
re-run, then P&L; base same-clock flow stays dead), then **EXP-0020
dual_magnet** as a one-shot MAT P&L (n=20, lottery ticket, hard
no-second-dig rule). These interleave *below* Tier A: they are cheap and
pre-specified, but they refine MAT, whose May failure is a regime problem
that neither dig addresses. REVIEW §7's wider list (calendar effects,
evening session 17:00–21:00, loss-clustering/daily-stop design, ES-as-venue
§6.8, pre-open day-type prior §6.9) remains documented there and is
unchanged by EXP-0049/0050; none of it outranks Tier A/B.

**Next action (updated 2026-10-26):** CFADE-family queue EXP-0057–0066
complete — synthesis in `results/CFADE_FAMILY_SYNTHESIS/`. Survivors:
allocator (c) for live MAT+CFADE sizing, and EXP-0061 continuation book
(Java shortlist). Still open in this map: B2 StackConsume ES-context,
C4 multi-second absorption (one shot), tighter D1/D2 MBO features,
legacy EXP-0033 / EXP-0020. Do not reopen Tier A or rejected C1–C3/C6–C7
by retuning on this sample.
