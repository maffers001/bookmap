# Edge ranking — which ideas to keep, dig, or drop

> Snapshot as of **2026-10-21**. Yardsticks: MAT vol-k=1 P&L for entries/fades;
> corrected points event study (60 s) for path ideas; May 2026 as untouched OOS
> where available. Pre-fix tick / look-ahead-85% numbers are not used here.
> Source detail: `docs/EXPERIMENT_LOG.md`, `docs/HYPOTHESES.md`,
> `results/SHORTLIST_RERUN_POINTS.md`, DIG4 / CFADE May / MAT May summaries.

## Short answer

The best edge is the **correlated-exhaustion fade (CFADE, EXP-0036 DIG4)** — the
only idea that made money on untouched May 2026 in the same regime where MAT
lost. Dig backlog: EXP-0014_PNL and EXP-0017_REOPEN both **reject/park**
(2026-10-23). Two queues now: the **correlated queue** (EXP-0049/H40 and
EXP-0050/H41 rejected; **Tier A A1–A4 all rejected 2026-10-24** —
EXP-0051 Stage 0, EXP-0052/0053 Stage 1, EXP-0054 matters bar with
`agree_with` n=0; next: Tier B B1 microprice-only CFADE execution timer)
outranks the **legacy queue** (EXP-0033 high-vol overlay → EXP-0020
dual_magnet one-shot). Full audited map: `CORRELATED_IDEAS_2026-10-23.md` §8;
Tier A tracker (complete): `docs/TIER_A.md`.
Depth / LVN / level ideas are
dead (one narrow construction exception). The deliverable is increasingly a
**two-book portfolio** (MAT continuation + CFADE fade), not a single strategy.

---

## Tier 1 — Proven edge, act on it

### 1. CFADE — correlated exhaustion fade (EXP-0036 → DIG4) — best overall

**Rule:** NQ still bursting with the original move; ES imbalance flips against
(≤ −0.4) within 5 s; fade NQ; gate to rotational days (`ER60 ≤ 0.30`);
vol-scaled brackets (live default SL 1R / TP 2R).

| Evidence | Result |
|----------|--------|
| DIG4 freeze (`skip_er0.3`) | n=91 net **+355** (train +6 / hold +123 / new +226); days+ 16/23; LOO_neg=0; top3 share 0.45 |
| Neighbour `skip_er0.25` | also cleared (+311) — required for freeze |
| May 2026 OOS (SL1/TP2) | n=95 net **+335**; 10/17 days+; primary alias only |
| Additive vs MAT (23d) | MAT+fade **+1,259** vs MAT alone +779; `not_in_mat` fades +453 of fade's +479 |

