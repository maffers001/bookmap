# Experiment Log

> **2026-10-18 evening — methodology reset.** Pre-fix EXP-0002…0033 event-study
> numbers used (a) tick units as if they were points, (b) often the look-ahead
> 85% EXP-0008 comparator, (c) depth only on 5 days. Those rows are marked
> `needs re-run (units/comparator)`. Corrected shortlist re-runs are appended
> under “Run notes” below. MAT-sim P&L results (points, causal ES) remain valid.

| ID | Hypothesis | Status | Sessions | Notes |
|----|------------|--------|----------|-------|
| EXP-0001 | Extract validation | pending | — | Spot-check after first real `.bmf` extract |
| EXP-0002 | H1 effort/result | needs re-run (units/comparator) | 5d | ε-spike defect; absorption LONGs +9pts@2s, noisy SHORTs ~0; fade by 10s |
| EXP-0002b | H1 gated long-only | needs re-run (units/comparator) | 5d | counter-ticks + no flat; mean +9.3@2s / +0.1@10s; hit+10=57%; too small |
| EXP-0002c | H1 gated both | needs re-run (units/comparator) | 5d | dropped 2 flats; shorts dilute; mean +4.9@2s / −1.3@10s |
| EXP-0005 | H2 absorption | re-run 14d (see notes) | 14d | was 5d-only depth; re-run after full depth convert |
| EXP-0006 | H2 replenishment | re-run 14d (see notes) | 14d | was 5d-only depth |
| EXP-0009 | H6 CD with-flow gate | needs re-run (units/comparator) | 5d | 22-tick CD SMA100; with-flow hurts H2 absorbs; against-trend absorbs look better; pullback n tiny |
| EXP-0004 | H3 persistence | pending | — | |
| EXP-0007 | H3 attract/repel/dwell | pending | — | |
| EXP-0008 | H4 ES+NQ momentum | corrected baseline | 14d | points + confirm stamp + 60s: hit+10≈0.44; see notes |
| EXP-0010 | H5 composite | pending | — | |
| EXP-0011 | H7 dual LVN rejection | re-run 14d (see notes) | 14d | was falsified on 5d ticks |
| EXP-0012 | H8 liquidity flip at LVN | needs re-run (units/comparator) | 5d | flip=0; beyond-only n=3 (one cluster) — not validated |
| EXP-0013 | H9 correlated dwell/repel | needs re-run (units/comparator) | 5d | uncontacted dwell/withdraw almost never closes on NQ abs≥50 |
| EXP-0014 | H6 CD pullback primary | **reopen dig** (standalone P&L) | 14d→23d | best path in points; MAT *gate* failed OOS; never MAT-sim'd as own entries |
| EXP-0015 | H10 CD BB regime × momentum | needs re-run (units/comparator) | 5d→14d ticks | wide≈narrow; non-monotonic in 14d tick re-run |
| EXP-0016 | H11 dual challenge-wo-trade ± LVN × narrow BB | re-run 14d (see notes) | 14d | |
| EXP-0017 | H12 aggressor sweep momentum | **reopen** points+P&L dig | 5d | beat null; killed only vs look-ahead 0008 85% — never points re-run |
| EXP-0018 | H13 near-BBO MBO vacuum | needs re-run (units/comparator) | 5d | dual hit+10 40% ≈ null 41%; n=5245 no edge |
| EXP-0019 | H14 VPOC/HVN departure (S20/S4 port) | needs re-run (units/comparator) | 5d | S20 hit+10 40% / S4 36% ≤ null 46% vs old 0008 85%; scalp horizon ≠ TP 25pt holds |
| EXP-0020 | H15 near-book depth imbalance | pressure **reject**; dual_magnet **tiny dig** | 14d | dual_magnet n=20 hit10 0.70 / @10s +6.2 — never MAT P&L |
| EXP-0021 | H16 dual micro-regime take+hold | needs re-run (units/comparator) | 5d | composite dual n=0; dual take≈null; dual hold n=9 hit22% |
| EXP-0022 | H17 dual ES+NQ CD trend/BB filter | needs re-run (units/comparator) | 5d | matched CD: ES flat; adapted ES: small hit bumps, path ≤ unfiltered 0008 |
| EXP-0023 | H7b tightened dual LVN (HVN sandwich) | needs re-run (units/comparator) | 5d | dual≈0; NQ-only hit~31–44% ≈ null; dig lookback/frac no lift |
| EXP-0024 | H18 ES-leads-NQ on lag&lt;250 | re-run 14d (see notes) | 14d | inverted → NQ-leads; test via mat_sim es_stamp=confirm |
| EXP-0025 | H19 swing-break × momentum | re-run 14d (see notes) | 14d | |
| EXP-0026 | H20 dual failed swing-break fade | needs re-run (units/comparator) | 5d | dual n=7 &lt;15 stop; NQ-only ≈ null |
| EXP-0027 | H21 flush-into-hold sweep reverse | needs re-run (units/comparator) | 5d | chart-size flush empty in RTH; soft n=29 hit41%≈null; dual=0 |
| EXP-0028 | H22 ES impulse / NQ quiet catch-up | needs re-run (units/comparator) | 5d | n=12 hit+10 42%≈null; path mean noisy |
| EXP-0029 | H23 ES+NQ same-window co-move | needs re-run (units/comparator) | 5d | n=206 hit+10 50%≈null; med@10s −1 ≪ 0008 |
| EXP-0030 | H24 avg rotation gate (mom fresh / fade ext) | needs re-run (units/comparator) | 5d→14d ticks | inverted again on 14d |
| EXP-0031 | H25 trapped peak (LVN band + quiet 20pt / ≤30s) | needs re-run (units/comparator) | 5d | H25b n=26 shorts 27%≪null; dig best ~55% flat |
| EXP-0032 | H26 range-bar buy/sell imb vs prior avg | needs re-run (units/comparator) | 5d | all cells ~44–49%≈null; path ≤0; ≪0008 85% |
| EXP-0033 | H27 simultaneous ES+NQ aggressor flow | base flow dead; **high-vol dig reopen** | 5d→14d ticks | high-vol+price parked vs 0008 without holdout P&L |
| EXP-0034 | H4 MAT gates | ran | 14d US RTH | trend+VWAP lift; see results/EXP-0034 |
| EXP-0035 | H28 correlated absorption fade (per-market adaptive thresholds) | ran — **reject** | 23d UK 14:30–17:00 (depth 15d) | dual hit10 .32–.36 ≈ NQ-only ≪ 0008 .45; ES adds nothing; train P&L negative |
| EXP-0036 | H29 correlated exhaustion (exit + fade) | exit **reject**; fade **FROZEN test-autotrade** (DIG4) | 23d UK 14:30–17:00 | m04+`skip_er0.3` +355; neighbour 0.25 also clears; spec in DIG4 |
| EXP-0037 | H30 HV Extreme Move + vol/fixed brackets | ran — **park** (final test fail) | 23d UK 14:30–17:00 | freeze m20/c15/b2/hv2/pb5/leg60 + fixed 10/30; test −214; reserve untouched |
| EXP-0038 | H30 redesign (retest entry + ablations) | ran — freeze retest | Sep+Oct full | `ret|retest_R10_t300_sb2_x3.0|none` both=454.5; see `results/EXP-0038/summary.md` |
| EXP-0039 | H19 MAT × significant swing (pullaway) | ran — **reject / park** | 32d train + 17d May OOS | gated through_5 worse than none; see notes |
| EXP-0039_NOGATE | H19 breakout, no trend/VWAP | ran — **train lift / May flip** | 32d + May | through_5 train +0.43 vs none −0.32; May −6.3 vs −1.1 — no ship |
| EXP-0040 | H31 MAT bounce at significant S/R | ran — **reject / empty** | 32d train + 17d May OOS | gated bounce_5 n=2; see notes |
| EXP-0040_NOGATE | H31 bounce, no trend/VWAP | ran — **reject / thin** | 32d + May | bounce_5 n=5 train +5.5 / May −20 — no ship |
| EXP-0041 | H32 stop-run fingerprint | ran — **visual freeze / no auto-trade** | 49d UK 14:30–17:00 (May OOS) | n=44 hit10 0.41≈null 0.39; see notes |
| EXP-0042 | H33 IRT dual-level correlated S/R | **reject / park** (causal re-run) | 14d Oct + Sep aligned | causal shallow vol_k1 **−100 Oct / −59 Sep**; legacy +612/+128 was look-ahead; see notes |
| EXP-0043 | H34 IRT hold vs break orderflow | **null pre-touch; StackConsume fade soft-confirms May** | 14d Oct + 15d Sep + 17d May depth | freeze fade StackConsume≤−0.25 tp30/sl15: +406/+142/+160 (post grid fix); AMB dilutes; see notes |
| EXP-0043_STACK_FADE | H34 frozen dig (May OOS) | **soft confirm** | 17d May OOS (Oct/Sep context) | n=192 net **+160** (8/17, maxDD 277; was +131 pre grid fix); HOLD +1475 / BREAK −788 / AMB −527; see notes |
| EXP-0043_CONFIRM_DIG | H34 follow-ups (b) approach-leg book, (a) confirm sweep + 2nd filter | **null (b); (a) robust to k, no filter adopted** | Oct+Sep train, May frozen | StackTrend_pre ≈ placebo; fade +ve 12/12 k×month; `after 15 min` Δavg +0.29 < 1 SE on May; see notes |
| EXP-0044 | H35 dwell + intact break-side wall | **reject / park** | 14d Oct + 14d Sep + 17d May | duration claim fails train; Arm A May +241 but Oct ≪ placebo; Arm B/protect null; see notes |

## Run notes

### 2026-10-21 — EXP-0043 IRT hold vs break OF (H34)

- Runner: `python -m research.irt_lvn.run_hold_break_of [--month YYYY-MM]`. Window UK **14:30–17:00**. NQ-only. Levels frozen at touch (LVN2/3, DOP, 2nd dVPOC); cluster ≤5 pts; 30 pt impulse / 30 pt reject vs continue; pad 3.
- Sessions: 13 Oct with depth+mbo (exclude `191026`); align gate all ok. Universe HOLD **67** / BREAK **61** / AMB **78**; stratified sample **63/51/72** (cap 12/day).
- Primary scores on `[-2s,+2s]` only: **TakeScore** med gap +0.38 (δ 0.30, d 0.53, LOO yes); **TapeScore** +0.41 (δ 0.20, LOO yes); StackScore +0.16 weak; AttractRepel ≈0 (few large levels tagged at IRT price). BreakLikeness +0.25 (δ 0.28).
- Hold→Amb→Break gradient on Take (0.18→0.30→0.56) and Tape (0.30→0.47→0.71). Impulse tertile: Take separates low/high; Tape fails in high-impulse bucket (approach-speed confound).
- Chart-review lists in `results/EXP-0043/summary.md`. No P&amp;L / no May. Candidate OF features for shallow-reclaim dig: beyond-aggressor + tape intensity at touch.
- **Sep descriptive OOS** (`results/EXP-0043_202609/`): converted depth/mbo for `150926`/`250926`/`280926` (Rithmic primary). Now 10 depth days. Universe HOLD **15** / BREAK **7** / AMB **34** (breaks 5/7 on `280926`). **Take gap −0.30 / Tape −0.60** — sign flip vs Oct (+0.38 / +0.41). Thin + concentrated, but **Oct Take/Tape not OOS-confirmed**. Early Sep `010926`…`100926` still trades-only (NQU6 align-risk).
- **The Oct numbers above are superseded** by the post-review re-run below (the `[-2s,+2s]` window included the reaction).

