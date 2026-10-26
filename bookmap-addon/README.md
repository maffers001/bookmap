# Bookmap Orderflow Indicators

Custom Bookmap Layer 1 add-ons for NQ scalping with ES confirmation:

| Add-on name in Bookmap | Purpose |
|---|---|
| **Orderflow Hello** | Load-test only â confirm the JAR installs |
| **Session Extractor** | Replay ? NDJSON trades/full depth/MBO for research |
| **Aggressive Flow** | Buy/sell volume & orders per window, avg size, delta |
| **Effort vs Result** | Aggression vs price progress (absorption / exhaustion) |
| **Liquidity Reaction** | Large-order attract / repel / absorb / dwell / withdraw |
| **ES/NQ Flow Confirm** | Normalized ES vs NQ flow agreement (attach on NQ; keep ES open) |
| **Momentum AutoTrade (ES+NQ)** | v1 â EXP-0008 tape momentum signals (+ optional CD pullback); optional bracket orders |
| **Momentum AutoTrade v4 (ES+NQ)** | v1 + regime/trend/VWAP gates + vol-scaled brackets (EXP-0034); leave v1 unchanged (was labelled v2) |
| **Correlated Fade AutoTrade (ES+NQ)** | H29 / EXP-0036_DIG4 frozen test auto-trade: fade an ES+NQ burst when NQ keeps pushing but ES flips against it; ER = 0.30 gate; vol RR brackets SL 1R / TP 2R |
| **MAT Hotkeys v4** | Classic companion: always-on-top **MAT Side v4** panel + keyboard shortcuts for long/short side filter |
| **Pitch and Catch** | High-vol peak/trough (âpitchâ) then low-vol move away (âcatchâ); chart markers |
| **HV Extreme Move AutoTrade** | HV tip ? clean Move pts leg ? tip markers; optional market + TP/SL brackets |
| **Trapped Retest** | HV peak ? weak lower-vol retest ? fire on break beyond 2nd swing |
| **Stop Run** | Inferred stop-run fingerprint beyond swing/session/overnight levels (EXP-0041 / H32); visual markers only â no auto-trade |
| **LVN Confluence AutoTrade (ES)** | ES open-cluster â© IRT LVN2/3 first-touch fade; console loads pre-open LVN file; default SL8/TP24 (adjustable); trading OFF until enabled |
| **Market Profile** | Developing VAL / VAH / VPOC from UK session open (default 14:30); 70% value area; chart lines + price tags |

## Prerequisites

- Bookmap installed (default `C:\Program Files\Bookmap`)
- JDK 17+ on `PATH` (JDK 21 works; Bookmap itself ships a JRE)
- Gradle 8+ **or** use the included Gradle wrapper (`gradlew`)

Optional: set `BOOKMAP_HOME` if Bookmap is not in the default path.

## Build

```powershell
cd c:\Work\Mat\bookmap\bookmap-addon
.\gradlew.bat jar
```

If the wrapper cannot download Gradle (network/SSL), install Gradle locally or use a previously unpacked distribution, then:

```powershell
gradle jar
```

Example with a local Gradle 8.10.2 unpack:

```powershell
& "$env:TEMP\gradle-8.10.2\bin\gradle.bat" jar
```

Output JAR:

```text
bookmap-addon\build\libs\bookmap-orderflow.jar
```

If Gradle wrapper fails (SSL), compile manually:

```powershell
cd c:\Work\Mat\bookmap\bookmap-addon
$cp = "C:\Program Files\Bookmap\lib\bm-l1api.jar;C:\Program Files\Bookmap\lib\bm-simplified-api-wrapper.jar"
$out = "build\classes\java\main"
$sources = Get-ChildItem -Recurse -Filter "*.java" -Path "src\main\java" | % FullName
javac -encoding UTF-8 -cp $cp -d $out @sources
Push-Location $out; jar cf "..\..\..\libs\bookmap-orderflow-mat-v4.jar" .; Pop-Location
```

For a named MAT v4 build with Gradle: `$env:ORDERFLOW_JAR_NAME='bookmap-orderflow-mat-v4.jar'; .\gradlew.bat jar`


The build compiles against your installed Bookmap API jars (`%BOOKMAP_HOME%\lib\bm-l1api.jar` and `bm-simplified-api-wrapper.jar`, default `C:\Program Files\Bookmap`) so the add-on matches your Bookmap runtime.

## Install in Bookmap

