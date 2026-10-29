package com.bookmap.orderflow.trade;

import java.awt.Color;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.HashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;

import com.bookmap.orderflow.core.CumulativeDeltaRange;
import com.bookmap.orderflow.core.InstrumentClassifier;
import com.bookmap.orderflow.core.PriceTicks;
import com.bookmap.orderflow.core.MatOpenVolAnchor;
import com.bookmap.orderflow.core.MatPriorRotAnchor;
import com.bookmap.orderflow.core.RegimeTracker;
import com.bookmap.orderflow.core.SessionRotationTracker;
import com.bookmap.orderflow.core.TapeBurstTracker;
import com.bookmap.orderflow.core.TapeBurstTracker.Burst;

import velox.api.layer1.Layer1ApiAdminAdapter;
import velox.api.layer1.Layer1ApiProvider;
import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1MultiAccountTradingSupported;
import velox.api.layer1.annotations.Layer1SimpleAttachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.annotations.Layer1TradingStrategy;
import velox.api.layer1.common.Log;
import velox.api.layer1.common.helper.AccountListManager;
import velox.api.layer1.data.AccountInfo;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.data.OrderDuration;
import velox.api.layer1.data.OrderEntryType;
import velox.api.layer1.data.OrderInfoUpdate;
import velox.api.layer1.data.OrderMoveParameters;
import velox.api.layer1.data.OrderStatus;
import velox.api.layer1.data.OrderType;
import velox.api.layer1.data.SimpleOrderSendParameters;
import velox.api.layer1.data.SimpleOrderSendParametersBuilder;
import velox.api.layer1.data.StatusInfo;
import velox.api.layer1.data.TradeInfo;
import velox.api.layer1.messages.SelectedAccountChangedMessage;
import velox.api.layer1.messages.indicators.Layer1ApiUserMessageModifyIndicator.GraphType;
import velox.api.layer1.simplified.Api;
import velox.api.layer1.simplified.AxisGroup;
import velox.api.layer1.simplified.CustomModule;
import velox.api.layer1.simplified.HistoricalModeListener;
import velox.api.layer1.simplified.Indicator;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.IntervalListener;
import velox.api.layer1.simplified.Intervals;
import velox.api.layer1.simplified.MultiInstrumentListener;
import velox.api.layer1.simplified.OrdersListener;
import velox.api.layer1.simplified.Parameter;
import velox.api.layer1.simplified.PositionListener;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/**
 * Momentum AutoTrade <b>v5 / MAT075</b> (class still {@code MomentumAutoTradeV2Module}) —
 * EXP-0008 tape momentum plus regime / trend / VWAP gates, vol-scaled brackets
 * (EXP-0034), open-vol anchor (v4), and <b>EXP-0075</b> keep-gates:
 * session-scale {@code rot_fresh} (ratio &lt; 0.90) ∩ near-VWAP
 * (|dist|/med_r30 ≤ 0.5). Side filter console via {@link MatControlPanel}
 * when <b>MAT Hotkeys v5</b> is enabled.
 *
 * <p><b>v4 open-vol:</b> for the first {@code Open vol period min} after UK
 * session open, brackets use a <em>prior-session</em> median 1-min range
 * ({@link com.bookmap.orderflow.core.MatOpenVolAnchor}); if none is stored,
 * fall back to fixed {@code Open fixed bracket pts}. After the open period,
 * live vol k=1 (clip 6–30) as before. Does not change CFADE.
 *
 * Leave v1 ({@code Momentum AutoTrade (ES+NQ)}) unchanged; do not enable trading
 * on both at once (they share {@link MatSideFilter}). Log tags stay {@code MAT2}
 * for replay compare tooling.
 *
 * Frozen research defaults: Trend mode = with-trend (1), Require VWAP side = true,
 * Max ES lag = 250, Vol-scaled brackets ON k=1 (clip 6–30),
 * <b>Require rot fresh = true</b>, <b>Require VWAP near = true</b>.
 * Regime range gate OFF. Optional {@code ES confirm mode=1} holds NQ-first
 * bursts until ES confirms within max lag.
 *
 * Attach on NQ with ES also open. Default: signals only (enableTrading=false).
 */