**Why #1:** only idea with honest May confirmation *and* opposite regime appetite
to MAT (MAT died in rotational May; CFADE's ER gate is "fade when not trending").

**Caveats (test-size, not full-size):**

- Train is razor-thin (+6 symmetric; +74 with asym). At 2-tick slip, gated train → −1. **Fill quality is the whole ballgame.**
- Shorts carried Sep–Oct (short +509 / long −30) — may be drift; check May side split before sizing up.
- Two May days (−153, −145) show a real left tail; DIG4 −150 kill switch is well-calibrated.

**Status:** frozen for TEST_AUTOTRADE. Spec: `results/EXP-0036_DIG4/TEST_AUTOTRADE_SPEC.md`.
No further param search on the 23d sample.

### 2. MAT momentum base (EXP-0008, vol k=1, trend+VWAP) — real but regime-decaying

| Evidence | Result |
|----------|--------|
| 23d UK 14:30–17:00 | ~+750 net; new-9 generalised (PF 1.22) |
| May 2026 OOS | n=457 net **−264**; 7/17 days+; maxDD 528 — do not retune |

**Role now:** one leg of a two-book portfolio, not a standalone product.
Most overlays were falsified (CD pullback gate new −182; swing-break EXP-0039
worse gated and ungated; exits/trails none beat base on both cohorts).

**Risk overlay May-tested (2026-10-21):** `skip_range175` on May —
n 457→247, net **−264 → −186**, days+ 7→9, maxDD 528→466. Softens the
loss / cuts exposure; **does not flip May**. Do not ship as a rescue.
Results: `results/MAT2_MAY2026_VOLK1_SKIP_RANGE175/`.

---

## Tier 2 — Best untested upside (dig backlog)

Order matches the 2026-10-19 audit in `EXPERIMENT_LOG.md`. Agree with that order.

### 3. EXP-0014 — standalone CD pullback × momentum — **DONE reject/park (2026-10-23)**

Corrected points (14d, causal): **n=49, hit+10 0.55** (vs 0.44 raw 0008),
**@60s +7.8**, MFE 20.1 / MAE 11.7 — best path asymmetry of any event set.

**EXP-0014_PNL:** standalone vol k=1, Sep+Oct n=89 net **−110.6**; bar FAIL
(both months ≤0, avg < momentum parent, ≪ null p90). May not read. Event-study
path does not survive as a P&L book. See `results/EXP-0014_PNL/`.

### 4. EXP-0017 — aggressor sweeps — **DONE reject/park (2026-10-23)**

Pre-fix: dual hit+10 52% vs null 43%; killed solely vs void 0008 85%.

**EXP-0017_REOPEN:** Stage-1 points bar FAIL (Sep pass / Oct miss hit10 vs
null by 0.2 pp). dual_q n=3672 hit10 0.45 / @60s +0.63 — beats NQ-only but not
month-stable vs large-n null. Stage 2 / May not run. See `results/EXP-0017_REOPEN/`.

### 5. EXP-0033 — high-vol simultaneous flow (overlay, not standalone)

High-vol + same-sign-price stratum was strongest in-sample cell of the
same-clock family; parked for lack of holdout. Headline numbers are pre-fix
tick units — treat as directional only. Test as MAT sizing/overlay on 23d P&L.
Base same-clock flow stays dead; `|lag|<50` already captures "same time."

### 6. EXP-0020 — dual_magnet (one shot, expect little)

Corrected points: n=20, hit10 0.70, @10s +6.2. Lottery ticket, not a signal.
One MAT-sim run; hard "no second dig" rule. Lowest priority.

---

## Tier 3 — Intriguing but not validated

### 7. HV Extreme retest (EXP-0038)

Retest-to-tip fixed extension entry (−404 → +454 Sep+Oct), but honest controls
undercut it:

- HV fires beat paired random null only in October (Sep P=0.40, holdout P=0.90;
  holdout net −220).
- On May, preferred R *flips*: freeze R10 +366 (P_null=0.20); R5 (rescued first
  holdout) loses −112. Parameter sign-change across holdouts → not shippable yet.

**Action:** park; shadow on Bookmap replay beside CFADE test-trading; judge on
next 10–15 *fresh* sessions. Do not re-slice Sep/Oct/May.

Sub-lead only: all-impulse retests with `es_leg_move_dir > 1.25` (+782 Sep+Oct,
holdout +24) — threshold chosen in-sample; lead, not result.

### 8. Stop-run fingerprint (EXP-0041) — visual only

Continuation hit10 0.41 ≈ null 0.39 ≪ 0008 0.45. `outcome_continue` stratum
(0.58) is post-hoc and untradeable. Freeze thresholds for visual Bookmap
module; **no auto-trade**. Collect more events before any causal
stall-vs-continue dig (current n=44).

---

## Tier 4 — Dead. Do not reopen

Stop-list endorsed. Common thread: **every level/depth/price-pattern idea
failed; every survivor is tape-flow-based.** Let that guide new ideas.

| Family | IDs | Why closed |
|--------|-----|------------|
| Absorption / replenishment | H2, EXP-0005/0006 | ≈ null on 14d depth points |
| Correlated absorption (100 ms bars) | H28, EXP-0035 | dual ≈ NQ-only; train P&L negative every variant |
| LVN families | H7 / H7b / H8, EXP-0011/0012/0023 | dual empty or ≈ null |
| Challenge-wo-trade / micro-regime | H11 / H16, EXP-0016/0021 | empty or ≪ null |
| MBO vacuum | H13, EXP-0018 | n=5245 hit10 ≈ null |
| VPOC / HVN departure | H14, EXP-0019 | scalp yardstick ≠ old TP25 holds |
| Book-imbalance pressure | H15, EXP-0020 pressure | ≈ null (magnet = Tier 2 one-shot only) |
| Dual CD filters | H17, EXP-0022 | path ≤ unfiltered 0008 |
| ES-leads timing | H18, EXP-0024 | inverted / no lift in points |
| Failed-break / flush-hold | H20 / H21, EXP-0026/0027 | empty or ≈ null |
| Catch-up / price co-move | H22 / H23, EXP-0028/0029 | edge is correlated *tape*, not *price* |
| Rotation gate | H24, EXP-0030 | inverted at every threshold |
| Trapped peak | H25, EXP-0031 | identifies pattern; no edge |
| Range-bar imbalance | H26, EXP-0032 | all cells ≈ null, path ≤ 0 |
| Exhaustion-as-exit | EXP-0036 Part A | fires ~65% of trades; gives back winners |
| MAT × significant swing / bounce | H19 / H31, EXP-0039/0040 | through/bounce worse or flip on May |
| BB-regime filters | H10, EXP-0015 | wide ≈ narrow |
| Fixed-bracket grid | — | 12.5/30 ~75% OOS degradation |

**One permitted resurrection (different construction, not a retune):** absorption
rebuilt as a **multi-second, level-anchored process** (sustained execute-into a
tracked large level over seconds, ES confirming over the same seconds) — as
specified in the EXP-0035 post-mortem. One attempt, pre-registered.

---

## Suggested new work (beyond the dig backlog)

### A. Combined book (highest value, zero new signal risk)

Two frozen, causally-clean signals with opposite regime appetites:

- MAT wants trend (died in rotational May).
- CFADE wants low-ER rotation (ER gate is literally "fade when not trending").

Pre-register: daily-P&L correlation of both books across all ~40 sessions
(23 + May 17); combined maxDD; one simple allocator (ER60 regime switch **or**
1-contract-each always-on) vs each book alone. Expectation: the product of this
project is a portfolio.

### B. CFADE anatomy on May (quasi-OOS, free)

DIG3 strata *not* frozen, still untouched on May:

1. `wait_500_2000` — n=46 +411, train +129 (only trigger cell that fixed train)
2. `after_mat_sl` — n=12 WR 0.92 +236
3. low burst-ES-vol tercile — +427 vs high tercile −78

Pre-register exact DIG3 definitions; run once on May; promote any that hold to
live flags. Do not refit thresholds.

### C. Diagnose MAT's May failure (frozen strata only)

Stratify May MAT trades by terciles already defined on the 23d sample (ER60,
60-min range, med_r30, ToD — e.g. 15:00–15:30 was +388, close −183). If a
*pre-existing* stratum cleanly separates May losers → candidate regime gate for
the *next* month. If nothing separates → MAT is decaying; size down. Fitting
anything new to May is forbidden.

### D. Cumulative tape-divergence (new correlated idea)

Every survivor is ES/NQ aggressor tape agreeing or disagreeing. Untested: the
*dose/duration* version — cumulative signed aggressor imbalance over rolling
5–30 s windows.

1. Cumulative divergence (NQ pushing, ES net-flat/against over ~10 s, not one
   100 ms bar) → slower, higher-n cousin of CFADE.
2. Both cumulatives build, then NQ price-progress per unit imbalance collapses
   → effort/result at the right horizon (H1 was 100 ms and ε-spike-plagued).

Reuses `corr_exhaust` / tape-bar plumbing.

### E. Dwell / repel around large levels (H3 / H9) — one careful build

Never actually tested: EXP-0013 died on a detector problem (uncontacted levels
almost never close), not a falsification. Closest to the stated discretionary
read ("longer dwell → more likely supports price"). Define via level lifetime:
spawn → approach within X ticks → dwell without trade → outcome (repel /
withdraw / absorb), requiring same state in ES within sync. Survival/lifetime
logging first; trading rules second. Rank behind A–D (heavier construction;
level family has failed ~10 times).

### F. CFADE execution research before more entry research

Edge at 1-tick is +355 / +663; train dies at 2-tick. Passive join at fade price
(accept partial fill rate) or 250–500 ms delayed-marketable comparison on the
frozen event set may be worth more expectancy than any new signal. Cheap;
directly monetisable.

---

## Suggested order of work

1. **Now:** CFADE test-autotrade forward (specced) + May side-split check;
   combined-book correlation / DD study (A).
2. **Next experiments** (each ≤ a day, pre-registered kill bars):
   ~~EXP-0014 standalone P&L~~ / ~~EXP-0017 sweeps~~ (both reject/park) →
   ~~Tier A A1–A4~~ (all reject/park 2026-10-24, EXP-0051–0054) →
   Tier B B1 microprice-only CFADE execution timer (`CORRELATED_IDEAS` §8) →
   EXP-0033 high-vol overlay → dual_magnet one-shot.
3. **Then:** CFADE May anatomy strata (B) and MAT May diagnosis (C) — look once,
   freeze or drop.
4. **Shadow / forward only:** HV retest and stop-run visual on fresh sessions.
5. **If appetite for new construction:** cumulative tape-divergence (D) →
   dwell/repel (E) → multi-second level absorption — in that order (from proven
   tape mechanics toward the level family that has failed repeatedly).
6. **Parallel / anytime:** CFADE execution dig (F).

---

## Methodology reminders (why this ranking is possible)

Keep the October-18 reset discipline:

- Chart **points**, not ticks; horizons to 60 s.
- **Causal** stamps when matching live; confirm stamps for research variants.
- **0008 is the bar only for continuation overlays that claim to beat the lead.**
  Fades / alternate entries use **null + MAT P&L**.
- Path-strong thin ideas get a P&L dig, not a hit% kill.
- Untouched months (May-style) are held out; lose on paper without retuning.
- Primary window: UK 14:30–17:00 unless ToD is the question.

See `docs/RESEARCH_RULES.md`.