1. Launch Bookmap (**Replay** recommended for first tests). Use an NQ recording from `C:\Bookmap\Feeds` that covers 14:30â17:00 UK if possible. For **ES/NQ Flow Confirm**, open both NQ and ES.
2. Open **Settings ? Api plugins configuration** (or the Configure API plugins toolbar button).
3. Click **Addâ¦** and select:
   `c:\Work\Mat\bookmap\bookmap-addon\build\libs\bookmap-orderflow.jar`
4. In the popup, pick an add-on (e.g. `Aggressive Flow`). Repeat **Addâ¦** for each add-on you want from the same JAR.
5. Enable each module with the **checkbox** on the left.
6. Open the settings panel via the button next to the checkbox to tune windows / thresholds (`@Parameter` fields).
7. After rebuilding: `.\gradlew.bat jar`, then remove/re-add the JAR (or restart Bookmap) so classes reload.

Logs (including Liquidity Reaction event lines and Hello load messages) appear under **File ? Show log file** / `C:\Bookmap\Logs`.

Official references:

- [Bookmap Simplified API tutorial](https://bookmap.com/knowledgebase/docs/API-Tutorial)
- [BookmapAPI/DemoStrategies](https://github.com/BookmapAPI/DemoStrategies)

## What each panel shows

### Aggressive Flow
- **AF Buy** / **AF Sell**: volume (or order counts) per window  
- **AF Delta**: buy - sell  
- **AF Avg Buy/Sell Size**: average aggressive order size  
- Parameters: window ms (maps to 100/250/500/1000 ms), EMA on/off, show orders vs volume  
- **Plot AF Buy / Sell / Delta / Avg Buy Size / Avg Sell Size**: toggle which series appear in the bottom pane (avg sizes off by default)  
- **AF Sell as positive**: when on, AF Sell plots above zero instead of as a negative mirror of buy  


### Effort vs Result
- **ER Buy/Sell Effort**: aggressive volume Ã· price progress (ticks)  
- **ER â¦ Accel**: change in effort vs prior window  
- **ER Exhaustion**: `1` when trend-side volume rises while progress collapses  
- Parameters: window, lookback, epsilon ticks, trend lookback  

### Liquidity Reaction
- **LR Absorb Score**: executed-into-level / displayed size (capped)  
- **LR Dwell Sec**: max dwell near a large level without finishing it  
- **LR Event Code**: `1` Attract, `2` Repel, `3` Absorb, `4` Withdraw, `5` Dwell support  
- **LR Bid/Ask Level**: primary-chart markers at tracked large prices  
- Parameters: large size threshold, z-score, tick radius, dwell alert seconds  

### ES/NQ Flow Confirm
- **XN NQ/ES Delta Z**: z-scored volume delta per market  
- **XN Confirm**: `+1` AGREE_BULL, `-1` AGREE_BEAR, `2` DIVERGE, `0` NEUTRAL  
- **XN Lead**: `+1` ES flipped first, `-1` NQ first (within ~2s)  
- Requires **MultiInstrument** data: enable on NQ with ES subscribed in the same Bookmap session  

### Momentum AutoTrade (ES+NQ)
Research lead from **EXP-0008** (tape momentum + ES sync). Optional **CD pullback** gate (EXP-0009b).

| Panel | Meaning |
|---|---|
| **MAT Signal** | `+1` long / `-1` short / `0` flat |
| **MAT Long / Short** | Primary-chart markers at signal price |
| **MAT ES lag ms** | Lag to matching ES burst (`-1` if none) |
| **MAT CD trend** | `+1` / `-1` / `0` from 22-tick CD SMA |

**Optional trade management** (all default **OFF** â unchanged behavior until enabled):

| Param | Meaning |
|---|---|
| **Reverse on stop** | After SL fill, enter the opposite side (new brackets) |
| **Reverse only if entry OK** | With reverse on: only reverse if opposite-side ES/CD/side-filter entry passes |
| **Extend TP if still favourable** | Near TP, if same-side entry still passes, push TP by **Take profit pts** (SL unchanged) |
| **TP extend proximity (pts)** | Distance to TP that arms an extend (default `2`) |
| **TP extend lookback (sec)** | Max age of same-side NQ burst for favourability (default `8`; does **not** reuse entry gates) |
| **Max TP extends (0=unlimited)** | Cap per trade (default `3`; `0` = no cap) |
| **Extend TP if still favourable** | Default **OFF** — enable only after EXP-0072-style promote |

Reverse always respects **Allow longs/shorts**. Logs: `MAT REVERSE â¦`, `MAT TP EXTEND â¦`.

**Side filter console + hotkeys (MAT v4):** the always-on-top **MAT Side v4** window opens when you **attach Momentum AutoTrade v4 (or v1) on the chart** (and also when **MAT Hotkeys v4** is enabled in API plugins). Buttons: Both / Longs / Shorts / Off + Longs/Shorts toggles. Closing hides it (filter unchanged). Re-open: detach/reattach MAT, or enable **MAT Hotkeys v4**, or bind Bookmap action **Show MAT Side panel**. Check the Bookmap log for `MAT Side v4 panel shown` / `MAT Hotkeys v4 loaded`.

With MAT attached and **Enable Ctrl+Shift side hotkeys = true** (default), or with the console focused:

| Shortcut | Effect |
|---|---|
| **Ctrl+Shift+L** | Toggle allow longs |
| **Ctrl+Shift+K** | Toggle allow shorts |
| **Ctrl+Shift+B** | Both on |
| **Ctrl+Shift+X** | Both off (no new signals) |

(Ctrl+Alt clashes with Windows / Evernote / Snipping Tool.) Log example: `MAT side filter: SHORTS only  [longs=false shorts=true]  (toggled longs)`. Panel **Allow longs/shorts** still seed/sync the same filter when changed.

**Replay test (recommended first):**
1. Rebuild JAR, remove/re-add the plugin in Bookmap if already loaded; enable **MAT Hotkeys v4** and **Momentum AutoTrade v4 (ES+NQ)** (or v1 for classic).
2. Open **Replay**, load a feed with **both NQ and ES**.
3. Add **Momentum AutoTrade v4 (ES+NQ)** on the **NQ** chart; leave **Enable trading = false**.
4. Defaults match the study: 100ms bars, min 6 trades, vol=25, |imb|=0.55, ES sync 1500ms.
5. Optional: set **Max ES lag ms = 250** (stronger subset from research) or **Require CD pullback to MA = true**.
6. Run replay; watch markers + `MAT Signal`. Logs: `MAT SIGNAL` / `MAT2 SIGNAL` in Bookmap log.
7. Only after signals look right: use a **simulation / paper** connection, set **Enable trading = true**, size/TP/SL. Brackets are absolute TP/SL in **price points**.

**Safety:** trading defaults **OFF**. Do not enable on a live account until you have validated on replay/sim.

### Momentum AutoTrade v4 (ES+NQ)

Same core as v1 (EXP-0008 tape + ES sync, reverse-on-stop, TP extend, Ctrl+Shift side hotkeys), plus optional **regime / trend / VWAP** gates and **vol-scaled brackets** from **EXP-0034**, plus **v4 open-vol calibration**: in the first N minutes after UK session open, brackets use the **prior-session** median 1-min range (persisted under `~/.bookmap-orderflow/mat_prior_vol_median.txt`); if none is stored, a **fixed open bracket** (default 10). After the open period, live vol k=1 as before. Log tags remain `MAT2 ?` for replay tooling.

Do **not** enable trading on v1 and v4 at the same time ? they share `MatSideFilter`. CFADE shares the same open-vol store (`MatOpenVolAnchor`) but keeps its own asymmetric RR `resolveBracketPts`.

JAR: `bookmap-addon/build/libs/bookmap-orderflow-mat-v4.jar` (or default `bookmap-orderflow.jar` from `gradle jar`). Remove any old mat-v2 plugin entry before installing.

**Look-ahead note:** research now stamps bursts at **bar end** (corrected 2026-10-18). Older ~85% hit+10 tick figures used bar-start entry and are invalid vs live MAT. Corrected EXP-0008 `p_hit_10pts` Ë **0.56**.

**New params** (EXP-0034 defaults baked in):

| Param | Default | Meaning |
|---|---|---|
| **Regime window min** | 60 | Rolling 1-min bars for day-type |
| **Regime gate: max range pts (0=off)** | **0** (off) | Skip when range exceeds this; leave off by default â 60m NQ range is usually Â»80 so this starved signals when stacked |
| **Regime gate: max efficiency ratio (0=off)** | 0 (off) | Same for `\|net\| / path` |
| **Regime hot action (0=skip 1=counter)** | 0 (skip) | When hot: skip all, or only allow against-trend |
| **Trend window min** | 30 | Net lookback for trend mode |
| **Trend mode (0=off 1=with 2=against)** | **1** (with) | Require burst with rolling net |
| **Require VWAP side** | **true** | Long only = session VWAP; short only = |
| **Vol-scaled brackets** | **true** | TP=SL = k Ã median 1-min range (floor/cap) â frozen base |
| **Vol multiple k** / **Vol window min** / **Bracket min/max pts** | **1** / 30 / 6?30 | **k=1** (OOS 2026-10-18); clip 6?30 |
| **Enable open vol anchor** | **true** | v4: open period uses prior-session median (else fixed) |
| **Open vol period min** | **20** | Minutes after VWAP UK open for open-vol path |
| **Open fixed bracket pts** | **10** | Used in open period when no prior median file |
| **ES confirm mode (0=causal 1=confirm)** | 0 | 1 = NQ-first: pending NQ burst fires when ES confirms within Max ES lag |
| **VWAP session open UK min-of-day** | 870 | UK 14:30 |

Logs: `MAT2 SIGNAL â¦`, `MAT2 SIGNAL(confirm) nqTs=â¦ esTs=â¦ lagMs=â¦`, `MAT2 REJECT regime_hot_skip|trend_mode_fail|vwap_side_fail â¦`.

Defaults are **with-trend + VWAP + vol-scaled k=1** (range gate off). Trend uses **completed 1-minute bars only** (no intra-minute hindsight). VWAP is a running trade VWAP.

**Replay check vs offline reference:**  
`python -m research.export_mat2_ref --session 021026 --stamp confirm --bracket vol_k1`  
Install `bookmap-orderflow-mat-v4.jar` (or a confirm-tagged build), set **ES confirm mode=1**, **Vol-scaled=true**, **k=1**, Verbose=true; grep `MAT2 SIGNAL` / `MAT2 SIGNAL(confirm)` and match with `python -m research.compare_replay_ref`.

### Correlated Fade AutoTrade (ES+NQ)

Frozen H29 fade from **EXP-0036_DIG4** (`results/EXP-0036_DIG4/TEST_AUTOTRADE_SPEC.md`). A *separate book* from MAT: MAT trades **with** the ES+NQ burst; this trades **against** it once ES stops participating. Both can run together as independent signals (DIG3 Â§6: additive), but do not enable trading on both against the same account until the fade has its own live record.

JAR: `bookmap-addon/build/libs/bookmap-orderflow-cfade.jar` (contains every add-on). Classes: `trade/CorrFadeAutoTradeModule` (panel, gates, orders) over `core/CorrFadeDetector` (Bookmap-free signal; `core/TapeBarSeries` gives per-slot NQ/ES bars).

**Signal (all 100 ms tape bars, causal):**
1. ES+NQ **confirm** burst â EXP-0008 rules (6 trades, vol 25, |imb| 0.55, progress 1 tick, cooldown 3), ES same side within Â±1500 ms and |lag| = 250 ms, stamp = max(NQ bar end, ES bar end). Pairing is by bar **stamp** with a short history on both legs, because Bookmap bars close lazily (on the next print) so either leg can become known after the other.
2. From the stamp, first complete bar (= **5 s**) where NQ is still bursting with the trade (sideÃimb = 0.55, vol = 25, trades = 6) **and** ES is against it (sideÃimb = **-0.4** on a bar with ES prints).
3. **Fade** (side = -burst side) at that bar's close. Two bursts resolving on the same bar ? one fade.

**Gates:** UK **14:30â17:00**; **Max efficiency ratio 0.30** on the 60-min regime (`|net| / path` over completed minutes; tracker resets at window open so the first hour matches research); flat-only; 5 s cooldown from the last *taken* signal; Allow longs/shorts. **No** trend/VWAP gate â DIG3 showed those hurt the fade.

**Brackets (default after CFADE_ASYM_RR):** `unit = k Ã median 30-min 1-minute range` (**k = 1**); **SL = clip(1R Ã unit, 4, 40)**, **TP = clip(2R Ã unit, 4, 40)** â same formula as `make_vol_bracket_rr`. Panel: Vol multiple k, SL R / TP R multiples, Bracket min/max pts.

**Open-vol (MAT v4, default ON):** for the first **Open vol period min** (20) after UK window open (14:30), unit uses the **prior-session** median 1-min range from `~/.bookmap-orderflow/mat_prior_vol_median.txt` (shared with MAT); if none is stored, **Open fixed bracket pts** (10) as TP=SL. After the open period, live vol RR as above. Logs `volSrc=prior|fixed|live` on `CFADE SIGNAL`.

| Panel | Meaning |
|---|---|
| **CF Signal** | `+1` fade long / `-1` fade short / `0` |
| **CF Fade Long / Short** | Primary-chart markers at the fade bar close |
| **CF ER60** | Current 60-min efficiency ratio (gate at 0.30) |
| **CF Pending fades** | Confirmed bursts still being watched for an ES-against bar |

**Trading / ops params:** Enable trading (default **off**), Use selected account, Order size (default 1), **Kill switch: max session loss pts** (default 150 â no more orders that UK day once realised net = -150; signals still plot), Verbose log.

Logs: `CFADE CONFIRM#â¦`, `CFADE PROBE â¦` (partial NQ-still / ES-against while waiting), `CFADE EXPIRE â¦` (slot counts + best `side*esImb` / `side*nqImb`), `CFADE AGG sample â¦` (first tape prints after realtime â aggressor polarity), `CFADE FADE PASS|REJECT â¦`, `CFADE SIGNAL â¦`, `CFADE ORDER â¦`, `CFADE TRADE CLOSED â¦`, `CFADE KILL SWITCH`. Reject reasons: `outside_window`, `regime_er_skip`, `signal_cooldown`, `not_flat(...)`, `longs_disabled` / `shorts_disabled`.

**Replay parity note:** fade slots wait for both NQ and ES watermarks (or +1 bar grace) before evaluating â otherwise an ES print can close a 100 ms slot before NQ prints for that slot arrive, and the confirm expires with `nqStillHits=0` while research (sorted full tape) still has the fade.

**Offline parity (2026-10-19):** `CorrFadeDetector` + `RegimeTracker` replayed on the 23 research sessions reproduce the research fade list **132 / 132** exactly (stamp, side, `es_imb_x`, `wait_ms`, close) and identical `er60` / bracket at every fade.

**Replay check:** `python -m research.export_cfade_ref --session 021026` ? `results/CFADE_REF_021026.md` lists every candidate with its ER verdict and the expected `CFADE SIGNAL` rows; grep the Bookmap log and match `tsNs=`.

**Not implemented (ops rules in the spec):** no auto-flatten at 17:00 and no cross-session "3 losing sessions" stop â handle manually.

### LVN Confluence AutoTrade (ES)

Frozen open-cluster ? IRT LVN first-touch fade (research
`study_es_lvn_bracket_8_24` outer book). **Attach on ES.** LVN2/LVN3 are
**not** computed in Bookmap ï¿½ export them from Investor/RT before the open
and load via the always-on-top **LVN Confluence ES** console.

JAR: `bookmap-addon/build/libs/bookmap-orderflow-lvn.jar` (or rebuild the
default `bookmap-orderflow.jar` ï¿½ same classes). Core:
`trade/LvnConfluenceAutoTradeModule` + `trade/LvnControlPanel` +
`core/OpenClusterDetector` + `core/LvnLevelFile`.

**Daily workflow**
1. In IRT, export the **full multi-day** ES (and NQ) chart CSVs that include `Date`, `LVN2`, `LVN3` (and ideally `HVN1`/`HVN2`/`HVN3` ï¿½ same exports research uses).
2. Attach **LVN Confluence AutoTrade (ES)** on the **ES** chart (required for large-order watch, 14:30 snap, trading) and optionally on **NQ/MNQ** (NQ LVN/HVN lines + NQ book if ES is not attached) ï¿½ console opens. Lines only draw on the chart the add-on is attached to.
3. Start **Replay** (or live) so the feed date is known, then console ? **Load ES chart exportï¿½** / **Load NQ chart exportï¿½**.
4. The add-on resolves **LVN2/LVN3 (and HVNs if present) as-of that session day at/before 14:30 UK** from the export (not calendar ï¿½todayï¿½). Status shows `Session YYYY-MM-DD`. On a new replay day the file re-resolves automatically.
5. **Show LVNs on chart** (default ON) plots LVN2/LVN3 on each attached chart. **Show HVNs on chart** (default OFF) plots HVN1/2/3 from the same as-of row.
6. Leave **Trading OFF** until ARM/FILL logs match `export_lvn_live_ref` (trading needs ES levels for the session date; NQ attach never trades).
7. At UK **14:30** on ES: snapshot bright clusters ? ES LVN?10 ï¿½ outer ï¿½ outside+2 ï¿½ min-open 5 ï¿½ SL8/TP24 defaults.
8. Console live table shows levels (LVN + HVN for ES+NQ) + large orders in play.

**Developing LVN stack (optional, recommended for multi-level display)**

IRT Profile `devNodes` (ES: 90d / sens **15 ticks**; NQ: 30d / sens **100 ticks**) is reverse-engineered offline from `@ES#` / `@NQ#` 1-tick files:

```text
.\\.venv\Scripts\python.exe research\irt_lvn\_build_minute_vap.py
.\\.venv\Scripts\python.exe -m research.irt_lvn.build_lvn_stack --market both --every 5
```

Outputs: `results/LVN_STACK/LVN_STACK_ES.csv` and `LVN_STACK_NQ.csv`. Console -> **Load ES/NQ LVN stack...**. Chart then plots nearest **LVNs above / below** mid (defaults **3 / 3**, knobs on the settings row, max 6 each). Confluence uses the full as-of stack when loaded; otherwise falls back to LVN2/LVN3. Validation notes: `results/LVN_PROFILE_VALIDATE/summary.md`.


**Defaults (console-adjustable; Apply applies to new orders only)**

| Knob | Default |
|---|---|
| SL / TP pts | **8 / 24** |
| Offset / near LVN / min open dist | 2 / 10 / 5 |
| LVNs above / below (stack display) | **3 / 3** |
| Entry until / EOS (UK) | 17:00 / 21:00 |
| Order size | 1 |
| Trading | **OFF** |

Unfilled entry limits are cancelled at entry-until; open position is market-flattened at EOS. Flat-only: sibling entry limits cancel when one fills. Kill switch: session net ? ?150 pts (console settings).

| Panel | Meaning |
|---|---|
| **LVN Signal** | `+1` long / `?1` short / `0` |
| **LVN Fade Long / Short** | Entry markers while armed / on fill |
| **LVN2 / LVN3** | Loaded level lines for this chartï¿½s market (when **Show LVNs on chart** is ON) |
| **LVN Above 1..6 / LVN Below 1..6** | Developing stack nearest N above/below mid |
| **HVN1 / HVN2 / HVN3** | High-volume nodes from the same export row (when **Show HVNs on chart** is ON) |

Logs: `LVN SNAP ï¿½`, `LVN ARM ï¿½`, `LVN FILL ï¿½`, `LVN ORDER ï¿½`, `LVN TRADE CLOSED ï¿½`, `LVN FLATTEN ï¿½`.

**Offline parity reference**

```powershell
.\.venv\Scripts\python.exe -m research.irt_lvn.export_lvn_live_ref --session 011026
```

Writes `results/LVN_REF_<sid>/summary.md` (expected ARM/FILL lines) and
`lvn_YYYY-MM-DD.csv` for the console. Replay with trading OFF; grep the
Bookmap log and match. Age-weighted ranking may differ from research CSV
centres if MBO history since attach is short ï¿½ investigate before enabling
trading.

**Safety:** trading defaults OFF. Do not enable on a live account until
replay ARM/FILL match the reference and sim brackets/EOS behave.

### Market Profile
Developing session volume profile from UK **14:30** (configurable). Attach on ES or NQ.

| Panel | Meaning |
|---|---|
| **MP Open** | Daily open  first trade at/after UK session open (blue) |
| **MP VPOC** | Highest-volume price since open (yellow) |
| **MP VAH** | Value area high  top of contiguous band covering Value area % (magenta) |
| **MP VAL** | Value area low (magenta) |
| **MP Total vol** | Session volume since open (bottom pane) |

**Logic:** trade volume by tick from session open; VPOC = max-volume tick (VWAP / lower-tick tie-break); VA expands from VPOC toward the richer adjacent side until ? **Value area %** (default 70). Day rolls at UK midnight; collection restarts at the next open.

| Param | Default | Meaning |
|---|---|---|
| Session open UK min-of-day | **870** | 14:30 UK |
| Value area % | **70** | Contiguous volume fraction around VPOC |
| Show all lines | true | Master toggle for Open / VPOC / VAH / VAL |
| Show Daily Open / VPOC / VAH / VAL | true | Per-line toggles (also need Show all lines) |
| Verbose log | false | `MP status` every 30 s |

Logs: `MP init`, `MP day reset`, `MP session open`, optional `MP status`.

### Pitch and Catch
Warns when a **high-volume** rotation peak/trough is followed by a **low-volume** move of **Catch pts** (default 20) without reclaiming the extreme.

| Panel | Meaning |
|---|---|
| **PC Pitch Long / Short** | Pitch price **while armed** (setup in play) |
| **PC Target Long / Short** | Catch level (pitch Â± Catch pts) while armed |
| **PC Fire Long / Short** | Marker when the catch **completes** |
| **PC Armed** | `+1` / `-1` / `0` while a setup is in play |
| **PC Signal** | Brief `+1` / `-1` when catch fires |
| **PC Peak ratio** | Pitch-band volume Ã· (avg vol/tick Ã band); = **High vol mult** to arm |
| **PC Path ratio** | Catch-leg volume Ã· (avg Ã catch pts); = **Low vol frac** to fire |

**Volume definition:** rolling profile over **Profile lookback min**.  
`avg vol/tick = total volume / (profile high - low + 1)`.  
Peak uses volume in **Peak band pts** around the extreme; catch uses the **Catch pts** price span away from it.

**Suggested start (NQ):** Catch pts `20`, Peak band `2`, Confirm `5`, High vol mult `1.5`, Low vol frac `0.70`. Tighten Low vol frac (e.g. `0.50`) for quieter catches only. Toggle **Show in-play lines** off if the pitch/target lines clutter the chart.

### Stop Run
- **SR Level High / Low**: nearest live unpierced reference level (pullaway swing, locked session H/L, overnight H/L)
- **SR Run Long / Short**: fire marker when the 2s fingerprint scores = min score after a pierce (continuation side)
- **SR Fade Long / Short**: reclaim (stall) marker within reclaim seconds â informational only
- **SR Score** / **SR Signal**: last score 0â4 and brief +/-1 on fire
- Attach on NQ with ES open for ES-burst confirm. MBO pull used when the feed provides MBO.
- Frozen defaults from EXP-0041 / H32 â **visual only, no auto-trade**
- Logs: `SR RUN â¦`, `SR RESOLVE stall|continue â¦`

### Stop Run
Inferred stop-run fingerprint beyond swing / session / overnight levels (EXP-0041 / H32). **Visual only â no auto-trade.** Attach on NQ with ES open.

| Panel | Meaning |
|---|---|
| **SR Level High / Low** | Nearest live unpierced reference level |
| **SR Run Long / Short** | Fire marker after pierce + 2s fingerprint (score = min) |
| **SR Fade Long / Short** | Reclaim (stall) marker within reclaim seconds |
| **SR Score** / **SR Signal** | Last score 0â4 / brief `Â±1` on fire |

Logs: `SR RUN â¦`, `SR RESOLVE stall|continue â¦`. Replay parity: `python -m research.stop_run.dump_session_events --session <sid>`.

### Trapped Retest
Two-swing trapped-size fade: **high-volume** extreme (peak1), then a **lower-volume** failed retest (peak2), then fire when price breaks **Break pts** beyond peak2.

| Panel | Meaning |
|---|---|
| **TR Peak1 Long / Short** | First HV extreme while in play |
| **TR Peak2 Long / Short** | Weak retest extreme while waiting for break |
| **TR Fire Long / Short** | Marker when break completes |
| **TR Phase** | `0` idle / `Â±1` after peak1 / `Â±2` after peak2 |
| **TR Peak ratio** | Peak1 band vol Ã· avg; = **High vol mult** to arm |
| **TR Retest ratio** | Peak2 Ã· peak1 vol (live while seeking); = **Max retest frac** to accept |
| **TR Signal** | Brief `Â±1` on fire |

**Logic (short):** confirm HV local high ? min pullback ? lower high with retest vol = frac Ã peak1 ? fire when price drops **Break pts** from peak2. Long is mirrored. Distinct from Pitch and Catch (one-swing quiet path).

**Suggested start (NQ):** Confirm `5`, Peak band `2`, Min pullback `5`, Break `4`, High vol mult `1.5`, Max retest frac `0.60`. Tighten Max retest frac (e.g. `0.45`) for quieter second peaks only.

### HV Extreme Move AutoTrade
HV tip ? **Move pts** leg (optional clean gates) ? tip markers. Optional **market entry** with fixed or **vol-scaled asymmetric** TP/SL brackets. Strategy name: **HV Extreme Move AutoTrade**. Default: signals only (`Enable trading` off).

| Panel | Meaning |
|---|---|
| **HV Ext Peak / Trough** | Extreme while watching (**Show in-play lines**) |
| **HV Ext Target** | Extreme Â± Move pts while watching |
| **HV Ext Fire Long / Short** | Marker at the **HV tip** when the move completes |
| **HV Ext Signal** | Brief `Â±1` fire pulse |
| **HV Ext Path ER** | Live path efficiency while armed |

**Signal defaults (EXP-0037 frozen, parked):** Move `20`, Confirm `15`, Peak band `2`, High vol mult `2`, Min path efficiency `0` (off), Max pullback `5`, Max leg sec `60`, Lookback `15`, Cooldown `30`. Final-test ship threshold **not** met â see `results/EXP-0037/summary.md`.

**Clean-leg gates:** Min path efficiency (`disp / path length`, `0`=off), Max pullback pts, Max leg sec, quiet-leg volume (prints *outside* Peak band): **Max leg vol vs avg** (Gate B: `< thr Ã avgVolPerTick Ã (moveTicks - bandHalf)`, default `1.0`) and **Max leg vol vs tip** (Gate A: `< thr Ã tip extremeVol`, default `0`=off). Fail ? cancel watch (no fire).

**Brackets:**
- Fixed (default â **Vol-scaled brackets** off): **Take profit pts** `30`, **Stop loss pts** `10` (EXP-0037 freeze).
- Vol-scaled (optional): `unit = k Ã max(median 1-min range over Vol window min, 0.25)`; `SL = clip(SL R Ã unit, min, max)`, `TP = clip(TP R Ã unit, min, max)`. Defaults: k=`1`, SL R=`1`, TP R=`2.5`, floor=`4`, cap=`40` (matches `make_vol_bracket_rr`).

**Trading params:** Enable trading (default **off**), Use selected account, Order size, Use brackets, Flat-only. Orders only after realtime. LONG after HV trough + move up; SHORT after HV peak + move down.

Logs: `HVEXT AT init`, `HV EXT FIRE â¦ tp= sl= medR30= volScaled=`, `HVEXT AT ORDER sent â¦`, `HVEXT AT TRADE CLOSED â¦`.

## Project layout

```text
bookmap-addon/
  build.gradle
  src/main/java/com/bookmap/orderflow/
    core/         shared flow / stats / tape burst / CD / large-level / pitch / hv-move
    hello/        load test
    extract/      Session Extractor (Replay ? NDJSON)
    flow/         Aggressive Flow
    effort/       Effort vs Result
    liquidity/    Liquidity Reaction
    crossmarket/  ES/NQ Flow Confirm
    trade/        Momentum AutoTrade v1 + v4 + MAT Hotkeys / MatControlPanel + Correlated Fade + LVN Confluence
    pitch/        Pitch and Catch
    hvmove/       HV Extreme Move
    retest/       Trapped Retest
    profile/      Market Profile (developing VAL/VAH/VPOC)
    core/         + OpenClusterDetector + LvnLevelFile + MarketProfileTracker
```

Order submission is in **Momentum AutoTrade** (v1/v2), **Correlated Fade AutoTrade**, **HV Extreme Move AutoTrade**, and **LVN Confluence AutoTrade (ES)**, and only when trading is enabled (MAT/CFADE/HV panel checkbox; LVN console **Trading ON**).

## Session Extractor playbook (research corpus)

Use this to dump **full** ES+NQ trades and depth from a `.bmf` into the research pipeline.

1. Build/install the JAR (see above). In API plugins, **Add** `Session Extractor` and enable it.
2. Open Bookmap **Replay**, load a feed from `C:\Bookmap\Feeds` that includes RTH (UK 14:30â17:00).
3. Ensure **both NQ and ES** instruments from the recording are open/subscribed.
4. Open Session Extractor settings **before enabling**:
   - **Output root directory:** `C:/Work/Mat/bookmap/data/raw`
   - **Session id override:** set once (e.g. `210926`) â do **not** change it mid-replay (that reloads into a new folder)
   - **Skip MNQ / micros:** leave **true** (default) so only full-size NQ + ES are written
5. Enable the extractor, set replay to **maximum speed**, let it run to the end (or through RTH).
6. **Disable** the extractor when done (forces flush + `finished: true` in `meta.json`).
7. Confirm sizes in Explorer (**F5**) or PowerShell â while replay is running, Explorer often shows **0 KB** for open files even though data is writing. After disable, sizes should be hundreds of MB / GB.
8. Confirm folder: `data/raw/<session_id>/trades.ndjson`, `depth.ndjson`, `meta.json` (and `mbo.ndjson` if MBO was present).
9. Repeat for 5â10 **RTH** days (UK 14:30â17:00), then:

```powershell
cd c:\Work\Mat\bookmap
.\.venv\Scripts\Activate.ps1
python -m research.extract.to_parquet
python -m research.run_experiment EXP-0002 --sessions all
```

**Note:** Full depth extracts are large (multi-GB per day with MBO). Prefer a drive with free space.

**Note:** Do not enable the extractor twice / change session id mid-run â you can get a partial folder plus a second folder.

## Sanity checks on replay

1. Aggressive Flow spikes line up with Bookmap trade / volume activity.  
2. Effort/Result exhaustion flags when heavy selling (or buying) fails to move price.  
3. Liquidity Reaction fires when a large resting order appears near price.  
4. ES/NQ Confirm goes `AGREE_*` only when both books push the same way.  
5. Momentum AutoTrade fires `MAT SIGNAL` in the log with Long/Short markers when ES confirms a fast NQ burst.