@Layer1SimpleAttachable
@Layer1TradingStrategy
@Layer1MultiAccountTradingSupported
@Layer1StrategyName("Momentum AutoTrade v5 (ES+NQ)")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class MomentumAutoTradeV2Module implements
        CustomModule,
        TradeDataListener,
        TimeListener,
        IntervalListener,
        MultiInstrumentListener,
        PositionListener,
        OrdersListener,
        HistoricalModeListener {

    // --- EXP-0008 tape defaults ---
    @Parameter(name = "Bar ms", step = 50.0)
    Double barMs = 100.0;

    @Parameter(name = "Min trades / bar", step = 1.0)
    Double minTrades = 6.0;

    @Parameter(name = "Min volume / bar", step = 1.0)
    Double minVol = 25.0;

    @Parameter(name = "Min |imbalance|", step = 0.05)
    Double minImbalance = 0.55;

    @Parameter(name = "Min progress (price pts)", step = 0.25)
    Double minProgressPts = 0.25;

    @Parameter(name = "ES sync window ms", step = 50.0)
    Double syncMs = 1500.0;

    /** Offline dig (EXP-0008): lag&lt;250ms lifts path and hit rate vs lag≥250. */
    @Parameter(name = "Max ES lag ms (0=off)", step = 50.0)
    Double maxEsLagMs = 250.0;

    /**
     * 0 = causal (ES at/before NQ — default live). 1 = confirm (allow NQ-first;
     * hold pending NQ burst until same-side ES arrives within Max ES lag).
     */
    @Parameter(name = "ES confirm mode (0=causal 1=confirm)", step = 1.0)
    Double esConfirmMode = 0.0;

    @Parameter(name = "Burst cooldown bars", step = 1.0)
    Double cooldownBars = 3.0;

    // --- CD pullback gate (EXP-0009b) ---
    @Parameter(name = "Require CD pullback to MA")
    Boolean requireCdPullback = false;

    @Parameter(name = "CD range ticks", step = 1.0)
    Double cdRangeTicks = 22.0;

    @Parameter(name = "CD SMA length", step = 1.0)
    Double cdSmaLen = 100.0;

    // --- Trading ---
    @Parameter(name = "Enable trading (OFF for signal-only)")
    Boolean enableTrading = false;

    /**
     * Seeds {@link MatSideFilter} on attach / when changed in the panel.
     * While trading prefer Ctrl+Shift hotkeys (see {@link MatSideHotkeys}).
     */
    @Parameter(name = "Allow longs")
    Boolean allowLongs = false;

    @Parameter(name = "Allow shorts")
    Boolean allowShorts = false;

    /** Ctrl+Shift+L/K toggle, Ctrl+Shift+B both, Ctrl+Shift+X none. */
    @Parameter(name = "Enable Ctrl+Shift side hotkeys")
    Boolean enableSideHotkeys = true;

    /** Last panel values pushed into {@link MatSideFilter} (detect UI changes). */
    private Boolean lastSyncedAllowLongs;
    private Boolean lastSyncedAllowShorts;
    private boolean sideHotkeysInstalled;
    private final MatTradeControl.Flattener flattenAction = this::flattenPosition;

    /**
     * When true, orders use Bookmap's Account dropdown for this instrument
     * (Simulator on Replay). When false, accountId is left null (provider primary).
     */
    @Parameter(name = "Use selected account (dropdown)")
    Boolean useSelectedAccount = true;

    @Parameter(name = "Order size", step = 1.0)
    Double orderSize = 1.0;

    /** When true, entry is sent with Bookmap bracket TP/SL (tick offsets). */
    @Parameter(name = "Attach brackets (TP/SL)")
    Boolean useBrackets = true;

    @Parameter(name = "Take profit (price pts)", step = 1.0)
    Double takeProfitPts = 10.0;

    @Parameter(name = "Stop loss (price pts)", step = 1.0)
    Double stopLossPts = 10.0;

    @Parameter(name = "Signal cooldown sec", step = 1.0)
    Double signalCooldownSec = 5.0;

    @Parameter(name = "Flat-only (no pyramid)")
    Boolean flatOnly = true;

    /** When stop fills, enter opposite side (respects Allow longs/shorts). */
    @Parameter(name = "Reverse on stop")
    Boolean reverseOnStop = false;

    /** If Reverse on stop: only reverse when opposite entry conditions pass. */
    @Parameter(name = "Reverse only if entry OK")
    Boolean reverseOnlyIfEntryOk = false;

    /**
     * Near TP, push TP further by one live bracket if same-side burst still
     * favourable (EXP-0072). Default OFF — promote only after research PASS.
     */
    @Parameter(name = "Extend TP if still favourable")
    Boolean extendTpIfFavourable = false;

    @Parameter(name = "TP extend proximity (pts)", step = 0.25)
    Double tpExtendProximityPts = 2.0;

    /** Max age of last same-side NQ burst for TP-extend favourability (sec). */
    @Parameter(name = "TP extend lookback (sec)", step = 1.0)
    Double tpExtendLookbackSec = 8.0;

    /** 0 = unlimited extends per trade. */
    @Parameter(name = "Max TP extends (0=unlimited)", step = 1.0)
    Double maxTpExtends = 3.0;

    // --- EXP-0034 regime / trend / vol gates (default OFF) ---
    @Parameter(name = "Regime window min", step = 1.0)
    Double regimeWindowMin = 60.0;

    @Parameter(name = "Regime gate: max range pts (0=off)", step = 5.0)
    Double regimeMaxRangePts = 0.0;

    @Parameter(name = "Regime gate: max efficiency ratio (0=off)", step = 0.05)
    Double regimeMaxEr = 0.0;

    /** 0 = skip when hot; 1 = counter-trend only when hot. */
    @Parameter(name = "Regime hot action (0=skip 1=counter)", step = 1.0)
    Double regimeHotAction = 0.0;

    @Parameter(name = "Trend window min", step = 1.0)
    Double trendWindowMin = 30.0;

    /** 0 = off; 1 = with-trend; 2 = against-trend. */
    @Parameter(name = "Trend mode (0=off 1=with 2=against)", step = 1.0)
    Double trendMode = 1.0;

    @Parameter(name = "Require VWAP side")
    Boolean requireVwapSide = true;

    /**
     * EXP-0075 / MAT075: skip with-leg entries when session rotation ratio ≥ max
     * (chase). When rotation stats are not ready, keep (research {@code rot_ready=0}).
     */
    @Parameter(name = "Require rot fresh (EXP-0075)")
    Boolean requireRotFresh = true;

    @Parameter(name = "Rot fresh max ratio", step = 0.05)
    Double rotFreshMaxRatio = 0.90;

    /**
     * EXP-0075 / MAT075: keep only when same-side of VWAP and
     * |last−VWAP| / med_r30 ≤ max norm.
     */
    @Parameter(name = "Require VWAP near (EXP-0075)")
    Boolean requireVwapNear = true;

    @Parameter(name = "VWAP near max |dist|/med_r30", step = 0.1)
    Double vwapNearMaxNorm = 0.5;

    /** Frozen base: TP=SL = clip(k × median 1-min range, min, max). */
    @Parameter(name = "Vol-scaled brackets")
    Boolean volScaledBrackets = true;

    /** Frozen base from OOS (2026-10-18): k=1 symmetric clip 6–30. */
    @Parameter(name = "Vol multiple k", step = 0.5)
    Double volMultipleK = 1.0;

    @Parameter(name = "Vol window min", step = 1.0)
    Double volWindowMin = 30.0;

    @Parameter(name = "Bracket min pts", step = 1.0)
    Double bracketMinPts = 6.0;

    @Parameter(name = "Bracket max pts", step = 1.0)
    Double bracketMaxPts = 30.0;

    /**
     * v4: during the first N minutes after VWAP session open, use prior-session
     * median range for vol brackets (or fixed open size if no prior).
     */
    @Parameter(name = "Open vol period min", step = 1.0)
    Double openVolPeriodMin = 20.0;

    /** Fixed TP=SL (pts) when in open period and no prior median is available. */
    @Parameter(name = "Open fixed bracket pts", step = 0.5)
    Double openFixedBracketPts = 10.0;

    @Parameter(name = "Enable open vol anchor")
    Boolean enableOpenVolAnchor = true;

    /** UK minute-of-day for VWAP session open (14*60+30 = 870). 0 = first trade. */
    @Parameter(name = "VWAP session open UK min-of-day", step = 1.0)
    Double vwapSessionOpenUkMin = 870.0;

    /**
     * When true, log every NQ burst with ES lag + gate decision (MAT2 CANDIDATE /
     * MAT2 REJECT / MAT2 SIGNAL) for replay vs offline reference comparison.
     */
    @Parameter(name = "Verbose candidate log")
    Boolean verboseCandidateLog = true;

    private Api api;
    private Layer1ApiProvider provider;
    private Layer1ApiAdminAdapter adminListener;
    private final AccountListManager accountListManager = new AccountListManager();
    private String primaryAlias;
    private String currentAlias;
    private long nowNs;
    private double pips = 0.25;
    /** Last StatusInfo.position (may lag or miss in Replay SIM). */
    private int position;
    /** Net filled qty from OrdersListener (entry +, exit −). */
    private int netFills;
    /** True after entry send until flat again — blocks pyramid even if pos still 0. */
    private boolean flatLocked;
    private final Map<String, Integer> filledByOrderId = new HashMap<>();
    /** Last resolved trading account id (null = primary / unspecified). */
    private volatile String tradingAccountId;

    private final Map<String, InstrumentState> byAlias = new HashMap<>();

    private Indicator signalIndicator;
    private Indicator longMarker;
    private Indicator shortMarker;
    private Indicator esLagIndicator;
    private Indicator cdTrendIndicator;

    private long lastSignalNs;
    private int lastSignalSide;
    private boolean signalThisInterval;

    /** NQ-first pending burst waiting for ES confirm (confirm mode only). */
    private PendingNqBurst pendingNqBurst;

    private static final class PendingNqBurst {
        final int side;
        final long tsNs;
        final double closePrice;
        final long expiryNs;

        PendingNqBurst(int side, long tsNs, double closePrice, long expiryNs) {
            this.side = side;
            this.tsNs = tsNs;
            this.closePrice = closePrice;
            this.expiryNs = expiryNs;
        }
    }

    /** False during catchup/history; true after {@link #onRealtimeStart()}. */
    private volatile boolean realtimeStarted;

    /** Open trade: 0 flat, +1 long, −1 short. */
    private int openSide;
    private double entryFillPx;
    private String tpOrderId;
    private String slOrderId;
    private double currentTpPx;
    private int tpExtendCount;
    /** False after an extend until price backs out of the proximity band. */
    private boolean tpExtendArmed = true;
    /** One-shot reverse after SL fill (processed when flat). */
    private boolean pendingReverse;
    private int pendingReverseSide;

    private long lastSeenTsNs = -1;
    private long nqTradeCount;
    private long esTradeCount;
    private long nqBurstCount;
    private long esBurstCount;
    private long rejectLogNs;
    private static final long REJECT_LOG_INTERVAL_NS = 2_000_000_000L; // 2s replay-time
    private static final ZoneId UK = ZoneId.of("Europe/London");
    private static final DateTimeFormatter TIME_FMT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS z");

    private static final class InstrumentState {
        String role = "OTHER";
        TapeBurstTracker burst;
        CumulativeDeltaRange cd;
        RegimeTracker regime;
        SessionRotationTracker rotation;
        double lastPrice;
        double instrumentPips = 0.25;
        boolean vwapSessionMarked;
    }

    /** Live bracket size used for extend (updated on each entry). */
    private double liveBracketPts = 10.0;

    /** Cached prior-session median (NaN until first load attempt). */
    private double priorVolMedian = Double.NaN;
    private boolean priorVolLoaded;
    private long lastPriorPersistNs;

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        this.api = api;
        this.primaryAlias = alias;
        this.currentAlias = alias;
        this.pips = info.pips > 0 ? info.pips : 0.25;
        this.realtimeStarted = false;
        this.position = 0;
        this.netFills = 0;
        this.flatLocked = false;
        this.filledByOrderId.clear();
        this.tradingAccountId = null;
        clearTradeContext("init");

        InstrumentState primary = newState(InstrumentClassifier.classify(info));
        byAlias.put(alias, primary);

        signalIndicator = api.registerIndicator("MAT2 Signal", GraphType.BOTTOM);
        esLagIndicator = api.registerIndicator("MAT2 ES lag ms", GraphType.BOTTOM);
        cdTrendIndicator = api.registerIndicator("MAT2 CD trend", GraphType.BOTTOM);
        longMarker = api.registerIndicator("MAT2 Long", GraphType.PRIMARY);
        shortMarker = api.registerIndicator("MAT2 Short", GraphType.PRIMARY);

        signalIndicator.setColor(new Color(255, 220, 60));
        esLagIndicator.setColor(new Color(160, 160, 160));
        cdTrendIndicator.setColor(new Color(120, 200, 255));
        longMarker.setColor(new Color(0, 200, 90));
        shortMarker.setColor(new Color(220, 60, 60));
        longMarker.setWidth(3);
        shortMarker.setWidth(3);

        AxisGroup bottom = new AxisGroup();
        bottom.add(signalIndicator);
        bottom.add(esLagIndicator);
        bottom.add(cdTrendIndicator);

        installAccountListener(api);
        refreshTradingAccountId("init");
        syncSideFilterFromParams("init");
        sideHotkeysInstalled = false;
        if (Boolean.TRUE.equals(enableSideHotkeys)) {
            MatSideHotkeys.install();
            sideHotkeysInstalled = true;
        } else {
            MatControlPanel.showPanel();
        }
        // Console / attach always starts with signals off (ignore saved Allow longs/shorts).
        MatSideFilter.set(false, false, "mat attach start OFF");
        lastSyncedAllowLongs = false;
        lastSyncedAllowShorts = false;
        MatTradeControl.register(flattenAction);

        Log.info("Momentum AutoTrade v5 init on " + alias
                + " role=" + primary.role
                + " trading=" + Boolean.TRUE.equals(enableTrading)
                + " longs=" + MatSideFilter.allowLongs()
                + " shorts=" + MatSideFilter.allowShorts()
                + " sideHotkeys=" + sideHotkeysInstalled
                + " useSelectedAccount=" + Boolean.TRUE.equals(useSelectedAccount)
                + " accountId=" + (tradingAccountId == null ? "null(primary)" : tradingAccountId)
                + " cdPullback=" + Boolean.TRUE.equals(requireCdPullback)
                + " maxEsLagMs=" + maxEsLagMs
                + " syncMs=" + syncMs
                + " pips=" + pips
                + " trendMode=" + trendMode
                + " requireVwap=" + requireVwapSide
                + " requireRotFresh=" + Boolean.TRUE.equals(requireRotFresh)
                + " requireVwapNear=" + Boolean.TRUE.equals(requireVwapNear)
                + " regimeMaxRange=" + regimeMaxRangePts
                + " regimeMaxEr=" + regimeMaxEr
                + " verboseCand=" + verboseCandidateLog
                + " (trade price→chart via PriceTicks.toChartPoints;"
                + " trend uses completed 1m bars only — no intra-minute hindsight)");
    }

    private void installAccountListener(Api api) {
        try {
            provider = api.getProvider();
            if (provider == null) {
                Log.info("MAT2 account listener: provider null — dropdown account not tracked");
                return;
            }
            adminListener = new Layer1ApiAdminAdapter() {
                @Override
                public void onUserMessage(Object data) {
                    boolean updated = accountListManager.onUserMessage(data);
                    if (data instanceof SelectedAccountChangedMessage) {
                        SelectedAccountChangedMessage msg = (SelectedAccountChangedMessage) data;
                        Log.info("MAT2 account selection changed alias=" + msg.alias
                                + " accountId=" + msg.accountId);
                        if (primaryAlias != null && primaryAlias.equals(msg.alias)) {
                            refreshTradingAccountId("selection");
                        }
                    } else if (updated) {
                        refreshTradingAccountId("accounts");
                    }
                }
            };
            provider.addListener(adminListener);
        } catch (Throwable t) {
            Log.info("MAT2 account listener install failed: " + t);
        }
    }

    private void refreshTradingAccountId(String reason) {
        String resolved = resolveAccountId();
        boolean unchanged = (resolved == null && tradingAccountId == null)
                || (resolved != null && resolved.equals(tradingAccountId));
        if (unchanged) {
            if ("init".equals(reason) || "selection".equals(reason)) {
                logAccountState(reason, resolved);
            }
            return;
        }
        tradingAccountId = resolved;
        logAccountState(reason, resolved);
    }

    private void logAccountState(String reason, String accountId) {
        String detail = "null(primary/unspecified)";
        if (accountId != null) {
            AccountInfo info = accountListManager.getAccountById(accountId);
            if (info != null) {
                detail = accountId + " name=" + info.shortName
                        + " summary=" + info.summary
                        + " sim=" + info.isSimulated()
                        + " primary=" + info.isPrimary;
            } else {
                detail = accountId;
            }
        }
        Log.info("MAT2 account [" + reason + "] " + detail
                + " accountsKnown=" + accountListManager.getAccounts().size());
    }

    /**
     * Prefer Bookmap Account dropdown for this alias (Simulator on Replay).
     * Falls back to null so Bookmap uses the provider primary / only account.
     */
    private String resolveAccountId() {
        if (!Boolean.TRUE.equals(useSelectedAccount)) {
            return null;
        }
        if (primaryAlias == null) {
            return null;
        }
        Optional<String> selected = accountListManager.getSelectedAccountIdForAlias(primaryAlias);
        if (selected.isPresent()) {
            String id = selected.get();
            return (id == null || id.isEmpty()) ? null : id;
        }
        // No selection message yet — if exactly one account exists for alias, use it
        // (typical Replay: only Simulator).
        Set<AccountInfo> forAlias = accountListManager.getAccountsByAlias(primaryAlias);
        if (forAlias != null && forAlias.size() == 1) {
            return forAlias.iterator().next().id;
        }
        Set<AccountInfo> all = accountListManager.getAccounts();
        if (all != null && all.size() == 1) {
            return all.iterator().next().id;
        }
        return null;
    }

    private InstrumentState newState(String role) {
        return newState(role, pips);
    }

    private InstrumentState newState(String role, double instrumentPips) {
        InstrumentState s = new InstrumentState();
        s.role = role;
        s.instrumentPips = instrumentPips > 0 ? instrumentPips : 0.25;
        int bar = barMs == null ? 100 : barMs.intValue();
        int trades = minTrades == null ? 6 : minTrades.intValue();
        double vol = minVol == null ? 25.0 : minVol;
        double imb = minImbalance == null ? 0.55 : minImbalance;
        // Min progress is configured in price points; API trade prices are tick indices.
        double progPts = minProgressPts == null ? s.instrumentPips : minProgressPts;
        double progApi = PriceTicks.pointsToApi(progPts, s.instrumentPips, true);
        int cool = cooldownBars == null ? 3 : cooldownBars.intValue();
        s.burst = new TapeBurstTracker(bar, trades, vol, imb, progApi, cool);
        int range = cdRangeTicks == null ? 22 : cdRangeTicks.intValue();
        int sma = cdSmaLen == null ? 100 : cdSmaLen.intValue();
        // CD range bars: price in ticks → tickSize=1 so range = N ticks
        s.cd = new CumulativeDeltaRange(range, 1.0, sma, 5, 0.35);
        s.regime = new RegimeTracker();
        s.rotation = new SessionRotationTracker();
        MatPriorRotAnchor.Prior prior = MatPriorRotAnchor.load();
        s.rotation.setPriorAvgs(prior.upAvgPts, prior.downAvgPts);
        if (prior.usable()) {
            Log.info("MAT2 prior-rot loaded up=" + prior.upAvgPts
                    + " down=" + prior.downAvgPts
                    + " from " + MatPriorRotAnchor.storePath());
        } else {
            Log.info("MAT2 prior-rot none at " + MatPriorRotAnchor.storePath()
                    + " (session avg only until 2 same-dir rots)");
        }
        s.vwapSessionMarked = false;
        return s;
    }

    @Override
    public void onInstrumentAdded(InstrumentInfo info) {
        String alias = currentAlias != null ? currentAlias : info.symbol;
        double ipips = info.pips > 0 ? info.pips : 0.25;
        InstrumentState state = byAlias.computeIfAbsent(alias, a -> newState("OTHER", ipips));
        String role = InstrumentClassifier.classify(info);
        state.role = role;
        state.instrumentPips = ipips;
        Log.info(String.format(
                "MAT2 instrument added alias=%s symbol=%s full=%s role=%s pips=%.4f",
                alias,
                info.symbol,
                info.fullName,
                role,
                ipips));
        if (alias.equals(primaryAlias) && info.pips > 0) {
            pips = info.pips;
            state.instrumentPips = pips;
            // Rebuild detectors with correct primary pips / tick-scaled thresholds
            InstrumentState rebuilt = newState(role, pips);
            rebuilt.lastPrice = state.lastPrice;
            byAlias.put(alias, rebuilt);
        }
    }

    @Override
    public void onCurrentInstrument(String alias) {
        this.currentAlias = alias;
        byAlias.computeIfAbsent(alias, a -> newState("OTHER"));
    }

    @Override
    public void onTimestamp(long nanoseconds) {
        // Replay rewind / catchup skip: reset detectors so we don't keep future bursts
        if (lastSeenTsNs > 0 && nanoseconds + 500_000_000L < lastSeenTsNs) {
            Log.info(String.format(
                    "MAT2 rewind/reset detected prevTs=%d newTs=%d — clearing burst/CD state",
                    lastSeenTsNs, nanoseconds));
            resetAllDetectors();
        }
        this.nowNs = nanoseconds;
        this.lastSeenTsNs = nanoseconds;
    }

    private void resetAllDetectors() {
        for (InstrumentState s : byAlias.values()) {
            if (s.burst != null) {
                s.burst.reset();
            }
            if (s.cd != null) {
                s.cd.reset();
            }
            if (s.regime != null) {
                s.regime.reset();
            }
            if (s.rotation != null) {
                s.rotation.reset();
            }
            s.vwapSessionMarked = false;
        }
        lastSignalNs = 0;
        nqBurstCount = esBurstCount = 0;
        pendingNqBurst = null;
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        InstrumentState state = byAlias.get(currentAlias);
        if (state == null) {
            state = newState("OTHER");
            byAlias.put(currentAlias, state);
        }
        state.lastPrice = price;
        long ts = nowNs > 0 ? nowNs : System.nanoTime();
        boolean buyAgg = tradeInfo.isBidAggressor;
        Burst burst = state.burst.onTrade(ts, price, size, buyAgg);
        state.cd.onTrade(price, size, buyAgg);
        // Regime uses chart points (same units as TP/SL params).
        if (state.regime != null && isPrimaryNq(state)) {
            double chartPx = PriceTicks.toChartPoints(price, state.instrumentPips);
            maybeMarkVwapSession(state, ts);
            state.regime.onTrade(ts, chartPx, size);
            // Research annotates rotation on UK-window trades only (from session open).
            // Feeding pre-open prints invents pivots and false rot_fresh rejects.
            if (state.rotation != null && state.vwapSessionMarked) {
                state.rotation.onTrade(ts, price);
            }
        }

        if ("ES".equals(state.role)) {
            esTradeCount++;
            if (burst != null) {
                esBurstCount++;
                if (esBurstCount <= 5 || esBurstCount % 50 == 0) {
                    double chartPx = PriceTicks.toChartPoints(burst.closePrice, state.instrumentPips);
                    Log.info(String.format(
                            "MAT2 ES burst#%d side=%d n=%d vol=%.0f imb=%.2f px=%.2f (api=%.0f) %s",
                            esBurstCount, burst.side, burst.nTrades, burst.totalVol,
                            burst.imbalance, chartPx, burst.closePrice, formatReplayTime(ts)));
                }
                tryPendingConfirm(burst);
            }
            return;
        }

        if ("NQ".equals(state.role) || currentAlias.equals(primaryAlias)) {
            nqTradeCount++;
        }

        if (isPrimaryNq(state)) {
            maybeExtendTp(state);
            tryPendingReverse();
        }

        // Only evaluate entries when the primary (NQ) chart closes a burst bar
        if (burst == null || !isPrimaryNq(state)) {
            return;
        }
        nqBurstCount++;
        maybeSignal(burst, state);
    }

    private boolean isPrimaryNq(InstrumentState state) {
        if ("NQ".equals(state.role)) {
            return currentAlias != null && currentAlias.equals(primaryAlias);
        }
        // Attached chart treated as NQ if unclassified
        return currentAlias != null && currentAlias.equals(primaryAlias)
                && ("NQ".equals(state.role) || "OTHER".equals(state.role));
    }

    private boolean isConfirmMode() {
        return esConfirmMode != null && esConfirmMode >= 0.5;
    }

    private void clearPendingNq(String via) {
        if (pendingNqBurst != null) {
            Log.info("MAT2 pending NQ cleared (" + via + ")");
            pendingNqBurst = null;
        }
    }

    private void maybeExpirePending(long tsNs) {
        if (pendingNqBurst != null && tsNs > pendingNqBurst.expiryNs) {
            clearPendingNq("expiry");
        }
    }

    private void maybeSignal(Burst nqBurst, InstrumentState nqState) {
        syncSideFilterFromParams("params");
        maybeExpirePending(nqBurst.tsNs);

        // Opposite-side NQ burst cancels a pending confirm
        if (pendingNqBurst != null && nqBurst.side != pendingNqBurst.side) {
            clearPendingNq("opposite_nq_burst");
        }

        EntryEval eval = evaluateEntry(nqBurst.side, nqBurst, nqState, false);

        esLagIndicator.addPoint(eval.lagMs >= Long.MAX_VALUE / 2 ? -1.0 : eval.lagMs);
        cdTrendIndicator.addPoint(nqState.cd.getMaTrend());

        // Verbose: every burst. Otherwise: always log gate rejects (not ES spam).
        boolean gateReject = eval.reason != null && (
                eval.reason.startsWith("regime_")
                        || eval.reason.startsWith("trend_")
                        || eval.reason.startsWith("vwap_"));
        if (Boolean.TRUE.equals(verboseCandidateLog) || gateReject) {
            logCandidate(nqBurst, nqState, eval);
        }

        if (!eval.ok) {
            // Hold NQ briefly when ES same-slot bar not closed yet (live ordering).
            // Confirm mode: wait up to maxEsLagMs for NQ-first. Causal: wait syncMs
            // for same-stamp ES only (see tryPendingConfirm).
            if ("ES_no_same_side_burst_in_sync".equals(eval.reason)
                    || "ES_lag_gt_max".equals(eval.reason)) {
                double waitMs = isConfirmMode()
                        ? (maxEsLagMs == null ? 250.0 : maxEsLagMs)
                        : (syncMs == null ? 1500.0 : syncMs);
                long expiry = nqBurst.tsNs + (long) (Math.max(waitMs, 1.0) * 1_000_000.0);
                pendingNqBurst = new PendingNqBurst(
                        nqBurst.side, nqBurst.tsNs, nqBurst.closePrice, expiry);
                if (Boolean.TRUE.equals(verboseCandidateLog)) {
                    Log.info(String.format(
                            "MAT2 pending NQ side=%d nqTs=%d expiry=%d reason=%s confirm=%s",
                            nqBurst.side, nqBurst.tsNs, expiry, eval.reason, isConfirmMode()));
                }
            }
            signalIndicator.addPoint(0);
            return;
        }

        clearPendingNq("causal_fire");
        fireSignal(nqBurst.side, nqBurst.closePrice, eval.lagMs, nqState, false);
    }

    /**
     * Confirm mode: when ES burst closes, fire a pending same-side NQ burst
     * if ES arrived within maxEsLagMs of the NQ bar end.
     */
    private void tryPendingConfirm(Burst esBurst) {
        if (pendingNqBurst == null || esBurst == null) {
            return;
        }
        maybeExpirePending(esBurst.tsNs);
        if (pendingNqBurst == null) {
            return;
        }
        if (esBurst.side != pendingNqBurst.side) {
            return;
        }
        long lagMs = (esBurst.tsNs - pendingNqBurst.tsNs) / 1_000_000L;
        if (lagMs < 0) {
            return;
        }
        // Causal: only same bar-end stamp (ES closed after NQ for the same slot).
        // Confirm mode: allow NQ-first up to maxEsLagMs.
        if (!isConfirmMode() && lagMs > 0) {
            return;
        }
        double maxLag = maxEsLagMs == null ? 0.0 : maxEsLagMs;
        if (isConfirmMode() && maxLag > 0 && lagMs > maxLag) {
            clearPendingNq("es_lag_gt_max");
            return;
        }
        long syncNs = (long) ((syncMs == null ? 1500.0 : syncMs) * 1_000_000.0);
        if (Math.abs(esBurst.tsNs - pendingNqBurst.tsNs) > syncNs) {
            clearPendingNq("es_outside_sync");
            return;
        }

        InstrumentState nqState = findPrimaryNqState();
        if (nqState == null) {
            return;
        }
        int side = pendingNqBurst.side;
        long nqTs = pendingNqBurst.tsNs;
        double nqPx = pendingNqBurst.closePrice;
        // asof = ES confirm time (tradable, no look-ahead)
        long asof = esBurst.tsNs > 0 ? esBurst.tsNs : (nowNs > 0 ? nowNs : 0);
        EntryEval eval = evaluateEntryPostEsConfirm(side, nqTs, nqState, lagMs, asof);
        if (Boolean.TRUE.equals(verboseCandidateLog) || !eval.ok) {
            logCandidateConfirm(side, nqTs, nqPx, nqState, eval);
        }
        if (!eval.ok) {
            clearPendingNq("confirm_gate_fail:" + eval.reason);
            return;
        }
        clearPendingNq("confirm_fire");
        // Plan-required confirm stamp line (nqTs / esTs / lag) before full SIGNAL log
        Log.info(String.format(
                "MAT2 SIGNAL(confirm) nqTs=%d esTs=%d lagMs=%d side=%d",
                nqTs, esBurst.tsNs, lagMs, side));
        fireSignal(side, nqPx, lagMs, nqState, true);
    }

    private InstrumentState findPrimaryNqState() {
        if (primaryAlias == null) {
            return null;
        }
        InstrumentState st = byAlias.get(primaryAlias);
        return st != null && isPrimaryNq(st) ? st : null;
    }

    /**
     * Like {@link #evaluateEntry} but ES already confirmed (skip ES presence/lag).
     * Regime/trend/VWAP stamped at confirm time ({@code asof}).
     */
    private EntryEval evaluateEntryPostEsConfirm(
            int side, long nqTsNs, InstrumentState nqState, long lagMs, long asof) {
        if (nqState == null) {
            return new EntryEval(false, "no_burst", lagMs);
        }
        if (Boolean.TRUE.equals(requireCdPullback)) {
            boolean cdOk = side > 0 ? nqState.cd.isPullbackLong() : nqState.cd.isPullbackShort();
            if (!cdOk) {
                return new EntryEval(false, "cd_pullback_fail", lagMs);
            }
        }
        long coolNs = (long) ((signalCooldownSec == null ? 5.0 : signalCooldownSec) * 1_000_000_000.0);
        long refTs = asof > 0 ? asof : (nqTsNs > 0 ? nqTsNs : (nowNs > 0 ? nowNs : System.nanoTime()));
        if (refTs - lastSignalNs < coolNs) {
            return new EntryEval(false, "signal_cooldown", lagMs);
        }
        syncSideFilterFromParams("params");
        if (!MatSideFilter.allowSide(side)) {
            return new EntryEval(false, side > 0 ? "longs_disabled" : "shorts_disabled", lagMs);
        }
        int flatPos = flatCheckPosition();
        boolean flat = !Boolean.TRUE.equals(flatOnly) || (flatPos == 0 && !flatLocked);
        if (!flat) {
            return new EntryEval(false, "not_flat(pos=" + flatPos + ",locked=" + flatLocked + ")", lagMs);
        }
        EntryEval gated = applyRegimeTrendGates(side, nqState, lagMs, asof);
        if (!gated.ok) {
            return gated;
        }
        return new EntryEval(true, "ok", lagMs);
    }

    private void logCandidateConfirm(
            int side, long nqTsNs, double apiPx, InstrumentState nqState, EntryEval eval) {
        int rw = regimeWindowMin == null ? 60 : Math.max(1, regimeWindowMin.intValue());
        int tw = trendWindowMin == null ? 30 : Math.max(1, trendWindowMin.intValue());
        long asof = nowNs > 0 ? nowNs : nqTsNs;
        RegimeTracker.Snapshot sr = nqState.regime != null
                ? nqState.regime.snapshot(rw, asof) : RegimeTracker.Snapshot.empty();
        RegimeTracker.Snapshot st = nqState.regime != null
                ? nqState.regime.snapshot(tw, asof) : RegimeTracker.Snapshot.empty();
        String tag = eval.ok ? "MAT2 CANDIDATE PASS" : "MAT2 REJECT";
        Log.info(String.format(
                "%s reason=%s confirm_pending side=%d px=%.2f time=%s tsNs=%d lagMs=%s "
                        + "trendMode=%.0f reqVwap=%s maxRange=%.0f "
                        + "range%.0f=%.1f er=%.3f net%.0f=%.1f last=%.2f vwap=%.2f nBars=%d",
                tag,
                eval.reason,
                side,
                PriceTicks.toChartPoints(apiPx, nqState.instrumentPips),
                formatReplayTime(nqTsNs),
                nqTsNs,
                eval.lagMs >= Long.MAX_VALUE / 2 ? "none" : Long.toString(eval.lagMs),
                trendMode == null ? 0.0 : trendMode,
                Boolean.TRUE.equals(requireVwapSide),
                regimeMaxRangePts == null ? 0.0 : regimeMaxRangePts,
                (double) rw,
                sr.rangePts,
                sr.efficiencyRatio,
                (double) tw,
                st.netPts,
                sr.lastPx,
                st.vwap,
                st.nBars));
    }

    /** Replay-compare line: one per NQ burst when verbose. */
    private void logCandidate(Burst nqBurst, InstrumentState nqState, EntryEval eval) {
        rejectLogNs = nqBurst.tsNs;
        int rw = regimeWindowMin == null ? 60 : Math.max(1, regimeWindowMin.intValue());
        int tw = trendWindowMin == null ? 30 : Math.max(1, trendWindowMin.intValue());
        long asof = nqBurst.tsNs > 0 ? nqBurst.tsNs : nowNs;
        RegimeTracker.Snapshot sr = nqState.regime != null
                ? nqState.regime.snapshot(rw, asof) : RegimeTracker.Snapshot.empty();
        RegimeTracker.Snapshot st = nqState.regime != null
                ? nqState.regime.snapshot(tw, asof) : RegimeTracker.Snapshot.empty();
        String tag = eval.ok ? "MAT2 CANDIDATE PASS" : "MAT2 REJECT";
        Log.info(String.format(
                "%s reason=%s nqBurst#%d side=%d px=%.2f time=%s tsNs=%d lagMs=%s "
                        + "trendMode=%.0f reqVwap=%s maxRange=%.0f "
                        + "range%.0f=%.1f er=%.3f net%.0f=%.1f last=%.2f vwap=%.2f nBars=%d "
                        + "esTrades=%d esBursts=%d nqTrades=%d aliases=%d flatPos=%d locked=%s",
                tag,
                eval.reason,
                nqBurstCount,
                nqBurst.side,
                PriceTicks.toChartPoints(nqBurst.closePrice, nqState.instrumentPips),
                formatReplayTime(nqBurst.tsNs),
                nqBurst.tsNs,
                eval.lagMs >= Long.MAX_VALUE / 2 ? "none" : Long.toString(eval.lagMs),
                trendMode == null ? 0.0 : trendMode,
                Boolean.TRUE.equals(requireVwapSide),
                regimeMaxRangePts == null ? 0.0 : regimeMaxRangePts,
                (double) rw,
                sr.rangePts,
                sr.efficiencyRatio,
                (double) tw,
                st.netPts,
                sr.lastPx,
                st.vwap,
                st.nBars,
                esTradeCount,
                esBurstCount,
                nqTradeCount,
                byAlias.size(),
                flatCheckPosition(),
                flatLocked));
    }

    private static final class EntryEval {
        final boolean ok;
        final String reason;
        final long lagMs;

        EntryEval(boolean ok, String reason, long lagMs) {
            this.ok = ok;
            this.reason = reason;
            this.lagMs = lagMs;
        }
    }

    /**
     * Shared entry gate for signals, reverse-if-OK, and TP-extend.
     * @param ignoreFlat when true, skip flat-only / flatLocked (in-trade checks)
     */
    private EntryEval evaluateEntry(int side, Burst nqBurst, InstrumentState nqState, boolean ignoreFlat) {
        if (nqBurst == null || nqState == null) {
            return new EntryEval(false, "no_burst", Long.MAX_VALUE);
        }
        if (nqBurst.side != side) {
            return new EntryEval(false, "burst_wrong_side", Long.MAX_VALUE);
        }

        InstrumentState es = findByRole("ES");
        long syncNs = (long) ((syncMs == null ? 1500.0 : syncMs) * 1_000_000.0);
        long lagMs = es == null ? Long.MAX_VALUE : es.burst.lagMsTo(nqBurst.side, nqBurst.tsNs);
        boolean esPresent = es != null;
        boolean esRecent = esPresent && es.burst.hasRecentBurst(nqBurst.side, nqBurst.tsNs, syncNs);
        // Same 100ms slot still open on ES (NQ closed first): peek like research offline pair
        if (esPresent && !esRecent) {
            Burst esPeek = es.burst.peekOpenBarAsBurst(nqBurst.tsNs);
            if (esPeek != null && esPeek.side == side) {
                esRecent = true;
                lagMs = 0;
                if (Boolean.TRUE.equals(verboseCandidateLog)) {
                    Log.info(String.format(
                            "MAT2 ES peek same-slot side=%d n=%d vol=%.0f imb=%.2f endTs=%d",
                            esPeek.side, esPeek.nTrades, esPeek.totalVol, esPeek.imbalance,
                            esPeek.tsNs));
                }
            }
        }
        double maxLag = maxEsLagMs == null ? 0.0 : maxEsLagMs;
        boolean lagOk = maxLag <= 0 || lagMs <= maxLag;

        if (!esPresent) {
            return new EntryEval(false, "no_ES_instrument", lagMs);
        }
        if (!esRecent) {
            return new EntryEval(false, "ES_no_same_side_burst_in_sync", lagMs);
        }
        if (!lagOk) {
            return new EntryEval(false, "ES_lag_gt_max", lagMs);
        }

        if (Boolean.TRUE.equals(requireCdPullback)) {
            boolean cdOk = side > 0 ? nqState.cd.isPullbackLong() : nqState.cd.isPullbackShort();
            if (!cdOk) {
                return new EntryEval(false, "cd_pullback_fail", lagMs);
            }
        }

        long coolNs = (long) ((signalCooldownSec == null ? 5.0 : signalCooldownSec) * 1_000_000_000.0);
        long refTs = nqBurst.tsNs > 0 ? nqBurst.tsNs : (nowNs > 0 ? nowNs : System.nanoTime());
        if (refTs - lastSignalNs < coolNs) {
            return new EntryEval(false, "signal_cooldown", lagMs);
        }

        syncSideFilterFromParams("params");
        if (!MatSideFilter.allowSide(side)) {
            return new EntryEval(false, side > 0 ? "longs_disabled" : "shorts_disabled", lagMs);
        }

        if (!ignoreFlat) {
            int flatPos = flatCheckPosition();
            boolean flat = !Boolean.TRUE.equals(flatOnly) || (flatPos == 0 && !flatLocked);
            if (!flat) {
                return new EntryEval(false, "not_flat(pos=" + flatPos + ",locked=" + flatLocked + ")", lagMs);
            }
        }

        // EXP-0034 regime / trend / VWAP gates (asof = burst bar end — no future data)
        long asof = nqBurst.tsNs > 0 ? nqBurst.tsNs : (nowNs > 0 ? nowNs : 0);
        EntryEval gated = applyRegimeTrendGates(side, nqState, lagMs, asof);
        if (!gated.ok) {
            return gated;
        }
        return new EntryEval(true, "ok", lagMs);
    }

    private void maybeMarkVwapSession(InstrumentState state, long tsNs) {
        if (state.vwapSessionMarked || state.regime == null) {
            return;
        }
        int openMin = vwapSessionOpenUkMin == null ? 870 : vwapSessionOpenUkMin.intValue();
        if (openMin <= 0) {
            state.regime.markSessionOpen(tsNs);
            if (state.rotation != null) {
                state.rotation.reset();
            }
            state.vwapSessionMarked = true;
            Log.info("MAT2 session open marked (first trade); rotation reset");
            return;
        }
        long sec = tsNs / 1_000_000_000L;
        long nano = tsNs % 1_000_000_000L;
        ZonedDateTime zdt = Instant.ofEpochSecond(sec, nano).atZone(UK);
        int mod = zdt.getHour() * 60 + zdt.getMinute();
        if (mod >= openMin) {
            state.regime.markSessionOpen(tsNs);
            if (state.rotation != null) {
                state.rotation.reset();
            }
            state.vwapSessionMarked = true;
            Log.info("MAT2 UK session open marked mod=" + mod
                    + "; rotation reset (UK-window only, prior up="
                    + (state.rotation != null ? state.rotation.priorUpAvgPts() : Double.NaN)
                    + " down="
                    + (state.rotation != null ? state.rotation.priorDownAvgPts() : Double.NaN)
                    + ")");
        }
    }

    private EntryEval applyRegimeTrendGates(int side, InstrumentState nqState, long lagMs, long asofNs) {
        if (nqState.regime == null) {
            return new EntryEval(true, "ok", lagMs);
        }
        // Never use System.nanoTime as asof — it is not exchange epoch time.
        long asof = asofNs > 0 ? asofNs : (nowNs > 0 ? nowNs : 0);
        if (asof <= 0) {
            return new EntryEval(true, "ok", lagMs); // no clock yet — do not false-reject
        }
        int rw = regimeWindowMin == null ? 60 : Math.max(1, regimeWindowMin.intValue());
        int tw = trendWindowMin == null ? 30 : Math.max(1, trendWindowMin.intValue());
        RegimeTracker.Snapshot sr = nqState.regime.snapshot(rw, asof);
        RegimeTracker.Snapshot st = nqState.regime.snapshot(tw, asof);
        double maxRange = regimeMaxRangePts == null ? 0.0 : regimeMaxRangePts;
        double maxEr = regimeMaxEr == null ? 0.0 : regimeMaxEr;
        int hotAction = regimeHotAction == null ? 0 : regimeHotAction.intValue();
        boolean hot = (maxRange > 0 && sr.rangePts > maxRange)
                || (maxEr > 0 && sr.efficiencyRatio > maxEr);
        if (hot) {
            if (hotAction <= 0) {
                return new EntryEval(false, "regime_hot_skip", lagMs);
            }
            // counter-trend only when hot
            if (Math.abs(st.netPts) > 1e-9) {
                int trendSign = st.netPts > 0 ? 1 : -1;
                if (trendSign == side) {
                    return new EntryEval(false, "regime_hot_not_counter", lagMs);
                }
            }
        }
        int mode = trendMode == null ? 0 : trendMode.intValue();
        if (mode != 0 && Math.abs(st.netPts) > 1e-9) {
            int trendSign = st.netPts > 0 ? 1 : -1;
            if (mode == 1 && trendSign != side) {
                return new EntryEval(false, "trend_mode_fail", lagMs);
            }
            if (mode == 2 && trendSign == side) {
                return new EntryEval(false, "trend_mode_fail", lagMs);
            }
        }
        if (Boolean.TRUE.equals(requireVwapSide)
                && !Double.isNaN(st.vwap)
                && !Double.isNaN(sr.lastPx)) {
            if (side > 0 && sr.lastPx < st.vwap) {
                return new EntryEval(false, "vwap_side_fail", lagMs);
            }
            if (side < 0 && sr.lastPx > st.vwap) {
                return new EntryEval(false, "vwap_side_fail", lagMs);
            }
        }
        // EXP-0075 / MAT075: rot_fresh ∩ vwap_near (defaults ON)
        if (Boolean.TRUE.equals(requireRotFresh) && nqState.rotation != null) {
            double maxRatio = rotFreshMaxRatio == null ? 0.90 : rotFreshMaxRatio;
            SessionRotationTracker.Snapshot rot = nqState.rotation.snapshot(asof);
            boolean fresh = nqState.rotation.isRotFresh(asof, side, maxRatio);
            if (!fresh || Boolean.TRUE.equals(verboseCandidateLog)) {
                boolean withLeg = side != 0 && side == rot.rotLegDir;
                Log.info(String.format(
                        "MAT2 ROT side=%d ready=%s fromPrior=%s ratio=%.3f max=%.2f withLeg=%s "
                                + "ext=%.1f avg=%.1f leg=%d nSame=%d pivots=%d rots=%d fresh=%s",
                        side,
                        rot.rotReady,
                        rot.fromPrior,
                        rot.rotRatio,
                        maxRatio,
                        withLeg,
                        rot.extensionPts,
                        rot.avgPts,
                        rot.rotLegDir,
                        rot.nSameDir,
                        rot.nPivots,
                        rot.nRots,
                        fresh));
            }
            if (!fresh) {
                return new EntryEval(false, "rot_fresh_fail", lagMs);
            }
        }
        if (Boolean.TRUE.equals(requireVwapNear)
                && !Double.isNaN(st.vwap)
                && !Double.isNaN(sr.lastPx)) {
            // same-side of VWAP (research same_side_vwap)
            boolean sameSide = (side > 0 && sr.lastPx >= st.vwap)
                    || (side < 0 && sr.lastPx <= st.vwap);
            int vw = volWindowMin == null ? 30 : Math.max(1, volWindowMin.intValue());
            // Session-scoped med: research tapes often start at UK open; global
            // last-30 would dilute with quiet pre-open bars from the live feed.
            RegimeTracker.Snapshot sv = nqState.regime.snapshotSession(asof, vw);
            double med = sv.medianMinuteRange;
            double maxNorm = vwapNearMaxNorm == null ? 0.5 : vwapNearMaxNorm;
            double norm = (med > 1e-9) ? Math.abs(sr.lastPx - st.vwap) / med : Double.NaN;
            boolean nearOk = sameSide && Double.isFinite(norm) && norm <= maxNorm;
            if (!nearOk || Boolean.TRUE.equals(verboseCandidateLog)) {
                Log.info(String.format(
                        "MAT2 VWAP_NEAR side=%d sameSide=%s last=%.2f vwap=%.2f med=%.2f "
                                + "norm=%.3f max=%.2f nBarsSess=%d ok=%s",
                        side, sameSide, sr.lastPx, st.vwap, med, norm, maxNorm, sv.nBars, nearOk));
            }
            if (!nearOk) {
                return new EntryEval(false, "vwap_near_fail", lagMs);
            }
        }
        return new EntryEval(true, "ok", lagMs);
    }

    private double resolveBracketPts(InstrumentState nqState) {
        double baseTp = takeProfitPts == null ? 10.0 : takeProfitPts;
        if (!Boolean.TRUE.equals(volScaledBrackets) || nqState == null || nqState.regime == null) {
            return baseTp;
        }
        int vw = volWindowMin == null ? 30 : Math.max(1, volWindowMin.intValue());
        long asof = nowNs > 0 ? nowNs : 0;
        if (asof <= 0) {
            return baseTp;
        }
        double k = volMultipleK == null ? 1.0 : volMultipleK;
        double floor = bracketMinPts == null ? 6.0 : bracketMinPts;
        double cap = bracketMaxPts == null ? 30.0 : bracketMaxPts;

        int openMin = vwapSessionOpenUkMin == null ? 870 : vwapSessionOpenUkMin.intValue();
        int openPeriod = openVolPeriodMin == null ? 20 : Math.max(1, openVolPeriodMin.intValue());
        boolean openAnchor = Boolean.TRUE.equals(enableOpenVolAnchor)
                && openMin > 0
                && MatOpenVolAnchor.inOpenPeriod(asof, openMin, openPeriod);

        double med;
        String volSrc;
        if (openAnchor) {
            ensurePriorVolLoaded();
            if (Double.isFinite(priorVolMedian) && priorVolMedian > 0) {
                med = priorVolMedian;
                volSrc = "prior";
            } else {
                // No prior session file — fixed open brackets (user fallback).
                double fixed = openFixedBracketPts == null ? 10.0 : openFixedBracketPts;
                double out = Math.max(floor, Math.min(cap, fixed));
                Log.info(String.format(
                        "MAT2 BRACKET volSrc=fixed med=n/a → %.2f (open period, no prior)", out));
                return out;
            }
        } else {
            // Session-scoped med (UK open+): matches research when the tape
            // effectively starts at open; avoids quiet pre-open diluting size.
            RegimeTracker.Snapshot sv = nqState.regime.snapshotSession(asof, vw);
            med = sv.medianMinuteRange;
            volSrc = "liveSess(n=" + sv.nBars + ")";
            if (!(med > 0)) {
                // Fallback: global rolling window if session not marked yet
                sv = nqState.regime.snapshot(vw, asof);
                med = sv.medianMinuteRange;
                volSrc = "liveGlobal(n=" + sv.nBars + ")";
            }
        }
        double raw = k * Math.max(med, 0.25);
        double out = Math.max(floor, Math.min(cap, raw));
        if (Boolean.TRUE.equals(verboseCandidateLog)) {
            Log.info(String.format(
                    "MAT2 BRACKET volSrc=%s med=%.2f k=%.2f → %.2f (clip %.0f–%.0f)",
                    volSrc, med, k, out, floor, cap));
        }
        return out;
    }

    private void ensurePriorVolLoaded() {
        if (priorVolLoaded) {
            return;
        }
        priorVolMedian = MatOpenVolAnchor.loadPriorMedian();
        priorVolLoaded = true;
        if (Double.isFinite(priorVolMedian)) {
            Log.info(String.format("MAT4 open-vol: loaded prior median=%.2f from %s",
                    priorVolMedian, MatOpenVolAnchor.storePath()));
        } else {
            Log.info("MAT4 open-vol: no prior median; open period will use fixed brackets");
        }
    }

    /** Persist today's post-open session median for tomorrow's open anchor. */
    private void maybePersistPriorVol(InstrumentState nqState) {
        if (!Boolean.TRUE.equals(enableOpenVolAnchor) || nqState == null || nqState.regime == null) {
            return;
        }
        long asof = nowNs > 0 ? nowNs : 0;
        if (asof <= 0) {
            return;
        }
        // Throttle: at most once per minute of exchange time
        if (lastPriorPersistNs > 0 && asof - lastPriorPersistNs < 60_000_000_000L) {
            return;
        }
        int openMin = vwapSessionOpenUkMin == null ? 870 : vwapSessionOpenUkMin.intValue();
        int openPeriod = openVolPeriodMin == null ? 20 : Math.max(1, openVolPeriodMin.intValue());
        if (openMin > 0 && MatOpenVolAnchor.inOpenPeriod(asof, openMin, openPeriod)) {
            return; // still in open — do not overwrite prior with thin tape
        }
        int minBars = volWindowMin == null ? 30 : Math.max(15, volWindowMin.intValue() / 2);
        int n = nqState.regime.sessionBarsCount(asof);
        if (n < minBars) {
            return;
        }
        double med = nqState.regime.medianRangeSinceSessionOpen(asof, minBars);
        if (!Double.isFinite(med) || med <= 0) {
            return;
        }
        MatOpenVolAnchor.savePriorMedian(med, n);
        lastPriorPersistNs = asof;
    }

    /** Entry check using the latest NQ burst of the requested side (for reverse / extend). */
    private EntryEval evaluateEntryFromLastBurst(int side, boolean ignoreFlat) {
        InstrumentState nq = byAlias.get(primaryAlias);
        if (nq == null || nq.burst == null) {
            return new EntryEval(false, "no_nq", Long.MAX_VALUE);
        }
        Burst last = nq.burst.getLastBurst();
        if (last == null || last.side != side) {
            return new EntryEval(false, "no_same_side_burst", Long.MAX_VALUE);
        }
        long ageNs = (nowNs > 0 ? nowNs : System.nanoTime()) - last.tsNs;
        long syncNs = (long) ((syncMs == null ? 1500.0 : syncMs) * 1_000_000.0);
        if (ageNs > syncNs) {
            return new EntryEval(false, "burst_stale", Long.MAX_VALUE);
        }
        return evaluateEntry(side, last, nq, ignoreFlat);
    }

    private void clearTradeContext(String via) {
        openSide = 0;
        entryFillPx = 0;
        tpOrderId = null;
        slOrderId = null;
        currentTpPx = 0;
        tpExtendCount = 0;
        tpExtendArmed = true;
        if (!"pending_reverse".equals(via)) {
            pendingReverse = false;
            pendingReverseSide = 0;
        }
        Log.info("MAT2 trade context cleared (" + via + ")");
    }

    /** Position used for flat-only: StatusInfo if set, else net fills. */
    private int flatCheckPosition() {
        return position != 0 ? position : netFills;
    }

    private void fireSignal(int side, double apiPrice, long esLagMs, InstrumentState nqState) {
        fireSignal(side, apiPrice, esLagMs, nqState, false);
    }

    private void fireSignal(
            int side, double apiPrice, long esLagMs, InstrumentState nqState, boolean confirm) {
        lastSignalNs = nowNs > 0 ? nowNs : System.nanoTime();
        lastSignalSide = side;
        signalThisInterval = true;
        signalIndicator.addPoint(side);
        clearPendingNq("signal_fired");

        double chartPrice = PriceTicks.toChartPoints(apiPrice, pips);
        if (side > 0) {
            longMarker.addPoint(chartPrice);
            shortMarker.addPoint(Double.NaN);
        } else {
            shortMarker.addPoint(chartPrice);
            longMarker.addPoint(Double.NaN);
        }

        boolean wantTrade = Boolean.TRUE.equals(enableTrading);
        boolean willTrade = wantTrade && realtimeStarted && api != null;
        int rw = regimeWindowMin == null ? 60 : Math.max(1, regimeWindowMin.intValue());
        RegimeTracker.Snapshot sr = nqState.regime != null
                ? nqState.regime.snapshot(rw, lastSignalNs)
                : RegimeTracker.Snapshot.empty();
        double brkt = resolveBracketPts(nqState);
        String sigTag = confirm ? "MAT2 SIGNAL(confirm)" : "MAT2 SIGNAL";
        Log.info(String.format(
                "%s %s @%.2f time=%s tsNs=%d apiPx=%.0f esLagMs=%d cdTrend=%d cdPB=%s "
                        + "range%.0f=%.1f er=%.3f net30=%.1f vwap=%.2f brkt=%.1f "
                        + "trading=%s realtime=%s pos=%d sideFilter=%s account=%s",
                sigTag,
                side > 0 ? "LONG" : "SHORT",
                chartPrice,
                formatReplayTime(lastSignalNs),
                lastSignalNs,
                apiPrice,
                esLagMs,
                nqState.cd.getMaTrend(),
                side > 0 ? nqState.cd.isPullbackLong() : nqState.cd.isPullbackShort(),
                (double) rw,
                sr.rangePts,
                sr.efficiencyRatio,
                nqState.regime != null
                        ? nqState.regime.snapshot(
                                trendWindowMin == null ? 30 : Math.max(1, trendWindowMin.intValue()),
                                lastSignalNs).netPts
                        : Double.NaN,
                sr.vwap,
                brkt,
                wantTrade,
                realtimeStarted,
                flatCheckPosition(),
                sideFilterLabel(),
                tradingAccountId == null ? "null" : tradingAccountId));

        if (willTrade) {
            sendBracketOrder(side > 0, chartPrice, false);
        } else if (wantTrade && !realtimeStarted) {
            Log.info("MAT2 ORDER skipped (catchup/history — waiting for realtime)");
        }
    }

    private String sideFilterLabel() {
        return MatSideFilter.label();
    }

    /**
     * Push panel Allow longs/shorts into {@link MatSideFilter} when they change.
     * Hotkeys update the filter directly; unchanged panel values do not overwrite them.
     */
    private void syncSideFilterFromParams(String via) {
        Boolean L = allowLongs;
        Boolean S = allowShorts;
        if (Objects.equals(lastSyncedAllowLongs, L) && Objects.equals(lastSyncedAllowShorts, S)) {
            return;
        }
        lastSyncedAllowLongs = L;
        lastSyncedAllowShorts = S;
        MatSideFilter.set(Boolean.TRUE.equals(L), Boolean.TRUE.equals(S), via);
    }

    @Override
    public void onRealtimeStart() {
        realtimeStarted = true;
        // Allow a fresh entry right after catchup; detectors stay warm.
        lastSignalNs = 0;
        flatLocked = false;
        clearTradeContext("realtime_start");
        refreshTradingAccountId("realtime");
        Log.info(String.format(
                "MAT2 realtime start — orders %s side=%s account=%s (pos=%d). "
                        + "trendMode=%.0f reqVwap=%s rotFresh=%s vwapNear=%s maxRange=%.0f verbose=%s. "
                        + "Play/forward from here for live signals. Compare MAT2 CANDIDATE/SIGNAL "
                        + "lines to results/MAT2_REF_021026.md",
                Boolean.TRUE.equals(enableTrading) ? "ENABLED" : "disabled (signal-only)",
                sideFilterLabel(),
                tradingAccountId == null ? "null(primary)" : tradingAccountId,
                position,
                trendMode == null ? 0.0 : trendMode,
                Boolean.TRUE.equals(requireVwapSide),
                Boolean.TRUE.equals(requireRotFresh),
                Boolean.TRUE.equals(requireVwapNear),
                regimeMaxRangePts == null ? 0.0 : regimeMaxRangePts,
                Boolean.TRUE.equals(verboseCandidateLog)));
    }

    private static String formatReplayTime(long tsNs) {
        if (tsNs <= 0) {
            return "unknown";
        }
        try {
            long sec = tsNs / 1_000_000_000L;
            long nano = tsNs % 1_000_000_000L;
            ZonedDateTime zdt = Instant.ofEpochSecond(sec, nano).atZone(UK);
            return TIME_FMT.format(zdt);
        } catch (Exception e) {
            return "ns=" + tsNs;
        }
    }

    /** Market-close open position (no brackets). Invoked from MAT Side console. */
    private void flattenPosition() {
        int pos = flatCheckPosition();
        int size = Math.abs(pos);
        if (size <= 0) {
            size = Math.abs(netFills);
        }
        if (size <= 0) {
            Log.info("MAT2 FLATTEN: already flat (pos=" + position + " netFills=" + netFills + ")");
            return;
        }
        if (api == null || primaryAlias == null) {
            Log.info("MAT2 FLATTEN: no api/alias");
            return;
        }
        if (!Boolean.TRUE.equals(enableTrading)) {
            Log.info("MAT2 FLATTEN: trading disabled — side filter already NONE; enable trading to market-close");
            return;
        }
        boolean isBuy = pos < 0; // cover short with buy, long with sell
        refreshTradingAccountId("flatten");
        flatLocked = true;
        pendingReverse = false;
        try {
            SimpleOrderSendParametersBuilder builder = new SimpleOrderSendParametersBuilder(
                    primaryAlias,
                    isBuy,
                    size,
                    OrderDuration.GTC,
                    null,
                    Double.NaN,
                    Double.NaN,
                    0,
                    0,
                    0,
                    0,
                    false);
            if (tradingAccountId != null && !tradingAccountId.isEmpty()) {
                builder.setAccountId(tradingAccountId);
            }
            api.sendOrder(builder.build());
            Log.info(String.format(
                    "MAT2 FLATTEN sent %s size=%d (close pos=%d netFills=%d) account=%s",
                    isBuy ? "BUY" : "SELL",
                    size,
                    pos,
                    netFills,
                    tradingAccountId == null ? "null(primary)" : tradingAccountId));
        } catch (Throwable t) {
            Log.info("MAT2 FLATTEN failed: " + t);
        }
    }

    private void sendBracketOrder(boolean isBuy, double entryPricePoints, boolean force) {
        int size = Math.max(1, orderSize == null ? 1 : orderSize.intValue());
        InstrumentState nq = byAlias.get(primaryAlias);
        double tpPts = resolveBracketPts(nq);
        double slPts = tpPts; // symmetric; fixed SL param ignored when vol-scaled
        if (!Boolean.TRUE.equals(volScaledBrackets)) {
            slPts = stopLossPts == null ? 10.0 : stopLossPts;
            tpPts = takeProfitPts == null ? 10.0 : takeProfitPts;
        }
        liveBracketPts = tpPts;
        double pip = pips > 0 ? pips : 0.25;

        // Bookmap brackets use tick offsets from fill (not absolute chart prices).
        // AbsoluteBracketsValuesOrderSendParameters was arriving with offset=0 and
        // never spawned TP/SL children in Replay SIM.
        boolean brackets = Boolean.TRUE.equals(useBrackets);
        int tpTicks = brackets ? Math.max(1, (int) Math.round(tpPts / pip)) : 0;
        int slTicks = brackets ? Math.max(1, (int) Math.round(slPts / pip)) : 0;
        double tpPx = isBuy ? entryPricePoints + tpPts : entryPricePoints - tpPts;
        double slPx = isBuy ? entryPricePoints - slPts : entryPricePoints + slPts;

        // Lock flat-only BEFORE send so same-thread / fast replay cannot pyramid
        // while PositionListener still reports 0. force=true for stop-reverse.
        if (!force && Boolean.TRUE.equals(flatOnly) && (flatCheckPosition() != 0 || flatLocked)) {
            Log.info("MAT2 ORDER blocked flat-only pos=" + position
                    + " netFills=" + netFills + " locked=" + flatLocked);
            return;
        }
        flatLocked = true;
        refreshTradingAccountId("order");

        try {
            // clientId null → Bookmap generates one. Account from dropdown when known.
            SimpleOrderSendParametersBuilder builder = new SimpleOrderSendParametersBuilder(
                    primaryAlias,
                    isBuy,
                    size,
                    OrderDuration.GTC,
                    null,
                    Double.NaN,
                    Double.NaN,
                    tpTicks,
                    slTicks,
                    0,
                    0,
                    false);
            if (tradingAccountId != null && !tradingAccountId.isEmpty()) {
                builder.setAccountId(tradingAccountId);
            }
            SimpleOrderSendParameters order = builder.build();
            api.sendOrder(order);
            // Expected TP for extend tracking until working LMT arrives
            openSide = isBuy ? 1 : -1;
            entryFillPx = entryPricePoints;
            currentTpPx = tpPx;
            tpOrderId = null;
            slOrderId = null;
            tpExtendCount = 0;
            tpExtendArmed = true;
            Log.info(String.format(
                    "MAT2 ORDER sent %s size=%d entry~%.2f brackets=%s tp=%d ticks (~%.2f pts @%.2f) "
                            + "sl=%d ticks (~%.2f pts @%.2f) account=%s locked=%s netFills=%d force=%s time=%s",
                    isBuy ? "BUY" : "SELL",
                    size,
                    entryPricePoints,
                    brackets,
                    tpTicks,
                    tpPts,
                    tpPx,
                    slTicks,
                    slPts,
                    slPx,
                    tradingAccountId == null ? "null(primary)" : tradingAccountId,
                    flatLocked,
                    netFills,
                    force,
                    formatReplayTime(nowNs)));
        } catch (Throwable t) {
            flatLocked = false;
            Log.info("MAT2 ORDER failed: " + t);
        }
    }

    private InstrumentState findByRole(String role) {
        for (InstrumentState s : byAlias.values()) {
            if (role.equals(s.role)) {
                return s;
            }
        }
        return null;
    }

    @Override
    public long getInterval() {
        return Intervals.INTERVAL_100_MILLISECONDS;
    }

    @Override
    public void onInterval() {
        // Bottom signal is a spike series — return to 0 between fires.
        // Do NOT NaN-out PRIMARY markers here: that erased dots within ~100ms
        // so the chart looked like "no signals" even when MAT SIGNAL logged.
        if (!signalThisInterval) {
            signalIndicator.addPoint(0);
        }
        signalThisInterval = false;

        InstrumentState nq = byAlias.get(primaryAlias);
        InstrumentState es = findByRole("ES");
        if (nq != null) {
            cdTrendIndicator.addPoint(nq.cd.getMaTrend());
        }
        if (es != null && nq != null && nq.burst.getLastBurst() != null) {
            long lag = es.burst.lagMsTo(nq.burst.getLastBurst().side, nowNs);
            esLagIndicator.addPoint(lag >= Long.MAX_VALUE / 2 ? -1.0 : lag);
        }
        if (nq != null) {
            maybePersistPriorVol(nq);
        }
    }

    @Override
    public void onPositionUpdate(StatusInfo statusInfo) {
        if (statusInfo == null) {
            return;
        }
        // Ignore other accounts when we are targeting a specific one.
        if (tradingAccountId != null
                && statusInfo.accountId != null
                && !tradingAccountId.equals(statusInfo.accountId)) {
            return;
        }
        position = statusInfo.position;
        if (statusInfo.position == 0
                && statusInfo.workingBuys == 0
                && statusInfo.workingSells == 0) {
            flatLocked = false;
            netFills = 0;
            if (openSide != 0 && !pendingReverse) {
                clearTradeContext("flat");
            }
            tryPendingReverse();
        } else if (statusInfo.position != 0
                || statusInfo.workingBuys > 0
                || statusInfo.workingSells > 0) {
            flatLocked = true;
            if (statusInfo.position != 0) {
                netFills = statusInfo.position;
            }
        }
        Log.info(String.format(
                "MAT2 POS update pos=%d avg=%.2f wb=%d ws=%d unreal=%.2f locked=%s openSide=%d account=%s",
                statusInfo.position,
                statusInfo.averagePrice,
                statusInfo.workingBuys,
                statusInfo.workingSells,
                statusInfo.unrealizedPnl,
                flatLocked,
                openSide,
                statusInfo.accountId));
    }

    @Override
    public void onOrderUpdated(OrderInfoUpdate order) {
        if (order == null || order.orderId == null) {
            return;
        }
        accountListManager.onOrderUpdated(order);
        if (primaryAlias != null && order.instrumentAlias != null
                && !primaryAlias.equals(order.instrumentAlias)) {
            return;
        }
        if (tradingAccountId != null
                && order.accountId != null
                && !tradingAccountId.equals(order.accountId)) {
            return;
        }

        trackBracketLeg(order);

        int prevFilled = filledByOrderId.getOrDefault(order.orderId, 0);
        int filled = order.filled;
        if (filled <= prevFilled) {
            filledByOrderId.put(order.orderId, Math.max(prevFilled, filled));
            return;
        }
        int delta = filled - prevFilled;
        filledByOrderId.put(order.orderId, filled);
        netFills += order.isBuy ? delta : -delta;
        if (netFills == 0 && position == 0) {
            flatLocked = false;
        } else {
            flatLocked = true;
        }
        Log.info(String.format(
                "MAT2 FILL %s order=%s type=%s delta=%d filled=%d netFills=%d statusPos=%d locked=%s account=%s",
                order.isBuy ? "BUY" : "SELL",
                order.orderId,
                order.type,
                delta,
                filled,
                netFills,
                position,
                flatLocked,
                order.accountId));

        boolean isStopFill = order.type == OrderType.STP || order.type == OrderType.STP_LMT
                || (slOrderId != null && slOrderId.equals(order.orderId));
        boolean isTpFill = order.type == OrderType.LMT
                && tpOrderId != null && tpOrderId.equals(order.orderId);

        if (isStopFill && openSide != 0) {
            onStopFilled(order);
        } else if (isTpFill || (netFills == 0 && position == 0 && openSide != 0 && !pendingReverse)) {
            clearTradeContext(isTpFill ? "tp_fill" : "flat_fill");
        }
        tryPendingReverse();
    }

    private void trackBracketLeg(OrderInfoUpdate order) {
        if (order.status == OrderStatus.CANCELLED || order.status == OrderStatus.REJECTED) {
            return;
        }
        OrderType type = order.type;
        if (type == null) {
            return;
        }

        // Entry: market (or leading) fill sets open side if still flat context
        if (openSide == 0 && order.filled > 0 && (type == OrderType.MKT || order.fakeMarketOrder)) {
            openSide = order.isBuy ? 1 : -1;
            entryFillPx = PriceTicks.toChartPoints(order.averageFillPrice, pips);
            double tpPts = takeProfitPts == null ? 10.0 : takeProfitPts;
            currentTpPx = openSide > 0 ? entryFillPx + tpPts : entryFillPx - tpPts;
            tpExtendCount = 0;
            tpExtendArmed = true;
            Log.info(String.format(
                    "MAT2 trade open %s entry~%.2f expectedTp~%.2f",
                    openSide > 0 ? "LONG" : "SHORT", entryFillPx, currentTpPx));
            return;
        }

        if (openSide == 0) {
            return;
        }

        // Exit legs are opposite the position
        boolean exitIsBuy = openSide < 0;
        if (order.isBuy != exitIsBuy) {
            return;
        }

        if (type == OrderType.LMT && (order.status == OrderStatus.WORKING
                || order.status == OrderStatus.PENDING_SUBMIT
                || order.status == OrderStatus.PENDING_MODIFY
                || order.unfilled > 0)) {
            tpOrderId = order.orderId;
            if (!Double.isNaN(order.limitPrice) && order.limitPrice != 0) {
                currentTpPx = PriceTicks.toChartPoints(order.limitPrice, pips);
            }
            Log.info("MAT2 TP leg id=" + tpOrderId + " px~" + currentTpPx);
        } else if ((type == OrderType.STP || type == OrderType.STP_LMT)
                && (order.status == OrderStatus.WORKING
                || order.status == OrderStatus.PENDING_SUBMIT
                || order.status == OrderStatus.PENDING_MODIFY
                || order.unfilled > 0)) {
            slOrderId = order.orderId;
            Log.info("MAT2 SL leg id=" + slOrderId
                    + " stop~" + PriceTicks.toChartPoints(order.stopPrice, pips));
        }
    }

    private void onStopFilled(OrderInfoUpdate order) {
        int closedSide = openSide;
        Log.info(String.format(
                "MAT2 STOP filled order=%s closedSide=%s reverseOn=%s onlyIfOk=%s",
                order.orderId,
                closedSide > 0 ? "LONG" : (closedSide < 0 ? "SHORT" : "FLAT"),
                Boolean.TRUE.equals(reverseOnStop),
                Boolean.TRUE.equals(reverseOnlyIfEntryOk)));

        if (!Boolean.TRUE.equals(reverseOnStop) || closedSide == 0) {
            clearTradeContext("stop_fill");
            return;
        }

        int reverseSide = -closedSide;
        syncSideFilterFromParams("params");
        if (!MatSideFilter.allowSide(reverseSide)) {
            Log.info("MAT2 REVERSE skipped side_filter " + MatSideFilter.label());
            clearTradeContext("stop_no_reverse");
            return;
        }

        if (Boolean.TRUE.equals(reverseOnlyIfEntryOk)) {
            EntryEval eval = evaluateEntryFromLastBurst(reverseSide, true);
            if (!eval.ok) {
                Log.info("MAT2 REVERSE skipped entry_not_ok reason=" + eval.reason);
                clearTradeContext("stop_no_reverse");
                return;
            }
        }

        pendingReverse = true;
        pendingReverseSide = reverseSide;
        clearTradeContext("pending_reverse");
        Log.info("MAT2 REVERSE armed side=" + (reverseSide > 0 ? "LONG" : "SHORT"));
    }

    private void tryPendingReverse() {
        if (!pendingReverse) {
            return;
        }
        if (!Boolean.TRUE.equals(enableTrading) || !realtimeStarted || api == null) {
            Log.info("MAT2 REVERSE deferred trading=" + enableTrading + " realtime=" + realtimeStarted);
            return;
        }
        if (flatCheckPosition() != 0) {
            return;
        }
        int side = pendingReverseSide;
        pendingReverse = false;
        pendingReverseSide = 0;

        syncSideFilterFromParams("params");
        if (!MatSideFilter.allowSide(side)) {
            Log.info("MAT2 REVERSE aborted side_filter " + MatSideFilter.label());
            return;
        }

        InstrumentState nq = byAlias.get(primaryAlias);
        double px = nq != null
                ? PriceTicks.toChartPoints(nq.lastPrice, pips)
                : entryFillPx;
        if (px <= 0 && nq != null) {
            px = PriceTicks.toChartPoints(nq.lastPrice, nq.instrumentPips);
        }
        Log.info("MAT2 REVERSE send " + (side > 0 ? "LONG" : "SHORT") + " @~" + px);
        lastSignalNs = nowNs > 0 ? nowNs : System.nanoTime();
        lastSignalSide = side;
        sendBracketOrder(side > 0, px, true);
    }

    /**
     * Favourability for TP-extend: recent same-side NQ burst + ES sync.
     * Deliberately does NOT reuse entry gates (cooldown / flat / CD / regime) —
     * those made extends almost never fire (burst must be ≤ syncMs old).
     */
    private EntryEval stillFavourableForExtend(int side) {
        InstrumentState nq = byAlias.get(primaryAlias);
        if (nq == null || nq.burst == null) {
            return new EntryEval(false, "no_nq", Long.MAX_VALUE);
        }
        Burst last = nq.burst.getLastBurst();
        if (last == null || last.side != side) {
            return new EntryEval(false, "no_same_side_burst", Long.MAX_VALUE);
        }
        long now = nowNs > 0 ? nowNs : System.nanoTime();
        long ageNs = now - last.tsNs;
        double lookbackSec = tpExtendLookbackSec == null ? 8.0 : tpExtendLookbackSec;
        long lookbackNs = (long) (Math.max(0.5, lookbackSec) * 1_000_000_000.0);
        if (ageNs < 0 || ageNs > lookbackNs) {
            return new EntryEval(false, "burst_stale", ageNs / 1_000_000L);
        }
        InstrumentState es = findByRole("ES");
        long syncNs = (long) ((syncMs == null ? 1500.0 : syncMs) * 1_000_000.0);
        if (es == null) {
            return new EntryEval(false, "no_ES_instrument", Long.MAX_VALUE);
        }
        long lagMs = es.burst.lagMsTo(side, now);
        if (!es.burst.hasRecentBurst(side, now, syncNs)) {
            return new EntryEval(false, "ES_no_same_side_burst_in_sync", lagMs);
        }
        return new EntryEval(true, "ok", lagMs);
    }

    private void maybeExtendTp(InstrumentState nqState) {
        if (!Boolean.TRUE.equals(extendTpIfFavourable)
                || !Boolean.TRUE.equals(useBrackets)
                || openSide == 0
                || api == null
                || !realtimeStarted) {
            return;
        }
        // Allow tracking expected TP before working LMT id is known; apply when present.
        if (currentTpPx == 0) {
            return;
        }

        double proximity = tpExtendProximityPts == null ? 2.0 : tpExtendProximityPts;
        if (proximity < 0) {
            proximity = 0;
        }
        int maxExt = maxTpExtends == null ? 3 : maxTpExtends.intValue();
        if (maxExt > 0 && tpExtendCount >= maxExt) {
            return;
        }

        double lastPx = PriceTicks.toChartPoints(nqState.lastPrice, pips);
        double dist = Math.abs(lastPx - currentTpPx);

        // Clear debounce when price backs away
        if (dist > proximity * 1.5) {
            tpExtendArmed = true;
            return;
        }
        if (!tpExtendArmed || dist > proximity) {
            return;
        }

        // Must still be approaching TP, not already through it
        boolean throughTp = openSide > 0 ? lastPx >= currentTpPx : lastPx <= currentTpPx;
        if (throughTp) {
            Log.info("MAT2 TP EXTEND skip through_tp last=" + lastPx + " tp=" + currentTpPx);
            return;
        }

        EntryEval eval = stillFavourableForExtend(openSide);
        if (!eval.ok) {
            Log.info("MAT2 TP EXTEND skip " + eval.reason + " dist=" + String.format("%.2f", dist));
            return;
        }

        double tpPts = liveBracketPts > 0
                ? liveBracketPts
                : (takeProfitPts == null ? 10.0 : takeProfitPts);
        double newTp = openSide > 0 ? currentTpPx + tpPts : currentTpPx - tpPts;
        if (tpOrderId == null) {
            // Track expected TP; apply updateOrder once working LMT arrives
            currentTpPx = newTp;
            tpExtendCount++;
            tpExtendArmed = false;
            Log.info(String.format(
                    "MAT2 TP EXTEND #%d (pending LMT id) %s oldDist=%.2f newTp=%.2f last=%.2f",
                    tpExtendCount,
                    openSide > 0 ? "LONG" : "SHORT",
                    dist,
                    newTp,
                    lastPx));
            return;
        }
        try {
            OrderMoveParameters move = new OrderMoveParameters.Builder(tpOrderId)
                    .setOrderEntryType(OrderEntryType.AUTO)
                    .setStopPrice(Double.NaN)
                    .setLimitPrice(newTp)
                    .build();
            api.updateOrder(move);
            tpExtendCount++;
            currentTpPx = newTp;
            tpExtendArmed = false;
            Log.info(String.format(
                    "MAT2 TP EXTEND #%d %s oldDist=%.2f newTp=%.2f last=%.2f reason=favourable lagMs=%s",
                    tpExtendCount,
                    openSide > 0 ? "LONG" : "SHORT",
                    dist,
                    newTp,
                    lastPx,
                    eval.lagMs >= Long.MAX_VALUE / 2 ? "n/a" : Long.toString(eval.lagMs)));
        } catch (Throwable t) {
            Log.info("MAT2 TP EXTEND failed: " + t);
        }
    }

    @Override
    public void onOrderExecuted(velox.api.layer1.data.ExecutionInfo executionInfo) {
        // Position synced via onOrderUpdated / onPositionUpdate.
    }

    @Override
    public void stop() {
        MatTradeControl.unregister(flattenAction);
        if (sideHotkeysInstalled) {
            MatSideHotkeys.uninstall();
            sideHotkeysInstalled = false;
        }
        if (provider != null && adminListener != null) {
            try {
                provider.removeListener(adminListener);
            } catch (Throwable t) {
                Log.info("MAT2 account listener remove failed: " + t);
            }
        }
        // Persist prior-rot only after a real-time session with enough structure.
        // Catchup/replay re-attach was overwriting the seeded prior with a
        // half-built avg and causing rot_fresh false rejects on the next run.
        if (realtimeStarted) {
            for (InstrumentState s : byAlias.values()) {
                if (s == null || s.rotation == null || !"NQ".equals(s.role)) {
                    continue;
                }
                if (s.rotation.rotationCount() < 4) {
                    continue;
                }
                double up = s.rotation.sessionAvgPts(1);
                double down = s.rotation.sessionAvgPts(-1);
                if ((Double.isFinite(up) && up > 0) || (Double.isFinite(down) && down > 0)) {
                    MatPriorRotAnchor.save(up, down);
                }
            }
        }
        byAlias.clear();
        Log.info("Momentum AutoTrade v5 stopped. lastSignalSide=" + lastSignalSide
                + " sideFilter=" + sideFilterLabel());
    }
}