### 2026-10-21 — EXP-0043 watertightness fixes + re-run (H34 → null pre-touch)

- **Problems fixed** (`hold_break.py`, `of_scores.py`, `run_hold_break_of.py`): at-window `[0,+2s]` overlapped the outcome → primary scores are now **pre-touch only** `[-2s,0)` (`TakeScore_pre`, `TapeScore_pre`, `StackScore`, `BreakLikeness_pre`); `_at`/unsuffixed scores kept as **mechanism checks** only. Baselines: robust median/MAD sliding windows, RTH-clipped to 14:30, `baseline_ok` requires ≥5 min; tape z on a dense 100 ms grid (empty slots count). StackScore baseline is BBO-relative with the same level offset (was fixed price band → tautological); large size = session p99.9 of depth sizes (floor 15; abs 50 never fired). Depth replay seeds from the pre-window book. Universe: DOP ignored for first 5 min (was tautological at open), real transitive clustering ≤5 pt, per-level-id cooldown, jump-through prints count as touches. Report: `baseline_ok` stratum, ≤15 / >15 min split, session-cluster bootstrap CI90, cliffs δ + d + LOO.
- **Oct (13d):** universe HOLD 64 / BREAK 50 / AMB 78 (sampled 58/44/72; baseline_ok 51/39/68). **Pre-touch primary:** Take_pre gap +0.33 **δ 0.165** boot δ CI90 **[−0.03, 0.33]**; Tape_pre gap +0.53 **δ 0.01**; StackScore δ −0.01; BreakLikeness_pre δ 0.12 CI ∋ 0. **Mechanism (`_at`):** Take_at gap +0.60 **δ 0.38** CI90 [0.24, 0.54], Tape_at δ 0.21, StackConsume δ 0.33 — i.e. the earlier "signal" was the first 2 s of the reaction. AttractRepel still ≡0 (tracker rarely tags at IRT price; `n_tracker_levels` median 0). Raw: `post_vol` δ 0.55 (post-hoc), `first_pierce_pts` δ 0.35, `pre_touch_vol` δ 0.22.
- **TOD:** first-15-min Take_pre δ 0.16 vs after-15 δ 0.18 — no open artefact remaining; Take_at δ 0.50 in first 15 min (reaction is fastest at open).
- **Sep (10d):** universe HOLD 14 / BREAK **5** / AMB 33 (baseline_ok BREAK **2**) — uninformative; signs negative but n_b=2.
- **Decision: H34 pre-touch OF does not separate hold from break at this granularity.** Aggressor/tape response *after* first touch does (δ≈0.35–0.4), which is a reaction-confirmation feature not a prediction. Next, if pursued: (a) a confirmation design — enter after the first 1–2 s of reaction and test whether `Take_at`-style confirmation beats a plain reclaim; (b) longer pre-windows (5–10 s) and approach-leg features (`pre_touch_vol`, impulse shape); (c) more depth days (early Sep NQU6 needs roll handling). No P&L / no May.

### 2026-10-22 — EXP-0043 scoring completion + follow-ups (placebo, confirmation, more days)

- **Scoring (completes the appraisal list):** MBO cancels carry only `order_id` (price=size=0) → resolved vectorised against the order map (`_resolve_mbo`, 99.98 % resolved; `replace` = cancel old + add new). Before this `net_add` was adds-only. New pre-touch `MboScore_pre` = beyond near-band pull-rate z + depletion (stack at touch vs −2 s) − replenish z. Tape z baseline 5 min (`tape_lookback_ms`); AttractRepel dropped from both composites (≡0 at IRT prices). Secondary leak-free windows `pre5`/`pre10`/`leg` (60 s). Approach-leg shape: `leg_last10_frac`, `leg_max_pullback_pts`. **Placebo**: same detector/scores at levels shifted ±40 pt, scored in the same pass.
- **Data:** converted Rithmic depth/mbo for `191026` (Oct now 14d) and NQU6 `010926`–`100926`. IRT `@NQ#` is back-adjusted: constant +297.25 pt basis (MAD ≤ 1) on those days → runner shifts IRT levels by the session median when MAD ≤ 2 pt (`basis_offset`/`shift_bars`); post-shift align err ≤ 1 pt. **Sep now 15 depth days.**
- **Pre-touch (Oct 14d, baseline_ok HOLD 55 / BREAK 41):** Take_pre δ 0.04, Tape_pre 0.01, StackScore 0.16 [−0.05, 0.33], MboScore_pre 0.11, BreakLikeness_pre 0.12 — all CI90 ∋ 0. pre5/pre10/leg: δ ≤ 0.13. **Placebo** gives the same magnitudes (Take 0.01, Tape 0.14, Stack 0.04). Sep (HOLD 24 / BREAK 4): signs negative, uninformative. **Pre-touch H34 is null, and nothing in the pre-touch book/tape/MBO is level-specific.**
- **Mechanism vs placebo:** `Take_at` real δ 0.39 (Oct) / 0.35 (Sep) vs placebo **0.22 / 0.32** → generic 2-s momentum, not about the level. `StackConsume` real **0.40 / 0.47** vs placebo **0.20 / −0.08** → the only level-specific signal.
- **Reaction-confirmation at +2 s** (all events still undecided at +2 s): P(BREAK) by StackConsume tertile Oct 0.25 → 0.66, Sep 0.00 → 0.30. P&L (1-tick slip, 0.25/RT, time stop +120 s): `gated[StackConsume≥0.25]` positive in **all 12** bracket × threshold × month cells — tp30/sl15 **+457 Oct (n=165, 9/14 days, maxDD 99) / +192 Sep (n=68, 7/14, maxDD 85)**; tp20/sl15 +190 / +166. Unconditional `fade_tc` −53/+185 Oct, +154/+215 Sep (Sep is hold-heavy); `cont_tc` negative everywhere; `Take_at`/`BreakLikeness` gates mixed. **Decomposition:** the P&L is the *fade* leg — "beyond stack not consumed in the first 2 s → fade": Oct n=122 +130 (tp20) / +422 (tp30); Sep n=60 +154 / +158; continue leg n=43/8, ≈ flat. No single session > 27 % of a month's net.
- **Plain reclaim** (EXP-0042-style, ≥2 pt pierce then back through level, NQ-only): −181/−58 Oct, +46/+57 Sep; pierce<8 subset worse. Consistent with the H33 rejection.
- **Decision: dig candidate — StackConsume-intact fade at +2 s.** Caveats: one gate of three chosen after seeing this run (direction is mechanistically sensible: stop-side liquidity held); two months; tp30/sl15 is a 2:1 bracket with 44 % win. Next: freeze `StackConsume ≤ −0.25` + tp30/sl15, run on May depth days if available (else forward paper), and test whether the AMBIGUOUS-label trades (35/60 of Sep fades) carry the edge or dilute it. Results: `results/EXP-0043/`, `results/EXP-0043_202609/` (`events.parquet` has per-event strategy P&L; `placebo_events.parquet`).

### 2026-10-22 — EXP-0043_STACK_FADE May OOS (frozen StackConsume-intact fade)

- **Freeze (no retune):** undecided at +2 s and `StackConsume ≤ −0.25` → fade; TP 30 / SL 15; 1-tick slip; 0.25 pt/RT; time stop +120 s. Runner: `python -m research.irt_lvn.run_stack_consume_fade`. Config: `research/experiments/EXP-0043_STACK_FADE.yaml`.
- **Data:** converted Rithmic depth/mbo for all 17 May sessions (`*0526`, NQM6). Basis shift −590 pt (MAD ≤ 1.5) → post-shift align ≤ 1.4 pt. Universe HOLD **154** / BREAK **108** / AMB **152** (sampled 100/86/100).
- **Frozen P&L:**

| month | role | n | net | avg | win | days+ | maxDD |
|---|---|---:|---:|---:|---:|---|---:|
| 2026-05 | **OOS** | 191 | **+131** | 0.69 | 0.39 | 8/17 | 261 |
| 2026-10 | context | 122 | +422 | 3.46 | 0.46 | 10/14 | 72 |
| 2026-09 | context | 60 | +158 | 2.63 | 0.47 | 7/14 | 132 |

- **AMBIGUOUS dilutes in every month** (diagnostic labels, not known at entry): May HOLD +1445 / BREAK −788 / AMB **−527**; Oct HOLD +1260 / BREAK −362 / AMB −476; Sep HOLD +442 / BREAK −63 / AMB −222. Edge is carried entirely by eventual HOLDs; AMB and BREAK lose.
- **May mechanism check:** StackConsume real δ 0.23 vs placebo **0.23** — level-specificity weaker than Oct/Sep (where real ≫ placebo). Pre-touch still null (Take_pre δ −0.04). Bidirectional `gated[StackConsume≥0.25]` tp30 also +489 May (consistent).
- **Caveats:** OOS days+ only 8/17; maxDD 261 (3.6× Oct); one session (`150526`) +194 ≈ 148 % of month net; freeze chosen after seeing Oct/Sep. Soft confirm, not a ship.
- **Decision: soft confirm — keep as dig / forward-paper candidate.** Do not increase size. Optional next: require something that cuts AMB/BREAK false fades (e.g. dual ES context, or a second confirmation print) without retuning thr/TP/SL on May. Results: `results/EXP-0043_STACK_FADE/`, `results/EXP-0043_202605/`.

### 2026-10-22 — EXP-0043_CONFIRM_DIG follow-ups: (b) longer pre-window, (a) reaction-confirmation

