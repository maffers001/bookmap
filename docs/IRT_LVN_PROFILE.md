# IRT LVN / HVN profile — reverse-engineer findings

Investigation of why our offline LVN stack did not match Investor/RT
`*_ALL.csv` exports (`ESZ6_ALL.csv`, `@NQ#_ALL.csv`). Detail and diagnostic
runs: `results/LVN_PROFILE_VALIDATE/`.

**Production decision (2026-10-29):** both ES and NQ stacks stay **tick-built**
and consistent with each other. We do **not** switch ES to IRT’s 1‑minute
bar-smear construction just to chase export parity. Matching IRT is a
diagnostic target; Bookmap/research levels use one coherent definition.

---

## Layman summary

A volume profile answers: *at each price, how much traded there?*

- **Tick-built (what we want for both markets):** every print goes onto its
  exact price. Same recipe on ES and NQ.
- **What IRT appears to do for ES:** for the ESZ6 lookback it often only has
  **1‑minute bars**. A bar knows Low, High, and total volume, but not how that
  volume split across prices inside the bar. So IRT **spreads** the bar’s
  volume across every price between Low and High, then stacks those minutes
  into a profile. Different input → different valleys/peaks vs our tick
  profile, even with the same “sensitivity” rule.

For NQ, the tick export is the real feed once you correct a **1‑hour clock
offset** in `@NQ#_ALL.csv` (chart stamps are tick time + 1h).

---

## Detector (shared, confirmed)

LinnSoft Profile / `devNodes` style:

- Rows at **0.25** (1 tick for ES and NQ).
- **Sensitivity S = number of ticks/rows** above *and* below the candidate
  (not chart points). ES **15**, NQ **100**. Treating 15/100 as points
  collapsed match to 0% — rejected.
- **HVN** = strict local volume max over ±S rows; **LVN** = strict local min.
  Adjacent same-kind nodes are at least S+1 rows apart (matches export
  spacing).
- Export labels LVN1/2/3 (and HVN1/2/3) = farthest / mid / nearest of the
  three nodes closest to Close (distance order holds on ~95–99% of rows).

Code: `research/irt_lvn/lvn_profile.py` (`detect_nodes`, `label_nearest_three`).
Stack builder: `research/irt_lvn/build_lvn_stack.py` (minute VAP from ticks →
sliding window → valleys).

---

## Why we disagreed with IRT exports

### ES — volume *source* / construction, not the node rule

| What we used | What IRT’s ESZ6 chart behaves like |
|---|---|
| `@ES#` continuous **tick** prints | **ESZ6** history, volume as if from **1‑min bars** with volume smeared across each bar’s [Low, High] |

Evidence (high level):

- Per-minute ES tick range/volume **matches** ESZ6 ALL bars exactly → same
  feed clock; the gap is how volume is attributed to prices over the
  multi‑day lookback, plus continuous vs front-month weighting before the
  roll.
- Tick VAP VPOC ≈ 7763.75 vs IRT `dVPOC#2` **7764.00**; a bar-distributed VAP
  hits **7764.00** exactly.
- Holdout nearest‑3 scoring: tick recipe ~5/36 exact → bar‑smear recipe
  ~31/36 exact. So the detector is fine; the **histogram** was wrong for
  *matching IRT*.

Best empirical bar-smear (diagnostic only): continuous density over
[Low, High], end rows weighted ~0.25, trailing ~90 calendar days, S=15.

### NQ — clock offset (+ window boundary on crash days)

- A bar stamped `14:30:30` on `@NQ#_ALL.csv` matches the tick bucket
  **13:30:00–13:30:30** (H/L/Volume). Chart clock = tick clock **+1 hour**.
  ES has no such offset.
- After shifting VAP end **−1h**, tick VAP / S=100 / ~35 calendar days
  recovers strongly (many days perfect 6/6). Remaining misses cluster in
  the Oct 14–20 selloff, where levels sit near the lookback tail.

Continuous-contract adjustment was **ruled out** (pre-roll Sep bars still
match tick prices at −1h with zero offset).

### Also ruled out

Sensitivity-as-points; TPO/time profiles; close-only VAP; RTH-only windows;
heavy smoothing/coarse binning as the primary fix; fixed “segment start”
profiles beating a trailing window.

**Scoring note:** early validation scored LVN1↔LVN1 label-to-label. When the
node *set* was right but distance ranking swapped labels, that reported 0%.
Set / nearest‑3 scoring is the right parity metric.

---

## Production choice: tick for both

**Do not** special-case ES to 1‑minute bar distribution in
`build_lvn_stack.py` just to mimic IRT.

Reasons:

1. **Consistency** — one definition on ES and NQ (and vs Bookmap tick/depth
   research). Mixed constructions make dual-market confluence hard to
   interpret.
2. **Truthfulness** — tick VAP is the real volume-at-price; bar-smear is an
   IRT data-limitation approximation.
3. **IRT parity is not the trading goal** — open-cluster ∩ LVN and stack
   display need stable, causal levels we control; ±1 tick vs IRT labels is
   acceptable once the *rule* is right. ES will remain systematically a bit
   off IRT exports unless/until we have ESZ6 ticks (or accept bar-smear as a
   *comparison-only* mode).

Keep / ship:

- Tick → minute VAP (`_build_minute_vap.py`) → sliding window stack
  (`build_lvn_stack.py`) for **both** markets.
- ES window **90d**, S=**15**; NQ window **~30–35d**, S=**100** (tick rows).
- When validating against `@NQ#_ALL.csv`, use **−1h** on timestamps.
- Optional later: a documented **IRT-parity diagnostic** path for ES
  (bar-smear from `ESZ6_ALL`) that never feeds the Bookmap stack by default.

---

## Practical implications

| Use | Source |
|---|---|
| Bookmap developing LVN stack / confluence | Tick stacks (`results/LVN_STACK/`) |
| Research studies using our stack | Same tick stacks |
| “Does this match IRT’s chart lines?” | Expect NQ close after clock fix; ES often ±1 tick or a different near‑3 set — explained by bar-smear / continuous vs ESZ6, not a broken detector |
| Live IRT export LVN2/3 (existing path) | Still valid as *IRT’s* levels; do not assume they equal our tick stack |

Rebuild stacks after tick VAP is refreshed:

```text
.venv\Scripts\python.exe research\irt_lvn\_build_minute_vap.py
.venv\Scripts\python.exe -m research.irt_lvn.build_lvn_stack --market both --every 5
```

---

## Still open (low priority)

- Exact IRT 1‑min→tick distribution (proprietary); w≈0.25 is an approximation.
- Exact NQ period boundary (calendar vs CME session) on crash days.
- Preferable close-out: IRT Profile prefs screenshot, or ESZ6 tick export /
  NQ ticks in the chart’s clock.

Diagnostics live under `research/irt_lvn/_diagnose_*.py` and
`results/LVN_PROFILE_VALIDATE/diagnose_*.txt`.

Bookmap indicator design (knobs, seed, live handoff): [LVN_INDICATOR_DESIGN.md](LVN_INDICATOR_DESIGN.md).
