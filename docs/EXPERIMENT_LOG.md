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
| EXP-0014 | H6 CD pullback primary | re-run 14d (see notes) | 14d | causal confirm window |
| EXP-0015 | H10 CD BB regime × momentum | needs re-run (units/comparator) | 5d→14d ticks | wide≈narrow; non-monotonic in 14d tick re-run |
| EXP-0016 | H11 dual challenge-wo-trade ± LVN × narrow BB | re-run 14d (see notes) | 14d | |
| EXP-0017 | H12 aggressor sweep momentum | needs re-run (units/comparator) | 5d | dual hit+10 52% vs null 43% vs EXP-0008 lag250 85% — worse than bar bursts |
| EXP-0018 | H13 near-BBO MBO vacuum | needs re-run (units/comparator) | 5d | dual hit+10 40% ≈ null 41%; n=5245 no edge |
| EXP-0019 | H14 VPOC/HVN departure (S20/S4 port) | needs re-run (units/comparator) | 5d | S20 hit+10 40% / S4 36% ≤ null 46% vs old 0008 85%; scalp horizon ≠ TP 25pt holds |
| EXP-0020 | H15 near-book depth imbalance | re-run 14d (see notes) | 14d | |
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
| EXP-0033 | H27 simultaneous ES+NQ aggressor flow | needs re-run (units/comparator) | 5d→14d ticks | dual range stratum was look-ahead; stamp fixed |
| EXP-0034 | H4 MAT gates | ran | 14d US RTH | trend+VWAP lift; see results/EXP-0034 |

## Run notes

### 2026-10-18 night — time-of-day (vol k=1, not 12.5/30)

- **Yardstick:** same frozen base — **vol k=1** symmetric brackets, trend+VWAP, causal ES, 1-tick slip, 0.25 pt/RT. Window extended to UK **14:30–21:00** for ToD only.
- Results: `results/MAT2_TOD_VOLK1/`. Edge concentrated 14:30–16:30; first 5m strong (+134); 14:35–14:45 and close 20:50–21:00 weak. See summary for half-hour/hour tables.
- First-5m indicator param sweep: `results/MAT2_TOD_FIRST5_SWEEP/` (follow-up).

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