- **Baseline-grid fix (affects all EXP-0043 numbers slightly):** the offset-matched book baselines in `score_event` used *every* snapshot time inside the 5-min window (`[t for t in book_at if b0 <= t < b1]`), which included other events' touch and pre-window snapshots; adding the −10/−30/−60 s snapshots moved `StackConsume` and shifted the frozen P&L (Oct 422 → 390). Now grid points only (`t % grid_ns == 0`). Re-run of the frozen rule after the fix: **Oct +406 (n 123, 10/14, maxDD 72) / Sep +142 (n 61, 7/14, 132) / May OOS +160 (n 192, 8/17, maxDD 277)**; by label May HOLD +1475 / BREAK −788 / AMB −527. Verdict unchanged (soft confirm). Row above updated.
- **(b) Approach-leg book (`StackTrend_pre`):** beyond-side stack build/pull `stack_build_{10,30,60}` = log((near_now+1)/(near_then+1)), hold-side `hold_build_*` (BBO-relative then), MBO pull-rate over −10/−30 s with grid z (`pull_rate_z10/30`); composite 0.35·pull30 + 0.25·pull10 + 0.25·z10 + 0.15·z30. Cliffs δ break vs hold Oct / Sep / May: **−0.01 / 0.04 / −0.15** vs placebo −0.01 / −0.22 / 0.05. Raws flip sign across months (`stack_build_60` 0.05 / −0.50 / 0.21). **Null, like every other pre-touch score.** The only features with one sign in all three months *and* above placebo each month are `StackScore` 0.15 / 0.19 / 0.18 and `near_sum_z` 0.17 / 0.27 / 0.13 (bigger beyond wall at touch → more breaks — the "fake wall" direction); StackScore CI90: Oct/Sep ∋ 0, May [0.06, 0.29]. Candidate at best; it is also what `StackConsume` already half-uses.
- **(a) Confirmation-time sweep** (fade when `StackConsume_{k}s ≤ −0.25`, matching consume window, TP30/SL15): net Oct / Sep / May — 1 s: 373 / 219 / 96; **2 s (frozen): 406 / 142 / 160**; 3 s: 333 / 47 / 67; 5 s: 390 / 53 / 177. **12/12 cells positive**, best k differs by month (2 / 1 / 5 s) → the fade is robust to confirmation time; 2 s stays frozen, no retune.
- **(a) Second filter on the +2 s fade — selected on Oct+Sep pooled only, May read once:** 12 candidates (thr −0.5, px on hold side, pierce ≤ 2 / = 0, Take_at ≤ 0, near_sum_z ≥ 0, consume_frac = 0, StackTrend_pre ≤ 0, time-of-day, level kind). Best train avg with n ≥ 60: `after 15 min` 3.40 vs base 2.98 (n 113). **May:** n 157 net 176 avg **1.12 ± 1.63** vs base 192 / 160 / **0.84 ± 1.46**; maxDD 225 vs 277; days+ 8/16 vs 8/17. Δavg +0.29 pt < 1 SE → **not adopted**. Appendix (not for selection): on May `pierce_at_tc == 0` (+265, maxDD 153) and `consume_frac == 0` (+268) look best but ranked 9th/12th on train — a May-only pick would be selection on OOS. `near_sum_z ≥ 0` is −72 on train, +137 on May: the "real wall" filter is unstable.
- **Decision: both follow-ups null for the pre-touch question; the StackConsume-intact fade stays a soft confirm with no second filter.** Nothing in the book over the approach leg or in a longer/shorter confirmation window cuts the AMB/BREAK false fades out-of-sample. Next steps that do not retune on May: forward paper on new depth days; dual-ES context as a filter (outside this experiment's data). Runner: `python -m research.irt_lvn.run_confirm_dig` (reads the three months' `events.parquet`). Results: `results/EXP-0043_CONFIRM_DIG/`.

### 2026-10-22 — EXP-0044 dwell + intact break-side wall (H35)

- **Design:** same IRT hold/break universe as EXP-0043. Vol unit `u` = median NQ minute-range (prior 30 RTH min). Pierce min `clip(1·u,6,20)`, pierce max eligible `clip(2·u,12,30)`, dwell band `clip(0.75·u,6,18)`. Strata touch_dwell / pierce_dwell (pierce pocket allows beyond-side up to max pierce). Break-side intact: consume ≤0.25 of reference stack and remaining ≥0.5×. Arm A: fade at first ≥15 s eligibility (deadline 90 s). Arm B: + hold-side large take NQ or ES. Protect dig: post-extreme hold-side MBO net add with consume ≤0.2. Bracket TP30/SL15. Placebo ±40. Duration claim = survival at 15/20/30/45 s (enter if still intact). Train Oct+Sep; May OOS. Runner: `python -m research.irt_lvn.run_dwell_hold [--month] [--dig]`.
- **Arm A:** Oct n=28 net **+2.5** (7/11, maxDD 126) vs placebo **+252**; Sep n=22 net **+53** vs placebo +25; May OOS n=38 net **+241** (8/14, maxDD 64) vs placebo **−64**. May looks good and level-specific; Oct is the opposite (real ≪ placebo).
- **Duration claim (train):** hold_rate 0.30 / 0.30 / 0.30 / 0.29 at 15/20/30/45 s; avg 1.11 / 2.92 / −2.43 / 3.61 — **not monotone**. May survival also fails longer→better (hold 0.40→0.25 at 45 s).
- **Arm B:** n=4/3/4 by month; May avg −11 vs Arm A +6.3 — **not adopted**. Protect gate train n=7 avg −8.1 < Arm A — **not selected**.
- **vs StackConsume +2 s fade:** Oct/Sep Arm A much weaker; May Arm A +241 vs StackConsume +160 (different entry set — not a retune of H34).
- **Decision: reject / park H35.** The registered “longer intact dwell → more hold” claim fails on train; Arm A is not stably level-specific across months. Keep H34 StackConsume-intact fade as the dig. Results: `results/EXP-0044/`, `…_202609/`, `…_202605/`, `results/EXP-0044_DWELL/`.

### 2026-10-21 — EXP-0042 IRT dual-level S/R (H33)

- Levels from `data/irt/`: LVN2/3, `HW_MT_DOP_NY`, 2nd `dVPOC`. Cross-kind dual (any×any). Window UK **14:30–21:00**. ES: `ESZ6_ALL.csv`. Runner: `python -m research.irt_lvn.run_exp_0042`.
- Converted raw `201026` → parquet. Oct now **14d** (`011026`…`201026`).
- **Oct P&L:** reclaim vol_k1 n=102 net **+364** / fixed 12.5/30 **+264**; **shallow&lt;8** vol_k1 n=66 net **+612** (11/13 days+, tpd 5.1, maxDD 60) / fixed **+348**; touch fade vol_k1 **−434**.
- **Kind-pair P&L (shallow vol_k1):** `LVN×LVN` n=36 **+374**; `LVN×DOP` n=10 **+141**; `DOP×LVN` n=7 **+87**; `DOP×DOP` n=9 +23. NQ-kind shallow: LVN +495 / DOP +111 / dVPOC +16.
- **Sep aligned** (`150926`–`300926`; early Sep skip ~297pt roll): shallow vol_k1 n=41 net **+128** (6/8); fixed shallow also reported in `EXP-0042_202609`. Soft confirm vs Oct.
- ~~Decision: dig candidate (shallow reclaim).~~ **SUPERSEDED — the numbers above are look-ahead biased** (see next note). Legacy summaries kept as `summary_LEGACY_lookahead.md` in each results dir.

### 2026-10-21 — EXP-0042 causal fix + re-run (H33 → reject/park)

- **Bugs found:** (1) `nq_pierce_pts` was the max pierce over the *full* 120 s horizon, so the "shallow&lt;8" filter knew the future path; (2) `reclaim_ts_ns` was nulled whenever `outcome != hold`, so only eventual holds were ever entered (survivorship); (3) ES "joint hold" required ES to hold over its whole horizon, not up to entry time.
- **Fix** (`events.py`): `_classify_one` now stamps `reclaim_causal_ts_ns` and `pierce_to_reclaim_pts` (max pierce **up to the reclaim print**); `classify_dual_events` adds `es_pierce_at_reclaim_pts` / `es_causal_ok` (ES not failed *by* NQ reclaim time); `reclaim_events_table(causal=True)` is the default (legacy behaviour via `causal=False`). Runners + dig updated; smoke test passes.
- **Causal Oct (14d):** reclaim vol_k1 n=110 net **+9** (6/13 days+, maxDD 325) / fixed **−53**; **shallow&lt;8** vol_k1 n=90 net **−100** / fixed −20; touch fade −434 (unchanged, was always causal). Kind-pair shallow: `LVN×LVN` +199 (n=44) vs `DOP×DOP` −190 — same post-hoc split as before, now with no OOS support.
- **Causal Sep aligned (8d):** reclaim n=61 net **+2** / shallow n=53 net **−59** (5/8). Hold-path diagnostic still shows hold hit10 0.63–0.72 vs fail 0.21–0.28, but that is conditional on the outcome and not tradeable.
- **Shallow dig re-run (causal):** Oct best of 30 variants `hvn_g30_sl15` +104 (n=42) — a search artefact; Sep **every** variant negative (best fixed_12p5_30 −44, `hvn_g30_sl15` −279). HVN TP does not confirm.
- **Decision: reject / park H33 as a standalone fade.** The +612/+128 "edge" was entirely the outcome-conditioned entry. Dual-level alignment may still be useful as a *context* feature for H34-style scoring but has no P&L on its own. CD/orderflow deferred; May untouched. Results: `results/EXP-0042/`, `results/EXP-0042_202609/`, `results/EXP-0042_SHALLOW_DIG_2026{10,09}/`.

### 2026-10-21 — EXP-0042 shallow bracket dig (HVN TP)

- Runner: `python -m research.irt_lvn.run_shallow_dig --month YYYY-MM`. Same shallow reclaim entry; exit variants.
- **HVN algo:** TP = nearest NQ HVN in profit direction − 2pt (cap 80), only if gap ≥ min_gap; SL = fixed 10/12/15 or beyond-level+8 or half-gap.
- ~~Oct: baselines win — `vol_rr3` +695 / `vol_rr2` +644 / `vol_k1` +612. Best HVN `hvn_g30_sl15` +255. Sep: `vol_k1` +128; all HVN negative.~~ **Legacy look-ahead numbers** (`summary_LEGACY_lookahead.md`).
- **Causal re-run:** Oct `vol_k1` **−100** / `fixed_12p5_30` −20 / `vol_rr3` +19; best HVN `hvn_g30_sl15` n=42 +104 (1 of 30 variants). Sep: all 25 variants negative (`fixed` −44, `vol_k1` −59, `hvn_g30_sl15` −279). HVN targeting does **not** confirm OOS; nothing here is tradeable.
- Results: `results/EXP-0042_SHALLOW_DIG_202610/`, `…_202609/`.

### 2026-10-21 — EXP-0041 stop-run fingerprint (H32)

- Detector: pierce causal level (pullaway swings + locked session H/L + overnight H/L) by 2 ticks → 2s same-side fingerprint (vol≥25, imb≥0.60, sweeps≥1, aggr≥3, score≥2). Optional MBO pull + ES burst. Stamp at window end.
- Stage A: continuation hit10 **0.409** ≈ null **0.394** ≪ 0008 **0.451** (n=44). Fade flip hit10 0.30. `outcome_continue` n=24 hit10 0.58 / @60s +6.0; `outcome_stall` n=20 hit10 0.20.
- Stage B (vol k=1, trend+VWAP, 1-tick slip): continuation n=33 net **+62** (train +21 / May +41) but train loo_neg=4; fade@reclaim gated empty (n=0).
- **Decision:** freeze default thresholds for visual `Stop Run` Bookmap module; **no auto-trade**. See `results/EXP-0041/summary.md`.
- **P&L dig** (`results/EXP-0041_PNL/`): join-run gated vol k=1 n=33 net +62 (train +21 / May +41) but loo_neg=4 and hit10≈null; fade@fire −78; fade@reclaim May flips; SWING_HIGH stratum n=16 +129 too thin (May 5/5). **Still no auto-trade.**

### 2026-10-21 — EXP-0039/0040 re-run without trend+VWAP

- S/R is a different paradigm from frozen MAT gates; both arms re-run with `gate=None` (`--no-gates` → `results/EXP-0039_NOGATE`, `EXP-0040_NOGATE`).
- Ungated base: train n=1131 net **−375**, May n=753 net **−872** (gates were carrying the MAT base).
- **Breakout:** train `through_5` @60s n=58 avg **+0.43** PF 1.06 vs `none` −0.32 / 0.99; May through **−6.32** vs none −1.07 — train lift, May flips → **no ship**.
- **Bounce:** train `bounce_5` n=5 (still ≪30) avg +5.5; May n=5 avg −20. Design B still ~empty (sync_ms confirm). **Reject / thin.**
- Runners: `python -m research.swing_break.run_exp_0039 --no-gates` (same for 0040).

### 2026-10-21 — EXP-0040 MAT bounce at significant S/R

- Opposite of EXP-0039: long off swing LOW / short off swing HIGH; stratify by bounce/at/none after recent hold-side touch.
- Detector touch stamp + `label_signal_sr` in `research/features/significant_swing.py`. Runner: `research/swing_break/run_exp_0040.py`. Spec: `EXP-0040.yaml`.
- **Primary A** empty: train `bounce_5` @60s **n=2**. Funnel: unbroken fade level 647/676 but touch-within-60s only 2 (MAT hold median ~143 pts from level). Dig lookback 5m / pullaway 15 → n≤3.
- **Secondary B:** 574 touches → ≤3 MAT confirms in sync → 0 trades after gates.
- **Verdict: reject / park (empty construction).** May held out; no retune. Results: `results/EXP-0040/`.

### 2026-10-21 — EXP-0039 MAT at significant swing highs/lows

- Question: does frozen MAT (vol k=1, causal, trend+VWAP, UK 14:30–17:00) perform better when it fires at a **20pt-pullaway** swing HIGH (long) / LOW (short)?
- Not EXP-0025 (20s time-confirm, tick units). Detector: `research/features/significant_swing.py`. Runner: `research/swing_break/run_exp_0039.py`. Spec: `research/experiments/EXP-0039.yaml`.
- **Primary A** — stratify every MAT trade by `level_state` (through_B / at / none); buffers 0/2.5/5; break windows 30/60/120/300s (primary 60); single + cluster≥2/3.
- Headline train `through_5` @60s: n=53 avg **−1.51** PF 0.90 vs `none` n=570 avg **+1.36** PF 1.17. May same direction (through worse). Window sweep: 30/60/120 through also worse; 300s train slight lift vs none but May flips — no retune.
- Brackets (rr_1_2, rr_1_3, struct_1_2) on through_5 do not rescue vs sym_k1.
- **Secondary B** (break-first + MAT confirm): single-level B negative; cluster≥2 B train positive but n≤14 and May thin — not a ship bar.
- **Verdict: reject / park.** Do not gate MAT to swing breaks; May held out, unread until end.
- Results: `results/EXP-0039/`.

### 2026-10-21 — May 2026 OOS + skip_range175 risk overlay

- Same frozen base as May OOS (causal, trend+VWAP, vol k=1) plus `make_daytype_skip(max_range=175)` — the 23d overlay that looked best by DD/days+ (`MAT2_OVERLAYS_VOLK1`).
- Runner: `python -m research.run_mat_may --skip-range 175` → `results/MAT2_MAY2026_VOLK1_SKIP_RANGE175/`.
- **vs ungated May** (`MAT2_MAY2026_VOLK1`): n 457→**247** (−46%); net **−264 → −186** (+78 pts); days+ 7→**9**/17; port maxDD 528→**466**; port high +104→+64 (never recovered as far).
- Still a **losing month**. Gate cuts exposure and softens the loss but does **not** flip May; do not ship as a May rescue. Matches the EDGE_RANKING pre-registered expectation (DD/exposure cut, not a sign flip).

### 2026-10-21 — May 2026 OOS (vol k=1, UK 14:30–17:00)

- Converted trades-only for 17 May sessions (`*0526`); ran frozen base (causal, trend+VWAP, vol k=1, 1-tick, 0.25 pt/RT).
- Results: `results/MAT2_MAY2026_VOLK1/`. n=457 net **−263.9**, days+ 7/17, portfolio high +104 then underwater; end −264. Per-day **equity_high** (peak running P&L from flat at 14:30) in summary.
- May is a **losing OOS month** on this base — do not retune; keep as holdout evidence.

### 2026-10-20 — EXP-0037 HV Extreme Move (vol brackets + staged backtest)

- **Java:** `RegimeTracker` + vol-RR brackets in `HvExtremeMoveModule` (`bookmap-orderflow-hvext-vol.jar`). After freeze: defaults updated, `bookmap-orderflow-hvext-frozen.jar`.
- **Python:** `research/features/hv_extreme.py` (tracker port); `research/hvext/{parity_hvext,run_hvext_sweep,run_hvext_validate}.py`; `EXP-0037.yaml` + `hv_extreme` dispatch.
- **Parity (191026):** extracted trades; panel Move20/Confirm10/Band5/HV2/pb5 — **15/15** Python fires matched Bookmap `HV EXT FIRE` tipChart lines (in-extract price range; log extras = other settings/replays/live). Report: `results/EXP-0037/parity_report.md`.
- **Cohorts:** train 9 / validate 5 / final-test 9; **reserve** (untouched): `191026, 250926, 280926, 010926, 020926, 040926, 080926, 100926, 150926`.
- **Stage 1:** 54-cell shape grid (gates off for cache). High-n cells mostly negative on fixed 12.5/30; HV=3 lottery cells n≪20.
- **Stage 1b:** post-filter triage then exact in-loop shortlist; preferred n≥20/40.
- **Stage 2–3:** brackets on n≥40 cells. **Frozen:** Move20 Confirm15 Band2 HV2 pb5 leg60 + **fixed SL10/TP30**. Train n=64 net **+24** PF 1.09; validate n=47 net **+38** PF 1.15 (val_ok). Backup neighbour: same signal pb=0 + fixed 10/30.
- **Stage 4 (once):** final-test n=95 net **−214** PF 0.75 days+ 3/9; slip+1 −248; null control +179. Event-study: hit+10 0.42 / hit+30 0.23; MFE med 15.8 vs MAE 20; @60s −3.5. **Ship threshold failed → park.** Vol-RR did not beat fixed on the high-n shortlist.
- **Decision: park.** Do not autotrade defaults live; jar defaults mirror freeze for replay comparison only. Reserve sessions left for a later OOS if the construction is revisited (e.g. fade-toward-tip instead of continuation, or evening window).

### 2026-10-19 — EXP-0036 correlated exhaustion (first of the correlated series)

- **Plumbing:** `simulate_session(events=..., exit_fn=...)` — events override (verified identical to the detect path on 021026) and a signal-driven force-exit (stop/TP still win ties on the same print). New `research/correlated/{harness,run,exp_0036_exhaust}.py`; `run_experiment` gets one `correlated_*` dispatch. Feature: `research/features/corr_exhaust.py`.
- **Cohorts:** train 9 / hold 5 / new 9 (= 23 sessions; `161026` is in **new**, already consumed as OOS when the vol k=1 base was frozen, so no further holdout exists for this series).
- **Base reproduction:** vol k=1 causal n=502 net **+779.2** (matches `MAT2_OVERLAYS_VOLK1`).
- **Exit (Part A), all 4 bases × 8 variants — reject.** Default rule fires on 65–67% of trades. Matched entries, vol k=1 causal: bracket on fired trades **+1840 gross (209 TP / 114 SL)** → exit **−981**. Net: default −294 vs +779; `imb_only` +193; `wait10` +177; `es_against` (ES imb ≤ −0.3) **+503** — the only variant with days+ = base, but hold −105 and still −276 vs base. Same ordering on confirm and on 12.5/30 (12.5/30 base +570 → best variant +64). Exhaustion is not a reliable early-exit; ES going quiet/against after a confirmed NQ burst does not predict the NQ leg failing within the bracket.
- **Fade (Part B), confirm bursts → first exhaustion bar ≤5 s, side against burst.** `default` n=452 hit10 0.42 ≈ null 0.40, @60s +0.9; `loose_nq` (plan-literal, imbalance-only) ≈ null. `es_against` n=168 (old 99 / new 69): hit10 **0.46 / 0.51** vs null 0.38 / 0.44 vs 0008 0.43 / 0.47; @60s **+1.4 / +5.1**; MFE 16–18 vs MAE 14–17. Event bar technically met on both cohorts but n is thin and it is 1 of 3 pre-listed fade variants.
- **Fade P&L (no trend/VWAP gate):** `es_against` vol k=1 n=143 net **+470** (train +20 / hold +176 / new +275), 15/23 days+, maxDD 153; same events fixed 10/10 **−128**. Default and loose_nq fades negative on all brackets.
- **Decision:** exit idea closed. Fade-on-ES-reversal flagged **dig** (not ship): sweep `es_imb_max` −0.2/−0.4, require min ES vol on the against bar, add trend/VWAP gate, and report ES-leg outcome before any further work. Results: `results/EXP-0036/`.

### 2026-10-19 — EXP-0035 correlated absorption fade

- **Scale check first** (`%TEMP%` script, 3 sessions): ES 100 ms bar vol p50/p95 ≈ 6–9 / 42–49 vs NQ 2 / 11–14; ES trades ≈ 1.7–2.6× NQ; `|net| ≤ 1 tick` holds on 99–100% of ES bars vs 68–80% NQ. A shared `min_vol=25` would be ~p80 on ES and ~p99 on NQ, and "flat" is meaningless on ES at 1 tick. Hence **per-market** thresholds: causal rolling quantile of own-market bar volume over trailing 30 completed minutes (verified: changing bars inside a minute leaves that minute's threshold unchanged), floors NQ 10 / ES 20, flat NQ ≤1 tick / ES ==0. On 021026 the p95 thresholds came out NQ 14 / ES 50 — self-scaling as intended.
- **Plumbing:** `features/corr_absorb.py` (`AbsorbParams`, `rolling_quantile_by_minute`, `absorption_bars`, `level_contact_flags`); harness `cross_match` (generic ES/NQ matcher, `match_col`, confirm/causal stamp) and `level_contacts` (NQ `LargeLevelTracker` contacted levels, cached `results/_cache/<sid>_nq_level_contacts.parquet`; ~30 s/session; 15/23 sessions have depth). `correlated_absorb` dispatch; `research/experiments/EXP-0035.yaml`.
- **Counts (default q95):** NQ absorb bars 7652, ES 7887, **dual 376** (≈16/session); ES-first 31%.
- **Event study (NQ path, fade side):** dual hit10 **0.34** (old 0.33 / new 0.36), @60s +0.5 (old +0.8 / new +0.0), MFE 9.1 / MAE 8.5. NQ-only absorption 0.31 / +0.2. 0008 confirm 0.45 / +0.3. Same-n null 0.30 but noisy; large-n null (EXP-0036 run, n=2133) is 0.40 / +0.6. **Dual ≈ NQ-only** — ES confirmation adds nothing; MFE≈MAE≈9 says these bars occur in quiet tape where neither side goes anywhere in 60 s.
- **Variants:** q90 (dual 874) 0.33; q98 (68) 0.24; flat0 (133) 0.36; range_cap (145) ≈ null; rel3 identical to default (rolling median already implied by q95); b250 (59) 0.32; sync500 (520) 0.34; imb70 (180) 0.33. No variant reaches 0008 or beats the large-n null.
- **Strata:** level_contact old 0.36 (n=150) vs no_level 0.28 (n=95) — small lift, @60s +1.0 vs +0.4, not enough; new-cohort depth n=8/5 (depth missing on new sessions). ES-first slightly better than NQ-first on old (+1.2 vs +0.6 @60s) — thin. Short fades > long fades consistently (@60s +1.3 vs −0.3) — directional drift in the sample, not a rule.
- **P&L (events override):** vol k=1 ungated train **negative for every variant** (default −361, q90 −354, imb70 −117). Only all-cohort-positive cell: `imb70` fixed 10/10 n=166 **+237** (train +46 / hold +90 / new +102) — 1 of 36 cells with hit10 0.33; treat as chance.
- **Decision: reject H28.** Correlated absorption on 100/250 ms bars does not beat NQ-only absorption, null or 0008. Not a ship, not a dig. If revisited, it needs a different construction (multi-second absorption into a tracked level with ES confirming over seconds, not a single bar).

### 2026-10-19 — EXP-0036_DIG (es_against fade dig)

- Runner: same `correlated_exhaust` path with `skip_exit_overlay`, `es_min_vol`, NQ+ES labelling, `fade_pnl_gates: [none, trend_vwap]`. Results: `results/EXP-0036_DIG/`.
- **ES imb sweep (−0.2 / −0.3 / −0.4), NQ outcomes:** tighter is better on path — `against_m04` hit10 **0.51** / @60s **+3.9** (old 0.47 / +2.6; new 0.57 / +6.1, n_new=49). Vol k=1 ungated net m02/m03/m04 = **+444 / +471 / +479**, but **train ≈ 0** (−4…+20) on all three; edge is hold+new. Fixed ±10 still negative (m04 −41).
- **Min ES vol on the against bar kills the edge.** `esvol10` collapses net (m02 −8, m03 +97, m04 +221 with new weak); `esvol25` goes negative. So the “signal” lives on **thin** ES against prints — suspicious, not a liquidity-backed reversal.
- **Trend+VWAP gate:** cuts n by ~⅔; train goes negative; new still strong. Gate concentrates the OOS-looking slice, does not fix train. Not a ship path.
- **ES-leg outcomes:** fade side @10s/@60s mildly positive on ES (+0.2…+0.7 vs null −0.3) — confirms ES really flipped with the trigger — but hit5 only ~0.13 and hit10≈0 (ES rarely moves 10 pts in 60 s here). No tradable ES-leg standalone.
- **Initial close (over-tight):** no-ship on thin-ES + train≈0 + vs-0008 hit rates. **Reopened 2026-10-19:** for a fade, MAT vol-k=1 P&L is the yardstick (0008 is continuation). m04 +479 / m03 +471 / m02 +444 with hold+new carrying train-flat is a live candidate. Exit overlay remains reject — **no need to re-run EXP-0036 Part A**. Dig continues in `EXP-0036_DIG2`.

### 2026-10-19 — EXP-0036_DIG2 (reopened fade: concentration + soft ES vol)

- Results: `results/EXP-0036_DIG2/`. Yardstick = ungated vol k=1 P&L (no trend/VWAP). Soft floors `es_min_vol` ∈ {1,3,5}; hard floors 10/25 already known-bad from DIG1.
- **Soft floors:** `esvol1` ≡ no floor (every against bar already has vol ≥1). `esvol3` / `esvol5` cut net but do **not** kill it — m04: 0→+479, vol3→+353, vol5→+384 (new still +240 / +185). Contrast DIG1 hard floors (≥10) which collapsed the edge. Soft volume is tolerable; hard is not.
- **Per-session concentration (ungated, no floor):**

  | variant | net | train | hold | new | days+ | top3 share of +net |
  |---|---:|---:|---:|---:|---:|---:|
  | m02 | +444 | +21 | +230 | +194 | 15/23 | **0.57** (over bar) |
  | m03 | +471 | +20 | +176 | +275 | 15/23 | 0.51 |
  | m04 | +479 | −4 | +144 | +339 | **16/23** | **0.42** (best) |

  Top days recur: `011026` (hold), `300926`/`151026`/`290926` (new), `050826` (train). Worst: `180926`, `260326`, `121026`, `160926`. Train is not uniformly flat — it is a few big wins vs a few big losses (050826 +79…+92 vs 180926 −73).
- **Ship bar (DIG2):** train+hold+new all >0, days+ ≥14, top3 share ≤0.55, soft floor OK. **Not cleared** — train still ≈0 / slightly negative on m04; m02 fails concentration. **Keep open dig**, prefer `against_m04` (least concentrated, strongest new). Next dig options: drop worst train days as a robustness check (not fitting), side split, ToD within 14:30–17:00, or require NQ still-bursting stricter so train losers filter out.
- **EXP-0036 Part A:** still reject; do not re-run.

### 2026-10-19 — EXP-0036_DIG3 (fade anatomy)

- Results: `results/EXP-0036_DIG3/`. Runner `correlated_exhaust_fade_dig`; events carry exhaustion-bar + `burst_*` fields; `TradeResult.exit_ts_ns` for overlap. Baseline reproduces DIG2: **m04 n=114 net +479** (train −4 / hold +144 / new +339), days+ 16, top3 share **0.42**, leave-one-day-out net<0 = **0**/23.
- **§1 Regime:** range skip gates hurt. **ER skip helps** — `skip_er0.3` n=91 net **+355** (train +6 / hold +123 / new +226), days+ 16, top3 share 0.45 — only slice clearing the pre-registered ship bar, but train is fragile. `skip_er0.4` stronger net (+387) but train −13. Story: fade when efficiency ratio is low (rotational), not when the tape is trending cleanly — opposite of MAT's continuation regime.
- **§2 Anatomy:** exit mix tp 68 / sl 46; bracket size win≈loss (~23 pts); **net on bracket≥20 = +476 (n=81) vs <20 = +32 (n=33)** — vol k=1 is doing the work (full-reversal trade). Fixed ±20 and vol k=1.5 both worse than vol k=1. Event study hit10 0.51 / @60s +3.9.
- **§3 Against gates (hypothesis failed):** `trend_against` / `vwap_against` / both all cut net vs ungated; only `vwap_against` has train>0 (+24) but days+ 13. Continuations-style `trend_with+vwap_with` still +261 but train −34. **Do not gate the fade "against" trend.**
- **§4 Side/ToD:** **shorts carry everything** (short +509 / long −30). ToD: 14:30 and 15:30 strongest; 16:30 weak. Short dominance may be Sept–Oct drift — flag before ship.
- **§5 Trigger quality (signal carries info):** gold cell `wait_500_2000` n=46 net **+411** (train +129 / new +225); `wait_gt2000` kills train (−123). Stronger ES against (`es_imb≤−0.6`) +388 vs mild −0.4..−0.6 +91. Mid NQ-still-pushing best. **Low burst ES vol** (lo tercile) +427 vs hi −78 — thin confirming ES on the *burst* (not the exhaust bar) is the feature. ES-first slightly better than NQ-first.
- **§6 MAT overlap:** fade is **additive**, not just exit-and-reverse. `not_in_mat` +453 vs `in_mat_opp` +27; combined independent books MAT+fade **+1259** vs MAT alone +779. `after_mat_sl` is rare (13%) but very high WR when it fires (n=12, +236).
- **§7 Robustness:** loo all positive. Neighbourhood `es_imb_max=-0.45` and `nq_imb≥0.65` look stronger on train — treat as exploratory (post-hoc), not a retune. 2-tick slip still +451. `wait10` blows up new but wrecks train.
- **Decision: keep open dig.** Baseline does not clear train>0. `skip_er0.3` clears the letter of the bar with train +6 only — confirm on a frozen holdout / DIG4 before any ship. Do **not** stack short-only + wait 0.5–2s + ER gate in one step (fitting). DIG4 candidates (one at a time): (a) freeze `skip_er0.3` and re-check on any new sessions, (b) report short-only as a stratum not a filter, (c) wait∈[500,2000] as a soft quality flag.

### 2026-10-19 — EXP-0036_DIG4 (ER-gate freeze confirmation → test auto-trade)

- Results: `results/EXP-0036_DIG4/`. Runner `correlated_exhaust_fade_freeze`. Pre-registered: each ER neighbour must clear train∧hold∧new>0, days+≥14, top3≤0.55, LOO_neg≤2; freeze only if **0.30 and ≥1 neighbour** clear.
- **Neighbours (1-tick):** ungated +479 train−4 (fails); `skip_er0.25` n=80 **+311** train+20/hold+146/new+145 **cleared**; `skip_er0.3` n=91 **+355** train+6/hold+123/new+226 **cleared**; `skip_er0.35` n=97 +387 train−13 **fails**. LOO_neg=0 on all gated; top3 ~0.45–0.49.
- **2-tick slip on 0.3:** net +332 but train −1 → does not clear ship bar; live fill quality is the main risk.
- **Decision: FREEZE for TEST_AUTOTRADE.** Spec: `results/EXP-0036_DIG4/TEST_AUTOTRADE_SPEC.md` — against_m04 fade, ER≤0.30, UK 14:30–17:00, vol k=1 (DIG4 sample used symmetric TP=SL; live default later moved to asym SL1R/TP2R — see 2026-10-20 note), 1 contract, kill −150 / 3 losing sessions. No further param search on these 23d.
- **Add-on shipped (same day):** `Correlated Fade AutoTrade (ES+NQ)` — `bookmap-addon/.../trade/CorrFadeAutoTradeModule.java` over Bookmap-free `core/CorrFadeDetector.java` (+ `core/TapeBarSeries.java`); JAR `build/libs/bookmap-orderflow-cfade.jar`. Separate book from MAT (no shared side filter). Defaults = frozen spec; trading OFF. Pairing is by bar **stamp** with short burst histories on both legs — Bookmap bars close lazily, and the clock-based MAT-style pending/expiry dropped 2 of 132 research confirms before the fix. **Offline parity on all 23 sessions: 132/132 fades exact** (stamp/side/es_imb_x/wait/close) and identical er60 + bracket at every fade. Replay reference: `python -m research.export_cfade_ref --session <sid>` → `results/CFADE_REF_<sid>.md`. Not automated: 17:00 flatten, 3-losing-sessions stop.

### 2026-10-20 — CFADE live defaults → vol RR SL 1R / TP 2R

- Backtest: `results/CFADE_ASYM_RR/` (`make_vol_bracket_rr`). Best practical cell **sl1/tp2** (same stop as DIG4 sym k=1, wider target); lifts Oct/Sep/Aug vs symmetric.
- **Live defaults updated:** panel `SL R multiple=1`, `TP R multiple=2`, bracket clip **4–40**; JAR `bookmap-orderflow-cfade-v4.jar` (+ overwritten `bookmap-orderflow-cfade.jar` when unlocked). Spec/README/`export_cfade_ref` aligned. Signal rule unchanged.

### 2026-10-21 — CFADE May 2026 OOS (sl1/tp2)

- Runner: `python -m research.run_cfade_may` → `results/CFADE_MAY2026_SL1TP2/`.
- 17 May sessions, UK 14:30–17:00, against_m04 + skip_er0.3, vol RR SL1/TP2, 1-tick + 0.25 pt/RT.
- **Feed fix:** extracts often have both `NQM6@TV` + `NQM6.CME@RITHMIC` with non-aligned timestamps; merging invented fades. `harness.load_pair` now keeps **primary alias only** (matches live/replay). Re-run: n=95 net **+335.1**; **210526 n=6 net −45.9** (matches Bookmap replay trade count). Prior merged figure n=151 / +349.8 was inflated.

### 2026-10-19 — Audit: ideas discounted vs EXP-0008

Same mistake pattern as the first EXP-0036 fade close: treating continuation hit rates (esp. the void look-ahead **85%**) as the success bar for fades / path overlays / alternate entries, then walking away without MAT vol-k=1 P&L.

**Rule:** 0008 is the right bar only for *continuation overlays that claim to beat the lead*. Fades / alternate entries use **null + MAT P&L**. Path-strong thin ideas get a P&L dig, not a hit% kill.

| Idea | Why it looked like the 0036 mistake | Stay closed? | Dig action |
|------|-------------------------------------|--------------|------------|
| EXP-0036 fade | Hit ≪ 0008; P&L +479 | No — **frozen DIG4** | Test auto-trade (spec) |
| EXP-0035 absorb fade | Framed ≪ 0008 | **Yes** — dual≈NQ-only + train P&L all negative | No reopen |
| **EXP-0014** CD×mom | Pre-fix: nice path but hit **loses to 0008**; demoted to gate; gate new −182 | Gate stay closed | **Standalone** causal CD×mom events → vol k=1 P&L (23d). Yardstick = P&L vs null, not vs 0008 hit |
| **EXP-0017** sweeps | Beat null 52% vs 43%; killed solely vs 0008 **85%** | Unknown | Points event study (60s) → if still beats null, MAT P&L as alternate entry |
| **EXP-0020** dual_magnet | Points: n=20 hit10 **0.70** / @10s **+6.2**; parked “dig only”, never dug | Pressure stay closed | Tiny-n MAT P&L dig (expect thin; one shot) |
| **EXP-0033** high-vol | Beat null / ≈0008 in ticks; parked “no holdout”; base flow later called dead vs 0008 | Base flow stay closed | High-vol+price stratum as MAT overlay / alternate entry on 23d P&L |

**Not the same mistake (correctly closed):** fades ≈null in points (0005/0006/0011/0016/0019/0026/0027/0031); continuations ≈null (0029/0032/0018/0021); swing-break 0025 no hit lift in points; CD *gate* on MAT (new cohort disagree). Pre-fix “≪0008 85%” rows that also fail null stay dead without a dig.

**Dig backlog order (EXP-0036 fade frozen; next reopen digs):**
1. EXP-0014 standalone CD×mom MAT P&L
2. EXP-0017 points re-run → conditional P&L
3. EXP-0033 high-vol holdout P&L
4. EXP-0020 dual_magnet one-shot P&L (lowest priority)

### 2026-10-18 night — time-of-day (vol k=1, not 12.5/30)

- **Yardstick:** same frozen base — **vol k=1** symmetric brackets, trend+VWAP, causal ES, 1-tick slip, 0.25 pt/RT. Window extended to UK **14:30–21:00** for ToD only.
- Results: `results/MAT2_TOD_VOLK1/`. Edge concentrated 14:30–16:30; first 5m strong (+134); 14:35–14:45 and close 20:50–21:00 weak. See summary for half-hour/hour tables.
- First-5m indicator param sweep: `results/MAT2_TOD_FIRST5_SWEEP/` (vol k=1 base).
  - BASE first-5: n=71 net **+134** (old +83 / new +52).
  - Loosening burst (`min_vol=15`, lower imb) or tightening (`min_vol≥40`, imb≥0.65, progress≥2, bar 50/200) **hurts**.
  - Mild lifts: `signal_cooldown=0` (+160), vol k≥1.5 at cap (+138). Fixed **±25** looks best on first-5 (+300) but is bracket fitting on n=71 — treat as exploratory, not a ship.
  - `gate=off` +259 but **new −14** — reject. Confirm stamp weakens open. **No indicator retune for open** beyond optional cooldown=0 dig.

### 2026-10-18 night — OOS + post-fix exploration (base = vol k=1)

- **OOS (9 new sessions):** `results/MAT2_ALL_SESSIONS_VOLK1/` (also mirrored under `MAT2_ALL_SESSIONS_TP12p5_SL30/`).
  - **vol k=1** causal trend+VWAP UK1430–1700 1-tick: old +432 gross / **new +472** (PF 1.22) — **generalised**. Net after 0.25 pt/RT: old +359 / new +420.
  - TP12.5/SL30: old +580 / **new +130** — ~75% OOS degradation; keep as comparator only.
  - fixed ±10: noise.
- **Decision: frozen base = vol k=1** (`make_vol_bracket(1.0)`, clip 6–30). Live: enable Vol-scaled + **k=1**.
- **Data hygiene:** `290926` vs `300926` are **not** duplicates (Sep 29 vs 30; different trade counts / P&L). Prior identical rows were a runner bug; corrected per-session table shows 290926 −168 net as the worst new day (drives much of new-cohort DD).
- **NQ-first (confirm):** Java `ES confirm mode` + `bookmap-orderflow-mat-v2-confirm.jar`; refs `MAT2_REF_*_confirm_vol_k1`. On vol k=1 across 23d, confirm **does not** lift vs causal (net +586 vs +779) — lift previously seen on 12.5/30. Ship param; default remains causal=0.
- **Exits** (`MAT2_EXITS_VOLK1/`): time/BE/trail variants on vol k=1 — none beat base net on both cohorts; trail0.5 cuts loss-tail to 0 but net collapses. No ship.
- **Risk gates / CD** (`MAT2_OVERLAYS_VOLK1/`):
  - Best by DD/days+: `causal_skip_range175` (maxDD 206, days+ 17, new +492) and `confirm_skip_range175` (maxDD 172, days+ 17, new +494). lag100 alone hurts new cohort.
  - **CD pullback gate:** old +165 / **new −182** (causal) — new-9 disagrees → **do not ship**.
- **Stop list (do not re-run):**
  - H2/H7/H11/H15 depth fades (≈ null on 14d depth in points).
  - Swing-break, rotation, BB-regime filters (no hit lift in points).
  - Fixed TP/SL grid search — 12.5/30 did not generalise; do not re-tune fixed brackets.

### 2026-10-18 evening — methodology fix + shortlist re-run
- **Yardstick:** `label.py` path in chart points (`×0.25`); horizons to 60 s; targets 5/10/20/30 pts.
- **Stamps:** `cross_confirm_bursts` default `stamp=confirm`; CD pullback filter causal; dual range stamp at `max(nq,es)`.
- **Depth:** all 14 sessions + 161026 (OOS) have depth/mbo parquet matching `meta.json` counts.
- **EXP-0008 corrected (14d, confirm stamp):** n=2252, `p_hit_5pts`≈0.53, `p_hit_10pts`≈0.44, `p_hit_20pts`≈0.24 @60s; mean@10s ≈ +0.32 pts.
- **MAT overlays TP12.5/SL30 trend+VWAP UK1430–1700 (1-tick, 14d, 161026 OOS):** see `results/MAT2_OVERLAYS_12p5_30/`.
  - `base_causal` n=312 net **+580.5** (train +397 / hold +183.5)
  - `base_confirm` (NQ-first) n=341 net **+723.2** (train +435 / hold +288)
  - `causal_lag100` net **+608.5**; `causal_skip_range175` net +508.8 (10/14 days+)
- Shortlist event-study (points, 60 s) — full dump `results/SHORTLIST_RERUN_POINTS.md`:
  - **0008** n=2252 hit+10 **0.44** / hit+5 0.53 / @10s +0.32 / @60s −0.61
  - **0025** swing-break n=470 hit+10 0.42 ≤ all_lag250 0.44 — **no hit lift** in points (path @10s slightly better)
  - **0024** es_leads/nq_leads/simult hit+10 ≈0.45/0.45/0.43 — tick-unit “NQ-leads 78%” **gone** in points; MAT `es_stamp=confirm` still lifts P&L (+723 vs +580)
  - **0014** CD×mom confirmed n=49 hit+10 **0.55** / @60s **+7.8** — thin but best path; causal stamp
  - **0005** absorb n=276 hit+10 0.37 ≈ null — still no edge on 14d depth
  - **0006** replenish n=218 hit+10 0.34 — no lift
  - **0011** dual LVN n=165 hit+10 0.39 ≈ null 0.37 — still no
  - **0016** dual challenge narrow n=2 — empty
  - **0020** NQ pressure n=2057 hit+10 0.43 ≈ null; dual_magnet n=20 hit+10 0.70 / @10s +6.2 — **tiny**, dig only

### 2026-10-09 — H27 simultaneous ES+NQ aggressor flow (same clock)
- Detector: both markets same-side aggressor imbalance in the **same** wall-clock tape bar (`research/features/simultaneous.py`). Also dual H26 range sync; compare to EXP-0008 |lag|&lt;50ms.
- **Primary b100 flow-only:** n=467 hit+10 **73%** / @10s **+5.7** / MAE 9.8 vs null **43%** / EXP-0008 lag250 **85%** / **+9.8**. Beats null; does not beat the lead.
- **+ same-sign price in bar:** b100 n=200 **84%** / **+9.6**; b250 n=346 **87%** / **+9.2**; b500 n=396 **86%** / **+8.8** — parity with 0008, not a lift.
- **Dig high-vol (nq≥40) + price:** b100 n=73 **92%** / **+14**; b250 n=138 **90%** / **+13** — best cells, in-sample only (thin vs 0008 n=248). Park as candidate overlay; **no ship** without holdout.
- **0008 |lag|&lt;50ms:** n=156 **87%** / **+11.1** / MAE 7.5 — already a “same time” reading of the lead; slightly better path than lag250 on these days.
- Dual range: r22 empty; r10 sync250 n=54 hit **80%** path fat / MAE 18 — not competitive.
- **No new ship.** Lead remains EXP-0008 + lag&lt;250 (optional tighten to lag&lt;50).

### 2026-10-09 — H26 range-bar aggressor imbalance vs prior ranges
- Detector: completed tick-range bars with buy/sell/total vol + duration vs rolling prior avg (`research/features/range_imbalance.py`). Continuation with imbalance side; gates: opp-side below avg, ±speed, ±total vol, with-bar.
- **Primary (22-tick, opp_low, mi=0.30):** n=789 hit+10 **44%** / @10s **−1.8** / MAE 18 vs null **46%** / EXP-0008 lag250 **85%** / +9.8 / MAE 9.
- Range sweep: r5 n~31k **44%**; r10 n~6.6k **45%**; r22 **44%**; r50 n=54 **48%** but @10s **−9**. Speed/vol gates do not lift; fast+hivol often worse path.
- Dig min_imbalance 0.2–0.5 and avg_n 10/30: largest n cells stay ≤47% hit with negative path. No ship.
- Lead remains EXP-0008 + lag&lt;250.

### 2026-09-24 — H25b recalibrated trapped peak
- Rewrote detector to user gates: **above-avg 250ms cluster vol** at local extreme + **50-price LVN band** (center ≤0.55× shoulder) + **≥20pt pullback in 5–30s** with clusters ≤45% of peak and no reclaim (`research/features/trapped.py`).
- **Default:** n=26 (shorts 11) hit+10 **27%** / @10s **−3.3** vs null **44%** / EXP-0008 **85%**. Longs 53% / +4.4 — not the chart side. Mean quiet ~8s; vol_mult ~11×.
- Dig vol_mult / LVN frac / quiet-vs-peak / pullback 10–20: shorts stay weak; best **15pt + quiet0.70** n=29 hit **55%** path ~0. Still no ship.
- Prior long-quiet/depth version was empty — this coding *identifies* the pattern but finds no continuation edge on UK 14:30–17:00 days.

### 2026-09-24 — H24 average rotation length filter
- Measure: confirmed swing-to-swing lengths → rolling avg (last 8); open-leg extension / avg (`research/features/rotations.py`). Momentum keep if ratio &lt; mult and with-leg; fade keep if ratio ≥ mult against-leg.
- **Default (3m swings, avg_n=8, mult=1):** lag&lt;250 n=248 hit+10 **85%** / @10s **+9.8**. Fresh-with-leg n=102 **83%** / **+6.6**. Extended n=39 **90%** / **+13** (lower MAE). Filter as specified **hurts** the lead.
- Absorb extended fade n=7 hit+10 **29%** / @10s **−8.6** vs all absorb 40%. Not a reversal rescue.
- Mult dig 0.5–1.5: extended stratum always ≥ fresh on hit and path. Mean avg rotation ~149 ticks; median ratio ~0.54.
- **No ship.** Optional note: *prefer* extended for momentum is in-sample curiosity only — do not invert the hypothesis into a ship without holdout. Lead remains EXP-0008 + lag&lt;250.

### 2026-09-24 — Wider stop/targets (±10…±40, max path 60s)
- Relabel saved events via `research/dig_wide_targets.py` → `results/WIDE_TARGETS/`. Same event sets; only race width and horizon change.
- **EXP-0008 lag&lt;250 (n=248):** WR 88% @±10 → 70% @±20 → 67% @±30 → 67% @±40. Resolve falls (100%→70% by ±40). Mean@60s **+9.5** / med **+5**. Edge **survives** wider 1:1; hit rate does not *improve* (harder targets). Still ≫ null (~53% WR @±20, ~47% @±40).
- **EXP-0025 swing-break (n=69):** stays best overlay at every width (WR 91/74/73/76); MAE 20 vs 0008’s 28.
- **Partial rescues (not ship):** EXP-0011 LVN n=28 WR 44%→**74% @±20**; EXP-0005 absorb n=57 47%→60% @±30. Classic “±10 noise stop.” Still below 0008 and thin n / incomplete resolve at ±40.
- **EXP-0028 catch-up (n=12):** WR 42%→**83% @±30–40** — dramatic but **n too small**; do not revive.
- **Still dead at all widths:** null, co-move, sweep, value-depart, book imbalance.
- **No new ship.** Lead remains EXP-0008 + lag&lt;250. Optional: use ±20–30 as live sizing once holdout days exist; ±10 remains the tight discovery yardstick.

### 2026-09-23 — H22/H23 correlation-first (catch-up + co-move)
- **EXP-0028** ES moved, NQ still quiet → trade NQ with ES. Default 1s / ES≥6 / NQ≤3: n=12 hit+10 **42%** ≈ null. Shorter window n=20 hit **55%** but @10s mean ~0. High recent sign-agreement does not rescue. **Not a strategy.**
- **EXP-0029** both markets same-sign move ≥4 ticks / 500ms: n=206 hit+10 **50%** ≈ null **46%**, median @10s **−1**, MAE **18** vs EXP-0008 **85%** / MAE **9**.
- Lesson: the 0008 edge is **correlated aggressive tape**, not “prices moved together.” No ship.

### 2026-09-23 — H21 flush into resting liquidity + hold
- Detector: flush ≥N ticks in 2s into large level, hold (limited penetration), reclaim → bounce (`research/features/flush_hold.py`).
- **Default (80 ticks ≈ chart-size):** n=0 on UK 14:30–17:00. Note: example Bookmap screenshot was ~18:25 UK (outside primary window).
- Dig: flush40 n=1; flush40_soft n=4; **flush20_soft n=29 hit+10 41% ≈ null 40%** (med @10s 0). Dual always 0. Chart-size 120 empty.
- **No ship.** Visual sweep-reverse not measurable as edge on these RTH days. Lead remains EXP-0008 + lag&lt;250.

### 2026-09-23 — H18 / H19 / H20 next-strategy batch
- **EXP-0024:** Signed ES lag on lag&lt;250. ES-leads n=64 hit+10 **81%**, @10s **+7.1** vs all **85%** / **+9.8**. NQ-leads / simultaneous not worse. **Falsified** as a filter. No ship.
- **EXP-0025:** Causal swing break-through on lag&lt;250. Default 3m: break n=69 hit+10 **88%**, @10s **+11.2**, MAE 6.7 vs all 85% / +9.8 / MAE 9.3. Dig lookback: 2m ≈ null lift; **5m** n=52 hit **92%** / +12.9. Directional but **in-sample only** — park as candidate overlay; **no Bookmap ship** until more RTH days.
- **EXP-0026:** Dual failed swing-break n=7 &lt;15 → **empty stop**. NQ-only n=439 hit+10 **41%** ≈ null. No loosen. No ship.
- Lead remains EXP-0008 + lag&lt;250; next confirmation = more extract days.

### 2026-09-23 — H7b tightened LVN (HVN sandwich + min vol + no edge)
- LVN now requires: min lookback volume, **HVN above and below** (≥50% POC vol within 20 ticks), reject zones within 6 ticks of profile hi/lo (`volume_profile.py`).
- **Default (30m, frac=0.35):** dual n=0; NQ-only n=28 hit+10 **39%** ≈ null **42%** ≪ EXP-0008 **85%**.
- Dig lookback 20/30/45 × frac 0.25/0.35/0.40 (+ min_vol 200/500/1500): dual only fires once (20m/0.40 n=2). NQ-only hit+10 stuck **~31–44%** ≈ null. No ship.

### 2026-09-23 — H17 dual ES+NQ CD trend / BB filter
- Filter EXP-0008 lag&lt;250 bursts by causal CD on **both** ES and NQ (trend aligned + BB regime).
- **Matched definition (22-tick / SMA100 both):** ES CD almost always flat at events (trend≠0 only 6.5%; BB pct never finite) → dual_trend n=5, dual_wide n=0. NQ-only trend n=119 hit+10 **85%** ≈ unfiltered **85%**.
- **One dig — denser ES CD** (range 5–10, SMA 30–50) so dual is observable: dual_trend / dual_wide hit+10 ~87–94% but mean @10s usually **≤** unfiltered (+9.8). dual_trend∩wide n=6–24 too thin / param-sensitive.
- **No ship.** Correlated CD does not improve the tape lead on these days. Lead remains EXP-0008 + lag&lt;250.

### 2026-09-23 — H16 dual micro-regime (attract→take + opposite hold)
- Causal legs: aggressive take through large level (continuation) + opposite-side hold-wo-trade; pair within 3s; dual ES+NQ.
- **Default composite:** dual n=0, NQ composite n=0 (takes rarely co-occur with holds). Dual hold-only n=9 hit+10 **22%** &lt; null.
- Sensitivity dig: even soft take (cons≥0.3, no attract, NQ size 25) → dual composite still **0**. Dual take-only n=109–495 hit+10 **35–52%** ≈ null, path ≤0. ≪ EXP-0008 **85%**.
- **No ship.** Lead remains EXP-0008 + lag&lt;250.

### 2026-09-23 — H15 near-book depth imbalance
- L2 band imbalance `(ask_near − bid_near)/(ask+bid)` within X ticks of mid; edge-trigger on |imb|≥thr (`research/features/book_imbalance.py`).
- **Pressure dual** almost empty (n=2). **NQ-only** n=201 hit+10 **44%** ≈ null **43%**; path flat/negative.
- Sensitivity (thr 0.30–0.45, radius 5–20): NQ hit+10 stuck ~43–44%. Dual n=25 at thr=0.30 still weak. Magnet dual n tiny.
- **No ship.** Lead remains EXP-0008 + lag&lt;250.

### 2026-09-23 — H14 leave-value port (trade-processor S20/S4)
- Ported causal developing POC + 22-tick S20/S4 detectors (`research/features/value_depart.py`) from trade-processor `nq_cd_tester.py`.
- **S20** n=35 hit+10 **40%**, @10s −2.2 med −7. **S4** n=64 hit+10 **36%**, @10s −5.3. Null ~46%; EXP-0008 lag&lt;250 **85%**.
- Dropping CD-regime / price-confirm gates does not rescue (S20 still ~39–40%). ES∩momentum near almost empty.
- Interpretation: those edges were validated on **multi-point stop/target P&amp;L** over range-bar holds, not 250ms–10s MFE/hit+10. No Bookmap ship for leave-value on this scalp yardstick.

### 2026-09-23 — H12/H13 aggressor sweep + MBO vacuum
- Built `aggressor_order_id` sweep detector (`research/features/sweeps.py`) and MBO cancel vacuum (`research/features/mbo_vacuum.py`; cancels resolved via send order_id).
- **EXP-0017:** dual sweeps n=311 hit+10 **52%** (NQ-only 34%, null 43%, EXP-0008 lag&lt;250 **85%**). Fails success bar vs 0008. Span/print dig not needed to overturn.
- **EXP-0018:** dual vacuum n=5245 hit+10 **40%** ≈ null **41%**. No predictive content.
- **No Bookmap ship.** Lead remains EXP-0008 + lag&lt;250.

### 2026-09-23 (morning) — CD BB regime + challenge-without-trade
- Causal BB-width percentile regime on 22-tick CD (`annotate_bb_regime`: wide≥p70, narrow≤p30).
- **EXP-0015:** On lag&lt;250 momentum (n=248), wide n=105 hit+10 **87%** vs narrow n=68 **85%** vs all **85%**. Path not better in wide (med @10s +5 vs narrow +7). **BB width is not a useful momentum filter on these days.**
- **EXP-0016:** Dual challenged-but-barely-traded fades n=14 hit+10 **21%**, @10s −4.6. Almost all in wide/mid — **narrow dual n=0** so H10∩H11 untested. Near-LVN subset worse (14%). Flip-to-continuation also fails (~29%).
- Dual LVN (EXP-0011) × narrow: n=8 hit+10 50% vs 39% all — tiny, not enough to revive H7.
- **No ship.** Lead remains EXP-0008 + lag&lt;250.

### 2026-09-23 — follow-up leads (no new days)
- **EXP-0014:** CD pullback as *primary* entry is weak (n=270, hit+10 46%). Intersection with ES+NQ momentum (n=14) has nice path (@10s +12.5 med +11) but hit+10 64% **loses** to raw momentum 82%. Keep CD as optional timing gate on momentum, not the event source.
- **EXP-0013 / H9:** LargeLevelTracker almost never closes NQ levels uncontacted at abs≥50 → dual dwell/withdraw untestable here. Falsify/park.
- **EXP-0008 overlay dig (best lead):** ES lag &lt;250ms n=248 hit+10 **85%**, @10s +9.8 med +7 vs lag≥250 n=192 hit 78%, @10s +0.6 med −1. Stable on 4/5 sessions. CD pullback on momentum still best *path* overlay (n=33, @10s +11.7 med +11, hit≈82%). lag∩pullback n=20 @10s +15.8.
- **Ship tweak:** Momentum AutoTrade default `maxEsLagMs=250` (was 0=off). JAR rebuild: `bookmap-orderflow-lag250.jar` (main jar locked by running Bookmap).

### 2026-09-23 — H7/H8 correlated LVN reversal (first try)
- Built causal VP + LVN (`research/features/volume_profile.py`) and dual-touch detector (`research/events/lvn.py`); wired EXP-0011/0012.
- Zone counts sane (~3–4 active LVNs per refresh; hundreds of single-market touches / session).
- **EXP-0011** dual n=28 across 4 sessions (210926 none): hit+10 **39%** vs NQ-only **40%** (n=777) vs random null **38%** (n=250). Mean path late-positive but medians weak; **no lift**.
- Dig: shorts n=11 look better (hit+10 55%, @10s +17) vs longs n=17 (29%, @10s −1.5) — not enough to retune on these days.
- **EXP-0012:** beyond∩flip gated **0**; flip never fires; beyond-only n=3 all 170926 15:04–15:05 shorts (one impulse).
- **Decision:** falsify H7 on this sample; H8 not testable/validated; **no Bookmap LVN indicators**. Lead remains EXP-0008.

### 2026-09-22 (night) — momentum tighten without new days
- Pure depth take-through (pen>=1) almost never fires on NQ; hybrid = tape burst + level executed-into.
- **0008b** n=7 collapses after early MFE; **0008d** n=44 hit+10 73% worse than tape-only 82%.
- **CD on 0008:** pullback-to-MA (n=34) improves path (@2s +13, @10s +11.6 med +8.5) without lifting hit rate; with-flow trend alone does not help.
- Working lead remains tape EXP-0008; best no-new-data overlay = CD pullback timing.

### 2026-09-22 (night) — H4 ES+NQ momentum (first try)
- **Not tried before.** Tape-based EXP-0008: 100ms one-sided fast bursts on NQ with same-side ES burst within ±1.5s; continuation outcomes.
- n=440 across 5 sessions. Hit+10 **82%**, hit+20 51%, mean path ~+7–8 pts through 2s, still +5.8 @10s (med +3).
- Baselines: random active bars hit+10 ≈49%; NQ-only bursts ≈67%; ES confirm lifts ~15pp. Lag&lt;250ms subset hit+10 86%.
- Caveat: tape proxy (not depth “large order taken” yet); same 5 days = no holdout.
- **Next:** more RTH days + holdout; optional depth-confirmed large-take tighten; try CD with-flow on momentum (not absorb).

### 2026-09-22 (night) — H6 CD filter (first try)
- Built 22-tick range CD + SMA100 + BB + MA-trend / pullback-to-MA (`research/features/cd.py`). Price column is tick units → `tick_size=1.0`.
- Applied as post-filter (EXP-0009) to EXP-0005 / 0002 / 0002b.
- **H2 absorbs:** with-flow CD trend **hurts** (n=28, hit+10 32%, @10s −3.9). Against-trend (n=24) hit+10 50%, @10s +13 med +9.5. Pullback∩absorb rare (n=4).
- **H1 gated longs:** trend-aligned n=5 mild; pullback n=1 (= best E3).
- Interpretation: absorb-fade ≠ with-the-flow. Next H6 should **emit CD pullback events first**, then measure OF confirmation — closer to discretionary use.
- **Next:** more RTH days; optional CD-pullback event study; do not retune on these 5.

### 2026-09-22 (night) — H2 absorption / replenishment
- Ported `LargeLevelTracker` to Python; wired EXP-0005 / EXP-0006 on depth+trades.
- Disabled z-score gate (z=99): default z=2.5 flooded ~10-lot noise (thousands of false absorbs). Absolute size ≥50 used instead.
- **EXP-0005** n=57: P(hit+10)=0.40, mean path −1.1@250ms → +0.8@2s → +3.6@10s (median 0). **No edge vs null on this sample.**
- **EXP-0006** n=50 replenish≥1: slightly worse / no lift. Exploratory only: no-replenish (n=7) and consume≥1.5 (n=10) look stronger — retest after more days, do not retune on these 5.
- **Next:** more RTH extracts from user; then re-run 0005/0006 and optional BID-only / high-consume filters. H3 attract/repel/dwell still pending.

### 2026-09-22 (late) — gated re-run
- Detector gates: `forbid_flat_bar`, `require_counter_ticks`, `trade_sides`.
- **EXP-0002b** (long-only absorption): n=7, all `sell_into_upticks`, matches dig clean set. Mean path +7.4@1s / +9.3@2s / +0.1@10s.
- **EXP-0002c** (both sides gated): n=14 (removed E7/E14 flats). Shorts still ~flat early path.
- **Next fork:** more RTH days, OR multi-bar effort structure tighten, OR move to H2 liquidity/absorption events (likely higher n).

### 2026-09-22 (evening) — dig into the 16 events
- Bar-level review of all 16 EXP-0002 fires: **most are ε-spikes** (vol≥25 with 0 progress ticks → effort≥50), not multi-bar rising-effort / collapsing-result.
- Split: **7 absorption-like LONGs** (sell into upticks) mean +9.3 pts @ 2s; **9 noisy SHORTs** (flat/zero-uptick buy bursts) mean ~0 @ 2s. Both fade by 10s.
- Standouts: E3 best path (MAE 0); E1 trap (+23 then −25 by 10s); E7 delayed lucky SHORT after flat bar.
- **Next:** gate ε-spikes (require progress ticks / counter-tick prints); treat long vs short separately; do not loosen detector until filter is fixed.

### 2026-09-22 (evening)
- Five real extracts converted to Parquet: `160926`, `170926`, `180926`, `210926`, `220926`.
- EXP-0002 on RTH UK window: **16 exhaustion events** across 5 sessions.
- Short-horizon mean returns positive through ~2s then fade by 10s; **n is far too small** for claiming edge.
- **Next:** loosen/tune detector for more events; run absorption/attract-repel experiments; add proper baseline/null; note `210926` misses RTH open.

### 2026-09-22 (earlier)
- Research scaffold + Session Extractor + Parquet converter + EXP-0002 CLI shipped.
- Feeds inventory: 335 `.bmf` files (~142 GB) under `C:\Bookmap\Feeds`.
- Synthetic smoke validated the pipeline.

### 2026-10-20 — EXP-0038 HV Extreme redesign series

- Package: `research/hvext/redesign/` (enrich + runner + stages P1–P8).
- **Freeze:** `ret|retest_R10_t300_sb2_x3.0|none` Sep n=83 net=65.5 / Oct n=142 net=389.0 / both=454.5 (pass).
- **Neighbour:** `ret|retest_R8_t300_sb4_x3.0|none` both=426.75.
- Key finding: extension entry stays negative; **retest entry** (pullback to tip) clears both months. Loud-leg / tip-order gates help extension but thin when stacked on retest.
- Java: retest mode defaults in `HvExtremeMoveModule`; jar `bookmap-orderflow-hvext-v2.jar`.
- Results: `results/EXP-0038/`.

